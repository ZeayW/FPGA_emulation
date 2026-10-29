#pragma once

#include "common/stage_runtime.hpp"

namespace emuflow::native {
enum class Stage {
  kInvalid, kImportPhysicalBoundary, kImportPhysicalLogic, kImportPhysicalLocal, kValidatePhysicalEvidence, kSealRoutes, kValidatePhysicalCells, kValidateAtomicOwnership,
  kAnalyzeExact, kFinalizeExact, kHashSemantics, kAuditPartitionDecision, kNormalizePartitionConfiguration,
  kWriteExactSections, kValidateExactBinary, kRoute,
  kGroupSignals, kRefinePositions, kRudy,
  kCompilePhase5, kRunPhase5, kBuildSchedule, kValidateSchedule, kProjectScheduleTiming, kBindGlobalEvents, kIndexGlobalEvents, kExportGlobalEvents, kImportGlobalMeasurements, kAuditGlobalMeasurements, kPublishGlobalQor, kValidateGlobalQor, kExportBookshelf, kBuildPartitionClusters, kBuildCandidateIds, kRefinePatronFromPrimary, kReducePartitionLoads, kPublishAssignmentIndex, kPublishAssignmentDescriptor, kValidateAssignmentCuts, kPublishClusterCatalog, kBuildCaptures, kValidateCaptures, kExportRwroute, kImportVivadoLineage, kExportVivado, kBuildRoutedSegments, kBuildRoutedEndpoints, kStageRoutedOpensta, kExportOpenstaInputs, kImportStaPaths, kValidateStaPaths, kBuildStaWeights, kValidateStaWeights, kBuildPhysicalCoverage, kAuditPhysicalCoverage
};
struct StageRequest {
  Stage stage = Stage::kInvalid;
  std::vector<std::string> input_paths;
  std::string output_path;
  bool indexed_input = false;
  bool typed_input = true;
  std::string expected_sha256;
  std::string report_path;
  // Native orchestration connects producer references directly, retaining
  // their expected generation. Paths are the separate CLI/import boundary.
  std::vector<core::ArtifactRef> input_artifacts;
  // Physical export publishes net text then its bounded summary. The scratch
  // relation is owned by that invocation and is never a downstream artifact.
  std::string auxiliary_output_path;
  std::string scratch_path;
};

// Typed in-process interface for native orchestration. CLI argument parsing is
// outside the stage library; no Python/FFI graph is involved.
core::StageResult ExecuteStage(const StageRequest& request);
const char* StageName(Stage stage);
}  // namespace emuflow::native
