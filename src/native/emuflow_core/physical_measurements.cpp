#include "emuflow_core/physical_evidence.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/global_event_schema.hpp"
#include "emuflow_core/json_record.hpp"

#include <array>
#include <cmath>
#include <fstream>
#include <sstream>
#include <vector>

namespace emuflow::core {
namespace {
using Integer=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
void Require(bool value,const std::string& message) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"physical timing import "+message);
}
std::string Text(SqliteStatement& row,int column) {
  auto value=row.TypedText(column);Require(!value.empty(),"has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
Integer Count(SqliteStatement& row,int column) {auto value=row.TypedInteger(column);Require(value>=0,"has a negative count");return value;}
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);Require(row.Step(),"source metadata is missing");auto text=Text(row,0);Require(text.size()<=4*1024*1024 && !row.Step(),"source metadata is invalid");return text;
}
std::string JsonText(SqliteDatabase& db,const std::string& json,const char* key,bool optional=false) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,json);row.BindText(2,key);Require(row.Step(),"configuration is missing");
  if(optional && (row.IsNull(0) || row.Text(0)=="null"))return {};
  Require(row.Text(0)=="text","configuration text is invalid");return Text(row,1);
}
void Generation(CheckedRequestReader& reader,const ArtifactRef& source) {
  FileGeneration expected;
  expected.device=reader.Read<std::uint64_t>();expected.inode=reader.Read<std::uint64_t>();expected.bytes=reader.Read<std::uint64_t>();
  expected.modified_seconds=reader.Read<std::int64_t>();expected.modified_nanoseconds=reader.Read<std::int64_t>();expected.changed_seconds=reader.Read<std::int64_t>();expected.changed_nanoseconds=reader.Read<std::int64_t>();
  if(!(expected==source.generation()))throw StageError(ErrorCode::kMixedGeneration,"physical timing input generation changed");
}
std::string HexText(const std::string& value) {
  std::string out;int high=-1;
  for(unsigned char c:value) {
    if(c==' ' || c=='\r' || c=='\v' || c=='\f')continue;
    int digit=c>='0' && c<='9'?c-'0':c>='a' && c<='f'?c-'a'+10:c>='A' && c<='F'?c-'A'+10:-1;
    Require(digit>=0,"hex identity is invalid");if(high<0)high=digit;else {out+=static_cast<char>((high<<4)|digit);high=-1;}
  }
  Require(high<0 && !out.empty(),"hex identity is empty or truncated");(void)JsonQuoteUtf8(out);return out;
}
class Measurements {
 public:
  Measurements(const std::string& path,bool boundary,bool legacy):input_(path,std::ios::binary) {
    Require(bool(input_),"cannot read measurements");std::string header;Require(Line(header),"measurement header is missing");
    const std::string base="endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex";
    if(header==base){Require(boundary || legacy,"legacy timing format requires explicit compatibility");extended_=false;}
    else {Require(!boundary && header==base+"\tmeasurement\tactual_start_object_hex\tactual_end_object_hex","measurement header is invalid");extended_=true;}
  }
  bool Take(std::vector<std::string>& fields) {
    std::string line;
    for(;;){if(!Line(line))return false;if(line.find_first_not_of(" \r\v\f")==std::string::npos)continue;break;}
    fields.clear();std::size_t start=0;
    for(;;){auto end=line.find('\t',start);fields.push_back(line.substr(start,end==std::string::npos?end:end-start));if(end==std::string::npos)break;start=end+1;}
    Require(fields.size()==(extended_?8:5),"measurement row fields are invalid");
    fields[0]=HexText(fields[0]);fields[3]=HexText(fields[3]);fields[4]=HexText(fields[4]);
    if(extended_){fields[6]=HexText(fields[6]);fields[7]=HexText(fields[7]);}
    else {fields.push_back("endpoint-exact");fields.push_back(fields[3]);fields.push_back(fields[4]);}
    return true;
  }
 private:
  bool Line(std::string& line) {
    // Bound one external record; never read a full measurement document.
    line.clear();char c;
    while(input_.get(c)) {
      Require(c!='\0' && line.size()<16*1024*1024,"measurement row is oversized or invalid");if(c=='\n')break;line+=c;
    }
    Require(!input_.bad(),"cannot read measurement row");if(line.empty() && input_.eof())return false;
    if(!line.empty() && line.back()=='\r')line.pop_back();return true;
  }
  std::ifstream input_;bool extended_=false;
};
}

