#include "frontend/frontend.hpp"
#include "frontend/report.hpp"
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
void Check(bool ok,const std::string& message){if(!ok)throw StageError(ErrorCode::kInvalidArtifact,"frontend "+message);}
std::string ReadSmall(const fs::path& path){const auto size=fs::file_size(path);Check(size<=512*1024,"summary exceeds bound");std::ifstream file(path,std::ios::binary);std::string result(size,'\0');file.read(result.data(),size);Check(bool(file),"cannot read summary");return result;}
struct Workspace{
  fs::path root;
  explicit Workspace(const fs::path& parent){std::string name=(parent/".frontend-generation-XXXXXX").string();Check(mkdtemp(name.data())!=nullptr,"cannot create private generation");root=name;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
std::vector<std::string> Catalog(CheckedRequestReader& reader){const auto count=reader.Read<std::uint32_t>();Check(count<=16384,"configuration catalog exceeds bound");std::vector<std::string> values;for(unsigned i=0;i<count;++i)values.push_back(reader.ReadString32(false,1024*1024));return values;}
struct Configuration: FrontendReportConfiguration{
  std::string output,top,library;std::vector<std::string> clocks;ToolRequest tool;
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
    BuildFrontendReport(stage,c,ReadSmall(import_summary),&tool);
    for(const auto& source:imported.outputs)source.RequireUnchanged();
    // The shared transaction requires same-directory staging. Adopt by rename,
    // with no SQLite row copy or public partially populated stage directory.
    const auto sibling=parent/(workspace.root.filename().string()+"-publish");fs::rename(stage,sibling);
    try{PublishNewDirectory(sibling.string(),output,c.sources);}catch(...){std::error_code error;fs::remove_all(sibling,error);throw;}
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(output)/"frontend-summary.json").string(),"phase1-summary-v1"),ArtifactRef::Bind((fs::path(output)/"design.emuir.sqlite").string(),"emuir-compact-v2")};});
}
}
