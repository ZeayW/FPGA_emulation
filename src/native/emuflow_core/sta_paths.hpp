#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <vector>
namespace emuflow::core {
void ImportStaPaths(const std::vector<ArtifactRef>& inputs,
                    const std::string& store, const std::string& summary);
void ValidateStaPaths(const std::vector<ArtifactRef>& inputs,
                      const std::string& summary);
}
