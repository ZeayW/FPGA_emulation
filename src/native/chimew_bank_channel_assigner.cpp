// SPDX-License-Identifier: Apache-2.0
// Chimew Section 3.4 two-stage bank/channel assignment kernel.

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <future>
#include <iostream>
#include <limits>
#include <map>
#include <optional>
#include <queue>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <type_traits>
#include <utility>
#include <vector>

#include "emuflow_core/hash.hpp"
#include "emuflow_core/native_stages.hpp"

namespace {

struct Point {
  double x = 0.0;
  double y = 0.0;
};

struct Member {
  int direction = -1;  // 0: FPGA A -> B, 1: FPGA B -> A.
  double timing_weight = 1.0;
  Point fanout;
  std::vector<Point> fanins;
};

struct Group {
  int index = -1;
  int domain = -1;
  int kind = -1;       // 0: TDM group, 1: common signal.
  int direction = -1;  // 0: A -> B, 1: B -> A, 2: shared TDM bundle.
  int expected_members = 0;
  std::vector<Member> members;
};

struct Channel {
  int index = -1;
  int bank = -1;
  int order = -1;
  Point pin_a;
  Point pin_b;
};

struct BankPair {
  int index = -1;
  int domain = -1;
  Point bank_a;
  Point bank_b;
  std::vector<int> channels;
};

struct Input {
  double cost_scale = 0.0;
  std::vector<BankPair> banks;
  std::vector<Channel> channels;
  std::vector<Group> groups;
};

double manhattan(const Point& lhs, const Point& rhs) {
  return std::abs(lhs.x - rhs.x) + std::abs(lhs.y - rhs.y);
}




void read_exact(std::istream& stream, void* destination, std::size_t size,
                const char* label) {
  if (!stream.read(static_cast<char*>(destination),
                   static_cast<std::streamsize>(size))) {
    throw std::runtime_error(std::string("truncated Chimew typed input ") +
                             label);
  }
}

template <typename Unsigned>
Unsigned read_little_endian(std::istream& stream, std::uint32_t* checksum,
                            const char* label) {
  static_assert(std::is_unsigned_v<Unsigned>);
  std::array<unsigned char, sizeof(Unsigned)> bytes{};
  read_exact(stream, bytes.data(), bytes.size(), label);
  if (checksum != nullptr) {
    emuflow::core::UpdateCrc32(*checksum, bytes.data(), bytes.size());
  }
  Unsigned result = 0;
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    result |= static_cast<Unsigned>(bytes[index]) << (8 * index);
  }
  return result;
}

std::int32_t read_i32(std::istream& stream, std::uint32_t* checksum,
                      const char* label) {
  const std::uint32_t bits =
      read_little_endian<std::uint32_t>(stream, checksum, label);
  std::int32_t result = 0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

double read_double(std::istream& stream, std::uint32_t* checksum,
                   const char* label) {
  const std::uint64_t bits =
      read_little_endian<std::uint64_t>(stream, checksum, label);
  double result = 0.0;
  static_assert(sizeof(result) == sizeof(bits));
  std::memcpy(&result, &bits, sizeof(result));
  if (!std::isfinite(result)) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
  return result;
}

void expect_tag(std::istream& stream, std::uint32_t& checksum,
                unsigned char expected, const char* label) {
  unsigned char tag = 0;
  read_exact(stream, &tag, 1, label);
  emuflow::core::UpdateCrc32(checksum, &tag, 1);
  if (tag != expected) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
}

int checked_count(std::uint64_t value, const char* label, bool positive) {
  if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()) ||
      (positive && value == 0)) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
  return static_cast<int>(value);
}

