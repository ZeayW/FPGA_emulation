#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Original connectivity, assignment and complete scheduled route trees own
// coverage. TimingPathDB is deliberately not an input. Costs are the explicit
// per-hop fixed+UI*ratio planning bound, not post-schedule physical timing.
void ExportSourceCommunication(const std::vector<ArtifactRef>& inputs,
                               const std::string& output);
// Phase-3 candidate ownership and the native PATRON transport model own these
// estimates. No TimingPathDB or concrete Phase-5 schedule is an input.
void ExportPatronSourceProxy(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
// OpenSTA can canonicalize an input-connected wire to its top-level port
// spelling. Retain those aliases alongside generated internal wire names.
void ExportSourceNetMap(const std::vector<ArtifactRef>& inputs,
                        const std::string& output);
void MergePatronWitnessPaths(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
}
