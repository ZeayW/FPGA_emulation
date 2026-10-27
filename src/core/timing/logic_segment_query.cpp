#include "timing/logic_segment_query.hpp"
#include "physical/physical_query_objects.hpp"
#include "physical/physical_evidence.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "scheduling/schedule_timing.hpp"
#include "scheduling/schedule_validation.hpp"
#include "storage/schema/global_event_schema.hpp"
#include "common/bounded_json.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <set>
#include <memory>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using I=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
using Raw=std::map<std::string,std::string>;
void Check(bool valid,const std::string& why){if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"logic segment query: "+why);}
void Stop(const volatile std::sig_atomic_t* flag){if(flag && *flag)throw StageError(ErrorCode::kExecutionFailed,"logic segment query cancelled");}
std::string Meta(SqliteDatabase& db,const std::string& key,const std::string& owner="metadata"){
 auto row=db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value END FROM "+owner+" WHERE key=?");row.BindText(1,key);Check(row.Step() && !row.IsNull(0),"metadata absent or exceeds bound: "+key);return row.TypedText(0);
}
std::string Hex(const std::string& text){std::string out;for(unsigned char c:text){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;}
std::string Digest(const std::string& text){Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());const auto value=hash.finish();return Hex(std::string(reinterpret_cast<const char*>(value.data()),value.size()));}
std::string FileHash(const fs::path& path){std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot read source hash");Sha256 hash;std::array<char,65536> bytes;while(in){in.read(bytes.data(),bytes.size());hash.update(reinterpret_cast<const unsigned char*>(bytes.data()),in.gcount());}Check(in.eof(),"source hash failed");const auto value=hash.finish();return Hex(std::string(reinterpret_cast<const char*>(value.data()),value.size()));}
std::string Segment(I number){auto digits=std::to_string(number);return "segment"+std::string(digits.size()<6?6-digits.size():0,'0')+digits;}
void Attach(SqliteDatabase& db,const ArtifactRef& source,const std::string& alias){auto row=db.Prepare("ATTACH DATABASE ? AS "+alias);row.BindText(1,SqliteImmutableUri(source.path()));row.Execute();}
I Scalar(SqliteDatabase& db,const std::string& sql){auto row=db.Prepare(sql);Check(row.Step(),"aggregate missing");return row.TypedInteger(0);}
std::string Field(const std::string& document,const std::string& key){return BoundedJson(document).Text(key);}
std::string OutputName(const Raw& c,const std::string& key){const auto value=DecodeJsonString(c.at(key));Check(!value.empty() && value!="." && value!=".." && fs::path(value).filename()==value && value.find('\0')==std::string::npos,"output name escapes owner");return value;}
struct Workspace {fs::path root;explicit Workspace(const fs::path& parent){auto path=(parent/".logic-query-XXXXXX").string();Check(mkdtemp(path.data())!=nullptr,"cannot create private workspace");root=path;}~Workspace(){if(!root.empty()){std::error_code error;fs::remove_all(root,error);}}};
struct Request {Raw fields;std::string fpga,provider,design,platform,query,identity,report;};
Request ReadRequest(const std::vector<ArtifactRef>& sources){
 static const char* roles[]={"emuir-compact-v2","assignment-index-v4","phase3-exact-data-v2","system-route-store-v3","tdm-schedule-store-v4","timing-path-store-v1","emuir-compact-v2","boundary-identity-store-v1","exact-schedule-board-request-v1","xilinx-primitive-library-v1","logic-segment-query-request-v1"};
 Check(sources.size()==11,"source role coverage disagrees");for(unsigned i=0;i<11;++i){Check(sources[i].schema()==roles[i],"source role disagrees");sources[i].RequireUnchanged();}
 CheckedRequestReader reader(sources.back().path(),4*1024*1024);Check(reader.Read<std::uint64_t>()==0x31514c50464d5545ULL && reader.Read<std::uint32_t>()==1,"request version disagrees");for(unsigned i=0;i<10;++i)Check(ReadFileGeneration(reader)==sources[i].generation(),"source generation changed");
 BoundedJson control(reader.ReadString32(false,3*1024*1024));reader.RequireEnd();
 control.Keys({"schema","fpga","provider","design","platform","query_name","identity_name","report_name","route_sha256","route_file_sha256","schedule_sha256","schedule_pretty_sha256","schedule_sections","path_sha256"});Check(control.Text("schema")=="emuflow.logic-segment-query/v1","request schema disagrees");
 Request result{control.Fields(),control.Text("fpga"),control.Text("provider"),control.Text("design"),control.Text("platform"),{},{},{}};
 Check(!result.fpga.empty() && (result.provider=="xilinx" || result.provider=="vivado"),"FPGA/provider invalid");result.query=OutputName(result.fields,"query_name");result.identity=OutputName(result.fields,"identity_name");result.report=OutputName(result.fields,"report_name");Check(std::set<std::string>{result.query,result.identity,result.report}.size()==3,"output names collide");return result;
}

class QueryPlan {
 public:
 QueryPlan(SqliteDatabase& db,const std::vector<ArtifactRef>& sources,const Request& request,const volatile std::sig_atomic_t* cancelled)
     :db(db),sources(sources),request(request),objects(sources[6],request.provider,&sources[9]),cancelled(cancelled){
   db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-16384;PRAGMA temp.cache_size=-16384");
   Attach(db,sources[0],"original");Attach(db,sources[1],"assignment");Attach(db,sources[3],"routes");Attach(db,sources[4],"schedule");Attach(db,sources[5],"paths");Attach(db,sources[7],"boundary");
   Check(Field(Meta(db,"design_json","original.metadata"),"name")==request.design,"original design namespace disagrees");
   Check(Field(Meta(objects_owner(),"design_json"),"name")==request.design+"__"+request.fpga,"merged design namespace disagrees");
   Check(DecodeJsonString(Meta(db,"design","assignment.metadata"))==request.design && DecodeJsonString(Meta(db,"platform","assignment.metadata"))==request.platform,"assignment namespace disagrees");
   Check(BoundedJson::IntegerToken(Meta(db,"source_instance_count","assignment.metadata"))==Scalar(db,"SELECT count(*) FROM original.cell_records"),"original assignment population disagrees");
   const auto assignment_sha=DecodeJsonString(Meta(db,"semantic_contract_sha256","assignment.metadata"));semantic_sha=assignment_sha;
   const auto descriptor=Meta(db,"semantic_contract_binary","assignment.metadata");Check(Field(descriptor,"semantic_contract_sha256")==assignment_sha,"exact descriptor semantic seal disagrees");
   Check(fs::path(sources[2].path()).parent_path()==fs::path(sources[1].path()).parent_path() && fs::path(sources[2].path()).filename()==Field(descriptor,"artifact"),"exact source ownership disagrees");
   const auto schedule_header=Meta(db,"header","schedule.metadata");Check(Field(schedule_header,"design")==request.design && Field(schedule_header,"platform")==request.platform && Field(schedule_header,"semantic_contract_sha256")==assignment_sha,"schedule semantic namespace disagrees");
   {SqliteStore store(sources[4]);AuditScheduleStoreSeal(store.database(),request.fields.at("schedule_sections"),DecodeJsonString(request.fields.at("schedule_sha256")),DecodeJsonString(request.fields.at("schedule_pretty_sha256")));}
   Check(FileHash(sources[3].path())==DecodeJsonString(request.fields.at("route_file_sha256")),"route source seal disagrees");
   Check(FileHash(sources[5].path())==DecodeJsonString(request.fields.at("path_sha256")),"original path source seal disagrees");
   {SqliteStore identity(sources[7]);AuditPhysicalEvidenceStore(identity.database(),1);const auto header=Meta(identity.database(),"header");Check(Field(header,"design")==request.design && Field(header,"platform")==request.platform && Field(header,"fpga")==request.fpga,"boundary source namespace disagrees");}
   db.Execute("CREATE TEMP TABLE query_facts(id TEXT PRIMARY KEY,kind TEXT,system_path TEXT,member_path TEXT,cut_index INTEGER,fpga TEXT,replace_tx TEXT,start_pin TEXT,end_pin TEXT,exact_id TEXT,start_kind TEXT,end_kind TEXT,anchor_kind TEXT,anchor_pin TEXT) WITHOUT ROWID;"
     "CREATE INDEX temp.query_fact_identity ON query_facts(exact_id,start_pin,end_pin);"
     "CREATE TEMP TABLE unsupported(path TEXT PRIMARY KEY,reason TEXT) WITHOUT ROWID;"
     "CREATE TEMP TABLE cone(net_seq INTEGER PRIMARY KEY);"
     "CREATE TEMP TABLE launches(instance TEXT,port TEXT,bit INTEGER,PRIMARY KEY(instance,port,bit)) WITHOUT ROWID;"
     "CREATE TEMP TABLE local_incoming AS SELECT c.net_id FROM assignment.cut_sinks c JOIN assignment.parts p ON p.part_index=c.sink_part_index WHERE p.fpga="+JsonSql(request.fpga)+";"
     "CREATE UNIQUE INDEX temp.local_incoming_id ON local_incoming(net_id);");
   ProjectExactQueryRecords(sources[2],descriptor,request.fpga,db,cancelled);
   Check(Scalar(db,"SELECT count(*) FROM assignment.parts WHERE fpga="+JsonSql(request.fpga))==1,"assignment FPGA absent");
 }
 // The resolver deliberately encapsulates its source. Namespace checks borrow
 // a separate bounded metadata reader, never a merged graph.
 SqliteDatabase& objects_owner(){if(!merged){merged=std::make_unique<SqliteStore>(sources[6]);}return merged->database();}
 static std::string JsonSql(const std::string& value){std::string out="'";for(char c:value){out+=c;if(c=='\'')out+='\'';}return out+'\'';}
 std::string Owner(const std::string& instance){auto row=db.Prepare("SELECT p.fpga FROM original.cell_records c JOIN assignment.copies a ON a.source_instance_seq=c.seq AND a.is_replica=0 JOIN assignment.parts p ON p.part_index=a.part_index WHERE c.id=?");row.BindText(1,instance);Check(row.Step(),"architectural endpoint owner missing");const auto value=row.TypedText(0);Check(!row.Step(),"architectural endpoint primary owner duplicates");return value;}
 PhysicalEndpoint Endpoint(SqliteStatement& row,int first){Check(!row.IsNull(first) && !row.IsNull(first+2) && !row.IsNull(first+3),"original member lacks structured endpoint identity");PhysicalEndpoint value;if(!row.IsNull(first+1))value.instance=row.TypedText(first+1);value.object=row.TypedText(first);value.port=row.TypedText(first+2);value.bit=row.TypedInteger(first+3);Check(value.bit>=0,"negative endpoint bit");return value;}
 PhysicalEndpoint Boundary(const std::string& id,const std::string& kind){
   auto row=db.Prepare("SELECT e.kind,e.external_port,e.external_port_bit,e.schedule_entry,e.demand,e.logical_net,e.link,e.peer,e.logical_lane,s.demand,s.net,s.link_id,s.source_fpga,s.sink_fpga,s.lane FROM boundary.endpoints e LEFT JOIN schedule.schedule_entries s ON s.entry_id=e.schedule_entry WHERE e.id=?");row.BindText(1,id);
   Check(row.Step() && row.TypedText(0)==kind && !row.IsNull(9),"scheduled boundary endpoint missing: "+id);
   Check(id=="__emuflow_"+kind+"_"+row.TypedText(3) && row.TypedText(4)==row.TypedText(9) && row.TypedText(5)==row.TypedText(10) && row.TypedText(6)==row.TypedText(11) && row.TypedInteger(8)==row.TypedInteger(14),"boundary identity disagrees with schedule: "+id);
   Check(row.TypedText(kind=="tx"?12:13)==request.fpga && row.TypedText(7)==row.TypedText(kind=="tx"?13:12),"boundary endpoint direction/peer disagrees: "+id);
   PhysicalEndpoint value;
   if(kind=="tx"){value.port=row.TypedText(1);value.bit=row.TypedInteger(2);}else{auto registers=db.Prepare("SELECT instance FROM boundary.boundary_registers WHERE endpoint_id=? ORDER BY ordinal");registers.BindText(1,id);Check(registers.Step(),"RX capture register missing");value.instance=registers.TypedText(0);value.port="Q";Check(!registers.Step(),"RX capture register ambiguous");}return value;
 }
 PhysicalEndpoint Driver(const std::string& net){auto row=db.Prepare("SELECT c.id,p.name,e.bit FROM original.net_records n JOIN original.endpoint_records e ON e.net_seq=n.seq AND e.role=0 LEFT JOIN original.cell_records c ON c.seq=e.instance_seq JOIN original.pin_names p ON p.seq=e.port_seq WHERE n.id=? ORDER BY e.ordinal");row.BindText(1,net);Check(row.Step(),"cut net lacks driver");PhysicalEndpoint result;if(!row.IsNull(0))result.instance=row.TypedText(0);result.port=row.TypedText(1);result.bit=row.TypedInteger(2);Check(!row.Step(),"cut net has multiple drivers");return result;}
 std::string Exact(const std::string& role,const std::optional<std::string>& source,const std::optional<std::string>& target,const std::optional<PhysicalEndpoint>& capture={}){
   auto row=db.Prepare("SELECT s.number,c.kind,c.endpoint,c.port,c.bit FROM exact_query_segments s LEFT JOIN exact_query_nodes a ON a.number=s.source LEFT JOIN exact_query_nodes b ON b.number=s.target AND s.kind!=2 LEFT JOIN exact_query_captures c ON c.number=s.target AND s.kind=2 WHERE s.kind=? AND a.net IS ? AND b.net IS ? ORDER BY s.number");row.BindInteger(1,role=="launch"?0:role=="transition"?1:2);if(source)row.BindText(2,*source);else row.BindNull(2);if(target)row.BindText(3,*target);else row.BindNull(3);
   std::optional<I> selected;
   while(row.Step()){
     if(capture){if(row.IsNull(1))continue;const auto endpoint=capture->instance?*capture->instance:"top:"+capture->port+"["+std::to_string(capture->bit)+"]";
       if(row.TypedText(2)!=endpoint || (capture->instance?(row.TypedInteger(1)!=1 || row.TypedText(3)!=capture->port || row.TypedInteger(4)!=capture->bit):row.TypedInteger(1)!=0))continue;}
     Check(!selected,"semantic identity mapping ambiguous");selected=row.TypedInteger(0);
   }Check(selected.has_value(),"original path segment absent from Static Exact contract");return Segment(*selected);
 }
 void Add(const std::string& id,const std::string& role,const std::string& path,const std::string& member,I cut,const std::optional<std::string>& tx,const PhysicalEndpoint& start,const PhysicalEndpoint& end,const std::string& exact,const std::optional<std::string>& anchor,bool supplemental=false){
   Stop(cancelled);const auto first=objects.Resolve(start),last=objects.Resolve(end);
   if(supplemental){auto existing=db.Prepare("SELECT 1 FROM query_facts WHERE exact_id=? AND start_pin=? AND end_pin=? LIMIT 1");existing.BindText(1,exact);existing.BindText(2,first.name);existing.BindText(3,last.name);if(existing.Step())return;}
   auto put=db.Prepare("INSERT INTO query_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)");put.BindText(1,id);put.BindText(2,role);put.BindText(3,path);put.BindText(4,member);put.BindInteger(5,cut);put.BindText(6,request.fpga);if(tx)put.BindText(7,*tx);else put.BindNull(7);put.BindText(8,first.name);put.BindText(9,last.name);put.BindText(10,exact);
   if(request.provider=="vivado"){put.BindText(11,first.kind);put.BindText(12,last.kind);if(anchor){const auto object=objects.Resolve(Driver(*anchor));put.BindText(13,object.kind);put.BindText(14,object.name);}else{put.BindNull(13);put.BindNull(14);}}else for(int i=11;i<=14;++i)put.BindNull(i);put.Execute();
 }
 void Members(){
   const auto board=ReadExactScheduleBoard(sources[3],sources[8].path());
   VisitScheduledTimingPaths(sources[3],sources[4],board,[&](const ScheduledTimingPath& path){Stop(cancelled);Check(!path.cuts.empty(),"cross path has no cuts");
     auto members=db.Prepare("SELECT value FROM json_each((SELECT record_json FROM routes.timing_paths WHERE seq=?),'$.compressed_path_ids') ORDER BY value");members.BindInteger(1,path.source_seq);
     I observed=0;const auto emit=[&](const std::string& member){Stop(cancelled);++observed;
       auto row=db.Prepare("SELECT start_object,start_instance,start_port,start_bit,end_object,end_instance,end_port,end_bit FROM paths.paths WHERE path_id=?");row.BindText(1,member);Check(row.Step(),"original timing member absent: "+member);auto start=Endpoint(row,0),end=Endpoint(row,4);
       try{start=objects.Normalize(start);end=objects.Normalize(end);if(start.instance)Check(Owner(*start.instance)==path.cuts.front().from,"launch endpoint partition mismatch");if(end.instance)Check(Owner(*end.instance)==path.cuts.back().to,"capture endpoint partition mismatch");}
       catch(const StageError& error){auto unsupported=db.Prepare("INSERT INTO unsupported VALUES(?,?)");unsupported.BindText(1,member);unsupported.BindText(2,error.what());unsupported.Execute();return;}
       ++exact_members;std::optional<std::string> prior;
       for(std::size_t k=0;k<path.cuts.size();++k){const auto& cut=path.cuts[k];Check(cut.hop_count>0,"cut path has no hop");const auto tx="__emuflow_tx_"+path.hops[cut.first_hop].entry,rx="__emuflow_rx_"+path.hops[cut.first_hop+cut.hop_count-1].entry;const std::string role=k?"transition":"launch";
         if(cut.from==request.fpga){const auto origin=k?Boundary(*prior,"rx"):start;const auto exact=Exact(role,k?std::optional<std::string>(path.cuts[k-1].net):std::nullopt,cut.net);const auto key=member+std::string(1,'\0')+std::to_string(k)+std::string(1,'\0')+role;Add("logic_"+Digest(key).substr(0,24)+"_"+std::to_string(k)+"_"+role,role,path.path,member,k,tx,origin,Boundary(tx,"tx"),exact,request.provider=="vivado"?std::optional<std::string>(cut.net):std::nullopt);}prior=rx;
       }
       if(path.cuts.back().to==request.fpga){const auto cut=path.cuts.size();const std::string role="capture",key=member+std::string(1,'\0')+std::to_string(cut)+std::string(1,'\0')+role;Add("logic_"+Digest(key).substr(0,24)+"_"+std::to_string(cut)+"_capture",role,path.path,member,cut,std::nullopt,Boundary(*prior,"rx"),end,Exact(role,path.cuts.back().net,std::nullopt,end),std::nullopt);}
     };
     if(!path.explicit_members)emit(path.path);else while(members.Step())emit(members.TypedText(0));Check(observed==I(path.explicit_members?path.member_count:1),"member coverage disagrees");
   });
 }
 void Launches(const std::string& net){
   db.Execute("DELETE FROM cone;DELETE FROM launches;");auto populate=db.Prepare(R"SQL(INSERT INTO cone WITH RECURSIVE walk(net_seq) AS (
 SELECT seq FROM original.net_records WHERE id=?1 UNION
 SELECT input.net_seq FROM walk w JOIN original.net_records n ON n.seq=w.net_seq
 JOIN original.endpoint_records driver ON driver.net_seq=w.net_seq AND driver.role=0
 JOIN original.cell_records cell ON cell.seq=driver.instance_seq
 JOIN assignment.copies own ON own.source_instance_seq=cell.seq AND own.is_replica=0
 JOIN assignment.parts part ON part.part_index=own.part_index AND part.fpga=?2
 JOIN original.endpoint_records input ON input.instance_seq=cell.seq AND input.role=1
 WHERE (n.id=?1 OR NOT EXISTS(SELECT 1 FROM local_incoming i WHERE i.net_id=n.id))
 AND NOT EXISTS(SELECT 1 FROM original.instance_resources r WHERE r.instance_seq=cell.seq AND r.resource IN ('ff','bram','bram18k','uram288') AND r.amount!=0)
 ) SELECT net_seq FROM walk)SQL");populate.BindText(1,net);populate.BindText(2,request.fpga);populate.Execute();
   auto foreign=db.Prepare("SELECT 1 FROM cone w JOIN original.net_records n ON n.seq=w.net_seq JOIN original.endpoint_records d ON d.net_seq=w.net_seq AND d.role=0 JOIN original.cell_records c ON c.seq=d.instance_seq LEFT JOIN assignment.copies a ON a.source_instance_seq=c.seq AND a.is_replica=0 LEFT JOIN assignment.parts p ON p.part_index=a.part_index WHERE (n.id=? OR NOT EXISTS(SELECT 1 FROM local_incoming i WHERE i.net_id=n.id)) AND (p.fpga IS NULL OR p.fpga!=?) LIMIT 1");foreign.BindText(1,net);foreign.BindText(2,request.fpga);Check(!foreign.Step(),"launch cone crosses undeclared partition boundary");
   auto insert=db.Prepare("INSERT OR IGNORE INTO launches SELECT coalesce(c.id,''),pin.name,d.bit FROM cone w JOIN original.net_records n ON n.seq=w.net_seq JOIN original.endpoint_records d ON d.net_seq=w.net_seq AND d.role=0 LEFT JOIN original.cell_records c ON c.seq=d.instance_seq JOIN original.pin_names pin ON pin.seq=d.port_seq WHERE (n.id=? OR NOT EXISTS(SELECT 1 FROM local_incoming i WHERE i.net_id=n.id)) AND (d.instance_seq IS NULL OR EXISTS(SELECT 1 FROM original.instance_resources r WHERE r.instance_seq=c.seq AND r.resource IN ('ff','bram','bram18k','uram288') AND r.amount!=0))");insert.BindText(1,net);insert.Execute();
 }
 void Semantics(){
   auto rows=db.Prepare("SELECT s.number,s.kind,s.constant,a.net,b.net,c.kind,c.endpoint,c.port,c.bit FROM exact_query_segments s LEFT JOIN exact_query_nodes a ON a.number=s.source LEFT JOIN exact_query_nodes b ON b.number=s.target AND s.kind!=2 LEFT JOIN exact_query_captures c ON c.number=s.target AND s.kind=2 ORDER BY s.number");
   while(rows.Step()){Stop(cancelled);const auto exact=Segment(rows.TypedInteger(0));const auto kind=rows.TypedInteger(1);const std::string role=kind==0?"launch":kind==1?"transition":"capture";
     const auto emit=[&](const PhysicalEndpoint& start,const PhysicalEndpoint& end,const std::optional<std::string>& tx,I ordinal,const std::optional<std::string>& anchor){const auto first=objects.Resolve(start),last=objects.Resolve(end);const auto key=exact+std::string(1,'\0')+first.name+std::string(1,'\0')+last.name+std::string(1,'\0')+std::to_string(ordinal),hash=Digest(key).substr(0,24);Add("static_exact_"+hash+"_"+role,role,"static-exact-contract:"+exact,"static-exact-contract:"+exact+":"+hash,ordinal,tx,start,end,exact,anchor,true);};
     if(kind==0 || kind==1){const auto target=rows.TypedText(4);auto txs=db.Prepare("SELECT entry_id FROM schedule.schedule_entries WHERE net=? AND source_fpga=? ORDER BY seq");txs.BindText(1,target);txs.BindText(2,request.fpga);bool found=false;I ordinal=0;
       if(kind==0){Launches(target);auto launches=db.Prepare("SELECT instance,port,bit FROM launches ORDER BY instance,port,bit");while(launches.Step()){Stop(cancelled);PhysicalEndpoint start;if(!launches.TypedText(0).empty())start.instance=launches.TypedText(0);start.port=launches.TypedText(1);start.bit=launches.TypedInteger(2);while(txs.Step()){found=true;const auto tx="__emuflow_tx_"+txs.TypedText(0);emit(start,Boundary(tx,"tx"),tx,ordinal++,request.provider=="vivado"?std::optional<std::string>(target):std::nullopt);}txs.Reset();txs.BindText(1,target);txs.BindText(2,request.fpga);}Check(ordinal>0 || rows.TypedInteger(2)==1,"nonconstant launch semantic has no architectural source");}
       else {const auto rx=Arrival(rows.TypedText(3));while(txs.Step()){found=true;const auto tx="__emuflow_tx_"+txs.TypedText(0);emit(Boundary(rx,"rx"),Boundary(tx,"tx"),tx,ordinal++,request.provider=="vivado"?std::optional<std::string>(target):std::nullopt);}}
       Check(found || (kind==0 && rows.TypedInteger(2)==1),"semantic cut has no local scheduled TX");
     }else{Check(!rows.IsNull(5),"capture semantic lacks capture owner");PhysicalEndpoint end;if(rows.TypedInteger(5)==0){const auto text=rows.TypedText(6);const auto bracket=text.rfind('[');Check(text.rfind("top:",0)==0 && bracket!=std::string::npos && text.back()==']',"top capture identity invalid");end.port=text.substr(4,bracket-4);end.bit=BoundedJson::IntegerToken(text.substr(bracket+1,text.size()-bracket-2));}else{end.instance=rows.TypedText(6);Check(objects.IsArchitecturalState(*end.instance),"capture target is not architectural state");end.port=rows.TypedText(7);end.bit=rows.TypedInteger(8);}emit(Boundary(Arrival(rows.TypedText(3)),"rx"),end,std::nullopt,0,std::nullopt);}
   }
 }
 std::string Arrival(const std::string& net){auto row=db.Prepare("SELECT entry_id FROM schedule.schedule_entries WHERE net=? AND sink_fpga=? ORDER BY seq");row.BindText(1,net);row.BindText(2,request.fpga);Check(row.Step(),"semantic cut has no RX");const auto result="__emuflow_rx_"+row.TypedText(0);Check(!row.Step(),"semantic cut has ambiguous RX");return result;}
 void Generate(){Members();Semantics();objects.RequireUnchanged();}
 I exact_members=0;
 std::string semantic_sha;
 private:
 SqliteDatabase& db;const std::vector<ArtifactRef>& sources;const Request& request;PhysicalQueryObjects objects;const volatile std::sig_atomic_t* cancelled;std::unique_ptr<SqliteStore> merged;
};

