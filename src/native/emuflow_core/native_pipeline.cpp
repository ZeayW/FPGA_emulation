#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/schedule_store.hpp"

#include <filesystem>
#include <iostream>

namespace emuflow::native {
const char* StageName(Stage stage) {
  switch (stage) {
    case Stage::kInvalid: return "native.request";
    case Stage::kAnalyzeExact: return "phase3.analyze";
    case Stage::kFinalizeExact: return "phase3.finalize-semantics";
    case Stage::kResolveCaptures: return "phase3.resolve-captures";
    case Stage::kHashSemantics: return "phase3.hash-semantics";
    case Stage::kWriteExactSections: return "phase3.write-exact-sections";
    case Stage::kRefinePatron: return "phase3.refine-patron";
    case Stage::kRoute: return "phase4.route";
    case Stage::kSchedule: return "phase5.schedule";
    case Stage::kPublishSchedule: return "phase5.publish-schedule";
    case Stage::kAssignBanks: return "phase6.assign-banks";
    case Stage::kGroupSignals: return "phase6.group-signals";
    case Stage::kRefinePositions: return "phase6.refine-groups";
    case Stage::kRudy: return "phase6.rudy";
  }
  return "unknown-native-stage";
}
namespace {
std::vector<std::string> InputSchemas(const StageRequest& request) {
  switch (request.stage) {
    case Stage::kInvalid: break;
    case Stage::kAnalyzeExact:
      return request.indexed_input
          ? std::vector<std::string>{"emuir-compact-v2", "exact-indexed-request-v4"}
          : std::vector<std::string>{"exact-analysis-input-v3"};
    case Stage::kFinalizeExact: return {"exact-topology-v3", "exact-identities-v1"};
    case Stage::kResolveCaptures:
      return {"emuir-compact-v2", "exact-indexed-request-v4", "exact-topology-v3", "exact-parts-v1"};
    case Stage::kHashSemantics:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-hash-request-v1"};
    case Stage::kWriteExactSections:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-section-request-v1"};
    case Stage::kRefinePatron: return {request.typed_input ? "patron-input-v1" : "patron-legacy-text"};
    case Stage::kRoute: return {"tlr-input-text"};
    case Stage::kSchedule: return {"exact-schedule-input-v1"};
    case Stage::kPublishSchedule: return {"exact-schedule-input-v1", "exact-schedule-output-v2", "system-route-store-v3", "exact-schedule-publication-request-v1"};
    case Stage::kAssignBanks: return {request.typed_input ? "chimew-bank-input-v1" : "chimew-bank-legacy-text"};
    case Stage::kGroupSignals: return {request.typed_input ? "chimew-group-input-v1" : "chimew-group-legacy-text"};
    case Stage::kRefinePositions: return {request.typed_input ? "chimew-refine-input-v1" : "chimew-refine-legacy-text"};
    case Stage::kRudy: return {"chimew-rudy-input-v1"};
  }
  throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
}
bool Equivalent(const std::string& a, const std::string& b) {
  std::error_code error;
  if (std::filesystem::equivalent(a, b, error)) return true;
  return std::filesystem::absolute(a).lexically_normal() ==
         std::filesystem::absolute(b).lexically_normal();
}
}  // namespace
core::StageResult ExecuteStage(const StageRequest& request) {
  return core::RunStage(StageName(request.stage), [&]() {
    const auto schemas = InputSchemas(request);
    const bool bound = !request.input_artifacts.empty();
    if ((bound && !request.input_paths.empty()) ||
        (bound ? request.input_artifacts.size() : request.input_paths.size()) != schemas.size() || request.output_path.empty())
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage artifact coverage is invalid");
    std::vector<std::string> paths = request.input_paths;
    if (bound) for (const auto& input : request.input_artifacts) paths.push_back(input.path());
    if (request.stage == Stage::kRefinePatron && request.typed_output && !request.summary_output)
      throw core::StageError(core::ErrorCode::kInvalidRequest, "typed PATRON output requires summary mode");
    const bool stream_stage = request.stage == Stage::kAssignBanks || request.stage == Stage::kGroupSignals ||
        request.stage == Stage::kRefinePositions || request.stage == Stage::kRudy;
    const bool pipe_input = stream_stage || (request.stage == Stage::kRefinePatron && request.typed_input);
    if (request.output_path == "-" && !stream_stage)
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage output requires a file");
    std::vector<core::ArtifactRef> inputs;
    for (std::size_t i = 0; i < schemas.size(); ++i) {
      if (paths[i] != "-" && !request.report_path.empty() &&
          Equivalent(paths[i], request.report_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases an input artifact");
    }
    if (!request.report_path.empty() && request.output_path != "-" && Equivalent(request.report_path, request.output_path))
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases the stage output");
    if (request.stage == Stage::kResolveCaptures && !request.report_path.empty() && Equivalent(request.report_path, request.output_path + ".sqlite"))
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases the capture index");
    for (std::size_t i = 0; i < schemas.size(); ++i) {
      auto input = bound ? request.input_artifacts[i] : core::ArtifactRef::Bind(paths[i], schemas[i], pipe_input);
      if (input.is_pipe() && !pipe_input)
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage input requires a file");
      if (input.schema() != schemas[i])
        throw core::StageError(core::ErrorCode::kInvalidArtifact, "native input artifact schema disagrees with the stage contract");
      if (!input.is_pipe() && request.output_path != "-" && Equivalent(input.path(), request.output_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native output aliases an input artifact");
      if (!request.report_path.empty() && !input.is_pipe() && Equivalent(input.path(), request.report_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases an input artifact");
      inputs.push_back(std::move(input));
    }
    return inputs;
  }, [&](const auto& inputs) {
    const auto path = [&](std::size_t i) -> const std::string& { return inputs.at(i).path(); };
    const auto& output = request.output_path;
    switch (request.stage) {
      case Stage::kAnalyzeExact:
        if (request.indexed_input) AnalyzeStaticExact(path(1), output, path(0));
        else AnalyzeStaticExact(path(0), output);
        break;
      case Stage::kFinalizeExact: FinalizeStaticExact(path(0), path(1), output); break;
      case Stage::kResolveCaptures: ResolveStaticExactCaptures(path(0), path(1), path(2), path(3), output); break;
      case Stage::kHashSemantics: HashStaticExactSemantics(path(0), path(1), path(2), output); break;
      case Stage::kWriteExactSections: WriteStaticExactSections(path(0), path(1), path(2), output); break;
      case Stage::kRefinePatron: RefinePatron(path(0), output, request.typed_input, request.summary_output, request.typed_output); break;
      case Stage::kRoute: RouteTlr(path(0), output, request.typed_output); break;
      case Stage::kSchedule: ScheduleStaticExact(path(0), output); break;
      case Stage::kPublishSchedule: core::PublishExactScheduleStore(path(0), path(1), path(2), path(3), output); break;
      case Stage::kAssignBanks: AssignChimewBankChannels(path(0), output, request.typed_input); break;
      case Stage::kGroupSignals: GroupChimewSignals(path(0), output, request.typed_input, request.expected_sha256); break;
      case Stage::kRefinePositions: RefineChimewPositions(path(0), output, request.typed_input, request.expected_sha256); break;
      case Stage::kRudy: EvaluateChimewRudy(path(0), output); break;
      default: throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
    }
    if (output == "-") {
      std::cout.flush();
      if (!std::cout) throw std::runtime_error("cannot flush native stage output");
    }
  }, [&]() {
    const char* schema = nullptr;
    switch (request.stage) {
      case Stage::kAnalyzeExact: schema = "exact-topology-v3"; break;
      case Stage::kFinalizeExact: schema = "exact-nodes-v2"; break;
      case Stage::kResolveCaptures: schema = "exact-captures-v1"; break;
      case Stage::kHashSemantics: schema = "exact-sha256-text-v1"; break;
      case Stage::kWriteExactSections: schema = "exact-section-summary-v2"; break;
      case Stage::kRefinePatron: schema = request.typed_output ? "patron-output-v1" : "patron-output-text"; break;
      case Stage::kRoute: schema = request.typed_output ? "tlr-output-v1" : "tlr-output-text"; break;
      case Stage::kSchedule: schema = "exact-schedule-output-v2"; break;
      case Stage::kPublishSchedule: schema = "tdm-schedule-store-v4"; break;
      case Stage::kAssignBanks: schema = "chimew-bank-output-text"; break;
      case Stage::kGroupSignals: schema = "chimew-group-output-text"; break;
      case Stage::kRefinePositions: schema = "chimew-refine-output-text"; break;
      case Stage::kRudy: schema = "chimew-rudy-output-text"; break;
      default: throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
    }
    std::vector<core::ArtifactRef> refs{core::ArtifactRef::Bind(request.output_path, schema, request.output_path == "-")};
    if (request.stage == Stage::kResolveCaptures)
      refs.push_back(core::ArtifactRef::Bind(request.output_path + ".sqlite", "exact-capture-index-private-v1"));
    return refs;
  });
}
}  // namespace emuflow::native
