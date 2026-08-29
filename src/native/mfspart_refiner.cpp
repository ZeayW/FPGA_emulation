// SPDX-License-Identifier: Apache-2.0
//
// Independent paper-level reproduction of MFSPart direct k-way FM refinement
// (TCAD 2026, Eqs. 9--10), extended with a weighted worst-sink-hop term so
// aggregate fanout gains cannot silently lengthen a net's critical board path.
// No source from the unlicensed companion repository is copied or linked.

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

struct Node {
  int fixed_part = -1;
  std::vector<long long> weights;
};

struct Net {
  double weight = 1.0;
  double bottleneck_weight = 1.0;
  int max_distance_limit = -1;
  int source = -1;
  std::vector<int> sinks;
};

struct TimingPath {
  double weight = 0.0;
  std::vector<int> pins;
};

struct Input {
  int parts = 0;
  int dimensions = 0;
  int hmax = 0;
  int move_distance = 0;
  int early_stop = 0;
  double gamma = 0.0;
  double lambda = 0.0;
  double mu = 0.0;
  double bottleneck_beta = 0.0;
  double timing_path_beta = 0.0;
  std::vector<std::vector<int>> distances;
  std::vector<std::vector<long long>> capacities;
  std::vector<Node> nodes;
  std::vector<Net> nets;
  std::vector<TimingPath> timing_paths;
  std::vector<int> assignment;
};

struct Move {
  int node = -1;
  int source = -1;
  int target = -1;
  double gain = 0.0;
  double cumulative = 0.0;
};

struct CandidateMove {
  double gain = -std::numeric_limits<double>::infinity();
  long long gain_rank = std::numeric_limits<long long>::min();
  int node = -1;
  int target = -1;
  int version = -1;

  bool operator<(const CandidateMove& other) const {
    if (gain_rank != other.gain_rank) {
      return gain_rank < other.gain_rank;
    }
    if (node != other.node) {
      return node > other.node;
    }
    return target > other.target;
  }
};

constexpr double kGainRankScale = 1'000'000'000.0;

long long gain_rank(double gain) {
  return std::llround(gain * kGainRankScale);
}

struct Metrics {
  double driver_sink_cut = 0.0;
  double connectivity = 0.0;
  double weighted_hops = 0.0;
  double mean_hops = 0.0;
  long long violating_pairs = 0;
  long long capacity_violations = 0;
  long long fixed_violations = 0;
  long long topology_guard_violations = 0;
  long long crossed_timing_paths = 0;
  double weighted_crossed_timing_paths = 0.0;
};

