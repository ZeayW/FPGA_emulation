#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Source-owned partitioning of every original path. The shared index retains
// only source path ordinals, scope and FPGA identity, never path payload copies.
StageResult PrepareLocalPathQueries(const std::vector<ArtifactRef>& inputs,
    const std::string& output,const volatile std::sig_atomic_t* cancelled=nullptr);
// Join original paths through that source-bound index and publish the complete
// query, typed identity, independent audit and small report in one directory.
StageResult WriteLocalPathQueries(const std::vector<ArtifactRef>& inputs,
    const std::string& output,const volatile std::sig_atomic_t* cancelled=nullptr);
}
