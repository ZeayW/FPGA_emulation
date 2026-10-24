#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Publish the current ownership/cut store from source-bound primary columns.
// Circuit instance records never cross the host request boundary.
void PublishAssignmentIndex(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
}