Input read_typed_input(const std::string& path) {
  std::ifstream input_file;
  std::istream* stream = &std::cin;
  if (path != "-") {
    input_file.open(path, std::ios::binary);
    if (!input_file) {
      throw std::runtime_error("cannot open Chimew typed input");
    }
    stream = &input_file;
  }
  // Seal scope: the complete fixed-width header and every record byte.  The
  // trailer carrying the declared checksum is outside the checksum itself.
  std::uint32_t checksum = 0;
  std::array<unsigned char, 8> magic{};
  read_exact(*stream, magic.data(), magic.size(), "header");
  emuflow::core::UpdateCrc32(checksum, magic.data(), magic.size());
  const std::array<unsigned char, 8> expected_magic = {
      'E', 'M', 'C', 'H', 'B', 'C', '3', 0};
  if (magic != expected_magic ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "version") != 1 ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "endianness") !=
          0x01020304U) {
    throw std::runtime_error("invalid Chimew typed input header");
  }
  const int bank_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "bank count"),
      "bank count", true);
  const int channel_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "channel count"),
      "channel count", true);
  const int group_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "group count"),
      "group count", true);
  const int member_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "member count"),
      "member count", true);
  const int fanin_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "fanin count"),
      "fanin count", true);
  const double cost_scale = read_double(*stream, &checksum, "cost scale");
  if (!(cost_scale > 0.0)) {
    throw std::runtime_error("invalid Chimew typed input cost scale");
  }
  std::array<unsigned char, 32> source_digest{};
  read_exact(*stream, source_digest.data(), source_digest.size(),
             "source digest");
  emuflow::core::UpdateCrc32(checksum, source_digest.data(), source_digest.size());

  Input input;
  input.cost_scale = cost_scale;
  input.banks.reserve(bank_count);
  input.channels.reserve(channel_count);
  input.groups.reserve(group_count);
  int observed_members = 0;
  int observed_fanins = 0;
  for (int bank_index = 0; bank_index < bank_count; ++bank_index) {
    expect_tag(*stream, checksum, 'B', "bank tag");
    BankPair bank;
    bank.index = read_i32(*stream, &checksum, "bank index");
    bank.domain = read_i32(*stream, &checksum, "bank domain");
    bank.bank_a.x = read_double(*stream, &checksum, "bank A x");
    bank.bank_a.y = read_double(*stream, &checksum, "bank A y");
    bank.bank_b.x = read_double(*stream, &checksum, "bank B x");
    bank.bank_b.y = read_double(*stream, &checksum, "bank B y");
    const int local_channels = checked_count(
        read_little_endian<std::uint64_t>(*stream, &checksum,
                                          "bank channel count"),
        "bank channel count", true);
    if (bank.index != bank_index || bank.domain < 0 ||
        local_channels > channel_count - static_cast<int>(input.channels.size())) {
      throw std::runtime_error("invalid Chimew typed input bank");
    }
    input.banks.push_back(std::move(bank));
    for (int order = 0; order < local_channels; ++order) {
      expect_tag(*stream, checksum, 'C', "channel tag");
      Channel channel;
      channel.index = read_i32(*stream, &checksum, "channel index");
      channel.bank = read_i32(*stream, &checksum, "channel bank");
      channel.order = read_i32(*stream, &checksum, "channel order");
      channel.pin_a.x = read_double(*stream, &checksum, "channel A x");
      channel.pin_a.y = read_double(*stream, &checksum, "channel A y");
      channel.pin_b.x = read_double(*stream, &checksum, "channel B x");
      channel.pin_b.y = read_double(*stream, &checksum, "channel B y");
      if (channel.index != static_cast<int>(input.channels.size()) ||
          channel.bank != bank_index || channel.order != order) {
        throw std::runtime_error("invalid Chimew typed input channel");
      }
      input.banks.back().channels.push_back(channel.index);
      input.channels.push_back(std::move(channel));
    }
  }
  if (static_cast<int>(input.channels.size()) != channel_count) {
    throw std::runtime_error("Chimew typed input channel count disagrees");
  }
  for (int group_index = 0; group_index < group_count; ++group_index) {
    expect_tag(*stream, checksum, 'G', "group tag");
    Group group;
    group.index = read_i32(*stream, &checksum, "group index");
    group.domain = read_i32(*stream, &checksum, "group domain");
    group.kind = read_i32(*stream, &checksum, "group kind");
    group.direction = read_i32(*stream, &checksum, "group direction");
    group.expected_members = checked_count(
        read_little_endian<std::uint64_t>(*stream, &checksum,
                                          "group member count"),
        "group member count", true);
    if (group.index != group_index || group.domain < 0 ||
        (group.kind != 0 && group.kind != 1) ||
        (group.direction != 0 && group.direction != 1 &&
         group.direction != 2) ||
        (group.kind == 1 && group.expected_members != 1) ||
        group.expected_members > member_count - observed_members) {
      throw std::runtime_error("invalid Chimew typed input group");
    }
    group.members.reserve(group.expected_members);
    for (int member_index = 0; member_index < group.expected_members;
         ++member_index) {
      expect_tag(*stream, checksum, 'M', "member tag");
      const int declared_index =
          read_i32(*stream, &checksum, "member index");
      Member member;
      member.direction = read_i32(*stream, &checksum, "member direction");
      member.timing_weight =
          read_double(*stream, &checksum, "member timing weight");
      member.fanout.x = read_double(*stream, &checksum, "member fanout x");
      member.fanout.y = read_double(*stream, &checksum, "member fanout y");
      const int local_fanins = checked_count(
          read_little_endian<std::uint64_t>(*stream, &checksum,
                                            "member fanin count"),
          "member fanin count", true);
      if (declared_index != member_index ||
          (member.direction != 0 && member.direction != 1) ||
          (group.direction != 2 && member.direction != group.direction) ||
          !(member.timing_weight > 0.0) ||
          local_fanins > fanin_count - observed_fanins) {
        throw std::runtime_error("invalid Chimew typed input member");
      }
      member.fanins.reserve(local_fanins);
      for (int fanin = 0; fanin < local_fanins; ++fanin) {
        expect_tag(*stream, checksum, 'F', "fanin tag");
        Point point;
        point.x = read_double(*stream, &checksum, "fanin x");
        point.y = read_double(*stream, &checksum, "fanin y");
        member.fanins.push_back(point);
        ++observed_fanins;
      }
      group.members.push_back(std::move(member));
      ++observed_members;
    }
    if (group.direction == 2) {
      bool seen_direction[2] = {false, false};
      for (const Member& member : group.members) {
        seen_direction[member.direction] = true;
      }
      if (!seen_direction[0] || !seen_direction[1]) {
        throw std::runtime_error(
            "bidirectional Chimew typed bundle lacks one direction");
      }
    }
    input.groups.push_back(std::move(group));
  }
  if (observed_members != member_count || observed_fanins != fanin_count) {
    throw std::runtime_error("Chimew typed input relation counts disagree");
  }
  std::array<unsigned char, 8> end_magic{};
  read_exact(*stream, end_magic.data(), end_magic.size(), "trailer");
  const std::array<unsigned char, 8> expected_end = {
      'E', 'M', 'C', 'H', 'E', 'N', 'D', 0};
  const std::uint64_t declared_checksum =
      read_little_endian<std::uint64_t>(*stream, nullptr, "checksum");
  std::array<unsigned char, 32> trailer_digest{};
  read_exact(*stream, trailer_digest.data(), trailer_digest.size(),
             "trailer digest");
  if (end_magic != expected_end || declared_checksum != checksum ||
      trailer_digest != source_digest || stream->peek() != std::char_traits<char>::eof()) {
    throw std::runtime_error("Chimew typed input seal disagrees");
  }
  return input;
}

