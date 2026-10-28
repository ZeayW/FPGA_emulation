#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
QualifyOpenparf(const ArtifactRef &request, const std::string &output,
                const volatile std::sig_atomic_t *cancelled = nullptr);
}
