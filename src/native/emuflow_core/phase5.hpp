#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <functional>

namespace emuflow::core {
// Complete exact Phase 5 transaction. Bounded board/header/source requests enter;
// the core owns scheduling, independent validation, timing and report-last commit.
using Phase5Progress = std::function<void(const std::string&)>;
void RunPhase5(const std::vector<ArtifactRef>& inputs, const std::string& report,
               const Phase5Progress& progress = {});
}
