#include "physical/placement_lowering.hpp"
#include "physical/placement_lowering_sql.hpp"
#include "ir/emuir_view.hpp"
#include "transport/split_overlay.hpp"
#include "common/artifact_io.hpp"
#include "common/json_record.hpp"
#include "physical/physical_evidence.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "storage/schema/emuir_compact_schema.hpp"
#include "storage/schema/global_event_schema.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using I=std::int64_t;
using Raw=std::map<std::string,std::string>;
constexpr const char* space="__emuflow_transport__/";
void Check(bool value,const std::string& message){if(!value)throw StageError(ErrorCode::kInvalidArtifact,"placement lowering: "+message);}
void Stop(const volatile std::sig_atomic_t* cancelled){if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"placement lowering cancelled");}
Raw Fields(const std::string& text){Raw out;VisitJsonObjectTokens(text,[&](const auto& key,const auto& value){Check(out.size()<16384 && out.emplace(key,value).second,"duplicate/excessive control fields");});return out;}
std::string Get(const Raw& value,const std::string& key){auto found=value.find(key);Check(found!=value.end(),"missing "+key);return found->second;}
std::string Text(const Raw& value,const std::string& key){return DecodeJsonString(Get(value,key));}
std::string SqlText(const std::string& text){std::string result="'";for(const auto c:text){result+=c;if(c=='\'')result+='\'';}return result+'\'';}
std::string Object(const Raw& fields){std::string result="{";for(const auto& field:fields){if(result.size()>1)result+=',';result+=JsonQuote(field.first)+':'+field.second;}return result+'}';}
std::string Meta(SqliteDatabase& db,const std::string& key,const std::string& table="metadata"){auto q=db.Prepare("SELECT value FROM "+table+" WHERE key=?");q.BindText(1,key);Check(q.Step(),"missing metadata "+key);return q.TypedText(0);}
I Scalar(SqliteDatabase& db,const std::string& sql){auto q=db.Prepare(sql);Check(q.Step(),"missing scalar");return q.TypedInteger(0);}
void Attach(SqliteDatabase& db,const ArtifactRef& source,const std::string& alias){auto q=db.Prepare("ATTACH DATABASE ? AS "+alias);q.BindText(1,SqliteImmutableUri(source.path()));q.Execute();}
void Execute(SqliteDatabase& db,const std::string& sql,const std::vector<std::string>& values={}){auto q=db.Prepare(sql);for(std::size_t i=0;i<values.size();++i)q.BindText(int(i+1),values[i]);q.Execute();}
std::string Read(const ArtifactRef& source){Check(source.generation().bytes<=8*1024*1024,"request exceeds bound");std::ifstream f(source.path(),std::ios::binary);std::string text(source.generation().bytes,'\0');f.read(text.data(),text.size());Check(bool(f),"request is truncated");return text;}
std::string Hash(const fs::path& path){std::ifstream f(path,std::ios::binary);Check(bool(f),"cannot hash output");Sha256 h;std::array<char,65536> bytes;while(f){f.read(bytes.data(),bytes.size());h.update(reinterpret_cast<const unsigned char*>(bytes.data()),f.gcount());}Check(f.eof(),"output hash failed");std::string out;const char* digits="0123456789abcdef";for(auto b:h.finish()){out+=digits[b>>4];out+=digits[b&15];}return out;}
std::string Name(const std::string& value){std::string out;for(std::size_t i=0;i<value.size();){const auto c=static_cast<unsigned char>(value[i++]);if(c>=128){while(i<value.size() && (static_cast<unsigned char>(value[i])&0xc0)==0x80)++i;out+='_';}else out+=((c>='a' && c<='z') || (c>='A' && c<='Z') || (c>='0' && c<='9') || c=='_')?char(c):'_';}if(out.empty() || (out[0]>='0' && out[0]<='9'))out="n_"+out;return out;}
std::string FileName(const Raw& request,const std::string& key){auto name=Text(request,key);Check(!name.empty() && name!="." && name!=".." && fs::path(name).filename()==name && name.find('\0')==std::string::npos,"output name escapes stage");return name;}
struct Workspace{fs::path root;explicit Workspace(const fs::path& parent){auto path=(parent/".lowering-stage-XXXXXX").string();Check(mkdtemp(path.data())!=nullptr,"cannot create workspace");root=path;}~Workspace(){if(!root.empty()){std::error_code error;fs::remove_all(root,error);}}};
void RequireGeneration(const std::string& raw,const ArtifactRef& ref){
  const auto value=Fields(raw);Check(value.size()==3 && Text(value,"path")==ref.path() && Text(value,"schema")==ref.schema(),"original source namespace disagrees");
  const auto& g=ref.generation();const auto expected="["+std::to_string(g.device)+","+std::to_string(g.inode)+","+std::to_string(g.bytes)+","+std::to_string(g.modified_seconds)+","+std::to_string(g.modified_nanoseconds)+","+std::to_string(g.changed_seconds)+","+std::to_string(g.changed_nanoseconds)+"]";
  SqliteDatabase db(":memory:",SqliteDatabase::Mode::kCreateExclusive);
  RequireEquivalentJson(db,Get(value,"generation"),expected,"placement lowering original source generation changed");
}
struct Result{Raw counts,resources;I original=0,transport=0;std::string design,platform;};

