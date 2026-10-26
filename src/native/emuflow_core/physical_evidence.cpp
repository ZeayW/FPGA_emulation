#include "emuflow_core/physical_evidence.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/global_event_schema.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/hash.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
using Integer=std::int64_t;
void Require(bool value,const std::string& message) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"physical evidence "+message);
}
std::string Text(SqliteStatement& row,int column) {
  auto value=row.TypedText(column);Require(!value.empty(),"record has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
Integer Count(SqliteStatement& row,int column) {auto value=row.TypedInteger(column);Require(value>=0,"record has a negative count");return value;}
double Real(SqliteStatement& row,int column,bool positive=false) {
  const auto value=row.TypedReal(column);Require(std::isfinite(value) && (positive?value>0:value>=0),"record has invalid delay/period");return value;
}
std::string Digest(Sha256& hash) {
  static const char* digits="0123456789abcdef";std::string result;
  for(auto c:hash.finish()){result+=digits[c>>4];result+=digits[c&15];}return result;
}
void Hash(Sha256& hash,std::string_view value) {hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());}
void Record(Sha256& hash,const Object& record) {Hash(hash,JsonRecord(record).EncodeUtf8());Hash(hash,"\n");}
void Generation(CheckedRequestReader& reader,const ArtifactRef& source) {
  FileGeneration expected;
  expected.device=reader.Read<std::uint64_t>();expected.inode=reader.Read<std::uint64_t>();expected.bytes=reader.Read<std::uint64_t>();
  expected.modified_seconds=reader.Read<std::int64_t>();expected.modified_nanoseconds=reader.Read<std::int64_t>();
  expected.changed_seconds=reader.Read<std::int64_t>();expected.changed_nanoseconds=reader.Read<std::int64_t>();
  if(!(expected==source.generation()))throw StageError(ErrorCode::kMixedGeneration,"physical evidence source generation changed");
}
std::string JsonText(SqliteDatabase& db,const std::string& object,const char* key,bool optional=false) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,key);
  Require(row.Step(),"header is missing");if(optional && (row.IsNull(0) || row.Text(0)=="null"))return {};
  Require(row.Text(0)=="text","header text field is invalid");return Text(row,1);
}

