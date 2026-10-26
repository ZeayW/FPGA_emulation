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
  Check(bool(stream),"cannot read Phase 5 control document");return result;
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
  if(kind=="real")return JsonRecord(type.Real(2));
  Check(kind=="object" || kind=="array","invalid Phase 5 control JSON type");
  auto rows=db.Prepare("SELECT key,CASE WHEN type IN ('object','array') THEN value WHEN type IN ('true','false','null') THEN type ELSE json_quote(value) END FROM json_each(?1)");rows.BindText(1,text);
  Object object;Array array;std::size_t count=0;
  while(rows.Step()) {
    Check(++count<=16384,"Phase 5 control collection exceeds bound");
    auto value=Record(db,rows.TypedText(1),depth+1);
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
  while(rows.Step())Check(result.emplace(rows.TypedText(0),Record(db,rows.TypedText(1))).second,"duplicate Phase 5 control key");
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
  Request(const fs::path& path,const std::vector<ArtifactRef>& sources):writer_(path.string(),sources){}
  template<class T>void Put(T value){writer_.Write(value);UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(&value),sizeof(value));}
  void Text(const std::string& value){Check(value.size()<=UINT32_MAX,"Phase 5 request string exceeds bound");Put<std::uint32_t>(value.size());writer_.WriteBytes(value.data(),value.size());UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(value.data()),value.size());}
  void Generation(const FileGeneration& g){Put(g.device);Put(g.inode);Put(g.bytes);Put(g.modified_seconds);Put(g.modified_nanoseconds);Put(g.changed_seconds);Put(g.changed_nanoseconds);}
  void Finish(){writer_.Write(crc_);writer_.Commit();}
 private:AtomicBinaryWriter writer_;std::uint32_t crc_=0;
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
} // namespace emuflow::core
