#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Canonical primary cluster owner. Inputs bind compact EmuIR and integer
// eligibility/group columns; output is a sealed, read-only column view.
void BuildPartitionClusters(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
}
