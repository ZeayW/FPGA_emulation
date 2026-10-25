#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
void ImportVivadoLineage(const std::vector<ArtifactRef>& inputs,
                         const std::string& output, const std::string& summary);
}