Result Placement(const std::vector<ArtifactRef>& sources,const std::string& fpga,const fs::path& output,const volatile std::sig_atomic_t* cancelled){
  SqliteArtifactWriter writer(output.string(),"emuir-compact-v2",sources);auto& db=writer.database();
  db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-131072");db.Execute(kEmuirCompactDDL);db.Execute(kLoweringStageDDL);
  Attach(db,sources[0],"split_owner");auto original=PrepareSplitProjection(db,sources[0],"split_owner","split_");
  Attach(db,sources[1],"transport_store");Attach(db,sources[2],"transport_ir");
  const auto part=[&]{auto selected=db.Prepare("SELECT part_index FROM split_parts WHERE fpga=?");selected.BindText(1,fpga);Check(selected.Step(),"split omits requested FPGA");return std::to_string(selected.TypedInteger(0));}();
  Check(Scalar(db,"SELECT count(*) FROM transport_store.parts WHERE fpga="+SqlText(fpga))==1,"transport omits requested FPGA");
  const auto design=Fields(Meta(db,"design","split_metadata"));Result result;result.design=Text(design,"name");result.platform=DecodeJsonString(Meta(db,"platform","split_metadata"));
  Check(DecodeJsonString(Meta(db,"design","transport_store.metadata"))==result.design && DecodeJsonString(Meta(db,"platform","transport_store.metadata"))==result.platform,"split and transport identities disagree");
  const auto configuration=Fields(Meta(db,"configuration","transport_store.metadata"));
  std::vector<std::string> bindings;VisitJsonArrayTokens(Get(configuration,"sources"),[&](const auto& token){bindings.push_back(token);});
  Check(bindings.size()==2 || bindings.size()==3,"transport source coverage disagrees");
  Check(Text(Fields(bindings[0]),"path")==original[1].path() && Text(Fields(bindings[1]),"path")==original[2].path(),"split/transport assignment or schedule namespace disagrees");
  for(const auto& token:bindings){const auto row=Fields(token);RequireGeneration(token,ArtifactRef::Bind(Text(row,"path"),Text(row,"schema")));}
  const auto exec=[&](const std::string& sql,const std::vector<std::string>& values=std::vector<std::string>{}){Stop(cancelled);Execute(db,sql,values);};
  db.Execute(R"SQL(
CREATE TEMP TABLE dut_ports AS SELECT record_json FROM split_ports WHERE part_index=)SQL"+part+R"SQL(;
CREATE TEMP TABLE merged_ports(id TEXT PRIMARY KEY,record_json TEXT NOT NULL) WITHOUT ROWID;
INSERT INTO merged_ports SELECT json_extract(record_json,'$.id'),record_json FROM dut_ports;
CREATE TEMP TABLE shared_ports AS SELECT p.seq,json_extract(p.record_json,'$.id') AS id,
 json_extract(p.record_json,'$.width') AS width,p.record_json
 FROM transport_ir.port_records p JOIN merged_ports d ON d.id=json_extract(p.record_json,'$.id')
 WHERE d.id NOT IN ('source_values','shadow_values');
)SQL");
  Check(Scalar(db,R"SQL(SELECT count(*) FROM shared_ports p JOIN merged_ports d ON d.id=p.id WHERE
 json_extract(p.record_json,'$.direction')!='input' OR
 json_extract(p.record_json,'$.direction') IS NOT json_extract(d.record_json,'$.direction') OR
 json_extract(p.record_json,'$.width') IS NOT json_extract(d.record_json,'$.width') OR
 json_extract(p.record_json,'$.clock') IS NOT json_extract(d.record_json,'$.clock') OR
 json_extract(p.record_json,'$.reset') IS NOT json_extract(d.record_json,'$.reset'))SQL")==0,"transport top port is incompatible with DUT port");
  db.Execute(R"SQL(INSERT INTO merged_ports SELECT json_extract(record_json,'$.id'),record_json FROM transport_ir.port_records
 WHERE json_extract(record_json,'$.id') NOT IN ('source_values','shadow_values')
 AND json_extract(record_json,'$.id') NOT IN (SELECT id FROM merged_ports);
CREATE TEMP TABLE syn_top AS SELECT p.name AS port,e.bit,e.role,count(*) AS records,min(e.net_seq) AS net_seq
 FROM transport_ir.endpoint_records e JOIN transport_ir.pin_names p ON p.seq=e.port_seq
 WHERE e.instance_seq IS NULL GROUP BY p.name,e.bit,e.role;
CREATE UNIQUE INDEX syn_top_identity ON syn_top(port,bit,role);
CREATE TEMP TABLE shadow_map AS SELECT s.signal,s.seq,min(e.net) AS net,count(DISTINCT e.net) AS net_count,
 t.net_seq,t.records AS top_count,CASE WHEN n.source_net_seq IS NULL THEN 0 ELSE 1 END AS local_net
 FROM transport_store.signals s LEFT JOIN transport_store.endpoints e ON e.fpga=s.fpga AND e.kind='rx' AND e.signal=s.signal
 LEFT JOIN syn_top t ON t.port='shadow_values' AND t.bit=s.seq AND t.role=1
 LEFT JOIN split_source_nets n ON n.original_net=e.net AND EXISTS(
 SELECT 1 FROM split_net_segments ns WHERE ns.source_net_seq=n.source_net_seq AND ns.part_index=)SQL"+part+R"SQL()
 WHERE s.fpga=)SQL"+SqlText(fpga)+R"SQL( AND s.kind='shadow' GROUP BY s.signal,s.seq;
CREATE TEMP TABLE source_map AS SELECT s.signal,s.seq,
 CASE WHEN substr(s.signal,1,4)='net:' THEN substr(s.signal,5) ELSE s.signal END AS net,
 t.net_seq,t.records AS top_count,CASE WHEN n.source_net_seq IS NULL THEN 0 ELSE 1 END AS local_net
 FROM transport_store.signals s LEFT JOIN syn_top t ON t.port='source_values' AND t.bit=s.seq AND t.role=0
 LEFT JOIN split_source_nets n ON n.original_net=CASE WHEN substr(s.signal,1,4)='net:' THEN substr(s.signal,5) ELSE s.signal END
 AND EXISTS(SELECT 1 FROM split_net_segments ns WHERE ns.source_net_seq=n.source_net_seq AND ns.part_index=)SQL"+part+R"SQL()
 WHERE s.fpga=)SQL"+SqlText(fpga)+R"SQL( AND s.kind='source';
)SQL");
  Check(Scalar(db,"SELECT count(*) FROM shadow_map WHERE net_count!=1 OR coalesce(top_count,0)!=1")==0,"transport shadow signal or synthesized net coverage is incomplete");
  Check(Scalar(db,"SELECT count(*) FROM transport_store.endpoints e WHERE e.fpga="+SqlText(fpga)+" AND e.kind='rx' AND NOT EXISTS(SELECT 1 FROM shadow_map s WHERE s.signal=e.signal)")==0,"transport RX endpoints contain an unindexed shadow signal");
  Check(Scalar(db,"SELECT count(*) FROM source_map WHERE local_net!=1 OR coalesce(top_count,0)!=1")==0,"transport source net is not local or has no unique synthesized net");
  Check(Scalar(db,"SELECT count(*) FROM shared_ports p WHERE (SELECT count(*) FROM syn_top t WHERE t.port=p.id AND t.role=0 AND t.records=1 AND t.bit>=0 AND t.bit<p.width)!=p.width")==0,"shared transport input has no unique synthesized net");
  exec(kLoweringDutCells,{fpga,part});exec(kLoweringTransportCells,{space,space});exec(kLoweringBaseSinks,{part});db.Execute(kLoweringBaseSinkIndex);db.Execute(kLoweringExactTable);
  const auto digest=Meta(db,"semantic_contract_sha256","split_metadata");if(digest!="null")exec(kLoweringExactNets,{part,part});
  {auto q=db.Prepare(kLoweringDutNets);if(digest=="null")q.BindNull(1);else q.BindText(1,DecodeJsonString(digest));q.BindText(2,part);q.Execute();}
  if(digest!="null"){
    Check(original.size()==4,"exact boundary owner is missing");
    ProjectExactBoundarySegments(original[3],Meta(db,"semantic_contract_binary","split_metadata"),fpga,db);
    db.Execute(R"SQL(UPDATE stage_nets SET extras_json=json_set(extras_json,'$.static_exact_boundary.logic_segment_ids',json((
      SELECT json_group_array(segment_id) FROM (SELECT b.segment_id FROM exact_boundary_segments b
      WHERE b.net_id=stage_nets.id AND b.side=CASE json_extract(stage_nets.extras_json,'$.static_exact_boundary.role')
      WHEN 'cut_source' THEN 0 WHEN 'transport_shadow_sink' THEN 1 ELSE -1 END ORDER BY b.ordinal))))
      WHERE json_type(extras_json,'$.static_exact_boundary')='object')SQL");
  }
  exec(kLoweringDutEndpoints,{part});
  exec("INSERT OR IGNORE INTO expected_rx SELECT e.net,e.id FROM transport_store.endpoints e WHERE e.fpga=? AND e.kind='rx' AND EXISTS(SELECT 1 FROM shadow_map s WHERE s.net=e.net AND s.local_net=1)",{fpga});
  db.Execute("INSERT INTO requested_nets SELECT 0,net FROM source_map");db.Execute(kLoweringRequestedRx);
  Check(Scalar(db,std::string("SELECT count(*) FROM (")+kLoweringMissingRequested+")")==0,"transport requested net is not local");
  {auto q=db.Prepare(kLoweringRxMismatch);q.BindText(1,part);Check(!q.Step(),"transport RX placeholder coverage is incomplete");}
  exec(R"SQL(INSERT INTO stage_endpoints
SELECT s.net,e.role,1,CASE WHEN e.role=0 THEN 0 ELSE 2 END,0,
 row_number() OVER (ORDER BY s.seq,e.role,e.ordinal)-1,?||c.id,p.name,e.bit,x.value
FROM shadow_map s JOIN transport_ir.endpoint_records e ON e.net_seq=s.net_seq
JOIN transport_ir.cell_records c ON c.seq=e.instance_seq
JOIN transport_ir.pin_names p ON p.seq=e.port_seq JOIN transport_ir.payloads x ON x.seq=e.extras_seq
WHERE s.local_net=1)SQL",{space});
  const auto added=std::to_string(Scalar(db,"SELECT count(*) FROM stage_endpoints WHERE order_group=1"));
  exec(R"SQL(INSERT INTO stage_endpoints
SELECT s.net,1,1,1,0,?+row_number() OVER (ORDER BY s.seq,e.ordinal)-1,?||c.id,p.name,e.bit,x.value
FROM source_map s JOIN transport_ir.endpoint_records e ON e.net_seq=s.net_seq AND e.role=1
JOIN transport_ir.cell_records c ON c.seq=e.instance_seq
JOIN transport_ir.pin_names p ON p.seq=e.port_seq JOIN transport_ir.payloads x ON x.seq=e.extras_seq)SQL",{added,space});
  exec(R"SQL(INSERT INTO port_additions
SELECT s.id,t.bit,row_number() OVER (ORDER BY s.seq,t.bit,e.ordinal)-1,?||c.id,p.name,e.bit,x.value
FROM shared_ports s JOIN syn_top t ON t.port=s.id AND t.role=0
JOIN transport_ir.endpoint_records e ON e.net_seq=t.net_seq AND e.role=1
JOIN transport_ir.cell_records c ON c.seq=e.instance_seq
JOIN transport_ir.pin_names p ON p.seq=e.port_seq JOIN transport_ir.payloads x ON x.seq=e.extras_seq)SQL",{space});
  exec(kLoweringPortEndpoints,{part});exec(kLoweringPortFanout,{part});
  // Count distinct DUT nets, not additions: one clock/reset may have many sinks.
  {std::string sql=kLoweringUnmatchedPort;const std::string needle="count(e.source_net_seq)",replacement="count(DISTINCT e.source_net_seq)";std::size_t at=0;while((at=sql.find(needle,at))!=std::string::npos){sql.replace(at,needle.size(),replacement);at+=replacement.size();}auto q=db.Prepare(sql);q.BindText(1,part);Check(!q.Step(),"shared transport input does not bind one DUT net");}
  db.Execute(R"SQL(INSERT OR IGNORE INTO fanout_recompute SELECT net FROM shadow_map WHERE local_net=1;
CREATE TEMP TABLE consumed_transport_nets(id TEXT PRIMARY KEY) WITHOUT ROWID;
INSERT OR IGNORE INTO consumed_transport_nets SELECT n.id FROM shadow_map s JOIN transport_ir.net_records n ON n.seq=s.net_seq WHERE s.local_net=1;
INSERT OR IGNORE INTO consumed_transport_nets SELECT n.id FROM source_map s JOIN transport_ir.net_records n ON n.seq=s.net_seq;
INSERT OR IGNORE INTO consumed_transport_nets SELECT n.id FROM shared_ports s JOIN syn_top t ON t.port=s.id AND t.role=0 JOIN transport_ir.net_records n ON n.seq=t.net_seq;
INSERT OR IGNORE INTO consumed_transport_nets SELECT n.id FROM syn_top t JOIN transport_ir.net_records n ON n.seq=t.net_seq
 WHERE t.port IN ('source_values','shadow_values') AND NOT EXISTS(SELECT 1 FROM shadow_map s WHERE s.net_seq=n.seq AND s.local_net=0);
)SQL");
  exec(kLoweringTransportNets,{space,space});exec(kLoweringTransportAliases,{space});exec(kLoweringTransportEndpoints,{space,space});db.Execute(kLoweringFanout);
  Check(Scalar(db,std::string("SELECT count(*) FROM (")+kLoweringMissingCell+")")==0,"net references missing instance");
  Check(Scalar(db,std::string("SELECT count(*) FROM (")+kLoweringMissingNet+")")==0,"endpoint references missing net");
  Stop(cancelled);db.Execute(kLoweringIntern);
  db.Execute("INSERT INTO port_records SELECT NULL,json_set(record_json,'$.constant_connections',json(coalesce(json_extract(record_json,'$.constant_connections'),'[]'))) FROM merged_ports ORDER BY id");
  db.Execute("INSERT INTO clocks SELECT NULL,json_object('id',id,'name',json_extract(record_json,'$.name'),'source_port',id,'period_ns',NULL) FROM merged_ports WHERE json_extract(record_json,'$.clock')=1 ORDER BY id");
  auto merged=design;merged["name"]=JsonQuote(result.design+"__"+fpga);merged["top"]=JsonQuote(Text(design,"top")+"__"+fpga);merged["source_format"]=JsonQuote("emuflow-phase7-merged");
  result.counts={{"cell_records",std::to_string(Scalar(db,"SELECT count(*) FROM cell_records"))},{"net_records",std::to_string(Scalar(db,"SELECT count(*) FROM net_records"))},{"endpoint_records",std::to_string(Scalar(db,"SELECT count(*) FROM endpoint_records"))}};
  const Raw metadata{{"storage_schema","emuflow.emuir-sqlite/v2"},{"logical_schema","emuflow.emuir/v1"},{"design_json",Object(merged)},{"warnings_json",Meta(db,"warnings_json","transport_ir.metadata")},{"record_counts_json",Object(result.counts)}};
  for(const auto& item:metadata)exec("INSERT INTO metadata VALUES (?,?)",{item.first,item.second});
  db.Execute(kLoweringEndpointIndex);result.original=Scalar(db,"SELECT count(*) FROM split_instance_copies WHERE part_index="+part);result.transport=Scalar(db,"SELECT count(*) FROM cell_records WHERE substr(id,1,length('__emuflow_transport__/'))='__emuflow_transport__/'");
  {auto resources=db.Prepare("SELECT resource,sum(amount) FROM instance_resources GROUP BY resource ORDER BY resource");while(resources.Step())result.resources.emplace(resources.TypedText(0),std::to_string(resources.TypedInteger(1)));}
  Stop(cancelled);for(const auto& source:original)source.RequireUnchanged();writer.Commit();return result;
}

