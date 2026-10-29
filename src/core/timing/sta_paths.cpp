#include "timing/sta_paths.hpp"
#include "storage/schema/sta_path_schema.hpp"
#include "common/exact_sum.hpp"
#include "storage/store.hpp"
#include "common/json_record.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <fstream>
#include <map>
#include <optional>
#include <set>

namespace emuflow::core {
namespace {
using I=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool valid,const std::string& reason) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"STA path database "+reason);
}
std::string Text(SqliteStatement& s,int col) {
  auto value=s.TypedText(col);Check(!value.empty(),"has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
std::string ReadRequest(const std::vector<ArtifactRef>& inputs) {
  Check(inputs.size()==3,"input coverage is invalid");CheckedRequestReader r(inputs[2].path(),256*1024);
  Check(r.Read<std::uint64_t>()==0x31505453464d5545ULL && r.Read<std::uint32_t>()==1,"request version is invalid");
  for(unsigned i=0;i<2;++i)if(!(ReadFileGeneration(r)==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"STA original source generation changed");
  const auto config=r.ReadString32(false,128*1024);r.RequireEnd();return config;
}
void AttachSource(SqliteDatabase& db,const ArtifactRef& source) {
  auto attach=db.Prepare("ATTACH DATABASE ? AS src");attach.BindText(1,SqliteImmutableUri(source.path()));attach.Execute();
  db.Execute("PRAGMA src.cache_size=-32768;PRAGMA cache_size=-8192;PRAGMA temp_store=FILE");
}
std::string Design(SqliteDatabase& db) {
  auto s=db.Prepare("SELECT json_type(value,'$.name'),json_extract(value,'$.name') FROM src.metadata WHERE key='design_json'");
  Check(s.Step() && s.Text(0)=="text","source design is missing");return Text(s,1);
}
std::vector<std::string> Split(const std::string& text,char separator) {
  std::vector<std::string> result;std::size_t start=0;
  for(;;){auto end=text.find(separator,start);result.push_back(text.substr(start,end==std::string::npos?end:end-start));if(end==std::string::npos)return result;start=end+1;}
}
bool Space(unsigned char c) {return c==' ' || c=='\t' || c=='\r' || c=='\n' || c=='\v' || c=='\f';}
std::string Hex(const std::string& text) {
  const auto digit=[](char c)->int {return c>='0' && c<='9'?c-'0':c>='a' && c<='f'?c-'a'+10:c>='A' && c<='F'?c-'A'+10:-1;};
  std::string value;
  for(std::size_t i=0;i<text.size();) {
    if(Space(text[i])){++i;continue;}Check(i+1<text.size(),"has invalid UTF-8 hex");const auto a=digit(text[i]),b=digit(text[i+1]);Check(a>=0 && b>=0,"has invalid UTF-8 hex");value+=char((a<<4)|b);i+=2;
  }
  (void)JsonQuoteUtf8(value);return value;
}
struct Endpoint {
  std::string object,port;std::optional<std::string> instance;I bit=0;
  bool operator==(const Endpoint& b)const{return object==b.object && port==b.port && instance==b.instance && bit==b.bit;}
};
class Resolver {
 public:
  explicit Resolver(SqliteDatabase& db):owner_(db.Prepare("SELECT seq FROM src.cell_records WHERE id=?")),pins_(db.Prepare("SELECT p.name,e.bit FROM src.endpoint_records e LEFT JOIN src.pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=? UNION ALL SELECT json_extract(k.value,'$.port'),json_extract(k.value,'$.bit') FROM src.cell_records c JOIN src.payloads p ON p.seq=c.constants_seq,json_each(p.value) k WHERE c.seq=?")) {
    auto rows=db.Prepare("SELECT json_extract(record_json,'$.id'),json_extract(record_json,'$.width') FROM src.port_records ORDER BY seq");
    while(rows.Step()){auto name=Text(rows,0);const auto width=rows.TypedInteger(1);Check(width>0 && ports_.emplace(name,width).second,"top port width/identity is invalid");}
  }
  std::optional<Endpoint> Get(const std::string& name) {
    auto found=cache_.find(name);if(found!=cache_.end())return found->second;
    auto value=Canonical(name);
    if(name.find("\\\\")!=std::string::npos) {
      std::string unescaped,escaped;
      for(std::size_t i=0;i<name.size();++i){unescaped+=name[i];if(name[i]=='\\' && i+1<name.size() && name[i+1]=='\\')++i;}
      for(char c:unescaped){escaped+=c;if(c=='\\')escaped+=c;}
      auto alias=Canonical(unescaped);
      if(alias && escaped==name){Check(!value || *value==*alias,"provider object alias is ambiguous");value=alias;}
    }
    // Bounded cache of reached provider objects, not a graph-wide identity map.
    if(cache_.size()>=8192 || bytes_+name.size()>8*1024*1024){cache_.clear();bytes_=0;}
    if(name.size()<65536){cache_.emplace(name,value);bytes_+=name.size()+(value?value->object.size()+value->port.size()+(value->instance?value->instance->size():0):0);}
    return value;
  }
 private:
  std::optional<Endpoint> Canonical(const std::string& name) {
    // Resolve only the reached scalar. Do not expand every port bit for every
    // timing endpoint, including endpoints that belong to internal cells.
    std::optional<Endpoint> port_endpoint;
    const auto candidate=[&](const std::string& port,const std::string& ordinal) {
      const auto found=ports_.find(port);if(found==ports_.end())return;
      I bit=0;
      if(!ordinal.empty()) {
        if(ordinal.size()>19 || (ordinal.size()>1 && ordinal[0]=='0'))return;
        for(char digit:ordinal){if(digit<'0' || digit>'9' || bit>(INT64_MAX-(digit-'0'))/10)return;bit=bit*10+(digit-'0');}
        if(found->second<=1 || bit>=found->second)return;
      } else if(found->second!=1)return;
      Endpoint value{name,port,std::nullopt,bit};
      Check(!port_endpoint || *port_endpoint==value,"top port scalar alias is ambiguous");port_endpoint=std::move(value);
    };
    candidate(name,"");
    if(!name.empty() && name.back()==']') {
      const auto at=name.rfind('[');
      if(at!=std::string::npos && at+2<name.size())candidate(name.substr(0,at),name.substr(at+1,name.size()-at-2));
    }
    const auto at=name.rfind("__");if(at!=std::string::npos && at+2<name.size())candidate(name.substr(0,at),name.substr(at+2));
    if(port_endpoint)return port_endpoint;
    const auto slash=name.rfind('/');if(slash==std::string::npos)return std::nullopt;const auto owner=name.substr(0,slash),scalar=name.substr(slash+1);
    owner_.Reset();owner_.BindText(1,owner);if(!owner_.Step())return std::nullopt;const auto seq=owner_.TypedInteger(0);
    pins_.Reset();pins_.BindInteger(1,seq);pins_.BindInteger(2,seq);std::map<std::string,I> widths;std::set<std::pair<std::string,I>> pins;
    while(pins_.Step()){const auto bit=pins_.TypedInteger(1);const auto pin=Text(pins_,0);Check(bit>=0,"source pin ordinal is invalid");pins.emplace(pin,bit);auto found=widths.find(pin);if(found==widths.end() || found->second<bit)widths[pin]=bit;}
    for(const auto& pin:pins)if(scalar==(widths.at(pin.first)==0?pin.first:pin.first+"__"+std::to_string(pin.second)))return Endpoint{name,pin.first,owner,pin.second};
    return std::nullopt;
  }
  SqliteStatement owner_,pins_;std::map<std::string,I> ports_;
  std::map<std::string,std::optional<Endpoint>> cache_;std::size_t bytes_=0;
};
std::pair<std::optional<Endpoint>,std::optional<Endpoint>> Endpoints(const std::string& id,Resolver& resolver) {
  if(id.size()<12 || id.find('\n')!=std::string::npos || id[id.size()-9]!='#')return {};
  for(std::size_t i=id.size()-8;i<id.size();++i)if(id[i]<'0' || id[i]>'9')return {};
  const auto identity=id.substr(0,id.size()-9);const auto arrow=identity.rfind("->");if(arrow==std::string::npos || arrow==0 || arrow+2==identity.size())return {};
  auto a=resolver.Get(identity.substr(0,arrow)),b=resolver.Get(identity.substr(arrow+2));if(!a || !b)return {};return {a,b};
}
struct Facts {
  I paths=0,structured=0,nets=0,failing=0,members=0,max_members=0;double worst=0,max_period=0,positive=-1,negative=0;ExactSum sum;Array clocks;
  Object Normalization()const{return {{"positive_slack_scale_ns",positive<=0?1.0:positive},{"negative_slack_scale_ns",negative==0?1.0:-negative},{"max_clock_period_ns",max_period}};}
  Object Qor()const{return {{"wns_ns",worst},{"tns_ns",sum.Finish()},{"failing_endpoints",failing},{"timed_endpoints",paths}};}
  Object Summary()const{return {{"paths",paths},{"structured_endpoint_paths",structured},{"unique_path_nets",nets},{"qor",Qor()}};}
  Object Validation(const std::string& design)const{return {{"status","pass"},{"design",design},{"provider","opensta-fpga-path-database-v1"},{"paths",paths},{"structured_endpoint_paths",structured},{"unresolved_endpoint_paths",paths-structured},{"clock_domains",clocks},{"unique_path_nets",nets},{"worst_slack_ns",worst},{"path_qor",Qor()}};}
};
std::array<double,3> Normalization(SqliteDatabase& db,const std::string& config) {
  std::array<double,3> result{};const char* fields[]={"positive_slack_scale_ns","negative_slack_scale_ns","max_clock_period_ns"};
  auto q=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
  for(unsigned i=0;i<3;++i){q.Reset();q.BindText(1,config);q.BindText(2,std::string("$.normalization.")+fields[i]);Check(q.Step() && (q.Text(0)=="real" || q.Text(0)=="integer"),"normalization is invalid");result[i]=q.TypedReal(1);Check(std::isfinite(result[i]) && result[i]>0,"normalization is invalid");}return result;
}
// Independent final-row audit. It consumes the canonical store rather than
// replaying parser counters, endpoint choices, or an in-memory path collection.
Facts Audit(SqliteDatabase& db,const std::optional<std::array<double,3>>& normalization={}) {
  RequireSqliteDDL(db,kStaPathStoreDDL);Facts facts;Resolver source_objects(db);
  auto paths=db.Prepare("SELECT p.*,a.seq,b.seq FROM paths p LEFT JOIN src.cell_records a ON a.id=p.start_instance LEFT JOIN src.cell_records b ON b.id=p.end_instance ORDER BY p.ordinal");
  auto members=db.Prepare("SELECT path_ordinal,net_ordinal,net_id FROM path_nets ORDER BY path_ordinal,net_ordinal");bool next=members.Step();
  while(paths.Step()) {
    Check(paths.TypedInteger(0)==facts.paths,"path ordering is invalid");(void)Text(paths,1);(void)Text(paths,2);
    const auto period=paths.TypedReal(3),slack=paths.TypedReal(4),delay=paths.TypedReal(5);
    Check(std::isfinite(period) && period>0 && std::isfinite(slack) && std::isfinite(delay) && delay>=0,"timing is invalid");
    if(normalization){const auto& n=*normalization;const double normalized=slack>=0?slack*period/(n[0]*n[2]):slack/(n[1]*period);Check(std::isfinite(normalized),"normalized_slack is invalid");}
    // Rebind final rows to source identities. No parser endpoint/cache/counter
    // is passed to this audit, including during producer-side validation.
    const auto endpoints=Endpoints(paths.TypedText(1),source_objects);
    if(paths.IsNull(6)) {for(unsigned i=7;i<14;++i)Check(paths.IsNull(i),"partial endpoint certificate is invalid");Check(!endpoints.first,"source endpoint certificate is missing");}
    else {
      for(unsigned first:{6,10}) {
        const auto& expected=first==6?endpoints.first:endpoints.second;
        Check(expected.has_value() && Text(paths,first)==expected->object && Text(paths,first+2)==expected->port && paths.TypedInteger(first+3)==expected->bit,"source endpoint identity disagrees for path "+paths.TypedText(1));
        Check(paths.IsNull(first+1)==!expected->instance,"source endpoint instance presence disagrees");
        if(expected->instance)Check(Text(paths,first+1)==*expected->instance && !paths.IsNull(first==6?14:15),"endpoint instance is invalid");
      }
      ++facts.structured;
    }
    std::set<std::string> seen;I ordinal=0;
    while(next && members.TypedInteger(0)==facts.paths) {
      Check(members.TypedInteger(1)==ordinal++,"net ordering is invalid");Check(seen.insert(Text(members,2)).second,"path has duplicate net");++facts.members;next=members.Step();
    }
    Check(ordinal>0,"path has no mapped nets");facts.max_members=std::max(facts.max_members,ordinal);
    if(facts.paths==0 || slack<facts.worst)facts.worst=slack;
    if(slack>=0)facts.positive=std::max(facts.positive,slack);else{facts.negative=std::min(facts.negative,slack);facts.sum.Add(slack);++facts.failing;}
    facts.max_period=std::max(facts.max_period,period);++facts.paths;
  }
  Check(facts.paths>0,"TSV contains no mapped timing paths");Check(!next,"net membership has an orphaned path");
  auto nets=db.Prepare("SELECT p.net_id,n.seq FROM (SELECT DISTINCT net_id FROM path_nets) p LEFT JOIN src.net_records n ON n.id=p.net_id ORDER BY p.net_id");
  while(nets.Step()){Check(!nets.IsNull(1),"contains unknown EmuIR nets: "+Text(nets,0));++facts.nets;}
  auto clocks=db.Prepare("SELECT DISTINCT clock_domain FROM paths ORDER BY clock_domain");while(clocks.Step())facts.clocks.emplace_back(Text(clocks,0));
  return facts;
}
void CompareSummary(SqliteDatabase& db,const std::string& config,const Facts& facts,const std::string& design) {
  auto q=db.Prepare("SELECT json_extract(?1,'$.design'),json_extract(?1,'$.provider'),json_extract(?1,'$.summary.paths'),json_extract(?1,'$.summary.structured_endpoint_paths'),json_extract(?1,'$.summary.unique_path_nets')");q.BindText(1,config);
  Check(q.Step() && q.TypedText(0)==design && q.Text(1)=="opensta-fpga-path-database-v1" && q.TypedInteger(2)==facts.paths && q.TypedInteger(3)==facts.structured && q.TypedInteger(4)==facts.nets,"summary disagrees");
  const auto qor=JsonRecord(facts.Qor()).Encode();
  auto numbers=db.Prepare("SELECT json_type(?1,?3),json_extract(?1,?3),json_extract(?2,?4)");
  for(const char* field:{"wns_ns","tns_ns","failing_endpoints","timed_endpoints"}) {
    numbers.Reset();numbers.BindText(1,config);numbers.BindText(2,qor);numbers.BindText(3,std::string("$.summary.qor.")+field);numbers.BindText(4,std::string("$.")+field);
    Check(numbers.Step() && (numbers.Text(0)=="integer" || numbers.Text(0)=="real"),"QoR summary is invalid");const auto value=numbers.TypedReal(1),expected=numbers.TypedReal(2);
    if(std::string(field)=="failing_endpoints" || std::string(field)=="timed_endpoints")Check(numbers.Text(0)=="integer" && numbers.TypedInteger(1)==numbers.TypedInteger(2),"QoR summary disagrees");
    else Check(std::isfinite(value) && std::abs(value-expected)<=std::max(1e-12,1e-12*std::max(std::abs(value),std::abs(expected))),"QoR summary disagrees");
  }
}
void PutEndpoint(SqliteStatement& row,int first,const std::optional<Endpoint>& endpoint) {
  if(!endpoint){for(int i=first;i<first+4;++i)row.BindNull(i);return;}
  row.BindText(first,endpoint->object);if(endpoint->instance)row.BindText(first+1,*endpoint->instance);else row.BindNull(first+1);row.BindText(first+2,endpoint->port);row.BindInteger(first+3,endpoint->bit);
}
}
void ImportStaPaths(const std::vector<ArtifactRef>& inputs,const std::string& store,const std::string& summary) {
  (void)ReadRequest(inputs);SqliteArtifactWriter writer(store,"timing-path-store-v1",inputs);AtomicBinaryWriter report(summary,inputs);Object result;
  {
    auto& db=writer.database();db.Execute(kStaPathStoreDDL);AttachSource(db,inputs[0]);SqliteTransaction transaction(db);
    Resolver resolver(db);auto path=db.Prepare("INSERT INTO paths VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"),net=db.Prepare("INSERT INTO path_nets VALUES(?,?,?)");
    std::ifstream stream(inputs[1].path(),std::ios::binary);Check(bool(stream),"TSV input is missing");std::string line;Check(bool(std::getline(stream,line)),"TSV header is missing");while(!line.empty() && line.back()=='\r')line.pop_back();
    Check(line=="path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex","TSV has invalid header");I count=0;double maximum_period=0,positive=-1,negative=0;
    while(std::getline(stream,line)) {
      while(!line.empty() && line.back()=='\r')line.pop_back();if(line.empty())continue;const auto fields=Split(line,'\t');Check(fields.size()==6,"TSV expected six fields");
      auto id=Hex(fields[0]),clock=Hex(fields[1]);Check(!id.empty() && !clock.empty(),"TSV identity is invalid");const auto period=ParseFiniteDecimal(fields[2],"clock period"),slack=ParseFiniteDecimal(fields[3],"slack"),delay=ParseFiniteDecimal(fields[4],"fixed delay");Check(period>0 && delay>=0,"TSV timing is invalid");
      maximum_period=std::max(maximum_period,period);if(slack>=0)positive=std::max(positive,slack);else negative=std::min(negative,slack);
      path.BindInteger(1,count);path.BindText(2,id);path.BindText(3,clock);path.BindReal(4,period);path.BindReal(5,slack);path.BindReal(6,delay);auto endpoints=Endpoints(id,resolver);PutEndpoint(path,7,endpoints.first);PutEndpoint(path,11,endpoints.second);path.Execute();
      std::set<std::string> seen;I ordinal=0;
      for(const auto& encoded:Split(fields[5],',')){auto name=Hex(encoded);Check(!name.empty() && seen.insert(name).second,"TSV has empty or duplicate net");net.BindInteger(1,count);net.BindInteger(2,ordinal++);net.BindText(3,name);net.Execute();}++count;
    }
    Check(!stream.bad(),"TSV read failed");path=SqliteStatement{};net=SqliteStatement{};
    const auto facts=Audit(db,std::array<double,3>{positive<=0?1:positive,negative==0?1:-negative,maximum_period});Check(facts.paths==count,"parser/final-record coverage disagrees");const auto design=Design(db);
    result={{"schema","emuflow.sta-path-import/v1"},{"status","pass"},{"implementation_id","native-sta-path-import-v1"},{"design",design},{"normalization",facts.Normalization()},{"summary",facts.Summary()},{"validation",facts.Validation(design)},{"members",facts.members},{"maximum_path_members",facts.max_members}};
    const auto encoded=JsonRecord(result).Encode();Check(encoded.size()<=128*1024,"summary exceeds bound");report.stream()<<encoded<<'\n';transaction.Commit();
  }
  writer.Commit();report.Commit();
}
void ValidateStaPaths(const std::vector<ArtifactRef>& inputs,const std::string& summary) {
  const auto config=ReadRequest(inputs);SqliteStore store(inputs[1]);auto& db=store.database();AttachSource(db,inputs[0]);const auto normalization=Normalization(db,config);const auto facts=Audit(db,normalization);const auto design=Design(db);CompareSummary(db,config,facts,design);
  AtomicBinaryWriter writer(summary,inputs);const auto encoded=JsonRecord(facts.Validation(design)).Encode();Check(encoded.size()<=128*1024,"validation summary exceeds bound");writer.stream()<<encoded<<'\n';writer.Commit();
}
}
