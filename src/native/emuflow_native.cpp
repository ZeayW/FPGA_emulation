#include <iostream>
#include <string>
#include <vector>
#include <csignal>
#include <filesystem>

#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/tool_runtime.hpp"
#include "emuflow_core/compile.hpp"

namespace {
using emuflow::native::Stage;
using emuflow::native::StageRequest;
constexpr const char* kUsage =
    "usage: emuflow-native [--report RESULT_JSON] PHASE OPERATION ...\n"
    "  check-runtime\n"
    "  compile REQUEST_JSON\n"
    "  run-tool REQUEST RESULT_JSON\n"
    "  phase3 analyze INPUT OUTPUT\n"
    "  phase3 analyze --indexed-input EMUIR INPUT OUTPUT\n"
    "  phase3 finalize-semantics TOPOLOGY METADATA OUTPUT\n"
    "  phase3 resolve-captures EMUIR REQUEST TOPOLOGY PARTS OUTPUT\n"
    "  phase3 hash-semantics NODES CAPTURES HEADER OUTPUT\n"
    "  phase3 write-exact-sections NODES CAPTURES REQUEST OUTPUT\n"
    "  phase3 refine-patron [--summary-output|--trace-output] [--typed-output] "
        "(--typed-input|--legacy-text) INPUT OUTPUT\n"
    "  phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT\n"
    "  phase3 build-candidate-ids EMUIR POLICY OUTPUT\n"
    "  phase3 build-clusters EMUIR REQUEST OUTPUT\n"
    "  phase3 reduce-loads EMUIR PRIMARY REQUEST OUTPUT\n"
    "  phase3 refine-patron-from-store EMUIR TIMING CLUSTERS REQUEST OUTPUT\n"
    "  phase4 route --typed-output INPUT OUTPUT\n"
    "  phase5 schedule INPUT OUTPUT\n"
    "  phase5 schedule-from-store ROUTE_STORE REQUEST OUTPUT\n"
    "  phase5 build-schedule ROUTE_STORE BOARD_REQUEST GENERATION_REQUEST OUTPUT\n"
    "  phase5 publish-schedule INPUT RESULT ROUTE_STORE REQUEST OUTPUT\n"
    "  phase6 assign-banks (--typed-input|--legacy-text) INPUT OUTPUT\n"
    "  phase6 group-signals --typed-input INPUT OUTPUT EXPECTED_SHA256\n"
    "  phase6 group-signals --legacy-text INPUT OUTPUT\n"
    "  phase6 refine-groups --typed-input INPUT OUTPUT EXPECTED_SHA256\n"
    "  phase6 refine-groups --legacy-text INPUT OUTPUT\n"
    "  phase6 rudy INPUT OUTPUT\n"
    "  phase7 export-bookshelf MAPPED ATOMS NETS SUMMARY SPOOL\n";

int Finish(emuflow::core::StageResult result, const std::string& report) {
  // Invalid requests may name a report which aliases an input/output. Never
  // overwrite that artifact merely to report rejection of the alias itself.
  if (!report.empty() && result.error != emuflow::core::ErrorCode::kInvalidRequest) {
    try { emuflow::core::PublishStageResult(report, result); }
    catch (const emuflow::core::StageError& e) {
      result.error = e.code(); result.message = e.what();
    }
  }
  if (result.ok()) return 0;
  std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
  return result.error == emuflow::core::ErrorCode::kInvalidRequest ? 2 : 1;
}

int Execute(StageRequest request, const std::string& report) {
  request.report_path = report;
  return Finish(emuflow::native::ExecuteStage(request),report);
}

volatile std::sig_atomic_t tool_cancelled = 0;
void CancelTool(int signal) { tool_cancelled = signal; }
struct ToolSignals {
  struct sigaction old_int{}, old_term{}, old_hup{};
  ToolSignals() {
    struct sigaction action{}; action.sa_handler = CancelTool; sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, &old_int); sigaction(SIGTERM, &action, &old_term);
    sigaction(SIGHUP, &action, &old_hup);
  }
  ~ToolSignals() {
    sigaction(SIGINT, &old_int, nullptr); sigaction(SIGTERM, &old_term, nullptr);
    sigaction(SIGHUP, &old_hup, nullptr);
  }
};
bool SamePath(const std::string& a, const std::string& b) {
  std::error_code error;
  return std::filesystem::equivalent(a,b,error) ||
      std::filesystem::weakly_canonical(a) == std::filesystem::weakly_canonical(b);
}
int RunTool(const std::string& input, const std::string& output) {
  // Reject aliases before launching an engine, not after it has had side effects.
  auto ref = emuflow::core::ArtifactRef::Bind(input,"external-tool-request-v1");
  auto request = emuflow::core::ReadToolRequest(ref.path());
  if (output.empty() || output == "-" || SamePath(input,output) || SamePath(request.argv[0],output))
    throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,
                                  "external tool result aliases a source or is not a file");
  const auto parent = std::filesystem::absolute(output).parent_path();
  if (!std::filesystem::is_directory(parent) || std::filesystem::is_directory(output))
    throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,
                                  "external tool result directory is unavailable");
  ToolSignals signals;
  const auto result = emuflow::core::RunExternalTool(request,&tool_cancelled,{ref});
  emuflow::core::PublishToolResult(output,result);
  // A cancelled supervisor completes group cleanup and evidence publication
  // before returning. No tool output is mixed into forwarded Yosys stdout.
  if (!result.operation.ok())
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result.operation);
  return result.operation.ok() ? 0 : 1;
}