double raw_cost(const Group& group, const Point& endpoint_a,
                const Point& endpoint_b) {
  double cost = 0.0;
  for (const Member& member : group.members) {
    const Point& output = member.direction == 0 ? endpoint_a : endpoint_b;
    const Point& input = member.direction == 0 ? endpoint_b : endpoint_a;
    double member_cost = manhattan(member.fanout, output);
    double fanin_distance = 0.0;
    for (const Point& fanin : member.fanins) {
      fanin_distance += manhattan(fanin, input);
    }
    member_cost += fanin_distance / static_cast<double>(member.fanins.size());
    cost += member.timing_weight * member_cost;
  }
  return cost;
}

std::int64_t ranked_cost(double raw, double scale) {
  if (!std::isfinite(raw) || raw < 0.0 ||
      raw > static_cast<double>(std::numeric_limits<std::int64_t>::max()) /
                scale) {
    throw std::runtime_error("Chimew edge cost is out of range");
  }
  return static_cast<std::int64_t>(std::llround(raw * scale));
}

struct Edge {
  int to = -1;
  int reverse = -1;
  int capacity = 0;
  std::int64_t cost = 0;
};

class MinCostFlow {
 public:
  explicit MinCostFlow(int nodes) : graph_(nodes) {}

  int add_edge(int from, int to, int capacity, std::int64_t cost) {
    const int index = static_cast<int>(graph_[from].size());
    Edge forward{to, static_cast<int>(graph_[to].size()), capacity, cost};
    Edge reverse{from, index, 0, -cost};
    graph_[from].push_back(forward);
    graph_[to].push_back(reverse);
    return index;
  }