Input read_input(const std::string& path) {
  std::ifstream stream(path);
  if (!stream) {
    throw std::runtime_error("cannot open input: " + path);
  }
  std::string magic;
  std::getline(stream, magic);
  const bool input_v4 = magic == "EMUFLOW_MFSPART_REFINER_INPUT_V4";
  const bool input_v3 =
      input_v4 || magic == "EMUFLOW_MFSPART_REFINER_INPUT_V3";
  const bool input_v2 =
      input_v3 || magic == "EMUFLOW_MFSPART_REFINER_INPUT_V2";
  if (!input_v2 && magic != "EMUFLOW_MFSPART_REFINER_INPUT_V1") {
    throw std::runtime_error("unsupported input header");
  }
  Input input;
  int node_count = -1;
  int net_count = -1;
  int timing_path_count = 0;
  bool saw_param = false;
  std::vector<std::vector<bool>> saw_distances;
  std::vector<std::vector<bool>> saw_capacities;
  std::vector<bool> saw_nodes;
  std::vector<bool> saw_nets;
  std::vector<bool> saw_timing_paths;
  std::vector<bool> saw_assignments;
  std::string kind;
  while (stream >> kind) {
    if (kind == "PARAM") {
      if (saw_param) {
        throw std::runtime_error("duplicate PARAM record");
      }
      stream >> input.parts >> node_count >> input.dimensions >> net_count >>
          input.hmax >> input.move_distance >> input.early_stop >> input.gamma >>
          input.lambda >> input.mu;
      if (input_v2) {
        stream >> input.bottleneck_beta;
      }
      if (input_v4) {
        stream >> timing_path_count >> input.timing_path_beta;
      }
      if (input.parts <= 0 || node_count <= 0 || input.dimensions <= 0 ||
          net_count < 0 || input.hmax < 1 || input.move_distance < 1 ||
          input.early_stop < 1 || !std::isfinite(input.gamma) ||
          !std::isfinite(input.lambda) || !std::isfinite(input.mu) ||
          !std::isfinite(input.bottleneck_beta) || input.gamma < 0.0 ||
          input.lambda < 0.0 || input.mu < 0.0 ||
          input.bottleneck_beta < 0.0 || timing_path_count < 0 ||
          !std::isfinite(input.timing_path_beta) ||
          input.timing_path_beta < 0.0) {
        throw std::runtime_error("invalid PARAM record");
      }
      input.distances.assign(input.parts,
                             std::vector<int>(input.parts, -1));
      input.capacities.assign(
          input.parts, std::vector<long long>(input.dimensions, 0));
      input.nodes.assign(node_count, Node{});
      input.nets.assign(net_count, Net{});
      input.timing_paths.assign(timing_path_count, TimingPath{});
      input.assignment.assign(node_count, -1);
      saw_distances.assign(input.parts,
                           std::vector<bool>(input.parts, false));
      saw_capacities.assign(input.parts,
                            std::vector<bool>(input.dimensions, false));
      saw_nodes.assign(node_count, false);
      saw_nets.assign(net_count, false);
      saw_timing_paths.assign(timing_path_count, false);
      saw_assignments.assign(node_count, false);
      saw_param = true;
    } else if (kind == "DIST") {
      if (!saw_param) {
        throw std::runtime_error("DIST record precedes PARAM");
      }
      int source = -1;
      int target = -1;
      int distance = -1;
      stream >> source >> target >> distance;
      if (source < 0 || source >= input.parts || target < 0 ||
          target >= input.parts || distance < 0 ||
          saw_distances[source][target]) {
        throw std::runtime_error("invalid or duplicate DIST record");
      }
      input.distances[source][target] = distance;
      saw_distances[source][target] = true;
    } else if (kind == "CAP") {
      if (!saw_param) {
        throw std::runtime_error("CAP record precedes PARAM");
      }
      int part = -1;
      int dimension = -1;
      long long capacity = -1;
      stream >> part >> dimension >> capacity;
      if (part < 0 || part >= input.parts || dimension < 0 ||
          dimension >= input.dimensions || capacity <= 0 ||
          saw_capacities[part][dimension]) {
        throw std::runtime_error("invalid or duplicate CAP record");
      }
      input.capacities[part][dimension] = capacity;
      saw_capacities[part][dimension] = true;
    } else if (kind == "NODE") {
      if (!saw_param) {
        throw std::runtime_error("NODE record precedes PARAM");
      }
      int index = -1;
      Node node;
      stream >> index >> node.fixed_part;
      node.weights.resize(input.dimensions);
      for (long long& weight : node.weights) {
        stream >> weight;
      }
      if (index < 0 || index >= node_count || saw_nodes[index] ||
          node.fixed_part < -1 || node.fixed_part >= input.parts ||
          node.weights.empty() || node.weights.front() <= 0 ||
          std::any_of(node.weights.begin(), node.weights.end(),
                      [](long long value) { return value < 0; })) {
        throw std::runtime_error("invalid or duplicate NODE record");
      }
      input.nodes[index] = std::move(node);
      saw_nodes[index] = true;
    } else if (kind == "NET") {
      if (!saw_param) {
        throw std::runtime_error("NET record precedes PARAM");
      }
      int index = -1;
      int sink_count = -1;
      Net net;
      stream >> index >> net.weight;
      if (input_v3) {
        stream >> net.bottleneck_weight >> net.max_distance_limit;
      } else {
        net.bottleneck_weight = net.weight;
      }
      stream >> net.source >> sink_count;
      if (index < 0 || index >= net_count || saw_nets[index] ||
          !std::isfinite(net.weight) || net.weight <= 0.0 ||
          !std::isfinite(net.bottleneck_weight) ||
          net.bottleneck_weight < 0.0 || net.max_distance_limit < -1 ||
          net.source < 0 || net.source >= node_count || sink_count <= 0) {
        throw std::runtime_error("invalid or duplicate NET record");
      }
      std::set<int> unique;
      net.sinks.resize(sink_count);
      for (int& sink : net.sinks) {
        stream >> sink;
        if (sink < 0 || sink >= node_count || sink == net.source ||
            !unique.insert(sink).second) {
          throw std::runtime_error("invalid NET sink");
        }
      }
      std::sort(net.sinks.begin(), net.sinks.end());
      input.nets[index] = std::move(net);
      saw_nets[index] = true;
    } else if (kind == "ASSIGN") {
      if (!saw_param) {
        throw std::runtime_error("ASSIGN record precedes PARAM");
      }
      int node = -1;
      int part = -1;
      stream >> node >> part;
      if (node < 0 || node >= node_count || part < 0 || part >= input.parts ||
          saw_assignments[node]) {
        throw std::runtime_error("invalid or duplicate ASSIGN record");
      }
      input.assignment[node] = part;
      saw_assignments[node] = true;
    } else if (kind == "PATH") {
      if (!saw_param || !input_v4) {
        throw std::runtime_error("PATH record requires V4 PARAM");
      }
      int index = -1;
      int pin_count = -1;
      TimingPath timing_path;
      stream >> index >> timing_path.weight >> pin_count;
      if (index < 0 || index >= timing_path_count ||
          saw_timing_paths[index] || !std::isfinite(timing_path.weight) ||
          timing_path.weight <= 0.0 || pin_count < 2) {
        throw std::runtime_error("invalid or duplicate PATH record");
      }
      std::set<int> unique;
      timing_path.pins.resize(pin_count);
      for (int& pin : timing_path.pins) {
        stream >> pin;
        if (pin < 0 || pin >= node_count || !unique.insert(pin).second) {
          throw std::runtime_error("invalid PATH pin");
        }
      }
      std::sort(timing_path.pins.begin(), timing_path.pins.end());
      input.timing_paths[index] = std::move(timing_path);
      saw_timing_paths[index] = true;
    } else {
      throw std::runtime_error("unknown input record: " + kind);
    }
    if (!stream) {
      throw std::runtime_error("malformed input record");
    }
  }
  const auto missing = [](const auto& values) {
    return std::any_of(values.begin(), values.end(),
                       [](bool value) { return !value; });
  };
  if (!saw_param || missing(saw_nodes) || missing(saw_nets) ||
      missing(saw_timing_paths) ||
      missing(saw_assignments)) {
    throw std::runtime_error("incomplete input");
  }
  for (int part = 0; part < input.parts; ++part) {
    if (missing(saw_distances[part]) || missing(saw_capacities[part]) ||
        input.distances[part][part] != 0) {
      throw std::runtime_error("incomplete topology or capacity input");
    }
    for (int other = 0; other < input.parts; ++other) {
      if (input.distances[part][other] != input.distances[other][part]) {
        throw std::runtime_error("paper-mode FPGA distances must be symmetric");
      }
    }
  }
  return input;
}

