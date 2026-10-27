#include "emuflow_core/local_path_query.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/bounded_json.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/global_event_schema.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/physical_evidence.hpp"
#include "emuflow_core/physical_query_objects.hpp"
#include "emuflow_core/sta_paths.hpp"
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <optional>
#include <set>
#include <sstream>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using I=std::int64_t;
using Doc=BoundedJson;
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
constexpr const char* kPartitionDDL=R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE path_ownership(path_ordinal INTEGER PRIMARY KEY NOT NULL CHECK(path_ordinal>=0),scope INTEGER NOT NULL CHECK(scope IN(0,1)),fpga TEXT,CHECK((scope=0 AND fpga IS NOT NULL AND length(fpga)>0) OR (scope=1 AND fpga IS NULL)));
CREATE INDEX path_ownership_fpga ON path_ownership(fpga,path_ordinal);
)SQL";
void Check(bool condition,const std::string& message){if(!condition)throw StageError(ErrorCode::kInvalidArtifact,"local path query: "+message);}
void Stop(const volatile std::sig_atomic_t* cancelled){if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"local path query cancelled");}
std::string Text(SqliteStatement& q,int column){auto text=q.TypedText(column);Check(!text.empty(),"empty identity");(void)JsonQuoteUtf8(text);return text;}
std::string HexDigest(const std::array<unsigned char,32>& digest){std::string out;for(auto b:digest){out+="0123456789abcdef"[b>>4];out+="0123456789abcdef"[b&15];}return out;}
void Hash(Sha256& hash,std::string_view bytes){hash.update(reinterpret_cast<const unsigned char*>(bytes.data()),bytes.size());}
std::string FileHash(const fs::path& path){std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot seal input");Sha256 hash;std::array<char,65536> block;while(in){in.read(block.data(),block.size());hash.update(reinterpret_cast<const unsigned char*>(block.data()),in.gcount());}Check(in.eof(),"source read failed");return HexDigest(hash.finish());}
std::string HexText(const std::string& value){std::string out;for(unsigned char c:value){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;}
std::string SafeQuery(const std::string& value){Check(value.find_first_of("\t\r\n")==std::string::npos && value.find('\0')==std::string::npos,"query identity contains a control delimiter");return value;}
std::string Meta(SqliteDatabase& db,const char* key){auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,key);Check(q.Step(),std::string("missing metadata ")+key);auto text=q.TypedText(0);Check(!q.Step(),"duplicate metadata");return text;}
void Put(SqliteDatabase& db,const char* key,const std::string& value){auto q=db.Prepare("INSERT INTO metadata VALUES(?,?)");q.BindText(1,key);q.BindText(2,value);q.Execute();}
void Attach(SqliteDatabase& db,const char* name,const ArtifactRef& source){auto q=db.Prepare(std::string("ATTACH DATABASE ? AS ")+name);q.BindText(1,SqliteImmutableUri(source.path()));q.Execute();}
bool Exists(SqliteDatabase& db,const std::string& sql){auto q=db.Prepare(sql);return q.Step();}
I Scalar(SqliteDatabase& db,const std::string& sql){auto q=db.Prepare(sql);Check(q.Step(),"missing aggregate");auto value=q.TypedInteger(0);Check(!q.Step(),"duplicate aggregate");return value;}
struct Workspace{fs::path root;explicit Workspace(const fs::path& parent){auto pattern=(parent/".local-path-stage-XXXXXX").string();Check(mkdtemp(pattern.data())!=nullptr,"cannot create generation");root=pattern;}~Workspace(){std::error_code error;fs::remove_all(root,error);}};
void Write(const fs::path& path,const std::string& text,const std::vector<ArtifactRef>& inputs){PublishTextArtifact(path.string(),inputs,[&](std::ostream& out){out<<text;});}
void RequireSources(const std::vector<ArtifactRef>& inputs){for(const auto& input:inputs)input.RequireUnchanged();}
std::string Sources(const std::vector<ArtifactRef>& inputs,std::size_t count){Array rows;for(std::size_t i=0;i<count;++i){const auto& g=inputs[i].generation();Array values;for(auto value:{g.device,g.inode,g.bytes})values.emplace_back(std::to_string(value));for(auto value:{g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds})values.emplace_back(std::to_string(value));rows.emplace_back(Object{{"path",inputs[i].path()},{"schema",inputs[i].schema()},{"generation",values}});}return JsonRecord(rows).EncodeUtf8();}
struct Request{std::string backend,fpga;};
Request ReadRequest(const std::vector<ArtifactRef>& inputs,bool query){
  const std::vector<std::string> schemas=query?std::vector<std::string>{"emuir-compact-v2","assignment-index-v4","timing-path-store-v1","system-route-store-v3","physical-input-document-v1","sta-path-selector-v1","system-route-selector-v1","local-path-partition-store-v1","emuir-compact-v2","xilinx-primitive-library-v1","local-path-query-request-v1"}:std::vector<std::string>{"emuir-compact-v2","assignment-index-v4","timing-path-store-v1","system-route-store-v3","physical-input-document-v1","sta-path-selector-v1","system-route-selector-v1","local-path-prepare-request-v1"};
  Check(inputs.size()==schemas.size(),"source coverage disagrees");for(std::size_t i=0;i<inputs.size();++i)Check(inputs[i].schema()==schemas[i],"source schema disagrees");
  CheckedRequestReader reader(inputs.back().path(),8*1024*1024);
  Check(reader.Read<std::uint64_t>()==(query?0x31514C51464D5545ULL:0x31504C51464D5545ULL) && reader.Read<std::uint32_t>()==1,"request version disagrees");
  for(std::size_t i=0;i+1<inputs.size();++i){const auto expected=ReadFileGeneration(reader);if(!(expected==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"local path input generation changed");}
  Request request;if(query){request.backend=reader.ReadString32(false,16);request.fpga=reader.ReadString32(false,4096);Check(request.backend=="xilinx" || request.backend=="vivado","unsupported backend");SafeQuery(request.fpga);}reader.RequireEnd();return request;
}
void AttachOriginal(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs){Attach(db,"original",inputs[0]);Attach(db,"assignment",inputs[1]);Attach(db,"paths",inputs[2]);Attach(db,"routes",inputs[3]);db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-8192; PRAGMA temp.cache_size=-8192");}
std::string Design(SqliteDatabase& db){auto q=db.Prepare("SELECT json_extract(value,'$.name') FROM original.metadata WHERE key='design_json'");Check(q.Step(),"missing design");return Text(q,0);}
void ValidateSelectors(const std::vector<ArtifactRef>& inputs,const std::string& design){
  // A selector is only a bounded identity/ref document. The C++ owner checks
  // its payload name, size/hash and count rather than trusting host lookups.
  Doc paths(Doc::Read(inputs[5],8*1024*1024));Check(paths.Text("schema")=="emuflow.sta-path-database/v3" && paths.Text("design")==design,"path selector identity disagrees");Doc path_origin(paths.Token("source"));Check(path_origin.Text("provider")=="opensta-fpga-path-database-v1","unsupported path source provider");Doc payloads(paths.Token("payloads"));Doc path(payloads.Token("paths"));
  Check(path.Text("format")=="sqlite-sta-path/v1" && (fs::path(inputs[5].path()).parent_path()/path.Text("path")).lexically_normal()==fs::path(inputs[2].path()),"path payload owner disagrees");Check(FileHash(inputs[2].path())==path.Text("sha256"),"path payload seal disagrees");
  if(inputs[4].path()!=inputs[1].path()){
    Doc assignment(Doc::Read(inputs[4],8*1024*1024));Check(assignment.Text("schema")=="emuflow.phase3-assignment-storage/v4","assignment selector schema disagrees");Doc document(assignment.Token("document"));Check(document.Text("schema")=="emuflow.partition-assignment/v1" && document.Text("design")==design,"assignment selector identity disagrees");Doc cuts(assignment.Token("cut_nets_index"));Check((fs::path(inputs[4].path()).parent_path()/cuts.Text("artifact")).lexically_normal()==fs::path(inputs[1].path()),"assignment payload owner disagrees");SqliteStore index(inputs[1]);Check(cuts.Text("sha256")==DecodeJsonString(Meta(index.database(),"cut_records_sha256")),"assignment cut seal disagrees");

  }
  SqliteStore assignment_store(inputs[1]),route_store(inputs[3]);Check(DecodeJsonString(Meta(assignment_store.database(),"design"))==design,"assignment source design disagrees");Doc route_header(Meta(route_store.database(),"header"));Check(route_header.Text("design")==design,"route source design disagrees");
  Doc routes(Doc::Read(inputs[6],8*1024*1024));Check(routes.Text("schema")=="emuflow.system-route-storage/v1","route selector identity disagrees");Check((fs::path(inputs[6].path()).parent_path()/routes.Text("artifact")).lexically_normal()==fs::path(inputs[3].path()) && routes.Text("store_sha256")==FileHash(inputs[3].path()),"route payload owner/seal disagrees");
}
Object Manifest(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs){
  Sha256 ids;Hash(ids,"[");I count=0;auto paths=db.Prepare("SELECT path_id FROM paths.paths ORDER BY path_id");while(paths.Step()){if(count++)Hash(ids,",");Hash(ids,JsonQuoteUtf8(Text(paths,0)));}Hash(ids,"]");Check(count>0,"empty original path population");
  return {{"path_database_sha256",FileHash(inputs[5].path())},{"original_ir_sha256",FileHash(inputs[0].path())},{"assignment_sha256",FileHash(inputs[4].path())},{"routes_sha256",FileHash(inputs[6].path())},{"original_paths",count},{"original_path_ids_sha256",HexDigest(ids.finish())}};
}
void ValidateOriginal(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs,const fs::path& scratch){
  EmuirView emuir(inputs[0]);AssignmentView assignment(inputs[1]);
  Check(assignment.source_cells()==emuir.counts().cells,"assignment population differs from source");
  Check(!Exists(db,"SELECT 1 FROM assignment.copies p LEFT JOIN original.cell_records c ON c.seq=p.source_instance_seq WHERE p.is_replica=0 AND (c.seq IS NULL OR c.id!=p.copy_id) LIMIT 1"),"assignment primary identities disagree");
  Check(!Exists(db,"SELECT 1 FROM original.cell_records c LEFT JOIN assignment.copies p ON p.source_instance_seq=c.seq AND p.is_replica=0 GROUP BY c.seq HAVING count(p.copy_id)!=1 LIMIT 1"),"assignment primary coverage is incomplete");
  const auto design=Design(db);ValidateSelectors(inputs,design);
  Doc path_selector(Doc::Read(inputs[5]));const auto config=Doc::Encode({{"normalization",path_selector.Token("normalization")},{"summary",path_selector.Token("summary")},{"design",JsonQuote(design)},{"provider",JsonQuote("opensta-fpga-path-database-v1")}});
  const auto request=scratch/"path-audit.bin";CheckedRequestWriter writer(request.string(),inputs);writer.Put<std::uint64_t>(0x31505453464D5545ULL);writer.Put<std::uint32_t>(1);writer.Generation(inputs[0].generation());writer.Generation(inputs[2].generation());writer.Text(config);writer.Finish();
  ValidateStaPaths({inputs[0],inputs[2],ArtifactRef::Bind(request.string(),"sta-path-request-v1")},(scratch/"path-audit.json").string());
}
void BuildCross(SqliteDatabase& db){
  // Member populations remain SQL relations, including a representative with
  // millions of original members; no bounded JSON document owns this array.
  Check(!Exists(db,"SELECT 1 FROM routes.timing_paths WHERE json_type(record_json,'$.path') IS NOT 'text' OR json_extract(record_json,'$.path')!=path_id OR (json_type(record_json,'$.compressed_path_ids') IS NOT NULL AND (json_type(record_json,'$.compressed_path_ids')!='array' OR json_array_length(record_json,'$.compressed_path_ids')=0)) LIMIT 1"),"route member identity/schema disagrees");
  Check(!Exists(db,"SELECT 1 FROM routes.timing_paths p,json_each(p.record_json,'$.compressed_path_ids') m WHERE m.type!='text' OR length(m.value)=0 LIMIT 1"),"route member is invalid");
  db.Execute("CREATE TEMP TABLE cross_ids(path_id TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID; INSERT INTO cross_ids SELECT path_id FROM routes.timing_paths WHERE json_type(record_json,'$.compressed_path_ids') IS NULL UNION ALL SELECT m.value FROM routes.timing_paths p,json_each(p.record_json,'$.compressed_path_ids') m");
  Check(!Exists(db,"SELECT 1 FROM cross_ids r LEFT JOIN paths.paths p ON p.path_id=r.path_id WHERE p.ordinal IS NULL LIMIT 1"),"route member is absent from original paths");
}
void Partition(SqliteDatabase& db){
  // The producer uses relational ownership. The independent audit below
  // resolves each original path afresh without reusing these evidence rows.
  db.Execute("CREATE TEMP TABLE candidates(path_ordinal INTEGER PRIMARY KEY) WITHOUT ROWID; INSERT INTO candidates SELECT p.ordinal FROM paths.paths p LEFT JOIN cross_ids r ON r.path_id=p.path_id WHERE r.path_id IS NULL");
  Check(!Exists(db,"SELECT 1 FROM candidates q JOIN paths.paths p ON p.ordinal=q.path_ordinal WHERE p.start_object IS NULL OR p.start_port IS NULL OR p.start_bit IS NULL OR p.end_object IS NULL OR p.end_port IS NULL OR p.end_bit IS NULL LIMIT 1"),"complete local timing has unresolved original endpoints");
  Check(!Exists(db,"SELECT 1 FROM candidates c JOIN paths.path_nets pn ON pn.path_ordinal=c.path_ordinal LEFT JOIN original.net_records n ON n.id=pn.net_id LEFT JOIN original.endpoint_records e ON e.net_seq=n.seq AND e.role=0 GROUP BY c.path_ordinal,pn.net_ordinal HAVING n.seq IS NULL OR count(e.ordinal)!=1 LIMIT 1"),"local selected net lacks one unique driver");
  db.Execute("CREATE TEMP TABLE evidence(path_ordinal INTEGER NOT NULL,instance_seq INTEGER NOT NULL,PRIMARY KEY(path_ordinal,instance_seq)) WITHOUT ROWID; INSERT INTO evidence SELECT c.path_ordinal,n.seq FROM candidates c JOIN paths.paths p ON p.ordinal=c.path_ordinal JOIN original.cell_records n ON n.id=p.start_instance UNION SELECT c.path_ordinal,n.seq FROM candidates c JOIN paths.paths p ON p.ordinal=c.path_ordinal JOIN original.cell_records n ON n.id=p.end_instance UNION SELECT c.path_ordinal,e.instance_seq FROM candidates c JOIN paths.path_nets pn ON pn.path_ordinal=c.path_ordinal JOIN original.net_records n ON n.id=pn.net_id JOIN original.endpoint_records e ON e.net_seq=n.seq AND e.role=0 WHERE e.instance_seq IS NOT NULL");
  Check(!Exists(db,"SELECT 1 FROM candidates c LEFT JOIN evidence e ON e.path_ordinal=c.path_ordinal LEFT JOIN assignment.copies p ON p.source_instance_seq=e.instance_seq AND p.is_replica=0 GROUP BY c.path_ordinal HAVING count(e.instance_seq)=0 OR count(p.part_index)!=count(e.instance_seq) OR count(DISTINCT p.part_index)!=1 LIMIT 1"),"local path has no unique assigned FPGA or is cross-partition but absent from Phase 4");
  db.Execute("INSERT INTO path_ownership SELECT p.ordinal,1,NULL FROM paths.paths p JOIN cross_ids r ON r.path_id=p.path_id; INSERT INTO path_ownership SELECT c.path_ordinal,0,f.fpga FROM candidates c JOIN evidence e ON e.path_ordinal=c.path_ordinal JOIN assignment.copies p ON p.source_instance_seq=e.instance_seq AND p.is_replica=0 JOIN assignment.parts f USING(part_index) GROUP BY c.path_ordinal ORDER BY c.path_ordinal");
}
void AuditPartition(SqliteDatabase& db,const volatile std::sig_atomic_t* cancelled){
  auto rows=db.Prepare("SELECT p.ordinal,p.path_id,p.start_instance,p.end_instance,o.scope,o.fpga FROM paths.paths p LEFT JOIN path_ownership o ON o.path_ordinal=p.ordinal ORDER BY p.ordinal");
  db.Execute("CREATE TEMP TABLE audit_cross(path_id TEXT PRIMARY KEY) WITHOUT ROWID; INSERT INTO audit_cross SELECT CASE WHEN json_type(p.record_json,'$.compressed_path_ids') IS NULL THEN p.path_id ELSE m.value END FROM routes.timing_paths p LEFT JOIN json_each(p.record_json,'$.compressed_path_ids') m ON 1");
  auto is_cross=db.Prepare("SELECT 1 FROM audit_cross WHERE path_id=?");
  auto owner=db.Prepare("SELECT DISTINCT f.fpga FROM assignment.copies a JOIN assignment.parts f USING(part_index) WHERE a.is_replica=0 AND a.source_instance_seq IN (SELECT seq FROM original.cell_records WHERE id=?1 OR id=?2 UNION SELECT e.instance_seq FROM paths.path_nets pn JOIN original.net_records n ON n.id=pn.net_id JOIN original.endpoint_records e ON e.net_seq=n.seq AND e.role=0 WHERE pn.path_ordinal=?3 AND e.instance_seq IS NOT NULL)");
  I count=0;while(rows.Step()){Stop(cancelled);Check(rows.TypedInteger(0)==count++ && !rows.IsNull(4),"original ownership coverage disagrees");is_cross.Reset();is_cross.BindText(1,Text(rows,1));const bool cross=is_cross.Step();Check(rows.TypedInteger(4)==int(cross),"original local/cross scope disagrees");if(cross){Check(rows.IsNull(5),"cross path carries local ownership");continue;}owner.Reset();for(int i=0;i<2;++i){if(rows.IsNull(2+i))owner.BindNull(i+1);else owner.BindText(i+1,rows.TypedText(2+i));}owner.BindInteger(3,rows.TypedInteger(0));Check(owner.Step() && Text(owner,0)==Text(rows,5) && !owner.Step(),"original local path FPGA disagrees");}
  Check(count==Scalar(db,"SELECT count(*) FROM path_ownership"),"ownership contains unknown paths");
}
std::string OwnershipHash(SqliteDatabase& db){Sha256 hash;auto rows=db.Prepare("SELECT path_ordinal,scope,fpga FROM path_ownership ORDER BY path_ordinal");while(rows.Step()){Hash(hash,JsonRecord(Array{rows.TypedInteger(0),rows.TypedInteger(1),rows.IsNull(2)?JsonRecord():JsonRecord(Text(rows,2))}).EncodeUtf8());Hash(hash,"\n");}return HexDigest(hash.finish());}
Object Counts(SqliteDatabase& db){Array parts;auto rows=db.Prepare("SELECT fpga,count(*) FROM path_ownership WHERE scope=0 GROUP BY fpga ORDER BY fpga");I local=0;while(rows.Step()){Check(parts.size()<65536,"FPGA catalog exceeds bound");auto count=rows.TypedInteger(1);local+=count;parts.emplace_back(Object{{"fpga",Text(rows,0)},{"paths",count}});}const auto cross=Scalar(db,"SELECT count(*) FROM path_ownership WHERE scope=1");return {{"local_paths",local},{"cross_paths",cross},{"original_paths",local+cross},{"fpgas",parts}};}
void Publish(const fs::path& stage,const fs::path& destination,const std::vector<ArtifactRef>& inputs){RequireSources(inputs);const auto sibling=destination.parent_path()/(stage.parent_path().filename().string()+"-publish");fs::rename(stage,sibling);try{PublishNewDirectory(sibling.string(),destination.string(),inputs);}catch(...){std::error_code error;fs::remove_all(sibling,error);throw;}}
fs::path Destination(const std::string& output){const fs::path target(output);Check(target.is_absolute() && !fs::exists(target) && !fs::is_symlink(target),"output must be a fresh absolute directory");fs::create_directories(target.parent_path());return target;}
PhysicalEndpoint Endpoint(SqliteStatement& rows,int first){PhysicalEndpoint endpoint;endpoint.object=Text(rows,first);if(!rows.IsNull(first+1))endpoint.instance=Text(rows,first+1);endpoint.port=Text(rows,first+2);endpoint.bit=rows.TypedInteger(first+3);Check(endpoint.bit>=0,"negative endpoint bit");return endpoint;}
}

StageResult PrepareLocalPathQueries(const std::vector<ArtifactRef>& inputs,const std::string& output,const volatile std::sig_atomic_t* cancelled){
  return RunStage("physical.prepare-local-paths",[&]{(void)ReadRequest(inputs,false);return inputs;},[&](const auto& sources){
    Stop(cancelled);const auto target=Destination(output);Workspace work(target.parent_path());const auto stage=work.root/"stage";fs::create_directory(stage);
    Object manifest,counts;std::string design,logical;
    {SqliteArtifactWriter writer((stage/"ownership.sqlite").string(),"local-path-partition-store-v1",sources);auto& db=writer.database();db.Execute(kPartitionDDL);AttachOriginal(db,sources);ValidateOriginal(db,sources,work.root);SqliteTransaction transaction(db);design=Design(db);BuildCross(db);Partition(db);AuditPartition(db,cancelled);manifest=Manifest(db,sources);counts=Counts(db);logical=OwnershipHash(db);Put(db,"header",JsonRecord(Object{{"schema","emuflow.local-path-partition/v1"},{"status","pass"},{"design",design}}).EncodeUtf8());Put(db,"sources",Sources(sources,7));Put(db,"source_manifest",JsonRecord(manifest).EncodeUtf8());Put(db,"counts",JsonRecord(counts).EncodeUtf8());Put(db,"logical_sha256",JsonQuote(logical));transaction.Commit();writer.Commit();}
    const auto report=JsonRecord(Object{{"schema","emuflow.local-path-prepare-report/v1"},{"status","pass"},{"implementation_id","native-local-path-partition-v1"},{"design",design},{"source",manifest},{"coverage",counts},{"logical_sha256",logical}}).EncodeUtf8();Check(report.size()<262144,"prepare report exceeds bound");Write(stage/"summary.json",report+"\n",sources);Stop(cancelled);Publish(stage,target,sources);
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(output)/"summary.json").string(),"local-path-prepare-report-v1"),ArtifactRef::Bind((fs::path(output)/"ownership.sqlite").string(),"local-path-partition-store-v1")};});
}

