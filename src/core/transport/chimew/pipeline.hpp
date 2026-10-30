#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
RunChimewPipeline(const ArtifactRef &request, const std::string &directory,
                  const volatile std::sig_atomic_t *cancelled = nullptr);
StageResult
AuditChimewPipeline(const ArtifactRef &request, const std::string &directory,
                    const std::string &summary,
                    const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
