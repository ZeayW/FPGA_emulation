#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete two-backend physical logic-query owner. All graph/path/semantic
// records remain in typed sources or private ordered SQL relations.
StageResult RunLogicSegmentQuery(const std::vector<ArtifactRef>& inputs,
    const std::string& output_directory,
    const volatile std::sig_atomic_t* cancelled=nullptr);
}
