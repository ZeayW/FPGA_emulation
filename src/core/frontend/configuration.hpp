#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Finite source configuration for RTL, supplied JSON and prepared boundaries.
// The same producer is used by native preflight and optional language hosts.
StageResult PrepareFrontendConfiguration(
    const ArtifactRef &request, const std::string &directory,
    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