StageResult WriteLocalPathQueries(const std::vector<ArtifactRef>& inputs,const std::string& output,const volatile std::sig_atomic_t* cancelled){
  std::optional<Request> request;
  return RunStage("physical.query-local-paths",[&]{request=ReadRequest(inputs,true);return inputs;},[&](const auto& sources){
    Stop(cancelled);const auto target=Destination(output);Workspace work(target.parent_path());const auto stage=work.root/"stage";fs::create_directory(stage);const auto& config=*request;
    SqliteStore partition(sources[7]);auto& db=partition.database();RequireSqliteDDL(db,kPartitionDDL);AttachOriginal(db,sources);
    Check(Meta(db,"sources")==Sources(sources,7),"prepared partition belongs to another source generation");Check(DecodeJsonString(Meta(db,"logical_sha256"))==OwnershipHash(db),"prepared ownership logical seal disagrees");AuditPartition(db,cancelled);
    Doc header(Meta(db,"header"));const auto design=Design(db);Check(header.Text("design")==design,"prepared design disagrees");
    auto fpga=db.Prepare("SELECT 1 FROM assignment.parts WHERE fpga=?");fpga.BindText(1,config.fpga);Check(fpga.Step(),"requested FPGA is absent");
    EmuirView merged(sources[8]);Doc merged_design(Meta(merged.store().database(),"design_json"));Check(merged_design.Text("name")==design+"__"+config.fpga,"merged target design disagrees");
    PhysicalQueryObjects objects(sources[8],config.backend,&sources[9]);
    auto paths=db.Prepare("SELECT p.path_id,p.clock_domain,p.clock_period_ns,p.slack_ns,p.fixed_delay_ns,p.start_object,p.start_instance,p.start_port,p.start_bit,p.end_object,p.end_instance,p.end_port,p.end_bit FROM paths.paths p JOIN path_ownership o ON o.path_ordinal=p.ordinal WHERE o.scope=0 AND o.fpga=? ORDER BY p.path_id");paths.BindText(1,config.fpga);
    const std::string provider=config.backend=="vivado"?"vivado-source-bound-local-path-query-v1":"original-timing-pathdb-to-xilinx-endpoint-bound-v1";
    auto source_manifest=Meta(db,"source_manifest");Doc manifest(source_manifest);Object origin;for(const auto& row:manifest.Fields()){if(row.first=="original_paths")origin.emplace(row.first,Doc::IntegerToken(row.second));else origin.emplace(row.first,DecodeJsonString(row.second));}
    const auto raw_store=stage/"identity.raw.sqlite";std::optional<PhysicalEvidenceSeal> seal;I count=0;
    {SqliteArtifactWriter writer(raw_store.string(),"local-path-timing-store-v1",sources);auto& identity=writer.database();identity.Execute(kLocalPathTimingDDL);SqliteTransaction transaction(identity);auto append=identity.Prepare("INSERT INTO timings VALUES(?,?,?,?,?,?,?,?,?,?,?)");AtomicBinaryWriter query((stage/"query.tsv").string(),sources);
      query.stream()<<(config.backend=="vivado"?"endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex":"endpoint\tkind\tstart_pin\tend_pin\tpath_pins")<<'\n';
      while(paths.Step()){
        Stop(cancelled);const auto id=Text(paths,0),clock=Text(paths,1);const auto period=paths.TypedReal(2),required=paths.TypedReal(3)+paths.TypedReal(4);Check(std::isfinite(period) && period>0 && std::isfinite(required) && required>0,"invalid local period/required time");const auto start=objects.Resolve(Endpoint(paths,5)),end=objects.Resolve(Endpoint(paths,9));
        append.BindInteger(1,count++);append.BindText(2,id);append.BindText(3,"local");append.BindText(4,config.fpga);append.BindText(5,clock);append.BindReal(6,period);append.BindReal(7,required);append.BindText(8,start.name);append.BindText(9,end.name);append.BindText(10,"endpoint-longest-path-fallback");append.BindReal(11,0);append.Execute();
        if(config.backend=="vivado")query.stream()<<HexText(id)<<"\tlocal\t"<<start.kind<<'\t'<<HexText(start.name)<<'\t'<<end.kind<<'\t'<<HexText(end.name)<<"\t\t\n";
        else query.stream()<<SafeQuery(id)<<"\tlocal\t"<<SafeQuery(start.name)<<'\t'<<SafeQuery(end.name)<<"\t-\n";
      }
      Object identity_header{{"schema","emuflow.local-path-timing/v1"},{"status","pass"},{"design",design},{"fpga",config.fpga},{"provider",provider},{"qualification","typed-local-path-query-identity-owner-v1"},{"identity_schema","emuflow.local-path-identity/v2"},{"source",origin},{"coverage",Object{{"local_paths",count}}}};
      seal=SealPhysicalEvidence(identity,4,JsonRecord(identity_header).EncodeUtf8(),"null");transaction.Commit();append=SqliteStatement{};writer.Commit();query.Commit();
    }
    // Reopen the final relation and independently compare source rows and
    // physical object resolution rather than trusting writer counters.
    {SqliteDatabase identity(raw_store.string(),SqliteDatabase::Mode::kImmutableReadOnly);(void)AuditPhysicalEvidenceStore(identity,4);auto audit=identity.Prepare("SELECT id,clock_domain,clock_period_ns,required_time_ns,start_pin,end_pin FROM timings ORDER BY id");paths.Reset();paths.BindText(1,config.fpga);I verified=0;std::ifstream query(stage/"query.tsv");std::string line;Check(bool(std::getline(query,line)),"missing query header");while(paths.Step()){Stop(cancelled);Check(audit.Step() && Text(audit,0)==Text(paths,0) && Text(audit,1)==Text(paths,1) && audit.TypedReal(2)==paths.TypedReal(2) && audit.TypedReal(3)==paths.TypedReal(3)+paths.TypedReal(4),"identity path population or timing disagrees");auto start=objects.Resolve(Endpoint(paths,5)),end=objects.Resolve(Endpoint(paths,9));Check(Text(audit,4)==start.name && Text(audit,5)==end.name,"query endpoint identity disagrees");Check(bool(std::getline(query,line)),"query coverage is incomplete");std::string expected=config.backend=="vivado"?HexText(Text(paths,0))+"\tlocal\t"+start.kind+"\t"+HexText(start.name)+"\t"+end.kind+"\t"+HexText(end.name)+"\t\t":Text(paths,0)+"\tlocal\t"+start.name+"\t"+end.name+"\t-";Check(line==expected,"query records disagree with identity/source");++verified;}Check(!audit.Step() && !std::getline(query,line) && query.eof() && verified==count,"query/identity has excess rows");}
    objects.RequireUnchanged();const auto store_digest=FileHash(raw_store);const auto store_name="identity.json.store."+store_digest+".sqlite";fs::rename(raw_store,stage/store_name);
    const auto identity_selector=Doc::Encode({{"schema",JsonQuote("emuflow.local-path-timing-storage/v1")},{"logical_schema",JsonQuote("emuflow.local-path-timing/v1")},{"application_id",std::to_string(0x454D554C)},{"user_version","1"},{"header",seal->header},{"counts",seal->counts},{"logical_sha256",JsonQuote(seal->logical_sha256)},{"artifact",JsonQuote(store_name)},{"store_bytes",std::to_string(fs::file_size(stage/store_name))},{"store_sha256",JsonQuote(store_digest)}});
    Write(stage/"identity.json",identity_selector+"\n",sources);
    Object report{{"schema","emuflow.local-path-query-report/v1"},{"status","pass"},{"implementation_id","native-local-path-query-v1"},{"fpga",config.fpga},{"local_paths",count},{"measurement_counts",Object{{"explicit-routed-path-chain",0},{"endpoint-longest-path-fallback",count}}},{"query",(target/"query.tsv").string()},{"identity",(target/"identity.json").string()},{"validation",Object{{"status","pass"},{"scope","original-path-owner-and-query-identity-rejoin"}}}};
    const auto encoded=JsonRecord(report).EncodeUtf8();Check(encoded.size()<262144,"query report exceeds bound");Write(stage/"summary.json",encoded+"\n",sources);Stop(cancelled);Publish(stage,target,sources);
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(output)/"summary.json").string(),"local-path-query-report-v1"),ArtifactRef::Bind((fs::path(output)/"query.tsv").string(),"local-path-query-tsv-v1"),ArtifactRef::Bind((fs::path(output)/"identity.json").string(),"local-path-timing-selector-v1")};});
}
}
