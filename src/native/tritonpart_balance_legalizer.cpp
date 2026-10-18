// SPDX-License-Identifier: Apache-2.0
// Native deterministic legalization for best-effort TritonPart assignments.

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

using Weight = long long;

struct Edge {
  double weight = 0.0;
  bool combinational = false;
  std::vector<int> vertices;
};

struct Problem {
  int parts = 0;
  int dimensions = 0;
  std::vector<std::vector<Weight>> allowed;
  std::vector<std::vector<Weight>> vertex_weights;
  std::vector<int> fixed;
  std::vector<int> assignment;
  std::vector<Edge> edges;
  std::vector<std::vector<int>> incident;
};

struct ChainTemplateMove {
  std::vector<Weight> weights;
  int source = -1;
  int target = -1;
};

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

Problem read_problem(const std::string& hgr_path,
                     const std::string& fixed_path,
                     const std::string& classes_path,
                     const std::string& limits_path,
                     const std::string& solution_path) {
  Problem problem;
  {
    std::ifstream input(limits_path);
    require(input.good(), "cannot open balance limits");
    std::string token;
    input >> token;
    require(token == "EMUFLOW_TRITONPART_BALANCE_LIMITS_V1",
            "invalid balance limits header");
    input >> token >> problem.parts >> problem.dimensions;
    require(token == "PARAM" && problem.parts > 1 && problem.dimensions > 0,
            "invalid balance limits PARAM");
    problem.allowed.assign(
        problem.parts, std::vector<Weight>(problem.dimensions, -1));
    for (int part = 0; part < problem.parts; ++part) {
      int index = -1;
      input >> token >> index;
      require(token == "CAP" && index == part,
              "invalid balance limits CAP");
      for (Weight& value : problem.allowed[part]) input >> value;
      require(input.good()
                  && std::all_of(problem.allowed[part].begin(),
                                 problem.allowed[part].end(),
                                 [](Weight value) { return value >= 0; }),
              "invalid balance capacity");
    }
    input >> token;
    require(input.good() && token == "END", "incomplete balance limits");
  }

  int edge_count = 0;
  int vertex_count = 0;
  {
    std::ifstream input(hgr_path);
    require(input.good(), "cannot open TritonPart hypergraph");
    std::string line;
    require(static_cast<bool>(std::getline(input, line)), "empty hypergraph");
    std::istringstream header(line);
    int format = 0;
    header >> edge_count >> vertex_count >> format;
    require(header.good() || header.eof(), "malformed hypergraph header");
    require(edge_count > 0 && vertex_count > 0 && format == 11,
            "balance legalizer requires weighted hMetis format 11");
    problem.edges.resize(edge_count);
    problem.incident.resize(vertex_count);
    for (int edge_index = 0; edge_index < edge_count; ++edge_index) {
      require(static_cast<bool>(std::getline(input, line)),
              "truncated hypergraph edges");
      std::istringstream record(line);
      Edge& edge = problem.edges[edge_index];
      record >> edge.weight;
      require(record.good() && std::isfinite(edge.weight),
              "invalid hyperedge weight");
      int one_based = 0;
      while (record >> one_based) {
        require(one_based > 0 && one_based <= vertex_count,
                "invalid hyperedge vertex");
        const int vertex = one_based - 1;
        edge.vertices.push_back(vertex);
        problem.incident[vertex].push_back(edge_index);
      }
      require(edge.vertices.size() >= 2, "degenerate hyperedge");
    }
    problem.vertex_weights.assign(
        vertex_count, std::vector<Weight>(problem.dimensions, 0));
    for (int vertex = 0; vertex < vertex_count; ++vertex) {
      require(static_cast<bool>(std::getline(input, line)),
              "truncated hypergraph vertex weights");
      std::istringstream record(line);
      for (Weight& value : problem.vertex_weights[vertex]) record >> value;
      require((record.good() || record.eof())
                  && std::all_of(problem.vertex_weights[vertex].begin(),
                                 problem.vertex_weights[vertex].end(),
                                 [](Weight value) { return value >= 0; }),
              "invalid vertex weights");
      Weight extra = 0;
      require(!(record >> extra), "extra vertex-weight dimension");
    }
  }

  problem.fixed.resize(vertex_count, -1);
  problem.assignment.resize(vertex_count, -1);
  std::ifstream fixed(fixed_path);
  std::ifstream solution(solution_path);
  std::ifstream classes(classes_path);
  require(fixed.good() && solution.good() && classes.good(),
          "cannot open fixed, solution, or edge-class file");
  for (int vertex = 0; vertex < vertex_count; ++vertex) {
    fixed >> problem.fixed[vertex];
    solution >> problem.assignment[vertex];
    require(fixed.good() && solution.good()
                && problem.fixed[vertex] >= -1
                && problem.fixed[vertex] < problem.parts
                && problem.assignment[vertex] >= 0
                && problem.assignment[vertex] < problem.parts
                && (problem.fixed[vertex] < 0
                    || problem.fixed[vertex] == problem.assignment[vertex]),
            "invalid fixed or assignment record");
  }
  for (int edge = 0; edge < edge_count; ++edge) {
    int value = -1;
    classes >> value;
    require(classes.good() && (value == 0 || value == 1),
            "invalid edge-class record");
    problem.edges[edge].combinational = value == 1;
  }
  int extra = 0;
  require(!(fixed >> extra) && !(solution >> extra) && !(classes >> extra),
          "extra fixed, assignment, or edge-class record");
  return problem;
}

