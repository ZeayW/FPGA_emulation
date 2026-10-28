#pragma once
#include "common/stage_runtime.hpp"
#include <filesystem>
namespace emuflow::core {
void FinalizePhase6Comparison(const ArtifactRef &input,
                              const std::filesystem::path &flow_root);
}
