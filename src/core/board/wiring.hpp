#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
#include <optional>

namespace emuflow::core {
// Finite board configuration, independent of circuit/schedule population.
// The checked-in cable table is verified against vendor pair identities.
std::string
BuildVirtualBoard(const std::string &normalized_board,
                  const ArtifactRef &inventory,
                  const std::optional<ArtifactRef> &wiring,
                  const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult BuildVirtualBoardArtifact(
    const ArtifactRef &board, const ArtifactRef &inventory,
    const std::optional<ArtifactRef> &wiring, const std::string &output,
    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
