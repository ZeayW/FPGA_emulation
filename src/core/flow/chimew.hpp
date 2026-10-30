#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete baseline physical+STA, placed lookahead, independently qualified
// pin pipeline and candidate Phase 6. The common physical controller
// subsequently implements the candidate with the same policy and finalizes the
// comparison.
StageResult
CompileChimewPhase6(const ArtifactRef &state, const std::string &flow_directory,
                    const volatile std::sig_atomic_t *cancelled = nullptr);
ArtifactRef
CompleteChimewPhysicalComparisonInput(const std::string &flow_directory,
                                      double physical_seconds);
} // namespace emuflow::core
