#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
// Independent read-only validation of the published v4 cut relation. This
// operation never calls the assignment producer or exports per-cut records.
void ValidateAssignmentCuts(const ArtifactRef& input, const std::string& output);
}
