#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <utility>

namespace emuflow::core {
struct ExactScheduleHop {
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
  std::uint32_t word_bits = 1;
  std::uint32_t phase_word = 0;
};

struct ExactScheduleRoute {
  std::uint32_t source;
  std::uint32_t dependency_level;
  std::uint32_t maximum_depth;
  std::uint32_t demand_rank;
  std::vector<std::uint32_t> sinks;
  std::vector<std::uint32_t> predecessors;
  std::vector<ExactScheduleHop> hops;
};

struct ExactScheduleModel {
  std::uint32_t frame_slots, barrier_slots, settle_slots, fpga_count;
  std::vector<std::uint32_t> capacity_lanes;
  std::vector<ExactScheduleRoute> routes;
};
struct ExactScheduleEntry {
  std::uint32_t route, hop, sequence, slot, lane, ready, arrival;
};
struct ExactScheduleCompletion {
  std::uint32_t route, ready, completion;
  std::vector<std::pair<std::uint32_t, std::uint32_t>> sink_arrivals;
};
struct ExactScheduleSolution {
  std::vector<std::uint32_t> order;
  std::vector<ExactScheduleEntry> entries;
  std::vector<ExactScheduleCompletion> completions;
};
struct ExactScheduleCatalogs {
  std::vector<std::string> fpgas, links, capacities;
};
struct ExactSchedulePublicationIdentity {
  std::string demand, net;
  std::uint32_t dependency_depth;
  std::vector<std::uint32_t> original_hop_order;
};
struct ExactScheduleBuildContext {
  bool uses_word_links = false;
  ExactScheduleCatalogs catalogs;
  std::vector<ExactSchedulePublicationIdentity> identities;
};
}  // namespace emuflow::core
