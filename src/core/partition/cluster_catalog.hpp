#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
// Final current cluster records from source-bound primary columns. Only policy
// and sparse constraint coordinates enter the request; no host cluster tree.
void PublishClusterCatalog(const std::vector<ArtifactRef>& inputs,
                           const std::string& output);
}
