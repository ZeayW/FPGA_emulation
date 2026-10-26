#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete TritonPart initialization: source/primary/weight owners -> streamed
// hMetis inputs -> external engine -> independently checked selected column.
StageResult InitializePartition(const std::vector<ArtifactRef>& inputs,
    const std::string& output_directory,
    const volatile std::sig_atomic_t* cancelled=nullptr);
}
