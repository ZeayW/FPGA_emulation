#pragma once
#include "common/stage_runtime.hpp"
#include <vector>
namespace emuflow::core {
void BuildStaWeights(const std::vector<ArtifactRef>& inputs,
                     const std::string& store, const std::string& summary);
void ValidateStaWeights(const std::vector<ArtifactRef>& inputs,
                        const std::string& summary);
}
