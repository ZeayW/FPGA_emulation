// SPDX-License-Identifier: Apache-2.0
//
// Timing-aware load-balanced die-level routing for EmuFlow.
//
// This is an in-tree C++17 implementation of timing-aware die-level routing
// plus the route/TDM coupling used in the DAC 2020 routing-topology/TDM and
// ASP-DAC 2021 hybrid routing/TDM co-optimization formulations.
//
// The compact line-oriented interface is intentional: it keeps the optimizer
// independent of a particular JSON library while Python remains only the
// artifact adapter and independent checker.

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <fstream>
#include <future>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <numeric>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();
constexpr double kEps = 1.0e-12;

struct Arc {
  int link = -1;
  int from = -1;
  int to = -1;
  int capacity_domain = -1;
  int direction_group = -1;
  int opposite_arc = -1;
  long long capacity = 0;
  int lanes = 0;
  double delay_ns = 0.0;
  double beta_ns = 0.0;
  bool is_sll = false;
};

struct Demand {
  int source = -1;
  std::vector<int> sinks;
  int width = 1;
  double normalized_slack = 0.0;
  double predicted_delay_ns = 0.0;
};

struct TimingPath {
  double clock_period_ns = 0.0;
  double baseline_slack_ns = 0.0;
  double fixed_delay_ns = 0.0;
  std::vector<int> demands;
};

struct Route {
  std::vector<int> arcs;
  double max_delay_ns = 0.0;
};

enum class CandidateGenerator {
  kShortestPath,
  kDelayDemandBalanced,
  kNearestTerminalSteiner,
  kDirectedMetricClosure,
  kShallowLight,
  kAdaptiveHop,
};

struct Objective {
  double worst_tdm_normalized_slack = -kInf;
  double worst_tdm_slack_ns = -kInf;
  double worst_normalized_slack = -kInf;
  double worst_slack_ns = -kInf;
  double max_utilization = kInf;
  long long bit_hops = std::numeric_limits<long long>::max();
};

struct Input {
  int node_count = 0;
  int topology_mode = 0;
  int max_iterations = 20;
  int reroute_rounds = 8;
  double lambda_load = 2.0;
  double lambda_timing = 4.0;
  double lambda_history = 1.0;
  double lambda_tdm = 0.1;
  int ratio_quantum = 8;
  int min_ratio = 1;
  int frame_slots = 1;
  double slack_positive_scale = 1.0;
  double slack_negative_scale = 1.0;
  double max_clock_period_ns = 1.0;
  bool tree_edge_sum_tdm = false;
  bool hard_sll_capacity = false;
  // Zero means unconstrained.  Positive values bound every source-to-sink
  // route-tree path by its number of board links.
  int max_route_hops = 0;
  int candidate_workers = 1;
  std::vector<Arc> arcs;
  std::vector<Demand> demands;
  std::vector<TimingPath> paths;
  std::vector<double> feedback_price;
};

std::vector<int> parse_int_list(const std::string& value) {
  std::vector<int> result;
  if (value == "-") {
    return result;
  }
  std::stringstream stream(value);
  std::string token;
  while (std::getline(stream, token, ',')) {
    if (!token.empty()) {
      result.push_back(std::stoi(token));
    }
  }
  return result;
}

Input read_input(const std::string& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open input: " + path);
  }
  std::string magic;
  std::getline(input, magic);
  const bool input_v9 = magic == "EMUFLOW_TLR_INPUT_V9";
  const bool input_v8 = magic == "EMUFLOW_TLR_INPUT_V8" || input_v9;
  const bool input_v7 = magic == "EMUFLOW_TLR_INPUT_V7" || input_v8;
  const bool input_v6 = magic == "EMUFLOW_TLR_INPUT_V6";
  const bool input_v5 = magic == "EMUFLOW_TLR_INPUT_V5";
  const bool input_v4 = magic == "EMUFLOW_TLR_INPUT_V4";
  if (!input_v9 && !input_v8 && !input_v7 && !input_v6 && !input_v5 && !input_v4 &&
      magic != "EMUFLOW_TLR_INPUT_V3") {
    throw std::runtime_error("unsupported input header: " + magic);
  }

  Input model;
  std::string line;
  while (std::getline(input, line)) {
    if (line.empty() || line[0] == '#') {
      continue;
    }
    std::stringstream stream(line);
    std::string kind;
    stream >> kind;
    if (kind == "PARAM") {
      stream >> model.node_count >> model.topology_mode >>
          model.max_iterations >>
          model.reroute_rounds >> model.lambda_load >>
          model.lambda_timing >> model.lambda_history >>
          model.lambda_tdm >> model.ratio_quantum >> model.frame_slots >>
          model.slack_positive_scale >> model.slack_negative_scale >>
          model.max_clock_period_ns;
      if (input_v4 || input_v5 || input_v6 || input_v7) {
        int tree_edge_sum_tdm = 0;
        stream >> tree_edge_sum_tdm;
        if (tree_edge_sum_tdm != 0 && tree_edge_sum_tdm != 1) {
          throw std::runtime_error(
              "tree-edge-sum TDM flag must be zero or one");
        }
        model.tree_edge_sum_tdm = tree_edge_sum_tdm != 0;
      }
      if (input_v5 || input_v6 || input_v7) {
        stream >> model.min_ratio;
      }
      if (input_v6 || input_v7) {
        int hard_sll_capacity = 0;
        stream >> hard_sll_capacity;
        if (hard_sll_capacity != 0 && hard_sll_capacity != 1) {
          throw std::runtime_error(
              "hard SLL capacity flag must be zero or one");
        }
        model.hard_sll_capacity = hard_sll_capacity != 0;
      }
      if (input_v7) {
        stream >> model.max_route_hops;
        if (model.max_route_hops < 0) {
          throw std::runtime_error(
              "maximum route hops must be zero or positive");
        }
      }
      if (input_v9) {
        stream >> model.candidate_workers;
        if (model.candidate_workers <= 0) {
          throw std::runtime_error(
              "candidate workers must be a positive integer");
        }
      }
    } else if (kind == "ARC") {
      int index = -1;
      Arc arc;
      int is_sll = 0;
      stream >> index >> arc.link >> arc.from >> arc.to >>
          arc.capacity_domain >> arc.direction_group >> arc.opposite_arc >>
          arc.capacity >> arc.lanes >> arc.delay_ns >> arc.beta_ns >> is_sll;
      arc.is_sll = is_sll != 0;
      if (index != static_cast<int>(model.arcs.size())) {
        throw std::runtime_error("ARC indices must be contiguous");
      }
      model.arcs.push_back(arc);
    } else if (kind == "DEMAND") {
      int index = -1;
      Demand demand;
      std::string sinks;
      stream >> index >> demand.source >> sinks >> demand.width >>
          demand.normalized_slack >> demand.predicted_delay_ns;
      demand.sinks = parse_int_list(sinks);
      if (index != static_cast<int>(model.demands.size())) {
        throw std::runtime_error("DEMAND indices must be contiguous");
      }
      model.demands.push_back(std::move(demand));
    } else if (kind == "PRICE") {
      int index = -1;
      double price = 0.0;
      stream >> index >> price;
      if (!input_v8 || index != static_cast<int>(model.feedback_price.size()) ||
          !std::isfinite(price) || price < 0.0) {
        throw std::runtime_error("invalid feedback price");
      }
      model.feedback_price.push_back(price);
    } else if (kind == "PATH") {
      int index = -1;
      TimingPath timing_path;
      std::string demands;
      stream >> index >> timing_path.clock_period_ns >>
          timing_path.baseline_slack_ns >> timing_path.fixed_delay_ns >>
          demands;
      timing_path.demands = parse_int_list(demands);
      if (index != static_cast<int>(model.paths.size())) {
        throw std::runtime_error("PATH indices must be contiguous");
      }
      model.paths.push_back(std::move(timing_path));
    } else {
      throw std::runtime_error("unknown record kind: " + kind);
    }
    if (!stream) {
      throw std::runtime_error("malformed input record: " + line);
    }
  }
  if (model.node_count <= 0 || model.topology_mode < 0 ||
      model.topology_mode > 2 || model.arcs.empty() ||
      model.demands.empty()) {
    throw std::runtime_error("input must contain nodes, arcs, and demands");
  }
  int capacity_domains = 0;
  for (const Arc& arc : model.arcs) {
    capacity_domains = std::max(capacity_domains, arc.capacity_domain + 1);
  }
  if (!model.feedback_price.empty() &&
      model.feedback_price.size() !=
          static_cast<std::size_t>(capacity_domains)) {
    throw std::runtime_error(
        "feedback prices must cover every capacity domain exactly");
  }
  return model;
}

class Router {
 public:
  explicit Router(Input model)
      : Router(std::make_shared<const Input>(std::move(model)), nullptr) {}

 private:
  explicit Router(std::shared_ptr<const Input> model,
                  const Input* borrowed_model)
      : owned_model_(std::move(model)),
        model_(owned_model_ ? *owned_model_ : *borrowed_model),
        adjacency_(model_.node_count),
        usage_(capacity_domain_count(), 0),
        history_(capacity_domain_count(), 0.0),
        direction_lock_(direction_group_count(), -1),
        routes_(model_.demands.size()) {
    for (int index = 0; index < static_cast<int>(model_.arcs.size()); ++index) {
      const Arc& arc = model_.arcs[index];
      if (arc.from < 0 || arc.from >= model_.node_count ||
          arc.to < 0 || arc.to >= model_.node_count ||
          arc.capacity <= 0 || arc.lanes <= 0 || arc.delay_ns < 0.0 ||
          arc.beta_ns <= 0.0 || model_.ratio_quantum <= 0 ||
          model_.min_ratio <= 0 ||
          (model_.min_ratio != 1 &&
           model_.min_ratio % model_.ratio_quantum != 0) ||
          model_.min_ratio > model_.frame_slots ||
          model_.frame_slots <= 0 || model_.lambda_tdm < 0.0) {
        throw std::runtime_error("invalid arc");
      }
      adjacency_[arc.from].push_back(index);
    }
    for (auto& outgoing : adjacency_) {
      std::sort(outgoing.begin(), outgoing.end());
    }
    demand_criticality_.resize(model_.demands.size(), 0.0);
    const auto [minimum, maximum] = demand_slack_range();
    for (int index = 0; index < static_cast<int>(model_.demands.size()); ++index) {
      const double slack = model_.demands[index].normalized_slack;
      demand_criticality_[index] =
          maximum <= minimum + kEps ? 1.0 : (maximum - slack) / (maximum - minimum);
    }
  }

 public:

  void run() {
    lock_shared_directions();
    const std::vector<int> order = timing_aware_order();
    // Generate two independent initial topology candidates.  The first is
    // the ASP-DAC 2026 source-rooted shortest-path tree.  The second is the
    // DAC 2025 delay-demand-balanced connection router: sinks are connected
    // incrementally, already reached tree vertices can be used as Steiner
    // attachment points, and SLL/cable costs use distinct congestion and TDM
    // models.  Keeping both candidates is important: a shortest-path tree is
    // strong for delay while the connection router usually needs fewer
    // bit-hops for high-fanout nets.
    const std::vector<double> initial_history = history_;
    Candidate baseline;
    Candidate balanced;
    Candidate steiner;
    Candidate metric_closure;
    Candidate shallow_light;
    Candidate adaptive_hop;
    if (model_.topology_mode == 2 && model_.candidate_workers > 1) {
      using Generated = std::pair<CandidateGenerator, Candidate>;
      const std::vector<CandidateGenerator> generators = {
          CandidateGenerator::kShortestPath,
          CandidateGenerator::kDelayDemandBalanced,
          CandidateGenerator::kNearestTerminalSteiner,
          CandidateGenerator::kDirectedMetricClosure,
          CandidateGenerator::kShallowLight,
          CandidateGenerator::kAdaptiveHop,
      };
      std::vector<Generated> generated;
      for (std::size_t first = 0; first < generators.size();
           first += model_.candidate_workers) {
        const std::size_t last = std::min(
            generators.size(), first + model_.candidate_workers);
        std::vector<std::future<Generated>> futures;
        for (std::size_t index = first; index < last; ++index) {
          const CandidateGenerator generator = generators[index];
          futures.push_back(std::async(
              std::launch::async, [this, generator]() {
                // The parent router owns the immutable model until every
                // future has joined.  Borrow it here so worker teardown does
                // not repeatedly release (and, on older libstdc++ builds,
                // occasionally destroy) the very large Input object.
                Router worker(std::shared_ptr<const Input>{}, &model_);
                worker.lock_shared_directions();
                return Generated{
                    generator,
                    worker.route_candidate(
                        worker.timing_aware_order(), generator)};
              }));
          ++parallel_candidate_tasks_;
        }
        for (auto& future : futures) {
          generated.push_back(future.get());
        }
      }
      for (Generated& result : generated) {
        switch (result.first) {
          case CandidateGenerator::kShortestPath:
            baseline = std::move(result.second);
            break;
          case CandidateGenerator::kDelayDemandBalanced:
            balanced = std::move(result.second);
            break;
          case CandidateGenerator::kNearestTerminalSteiner:
            steiner = std::move(result.second);
            break;
          case CandidateGenerator::kDirectedMetricClosure:
            metric_closure = std::move(result.second);
            break;
          case CandidateGenerator::kShallowLight:
            shallow_light = std::move(result.second);
            break;
          case CandidateGenerator::kAdaptiveHop:
            adaptive_hop = std::move(result.second);
            break;
        }
      }
    } else {
      baseline = route_candidate(order, false);
    }
    shortest_candidate_routes_ = baseline.routes;
    history_ = initial_history;
    if (model_.topology_mode >= 1 &&
        !(model_.topology_mode == 2 && model_.candidate_workers > 1)) {
      balanced = route_candidate(order, true);
      history_ = initial_history;
      steiner = route_candidate(
          order, CandidateGenerator::kNearestTerminalSteiner);
    }
    balanced_candidate_routes_ = balanced.routes;
    steiner_candidate_routes_ = steiner.routes;
    metric_closure_candidate_routes_ = metric_closure.routes;
    shallow_light_candidate_routes_ = shallow_light.routes;
    adaptive_hop_candidate_routes_ = adaptive_hop.routes;
    if (model_.topology_mode == 2 && model_.candidate_workers == 1) {
      history_ = initial_history;
      metric_closure = route_candidate(
          order, CandidateGenerator::kDirectedMetricClosure);
      history_ = initial_history;
      shallow_light = route_candidate(
          order, CandidateGenerator::kShallowLight);
      history_ = initial_history;
      adaptive_hop = route_candidate(
          order, CandidateGenerator::kAdaptiveHop);
      metric_closure_candidate_routes_ = metric_closure.routes;
      shallow_light_candidate_routes_ = shallow_light.routes;
      adaptive_hop_candidate_routes_ = adaptive_hop.routes;
    }
    baseline_candidate_feasible_ = baseline.feasible;
    balanced_candidate_feasible_ = balanced.feasible;
    steiner_candidate_feasible_ = steiner.feasible;
    metric_closure_candidate_feasible_ = metric_closure.feasible;
    shallow_light_candidate_feasible_ = shallow_light.feasible;
    adaptive_hop_candidate_feasible_ = adaptive_hop.feasible;
    shortest_candidate_generated_ = baseline.generated;
    balanced_candidate_generated_ = balanced.generated;
    steiner_candidate_generated_ = steiner.generated;
    metric_closure_candidate_generated_ = metric_closure.generated;
    shallow_light_candidate_generated_ = shallow_light.generated;
    adaptive_hop_candidate_generated_ = adaptive_hop.generated;
    if (!baseline.feasible && !balanced.feasible &&
        model_.topology_mode != 2) {
      throw std::runtime_error("routing infeasible after capacity iterations");
    }

    const Candidate* selected = nullptr;
    if (model_.topology_mode == 2 && !baseline.feasible &&
        !balanced.feasible && steiner.feasible) {
      selected = &steiner;
    } else if (!baseline.feasible) {
      selected = &balanced;
    } else if (!balanced.feasible) {
      selected = &baseline;
    } else {
      selected = better(balanced.objective, baseline.objective)
          ? &balanced
          : &baseline;
    }
    if (selected == nullptr && model_.topology_mode == 2) {
      selected = baseline.generated
          ? &baseline
          : balanced.generated ? &balanced : &steiner;
    }
    if (selected == nullptr || !selected->generated) {
      throw std::runtime_error("routing candidates could not span all sinks");
    }
    routes_ = selected->routes;
    usage_ = selected->usage;
    history_ = selected->history;
    completed_iterations_ = selected->iterations;
    selected_balanced_ = selected == &balanced;
    master_selection_.assign(
        model_.demands.size(),
        selected == &steiner
            ? "nearest-terminal-steiner"
            : selected_balanced_
                ? "delay-demand-balanced"
                : "shortest-path-tree");
    if (model_.topology_mode == 2) {
      run_candidate_master(order);
    }

    if (model_.topology_mode == 2) {
      run_batched_refinement();
      return;
    }
    Objective best = objective();
    for (int round = 0; round < model_.reroute_rounds; ++round) {
      const int critical_path =
          model_.lambda_tdm > kEps
              ? worst_tdm_path_index()
              : worst_path_index();
      if (critical_path < 0) {
        break;
      }
      std::vector<int> affected = model_.paths[critical_path].demands;
      std::sort(affected.begin(), affected.end());
      affected.erase(std::unique(affected.begin(), affected.end()), affected.end());
      if (affected.empty()) {
        break;
      }

      const std::vector<Route> route_backup = routes_;
      const std::vector<long long> usage_backup = usage_;
      std::set<int> discouraged;
      for (int demand : affected) {
        for (int arc : routes_[demand].arcs) {
          discouraged.insert(arc);
        }
        add_usage(routes_[demand], -model_.demands[demand].width);
      }
      bool reroute_ok = true;
      try {
        for (int demand : affected) {
          routes_[demand] = route_for_generator(
              demand, master_selection_[demand], discouraged);
          add_usage(routes_[demand], model_.demands[demand].width);
        }
      } catch (const std::runtime_error&) {
        reroute_ok = false;
      }

      const Objective candidate = reroute_ok ? objective() : Objective{};
      if (reroute_ok && capacity_legal() && better(candidate, best)) {
        best = candidate;
        ++accepted_reroutes_;
      } else {
        routes_ = route_backup;
        usage_ = usage_backup;
        ++rolled_back_reroutes_;
        break;
      }
    }
  }

  void write_output(const std::string& path) const {
    std::ofstream output(path);
    if (!output) {
      throw std::runtime_error("cannot open output: " + path);
    }
    output << "EMUFLOW_TLR_OUTPUT_V2\n";
    output << std::setprecision(17);
    for (int group = 0; group < static_cast<int>(direction_lock_.size()); ++group) {
      output << "LOCK " << group << ' ' << direction_lock_[group] << '\n';
    }
    for (int demand = 0; demand < static_cast<int>(routes_.size()); ++demand) {
      output << "ROUTE " << demand << ' ' << routes_[demand].max_delay_ns << ' ';
      if (routes_[demand].arcs.empty()) {
        output << '-';
      } else {
        for (std::size_t index = 0; index < routes_[demand].arcs.size(); ++index) {
          if (index) {
            output << ',';
          }
          output << routes_[demand].arcs[index];
        }
      }
      output << '\n';
    }
    write_candidate_routes(output, "shortest-path-tree",
                           shortest_candidate_routes_,
                           shortest_candidate_generated_);
    write_candidate_routes(output, "delay-demand-balanced",
                           balanced_candidate_routes_,
                           balanced_candidate_generated_);
    write_candidate_routes(output, "nearest-terminal-steiner",
                           steiner_candidate_routes_,
                           steiner_candidate_generated_);
    write_candidate_routes(output, "directed-metric-closure",
                           metric_closure_candidate_routes_,
                           metric_closure_candidate_generated_);
    write_candidate_routes(output, "shallow-light-tree",
                           shallow_light_candidate_routes_,
                           shallow_light_candidate_generated_);
    write_candidate_routes(output, "adaptive-hop-tree",
                           adaptive_hop_candidate_routes_,
                           adaptive_hop_candidate_generated_);
    write_candidate_routes(output, "refined-final", routes_, true);
    for (int demand = 0;
         demand < static_cast<int>(master_selection_.size()); ++demand) {
      output << "SELECTION " << demand << ' '
             << master_selection_[demand] << '\n';
    }
    for (int path_index = 0;
         path_index < static_cast<int>(model_.paths.size()); ++path_index) {
      const auto [delay, slack, normalized] = path_metrics(path_index);
      output << "PATH " << path_index << ' ' << delay << ' ' << slack << ' '
             << normalized << ' ' << path_signature(path_index) << '\n';
    }
    const Objective final = objective();
    output << "METRIC iterations " << completed_iterations_ << '\n';
    output << "METRIC accepted_reroutes " << accepted_reroutes_ << '\n';
    output << "METRIC rolled_back_reroutes " << rolled_back_reroutes_ << '\n';
    output << "METRIC baseline_candidate_feasible "
           << static_cast<int>(baseline_candidate_feasible_) << '\n';
    output << "METRIC balanced_candidate_feasible "
           << static_cast<int>(balanced_candidate_feasible_) << '\n';
    output << "METRIC steiner_candidate_feasible "
           << static_cast<int>(steiner_candidate_feasible_) << '\n';
    output << "METRIC metric_closure_candidate_feasible "
           << static_cast<int>(metric_closure_candidate_feasible_) << '\n';
    output << "METRIC shallow_light_candidate_feasible "
           << static_cast<int>(shallow_light_candidate_feasible_) << '\n';
    output << "METRIC adaptive_hop_candidate_feasible "
           << static_cast<int>(adaptive_hop_candidate_feasible_) << '\n';
    output << "METRIC master_rounds " << master_rounds_ << '\n';
    output << "METRIC master_switches " << master_switches_ << '\n';
    output << "METRIC master_exact "
           << static_cast<int>(master_exact_) << '\n';
    output << "METRIC candidate_workers " << model_.candidate_workers << '\n';
    output << "METRIC parallel_candidate_tasks "
           << parallel_candidate_tasks_ << '\n';
    output << "METRIC reroute_conflict_batches "
           << reroute_conflict_batches_ << '\n';
    output << "METRIC maximum_parallel_batch "
           << maximum_parallel_batch_ << '\n';
    output << "METRIC parallel_reroute_tasks "
           << parallel_reroute_tasks_ << '\n';
    output << "METRIC selected_delay_demand_balanced "
           << static_cast<int>(selected_balanced_) << '\n';
    output << "METRIC worst_slack_ns " << final.worst_slack_ns << '\n';
    output << "METRIC worst_normalized_slack "
           << final.worst_normalized_slack << '\n';
    output << "METRIC estimated_worst_tdm_slack_ns "
           << final.worst_tdm_slack_ns << '\n';
    output << "METRIC estimated_worst_tdm_normalized_slack "
           << final.worst_tdm_normalized_slack << '\n';
    output << "METRIC estimated_max_tdm_ratio "
           << estimated_max_tdm_ratio() << '\n';
    output << "METRIC max_utilization " << final.max_utilization << '\n';
    output << "METRIC total_link_bit_hops " << final.bit_hops << '\n';
  }