void Metadata(SqliteDatabase& db,const std::map<std::string,std::string>& expected) {
  auto rows=db.Prepare("SELECT key,length(value),value FROM metadata ORDER BY key");std::size_t count=0;
  while(rows.Step()) {
    const auto key=Text(rows,0);auto found=expected.find(key);
    Require(found!=expected.end() && Count(rows,1)<=8*1024*1024,"metadata fields are unexpected or oversized");
    const auto value=rows.TypedText(2);RequireEquivalentJson(db,value,found->second,"metadata disagrees");++count;
  }
  Require(count==expected.size(),"metadata coverage disagrees");
}
void AddOptional(Object& record,SqliteStatement& row,int column,const char* key) {
  if(!row.IsNull(column))record.emplace(key,Text(row,column));
}
void Sequence(Integer count,Integer minimum,Integer maximum) {
  Require(!count || (minimum==0 && maximum==count-1),"record sequence is incomplete");
}
Object BoundaryIdentity(SqliteDatabase& db,Sha256& digest) {
  auto rows=db.Prepare("SELECT id,kind,schedule_entry,demand,logical_net,link,peer,lane,logical_lane,internal_signal,source_class,external_port,external_port_bit,external_net,merged_logical_net FROM endpoints ORDER BY id");
  auto registers=db.Prepare("SELECT endpoint_id,ordinal,instance FROM boundary_registers ORDER BY endpoint_id,ordinal");bool have=registers.Step();
  Integer total=0,tx=0,rx=0;
  while(rows.Step()) {
    const auto id=Text(rows,0),kind=Text(rows,1),source=Text(rows,10);
    Require(kind=="tx" || kind=="rx","boundary kind is invalid");
    Require(source=="dut-net" || source=="forwarded-shadow" || source=="captured-shadow","boundary source class is invalid");
    Array instances;std::string previous;
    while(have && Text(registers,0)==id) {
      const auto value=Text(registers,2);
      Require(Count(registers,1)==static_cast<Integer>(instances.size()) && (instances.empty() || previous<value),"boundary register order is invalid");
      previous=value;instances.emplace_back(value);have=registers.Step();
    }
    Require(!have || Text(registers,0)>id,"boundary register owner is invalid");
    Object merged{{"external_port",Text(rows,11)},{"external_port_bit",Count(rows,12)},{"external_net",Text(rows,13)},
        {"logical_net",rows.IsNull(14)?JsonRecord():JsonRecord(Text(rows,14))},{"boundary_register_instances",std::move(instances)}};
    Object record{{"id",id},{"kind",kind},{"schedule_entry",Text(rows,2)},{"demand",Text(rows,3)},{"logical_net",Text(rows,4)},
        {"link",Text(rows,5)},{"peer",Text(rows,6)},{"lane",Count(rows,7)},{"logical_lane",Count(rows,8)},
        {"internal_signal",Text(rows,9)},{"source_class",source},{"merged_ir",std::move(merged)}};
    Record(digest,record);++total;if(kind=="tx")++tx;else ++rx;
  }
  Require(!have,"boundary register owner is invalid");
  return {{"endpoints",total},{"tx",tx},{"rx",rx},{"external_port_nets",total}};
}
Object BoundaryTiming(SqliteDatabase& db,Sha256& digest,double* maximum=nullptr) {
  auto rows=db.Prepare("SELECT id,kind,schedule_entry,delay_ns,start_object,end_object,measurement FROM timings ORDER BY id");
  Integer total=0,tx=0,rx=0;
  while(rows.Step()) {
    const auto kind=Text(rows,1);Require(kind=="tx" || kind=="rx","boundary kind is invalid");
    const auto delay=Real(rows,3);if(maximum)*maximum=std::max(*maximum,delay);
    Record(digest,Object{{"id",Text(rows,0)},{"kind",kind},{"schedule_entry",Text(rows,2)},{"delay_ns",delay},
        {"start_object",Text(rows,4)},{"end_object",Text(rows,5)},{"measurement",Text(rows,6)}});
    ++total;if(kind=="tx")++tx;else ++rx;
  }
  return {{"endpoints",total},{"tx",tx},{"rx",rx}};
}
struct LocalSummary {Object counts,measurements;double maximum=0;};
LocalSummary LocalTiming(SqliteDatabase& db,Sha256& digest,const std::string& header) {
  const auto fpga=JsonText(db,header,"$.fpga"),identity=JsonText(db,header,"$.identity_schema");
  const bool v2=identity=="emuflow.local-path-identity/v2";
  Require(v2 || identity=="emuflow.local-path-identity/v1","local identity schema is invalid");
  auto rows=db.Prepare("SELECT seq,id,kind,fpga,clock_domain,clock_period_ns,required_time_ns,start_pin,end_pin,measurement,delay_ns FROM timings ORDER BY id");
  auto pins=db.Prepare("SELECT path_id,ordinal,pin FROM path_pins ORDER BY path_id,ordinal");bool have=pins.Step();
  Integer total=0,minimum=std::numeric_limits<Integer>::max(),maximum=-1,exact=0,fallback=0;double largest=0;
  while(rows.Step()) {
    const auto seq=Count(rows,0);const auto id=Text(rows,1),start=Text(rows,7),end=Text(rows,8);
    const auto delay=Real(rows,10);largest=std::max(largest,delay);minimum=std::min(minimum,seq);maximum=std::max(maximum,seq);
    Require(Text(rows,2)=="local" && Text(rows,3)==fpga,"local identity disagrees");
    Array chain;std::string first,last;
    while(have && Text(pins,0)==id) {
      const auto pin=Text(pins,2);Require(Count(pins,1)==static_cast<Integer>(chain.size()),"local path pin order is invalid");
      Require(pin.find(',')==std::string::npos,"local path pin is invalid");if(chain.empty())first=pin;last=pin;
      chain.emplace_back(pin);have=pins.Step();
    }
    Require(!have || Text(pins,0)>id,"local path pin owner is invalid");
    Object record{{"id",id},{"kind","local"},{"fpga",fpga},{"clock_domain",Text(rows,4)},
        {"clock_period_ns",Real(rows,5,true)},{"start_pin",start},{"end_pin",end},{"delay_ns",delay}};
    Require(v2?!rows.IsNull(6):rows.IsNull(6),"local required time schema disagrees");
    if(v2) {
      const auto measurement=Text(rows,9);
      if(measurement=="explicit-routed-path-chain") {
        Require(chain.size()>=2 && first==start && last==end,"local path measurement chain is invalid");++exact;
      } else {Require(measurement=="endpoint-longest-path-fallback" && chain.empty(),"local path measurement is invalid");++fallback;}
      record.emplace("required_time_ns",Real(rows,6,true));record.emplace("measurement",measurement);record.emplace("path_pins",std::move(chain));
    }
    Record(digest,record);++total;
  }
  Require(!have,"local path pin owner is invalid");Sequence(total,minimum,maximum);
  return {{{"local_paths",total}},v2?Object{{"explicit-routed-path-chain",exact},{"endpoint-longest-path-fallback",fallback}}:Object{},largest};
}
Object LogicTiming(SqliteDatabase& db,Sha256& segments,Sha256& unsupported,Sha256& unmeasured,const std::string& header,const std::string& counts,double& largest) {
  const auto fpga=JsonText(db,header,"$.fpga"),semantic=JsonText(db,header,"$.semantic_contract_sha256",true);
  auto rows=db.Prepare("SELECT seq,segment_id,kind,system_path,member_path,cut_index,fpga,replace_tx_endpoint,start_pin,end_pin,delay_ns,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,measurement,actual_start_object,actual_end_object FROM segments ORDER BY system_path,member_path,cut_index,segment_id");
  Integer total=0,minimum=std::numeric_limits<Integer>::max(),maximum=-1,exact=0,cone=0,collection=0;
  while(rows.Step()) {
    const auto seq=Count(rows,0);const auto kind=Text(rows,2),measurement=Text(rows,16);
    const auto delay=Real(rows,10);largest=std::max(largest,delay);
    minimum=std::min(minimum,seq);maximum=std::max(maximum,seq);
    Require(Text(rows,6)==fpga && (kind=="launch" || kind=="transition" || kind=="capture"),"logic segment identity disagrees");
    Require((kind=="capture")==rows.IsNull(7),"logic segment replacement is invalid");
    Require(semantic.empty()?rows.IsNull(11):!rows.IsNull(11),"logic segment exact identity is invalid");
    const auto object_kind=[&](int column){const auto text=Text(rows,column);Require(text=="pin" || text=="port","logic segment object kind is invalid");};
    if(!rows.IsNull(12) || !rows.IsNull(13)){object_kind(12);object_kind(13);}
    Require(rows.IsNull(14)==rows.IsNull(15),"logic segment cone anchor is invalid");
    if(!rows.IsNull(14)){object_kind(14);(void)Text(rows,15);Require(kind!="capture","logic segment capture has a cone anchor");}
    if(measurement=="endpoint-exact")++exact;
    else if(measurement=="cut-net-cone-upper-bound"){Require(!rows.IsNull(15),"logic segment cone-bound trace is invalid");++cone;}
    else {Require(measurement=="optimized-endpoint-collection-upper-bound","logic segment measurement is invalid");++collection;}
    Object record{{"id",Text(rows,1)},{"kind",kind},{"system_path",Text(rows,3)},{"member_path",Text(rows,4)},
        {"cut_index",Count(rows,5)},{"fpga",fpga},{"replace_tx_endpoint",rows.IsNull(7)?JsonRecord():JsonRecord(Text(rows,7))},
        {"start_pin",Text(rows,8)},{"end_pin",Text(rows,9)},{"delay_ns",delay},{"measurement",measurement}};
    for(auto field:std::initializer_list<std::pair<int,const char*>>{{11,"static_exact_segment_id"},{12,"start_object_kind"},{13,"end_object_kind"},
        {14,"cone_anchor_object_kind"},{15,"cone_anchor_pin"},{17,"actual_start_object"},{18,"actual_end_object"}})AddOptional(record,rows,field.first,field.second);
    Record(segments,record);++total;
  }
  Sequence(total,minimum,maximum);
  auto missing=db.Prepare("SELECT seq,path,reason FROM unsupported_members ORDER BY seq");Integer missing_count=0;
  while(missing.Step()){Require(Count(missing,0)==missing_count++,"unsupported member sequence is invalid");Record(unsupported,{{"path",Text(missing,1)},{"reason",Text(missing,2)}});}
  auto absent=db.Prepare("SELECT seq,segment_id,kind,system_path,member_path,reason FROM unmeasured_segments ORDER BY seq");Integer absent_count=0;
  while(absent.Step()) {
    Require(Count(absent,0)==absent_count++,"unmeasured segment sequence is invalid");const auto kind=Text(absent,2);
    Require(kind=="launch" || kind=="transition" || kind=="capture","unmeasured segment kind is invalid");
    Record(unmeasured,{{"id",Text(absent,1)},{"kind",kind},{"system_path",Text(absent,3)},{"member_path",Text(absent,4)},{"reason",Text(absent,5)}});
  }
  auto overlap=db.Prepare("SELECT 1 FROM segments JOIN unmeasured_segments USING(segment_id) LIMIT 1");Require(!overlap.Step(),"logic segment is both measured and unmeasured");
  auto distinct=db.Prepare("SELECT COUNT(DISTINCT system_path),COUNT(DISTINCT member_path) FROM segments");Require(distinct.Step(),"logic segment aggregate is missing");
  Object result{{"segments",total},{"system_paths",Count(distinct,0)},{"member_paths",Count(distinct,1)},{"unsupported_member_paths",missing_count},
      {"unmeasured_segments",absent_count},{"endpoint_exact_segments",exact},{"cone_bound_segments",cone}};
  auto optional=db.Prepare("SELECT json_type(?,'$.endpoint_collection_bound_segments')");optional.BindText(1,counts);Require(optional.Step(),"logic segment counts are missing");
  if(collection || !optional.IsNull(0))result.emplace("endpoint_collection_bound_segments",collection);
  return result;
}
Object LogicIdentity(SqliteDatabase& db,Sha256& segments,Sha256& unsupported,const std::string& header) {
  const auto fpga=JsonText(db,header,"$.fpga"),semantic=JsonText(db,header,"$.semantic_contract_sha256",true);
  auto rows=db.Prepare("SELECT seq,segment_id,kind,system_path,member_path,cut_index,fpga,replace_tx_endpoint,start_pin,end_pin,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin FROM segments ORDER BY seq");
  Integer total=0,missing=0;std::string previous;
  while(rows.Step()) {
    const auto id=Text(rows,1),kind=Text(rows,2);
    Require(Count(rows,0)==total && (!total || previous<id),"logic identity sequence/order is invalid");previous=id;
    Require(Text(rows,6)==fpga && (kind=="launch" || kind=="transition" || kind=="capture"),"logic identity classification is invalid");
    Require((kind=="capture")==rows.IsNull(7),"logic identity replacement is invalid");
    Require(semantic.empty()?rows.IsNull(10):!rows.IsNull(10),"logic identity exact identity is invalid");
    const auto object_kind=[&](int column){const auto value=Text(rows,column);Require(value=="pin" || value=="port","logic identity object kind is invalid");};
    Require(rows.IsNull(11)==rows.IsNull(12),"logic identity object kinds are invalid");
    if(!rows.IsNull(11)){object_kind(11);object_kind(12);}
    Require(rows.IsNull(13)==rows.IsNull(14),"logic identity cone anchor is invalid");
    if(!rows.IsNull(13)){object_kind(13);(void)Text(rows,14);Require(kind!="capture","logic identity cone anchor is invalid");}
    Object record{{"id",id},{"kind",kind},{"system_path",Text(rows,3)},{"member_path",Text(rows,4)},
        {"cut_index",Count(rows,5)},{"fpga",fpga},{"replace_tx_endpoint",rows.IsNull(7)?JsonRecord():JsonRecord(Text(rows,7))},
        {"start_pin",Text(rows,8)},{"end_pin",Text(rows,9)}};
    for(auto field:std::initializer_list<std::pair<int,const char*>>{{10,"static_exact_segment_id"},{11,"start_object_kind"},{12,"end_object_kind"},
        {13,"cone_anchor_object_kind"},{14,"cone_anchor_pin"}})AddOptional(record,rows,field.first,field.second);
    Record(segments,record);++total;
  }
  auto absent=db.Prepare("SELECT seq,path,reason FROM unsupported_members ORDER BY seq");previous.clear();
  while(absent.Step()) {
    const auto path=Text(absent,1);Require(Count(absent,0)==missing && (!missing || previous<path),"unsupported member sequence/order is invalid");previous=path;
    Record(unsupported,{{"path",path},{"reason",Text(absent,2)}});++missing;
  }
  auto distinct=db.Prepare("SELECT count(DISTINCT system_path),count(DISTINCT member_path) FROM segments");Require(distinct.Step(),"logic identity counts are missing");
  return {{"segments",total},{"system_paths",Count(distinct,0)},{"member_paths",Count(distinct,1)},{"unsupported_member_paths",missing}};
}
} // namespace

