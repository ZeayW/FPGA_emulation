#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <string>

namespace emuflow::core {
// Whole native producer: RouteStore + bounded board/header requests -> store.
// Model, solution, certificate and record publication stay in one C++ process.
void BuildExactScheduleStore(const std::vector<ArtifactRef>& inputs,
                            const std::string& output);
}  // namespace emuflow::core
