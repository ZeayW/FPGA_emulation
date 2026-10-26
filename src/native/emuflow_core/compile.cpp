#include "emuflow_core/compile.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/phase5.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <unistd.h>
#include <fcntl.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
constexpr const char* stages[] = {"preflight","frontend","timing","partition","route-policy",
    "route","tdm","split","physical","signoff","finalize"};
void Require(bool condition, const std::string& message) {
  if (!condition) throw StageError(ErrorCode::kInvalidRequest,message);
}
// JSON is bounded control-plane configuration, never a graph store. SQLite's
// existing JSON reader avoids adding an unrelated parser/dependency closure.
class ControlJson {
 public:
  explicit ControlJson(const std::string& path, std::size_t limit = 8 * 1024 * 1024)
      : db_(":memory:",SqliteDatabase::Mode::kReadOnly) {
    const auto size = fs::file_size(path);
    Require(size <= limit,"native compiler control file exceeds bound");
    std::ifstream stream(path,std::ios::binary);
    data_.resize(size); stream.read(data_.data(),size);
    Require(bool(stream),"cannot read native compiler control file");
    (void)JsonQuote(data_);
    auto valid = db_.Prepare("SELECT json_valid(?1),json_type(?1)"); valid.BindText(1,data_);
    Require(valid.Step() && valid.Integer(0) && valid.Text(1) == "object","invalid native compiler JSON object");
    auto duplicates = db_.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL "
        "GROUP BY parent,key HAVING count(*) > 1 LIMIT 1"); duplicates.BindText(1,data_);
    Require(!duplicates.Step(),"duplicate native compiler JSON key");
  }
  std::string String(const std::string& path) {
    auto field = db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
    field.BindText(1,data_); field.BindText(2,path);
    Require(field.Step() && field.Text(0) == "text","native compiler requires string field " + path);
    auto value = field.Text(1);
    Require(value.find('\0') == std::string::npos,"NUL in native compiler field");
    return value;
  }
  void Envelope() {
    auto fields = db_.Prepare("SELECT count(*),sum(key IN ('schema','python_executable',"
        "'python_source','working_directory','options')) FROM json_each(?1)"); fields.BindText(1,data_);
    Require(fields.Step() && fields.Integer(0) == 5 && fields.Integer(1) == 5,
            "unknown native compiler request fields");
    Require(String("$.schema") == "emuflow.native-compile/v1","unsupported native compiler request schema");
  }
  bool Generation(const FileGeneration& expected) {
    const std::string fields[] = {std::to_string(expected.device),std::to_string(expected.inode),
      std::to_string(expected.bytes),std::to_string(expected.modified_seconds),std::to_string(expected.modified_nanoseconds),
      std::to_string(expected.changed_seconds),std::to_string(expected.changed_nanoseconds)};
    auto size = db_.Prepare("SELECT json_array_length(?1,'$.request_generation')"); size.BindText(1,data_);
    if (!size.Step() || size.Integer(0) != 7) return false;
    for (unsigned i = 0; i < 7; ++i) {
      if (String("$.request_generation[" + std::to_string(i) + "]") != fields[i]) return false;
    }
    return true;
  }
  bool StateShape() {
    auto shape = db_.Prepare("SELECT json_type(?1,'$.state'),json_array_length(?1,'$.state'),"
        "json_extract(?1,'$.state[0]'),json_type(?1,'$.state[1]')"); shape.BindText(1,data_);
    return shape.Step() && shape.Text(0) == "array" && shape.Integer(1) == 2 &&
        shape.Text(2) == "map" && shape.Text(3) == "object";
  }
  std::string Object(const std::string& path) {
    auto value = db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
    value.BindText(1,data_); value.BindText(2,path);
    Require(value.Step() && value.Text(0) == "object","native compiler requires object field " + path);
    return value.Text(1);
  }
  std::string StatePath(const std::string& name) {
    const auto path="$.state[1]."+name;
    Require(String(path+"[0]")=="path","native compiler requires a path binding for "+name);
    const auto value=String(path+"[1]");Require(fs::path(value).is_absolute(),"native compiler state path is relative");return value;
  }
  bool OutputGeneration(const ArtifactRef& output) {
    if(String("$.outputs[0].schema")!=output.schema() || String("$.outputs[0].path")!=output.path())return false;
    const auto& g=output.generation();
    const std::pair<const char*,std::int64_t> values[]={{"device",std::int64_t(g.device)},{"inode",std::int64_t(g.inode)},{"bytes",std::int64_t(g.bytes)},{"mtime_seconds",g.modified_seconds},{"mtime_nanoseconds",g.modified_nanoseconds},{"ctime_seconds",g.changed_seconds},{"ctime_nanoseconds",g.changed_nanoseconds}};
    for(const auto& item:values) {
      auto row=db_.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,data_);row.BindText(2,std::string("$.outputs[0].generation.")+item.first);
      if(!row.Step() || row.Text(0)!="integer" || row.TypedInteger(1)!=item.second)return false;
    }return true;
  }
  std::string Phase5State(const std::string& reference) {
    auto row=db_.Prepare("SELECT json_set(?1,'$.stage','tdm','$.state[1].phase5_report',json(?2))");row.BindText(1,data_);row.BindText(2,reference);Require(row.Step(),"cannot construct native Phase 5 compiler state");return row.TypedText(0);
  }
  void RequirePublishedPaths(const fs::path& temporary) {
    const auto root = fs::weakly_canonical(temporary);
    const auto check = [&](const fs::path& path) {
      const auto relative = path.lexically_relative(root);
      Require(relative.empty() || *relative.begin() == "..",
              "native compiler state references a phase temporary path");
    };
    auto paths = db_.Prepare("SELECT atom FROM json_tree(?1,'$.state') WHERE type='text'");
    paths.BindText(1,data_);
    while (paths.Step()) {
      const fs::path path(paths.Text(0));
      if (!path.is_absolute()) continue;
      check(path.lexically_normal());
      std::error_code error;
      const auto canonical = fs::weakly_canonical(path,error);
      // Control strings also include diagnostic text, not only file paths.
      // Do not treat an unresolvable string as a filesystem lookup failure.
      if (!error) check(canonical);
    }
  }
 private:
  SqliteDatabase db_;
  std::string data_;
};
struct Scratch {
  fs::path root;
  explicit Scratch(const fs::path& parent) {
    root = parent / (".emuflow-compile-" + std::to_string(getpid()) + "-" +
        std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    Require(fs::create_directory(root),"native compiler scratch already exists");
    fs::create_directory(root / "tmp");
  }
  ~Scratch() { std::error_code error; fs::remove_all(root,error); }
};
void ClaimOutput(const fs::path& output, CompileResult& result) {
  const auto owner = (output / ".native-compile-owner").string();
  const int fd = open(owner.c_str(),O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC,0600);
  Require(fd >= 0,"native compiler output is already owned");
  const auto count = write(fd,result.run_id.data(),result.run_id.size());
  const auto synced = fsync(fd); close(fd);
  if (count != static_cast<ssize_t>(result.run_id.size()) || synced != 0) {
    fs::remove(owner);
    throw StageError(ErrorCode::kPublicationFailed,"cannot claim native compiler output");
  }
  result.owns_output = true;
}
void RequireOwner(const CompileResult& result) {
  std::ifstream owner(fs::path(result.output_directory) / ".native-compile-owner");
  std::string run_id; owner >> run_id;
  Require(run_id == result.run_id,"native compiler output owner changed");
}
}  // namespace