void InstallRows(SqliteDatabase& db){
 db.Execute("INSERT INTO segments SELECT row_number() OVER(ORDER BY id)-1,id,kind,system_path,member_path,cut_index,fpga,replace_tx,start_pin,end_pin,exact_id,start_kind,end_kind,anchor_kind,anchor_pin FROM query_facts ORDER BY id;"
     "INSERT INTO unsupported_members SELECT row_number() OVER(ORDER BY path)-1,path,reason FROM unsupported ORDER BY path;");
}
void QueryText(SqliteDatabase& db,const Request& request,const fs::path& target,const std::vector<ArtifactRef>& sources,const volatile std::sig_atomic_t* cancelled){
 PublishTextArtifact(target.string(),sources,[&](std::ostream& out){out<<(request.provider=="xilinx"?"endpoint\tkind\tstart_pin\tend_pin":"endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex")<<'\n';
   auto rows=db.Prepare("SELECT segment_id,kind,start_pin,end_pin,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin FROM segments ORDER BY seq");
   while(rows.Step()){Stop(cancelled);const auto id=rows.TypedText(0),kind=rows.TypedText(1),start=rows.TypedText(2),end=rows.TypedText(3);
     if(request.provider=="xilinx"){for(const auto& value:{id,kind,start,end})Check(value.find_first_of("\t\r\n")==std::string::npos,"Xilinx query field contains delimiter");out<<id<<'\t'<<kind<<'\t'<<start<<'\t'<<end<<'\n';}
     else out<<Hex(id)<<'\t'<<kind<<'\t'<<rows.TypedText(4)<<'\t'<<Hex(start)<<'\t'<<rows.TypedText(5)<<'\t'<<Hex(end)<<'\t'<<(rows.IsNull(6)?"":rows.TypedText(6))<<'\t'<<(rows.IsNull(7)?"":Hex(rows.TypedText(7)))<<'\n';
   }
 });
}
void AuditQueryText(SqliteDatabase& db,const Request& request,const fs::path& target,const volatile std::sig_atomic_t* cancelled){
 std::ifstream stream(target);Check(bool(stream),"independent query reader cannot open TSV");std::string line;
 Check(bool(std::getline(stream,line)) && line==(request.provider=="xilinx"?"endpoint\tkind\tstart_pin\tend_pin":"endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex"),"independent query header disagrees");
 auto rows=db.Prepare("SELECT segment_id,kind,start_pin,end_pin,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin FROM segments ORDER BY seq");
 while(rows.Step()){Stop(cancelled);Check(bool(std::getline(stream,line)),"independent query row missing");std::vector<std::string> fields;std::size_t begin=0;for(;;){const auto end=line.find('\t',begin);fields.push_back(line.substr(begin,end==std::string::npos?end:end-begin));if(end==std::string::npos)break;begin=end+1;}
   Check(fields.size()==(request.provider=="xilinx"?4:8),"independent query column coverage disagrees");
   if(request.provider=="xilinx")for(unsigned n=0;n<4;++n)Check(fields[n]==rows.TypedText(n),"independent query record disagrees");
   else{Check(fields[0]==Hex(rows.TypedText(0)) && fields[1]==rows.TypedText(1) && fields[2]==rows.TypedText(4) && fields[3]==Hex(rows.TypedText(2)) && fields[4]==rows.TypedText(5) && fields[5]==Hex(rows.TypedText(3)) && fields[6]==(rows.IsNull(6)?"":rows.TypedText(6)) && fields[7]==(rows.IsNull(7)?"":Hex(rows.TypedText(7))),"independent vendor query record disagrees");}
 }
 Check(!std::getline(stream,line) && stream.eof(),"independent query has extra rows or read failure");
}

} // namespace

