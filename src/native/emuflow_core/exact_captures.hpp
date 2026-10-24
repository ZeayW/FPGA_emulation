#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Production builds and independently validates before atomic publication.
void BuildExactCaptures(const std::vector<ArtifactRef>& inputs,const std::string& output);
// Explicit independent checker for an existing v1 capture stream.
void ValidateExactCaptures(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