  std::pair<int, std::int64_t> solve(int source, int sink, int target) {
    const int nodes = static_cast<int>(graph_.size());
    const auto infinity = std::numeric_limits<std::int64_t>::max() / 4;
    int flow = 0;
    std::int64_t total = 0;
    while (flow < target) {
      std::vector<std::int64_t> distance(nodes, infinity);
      std::vector<int> previous_node(nodes, -1);
      std::vector<int> previous_edge(nodes, -1);
      std::vector<char> queued(nodes, false);
      std::queue<int> queue;
      distance[source] = 0;
      queue.push(source);
      queued[source] = true;
      while (!queue.empty()) {
        const int node = queue.front();
        queue.pop();
        queued[node] = false;
        for (int index = 0; index < static_cast<int>(graph_[node].size());
             ++index) {
          const Edge& edge = graph_[node][index];
          if (edge.capacity <= 0 || distance[node] == infinity) {
            continue;
          }
          const std::int64_t candidate = distance[node] + edge.cost;
          if (candidate < distance[edge.to]) {
            distance[edge.to] = candidate;
            previous_node[edge.to] = node;
            previous_edge[edge.to] = index;
            if (!queued[edge.to]) {
              queued[edge.to] = true;
              queue.push(edge.to);
            }
          }
        }
      }
      if (distance[sink] == infinity) {
        break;
      }
      for (int node = sink; node != source; node = previous_node[node]) {
        if (previous_node[node] < 0) {
          throw std::runtime_error("broken Chimew augmenting path");
        }
        Edge& edge = graph_[previous_node[node]][previous_edge[node]];
        --edge.capacity;
        ++graph_[node][edge.reverse].capacity;
      }
      ++flow;
      total += distance[sink];
    }
    return {flow, total};
  }

  std::vector<std::int64_t> certificate_potentials() const {
    const int nodes = static_cast<int>(graph_.size());
    std::vector<std::int64_t> distance(nodes, 0);
    std::vector<int> path_length(nodes, 0);
    std::vector<char> queued(nodes, true);
    std::queue<int> queue;
    for (int node = 0; node < nodes; ++node) {
      queue.push(node);
    }
    while (!queue.empty()) {
      const int node = queue.front();
      queue.pop();
      queued[node] = false;
      for (const Edge& edge : graph_[node]) {
        if (edge.capacity <= 0 ||
            distance[edge.to] <= distance[node] + edge.cost) {
          continue;
        }
        distance[edge.to] = distance[node] + edge.cost;
        path_length[edge.to] = path_length[node] + 1;
        if (path_length[edge.to] >= nodes) {
          throw std::runtime_error(
              "negative residual cycle in Chimew assignment");
        }
        if (!queued[edge.to]) {
          queued[edge.to] = true;
          queue.push(edge.to);
        }
      }
    }
    for (int node = 0; node < nodes; ++node) {
      for (const Edge& edge : graph_[node]) {
        if (edge.capacity > 0 &&
            edge.cost + distance[node] - distance[edge.to] < 0) {
          throw std::runtime_error("invalid Chimew optimality certificate");
        }
      }
    }
    return distance;
  }

  const Edge& edge(int node, int index) const { return graph_[node][index]; }

 private:
  std::vector<std::vector<Edge>> graph_;
};

struct CandidateEdge {
  int right = -1;
  int left = -1;
  std::int64_t cost = 0;
};

struct AssignmentResult {
  std::vector<int> right_for_left;
  std::vector<std::int64_t> cost_for_left;
  std::int64_t total_cost = 0;
  std::vector<std::int64_t> potentials;
};

// Accumulate the sign of a short linear expression without relying on the
// non-standard __int128 extension or overflowing int64_t.  The certificate
// checks below contain at most three signed int64_t terms, so a two-limb
// unsigned magnitude is more than sufficient while remaining strict C++17.
class ExactSignedSum {
 public:
  void add(std::int64_t value, bool negate = false) {
    const bool term_negative = (value < 0) != negate;
    const std::uint64_t magnitude =
        value < 0 ? static_cast<std::uint64_t>(-(value + 1)) + 1U
                  : static_cast<std::uint64_t>(value);
    if (magnitude == 0) {
      return;
    }
    if (high_ == 0 && low_ == 0) {
      negative_ = term_negative;
      low_ = magnitude;
      return;
    }
    if (negative_ == term_negative) {
      const std::uint64_t previous = low_;
      low_ += magnitude;
      if (low_ < previous) {
        ++high_;
      }
      return;
    }
    if (high_ != 0 || low_ >= magnitude) {
      if (low_ < magnitude) {
        --high_;
      }
      low_ -= magnitude;
      if (high_ == 0 && low_ == 0) {
        negative_ = false;
      }
      return;
    }
    low_ = magnitude - low_;
    negative_ = term_negative;
  }

  bool negative() const { return negative_; }

 private:
  bool negative_ = false;
  std::uint64_t high_ = 0;
  std::uint64_t low_ = 0;
};

