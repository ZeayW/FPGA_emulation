#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
PreparePhase2Policy(const ArtifactRef &request, const std::string &output,
                    const volatile std::sig_atomic_t *cancelled = nullptr,
                    const ArtifactRef *compiled_emuir = nullptr);
// Own the complete pre-placement timing stage, including the tool lifecycle,
// independent path/weight audits and one atomic directory publication.
StageResult RunPhase2(const ArtifactRef &emuir, const ArtifactRef &policy,
                      const std::string &output,
                      const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
