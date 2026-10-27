#include "physical/physical_coverage.hpp"
#include "physical/physical_evidence.hpp"
#include "scheduling/schedule_validation.hpp"
#include "common/bounded_json.hpp"
#include "common/artifact_io.hpp"
#include "storage/schema/global_event_schema.hpp"
#include "storage/schema/physical_coverage_schema.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "storage/store.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include <algorithm>
#include <cmath>
#include <map>
#include <filesystem>
#include <ostream>
#include <set>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
using Raw=std::map<std::string,std::string>;
using I=std::int64_t;
constexpr std::uint64_t kMagic=0x31564350464d5545ULL;
void Check(bool value,const std::string& message){if(!value)throw StageError(ErrorCode::kInvalidArtifact,"physical coverage: "+message);}
Raw Fields(const std::string& text){Raw r;VisitJsonObjectTokens(text,[&](const auto& key,const auto& value){Check(r.size()<16384 && r.emplace(key,value).second,"duplicate/excessive fields");});return r;}
std::string Get(const Raw& value,const std::string& key){auto row=value.find(key);Check(row!=value.end(),"missing field "+key);return row->second;}
std::string Text(const Raw& value,const std::string& key){return DecodeJsonString(Get(value,key));}
I Integer(const std::string& value){auto parsed=BoundedJson::IntegerToken(value);Check(parsed>=0 && parsed<=INT32_MAX,"invalid catalog integer");return parsed;}
std::string Meta(SqliteDatabase& db,const std::string& key){auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,key);Check(q.Step(),"missing metadata "+key);return q.TypedText(0);}
I Count(SqliteDatabase& db,const std::string& sql){auto q=db.Prepare(sql);Check(q.Step(),"missing scalar");return q.TypedInteger(0);}
std::string Digest(const std::string& text){Sha256 h;h.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());const auto value=h.finish();std::string out;for(auto c:value){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;}
void Put(SqliteDatabase& db,const std::string& key,const std::string& value){auto q=db.Prepare("INSERT INTO metadata VALUES(?,?)");q.BindText(1,key);q.BindText(2,value);q.Execute();}
struct Request {Raw config;std::vector<std::string> schemas;};
Request Read(const std::string& path,const std::vector<ArtifactRef>* sources=nullptr){
 CheckedRequestReader reader(path,4*1024*1024);Check(reader.Read<std::uint64_t>()==kMagic && reader.Read<std::uint32_t>()==1,"request version disagrees");
 const auto count=reader.Read<std::uint32_t>();Check(count>0 && count<=2048,"source catalog exceeds bound");Request result;
 for(unsigned i=0;i<count;++i){const auto name=reader.ReadString32(false,256);(void)FindSchema(name);result.schemas.push_back(name);}
 for(unsigned i=0;i<count;++i){const auto generation=ReadFileGeneration(reader);if(sources)Check(i<sources->size() && (*sources)[i].schema()==result.schemas[i] && (*sources)[i].generation()==generation,"source generation/role changed");}
 const auto encoded=reader.ReadString32(false,3*1024*1024);reader.RequireEnd();
 BoundedJson bounded(encoded,3*1024*1024);result.config=bounded.Fields();
 bounded.Keys({"schema","contract","contract_sha256","schedule_sha256","schedule_logical_sha256","schedule_sections","schedule","assignment","exact","fixture","physical_source","fpgas","board_links","board_signoff"});
 (void)bounded.Boolean("fixture");(void)bounded.Boolean("board_signoff");
 Check(Text(result.config,"schema")=="emuflow.physical-coverage-request/v1","request schema disagrees");return result;
}
const ArtifactRef& Source(const std::vector<ArtifactRef>& inputs,const Raw& config,const std::string& key,const std::string& schema){const auto at=Integer(Get(config,key));Check(at<static_cast<I>(inputs.size()-1) && inputs[at].schema()==schema,"source catalog role disagrees: "+key);return inputs[at];}
void Header(SqliteDatabase& db,const std::string& fpga,const std::string& design,const std::string& platform){const auto h=Fields(Meta(db,"header"));Check(Text(h,"fpga")==fpga && Text(h,"design")==design && Text(h,"platform")==platform,"physical source namespace disagrees");const auto status=Text(h,"status");Check(status=="pass","physical source did not pass");}
void Facts(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs,const Request& request){
 const auto& c=request.config;const auto contract=Fields(Get(c,"contract"));
 const auto schema=Text(contract,"schema"),provider=Text(contract,"provider");
 Check((schema=="emuflow.cross-layer-timing/v2" && provider=="provider-neutral-route-schedule-binding-v2") ||
       (schema=="emuflow.cross-layer-timing/v3" && provider=="provider-neutral-route-schedule-binding-v3"),"retired/invalid timing contract");
 const auto& schedule_ref=Source(inputs,c,"schedule","tdm-schedule-store-v4");SqliteStore schedule(schedule_ref);auto& s=schedule.database();const auto sh=Fields(Meta(s,"header"));
 const auto design=Text(sh,"design"),platform=Text(sh,"platform"),semantics=Text(sh,"transport_semantics");
 Check(Text(contract,"design")==design && Text(contract,"platform")==platform && Text(contract,"transport_semantics")==semantics,"contract namespace/semantics disagree");
 Check(Text(Fields(Get(contract,"schedule_binding")),"sha256")==Text(c,"schedule_sha256"),"schedule contract seal disagrees");
 AuditScheduleStoreSeal(s,Get(c,"schedule_sections"),Text(c,"schedule_logical_sha256"),Text(c,"schedule_sha256"));
 db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-8192;PRAGMA temp.cache_size=-8192;"
 "CREATE TEMP TABLE scheduled(seq INTEGER PRIMARY KEY,entry TEXT UNIQUE,demand TEXT,net TEXT,link TEXT,source TEXT,target TEXT,lane INTEGER);"
 "CREATE TEMP TABLE expected_endpoints(endpoint TEXT PRIMARY KEY,kind TEXT,entry TEXT,fpga TEXT,peer TEXT,demand TEXT,net TEXT,link TEXT,lane INTEGER) WITHOUT ROWID;"
 "CREATE TEMP TABLE observed_endpoints(endpoint TEXT PRIMARY KEY,kind TEXT,entry TEXT,fpga TEXT,delay REAL,measurement TEXT) WITHOUT ROWID;"
 "CREATE TEMP TABLE expected_logic(segment TEXT PRIMARY KEY,fpga TEXT,kind TEXT,constant INTEGER) WITHOUT ROWID;"
 "CREATE TEMP TABLE observed_logic(id TEXT PRIMARY KEY,segment TEXT,fpga TEXT,delay REAL,cone INTEGER) WITHOUT ROWID;"
 "CREATE INDEX temp.observed_logic_segment ON observed_logic(segment);"
 "CREATE TEMP TABLE board_links(link TEXT,source TEXT,target TEXT,delay REAL,qualification TEXT,PRIMARY KEY(link,source,target)) WITHOUT ROWID;");
 auto rows=s.Prepare("SELECT seq,entry_id,demand,net,link_id,source_fpga,sink_fpga,lane FROM schedule_entries ORDER BY seq");auto put=db.Prepare("INSERT INTO scheduled VALUES(?,?,?,?,?,?,?,?)");I hops=0;
 while(rows.Step()){Check(rows.TypedInteger(0)==hops++,"schedule ordinal coverage disagrees");put.BindInteger(1,rows.TypedInteger(0));for(int i=1;i<7;++i)put.BindText(i+1,rows.TypedText(i));put.BindInteger(8,rows.TypedInteger(7));put.Execute();}
 Check(hops==Integer(Get(Fields(Get(contract,"metrics")),"scheduled_hops")),"contract hop population disagrees");
 const auto hop_binding=Fields(Get(contract,"hop_binding"));Check(Text(hop_binding,"mode")=="canonical-route-schedule-merge-v1" && Integer(Get(hop_binding,"scheduled_hops"))==hops,"contract hop summary disagrees");
 db.Execute("INSERT INTO expected_endpoints SELECT '__emuflow_tx_'||entry,'tx',entry,source,target,demand,net,link,lane FROM scheduled;"
 "INSERT INTO expected_endpoints SELECT '__emuflow_rx_'||entry,'rx',entry,target,source,demand,net,link,lane FROM scheduled;");
 auto board=db.Prepare("INSERT INTO board_links VALUES(?,?,?,?,?)");I links=0;VisitJsonArrayTokens(Get(c,"board_links"),[&](const auto& raw){Check(++links<=8192,"board link catalog exceeds bound");const auto v=Fields(raw);const auto delay=ParseFiniteDecimal(Get(v,"delay_bound_ns"),"board delay");Check(delay>=0,"negative board delay");const auto qualification=Text(v,"qualification");Check(qualification=="model-only" || qualification=="characterized-upper-bound" || qualification=="measured-upper-bound","invalid board qualification");board.BindText(1,Text(v,"link"));board.BindText(2,Text(v,"from"));board.BindText(3,Text(v,"to"));board.BindReal(4,delay);board.BindText(5,Text(v,"qualification"));board.Execute();});
 Check((Get(c,"board_signoff")=="true")==bool(links && !Count(db,"SELECT count(*) FROM board_links WHERE qualification!='measured-upper-bound'")),"board signoff claim disagrees");
 Check(!Count(db,"SELECT count(*) FROM scheduled s LEFT JOIN board_links b ON b.link=s.link AND b.source=s.source AND b.target=s.target WHERE b.link IS NULL"),"board hop coverage disagrees");
 if(semantics=="sampled-virtual-wire") {
   if(Get(c,"assignment")!="null") {
     const auto& assignment_ref=Source(inputs,c,"assignment","assignment-index-v4");SqliteStore assignment(assignment_ref);auto& a=assignment.database();
     const auto descriptor=Meta(a,"semantic_contract_binary");const auto binding=Fields(Get(contract,"semantic_binding"));
     Check(Text(Fields(descriptor),"semantic_contract_sha256")==Text(binding,"sha256"),"assignment exact descriptor seal disagrees");
     const auto indexed=Fields(Get(contract,"logic_segment_binding"));Check(Text(indexed,"mode")=="phase3-assignment-index" && Text(indexed,"semantic_contract_sha256")==Text(binding,"sha256"),"indexed semantic binding disagrees");
     Check(DecodeJsonString(Meta(a,"semantic_contract_sha256"))==Text(binding,"sha256"),"assignment semantic source seal disagrees");
     const auto& exact=Source(inputs,c,"exact","phase3-exact-data-v2");
     Check(std::filesystem::path(exact.path()).filename().string()==Text(Fields(descriptor),"artifact") && std::filesystem::path(exact.path()).parent_path()==std::filesystem::path(assignment_ref.path()).parent_path(),"assignment exact owner path disagrees");
     ProjectExactCoverageSegments(exact,descriptor,db);
   } else {
     Check(Get(c,"fixture")=="true","production sampled coverage requires assignment ownership");I count=0;
     auto insert=db.Prepare("INSERT INTO expected_logic VALUES(?,?,?,?)");
     VisitJsonArrayTokens(Get(contract,"logic_segment_bindings"),[&](const auto& raw){Check(++count<=4096,"explicit fixture segment catalog exceeds bound");const auto v=Fields(raw);const auto kind=Text(v,"kind");Check(kind=="launch_to_tx" || kind=="rx_to_tx" || kind=="rx_to_capture","invalid semantic segment kind");const auto constant=v.count("source_semantics") && Text(v,"source_semantics")=="configuration-stable-constant";Check(!constant || kind=="launch_to_tx","constant segment must be launch");insert.BindText(1,Text(v,"segment"));insert.BindText(2,Text(v,"fpga"));insert.BindText(3,kind);insert.BindInteger(4,constant);insert.Execute();});
   }
 } else Check(semantics=="registered-boundary","unsupported transport semantics");
 I fpga_count=0;std::set<std::string> seen;
 VisitJsonArrayTokens(Get(c,"fpgas"),[&](const auto& raw){Check(++fpga_count<=256,"FPGA catalog exceeds bound");const auto f=Fields(raw);const auto fpga=Text(f,"fpga");Check(seen.insert(fpga).second,"FPGA repeats");
   if(Get(f,"timing")!="null") {
     SqliteStore timing(Source(inputs,f,"timing","boundary-timing-store-v1")),identity(Source(inputs,f,"identity","boundary-identity-store-v1"));auto& t=timing.database();auto& i=identity.database();RequireSqliteDDL(t,kBoundaryTimingDDL);RequireSqliteDDL(i,kBoundaryIdentityDDL);Header(t,fpga,design,platform);Header(i,fpga,design,platform);(void)AuditPhysicalEvidenceStore(t,2);(void)AuditPhysicalEvidenceStore(i,1);
     auto tr=t.Prepare("SELECT id,kind,schedule_entry,delay_ns,measurement FROM timings ORDER BY id");auto ir=i.Prepare("SELECT id,kind,schedule_entry,demand,logical_net,link,peer,logical_lane FROM endpoints ORDER BY id");auto expected=db.Prepare("SELECT kind,entry,fpga,peer,demand,net,link,lane FROM expected_endpoints WHERE endpoint=?");auto insert=db.Prepare("INSERT INTO observed_endpoints VALUES(?,?,?,?,?,?)");I count=0,tx=0;
     while(tr.Step()){Check(ir.Step(),"boundary identity/timing coverage disagrees");const auto id=tr.TypedText(0),kind=tr.TypedText(1),entry=tr.TypedText(2);Check(id==ir.TypedText(0) && kind==ir.TypedText(1) && entry==ir.TypedText(2),"boundary identity/timing facts disagree");const auto delay=tr.TypedReal(3);Check(std::isfinite(delay) && delay>=0,"invalid endpoint delay");expected.BindText(1,id);Check(expected.Step() && expected.TypedText(0)==kind && expected.TypedText(1)==entry && expected.TypedText(2)==fpga && expected.TypedText(3)==ir.TypedText(6) && expected.TypedText(4)==ir.TypedText(3) && expected.TypedText(5)==ir.TypedText(4) && expected.TypedText(6)==ir.TypedText(5) && expected.TypedInteger(7)==ir.TypedInteger(7),"unexpected physical endpoint ownership");expected.Reset();insert.BindText(1,id);insert.BindText(2,kind);insert.BindText(3,entry);insert.BindText(4,fpga);insert.BindReal(5,delay);insert.BindText(6,tr.TypedText(4));insert.Execute();++count;tx+=kind=="tx";}
     Check(!ir.Step(),"boundary timing omits an identity");for(auto* source:{&t,&i}){const auto coverage=Fields(Get(Fields(Meta(*source,"header")),"coverage"));Check(Integer(Get(coverage,"endpoints"))==count && Integer(Get(coverage,"tx"))==tx && Integer(Get(coverage,"rx"))==count-tx,"boundary header coverage disagrees");}
   } else Check(Get(f,"identity")=="null","boundary timing and identity catalogs disagree");
   if(Get(f,"logic")!="null") {
     SqliteStore logic(Source(inputs,f,"logic","logic-segment-timing-store-v1"));auto& l=logic.database();RequireSqliteDDL(l,kLogicSegmentTimingDDL);Header(l,fpga,design,platform);(void)AuditPhysicalEvidenceStore(l,3);
     auto rows=l.Prepare("SELECT segment_id,static_exact_segment_id,fpga,delay_ns,measurement FROM segments ORDER BY segment_id");auto insert=db.Prepare("INSERT INTO observed_logic VALUES(?,?,?,?,?)");auto expected=db.Prepare("SELECT fpga FROM expected_logic WHERE segment=?");
     while(rows.Step()){const auto delay=rows.TypedReal(3);Check(std::isfinite(delay) && delay>=0 && rows.TypedText(2)==fpga,"invalid physical logic owner/delay");if(!rows.IsNull(1)){expected.BindText(1,rows.TypedText(1));Check(expected.Step() && expected.TypedText(0)==fpga,"unexpected physical semantic segment");expected.Reset();}insert.BindText(1,rows.TypedText(0));if(rows.IsNull(1))insert.BindNull(2);else insert.BindText(2,rows.TypedText(1));insert.BindText(3,fpga);insert.BindReal(4,delay);const auto measurement=rows.TypedText(4);Check(measurement=="endpoint-exact" || measurement=="cut-net-cone-upper-bound" || measurement=="optimized-endpoint-collection-upper-bound","invalid logic measurement");insert.BindInteger(5,measurement!="endpoint-exact");insert.Execute();}
   }
 });
}
const std::map<std::string,std::string>& Queries(){static const std::map<std::string,std::string> queries{
 {"endpoint_bindings","SELECT endpoint,kind,entry,fpga,delay,delay,measurement FROM observed_endpoints ORDER BY endpoint"},
 {"logic_segment_bindings","SELECT e.segment,e.fpga,coalesce(min(o.delay),0.0),coalesce(max(o.delay),0.0),CASE WHEN count(o.id)=0 THEN 'structural-configuration-stable-constant' WHEN max(o.cone)>0 THEN 'routed-cone-upper-bound' ELSE 'routed-endpoint-exact' END FROM expected_logic e LEFT JOIN observed_logic o ON o.segment=e.segment GROUP BY e.segment HAVING count(o.id)>0 OR e.constant=1 ORDER BY e.segment"},
 {"board_bindings","SELECT row_number() OVER(ORDER BY s.demand,s.link,s.source,s.target,s.entry)-1,s.entry,s.link,s.source,s.target,b.delay,b.qualification FROM scheduled s JOIN board_links b ON b.link=s.link AND b.source=s.source AND b.target=s.target ORDER BY s.demand,s.link,s.source,s.target,s.entry"},
 {"missing_endpoints","SELECT e.endpoint FROM expected_endpoints e LEFT JOIN observed_endpoints o ON o.endpoint=e.endpoint WHERE o.endpoint IS NULL ORDER BY e.endpoint"},
 {"missing_logic","SELECT e.segment FROM expected_logic e LEFT JOIN observed_logic o ON o.segment=e.segment WHERE o.id IS NULL AND e.constant=0 ORDER BY e.segment"}};return queries;}
