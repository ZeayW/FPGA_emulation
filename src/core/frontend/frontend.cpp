#include "frontend/frontend.hpp"
#include "common/artifact_io.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include "common/tool_runtime.hpp"
#include "frontend/yosys_hierarchy.hpp"
#include <filesystem>
#include <fstream>
#include <limits>
#include <optional>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using Fields=std::map<std::string,std::string>;
void Check(bool ok,const std::string& message){if(!ok)throw StageError(ErrorCode::kInvalidArtifact,"frontend "+message);}
std::string Encode(const Fields& fields){std::string text="{";for(const auto& field:fields){if(text.size()>1)text+=',';text+=JsonQuoteUtf8(field.first)+":"+field.second;}return text+'}';}
std::string Array(const std::vector<std::string>& values){std::string text="[";for(const auto& value:values){if(text.size()>1)text+=',';text+=JsonQuoteUtf8(value);}return text+']';}
void ValidJson(SqliteDatabase& db,const std::string& text,const std::string& kind){
  auto row=db.Prepare("SELECT json_valid(?1),json_type(?1)");row.BindText(1,text);Check(row.Step() && row.Integer(0) && row.Text(1)==kind,"invalid "+kind+" control document");
  auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,text);Check(!duplicate.Step(),"duplicate control key");
}
std::string Field(SqliteDatabase& db,const std::string& text,const std::string& key,const std::string& kind){
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,text);row.BindText(2,"$."+key);Check(row.Step() && row.Text(0)==kind,"invalid control field "+key);return row.TypedText(1);
}
std::string Metadata(SqliteDatabase& db,const std::string& key){auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);Check(row.Step(),"missing metadata "+key);auto value=row.TypedText(0);Check(!row.Step(),"duplicate metadata");return value;}
std::string ReadSmall(const fs::path& path){const auto size=fs::file_size(path);Check(size<=512*1024,"summary exceeds bound");std::ifstream file(path,std::ios::binary);std::string result(size,'\0');file.read(result.data(),size);Check(bool(file),"cannot read summary");return result;}
struct Workspace{
  fs::path root;
  explicit Workspace(const fs::path& parent){std::string name=(parent/".frontend-generation-XXXXXX").string();Check(mkdtemp(name.data())!=nullptr,"cannot create private generation");root=name;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
std::vector<std::string> Catalog(CheckedRequestReader& reader){const auto count=reader.Read<std::uint32_t>();Check(count<=16384,"configuration catalog exceeds bound");std::vector<std::string> values;for(unsigned i=0;i<count;++i)values.push_back(reader.ReadString32(false,1024*1024));return values;}
struct Configuration{
  std::string output,top,platform,synthesis,library;std::vector<std::string> clocks;ToolRequest tool;
  bool require_no_fabric_clock=false;std::vector<ArtifactRef> sources;
};
Configuration Read(const ArtifactRef& request){
  Configuration c;c.sources.push_back(request);CheckedRequestReader reader(request.path(),4*1024*1024);
  Check(reader.Read<std::uint64_t>()==0x31544e52464d5545ULL && reader.Read<std::uint32_t>()==1,"request version is invalid");
  c.output=reader.ReadString32();c.top=reader.ReadString32();c.clocks=Catalog(reader);
  c.platform=reader.ReadString32(false,1024*1024);c.synthesis=reader.ReadString32(false,1024*1024);
  const auto strict=reader.Read<std::uint32_t>();Check(strict<=1,"invalid clock policy");c.require_no_fabric_clock=strict;
  c.tool.argv=Catalog(reader);c.tool.working_directory=reader.ReadString32();c.tool.tail_bytes=65536;
  c.tool.timeout_ms=reader.Read<std::uint64_t>();c.tool.termination_grace_ms=1000;
  c.library=reader.ReadString32();auto library=ArtifactRef::Bind(c.library,"xilinx-primitive-library-v1");
  Check(ReadFileGeneration(reader)==library.generation(),"primitive library generation changed");c.sources.push_back(library);
  const auto count=reader.Read<std::uint32_t>();Check(count<=16384,"source catalog exceeds bound");
  for(unsigned i=0;i<count;++i){auto source=ArtifactRef::Bind(reader.ReadString32(),"frontend-source-v1");Check(ReadFileGeneration(reader)==source.generation(),"RTL/source generation changed");c.sources.push_back(std::move(source));}
  reader.RequireEnd();Check(fs::path(c.output).is_absolute() && fs::path(c.output).filename()=="phase1","output directory is invalid");
  Check(!c.tool.argv.empty() && c.sources.size()>2,"missing engine or RTL sources");return c;
}
struct ClockFacts{std::uint64_t nets=0,fabric=0,ffs=0,maximum=0;};
// One ordered endpoint join; no Python endpoint population or one-query-per-pin.
ClockFacts VisitClocks(SqliteDatabase& db,const std::function<void(const std::string&,const std::vector<std::string>&,std::uint64_t)>& visit){
  auto rows=db.Prepare(R"SQL(WITH clocked AS (
    SELECT e.net_seq,COUNT(*) AS sinks FROM endpoints e JOIN instances i ON i.id=e.instance_id
    WHERE e.role=1 AND (((substr(i.type,1,2)='FD' OR substr(i.type,1,6)='$_DFF_') AND e.port='C')
      OR (i.type IN ('VTR_SP_RAM','VTR_DP_RAM') AND e.port='clk')) GROUP BY e.net_seq),
  drivers AS (SELECT e.net_seq,e.instance_id FROM endpoints e JOIN instances i ON i.id=e.instance_id
    WHERE e.role=0 AND (substr(i.type,1,3)='LUT' OR i.type='$lut'))
  SELECT c.net_seq,n.id,c.sinks,d.instance_id FROM clocked c JOIN nets n ON n.seq=c.net_seq
  LEFT JOIN drivers d ON d.net_seq=c.net_seq ORDER BY c.net_seq,d.instance_id)SQL");
  ClockFacts result;std::int64_t current=-1;std::string name;std::uint64_t count=0;std::vector<std::string> drivers;
  const auto finish=[&]{if(current<0)return;++result.nets;if(!drivers.empty()){++result.fabric;result.ffs+=count;result.maximum=std::max(result.maximum,count);if(visit)visit(name,drivers,count);}};
  while(rows.Step()){if(rows.TypedInteger(0)!=current){finish();current=rows.TypedInteger(0);name=rows.TypedText(1);count=rows.TypedInteger(2);drivers.clear();}if(!rows.IsNull(3))drivers.push_back(rows.TypedText(3));}finish();return result;
}
std::string BuildReport(const fs::path& root,const Configuration& c,const std::string& importer,const ToolResult& tool){
  SqliteDatabase control(":memory:",SqliteDatabase::Mode::kReadOnly);
  ValidJson(control,c.platform,"object");ValidJson(control,c.synthesis,"object");ValidJson(control,importer,"object");
  const auto platform=Field(control,c.platform,"platform.name","text");
  SqliteStore store(ArtifactRef::Bind((root/"design.emuir.sqlite").string(),"emuir-compact-v2"));auto& db=store.database();
  const auto design=Field(control,Metadata(db,"design_json"),"name","text");
  auto warnings=Metadata(db,"warnings_json");ValidJson(control,warnings,"array");
  Fields totals;std::map<std::string,std::int64_t> amounts;
  auto resources=db.Prepare("SELECT r.key,SUM(r.value*g.amount) FROM (SELECT resources_seq,COUNT(*) AS amount FROM cell_records GROUP BY resources_seq) g JOIN payloads p ON p.seq=g.resources_seq,json_each(p.value) r GROUP BY r.key");
  while(resources.Step()){auto name=resources.TypedText(0);auto amount=resources.TypedInteger(1);Check(amount>=0 && std::find(kResourceFields.begin(),kResourceFields.end(),name)!=kResourceFields.end(),"invalid resource total");amounts[name]=amount;if(amount)totals[name]=std::to_string(amount);}
  Fields classes;auto counts=db.Prepare("SELECT cut_class,COUNT(*) FROM nets GROUP BY cut_class");while(counts.Step())classes[counts.TypedText(0)]=std::to_string(counts.TypedInteger(1));
  std::map<std::string,std::int64_t> aggregate;Fields fits;bool single=false;
  auto fpgas=control.Prepare("SELECT json_extract(value,'$.id'),json_extract(value,'$.effective_capacity') FROM json_each(?1,'$.fpgas')");fpgas.BindText(1,c.platform);
  while(fpgas.Step()){
    auto id=fpgas.TypedText(0),capacity=fpgas.TypedText(1);Check(!id.empty() && !fits.count(id),"invalid FPGA catalog");ValidJson(control,capacity,"object");
    std::map<std::string,std::int64_t> available;auto values=control.Prepare("SELECT key,type,value FROM json_each(?)");values.BindText(1,capacity);
    while(values.Step()){const auto name=values.TypedText(0);Check(values.Text(1)=="integer" && std::find(kResourceFields.begin(),kResourceFields.end(),name)!=kResourceFields.end(),"invalid effective capacity");const auto count=values.TypedInteger(2);Check(count>=0 && aggregate[name]<=INT64_MAX-count,"capacity overflow");available[name]=count;aggregate[name]+=count;}
    bool fit=true;for(const auto& item:amounts)fit=fit && item.second<=available[item.first];fits[id]=fit?"true":"false";single=single||fit;
  }
  Check(!fits.empty(),"empty FPGA catalog");bool fits_platform=true;for(const auto& item:amounts)fits_platform=fits_platform && item.second<=(aggregate.count(item.first)?aggregate.at(item.first):0);
  Fields aggregate_json;for(const auto& item:aggregate)aggregate_json[item.first]=std::to_string(item.second);
  const auto clock=VisitClocks(db,{});
  Check(fits_platform,"capacity check failed");Check(!c.require_no_fabric_clock || !clock.fabric,"clock topology check failed");
  const auto normalization=Field(control,importer,"normalization","object");
  // The source-to-compact producer and its independent source cursor must both
  // have succeeded before any report or public directory is committed.
  Check(Field(control,importer,"status","text")=="pass" && Field(control,Field(control,importer,"validation","object"),"status","text")=="pass","source audit failed");
  auto native_import="{\"implementation_id\":\"native-yosys-emuir-v1\",\"records\":"+Field(control,importer,"emuir_records","object")+",\"validation\":"+Field(control,importer,"validation","object")+"}";
  auto transform=normalization;auto update=control.Prepare("SELECT json_set(?1,'$.input_sha256',NULL,'$.hierarchical_interchange',json(?2),'$.native_emuir_import',json(?3))");update.BindText(1,transform);update.BindText(2,Field(control,importer,"hierarchical_interchange","object"));update.BindText(3,native_import);Check(update.Step(),"cannot construct normalization report");transform=update.TypedText(0);
  auto synthesis=c.synthesis;auto synth=control.Prepare("SELECT json_set(?1,'$.tool_report.normalization',json(?2),'$.tool_report.primitive_audit',json(?3))");synth.BindText(1,synthesis);synth.BindText(2,transform);synth.BindText(3,Field(control,normalization,"primitive_audit","object"));Check(synth.Step(),"cannot construct synthesis report");synthesis=synth.TypedText(0);
  Fields summary{{"schema",JsonQuoteUtf8("emuflow.phase1-report/v1")},{"phase","1"},{"status",JsonQuoteUtf8("pass")},{"design",JsonQuoteUtf8(design)},{"platform",JsonQuoteUtf8(platform)},{"synthesis",synthesis}};
  const auto summary_text=Encode(summary);Check(summary_text.size()<=512*1024,"frontend summary exceeds bound");
  Fields report=summary;report["resource_totals"]=Encode(totals);report["fits_on_fpga"]=Encode(fits);report["fits_on_platform"]="true";report["fit_scope"]=JsonQuoteUtf8(single?"single_fpga":"aggregate_platform");report["aggregate_effective_capacity"]=Encode(aggregate_json);report["cut_classes"]=Encode(classes);report["require_no_fabric_clock"]=c.require_no_fabric_clock?"true":"false";report["warnings"]=warnings;report["indexed_staging_transform"]=transform;
  report["artifacts"]="{\"emuir\":\"design.emuir.sqlite\",\"platform\":\"platform.normalized.json\",\"report\":\"phase1_report.json\"}";
  // Details remain a streamed derived report. They are never deserialized by
  // the controller or carried to a subsequent phase.
  PublishTextArtifact((root/"phase1_report.json").string(),c.sources,[&](std::ostream& out){
    out<<"{\"clock_topology\":{\"status\":"<<JsonQuoteUtf8(clock.fabric?"unsafe":"pass")<<",\"ff_clock_nets\":"<<clock.nets<<",\"fabric_logic_clock_nets\":"<<clock.fabric<<",\"fabric_logic_clocked_ffs\":"<<clock.ffs<<",\"maximum_fabric_clock_fanout\":"<<clock.maximum<<",\"fabric_clock_details\":[";
    bool first=true;VisitClocks(db,[&](const auto& name,const auto& drivers,auto count){if(!first)out<<',';first=false;out<<"{\"net\":"<<JsonQuoteUtf8(name)<<",\"lut_drivers\":"<<Array(drivers)<<",\"clocked_ffs\":"<<count<<'}';});out<<"]}";
    for(const auto& field:report)out<<','<<JsonQuoteUtf8(field.first)<<':'<<field.second;out<<"}\n";
  });
  PublishTextArtifact((root/"platform.normalized.json").string(),c.sources,[&](std::ostream& out){out<<c.platform<<'\n';});
  PublishTextArtifact((root/"frontend-summary.json").string(),c.sources,[&](std::ostream& out){out<<summary_text<<'\n';});
  PublishToolResult((root/"yosys-execution.json").string(),tool);
  return summary_text;
}
}
StageResult RunFrontend(const ArtifactRef& request,const volatile std::sig_atomic_t* cancelled,const std::string& expected_output){
  std::string output;std::optional<Configuration> configuration;
  return RunStage("phase1.run",[&]{Check(request.schema()=="frontend-request-v1","request schema is invalid");configuration=Read(request);output=configuration->output;return configuration->sources;},[&](const auto&){
    auto& c=*configuration;Check(expected_output.empty() || fs::weakly_canonical(output)==fs::weakly_canonical(expected_output),"output escapes compiler ownership");const auto parent=fs::path(output).parent_path();fs::create_directories(parent);
    Check(!fs::exists(output) && !fs::is_symlink(output),"output directory must be absent");Workspace workspace(parent);
    const auto stage=workspace.root/"phase1";fs::create_directory(stage);
    const auto import_path=workspace.root/"import.bin";CheckedRequestWriter writer(import_path.string(),c.sources);
    writer.Put<std::uint64_t>(0x31524959464d5545ULL);writer.Put<std::uint32_t>(1);writer.Put<std::uint32_t>(1);writer.Text(c.top);writer.Text("<yosys-stdout>");writer.Put<std::uint32_t>(c.clocks.size());for(const auto& clock:c.clocks)writer.Text(clock);writer.Generation(c.sources[1].generation());writer.Finish();
    const auto import_request=ArtifactRef::Bind(import_path.string(),"yosys-emuir-import-request-v1");
    StageResult imported;const auto import_summary=workspace.root/"import.json";
    auto tool=RunExternalToolStream(c.tool,[&](std::istream& stream){imported=ImportYosysEmuir(import_request,stream,(stage/"design.emuir.sqlite").string(),import_summary.string(),&c.sources[1]);if(!imported.ok())throw StageError(imported.error,imported.message);},cancelled,c.sources);
    Check(tool.operation.ok(),"Yosys/import failed: "+tool.operation.message+"\n"+tool.diagnostic_tail);
    Check(imported.ok() && imported.outputs.size()==2,"native source importer did not complete");for(const auto& source:imported.outputs)source.RequireUnchanged();
    BuildReport(stage,c,ReadSmall(import_summary),tool);
    for(const auto& source:imported.outputs)source.RequireUnchanged();
    // The shared transaction requires same-directory staging. Adopt by rename,
    // with no SQLite row copy or public partially populated stage directory.
    const auto sibling=parent/(workspace.root.filename().string()+"-publish");fs::rename(stage,sibling);
    try{PublishNewDirectory(sibling.string(),output,c.sources);}catch(...){std::error_code error;fs::remove_all(sibling,error);throw;}
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(output)/"frontend-summary.json").string(),"phase1-summary-v1"),ArtifactRef::Bind((fs::path(output)/"design.emuir.sqlite").string(),"emuir-compact-v2")};});
}
}
