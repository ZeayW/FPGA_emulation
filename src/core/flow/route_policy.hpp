#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
PrepareRouting(const ArtifactRef &previous, const std::string &root,
               const std::string &next,
               const volatile std::sig_atomic_t *cancelled = nullptr);
}
