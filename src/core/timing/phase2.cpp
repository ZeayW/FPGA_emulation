#include "timing/phase2.hpp"
#include "common/bounded_json.hpp"
#include "ir/emuir_view.hpp"
#include "common/hash.hpp"
#include "timing/opensta_export.hpp"
#include "timing/sta_paths.hpp"
#include "timing/sta_weights.hpp"
#include "common/tool_runtime.hpp"
#include <array>
#include <filesystem>
#include <iomanip>
#include <optional>
#include <regex>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using Doc=BoundedJson;
using Raw=std::map<std::string,std::string>;
void Check(bool value,const std::string& reason){if(!value)throw StageError(ErrorCode::kInvalidArtifact,"Phase 2: "+reason);}
void Stop(const volatile std::sig_atomic_t* cancelled){if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"Phase 2 cancelled");}
std::string HexDigest(const std::array<unsigned char,32>& digest){std::string text;for(auto c:digest){text+="0123456789abcdef"[c>>4];text+="0123456789abcdef"[c&15];}return text;}
std::string Digest(const std::string& text){Sha256 h;h.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());return HexDigest(h.finish());}
std::string FileDigest(const fs::path& path){std::ifstream file(path,std::ios::binary);Check(bool(file),"cannot seal source");Sha256 h;std::array<char,65536> block;while(file){file.read(block.data(),block.size());h.update(reinterpret_cast<const unsigned char*>(block.data()),file.gcount());}Check(file.eof(),"source read failed");return HexDigest(h.finish());}
std::string Hex(const std::string& text){std::string value;for(unsigned char c:text){value+="0123456789abcdef"[c>>4];value+="0123456789abcdef"[c&15];}return value;}
std::string SafeText(std::string value){Check(value.find('\0')==std::string::npos,"NUL in control string");return value;}
struct Workspace{
  fs::path root;
  explicit Workspace(const fs::path& parent){auto path=(parent/".phase2-stage-XXXXXX").string();Check(mkdtemp(path.data())!=nullptr,"cannot create private generation");root=path;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
std::string Metadata(SqliteDatabase& db,const char* key){auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,key);Check(q.Step(),std::string("missing metadata ")+key);auto value=q.TypedText(0);Check(!q.Step(),"duplicate metadata");return value;}
void Write(const fs::path& path,const std::string& text,const std::vector<ArtifactRef>& sources){PublishTextArtifact(path.string(),sources,[&](std::ostream& out){out<<text;});}
std::string Small(const fs::path& path){return Doc::Read(ArtifactRef::Bind(path.string(),"phase2-source-v1"),512*1024);}
void Generation(Doc& value,const FileGeneration& g){
  const auto v=Doc::Array(value.Token("generation"));
  const std::int64_t expected[]={std::int64_t(g.device),std::int64_t(g.inode),std::int64_t(g.bytes),g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};
  Check(v.size()==7,"source generation shape disagrees");for(unsigned i=0;i<7;++i)Check(Doc::IntegerToken(v[i])==expected[i],"source generation changed");
}
struct Configuration{
  Raw values;std::vector<ArtifactRef> sources;std::map<std::string,ArtifactRef> roles;
};
Configuration Read(const ArtifactRef& emuir,const ArtifactRef& policy){
  Check(emuir.schema()=="emuir-compact-v2" && policy.schema()=="phase2-policy-v1","source roles disagree");
  Doc document(Doc::Read(policy));document.Keys({"schema","sources","configuration"});
  Check(document.Text("schema")=="emuflow.phase2-policy/v1","unsupported policy");Configuration c;c.sources={policy};
  const std::set<std::string> required={"emuir","timing-model","opensta-executable","export-script","runtime-check-script"};
  for(const auto& token:Doc::Array(document.Token("sources"),16)){
    Doc row(token);row.Keys({"role","path","schema","generation"});const auto role=row.Text("role");
    Check(required.count(role) || role=="architecture-timing-db" || role=="architecture-contract" || role=="runtime-launcher" || role=="container-binary","unknown source role");
    const auto schema=role=="emuir"?"emuir-compact-v2":role=="opensta-executable" || role=="container-binary"?"external-tool-executable-v1":"phase2-source-v1";
    Check(row.Text("schema")==schema,"source schema disagrees");const auto path=SafeText(row.Text("path"));Check(fs::path(path).is_absolute(),"source path is relative");
    auto source=ArtifactRef::Bind(path,schema);Generation(row,source.generation());
    Check(c.roles.emplace(role,source).second,"duplicate source role");c.sources.push_back(source);
  }
  for(const auto& role:required)Check(c.roles.count(role),"missing source role "+role);
  Check(c.roles.at("emuir").path()==emuir.path() && c.roles.at("emuir").generation()==emuir.generation(),"policy belongs to another EmuIR");
  Doc config(document.Token("configuration"));
  config.Keys({"design","liberty","clock_map","timing_io","timing_io_sha256","model","engine","used_cell_type_summary","export_mode","max_paths","optimization_enabled","criticality_scale","criticality_exponent"});
  c.values=config.Fields();Check(config.Integer("max_paths")>0,"max_paths must be positive");
  Check(config.Number("criticality_scale")>0 && config.Number("criticality_exponent")>0,"weight parameters must be positive");
  (void)config.Boolean("optimization_enabled");const auto mode=config.Integer("export_mode");Check(mode==0 || mode==1,"invalid primitive timing policy");
  Check(bool(mode)==bool(c.roles.count("architecture-timing-db")),"architecture model/export mode disagree");
  Check(!config.Text("liberty").empty(),"missing Liberty model");return c;
}
ToolResult Tool(const Configuration& c,std::vector<std::string> argv,
                std::vector<std::pair<std::string,std::string>> environment,
                const volatile std::sig_atomic_t* cancelled){
  Stop(cancelled);for(const auto& source:c.sources)source.RequireUnchanged();
  ToolRequest request;request.argv=std::move(argv);request.working_directory=fs::current_path().string();
  request.environment=std::move(environment);request.tail_bytes=65536;request.termination_grace_ms=10000;
  if(c.roles.count("container-binary"))request.environment.emplace_back("EMUFLOW_OPENSTA_CONTAINER_BINARY",c.roles.at("container-binary").path());
  auto result=RunExternalTool(request,cancelled,c.sources);
  Check(result.operation.ok(),"OpenSTA failed: "+result.operation.message+"\n"+result.diagnostic_tail);return result;
}
std::string Engine(const Configuration& c,const volatile std::sig_atomic_t* cancelled){
  Doc identity(c.values.at("engine"));Check(identity.Text("name")=="OpenSTA","wrong engine");
  const auto& executable=c.roles.at("opensta-executable");const auto digest=FileDigest(executable.path());
  if(c.roles.count("container-binary")){
    Check(identity.Text("launcher_sha256")==digest && identity.Text("executable_sha256")==FileDigest(c.roles.at("container-binary").path()),"container engine content changed");
  }else Check(!identity.Has("launcher_sha256") && identity.Text("executable_sha256")==digest,"engine content changed");
  const auto version=Tool(c,{executable.path(),"-version"},{},cancelled);std::smatch match;
  Check(std::regex_search(version.diagnostic_tail,match,std::regex(R"((?:^|\s)([0-9]+)\.([0-9]+)\.([0-9]+)(?:\s|$))")),"engine has no semantic version");
  const auto major=std::stoull(match[1]),minor=std::stoull(match[2]);
  Check(major>3 || (major==3 && minor>=1),"OpenSTA version is unsupported");
  Check(identity.Text("version")==match[1].str()+"."+match[2].str()+"."+match[3].str(),"engine version changed");
  const auto runtime=Tool(c,{executable.path(),"-no_init","-exit",c.roles.at("runtime-check-script").path()},{},cancelled);
  Check(runtime.diagnostic_tail.find("EMUFLOW_OPENSTA_RUNTIME status=pass")!=std::string::npos,"OpenSTA Tcl runtime qualification failed");return identity.text();
}
std::string ValidateModel(SqliteDatabase& db,Doc& config){
  Doc model(config.Token("model"));const auto mode=config.Integer("export_mode");std::set<std::string> supported,used;
  for(const auto& token:Doc::Array(model.Token("cell_types"),4096))Check(supported.insert(DecodeJsonString(token)).second,"duplicate supported primitive");
  // One set reduction of the source type/inversion vocabulary. No cell-name
  // map or graph-sized host collection is built during model configuration.
  auto types=db.Prepare("SELECT DISTINCT c.type,CASE WHEN c.type IN ('FDCE','FDPE','FDRE','FDSE') THEN json_type(p.value,'$.IS_C_INVERTED') END,CASE WHEN c.type IN ('FDCE','FDPE','FDRE','FDSE') THEN json_extract(p.value,'$.IS_C_INVERTED') END FROM cell_records c LEFT JOIN payloads p ON p.seq=c.parameters_seq");
  while(types.Step()){
    auto name=types.TypedText(0);const auto kind=types.IsNull(1)?"null":types.TypedText(1);
    if(mode && TimingParameterIsOne(kind,kind=="integer"?types.TypedInteger(2):0,kind=="text"?types.TypedText(2):""))name+="__NEG";
    Check(supported.count(name),"timing model does not cover "+name);used.insert(name);Check(used.size()<=4096,"primitive vocabulary exceeds bound");
  }
  Sha256 digest;for(const auto& name:used){std::array<unsigned char,8> length{};for(unsigned i=0;i<8;++i)length[7-i]=name.size()>>(i*8);digest.update(length.data(),length.size());digest.update(reinterpret_cast<const unsigned char*>(name.data()),name.size());}
  const auto result=Doc::Encode({{"count",std::to_string(used.size())},{"sha256",JsonQuote(HexDigest(digest.finish()))}});
  RequireEquivalentJson(db,result,config.Token("used_cell_type_summary"),"primitive model coverage disagrees");return result;
}
std::string WriteClockIo(SqliteDatabase& db,Doc& config,const fs::path& clocks_file,const fs::path& io_file,const std::vector<ArtifactRef>& sources){
  Doc clocks(config.Token("clock_map"));const auto values=clocks.Fields();Check(!values.empty() && values.size()<=4096,"empty or excessive clock catalog");
  auto known=db.Prepare("SELECT 1 FROM clocks WHERE json_extract(record_json,'$.id')=? LIMIT 1");
  {AtomicBinaryWriter file(clocks_file.string(),sources);file.stream()<<"clock_hex\tperiod_ns\n";
    for(const auto& clock:values){known.Reset();known.BindText(1,clock.first);Check(known.Step(),"clock is absent from EmuIR");const auto period=clocks.Number(clock.first);Check(period>0,"clock period must be positive");file.stream()<<Hex(clock.first)<<'\t'<<std::setprecision(12)<<period<<'\n';}file.Commit();}
  const auto io=config.Token("timing_io");std::int64_t inputs=0,outputs=0;std::set<std::pair<std::string,std::string>> seen;
  AtomicBinaryWriter file(io_file.string(),sources);file.stream()<<"direction\tclock_hex\tdelay_ns\tport_hex\n";
  if(io!="null"){
    Doc groups(io);groups.Keys({"input_groups","output_groups"});
    auto port=db.Prepare("SELECT json_extract(record_json,'$.direction') FROM port_records WHERE json_extract(record_json,'$.id')=?");
    // The top-level port owner remains in SQLite; only explicit benchmark I/O
    // constraints are retained, never an expanded port-bit population.
    for(const std::string direction:{"input","output"})for(const auto& token:Doc::Array(groups.Token(direction+"_groups"))){
      Doc group(token);group.Keys({"clock","delay_ns","ports"});const auto clock=group.Text("clock");Check(values.count(clock),"I/O clock is undeclared");const auto delay=group.Number("delay_ns");Check(delay>=0,"negative I/O delay");const auto declared=Doc::Array(group.Token("ports"));Check(!declared.empty(),"empty I/O group");
      for(const auto& value:declared){const auto name=DecodeJsonString(value);port.Reset();port.BindText(1,name);Check(port.Step() && port.TypedText(0)==direction && !port.Step() && !values.count(name) && seen.emplace(direction,name).second,"invalid or duplicate I/O port");file.stream()<<direction<<'\t'<<Hex(clock)<<'\t'<<std::setprecision(17)<<delay<<'\t'<<Hex(name)<<'\n';if(direction=="input")++inputs;else ++outputs;}
    }
    // The bounded request is canonical sorted-key JSON. Keep its numeric
    // spellings: 0 and 0.0 are distinct benchmark source identities.
    auto canonical=db.Prepare("SELECT json(?)");canonical.BindText(1,io);Check(canonical.Step(),"invalid I/O contract");
    Check(config.Text("timing_io_sha256")==Digest(canonical.TypedText(0)),"I/O source seal disagrees");
  }else Check(config.Token("timing_io_sha256")=="null","missing I/O source must have null identity");
  file.Commit();return Doc::Encode({{"sha256",config.Token("timing_io_sha256")},{"input_ports",std::to_string(inputs)},{"output_ports",std::to_string(outputs)}});
}
ArtifactRef Request(const fs::path& path,std::uint64_t magic,const std::vector<ArtifactRef>& sources,const std::string& configuration,const char* schema){
  CheckedRequestWriter writer(path.string(),sources);writer.Put<std::uint64_t>(magic);writer.Put<std::uint32_t>(1);for(const auto& source:sources)writer.Generation(source.generation());writer.Text(configuration);writer.Finish();return ArtifactRef::Bind(path.string(),schema);
}
std::pair<fs::path,std::string> Adopt(const fs::path& temporary,const fs::path& selector,const char* kind){
  auto digest=FileDigest(temporary);auto output=selector.parent_path()/(selector.filename().string()+"."+kind+"."+digest+".sqlite");Check(!fs::exists(output),"duplicate private payload");fs::rename(temporary,output);return {output,digest};
}
std::string PathSelector(Doc& result,const fs::path& payload,const std::string& digest,const std::string& source){
  Doc summary(result.Token("summary"));
  return Doc::Encode({{"schema",JsonQuote("emuflow.sta-path-database/v3")},{"design",result.Token("design")},{"source",source},{"normalization",result.Token("normalization")},{"summary",summary.text()},
    {"payloads",Doc::Encode({{"paths",Doc::Encode({{"format",JsonQuote("sqlite-sta-path/v1")},{"path",JsonQuote(payload.filename().string())},{"sha256",JsonQuote(digest)},{"records",summary.Token("paths")}})}})}});
}
std::string WeightSelector(Doc& result,const std::string& parameters,const fs::path& path_selector,const fs::path& payload,const std::string& digest){
  Raw summary;for(const auto* field:{"paths","timed_nets","weighted_nets","minimum_weight","maximum_weight"})summary[field]=result.Token(field);
  Doc policy(parameters);auto p=policy.Fields();p["criticality_definition"]=JsonQuote("clamp(1-slack/period,0,1)");p["path_reduction"]=JsonQuote("maximum");
  return Doc::Encode({{"schema",JsonQuote("emuflow.partition-net-weights/v2")},{"design",result.Token("design")},
    {"source",Doc::Encode({{"provider",JsonQuote("sta-max-criticality-power-law-v2-indexed")},{"path_database",JsonQuote(path_selector.filename().string())},{"path_database_provider",JsonQuote("opensta-fpga-path-database-v1")}})},
    {"parameters",Doc::Encode(p)},{"summary",Doc::Encode(summary)},
    {"payloads",Doc::Encode({{"weights",Doc::Encode({{"format",JsonQuote("sqlite-net-weights/v1")},{"path",JsonQuote(payload.filename().string())},{"sha256",JsonQuote(digest)},{"records",result.Token("timed_nets")}})}})}});
}
}
StageResult RunPhase2(const ArtifactRef& emuir,const ArtifactRef& policy,const std::string& output,const volatile std::sig_atomic_t* cancelled){
  std::optional<Configuration> configuration;
  return RunStage("phase2.run",[&]{configuration=Read(emuir,policy);return configuration->sources;},[&](const auto& sources){
    Stop(cancelled);const auto destination=fs::path(output);Check(destination.is_absolute() && !fs::exists(destination) && !fs::is_symlink(destination),"output must be a fresh absolute directory");fs::create_directories(destination.parent_path());Workspace work(destination.parent_path());const auto stage=work.root/"stage";fs::create_directory(stage);
    auto& c=*configuration;Doc config(Doc::Encode(c.values));EmuirView source(emuir);auto& db=source.store().database();
    Doc design(Metadata(db,"design_json"));Check(config.Text("design")==design.Text("name"),"design identity disagrees");const auto top=SafeText(design.Text("top"));
    const auto engine=Engine(c,cancelled),coverage=ValidateModel(db,config);Doc model(config.Token("model"));
    const auto clocks=work.root/"clocks.tsv",io=work.root/"timing-io.tsv";
    const auto io_summary=WriteClockIo(db,config,clocks,io,sources);
    const auto liberty=work.root/"timing.lib",verilog=work.root/"mapped.v",net_map=work.root/"net-map.tsv",raw=work.root/"paths.tsv";
    Write(liberty,config.Text("liberty"),sources);
    const auto export_path=work.root/"export.bin";
    {CheckedRequestWriter writer(export_path.string(),sources);writer.Put<std::uint64_t>(0x31455853464d5545ULL);writer.Put<std::uint32_t>(1);writer.Generation(emuir.generation());writer.Put<std::uint32_t>(config.Integer("export_mode"));writer.Put<std::uint32_t>(3);writer.Finish();}
    Stop(cancelled);const auto export_summary=work.root/"export.json";
    ExportOpenstaInputs({emuir,ArtifactRef::Bind(export_path.string(),"opensta-input-export-request-v1")},export_summary.string(),verilog.string(),net_map.string());
    const auto execute=Tool(c,{c.roles.at("opensta-executable").path(),"-exit",c.roles.at("export-script").path()},
      {{"EMUFLOW_STA_LIBERTY",liberty.string()},{"EMUFLOW_STA_EXTRA_LIBERTY",""},{"EMUFLOW_STA_VERILOG",verilog.string()},{"EMUFLOW_STA_TOP",top},{"EMUFLOW_STA_NET_MAP",net_map.string()},{"EMUFLOW_STA_CLOCKS",clocks.string()},{"EMUFLOW_STA_TIMING_IO",io.string()},{"EMUFLOW_STA_OUTPUT",raw.string()},{"EMUFLOW_STA_MAX_PATHS",std::to_string(config.Integer("max_paths"))},{"EMUFLOW_STA_THROUGH_NETS",""},{"EMUFLOW_STA_THROUGH_COVERAGE",""},{"EMUFLOW_STA_THROUGH_ENDPOINTS",""}},cancelled);
    Write(stage/"opensta.log",execute.diagnostic_tail,sources);PublishToolResult((stage/"opensta-execution.json").string(),execute);
    Check(fs::is_regular_file(raw) && !fs::is_symlink(raw),"OpenSTA did not produce regular path TSV");
    const auto measured=ArtifactRef::Bind(raw.string(),"sta-source-document-v1");const std::vector<ArtifactRef> path_sources={emuir,measured};
    auto path_inputs=path_sources;path_inputs.push_back(Request(work.root/"import.bin",0x31505453464d5545ULL,path_sources,"{}","sta-path-request-v1"));
    Stop(cancelled);ImportStaPaths(path_inputs,(work.root/"paths.sqlite").string(),(work.root/"import.json").string());Doc imported(Small(work.root/"import.json"));Doc stats(imported.Token("summary"));
    Check(imported.Text("design")==design.Text("name") && imported.Text("status")=="pass","path import identity disagrees");
    const auto path_selector=stage/"path-database.json";const auto adopted=Adopt(work.root/"paths.sqlite",path_selector,"paths");
    const auto origin=Doc::Encode({{"provider",JsonQuote("opensta-fpga-path-database-v1")},{"input",JsonQuote("<opensta-path-tsv>")},{"input_sha256",JsonQuote(FileDigest(raw))},{"timing_model",model.Token("name")},{"timing_model_qualification",model.Token("qualification")},{"engine",engine},{"architecture_timing_db",model.Token("architecture_timing_db")},{"timing_io",io_summary}});
    const auto manifest=PathSelector(imported,adopted.first,adopted.second,origin);Write(path_selector,manifest+"\n",sources);
    const auto selector=ArtifactRef::Bind(path_selector.string(),"sta-path-selector-v1"),payload=ArtifactRef::Bind(adopted.first.string(),"timing-path-store-v1");
    // Reopen the final typed generation under its public selector. This audit
    // independently resolves source endpoints and every original membership.
    const auto audit_configuration=Doc::Encode({{"normalization",imported.Token("normalization")},{"summary",stats.text()},{"design",design.Token("name")},{"provider",JsonQuote("opensta-fpga-path-database-v1")}});
    auto audit_inputs=std::vector<ArtifactRef>{emuir,payload};audit_inputs.push_back(Request(work.root/"audit-paths.bin",0x31505453464d5545ULL,audit_inputs,audit_configuration,"sta-path-request-v1"));
    Stop(cancelled);ValidateStaPaths(audit_inputs,(work.root/"paths-validation.json").string());
    Raw sta={{"status",JsonQuote("pass")},{"design",design.Token("name")},{"provider",JsonQuote("opensta-fpga-path-database-v1")},{"timing_model",model.Token("name")},{"timing_model_qualification",model.Token("qualification")},{"engine",engine},{"clocks",config.Token("clock_map")},{"timing_io",io_summary},{"paths",stats.Token("paths")},{"max_paths",config.Token("max_paths")},{"through_nets","[]"},{"covered_through_nets","[]"},{"through_net_coverage","null"},{"path_limit_reached",stats.Integer("paths")>=config.Integer("max_paths")?"true":"false"},{"unique_path_nets",stats.Token("unique_path_nets")},{"used_cell_type_summary",coverage},{"checker",Small(work.root/"paths-validation.json")},{"output",JsonQuote((destination/"path-database.json").string())},{"log",JsonQuote((destination/"opensta.log").string())},{"input_export",Small(export_summary)},{"path_qor",stats.Token("qor")},{"path_import",Doc::Encode({{"implementation_id",imported.Token("implementation_id")}})}};
    Raw report={{"schema",JsonQuote("emuflow.phase2-report/v1")},{"stage_implementation_id",JsonQuote("native-phase2-v1")},{"status",JsonQuote("pass")},{"mode",JsonQuote("opensta-preplacement")},{"backend",JsonQuote("opensta")},{"sta",Doc::Encode(sta)},{"optimization_enabled",config.Token("optimization_enabled")}};
    if(config.Boolean("optimization_enabled")){
      const auto parameters=Doc::Encode({{"criticality_scale",config.Token("criticality_scale")},{"criticality_exponent",config.Token("criticality_exponent")}});
      auto weight_inputs=std::vector<ArtifactRef>{emuir,selector,payload};weight_inputs.push_back(Request(work.root/"weights.bin",0x31574754534d5545ULL,weight_inputs,parameters,"sta-weight-request-v1"));
      Stop(cancelled);BuildStaWeights(weight_inputs,(work.root/"weights.sqlite").string(),(work.root/"weights.json").string());Doc weighted(Small(work.root/"weights.json"));
      const auto weight_selector=stage/"partition-net-weights.json";const auto weights=Adopt(work.root/"weights.sqlite",weight_selector,"weights");Write(weight_selector,WeightSelector(weighted,parameters,path_selector,weights.first,weights.second)+"\n",sources);
      auto validation=std::vector<ArtifactRef>{emuir,selector,payload,ArtifactRef::Bind(weight_selector.string(),"net-weight-selector-v2"),ArtifactRef::Bind(weights.first.string(),"net-weight-store-v1")};validation.push_back(Request(work.root/"weight-audit.bin",0x31574754534d5545ULL,validation,parameters,"sta-weight-request-v1"));
      Stop(cancelled);ValidateStaWeights(validation,(work.root/"weights-validation.json").string());auto result=weighted.Fields();result["output"]=JsonQuote((destination/"partition-net-weights.json").string());report["partition_weights"]=Doc::Encode(result);
    }
    Stop(cancelled);const auto summary=Doc::Encode(report);Check(summary.size()+1<=262144,"stage summary exceeds bound");Write(stage/"phase2_report.json",summary+"\n",sources);
    for(const auto& input:sources)input.RequireUnchanged();
    const auto sibling=destination.parent_path()/(work.root.filename().string()+"-publish");fs::rename(stage,sibling);
    try{PublishNewDirectory(sibling.string(),destination.string(),sources);}catch(...){std::error_code error;fs::remove_all(sibling,error);throw;}
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(output)/"phase2_report.json").string(),"phase2-report-v1"),ArtifactRef::Bind((fs::path(output)/"path-database.json").string(),"sta-path-selector-v1")};});
}
}
