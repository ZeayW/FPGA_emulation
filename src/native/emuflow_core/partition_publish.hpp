#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Selected source-bound records -> one independently checked final directory.
// The bounded request contains headers/configuration, never ownership or hops.
StageResult PublishPartition(const std::vector<ArtifactRef>& inputs,
                             const std::string& directory);
}
