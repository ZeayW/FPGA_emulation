#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
#include <optional>
namespace emuflow::core {
std::string
QualifyOpenstaEngine(const ArtifactRef &executable, const ArtifactRef &launcher,
                     const ArtifactRef &runtime_check,
                     const std::optional<ArtifactRef> &container_binary,
                     const volatile std::sig_atomic_t *cancelled = nullptr);
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