int Dispatch(int argc, char** argv, const std::string& report) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << kUsage; return 0;
  }
  if (argc == 2 && std::string(argv[1]) == "check-runtime")
    return Finish(emuflow::core::CheckNativeRuntime(),report);
  if (argc == 4 && std::string(argv[1]) == "run-tool" && report.empty())
    return RunTool(argv[2],argv[3]);
  if (argc == 3 && std::string(argv[1]) == "compile" && report.empty()) {
    ToolSignals signals;
    const auto configuration = emuflow::core::ArtifactRef::Bind(argv[2],"native-compile-request-v1");
    const auto result = emuflow::core::Compile(configuration,&tool_cancelled);
    emuflow::core::PublishCompileResult(result);
    return Finish(result.operation,"");
  }
  const std::string phase = argc > 1 ? argv[1] : "";
  const std::string operation = argc > 2 ? argv[2] : "";
  auto run = [&](Stage stage, std::vector<std::string> inputs,
                 const std::string& output, bool typed = false,
                 bool indexed = false, const std::string& sha = "") {
    StageRequest request{stage, std::move(inputs), output};
    request.typed_input = typed; request.indexed_input = indexed;
    request.typed_output = stage == Stage::kRoute;
    request.expected_sha256 = sha;
    return Execute(std::move(request), report);
  };
  if (phase == "phase3" && operation == "refine-patron-from-primary" && argc == 9)
    return run(Stage::kRefinePatronFromPrimary,{argv[3],argv[4],argv[5],argv[6],argv[7]},argv[8]);
  if (phase == "phase3" && operation == "reduce-loads" && argc == 7)
    return run(Stage::kReducePartitionLoads,{argv[3],argv[4],argv[5]},argv[6]);
  if (phase == "phase3" && operation == "build-candidate-ids" && argc == 6)
    return run(Stage::kBuildCandidateIds,{argv[3],argv[4]},argv[5]);
  if (phase == "phase3" && operation == "build-clusters" && argc == 6)
    return run(Stage::kBuildPartitionClusters,{argv[3],argv[4]},argv[5]);
  if (phase == "phase3") {
    if (operation == "refine-patron-from-store" && argc == 8)
      return run(Stage::kRefinePatronFromStore,{argv[3],argv[4],argv[5],argv[6]},argv[7]);
    if (operation == "analyze" && argc == 5)
      return run(Stage::kAnalyzeExact, {argv[3]}, argv[4]);
    if (operation == "analyze" && argc == 7 && std::string(argv[3]) == "--indexed-input")
      return run(Stage::kAnalyzeExact, {argv[4], argv[5]}, argv[6], false, true);
    if (operation == "finalize-semantics" && argc == 6)
      return run(Stage::kFinalizeExact, {argv[3], argv[4]}, argv[5]);
    if (operation == "resolve-captures" && argc == 8)
      return run(Stage::kResolveCaptures, {argv[3], argv[4], argv[5], argv[6]}, argv[7]);
    if (operation == "hash-semantics" && argc == 7)
      return run(Stage::kHashSemantics, {argv[3], argv[4], argv[5]}, argv[6]);
    if (operation == "write-exact-sections" && argc == 7)
      return run(Stage::kWriteExactSections, {argv[3], argv[4], argv[5]}, argv[6]);
    if (operation == "refine-patron") {
      StageRequest request{Stage::kRefinePatron, {}, ""};
      request.typed_input = false;  // Require an explicit CLI format flag.
      std::vector<std::string> positional;
      bool legacy = false, invalid = false, trace = false, summary = false, typed_output = false;
      for (int i = 3; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--typed-input") request.typed_input = true;
        else if (arg == "--legacy-text") legacy = true;
        else if (arg == "--typed-output") typed_output = true;
        else if (arg == "--summary-output") summary = true;
        else if (arg == "--trace-output") trace = true;
        else if (arg.rfind("--", 0) == 0) invalid = true;
        else positional.push_back(arg);
      }
      if (!invalid && !(trace && (summary || typed_output)) && positional.size() == 2 && legacy != request.typed_input) {
        request.summary_output = !trace;
        request.typed_output = !trace;
        request.input_paths = {positional[0]}; request.output_path = positional[1];
        return Execute(std::move(request), report);
      }
    }
  }
  if (phase == "phase4" && operation == "route" && argc == 6 && std::string(argv[3]) == "--typed-output")
    return run(Stage::kRoute, {argv[4]}, argv[5]);
  if (phase == "phase5" && operation == "schedule-from-store" && argc == 6)
    return run(Stage::kScheduleFromStore, {argv[3],argv[4]}, argv[5]);
  if (phase == "phase5" && operation == "build-schedule" && argc == 7)
    return run(Stage::kBuildSchedule, {argv[3],argv[4],argv[5]}, argv[6]);
  if (phase == "phase5" && operation == "publish-schedule" && argc == 8)
    return run(Stage::kPublishSchedule, {argv[3], argv[4], argv[5], argv[6]}, argv[7]);
  if (phase == "phase5" && operation == "schedule" && argc == 5)
    return run(Stage::kSchedule, {argv[3]}, argv[4]);
  if (phase == "phase6") {
    if (operation == "rudy" && argc == 5)
      return run(Stage::kRudy, {argv[3]}, argv[4]);
    const std::string mode = argc > 3 ? argv[3] : "";
    if (operation == "assign-banks" && argc == 6 && (mode == "--typed-input" || mode == "--legacy-text"))
      return run(Stage::kAssignBanks, {argv[4]}, argv[5], mode == "--typed-input");
    if (operation == "group-signals" || operation == "refine-groups") {
      const auto stage = operation == "group-signals" ? Stage::kGroupSignals : Stage::kRefinePositions;
      if (argc == 7 && mode == "--typed-input")
        return run(stage, {argv[4]}, argv[5], true, false, argv[6]);
      if (argc == 6 && mode == "--legacy-text")
        return run(stage, {argv[4]}, argv[5]);
    }
  }
  if (phase == "phase7" && operation == "export-bookshelf" && argc == 8) {
    StageRequest request{Stage::kExportBookshelf,{argv[3],argv[4]},argv[5]};
    request.auxiliary_output_path = argv[6]; request.scratch_path = argv[7];
    return Execute(std::move(request),report);
  }
  emuflow::core::StageResult result;
  result.stage = "native.request"; result.error = emuflow::core::ErrorCode::kInvalidRequest;
  result.message = "unknown or invalid native stage request";
  std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result) << kUsage;
  return 2;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    std::string report;
    if (argc > 1 && std::string(argv[1]) == "--report") {
      if (argc < 4 || std::string(argv[2]).empty() || std::string(argv[2]) == "-") {
        std::cerr << kUsage; return 2;
      }
      report = argv[2]; argc -= 2; argv += 2;
    }
    return Dispatch(argc, argv, report);
  } catch (const emuflow::core::StageError& error) {
    emuflow::core::StageResult result;
    result.stage = "native.request"; result.error = error.code();
    result.message = error.what();
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
    return result.error == emuflow::core::ErrorCode::kInvalidRequest ? 2 : 1;
  } catch (const std::exception& error) {
    emuflow::core::StageResult result;
    result.stage = "native.request"; result.error = emuflow::core::ErrorCode::kExecutionFailed;
    result.message = error.what();
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
    return 1;
  }
}
