#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
void BuildRoutedEndpoints(const std::vector<ArtifactRef>& inputs,
                         const std::string& endpoints,const std::string& manifest,
                         const std::string& summary);
}
