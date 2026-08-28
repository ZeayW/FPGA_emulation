// SPDX-License-Identifier: Apache-2.0
//
// Independent scalable certificate checker for the topology-bottleneck
// MFSPart direct k-way FM extension.
// The optimizer uses versioned candidate heaps and capacity-threshold
// invalidation.  This checker deliberately uses a dynamic multidimensional
// range-maximum tree: capacity changes alter query bounds and never scan all
// nodes crossing a feasibility threshold.

#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

constexpr double kGainRankScale = 1'000'000'000.0;

long long gain_rank(double value) {
  return std::llround(value * kGainRankScale);
}

bool close_number(double left, double right) {
  if (!std::isfinite(left) || !std::isfinite(right)) return false;
  const double scale = std::max(std::abs(left), std::abs(right));
  return std::abs(left - right) <= 1e-12 + 1e-12 * scale;
}

struct Node {
  int fixed_part = -1;
  std::vector<long long> weights;
};

struct Net {
  double weight = 0.0;
  double bottleneck_weight = 0.0;
  int max_distance_limit = -1;
  int source = -1;
  std::vector<int> sinks;
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
  std::vector<std::vector<int>> distances;
  std::vector<std::vector<long long>> capacities;
  std::vector<Node> nodes;
  std::vector<Net> nets;
  std::vector<int> assignment;
};

struct Move {
  int node = -1;
  int source = -1;
  int target = -1;
  double gain = 0.0;
  double cumulative = 0.0;
  bool kept = false;
};

struct Output {
  std::vector<Move> moves;
  std::vector<int> assignment;
  std::map<std::string, double> metrics;
};

struct Metrics {
  double driver_sink_cut = 0.0;
  double connectivity = 0.0;
  double weighted_hops = 0.0;
  double mean_hops = 0.0;
  long long violating_pairs = 0;
  long long capacity_violations = 0;
  long long fixed_violations = 0;
  long long topology_guard_violations = 0;
};

template <typename Values>
bool any_missing(const Values& values) {
  return std::any_of(values.begin(), values.end(),
                     [](bool value) { return !value; });
}

