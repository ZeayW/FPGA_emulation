#pragma once
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <functional>

namespace emuflow::core {
struct RouteEdgeIdentity { std::string link, from, to; };
struct RouteIdentity {
  std::string demand, net, source;
  std::vector<std::string> sinks;
  std::vector<RouteEdgeIdentity> edges;
};
// Ordered identity view over the existing public Phase 4 owner. No new graph
// catalog, copied JSON relation or per-route SQL query is constructed.
class RouteView {
 public:
  explicit RouteView(const ArtifactRef& artifact);
  std::uint64_t count() const { return count_; }
  void VisitNetOrder(const std::function<void(std::uint64_t,const RouteIdentity&)>& visitor);
 private:
  ArtifactRef artifact_;
  SqliteDatabase database_;
  std::uint64_t count_ = 0;
};
}  // namespace emuflow::core
