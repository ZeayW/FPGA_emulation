#pragma once
#include "common/stage_runtime.hpp"
#include <optional>
namespace emuflow::core {
// Board-level facts only. No circuit route/path population is materialized.
std::string NormalizeRoutePolicy(const std::string &board,
                                 const std::string &raw,
                                 std::optional<std::int64_t> frame = {},
                                 std::optional<std::int64_t> iterations = {});
struct PartitionRoutingPolicy {
  std::string constraints, domains, shortest_routes, hop_distances;
};
PartitionRoutingPolicy BuildPartitionRoutingPolicy(const ArtifactRef &board,
                                                   const std::string &raw);
} // namespace emuflow::core