PhysicalEvidenceSeal Boundary(const std::vector<ArtifactRef>& sources,const std::string& fpga,const fs::path& placement,const fs::path& output,const Result& result,const volatile std::sig_atomic_t* cancelled){
  auto bound=sources;bound.push_back(ArtifactRef::Bind(placement.string(),"emuir-compact-v2"));
  SqliteArtifactWriter writer(output.string(),"boundary-identity-store-v1",bound);auto& db=writer.database();db.Execute(kBoundaryIdentityDDL);db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-65536");
  Attach(db,bound.back(),"merged");Attach(db,sources[1],"transport");Attach(db,sources[2],"synth");
  db.Execute(R"SQL(CREATE TEMP TABLE link_ports(kind TEXT,link TEXT,peer TEXT,port TEXT,PRIMARY KEY(kind,link,peer)) WITHOUT ROWID;
CREATE TEMP TABLE top_map AS SELECT p.name AS port,e.bit,count(DISTINCT e.net_seq) AS net_count,min(n.id) AS net
 FROM merged.endpoint_records e JOIN merged.pin_names p ON p.seq=e.port_seq JOIN merged.net_records n ON n.seq=e.net_seq
 WHERE e.instance_seq IS NULL GROUP BY p.name,e.bit;
CREATE UNIQUE INDEX top_identity ON top_map(port,bit);
CREATE TEMP TABLE endpoint_registers(endpoint_id TEXT,instance TEXT,PRIMARY KEY(endpoint_id,instance)) WITHOUT ROWID;
)SQL");
  Check(Scalar(db,"SELECT count(*) FROM top_map WHERE net_count!=1")==0,"physical boundary port is on multiple nets");
  {auto q=db.Prepare("SELECT DISTINCT kind,link,peer FROM transport.endpoints WHERE fpga=? ORDER BY kind,link,peer");q.BindText(1,fpga);while(q.Step()){const auto kind=q.TypedText(0),link=q.TypedText(1),peer=q.TypedText(2);Execute(db,"INSERT INTO link_ports VALUES (?,?,?,?)",{kind,link,peer,kind+'_'+Name(link)+'_'+Name(peer)});}}
  Execute(db,R"SQL(INSERT INTO endpoints SELECT e.id,e.kind,e.schedule_entry,e.demand,e.net,e.link,e.peer,e.lane,e.logical_lane,e.signal,
 CASE WHEN e.kind='rx' THEN 'captured-shadow' WHEN substr(e.signal,1,4)='net:' THEN 'dut-net' ELSE 'forwarded-shadow' END,
 p.port,e.lane,t.net,n.id FROM transport.endpoints e
 JOIN link_ports p ON p.kind=e.kind AND p.link=e.link AND p.peer=e.peer
 LEFT JOIN top_map t ON t.port=p.port AND t.bit=e.lane LEFT JOIN merged.net_records n ON n.id=e.net WHERE e.fpga=?)SQL",{fpga});
  Check(Scalar(db,"SELECT count(*) FROM endpoints e LEFT JOIN merged.port_records p ON json_extract(p.record_json,'$.id')=e.external_port WHERE p.seq IS NULL OR json_extract(p.record_json,'$.direction')!=CASE WHEN e.kind='tx' THEN 'output' ELSE 'input' END")==0,"boundary endpoint cannot bind external port");
  Check(Scalar(db,"SELECT count(*) FROM endpoints WHERE source_class='dut-net' AND merged_logical_net IS NULL")==0,"TX DUT source net is absent");
  Execute(db,R"SQL(INSERT OR IGNORE INTO endpoint_registers
 SELECT e.id,?||c.id FROM endpoints e JOIN transport.signals s ON s.signal=e.internal_signal AND s.kind='shadow' AND s.fpga=?
 JOIN synth.pin_names p ON p.name='shadow_values' JOIN synth.endpoint_records t ON t.instance_seq IS NULL AND t.role=1 AND t.port_seq=p.seq AND t.bit=s.seq
 JOIN synth.endpoint_records d ON d.net_seq=t.net_seq AND d.role=0
 JOIN synth.cell_records c ON c.seq=d.instance_seq)SQL",{space,fpga});
  db.Execute("CREATE TEMP TABLE need_fallback AS SELECT id FROM endpoints e WHERE NOT EXISTS(SELECT 1 FROM endpoint_registers r WHERE r.endpoint_id=e.id)");
  Execute(db,R"SQL(INSERT OR IGNORE INTO endpoint_registers
 SELECT e.id,c.id FROM endpoints e JOIN merged.net_records n ON n.id=e.merged_logical_net
 JOIN merged.endpoint_records d ON d.net_seq=n.seq AND d.role=0 JOIN merged.cell_records c ON c.seq=d.instance_seq
 WHERE substr(c.id,1,length(?))=? AND e.id IN (SELECT id FROM need_fallback))SQL",{space,space});
  Check(Scalar(db,"SELECT count(*) FROM endpoints e WHERE e.source_class!='dut-net' AND (SELECT count(*) FROM endpoint_registers r WHERE r.endpoint_id=e.id)!=1")==0,"transport endpoint lacks one shadow register");
  db.Execute("INSERT INTO boundary_registers SELECT endpoint_id,row_number() OVER (PARTITION BY endpoint_id ORDER BY instance)-1,instance FROM endpoint_registers ORDER BY endpoint_id,instance");
  {auto q=db.Prepare("SELECT count(*) FROM transport.endpoints WHERE fpga=?");q.BindText(1,fpga);Check(q.Step() && q.TypedInteger(0)==Scalar(db,"SELECT count(*) FROM endpoints"),"boundary endpoint coverage disagrees");}
  const Raw header{{"schema",JsonQuote("emuflow.boundary-identity/v1")},{"status",JsonQuote("pass")},{"design",JsonQuote(result.design)},{"platform",JsonQuote(result.platform)},{"fpga",JsonQuote(fpga)},{"provider",JsonQuote("phase6-endpoint-to-merged-emuir-identity-v1")},{"coverage","{}"}};
  const auto seal=SealPhysicalEvidence(db,1,Object(header),"null");Stop(cancelled);writer.Commit();return seal;
}
}