std::vector<std::vector<std::pair<int, double>>> build_pair_adjacency(
    const Input& input) {
  std::vector<std::vector<std::pair<int, double>>> adjacency(
      input.nodes.size());
  for (const Net& net : input.nets) {
    for (const int sink : net.sinks) {
      adjacency[net.source].push_back({sink, net.weight});
      adjacency[sink].push_back({net.source, net.weight});
    }
  }
  return adjacency;
}

std::vector<std::vector<int>> build_incidence(const Input& input) {
  std::vector<std::vector<int>> incidence(input.nodes.size());
  for (int net_index = 0; net_index < static_cast<int>(input.nets.size());
       ++net_index) {
    const Net& net = input.nets[net_index];
    incidence[net.source].push_back(net_index);
    for (const int sink : net.sinks) {
      incidence[sink].push_back(net_index);
    }
  }
  return incidence;
}

std::vector<std::vector<long long>> compute_loads(
    const Input& input, const std::vector<int>& assignment) {
  std::vector<std::vector<long long>> loads(
      input.parts, std::vector<long long>(input.dimensions, 0));
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      loads[assignment[node]][dimension] += input.nodes[node].weights[dimension];
    }
  }
  return loads;
}

bool target_fits(const Input& input,
                 const std::vector<std::vector<long long>>& loads, int node,
                 int target) {
  for (int dimension = 0; dimension < input.dimensions; ++dimension) {
    if (loads[target][dimension] + input.nodes[node].weights[dimension] >
        input.capacities[target][dimension]) {
      return false;
    }
  }
  return true;
}

