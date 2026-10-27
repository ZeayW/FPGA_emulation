#pragma once
#include "emuflow_core/word_physical_binding.hpp"
#include "emuflow_core/original_timing.hpp"
#include "emuflow_core/optimization_proofs.hpp"
#include <variant>

namespace emuflow::core {
struct LocalCaptureCostTag {};
struct OriginalPhysicalPartition {
  ArtifactRef logic_costs,local_costs,boundary_identity;
  NativeOptimizationProofView* optimization=nullptr;
};
struct OriginalCaptureCost { double data_delay_ns,sampling_cost_ns;std::string measurement; };
struct OriginalPhysicalCapture {
  std::uint32_t partition;
  bool crosses;
  // Cross captures start at this canonical transfer's RX shadow event. The
  // fixed word arc already includes RX -> shadow; it is never added again.
  std::optional<std::uint32_t> transfer;
  std::variant<ArtifactId<LocalCaptureCostTag>,ArtifactId<WordLogicCostTag>> cost_origin;
  std::optional<OriginalCaptureCost> cost;
  std::optional<EliminatedSourceFact> eliminated;
};
struct OriginalCaptureCoverage {
  std::uint64_t original=0,local=0,cross=0,optimized_away=0;
  std::vector<ArtifactRef> inputs;
  void RequireUnchanged() const;
};
// Bind every original member exactly once to its implemented local/capture
// row or a checked source-bound elimination. Parent physical identity/cost
// owners have already passed their publication/assembly checks. This consumer
// verifies original endpoints and the last canonical RX, reusing the word
// binding's semantic view; it does not re-import the complete capture graph.
// The visitor is invocation-scoped. No numerical values are made for removal.
OriginalCaptureCoverage VisitOriginalPhysicalCaptures(OriginalTimingView& original,
    const ArtifactRef& original_source,const BoundPhysicalWordModel& words,
    const std::string& assignment_sha256,const std::vector<OriginalPhysicalPartition>& partitions,
    const std::function<void(const OriginalTimingPath&,const OriginalPhysicalCapture&)>& visitor);

struct SemanticCaptureCoverage {
  std::uint64_t cross_capture_endpoints=0,measured=0,optimized_away=0;
  std::vector<ArtifactRef> inputs;
  void RequireUnchanged() const;
};
// Stream the complete Phase6 cross-capture population, independently of the
// selected original timing paths. Each endpoint owns its routed D/E costs and
// the final canonical RX event, or an explicit checked removal. This is the
// physical input to the global timer, not an engine result or a local-path
// coverage certificate. No new path selection or partitioning is performed.
SemanticCaptureCoverage VisitSemanticPhysicalCaptures(const ArtifactRef& source,
    const BoundPhysicalWordModel& words,const std::vector<WordPhysicalPartition>& partitions,
    const std::function<void(const ExactCaptureEndpoint&,const OriginalPhysicalCapture&)>& visitor);
}  // namespace emuflow::core
