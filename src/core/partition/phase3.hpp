#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
#include <map>
namespace emuflow::core {
StageResult PreparePhase3Policy(const ArtifactRef& request,const std::string& directory,
    const volatile std::sig_atomic_t* cancelled=nullptr,
    const std::map<std::string,ArtifactRef>& compiled_sources={});
// Original sources and bounded board policy. The complete stage owns all
// circuit-dependent request construction, algorithms, audits and publication.
StageResult RunPhase3(const ArtifactRef& request,const std::string& directory,
                     const volatile std::sig_atomic_t* cancelled=nullptr);
}
