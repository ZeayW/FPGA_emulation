#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// One source-bound lowering generation: independent compact PlacementIR,
// typed boundary identity, original-input validation and atomic publication.
StageResult LowerPlacement(const std::vector<ArtifactRef>& inputs,
    const std::string& directory,const volatile std::sig_atomic_t* cancelled=nullptr);
}
