#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
StageResult
BuildMappedIndex(const std::vector<ArtifactRef> &inputs,
                 const std::string &output, const std::string &summary,
                 const volatile std::sig_atomic_t *cancelled = nullptr);
}
