#pragma once
#include "common/stage_runtime.hpp"
#include <optional>
#include <string>
namespace emuflow::core {
struct PhysicalBoardTiming {
  std::string event_links;
  std::string coverage_links;
  bool final_signoff = false;
};
PhysicalBoardTiming
ResolvePhysicalBoardTiming(const ArtifactRef &board,
                           const std::optional<ArtifactRef> &measured = {});
} // namespace emuflow::core