StageResult LowerPlacement(const std::vector<ArtifactRef>& original,const std::string& directory,const volatile std::sig_atomic_t* cancelled){
  std::vector<ArtifactRef> sources=original;Raw request;std::string fpga,placement_name,boundary_name,report_name;
  return RunStage("phase7.lower",[&]{
    Check(original.size()==4 && original[0].schema()=="split-netlists-v3" && original[1].schema()=="transport-store-v2" && original[2].schema()=="emuir-compact-v2" && original[3].schema()=="placement-lowering-request-v1","source roles disagree");
    request=Fields(Read(original[3]));Check(request.size()==6 && Text(request,"schema")=="emuflow.placement-lowering/v1","unsupported request");
    fpga=Text(request,"fpga");Check(!fpga.empty(),"missing FPGA identity");placement_name=FileName(request,"output_name");boundary_name=FileName(request,"boundary_name");report_name=FileName(request,"report_name");
    Check(std::set<std::string>{placement_name,boundary_name,report_name}.size()==3,"output names collide");
    std::size_t at=0;VisitJsonArrayTokens(Get(request,"sources"),[&](const auto& value){Check(at<3,"excess source binding");RequireGeneration(value,original[at++]);});Check(at==3,"missing source binding");
    SqliteStore split(original[0]);auto parents=PrepareSplitProjection(split.database(),original[0]);sources.insert(sources.end(),parents.begin(),parents.end());
    SqliteStore transport(original[1]);std::size_t role=0;
    VisitJsonArrayTokens(Get(Fields(Meta(transport.database(),"configuration")),"sources"),[&](const auto& token){
      const char* roles[]={"assignment-index-v4","tdm-schedule-store-v4","pin-plan-store-v1"};
      const auto row=Fields(token);Check(role<3 && Text(row,"schema")==roles[role++],"transport original source role disagrees");
      auto source=ArtifactRef::Bind(Text(row,"path"),Text(row,"schema"));RequireGeneration(token,source);sources.push_back(source);
    });Check(role==2 || role==3,"transport original source coverage disagrees");
    EmuirView synthesized(original[2]);(void)synthesized.counts();return sources;
  },[&](const auto& refs){
    const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"output directory exists");fs::create_directories(destination.parent_path());Workspace work(destination.parent_path());
    const auto result=Placement(refs,fpga,work.root/placement_name,cancelled);
    const auto temporary_boundary=work.root/"boundary.sqlite";auto boundary=Boundary(refs,fpga,work.root/placement_name,temporary_boundary,result,cancelled);
    const auto hash=Hash(temporary_boundary),artifact=boundary_name+".store."+hash+".sqlite";fs::rename(temporary_boundary,work.root/artifact);
    const Raw selector{{"schema",JsonQuote("emuflow.boundary-identity-storage/v1")},{"logical_schema",JsonQuote("emuflow.boundary-identity/v1")},{"application_id",std::to_string(0x454d5542)},{"user_version","1"},{"header",boundary.header},{"counts",boundary.counts},{"logical_sha256",JsonQuote(boundary.logical_sha256)},{"artifact",JsonQuote(artifact)},{"store_bytes",std::to_string(fs::file_size(work.root/artifact))},{"store_sha256",JsonQuote(hash)}};
    PublishTextArtifact((work.root/boundary_name).string(),refs,[&](std::ostream& out){out<<Object(selector)<<'\n';});
    auto coverage=Fields(boundary.counts);Raw validation{{"status",JsonQuote("pass")},{"fpga",JsonQuote(fpga)},{"endpoints",Get(coverage,"endpoints")},{"tx",Get(coverage,"tx")},{"rx",Get(coverage,"rx")}};
    Raw report{{"schema",JsonQuote("emuflow.placement-ir-report/v1")},{"status",JsonQuote("pass")},{"implementation_id",JsonQuote("native-placement-lowering-v1")},{"design",JsonQuote(result.design+"__"+fpga)},
      {"instances",Get(result.counts,"cell_records")},{"nets",Get(result.counts,"net_records")},{"resource_totals",Object(result.resources)},{"transport_instances",std::to_string(result.transport)},{"original_instances",std::to_string(result.original)},{"output",JsonQuote((destination/placement_name).string())},
      {"boundary_identity",Object({{"schema",JsonQuote("emuflow.boundary-identity/v1")},{"output",JsonQuote((destination/boundary_name).string())},{"validation",Object(validation)}})}};
    PublishTextArtifact((work.root/report_name).string(),refs,[&](std::ostream& out){out<<Object(report)<<'\n';});Stop(cancelled);PublishNewDirectory(work.root.string(),destination.string(),refs);work.root.clear();
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/placement_name).string(),"emuir-compact-v2"),ArtifactRef::Bind((fs::path(directory)/boundary_name).string(),"boundary-identity-selector-v1"),ArtifactRef::Bind((fs::path(directory)/report_name).string(),"placement-lowering-report-v1")};});
}
}