struct Legalizer {
  explicit Legalizer(Problem value)
      : problem(std::move(value)),
        labels(problem.assignment),
        loads(problem.parts,
              std::vector<Weight>(problem.dimensions, 0)),
        preferred_chain_weights(problem.parts),
        preferred_chain_targets(problem.parts, -1),
        preferred_chain_templates(problem.parts),
        edge_part_counts(problem.edges.size() * problem.parts, 0) {
    for (int vertex = 0; vertex < static_cast<int>(labels.size()); ++vertex) {
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        loads[labels[vertex]][dimension]
            += problem.vertex_weights[vertex][dimension];
      }
    }
    initial_loads = loads;
    for (int edge = 0; edge < static_cast<int>(problem.edges.size()); ++edge) {
      for (int vertex : problem.edges[edge].vertices) {
        ++edge_part_counts[edge * problem.parts + labels[vertex]];
      }
      ordinary_weight_sum += std::abs(problem.edges[edge].weight);
    }
    combinational_penalty = ordinary_weight_sum + 1.0;
  }

  Problem problem;
  std::vector<int> labels;
  std::vector<std::vector<Weight>> loads;
  std::vector<std::vector<Weight>> initial_loads;
  std::vector<std::vector<Weight>> preferred_chain_weights;
  std::vector<int> preferred_chain_targets;
  std::vector<std::vector<ChainTemplateMove>> preferred_chain_templates;
  std::vector<int> edge_part_counts;
  double ordinary_weight_sum = 0.0;
  double combinational_penalty = 0.0;
  long long direct_moves = 0;
  long long paired_sequences = 0;
  long long ejection_chain_sequences = 0;
  long long replayed_chain_sequences = 0;
  long long max_ejection_chain_moves = 0;
  long long moves = 0;
  std::chrono::steady_clock::time_point started =
      std::chrono::steady_clock::now();
  std::chrono::steady_clock::time_point last_progress = started;

  std::vector<Weight> overload(int part) const {
    std::vector<Weight> result(problem.dimensions, 0);
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      result[dimension] = std::max<Weight>(
          0, loads[part][dimension] - problem.allowed[part][dimension]);
    }
    return result;
  }

  double overload_score(int part,
                        const std::vector<Weight>* projected = nullptr) const {
    const auto& values = projected == nullptr ? loads[part] : *projected;
    double score = 0.0;
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      score += static_cast<double>(std::max<Weight>(
                   0, values[dimension] - problem.allowed[part][dimension]))
               / std::max<Weight>(1, problem.allowed[part][dimension]);
    }
    return score;
  }

  bool fits(int vertex, int part) const {
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      if (loads[part][dimension]
              + problem.vertex_weights[vertex][dimension]
          > problem.allowed[part][dimension]) return false;
    }
    return true;
  }

  double edge_delta(int vertex, int source, int target) const {
    double delta = 0.0;
    for (int edge_index : problem.incident[vertex]) {
      int before_parts = 0;
      for (int part = 0; part < problem.parts; ++part) {
        before_parts += edge_part_counts[edge_index * problem.parts + part] > 0;
      }
      int after_parts = before_parts;
      if (edge_part_counts[edge_index * problem.parts + source] == 1) --after_parts;
      if (edge_part_counts[edge_index * problem.parts + target] == 0) ++after_parts;
      const Edge& edge = problem.edges[edge_index];
      const double weight = edge.weight
                            + (edge.combinational ? combinational_penalty : 0.0);
      delta += (static_cast<int>(after_parts > 1)
                - static_cast<int>(before_parts > 1)) * weight;
    }
    return delta;
  }

  void apply(int vertex, int source, int target) {
    require(labels[vertex] == source && problem.fixed[vertex] < 0,
            "internal illegal balance move");
    for (int edge : problem.incident[vertex]) {
      --edge_part_counts[edge * problem.parts + source];
      ++edge_part_counts[edge * problem.parts + target];
    }
    labels[vertex] = target;
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      const Weight weight = problem.vertex_weights[vertex][dimension];
      loads[source][dimension] -= weight;
      loads[target][dimension] += weight;
    }
    ++moves;
  }

  double peak_overload(int part) const {
    double peak = 0.0;
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      peak = std::max(
          peak,
          static_cast<double>(loads[part][dimension]
                              - problem.allowed[part][dimension])
              / std::max<Weight>(1, problem.allowed[part][dimension]));
    }
    return peak;
  }

  std::vector<int> overloaded_sources() const {
    std::vector<int> sources;
    for (int part = 0; part < problem.parts; ++part) {
      if (peak_overload(part) > 0.0) sources.push_back(part);
    }
    std::sort(sources.begin(), sources.end(), [this](int left, int right) {
      return std::make_pair(-peak_overload(left), left)
             < std::make_pair(-peak_overload(right), right);
    });
    return sources;
  }

  struct Direct {
    double normalized = 0.0;
    double delta = 0.0;
    double negative_relief = 0.0;
    int vertex = -1;
    bool operator<(const Direct& other) const {
      return std::tie(negative_relief, normalized, delta, vertex)
             < std::tie(other.negative_relief, other.normalized,
                        other.delta, other.vertex);
    }
  };

  bool direct(int source, const std::vector<Weight>& excess) {
    std::vector<Direct> candidates;
    for (int vertex = 0; vertex < static_cast<int>(labels.size()); ++vertex) {
      if (labels[vertex] != source || problem.fixed[vertex] >= 0) continue;
      const auto& weights = problem.vertex_weights[vertex];
      double relief = 0.0;
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        relief += static_cast<double>(std::min(weights[dimension], excess[dimension]))
                  / std::max<Weight>(1, problem.allowed[source][dimension]);
      }
      if (relief <= 0.0) continue;
      bool found = false;
      std::tuple<double, double, int> best;
      for (int target = 0; target < problem.parts; ++target) {
        if (target == source || !fits(vertex, target)) continue;
        const double delta = edge_delta(vertex, source, target);
        double projected_peak = 0.0;
        for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
          projected_peak = std::max(
              projected_peak,
              static_cast<double>(loads[target][dimension] + weights[dimension])
                  / std::max<Weight>(1, problem.allowed[target][dimension]));
        }
        const auto key = std::make_tuple(projected_peak, delta, target);
        if (!found || key < best) {
          found = true;
          best = key;
        }
      }
      if (found) {
        candidates.push_back(Direct{
            std::get<1>(best) / relief, std::get<1>(best), -relief,
            vertex});
      }
    }
    std::sort(candidates.begin(), candidates.end());
    bool changed = false;
    for (const Direct& candidate : candidates) {
      const auto current = overload(source);
      if (std::none_of(current.begin(), current.end(),
                       [](Weight value) { return value > 0; })) break;
      if (labels[candidate.vertex] != source) continue;
      bool contributes = false;
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        contributes = contributes
                      || (current[dimension] > 0
                          && problem.vertex_weights[candidate.vertex][dimension] > 0);
      }
      if (!contributes) continue;
      bool found = false;
      std::tuple<double, double, int> best;
      const auto& weights = problem.vertex_weights[candidate.vertex];
      for (int target = 0; target < problem.parts; ++target) {
        if (target == source || !fits(candidate.vertex, target)) continue;
        const double delta = edge_delta(candidate.vertex, source, target);
        double projected_peak = 0.0;
        for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
          projected_peak = std::max(
              projected_peak,
              static_cast<double>(loads[target][dimension] + weights[dimension])
                  / std::max<Weight>(1, problem.allowed[target][dimension]));
        }
        const auto key = std::make_tuple(projected_peak, delta, target);
        if (!found || key < best) {
          found = true;
          best = key;
        }
      }
      if (!found) continue;
      apply(candidate.vertex, source, std::get<2>(best));
      ++direct_moves;
      changed = true;
    }
    return changed;
  }

  struct Pair {
    bool valid = false;
    double source_score = 0.0;
    double normalized = 0.0;
    double delta = 0.0;
    int vertex = -1;
    int blocker = -1;
    int target = -1;
    int destination = -1;
    auto key() const {
      return std::tie(source_score, normalized, delta, vertex,
                      blocker, target, destination);
    }
  };

  bool paired(int source, const std::vector<Weight>& excess) {
    Pair best;
    const double before_source_score = overload_score(source);
    std::map<std::vector<Weight>, std::vector<int>> source_groups;
    std::vector<std::map<std::vector<Weight>, std::vector<int>>> part_groups(
        problem.parts);
    for (int vertex = 0; vertex < static_cast<int>(labels.size()); ++vertex) {
      if (problem.fixed[vertex] >= 0) continue;
      part_groups[labels[vertex]][problem.vertex_weights[vertex]].push_back(vertex);
      if (labels[vertex] != source) continue;
      bool contributes = false;
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        contributes = contributes
                      || (excess[dimension] > 0
                          && problem.vertex_weights[vertex][dimension] > 0);
      }
      if (contributes) source_groups[problem.vertex_weights[vertex]].push_back(vertex);
    }

    for (const auto& source_group : source_groups) {
      const auto& weights = source_group.first;
      std::vector<Weight> source_without = loads[source];
      double relief = 0.0;
      for (int d = 0; d < problem.dimensions; ++d) {
        source_without[d] -= weights[d];
        relief += static_cast<double>(std::min(weights[d], excess[d]))
                  / std::max<Weight>(1, problem.allowed[source][d]);
      }
      const double source_without_score = overload_score(source, &source_without);
      for (int target = 0; target < problem.parts; ++target) {
        if (target == source) continue;
        int vertex = -1;
        double source_delta = 0.0;
        for (int candidate : source_group.second) {
          const double delta = edge_delta(candidate, source, target);
          if (vertex < 0 || std::tie(delta, candidate)
                              < std::tie(source_delta, vertex)) {
            vertex = candidate;
            source_delta = delta;
          }
        }
        std::vector<Weight> deficits(problem.dimensions, 0);
        bool target_fits = true;
        for (int d = 0; d < problem.dimensions; ++d) {
          deficits[d] = std::max<Weight>(
              0, loads[target][d] + weights[d] - problem.allowed[target][d]);
          target_fits = target_fits && deficits[d] == 0;
        }
        if (target_fits) continue;
        for (const auto& blocker_group : part_groups[target]) {
          const auto& blocker_weights = blocker_group.first;
          bool frees = true;
          for (int d = 0; d < problem.dimensions; ++d) {
            frees = frees && blocker_weights[d] >= deficits[d];
          }
          if (!frees) continue;
          for (int blocker : blocker_group.second) {
            for (int destination = 0; destination < problem.parts; ++destination) {
              if (destination == target) continue;
              bool destination_fits = true;
              std::vector<Weight> projected_source = source_without;
              for (int d = 0; d < problem.dimensions; ++d) {
                if (destination == source) projected_source[d] += blocker_weights[d];
                const Weight base = destination == source
                                        ? source_without[d]
                                        : loads[destination][d];
                destination_fits = destination_fits
                                   && base + blocker_weights[d]
                                          <= problem.allowed[destination][d];
              }
              if (!destination_fits) continue;
              const double after_score = destination == source
                                             ? overload_score(source, &projected_source)
                                             : source_without_score;
              if (destination == source
                  && after_score >= before_source_score) continue;
              const double delta = source_delta
                                   + edge_delta(blocker, target, destination);
              Pair candidate{
                  true, after_score, delta / relief, delta,
                  vertex, blocker, target, destination};
              if (!best.valid || candidate.key() < best.key()) best = candidate;
            }
          }
        }
      }
    }
    if (!best.valid) return false;
    apply(best.blocker, best.target, best.destination);
    apply(best.vertex, source, best.target);
    ++paired_sequences;
    return true;
  }

  struct Move {
    int vertex = -1;
    int source = -1;
    int target = -1;
    auto key() const { return std::tie(vertex, source, target); }
    bool operator<(const Move& other) const { return key() < other.key(); }
  };

  struct Violation {
    int count = 0;
    double normalized_sum = 0.0;
    double normalized_peak = 0.0;
    Weight absolute = 0;
    auto key() const {
      return std::tie(count, normalized_sum, normalized_peak, absolute);
    }
    bool operator<(const Violation& other) const {
      return key() < other.key();
    }
  };

  Violation violation_key(
      const std::vector<std::vector<Weight>>& state_loads,
      const std::vector<std::vector<Weight>>& limits) const {
    Violation result;
    for (int part = 0; part < problem.parts; ++part) {
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        const Weight excess = std::max<Weight>(
            0, state_loads[part][dimension] - limits[part][dimension]);
        if (excess == 0) continue;
        const double normalized = static_cast<double>(excess)
                                  / std::max<Weight>(
                                        1, problem.allowed[part][dimension]);
        ++result.count;
        result.normalized_sum += normalized;
        result.normalized_peak = std::max(result.normalized_peak, normalized);
        result.absolute += excess;
      }
    }
    return result;
  }

  void remember_chain(int source, const std::vector<Move>& sequence) {
    std::vector<ChainTemplateMove> result;
    result.reserve(sequence.size());
    for (const Move& move : sequence) {
      result.push_back(ChainTemplateMove{
          problem.vertex_weights[move.vertex], move.source, move.target});
    }
    preferred_chain_templates[source] = std::move(result);
  }

  bool replay_chain_template(int source,
                             const std::vector<Weight>& excess) {
    const auto& chain_template = preferred_chain_templates[source];
    if (chain_template.empty() || chain_template.front().source != source) {
      return false;
    }
    using Groups = std::map<std::vector<Weight>, std::vector<int>>;
    std::vector<Groups> groups(problem.parts);
    for (int vertex = 0; vertex < static_cast<int>(labels.size()); ++vertex) {
      if (problem.fixed[vertex] < 0) {
        groups[labels[vertex]][problem.vertex_weights[vertex]].push_back(vertex);
      }
    }
    std::vector<int> selected;
    std::vector<Move> sequence;
    auto candidate_loads = loads;
    for (const ChainTemplateMove& step : chain_template) {
      const auto found = groups[step.source].find(step.weights);
      if (found == groups[step.source].end()) return false;
      int vertex = -1;
      for (int candidate : found->second) {
        if (std::find(selected.begin(), selected.end(), candidate)
            == selected.end()) {
          vertex = candidate;
          break;
        }
      }
      if (vertex < 0) return false;
      selected.push_back(vertex);
      sequence.push_back(Move{vertex, step.source, step.target});
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        candidate_loads[step.source][dimension] -= step.weights[dimension];
        candidate_loads[step.target][dimension] += step.weights[dimension];
      }
    }
    const auto& root_weights = chain_template.front().weights;
    bool contributes = false;
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      contributes = contributes
                    || (excess[dimension] > 0
                        && root_weights[dimension] > 0);
    }
    if (!contributes) return false;
    std::vector<std::vector<Weight>> limits = problem.allowed;
    for (int part = 0; part < problem.parts; ++part) {
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        limits[part][dimension] = std::max(
            limits[part][dimension], loads[part][dimension]);
      }
    }
    for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
      limits[source][dimension] = std::max(
          problem.allowed[source][dimension],
          loads[source][dimension] - root_weights[dimension]);
    }
    if (violation_key(candidate_loads, limits).count != 0) return false;
    for (const Move& move : sequence) {
      apply(move.vertex, move.source, move.target);
    }
    ++ejection_chain_sequences;
    ++replayed_chain_sequences;
    max_ejection_chain_moves = std::max<long long>(
        max_ejection_chain_moves,
        static_cast<long long>(sequence.size()));
    return true;
  }

  bool ejection_chain(int source,
                      const std::vector<Weight>& excess,
                      bool preferred_only = false) {
    using Groups = std::map<std::vector<Weight>, std::vector<int>>;
    std::vector<Groups> groups(problem.parts);
    for (int vertex = 0; vertex < static_cast<int>(labels.size()); ++vertex) {
      if (problem.fixed[vertex] < 0) {
        groups[labels[vertex]][problem.vertex_weights[vertex]].push_back(vertex);
      }
    }

    struct Root {
      int preference = 2;
      double negative_relief = 0.0;
      double source_score = 0.0;
      double normalized_delta = 0.0;
      double delta = 0.0;
      int vertex = -1;
      int target = -1;
      auto key() const {
        return std::tie(preference, negative_relief,
                        source_score, normalized_delta,
                        delta, vertex, target);
      }
      bool operator<(const Root& other) const { return key() < other.key(); }
    };

    std::vector<Root> roots;
    const double before_source_score = overload_score(source);
    for (const auto& group : groups[source]) {
      const auto& weights = group.first;
      double relief = 0.0;
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        relief += static_cast<double>(
                      std::min(weights[dimension], excess[dimension]))
                  / std::max<Weight>(
                        1, problem.allowed[source][dimension]);
      }
      if (relief <= 0.0) continue;
      std::vector<Weight> source_after = loads[source];
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        source_after[dimension] -= weights[dimension];
      }
      const double source_score = overload_score(source, &source_after);
      if (source_score >= before_source_score) continue;
      for (int target = 0; target < problem.parts; ++target) {
        if (target == source) continue;
        int selected = -1;
        double selected_delta = 0.0;
        for (int vertex : group.second) {
          const double delta = edge_delta(vertex, source, target);
          if (selected < 0
              || std::tie(delta, vertex)
                     < std::tie(selected_delta, selected)) {
            selected = vertex;
            selected_delta = delta;
          }
        }
        int preference = 2;
        if (!preferred_chain_weights[source].empty()
            && weights == preferred_chain_weights[source]) {
          preference = target == preferred_chain_targets[source] ? 0 : 1;
        }
        roots.push_back(Root{preference, -relief, source_score,
                             selected_delta / relief, selected_delta,
                             selected, target});
      }
    }
    std::sort(roots.begin(), roots.end());
    if (preferred_only) {
      roots.erase(
          std::remove_if(roots.begin(), roots.end(), [](const Root& root) {
            return root.preference > 1;
          }),
          roots.end());
    }
    constexpr std::size_t root_width = 64;
    if (roots.size() > root_width) roots.resize(root_width);

    struct State {
      std::vector<std::vector<Weight>> state_loads;
      std::vector<int> moved;
      std::vector<Move> sequence;
      double approximate_delta = 0.0;
      Violation violation;
    };
    struct Transition {
      Violation violation;
      double normalized_delta = 0.0;
      double delta = 0.0;
      int vertex = -1;
      int target = -1;
      std::vector<std::vector<Weight>> state_loads;
      bool operator<(const Transition& other) const {
        return std::tie(violation.count, violation.normalized_sum,
                        violation.normalized_peak, violation.absolute,
                        normalized_delta, delta, vertex, target)
               < std::tie(other.violation.count,
                          other.violation.normalized_sum,
                          other.violation.normalized_peak,
                          other.violation.absolute,
                          other.normalized_delta, other.delta,
                          other.vertex, other.target);
      }
    };
    constexpr std::size_t beam_width = 128;
    constexpr std::size_t transition_width = 128;
    const int max_chain_moves = std::max(
        3, std::min(16, problem.parts * 2 + problem.dimensions));

    for (const Root& root : roots) {
      std::vector<std::vector<Weight>> limits = problem.allowed;
      for (int part = 0; part < problem.parts; ++part) {
        for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
          limits[part][dimension] = std::max(
              limits[part][dimension], loads[part][dimension]);
        }
      }
      const auto& root_weights = problem.vertex_weights[root.vertex];
      std::vector<std::vector<Weight>> root_loads = loads;
      for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
        root_loads[source][dimension] -= root_weights[dimension];
        root_loads[root.target][dimension] += root_weights[dimension];
        limits[source][dimension] = std::max(
            problem.allowed[source][dimension],
            root_loads[source][dimension]);
      }
      State initial{
          root_loads,
          {root.vertex},
          {Move{root.vertex, source, root.target}},
          root.delta,
          violation_key(root_loads, limits)};
      if (initial.violation.count == 0) {
        preferred_chain_weights[source] = root_weights;
        preferred_chain_targets[source] = root.target;
        remember_chain(source, initial.sequence);
        apply(root.vertex, source, root.target);
        ++ejection_chain_sequences;
        max_ejection_chain_moves = std::max<long long>(
            max_ejection_chain_moves, 1);
        return true;
      }
      std::vector<State> beam{std::move(initial)};
      for (int depth = 1; depth < max_chain_moves; ++depth) {
        std::vector<State> next;
        for (const State& state : beam) {
          int blocked_part = -1;
          double blocked_peak = -1.0;
          std::vector<int> exceeded;
          for (int part = 0; part < problem.parts; ++part) {
            std::vector<int> part_exceeded;
            double part_peak = 0.0;
            for (int dimension = 0; dimension < problem.dimensions;
                 ++dimension) {
              const Weight amount = state.state_loads[part][dimension]
                                    - limits[part][dimension];
              if (amount <= 0) continue;
              part_exceeded.push_back(dimension);
              part_peak = std::max(
                  part_peak,
                  static_cast<double>(amount)
                      / std::max<Weight>(
                            1, problem.allowed[part][dimension]));
            }
            if (!part_exceeded.empty()
                && (blocked_part < 0
                    || std::make_pair(-part_peak, part)
                           < std::make_pair(-blocked_peak, blocked_part))) {
              blocked_part = part;
              blocked_peak = part_peak;
              exceeded = std::move(part_exceeded);
            }
          }
          if (blocked_part < 0) return false;
          int pivot = -1;
          std::size_t pivot_groups = std::numeric_limits<std::size_t>::max();
          for (int dimension : exceeded) {
            std::size_t count = 0;
            for (const auto& group : groups[blocked_part]) {
              count += group.first[dimension] > 0;
            }
            if (std::make_pair(count, dimension)
                < std::make_pair(pivot_groups, pivot)) {
              pivot_groups = count;
              pivot = dimension;
            }
          }
          std::vector<Weight> deficits(problem.dimensions, 0);
          for (int dimension = 0; dimension < problem.dimensions; ++dimension) {
            deficits[dimension] = std::max<Weight>(
                0, state.state_loads[blocked_part][dimension]
                       - limits[blocked_part][dimension]);
          }
          std::vector<Transition> transitions;
          for (const auto& group : groups[blocked_part]) {
            const auto& weights = group.first;
            if (weights[pivot] == 0) continue;
            int vertex = -1;
            for (int candidate : group.second) {
              if (std::find(state.moved.begin(), state.moved.end(), candidate)
                  == state.moved.end()) {
                vertex = candidate;
                break;
              }
            }
            if (vertex < 0) continue;
            double relief = 0.0;
            for (int dimension = 0; dimension < problem.dimensions;
                 ++dimension) {
              relief += static_cast<double>(
                            std::min(weights[dimension], deficits[dimension]))
                        / std::max<Weight>(
                              1, problem.allowed[blocked_part][dimension]);
            }
            if (relief <= 0.0) continue;
            for (int target = 0; target < problem.parts; ++target) {
              if (target == blocked_part) continue;
              auto candidate_loads = state.state_loads;
              for (int dimension = 0; dimension < problem.dimensions;
                   ++dimension) {
                candidate_loads[blocked_part][dimension] -= weights[dimension];
                candidate_loads[target][dimension] += weights[dimension];
              }
              const double delta = edge_delta(vertex, blocked_part, target);
              transitions.push_back(Transition{
                  violation_key(candidate_loads, limits),
                  delta / relief,
                  delta,
                  vertex,
                  target,
                  std::move(candidate_loads)});
            }
          }
          std::sort(transitions.begin(), transitions.end());
          if (transitions.size() > transition_width) {
            transitions.resize(transition_width);
          }
          for (Transition& transition : transitions) {
            State candidate;
            candidate.state_loads = std::move(transition.state_loads);
            candidate.moved = state.moved;
            candidate.moved.push_back(transition.vertex);
            std::sort(candidate.moved.begin(), candidate.moved.end());
            candidate.sequence = state.sequence;
            candidate.sequence.push_back(
                Move{transition.vertex, blocked_part, transition.target});
            candidate.approximate_delta = state.approximate_delta
                                          + transition.delta;
            candidate.violation = transition.violation;
            if (candidate.violation.count == 0) {
              preferred_chain_weights[source] = root_weights;
              preferred_chain_targets[source] = root.target;
              remember_chain(source, candidate.sequence);
              for (const Move& move : candidate.sequence) {
                apply(move.vertex, move.source, move.target);
              }
              ++ejection_chain_sequences;
              max_ejection_chain_moves = std::max<long long>(
                  max_ejection_chain_moves,
                  static_cast<long long>(candidate.sequence.size()));
              return true;
            }
            next.push_back(std::move(candidate));
          }
        }
        std::sort(next.begin(), next.end(), [](const State& left,
                                               const State& right) {
          return std::tie(left.violation.count,
                          left.violation.normalized_sum,
                          left.violation.normalized_peak,
                          left.violation.absolute,
                          left.approximate_delta,
                          left.sequence)
                 < std::tie(right.violation.count,
                            right.violation.normalized_sum,
                            right.violation.normalized_peak,
                            right.violation.absolute,
                            right.approximate_delta,
                            right.sequence);
        });
        if (next.size() > beam_width) next.resize(beam_width);
        beam = std::move(next);
        if (beam.empty()) break;
      }
    }
    return false;
  }

  void write_report(const std::string& report_path,
                    const std::string& status,
                    const std::string& error = "") const {
    const std::string temporary = report_path + ".tmp";
    std::ofstream output(temporary);
    require(output.good(), "cannot write legalizer report");
    output << "EMUFLOW_TRITONPART_BALANCE_REPORT_V1\n"
           << "STATUS " << status << '\n'
           << "METRIC moves " << moves << '\n'
           << "METRIC direct_moves " << direct_moves << '\n'
           << "METRIC paired_sequences " << paired_sequences << '\n'
           << "METRIC ejection_chain_sequences "
           << ejection_chain_sequences << '\n'
           << "METRIC replayed_chain_sequences "
           << replayed_chain_sequences << '\n'
           << "METRIC max_ejection_chain_moves "
           << max_ejection_chain_moves << '\n'
           << "METRIC elapsed_milliseconds "
           << std::chrono::duration_cast<std::chrono::milliseconds>(
                  std::chrono::steady_clock::now() - started).count() << '\n';
    if (!error.empty()) output << "ERROR " << error << '\n';
    for (int part = 0; part < problem.parts; ++part) {
      output << "INITIAL " << part;
      for (Weight value : initial_loads[part]) output << ' ' << value;
      output << "\nFINAL " << part;
      for (Weight value : loads[part]) output << ' ' << value;
      output << "\nALLOWED " << part;
      for (Weight value : problem.allowed[part]) output << ' ' << value;
      output << '\n';
    }
    output << "END\n";
    output.close();
    require(std::rename(temporary.c_str(), report_path.c_str()) == 0,
            "cannot publish legalizer report");
  }

  void run(const std::string& output_solution,
           const std::string& report_path) {
    write_report(report_path, "RUNNING");
    while (true) {
      const auto sources = overloaded_sources();
      if (sources.empty()) break;
      bool changed = false;
      for (int source : sources) {
        const auto excess = overload(source);
        if (direct(source, excess)) {
          changed = true;
          break;
        }
      }
      if (!changed) {
        for (int source : sources) {
          if (replay_chain_template(source, overload(source))) {
            changed = true;
            break;
          }
        }
      }
      if (!changed) {
        for (int source : sources) {
          if (!preferred_chain_weights[source].empty()
              && ejection_chain(source, overload(source), true)) {
            changed = true;
            break;
          }
        }
      }
      if (!changed) {
        for (int source : sources) {
          if (paired(source, overload(source))) {
            changed = true;
            break;
          }
        }
      }
      if (!changed) {
        for (int source : sources) {
          if (ejection_chain(source, overload(source))) {
            changed = true;
            break;
          }
        }
      }
      if (!changed) {
        write_report(
            report_path, "FAILED",
            "no deterministic direct, paired, or bounded-chain repair");
        throw std::runtime_error(
            "no deterministic direct, paired, or bounded-chain balance repair");
      }
      const auto now = std::chrono::steady_clock::now();
      if (now - last_progress >= std::chrono::seconds(60)) {
        write_report(report_path, "RUNNING");
        last_progress = now;
      }
    }
    const std::string temporary_solution = output_solution + ".tmp";
    std::ofstream solution(temporary_solution);
    require(solution.good(), "cannot write legalized solution");
    for (int part : labels) solution << part << '\n';
    solution.close();
    require(std::rename(temporary_solution.c_str(), output_solution.c_str()) == 0,
            "cannot publish legalized solution");
    write_report(report_path, "PASS");
  }
};

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_tritonpart_balance_legalizer HGR FIXED "
                 "EDGE_CLASSES LIMITS INPUT_SOLUTION OUTPUT_SOLUTION REPORT\n";
    return 0;
  }
  if (argc != 8) {
    std::cerr << "usage: emuflow_tritonpart_balance_legalizer HGR FIXED "
                 "EDGE_CLASSES LIMITS INPUT_SOLUTION OUTPUT_SOLUTION REPORT\n";
    return 2;
  }
  try {
    Legalizer legalizer(read_problem(argv[1], argv[2], argv[3], argv[4], argv[5]));
    legalizer.run(argv[6], argv[7]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_tritonpart_balance_legalizer: " << error.what() << '\n';
    return 1;
  }
}
