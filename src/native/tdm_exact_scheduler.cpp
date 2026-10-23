#include <algorithm>
#include <cstdint>
#include <iostream>
#include <limits>
#include <queue>
#include <sstream>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/native_stages.hpp"

namespace {

constexpr std::uint64_t kInputMagic = 0x3149534d44544545ULL;
constexpr std::uint64_t kOutputMagic = 0x314f534d44544545ULL;
constexpr std::uint32_t kInputVersion = 1;
constexpr std::uint32_t kOutputVersion = 2;
constexpr emuflow::core::ArtifactSchema kInputSchema{
    kInputMagic, kInputVersion, "exact schedule input"};
constexpr std::uint32_t kUnassignedLane =
    std::numeric_limits<std::uint32_t>::max();

struct Hop {
  std::uint32_t depth;
  std::uint32_t capacity;
  std::uint32_t link;
  std::uint32_t from;
  std::uint32_t to;
  std::uint32_t lane_offset;
  std::uint32_t lane_budget;
  std::uint32_t assigned_lane;
  std::uint32_t ratio;
  std::uint32_t latency;
};

struct Route {
  std::uint32_t source;
  std::uint32_t dependency_level;
  std::uint32_t maximum_depth;
  std::uint32_t demand_rank;
  std::vector<std::uint32_t> sinks;
  std::vector<std::uint32_t> predecessors;
  std::vector<Hop> hops;
};

struct Entry {
  std::uint32_t route;
  std::uint32_t hop;
  std::uint32_t sequence;
  std::uint32_t slot;
  std::uint32_t lane;
  std::uint32_t ready;
  std::uint32_t arrival;
};

struct Completion {
  std::uint32_t route;
  std::uint32_t ready;
  std::uint32_t completion;
  std::vector<std::pair<std::uint32_t, std::uint32_t>> sink_arrivals;
};

class AvailableSlots {
 public:
  AvailableSlots(std::uint32_t frame_slots, std::uint32_t lane_budget)
      : fill_(frame_slots, 0), parent_(frame_slots + 1),
        lane_budget_(lane_budget) {
    if (lane_budget == 0) throw std::runtime_error("capacity has no lanes");
    for (std::uint32_t slot = 0; slot <= frame_slots; ++slot) {
      parent_[slot] = slot;
    }
  }

  std::uint32_t First(std::uint32_t start) {
    if (start >= parent_.size()) return static_cast<std::uint32_t>(fill_.size());
    std::uint32_t root = start;
    while (parent_[root] != root) root = parent_[root];
    while (parent_[start] != start) {
      const auto next = parent_[start];
      parent_[start] = root;
      start = next;
    }
    return root;
  }

  std::uint32_t Assign(std::uint32_t slot, std::uint32_t lane_offset) {
    if (slot >= fill_.size()) throw std::runtime_error("slot is outside frame");
    const auto lane = lane_offset + fill_[slot]++;
    if (fill_[slot] == lane_budget_) parent_[slot] = First(slot + 1);
    return lane;
  }

