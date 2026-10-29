#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete supplied-JSON import or immutable prepared frontend/timing adoption.
StageResult
RunFrontendInputs(const ArtifactRef &request, const std::string &output,
                  const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
