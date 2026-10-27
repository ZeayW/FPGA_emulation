#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
// Independent population/pin checks over the existing mapped and physical
// owners. Geometry and the bounded FF/MUX/CARRY semantic checks remain separate.
void ValidatePhysicalCells(const ArtifactRef& mapped, const ArtifactRef& physical,
                           const ArtifactRef& request, const std::string& summary);
}  // namespace emuflow::core