 private:
  std::vector<std::uint32_t> fill_;
  std::vector<std::uint32_t> parent_;
  std::uint32_t lane_budget_;
};

void RunImpl(const std::string& input_path, const std::string& output_path) {
  emuflow::core::BinaryReader input(input_path);
  emuflow::core::ReadAndValidateHeader(input, kInputSchema);
  const auto frame_slots = input.Read<std::uint32_t>();
  const auto barrier_slots = input.Read<std::uint32_t>();
  const auto settle_slots = input.Read<std::uint32_t>();
  const auto fpga_count = input.Read<std::uint32_t>();
  const auto capacity_count = input.Read<std::uint32_t>();
  const auto route_count = input.Read<std::uint32_t>();
  if (frame_slots <= barrier_slots || fpga_count == 0) {
    throw std::runtime_error("exact schedule dimensions are invalid");
  }

  std::vector<std::uint32_t> capacity_lanes(capacity_count);
  for (auto& lanes : capacity_lanes) {
    lanes = input.Read<std::uint32_t>();
    if (lanes == 0) throw std::runtime_error("capacity has no lanes");
  }
  std::vector<Route> routes(route_count);
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

  std::vector<std::vector<std::uint32_t>> successors(route_count);
  std::vector<std::uint32_t> indegree(route_count);
  for (std::uint32_t route = 0; route < route_count; ++route) {
    indegree[route] = static_cast<std::uint32_t>(routes[route].predecessors.size());
    for (const auto predecessor : routes[route].predecessors) {
      successors[predecessor].push_back(route);
    }
  }
  for (auto& values : successors) std::sort(values.begin(), values.end());

  std::vector<std::uint32_t> reverse_order(route_count);
  for (std::uint32_t index = 0; index < route_count; ++index) reverse_order[index] = index;
  std::sort(reverse_order.begin(), reverse_order.end(), [&](auto lhs, auto rhs) {
    return std::tie(routes[lhs].dependency_level, lhs) >
           std::tie(routes[rhs].dependency_level, rhs);
  });
  std::vector<std::uint32_t> remaining_depth(route_count, 0);
  for (const auto route : reverse_order) {
    for (const auto predecessor : routes[route].predecessors) {
      remaining_depth[predecessor] = std::max(
          remaining_depth[predecessor], remaining_depth[route] + 1);
    }
  }

  using Priority = std::tuple<std::int64_t, std::int64_t, std::uint32_t>;
  std::priority_queue<Priority, std::vector<Priority>, std::greater<Priority>> ready;
  for (std::uint32_t route = 0; route < route_count; ++route) {
    if (indegree[route] == 0) {
      ready.emplace(-static_cast<std::int64_t>(remaining_depth[route]),
                    -static_cast<std::int64_t>(routes[route].maximum_depth), route);
    }
  }
  std::vector<std::uint32_t> order;
  order.reserve(route_count);
  while (!ready.empty()) {
    const auto route = std::get<2>(ready.top());
    ready.pop();
    order.push_back(route);
    for (const auto successor : successors[route]) {
      if (--indegree[successor] == 0) {
        ready.emplace(-static_cast<std::int64_t>(remaining_depth[successor]),
                      -static_cast<std::int64_t>(routes[successor].maximum_depth),
                      successor);
      }
    }
  }
  if (order.size() != route_count) {
    throw std::runtime_error("exact schedule dependency graph is cyclic");
  }

  std::vector<AvailableSlots> available;
  available.reserve(capacity_count);
  for (const auto lanes : capacity_lanes) available.emplace_back(frame_slots, lanes);
  std::vector<std::set<std::pair<std::uint32_t, std::uint32_t>>> occupied(capacity_count);
  const auto missing = std::numeric_limits<std::int64_t>::min();
  std::vector<std::vector<std::int64_t>> arrivals(
      route_count, std::vector<std::int64_t>(fpga_count, missing));
  std::vector<Entry> entries;
  std::vector<Completion> completions;
  entries.reserve(input.size() / 36);
  completions.reserve(route_count);

  for (const auto route_index : order) {
    const auto& route = routes[route_index];
    std::uint32_t source_ready = settle_slots;
    for (const auto predecessor : route.predecessors) {
      const auto arrival = arrivals[predecessor][route.source];
      if (arrival == missing || arrival < 0) {
        throw std::runtime_error("exact predecessor has not arrived at source");
      }
      source_ready = std::max(
          source_ready, static_cast<std::uint32_t>(arrival) + settle_slots);
    }
    std::vector<std::int64_t> route_arrivals(fpga_count, missing);
    route_arrivals[route.source] = static_cast<std::int64_t>(source_ready) - 1;
    for (std::uint32_t hop_index = 0; hop_index < route.hops.size(); ++hop_index) {
      const auto& hop = route.hops[hop_index];
      std::uint32_t hop_ready = source_ready;
      if (hop.from != route.source) {
        if (route_arrivals[hop.from] == missing || route_arrivals[hop.from] < 0) {
          throw std::runtime_error("route hop predecessor has not arrived");
        }
        hop_ready = static_cast<std::uint32_t>(route_arrivals[hop.from]) + 1;
      }
      if (hop.latency >= frame_slots - barrier_slots) {
        std::ostringstream detail;
        detail << "exact schedule is infeasible: route=" << route_index
               << " demand_rank=" << route.demand_rank
               << " hop=" << hop_index << " capacity=" << hop.capacity
               << " link=" << hop.link << " from=" << hop.from
               << " to=" << hop.to << " ready=" << hop_ready
               << " frame_slots=" << frame_slots
               << " barrier_slots=" << barrier_slots
               << " latency=" << hop.latency << " ratio=" << hop.ratio
               << " reason=hop-latency-exceeds-frame";
        throw std::runtime_error(detail.str());
      }
      const auto latest = frame_slots - barrier_slots - hop.latency;
      std::uint32_t slot = hop_ready;
      std::uint32_t lane = 0;
      if (hop.assigned_lane == kUnassignedLane) {
        slot = available[hop.capacity].First(hop_ready);
        if (slot < frame_slots) {
          lane = available[hop.capacity].Assign(slot, hop.lane_offset);
        }
      } else {
        lane = hop.assigned_lane;
        const auto window_end = std::min(latest, hop_ready + hop.ratio);
        while (slot < window_end && occupied[hop.capacity].count({slot, lane})) {
          ++slot;
        }
      }
      if (slot >= latest) {
        std::ostringstream detail;
        detail << "exact schedule is infeasible: route=" << route_index
               << " demand_rank=" << route.demand_rank
               << " hop=" << hop_index << " capacity=" << hop.capacity
               << " link=" << hop.link << " from=" << hop.from
               << " to=" << hop.to << " ready=" << hop_ready
               << " selected_slot=" << slot << " latest_exclusive=" << latest
               << " frame_slots=" << frame_slots
               << " barrier_slots=" << barrier_slots
               << " latency=" << hop.latency << " ratio=" << hop.ratio
               << " lane=" << lane << " reason=no-slot-before-frame-boundary";
        throw std::runtime_error(detail.str());
      }
      if (hop.assigned_lane != kUnassignedLane && slot >= hop_ready + hop.ratio) {
        std::ostringstream detail;
        detail << "exact ratio window is infeasible: route=" << route_index
               << " demand_rank=" << route.demand_rank
               << " hop=" << hop_index << " capacity=" << hop.capacity
               << " link=" << hop.link << " from=" << hop.from
               << " to=" << hop.to << " ready=" << hop_ready
               << " selected_slot=" << slot << " ratio=" << hop.ratio
               << " lane=" << lane << " reason=lane-window-exhausted";
        throw std::runtime_error(detail.str());
      }
      if (hop.assigned_lane != kUnassignedLane) {
        occupied[hop.capacity].insert({slot, lane});
      }
      const auto arrival = slot + hop.latency;
      route_arrivals[hop.to] = arrival;
      entries.push_back({route_index, hop_index,
                         static_cast<std::uint32_t>(entries.size()), slot, lane,
                         hop_ready, arrival});
    }
    Completion completion{route_index, source_ready, 0, {}};
    for (const auto sink : route.sinks) {
      const auto arrival = route_arrivals[sink];
      if (arrival == missing || arrival < 0) {
        throw std::runtime_error("route sink was not scheduled");
      }
      const auto value = static_cast<std::uint32_t>(arrival);
      arrivals[route_index][sink] = value;
      completion.completion = std::max(completion.completion, value);
      completion.sink_arrivals.emplace_back(sink, value);
    }
    std::sort(completion.sink_arrivals.begin(), completion.sink_arrivals.end());
    completions.push_back(std::move(completion));
  }

  std::sort(entries.begin(), entries.end(), [&](const auto& lhs, const auto& rhs) {
    const auto& left_hop = routes[lhs.route].hops[lhs.hop];
    const auto& right_hop = routes[rhs.route].hops[rhs.hop];
    return std::tie(lhs.slot, left_hop.capacity, lhs.lane,
                    routes[lhs.route].demand_rank) <
           std::tie(rhs.slot, right_hop.capacity, rhs.lane,
                    routes[rhs.route].demand_rank);
  });
  std::sort(completions.begin(), completions.end(), [&](const auto& lhs, const auto& rhs) {
    return routes[lhs.route].demand_rank < routes[rhs.route].demand_rank;
  });

  emuflow::core::AtomicBinaryWriter output(output_path);
  output.Write(kOutputMagic);
  output.Write(kOutputVersion);
  output.Write(static_cast<std::uint64_t>(entries.size()));
  output.Write(route_count);
  for (const auto route : order) output.Write(route);
  for (const auto& entry : entries) {
    const auto& hop = routes[entry.route].hops[entry.hop];
    output.Write(entry.route);
    output.Write(entry.sequence);
    output.Write(hop.depth);
    output.Write(hop.capacity);
    output.Write(hop.link);
    output.Write(hop.from);
    output.Write(hop.to);
    output.Write(entry.slot);
    output.Write(entry.lane);
    output.Write(entry.ready);
    output.Write(entry.arrival);
    output.Write(hop.assigned_lane == kUnassignedLane ? 0u : hop.ratio);
  }
  for (const auto& completion : completions) {
    output.Write(completion.route);
    output.Write(completion.ready);
    output.Write(completion.completion);
    output.Write(static_cast<std::uint32_t>(completion.sink_arrivals.size()));
    for (const auto& [sink, arrival] : completion.sink_arrivals) {
      output.Write(sink);
      output.Write(arrival);
    }
  }
  output.Commit();
}

}  // namespace

void emuflow::native::ScheduleStaticExact(const std::string& input_path,
                                          const std::string& output_path) {
  RunImpl(input_path, output_path);
}

#if !defined(EMUFLOW_NATIVE_STAGE_LIBRARY)
int main(int argc, char** argv) {
  try {
    if (argc != 3) {
      std::cerr << "usage: emuflow_tdm_exact_scheduler INPUT OUTPUT\n";
      return 2;
    }
    emuflow::native::ScheduleStaticExact(argv[1], argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
#endif