Input read_input(const std::string& path) {
  std::ifstream stream(path);
  if (!stream) throw std::runtime_error("cannot open checker input");
  std::string magic;
  std::getline(stream, magic);
  const bool input_v3 = magic == "EMUFLOW_MFSPART_REFINER_INPUT_V3";
  const bool input_v2 =
      input_v3 || magic == "EMUFLOW_MFSPART_REFINER_INPUT_V2";
  if (!input_v2 && magic != "EMUFLOW_MFSPART_REFINER_INPUT_V1") {
    throw std::runtime_error("unsupported checker input header");
  }
  Input input;
  int node_count = -1;
  int net_count = -1;
  bool saw_param = false;
  std::vector<std::vector<bool>> saw_distance;
  std::vector<std::vector<bool>> saw_capacity;
  std::vector<bool> saw_node, saw_net, saw_assignment;
  std::string kind;
  while (stream >> kind) {
    if (kind == "PARAM") {
      if (saw_param) throw std::runtime_error("duplicate PARAM");
      stream >> input.parts >> node_count >> input.dimensions >> net_count >>
          input.hmax >> input.move_distance >> input.early_stop >> input.gamma >>
          input.lambda >> input.mu;
      if (input_v2) stream >> input.bottleneck_beta;
      if (input.parts <= 0 || node_count <= 0 || input.dimensions <= 0 ||
          net_count < 0 || input.hmax < 1 || input.move_distance < 1 ||
          input.early_stop < 1 || !std::isfinite(input.gamma) ||
          !std::isfinite(input.lambda) || !std::isfinite(input.mu) ||
          !std::isfinite(input.bottleneck_beta) || input.gamma < 0.0 ||
          input.lambda < 0.0 || input.mu < 0.0 ||
          input.bottleneck_beta < 0.0) {
        throw std::runtime_error("invalid PARAM");
      }
      input.distances.assign(input.parts, std::vector<int>(input.parts, -1));
      input.capacities.assign(
          input.parts, std::vector<long long>(input.dimensions, 0));
      input.nodes.assign(node_count, Node{});
      input.nets.assign(net_count, Net{});
      input.assignment.assign(node_count, -1);
      saw_distance.assign(input.parts, std::vector<bool>(input.parts, false));
      saw_capacity.assign(input.parts,
                          std::vector<bool>(input.dimensions, false));
      saw_node.assign(node_count, false);
      saw_net.assign(net_count, false);
      saw_assignment.assign(node_count, false);
      saw_param = true;
    } else if (kind == "DIST") {
      int source = -1, target = -1, distance = -1;
      stream >> source >> target >> distance;
      if (!saw_param || source < 0 || source >= input.parts || target < 0 ||
          target >= input.parts || distance < 0 ||
          saw_distance[source][target]) {
        throw std::runtime_error("invalid DIST");
      }
      input.distances[source][target] = distance;
      saw_distance[source][target] = true;
    } else if (kind == "CAP") {
      int part = -1, dimension = -1;
      long long capacity = -1;
      stream >> part >> dimension >> capacity;
      if (!saw_param || part < 0 || part >= input.parts || dimension < 0 ||
          dimension >= input.dimensions || capacity <= 0 ||
          saw_capacity[part][dimension]) {
        throw std::runtime_error("invalid CAP");
      }
      input.capacities[part][dimension] = capacity;
      saw_capacity[part][dimension] = true;
    } else if (kind == "NODE") {
      int index = -1;
      Node node;
      stream >> index >> node.fixed_part;
      node.weights.resize(input.dimensions);
      for (long long& weight : node.weights) stream >> weight;
      if (!saw_param || index < 0 || index >= node_count || saw_node[index] ||
          node.fixed_part < -1 || node.fixed_part >= input.parts ||
          node.weights.front() <= 0 ||
          std::any_of(node.weights.begin(), node.weights.end(),
                      [](long long value) { return value < 0; })) {
        throw std::runtime_error("invalid NODE");
      }
      input.nodes[index] = std::move(node);
      saw_node[index] = true;
    } else if (kind == "NET") {
      int index = -1, sink_count = -1;
      Net net;
      stream >> index >> net.weight;
      if (input_v3) {
        stream >> net.bottleneck_weight >> net.max_distance_limit;
      } else {
        net.bottleneck_weight = net.weight;
      }
      stream >> net.source >> sink_count;
      if (!saw_param || index < 0 || index >= net_count || saw_net[index] ||
          !std::isfinite(net.weight) || net.weight <= 0.0 ||
          !std::isfinite(net.bottleneck_weight) ||
          net.bottleneck_weight < 0.0 || net.max_distance_limit < -1 ||
          net.source < 0 ||
          net.source >= node_count || sink_count <= 0) {
        throw std::runtime_error("invalid NET");
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
      input.nets[index] = std::move(net);
      saw_net[index] = true;
    } else if (kind == "ASSIGN") {
      int node = -1, part = -1;
      stream >> node >> part;
      if (!saw_param || node < 0 || node >= node_count || part < 0 ||
          part >= input.parts || saw_assignment[node]) {
        throw std::runtime_error("invalid ASSIGN");
      }
      input.assignment[node] = part;
      saw_assignment[node] = true;
    } else {
      throw std::runtime_error("unknown checker input record");
    }
    if (!stream) throw std::runtime_error("malformed checker input");
  }
  if (!saw_param || any_missing(saw_node) || any_missing(saw_net) ||
      any_missing(saw_assignment)) {
    throw std::runtime_error("incomplete checker input");
  }
  for (int part = 0; part < input.parts; ++part) {
    if (any_missing(saw_distance[part]) || any_missing(saw_capacity[part]) ||
        input.distances[part][part] != 0) {
      throw std::runtime_error("incomplete checker topology/capacity");
    }
    for (int other = 0; other < input.parts; ++other) {
      if (input.distances[part][other] != input.distances[other][part]) {
        throw std::runtime_error("checker requires symmetric distances");
      }
    }
  }
  return input;
}

Output read_output(const std::string& path, int node_count) {
  std::ifstream stream(path);
  if (!stream) throw std::runtime_error("cannot open refiner output");
  std::string magic;
  std::getline(stream, magic);
  if (magic != "EMUFLOW_MFSPART_REFINER_OUTPUT_V1") {
    throw std::runtime_error("unsupported refiner output header");
  }
  Output output;
  output.assignment.assign(node_count, -1);
  std::vector<bool> saw_final(node_count, false);
  bool saw_status = false;
  std::string kind;
  while (stream >> kind) {
    if (kind == "STATUS") {
      std::string status;
      stream >> status;
      if (saw_status || status != "PASS") {
        throw std::runtime_error("invalid refiner status");
      }
      saw_status = true;
    } else if (kind == "MOVE") {
      int index = -1, kept = -1;
      Move move;
      stream >> index >> move.node >> move.source >> move.target >> move.gain >>
          move.cumulative >> kept;
      if (index != static_cast<int>(output.moves.size()) || move.node < 0 ||
          move.node >= node_count || kept < 0 || kept > 1 ||
          !std::isfinite(move.gain) || !std::isfinite(move.cumulative)) {
        throw std::runtime_error("invalid MOVE");
      }
      move.kept = kept != 0;
      output.moves.push_back(move);
    } else if (kind == "FINAL") {
      int node = -1, part = -1;
      stream >> node >> part;
      if (node < 0 || node >= node_count || saw_final[node]) {
        throw std::runtime_error("invalid FINAL");
      }
      saw_final[node] = true;
      output.assignment[node] = part;
    } else if (kind == "METRIC") {
      std::string name;
      double value = 0.0;
      stream >> name >> value;
      if (!std::isfinite(value) || !output.metrics.emplace(name, value).second) {
        throw std::runtime_error("invalid METRIC");
      }
    } else {
      throw std::runtime_error("unknown refiner output record");
    }
    if (!stream) throw std::runtime_error("malformed refiner output");
  }
  if (!saw_status || any_missing(saw_final)) {
    throw std::runtime_error("incomplete refiner output");
  }
  return output;
}

std::vector<std::vector<std::pair<int, double>>> adjacency(const Input& input) {
  std::vector<std::vector<std::pair<int, double>>> result(input.nodes.size());
  for (const Net& net : input.nets) {
    for (int sink : net.sinks) {
      result[net.source].push_back({sink, net.weight});
      result[sink].push_back({net.source, net.weight});
    }
  }
  return result;
}

std::vector<std::vector<int>> incidence(const Input& input) {
  std::vector<std::vector<int>> result(input.nodes.size());
  for (int index = 0; index < static_cast<int>(input.nets.size()); ++index) {
    result[input.nets[index].source].push_back(index);
    for (int sink : input.nets[index].sinks) result[sink].push_back(index);
  }
  return result;
}

std::vector<std::vector<long long>> loads(
    const Input& input, const std::vector<int>& assignment) {
  std::vector<std::vector<long long>> result(
      input.parts, std::vector<long long>(input.dimensions, 0));
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      result[assignment[node]][dimension] += input.nodes[node].weights[dimension];
    }
  }
  return result;
}

