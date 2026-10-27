#include "routing/route_view.hpp"
#include <limits>

namespace emuflow::core {
namespace {
const ArtifactRef& Checked(const ArtifactRef& artifact) {
  if (artifact.schema() != "system-route-store-v3" || artifact.is_pipe())
    throw StageError(ErrorCode::kInvalidArtifact,"native RouteView requires typed system-route v3 storage");
  artifact.RequireUnchanged();
  return artifact;
}
void Require(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
}  // namespace
RouteView::RouteView(const ArtifactRef& artifact)
    : artifact_(artifact),store_(Checked(artifact)),database_(store_.database()) {
  auto count = database_.Prepare("SELECT json_extract(value,'$.routes.records'),json_type(value,'$.routes.records') FROM metadata WHERE key='sections'");
  Require(count.Step() &&
          count.Text(1) == "integer" && count.Integer(0) >= 0,"native RouteView metadata is invalid");
  count_ = static_cast<std::uint64_t>(count.Integer(0));
  database_.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-65536;");
  artifact_.RequireUnchanged();
}
void RouteView::VisitNetOrder(const std::function<void(const RouteCoordinate&,const RouteIdentity&)>& visitor, bool exact_dependencies) {
  artifact_.RequireUnchanged();
  auto routes = database_.Prepare("SELECT seq,demand_id,net_id,source_fpga,typeof(seq)='integer',dependency_level,combinational_dependency_depth,cut_class, "
                                 "typeof(dependency_level),typeof(combinational_dependency_depth) "
                                 "FROM routes ORDER BY net_id,seq");
  auto sinks = database_.Prepare("SELECT r.net_id,r.seq,s.seq,s.sink_fpga,typeof(s.seq)='integer' "
      "FROM route_sinks s LEFT JOIN routes r ON s.route_seq=r.seq ORDER BY r.net_id,r.seq,s.seq");
  auto edges = database_.Prepare("SELECT r.net_id,r.seq,e.seq,e.link_id,e.source_fpga,e.sink_fpga,typeof(e.seq)='integer' "
      "FROM route_edges e LEFT JOIN routes r ON e.route_seq=r.seq ORDER BY r.net_id,r.seq,e.seq");
  auto predecessors = exact_dependencies ? database_.Prepare(
      "SELECT r.net_id,r.seq,p.seq,p.predecessor_net,typeof(p.seq)='integer' "
      "FROM route_predecessors p LEFT JOIN routes r ON p.route_seq=r.seq ORDER BY r.net_id,r.seq,p.seq")
      : SqliteStatement{};
  bool have_predecessor = exact_dependencies && predecessors.Step();
  bool have_sink = sinks.Step(), have_edge = edges.Step();
  std::uint64_t observed = 0;
  std::string previous_net;
  while (routes.Step()) {
    const auto seq = routes.Integer(0);
    RouteIdentity route{routes.Text(1),routes.Text(2),routes.Text(3),{}, {}};
    Require(observed < count_ && routes.Integer(4) && seq >= 0 && static_cast<std::uint64_t>(seq) < count_ &&
            !route.demand.empty() && !route.net.empty() && !route.source.empty() &&
            (observed == 0 || previous_net < route.net),"native RouteView identity/order is invalid");
    while (have_sink && sinks.Text(0) == route.net && sinks.Integer(1) == seq) {
      Require(sinks.Integer(4) && sinks.Integer(2) == static_cast<std::int64_t>(route.sinks.size()) && !sinks.Text(3).empty(),
              "native RouteView sink sequence is invalid");
      route.sinks.push_back(sinks.Text(3)); have_sink = sinks.Step();
    }
    while (have_edge && edges.Text(0) == route.net && edges.Integer(1) == seq) {
      Require(edges.Integer(6) && edges.Integer(2) == static_cast<std::int64_t>(route.edges.size()) &&
              !edges.Text(3).empty() && !edges.Text(4).empty() && !edges.Text(5).empty(),
              "native RouteView edge sequence is invalid");
      route.edges.push_back({edges.Text(3),edges.Text(4),edges.Text(5)}); have_edge = edges.Step();
    }
    if (exact_dependencies) {
      const auto level = routes.Integer(5), depth = routes.Integer(6);
      Require(routes.Text(8) == "integer" && routes.Text(9) == "integer" && level >= 0 && depth >= 0 &&
              level <= std::numeric_limits<std::uint32_t>::max() && depth <= std::numeric_limits<std::uint32_t>::max() &&
              (routes.Text(7) == "combinational" || routes.Text(7) == "register_input" || routes.Text(7) == "register_output"),
              "native RouteView exact dependency metadata is invalid");
      route.dependency_level = static_cast<std::uint32_t>(level);
      route.dependency_depth = static_cast<std::uint32_t>(depth);
      while (have_predecessor && predecessors.Text(0) == route.net && predecessors.Integer(1) == seq) {
        Require(predecessors.Integer(4) && predecessors.Integer(2) == static_cast<std::int64_t>(route.predecessors.size()) &&
                !predecessors.Text(3).empty(),"native RouteView predecessor sequence is invalid");
        route.predecessors.push_back(predecessors.Text(3)); have_predecessor = predecessors.Step();
      }
    }
    visitor({store_.Id<RouteTag>(seq),observed++},route); previous_net = route.net;
  }
  Require(observed == count_ && !have_sink && !have_edge && !have_predecessor,"native RouteView record coverage disagrees");
  artifact_.RequireUnchanged();
}
}  // namespace emuflow::core
