#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// The producer-owned v3 relation file stays immutable during this operation.
// Only a bounded logical/section seal is returned; no route JSON is exported.
void SealSystemRoutes(const std::vector<ArtifactRef>& inputs,
                      const std::string& output);
}
