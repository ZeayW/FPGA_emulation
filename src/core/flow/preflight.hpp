#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>

namespace emuflow::core {
// Admit the complete bounded configuration before starting circuit engines.
// Produces all static stage requests and the source-bound control state.
StageResult
PrepareCompile(const ArtifactRef &request, const ArtifactRef &native,
               const std::string &run_id, const std::string &state,
               const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
