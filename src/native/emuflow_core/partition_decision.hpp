#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Original source/primary/solver owners -> independent legality and bounded
// decision evidence. No ownership column or source identity catalog is supplied
// by the caller, and no schedule feasibility participates in candidate choice.
void AuditPartitionDecision(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
// The same original ownership handoff for consumers needing only resource
// facts. This is independent of cached optimizer resource totals.
void ReduceOwnedPartitionLoads(const std::vector<ArtifactRef>& inputs,
                               const std::string& output);
}