AssignmentResult assign(int right_count, const std::vector<int>& capacities,
                        int left_count,
                        const std::vector<CandidateEdge>& candidates) {
  const int source = 0;
  const int first_right = 1;
  const int first_left = first_right + right_count;
  const int sink = first_left + left_count;

  // A materialized platform commonly has exactly one legal bank for every
  // group.  Running one residual-graph traversal per group in that case is
  // mathematically redundant and turns an otherwise linear stage-1 binding
  // into a quadratic workload.  Build the same unique feasible assignment
  // directly and emit a residual-dual certificate that the independent
  // Python checker verifies in exactly the same way as the general solver.
  std::vector<int> unique_right(left_count, -1);
  std::vector<std::int64_t> unique_cost(left_count, 0);
  bool unique_candidate = true;
  for (const CandidateEdge& candidate : candidates) {
    if (candidate.left < 0 || candidate.left >= left_count ||
        candidate.right < 0 || candidate.right >= right_count ||
        unique_right[candidate.left] >= 0) {
      unique_candidate = false;
      break;
    }
    unique_right[candidate.left] = candidate.right;
    unique_cost[candidate.left] = candidate.cost;
  }
  if (unique_candidate &&
      std::find(unique_right.begin(), unique_right.end(), -1) ==
          unique_right.end()) {
    std::vector<int> used(right_count, 0);
    AssignmentResult result;
    result.right_for_left = unique_right;
    result.cost_for_left = unique_cost;
    result.potentials.assign(sink + 1, 0);
    for (int left = 0; left < left_count; ++left) {
      const int right = unique_right[left];
      if (++used[right] > capacities[right]) {
        throw std::runtime_error("no complete Chimew assignment exists");
      }
      if (unique_cost[left] >
          std::numeric_limits<std::int64_t>::max() - result.total_cost) {
        throw std::runtime_error("Chimew assignment cost is out of range");
      }
      result.total_cost += unique_cost[left];
      result.potentials[first_left + left] = unique_cost[left];
      result.potentials[sink] =
          std::max(result.potentials[sink], unique_cost[left]);
    }
    return result;
  }

  // Large physical banks often contain many signal groups with exactly the
  // same ranked cost row.  Collapsing those indistinguishable left vertices
  // into demand vertices preserves the integer min-cost-flow problem while
  // avoiding one copy of the dense row per signal.  The expanded result is
  // accepted only after every original residual edge is checked against the
  // expanded dual, so this is an exact certified path rather than a heuristic.
  const auto compressed_assignment = [&]() -> std::optional<AssignmentResult> {
    if (!std::all_of(capacities.begin(), capacities.end(),
                     [](int capacity) { return capacity == 1; })) {
      return std::nullopt;
    }
    using CostRow = std::vector<std::pair<int, std::int64_t>>;
    std::vector<CostRow> rows(left_count);
    for (const CandidateEdge& candidate : candidates) {
      if (candidate.left < 0 || candidate.left >= left_count ||
          candidate.right < 0 || candidate.right >= right_count) {
        return std::nullopt;
      }
      rows[candidate.left].push_back({candidate.right, candidate.cost});
    }
    std::map<CostRow, int> type_by_row;
    std::vector<CostRow> type_rows;
    std::vector<std::vector<int>> members_by_type;
    for (int left = 0; left < left_count; ++left) {
      CostRow& row = rows[left];
      std::sort(row.begin(), row.end());
      if (row.empty() ||
          std::adjacent_find(row.begin(), row.end(),
                             [](const auto& lhs, const auto& rhs) {
                               return lhs.first == rhs.first;
                             }) != row.end()) {
        return std::nullopt;
      }
      auto [position, inserted] =
          type_by_row.emplace(row, static_cast<int>(type_rows.size()));
      if (inserted) {
        type_rows.push_back(row);
        members_by_type.push_back({});
      }
      members_by_type[position->second].push_back(left);
    }
    const int type_count = static_cast<int>(type_rows.size());
    if (type_count == 0 || type_count > left_count / 4) {
      return std::nullopt;
    }

    const int compressed_first_right = 1;
    const int compressed_first_type = compressed_first_right + right_count;
    const int compressed_sink = compressed_first_type + type_count;
    MinCostFlow flow(compressed_sink + 1);
    for (int right = 0; right < right_count; ++right) {
      flow.add_edge(source, compressed_first_right + right, 1, 0);
    }
    struct CompressedReference {
      int right = -1;
      int type = -1;
      std::int64_t cost = 0;
      int edge_index = -1;
    };
    std::vector<CompressedReference> references;
    for (int type = 0; type < type_count; ++type) {
      for (const auto& [right, cost] : type_rows[type]) {
        const int edge_index =
            flow.add_edge(compressed_first_right + right,
                          compressed_first_type + type, 1, cost);
        references.push_back({right, type, cost, edge_index});
      }
      flow.add_edge(compressed_first_type + type, compressed_sink,
                    static_cast<int>(members_by_type[type].size()), 0);
    }
    const auto [assigned, total] = flow.solve(source, compressed_sink, left_count);
    if (assigned != left_count) {
      throw std::runtime_error("no complete Chimew assignment exists");
    }
    std::vector<std::vector<std::pair<int, std::int64_t>>> selected_by_type(
        type_count);
    for (const CompressedReference& reference : references) {
      const int node = compressed_first_right + reference.right;
      if (flow.edge(node, reference.edge_index).capacity == 0) {
        selected_by_type[reference.type].push_back(
            {reference.right, reference.cost});
      }
    }

    AssignmentResult result;
    result.right_for_left.assign(left_count, -1);
    result.cost_for_left.assign(left_count, 0);
    result.total_cost = total;
    const std::vector<std::int64_t> compressed_potentials =
        flow.certificate_potentials();
    result.potentials.assign(sink + 1, 0);
    result.potentials[source] = compressed_potentials[source];
    for (int right = 0; right < right_count; ++right) {
      result.potentials[first_right + right] =
          compressed_potentials[compressed_first_right + right];
    }
    for (int type = 0; type < type_count; ++type) {
      auto& selected = selected_by_type[type];
      std::sort(selected.begin(), selected.end());
      if (selected.size() != members_by_type[type].size()) {
        return std::nullopt;
      }
      for (std::size_t index = 0; index < selected.size(); ++index) {
        const int left = members_by_type[type][index];
        result.right_for_left[left] = selected[index].first;
        result.cost_for_left[left] = selected[index].second;
        result.potentials[first_left + left] =
            compressed_potentials[compressed_first_type + type];
      }
    }
    result.potentials[sink] = compressed_potentials[compressed_sink];

    std::vector<char> used_right(right_count, false);
    for (int left = 0; left < left_count; ++left) {
      const int right = result.right_for_left[left];
      if (right < 0 || used_right[right]) {
        return std::nullopt;
      }
      used_right[right] = true;
      ExactSignedSum reverse_reduced;
      reverse_reduced.add(result.cost_for_left[left], true);
      reverse_reduced.add(result.potentials[first_left + left]);
      reverse_reduced.add(result.potentials[first_right + right], true);
      if (reverse_reduced.negative()) {
        return std::nullopt;
      }
    }
    for (const CandidateEdge& candidate : candidates) {
      if (result.right_for_left[candidate.left] == candidate.right) {
        continue;
      }
      ExactSignedSum forward_reduced;
      forward_reduced.add(candidate.cost);
      forward_reduced.add(
          result.potentials[first_right + candidate.right]);
      forward_reduced.add(
          result.potentials[first_left + candidate.left], true);
      if (forward_reduced.negative()) {
        return std::nullopt;
      }
    }
    for (int right = 0; right < right_count; ++right) {
      const std::int64_t right_potential =
          result.potentials[first_right + right];
      const std::int64_t source_potential = result.potentials[source];
      if ((used_right[right] && right_potential < source_potential) ||
          (!used_right[right] && source_potential < right_potential)) {
        return std::nullopt;
      }
    }
    for (int left = 0; left < left_count; ++left) {
      if (result.potentials[sink] <
          result.potentials[first_left + left]) {
        return std::nullopt;
      }
    }
    return result;
  }();
  if (compressed_assignment.has_value()) {
    return *compressed_assignment;
  }

  MinCostFlow flow(sink + 1);
  for (int right = 0; right < right_count; ++right) {
    flow.add_edge(source, first_right + right, capacities[right], 0);
  }
  struct Reference {
    CandidateEdge candidate;
    int edge_index = -1;
  };
  std::vector<Reference> references;
  references.reserve(candidates.size());
  for (const CandidateEdge& candidate : candidates) {
    const int edge_index = flow.add_edge(first_right + candidate.right,
                                         first_left + candidate.left, 1,
                                         candidate.cost);
    references.push_back({candidate, edge_index});
  }
  for (int left = 0; left < left_count; ++left) {
    flow.add_edge(first_left + left, sink, 1, 0);
  }
  const auto [assigned, total] = flow.solve(source, sink, left_count);
  if (assigned != left_count) {
    throw std::runtime_error("no complete Chimew assignment exists");
  }
  AssignmentResult result;
  result.right_for_left.assign(left_count, -1);
  result.cost_for_left.assign(left_count, 0);
  result.total_cost = total;
  for (const Reference& reference : references) {
    const int node = first_right + reference.candidate.right;
    if (flow.edge(node, reference.edge_index).capacity != 0) {
      continue;
    }
    const int left = reference.candidate.left;
    if (result.right_for_left[left] >= 0) {
      throw std::runtime_error("ambiguous Chimew assignment");
    }
    result.right_for_left[left] = reference.candidate.right;
    result.cost_for_left[left] = reference.candidate.cost;
  }
  if (std::find(result.right_for_left.begin(), result.right_for_left.end(), -1) !=
      result.right_for_left.end()) {
    throw std::runtime_error("incomplete Chimew assignment reconstruction");
  }
  result.potentials = flow.certificate_potentials();
  return result;
}

