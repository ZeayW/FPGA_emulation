#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Original typed paths -> exact ordered cuts -> lossless route representatives.
// The original paths and assignment are borrowed immutable owners.
StageResult
ProjectStaPaths(const std::vector<ArtifactRef> &inputs,
                const std::string &output, const std::string &summary,
                const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