std::string HeaderRecord(SqliteDatabase& db,const Request& request){const auto& c=request.config;const auto contract=Fields(Get(c,"contract"));
 const auto missing_endpoints=Count(db,"SELECT count(*) FROM expected_endpoints e LEFT JOIN observed_endpoints o ON o.endpoint=e.endpoint WHERE o.endpoint IS NULL"),missing_logic=Count(db,"SELECT count(*) FROM expected_logic e WHERE constant=0 AND NOT EXISTS(SELECT 1 FROM observed_logic o WHERE o.segment=e.segment)");
 Object metrics{{"required_endpoints",Count(db,"SELECT count(*) FROM expected_endpoints")},{"measured_endpoints",Count(db,"SELECT count(*) FROM observed_endpoints")},{"required_logic_segments",Count(db,"SELECT count(*) FROM expected_logic")},{"measured_logic_segments",Count(db,"SELECT count(*) FROM expected_logic e WHERE constant=1 OR EXISTS(SELECT 1 FROM observed_logic o WHERE o.segment=e.segment)")},{"routed_board_hops",Count(db,"SELECT count(*) FROM scheduled")},{"model_only_board_hops",Count(db,"SELECT count(*) FROM scheduled s JOIN board_links b ON b.link=s.link AND b.source=s.source AND b.target=s.target WHERE b.qualification='model-only'")},{"characterized_board_hops",Count(db,"SELECT count(*) FROM scheduled s JOIN board_links b ON b.link=s.link AND b.source=s.source AND b.target=s.target WHERE b.qualification='characterized-upper-bound'")},{"measured_board_hops",Count(db,"SELECT count(*) FROM scheduled s JOIN board_links b ON b.link=s.link AND b.source=s.source AND b.target=s.target WHERE b.qualification='measured-upper-bound'")},{"final_board_link_timing_signoff",Get(c,"board_signoff")=="true"}};
 Object physical_source;for(const auto& entry:Fields(Get(c,"physical_source")))physical_source.emplace(entry.first,entry.second=="null"?JsonRecord():JsonRecord(DecodeJsonString(entry.second)));
 return JsonRecord(Object{{"schema","emuflow.cross-layer-physical-binding/v1"},{"provider","provider-neutral-routed-physical-evidence-v1"},{"status",missing_endpoints || missing_logic?"incomplete":"pass"},{"design",Text(contract,"design")},{"platform",Text(contract,"platform")},{"transport_semantics",Text(contract,"transport_semantics")},{"cross_layer_timing_sha256",Text(c,"contract_sha256")},{"physical_source",physical_source},{"metrics",metrics}}).Encode();
}
std::string Logical(SqliteDatabase& db,const std::string& prefix=""){Object sections;for(const auto& q:Queries()){std::string columns=q.first=="board_bindings"?"*":"*";auto rows=db.Prepare("SELECT "+columns+" FROM "+prefix+q.first+" ORDER BY 1");Sha256 hash;I count=0;while(rows.Step()){Array record;const int fields=q.first=="endpoint_bindings"?7:q.first=="logic_segment_bindings"?5:q.first=="board_bindings"?7:1;for(int i=0;i<fields;++i){if(rows.IsNull(i))record.emplace_back();else if((q.first=="endpoint_bindings" && (i==4 || i==5)) || (q.first=="logic_segment_bindings" && (i==2 || i==3)) || (q.first=="board_bindings" && i==5))record.emplace_back(rows.TypedReal(i));else if(q.first=="board_bindings" && i==0)record.emplace_back(rows.TypedInteger(i));else record.emplace_back(rows.TypedText(i));}const auto encoded=JsonRecord(record).Encode()+"\n";hash.update(reinterpret_cast<const unsigned char*>(encoded.data()),encoded.size());++count;}const auto value=hash.finish();std::string text;for(auto byte:value){text+="0123456789abcdef"[byte>>4];text+="0123456789abcdef"[byte&15];}sections.emplace(q.first,Object{{"records",count},{"sha256",text}});}return JsonRecord(sections).Encode();}
std::string Sources(const std::vector<ArtifactRef>& inputs){Array records;for(std::size_t n=0;n+1<inputs.size();++n){const auto& source=inputs[n];if(source.schema()=="physical-coverage-store-v1")continue;const auto& g=source.generation();records.emplace_back(Object{{"path",source.path()},{"schema",source.schema()},{"generation",Array{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}}});}return JsonRecord(records).Encode();}

}
std::vector<std::string> PhysicalCoverageInputSchemas(const std::string& path){auto request=Read(path);request.schemas.push_back("physical-coverage-request-v1");return request.schemas;}
void BuildPhysicalCoverage(const std::vector<ArtifactRef>& inputs,const std::string& output){Check(inputs.size()>=2,"sources missing");auto request=Read(inputs.back().path(),&inputs);Check(request.schemas.size()+1==inputs.size(),"source coverage disagrees");SqliteArtifactWriter writer(output,"physical-coverage-store-v1",inputs);auto& db=writer.database();db.Execute(kPhysicalCoverageDDL);SqliteTransaction transaction(db);Facts(db,inputs,request);for(const auto& query:Queries())db.Execute("INSERT INTO "+query.first+" "+query.second);const auto header=HeaderRecord(db,request),sections=Logical(db);Put(db,"header",header);Put(db,"sections",sections);Put(db,"sources",Sources(inputs));Put(db,"logical_sha256",JsonQuote(Digest(header+"\n"+sections)));transaction.Commit();writer.Commit();}
void AuditPhysicalCoverage(const std::vector<ArtifactRef>& inputs,const std::string& summary){Check(inputs.size()>=3 && inputs[inputs.size()-2].schema()=="physical-coverage-store-v1","audit owner is missing");auto request=Read(inputs.back().path(),&inputs);Check(request.schemas.size()+1==inputs.size(),"audit source coverage disagrees");SqliteDatabase db(":memory:",SqliteDatabase::Mode::kReadOnly);Facts(db,inputs,request);const auto& store_ref=inputs[inputs.size()-2];SqliteStore store(store_ref);RequireSqliteDDL(store.database(),kPhysicalCoverageDDL);auto attach=db.Prepare("ATTACH DATABASE ? AS checked");attach.BindText(1,SqliteImmutableUri(store_ref.path()));attach.Execute();for(const auto& query:Queries()){auto extra=db.Prepare("SELECT * FROM checked."+query.first+" EXCEPT SELECT * FROM ("+query.second+")");Check(!extra.Step(),"unexpected published "+query.first);auto missing=db.Prepare("SELECT * FROM ("+query.second+") EXCEPT SELECT * FROM checked."+query.first);Check(!missing.Step(),"missing published "+query.first);}const auto header=HeaderRecord(db,request),sections=Logical(db,"checked.");Check(Meta(store.database(),"sources")==Sources(inputs) && Meta(store.database(),"header")==header && Meta(store.database(),"sections")==sections && DecodeJsonString(Meta(store.database(),"logical_sha256"))==Digest(header+"\n"+sections),"published coverage summary/digest disagrees");PublishTextArtifact(summary,inputs,[&](std::ostream& stream){stream<<header<<'\n';});}
}
