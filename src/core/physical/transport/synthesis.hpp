#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Supervise the maintained generic LUT6/FF synthesis, streamed source import,
// native writer-owned buffer tree and publication as one checked generation.
StageResult
SynthesizeTransport(const ArtifactRef &request, const std::string &output,
                    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