void ValidatePhysicalEvidence(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Require(inputs.size()==2,"input coverage disagrees");CheckedRequestReader request(inputs[1].path(),8*1024*1024);
  Require(request.Read<std::uint64_t>()==0x31564550464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  const auto kind=request.Read<std::uint32_t>();Require(kind>=1 && kind<=5,"kind is invalid");Generation(request,inputs[0]);
  const auto header=request.ReadString32(false,4*1024*1024),counts=request.ReadString32(false,65536),source=request.ReadString32(false,65536),logical=request.ReadString32(false,64);request.RequireEnd();
  Require(logical.size()==64 && logical.find_first_not_of("0123456789abcdef")==std::string::npos,"logical seal is invalid");
  SqliteStore store(inputs[0]);auto& db=store.database();db.Execute("PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-8192");
  const char* schemas[]={kBoundaryIdentityDDL,kBoundaryTimingDDL,kLogicSegmentTimingDDL,kLocalPathTimingDDL,kLogicSegmentIdentityDDL};RequireSqliteDDL(db,schemas[kind-1]);
  std::map<std::string,std::string> metadata{{"header",header},{"counts",counts},{"logical_sha256",JsonQuote(logical)}};
  if(kind==3 || kind==5)metadata.emplace("source",source);else Require(source=="null","unexpected source metadata");Metadata(db,metadata);
  Sha256 records,unsupported,unmeasured;Object actual,measurements;double maximum=0;
  if(kind==1)actual=BoundaryIdentity(db,records);
  else if(kind==2)actual=BoundaryTiming(db,records,&maximum);
  else if(kind==3)actual=LogicTiming(db,records,unsupported,unmeasured,header,counts,maximum);
  else if(kind==5)actual=LogicIdentity(db,records,unsupported,header);
  else {auto result=LocalTiming(db,records,header);actual=std::move(result.counts);measurements=std::move(result.measurements);maximum=result.maximum;}
  const auto actual_json=JsonRecord(actual).EncodeUtf8();RequireEquivalentJson(db,actual_json,counts,"row counts disagree");
  std::string payload="{\"counts\":"+actual_json;
  if(kind<=2)payload+=",\"endpoint_sha256\":"+JsonQuote(Digest(records));
  payload+=",\"header\":"+header;
  if(kind==4)payload+=",\"path_sha256\":"+JsonQuote(Digest(records));
  if(kind==3)payload+=",\"segment_sha256\":"+JsonQuote(Digest(records))+",\"source\":"+source+
      ",\"unmeasured_sha256\":"+JsonQuote(Digest(unmeasured))+",\"unsupported_sha256\":"+JsonQuote(Digest(unsupported));
  if(kind==5)payload+=",\"segment_sha256\":"+JsonQuote(Digest(records))+",\"source\":"+source+",\"unsupported_sha256\":"+JsonQuote(Digest(unsupported));
  payload+='}';Sha256 hash;Hash(hash,payload);const auto observed=Digest(hash);Require(observed==logical,"logical seal disagrees");
  AtomicBinaryWriter writer(output,inputs);writer.stream()<<JsonRecord(Object{{"schema","emuflow.physical-evidence-validation/v1"},{"status","pass"},
      {"implementation_id","native-physical-evidence-validation-v1"},{"kind",kind},{"counts",std::move(actual)},{"logical_sha256",observed},
      {"maximum_delay_ns",maximum},{"measurement_counts",std::move(measurements)}}).Encode()<<'\n';writer.Commit();
}
} // namespace emuflow::core