StageResult RunLogicSegmentQuery(const std::vector<ArtifactRef>& inputs,const std::string& directory,const volatile std::sig_atomic_t* cancelled){
 std::optional<Request> request;std::string artifact;
 return RunStage("physical.query-logic-segments",[&]{request=ReadRequest(inputs);return inputs;},[&](const auto& sources){
   Stop(cancelled);const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"output generation directory exists");fs::create_directories(destination.parent_path());Workspace work(destination.parent_path());
   const auto private_store=work.root/"identity.sqlite";PhysicalEvidenceSeal seal;I exact_members=0;
   {
     SqliteArtifactWriter writer(private_store.string(),"logic-segment-identity-store-v1",sources);auto& db=writer.database();db.Execute(kLogicSegmentIdentityDDL);
     QueryPlan plan(db,sources,*request,cancelled);SqliteTransaction transaction(db);plan.Generate();exact_members=plan.exact_members;InstallRows(db);
     const Object header{{"schema","emuflow.logic-segment-identity/v1"},{"status","pass"},{"design",request->design},{"platform",request->platform},{"fpga",request->fpga},{"provider","sta-endpoint-chain-to-"+request->provider+"-objects-v1"},{"coverage",Object{}},
       {"system_routes_sha256",DecodeJsonString(request->fields.at("route_sha256"))},{"tdm_schedule_sha256",DecodeJsonString(request->fields.at("schedule_sha256"))},{"semantic_contract_sha256",plan.semantic_sha}};
     Object source{{"design",request->design},{"platform",request->platform},{"fpga",request->fpga},{"system_routes_sha256",DecodeJsonString(request->fields.at("route_sha256"))},{"tdm_schedule_sha256",DecodeJsonString(request->fields.at("schedule_sha256"))},{"semantic_contract_sha256",plan.semantic_sha}};
     seal=SealPhysicalEvidence(db,5,JsonRecord(header).EncodeUtf8(),JsonRecord(source).EncodeUtf8());
     transaction.Commit();QueryText(db,*request,work.root/request->query,sources,cancelled);AuditQueryText(db,*request,work.root/request->query,cancelled);(void)AuditPhysicalEvidenceStore(db,5);writer.Commit();
   }
     // The independent reader replays source facts into a fresh private SQL
     // owner and compares final rows, not producer counters or decisions.
     {
       SqliteDatabase audit(":memory:",SqliteDatabase::Mode::kReadOnly);QueryPlan expected(audit,sources,*request,cancelled);expected.Generate();Attach(audit,ArtifactRef::Bind(private_store.string(),"logic-segment-identity-store-v1"),"published");
       const auto expected_rows="SELECT row_number() OVER(ORDER BY id)-1,id,kind,system_path,member_path,cut_index,fpga,replace_tx,start_pin,end_pin,exact_id,start_kind,end_kind,anchor_kind,anchor_pin FROM query_facts";
       for(const auto& sql:{"SELECT * FROM published.segments EXCEPT SELECT * FROM ("+std::string(expected_rows)+")", "SELECT * FROM ("+std::string(expected_rows)+") EXCEPT SELECT * FROM published.segments"})Check(!audit.Prepare(sql).Step(),"independent original-source query coverage disagrees");
       Check(expected.exact_members==exact_members,"independent member qualification disagrees");
       auto absent=audit.Prepare("SELECT path,reason FROM unsupported EXCEPT SELECT path,reason FROM published.unsupported_members");Check(!absent.Step(),"independent unsupported coverage disagrees");
       auto extra=audit.Prepare("SELECT path,reason FROM published.unsupported_members EXCEPT SELECT path,reason FROM unsupported");Check(!extra.Step(),"independent unsupported coverage disagrees");
     }
   const auto hash=FileHash(private_store);artifact=request->identity+".store."+hash+".sqlite";fs::rename(private_store,work.root/artifact);
   const auto selector=std::string("{\"application_id\":")+std::to_string(0x454d5549)+",\"artifact\":"+JsonQuote(artifact)+",\"counts\":"+seal.counts+",\"header\":"+seal.header+",\"logical_schema\":\"emuflow.logic-segment-identity/v1\",\"logical_sha256\":"+JsonQuote(seal.logical_sha256)+",\"schema\":\"emuflow.logic-segment-identity-storage/v1\",\"source\":"+seal.source+",\"store_bytes\":"+std::to_string(fs::file_size(work.root/artifact))+",\"store_sha256\":"+JsonQuote(hash)+",\"user_version\":1}";
   PublishTextArtifact((work.root/request->identity).string(),sources,[&](std::ostream& out){out<<selector<<'\n';});
   const auto metrics=BoundedJson(seal.counts).Fields();Object report{{"schema","emuflow.logic-segment-query/v1"},{"status","pass"},{"implementation_id","native-logic-segment-query-v1"},{"fpga",request->fpga},{"segments",BoundedJson::IntegerToken(metrics.at("segments"))},{"exact_member_paths",exact_members},{"unsupported_member_paths",BoundedJson::IntegerToken(metrics.at("unsupported_member_paths"))},{"query",(destination/request->query).string()},{"identity",(destination/request->identity).string()},{"logical_sha256",seal.logical_sha256}};
   PublishTextArtifact((work.root/request->report).string(),sources,[&](std::ostream& out){out<<JsonRecord(report).Encode()<<'\n';});Stop(cancelled);PublishNewDirectory(work.root.string(),destination.string(),sources);work.root.clear();
 },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/request->query).string(),"logic-segment-query-tsv-v1"),ArtifactRef::Bind((fs::path(directory)/request->identity).string(),"logic-segment-identity-selector-v1"),ArtifactRef::Bind((fs::path(directory)/artifact).string(),"logic-segment-identity-store-v1"),ArtifactRef::Bind((fs::path(directory)/request->report).string(),"logic-segment-query-summary-v1")};});
}
} // namespace emuflow::core