 private:
  struct Candidate {
    bool generated = false;
    bool feasible = false;
    int iterations = 0;
    std::vector<Route> routes;
    std::vector<long long> usage;
    std::vector<double> history;
    Objective objective;
  };

  struct RerouteWork {
    int path = -1;
    std::vector<int> demands;
    std::set<int> capacity_domains;
    std::set<int> timing_paths;
  };

  struct RerouteProposal {
    int path = -1;
    bool generated = false;
    std::vector<std::pair<int, Route>> replacements;
  };

  static bool intersects(const std::set<int>& left,
                         const std::set<int>& right) {
    auto first = left.begin();
    auto second = right.begin();
    while (first != left.end() && second != right.end()) {
      if (*first == *second) {
        return true;
      }
      if (*first < *second) {
        ++first;
      } else {
        ++second;
      }
    }
    return false;
  }

  std::vector<std::vector<RerouteWork>> build_reroute_batches() const {
    std::vector<std::set<int>> paths_by_demand(model_.demands.size());
    for (int path = 0; path < static_cast<int>(model_.paths.size()); ++path) {
      for (int demand : model_.paths[path].demands) {
        paths_by_demand[demand].insert(path);
      }
    }
    std::vector<int> order(model_.paths.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [this](int left, int right) {
      const double left_slack = normalized_slack(
          model_.paths[left], model_.paths[left].baseline_slack_ns);
      const double right_slack = normalized_slack(
          model_.paths[right], model_.paths[right].baseline_slack_ns);
      if (std::abs(left_slack - right_slack) > kEps) {
        return left_slack < right_slack;
      }
      return left < right;
    });
    std::vector<RerouteWork> work;
    for (int path : order) {
      RerouteWork item;
      item.path = path;
      item.demands = model_.paths[path].demands;
      std::sort(item.demands.begin(), item.demands.end());
      item.demands.erase(
          std::unique(item.demands.begin(), item.demands.end()),
          item.demands.end());
      for (int demand : item.demands) {
        item.timing_paths.insert(
            paths_by_demand[demand].begin(), paths_by_demand[demand].end());
        const std::vector<const std::vector<Route>*> alternatives = {
            &shortest_candidate_routes_, &balanced_candidate_routes_,
            &steiner_candidate_routes_, &metric_closure_candidate_routes_,
            &shallow_light_candidate_routes_,
            &adaptive_hop_candidate_routes_};
        for (const auto* routes : alternatives) {
          if (routes->size() != model_.demands.size()) {
            continue;
          }
          for (int arc : (*routes)[demand].arcs) {
            item.capacity_domains.insert(
                model_.arcs[arc].capacity_domain);
          }
        }
      }
      if (!item.demands.empty()) {
        work.push_back(std::move(item));
      }
    }
    std::vector<std::vector<RerouteWork>> batches;
    if (work.size() <= 4096) {
      for (const RerouteWork& item : work) {
        bool placed = false;
        for (auto& batch : batches) {
          bool conflict = false;
          for (const RerouteWork& other : batch) {
            if (intersects(item.capacity_domains, other.capacity_domains) ||
                intersects(item.timing_paths, other.timing_paths)) {
              conflict = true;
              break;
            }
          }
          if (!conflict) {
            batch.push_back(item);
            placed = true;
            break;
          }
        }
        if (!placed) {
          batches.push_back({item});
        }
      }
      return batches;
    }
    std::vector<int> last_capacity_batch(capacity_domain_count(), -1);
    std::vector<int> last_timing_batch(model_.paths.size(), -1);
    for (const RerouteWork& item : work) {
      int batch_index = 0;
      for (int domain : item.capacity_domains) {
        batch_index = std::max(
            batch_index, last_capacity_batch[domain] + 1);
      }
      for (int path : item.timing_paths) {
        batch_index = std::max(
            batch_index, last_timing_batch[path] + 1);
      }
      if (batch_index >= static_cast<int>(batches.size())) {
        batches.resize(batch_index + 1);
      }
      batches[batch_index].push_back(item);
      for (int domain : item.capacity_domains) {
        last_capacity_batch[domain] = batch_index;
      }
      for (int path : item.timing_paths) {
        last_timing_batch[path] = batch_index;
      }
    }
    return batches;
  }

  RerouteProposal propose_reroute(int path) const {
    Router worker(*this);
    RerouteProposal proposal;
    proposal.path = path;
    std::vector<int> affected = worker.model_.paths[path].demands;
    std::sort(affected.begin(), affected.end());
    affected.erase(
        std::unique(affected.begin(), affected.end()), affected.end());
    std::set<int> discouraged;
    for (int demand : affected) {
      for (int arc : worker.routes_[demand].arcs) {
        discouraged.insert(arc);
      }
      worker.add_usage(
          worker.routes_[demand], -worker.model_.demands[demand].width);
    }
    try {
      for (int demand : affected) {
        worker.routes_[demand] = worker.route_for_generator(
            demand, worker.master_selection_[demand], discouraged);
        worker.add_usage(
            worker.routes_[demand], worker.model_.demands[demand].width);
        proposal.replacements.push_back(
            {demand, worker.routes_[demand]});
      }
      proposal.generated = worker.capacity_legal();
    } catch (const std::runtime_error&) {
      proposal.generated = false;
    }
    return proposal;
  }

  void run_batched_refinement() {
    const auto batches = build_reroute_batches();
    reroute_conflict_batches_ = static_cast<int>(batches.size());
    for (const auto& batch : batches) {
      maximum_parallel_batch_ = std::max(
          maximum_parallel_batch_, static_cast<int>(batch.size()));
    }
    Objective best = objective();
    const int batch_limit = std::min(
        model_.reroute_rounds, static_cast<int>(batches.size()));
    for (int batch_index = 0; batch_index < batch_limit; ++batch_index) {
      const auto& batch = batches[batch_index];
      std::vector<RerouteProposal> proposals;
      for (std::size_t first = 0; first < batch.size();
           first += model_.candidate_workers) {
        const std::size_t last = std::min(
            batch.size(), first + model_.candidate_workers);
        std::vector<std::future<RerouteProposal>> futures;
        for (std::size_t index = first; index < last; ++index) {
          if (model_.candidate_workers == 1) {
            proposals.push_back(propose_reroute(batch[index].path));
          } else {
            futures.push_back(std::async(
                std::launch::async, [this, path = batch[index].path]() {
                  return propose_reroute(path);
                }));
            ++parallel_reroute_tasks_;
          }
        }
        for (auto& future : futures) {
          proposals.push_back(future.get());
        }
      }
      std::sort(
          proposals.begin(), proposals.end(),
          [](const RerouteProposal& left, const RerouteProposal& right) {
            return left.path < right.path;
          });
      if (model_.paths.size() <= 4096) {
        for (const RerouteProposal& proposal : proposals) {
          const std::vector<Route> route_backup = routes_;
          const std::vector<long long> usage_backup = usage_;
          if (proposal.generated) {
            for (const auto& [demand, route] : proposal.replacements) {
              add_usage(routes_[demand], -model_.demands[demand].width);
              routes_[demand] = route;
              add_usage(routes_[demand], model_.demands[demand].width);
            }
          }
          const Objective candidate =
              proposal.generated ? objective() : Objective{};
          if (proposal.generated && capacity_legal() &&
              better(candidate, best)) {
            best = candidate;
            ++accepted_reroutes_;
          } else {
            routes_ = route_backup;
            usage_ = usage_backup;
            ++rolled_back_reroutes_;
          }
        }
        continue;
      }
      const std::vector<Route> route_backup = routes_;
      const std::vector<long long> usage_backup = usage_;
      int generated_proposals = 0;
      for (const RerouteProposal& proposal : proposals) {
        if (proposal.generated) {
          ++generated_proposals;
          for (const auto& [demand, route] : proposal.replacements) {
            add_usage(routes_[demand], -model_.demands[demand].width);
            routes_[demand] = route;
            add_usage(routes_[demand], model_.demands[demand].width);
          }
        }
      }
      const Objective candidate =
          generated_proposals ? objective() : Objective{};
      if (generated_proposals && capacity_legal() && better(candidate, best)) {
        best = candidate;
        accepted_reroutes_ += generated_proposals;
      } else {
        routes_ = route_backup;
        usage_ = usage_backup;
        rolled_back_reroutes_ += static_cast<int>(proposals.size());
      }
    }
  }

  Candidate route_candidate(const std::vector<int>& order,
                            bool delay_demand_balanced) {
    return route_candidate(
        order,
        delay_demand_balanced
            ? CandidateGenerator::kDelayDemandBalanced
            : CandidateGenerator::kShortestPath);
  }

