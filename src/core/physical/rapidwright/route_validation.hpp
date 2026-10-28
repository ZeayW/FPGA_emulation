#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
ValidateRouteCertificate(const ArtifactRef &request, const std::string &summary,
                         const volatile std::sig_atomic_t *cancelled = nullptr);
}