struct Stage2Result {
  int priority = 0;
  std::vector<int> groups;
  AssignmentResult assignment;
};

int bank_worker_count(std::size_t jobs) {
  if (jobs == 0) {
    return 0;
  }
  unsigned int requested = std::thread::hardware_concurrency();
  if (requested == 0) {
    requested = 1;
  }
  bool explicitly_requested = false;
  if (const char* override_value =
          std::getenv("EMUFLOW_CHIMEW_BANK_WORKERS")) {
    char* end = nullptr;
    const long parsed = std::strtol(override_value, &end, 10);
    if (override_value[0] == '\0' || end == nullptr || *end != '\0' ||
        parsed <= 0 || parsed > 256) {
      throw std::runtime_error(
          "EMUFLOW_CHIMEW_BANK_WORKERS must be an integer in [1, 256]");
    }
    requested = static_cast<unsigned int>(parsed);
    explicitly_requested = true;
  }
  // Candidate graphs are dense.  Bound automatic parallelism so a large
  // platform cannot multiply peak memory by every available host core; an
  // explicit override remains available on memory-rich validation nodes.
  if (!explicitly_requested) {
    requested = std::min(requested, 8U);
  }
  return static_cast<int>(
      std::min<std::size_t>(jobs, static_cast<std::size_t>(requested)));
}