Metrics metrics(const Input& input, const std::vector<int>& assignment) {
  Metrics result;
  double total_pair_weight = 0.0;
  for (const Net& net : input.nets) {
    std::set<int> remote_parts;
    int maximum_distance = 0;
    for (int sink : net.sinks) {
      const int source_part = assignment[net.source];
      const int sink_part = assignment[sink];
      const int distance = input.distances[source_part][sink_part];
      if (source_part != sink_part) {
        result.driver_sink_cut += net.weight;
        remote_parts.insert(sink_part);
      }
      result.violating_pairs += distance > input.hmax;
      result.weighted_hops += net.weight * distance;
      maximum_distance = std::max(maximum_distance, distance);
      total_pair_weight += net.weight;
    }
    if (net.max_distance_limit >= 0 &&
        maximum_distance > net.max_distance_limit) {
      ++result.topology_guard_violations;
    }
    result.connectivity += net.weight * remote_parts.size();
  }
  result.mean_hops = total_pair_weight == 0.0
                         ? 0.0
                         : result.weighted_hops / total_pair_weight;
  const auto current_loads = loads(input, assignment);
  for (int part = 0; part < input.parts; ++part) {
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      result.capacity_violations +=
          current_loads[part][dimension] > input.capacities[part][dimension];
    }
  }
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    result.fixed_violations += input.nodes[node].fixed_part >= 0 &&
                               assignment[node] != input.nodes[node].fixed_part;
  }
  return result;
}

