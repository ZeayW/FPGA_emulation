#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Ordered path projection and fixed-event/ratio timing. Paths remain in the
// source RouteStore; output contains only a sealed projection and summaries.
void ProjectScheduleTiming(const std::vector<ArtifactRef>& inputs,
                           const std::string& output);
}