  Candidate route_candidate(const std::vector<int>& order,
                            CandidateGenerator generator) {
    Candidate result;
    for (int iteration = 1; iteration <= model_.max_iterations; ++iteration) {
      std::fill(usage_.begin(), usage_.end(), 0);
      bool reachable = true;
      try {
        for (int demand : order) {
          if (generator == CandidateGenerator::kDelayDemandBalanced) {
            routes_[demand] = delay_demand_balanced_tree(demand, {});
          } else if (
              generator == CandidateGenerator::kNearestTerminalSteiner) {
            routes_[demand] = nearest_terminal_steiner_tree(demand, {});
          } else if (
              generator == CandidateGenerator::kDirectedMetricClosure) {
            routes_[demand] = directed_metric_closure_tree(demand, {});
          } else if (generator == CandidateGenerator::kShallowLight) {
            routes_[demand] = shallow_light_tree(demand, {});
          } else if (generator == CandidateGenerator::kAdaptiveHop) {
            routes_[demand] = adaptive_hop_tree(demand, {});
          } else {
            routes_[demand] = shortest_path_tree(demand, {});
          }
          add_usage(routes_[demand], model_.demands[demand].width);
        }
      } catch (const std::runtime_error&) {
        reachable = false;
      }
      result.iterations = iteration;
      if (reachable) {
        result.generated = true;
        result.routes = routes_;
        result.usage = usage_;
        result.history = history_;
      }
      if (reachable && capacity_legal()) {
        result.feasible = true;
        result.routes = routes_;
        result.usage = usage_;
        result.history = history_;
        result.objective = objective();
        return result;
      }
      for (int domain = 0; domain < static_cast<int>(usage_.size()); ++domain) {
        const long long capacity = capacity_for_domain(domain);
        if (usage_[domain] > capacity) {
          history_[domain] += 1.0 +
              static_cast<double>(usage_[domain] - capacity) / capacity;
        }
      }
    }
    return result;
  }

  void write_candidate_routes(
      std::ostream& output, const std::string& generator,
      const std::vector<Route>& routes, bool feasible) const {
    if (!feasible || routes.size() != model_.demands.size()) {
      return;
    }
    for (int demand = 0; demand < static_cast<int>(routes.size()); ++demand) {
      output << "CANDIDATE " << demand << ' ' << generator << ' '
             << routes[demand].max_delay_ns << ' ';
      if (routes[demand].arcs.empty()) {
        output << '-';
      } else {
        for (std::size_t index = 0; index < routes[demand].arcs.size(); ++index) {
          if (index) {
            output << ',';
          }
          output << routes[demand].arcs[index];
        }
      }
      output << '\n';
    }
  }

  Route route_for_generator(
      int demand, const std::string& generator,
      const std::set<int>& discouraged) const {
    if (generator == "delay-demand-balanced") {
      return delay_demand_balanced_tree(demand, discouraged);
    }
    if (generator == "nearest-terminal-steiner") {
      return nearest_terminal_steiner_tree(demand, discouraged);
    }
    if (generator == "directed-metric-closure") {
      return directed_metric_closure_tree(demand, discouraged);
    }
    if (generator == "shallow-light-tree") {
      return shallow_light_tree(demand, discouraged);
    }
    if (generator == "adaptive-hop-tree") {
      return adaptive_hop_tree(demand, discouraged);
    }
    return shortest_path_tree(demand, discouraged);
  }

  void run_candidate_master(const std::vector<int>& order) {
    struct Alternative {
      const char* generator;
      const std::vector<Route>* routes;
      bool generated;
    };
    const std::vector<Alternative> alternatives = {
        {"shortest-path-tree", &shortest_candidate_routes_,
         shortest_candidate_generated_},
        {"delay-demand-balanced", &balanced_candidate_routes_,
         balanced_candidate_generated_},
        {"nearest-terminal-steiner", &steiner_candidate_routes_,
         steiner_candidate_generated_},
        {"directed-metric-closure", &metric_closure_candidate_routes_,
         metric_closure_candidate_generated_},
        {"shallow-light-tree", &shallow_light_candidate_routes_,
         shallow_light_candidate_generated_},
        {"adaptive-hop-tree", &adaptive_hop_candidate_routes_,
         adaptive_hop_candidate_generated_},
    };
    std::vector<Alternative> feasible_alternatives;
    for (const Alternative& alternative : alternatives) {
      if (alternative.generated &&
          alternative.routes->size() == model_.demands.size()) {
        feasible_alternatives.push_back(alternative);
      }
    }
    constexpr std::size_t kExactCombinationLimit = 200000;
    std::size_t combinations = 1;
    for (std::size_t demand = 0; demand < model_.demands.size(); ++demand) {
      if (combinations >
          kExactCombinationLimit / feasible_alternatives.size()) {
        combinations = kExactCombinationLimit + 1;
        break;
      }
      combinations *= feasible_alternatives.size();
    }
    if (combinations <= kExactCombinationLimit) {
      const std::vector<std::string> initial_selection = master_selection_;
      Objective best_objective;
      bool found = false;
      std::vector<Route> best_routes = routes_;
      std::vector<std::string> best_selection = master_selection_;
      std::fill(usage_.begin(), usage_.end(), 0);
      std::vector<std::string> selection(model_.demands.size());
      std::function<void(int)> enumerate = [&](int demand) {
        if (demand == static_cast<int>(model_.demands.size())) {
          const Objective candidate = objective();
          if (!found || better(candidate, best_objective)) {
            found = true;
            best_objective = candidate;
            best_routes = routes_;
            best_selection = selection;
          }
          return;
        }
        for (const Alternative& alternative : feasible_alternatives) {
          const Route& candidate = (*alternative.routes)[demand];
          routes_[demand] = candidate;
          add_usage(candidate, model_.demands[demand].width);
          if (capacity_legal()) {
            selection[demand] = alternative.generator;
            enumerate(demand + 1);
          }
          add_usage(candidate, -model_.demands[demand].width);
        }
      };
      enumerate(0);
      if (!found) {
        throw std::runtime_error(
            "restricted candidate master found no legal combination");
      }
      routes_ = best_routes;
      master_selection_ = best_selection;
      std::fill(usage_.begin(), usage_.end(), 0);
      for (int demand = 0;
           demand < static_cast<int>(model_.demands.size()); ++demand) {
        add_usage(routes_[demand], model_.demands[demand].width);
        if (master_selection_[demand] != initial_selection[demand]) {
          ++master_switches_;
        }
      }
      master_rounds_ = 1;
      master_exact_ = true;
      return;
    }
    if (!capacity_legal()) {
      throw std::runtime_error(
          "large candidate master requires a legal initial solution");
    }
    Objective global_best = objective();
    const int maximum_rounds = 8;
    for (int round = 0; round < maximum_rounds; ++round) {
      const std::vector<Route> round_routes = routes_;
      const std::vector<long long> round_usage = usage_;
      const std::vector<std::string> round_selection = master_selection_;
      bool changed = false;
      ++master_rounds_;
      for (int demand : order) {
        const Route original = routes_[demand];
        const std::string original_generator = master_selection_[demand];
        add_usage(original, -model_.demands[demand].width);
        Route best_route = original;
        std::string best_generator = original_generator;
        // The exact small master above evaluates the complete objective at
        // every leaf.  Repeating that O(number of timing paths) scan for
        // every demand/candidate is quadratic on contest designs.  The large
        // master instead performs a dual-price style decomposed sweep: each
        // candidate sees the current domain loads and is ranked by its own
        // projected TDM delay, physical delay, and bit-hop footprint.  A
        // complete objective evaluation below certifies the whole sweep and
        // atomically rolls it back if the lexicographic global objective did
        // not improve.
        bool found_local = false;
        std::tuple<double, double, long long, int> best_local;
        int generator_rank = 0;
        for (const Alternative& alternative : alternatives) {
          if (!alternative.generated ||
              alternative.routes->size() != model_.demands.size()) {
            ++generator_rank;
            continue;
          }
          const Route& candidate = (*alternative.routes)[demand];
          routes_[demand] = candidate;
          add_usage(candidate, model_.demands[demand].width);
          bool legal = true;
          std::set<int> touched_domains;
          for (int arc : candidate.arcs) {
            touched_domains.insert(model_.arcs[arc].capacity_domain);
          }
          for (int domain : touched_domains) {
            if (usage_[domain] > capacity_for_domain(domain)) {
              legal = false;
              break;
            }
          }
          if (legal) {
            const auto local = std::make_tuple(
                demand_tdm_delay(demand), candidate.max_delay_ns,
                static_cast<long long>(candidate.arcs.size()) *
                    model_.demands[demand].width,
                generator_rank);
            if (!found_local || local < best_local) {
              found_local = true;
              best_local = local;
              best_route = candidate;
              best_generator = alternative.generator;
            }
          }
          add_usage(candidate, -model_.demands[demand].width);
          ++generator_rank;
        }
        if (!found_local) {
          throw std::runtime_error(
              "large candidate master found no legal candidate");
        }
        routes_[demand] = best_route;
        add_usage(best_route, model_.demands[demand].width);
        if (best_generator != original_generator) {
          master_selection_[demand] = best_generator;
          changed = true;
        }
      }
      if (!changed) {
        break;
      }
      const Objective candidate = objective();
      if (!capacity_legal() || !better(candidate, global_best)) {
        routes_ = round_routes;
        usage_ = round_usage;
        master_selection_ = round_selection;
        break;
      }
      global_best = candidate;
      for (std::size_t demand = 0; demand < master_selection_.size(); ++demand) {
        if (master_selection_[demand] != round_selection[demand]) {
          ++master_switches_;
        }
      }
    }
  }

  int capacity_domain_count() const {
    int result = 0;
    for (const Arc& arc : model_.arcs) {
      result = std::max(result, arc.capacity_domain + 1);
    }
    return result;
  }

  int direction_group_count() const {
    int result = 0;
    for (const Arc& arc : model_.arcs) {
      result = std::max(result, arc.direction_group + 1);
    }
    return result;
  }

  std::pair<double, double> demand_slack_range() const {
    double minimum = kInf;
    double maximum = -kInf;
    for (const Demand& demand : model_.demands) {
      minimum = std::min(minimum, demand.normalized_slack);
      maximum = std::max(maximum, demand.normalized_slack);
    }
    return {minimum, maximum};
  }

  long long capacity_for_domain(int domain) const {
    for (const Arc& arc : model_.arcs) {
      if (arc.capacity_domain == domain) {
        return arc.capacity;
      }
    }
    throw std::runtime_error("unknown capacity domain");
  }

