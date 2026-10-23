#include "emuflow_core/schedule_model.hpp"
#include "emuflow_core/artifact_io.hpp"
#include <algorithm>
#include <limits>

namespace emuflow::core {
ExactScheduleModel ReadExactScheduleModel(const std::string& input_path) {
  emuflow::core::BinaryReader input(input_path);
  emuflow::core::ReadAndValidateHeader(input, {0x3149534d44544545ULL,1,"exact schedule input"});
  const auto frame_slots = input.Read<std::uint32_t>();
  const auto barrier_slots = input.Read<std::uint32_t>();
  const auto settle_slots = input.Read<std::uint32_t>();
  const auto fpga_count = input.Read<std::uint32_t>();
  const auto capacity_count = input.Read<std::uint32_t>();
  const auto route_count = input.Read<std::uint32_t>();
  if (frame_slots <= barrier_slots || fpga_count == 0) {
    throw std::runtime_error("exact schedule dimensions are invalid");
  }

  if (capacity_count > input.remaining() / 4) throw std::runtime_error("exact schedule capacities are truncated");
  std::vector<std::uint32_t> capacity_lanes(capacity_count);
  for (auto& lanes : capacity_lanes) {
    lanes = input.Read<std::uint32_t>();
    if (lanes == 0) throw std::runtime_error("capacity has no lanes");
  }
  if (route_count > input.remaining() / 28) throw std::runtime_error("exact schedule routes are truncated");
  std::vector<ExactScheduleRoute> routes(route_count);
  for (std::uint32_t route_index = 0; route_index < route_count; ++route_index) {
    auto& route = routes[route_index];
    route.source = input.Read<std::uint32_t>();
    const auto sink_count = input.Read<std::uint32_t>();
    const auto predecessor_count = input.Read<std::uint32_t>();
    const auto hop_count = input.Read<std::uint32_t>();
    route.dependency_level = input.Read<std::uint32_t>();
    route.maximum_depth = input.Read<std::uint32_t>();
    route.demand_rank = input.Read<std::uint32_t>();
    if (route.source >= fpga_count || route.demand_rank >= route_count) {
      throw std::runtime_error("exact schedule route identity is invalid");
    }
    if (std::uint64_t(sink_count) * 4 + std::uint64_t(predecessor_count) * 4 + std::uint64_t(hop_count) * 40 > input.remaining())
      throw std::runtime_error("exact schedule route children are truncated");
    route.sinks.resize(sink_count);
    for (auto& sink : route.sinks) {
      sink = input.Read<std::uint32_t>();
      if (sink >= fpga_count) throw std::runtime_error("route sink is invalid");
    }
    route.predecessors.resize(predecessor_count);
    for (auto& predecessor : route.predecessors) {
      predecessor = input.Read<std::uint32_t>();
      if (predecessor >= route_count || predecessor == route_index) {
        throw std::runtime_error("route predecessor is invalid");
      }
    }
    auto unique_predecessors = route.predecessors;
    std::sort(unique_predecessors.begin(), unique_predecessors.end());
    if (std::adjacent_find(unique_predecessors.begin(),
                           unique_predecessors.end()) !=
        unique_predecessors.end()) {
      throw std::runtime_error("route predecessors are duplicated");
    }
    if (route.sinks.empty()) throw std::runtime_error("route has no sinks");
    route.hops.resize(hop_count);
    for (auto& hop : route.hops) {
      hop.depth = input.Read<std::uint32_t>();
      hop.capacity = input.Read<std::uint32_t>();
      hop.link = input.Read<std::uint32_t>();
      hop.from = input.Read<std::uint32_t>();
      hop.to = input.Read<std::uint32_t>();
      hop.lane_offset = input.Read<std::uint32_t>();
      hop.lane_budget = input.Read<std::uint32_t>();
      hop.assigned_lane = input.Read<std::uint32_t>();
      hop.ratio = input.Read<std::uint32_t>();
      hop.latency = input.Read<std::uint32_t>();
      if (hop.capacity >= capacity_count || hop.from >= fpga_count ||
          hop.to >= fpga_count || hop.lane_budget == 0 || hop.ratio == 0) {
        throw std::runtime_error("exact schedule hop is invalid");
      }
    }
  }
  input.RequireEnd();

  return {frame_slots,barrier_slots,settle_slots,fpga_count,std::move(capacity_lanes),std::move(routes)};
}

void WriteExactScheduleModel(const ExactScheduleModel& model, const std::string& path) {
  AtomicBinaryWriter out(path);
  out.Write(std::uint64_t{0x3149534d44544545ULL}); out.Write(std::uint32_t{1});
  for (auto value : {model.frame_slots,model.barrier_slots,model.settle_slots,model.fpga_count,
                    static_cast<std::uint32_t>(model.capacity_lanes.size()),static_cast<std::uint32_t>(model.routes.size())}) out.Write(value);
  for (auto lanes : model.capacity_lanes) out.Write(lanes);
  for (const auto& route : model.routes) {
    for (auto value : {route.source,static_cast<std::uint32_t>(route.sinks.size()),
        static_cast<std::uint32_t>(route.predecessors.size()),static_cast<std::uint32_t>(route.hops.size()),
        route.dependency_level,route.maximum_depth,route.demand_rank}) out.Write(value);
    for (auto sink : route.sinks) out.Write(sink);
    for (auto predecessor : route.predecessors) out.Write(predecessor);
    for (const auto& hop : route.hops)
      for (auto value : {hop.depth,hop.capacity,hop.link,hop.from,hop.to,hop.lane_offset,
                        hop.lane_budget,hop.assigned_lane,hop.ratio,hop.latency}) out.Write(value);
  }
  out.Commit();
}
}
