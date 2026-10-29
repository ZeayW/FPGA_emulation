#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>

namespace emuflow::core {
StageResult PrepareChimewPackageGeometry(
    const ArtifactRef &architecture, const std::string &part,
    const std::string &output,
    const volatile std::sig_atomic_t *cancelled = nullptr);
// Consume the selected schedule and original physical owners directly. Only
// board/device facts and a bounded producer report enter the request envelope.
StageResult
PrepareChimewGeometry(const ArtifactRef &request, const std::string &directory,
                      const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