Stage2Result solve_bank(const Input& input, int bank_index,
                        const std::vector<int>& groups, int priority) {
  const BankPair& bank = input.banks[bank_index];
  int direction_counts[3] = {0, 0, 0};
  int common_count = 0;
  for (int group_index : groups) {
    const Group& group = input.groups[group_index];
    if (group.kind == 0) {
      ++direction_counts[group.direction];
    } else {
      ++common_count;
    }
  }
  int dedicated_direction = -1;
  if (common_count == 0) {
    if (direction_counts[0] > 0 && direction_counts[1] == 0 &&
        direction_counts[2] == 0) {
      dedicated_direction = 0;
    } else if (direction_counts[1] > 0 && direction_counts[0] == 0 &&
               direction_counts[2] == 0) {
      dedicated_direction = 1;
    } else if (direction_counts[2] > 0 && direction_counts[0] == 0 &&
               direction_counts[1] == 0) {
      dedicated_direction = 2;
    }
  }
  std::vector<CandidateEdge> candidates;
  for (int right = 0; right < static_cast<int>(bank.channels.size()); ++right) {
    int required_kind = 1;
    int required_direction = -1;
    const int first_direction = priority;
    const int second_direction = 1 - priority;
    if (right < direction_counts[first_direction]) {
      required_kind = 0;
      required_direction = first_direction;
    } else if (right < direction_counts[first_direction] +
                           direction_counts[second_direction]) {
      required_kind = 0;
      required_direction = second_direction;
    } else if (right < direction_counts[first_direction] +
                           direction_counts[second_direction] +
                           direction_counts[2]) {
      required_kind = 0;
      required_direction = 2;
    }
    const Channel& channel = input.channels[bank.channels[right]];
    for (int left = 0; left < static_cast<int>(groups.size()); ++left) {
      const Group& group = input.groups[groups[left]];
      const bool eligible = dedicated_direction >= 0
                                ? group.kind == 0 &&
                                      group.direction == dedicated_direction
                                : (required_kind == 0
                                       ? group.kind == 0 &&
                                             group.direction ==
                                                 required_direction
                                       : group.kind == 1);
      if (!eligible) {
        continue;
      }
      candidates.push_back(
          {right, left,
           ranked_cost(raw_cost(group, channel.pin_a, channel.pin_b),
                       input.cost_scale)});
    }
  }
  std::vector<int> capacities(bank.channels.size(), 1);
  return {priority, groups,
          assign(static_cast<int>(bank.channels.size()), capacities,
                 static_cast<int>(groups.size()), candidates)};
}

void write_certificate(std::ostream& output, const std::string& label,
                       const AssignmentResult& result) {
  output << "CERT " << label << " " << result.potentials.size() << "\n";
  for (int node = 0; node < static_cast<int>(result.potentials.size()); ++node) {
    output << "POT " << label << " " << node << " "
           << result.potentials[node] << "\n";
  }
}

