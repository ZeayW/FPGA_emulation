#pragma once
#include <cstdint>
#include <string>
#include <vector>

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
// Shared, checked model boundary; no strings/JSON/FFI graph transport.
ExactScheduleModel ReadExactScheduleModel(const std::string& path);
void WriteExactScheduleModel(const ExactScheduleModel& model, const std::string& path);
}  // namespace emuflow::core