CompileResult Compile(const ArtifactRef& configuration,const ArtifactRef& native_executable, const volatile std::sig_atomic_t* cancelled) {
  CompileResult result;
  std::string python, python_source, adapter, working_directory;
  result.operation = RunStage("compile", [&] {
    Require(native_executable.schema()=="external-tool-executable-v1","invalid native compiler executable schema");
    Require(configuration.schema() == "native-compile-request-v1","invalid native compiler artifact schema");
    configuration.RequireUnchanged();
    ControlJson request(configuration.path(),1024 * 1024); request.Envelope();
    result.request_configuration = request.Object("$");
    python = request.String("$.python_executable"); python_source = request.String("$.python_source");
    auto output = request.String("$.options.output_dir");
    working_directory = request.String("$.working_directory");
    Require(fs::path(python).is_absolute() && fs::path(python_source).is_absolute() && fs::path(output).is_absolute(),
            "native compiler executable, source and output paths must be absolute");
    Require(fs::is_directory(python_source),"native compiler Python source directory is unavailable");
    Require(fs::path(working_directory).is_absolute() && fs::is_directory(working_directory),
            "native compiler invocation directory is unavailable or not absolute");
    adapter = (fs::path(python_source) / "emuflow/native_compile_adapter.py").string();
    result.output_directory = fs::weakly_canonical(output).string();
    Require(!fs::exists(output) || (fs::is_directory(output) && fs::is_empty(output)),
            "native compiler output must be an empty directory");
    return std::vector<ArtifactRef>{configuration,ArtifactRef::Bind(adapter,"native-compile-adapter-v1"),
                                   ArtifactRef::Bind(python,"external-tool-executable-v1"),
                                   native_executable};
  }, [&](const auto& inputs) {
    const auto output = fs::path(result.output_directory);
    fs::create_directories(output.parent_path());
    Scratch scratch(output.parent_path());
    const auto run_id = scratch.root.filename().string();
    result.run_id = run_id;
    fs::path previous;
    for (const auto* stage : stages) {
      if (cancelled && *cancelled) throw StageError(ErrorCode::kExecutionFailed,"native compilation cancelled");
      if (result.owns_output) RequireOwner(result);
      for (const auto& input : inputs) input.RequireUnchanged();
      Require(fs::canonical(python) == inputs[2].path(),"native compiler executable alias changed");
      const auto next = scratch.root / (std::string(stage) + ".json");
      const auto temporary = scratch.root / "tmp" / stage;
      fs::create_directory(temporary);
      ToolRequest tool;
      const bool native_phase5=std::string(stage)=="tdm";
      std::string phase5_report;
      if(native_phase5) {
        ControlJson prior(previous.string());
        Require(prior.String("$.stage")=="route" && prior.String("$.state[1].effective_tdm_provider")=="deterministic-dependency-earliest-slot-v1","invalid native Phase 5 compiler predecessor/provider");
        const auto phase5_root=fs::path(prior.StatePath("phase5_root"));
        Require(fs::weakly_canonical(phase5_root)==output/"tdm","native Phase 5 output escapes compiler ownership");
        phase5_report=(phase5_root/"phase5_report.json").string();
        tool.argv={inputs[3].path(),"--report",(temporary/"native-result.json").string(),"phase5","compile",
            (fs::path(prior.StatePath("phase4_root"))/"routes.json").string(),
            (fs::path(prior.StatePath("phase3_root"))/"assignment.sqlite").string(),
            (fs::path(prior.StatePath("phase1_root"))/"platform.normalized.json").string(),phase5_report};
      } else {
        tool.argv = {python,"-m","emuflow.native_compile_adapter","--stage",stage,
                     "--request",configuration.path(),"--output",next.string(),"--run-id",run_id};
        if (!previous.empty()) { tool.argv.push_back("--previous"); tool.argv.push_back(previous.string()); }
      }
      tool.working_directory = working_directory;
      tool.environment = {{"PYTHONPATH",python_source},{"TMPDIR",temporary.string()},
          {"TMP",temporary.string()},{"TEMP",temporary.string()},{"SQLITE_TMPDIR",temporary.string()},
          {"XDG_CACHE_HOME",(temporary / "cache").string()}};
      tool.tail_bytes = 32768;
      // Adapters unwind nested stream/tool owners before leaving their group.
      tool.termination_grace_ms = 10000;
      auto step_inputs = inputs;
      if (!previous.empty()) step_inputs.push_back(ArtifactRef::Bind(previous.string(),"native-compile-state-v1"));
      const auto step = RunExternalTool(tool,cancelled,step_inputs);
      result.steps.push_back({stage,step.operation.ok(),native_phase5?"native-exact-phase5-v1":"isolated-python-adapter-v1",step.wall_seconds,step.user_cpu_seconds,
                             step.system_cpu_seconds,step.maximum_process_rss_kib});
      if (!step.operation.ok())
        throw StageError(ErrorCode::kExecutionFailed,"native compile " + std::string(stage) + ": " +
                         step.operation.message + "\n" + step.diagnostic_tail);
      if(native_phase5) {
        ControlJson producer((temporary/"native-result.json").string());
        auto published=ArtifactRef::Bind(phase5_report,"phase5-report-v1");
        Require(producer.String("$.schema")=="emuflow.native-stage-result/v1" && producer.String("$.stage")=="phase5.compile" && producer.String("$.status")=="pass" && producer.OutputGeneration(published),"native Phase 5 producer result binding disagrees");
        ControlJson prior(previous.string());const auto state=prior.Phase5State(Phase5ReportControlReference(published));
        auto bindings=step_inputs;bindings.push_back(published);
        PublishTextArtifact(next.string(),bindings,[&](std::ostream& stream){stream<<state<<'\n';});
        const auto metrics=(output/"stage-metrics"/"phase5.json");fs::create_directories(metrics.parent_path());
        const auto text=JsonRecord(std::map<std::string,JsonRecord>{{"stage","phase5"},{"status","pass"},{"wall_seconds",step.wall_seconds},{"peak_process_tree_rss_kib",step.maximum_process_rss_kib},{"rss_measurement","wait4-single-native-process-high-water"}}).Encode();
        PublishTextArtifact(metrics.string(),bindings,[&](std::ostream& stream){stream<<text<<'\n';});
      }
      try {
        auto state_ref = ArtifactRef::Bind(next.string(),"native-compile-state-v1");
        ControlJson state(state_ref.path());
        Require(state.String("$.schema") == "emuflow.native-compile-state/v1" &&
                state.String("$.stage") == stage && state.String("$.run_id") == run_id &&
                state.String("$.status") == "pass" && state.StateShape() && state.Generation(configuration.generation()),
                "mixed or invalid native compiler phase result");
        state.RequirePublishedPaths(temporary);
        state_ref.RequireUnchanged();
        if (std::string(stage) == "preflight") result.effective_configuration = state.Object("$.effective_options");
      } catch (const std::exception& error) {
        result.steps.back().passed = false;
        throw StageError(ErrorCode::kInvalidArtifact,"native compile " + std::string(stage) + ": " + error.what());
      }
      if (std::string(stage) == "preflight") ClaimOutput(output,result);
      try {
        // Adapter execution and native group cleanup have completed. No phase
        // may carry a temporary graph/cache into the next process through state.
        fs::remove_all(temporary);
      } catch (const std::exception& error) {
        result.steps.back().passed = false;
        throw StageError(ErrorCode::kPublicationFailed,
                         "cannot retire native phase temporary files: " + std::string(error.what()));
      }
      if (!previous.empty()) fs::remove(previous);
      previous = next;
      std::cout << "COMPILE_STAGE_COMPLETE " << stage << " " << step.wall_seconds << "s\n" << std::flush;
    }
    ControlJson report((output / "multi-fpga-flow-report.json").string());
    Require(report.String("$.schema") == "emuflow.multi-fpga-flow/v2" && report.String("$.status") == "pass",
            "native compiler final flow report is invalid");
  }, [&] {
    return std::vector<ArtifactRef>{ArtifactRef::Bind(
        (fs::path(result.output_directory) / "multi-fpga-flow-report.json").string(),"multi-fpga-flow-report-v2")};
  });
  return result;
}
std::string CompileResultJson(const CompileResult& result) {
  std::ostringstream out; out << std::setprecision(17);
  out << "{\"schema\":\"emuflow.native-compile-result/v2\",\"implementation_id\":\"native-orchestration-v1\","
      << "\"phase_implementation\":\"native-phase5-and-isolated-python-adapters-v1\",\"native_data_plane_complete\":false,"
      << "\"operation\":" << StageResultJson(result.operation) << ",\"output_directory\":" << JsonQuote(result.output_directory)
      << ",\"request_configuration\":" << (result.request_configuration.empty() ? "null" : result.request_configuration)
      << ",\"effective_configuration\":" << (result.effective_configuration.empty() ? "null" : result.effective_configuration)
      << ",\"stages\":[";
  bool first = true;
  for (const auto& stage : result.steps) {
    if (!first) out << ',';
    first = false;
    out << "{\"stage\":" << JsonQuote(stage.stage) << ",\"implementation_id\":" << JsonQuote(stage.implementation_id) << ",\"status\":\"" << (stage.passed ? "pass" : "fail")
        << "\",\"wall_seconds\":" << stage.wall_seconds << ",\"user_cpu_seconds\":" << stage.user_cpu_seconds
        << ",\"system_cpu_seconds\":" << stage.system_cpu_seconds << ",\"maximum_process_rss_kib\":" << stage.maximum_process_rss_kib
        << ",\"cpu_scope\":\"wait4-child-and-reaped-descendants\",\"rss_scope\":\"wait4-maximum-process-high-water\"}";
  }
  out << "]}\n"; return out.str();
}
void PublishCompileResult(const CompileResult& result) {
  if (!result.owns_output) return;
  RequireOwner(result);
  const auto text = CompileResultJson(result);
  AtomicBinaryWriter writer((fs::path(result.output_directory) / "native-compile-result.json").string());
  writer.WriteBytes(text.data(),text.size()); writer.Commit();
  fs::remove(fs::path(result.output_directory) / ".native-compile-owner");
}
}  // namespace emuflow::core
