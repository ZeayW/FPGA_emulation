#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Original sources and bounded board policy. The complete stage owns all
// circuit-dependent request construction, algorithms, audits and publication.
StageResult RunPhase3(const ArtifactRef& request,const std::string& directory,
                     const volatile std::sig_atomic_t* cancelled=nullptr);
}
