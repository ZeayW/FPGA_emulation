#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
BuildChimewPinArtifacts(const ArtifactRef &request,
                        const std::string &directory,
                        const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult ValidateChimewPinArtifacts(
    const ArtifactRef &request, const std::string &summary,
    const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
AuditChimewElectricalMap(const ArtifactRef &request, const std::string &summary,
                         const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
CopyPinArtifact(const ArtifactRef &selector, const std::string &schema,
                const std::string &target,
                const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
