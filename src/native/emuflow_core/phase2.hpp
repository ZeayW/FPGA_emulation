#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Own the complete pre-placement timing stage, including the tool lifecycle,
// independent path/weight audits and one atomic directory publication.
StageResult RunPhase2(const ArtifactRef& emuir,const ArtifactRef& policy,
                      const std::string& output,
                      const volatile std::sig_atomic_t* cancelled=nullptr);
}
