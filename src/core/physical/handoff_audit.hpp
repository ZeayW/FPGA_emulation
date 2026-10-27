#pragma once
#include "physical/device.hpp"
namespace emuflow::core {
// Bounded document result; complete ownership, placement and connectivity are
// independently reconstructed from original typed sources and device facts.
std::string
AuditPhysicalHandoff(const ArtifactRef &mapped, const ArtifactRef &handoff,
                     PhysicalDevice &device,
                     const ArtifactRef *constraints = nullptr,
                     const volatile std::sig_atomic_t *cancelled = nullptr);
std::string ValidatePhysicalHandoffStore(
    const ArtifactRef &handoff, bool full_integrity = false,
    const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
RunPhysicalHandoffAudit(const std::vector<ArtifactRef> &inputs,
                        const std::string &summary,
                        const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult RunPhysicalHandoffStructure(
    const std::vector<ArtifactRef> &inputs, const std::string &summary,
    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