  int lanes_for_domain(int domain) const {
    for (const Arc& arc : model_.arcs) {
      if (arc.capacity_domain == domain) {
        return arc.lanes;
      }
    }
    throw std::runtime_error("unknown capacity domain");
  }

  int estimated_tdm_ratio(int domain, int additional_width = 0) const {
    for (const Arc& arc : model_.arcs) {
      if (arc.capacity_domain == domain && arc.is_sll) {
        return 1;
      }
    }
    const long long signals = usage_[domain] + additional_width;
    if (signals <= 0) {
      return 1;
    }
    const long long raw = std::max(
        static_cast<long long>(model_.min_ratio),
        (signals + lanes_for_domain(domain) - 1) /
            lanes_for_domain(domain));
    if (raw == 1) {
      return 1;
    }
    const long long quantized =
        ((raw + model_.ratio_quantum - 1) / model_.ratio_quantum) *
        model_.ratio_quantum;
    return static_cast<int>(std::min(
        static_cast<long long>(model_.frame_slots), quantized));
  }

  int estimated_max_tdm_ratio() const {
    int result = 1;
    for (int domain = 0; domain < static_cast<int>(usage_.size()); ++domain) {
      result = std::max(result, estimated_tdm_ratio(domain));
    }
    return result;
  }

  double static_arc_cost(const Arc& arc) const {
    return arc.delay_ns + (arc.is_sll ? 0.0 : 1.0e-6);
  }

  void lock_shared_directions() {
    if (direction_lock_.empty()) {
      return;
    }
    // Algorithm 1 performs one all-pairs pass both for relay lookup and
    // majority-flow direction locking.
    std::vector<std::vector<double>> distance(
        model_.node_count, std::vector<double>(model_.node_count, kInf));
    std::vector<std::vector<int>> next_arc(
        model_.node_count, std::vector<int>(model_.node_count, -1));
    for (int node = 0; node < model_.node_count; ++node) {
      distance[node][node] = 0.0;
    }
    for (int arc_index = 0;
         arc_index < static_cast<int>(model_.arcs.size()); ++arc_index) {
      const Arc& arc = model_.arcs[arc_index];
      const double cost = static_arc_cost(arc);
      if (cost + kEps < distance[arc.from][arc.to] ||
          (std::abs(cost - distance[arc.from][arc.to]) <= kEps &&
           arc_index < next_arc[arc.from][arc.to])) {
        distance[arc.from][arc.to] = cost;
        next_arc[arc.from][arc.to] = arc_index;
      }
    }
    for (int relay = 0; relay < model_.node_count; ++relay) {
      for (int source = 0; source < model_.node_count; ++source) {
        if (!std::isfinite(distance[source][relay])) {
          continue;
        }
        for (int sink = 0; sink < model_.node_count; ++sink) {
          const double candidate =
              distance[source][relay] + distance[relay][sink];
          if (candidate + kEps < distance[source][sink]) {
            distance[source][sink] = candidate;
            next_arc[source][sink] = next_arc[source][relay];
          }
        }
      }
    }

    std::vector<std::unordered_map<int, long long>> votes(direction_lock_.size());
    for (const Demand& demand : model_.demands) {
      for (int sink : demand.sinks) {
        if (!std::isfinite(distance[demand.source][sink])) {
          throw std::runtime_error(
              "unreachable sink during direction locking");
        }
        int node = demand.source;
        std::set<int> seen;
        while (node != sink) {
          if (!seen.insert(node).second) {
            throw std::runtime_error(
                "Floyd-Warshall relay cycle during direction locking");
          }
          const int arc_index = next_arc[node][sink];
          if (arc_index < 0) {
            throw std::runtime_error(
                "broken Floyd-Warshall relay during direction locking");
          }
          const Arc& arc = model_.arcs[arc_index];
          if (arc.direction_group >= 0) {
            votes[arc.direction_group][arc_index] += demand.width;
          }
          node = arc.to;
        }
      }
    }
    for (int group = 0; group < static_cast<int>(direction_lock_.size()); ++group) {
      long long best_votes = -1;
      int best_arc = -1;
      for (const auto& [arc, count] : votes[group]) {
        if (count > best_votes || (count == best_votes && arc < best_arc)) {
          best_votes = count;
          best_arc = arc;
        }
      }
      if (best_arc < 0) {
        for (int index = 0; index < static_cast<int>(model_.arcs.size()); ++index) {
          if (model_.arcs[index].direction_group == group) {
            best_arc = index;
            break;
          }
        }
      }
      direction_lock_[group] = best_arc;
    }
  }

  bool direction_allowed(int arc_index) const {
    const Arc& arc = model_.arcs[arc_index];
    return arc.direction_group < 0 ||
        direction_lock_[arc.direction_group] == arc_index;
  }

