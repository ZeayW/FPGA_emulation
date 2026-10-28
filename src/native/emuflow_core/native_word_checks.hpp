#pragma once
#include "emuflow_core/native_word_network.hpp"
#include <ostream>
namespace emuflow::core {
void NativeWordCheckHeader(std::ostream& stream);
// Reify every causal contribution at a window's worst continuous-phase
// witness. Independent engine checks cover readiness, rejection of the prior
// scheduled occurrence, declared link latency and all capture bounds. This
// validates the witness; exhaustive phase maximization remains the kernel's
// responsibility, and original-path dispositions are separate.
std::uint64_t WriteNativeWordWindowChecks(std::ostream& stream,std::uint64_t window,
    const NativeWordWindowProjection& projection,
    const std::vector<NativeWordTransferOwner>& owners,
    const std::vector<WordPhaseTransfer>& graph,std::int64_t word_period_ps);
}
