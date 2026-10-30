#pragma once

#include "common/stage_runtime.hpp"
#include <csignal>

namespace emuflow::core {
// Assignment, board, bounded execution policy and optional projected paths.
// The stage owns model construction, routing, independent result validation,
// the current RouteStore generation and its atomic directory publication.
StageResult RunPhase4(const std::vector<ArtifactRef>& inputs,
                      const std::string& directory,
                      const volatile std::sig_atomic_t* cancelled=nullptr);
// Reopen published typed records and reconstruct source, topology and timing
// legality without invoking the router or trusting its report.
StageResult ValidatePhase4(const std::vector<ArtifactRef>& inputs,
                           const std::string& summary,
                           const volatile std::sig_atomic_t* cancelled=nullptr);
}
