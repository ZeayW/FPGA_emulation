#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete maintained Vivado partition owner: inputs, vendor process lifecycle,
// lineage, full-population timing import, resource/closure checks and report.
StageResult
RunVivadoPartition(const ArtifactRef &request, const std::string &output,
                   const volatile std::sig_atomic_t *cancelled = nullptr,
                   std::vector<ArtifactRef> *accepted_artifacts = nullptr);
} // namespace emuflow::core
