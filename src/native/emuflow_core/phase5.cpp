#include "emuflow_core/phase5.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/schedule_store.hpp"
#include "emuflow_core/schedule_validation.hpp"
#include "emuflow_core/schedule_timing.hpp"
#include "emuflow_core/store.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <memory>
#include <unistd.h>
#include <optional>
#include <set>
#include <tuple>
#include <limits>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool value,const std::string& message) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::string Hex(Sha256& hash) {
  std::string result;const char* digits="0123456789abcdef";
  for(auto c:hash.finish()){result+=digits[c>>4];result+=digits[c&15];}return result;
}
std::string HashText(const std::string& value) {
  Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());return Hex(hash);
}
std::string HashFile(const fs::path& path) {
  std::ifstream stream(path,std::ios::binary);Check(bool(stream),"cannot seal Phase 5 member");
  Sha256 hash;std::array<char,65536> buffer;
  while(stream){stream.read(buffer.data(),buffer.size());hash.update(reinterpret_cast<const unsigned char*>(buffer.data()),stream.gcount());}
  Check(stream.eof(),"cannot read Phase 5 member");return Hex(hash);
}
std::string ReadSmall(const fs::path& path) {
  const auto size=fs::file_size(path);Check(size<=8*1024*1024,"Phase 5 control document exceeds bound");
  std::ifstream stream(path,std::ios::binary);std::string result(size,'\0');stream.read(result.data(),size);
  Check(bool(stream),"cannot read Phase 5 control document");(void)JsonQuote(result);return result;
}
// Bounded reports/configuration only. No route, hop or path population is decoded.
JsonRecord Record(SqliteDatabase& db,const std::string& text,unsigned depth=0) {
  Check(depth<=64 && text.size()<=8*1024*1024,"Phase 5 control JSON exceeds bound");
  auto type=db.Prepare("SELECT json_valid(?1),json_type(?1),json_extract(?1,'$')");type.BindText(1,text);
  Check(type.Step() && type.Integer(0),"invalid Phase 5 control JSON");const auto kind=type.TypedText(1);
  if(kind=="null")return JsonRecord();
  if(kind=="true" || kind=="false")return JsonRecord(kind=="true");
  if(kind=="text")return JsonRecord(type.TypedText(2));
  if(kind=="integer")return JsonRecord(type.TypedInteger(2));
  if(kind=="real")return JsonRecord(ParseFiniteDecimal(text,"Phase 5 control number"));
  Check(kind=="object" || kind=="array","invalid Phase 5 control JSON type");
  auto rows=db.Prepare("SELECT key,CASE WHEN type IN ('object','array') THEN value WHEN type IN ('true','false','null') THEN type ELSE json_quote(value) END FROM json_each(?1)");rows.BindText(1,text);
  Object object;Array array;std::size_t count=0;
  while(rows.Step()) {
    Check(++count<=16384,"Phase 5 control collection exceeds bound");
    auto value=Record(db,kind=="object" ? JsonMemberToken(db,text,rows.TypedText(0)):rows.TypedText(1),depth+1);
    if(kind=="object")Check(object.emplace(rows.TypedText(0),std::move(value)).second,"duplicate Phase 5 control key");
    else array.emplace_back(std::move(value));
  }
  return kind=="object" ? JsonRecord(std::move(object)):JsonRecord(std::move(array));
}
Object Fields(SqliteDatabase& db,const std::string& text) {
  auto rows=db.Prepare("SELECT key,CASE WHEN type IN ('object','array') THEN value WHEN type IN ('true','false','null') THEN type ELSE json_quote(value) END FROM json_each(?1)");rows.BindText(1,text);
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,text);
  Check(valid.Step() && valid.Integer(0) && valid.Text(1)=="object","Phase 5 expected control object");
  Object result;
  while(rows.Step())Check(result.emplace(rows.TypedText(0),Record(db,JsonMemberToken(db,text,rows.TypedText(0)))).second,"duplicate Phase 5 control key");
  return result;
}
std::string Field(SqliteDatabase& db,const std::string& text,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,text);row.BindText(2,path);
  Check(row.Step() && !row.IsNull(0),"missing Phase 5 control field "+path);
  return row.Text(1);
}
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"missing Phase 5 metadata");auto result=row.TypedText(0);Check(!row.Step(),"duplicate Phase 5 metadata");return result;
}
struct Workspace {
  fs::path path;bool retain=false;
  explicit Workspace(const fs::path& parent) {
    std::string name=(parent/".phase5-generation-XXXXXX").string();
    Check(mkdtemp(name.data())!=nullptr,"cannot create Phase 5 generation workspace");path=name;
  }
  ~Workspace(){if(!retain){std::error_code error;fs::remove_all(path,error);}}
};
class Request {
 public:
  Request(const fs::path& path,const std::vector<ArtifactRef>& sources,bool checked=true):writer_(path.string(),sources),checked_(checked){}
  template<class T>void Put(T value){writer_.Write(value);UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(&value),sizeof(value));}
  void Text(const std::string& value){Check(value.size()<=UINT32_MAX,"Phase 5 request string exceeds bound");Put<std::uint32_t>(value.size());writer_.WriteBytes(value.data(),value.size());UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(value.data()),value.size());}
  void Generation(const FileGeneration& g){Put(g.device);Put(g.inode);Put(g.bytes);Put(g.modified_seconds);Put(g.modified_nanoseconds);Put(g.changed_seconds);Put(g.changed_nanoseconds);}
  void Finish(){if(checked_)writer_.Write(crc_);writer_.Commit();}
 private:AtomicBinaryWriter writer_;std::uint32_t crc_=0;bool checked_;
};
void WriteJson(const fs::path& path,const JsonRecord& record,const std::vector<ArtifactRef>& sources) {
  PublishTextArtifact(path.string(),sources,[&](std::ostream& stream){stream<<record.EncodeUtf8()<<'\n';});
}
void RequireSources(const std::vector<ArtifactRef>& sources){for(const auto& source:sources)source.RequireUnchanged();}
// Report last. Hard-linked recovery generations survive a rollback failure.
void Commit(Workspace& workspace,const fs::path& root,const std::vector<std::string>& names,const std::vector<ArtifactRef>& sources,const Phase5Progress& progress) {
  struct Member {fs::path target,backup;std::unique_ptr<StagedArtifact> staged;bool changed=false;};
  std::vector<Member> members;fs::create_directory(workspace.path/"recovery");
  for(const auto& name:names) {
    Check(fs::path(name).filename()==name,"Phase 5 member name escapes output");
    auto target=root/name;
    Check(!fs::is_symlink(target) && (!fs::exists(target) || fs::is_regular_file(target)),"invalid Phase 5 member destination");
    for(const auto& source:sources){std::error_code error;Check(fs::weakly_canonical(target)!=source.path() && !fs::equivalent(target,source.path(),error),"Phase 5 member aliases input");}
    members.push_back({target,{},std::make_unique<StagedArtifact>(target.string()),false});
    auto& member=members.back();fs::rename(workspace.path/name,member.staged->path());
    if(fs::exists(target)){member.backup=workspace.path/"recovery"/name;fs::create_hard_link(target,member.backup);}
  }
  try {
    RequireSources(sources);
    for(auto& member:members){member.changed=true;PublishTemporaryFile(member.staged->path(),member.target.string(),sources);if(progress)progress("published:"+member.target.filename().string());}
    RequireSources(sources);
  } catch(...) {
    try {
      for(auto i=members.rbegin();i!=members.rend();++i)if(i->changed){if(i->backup.empty())fs::remove(i->target);else {StagedArtifact restored(i->target.string());fs::rename(i->backup,restored.path());PublishTemporaryFile(restored.path(),i->target.string());}}
    } catch(...) {workspace.retain=true;throw StageError(ErrorCode::kPublicationFailed,"Phase 5 rollback failed; recovery retained at "+workspace.path.string());}
    throw;
  }
}
std::uint64_t Unsigned(SqliteDatabase& db,const std::string& document,const std::string& path,std::uint64_t maximum=UINT32_MAX) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,document);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="integer","Phase 5 requires integer "+path);
  const auto value=row.TypedInteger(1);Check(value>=0 && std::uint64_t(value)<=maximum,"Phase 5 integer out of range "+path);return value;
}
std::string String(SqliteDatabase& db,const std::string& document,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,document);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="text","Phase 5 requires string "+path);const auto value=row.TypedText(1);
  Check(!value.empty() && value.find('\0')==std::string::npos,"empty/NUL Phase 5 identity");return value;
}
bool Present(SqliteDatabase& db,const std::string& document,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2)");row.BindText(1,document);row.BindText(2,path);
  return row.Step() && !row.IsNull(0) && row.Text(0)!="null";
}
std::set<std::string> Strings(SqliteDatabase& db,const std::string& document,const std::string& path) {
  auto valid=db.Prepare("SELECT json_type(?1,?2)");valid.BindText(1,document);valid.BindText(2,path);
  Check(valid.Step() && valid.Text(0)=="array","Phase 5 requires array "+path);
  auto rows=db.Prepare("SELECT type,value FROM json_each(?1,?2)");rows.BindText(1,document);rows.BindText(2,path);
  std::set<std::string> result;
  while(rows.Step())Check(rows.Text(0)=="text" && !rows.Text(1).empty() && result.insert(rows.TypedText(1)).second,"invalid Phase 5 identity catalog");return result;
}
std::optional<std::string> Member(SqliteDatabase& db,const std::string& object,const std::string& name) {
  auto row=db.Prepare("SELECT 1 FROM json_each(?1) WHERE key=?2");row.BindText(1,object);row.BindText(2,name);
  if(!row.Step())return {};return JsonMemberToken(db,object,name);
}
double Decimal(SqliteDatabase& db,const std::string& object,const std::string& name) {
  auto row=db.Prepare("SELECT type FROM json_each(?1) WHERE key=?2");row.BindText(1,object);row.BindText(2,name);
  Check(row.Step() && (row.Text(0)=="integer" || row.Text(0)=="real"),"Phase 5 requires finite number "+name);
  const auto value=ParseFiniteDecimal(JsonMemberToken(db,object,name),name);Check(value>=0,"negative Phase 5 board delay/frequency");return value;
}
struct PreparedArc {std::string link,from,to,capacity;std::uint32_t offset=0,budget=0,latency=0;bool affine=false;double base=0,slope=0,period=0;};
struct PreparedBoard {std::vector<std::string> fpgas,links,capacities;std::map<std::tuple<std::string,std::string,std::string>,PreparedArc> arcs;Object header;std::map<std::string,JsonRecord> domains;};
PreparedBoard PrepareBoard(SqliteDatabase& json,SqliteDatabase& routes,const std::string& board,const std::string& header) {
  Check(String(json,board,"$.schema")=="emuflow.boarddb/v1","unsupported Phase 5 BoardDB");
  const auto platform=String(json,board,"$.platform.name");Check(platform==String(json,header,"$.platform"),"Phase 5 board and routes disagree");
  const auto constraints=Field(json,header,"$.constraints");Check(String(json,constraints,"$.schema")=="emuflow.system-route-constraints/v1","Phase 5 requires normalized route constraints");
  const auto frame=Unsigned(json,constraints,"$.frame_slots"),minimum=Unsigned(json,constraints,"$.tdm_min_ratio"),maximum=Unsigned(json,constraints,"$.tdm_max_ratio"),quantum=Unsigned(json,constraints,"$.tdm_ratio_quantum");
  Check(frame>1 && minimum>0 && minimum<=maximum && maximum<=frame && quantum>0,"invalid Phase 5 ratio/frame domain");
  PreparedBoard result;std::set<std::string> fpga_set,link_set;
  auto fpga=json.Prepare("SELECT json_extract(value,'$.id'),json_type(value,'$.id') FROM json_each(?1,'$.fpgas')");fpga.BindText(1,board);
  while(fpga.Step())Check(fpga.Text(1)=="text" && !fpga.Text(0).empty() && fpga_set.insert(fpga.Text(0)).second,"invalid Phase 5 FPGA catalog");Check(!fpga_set.empty(),"empty Phase 5 FPGA catalog");
  result.fpgas.assign(fpga_set.begin(),fpga_set.end());
  const auto unavailable=Strings(json,constraints,"$.unavailable_links"),shared=Strings(json,constraints,"$.shared_capacity_links"),sll=Strings(json,constraints,"$.sll_links");
  std::map<std::tuple<std::string,std::string,std::string>,std::uint64_t> loads;
  auto counts=routes.Prepare("SELECT link_id,source_fpga,sink_fpga,count(*) FROM route_edges GROUP BY link_id,source_fpga,sink_fpga");
  while(counts.Step())loads.emplace(std::make_tuple(counts.TypedText(0),counts.TypedText(1),counts.TypedText(2)),counts.TypedInteger(3));
  Array allocations;std::map<std::tuple<std::string,std::string,std::string>,JsonRecord> allocation_records;
  std::optional<std::tuple<std::uint64_t,std::uint64_t,std::uint64_t>> ratio_domain;
  auto links=json.Prepare("SELECT type,value FROM json_each(?1,'$.links')");links.BindText(1,board);
  while(links.Step()) {
    Check(links.Text(0)=="object","invalid Phase 5 board link");const auto link=links.TypedText(1),id=String(json,link,"$.id"),direction=String(json,link,"$.direction"),mode=String(json,link,"$.mode");
    Check(link_set.insert(id).second && (direction=="unidirectional" || direction=="full_duplex" || direction=="half_duplex"),"invalid/duplicate Phase 5 link direction");
    Check(mode=="abstract" || mode=="parallel" || mode=="source_synchronous" || mode=="serial","invalid Phase 5 link mode");
    auto ends=json.Prepare("SELECT json_array_length(?1,'$.endpoints')");ends.BindText(1,link);Check(ends.Step() && ends.Integer(0)==2,"invalid Phase 5 link endpoints");
    const auto left=String(json,link,"$.endpoints[0]"),right=String(json,link,"$.endpoints[1]");Check(left!=right && fpga_set.count(left) && fpga_set.count(right),"foreign Phase 5 link endpoint");
    const auto lanes=Unsigned(json,link,"$.data_lanes_per_direction"),width=Present(json,link,"$.payload_bits_per_lane_per_cycle") ? Unsigned(json,link,"$.payload_bits_per_lane_per_cycle"):1;
    const auto sharing=String(json,link,"$.capacity_sharing");Check((sharing=="per_direction" || sharing=="shared_bidirectional") && (sharing!="shared_bidirectional" || direction=="full_duplex"),"invalid Phase 5 capacity sharing");
    Check(lanes>0 && width>0 && lanes<=UINT32_MAX/width && (mode=="serial" || width==1),"invalid Phase 5 transport width");const auto bits=lanes*width;
    if(sharing=="shared_bidirectional")Check(shared.count(id),"normalized route constraints omit shared board capacity");
    const auto frequency=Decimal(json,link,"fabric_clock_mhz");Check(frequency>0,"zero Phase 5 fabric clock");const auto period=1000.0/frequency;const auto latency=Unsigned(json,link,"$.latency_cycles");
    const bool affine=Present(json,link,"$.tdm_delay_model"),banks=Present(json,link,"$.bank_count");
    double base=double(latency)*1000.0/frequency,slope=period;
    if(affine) {
      const auto model=Field(json,link,"$.tdm_delay_model");Check(String(json,model,"$.model")=="linear-ratio-v1","unknown Phase 5 delay model");
      const auto lo=Unsigned(json,model,"$.min_ratio"),hi=Unsigned(json,model,"$.max_ratio"),q=Unsigned(json,model,"$.ratio_quantum");
      Check(lo>0 && hi>=lo && q>0 && lo%q==0 && hi%q==0 && minimum>=lo && maximum<=hi && quantum==q && minimum%q==0 && maximum%q==0,"Phase 5 ratio domain disagrees with BoardDB");
      const auto domain=std::make_tuple(lo,hi,q);Check(!ratio_domain || *ratio_domain==domain,"mixed Phase 5 BoardDB ratio domains");ratio_domain=domain;
      slope=Decimal(json,model,"ratio_coefficient_ns");base=Decimal(json,model,"intercept_ns")+slope;
    }
    std::uint64_t forward_banks=0,reverse_banks=0,wires=0,bank_count=0;
    const auto first=std::min(left,right),last=std::max(left,right);
    if(banks) {
      bank_count=Unsigned(json,link,"$.bank_count");wires=Unsigned(json,link,"$.wires_per_bank");
      Check(bank_count>0 && wires>0 && bank_count<=UINT32_MAX/wires && bank_count*wires==bits && sharing=="shared_bidirectional" && mode!="serial" && !sll.count(id),"invalid Phase 5 shared-bank board contract");
      const auto forward=loads[{id,first,last}],reverse=loads[{id,last,first}];
      const auto ratio=[&](std::uint64_t signals,std::uint64_t n)->std::optional<std::uint64_t> {
        if(!signals)return 1;if(!n)return {};const auto lanes=n*wires,raw=std::max(minimum,signals/lanes+(signals%lanes!=0));
        const auto value=raw==1?1:(raw/quantum+(raw%quantum!=0))*quantum;if(value>maximum)return {};return value;
      };
      using Objective=std::tuple<std::uint64_t,std::uint64_t,double,double,std::uint64_t>;std::optional<Objective> best;
      for(std::uint64_t f=0;f<=bank_count;++f) {
        const auto r=bank_count-f;const auto a=ratio(forward,f),b=ratio(reverse,r);if(!a || !b)continue;
        const double p=f?double(forward)/double(f*wires):0,q=r?double(reverse)/double(r*wires):0;
        const Objective objective{std::max(*a,*b),*a+*b,std::max(p,q),p+q,f};
        if(!best || objective<*best){best=objective;forward_banks=f;reverse_banks=r;}
      }
      Check(best.has_value(),"infeasible shared bank allocation for Phase 5 link "+id);
      for(bool reverse:{false,true}) {
        const auto from=reverse?last:first,to=reverse?first:last;const auto n=reverse?reverse_banks:forward_banks,offset=reverse?forward_banks*wires:0;
        allocation_records.emplace(std::make_tuple(id,from,to),Object{{"link",id},{"from",from},{"to",to},{"bank_count",n},{"wires_per_bank",wires},{"lane_offset",offset},{"lanes",n*wires}});
      }
    }
    if(unavailable.count(id))continue;
    for(bool reverse:{false,true}) {
      if(reverse && direction=="unidirectional")continue;
      const auto from=reverse?right:left,to=reverse?left:right;const bool pooled=!banks && (direction=="half_duplex" || shared.count(id));
      const auto capacity=id+(pooled?":shared":":"+from+"->"+to);double delay=base;
      // Fixed-model overrides follow directed, then link, then board latency.
      auto override=Member(json,Field(json,constraints,"$.link_delay_ns"),id);
      const auto directed=Member(json,Field(json,constraints,"$.directed_link_delay_ns"),id);
      if(directed){const auto source=Member(json,*directed,from);if(source){const auto sink=Member(json,*source,to);if(sink)override=sink;}}
      if(override) {
        const auto value=ParseFiniteDecimal(*override,"Phase 5 delay override");Check(value>=0,"negative Phase 5 delay override");
        if(!affine)delay=value;
        else {const auto model=Field(json,link,"$.tdm_delay_model");const auto reference=Decimal(json,model,"intercept_ns")+slope*Unsigned(json,model,"$.min_ratio");Check(std::abs(value-reference)<=std::max(1e-9,1e-9*std::max(std::abs(value),std::abs(reference))),"fixed override conflicts with BoardDB ratio delay");}
      }
      PreparedArc arc{id,from,to,capacity,0,static_cast<std::uint32_t>(bits),static_cast<std::uint32_t>(latency),affine,delay,slope,period};
      if(banks){const bool back=from==last;arc.budget=(back?reverse_banks:forward_banks)*wires;arc.offset=back?forward_banks*wires:0;}
      result.arcs.emplace(std::make_tuple(id,from,to),arc);
      result.domains.emplace(capacity,Object{{"key",capacity},{"link",id},{"direction",pooled?"shared":from+"->"+to},{"lanes",bits},{"frame_slots",frame},{"capacity_bit_hops",bits*frame}});
    }
  }
  Check(ratio_domain.has_value() || minimum==1 || minimum%quantum==0,"invalid fixed-model Phase 5 minimum ratio");
  for(const auto& catalog:{unavailable,shared,sll})for(const auto& id:catalog)Check(link_set.count(id),"unknown Phase 5 constraint link");
  for(const auto& item:loads)Check(result.arcs.count(item.first),"route uses unavailable/foreign Phase 5 arc");
  result.links.assign(link_set.begin(),link_set.end());for(const auto& item:result.domains)result.capacities.push_back(item.first);
  result.header={{"schema","emuflow.tdm-schedule/v1"},{"design",String(json,header,"$.design")},{"platform",platform},{"provider","deterministic-dependency-earliest-slot-v1"},{"transport_semantics","sampled-virtual-wire"},{"route_constraints",Record(json,constraints)},{"qualification","dependency-schedule-readiness-pass"},{"timing_constraints",Object{{"schema","emuflow.sampled-virtual-wire-timing-constraints/v1"},{"settle_slots",1},{"commit_slot",frame-1},{"slot_edge_convention","tx-sample-before-rx-shadow-update-v1"}}},{"semantic_contract_schema",String(json,header,"$.semantic_contract_schema")},{"semantic_contract_sha256",String(json,header,"$.semantic_contract_sha256")}};
  for(auto& item:allocation_records)allocations.push_back(std::move(item.second));if(!allocations.empty())result.header.emplace("bank_allocations",std::move(allocations));return result;
}
} // namespace

