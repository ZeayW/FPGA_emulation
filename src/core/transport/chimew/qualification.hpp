#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
#include <functional>

namespace emuflow::core {
// Bounded proof for one typed bank input/report pair. Group records are visited
// under both original artifact leases; no graph crosses the stage boundary.
struct ChimewBankValidation {
  std::string design, platform, input_sha256, report_sha256;
  std::string provenance, metrics;
  std::vector<ArtifactRef> inputs;
};
ChimewBankValidation
InspectChimewBankInput(const ArtifactRef &input,
                       const volatile std::sig_atomic_t *cancelled = nullptr);
using ChimewBankGroupVisitor =
    std::function<void(std::size_t, const std::string &)>;
ChimewBankValidation ValidateChimewBankAssignment(
    const ArtifactRef &input, const ArtifactRef &report,
    const ChimewBankGroupVisitor &visit_group = {},
    const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult AuditChimewBankAssignment(
    const ArtifactRef &input, const ArtifactRef &report,
    const std::string &summary,
    const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
BuildChimewKernelReports(const ArtifactRef &request, const std::string &output,
                         const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
QualifyChimewChain(const ArtifactRef &request, const std::string &output,
                   const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
