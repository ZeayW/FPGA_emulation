#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
RunRoutedOpensta(const ArtifactRef &request, const std::string &report,
                 const volatile std::sig_atomic_t *cancelled = nullptr);
}