void RunPhase5(const std::vector<ArtifactRef>& inputs,const std::string& report_path,const Phase5Progress& progress) {
  Check(inputs.size()==4,"Phase 5 stage input coverage disagrees");
  const char* schemas[]={"system-route-store-v3","exact-schedule-board-request-v1","exact-schedule-generation-request-v1","phase5-stage-request-v1"};
  for(unsigned i=0;i<4;++i)Check(inputs[i].schema()==schemas[i],"Phase 5 typed input schema disagrees");
  RequireSources(inputs);const auto root=fs::absolute(report_path).parent_path();
  Check(fs::path(report_path).filename()=="phase5_report.json" && fs::is_directory(root),"Phase 5 requires its canonical report destination");
  std::vector<ArtifactRef> sources=inputs;
  CheckedRequestReader request(inputs[3].path(),256*1024);
  Check(request.Read<std::uint64_t>()==0x31535035464d5545ULL && request.Read<std::uint32_t>()==1,"unsupported Phase 5 stage request");
  const auto count=request.Read<std::uint32_t>();Check(count>0 && count<=64,"Phase 5 bound source coverage exceeds bound");
  bool route_bound=false;
  for(std::uint32_t i=0;i<count;++i){const auto path=request.ReadString32(false,16384),schema=request.ReadString32(false,256);const auto expected=ReadFileGeneration(request);auto source=ArtifactRef::Bind(path,schema);if(!(source.generation()==expected))throw StageError(ErrorCode::kMixedGeneration,"Phase 5 original input generation changed");route_bound|=source.path()==inputs[0].path() && source.schema()==inputs[0].schema();sources.push_back(std::move(source));}
  request.RequireEnd();Check(route_bound,"Phase 5 original route binding is missing");
  Workspace work(root);const auto store_path=work.path/"schedule.sqlite";
  const auto started=std::chrono::steady_clock::now();
  BuildExactScheduleStore({inputs[0],inputs[1],inputs[2]},store_path.string());
  if(progress)progress("scheduled");
  const auto schedule_source=ArtifactRef::Bind(store_path.string(),"tdm-schedule-store-v4");
  SqliteDatabase json(":memory:",SqliteDatabase::Mode::kCreateExclusive);
  std::string header_text,summary_text;
  {SqliteStore store(schedule_source);header_text=Metadata(store.database(),"header");summary_text=Metadata(store.database(),"native_publication_summary");}
  auto header=Fields(json,header_text);
  const auto logical=Field(json,summary_text,"$.logical_sha256"),pretty=Field(json,summary_text,"$.logical_pretty_sha256");
  const auto sections=Field(json,summary_text,"$.sections"),relations=Field(json,summary_text,"$.relation_counts");
  const auto store_hash=HashFile(store_path),name="schedule."+store_hash+".sqlite";
  const auto bytes=fs::file_size(store_path);
  const auto storage_seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-started).count();
  // The validator independently reconstructs legality/certificates from upstream
  // routes and board. Domain templates come from the original request, not output.
  std::vector<std::string> domains;
  {BinaryReader generation(inputs[2].path());ReadAndValidateHeader(generation,{0x3147534d44544545ULL,1,"exact schedule generation request"});
   for(unsigned i=0;i<7;++i)(void)generation.Read<std::uint64_t>();
   const auto headers=generation.Read<std::uint32_t>();Check(headers<=4096,"Phase 5 generation header exceeds bound");
   for(std::uint32_t i=0;i<headers;++i){generation.ReadString32();generation.ReadString32();generation.ReadString32();}
   const auto n=generation.Read<std::uint32_t>();Check(n<=4096,"Phase 5 board domain count exceeds bound");
   for(std::uint32_t i=0;i<n;++i){generation.ReadString32();domains.push_back(generation.ReadString32());generation.ReadString32();}generation.RequireEnd();}
  const auto validation_request=work.path/"validation.bin",validation_output=work.path/"validation.json";
  {Request out(validation_request,sources);out.Put<std::uint64_t>(0x3156534d44544545ULL);out.Put<std::uint32_t>(1);out.Generation(schedule_source.generation());out.Put<std::uint32_t>(header.size());
   for(const auto& item:header){out.Text(item.first);out.Text(item.second.Encode());}out.Put<std::uint32_t>(domains.size());for(const auto& domain:domains)out.Text(domain);
   for(const auto* key:{"demand_completions","domain_schedules","entries","routes"}){out.Put<std::uint64_t>(std::stoull(Field(json,sections,std::string("$.")+key+".records")));out.Text(Field(json,sections,std::string("$.")+key+".sha256"));}out.Text(logical);out.Text(pretty);out.Finish();}
  ValidateExactSchedule({inputs[0],inputs[1],schedule_source,ArtifactRef::Bind(validation_request.string(),"exact-schedule-validation-request-v1")},validation_output.string());
  auto validation=Fields(json,ReadSmall(validation_output));validation.erase("schema");
  const auto board=ReadExactScheduleBoard(inputs[0],inputs[1].path());
  const auto timing_request=work.path/"timing.bin",timing_output=work.path/"timing.json";
  std::string route_header,route_hash;
  {SqliteStore route(inputs[0]);route_header=Metadata(route.database(),"header");route_hash=Field(json,Metadata(route.database(),"logical_sha256"),"$");}
  auto timing_present=json.Prepare("SELECT json_type(?1,'$.timing')='object'");timing_present.BindText(1,route_header);Check(timing_present.Step(),"invalid route timing envelope");const bool analyze=timing_present.Integer(0);
  bool affine=false;for(const auto& arc:board.arcs)affine|=arc.second.affine;
  {Request out(timing_request,sources);out.Put<std::uint64_t>(0x31544354464d5545ULL);out.Put<std::uint32_t>(1);out.Put<std::uint32_t>(analyze?3:1);out.Generation(inputs[0].generation());out.Generation(schedule_source.generation());out.Text(route_hash);out.Text(logical);out.Text(pretty);out.Put<std::uint8_t>(analyze && affine);out.Finish();}
  std::vector<ArtifactRef> timing_inputs{inputs[0],schedule_source};if(analyze)timing_inputs.push_back(inputs[1]);timing_inputs.push_back(ArtifactRef::Bind(timing_request.string(),"phase5-timing-request-v1"));
  ProjectScheduleTiming(timing_inputs,timing_output.string());const auto timing_text=ReadSmall(timing_output);
  auto timing=Fields(json,timing_text);const auto contract_text=Field(json,timing_text,"$.contract");
  const auto contract=Record(json,contract_text);
  auto cross_validation=Fields(json,Field(json,contract_text,"$.metrics"));cross_validation.emplace("status","pass");
  Object manifest{{"schema","emuflow.tdm-schedule-storage/v4"},{"logical_schema","emuflow.tdm-schedule/v1"},{"artifact",name},{"application_id",0x454d5554},{"user_version",3},{"store_bytes",bytes},{"store_sha256",store_hash},{"logical_sha256",logical},{"logical_pretty_sha256",pretty},{"sections",Record(json,sections)},
    {"entry_relation",Object{{"schema","emuflow.tdm-schedule-entry-relation/v1"},{"records",Record(json,Field(json,sections,"$.entries.records"))}}},
    {"route_relation",Object{{"schema","emuflow.tdm-schedule-route-relation/v1"},{"records",Record(json,Field(json,sections,"$.routes.records"))},{"sink_records",Record(json,Field(json,relations,"$.route_sinks"))},{"edge_records",Record(json,Field(json,relations,"$.route_edges"))}}},
    {"completion_relation",Object{{"schema","emuflow.tdm-schedule-completion-relation/v1"},{"records",Record(json,Field(json,sections,"$.demand_completions.records"))},{"arrival_records",Record(json,Field(json,relations,"$.completion_arrivals"))}}}};
  WriteJson(work.path/"schedule.json",JsonRecord(manifest),sources);WriteJson(work.path/"cross_layer_timing.json",contract,sources);
  fs::rename(store_path,work.path/name);
  Object records;for(const auto* key:{"routes","entries","demand_completions","domain_schedules"})records.emplace(key,Record(json,Field(json,sections,std::string("$.")+key+".records")));
  Object report{{"schema","emuflow.phase5-report/v1"},{"phase",5},{"status","pass"},{"design",header.at("design")},{"platform",header.at("platform")},{"provider",header.at("provider")},{"validation",JsonRecord(validation)},{"cross_layer_timing_validation",JsonRecord(cross_validation)},{"timing_dataflow",timing.at("implementation_id")},{"phase_implementation","native-exact-phase5-v1"},
    {"artifacts",Object{{"schedule","schedule.json"},{"cross_layer_timing","cross_layer_timing.json"},{"report","phase5_report.json"},{"schedule_store",name}}},{"cut_mode","static-exact-combinational"},{"qualification","dependency-schedule-readiness-pass"},
    {"schedule_storage",Object{{"schema","emuflow.tdm-schedule-storage/v4"},{"implementation_id","native-exact-schedule-store-v1"},{"producer_implementation_id","native-exact-phase5-producer-v1"},{"dataflow","typed-sqlite-route-entry-completion-relations-v1"},{"wall_seconds",storage_seconds},{"wall_scope","native-model-schedule-certificate-publication-and-store-seal"},{"bytes",bytes},{"logical_sha256",logical},{"records",JsonRecord(records)}}}};
  if(analyze)report.emplace("timing_validation",timing.at("timing_validation"));
  Array members;const std::vector<std::string> names{name,"schedule.json","cross_layer_timing.json","phase5_report.json"};
  for(std::size_t i=0;i<3;++i)members.emplace_back(Object{{"path",names[i]},{"bytes",fs::file_size(work.path/names[i])},{"sha256",i?HashFile(work.path/names[i]):store_hash}});
  auto publish_names=names;
  if(fs::exists(root/name) || fs::is_symlink(root/name)) {
    Check(!fs::is_symlink(root/name),"Phase 5 immutable store destination is a symlink");
    auto existing=ArtifactRef::Bind((root/name).string(),"tdm-schedule-store-v4");
    Check(existing.generation().bytes==bytes && HashFile(root/name)==store_hash,"Phase 5 immutable store identity disagrees");
    existing.RequireUnchanged();sources.push_back(std::move(existing));
    // A previously published immutable owner must retain its inode/generation
    // for borrowers. Reuse it instead of replacing it with identical bytes.
    fs::remove(work.path/name);publish_names.erase(publish_names.begin());
  }
  Object generation{{"schema","emuflow.phase5-generation/v1"},{"commit_point","phase5_report.json"},{"members",JsonRecord(members)}};
  report.emplace("generation",JsonRecord(generation));generation.emplace("report_identity_sha256",HashText(JsonRecord(report).EncodeUtf8()));report["generation"]=JsonRecord(generation);
  WriteJson(work.path/"phase5_report.json",JsonRecord(report),sources);RequireSources(sources);Commit(work,root,publish_names,sources,progress);
}
void RunPhase5FromStores(const std::vector<ArtifactRef>& inputs,const std::string& report) {
  Check(inputs.size()==3 && inputs[0].schema()=="system-route-selector-v1" && inputs[1].schema()=="assignment-index-v4" && inputs[2].schema()=="boarddb-v1","Phase 5 requires current selector/assignment/BoardDB owners");
  RequireSources(inputs);SqliteDatabase json(":memory:",SqliteDatabase::Mode::kCreateExclusive);
  const auto selector=ReadSmall(inputs[0].path());(void)Fields(json,selector);
  Check(String(json,selector,"$.schema")=="emuflow.system-route-storage/v1" && String(json,selector,"$.logical_schema")=="emuflow.system-routes/v1" && Unsigned(json,selector,"$.user_version")==3 && Unsigned(json,selector,"$.application_id")==0x454d5552,"unsupported Phase 5 RouteStore selector");
  const auto hash=String(json,selector,"$.store_sha256"),artifact=String(json,selector,"$.artifact");
  Check(hash.size()==64 && hash.find_first_not_of("0123456789abcdef")==std::string::npos && artifact=="routes."+hash+".sqlite","invalid Phase 5 route artifact identity");
  auto routes=ArtifactRef::Bind((fs::path(inputs[0].path()).parent_path()/artifact).string(),"system-route-store-v3");
  Check(routes.generation().bytes==Unsigned(json,selector,"$.store_bytes",INT64_MAX) && HashFile(routes.path())==hash,"Phase 5 route file seal disagrees");routes.RequireUnchanged();
  std::vector<ArtifactRef> sources=inputs;sources.push_back(routes);
  SqliteStore route(routes),assignment(inputs[1]);auto& db=route.database();
  const auto header=Metadata(db,"header"),sections=Field(json,selector,"$.sections");
  RequireEquivalentJson(json,header,Field(json,selector,"$.header"),"Phase 5 route header seal disagrees");
  RequireEquivalentJson(json,Metadata(db,"sections"),sections,"Phase 5 route section seal disagrees");
  Check(Field(json,Metadata(db,"logical_sha256"),"$")==String(json,selector,"$.logical_sha256"),"Phase 5 route logical seal disagrees");
  const std::vector<std::pair<std::string,std::string>> tables={{"demands","demands"},{"routes","routes"},{"timing_paths","timing_paths"},{"route_master_selections","master_selections"},{"refinement_batch_paths","refinement_batch_paths"}};
  const std::vector<std::string> expected_columns={"seq","demand_id","net_id","source_fpga","width_bits","transport_round","cut_class","dependency_level","combinational_dependency_depth","max_latency_cycles","predicted_max_delay_ns"};
  auto columns=db.Prepare("PRAGMA table_info(routes)");std::size_t column=0;
  while(columns.Step())Check(column<expected_columns.size() && columns.TypedText(1)==expected_columns[column++],"unsupported Phase 5 typed route columns");Check(column==expected_columns.size(),"missing Phase 5 typed route columns");
  Check(Fields(json,sections).size()==tables.size(),"unknown Phase 5 route sections");
  for(const auto& item:tables){const auto section=Field(json,sections,"$."+item.second);const auto fields=Fields(json,section);Check(fields.size()==(item.second=="refinement_batch_paths"?3:2),"invalid Phase 5 route section descriptor");const auto digest=String(json,section,"$.sha256");Check(digest.size()==64 && digest.find_first_not_of("0123456789abcdef")==std::string::npos,"invalid Phase 5 route section digest");}
  for(const auto& item:tables){const auto n=Unsigned(json,sections,"$."+item.second+".records",INT64_MAX);auto row=db.Prepare("SELECT count(*),min(seq),max(seq) FROM "+item.first);Check(row.Step() && row.Integer(0)==std::int64_t(n) && (n ? row.Integer(1)==0 && row.Integer(2)==std::int64_t(n-1):row.IsNull(1) && row.IsNull(2)),"Phase 5 route sequence coverage disagrees");}
  auto orphan=db.Prepare("SELECT d.route_seq FROM demands d LEFT JOIN routes r ON r.seq=d.route_seq WHERE r.seq IS NULL OR r.demand_id!=d.demand_id OR d.seq!=d.route_seq UNION ALL SELECT t.seq FROM timing_paths t WHERE path_id IS NOT coalesce(json_extract(record_json,'$.path'),json_extract(record_json,'$.id')) UNION ALL SELECT s.seq FROM route_master_selections s LEFT JOIN routes r ON r.demand_id=s.demand_id WHERE r.seq IS NULL OR r.seq!=s.seq LIMIT 1");Check(!orphan.Step(),"Phase 5 route relation is malformed");
  auto batch=db.Prepare("SELECT batch_index FROM refinement_batch_paths WHERE batch_index<0 OR batch_index>=? OR path_index<0 OR path_index>=? UNION ALL SELECT batch_index FROM refinement_batch_paths GROUP BY batch_index HAVING min(position)!=0 OR max(position)!=count(*)-1 LIMIT 1");batch.BindInteger(1,Unsigned(json,sections,"$.refinement_batch_paths.batches",INT64_MAX));batch.BindInteger(2,Unsigned(json,sections,"$.timing_paths.records",INT64_MAX));Check(!batch.Step(),"Phase 5 route batch relation is malformed");
  Check(!Present(json,header,"$.semantic_contract"),"Phase 5 routes duplicate the semantic contract");
  const auto semantic=String(json,header,"$.semantic_contract_schema"),identity=String(json,header,"$.semantic_contract_sha256");
  Check(semantic=="emuflow.static-exact-combinational-cut/v3" && identity.size()==64 && identity.find_first_not_of("0123456789abcdef")==std::string::npos,"unsupported Phase 5 semantic contract");
  Check(Field(json,Metadata(assignment.database(),"semantic_contract_schema"),"$")==semantic && Field(json,Metadata(assignment.database(),"semantic_contract_sha256"),"$")==identity && Field(json,Metadata(assignment.database(),"design"),"$")==String(json,header,"$.design") && Field(json,Metadata(assignment.database(),"platform"),"$")==String(json,header,"$.platform"),"Phase 5 route/assignment identity disagrees");
  const auto board_document=ReadSmall(inputs[2].path());(void)Fields(json,board_document);const auto prepared=PrepareBoard(json,db,board_document,header);
  const auto root=fs::absolute(report).parent_path();fs::create_directories(root);Workspace work(root);
  const auto board_path=work.path/"board.bin",generation_path=work.path/"generation.bin",request_path=work.path/"stage.bin";
  const auto index=[](const std::vector<std::string>& names,const std::string& name){const auto found=std::lower_bound(names.begin(),names.end(),name);Check(found!=names.end() && *found==name,"unknown Phase 5 board ID");return std::uint32_t(found-names.begin());};
  const auto constraints=Field(json,header,"$.constraints");
  {Request out(board_path,sources,false);out.Put<std::uint64_t>(0x3142534d44544545ULL);out.Put<std::uint32_t>(1);out.Generation(routes.generation());for(auto v:{Unsigned(json,constraints,"$.frame_slots"),std::uint64_t(1),std::uint64_t(1),Unsigned(json,constraints,"$.tdm_min_ratio"),Unsigned(json,constraints,"$.tdm_max_ratio"),Unsigned(json,constraints,"$.tdm_ratio_quantum")})out.Put<std::uint32_t>(v);
   for(const auto& catalog:{prepared.fpgas,prepared.links,prepared.capacities}){out.Put<std::uint32_t>(catalog.size());for(const auto& name:catalog)out.Text(name);}out.Put<std::uint32_t>(prepared.arcs.size());
   for(const auto& item:prepared.arcs){const auto& a=item.second;for(auto v:{index(prepared.links,a.link),index(prepared.fpgas,a.from),index(prepared.fpgas,a.to),index(prepared.capacities,a.capacity),a.offset,a.budget,std::uint32_t(a.affine),a.latency})out.Put(v);out.Put(a.base);out.Put(a.slope);out.Put(a.period);}out.Finish();}
  {Request out(generation_path,sources,false);out.Put<std::uint64_t>(0x3147534d44544545ULL);out.Put<std::uint32_t>(1);out.Generation(routes.generation());out.Put<std::uint32_t>(prepared.header.size());for(const auto& item:prepared.header){out.Text(item.first);out.Text(item.second.Encode());out.Text(item.second.Encode(true));}out.Put<std::uint32_t>(prepared.domains.size());for(const auto& item:prepared.domains){out.Text(item.first);out.Text(item.second.Encode());out.Text(item.second.Encode(true));}out.Finish();}
  {Request out(request_path,sources);out.Put<std::uint64_t>(0x31535035464d5545ULL);out.Put<std::uint32_t>(1);out.Put<std::uint32_t>(sources.size());for(const auto& source:sources){out.Text(source.path());out.Text(source.schema());out.Generation(source.generation());}out.Finish();}
  RequireSources(sources);RunPhase5({routes,ArtifactRef::Bind(board_path.string(),"exact-schedule-board-request-v1"),ArtifactRef::Bind(generation_path.string(),"exact-schedule-generation-request-v1"),ArtifactRef::Bind(request_path.string(),"phase5-stage-request-v1")},report);RequireSources(sources);
}
std::string Phase5ReportControlReference(const ArtifactRef& report) {
  report.RequireUnchanged();SqliteDatabase db(":memory:",SqliteDatabase::Mode::kCreateExclusive);const auto text=ReadSmall(report.path());auto fields=Fields(db,text);
  Check(String(db,text,"$.schema")=="emuflow.phase5-report/v1" && String(db,text,"$.status")=="pass","invalid native Phase 5 report");
  auto generation=Fields(db,Field(db,text,"$.generation"));const auto identity=String(db,text,"$.generation.report_identity_sha256");generation.erase("report_identity_sha256");fields["generation"]=generation;Check(HashText(JsonRecord(fields).EncodeUtf8())==identity,"native Phase 5 report identity disagrees");
  const auto& g=report.generation();Array numbers{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};report.RequireUnchanged();
  return JsonRecord(Array{"phase-report",Object{{"schema","emuflow.phase-report-reference/v1"},{"path",report.path()},{"report_schema","emuflow.phase5-report/v1"},{"generation",std::move(numbers)}}}).EncodeUtf8();
}
} // namespace emuflow::core
