#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Prove post-map hierarchy port resizing against the bound primitive source.
// No logic optimization, renamed cells or changed parameters are accepted.
void ValidateMappedNormalization(const std::vector<ArtifactRef>& inputs,
                                 const std::string& output);
}
