#pragma once
#include <cstdint>
#include <utility>
#include <vector>

namespace emuflow::native {
// Dense domains and row positions belong to one caller-bound schedule. The
// producer retains its artifact lease throughout grouping and refinement.
struct ChimewGroupingSignal {
  int domain = -1;
  int guard_lane = -1;
  int ratio = 0;
  int slot = -1;
  std::uint64_t encoding = 0;
};
struct ChimewGroupingResult {
  std::vector<int> assignment;
  int groups = 0;
  std::int64_t crossing_bits = 0;
};
ChimewGroupingResult
GroupChimewSignalRecords(std::vector<ChimewGroupingSignal> signals);

struct ChimewRefinementSignal {
  int index = -1;
  int domain = -1;
  int ratio = 0;
  unsigned long long encoding = 0;
  int group = -1;
  double source_y = 0.0;
};
struct ChimewRefinementResult {
  std::vector<ChimewRefinementSignal> entries;
  int accepted_buckets = 0, moved_signals = 0;
  double before = 0.0, after = 0.0;
};
ChimewRefinementResult
RefineChimewSignalRecords(std::vector<ChimewRefinementSignal> signals);

struct ChimewPoint {
  double x = 0.0, y = 0.0;
};
struct ChimewRudyNet {
  int index = -1;
  std::vector<ChimewPoint> pins;
};
struct ChimewRudyInput {
  double origin_x = 0.0, origin_y = 0.0, bin_width = 0.0, bin_height = 0.0;
  int columns = 0, rows = 0;
  double wire_pitch_per_layer = 0.0, max_utilization = 0.0;
  std::vector<double> capacities;
  std::vector<ChimewRudyNet> nets;
};
struct ChimewRudyResult {
  std::vector<double> loads, utilizations;
  int pins = 0, overloaded_bins = 0;
  double wire_area = 0.0, total_bin_load = 0.0, peak_load = 0.0,
         peak_utilization = 0.0;
};
ChimewRudyResult EvaluateChimewRudyRecords(const ChimewRudyInput &input);

struct ChimewBankMember {
  int direction = -1;
  double timing_weight = 1.0;
  ChimewPoint fanout;
  std::vector<ChimewPoint> fanins;
};
struct ChimewBankGroup {
  int index = -1, domain = -1, kind = -1, direction = -1, expected_members = 0;
  std::vector<ChimewBankMember> members;
};
struct ChimewBankChannel {
  int index = -1, bank = -1, order = -1;
  ChimewPoint pin_a, pin_b;
};
struct ChimewBankPair {
  int index = -1, domain = -1;
  ChimewPoint bank_a, bank_b;
  std::vector<int> channels;
};
struct ChimewBankInput {
  double cost_scale = 0.0;
  std::vector<ChimewBankPair> banks;
  std::vector<ChimewBankChannel> channels;
  std::vector<ChimewBankGroup> groups;
};
struct ChimewAssignment {
  std::vector<int> right_for_left;
  std::vector<std::int64_t> cost_for_left;
  std::int64_t total_cost = 0;
  std::vector<std::int64_t> potentials;
};
struct ChimewBankAlternative {
  int priority = 0;
  std::vector<int> groups;
  ChimewAssignment assignment;
};
struct ChimewBankResult {
  ChimewAssignment stage1;
  std::vector<std::pair<ChimewBankAlternative, ChimewBankAlternative>>
      alternatives;
  std::vector<int> chosen_priority;
  std::int64_t channel_cost = 0;
};
ChimewBankResult AssignChimewBankRecords(const ChimewBankInput &input);
} // namespace emuflow::native
