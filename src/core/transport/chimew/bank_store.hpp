#pragma once
#include "common/stage_runtime.hpp"
#include "transport/chimew/kernels.hpp"
#include <csignal>
namespace emuflow::core {
// Independent primal, eligibility and residual-dual validation. This never
// calls the matching optimizer or trusts its edge-cost calculations.
void ValidateChimewBankSolution(
    const emuflow::native::ChimewBankInput &input,
    const emuflow::native::ChimewBankResult &result,
    const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
AssignChimewBankStore(const ArtifactRef &selector, const std::string &output,
                      const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
CopyChimewBankReport(const ArtifactRef &selector, const std::string &output,
                     const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
