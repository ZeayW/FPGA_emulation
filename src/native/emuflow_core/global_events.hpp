#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Canonical event facts and the derived numerical bundle index have different
// owners. The index contains offsets/identities, never another arc population.
void IndexGlobalEvents(const ArtifactRef& events,const std::string& output);
void ExportGlobalEvents(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
