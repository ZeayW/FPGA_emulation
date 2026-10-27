#pragma once
#include "common/artifact_io.hpp"
namespace emuflow::core {
void ExportVivado(const std::vector<ArtifactRef>& inputs,const std::string& rtl,
                  const std::string& functions,const std::string& summary);
}
