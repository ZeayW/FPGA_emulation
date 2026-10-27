#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
// Publish the current ownership/cut store from source-bound primary columns.
// Circuit instance records never cross the host request boundary.
void PublishAssignmentIndex(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
// Publish the existing packed JSON selector directly from the authoritative
// assignment index and primary columns. The request contains only its header.
void PublishAssignmentDescriptor(const std::vector<ArtifactRef>& inputs,
                                 const std::string& output);
}
