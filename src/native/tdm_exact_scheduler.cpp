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

#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/schedule_model.hpp"

namespace {

constexpr std::uint32_t kUnassignedLane =
    std::numeric_limits<std::uint32_t>::max();

using Entry = emuflow::core::ExactScheduleEntry;
using Completion = emuflow::core::ExactScheduleCompletion;

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

emuflow::core::ExactScheduleSolution Compute(const emuflow::core::ExactScheduleModel& model) {
  const auto frame_slots = model.frame_slots, barrier_slots = model.barrier_slots, settle_slots = model.settle_slots;
  const auto fpga_count = model.fpga_count;
  const auto capacity_count = static_cast<std::uint32_t>(model.capacity_lanes.size());
  const auto route_count = static_cast<std::uint32_t>(model.routes.size());
  const auto& capacity_lanes = model.capacity_lanes;
  const auto& routes = model.routes;
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
  std::size_t hop_count = 0;
  for (const auto& route : routes) hop_count += route.hops.size();
  entries.reserve(hop_count);
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
      if ((hop.word_bits != 1 && hop.word_bits != 8) ||
          (hop.assigned_lane != kUnassignedLane && hop.ratio % hop.word_bits)) {
        throw std::runtime_error("invalid scheduled word format or ratio");
      }
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
      if (hop.word_bits > 1 && hop.assigned_lane != kUnassignedLane) {
        const auto words = hop.ratio / hop.word_bits;
        if (hop.phase_word >= words) throw std::runtime_error("word phase exceeds periodic lane table");
        const std::uint64_t phase = hop.phase_word;
        const std::uint64_t rounds = hop_ready <= phase ? 0 :
            (std::uint64_t(hop_ready) - phase + words - 1) / words;
        const auto selected = phase + rounds * words;
        if (selected > UINT32_MAX) throw std::runtime_error("word event exceeds native time domain");
        slot = static_cast<std::uint32_t>(selected);
        lane = hop.assigned_lane;
      } else if (hop.assigned_lane == kUnassignedLane) {
        slot = available[hop.capacity].First(hop_ready);
        if (slot < frame_slots) {
          lane = available[hop.capacity].Assign(slot, hop.lane_offset);
        }
      } else {
        lane = hop.assigned_lane;
        const auto window_end = std::min<std::uint64_t>(latest, std::uint64_t(hop_ready) + hop.ratio / hop.word_bits);
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
      if (hop.assigned_lane != kUnassignedLane && std::uint64_t(slot) >= std::uint64_t(hop_ready) + hop.ratio / hop.word_bits) {
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

  return {std::move(order),std::move(entries),std::move(completions)};
}

}  // namespace

emuflow::core::ExactScheduleSolution emuflow::native::ComputeStaticExactSchedule(
    const emuflow::core::ExactScheduleModel& model) {
  return Compute(model);
}
