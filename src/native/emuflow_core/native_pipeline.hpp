#pragma once

#include "emuflow_core/stage_runtime.hpp"
#include <csignal>

namespace emuflow::native {
enum class Stage {
  kInvalid, kExportSourceCommunication, kBuildSourceLaunchClocks, kBuildSourceCaptureClocks, kSealRoutes, kValidatePhysicalCells, kValidateAtomicOwnership,
  kAnalyzeExact, kFinalizeExact, kHashSemantics,
  kWriteExactSections, kRefinePatron, kRoute,
  kAssignBanks, kGroupSignals, kRefinePositions, kRudy,
  kBuildSchedule, kExportBookshelf, kBuildPartitionClusters, kBuildCandidateIds, kRefinePatronFromPrimary, kReducePartitionLoads, kPublishAssignmentIndex, kValidateAssignmentCuts, kPublishClusterCatalog, kBuildCaptures, kValidateCaptures, kExportRwroute, kImportVivadoLineage, kRefinePartitionHops,
  kExportSourceMetricWeights, kExportPatronSourceProxy, kExportSourceNetMap, kMergePatronWitnessPaths, kValidateMappedNormalization,
  kBuildSourceLutModels, kAnalyzeNativeWordNetwork, kExportNativeWordChecks, kExportNativeWordArrivals, kIndexOriginalEndpoints, kIndexOriginalLineage
};
struct StageRequest {
  Stage stage = Stage::kInvalid;
  std::vector<std::string> input_paths;
  std::string output_path;
  bool indexed_input = false;
  bool typed_input = true;
  bool typed_output = true;
  bool summary_output = true;
  std::string expected_sha256;
  std::string report_path;
  // Native orchestration connects producer references directly, retaining
  // their expected generation. Paths are the separate CLI/import boundary.
  std::vector<core::ArtifactRef> input_artifacts;
  // Physical export publishes net text then its bounded summary. The scratch
  // relation is owned by that invocation and is never a downstream artifact.
  std::string auxiliary_output_path;
  std::string scratch_path;
  const volatile std::sig_atomic_t* cancelled = nullptr;
};

// Typed in-process interface for native orchestration. CLI argument parsing is
// outside the stage library; no Python/FFI graph is involved.
core::StageResult ExecuteStage(const StageRequest& request);
const char* StageName(Stage stage);
}  // namespace emuflow::native
