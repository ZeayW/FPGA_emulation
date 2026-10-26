#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Original connectivity, assignment and complete scheduled route trees own
// coverage. TimingPathDB is deliberately not an input. Costs are the explicit
// per-hop fixed+UI*ratio planning bound, not post-schedule physical timing.
void ExportSourceCommunication(const std::vector<ArtifactRef>& inputs,
                               const std::string& output);
}