namespace emuflow::core {
PhysicalEvidenceSeal SealPhysicalEvidence(SqliteDatabase& db,unsigned kind,const std::string& header,const std::string& source) {
  Require(kind>=2 && kind<=4,"producer kind is invalid");
  Sha256 records,unsupported,unmeasured;Object counts,measurements;double maximum=0;
  if(kind==2)counts=BoundaryTiming(db,records,&maximum);
  else if(kind==3)counts=LogicTiming(db,records,unsupported,unmeasured,header,"{\"endpoint_collection_bound_segments\":0}",maximum);
  else {auto result=LocalTiming(db,records,header);counts=std::move(result.counts);measurements=std::move(result.measurements);maximum=result.maximum;}
  Object output;
  for(const char* field:{"schema","status","design","fpga","provider","qualification"})
    output.emplace(field,JsonText(db,header,(std::string("$.")+field).c_str()));
  Require(JsonText(db,header,"$.status")=="pass","producer status is invalid");
  Object coverage;auto old=db.Prepare("SELECT key,value FROM json_each(?,'$.coverage') ORDER BY key");old.BindText(1,header);
  while(old.Step())coverage.emplace(Text(old,0),Count(old,1));
  if(kind==2) {
    Require(JsonText(db,header,"$.schema")=="emuflow.boundary-timing/v1","producer schema is invalid");
    output.emplace("platform",JsonText(db,header,"$.platform"));output.emplace("measurement_scope",JsonText(db,header,"$.measurement_scope"));
    coverage=counts;
  } else if(kind==3) {
    Require(JsonText(db,header,"$.schema")=="emuflow.logic-segment-timing/v1","producer schema is invalid");
    for(const char* field:{"platform","system_routes_sha256","tdm_schedule_sha256"})output.emplace(field,JsonText(db,header,(std::string("$.")+field).c_str()));
    const auto semantic=JsonText(db,header,"$.semantic_contract_sha256",true);
    auto declared=db.Prepare("SELECT json_type(?,'$.semantic_contract_sha256')");declared.BindText(1,header);Require(declared.Step(),"semantic header is missing");
    if(!declared.IsNull(0))output.emplace("semantic_contract_sha256",semantic.empty()?JsonRecord():JsonRecord(semantic));
    for(const char* key:{"segments","system_paths","member_paths","unsupported_member_paths","endpoint_exact_segments","cone_bound_segments","endpoint_collection_bound_segments"})coverage[key]=counts.at(key);
  } else {
    Require(JsonText(db,header,"$.schema")=="emuflow.local-path-timing/v1","producer schema is invalid");
    output.emplace("identity_schema",JsonText(db,header,"$.identity_schema"));Object original;
    auto shape=db.Prepare("SELECT count(*),count(DISTINCT key),sum(key IN ('path_database_sha256','original_ir_sha256','assignment_sha256','routes_sha256','original_path_ids_sha256','original_paths')) FROM json_each(?,'$.source')");shape.BindText(1,header);
    Require(shape.Step() && Count(shape,0)==6 && Count(shape,1)==6 && Count(shape,2)==6,"original source fields are invalid");
    for(const char* field:{"path_database_sha256","original_ir_sha256","assignment_sha256","routes_sha256","original_path_ids_sha256"}) {
      const auto digest=JsonText(db,header,(std::string("$.source.")+field).c_str());Require(digest.size()==64 && digest.find_first_not_of("0123456789abcdef")==std::string::npos,"original source seal is invalid");original.emplace(field,digest);
    }
    auto count=db.Prepare("SELECT json_extract(?,'$.source.original_paths')");count.BindText(1,header);Require(count.Step() && Count(count,0)>0,"original source count is invalid");original.emplace("original_paths",Count(count,0));
    output.emplace("source",std::move(original));coverage=counts;
  }
  output.emplace("coverage",std::move(coverage));
  PhysicalEvidenceSeal result{JsonRecord(output).EncodeUtf8(),JsonRecord(counts).EncodeUtf8(),source,"",JsonRecord(measurements).EncodeUtf8(),maximum};
  std::string payload="{\"counts\":"+result.counts;
  if(kind==2)payload+=",\"endpoint_sha256\":"+JsonQuote(Digest(records));
  payload+=",\"header\":"+result.header;
  if(kind==4)payload+=",\"path_sha256\":"+JsonQuote(Digest(records));
  if(kind==3)payload+=",\"segment_sha256\":"+JsonQuote(Digest(records))+",\"source\":"+source+
      ",\"unmeasured_sha256\":"+JsonQuote(Digest(unmeasured))+",\"unsupported_sha256\":"+JsonQuote(Digest(unsupported));
  payload+='}';Sha256 hash;Hash(hash,payload);result.logical_sha256=Digest(hash);
  auto put=db.Prepare("INSERT INTO metadata VALUES(?,?)");
  std::map<std::string,std::string> metadata{{"header",result.header},{"counts",result.counts},{"logical_sha256",JsonQuote(result.logical_sha256)}};
  if(kind==3)metadata.emplace("source",source);
  for(const auto& value:metadata){put.BindText(1,value.first);put.BindText(2,value.second);put.Execute();}
  return result;
}
}
