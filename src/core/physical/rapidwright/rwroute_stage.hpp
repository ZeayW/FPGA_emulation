#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Owns typed export, pinned vendor data, Java process supervision, independent
// route validation and report-last publication. No Python stage callbacks.
StageResult RunRwroute(const ArtifactRef &request, const std::string &summary,
                       const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
