#pragma once
#include "common/stage_runtime.hpp"
#include <string>
namespace emuflow::core {
// Finite board configuration only, independent of the synthesized circuit.
// Validate and normalize capacities, topology, fixed cable inventory, serial
// package bindings and clock/reset services through one shared authority.
std::string NormalizeBoard(const std::string &source);
StageResult NormalizeBoardArtifact(const ArtifactRef &source,
                                   const std::string &output);
} // namespace emuflow::core