struct Candidate {
  long long rank = std::numeric_limits<long long>::min();
  int node = -1;
  int target = -1;
  double gain = -std::numeric_limits<double>::infinity();
};

bool better(const Candidate& left, const Candidate& right) {
  if (left.node < 0) return false;
  if (right.node < 0) return true;
  if (left.rank != right.rank) return left.rank > right.rank;
  if (left.node != right.node) return left.node < right.node;
  return left.target < right.target;
}

Candidate maximum(Candidate left, const Candidate& right) {
  if (better(right, left)) left = right;
  return left;
}

class OrthantMaximumTree {
 public:
  OrthantMaximumTree(const Input& input, std::vector<int> points)
      : input_(input), point_to_tree_(input.nodes.size(), -1) {
    nodes_.reserve(points.size());
    root_ = build(points, 0, static_cast<int>(points.size()), 0, -1);
    for (int index = static_cast<int>(nodes_.size()) - 1; index >= 0; --index) {
      recompute_all(index);
    }
  }

  void set_point(int point, const std::vector<Candidate>& candidates) {
    int index = point_to_tree_.at(point);
    if (index < 0 || static_cast<int>(candidates.size()) != input_.parts) {
      throw std::runtime_error("checker tree point update mismatch");
    }
    nodes_[index].own = candidates;
    while (index >= 0) {
      recompute_all(index);
      index = nodes_[index].parent;
    }
  }

  Candidate query(int target, const std::vector<long long>& bounds) {
    return query_node(root_, target, bounds);
  }

  long long nodes_visited() const { return nodes_visited_; }

 private:
  struct TreeNode {
    int point = -1;
    int left = -1;
    int right = -1;
    int parent = -1;
    std::vector<long long> minimum;
    std::vector<long long> maximum;
    std::vector<Candidate> own;
    std::vector<Candidate> best;
  };

  int build(std::vector<int>& points, int begin, int end, int depth,
            int parent) {
    if (begin >= end) return -1;
    const int axis = depth % input_.dimensions;
    const int middle = begin + (end - begin) / 2;
    std::nth_element(
        points.begin() + begin, points.begin() + middle, points.begin() + end,
        [&](int left, int right) {
          return std::tie(input_.nodes[left].weights[axis], left) <
                 std::tie(input_.nodes[right].weights[axis], right);
        });
    const int index = static_cast<int>(nodes_.size());
    nodes_.push_back(TreeNode{});
    nodes_[index].point = points[middle];
    nodes_[index].parent = parent;
    nodes_[index].minimum = input_.nodes[points[middle]].weights;
    nodes_[index].maximum = input_.nodes[points[middle]].weights;
    nodes_[index].own.assign(input_.parts, Candidate{});
    nodes_[index].best.assign(input_.parts, Candidate{});
    point_to_tree_[points[middle]] = index;
    nodes_[index].left = build(points, begin, middle, depth + 1, index);
    nodes_[index].right = build(points, middle + 1, end, depth + 1, index);
    for (int child : {nodes_[index].left, nodes_[index].right}) {
      if (child < 0) continue;
      for (int dimension = 0; dimension < input_.dimensions; ++dimension) {
        nodes_[index].minimum[dimension] = std::min(
            nodes_[index].minimum[dimension], nodes_[child].minimum[dimension]);
        nodes_[index].maximum[dimension] = std::max(
            nodes_[index].maximum[dimension], nodes_[child].maximum[dimension]);
      }
    }
    return index;
  }

  void recompute_all(int index) {
    for (int target = 0; target < input_.parts; ++target) {
      Candidate value = nodes_[index].own[target];
      if (nodes_[index].left >= 0) {
        value = maximum(value, nodes_[nodes_[index].left].best[target]);
      }
      if (nodes_[index].right >= 0) {
        value = maximum(value, nodes_[nodes_[index].right].best[target]);
      }
      nodes_[index].best[target] = value;
    }
  }

