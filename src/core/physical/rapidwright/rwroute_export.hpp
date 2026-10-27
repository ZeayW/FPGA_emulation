#pragma once

#include "common/stage_runtime.hpp"

namespace emuflow::core {
// The mapped and physical stores own all circuit-sized populations. The
// versioned request contains only source identities, resource limits and I/O.
void ExportRwroute(const ArtifactRef& mapped, const ArtifactRef& physical,
                   const ArtifactRef& request, const std::string& output,
                   const std::string& summary);
}  // namespace emuflow::core