  std::vector<int> timing_aware_order() const {
    std::vector<int> order(model_.demands.size());
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](int left, int right) {
      const Demand& lhs = model_.demands[left];
      const Demand& rhs = model_.demands[right];
      if (lhs.normalized_slack != rhs.normalized_slack) {
        return lhs.normalized_slack < rhs.normalized_slack;
      }
      if (lhs.predicted_delay_ns != rhs.predicted_delay_ns) {
        return lhs.predicted_delay_ns > rhs.predicted_delay_ns;
      }
      return left < right;
    });
    return order;
  }

  Route shortest_path_tree(int demand_index,
                           const std::set<int>& discouraged) const {
    if (model_.max_route_hops > 0) {
      return hop_bounded_shortest_path_tree(demand_index, discouraged);
    }
    const Demand& demand = model_.demands[demand_index];
    using QueueItem = std::pair<double, int>;
    std::priority_queue<QueueItem, std::vector<QueueItem>,
                        std::greater<QueueItem>> queue;
    std::vector<double> distance(model_.node_count, kInf);
    std::vector<int> predecessor(model_.node_count, -1);
    distance[demand.source] = 0.0;
    queue.emplace(0.0, demand.source);

    while (!queue.empty()) {
      const auto [current, node] = queue.top();
      queue.pop();
      if (current != distance[node]) {
        continue;
      }
      for (int arc_index : adjacency_[node]) {
        const Arc& arc = model_.arcs[arc_index];
        if (!direction_allowed(arc_index)) {
          continue;
        }
        if (model_.hard_sll_capacity && arc.is_sll &&
            usage_[arc.capacity_domain] + demand.width > arc.capacity) {
          continue;
        }
        const double projected =
            static_cast<double>(usage_[arc.capacity_domain] + demand.width) /
            arc.capacity;
        const double timing_weight =
            1.0 + model_.lambda_timing * demand_criticality_[demand_index];
        // Below 10% utilization, SLL occupancy is too sparse to be a useful
        // topology discriminator.  The dead zone preserves shortest routes
        // on small instances; above it, rescale to [0, 1] so the load term
        // progressively balances scarce, non-TDM SLL capacity.
        const double load_pressure = model_.hard_sll_capacity
            ? std::max(0.0, (projected - 0.1) / 0.9)
            : projected;
        double edge_cost = timing_weight * arc.delay_ns +
            model_.lambda_load * load_pressure +
            model_.lambda_history * history_[arc.capacity_domain];
        if (!arc.is_sll) {
          const int projected_ratio =
              estimated_tdm_ratio(arc.capacity_domain, demand.width);
          edge_cost += model_.lambda_tdm * timing_weight *
              arc.beta_ns * (projected_ratio - 1);
        }
        if (discouraged.count(arc_index)) {
          edge_cost += model_.lambda_timing *
              std::max(1.0, arc.delay_ns) *
              (1.0 + demand_criticality_[demand_index]);
        }
        const double candidate = current + edge_cost;
        if (candidate + kEps < distance[arc.to] ||
            (std::abs(candidate - distance[arc.to]) <= kEps &&
             arc_index < predecessor[arc.to])) {
          distance[arc.to] = candidate;
          predecessor[arc.to] = arc_index;
          queue.emplace(candidate, arc.to);
        }
      }
    }

    std::set<int> tree_arcs;
    double max_delay = 0.0;
    for (int sink : demand.sinks) {
      if (!std::isfinite(distance[sink])) {
        throw std::runtime_error("demand has unreachable sink");
      }
      double delay = 0.0;
      std::set<int> seen;
      for (int node = sink; node != demand.source;) {
        if (!seen.insert(node).second) {
          throw std::runtime_error("predecessor cycle");
        }
        const int arc_index = predecessor[node];
        if (arc_index < 0) {
          throw std::runtime_error("broken predecessor");
        }
        tree_arcs.insert(arc_index);
        delay += model_.arcs[arc_index].delay_ns;
        node = model_.arcs[arc_index].from;
      }
      max_delay = std::max(max_delay, delay);
    }
    Route route;
    route.arcs.assign(tree_arcs.begin(), tree_arcs.end());
    route.max_delay_ns = max_delay;
    return route;
  }

  double routing_arc_cost(int demand_index, int arc_index,
                          const std::set<int>& discouraged) const {
    const Demand& demand = model_.demands[demand_index];
    const Arc& arc = model_.arcs[arc_index];
    const double projected =
        static_cast<double>(usage_[arc.capacity_domain] + demand.width) /
        arc.capacity;
    const double timing_weight =
        1.0 + model_.lambda_timing * demand_criticality_[demand_index];
    const double load_pressure = model_.hard_sll_capacity
        ? std::max(0.0, (projected - 0.1) / 0.9)
        : projected;
    double edge_cost = timing_weight * arc.delay_ns +
        model_.lambda_load * load_pressure +
        model_.lambda_history * history_[arc.capacity_domain];
    if (arc.capacity_domain <
        static_cast<int>(model_.feedback_price.size())) {
      edge_cost += model_.lambda_history *
          model_.feedback_price[arc.capacity_domain];
    }
    if (!arc.is_sll) {
      const int projected_ratio =
          estimated_tdm_ratio(arc.capacity_domain, demand.width);
      edge_cost += model_.lambda_tdm * timing_weight *
          arc.beta_ns * (projected_ratio - 1);
    }
    if (discouraged.count(arc_index)) {
      edge_cost += model_.lambda_timing * std::max(1.0, arc.delay_ns) *
          (1.0 + demand_criticality_[demand_index]);
    }
    return edge_cost;
  }

  Route hop_bounded_shortest_path_tree(
      int demand_index, const std::set<int>& discouraged,
      int hop_limit = -1) const {
    const Demand& demand = model_.demands[demand_index];
    const int maximum_hops =
        hop_limit > 0 ? hop_limit : model_.max_route_hops;
    if (maximum_hops <= 0) {
      throw std::runtime_error("hop-bounded routing needs a positive limit");
    }
    const int stride = maximum_hops + 1;
    const int state_count = model_.node_count * stride;
    using QueueItem = std::pair<double, int>;
    std::priority_queue<QueueItem, std::vector<QueueItem>,
                        std::greater<QueueItem>> queue;
    std::vector<double> distance(state_count, kInf);
    std::vector<int> predecessor_state(state_count, -1);
    std::vector<int> predecessor_arc(state_count, -1);
    const int source_state = demand.source * stride;
    distance[source_state] = 0.0;
    queue.emplace(0.0, source_state);

    while (!queue.empty()) {
      const auto [current, state] = queue.top();
      queue.pop();
      if (current != distance[state]) {
        continue;
      }
      const int node = state / stride;
      const int hops = state % stride;
      if (hops == maximum_hops) {
        continue;
      }
      for (int arc_index : adjacency_[node]) {
        const Arc& arc = model_.arcs[arc_index];
        if (!direction_allowed(arc_index)) {
          continue;
        }
        if (model_.hard_sll_capacity && arc.is_sll &&
            usage_[arc.capacity_domain] + demand.width > arc.capacity) {
          continue;
        }
        const int next_state = arc.to * stride + hops + 1;
        const double candidate = current +
            routing_arc_cost(demand_index, arc_index, discouraged);
        if (candidate + kEps < distance[next_state] ||
            (std::abs(candidate - distance[next_state]) <= kEps &&
             arc_index < predecessor_arc[next_state])) {
          distance[next_state] = candidate;
          predecessor_state[next_state] = state;
          predecessor_arc[next_state] = arc_index;
          queue.emplace(candidate, next_state);
        }
      }
    }

    // Union one legal path per sink, then extract a deterministic
    // source-rooted minimum-hop arborescence.  This removes duplicate parents
    // that can arise when the same physical vertex has different layered
    // states while preserving the hop bound for every sink.
    std::set<int> union_arcs;
    for (int sink : demand.sinks) {
      int best_state = -1;
      for (int hops = 0; hops <= maximum_hops; ++hops) {
        const int state = sink * stride + hops;
        if (best_state < 0 || distance[state] < distance[best_state]) {
          best_state = state;
        }
      }
      if (best_state < 0 || !std::isfinite(distance[best_state])) {
        throw std::runtime_error("demand has no path within maximum hops");
      }
      for (int state = best_state; state != source_state;) {
        const int arc_index = predecessor_arc[state];
        if (arc_index < 0) {
          throw std::runtime_error("broken hop-bounded predecessor");
        }
        union_arcs.insert(arc_index);
        state = predecessor_state[state];
      }
    }

    std::vector<std::vector<int>> union_adjacency(model_.node_count);
    for (int arc_index : union_arcs) {
      union_adjacency[model_.arcs[arc_index].from].push_back(arc_index);
    }
    for (auto& outgoing : union_adjacency) {
      std::sort(outgoing.begin(), outgoing.end());
    }
    std::vector<int> tree_predecessor(model_.node_count, -1);
    std::vector<int> hop_depth(model_.node_count, -1);
    hop_depth[demand.source] = 0;
    std::queue<int> breadth_first;
    breadth_first.push(demand.source);
    while (!breadth_first.empty()) {
      const int node = breadth_first.front();
      breadth_first.pop();
      for (int arc_index : union_adjacency[node]) {
        const int sink = model_.arcs[arc_index].to;
        if (hop_depth[sink] >= 0) {
          continue;
        }
        hop_depth[sink] = hop_depth[node] + 1;
        tree_predecessor[sink] = arc_index;
        breadth_first.push(sink);
      }
    }

    std::set<int> tree_arcs;
    double max_delay = 0.0;
    for (int sink : demand.sinks) {
      if (hop_depth[sink] < 0 ||
          hop_depth[sink] > maximum_hops) {
        throw std::runtime_error("hop-bounded tree does not span sink");
      }
      double delay = 0.0;
      for (int node = sink; node != demand.source;) {
        const int arc_index = tree_predecessor[node];
        if (arc_index < 0) {
          throw std::runtime_error("broken hop-bounded tree");
        }
        tree_arcs.insert(arc_index);
        delay += model_.arcs[arc_index].delay_ns;
        node = model_.arcs[arc_index].from;
      }
      max_delay = std::max(max_delay, delay);
    }
    Route route;
    route.arcs.assign(tree_arcs.begin(), tree_arcs.end());
    route.max_delay_ns = max_delay;
    return route;
  }

  int maximum_route_hops(const Route& route, const Demand& demand) const {
    std::vector<std::vector<int>> tree(model_.node_count);
    for (int arc_index : route.arcs) {
      tree[model_.arcs[arc_index].from].push_back(arc_index);
    }
    std::vector<int> depth(model_.node_count, -1);
    depth[demand.source] = 0;
    std::queue<int> queue;
    queue.push(demand.source);
    while (!queue.empty()) {
      const int node = queue.front();
      queue.pop();
      for (int arc_index : tree[node]) {
        const int sink = model_.arcs[arc_index].to;
        if (depth[sink] >= 0) {
          continue;
        }
        depth[sink] = depth[node] + 1;
        queue.push(sink);
      }
    }
    int result = 0;
    for (int sink : demand.sinks) {
      if (depth[sink] < 0) {
        throw std::runtime_error("route tree does not span sink");
      }
      result = std::max(result, depth[sink]);
    }
    return result;
  }

  Route shortest_path_tree_with_limit(
      int demand_index, const std::set<int>& discouraged,
      int hop_limit) const {
    // A previous implementation cloned the complete Input merely to replace
    // max_route_hops.  On a 100k+ demand design that made the adaptive-hop
    // generator quadratic in input size.  Keep the shared immutable model
    // and pass the per-demand limit as local search state instead.
    return hop_bounded_shortest_path_tree(
        demand_index, discouraged, hop_limit);
  }

  int minimum_hop_lower_bound(int demand_index) const {
    const Demand& demand = model_.demands[demand_index];
    std::vector<int> depth(model_.node_count, -1);
    std::queue<int> queue;
    depth[demand.source] = 0;
    queue.push(demand.source);
    while (!queue.empty()) {
      const int node = queue.front();
      queue.pop();
      for (int arc_index : adjacency_[node]) {
        if (!direction_allowed(arc_index)) {
          continue;
        }
        const int sink = model_.arcs[arc_index].to;
        if (depth[sink] < 0) {
          depth[sink] = depth[node] + 1;
          queue.push(sink);
        }
      }
    }
    int result = 0;
    for (int sink : demand.sinks) {
      if (depth[sink] < 0) {
        throw std::runtime_error("adaptive-hop sink is unreachable");
      }
      result = std::max(result, depth[sink]);
    }
    return result;
  }

  Route adaptive_hop_tree(
      int demand_index, const std::set<int>& discouraged) const {
    const int lower = minimum_hop_lower_bound(demand_index);
    const double criticality = demand_criticality_[demand_index];
    const int allowance = std::max(
        0, static_cast<int>(std::floor((1.0 - criticality) * 2.0 + kEps)));
    int limit = lower + allowance;
    if (model_.max_route_hops > 0) {
      limit = std::min(limit, model_.max_route_hops);
    }
    return shortest_path_tree_with_limit(
        demand_index, discouraged, limit);
  }

  Route shallow_light_tree(
      int demand_index, const std::set<int>& discouraged) const {
    const Route shortest = shortest_path_tree(demand_index, discouraged);
    const Route steiner = nearest_terminal_steiner_tree(
        demand_index, discouraged);
    const double criticality = demand_criticality_[demand_index];
    const double stretch = 1.0 + 0.5 * (1.0 - criticality);
    if (steiner.max_delay_ns <= shortest.max_delay_ns * stretch + kEps &&
        (steiner.arcs.size() < shortest.arcs.size() ||
         (steiner.arcs.size() == shortest.arcs.size() &&
          steiner.max_delay_ns + kEps < shortest.max_delay_ns))) {
      return steiner;
    }
    return shortest;
  }

  Route directed_metric_closure_tree(
      int demand_index, const std::set<int>& discouraged) const {
    const Demand& demand = model_.demands[demand_index];
    std::vector<int> terminals = {demand.source};
    terminals.insert(terminals.end(), demand.sinks.begin(), demand.sinks.end());
    std::sort(terminals.begin() + 1, terminals.end());
    const int count = static_cast<int>(terminals.size());
    std::vector<std::vector<double>> distance(
        count, std::vector<double>(model_.node_count, kInf));
    std::vector<std::vector<int>> predecessor(
        count, std::vector<int>(model_.node_count, -1));
    using QueueItem = std::pair<double, int>;
    for (int terminal = 0; terminal < count; ++terminal) {
      std::priority_queue<QueueItem, std::vector<QueueItem>,
                          std::greater<QueueItem>> queue;
      distance[terminal][terminals[terminal]] = 0.0;
      queue.emplace(0.0, terminals[terminal]);
      while (!queue.empty()) {
        const auto [current, node] = queue.top();
        queue.pop();
        if (current != distance[terminal][node]) {
          continue;
        }
        for (int arc_index : adjacency_[node]) {
          if (!direction_allowed(arc_index)) {
            continue;
          }
          const Arc& arc = model_.arcs[arc_index];
          const double candidate = current +
              routing_arc_cost(demand_index, arc_index, discouraged);
          if (candidate + kEps < distance[terminal][arc.to] ||
              (std::abs(candidate - distance[terminal][arc.to]) <= kEps &&
               (predecessor[terminal][arc.to] < 0 ||
                arc_index < predecessor[terminal][arc.to]))) {
            distance[terminal][arc.to] = candidate;
            predecessor[terminal][arc.to] = arc_index;
            queue.emplace(candidate, arc.to);
          }
        }
      }
    }
    std::vector<bool> in_closure(count, false);
    in_closure[0] = true;
    std::set<int> expanded;
    for (int step = 1; step < count; ++step) {
      int best_from = -1;
      int best_to = -1;
      double best = kInf;
      for (int from = 0; from < count; ++from) {
        if (!in_closure[from]) {
          continue;
        }
        for (int to = 1; to < count; ++to) {
          const double candidate = distance[from][terminals[to]];
          if (in_closure[to] || !std::isfinite(candidate)) {
            continue;
          }
          if (candidate + kEps < best ||
              (std::abs(candidate - best) <= kEps &&
               std::pair<int, int>{from, to} <
                   std::pair<int, int>{best_from, best_to})) {
            best = candidate;
            best_from = from;
            best_to = to;
          }
        }
      }
      if (best_to < 0) {
        throw std::runtime_error("directed metric closure is disconnected");
      }
      for (int node = terminals[best_to]; node != terminals[best_from];) {
        const int arc_index = predecessor[best_from][node];
        if (arc_index < 0) {
          throw std::runtime_error("broken metric-closure predecessor");
        }
        expanded.insert(arc_index);
        node = model_.arcs[arc_index].from;
      }
      in_closure[best_to] = true;
    }
    std::vector<std::vector<int>> graph(model_.node_count);
    for (int arc_index : expanded) {
      graph[model_.arcs[arc_index].from].push_back(arc_index);
    }
    for (auto& outgoing : graph) {
      std::sort(outgoing.begin(), outgoing.end());
    }
    std::vector<int> parent(model_.node_count, -1);
    std::vector<double> delay(model_.node_count, kInf);
    delay[demand.source] = 0.0;
    std::queue<int> queue;
    queue.push(demand.source);
    while (!queue.empty()) {
      const int node = queue.front();
      queue.pop();
      for (int arc_index : graph[node]) {
        const Arc& arc = model_.arcs[arc_index];
        if (parent[arc.to] >= 0 || arc.to == demand.source) {
          continue;
        }
        parent[arc.to] = arc_index;
        delay[arc.to] = delay[node] + arc.delay_ns;
        queue.push(arc.to);
      }
    }
    std::set<int> tree;
    Route result;
    for (int sink : demand.sinks) {
      if (!std::isfinite(delay[sink])) {
        throw std::runtime_error(
            "expanded metric closure does not span a sink");
      }
      result.max_delay_ns = std::max(result.max_delay_ns, delay[sink]);
      for (int node = sink; node != demand.source;) {
        const int arc_index = parent[node];
        if (arc_index < 0) {
          throw std::runtime_error("broken metric-closure arborescence");
        }
        tree.insert(arc_index);
        node = model_.arcs[arc_index].from;
      }
    }
    result.arcs.assign(tree.begin(), tree.end());
    if (model_.max_route_hops > 0 &&
        maximum_route_hops(result, demand) > model_.max_route_hops) {
      return hop_bounded_shortest_path_tree(demand_index, discouraged);
    }
    return result;
  }

  Route delay_demand_balanced_tree(
      int demand_index, const std::set<int>& discouraged) const {
    const Demand& demand = model_.demands[demand_index];
    using QueueItem = std::pair<double, int>;

    // DAC 2025 routes more difficult connections first.  Static all-sink
    // distances provide the same ordering signal without another all-pairs
    // matrix per demand.
    std::vector<double> static_distance(model_.node_count, kInf);
    std::priority_queue<QueueItem, std::vector<QueueItem>,
                        std::greater<QueueItem>> static_queue;
    static_distance[demand.source] = 0.0;
    static_queue.emplace(0.0, demand.source);
    while (!static_queue.empty()) {
      const auto [current, node] = static_queue.top();
      static_queue.pop();
      if (current != static_distance[node]) {
        continue;
      }
      for (int arc_index : adjacency_[node]) {
        if (!direction_allowed(arc_index)) {
          continue;
        }
        const Arc& arc = model_.arcs[arc_index];
        const double candidate = current + static_arc_cost(arc);
        if (candidate + kEps < static_distance[arc.to]) {
          static_distance[arc.to] = candidate;
          static_queue.emplace(candidate, arc.to);
        }
      }
    }
    std::vector<int> sinks = demand.sinks;
    std::stable_sort(sinks.begin(), sinks.end(), [&](int lhs, int rhs) {
      if (static_distance[lhs] != static_distance[rhs]) {
        return static_distance[lhs] > static_distance[rhs];
      }
      return lhs < rhs;
    });

    std::vector<bool> in_tree(model_.node_count, false);
    std::vector<double> tree_delay(model_.node_count, kInf);
    std::set<int> tree_arcs;
    in_tree[demand.source] = true;
    tree_delay[demand.source] = 0.0;
    const double criticality = demand_criticality_[demand_index];
    const double timing_weight = 1.0 + model_.lambda_timing * criticality;

    for (int sink : sinks) {
      if (in_tree[sink]) {
        continue;
      }
      std::vector<double> distance(model_.node_count, kInf);
      std::vector<int> predecessor(model_.node_count, -1);
      std::priority_queue<QueueItem, std::vector<QueueItem>,
                          std::greater<QueueItem>> queue;
      for (int node = 0; node < model_.node_count; ++node) {
        if (in_tree[node]) {
          // Reusing the existing prefix is free in routing-resource cost, but
          // its physical delay still contributes to the connection delay.
          distance[node] = timing_weight * tree_delay[node];
          queue.emplace(distance[node], node);
        }
      }
      while (!queue.empty()) {
        const auto [current, node] = queue.top();
        queue.pop();
        if (current != distance[node]) {
          continue;
        }
        for (int arc_index : adjacency_[node]) {
          const Arc& arc = model_.arcs[arc_index];
          if (!direction_allowed(arc_index) || in_tree[arc.to]) {
            continue;
          }
          if (model_.hard_sll_capacity && arc.is_sll &&
              usage_[arc.capacity_domain] + demand.width > arc.capacity) {
            continue;
          }
          const double projected =
              static_cast<double>(
                  usage_[arc.capacity_domain] + demand.width) /
              arc.capacity;
          const int projected_ratio =
              estimated_tdm_ratio(arc.capacity_domain, demand.width);
          // Eq. (2) of DAC 2025 separates the fixed cable delay, the
          // quantized TDM component, and demand/capacity pressure.  SLLs use
          // fixed delay plus negotiated congestion because they do not TDM.
          double edge_cost = timing_weight * arc.delay_ns;
          if (!arc.is_sll) {
            edge_cost += model_.lambda_tdm * timing_weight * arc.beta_ns *
                (projected_ratio - 1);
          }
          // Keep the same sparse-load dead zone in both shortest-path and
          // multicast-tree construction so their cost models agree.
          const double load_pressure = model_.hard_sll_capacity
              ? std::max(0.0, (projected - 0.1) / 0.9)
              : projected;
          edge_cost += model_.lambda_load * load_pressure +
              model_.lambda_history * history_[arc.capacity_domain];
          if (discouraged.count(arc_index)) {
            edge_cost += model_.lambda_timing *
                std::max(1.0, arc.delay_ns) * (1.0 + criticality);
          }
          const double candidate = current + edge_cost;
          if (candidate + kEps < distance[arc.to] ||
              (std::abs(candidate - distance[arc.to]) <= kEps &&
               (predecessor[arc.to] < 0 ||
                arc_index < predecessor[arc.to]))) {
            distance[arc.to] = candidate;
            predecessor[arc.to] = arc_index;
            queue.emplace(candidate, arc.to);
          }
        }
      }
      if (!std::isfinite(distance[sink])) {
        throw std::runtime_error(
            "delay-demand-balanced connection has unreachable sink");
      }
      std::vector<int> addition;
      std::set<int> seen;
      int node = sink;
      while (!in_tree[node]) {
        if (!seen.insert(node).second) {
          throw std::runtime_error(
              "delay-demand-balanced predecessor cycle");
        }
        const int arc_index = predecessor[node];
        if (arc_index < 0) {
          throw std::runtime_error(
              "broken delay-demand-balanced predecessor");
        }
        addition.push_back(arc_index);
        node = model_.arcs[arc_index].from;
      }
      std::reverse(addition.begin(), addition.end());
      for (int arc_index : addition) {
        const Arc& arc = model_.arcs[arc_index];
        if (!in_tree[arc.from] || in_tree[arc.to]) {
          throw std::runtime_error(
              "delay-demand-balanced route is not an arborescence");
        }
        tree_delay[arc.to] = tree_delay[arc.from] + arc.delay_ns;
        in_tree[arc.to] = true;
        tree_arcs.insert(arc_index);
      }
    }

    Route route;
    route.arcs.assign(tree_arcs.begin(), tree_arcs.end());
    route.max_delay_ns = 0.0;
    for (int sink : demand.sinks) {
      if (!in_tree[sink] || !std::isfinite(tree_delay[sink])) {
        throw std::runtime_error(
            "delay-demand-balanced tree does not span all sinks");
      }
      route.max_delay_ns = std::max(route.max_delay_ns, tree_delay[sink]);
    }
    if (model_.max_route_hops > 0 &&
        maximum_route_hops(route, demand) > model_.max_route_hops) {
      return hop_bounded_shortest_path_tree(demand_index, discouraged);
    }
    return route;
  }

  // Takahashi-Matsuyama grows a Steiner tree by repeatedly attaching the
  // closest unspanned terminal to any vertex already in the tree.  The
  // directed adaptation below uses the same negotiated arc-cost model as the
  // production router and then independently validates the arborescence.
  Route nearest_terminal_steiner_tree(
      int demand_index, const std::set<int>& discouraged) const {
    const Demand& demand = model_.demands[demand_index];
    using QueueItem = std::pair<double, int>;
    std::vector<bool> in_tree(model_.node_count, false);
    std::vector<double> tree_delay(model_.node_count, kInf);
    std::set<int> tree_arcs;
    in_tree[demand.source] = true;
    tree_delay[demand.source] = 0.0;
    int remaining = static_cast<int>(demand.sinks.size());

    while (remaining > 0) {
      std::vector<double> distance(model_.node_count, kInf);
      std::vector<int> predecessor(model_.node_count, -1);
      std::priority_queue<QueueItem, std::vector<QueueItem>,
                          std::greater<QueueItem>> queue;
      for (int node = 0; node < model_.node_count; ++node) {
        if (in_tree[node]) {
          distance[node] = 0.0;
          queue.emplace(0.0, node);
        }
      }
      while (!queue.empty()) {
        const auto [current, node] = queue.top();
        queue.pop();
        if (current != distance[node]) {
          continue;
        }
        for (int arc_index : adjacency_[node]) {
          const Arc& arc = model_.arcs[arc_index];
          if (!direction_allowed(arc_index) || in_tree[arc.to]) {
            continue;
          }
          if (model_.hard_sll_capacity && arc.is_sll &&
              usage_[arc.capacity_domain] + demand.width > arc.capacity) {
            continue;
          }
          const double candidate = current +
              routing_arc_cost(demand_index, arc_index, discouraged);
          if (candidate + kEps < distance[arc.to] ||
              (std::abs(candidate - distance[arc.to]) <= kEps &&
               (predecessor[arc.to] < 0 ||
                arc_index < predecessor[arc.to]))) {
            distance[arc.to] = candidate;
            predecessor[arc.to] = arc_index;
            queue.emplace(candidate, arc.to);
          }
        }
      }

      int selected_sink = -1;
      for (int sink : demand.sinks) {
        if (in_tree[sink]) {
          continue;
        }
        if (selected_sink < 0 ||
            distance[sink] + kEps < distance[selected_sink] ||
            (std::abs(distance[sink] - distance[selected_sink]) <= kEps &&
             sink < selected_sink)) {
          selected_sink = sink;
        }
      }
      if (selected_sink < 0 || !std::isfinite(distance[selected_sink])) {
        throw std::runtime_error(
            "nearest-terminal Steiner tree has unreachable sink");
      }

      std::vector<int> addition;
      std::set<int> seen;
      int node = selected_sink;
      while (!in_tree[node]) {
        if (!seen.insert(node).second) {
          throw std::runtime_error(
              "nearest-terminal Steiner predecessor cycle");
        }
        const int arc_index = predecessor[node];
        if (arc_index < 0) {
          throw std::runtime_error(
              "broken nearest-terminal Steiner predecessor");
        }
        addition.push_back(arc_index);
        node = model_.arcs[arc_index].from;
      }
      std::reverse(addition.begin(), addition.end());
      for (int arc_index : addition) {
        const Arc& arc = model_.arcs[arc_index];
        if (!in_tree[arc.from] || in_tree[arc.to]) {
          throw std::runtime_error(
              "nearest-terminal Steiner route is not an arborescence");
        }
        tree_delay[arc.to] = tree_delay[arc.from] + arc.delay_ns;
        in_tree[arc.to] = true;
        tree_arcs.insert(arc_index);
      }
      remaining = 0;
      for (int sink : demand.sinks) {
        if (!in_tree[sink]) {
          ++remaining;
        }
      }
    }

    Route route;
    route.arcs.assign(tree_arcs.begin(), tree_arcs.end());
    route.max_delay_ns = 0.0;
    for (int sink : demand.sinks) {
      route.max_delay_ns = std::max(route.max_delay_ns, tree_delay[sink]);
    }
    if (model_.max_route_hops > 0 &&
        maximum_route_hops(route, demand) > model_.max_route_hops) {
      return hop_bounded_shortest_path_tree(demand_index, discouraged);
    }
    return route;
  }

  void add_usage(const Route& route, int signed_width) {
    for (int arc_index : route.arcs) {
      usage_[model_.arcs[arc_index].capacity_domain] += signed_width;
    }
  }

  bool capacity_legal() const {
    for (int domain = 0; domain < static_cast<int>(usage_.size()); ++domain) {
      if (usage_[domain] > capacity_for_domain(domain)) {
        return false;
      }
    }
    return true;
  }

  double normalized_slack(const TimingPath& path, double slack) const {
    if (slack >= 0.0) {
      return slack * path.clock_period_ns /
          (model_.slack_positive_scale * model_.max_clock_period_ns);
    }
    return slack /
        (model_.slack_negative_scale * path.clock_period_ns);
  }

  std::tuple<double, double, double> path_metrics(int path_index) const {
    const TimingPath& path = model_.paths[path_index];
    double delay = path.fixed_delay_ns;
    for (int demand : path.demands) {
      delay += routes_[demand].max_delay_ns;
    }
    const double transport_delay = delay - path.fixed_delay_ns;
    const double slack = path.baseline_slack_ns - transport_delay;
    return {delay, slack, normalized_slack(path, slack)};
  }

  int worst_path_index() const {
    int result = -1;
    double worst = kInf;
    for (int index = 0; index < static_cast<int>(model_.paths.size()); ++index) {
      const double normalized = std::get<2>(path_metrics(index));
      if (normalized < worst) {
        worst = normalized;
        result = index;
      }
    }
    return result;
  }

  double demand_tdm_delay(int demand_index) const {
    const Demand& demand = model_.demands[demand_index];
    if (model_.tree_edge_sum_tdm) {
      double result = 0.0;
      for (int arc_index : routes_[demand_index].arcs) {
        const Arc& arc = model_.arcs[arc_index];
        result += arc.delay_ns + arc.beta_ns *
            (estimated_tdm_ratio(arc.capacity_domain) - 1);
      }
      return result;
    }
    std::vector<std::vector<int>> tree(model_.node_count);
    for (int arc_index : routes_[demand_index].arcs) {
      tree[model_.arcs[arc_index].from].push_back(arc_index);
    }
    std::vector<double> delay(model_.node_count, -kInf);
    delay[demand.source] = 0.0;
    std::queue<int> queue;
    queue.push(demand.source);
    while (!queue.empty()) {
      const int node = queue.front();
      queue.pop();
      for (int arc_index : tree[node]) {
        const Arc& arc = model_.arcs[arc_index];
        delay[arc.to] = delay[node] + arc.delay_ns +
            arc.beta_ns *
                (estimated_tdm_ratio(arc.capacity_domain) - 1);
        queue.push(arc.to);
      }
    }
    double result = 0.0;
    for (int sink : demand.sinks) {
      if (!std::isfinite(delay[sink])) {
        throw std::runtime_error("TDM estimate encountered disconnected tree");
      }
      result = std::max(result, delay[sink]);
    }
    return result;
  }

  std::tuple<double, double, double> tdm_path_metrics(
      int path_index) const {
    const TimingPath& path = model_.paths[path_index];
    double delay = path.fixed_delay_ns;
    for (int demand : path.demands) {
      delay += demand_tdm_delay(demand);
    }
    const double transport_delay = delay - path.fixed_delay_ns;
    const double slack = path.baseline_slack_ns - transport_delay;
    return {delay, slack, normalized_slack(path, slack)};
  }

  int worst_tdm_path_index() const {
    int result = -1;
    double worst = kInf;
    for (int index = 0; index < static_cast<int>(model_.paths.size()); ++index) {
      const double normalized = std::get<2>(tdm_path_metrics(index));
      if (normalized < worst) {
        worst = normalized;
        result = index;
      }
    }
    return result;
  }

  std::string path_signature(int path_index) const {
    std::ostringstream signature;
    bool first = true;
    for (int demand : model_.paths[path_index].demands) {
      for (int arc : routes_[demand].arcs) {
        if (!first) {
          signature << ',';
        }
        first = false;
        signature << arc;
      }
    }
    return first ? "-" : signature.str();
  }

  Objective objective() const {
    Objective result;
    result.worst_tdm_normalized_slack = kInf;
    result.worst_tdm_slack_ns = kInf;
    result.worst_normalized_slack = kInf;
    result.worst_slack_ns = kInf;
    for (int path_index = 0;
         path_index < static_cast<int>(model_.paths.size()); ++path_index) {
      const auto [delay, slack, normalized] = path_metrics(path_index);
      (void)delay;
      result.worst_slack_ns = std::min(result.worst_slack_ns, slack);
      result.worst_normalized_slack =
          std::min(result.worst_normalized_slack, normalized);
      const auto [tdm_delay, tdm_slack, tdm_normalized] =
          tdm_path_metrics(path_index);
      (void)tdm_delay;
      result.worst_tdm_slack_ns =
          std::min(result.worst_tdm_slack_ns, tdm_slack);
      result.worst_tdm_normalized_slack =
          std::min(result.worst_tdm_normalized_slack, tdm_normalized);
    }
    if (model_.paths.empty()) {
      result.worst_slack_ns = 0.0;
      result.worst_normalized_slack = 0.0;
      result.worst_tdm_slack_ns = 0.0;
      result.worst_tdm_normalized_slack = 0.0;
    }
    result.max_utilization = 0.0;
    result.bit_hops = 0;
    for (int domain = 0; domain < static_cast<int>(usage_.size()); ++domain) {
      result.max_utilization =
          std::max(result.max_utilization,
                   static_cast<double>(usage_[domain]) /
                       capacity_for_domain(domain));
      result.bit_hops += usage_[domain];
    }
    return result;
  }

  bool better(const Objective& candidate, const Objective& best) const {
    if (model_.lambda_tdm > kEps) {
      if (candidate.worst_tdm_normalized_slack >
          best.worst_tdm_normalized_slack + kEps) {
        return true;
      }
      if (std::abs(candidate.worst_tdm_normalized_slack -
                   best.worst_tdm_normalized_slack) > kEps) {
        return false;
      }
    }
    if (candidate.worst_normalized_slack >
        best.worst_normalized_slack + kEps) {
      return true;
    }
    if (std::abs(candidate.worst_normalized_slack -
                 best.worst_normalized_slack) > kEps) {
      return false;
    }
    if (candidate.max_utilization + kEps < best.max_utilization) {
      return true;
    }
    if (std::abs(candidate.max_utilization - best.max_utilization) > kEps) {
      return false;
    }
    return candidate.bit_hops < best.bit_hops;
  }

  std::shared_ptr<const Input> owned_model_;
  const Input& model_;
  std::vector<std::vector<int>> adjacency_;
  std::vector<long long> usage_;
  std::vector<double> history_;
  std::vector<int> direction_lock_;
  std::vector<Route> routes_;
  std::vector<double> demand_criticality_;
  int completed_iterations_ = 0;
  int accepted_reroutes_ = 0;
  int rolled_back_reroutes_ = 0;
  bool baseline_candidate_feasible_ = false;
  bool balanced_candidate_feasible_ = false;
  bool steiner_candidate_feasible_ = false;
  bool metric_closure_candidate_feasible_ = false;
  bool shallow_light_candidate_feasible_ = false;
  bool adaptive_hop_candidate_feasible_ = false;
  bool shortest_candidate_generated_ = false;
  bool balanced_candidate_generated_ = false;
  bool steiner_candidate_generated_ = false;
  bool metric_closure_candidate_generated_ = false;
  bool shallow_light_candidate_generated_ = false;
  bool adaptive_hop_candidate_generated_ = false;
  bool selected_balanced_ = false;
  std::vector<Route> shortest_candidate_routes_;
  std::vector<Route> balanced_candidate_routes_;
  std::vector<Route> steiner_candidate_routes_;
  std::vector<Route> metric_closure_candidate_routes_;
  std::vector<Route> shallow_light_candidate_routes_;
  std::vector<Route> adaptive_hop_candidate_routes_;
  std::vector<std::string> master_selection_;
  int master_rounds_ = 0;
  int master_switches_ = 0;
  bool master_exact_ = false;
  int parallel_candidate_tasks_ = 0;
  int reroute_conflict_batches_ = 0;
  int maximum_parallel_batch_ = 0;
  int parallel_reroute_tasks_ = 0;
};

void usage(const char* executable) {
  std::cerr << "usage: " << executable << " INPUT OUTPUT\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    usage(argv[0]);
    return 0;
  }
  if (argc != 3) {
    usage(argv[0]);
    return 2;
  }
  try {
    Router router(read_input(argv[1]));
    router.run();
    router.write_output(argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_tlr_router: " << error.what() << '\n';
    return 1;
  }
}