  Candidate query_node(int index, int target,
                       const std::vector<long long>& bounds) {
    if (index < 0) return Candidate{};
    ++nodes_visited_;
    bool wholly_inside = true;
    for (int dimension = 0; dimension < input_.dimensions; ++dimension) {
      if (nodes_[index].minimum[dimension] > bounds[dimension]) {
        return Candidate{};
      }
      wholly_inside &= nodes_[index].maximum[dimension] <= bounds[dimension];
    }
    if (wholly_inside) return nodes_[index].best[target];
    Candidate result;
    const Node& point = input_.nodes[nodes_[index].point];
    bool fits = true;
    for (int dimension = 0; dimension < input_.dimensions; ++dimension) {
      fits &= point.weights[dimension] <= bounds[dimension];
    }
    if (fits) result = nodes_[index].own[target];
    result = maximum(
        result, query_node(nodes_[index].left, target, bounds));
    return maximum(
        result, query_node(nodes_[index].right, target, bounds));
  }

  const Input& input_;
  std::vector<TreeNode> nodes_;
  std::vector<int> point_to_tree_;
  int root_ = -1;
  long long nodes_visited_ = 0;
};

void require_metric(const Output& output, const std::string& name,
                    double expected) {
  const auto found = output.metrics.find(name);
  if (found == output.metrics.end() || !close_number(found->second, expected)) {
    throw std::runtime_error("refiner metric mismatch for " + name);
  }
}

void require_partition_metrics(const Output& output, const std::string& prefix,
                               const Metrics& expected) {
  require_metric(output, prefix + "_driver_sink_cut",
                 expected.driver_sink_cut);
  require_metric(output, prefix + "_connectivity", expected.connectivity);
  require_metric(output, prefix + "_weighted_hops", expected.weighted_hops);
  require_metric(output, prefix + "_mean_hops", expected.mean_hops);
  require_metric(output, prefix + "_violating_pairs", expected.violating_pairs);
  require_metric(output, prefix + "_capacity_violations",
                 expected.capacity_violations);
  require_metric(output, prefix + "_fixed_violations",
                 expected.fixed_violations);
  require_metric(output, prefix + "_topology_guard_violations",
                 expected.topology_guard_violations);
}

