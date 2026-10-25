#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/route_view.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <numeric>
#include <tuple>

namespace emuflow::core {
namespace {
void Require(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::vector<std::string> ReadCatalog(BinaryReader& request) {
  const auto count = request.Read<std::uint32_t>();
  Require(count <= request.remaining() / 5,"schedule board catalog is truncated");
  std::vector<std::string> result;
  for (std::uint32_t i = 0; i < count; ++i) {
    auto name = request.ReadString32();
    Require(i == 0 || name > result.back(),"schedule board catalog is not ordered and unique");
    result.push_back(std::move(name));
  }
  return result;
}
std::uint32_t Index(const std::vector<std::string>& catalog, const std::string& value) {
  auto found = std::lower_bound(catalog.begin(),catalog.end(),value);
  Require(found != catalog.end() && *found == value,"schedule RouteStore uses an unknown board identity");
  return static_cast<std::uint32_t>(found - catalog.begin());
}
FileGeneration ReadGeneration(BinaryReader& request) {
  FileGeneration value;
  value.device = request.Read<std::uint64_t>(); value.inode = request.Read<std::uint64_t>();
  value.bytes = request.Read<std::uint64_t>(); value.modified_seconds = request.Read<std::int64_t>();
  value.modified_nanoseconds = request.Read<std::int64_t>(); value.changed_seconds = request.Read<std::int64_t>();
  value.changed_nanoseconds = request.Read<std::int64_t>();
  return value;
}
using Arc = ExactScheduleArc;
using ArcKey = ExactScheduleArcKey;
}
ExactScheduleBoard ReadExactScheduleBoard(const ArtifactRef& artifact,
                                          const std::string& request_path) {
  BinaryReader request(request_path);
  ReadAndValidateHeader(request,{0x3142534d44544545ULL,1,"exact schedule board request"});
  const auto expected = ReadGeneration(request);
  if (!(artifact.generation() == expected))
    throw StageError(ErrorCode::kMixedGeneration,"schedule ingress RouteStore generation changed");
  const auto frame = request.Read<std::uint32_t>(), barrier = request.Read<std::uint32_t>(), settle = request.Read<std::uint32_t>();
  const auto minimum = request.Read<std::uint32_t>(), maximum = request.Read<std::uint32_t>(), quantum = request.Read<std::uint32_t>();
  const auto fpga = ReadCatalog(request), links = ReadCatalog(request), capacities = ReadCatalog(request);
  Require(frame > barrier && !fpga.empty() && minimum && maximum >= minimum && maximum <= frame && quantum,
          "schedule board request dimensions are invalid");
  const auto arc_count = request.Read<std::uint32_t>();
  Require(arc_count <= request.remaining() / 56,"schedule board arcs are truncated");
  std::map<ArcKey,Arc> arcs;
  for (std::uint32_t i = 0; i < arc_count; ++i) {
    Arc arc{request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),
            request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),request.Read<std::uint32_t>(),
            request.Read<double>(),request.Read<double>(),request.Read<double>()};
    Require(arc.link < links.size() && arc.from < fpga.size() && arc.to < fpga.size() && arc.from != arc.to &&
            arc.capacity < capacities.size() && arc.affine <= 1 && std::isfinite(arc.base) && arc.base >= 0 &&
            std::isfinite(arc.slope) && arc.slope >= 0 && std::isfinite(arc.period) && arc.period > 0 &&
            std::uint64_t(arc.offset) + arc.budget <= UINT32_MAX &&
            arcs.emplace(ArcKey{arc.link,arc.from,arc.to},arc).second,"schedule board arc is invalid");
  }
  request.RequireEnd();
  return {frame,barrier,settle,minimum,maximum,quantum,{fpga,links,capacities},std::move(arcs)};
}
ExactScheduleModel BuildExactScheduleModel(const std::string& route_path,
                                          const std::string& request_path,
                                          ExactScheduleBuildContext& context) {
  const auto artifact = ArtifactRef::Bind(route_path,"system-route-store-v3");
  const auto board=ReadExactScheduleBoard(artifact,request_path);
  const auto frame=board.frame,barrier=board.barrier,settle=board.settle;
  const auto minimum=board.minimum,maximum=board.maximum,quantum=board.quantum;
  const auto& fpga=board.catalogs.fpgas;
  const auto& links=board.catalogs.links;
  const auto& capacities=board.catalogs.capacities;
  const auto& arcs=board.arcs;
  context.catalogs=board.catalogs;
  RouteView source(artifact);
  Require(source.count() <= UINT32_MAX,"schedule has too many routes");
  Require(source.count() <= artifact.generation().bytes,"schedule route count exceeds source bounds");
  ExactScheduleModel model{frame,barrier,settle,static_cast<std::uint32_t>(fpga.size()),
      std::vector<std::uint32_t>(capacities.size(),1),std::vector<ExactScheduleRoute>(source.count())};
  const auto route_count = static_cast<std::uint32_t>(model.routes.size());
  context.identities.resize(route_count);
  std::vector<std::string> demands, nets;
  std::vector<std::vector<std::string>> predecessors;
  std::vector<bool> used_capacity(capacities.size(),false);
  source.VisitNetOrder([&](const RouteCoordinate& coordinate, const RouteIdentity& input) {
    const auto rank=coordinate.net_order;
    auto& route = model.routes[rank];
    demands.push_back(input.demand); nets.push_back(input.net); predecessors.push_back(input.predecessors);
    route.source = Index(fpga,input.source);
    Require(input.dependency_level.has_value() && input.dependency_depth.has_value() && !input.sinks.empty(),
            "schedule RouteStore has incomplete exact metadata");
    route.dependency_level = *input.dependency_level;
    route.maximum_depth = 0;
    std::vector<bool> seen_sink(fpga.size(),false);
    for (const auto& sink : input.sinks) {
      const auto index = Index(fpga,sink);
      Require(!seen_sink[index],"schedule route has a duplicate sink"); seen_sink[index] = true;
      route.sinks.push_back(index);
    }
    std::vector<std::vector<const Arc*>> adjacency(fpga.size());
    std::vector<const Arc*> original_arcs;
    for (const auto& edge : input.edges) {
      auto found = arcs.find({Index(links,edge.link),Index(fpga,edge.from),Index(fpga,edge.to)});
      Require(found != arcs.end(),"schedule route uses an illegal directed arc");
      adjacency[found->second.from].push_back(&found->second);
      original_arcs.push_back(&found->second);
    }
    for (auto& children : adjacency)
      std::sort(children.begin(),children.end(),[](const Arc* a, const Arc* b) { return std::tie(a->to,a->link) < std::tie(b->to,b->link); });
    std::vector<bool> visited(fpga.size(),false); visited[route.source] = true;
    std::map<const Arc*,std::uint32_t> hop_indices;
    std::vector<std::pair<std::uint32_t,std::uint32_t>> queue{{route.source,0}};
    for (std::size_t cursor = 0; cursor < queue.size(); ++cursor) {
      const auto node = queue[cursor].first, depth = queue[cursor].second;
      for (const auto* arc : adjacency[node]) {
        Require(!visited[arc->to],"schedule RouteStore route is not an acyclic tree");
        visited[arc->to] = true; queue.emplace_back(arc->to,depth + 1);
        Require(arc->budget > 0,"infeasible active schedule direction has no allocated bank");
        Require(!used_capacity[arc->capacity] || model.capacity_lanes[arc->capacity] == arc->budget,
                "schedule capacity has inconsistent lane budgets");
        used_capacity[arc->capacity] = true; model.capacity_lanes[arc->capacity] = arc->budget;
        route.maximum_depth = std::max(route.maximum_depth,depth);
        hop_indices.emplace(arc,static_cast<std::uint32_t>(route.hops.size()));
        route.hops.push_back({depth,arc->capacity,arc->link,arc->from,arc->to,arc->offset,arc->budget,UINT32_MAX,1,arc->fixed_latency});
      }
    }
    Require(route.hops.size() == input.edges.size(),"schedule RouteStore route has disconnected tree edges");
    for (auto sink : route.sinks) Require(visited[sink],"schedule route sink is not reached by its tree");
    {
      auto& identity = context.identities[rank];
      identity.dependency_depth = *input.dependency_depth;
      for (const auto* arc : original_arcs) identity.original_hop_order.push_back(hop_indices.at(arc));
    }
  },true);
  std::vector<std::uint32_t> demand_order(route_count);
  std::iota(demand_order.begin(),demand_order.end(),0);
  std::sort(demand_order.begin(),demand_order.end(),[&](auto a, auto b) { return demands[a] < demands[b]; });
  for (std::uint32_t i = 0; i < route_count; ++i) {
    if (i) Require(demands[demand_order[i-1]] < demands[demand_order[i]],"schedule demand identities repeat");
    model.routes[demand_order[i]].demand_rank = i;
  }
  for (std::uint32_t i = 0; i < route_count; ++i) {
    auto& route = model.routes[i];
    for (const auto& net : predecessors[i]) {
      auto found = std::lower_bound(nets.begin(),nets.end(),net);
      Require(found != nets.end() && *found == net,"schedule route predecessor is unknown");
      const auto parent = static_cast<std::uint32_t>(found - nets.begin());
      Require(parent != i,"schedule route is its own predecessor"); route.predecessors.push_back(parent);
    }
  }
  // All graph-sized wire groups and sorting remain in this native owner.
  std::vector<std::vector<std::pair<std::uint32_t,std::uint32_t>>> groups(capacities.size());
  for (std::uint32_t route = 0; route < route_count; ++route)
    for (std::uint32_t h = 0; h < model.routes[route].hops.size(); ++h) {
      const auto& hop = model.routes[route].hops[h];
      if (arcs.at({hop.link,hop.from,hop.to}).affine) groups[hop.capacity].emplace_back(route,h);
    }
  for (auto& group : groups) {
    if (group.empty()) continue;
    std::sort(group.begin(),group.end(),[&](const auto& a, const auto& b) {
      const auto& ah = model.routes[a.first].hops[a.second]; const auto& bh = model.routes[b.first].hops[b.second];
      return std::tie(model.routes[a.first].demand_rank,ah.link,ah.from,ah.to) < std::tie(model.routes[b.first].demand_rank,bh.link,bh.from,bh.to);
    });
    const auto& first = model.routes[group[0].first].hops[group[0].second];
    const auto lanes = first.lane_budget, offset = first.lane_offset;
    for (std::uint64_t i = 0; i < group.size(); ++i) {
      auto& hop = model.routes[group[i].first].hops[group[i].second];
      const auto lane = i % lanes, count = (group.size() - 1 - lane) / lanes + 1;
      const auto raw = std::max(std::uint64_t(minimum),std::uint64_t(count));
      const auto ratio = raw == 1 ? 1 : ((raw + quantum - 1) / quantum) * quantum;
      Require(ratio <= maximum,"infeasible baseline wire group exceeds maximum TDM ratio");
      hop.assigned_lane = static_cast<std::uint32_t>(offset + lane); hop.ratio = static_cast<std::uint32_t>(ratio);
      const auto& arc = arcs.at({hop.link,hop.from,hop.to});
      // Python rounds multiplication, addition and division as separate double
      // operations. Prevent FMA contraction from moving an integer boundary.
      volatile double scaled = arc.slope * (hop.ratio - 1);
      volatile double delay = arc.base + scaled;
      const auto cycles = std::ceil(delay / arc.period);
      Require(std::isfinite(cycles) && cycles >= 0 && cycles <= UINT32_MAX,"schedule affine hop latency is out of range");
      hop.latency = static_cast<std::uint32_t>(cycles);
    }
  }
  artifact.RequireUnchanged();
  for (std::uint32_t i = 0; i < route_count; ++i) {
    context.identities[i].demand = std::move(demands[i]);
    context.identities[i].net = std::move(nets[i]);
  }
  return model;
}

}  // namespace emuflow::core
