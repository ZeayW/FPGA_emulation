#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
PrepareChimewSignals(const ArtifactRef &request, const std::string &directory,
                     const volatile std::sig_atomic_t *cancelled = nullptr);
}