void check(const Input& input, const Output& output,
           const std::string& report_path) {
  std::vector<int> assignment = input.assignment;
  auto current_loads = loads(input, assignment);
  const Metrics initial_metrics = metrics(input, assignment);
  if (initial_metrics.capacity_violations || initial_metrics.fixed_violations ||
      initial_metrics.topology_guard_violations) {
    throw std::runtime_error("checker initial assignment is illegal");
  }
  const auto pair_adjacency = adjacency(input);
  const auto node_incidence = incidence(input);
  std::vector<std::vector<double>> neighbor_part_weights(
      input.nodes.size(), std::vector<double>(input.parts, 0.0));
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    for (const auto& [neighbor, weight] : pair_adjacency[node]) {
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
  std::vector<std::vector<int>> net_pins(input.nets.size());
  for (int net_index = 0; net_index < static_cast<int>(input.nets.size());
       ++net_index) {
    const Net& net = input.nets[net_index];
    net_pins[net_index] = {net.source};
    ++net_part_counts[net_index][assignment[net.source]];
    for (int sink : net.sinks) {
      net_pins[net_index].push_back(sink);
      ++net_part_counts[net_index][assignment[sink]];
      ++net_sink_part_counts[net_index][assignment[sink]];
    }
    net_unique_parts[net_index] = std::count_if(
        net_part_counts[net_index].begin(), net_part_counts[net_index].end(),
        [](int count) { return count > 0; });
  }
  auto rebuild_sink_distance_summary = [&](int net_index) {
    for (int driver_part = 0; driver_part < input.parts; ++driver_part) {
      int first = 0;
      int second = 0;
      int first_count = 0;
      for (int sink_part = 0; sink_part < input.parts; ++sink_part) {
        if (net_sink_part_counts[net_index][sink_part] == 0) continue;
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
  std::vector<bool> locked(input.nodes.size(), false);
  long long objective_recomputations = 0;

  auto compatibility = [&](int node, int candidate) {
    double hop = 0.0;
    double violation = 0.0;
    for (int neighbor_part = 0; neighbor_part < input.parts; ++neighbor_part) {
      const double weight = neighbor_part_weights[node][neighbor_part];
      if (weight == 0.0) continue;
      const int distance = input.distances[neighbor_part][candidate];
      if (distance <= input.hmax) {
        hop += (input.hmax - distance) * weight;
      } else {
        violation +=
            weight * (1.0 + input.mu * (distance - input.hmax));
      }
    }
    double connectivity = 0.0;
    double bottleneck_hops = 0.0;
    const int source = assignment[node];
    for (int net_index : node_incidence[node]) {
      int spanned = net_unique_parts[net_index];
      if (candidate != source) {
        spanned += net_part_counts[net_index][candidate] == 0;
        spanned -= net_part_counts[net_index][source] == 1;
      }
      connectivity += input.nets[net_index].weight * spanned;
      const Net& net = input.nets[net_index];
      int maximum_distance = 0;
      if (net.source == node) {
        maximum_distance = net_sink_top1[net_index][candidate];
      } else {
        const int driver_part = assignment[net.source];
        maximum_distance = net_sink_top1[net_index][driver_part];
        if (candidate != source &&
            net_sink_part_counts[net_index][source] == 1 &&
            input.distances[driver_part][source] == maximum_distance &&
            net_sink_top1_counts[net_index][driver_part] == 1) {
          maximum_distance = net_sink_top2[net_index][driver_part];
        }
        maximum_distance = std::max(
            maximum_distance, input.distances[driver_part][candidate]);
      }
      if (net.max_distance_limit >= 0 &&
          maximum_distance > net.max_distance_limit) {
        return -std::numeric_limits<double>::infinity();
      }
      bottleneck_hops += net.bottleneck_weight * maximum_distance;
    }
    return hop - input.gamma * connectivity - input.lambda * violation -
           input.bottleneck_beta * bottleneck_hops;
  };

  std::vector<int> points(input.nodes.size());
  for (int node = 0; node < static_cast<int>(points.size()); ++node) {
    points[node] = node;
  }
  OrthantMaximumTree tree(input, points);
  auto candidates_for = [&](int node) {
    std::vector<Candidate> result(input.parts);
    if (locked[node] || input.nodes[node].fixed_part >= 0) return result;
    const int source = assignment[node];
    const double source_score = compatibility(node, source);
    ++objective_recomputations;
    for (int target = 0; target < input.parts; ++target) {
      if (target == source ||
          input.distances[source][target] > input.move_distance) {
        continue;
      }
      const double target_score = compatibility(node, target);
      ++objective_recomputations;
      if (!std::isfinite(target_score)) {
        continue;
      }
      const double gain = target_score - source_score;
      result[target] = {gain_rank(gain), node, target, gain};
    }
    return result;
  };
  for (int node = 0; node < static_cast<int>(input.nodes.size()); ++node) {
    tree.set_point(node, candidates_for(node));
  }

  auto global_best = [&]() {
    Candidate result;
    for (int target = 0; target < input.parts; ++target) {
      std::vector<long long> remaining(input.dimensions);
      for (int dimension = 0; dimension < input.dimensions; ++dimension) {
        remaining[dimension] = input.capacities[target][dimension] -
                               current_loads[target][dimension];
      }
      result = maximum(result, tree.query(target, remaining));
    }
    return result;
  };

  double cumulative = 0.0;
  double best_cumulative = 0.0;
  int best_prefix = 0;
  int ineffective = 0;
  std::vector<int> marks(input.nodes.size(), 0);
  int epoch = 0;
  for (int index = 0; index < static_cast<int>(output.moves.size()); ++index) {
    if (ineffective >= input.early_stop) {
      throw std::runtime_error("refiner moved after early-stop");
    }
    const Candidate expected = global_best();
    if (expected.node < 0) {
      throw std::runtime_error("refiner moved without a legal candidate");
    }
    const Move& actual = output.moves[index];
    if (actual.node != expected.node || actual.target != expected.target ||
        actual.source != assignment[expected.node] ||
        !close_number(actual.gain, expected.gain)) {
      throw std::runtime_error("refiner global-best certificate mismatch");
    }
    cumulative += actual.gain;
    if (!close_number(actual.cumulative, cumulative)) {
      throw std::runtime_error("refiner cumulative gain mismatch");
    }
    if (cumulative > best_cumulative) {
      best_cumulative = cumulative;
      best_prefix = index + 1;
      ineffective = 0;
    } else {
      ++ineffective;
    }
    const int node = actual.node;
    const int source = actual.source;
    const int target = actual.target;
    for (int dimension = 0; dimension < input.dimensions; ++dimension) {
      current_loads[source][dimension] -= input.nodes[node].weights[dimension];
      current_loads[target][dimension] += input.nodes[node].weights[dimension];
      if (current_loads[target][dimension] >
          input.capacities[target][dimension]) {
        throw std::runtime_error("refiner move exceeds capacity");
      }
    }
    assignment[node] = target;
    for (const auto& [neighbor, weight] : pair_adjacency[node]) {
      neighbor_part_weights[neighbor][source] -= weight;
      neighbor_part_weights[neighbor][target] += weight;
    }
    for (int net_index : node_incidence[node]) {
      const Net& net = input.nets[net_index];
      --net_part_counts[net_index][source];
      if (net_part_counts[net_index][source] == 0) --net_unique_parts[net_index];
      if (net_part_counts[net_index][target] == 0) ++net_unique_parts[net_index];
      ++net_part_counts[net_index][target];
      if (net.source != node) {
        --net_sink_part_counts[net_index][source];
        ++net_sink_part_counts[net_index][target];
        rebuild_sink_distance_summary(net_index);
      }
    }
    locked[node] = true;
    tree.set_point(node, candidates_for(node));
    ++epoch;
    std::vector<int> affected;
    auto mark = [&](int candidate) {
      if (candidate != node && marks[candidate] != epoch) {
        marks[candidate] = epoch;
        affected.push_back(candidate);
      }
    };
    for (const auto& [neighbor, unused] : pair_adjacency[node]) {
      (void)unused;
      mark(neighbor);
    }
    for (int net_index : node_incidence[node]) {
      for (int pin : net_pins[net_index]) mark(pin);
    }
    std::sort(affected.begin(), affected.end());
    for (int candidate : affected) {
      tree.set_point(candidate, candidates_for(candidate));
    }
  }
  if (ineffective < input.early_stop && global_best().node >= 0) {
    throw std::runtime_error("refiner stopped before exhausting legal moves");
  }
  for (int index = 0; index < static_cast<int>(output.moves.size()); ++index) {
    if (output.moves[index].kept != (index < best_prefix)) {
      throw std::runtime_error("refiner best-prefix marker mismatch");
    }
  }
  std::vector<int> final_assignment = input.assignment;
  for (int index = 0; index < best_prefix; ++index) {
    final_assignment[output.moves[index].node] = output.moves[index].target;
  }
  if (output.assignment != final_assignment) {
    throw std::runtime_error("refiner rollback assignment mismatch");
  }
  const Metrics final_metrics = metrics(input, final_assignment);
  require_metric(output, "attempted_moves", output.moves.size());
  require_metric(output, "best_prefix", best_prefix);
  require_metric(output, "best_cumulative_gain", best_cumulative);
  require_partition_metrics(output, "initial", initial_metrics);
  require_partition_metrics(output, "final", final_metrics);

  std::ofstream report(report_path);
  if (!report) throw std::runtime_error("cannot open checker report");
  report << "EMUFLOW_MFSPART_REFINER_CHECK_OUTPUT_V1\n";
  report << "STATUS PASS\n";
  report << "METRIC attempted_moves " << output.moves.size() << '\n';
  report << "METRIC kept_moves " << best_prefix << '\n';
  report << "METRIC objective_recomputations " << objective_recomputations
         << '\n';
  report << "METRIC orthant_tree_nodes_visited " << tree.nodes_visited()
         << '\n';
  report << "METRIC best_cumulative_gain " << std::setprecision(17)
         << best_cumulative << '\n';
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_mfspart_refiner_checker INPUT REFINER_OUTPUT "
                 "CHECK_OUTPUT\n";
    return 0;
  }
  if (argc != 4) {
    std::cerr << "usage: emuflow_mfspart_refiner_checker INPUT REFINER_OUTPUT "
                 "CHECK_OUTPUT\n";
    return 2;
  }
  try {
    const Input input = read_input(argv[1]);
    check(input, read_output(argv[2], input.nodes.size()), argv[3]);
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
  return 0;
}