double compatibility(
    const Input& input,
    const std::vector<std::vector<double>>& neighbor_part_weights,
    const std::vector<std::vector<int>>& incidence,
    const std::vector<std::vector<int>>& net_part_counts,
    const std::vector<int>& net_unique_parts,
    const std::vector<std::vector<int>>& net_sink_part_counts,
    const std::vector<std::vector<int>>& net_sink_top1,
    const std::vector<std::vector<int>>& net_sink_top2,
    const std::vector<std::vector<int>>& net_sink_top1_counts,
    const std::vector<double>& timing_path_local_penalty,
    const std::vector<std::vector<double>>& timing_path_rescue,
    const std::vector<int>& assignment, int node, int candidate_part) {
  double local_hop_score = 0.0;
  double violation_penalty = 0.0;
  for (int neighbor_part = 0; neighbor_part < input.parts; ++neighbor_part) {
    const double weight = neighbor_part_weights[node][neighbor_part];
    if (weight == 0.0) {
      continue;
    }
    const int distance = input.distances[neighbor_part][candidate_part];
    if (distance <= input.hmax) {
      local_hop_score +=
          static_cast<double>(input.hmax - distance) * weight;
    } else {
      violation_penalty +=
          weight * (1.0 + input.mu * (distance - input.hmax));
    }
  }
  double connectivity = 0.0;
  double bottleneck_hops = 0.0;
  for (const int net_index : incidence[node]) {
    const Net& net = input.nets[net_index];
    int spanned_parts = net_unique_parts[net_index];
    const int source_part = assignment[node];
    if (candidate_part != source_part) {
      if (net_part_counts[net_index][candidate_part] == 0) {
        ++spanned_parts;
      }
      if (net_part_counts[net_index][source_part] == 1) {
        --spanned_parts;
      }
    }
    connectivity += net.weight * static_cast<double>(spanned_parts);
    int maximum_distance = 0;
    if (net.source == node) {
      maximum_distance = net_sink_top1[net_index][candidate_part];
    } else {
      const int driver_part = assignment[net.source];
      const int source_part = assignment[node];
      maximum_distance = net_sink_top1[net_index][driver_part];
      if (candidate_part != source_part &&
          net_sink_part_counts[net_index][source_part] == 1 &&
          input.distances[driver_part][source_part] == maximum_distance &&
          net_sink_top1_counts[net_index][driver_part] == 1) {
        maximum_distance = net_sink_top2[net_index][driver_part];
      }
      maximum_distance = std::max(
          maximum_distance, input.distances[driver_part][candidate_part]);
    }
    if (net.max_distance_limit >= 0 &&
        maximum_distance > net.max_distance_limit) {
      return -std::numeric_limits<double>::infinity();
    }
    bottleneck_hops +=
        net.bottleneck_weight * static_cast<double>(maximum_distance);
  }
  const double timing_path_delta =
      candidate_part == assignment[node]
          ? 0.0
          : -timing_path_local_penalty[node] +
                timing_path_rescue[node][candidate_part];
  return local_hop_score - input.gamma * connectivity -
         input.lambda * violation_penalty -
         input.bottleneck_beta * bottleneck_hops + timing_path_delta;
}

Metrics compute_metrics(const Input& input,
                        const std::vector<int>& assignment) {
  Metrics metrics;
  double total_pair_weight = 0.0;
  for (const Net& net : input.nets) {
    std::set<int> remote_sink_parts;
    int maximum_distance = 0;
    for (const int sink : net.sinks) {
      const int distance =
          input.distances[assignment[net.source]][assignment[sink]];
      if (assignment[net.source] != assignment[sink]) {
        metrics.driver_sink_cut += net.weight;
        remote_sink_parts.insert(assignment[sink]);
      }
      if (distance > input.hmax) {
        ++metrics.violating_pairs;
      }
      metrics.weighted_hops += net.weight * static_cast<double>(distance);
      maximum_distance = std::max(maximum_distance, distance);
      total_pair_weight += net.weight;
    }
    if (net.max_distance_limit >= 0 &&
        maximum_distance > net.max_distance_limit) {
      ++metrics.topology_guard_violations;
    }
    metrics.connectivity +=
        net.weight * static_cast<double>(remote_sink_parts.size());
  }
  metrics.mean_hops = total_pair_weight > 0.0
                           ? metrics.weighted_hops / total_pair_weight
                           : 0.0;
  const auto loads = compute_loads(input, assignment);
  for (int part = 0; part < input.parts; ++part) {
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      if (loads[part][dimension] > input.capacities[part][dimension]) {
        ++metrics.capacity_violations;
      }
    }
  }
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    if (input.nodes[node].fixed_part >= 0 &&
        assignment[node] != input.nodes[node].fixed_part) {
      ++metrics.fixed_violations;
    }
  }
  for (const TimingPath& timing_path : input.timing_paths) {
    const int first_part = assignment[timing_path.pins.front()];
    const bool crossed = std::any_of(
        timing_path.pins.begin() + 1, timing_path.pins.end(),
        [&](int pin) { return assignment[pin] != first_part; });
    if (crossed) {
      ++metrics.crossed_timing_paths;
      metrics.weighted_crossed_timing_paths += timing_path.weight;
    }
  }
  return metrics;
}

void write_metrics(std::ostream& stream, const std::string& prefix,
                   const Metrics& metrics) {
  stream << "METRIC " << prefix << "_driver_sink_cut " << std::setprecision(17)
         << metrics.driver_sink_cut << '\n';
  stream << "METRIC " << prefix << "_connectivity " << std::setprecision(17)
         << metrics.connectivity << '\n';
  stream << "METRIC " << prefix << "_weighted_hops " << std::setprecision(17)
         << metrics.weighted_hops << '\n';
  stream << "METRIC " << prefix << "_mean_hops " << std::setprecision(17)
         << metrics.mean_hops << '\n';
  stream << "METRIC " << prefix << "_violating_pairs "
         << metrics.violating_pairs << '\n';
  stream << "METRIC " << prefix << "_capacity_violations "
         << metrics.capacity_violations << '\n';
  stream << "METRIC " << prefix << "_fixed_violations "
         << metrics.fixed_violations << '\n';
  stream << "METRIC " << prefix << "_topology_guard_violations "
         << metrics.topology_guard_violations << '\n';
  stream << "METRIC " << prefix << "_crossed_timing_paths "
         << metrics.crossed_timing_paths << '\n';
  stream << "METRIC " << prefix << "_weighted_crossed_timing_paths "
         << std::setprecision(17) << metrics.weighted_crossed_timing_paths
         << '\n';
}

