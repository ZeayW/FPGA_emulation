#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete maintained OpenPARF/RWRoute partition, including independent routed
// timing projections, full-population OpenSTA, budgets and report acceptance.
StageResult
RunRapidwrightPartition(const ArtifactRef &request, const std::string &output,
                        const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