void run(const std::string& input_path, std::ostream& output) {
  const Input input = read_typed_input(input_path);
  std::vector<CandidateEdge> bank_candidates;
  for (int bank = 0; bank < static_cast<int>(input.banks.size()); ++bank) {
    for (int group = 0; group < static_cast<int>(input.groups.size()); ++group) {
      if (input.banks[bank].domain != input.groups[group].domain) {
        continue;
      }
      bank_candidates.push_back(
          {bank, group,
           ranked_cost(raw_cost(input.groups[group], input.banks[bank].bank_a,
                                input.banks[bank].bank_b),
                       input.cost_scale)});
    }
  }
  std::vector<int> bank_capacities;
  for (const BankPair& bank : input.banks) {
    bank_capacities.push_back(static_cast<int>(bank.channels.size()));
  }
  const AssignmentResult stage1 =
      assign(static_cast<int>(input.banks.size()), bank_capacities,
             static_cast<int>(input.groups.size()), bank_candidates);

  std::vector<std::vector<int>> groups_by_bank(input.banks.size());
  for (int group = 0; group < static_cast<int>(input.groups.size()); ++group) {
    groups_by_bank[stage1.right_for_left[group]].push_back(group);
  }
  std::vector<std::pair<Stage2Result, Stage2Result>> alternatives(
      input.banks.size());
  std::vector<int> chosen_priority(input.banks.size(), 0);
  std::vector<std::int64_t> selected_cost(input.banks.size(), 0);
  std::vector<int> active_banks;
  for (int bank = 0; bank < static_cast<int>(input.banks.size()); ++bank) {
    if (!groups_by_bank[bank].empty()) {
      active_banks.push_back(bank);
    }
  }
  std::atomic<std::size_t> next_bank{0};
  std::vector<std::future<void>> workers;
  const int worker_count = bank_worker_count(active_banks.size());
  workers.reserve(worker_count);
  for (int worker = 0; worker < worker_count; ++worker) {
    workers.push_back(std::async(std::launch::async, [&]() {
      while (true) {
        const std::size_t job = next_bank.fetch_add(1);
        if (job >= active_banks.size()) {
          return;
        }
        const int bank = active_banks[job];
        Stage2Result first =
            solve_bank(input, bank, groups_by_bank[bank], 0);
        Stage2Result second =
            solve_bank(input, bank, groups_by_bank[bank], 1);
        const int priority =
            second.assignment.total_cost < first.assignment.total_cost ? 1
                                                                        : 0;
        selected_cost[bank] = priority == 0 ? first.assignment.total_cost
                                             : second.assignment.total_cost;
        chosen_priority[bank] = priority;
        alternatives[bank] =
            {std::move(first), std::move(second)};
      }
    }));
  }
  for (std::future<void>& worker : workers) {
    worker.get();
  }
  std::int64_t stage2_total = 0;
  for (std::int64_t cost : selected_cost) {
    if (cost > std::numeric_limits<std::int64_t>::max() - stage2_total) {
      throw std::runtime_error("Chimew assignment cost is out of range");
    }
    stage2_total += cost;
  }

  output << "EMUFLOW_CHIMEW_BANK_CHANNEL_OUTPUT_V1\n";
  output << "METRIC " << input.groups.size() << " " << stage1.total_cost << " "
         << stage2_total << "\n";
  for (int group = 0; group < static_cast<int>(input.groups.size()); ++group) {
    output << "BANK_ASSIGN " << group << " " << stage1.right_for_left[group]
           << " " << stage1.cost_for_left[group] << "\n";
  }
  write_certificate(output, "STAGE1", stage1);
  for (int bank = 0; bank < static_cast<int>(input.banks.size()); ++bank) {
    if (groups_by_bank[bank].empty()) {
      continue;
    }
    output << "CHOSEN " << bank << " " << chosen_priority[bank] << "\n";
    for (int priority = 0; priority < 2; ++priority) {
      const Stage2Result& result = priority == 0 ? alternatives[bank].first
                                                 : alternatives[bank].second;
      const std::string label =
          "BANK" + std::to_string(bank) + "P" + std::to_string(priority);
      output << "ALTERNATIVE " << bank << " " << priority << " "
             << result.assignment.total_cost << "\n";
      for (int left = 0; left < static_cast<int>(result.groups.size()); ++left) {
        const int local_channel = result.assignment.right_for_left[left];
        output << "CHANNEL_ASSIGN " << bank << " " << priority << " "
               << result.groups[left] << " "
               << input.banks[bank].channels[local_channel] << " "
               << result.assignment.cost_for_left[left] << "\n";
      }
      write_certificate(output, label, result.assignment);
    }
  }
}

}  // namespace

void emuflow::native::AssignChimewBankChannels(
    const std::string& input_path, std::ostream& output) {
  run(input_path, output);
}
