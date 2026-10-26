#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
class PrimaryClusterView;
struct SelectedPartitionOwnership {
  std::vector<std::uint32_t> cluster_parts;
  std::vector<std::string> parts;
};
// Borrow original selected solver artifacts, with no host partition-column
// round trip. An empty owner set denotes a single constant partition.
SelectedPartitionOwnership ReadSelectedPartitionOwnership(
    const PrimaryClusterView& primary, const std::vector<ArtifactRef>& owners,
    const std::vector<std::string>& parts);
// Selected native ownership -> cuts, analyzer/captures and final semantic records.
// All graph projections and independent record checks stay inside this operation.
StageResult FinalizePartitionSemantics(const std::vector<ArtifactRef>& inputs,
                                      const std::string& directory);
}
