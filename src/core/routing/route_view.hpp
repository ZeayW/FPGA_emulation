#pragma once
#include "storage/store.hpp"
#include "common/stage_runtime.hpp"
#include <functional>
#include <optional>

namespace emuflow::core {
struct RouteEdgeIdentity { std::string link, from, to; };
struct RouteIdentity {
  std::string demand, net, source;
  std::vector<std::string> sinks;
  std::vector<RouteEdgeIdentity> edges;
  std::optional<std::uint32_t> dependency_level, dependency_depth;
  std::vector<std::string> predecessors;
};
struct RouteCoordinate {
  RouteId source;
  std::uint64_t net_order;
};
// Ordered identity view over the existing public Phase 4 owner. No new graph
// catalog, copied JSON relation or per-route SQL query is constructed.
class RouteView {
 public:
  explicit RouteView(const ArtifactRef& artifact);
  std::uint64_t count() const { return count_; }
  void VisitNetOrder(const std::function<void(const RouteCoordinate&,const RouteIdentity&)>& visitor,
                     bool exact_dependencies = false);
 private:
  ArtifactRef artifact_;
  SqliteStore store_;
  SqliteDatabase& database_;
  std::uint64_t count_ = 0;
};
}  // namespace emuflow::core
