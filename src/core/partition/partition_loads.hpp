#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
// Independently reduce immutable primary resource columns by final ownership.
// The result contains only partition-sized counts; no solver decisions or
// precomputed solver loads are accepted as evidence.
void ReducePartitionLoads(const std::vector<ArtifactRef>& inputs,
                          const std::string& output);
}
