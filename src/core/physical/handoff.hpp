#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Complete certificate -> physical owner. The CRC catalog binds all original
// typed owners/device documents; only bounded configuration enters the host.
StageResult
BuildPhysicalHandoff(const ArtifactRef &request_ref,
                     const std::string &output_directory,
                     const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
