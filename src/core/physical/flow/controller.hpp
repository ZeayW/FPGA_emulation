#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
#include <utility>
namespace emuflow::core {
// Own the complete maintained multi-FPGA physical transaction from current
// Phase 6 owners. Circuit facts stay in typed sources throughout its workers.
StageResult
RunPhysicalFlow(const ArtifactRef &request, const std::string &output,
                const volatile std::sig_atomic_t *cancelled = nullptr);
ArtifactRef PreparePhysicalFlowCompileRequest(
    const ArtifactRef &policy,
    const std::vector<std::pair<std::string, ArtifactRef>> &dynamic,
    const std::string &output);
} // namespace emuflow::core
