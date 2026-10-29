#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Independently reconstruct structural timing reachability, original-path/cut
// associations and exact identities from immutable source stores.
StageResult
QualifyCutSegments(const ArtifactRef &request, const std::string &output,
                   const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
