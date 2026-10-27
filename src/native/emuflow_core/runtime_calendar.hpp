#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <string>
namespace emuflow::core {
// Decimal root-clock arithmetic and typed schedule cursors; no hop JSON scan.
std::string BuildVirtualRuntime(const ArtifactRef& schedule,const ArtifactRef& board,
                               const std::string& clock_periods);
}
