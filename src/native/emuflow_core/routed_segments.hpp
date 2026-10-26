#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// One graph lifetime, native grouped queries and direct typed timing owners.
// The directory is private to this invocation; selectors are published by the
// consumer only after every requested projection succeeds.
void BuildRoutedSegments(const std::vector<ArtifactRef>& inputs,
                         const std::string& directory,const std::string& summary);
}