void run(const Input& input, const std::string& output_path) {
  std::vector<int> assignment = input.assignment;
  auto loads = compute_loads(input, assignment);
  const Metrics initial_metrics = compute_metrics(input, assignment);
  if (initial_metrics.capacity_violations != 0 ||
      initial_metrics.fixed_violations != 0 ||
      initial_metrics.topology_guard_violations != 0) {
    throw std::runtime_error(
        "initial assignment violates capacity, fixed nodes, or topology guard");
  }
  const auto adjacency = build_pair_adjacency(input);
  const auto incidence = build_incidence(input);
  std::vector<std::vector<double>> neighbor_part_weights(
      input.nodes.size(), std::vector<double>(input.parts, 0.0));
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    for (const auto& [neighbor, weight] : adjacency[node]) {
      neighbor_part_weights[node][assignment[neighbor]] += weight;
    }
  }
  std::vector<std::vector<int>> net_part_counts(
      input.nets.size(), std::vector<int>(input.parts, 0));
  std::vector<int> net_unique_parts(input.nets.size(), 0);
  std::vector<std::vector<int>> net_sink_part_counts(
      input.nets.size(), std::vector<int>(input.parts, 0));
  std::vector<std::vector<int>> net_sink_top1(
      input.nets.size(), std::vector<int>(input.parts, 0));
  std::vector<std::vector<int>> net_sink_top2(
      input.nets.size(), std::vector<int>(input.parts, 0));
  std::vector<std::vector<int>> net_sink_top1_counts(
      input.nets.size(), std::vector<int>(input.parts, 0));
  for (int net_index = 0; net_index < static_cast<int>(input.nets.size());
       ++net_index) {
    const Net& net = input.nets[net_index];
    ++net_part_counts[net_index][assignment[net.source]];
    for (const int sink : net.sinks) {
      ++net_part_counts[net_index][assignment[sink]];
      ++net_sink_part_counts[net_index][assignment[sink]];
    }
    net_unique_parts[net_index] = static_cast<int>(std::count_if(
        net_part_counts[net_index].begin(), net_part_counts[net_index].end(),
        [](int count) { return count > 0; }));
  }
  auto rebuild_sink_distance_summary = [&](int net_index) {
    for (int driver_part = 0; driver_part < input.parts; ++driver_part) {
      int first = 0;
      int second = 0;
      int first_count = 0;
      for (int sink_part = 0; sink_part < input.parts; ++sink_part) {
        if (net_sink_part_counts[net_index][sink_part] == 0) {
          continue;
        }
        const int distance = input.distances[driver_part][sink_part];
        if (distance > first) {
          second = first;
          first = distance;
          first_count = 1;
        } else if (distance == first) {
          ++first_count;
        } else if (distance > second) {
          second = distance;
        }
      }
      net_sink_top1[net_index][driver_part] = first;
      net_sink_top2[net_index][driver_part] = second;
      net_sink_top1_counts[net_index][driver_part] = first_count;
    }
  };
  for (int net_index = 0; net_index < static_cast<int>(input.nets.size());
       ++net_index) {
    rebuild_sink_distance_summary(net_index);
  }
  std::vector<std::vector<int>> timing_path_incidence(input.nodes.size());
  std::vector<std::vector<int>> timing_path_part_counts(
      input.timing_paths.size(), std::vector<int>(input.parts, 0));
  std::vector<std::vector<int>> timing_path_part_xor(
      input.timing_paths.size(), std::vector<int>(input.parts, 0));
  std::vector<int> timing_path_unique_parts(input.timing_paths.size(), 0);
  std::vector<double> timing_path_local_penalty(input.nodes.size(), 0.0);
  std::vector<std::vector<double>> timing_path_rescue(
      input.nodes.size(), std::vector<double>(input.parts, 0.0));
  for (int path_index = 0;
       path_index < static_cast<int>(input.timing_paths.size()); ++path_index) {
    for (const int pin : input.timing_paths[path_index].pins) {
      timing_path_incidence[pin].push_back(path_index);
      const int part = assignment[pin];
      ++timing_path_part_counts[path_index][part];
      timing_path_part_xor[path_index][part] ^= pin;
    }
    timing_path_unique_parts[path_index] = static_cast<int>(std::count_if(
        timing_path_part_counts[path_index].begin(),
        timing_path_part_counts[path_index].end(),
        [](int count) { return count > 0; }));
  }
  auto update_timing_path_contribution =
      [&](int path_index, double sign, std::set<int>* affected) {
        if (input.timing_path_beta == 0.0) return;
        const TimingPath& timing_path = input.timing_paths[path_index];
        const double weighted =
            sign * input.timing_path_beta * timing_path.weight;
        const int unique = timing_path_unique_parts[path_index];
        if (unique == 1) {
          for (const int pin : timing_path.pins) {
            timing_path_local_penalty[pin] += weighted;
            if (affected != nullptr) affected->insert(pin);
          }
        } else if (unique == 2) {
          int first = -1;
          int second = -1;
          for (int part = 0; part < input.parts; ++part) {
            if (timing_path_part_counts[path_index][part] == 0) continue;
            if (first < 0) {
              first = part;
            } else {
              second = part;
              break;
            }
          }
          for (const auto [part, other] :
               {std::pair<int, int>{first, second},
                std::pair<int, int>{second, first}}) {
            if (part >= 0 &&
                timing_path_part_counts[path_index][part] == 1) {
              const int singleton = timing_path_part_xor[path_index][part];
              timing_path_rescue[singleton][other] += weighted;
              if (affected != nullptr) affected->insert(singleton);
            }
          }
        }
      };
  for (int path_index = 0;
       path_index < static_cast<int>(input.timing_paths.size()); ++path_index) {
    update_timing_path_contribution(path_index, 1.0, nullptr);
  }
  std::vector<bool> locked(input.nodes.size(), false);
  std::vector<int> versions(input.nodes.size(), 0);
  std::priority_queue<CandidateMove> queue;
  std::vector<Move> moves;
  long long compatibility_evaluations = 0;
  long long candidate_recomputations = 0;
  long long capacity_invalidations = 0;

  std::vector<std::vector<std::pair<long long, int>>> weight_index(
      input.dimensions);
  for (int dimension = 0; dimension < input.dimensions; ++dimension) {
    for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
      weight_index[dimension].push_back(
          {input.nodes[node].weights[dimension], node});
    }
    std::sort(weight_index[dimension].begin(),
              weight_index[dimension].end());
  }

  std::vector<std::vector<char>> fit_cache(
      input.nodes.size(), std::vector<char>(input.parts, 0));
  std::vector<std::vector<double>> cached_gains(
      input.nodes.size(),
      std::vector<double>(input.parts,
                          -std::numeric_limits<double>::infinity()));
  std::vector<std::vector<long long>> cached_gain_ranks(
      input.nodes.size(),
      std::vector<long long>(input.parts,
                             std::numeric_limits<long long>::min()));
  std::vector<std::vector<char>> cached_gain_valid(
      input.nodes.size(), std::vector<char>(input.parts, 0));
  std::vector<int> best_targets(input.nodes.size(), -1);
  std::vector<double> best_gains(
      input.nodes.size(), -std::numeric_limits<double>::infinity());
  std::vector<long long> best_gain_ranks(
      input.nodes.size(), std::numeric_limits<long long>::min());
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    for (int target = 0; target < input.parts; ++target) {
      fit_cache[node][target] = target_fits(input, loads, node, target);
    }
  }

  auto select_cached_best = [&](int node) {
    const int source = assignment[node];
    int best_target = -1;
    double best_gain = -std::numeric_limits<double>::infinity();
    long long best_rank = std::numeric_limits<long long>::min();
    for (int target = 0; target < input.parts; ++target) {
      if (!cached_gain_valid[node][target] || target == source ||
          input.distances[source][target] > input.move_distance ||
          !fit_cache[node][target]) {
        continue;
      }
      const long long rank = cached_gain_ranks[node][target];
      if (rank > best_rank ||
          (rank == best_rank && target < best_target)) {
        best_target = target;
        best_gain = cached_gains[node][target];
        best_rank = rank;
      }
    }
    return std::tuple<int, double, long long>{best_target, best_gain,
                                               best_rank};
  };

  auto publish_best = [&](int node, int target, double gain, long long rank) {
    ++versions[node];
    best_targets[node] = target;
    best_gains[node] = gain;
    best_gain_ranks[node] = rank;
    if (target >= 0) {
      queue.push({gain, rank, node, target, versions[node]});
    }
  };

  auto recompute_candidate = [&](int node) {
    if (locked[node] || input.nodes[node].fixed_part >= 0) {
      publish_best(node, -1, -std::numeric_limits<double>::infinity(),
                   std::numeric_limits<long long>::min());
      return;
    }
    ++candidate_recomputations;
    const int source = assignment[node];
    const double source_score =
        compatibility(input, neighbor_part_weights, incidence, net_part_counts,
                      net_unique_parts, net_sink_part_counts, net_sink_top1,
                      net_sink_top2, net_sink_top1_counts,
                      timing_path_local_penalty, timing_path_rescue, assignment,
                      node, source);
    ++compatibility_evaluations;
    for (int target = 0; target < input.parts; ++target) {
      if (target == source ||
          input.distances[source][target] > input.move_distance) {
        cached_gain_valid[node][target] = false;
        continue;
      }
      const double target_score = compatibility(
          input, neighbor_part_weights, incidence, net_part_counts,
          net_unique_parts, net_sink_part_counts, net_sink_top1, net_sink_top2,
          net_sink_top1_counts, timing_path_local_penalty, timing_path_rescue,
          assignment, node, target);
      ++compatibility_evaluations;
      if (!std::isfinite(target_score)) {
        cached_gain_valid[node][target] = false;
        continue;
      }
      const double gain = target_score - source_score;
      const long long rank = gain_rank(gain);
      cached_gains[node][target] = gain;
      cached_gain_ranks[node][target] = rank;
      cached_gain_valid[node][target] = true;
    }
    const auto [best_target, best_gain, best_rank] =
        select_cached_best(node);
    publish_best(node, best_target, best_gain, best_rank);
  };

  auto refresh_capacity_candidate = [&](int node, int part,
                                        bool publish) {
    const bool old_fit = fit_cache[node][part];
    const bool new_fit = target_fits(input, loads, node, part);
    if (old_fit == new_fit) {
      return;
    }
    fit_cache[node][part] = new_fit;
    if (!publish || locked[node] || input.nodes[node].fixed_part >= 0) {
      return;
    }
    if (new_fit) {
      if (cached_gain_valid[node][part]) {
        const long long rank = cached_gain_ranks[node][part];
        if (rank > best_gain_ranks[node] ||
            (rank == best_gain_ranks[node] && part < best_targets[node])) {
          publish_best(node, part, cached_gains[node][part], rank);
        }
      }
    } else if (best_targets[node] == part) {
      const auto [target, gain, rank] = select_cached_best(node);
      publish_best(node, target, gain, rank);
    }
  };
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    recompute_candidate(node);
  }

  double cumulative = 0.0;
  double best_cumulative = 0.0;
  int best_prefix = 0;
  int ineffective = 0;
  while (ineffective < input.early_stop) {
    while (!queue.empty() &&
           (locked[queue.top().node] ||
            queue.top().version != versions[queue.top().node])) {
      queue.pop();
    }
    if (queue.empty()) {
      break;
    }
    const CandidateMove best = queue.top();
    queue.pop();
    const int best_node = best.node;
    const int best_target = best.target;
    const double best_gain = best.gain;
    const int source = assignment[best_node];
    std::vector<long long> old_source_remaining(input.dimensions);
    std::vector<long long> old_target_remaining(input.dimensions);
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      old_source_remaining[dimension] =
          input.capacities[source][dimension] - loads[source][dimension];
      old_target_remaining[dimension] =
          input.capacities[best_target][dimension] -
          loads[best_target][dimension];
    }
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      loads[source][dimension] -= input.nodes[best_node].weights[dimension];
      loads[best_target][dimension] += input.nodes[best_node].weights[dimension];
    }
    std::set<int> timing_path_affected;
    for (const int path_index : timing_path_incidence[best_node]) {
      update_timing_path_contribution(path_index, -1.0,
                                      &timing_path_affected);
    }
    assignment[best_node] = best_target;
    for (const auto& [neighbor, weight] : adjacency[best_node]) {
      neighbor_part_weights[neighbor][source] -= weight;
      neighbor_part_weights[neighbor][best_target] += weight;
    }
    for (const int net_index : incidence[best_node]) {
      const Net& net = input.nets[net_index];
      --net_part_counts[net_index][source];
      if (net_part_counts[net_index][source] == 0) {
        --net_unique_parts[net_index];
      }
      if (net_part_counts[net_index][best_target] == 0) {
        ++net_unique_parts[net_index];
      }
      ++net_part_counts[net_index][best_target];
      if (net.source != best_node) {
        --net_sink_part_counts[net_index][source];
        ++net_sink_part_counts[net_index][best_target];
        rebuild_sink_distance_summary(net_index);
      }
    }
    for (const int path_index : timing_path_incidence[best_node]) {
      auto& counts = timing_path_part_counts[path_index];
      auto& xors = timing_path_part_xor[path_index];
      --counts[source];
      xors[source] ^= best_node;
      if (counts[source] == 0) --timing_path_unique_parts[path_index];
      if (counts[best_target] == 0) ++timing_path_unique_parts[path_index];
      ++counts[best_target];
      xors[best_target] ^= best_node;
      update_timing_path_contribution(path_index, 1.0,
                                      &timing_path_affected);
    }
    locked[best_node] = true;
    ++versions[best_node];
    cumulative += best_gain;
    moves.push_back({best_node, source, best_target, best_gain, cumulative});
    if (cumulative > best_cumulative) {
      best_cumulative = cumulative;
      best_prefix = static_cast<int>(moves.size());
      ineffective = 0;
    } else {
      ++ineffective;
    }

    std::set<int> affected;
    affected.insert(timing_path_affected.begin(), timing_path_affected.end());
    for (const auto& [neighbor, unused_weight] : adjacency[best_node]) {
      (void)unused_weight;
      affected.insert(neighbor);
    }
    for (const int net_index : incidence[best_node]) {
      const Net& net = input.nets[net_index];
      affected.insert(net.source);
      affected.insert(net.sinks.begin(), net.sinks.end());
    }
    std::set<int> source_fit_nodes;
    std::set<int> target_fit_nodes;
    auto invalidate_capacity_interval = [&](int dimension, long long low,
                                            long long high,
                                            std::set<int>& fit_nodes) {
      if (low >= high) {
        return;
      }
      const auto begin = std::upper_bound(
          weight_index[dimension].begin(), weight_index[dimension].end(),
          std::pair<long long, int>{low, std::numeric_limits<int>::max()});
      const auto end = std::upper_bound(
          weight_index[dimension].begin(), weight_index[dimension].end(),
          std::pair<long long, int>{high, std::numeric_limits<int>::max()});
      for (auto iterator = begin; iterator != end; ++iterator) {
        if (fit_nodes.insert(iterator->second).second) {
          ++capacity_invalidations;
        }
      }
    };
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      const long long new_source_remaining =
          input.capacities[source][dimension] - loads[source][dimension];
      const long long new_target_remaining =
          input.capacities[best_target][dimension] -
          loads[best_target][dimension];
      invalidate_capacity_interval(dimension, old_source_remaining[dimension],
                                   new_source_remaining, source_fit_nodes);
      invalidate_capacity_interval(dimension, new_target_remaining,
                                   old_target_remaining[dimension],
                                   target_fit_nodes);
    }
    affected.erase(best_node);
    for (const int node : source_fit_nodes) {
      refresh_capacity_candidate(node, source,
                                 affected.find(node) == affected.end());
    }
    for (const int node : target_fit_nodes) {
      refresh_capacity_candidate(node, best_target,
                                 affected.find(node) == affected.end());
    }
    for (const int node : affected) {
      recompute_candidate(node);
    }
  }
  for (int index = static_cast<int>(moves.size()) - 1; index >= best_prefix;
       --index) {
    const Move& move = moves[index];
    assignment[move.node] = move.source;
  }
  const Metrics final_metrics = compute_metrics(input, assignment);

  std::ofstream stream(output_path);
  if (!stream) {
    throw std::runtime_error("cannot open output: " + output_path);
  }
  stream << "EMUFLOW_MFSPART_REFINER_OUTPUT_V1\n";
  stream << "STATUS PASS\n";
  for (int index = 0; index < static_cast<int>(moves.size()); ++index) {
    const Move& move = moves[index];
    stream << "MOVE " << index << ' ' << move.node << ' ' << move.source << ' '
           << move.target << ' ' << std::setprecision(17) << move.gain << ' '
           << move.cumulative << ' ' << (index < best_prefix ? 1 : 0) << '\n';
  }
  for (int node = 0; node < static_cast<int>(assignment.size()); ++node) {
    stream << "FINAL " << node << ' ' << assignment[node] << '\n';
  }
  stream << "METRIC attempted_moves " << moves.size() << '\n';
  stream << "METRIC best_prefix " << best_prefix << '\n';
  stream << "METRIC best_cumulative_gain " << std::setprecision(17)
         << best_cumulative << '\n';
  stream << "METRIC candidate_recomputations " << candidate_recomputations
         << '\n';
  stream << "METRIC compatibility_evaluations "
         << compatibility_evaluations << '\n';
  stream << "METRIC capacity_invalidations " << capacity_invalidations
         << '\n';
  write_metrics(stream, "initial", initial_metrics);
  write_metrics(stream, "final", final_metrics);
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_mfspart_refiner INPUT OUTPUT\n";
    return 0;
  }
  if (argc != 3) {
    std::cerr << "usage: emuflow_mfspart_refiner INPUT OUTPUT\n";
    return 2;
  }
  try {
    run(read_input(argv[1]), argv[2]);
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