void ImportPhysicalMeasurements(const std::vector<ArtifactRef>& inputs,unsigned kind,const std::string& output,const std::string& summary) {
  Require(inputs.size()==3 && kind>=2 && kind<=4,"source coverage is invalid");
  CheckedRequestReader request(inputs[2].path(),8*1024*1024);
  Require(request.Read<std::uint64_t>()==0x314d4950464d5545ULL && request.Read<std::uint32_t>()==1 && request.Read<std::uint32_t>()==kind,"request version/kind is invalid");
  const auto flags=request.Read<std::uint32_t>();Require(flags<=3 && (kind==3 || flags==0),"import mode is invalid");
  Generation(request,inputs[0]);Generation(request,inputs[1]);
  const auto header=request.ReadString32(false,4*1024*1024),source=request.ReadString32(false,65536);request.RequireEnd();
  const bool missing_allowed=flags&1,legacy=flags&2;
  SqliteStore identity(inputs[0]);auto& id=identity.database();
  RequireSqliteDDL(id,kind==2?kBoundaryIdentityDDL:kind==3?kLogicSegmentIdentityDDL:kLocalPathTimingDDL);
  const auto origin=Metadata(id,"header");
  for(const char* key:{"design","fpga"})Require(JsonText(id,origin,(std::string("$.")+key).c_str())==JsonText(id,header,(std::string("$.")+key).c_str()),"source identity disagrees");
  if(kind!=4)Require(JsonText(id,origin,"$.platform")==JsonText(id,header,"$.platform"),"source platform disagrees");
  if(kind==3) {
    for(const char* key:{"system_routes_sha256","tdm_schedule_sha256","semantic_contract_sha256"})Require(JsonText(id,origin,(std::string("$.")+key).c_str(),true)==JsonText(id,header,(std::string("$.")+key).c_str(),true),"source seals disagree");
    RequireEquivalentJson(id,Metadata(id,"source"),source,"physical timing source namespace disagrees");
  }
  const char* output_schemas[]={"","","boundary-timing-store-v1","logic-segment-timing-store-v1","local-path-timing-store-v1"};
  SqliteArtifactWriter writer(output,output_schemas[kind],inputs);auto& db=writer.database();
  db.Execute(kind==2?kBoundaryTimingDDL:kind==3?kLogicSegmentTimingDDL:kLocalPathTimingDDL);
  db.Execute("PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-8192");
  db.Execute("CREATE TEMP TABLE measurements(ordinal INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,delay REAL NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,measurement TEXT NOT NULL,actual_start TEXT NOT NULL,actual_end TEXT NOT NULL)");
  {auto attach=db.Prepare("ATTACH DATABASE ? AS identity");attach.BindText(1,SqliteImmutableUri(inputs[0].path()));attach.Execute();}
  SqliteTransaction transaction(db);
  auto append=db.Prepare("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?)");Measurements measurements(inputs[1].path(),kind==2,legacy);
  std::vector<std::string> fields;Integer inserted=0;
  while(measurements.Take(fields)) {
    const auto delay=ParseFiniteDecimal(fields[2],"physical measurement delay");Require(delay>=0,"measurement delay is negative");
    Require(kind==2?(fields[1]=="tx" || fields[1]=="rx"):kind==3?(fields[1]=="launch" || fields[1]=="transition" || fields[1]=="capture"):fields[1]=="local","measurement kind is invalid");
    if(kind!=2)Require(fields[5]=="endpoint-exact" || fields[5]=="optimized-endpoint-collection-upper-bound" || (kind==3 && fields[5]=="cut-net-cone-upper-bound"),"measurement classification is invalid");
    append.BindInteger(1,inserted++);append.BindText(2,fields[0]);append.BindText(3,fields[1]);append.BindReal(4,delay);
    for(int i=0;i<5;++i)append.BindText(i+5,fields[i+3]);append.Execute();
  }
  const auto relation=kind==2?"endpoints":kind==3?"segments":"timings";
  const auto key=kind==3?"segment_id":"id";
  auto unknown=db.Prepare(std::string("SELECT 1 FROM measurements m LEFT JOIN identity.")+relation+" i ON i."+key+"=m.id WHERE i."+key+" IS NULL LIMIT 1");
  Require(!unknown.Step(),"measurement references an unknown identity");
  const auto mismatch_sql=kind==2?"SELECT 1 FROM measurements m JOIN identity.endpoints i USING(id) WHERE m.kind!=i.kind LIMIT 1":
    kind==3?"SELECT 1 FROM measurements m JOIN identity.segments i ON i.segment_id=m.id WHERE m.kind!=i.kind OR m.start!=i.start_pin OR m.end!=i.end_pin LIMIT 1":
    "SELECT 1 FROM measurements m JOIN identity.timings i USING(id) WHERE m.start!=i.start_pin OR m.end!=i.end_pin LIMIT 1";
  auto mismatch=db.Prepare(mismatch_sql);Require(!mismatch.Step(),"measurement endpoint identity disagrees");
  if(kind==3) {
    auto order=db.Prepare("SELECT 1 FROM (SELECT i.seq,LAG(i.seq) OVER (ORDER BY m.ordinal) AS previous FROM measurements m JOIN identity.segments i ON i.segment_id=m.id) WHERE previous IS NOT NULL AND seq<=previous LIMIT 1");
    Require(!order.Step(),"logic measurement order is invalid");
  }
  auto total=id.Prepare(kind==2?"SELECT count(*) FROM endpoints":kind==3?"SELECT count(*) FROM segments":"SELECT count(*) FROM timings");Require(total.Step(),"source count is missing");const auto expected=Count(total,0),missing=expected-inserted;
  Require(missing>=0 && (!missing || missing_allowed),"timing coverage is incomplete");
  if(kind==2) {
    const auto scope=JsonText(id,header,"$.measurement_scope");Require(scope=="partition-boundary" || scope=="board-integrated-interface","boundary measurement scope is invalid");
    const auto tx=scope=="partition-boundary"?"logical-source-to-tx-port":"routed-launch-through-tx-boundary-to-interface-endpoint";
    const auto rx=scope=="partition-boundary"?"rx-port-to-shadow-capture":"interface-startpoint-through-rx-boundary-to-shadow-capture";
    auto put=db.Prepare("INSERT INTO timings SELECT i.id,i.kind,i.schedule_entry,m.delay,m.start,m.end,CASE WHEN i.kind='tx' THEN ?1 ELSE ?2 END FROM identity.endpoints i JOIN measurements m USING(id) ORDER BY i.id");put.BindText(1,tx);put.BindText(2,rx);put.Execute();
  } else if(kind==3) {
    db.Execute("INSERT INTO segments SELECT ROW_NUMBER() OVER (ORDER BY i.system_path,i.member_path,i.cut_index,i.segment_id)-1,i.segment_id,i.kind,i.system_path,i.member_path,i.cut_index,i.fpga,i.replace_tx_endpoint,i.start_pin,i.end_pin,m.delay,i.static_exact_segment_id,i.start_object_kind,i.end_object_kind,i.cone_anchor_object_kind,i.cone_anchor_pin,m.measurement,m.actual_start,m.actual_end FROM identity.segments i JOIN measurements m ON i.segment_id=m.id ORDER BY i.system_path,i.member_path,i.cut_index,i.segment_id");
    db.Execute("CREATE TEMP TABLE unsupported(path TEXT PRIMARY KEY,reason TEXT NOT NULL) WITHOUT ROWID; INSERT INTO unsupported SELECT path,reason FROM identity.unsupported_members");
    if(missing)db.Execute("INSERT INTO unsupported SELECT DISTINCT i.member_path,'no-routed-vivado-board-path' FROM identity.segments i LEFT JOIN measurements m ON i.segment_id=m.id WHERE m.id IS NULL ON CONFLICT(path) DO UPDATE SET reason=excluded.reason");
    db.Execute("INSERT INTO unsupported_members SELECT ROW_NUMBER() OVER (ORDER BY path)-1,path,reason FROM unsupported ORDER BY path");
    if(missing)db.Execute("INSERT INTO unmeasured_segments SELECT ROW_NUMBER() OVER (ORDER BY i.segment_id)-1,i.segment_id,i.kind,i.system_path,i.member_path,'no-routed-vivado-board-path' FROM identity.segments i LEFT JOIN measurements m ON i.segment_id=m.id WHERE m.id IS NULL ORDER BY i.segment_id");
  } else {
    Require(JsonText(id,origin,"$.identity_schema")=="emuflow.local-path-identity/v2" && JsonText(id,header,"$.identity_schema")=="emuflow.local-path-identity/v2","local identity schema is invalid");
    Require(JsonText(id,origin,"$.qualification")=="typed-local-path-query-identity-owner-v1","local source is not an identity owner");
    auto source_fields=id.Prepare("SELECT json_extract(?1,'$.source'),json_extract(?2,'$.source'),json_extract(?1,'$.coverage.local_paths')");source_fields.BindText(1,origin);source_fields.BindText(2,header);Require(source_fields.Step(),"local source is missing");
    RequireEquivalentJson(id,source_fields.TypedText(0),source_fields.TypedText(1),"physical timing original source disagrees");Require(Count(source_fields,2)==expected,"local identity coverage disagrees");
    auto zero=id.Prepare("SELECT 1 FROM timings WHERE delay_ns!=0.0 LIMIT 1");Require(!zero.Step(),"local identity contains measured delays");
    db.Execute("INSERT INTO timings SELECT ROW_NUMBER() OVER (ORDER BY p.id)-1,p.id,p.kind,p.fpga,p.clock_domain,p.clock_period_ns,p.required_time_ns,p.start_pin,p.end_pin,p.measurement,m.delay FROM identity.timings p JOIN measurements m USING(id) ORDER BY p.id");
    db.Execute("INSERT INTO path_pins SELECT path_id,ordinal,pin FROM identity.path_pins ORDER BY path_id,ordinal");
  }
  const auto seal=SealPhysicalEvidence(db,kind,header,source);transaction.Commit();append=SqliteStatement{};unknown=SqliteStatement{};mismatch=SqliteStatement{};
  writer.Commit();
  AtomicBinaryWriter summary_writer(summary,inputs);
  summary_writer.stream()<<JsonRecord(Object{{"schema","emuflow.physical-measurement-import/v1"},{"status","pass"},{"implementation_id","native-physical-measurement-import-v1"},
      {"kind",kind},{"header_json",seal.header},{"counts_json",seal.counts},{"source_json",seal.source},{"logical_sha256",seal.logical_sha256},
      {"maximum_delay_ns",seal.maximum_delay_ns},{"measurement_counts_json",seal.measurement_counts},{"missing_segments",missing}}).Encode()<<'\n';summary_writer.Commit();
}
}
