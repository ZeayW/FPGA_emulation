#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
// Compare existing authorities; singleton IDs are derived in mapped-name order.
// No ownership snapshot, cell-name map, or private mirror database is created.
void ValidateAtomicOwnership(const std::vector<ArtifactRef>& inputs,
                             const std::string& summary);
}  // namespace emuflow::core
