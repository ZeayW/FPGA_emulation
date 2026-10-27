#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Whole maintained RTL frontend: engine lifetime, streamed import, independent
// source audit, clock/resource checks and atomic stage-directory publication.
StageResult RunFrontend(const ArtifactRef& request,
                        const volatile std::sig_atomic_t* cancelled=nullptr,
                        const std::string& expected_output={});
}
