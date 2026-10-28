#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Indexed source ports and sealed fixed I/O -> one complete tool-input
// generation.
StageResult
PrepareVivadoInputs(const ArtifactRef &request, const std::string &output,
                    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
