#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
std::vector<std::string> OriginalEndpointInputSchemas(const std::string& request);
// Complete original-member source coordinates and mapped copy ownership.
// The caller owns Phase6 source/function admission, including any normalization
// bridge. This index does not require old cell names/types to survive Vivado,
// infer removal from missing tags, or supply numerical physical timing.
void BuildOriginalEndpointIndex(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
