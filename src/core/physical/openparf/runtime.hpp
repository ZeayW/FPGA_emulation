#pragma once
#include "common/json_record.hpp"
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// The PyTorch host emits convergence evidence; acceptance belongs to the core.
JsonRecord ValidateOpenparfMetrics(const ArtifactRef &metrics, bool detailed);
StageResult
RunOpenparfStage(const ArtifactRef &request, const std::string &report,
                 const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
