#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

constexpr double kQuantum = 1.0e-9;

struct Domain {
  int lanes = 0;
  bool is_sll = false;
  double cycle_ns = 0.0;
};

struct Arc {
  int domain = -1;
  double delay_ns = 0.0;
};

struct Route {
  bool reachable = false;
  std::vector<Arc> arcs;
};

struct Cluster {
  int part = -1;
  int fixed = -1;
  std::vector<double> weight;
};

struct Net {
  std::vector<int> drivers;
  std::vector<int> sinks;
};

struct Transition {
  int source = -1;
  int sink = -1;
  int sink_clusters = 0;
};

struct Path {
  double period_ns = 0.0;
  double slack_ns = 0.0;
  int start_cluster = -1;
  int end_cluster = -1;
  std::vector<int> nets;
  int feedback_source = -1;
  int feedback_sink = -1;
  double feedback_residual_ns = 0.0;
};

struct Model {
  int parts = 0;
  int clusters = 0;
  int dimensions = 0;
  int domains = 0;
  int nets = 0;
  int paths = 0;
  int max_hops = -1;
  int frame_slots = 0;
  int ratio_quantum = 1;
  int min_used_parts = 1;
  int max_moves = 0;
  int max_sweeps = 1;
  int ejection_critical_limit = 0;
  int ejection_donor_limit = 0;
  int max_ejections = 0;
  bool flow_refinement = false;
  int flow_max_clusters = 0;
  int flow_corridor_distance = 0;
  int flow_piercing_strategy = 0;
  int flow_max_legal_candidates = 0;
  int flow_max_polish_moves = 0;
  int flow_max_frontier_paths = 0;
  int flow_max_tail_moves = 0;
  int flow_version = 0;
  double positive_scale = 1.0;
  double negative_scale = 1.0;
  double max_period = 1.0;
  double boundary_fanout_penalty_scale_ns = 0.0;
  double physical_hop_guard_scale_ns = 0.0;
  std::vector<std::vector<double>> hard_capacity;
  std::vector<std::vector<double>> balance_capacity;
  std::vector<Cluster> cluster;
  std::vector<Domain> domain;
  std::vector<std::vector<Route>> route;
  std::vector<Net> net;
  std::vector<Path> path;
};

struct Evaluation {
  bool feasible = false;
  std::vector<double> objective;
  std::vector<long long> ranked;
};

struct ProxyNetState {
  bool feasible = true;
  double delay_ns = 0.0;
  std::pair<int, int> worst_transition = {-1, -1};
  std::vector<Transition> transitions;
  std::vector<std::pair<int, int>> sink_counts;
  std::vector<std::pair<int, int>> domain_counts;
  long long hops = 0;
  long long cuts = 0;
};

struct ProxyPathState {
  double normalized_slack = 0.0;
  bool negative = false;
  long long snaking = 0;
  std::vector<int> dependency_domains;
};

struct ProxyState {
  std::vector<int> assignment;
  std::vector<std::vector<double>> resource_load;
  std::vector<int> part_counts;
  std::vector<int> domain_load;
  std::vector<int> domain_ratio;
  std::multiset<int> ratio_order;
  std::vector<ProxyNetState> net;
  std::vector<ProxyPathState> path;
  std::vector<std::set<int>> domain_paths;
  std::set<std::pair<double, int>> slack_path_order;
  std::set<std::pair<long long, int>> ranked_path_order;
  double total_negative = 0.0;
  long long negative_paths = 0;
  long long snaking = 0;
  long long hops = 0;
  long long cuts = 0;
  Evaluation evaluation;
};

struct ProxyDelta {
  bool feasible = false;
  int cluster = -1;
  int source = -1;
  int target = -1;
  int partner = -1;
  int partner_source = -1;
  int partner_target = -1;
  Evaluation evaluation;
  std::map<int, ProxyNetState> nets;
  std::map<int, int> domain_delta;
  std::map<int, ProxyPathState> paths;
};

struct FlowEdge {
  int target = -1;
  int reverse = -1;
  long long capacity = 0;
};

void require(bool condition, const std::string& message);

class DinicFlow {
 public:
  explicit DinicFlow(int nodes) : graph_(nodes), level_(nodes), next_(nodes) {}

  void add_edge(int source, int target, long long capacity) {
    require(source >= 0 && source < static_cast<int>(graph_.size())
                && target >= 0 && target < static_cast<int>(graph_.size())
                && capacity >= 0,
            "invalid flow edge");
    const int source_reverse = static_cast<int>(graph_[target].size());
    const int target_reverse = static_cast<int>(graph_[source].size());
    graph_[source].push_back(FlowEdge{target, source_reverse, capacity});
    graph_[target].push_back(FlowEdge{source, target_reverse, 0});
  }

  long long maximum_flow(int source, int sink) {
    long long flow = 0;
    while (build_levels(source, sink)) {
      std::fill(next_.begin(), next_.end(), 0);
      while (true) {
        const long long pushed = push(
            source, sink, std::numeric_limits<long long>::max() / 4);
        if (pushed == 0) {
          break;
        }
        flow += pushed;
      }
    }
    return flow;
  }

  std::vector<bool> source_reachable(int source) const {
    std::vector<bool> reached(graph_.size(), false);
    std::queue<int> work;
    reached[source] = true;
    work.push(source);
    while (!work.empty()) {
      const int node = work.front();
      work.pop();
      for (const FlowEdge& edge : graph_[node]) {
        if (edge.capacity > 0 && !reached[edge.target]) {
          reached[edge.target] = true;
          work.push(edge.target);
        }
      }
    }
    return reached;
  }

 private:
  bool build_levels(int source, int sink) {
    std::fill(level_.begin(), level_.end(), -1);
    std::queue<int> work;
    level_[source] = 0;
    work.push(source);
    while (!work.empty()) {
      const int node = work.front();
      work.pop();
      for (const FlowEdge& edge : graph_[node]) {
        if (edge.capacity > 0 && level_[edge.target] < 0) {
          level_[edge.target] = level_[node] + 1;
          work.push(edge.target);
        }
      }
    }
    return level_[sink] >= 0;
  }

  long long push(int node, int sink, long long capacity) {
    if (node == sink) {
      return capacity;
    }
    for (int& index = next_[node];
         index < static_cast<int>(graph_[node].size());
         ++index) {
      FlowEdge& edge = graph_[node][index];
      if (edge.capacity <= 0
          || level_[edge.target] != level_[node] + 1) {
        continue;
      }
      const long long pushed = push(
          edge.target, sink, std::min(capacity, edge.capacity));
      if (pushed == 0) {
        continue;
      }
      edge.capacity -= pushed;
      graph_[edge.target][edge.reverse].capacity += pushed;
      return pushed;
    }
    return 0;
  }

  std::vector<std::vector<FlowEdge>> graph_;
  std::vector<int> level_;
  std::vector<int> next_;
};

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

long long rank_float(double value) {
  require(std::isfinite(value), "non-finite objective");
  const long double scaled = static_cast<long double>(value) / kQuantum;
  require(scaled >= static_cast<long double>(std::numeric_limits<long long>::min())
              && scaled <= static_cast<long double>(std::numeric_limits<long long>::max()),
          "objective rank overflow");
  return std::llround(scaled);
}

bool less_ranked(const std::vector<long long>& left,
                 const std::vector<long long>& right) {
  return std::lexicographical_compare(
      left.begin(), left.end(), right.begin(), right.end());
}

double normalized_slack(const Model& model, double period, double slack) {
  if (slack >= 0.0) {
    return slack * period / (model.positive_scale * model.max_period);
  }
  return slack / (model.negative_scale * period);
}

int tdm_ratio(const Model& model, int load, const Domain& domain) {
  if (domain.is_sll || load <= 0) {
    return 1;
  }
  int raw = std::max(1, (load + domain.lanes - 1) / domain.lanes);
  if (raw == 1) {
    return 1;
  }
  raw = ((raw + model.ratio_quantum - 1) / model.ratio_quantum)
        * model.ratio_quantum;
  return std::min(model.frame_slots, raw);
}

double predicted_route_delay(const Model& model,
                             const Route& route,
                             const std::vector<int>& ratios) {
  double delay = 0.0;
  for (const Arc& arc : route.arcs) {
    delay += arc.delay_ns
             + std::max(0, ratios[arc.domain] - 1)
                   * model.domain[arc.domain].cycle_ns
             + model.physical_hop_guard_scale_ns;
  }
  return delay;
}

double predicted_transition_delay(const Model& model,
                                  const Route& route,
                                  const std::vector<int>& ratios,
                                  int sink_clusters) {
  require(sink_clusters > 0, "transition has no remote sink clusters");
  return predicted_route_delay(model, route, ratios)
         + model.boundary_fanout_penalty_scale_ns
               * std::log2(1.0 + static_cast<double>(sink_clusters));
}

Model read_model(const std::string& path) {
  std::ifstream stream(path);
  require(stream.good(), "cannot open input");
  std::string token;
  stream >> token;
  const bool flow_v7 = token == "EMUFLOW_PATRON_INPUT_V7";
  const bool flow_v8 = token == "EMUFLOW_PATRON_INPUT_V8";
  const bool flow_v9 = token == "EMUFLOW_PATRON_INPUT_V9";
  const bool flow_v10 = token == "EMUFLOW_PATRON_INPUT_V10";
  const bool flow_v11 = token == "EMUFLOW_PATRON_INPUT_V11";
  const bool flow_input = flow_v7 || flow_v8 || flow_v9 || flow_v10
                          || flow_v11;
  require(token == "EMUFLOW_PATRON_INPUT_V6" || flow_input,
          "invalid input header");
  Model model;
  stream >> token;
  require(token == "PARAM", "missing PARAM");
  stream >> model.parts >> model.clusters >> model.dimensions >> model.domains
      >> model.nets >> model.paths >> model.max_hops >> model.frame_slots
      >> model.ratio_quantum >> model.min_used_parts >> model.max_moves
      >> model.positive_scale >> model.negative_scale >> model.max_period
      >> model.boundary_fanout_penalty_scale_ns >> model.max_sweeps
      >> model.ejection_critical_limit >> model.ejection_donor_limit
      >> model.max_ejections;
  require(stream.good() && model.parts > 0 && model.clusters > 0
              && model.dimensions > 0 && model.domains > 0
              && model.nets > 0 && model.paths > 0
              && model.frame_slots > 0 && model.ratio_quantum > 0
              && model.min_used_parts > 0 && model.max_moves >= 0
              && model.max_sweeps > 0
              && model.ejection_critical_limit >= 0
              && model.ejection_donor_limit >= 0
              && model.max_ejections >= 0
              && std::isfinite(model.boundary_fanout_penalty_scale_ns)
              && model.boundary_fanout_penalty_scale_ns >= 0.0,
          "invalid PARAM");

  if (flow_input) {
    stream >> token;
    require(token == "FLOW", "missing FLOW");
    int enabled = -1;
    stream >> enabled >> model.flow_max_clusters
        >> model.flow_corridor_distance >> model.flow_piercing_strategy
        >> model.flow_max_legal_candidates
        >> model.flow_max_polish_moves;
    if (flow_v8 || flow_v9 || flow_v10 || flow_v11) {
      stream >> model.flow_max_frontier_paths
          >> model.flow_max_tail_moves;
    }
    if (flow_v10 || flow_v11) {
      stream >> model.physical_hop_guard_scale_ns;
    }
    require(stream.good() && enabled == 1
                && model.flow_max_clusters > 0
                && model.flow_max_clusters <= model.clusters
                && model.flow_corridor_distance >= 0
                && model.flow_corridor_distance <= model.clusters
                && model.flow_piercing_strategy >= 0
                && model.flow_piercing_strategy <= 3
                && model.flow_max_legal_candidates > 0
                && model.flow_max_polish_moves >= 0
                && (!(flow_v8 || flow_v9 || flow_v10 || flow_v11)
                    || (model.flow_max_frontier_paths > 0
                        && model.flow_max_tail_moves >= 0)),
            "invalid FLOW");
    require(std::isfinite(model.physical_hop_guard_scale_ns)
                && model.physical_hop_guard_scale_ns >= 0.0,
            "invalid physical hop guard");
    model.flow_refinement = true;
    model.flow_version = flow_v11
                             ? 11
                             : (flow_v10 ? 10
                                         : (flow_v9 ? 9 : (flow_v8 ? 8 : 7)));
  }

  model.hard_capacity.assign(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  model.balance_capacity.assign(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  for (int part = 0; part < model.parts; ++part) {
    stream >> token;
    require(token == "CAP", "missing CAP");
    int index = -1;
    stream >> index;
    require(index == part, "non-canonical CAP index");
    for (int dim = 0; dim < model.dimensions; ++dim) {
      stream >> model.hard_capacity[part][dim]
          >> model.balance_capacity[part][dim];
    }
  }

  model.cluster.resize(model.clusters);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    stream >> token;
    require(token == "CLUSTER", "missing CLUSTER");
    int index = -1;
    stream >> index >> model.cluster[cluster].part
        >> model.cluster[cluster].fixed;
    require(index == cluster, "non-canonical CLUSTER index");
    model.cluster[cluster].weight.resize(model.dimensions);
    for (double& weight : model.cluster[cluster].weight) {
      stream >> weight;
    }
  }

  model.domain.resize(model.domains);
  for (int domain = 0; domain < model.domains; ++domain) {
    stream >> token;
    require(token == "DOMAIN", "missing DOMAIN");
    int index = -1;
    int is_sll = 0;
    stream >> index >> model.domain[domain].lanes >> is_sll
        >> model.domain[domain].cycle_ns;
    require(index == domain && model.domain[domain].lanes > 0,
            "invalid DOMAIN");
    model.domain[domain].is_sll = is_sll != 0;
  }

  model.route.assign(model.parts, std::vector<Route>(model.parts));
  for (int source = 0; source < model.parts; ++source) {
    for (int sink = 0; sink < model.parts; ++sink) {
      stream >> token;
      require(token == "ROUTE", "missing ROUTE");
      int actual_source = -1;
      int actual_sink = -1;
      int arcs = -1;
      stream >> actual_source >> actual_sink >> arcs;
      require(actual_source == source && actual_sink == sink && arcs >= -1,
              "invalid ROUTE");
      Route& route = model.route[source][sink];
      route.reachable = arcs >= 0;
      for (int index = 0; index < arcs; ++index) {
        Arc arc;
        stream >> arc.domain >> arc.delay_ns;
        require(arc.domain >= 0 && arc.domain < model.domains
                    && std::isfinite(arc.delay_ns) && arc.delay_ns >= 0.0,
                "invalid ROUTE arc");
        route.arcs.push_back(arc);
      }
    }
  }

  model.net.resize(model.nets);
  for (int net = 0; net < model.nets; ++net) {
    stream >> token;
    require(token == "NET", "missing NET");
    int index = -1;
    int drivers = -1;
    stream >> index >> drivers;
    require(index == net && drivers > 0, "invalid NET drivers");
    for (int count = 0; count < drivers; ++count) {
      int cluster = -1;
      stream >> cluster;
      require(cluster >= 0 && cluster < model.clusters,
              "invalid NET driver");
      model.net[net].drivers.push_back(cluster);
    }
    int sinks = -1;
    stream >> sinks;
    require(sinks > 0, "invalid NET sinks");
    for (int count = 0; count < sinks; ++count) {
      int cluster = -1;
      stream >> cluster;
      require(cluster >= 0 && cluster < model.clusters,
              "invalid NET sink");
      model.net[net].sinks.push_back(cluster);
    }
  }

  model.path.resize(model.paths);
  for (int path_index = 0; path_index < model.paths; ++path_index) {
    stream >> token;
    require(token == "PATH", "missing PATH");
    int index = -1;
    int nets = -1;
    stream >> index >> model.path[path_index].period_ns
        >> model.path[path_index].slack_ns
        >> model.path[path_index].start_cluster
        >> model.path[path_index].end_cluster >> nets;
    require(index == path_index && model.path[path_index].period_ns > 0.0
                && std::isfinite(model.path[path_index].slack_ns) && nets >= 0,
            "invalid PATH");
    require((model.path[path_index].start_cluster < 0
             && model.path[path_index].end_cluster < 0)
                || (model.path[path_index].start_cluster >= 0
                    && model.path[path_index].start_cluster < model.clusters
                    && model.path[path_index].end_cluster >= 0
                    && model.path[path_index].end_cluster < model.clusters),
            "invalid PATH endpoints");
    for (int count = 0; count < nets; ++count) {
      int net = -1;
      stream >> net;
      require(net >= 0 && net < model.nets, "invalid PATH net");
      model.path[path_index].nets.push_back(net);
    }
    if (flow_v11) {
      stream >> model.path[path_index].feedback_source
          >> model.path[path_index].feedback_sink
          >> model.path[path_index].feedback_residual_ns;
      const bool no_feedback =
          model.path[path_index].feedback_source == -1
          && model.path[path_index].feedback_sink == -1;
      const bool valid_feedback =
          model.path[path_index].feedback_source >= 0
          && model.path[path_index].feedback_source < model.parts
          && model.path[path_index].feedback_sink >= 0
          && model.path[path_index].feedback_sink < model.parts
          && model.path[path_index].feedback_source
                 != model.path[path_index].feedback_sink;
      require(stream.good() && (no_feedback || valid_feedback)
                  && std::isfinite(
                      model.path[path_index].feedback_residual_ns)
                  && model.path[path_index].feedback_residual_ns >= 0.0
                  && (!no_feedback
                      || model.path[path_index].feedback_residual_ns == 0.0),
              "invalid PATH physical feedback");
    }
  }
  stream >> token;
  require(stream.good() && token == "END", "missing END");
  return model;
}

Evaluation evaluate(const Model& model, const std::vector<int>& assignment) {
  Evaluation result;
  std::vector<std::vector<double>> load(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  std::vector<int> part_counts(model.parts, 0);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    const int part = assignment[cluster];
    if (part < 0 || part >= model.parts
        || (model.cluster[cluster].fixed >= 0
            && model.cluster[cluster].fixed != part)) {
      return result;
    }
    ++part_counts[part];
    for (int dim = 0; dim < model.dimensions; ++dim) {
      load[part][dim] += model.cluster[cluster].weight[dim];
    }
  }
  if (std::count_if(part_counts.begin(), part_counts.end(),
                    [](int count) { return count > 0; })
      < model.min_used_parts) {
    return result;
  }
  for (int part = 0; part < model.parts; ++part) {
    for (int dim = 0; dim < model.dimensions; ++dim) {
      if (load[part][dim] > model.hard_capacity[part][dim] + 1.0e-9
          || load[part][dim]
                 > model.balance_capacity[part][dim] + 1.0e-9) {
        return result;
      }
    }
  }

  std::vector<int> domain_load(model.domains, 0);
  std::vector<std::vector<Transition>> transitions(model.nets);
  for (int net_index = 0; net_index < model.nets; ++net_index) {
    std::set<int> sources;
    std::map<int, int> sink_counts;
    for (int cluster : model.net[net_index].drivers) {
      sources.insert(assignment[cluster]);
    }
    for (int cluster : model.net[net_index].sinks) {
      ++sink_counts[assignment[cluster]];
    }
    for (int source : sources) {
      for (const auto& sink_item : sink_counts) {
        const int sink = sink_item.first;
        if (source == sink) {
          continue;
        }
        const Route& route = model.route[source][sink];
        if (!route.reachable
            || (model.max_hops >= 0
                && static_cast<int>(route.arcs.size()) > model.max_hops)) {
          return result;
        }
        transitions[net_index].push_back(
            Transition{source, sink, sink_item.second});
        for (const Arc& arc : route.arcs) {
          ++domain_load[arc.domain];
        }
      }
    }
  }
  std::vector<int> ratios(model.domains, 1);
  int maximum_ratio = 1;
  int maximum_load = 0;
  for (int domain = 0; domain < model.domains; ++domain) {
    ratios[domain] = tdm_ratio(model, domain_load[domain], model.domain[domain]);
    maximum_ratio = std::max(maximum_ratio, ratios[domain]);
    maximum_load = std::max(maximum_load, domain_load[domain]);
  }

  std::vector<double> net_delay(model.nets, 0.0);
  std::vector<std::pair<int, int>> worst_transition(
      model.nets, std::make_pair(-1, -1));
  long long total_hops = 0;
  long long cut_bits = 0;
  for (int net_index = 0; net_index < model.nets; ++net_index) {
    for (const auto& transition : transitions[net_index]) {
      const Route& route = model.route[transition.source][transition.sink];
      const double delay = predicted_transition_delay(
          model, route, ratios, transition.sink_clusters);
      const std::pair<int, int> identity = {
          transition.source, transition.sink};
      if (delay > net_delay[net_index]
          || (std::abs(delay - net_delay[net_index]) <= 1.0e-12
              && (worst_transition[net_index].first < 0
                  || identity < worst_transition[net_index]))) {
        net_delay[net_index] = delay;
        worst_transition[net_index] = identity;
      }
      total_hops += static_cast<long long>(route.arcs.size());
      ++cut_bits;
    }
  }

  double worst_slack = std::numeric_limits<double>::infinity();
  double total_negative = 0.0;
  long long negative_paths = 0;
  long long total_snaking = 0;
  for (const Path& path : model.path) {
    double transport = 0.0;
    std::vector<int> sequence;
    if (path.start_cluster >= 0) {
      int target = assignment[path.end_cluster];
      std::vector<std::pair<int, int>> reverse_transitions;
      for (auto item = path.nets.rbegin(); item != path.nets.rend(); ++item) {
        const Net& net = model.net[*item];
        if (net.drivers.size() != 1) {
          return result;
        }
        const int source = assignment[net.drivers.front()];
        bool reaches_target = false;
        for (int sink : net.sinks) {
          reaches_target = reaches_target || assignment[sink] == target;
        }
        if (!reaches_target) {
          return result;
        }
        if (source == target) {
          continue;
        }
        const int sink_clusters = static_cast<int>(std::count_if(
            net.sinks.begin(), net.sinks.end(), [&](int sink) {
              return assignment[sink] == target;
            }));
        const Route& route = model.route[source][target];
        if (!route.reachable
            || (model.max_hops >= 0
                && static_cast<int>(route.arcs.size()) > model.max_hops)) {
          return result;
        }
        transport += predicted_transition_delay(
            model, route, ratios, sink_clusters);
        reverse_transitions.emplace_back(source, target);
        target = source;
      }
      if (assignment[path.start_cluster] != target) {
        return result;
      }
      for (auto item = reverse_transitions.rbegin();
           item != reverse_transitions.rend(); ++item) {
        for (int part : {item->first, item->second}) {
          if (sequence.empty() || sequence.back() != part) {
            sequence.push_back(part);
          }
        }
      }
    } else {
      for (int net : path.nets) {
        transport += net_delay[net];
        const auto transition = worst_transition[net];
        if (transition.first < 0) {
          continue;
        }
        for (int part : {transition.first, transition.second}) {
          if (sequence.empty() || sequence.back() != part) {
            sequence.push_back(part);
          }
        }
      }
    }
    std::set<int> seen;
    for (int part : sequence) {
      if (seen.count(part)) {
        ++total_snaking;
      }
      seen.insert(part);
    }
    if (sequence.size() > 1
        && sequence.front() == path.feedback_source
        && sequence.back() == path.feedback_sink) {
      transport += path.feedback_residual_ns;
    }
    const double predicted = path.slack_ns - transport;
    const double normalized = normalized_slack(
        model, path.period_ns, predicted);
    worst_slack = std::min(worst_slack, normalized);
    total_negative += std::min(0.0, normalized);
    if (normalized < 0.0) {
      ++negative_paths;
    }
  }
  result.feasible = true;
  result.objective = {
      -worst_slack,
      -total_negative,
      static_cast<double>(negative_paths),
      static_cast<double>(maximum_ratio),
      static_cast<double>(maximum_load),
      static_cast<double>(total_snaking),
      static_cast<double>(total_hops),
      static_cast<double>(cut_bits),
  };
  result.ranked = {
      rank_float(result.objective[0]),
      rank_float(result.objective[1]),
      negative_paths,
      maximum_ratio,
      maximum_load,
      total_snaking,
      total_hops,
      cut_bits,
  };
  return result;
}

ProxyNetState build_proxy_net(const Model& model,
                              const std::vector<int>& assignment,
                              int net_index) {
  ProxyNetState state;
  std::set<int> sources;
  std::map<int, int> sink_counts;
  for (int cluster : model.net[net_index].drivers) {
    sources.insert(assignment[cluster]);
  }
  for (int cluster : model.net[net_index].sinks) {
    ++sink_counts[assignment[cluster]];
  }
  std::map<int, int> domain_counts;
  for (int source : sources) {
    for (const auto& sink_item : sink_counts) {
      const int sink = sink_item.first;
      if (source == sink) {
        continue;
      }
      const Route& route = model.route[source][sink];
      if (!route.reachable
          || (model.max_hops >= 0
              && static_cast<int>(route.arcs.size()) > model.max_hops)) {
        state.feasible = false;
        return state;
      }
      double delay = 0.0;
      for (const Arc& arc : route.arcs) {
        delay += arc.delay_ns;
        ++domain_counts[arc.domain];
      }
      const std::pair<int, int> transition = {source, sink};
      state.transitions.push_back(
          Transition{source, sink, sink_item.second});
      if (delay > state.delay_ns
          || (std::abs(delay - state.delay_ns) <= 1.0e-12
              && (state.worst_transition.first < 0
                  || transition < state.worst_transition))) {
        state.delay_ns = delay;
        state.worst_transition = transition;
      }
      state.hops += static_cast<long long>(route.arcs.size());
      ++state.cuts;
    }
  }
  state.sink_counts.assign(sink_counts.begin(), sink_counts.end());
  state.domain_counts.assign(domain_counts.begin(), domain_counts.end());
  return state;
}

const ProxyNetState& selected_proxy_net(
    const ProxyState& state,
    const std::map<int, ProxyNetState>& replacements,
    int net) {
  const auto replacement = replacements.find(net);
  return replacement == replacements.end() ? state.net[net]
                                           : replacement->second;
}

int proxy_sink_count(const ProxyNetState& state, int part) {
  const auto item = std::lower_bound(
      state.sink_counts.begin(), state.sink_counts.end(),
      std::make_pair(part, std::numeric_limits<int>::min()));
  return item != state.sink_counts.end() && item->first == part
             ? item->second
             : 0;
}

ProxyPathState build_proxy_path(
    const Model& model,
    const ProxyState& state,
    const std::map<int, ProxyNetState>& replacements,
    const std::vector<int>& ratios,
    int path_index) {
  const Path& path = model.path[path_index];
  double transport = 0.0;
  std::vector<int> sequence;
  std::set<int> dependency_domains;
  if (path.start_cluster >= 0) {
    int target = state.assignment[path.end_cluster];
    std::vector<std::pair<int, int>> reverse_transitions;
    for (auto item = path.nets.rbegin(); item != path.nets.rend(); ++item) {
      const Net& net = model.net[*item];
      const ProxyNetState& net_state = selected_proxy_net(
          state, replacements, *item);
      require(net.drivers.size() == 1,
              "endpoint-exact scalable path has multiple drivers");
      const int source = state.assignment[net.drivers.front()];
      const int sink_clusters = proxy_sink_count(net_state, target);
      require(sink_clusters > 0,
              "endpoint-exact scalable path cannot reach its target");
      if (source == target) {
        continue;
      }
      const Route& route = model.route[source][target];
      require(route.reachable
                  && (model.max_hops < 0
                      || static_cast<int>(route.arcs.size()) <= model.max_hops),
              "endpoint-exact scalable path chain is invalid");
      transport += predicted_transition_delay(
          model, route, ratios, sink_clusters);
      for (const Arc& arc : route.arcs) {
        dependency_domains.insert(arc.domain);
      }
      reverse_transitions.emplace_back(source, target);
      target = source;
    }
    require(state.assignment[path.start_cluster] == target,
            "endpoint-exact scalable path endpoints are inconsistent");
    for (auto item = reverse_transitions.rbegin();
         item != reverse_transitions.rend(); ++item) {
      for (int part : {item->first, item->second}) {
        if (sequence.empty() || sequence.back() != part) {
          sequence.push_back(part);
        }
      }
    }
  } else {
    for (int net : path.nets) {
      const ProxyNetState& net_state = selected_proxy_net(
          state, replacements, net);
      double worst_delay = 0.0;
      std::pair<int, int> worst = {-1, -1};
      for (const auto& transition : net_state.transitions) {
        const Route& route = model.route[transition.source][transition.sink];
        const double delay = predicted_transition_delay(
            model, route, ratios, transition.sink_clusters);
        for (const Arc& arc : route.arcs) {
          dependency_domains.insert(arc.domain);
        }
        const std::pair<int, int> identity = {
            transition.source, transition.sink};
        if (delay > worst_delay
            || (std::abs(delay - worst_delay) <= 1.0e-12
                && (worst.first < 0 || identity < worst))) {
          worst_delay = delay;
          worst = identity;
        }
      }
      transport += worst_delay;
      if (worst.first < 0) {
        continue;
      }
      for (int part : {worst.first, worst.second}) {
        if (sequence.empty() || sequence.back() != part) {
          sequence.push_back(part);
        }
      }
    }
  }
  std::set<int> seen;
  long long snaking = 0;
  for (int part : sequence) {
    if (seen.count(part)) {
      ++snaking;
    }
    seen.insert(part);
  }
  if (sequence.size() > 1
      && sequence.front() == path.feedback_source
      && sequence.back() == path.feedback_sink) {
    transport += path.feedback_residual_ns;
  }
  const double predicted = path.slack_ns - transport;
  const double slack = normalized_slack(model, path.period_ns, predicted);
  return ProxyPathState{
      slack,
      slack < 0.0,
      snaking,
      std::vector<int>(dependency_domains.begin(), dependency_domains.end()),
  };
}

Evaluation proxy_evaluation(const ProxyState& state) {
  Evaluation result;
  result.feasible = true;
  const double worst = state.slack_path_order.begin()->first;
  const int maximum_ratio = *state.ratio_order.rbegin();
  const int maximum_load = *std::max_element(
      state.domain_load.begin(), state.domain_load.end());
  result.objective = {
      -worst,
      -state.total_negative,
      static_cast<double>(state.negative_paths),
      static_cast<double>(maximum_ratio),
      static_cast<double>(maximum_load),
      static_cast<double>(state.snaking),
      static_cast<double>(state.hops),
      static_cast<double>(state.cuts),
  };
  result.ranked = {
      rank_float(result.objective[0]),
      rank_float(result.objective[1]),
      state.negative_paths,
      maximum_ratio,
      maximum_load,
      state.snaking,
      state.hops,
      state.cuts,
  };
  return result;
}

ProxyState build_proxy_state(
    const Model& model,
    const std::vector<int>* assignment_override = nullptr) {
  ProxyState state;
  state.assignment.resize(model.clusters);
  state.resource_load.assign(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  state.part_counts.assign(model.parts, 0);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    const int part = assignment_override == nullptr
                         ? model.cluster[cluster].part
                         : assignment_override->at(cluster);
    require(part >= 0 && part < model.parts,
            "scalable assignment part is invalid");
    require(model.cluster[cluster].fixed < 0
                || model.cluster[cluster].fixed == part,
            "scalable assignment violates a fixed cluster");
    state.assignment[cluster] = part;
    ++state.part_counts[part];
    for (int dim = 0; dim < model.dimensions; ++dim) {
      state.resource_load[part][dim] += model.cluster[cluster].weight[dim];
    }
  }
  require(std::count_if(state.part_counts.begin(), state.part_counts.end(),
                        [](int count) { return count > 0; })
              >= model.min_used_parts,
          "scalable assignment violates minimum used parts");
  state.domain_load.assign(model.domains, 0);
  state.net.resize(model.nets);
  for (int net = 0; net < model.nets; ++net) {
    state.net[net] = build_proxy_net(model, state.assignment, net);
    require(state.net[net].feasible, "initial scalable net is infeasible");
    for (const auto& item : state.net[net].domain_counts) {
      state.domain_load[item.first] += item.second;
    }
    state.hops += state.net[net].hops;
    state.cuts += state.net[net].cuts;
  }
  state.domain_ratio.resize(model.domains);
  for (int domain = 0; domain < model.domains; ++domain) {
    state.domain_ratio[domain]
        = tdm_ratio(model, state.domain_load[domain], model.domain[domain]);
    state.ratio_order.insert(state.domain_ratio[domain]);
  }
  state.path.resize(model.paths);
  state.domain_paths.resize(model.domains);
  const std::map<int, ProxyNetState> empty;
  for (int path = 0; path < model.paths; ++path) {
    state.path[path] = build_proxy_path(
        model, state, empty, state.domain_ratio, path);
    for (int domain : state.path[path].dependency_domains) {
      state.domain_paths[domain].insert(path);
    }
    state.slack_path_order.emplace(
        state.path[path].normalized_slack, path);
    state.ranked_path_order.emplace(
        rank_float(state.path[path].normalized_slack), path);
    state.total_negative += std::min(0.0, state.path[path].normalized_slack);
    state.negative_paths += state.path[path].negative ? 1 : 0;
    state.snaking += state.path[path].snaking;
  }
  require(!state.slack_path_order.empty() && !state.ranked_path_order.empty()
              && !state.ratio_order.empty(),
          "scalable state is empty");
  state.evaluation = proxy_evaluation(state);
  return state;
}

std::vector<std::vector<int>> build_cluster_nets(const Model& model) {
  std::vector<std::set<int>> sets(model.clusters);
  for (int net = 0; net < model.nets; ++net) {
    for (int cluster : model.net[net].drivers) {
      sets[cluster].insert(net);
    }
    for (int cluster : model.net[net].sinks) {
      sets[cluster].insert(net);
    }
  }
  std::vector<std::vector<int>> result(model.clusters);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    result[cluster].assign(sets[cluster].begin(), sets[cluster].end());
  }
  return result;
}

std::vector<std::vector<int>> build_net_paths(const Model& model) {
  std::vector<std::set<int>> sets(model.nets);
  for (int path = 0; path < model.paths; ++path) {
    for (int net : model.path[path].nets) {
      sets[net].insert(path);
    }
  }
  std::vector<std::vector<int>> result(model.nets);
  for (int net = 0; net < model.nets; ++net) {
    result[net].assign(sets[net].begin(), sets[net].end());
  }
  return result;
}

ProxyDelta evaluate_proxy_move(
    const Model& model,
    ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    int cluster,
    int target);

void apply_proxy_delta(const Model& model,
                       ProxyState& state,
                       const ProxyDelta& delta);

std::vector<int> diagnose_flow_corridors(
    const Model& model,
    const ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    const std::vector<double>& exposure,
    int cover_domain) {
  std::vector<int> selected_assignment;
  std::vector<long long> selected_rank;
  int edge_left = -1;
  int edge_right = -1;
  for (int source = 0; source < model.parts && edge_left < 0; ++source) {
    for (int sink = 0; sink < model.parts; ++sink) {
      if (sink == source) {
        continue;
      }
      if (model.route[source][sink].reachable
          && model.route[source][sink].arcs.size() == 1
          && model.route[source][sink].arcs.front().domain == cover_domain) {
        edge_left = source;
        edge_right = sink;
        break;
      }
    }
  }
  if (edge_left < 0) {
    std::cerr << "PATRON_FLOW_CORRIDOR status=no-edge-endpoints\n";
    return {};
  }
  std::vector<bool> opposite_side(model.parts, false);
  for (int part = 0; part < model.parts; ++part) {
    const Route& route = model.route[edge_left][part];
    opposite_side[part] = std::any_of(
        route.arcs.begin(), route.arcs.end(), [&](const Arc& arc) {
          return arc.domain == cover_domain;
        });
  }
  require(!opposite_side[edge_left] && opposite_side[edge_right],
          "flow corridor topology sides are inconsistent");
  const auto consider_dual_improving
      = [&](const std::vector<int>& assignment, bool capacity_compatible) {
          if (!capacity_compatible) {
            return;
          }
          std::vector<int> counts(model.parts, 0);
          for (int part : assignment) {
            require(part >= 0 && part < model.parts,
                    "flow candidate part is invalid");
            ++counts[part];
          }
          if (std::count_if(counts.begin(), counts.end(),
                            [](int count) { return count > 0; })
              < model.min_used_parts) {
            return;
          }
          ProxyState candidate = build_proxy_state(model, &assignment);
          if (candidate.evaluation.ranked.size() >= 2
              && state.evaluation.ranked.size() >= 2
              && candidate.evaluation.ranked[0]
                     < state.evaluation.ranked[0]
              && candidate.evaluation.ranked[1]
                     < state.evaluation.ranked[1]
              && (selected_assignment.empty()
                  || less_ranked(candidate.evaluation.ranked,
                                 selected_rank))) {
            selected_assignment = candidate.assignment;
            selected_rank = candidate.evaluation.ranked;
          }
        };

  const int maximum_corridor_clusters
      = model.flow_refinement
            ? model.flow_max_clusters
            : std::min(50000, model.clusters);
  const int corridor_distance
      = model.flow_refinement ? model.flow_corridor_distance : 2;
  const int maximum_legal_candidates
      = model.flow_refinement ? model.flow_max_legal_candidates : 8;
  const int maximum_polish_moves
      = model.flow_refinement ? model.flow_max_polish_moves : 512;
  for (int pair_target = 0; pair_target < model.parts; ++pair_target) {
    if (!opposite_side[pair_target]) {
      continue;
    }
    std::set<int> boundary_set;
    for (int net = 0; net < model.nets; ++net) {
      const auto domain = std::lower_bound(
          state.net[net].domain_counts.begin(),
          state.net[net].domain_counts.end(),
          std::make_pair(cover_domain, std::numeric_limits<int>::min()));
      if (domain == state.net[net].domain_counts.end()
          || domain->first != cover_domain) {
        continue;
      }
      for (int cluster : model.net[net].drivers) {
        boundary_set.insert(cluster);
      }
      for (int cluster : model.net[net].sinks) {
        boundary_set.insert(cluster);
      }
    }
    std::vector<int> boundary(boundary_set.begin(), boundary_set.end());
    std::sort(boundary.begin(), boundary.end(), [&](int left, int right) {
      return std::tie(exposure[right], left)
             < std::tie(exposure[left], right);
    });

    std::vector<int> distance(model.clusters, -1);
    std::queue<int> work;
    int corridor_count = 0;
    const auto try_add = [&](int cluster, int candidate_distance) {
      if (distance[cluster] >= 0
          || corridor_count >= maximum_corridor_clusters
          || model.cluster[cluster].fixed >= 0) {
        return false;
      }
      distance[cluster] = candidate_distance;
      work.push(cluster);
      ++corridor_count;
      return true;
    };
    for (int cluster : boundary) {
      if (corridor_count >= maximum_corridor_clusters) {
        break;
      }
      try_add(cluster, 0);
    }
    while (!work.empty()) {
      const int cluster = work.front();
      work.pop();
      if (distance[cluster] >= corridor_distance) {
        continue;
      }
      std::set<int> neighbor_set;
      for (int net : cluster_nets[cluster]) {
        neighbor_set.insert(model.net[net].drivers.begin(),
                            model.net[net].drivers.end());
        neighbor_set.insert(model.net[net].sinks.begin(),
                            model.net[net].sinks.end());
      }
      std::vector<int> neighbors(neighbor_set.begin(), neighbor_set.end());
      std::sort(neighbors.begin(), neighbors.end(), [&](int left, int right) {
        return std::tie(exposure[right], left)
               < std::tie(exposure[left], right);
      });
      for (int neighbor : neighbors) {
        if (opposite_side[state.assignment[neighbor]]
            == opposite_side[state.assignment[cluster]]) {
          try_add(neighbor, distance[cluster] + 1);
        }
      }
    }

    std::vector<int> corridor;
    for (int cluster = 0; cluster < model.clusters; ++cluster) {
      if (distance[cluster] >= 0) {
        corridor.push_back(cluster);
      }
    }
    if (corridor.empty()) {
      std::cerr << "PATRON_FLOW_CORRIDOR pair=" << edge_left << ':'
                << pair_target << " status=empty\n";
      continue;
    }
    std::set<int> corridor_nets;
    for (int cluster : corridor) {
      corridor_nets.insert(cluster_nets[cluster].begin(),
                           cluster_nets[cluster].end());
    }
    const int source_node = 0;
    const int sink_node = 1;
    std::vector<int> cluster_node(model.clusters, -1);
    int next_node = 2;
    for (int cluster : corridor) {
      cluster_node[cluster] = next_node++;
    }
    std::map<int, std::pair<int, int>> net_nodes;
    for (int net : corridor_nets) {
      net_nodes[net] = {next_node, next_node + 1};
      next_node += 2;
    }
    DinicFlow flow(next_node);
    const long long infinity
        = static_cast<long long>(corridor_nets.size()) + 1;
    for (const auto& item : net_nodes) {
      const int net = item.first;
      const int in_node = item.second.first;
      const int out_node = item.second.second;
      flow.add_edge(in_node, out_node, 1);
      std::set<int> pins;
      pins.insert(model.net[net].drivers.begin(),
                  model.net[net].drivers.end());
      pins.insert(model.net[net].sinks.begin(), model.net[net].sinks.end());
      bool fixed_source = false;
      bool fixed_sink = false;
      for (int cluster : pins) {
        if (cluster_node[cluster] >= 0) {
          flow.add_edge(cluster_node[cluster], in_node, infinity);
          flow.add_edge(out_node, cluster_node[cluster], infinity);
        } else if (opposite_side[state.assignment[cluster]]) {
          fixed_sink = true;
        } else {
          fixed_source = true;
        }
      }
      if (fixed_source) {
        flow.add_edge(source_node, in_node, infinity);
      }
      if (fixed_sink) {
        flow.add_edge(out_node, sink_node, infinity);
      }
    }
    for (int cluster : corridor) {
      if (state.assignment[cluster] == edge_left) {
        flow.add_edge(source_node, cluster_node[cluster], 0);
      } else {
        flow.add_edge(cluster_node[cluster], sink_node, 0);
      }
    }
    const long long cut = flow.maximum_flow(source_node, sink_node);
    const std::vector<bool> source_side
        = flow.source_reachable(source_node);
    std::vector<int> candidate_assignment = state.assignment;
    int moved_to_left = 0;
    int moved_to_target = 0;
    for (int cluster : corridor) {
      const int original = state.assignment[cluster];
      const int target = source_side[cluster_node[cluster]]
                             ? (!opposite_side[original]
                                    ? original
                                    : edge_left)
                             : (opposite_side[original]
                                    ? original
                                    : pair_target);
      if (target != state.assignment[cluster]) {
        candidate_assignment[cluster] = target;
        moved_to_left += target == edge_left ? 1 : 0;
        moved_to_target += target == pair_target ? 1 : 0;
      }
    }
    bool capacity_compatible = true;
    std::vector<std::vector<double>> candidate_load = state.resource_load;
    std::vector<int> candidate_counts = state.part_counts;
    for (int cluster : corridor) {
      const int source = state.assignment[cluster];
      const int target = candidate_assignment[cluster];
      if (source == target) {
        continue;
      }
      --candidate_counts[source];
      ++candidate_counts[target];
      for (int dim = 0; dim < model.dimensions; ++dim) {
        candidate_load[source][dim] -= model.cluster[cluster].weight[dim];
        candidate_load[target][dim] += model.cluster[cluster].weight[dim];
      }
    }
    for (int part = 0; part < model.parts; ++part) {
      capacity_compatible = capacity_compatible
                            && candidate_counts[part] >= 0;
      for (int dim = 0; dim < model.dimensions; ++dim) {
        capacity_compatible = capacity_compatible
                              && candidate_load[part][dim]
                                     <= model.hard_capacity[part][dim] + 1.0e-9
                              && candidate_load[part][dim]
                                     <= model.balance_capacity[part][dim]
                                            + 1.0e-9;
      }
    }
    const std::vector<int> raw_candidate_assignment = candidate_assignment;
    const std::vector<std::vector<double>> raw_candidate_load = candidate_load;
    ProxyState candidate = build_proxy_state(model, &candidate_assignment);
    std::cerr << "PATRON_FLOW_CORRIDOR pair=" << edge_left << ':'
              << pair_target
              << " boundary=" << boundary.size()
              << " clusters=" << corridor.size()
              << " cluster_limit=" << maximum_corridor_clusters
              << " distance_limit=" << corridor_distance
              << " nets=" << corridor_nets.size()
              << " mincut=" << cut
              << " moved_to_left=" << moved_to_left
              << " moved_to_target=" << moved_to_target
              << " capacity_compatible=" << (capacity_compatible ? 1 : 0)
              << " domain_load=" << candidate.domain_load[cover_domain]
              << " improving="
              << (capacity_compatible
                          && less_ranked(candidate.evaluation.ranked,
                                         state.evaluation.ranked)
                      ? 1
                      : 0)
              << " rank=";
    for (long long value : candidate.evaluation.ranked) {
      std::cerr << value << ',';
    }
    std::cerr << '\n';
    consider_dual_improving(candidate_assignment, capacity_compatible);

    if (!capacity_compatible) {
      struct SpillOption {
        int cluster = -1;
        int target = -1;
        double cost_per_relief = 0.0;
        double exposure = 0.0;
        double relief = 0.0;
      };
      std::vector<int> incoming;
      for (int cluster : corridor) {
        if (!opposite_side[state.assignment[cluster]]
            && candidate_assignment[cluster] == pair_target) {
          incoming.push_back(cluster);
        }
      }
      std::vector<std::vector<int>> net_part_pins(
          model.nets, std::vector<int>(model.parts, 0));
      for (int net = 0; net < model.nets; ++net) {
        for (int cluster : model.net[net].drivers) {
          ++net_part_pins[net][candidate_assignment[cluster]];
        }
        for (int cluster : model.net[net].sinks) {
          ++net_part_pins[net][candidate_assignment[cluster]];
        }
      }
      std::vector<double> excess(model.dimensions, 0.0);
      for (int dim = 0; dim < model.dimensions; ++dim) {
        const double limit = std::min(
            model.hard_capacity[pair_target][dim],
            model.balance_capacity[pair_target][dim]);
        excess[dim] = std::max(
            0.0, candidate_load[pair_target][dim] - limit);
      }
      std::vector<SpillOption> options;
      for (int cluster : incoming) {
        int old_affinity = 0;
        for (int net : cluster_nets[cluster]) {
          old_affinity += net_part_pins[net][pair_target] > 1 ? 1 : 0;
        }
        for (int target = 0; target < model.parts; ++target) {
          if (!opposite_side[target] || target == pair_target) {
            continue;
          }
          double relief = 0.0;
          for (int dim = 0; dim < model.dimensions; ++dim) {
            const double limit = std::min(
                model.hard_capacity[pair_target][dim],
                model.balance_capacity[pair_target][dim]);
            if (excess[dim] > 1.0e-9 && limit > 0.0) {
              relief += std::min(model.cluster[cluster].weight[dim],
                                 excess[dim])
                        / limit;
            }
          }
          if (relief <= 0.0) {
            continue;
          }
          int new_affinity = 0;
          for (int net : cluster_nets[cluster]) {
            new_affinity += net_part_pins[net][target] > 0 ? 1 : 0;
          }
          const double timing_weight = 1.0 + std::log1p(exposure[cluster]);
          const double spill_cost
              = static_cast<double>(old_affinity - new_affinity)
                * timing_weight;
          options.push_back(SpillOption{
              cluster,
              target,
              spill_cost / relief,
              exposure[cluster],
              relief});
        }
      }
      std::sort(options.begin(), options.end(), [](const SpillOption& left,
                                                   const SpillOption& right) {
        return std::tie(left.cost_per_relief,
                        left.exposure,
                        left.cluster,
                        left.target)
               < std::tie(right.cost_per_relief,
                          right.exposure,
                          right.cluster,
                          right.target);
      });
      int spills = 0;
      for (const SpillOption& option : options) {
        bool target_overloaded = false;
        bool contributes = false;
        for (int dim = 0; dim < model.dimensions; ++dim) {
          const double source_limit = std::min(
              model.hard_capacity[pair_target][dim],
              model.balance_capacity[pair_target][dim]);
          const double target_limit = std::min(
              model.hard_capacity[option.target][dim],
              model.balance_capacity[option.target][dim]);
          target_overloaded = target_overloaded
                              || candidate_load[pair_target][dim]
                                     > source_limit + 1.0e-9;
          contributes = contributes
                        || (candidate_load[pair_target][dim]
                                    > source_limit + 1.0e-9
                            && model.cluster[option.cluster].weight[dim]
                                   > 0.0);
          if (candidate_load[option.target][dim]
                  + model.cluster[option.cluster].weight[dim]
              > target_limit + 1.0e-9) {
            contributes = false;
            break;
          }
        }
        if (!target_overloaded) {
          break;
        }
        if (!contributes
            || candidate_assignment[option.cluster] != pair_target) {
          continue;
        }
        candidate_assignment[option.cluster] = option.target;
        for (int dim = 0; dim < model.dimensions; ++dim) {
          const double weight = model.cluster[option.cluster].weight[dim];
          candidate_load[pair_target][dim] -= weight;
          candidate_load[option.target][dim] += weight;
        }
        ++spills;
      }
      bool legalized_capacity = true;
      for (int part = 0; part < model.parts; ++part) {
        for (int dim = 0; dim < model.dimensions; ++dim) {
          legalized_capacity = legalized_capacity
                               && candidate_load[part][dim]
                                      <= model.hard_capacity[part][dim]
                                             + 1.0e-9
                               && candidate_load[part][dim]
                                      <= model.balance_capacity[part][dim]
                                             + 1.0e-9;
        }
      }
      std::cerr << "PATRON_FLOW_LEGALIZATION pair=" << edge_left << ':'
                << pair_target
                << " incoming=" << incoming.size()
                << " options=" << options.size()
                << " spills=" << spills
                << " capacity_compatible=" << (legalized_capacity ? 1 : 0);
      if (legalized_capacity) {
        ProxyState legalized = build_proxy_state(model, &candidate_assignment);
        std::cerr << " domain_load=" << legalized.domain_load[cover_domain]
                  << " improving="
                  << (less_ranked(legalized.evaluation.ranked,
                                  state.evaluation.ranked)
                          ? 1
                          : 0)
                  << " rank=";
        for (long long value : legalized.evaluation.ranked) {
          std::cerr << value << ',';
        }
        consider_dual_improving(candidate_assignment, true);
      }
      std::cerr << '\n';

      if (pair_target == edge_right) {
        const auto run_region_legalization
            = [&](const std::string& mode, bool affinity_first) {
                std::vector<int> region_assignment
                    = raw_candidate_assignment;
                std::vector<std::vector<double>> region_load
                    = raw_candidate_load;
                std::vector<int> unassigned;
                for (int cluster : corridor) {
                  if (!opposite_side[state.assignment[cluster]]
                      && raw_candidate_assignment[cluster] == pair_target) {
                    for (int dim = 0; dim < model.dimensions; ++dim) {
                      region_load[pair_target][dim]
                          -= model.cluster[cluster].weight[dim];
                    }
                    region_assignment[cluster] = -1;
                    unassigned.push_back(cluster);
                  }
                }
                std::vector<std::vector<int>> region_net_part_pins(
                    model.nets, std::vector<int>(model.parts, 0));
                for (int net = 0; net < model.nets; ++net) {
                  for (int cluster : model.net[net].drivers) {
                    if (region_assignment[cluster] >= 0) {
                      ++region_net_part_pins[net]
                                             [region_assignment[cluster]];
                    }
                  }
                  for (int cluster : model.net[net].sinks) {
                    if (region_assignment[cluster] >= 0) {
                      ++region_net_part_pins[net]
                                             [region_assignment[cluster]];
                    }
                  }
                }
                std::vector<double> aggregate_slack(
                    model.dimensions, 0.0);
                for (int part = 0; part < model.parts; ++part) {
                  if (!opposite_side[part]) {
                    continue;
                  }
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    aggregate_slack[dim] += std::max(
                        0.0,
                        std::min(model.hard_capacity[part][dim],
                                 model.balance_capacity[part][dim])
                            - region_load[part][dim]);
                  }
                }
                std::sort(unassigned.begin(), unassigned.end(),
                          [&](int left, int right) {
                            double left_constraint = 0.0;
                            double right_constraint = 0.0;
                            for (int dim = 0; dim < model.dimensions; ++dim) {
                              if (aggregate_slack[dim] > 1.0e-12) {
                                left_constraint = std::max(
                                    left_constraint,
                                    model.cluster[left].weight[dim]
                                        / aggregate_slack[dim]);
                                right_constraint = std::max(
                                    right_constraint,
                                    model.cluster[right].weight[dim]
                                        / aggregate_slack[dim]);
                              }
                            }
                            return std::tie(right_constraint,
                                            exposure[right],
                                            right)
                                   < std::tie(left_constraint,
                                              exposure[left],
                                              left);
                          });
                bool assigned_all = true;
                for (int cluster : unassigned) {
                  int best_target = -1;
                  int best_affinity = -1;
                  double best_utilization
                      = std::numeric_limits<double>::infinity();
                  for (int target = 0; target < model.parts; ++target) {
                    if (!opposite_side[target]) {
                      continue;
                    }
                    bool fits = true;
                    double utilization = 0.0;
                    for (int dim = 0; dim < model.dimensions; ++dim) {
                      const double limit = std::min(
                          model.hard_capacity[target][dim],
                          model.balance_capacity[target][dim]);
                      const double projected
                          = region_load[target][dim]
                            + model.cluster[cluster].weight[dim];
                      if (projected > limit + 1.0e-9) {
                        fits = false;
                        break;
                      }
                      if (limit > 0.0) {
                        utilization = std::max(utilization,
                                               projected / limit);
                      }
                    }
                    if (!fits) {
                      continue;
                    }
                    int affinity = 0;
                    for (int net : cluster_nets[cluster]) {
                      affinity += region_net_part_pins[net][target] > 0
                                      ? 1
                                      : 0;
                    }
                    bool better = best_target < 0;
                    if (affinity_first) {
                      better = better || affinity > best_affinity
                               || (affinity == best_affinity
                                   && utilization
                                          < best_utilization - 1.0e-12)
                               || (affinity == best_affinity
                                   && std::abs(utilization - best_utilization)
                                          <= 1.0e-12
                                   && target < best_target);
                    } else {
                      better = better
                               || utilization
                                      < best_utilization - 1.0e-12
                               || (std::abs(utilization - best_utilization)
                                          <= 1.0e-12
                                   && affinity > best_affinity)
                               || (std::abs(utilization - best_utilization)
                                          <= 1.0e-12
                                   && affinity == best_affinity
                                   && target < best_target);
                    }
                    if (better) {
                      best_target = target;
                      best_affinity = affinity;
                      best_utilization = utilization;
                    }
                  }
                  if (best_target < 0) {
                    assigned_all = false;
                    break;
                  }
                  region_assignment[cluster] = best_target;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    region_load[best_target][dim]
                        += model.cluster[cluster].weight[dim];
                  }
                  for (int net : cluster_nets[cluster]) {
                    int occurrences = static_cast<int>(std::count(
                        model.net[net].drivers.begin(),
                        model.net[net].drivers.end(),
                        cluster));
                    occurrences += static_cast<int>(std::count(
                        model.net[net].sinks.begin(),
                        model.net[net].sinks.end(),
                        cluster));
                    region_net_part_pins[net][best_target] += occurrences;
                  }
                }
                bool region_capacity = assigned_all;
                for (int part = 0; part < model.parts; ++part) {
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    region_capacity = region_capacity
                                      && region_load[part][dim]
                                             <= model.hard_capacity[part][dim]
                                                    + 1.0e-9
                                      && region_load[part][dim]
                                             <= model.balance_capacity[part]
                                                                      [dim]
                                                    + 1.0e-9;
                  }
                }
                std::cerr << "PATRON_FLOW_REGION_LEGALIZATION pair="
                          << edge_left << ':' << pair_target
                          << " mode=" << mode
                          << " incoming=" << unassigned.size()
                          << " assigned="
                          << (assigned_all ? unassigned.size() : 0)
                          << " capacity_compatible="
                          << (region_capacity ? 1 : 0);
                if (region_capacity) {
                  ProxyState legalized
                      = build_proxy_state(model, &region_assignment);
                  std::cerr << " domain_load="
                            << legalized.domain_load[cover_domain]
                            << " improving="
                            << (less_ranked(legalized.evaluation.ranked,
                                            state.evaluation.ranked)
                                    ? 1
                                    : 0)
                            << " rank=";
                  for (long long value : legalized.evaluation.ranked) {
                    std::cerr << value << ',';
                  }
                  consider_dual_improving(region_assignment, true);
                }
                std::cerr << '\n';
              };
        run_region_legalization("affinity-first", true);
        run_region_legalization("balance-first", false);

        std::vector<int> right_parts;
        for (int part = 0; part < model.parts; ++part) {
          if (opposite_side[part]) {
            right_parts.push_back(part);
          }
        }
        if (right_parts.size() == 2) {
          const int right_source = edge_right;
          const int right_sink = right_parts.front() == right_source
                                     ? right_parts.back()
                                     : right_parts.front();
          std::vector<int> parametric_base = raw_candidate_assignment;
          std::vector<std::vector<double>> parametric_base_load
              = raw_candidate_load;
          std::vector<int> parametric_variables;
          for (int cluster : corridor) {
            if (!opposite_side[state.assignment[cluster]]
                && raw_candidate_assignment[cluster] == pair_target) {
              for (int dim = 0; dim < model.dimensions; ++dim) {
                parametric_base_load[pair_target][dim]
                    -= model.cluster[cluster].weight[dim];
              }
              parametric_base[cluster] = -1;
              parametric_variables.push_back(cluster);
            }
          }
          std::set<int> parametric_nets;
          for (int cluster : parametric_variables) {
            parametric_nets.insert(cluster_nets[cluster].begin(),
                                   cluster_nets[cluster].end());
          }
          const int parametric_source_node = 0;
          const int parametric_sink_node = 1;
          std::vector<int> parametric_cluster_node(model.clusters, -1);
          int parametric_next_node = 2;
          for (int cluster : parametric_variables) {
            parametric_cluster_node[cluster] = parametric_next_node++;
          }
          std::map<int, std::pair<int, int>> parametric_net_nodes;
          for (int net : parametric_nets) {
            parametric_net_nodes[net]
                = {parametric_next_node, parametric_next_node + 1};
            parametric_next_node += 2;
          }
          DinicFlow parametric_flow(parametric_next_node);
          constexpr long long kParametricNetCapacity = 1000000;
          constexpr long long kParametricInfinity
              = std::numeric_limits<long long>::max() / 16;
          for (const auto& item : parametric_net_nodes) {
            const int net = item.first;
            const int in_node = item.second.first;
            const int out_node = item.second.second;
            parametric_flow.add_edge(
                in_node, out_node, kParametricNetCapacity);
            std::set<int> pins;
            pins.insert(model.net[net].drivers.begin(),
                        model.net[net].drivers.end());
            pins.insert(model.net[net].sinks.begin(),
                        model.net[net].sinks.end());
            bool anchored_source = false;
            bool anchored_sink = false;
            for (int cluster : pins) {
              if (parametric_cluster_node[cluster] >= 0) {
                parametric_flow.add_edge(
                    parametric_cluster_node[cluster],
                    in_node,
                    kParametricInfinity);
                parametric_flow.add_edge(
                    out_node,
                    parametric_cluster_node[cluster],
                    kParametricInfinity);
              } else if (parametric_base[cluster] == right_source) {
                anchored_source = true;
              } else if (parametric_base[cluster] == right_sink) {
                anchored_sink = true;
              }
            }
            if (anchored_source) {
              parametric_flow.add_edge(parametric_source_node,
                                       in_node,
                                       kParametricInfinity);
            }
            if (anchored_sink) {
              parametric_flow.add_edge(out_node,
                                       parametric_sink_node,
                                       kParametricInfinity);
            }
          }
          std::vector<double> variable_totals(model.dimensions, 0.0);
          for (int cluster : parametric_variables) {
            for (int dim = 0; dim < model.dimensions; ++dim) {
              variable_totals[dim] += model.cluster[cluster].weight[dim];
            }
          }
          std::vector<long long> balance_weight(model.clusters, 1);
          for (int cluster : parametric_variables) {
            long long weight = 1;
            for (int dim = 0; dim < model.dimensions; ++dim) {
              if (variable_totals[dim] > 0.0) {
                weight += static_cast<long long>(std::llround(
                    1000000.0 * model.cluster[cluster].weight[dim]
                    / variable_totals[dim]));
              }
            }
            balance_weight[cluster] = std::max(1LL, weight);
          }
          long long cumulative_flow = parametric_flow.maximum_flow(
              parametric_source_node, parametric_sink_node);
          long long lambda = 0;
          long long lambda_increment = 1;
          bool direction_selected = false;
          bool push_to_sink = true;
          bool saw_feasible = false;
          bool piercing_attempted = false;
          int feasible_cuts = 0;
          std::vector<int> first_feasible_assignment;
          std::vector<int> best_parametric_assignment;
          std::vector<long long> best_parametric_key;
          std::vector<int> best_piercing_assignment;
          std::vector<long long> best_piercing_rank;
          std::vector<int> best_dual_piercing_assignment;
          std::vector<long long> best_dual_piercing_rank;
          for (int iteration = 0; iteration < 96; ++iteration) {
            const std::vector<bool> parametric_source_side
                = parametric_flow.source_reachable(parametric_source_node);
            std::vector<int> parametric_assignment = parametric_base;
            std::vector<std::vector<double>> parametric_load
                = parametric_base_load;
            for (int cluster : parametric_variables) {
              const int target
                  = parametric_source_side[parametric_cluster_node[cluster]]
                        ? right_source
                        : right_sink;
              parametric_assignment[cluster] = target;
              for (int dim = 0; dim < model.dimensions; ++dim) {
                parametric_load[target][dim]
                    += model.cluster[cluster].weight[dim];
              }
            }
            bool parametric_capacity = true;
            double source_excess = 0.0;
            double sink_excess = 0.0;
            for (int part = 0; part < model.parts; ++part) {
              for (int dim = 0; dim < model.dimensions; ++dim) {
                const double limit = std::min(
                    model.hard_capacity[part][dim],
                    model.balance_capacity[part][dim]);
                const double excess_value = std::max(
                    0.0, parametric_load[part][dim] - limit);
                parametric_capacity = parametric_capacity
                                      && excess_value <= 1.0e-9;
                if (limit > 0.0 && part == right_source) {
                  source_excess = std::max(
                      source_excess, excess_value / limit);
                }
                if (limit > 0.0 && part == right_sink) {
                  sink_excess = std::max(
                      sink_excess, excess_value / limit);
                }
              }
            }
            if (!direction_selected) {
              push_to_sink = source_excess >= sink_excess;
              direction_selected = true;
            }
            std::vector<int> parametric_domain_load = state.domain_load;
            long long parametric_hops = state.hops;
            long long parametric_cuts = state.cuts;
            bool parametric_routes = true;
            for (int net : corridor_nets) {
              ProxyNetState replacement = build_proxy_net(
                  model, parametric_assignment, net);
              if (!replacement.feasible) {
                parametric_routes = false;
                break;
              }
              for (const auto& domain : state.net[net].domain_counts) {
                parametric_domain_load[domain.first] -= domain.second;
              }
              for (const auto& domain : replacement.domain_counts) {
                parametric_domain_load[domain.first] += domain.second;
              }
              parametric_hops += replacement.hops - state.net[net].hops;
              parametric_cuts += replacement.cuts - state.net[net].cuts;
            }
            int parametric_max_ratio = 1;
            int parametric_max_load = 0;
            for (int domain = 0; domain < model.domains; ++domain) {
              parametric_max_ratio = std::max(
                  parametric_max_ratio,
                  tdm_ratio(model,
                            parametric_domain_load[domain],
                            model.domain[domain]));
              parametric_max_load = std::max(
                  parametric_max_load, parametric_domain_load[domain]);
            }
            std::cerr << "PATRON_FLOW_PARAMETRIC pair=" << edge_left << ':'
                      << pair_target
                      << " iteration=" << iteration
                      << " lambda=" << lambda
                      << " direction=" << (push_to_sink ? "sink" : "source")
                      << " flow=" << cumulative_flow
                      << " capacity_compatible="
                      << (parametric_capacity ? 1 : 0)
                      << " source_excess=" << source_excess
                      << " sink_excess=" << sink_excess
                      << " max_ratio=" << parametric_max_ratio
                      << " max_load=" << parametric_max_load
                      << " cover_load="
                      << parametric_domain_load[cover_domain]
                      << " hops=" << parametric_hops
                      << " cuts=" << parametric_cuts << '\n';
            if (parametric_capacity && parametric_routes) {
              const std::vector<long long> key = {
                  parametric_max_ratio,
                  parametric_domain_load[cover_domain],
                  parametric_max_load,
                  parametric_hops,
                  parametric_cuts};
              if (!saw_feasible) {
                first_feasible_assignment = parametric_assignment;
              }
              if (best_parametric_assignment.empty()
                  || key < best_parametric_key) {
                best_parametric_key = key;
                best_parametric_assignment = parametric_assignment;
              }
              saw_feasible = true;
              ++feasible_cuts;
              if (feasible_cuts >= 4) {
                break;
              }
            } else if (saw_feasible
                       && ((push_to_sink && sink_excess > 0.0)
                           || (!push_to_sink && source_excess > 0.0))) {
              break;
            }
            if (!parametric_capacity
                && !piercing_attempted
                && push_to_sink
                && source_excess > 0.0
                && source_excess <= 0.05
                && sink_excess <= 1.0e-12) {
              piercing_attempted = true;
              const int piercing_strategy
                  = model.flow_refinement
                        ? model.flow_piercing_strategy
                        : 0;
              std::vector<bool> pierced(model.clusters, false);
              constexpr int kPiercingBatch = 1;
              std::vector<std::vector<double>> current_piercing_load
                  = parametric_load;
              double current_source_excess = source_excess;
              double current_sink_excess = sink_excess;
              double current_source_pressure = 0.0;
              double current_sink_pressure = 0.0;
              int feasible_piercing_cuts = 0;
              for (int dim = 0; dim < model.dimensions; ++dim) {
                const double source_limit = std::min(
                    model.hard_capacity[right_source][dim],
                    model.balance_capacity[right_source][dim]);
                const double sink_limit = std::min(
                    model.hard_capacity[right_sink][dim],
                    model.balance_capacity[right_sink][dim]);
                if (source_limit > 0.0) {
                  current_source_pressure = std::max(
                      current_source_pressure,
                      current_piercing_load[right_source][dim]
                          / source_limit);
                }
                if (sink_limit > 0.0) {
                  current_sink_pressure = std::max(
                      current_sink_pressure,
                      current_piercing_load[right_sink][dim] / sink_limit);
                }
              }
              for (int piercing_iteration = 0;
                   piercing_iteration < static_cast<int>(
                       parametric_variables.size());
                   ++piercing_iteration) {
                const std::vector<bool> piercing_source_side
                    = parametric_flow.source_reachable(
                        parametric_source_node);
                const bool grow_sink
                    = current_source_excess > current_sink_excess
                      || (current_source_excess == current_sink_excess
                          && current_source_pressure
                                 >= current_sink_pressure);
                std::vector<int> candidates;
                std::vector<int> fallback;
                for (int cluster : parametric_variables) {
                  if (pierced[cluster]
                      || piercing_source_side[
                             parametric_cluster_node[cluster]]
                             != grow_sink) {
                    continue;
                  }
                  bool boundary_cluster = false;
                  for (int net : cluster_nets[cluster]) {
                    if (parametric_nets.find(net) == parametric_nets.end()) {
                      continue;
                    }
                    std::set<int> pins;
                    pins.insert(model.net[net].drivers.begin(),
                                model.net[net].drivers.end());
                    pins.insert(model.net[net].sinks.begin(),
                                model.net[net].sinks.end());
                    for (int pin : pins) {
                      bool opposite_pin = false;
                      if (parametric_cluster_node[pin] >= 0) {
                        opposite_pin
                            = piercing_source_side[
                                  parametric_cluster_node[pin]]
                              != grow_sink;
                      } else if (grow_sink) {
                        opposite_pin
                            = parametric_base[pin] == right_sink;
                      } else {
                        opposite_pin
                            = parametric_base[pin] == right_source;
                      }
                      if (opposite_pin) {
                        boundary_cluster = true;
                        break;
                      }
                    }
                    if (boundary_cluster) {
                      break;
                    }
                  }
                  fallback.push_back(cluster);
                  if (boundary_cluster) {
                    candidates.push_back(cluster);
                  }
                }
                if (candidates.empty()) {
                  candidates = fallback;
                }
                if (candidates.empty()) {
                  break;
                }
                const auto relief_score = [&](int cluster) {
                  double score = 0.0;
                  const int overloaded_part
                      = grow_sink ? right_source : right_sink;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    const double limit = std::min(
                        model.hard_capacity[overloaded_part][dim],
                        model.balance_capacity[overloaded_part][dim]);
                    if (limit > 0.0
                        && current_piercing_load[overloaded_part][dim]
                               > limit + 1.0e-9) {
                      score += model.cluster[cluster].weight[dim] / limit;
                    }
                  }
                  return score / (1.0 + std::log1p(exposure[cluster]));
                };
                std::sort(candidates.begin(), candidates.end(),
                          [&](int left, int right) {
                            const double left_score = relief_score(left);
                            const double right_score = relief_score(right);
                            if (piercing_strategy == 1
                                && left_score != right_score) {
                              return left_score < right_score;
                            }
                            if (piercing_strategy == 2
                                && exposure[left] != exposure[right]) {
                              return exposure[left] < exposure[right];
                            }
                            if (piercing_strategy == 3) {
                              return left < right;
                            }
                            if (left_score != right_score) {
                              return left_score > right_score;
                            }
                            if (exposure[left] != exposure[right]) {
                              return exposure[left] < exposure[right];
                            }
                            return left < right;
                          });
                const int batch = std::min(
                    kPiercingBatch, static_cast<int>(candidates.size()));
                for (int index = 0; index < batch; ++index) {
                  const int cluster = candidates[index];
                  pierced[cluster] = true;
                  if (grow_sink) {
                    parametric_flow.add_edge(
                        parametric_cluster_node[cluster],
                        parametric_sink_node,
                        kParametricInfinity);
                  } else {
                    parametric_flow.add_edge(
                        parametric_source_node,
                        parametric_cluster_node[cluster],
                        kParametricInfinity);
                  }
                  if (piercing_iteration < 16) {
                    std::cerr << "PATRON_FLOW_PIERCING_CHOICE pair="
                              << edge_left << ':' << pair_target
                              << " strategy=" << piercing_strategy
                              << " direction="
                              << (grow_sink ? "sink" : "source")
                              << " iteration=" << piercing_iteration
                              << " cluster=" << cluster
                              << " relief=" << relief_score(cluster)
                              << " exposure=" << exposure[cluster]
                              << '\n';
                  }
                }
                cumulative_flow += parametric_flow.maximum_flow(
                    parametric_source_node, parametric_sink_node);
                const std::vector<bool> updated_source_side
                    = parametric_flow.source_reachable(
                        parametric_source_node);
                std::vector<int> piercing_assignment = parametric_base;
                std::vector<std::vector<double>> piercing_load
                    = parametric_base_load;
                for (int cluster : parametric_variables) {
                  const int target
                      = updated_source_side[
                            parametric_cluster_node[cluster]]
                            ? right_source
                            : right_sink;
                  piercing_assignment[cluster] = target;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    piercing_load[target][dim]
                        += model.cluster[cluster].weight[dim];
                  }
                }
                bool piercing_capacity = true;
                double piercing_source_excess = 0.0;
                double piercing_sink_excess = 0.0;
                double piercing_source_pressure = 0.0;
                double piercing_sink_pressure = 0.0;
                for (int part = 0; part < model.parts; ++part) {
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    const double limit = std::min(
                        model.hard_capacity[part][dim],
                        model.balance_capacity[part][dim]);
                    const double excess_value = std::max(
                        0.0, piercing_load[part][dim] - limit);
                    piercing_capacity = piercing_capacity
                                        && excess_value <= 1.0e-9;
                    if (limit > 0.0 && part == right_source) {
                      piercing_source_pressure = std::max(
                          piercing_source_pressure,
                          piercing_load[part][dim] / limit);
                      piercing_source_excess = std::max(
                          piercing_source_excess, excess_value / limit);
                    }
                    if (limit > 0.0 && part == right_sink) {
                      piercing_sink_pressure = std::max(
                          piercing_sink_pressure,
                          piercing_load[part][dim] / limit);
                      piercing_sink_excess = std::max(
                          piercing_sink_excess, excess_value / limit);
                    }
                  }
                }
                if (piercing_iteration % 32 == 0
                    || piercing_capacity
                    || (grow_sink && piercing_sink_excess > 0.0)
                    || (!grow_sink && piercing_source_excess > 0.0)) {
                  std::cerr << "PATRON_FLOW_PIERCING pair="
                            << edge_left << ':' << pair_target
                            << " iteration=" << piercing_iteration
                            << " forced="
                            << std::count(pierced.begin(), pierced.end(), true)
                            << " capacity_compatible="
                            << (piercing_capacity ? 1 : 0)
                            << " source_excess=" << piercing_source_excess
                            << " sink_excess=" << piercing_sink_excess
                            << '\n';
                }
                if (piercing_capacity) {
                  ProxyState candidate
                      = build_proxy_state(model, &piercing_assignment);
                  std::cerr << "PATRON_FLOW_PIERCING_FEASIBLE pair="
                            << edge_left << ':' << pair_target
                            << " index=" << feasible_piercing_cuts
                            << " forced="
                            << std::count(pierced.begin(), pierced.end(), true)
                            << " rank=";
                  for (long long value : candidate.evaluation.ranked) {
                    std::cerr << value << ',';
                  }
                  std::cerr << '\n';
                  if (best_piercing_assignment.empty()
                      || less_ranked(candidate.evaluation.ranked,
                                     best_piercing_rank)) {
                    best_piercing_assignment = piercing_assignment;
                    best_piercing_rank = candidate.evaluation.ranked;
                  }
                  if (candidate.evaluation.ranked.size() >= 2
                      && state.evaluation.ranked.size() >= 2
                      && candidate.evaluation.ranked[0]
                             < state.evaluation.ranked[0]
                      && candidate.evaluation.ranked[1]
                             < state.evaluation.ranked[1]
                      && (best_dual_piercing_assignment.empty()
                          || less_ranked(candidate.evaluation.ranked,
                                         best_dual_piercing_rank))) {
                    best_dual_piercing_assignment = piercing_assignment;
                    best_dual_piercing_rank = candidate.evaluation.ranked;
                  }
                  ++feasible_piercing_cuts;
                  if (feasible_piercing_cuts
                      >= maximum_legal_candidates) {
                    break;
                  }
                }
                current_piercing_load = std::move(piercing_load);
                current_source_excess = piercing_source_excess;
                current_sink_excess = piercing_sink_excess;
                current_source_pressure = piercing_source_pressure;
                current_sink_pressure = piercing_sink_pressure;
              }
              break;
            }
            long long step = lambda_increment;
            const double maximum_excess = std::max(
                source_excess, sink_excess);
            if (maximum_excess < 0.01) {
              step = std::min(step, 1LL);
            } else if (maximum_excess < 0.05) {
              step = std::min(step, 4LL);
            }
            const long long next_lambda = lambda + step;
            const long long delta_lambda = next_lambda - lambda;
            for (int cluster : parametric_variables) {
              const long long unary
                  = delta_lambda * balance_weight[cluster];
              if (push_to_sink) {
                parametric_flow.add_edge(
                    parametric_cluster_node[cluster],
                    parametric_sink_node,
                    unary);
              } else {
                parametric_flow.add_edge(
                    parametric_source_node,
                    parametric_cluster_node[cluster],
                    unary);
              }
            }
            lambda = next_lambda;
            if (step == lambda_increment) {
              lambda_increment = std::min(
                  lambda_increment * 2, 1LL << 24);
            } else {
              lambda_increment = step;
            }
            cumulative_flow += parametric_flow.maximum_flow(
                parametric_source_node, parametric_sink_node);
          }
          const auto report_parametric
              = [&](const std::string& label,
                    const std::vector<int>& assignment) {
                  if (assignment.empty()) {
                    return;
                  }
                  ProxyState legalized = build_proxy_state(model, &assignment);
                  std::cerr << "PATRON_FLOW_PARAMETRIC_RESULT pair="
                            << edge_left << ':' << pair_target
                            << " label=" << label
                            << " domain_load="
                            << legalized.domain_load[cover_domain]
                            << " improving="
                            << (less_ranked(legalized.evaluation.ranked,
                                            state.evaluation.ranked)
                                    ? 1
                                    : 0)
                            << " rank=";
                  for (long long value : legalized.evaluation.ranked) {
                    std::cerr << value << ',';
                  }
                  std::cerr << '\n';
                };
          report_parametric("first-feasible", first_feasible_assignment);
          if (best_parametric_assignment != first_feasible_assignment) {
            report_parametric("best-domain", best_parametric_assignment);
          }
          report_parametric("flowcutter-piercing", best_piercing_assignment);
          report_parametric("flowcutter-dual-improving",
                            best_dual_piercing_assignment);
          if (!best_piercing_assignment.empty()) {
            ProxyState polished
                = build_proxy_state(model, &best_piercing_assignment);
            std::vector<int> polish_order = parametric_variables;
            std::sort(polish_order.begin(), polish_order.end(),
                      [&](int left, int right) {
                        return std::tie(exposure[right], left)
                               < std::tie(exposure[left], right);
                      });
            int accepted_polish_moves = 0;
            for (int sweep = 0;
                 sweep < 2
                 && accepted_polish_moves < maximum_polish_moves;
                 ++sweep) {
              const int sweep_start = accepted_polish_moves;
              for (int cluster : polish_order) {
                if (accepted_polish_moves
                    >= maximum_polish_moves) {
                  break;
                }
                bool found = false;
                ProxyDelta best;
                for (int target = 0; target < model.parts; ++target) {
                  ProxyDelta candidate = evaluate_proxy_move(
                      model,
                      polished,
                      cluster_nets,
                      net_paths,
                      cluster,
                      target);
                  if (!candidate.feasible
                      || candidate.evaluation.ranked.size() < 2
                      || polished.evaluation.ranked.size() < 2
                      || candidate.evaluation.ranked[0]
                             > polished.evaluation.ranked[0]
                      || candidate.evaluation.ranked[1]
                             >= polished.evaluation.ranked[1]) {
                    continue;
                  }
                  if (!found
                      || less_ranked(candidate.evaluation.ranked,
                                     best.evaluation.ranked)
                      || (candidate.evaluation.ranked
                              == best.evaluation.ranked
                          && candidate.target < best.target)) {
                    found = true;
                    best = std::move(candidate);
                  }
                }
                if (found) {
                  apply_proxy_delta(model, polished, best);
                  ++accepted_polish_moves;
                }
              }
              if (accepted_polish_moves == sweep_start) {
                break;
              }
            }
            std::cerr << "PATRON_FLOW_POLISH pair=" << edge_left << ':'
                      << pair_target
                      << " accepted=" << accepted_polish_moves
                      << " dual_improving="
                      << (polished.evaluation.ranked.size() >= 2
                              && state.evaluation.ranked.size() >= 2
                              && polished.evaluation.ranked[0]
                                     < state.evaluation.ranked[0]
                              && polished.evaluation.ranked[1]
                                     < state.evaluation.ranked[1]
                              ? 1
                              : 0)
                      << " rank=";
            for (long long value : polished.evaluation.ranked) {
              std::cerr << value << ',';
            }
            std::cerr << '\n';
            if (polished.evaluation.ranked.size() >= 2
                && state.evaluation.ranked.size() >= 2
                && polished.evaluation.ranked[0]
                       < state.evaluation.ranked[0]
                && polished.evaluation.ranked[1]
                       < state.evaluation.ranked[1]
                && (selected_assignment.empty()
                    || less_ranked(polished.evaluation.ranked,
                                   selected_rank))) {
              selected_assignment = polished.assignment;
              selected_rank = polished.evaluation.ranked;
            }
          }
        }
      }
    }
  }
  return selected_assignment;
}

void erase_one(std::multiset<int>& values, int value) {
  const auto found = values.find(value);
  require(found != values.end(), "missing scalable ratio record");
  values.erase(found);
}

ProxyDelta evaluate_proxy_changes(
    const Model& model,
    ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    int cluster,
    int target,
    int partner = -1,
    int partner_target = -1,
    const std::vector<std::pair<int, int>>& extra_changes = {},
    bool enforce_capacity = true) {
  ProxyDelta delta;
  delta.cluster = cluster;
  delta.source = state.assignment[cluster];
  delta.target = target;
  if (target == delta.source || model.cluster[cluster].fixed >= 0) {
    return delta;
  }
  std::vector<std::tuple<int, int, int>> changes = {
      {cluster, delta.source, target}};
  std::set<int> changed_clusters = {cluster};
  if (partner >= 0) {
    if (partner == cluster || partner_target < 0
        || model.cluster[partner].fixed >= 0) {
      return delta;
    }
    delta.partner = partner;
    delta.partner_source = state.assignment[partner];
    delta.partner_target = partner_target;
    if (delta.partner_source == delta.partner_target) {
      return delta;
    }
    if (!changed_clusters.insert(partner).second) {
      return delta;
    }
    changes.emplace_back(
        partner, delta.partner_source, delta.partner_target);
  }
  for (const auto& extra : extra_changes) {
    const int changed_cluster = extra.first;
    const int changed_target = extra.second;
    if (changed_cluster < 0 || changed_cluster >= model.clusters
        || changed_target < 0 || changed_target >= model.parts
        || model.cluster[changed_cluster].fixed >= 0
        || state.assignment[changed_cluster] == changed_target
        || !changed_clusters.insert(changed_cluster).second) {
      return delta;
    }
    changes.emplace_back(changed_cluster,
                         state.assignment[changed_cluster],
                         changed_target);
  }

  auto projected_load = state.resource_load;
  auto projected_counts = state.part_counts;
  for (const auto& change : changes) {
    const int changed_cluster = std::get<0>(change);
    const int source = std::get<1>(change);
    const int changed_target = std::get<2>(change);
    --projected_counts[source];
    ++projected_counts[changed_target];
    for (int dim = 0; dim < model.dimensions; ++dim) {
      projected_load[source][dim]
          -= model.cluster[changed_cluster].weight[dim];
      projected_load[changed_target][dim]
          += model.cluster[changed_cluster].weight[dim];
    }
  }
  if (enforce_capacity) {
    if (std::count_if(projected_counts.begin(), projected_counts.end(),
                      [](int count) { return count > 0; })
        < model.min_used_parts) {
      return delta;
    }
    for (int part = 0; part < model.parts; ++part) {
      if (projected_counts[part] < 0) {
        return delta;
      }
      for (int dim = 0; dim < model.dimensions; ++dim) {
        if (projected_load[part][dim]
                > model.hard_capacity[part][dim] + 1.0e-9
            || projected_load[part][dim]
                   > model.balance_capacity[part][dim] + 1.0e-9) {
          return delta;
        }
      }
    }
  }

  for (const auto& change : changes) {
    state.assignment[std::get<0>(change)] = std::get<2>(change);
  }
  const auto restore_assignment = [&]() {
    for (const auto& change : changes) {
      state.assignment[std::get<0>(change)] = std::get<1>(change);
    }
  };
  std::set<int> affected_nets;
  for (const auto& change : changes) {
    const int changed_cluster = std::get<0>(change);
    affected_nets.insert(cluster_nets[changed_cluster].begin(),
                         cluster_nets[changed_cluster].end());
  }
  for (int net : affected_nets) {
    ProxyNetState replacement = build_proxy_net(model, state.assignment, net);
    if (!replacement.feasible) {
      restore_assignment();
      return delta;
    }
    delta.nets.emplace(net, std::move(replacement));
  }
  for (const auto& item : delta.nets) {
    for (const auto& domain : state.net[item.first].domain_counts) {
      delta.domain_delta[domain.first] -= domain.second;
    }
    for (const auto& domain : item.second.domain_counts) {
      delta.domain_delta[domain.first] += domain.second;
    }
  }
  for (auto item = delta.domain_delta.begin(); item != delta.domain_delta.end();) {
    if (item->second == 0) {
      item = delta.domain_delta.erase(item);
    } else {
      const int projected = state.domain_load[item->first] + item->second;
      if (projected < 0) {
        throw std::runtime_error("negative scalable domain load");
      }
      ++item;
    }
  }
  std::vector<int> projected_ratios = state.domain_ratio;
  std::set<int> ratio_changed_domains;
  for (const auto& item : delta.domain_delta) {
    const int projected_load = state.domain_load[item.first] + item.second;
    projected_ratios[item.first] = tdm_ratio(
        model, projected_load, model.domain[item.first]);
    if (projected_ratios[item.first] != state.domain_ratio[item.first]) {
      ratio_changed_domains.insert(item.first);
    }
  }
  std::set<int> affected_paths;
  for (const auto& item : delta.nets) {
    affected_paths.insert(
        net_paths[item.first].begin(), net_paths[item.first].end());
  }
  for (int domain : ratio_changed_domains) {
    affected_paths.insert(
        state.domain_paths[domain].begin(), state.domain_paths[domain].end());
  }
  for (int path : affected_paths) {
    delta.paths.emplace(
        path,
        build_proxy_path(
            model, state, delta.nets, projected_ratios, path));
  }
  restore_assignment();

  double candidate_negative = state.total_negative;
  long long candidate_negative_paths = state.negative_paths;
  long long candidate_snaking = state.snaking;
  long long candidate_hops = state.hops;
  long long candidate_cuts = state.cuts;
  for (const auto& item : delta.nets) {
    candidate_hops += item.second.hops - state.net[item.first].hops;
    candidate_cuts += item.second.cuts - state.net[item.first].cuts;
  }
  for (const auto& item : delta.paths) {
    const ProxyPathState& old = state.path[item.first];
    const ProxyPathState& replacement = item.second;
    candidate_negative += std::min(0.0, replacement.normalized_slack)
                          - std::min(0.0, old.normalized_slack);
    candidate_negative_paths += (replacement.negative ? 1 : 0)
                                - (old.negative ? 1 : 0);
    candidate_snaking += replacement.snaking - old.snaking;
  }
  double worst = std::numeric_limits<double>::infinity();
  for (const auto& item : delta.paths) {
    worst = std::min(worst, item.second.normalized_slack);
  }
  for (const auto& item : state.slack_path_order) {
    if (!affected_paths.count(item.second)) {
      worst = std::min(worst, item.first);
      break;
    }
  }
  require(std::isfinite(worst), "empty scalable candidate path order");
  const int maximum_ratio = *std::max_element(
      projected_ratios.begin(), projected_ratios.end());
  int maximum_load = 0;
  for (int domain = 0; domain < model.domains; ++domain) {
    const auto changed = delta.domain_delta.find(domain);
    maximum_load = std::max(
        maximum_load,
        state.domain_load[domain]
            + (changed == delta.domain_delta.end() ? 0 : changed->second));
  }
  delta.evaluation.feasible = true;
  delta.evaluation.objective = {
      -worst,
      -candidate_negative,
      static_cast<double>(candidate_negative_paths),
      static_cast<double>(maximum_ratio),
      static_cast<double>(maximum_load),
      static_cast<double>(candidate_snaking),
      static_cast<double>(candidate_hops),
      static_cast<double>(candidate_cuts),
  };
  delta.evaluation.ranked = {
      rank_float(delta.evaluation.objective[0]),
      rank_float(delta.evaluation.objective[1]),
      candidate_negative_paths,
      maximum_ratio,
      maximum_load,
      candidate_snaking,
      candidate_hops,
      candidate_cuts,
  };
  delta.feasible = true;

  return delta;
}

ProxyDelta evaluate_proxy_move(
    const Model& model,
    ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    int cluster,
    int target) {
  return evaluate_proxy_changes(
      model, state, cluster_nets, net_paths, cluster, target);
}

ProxyDelta evaluate_proxy_ejection(
    const Model& model,
    ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    int cluster,
    int partner,
    int partner_target) {
  ProxyDelta invalid;
  if (cluster == partner) {
    return invalid;
  }
  const int source = state.assignment[cluster];
  const int partner_source = state.assignment[partner];
  if (source == partner_source || partner_target == partner_source) {
    return invalid;
  }
  return evaluate_proxy_changes(model,
                                state,
                                cluster_nets,
                                net_paths,
                                cluster,
                                partner_source,
                                partner,
                                partner_target);
}

ProxyDelta evaluate_proxy_comove(
    const Model& model,
    ProxyState& state,
    const std::vector<std::vector<int>>& cluster_nets,
    const std::vector<std::vector<int>>& net_paths,
    int cluster,
    int partner,
    int target) {
  ProxyDelta invalid;
  if (cluster == partner || state.assignment[cluster] != state.assignment[partner]
      || target == state.assignment[cluster]) {
    return invalid;
  }
  return evaluate_proxy_changes(model,
                                state,
                                cluster_nets,
                                net_paths,
                                cluster,
                                target,
                                partner,
                                target);
}

void apply_proxy_delta(const Model& model,
                       ProxyState& state,
                       const ProxyDelta& delta) {
  std::vector<std::tuple<int, int, int>> changes = {
      {delta.cluster, delta.source, delta.target}};
  if (delta.partner >= 0) {
    changes.emplace_back(
        delta.partner, delta.partner_source, delta.partner_target);
  }
  for (const auto& change : changes) {
    const int cluster = std::get<0>(change);
    const int source = std::get<1>(change);
    const int target = std::get<2>(change);
    for (int dim = 0; dim < model.dimensions; ++dim) {
      const double weight = model.cluster[cluster].weight[dim];
      state.resource_load[source][dim] -= weight;
      state.resource_load[target][dim] += weight;
    }
    --state.part_counts[source];
    ++state.part_counts[target];
    state.assignment[cluster] = target;
  }
  for (const auto& item : delta.nets) {
    state.hops += item.second.hops - state.net[item.first].hops;
    state.cuts += item.second.cuts - state.net[item.first].cuts;
    state.net[item.first] = item.second;
  }
  for (const auto& item : delta.domain_delta) {
    const int domain = item.first;
    erase_one(state.ratio_order, state.domain_ratio[domain]);
    state.domain_load[domain] += item.second;
    state.domain_ratio[domain]
        = tdm_ratio(model, state.domain_load[domain], model.domain[domain]);
    state.ratio_order.insert(state.domain_ratio[domain]);
  }
  for (const auto& item : delta.paths) {
    ProxyPathState& old = state.path[item.first];
    for (int domain : old.dependency_domains) {
      const std::size_t erased = state.domain_paths[domain].erase(item.first);
      require(erased == 1, "missing scalable domain/path dependency");
    }
    const std::size_t ranked_erased = state.ranked_path_order.erase(
        {rank_float(old.normalized_slack), item.first});
    require(ranked_erased == 1, "missing scalable ranked path");
    const std::size_t slack_erased = state.slack_path_order.erase(
        {old.normalized_slack, item.first});
    require(slack_erased == 1, "missing scalable slack/path record");
    state.total_negative += std::min(0.0, item.second.normalized_slack)
                            - std::min(0.0, old.normalized_slack);
    state.negative_paths += (item.second.negative ? 1 : 0)
                            - (old.negative ? 1 : 0);
    state.snaking += item.second.snaking - old.snaking;
    old = item.second;
    for (int domain : old.dependency_domains) {
      state.domain_paths[domain].insert(item.first);
    }
    state.slack_path_order.emplace(old.normalized_slack, item.first);
    state.ranked_path_order.emplace(
        rank_float(old.normalized_slack), item.first);
  }
  state.evaluation = proxy_evaluation(state);
  require(state.evaluation.ranked == delta.evaluation.ranked,
          "scalable apply/evaluate mismatch");
}

void write_vector(std::ostream& stream, const std::vector<double>& values) {
  for (double value : values) {
    stream << ' ' << std::setprecision(17) << value;
  }
}

void write_vector(std::ostream& stream,
                  const std::vector<long long>& values) {
  for (long long value : values) {
    stream << ' ' << value;
  }
}

struct NativeMove {
  int index = 0;
  int phase = 0;
  int sweep = 0;
  int cluster = -1;
  int source = -1;
  int target = -1;
  int partner = -1;
  int partner_source = -1;
  int partner_target = -1;
  Evaluation before;
  Evaluation after;
};

struct NativeBatch {
  std::vector<std::tuple<int, int, int>> changes;
  Evaluation before;
  Evaluation after;
};

void write_output(const std::string& output_path,
                  const std::string& mode,
                  const Evaluation& initial,
                  const Evaluation& final,
                  const std::vector<NativeMove>& moves,
                  const std::vector<int>& assignment,
                  const std::vector<NativeBatch>& batches = {},
                  int flow_output_version = 0) {
  std::ofstream output(output_path);
  require(output.good(), "cannot open output");
  const char* header = "EMUFLOW_PATRON_OUTPUT_V6\n";
  if (flow_output_version == 11) {
    header = "EMUFLOW_PATRON_OUTPUT_V11\n";
  } else if (flow_output_version == 10) {
    header = "EMUFLOW_PATRON_OUTPUT_V10\n";
  } else if (flow_output_version == 9) {
    header = "EMUFLOW_PATRON_OUTPUT_V9\n";
  } else if (flow_output_version == 8) {
    header = "EMUFLOW_PATRON_OUTPUT_V8\n";
  } else if (flow_output_version == 7) {
    header = "EMUFLOW_PATRON_OUTPUT_V7\n";
  }
  output << header;
  output << "MODE " << mode << '\n';
  output << "INITIAL";
  write_vector(output, initial.objective);
  output << '\n';
  for (const NativeMove& move : moves) {
    output << "STEP " << move.index << ' ' << move.phase << ' '
           << move.sweep << ' '
           << move.cluster << ' '
           << move.source << ' ' << move.target << ' '
           << move.partner << ' ' << move.partner_source << ' '
           << move.partner_target;
    write_vector(output, move.before.objective);
    write_vector(output, move.after.objective);
    write_vector(output, move.after.ranked);
    output << '\n';
  }
  for (int batch = 0; batch < static_cast<int>(batches.size()); ++batch) {
    const NativeBatch& record = batches[batch];
    output << "BATCH " << batch << ' ' << record.changes.size();
    write_vector(output, record.before.objective);
    write_vector(output, record.after.objective);
    write_vector(output, record.after.ranked);
    output << '\n';
    for (const auto& change : record.changes) {
      output << "CHANGE " << batch << ' '
             << std::get<0>(change) << ' '
             << std::get<1>(change) << ' '
             << std::get<2>(change) << '\n';
    }
  }
  output << "FINAL";
  write_vector(output, final.objective);
  output << '\n';
  for (int cluster = 0; cluster < static_cast<int>(assignment.size()); ++cluster) {
    output << "ASSIGN " << cluster << ' ' << assignment[cluster] << '\n';
  }
  output << "END\n";
}

void run_exact(const Model& model, const std::string& output_path) {
  std::vector<int> assignment(model.clusters);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    assignment[cluster] = model.cluster[cluster].part;
  }
  Evaluation current = evaluate(model, assignment);
  require(current.feasible, "initial assignment is infeasible");

  const Evaluation initial = current;
  std::vector<NativeMove> moves;
  while (static_cast<int>(moves.size()) < model.max_moves) {
    bool found = false;
    int best_phase = -1;
    int best_cluster = -1;
    int best_target = -1;
    int best_partner = -1;
    int best_partner_source = -1;
    int best_partner_target = -1;
    Evaluation best;
    for (int cluster = 0; cluster < model.clusters; ++cluster) {
      if (model.cluster[cluster].fixed >= 0) {
        continue;
      }
      const int source = assignment[cluster];
      for (int target = 0; target < model.parts; ++target) {
        if (target == source) {
          continue;
        }
        assignment[cluster] = target;
        Evaluation candidate = evaluate(model, assignment);
        assignment[cluster] = source;
        if (!candidate.feasible
            || !less_ranked(candidate.ranked, current.ranked)) {
          continue;
        }
        const auto identity = std::make_tuple(0, cluster, target, -1, -1);
        const auto best_identity = std::make_tuple(
            best_phase,
            best_cluster,
            best_target,
            best_partner,
            best_partner_target);
        if (!found || less_ranked(candidate.ranked, best.ranked)
            || (candidate.ranked == best.ranked
                && identity < best_identity)) {
          found = true;
          best_phase = 0;
          best_cluster = cluster;
          best_target = target;
          best_partner = -1;
          best_partner_source = -1;
          best_partner_target = -1;
          best = std::move(candidate);
        }
      }
    }
    for (int cluster = 0; cluster < model.clusters; ++cluster) {
      if (model.cluster[cluster].fixed >= 0) {
        continue;
      }
      const int source = assignment[cluster];
      for (int target = 0; target < model.parts; ++target) {
        if (target == source) {
          continue;
        }
        for (int partner = 0; partner < model.clusters; ++partner) {
          if (partner == cluster || model.cluster[partner].fixed >= 0
              || assignment[partner] != target) {
            continue;
          }
          const int partner_source = assignment[partner];
          for (int partner_target = 0;
               partner_target < model.parts;
               ++partner_target) {
            if (partner_target == partner_source) {
              continue;
            }
            assignment[cluster] = target;
            assignment[partner] = partner_target;
            Evaluation candidate = evaluate(model, assignment);
            assignment[cluster] = source;
            assignment[partner] = partner_source;
            if (!candidate.feasible
                || !less_ranked(candidate.ranked, current.ranked)) {
              continue;
            }
            const auto identity = std::make_tuple(
                1, cluster, target, partner, partner_target);
            const auto best_identity = std::make_tuple(
                best_phase,
                best_cluster,
                best_target,
                best_partner,
                best_partner_target);
            if (!found || less_ranked(candidate.ranked, best.ranked)
                || (candidate.ranked == best.ranked
                    && identity < best_identity)) {
              found = true;
              best_phase = 1;
              best_cluster = cluster;
              best_target = target;
              best_partner = partner;
              best_partner_source = partner_source;
              best_partner_target = partner_target;
              best = std::move(candidate);
            }
          }
        }
      }
    }
    if (!found) {
      break;
    }
    const int source = assignment[best_cluster];
    moves.push_back(NativeMove{
        static_cast<int>(moves.size()),
        best_phase,
        0,
        best_cluster,
        source,
        best_target,
        best_partner,
        best_partner_source,
        best_partner_target,
        current,
        best});
    assignment[best_cluster] = best_target;
    if (best_partner >= 0) {
      assignment[best_partner] = best_partner_target;
    }
    current = best;
  }

  write_output(output_path,
               "endpoint-exact-global-best-v6",
               initial,
               current,
               moves,
               assignment);
}

void run_scalable(const Model& model, const std::string& output_path) {
  ProxyState state = build_proxy_state(model);
  const Evaluation initial = state.evaluation;
  const auto cluster_nets = build_cluster_nets(model);
  const auto net_paths = build_net_paths(model);

  std::vector<double> exposure(model.clusters, 0.0);
  for (int path = 0; path < model.paths; ++path) {
    const double criticality = std::max(
        0.0,
        std::min(1.0,
                 1.0 - model.path[path].slack_ns / model.path[path].period_ns));
    const double weight = 1.0 + 9.0 * criticality * criticality;
    std::set<int> touched;
    for (int net : model.path[path].nets) {
      touched.insert(model.net[net].drivers.begin(), model.net[net].drivers.end());
      touched.insert(model.net[net].sinks.begin(), model.net[net].sinks.end());
    }
    for (int cluster : touched) {
      exposure[cluster] += weight;
    }
  }
  std::vector<int> order(model.clusters);
  std::iota(order.begin(), order.end(), 0);
  std::sort(order.begin(), order.end(), [&](int left, int right) {
    return std::tie(exposure[right], left)
           < std::tie(exposure[left], right);
  });

  std::vector<NativeMove> moves;
  std::vector<NativeBatch> batches;
  for (int sweep = 0; sweep < model.max_sweeps; ++sweep) {
    const std::size_t sweep_start = moves.size();
    for (int cluster : order) {
      if (static_cast<int>(moves.size()) >= model.max_moves) {
        break;
      }
      bool found = false;
      ProxyDelta best;
      for (int target = 0; target < model.parts; ++target) {
        ProxyDelta candidate = evaluate_proxy_move(
            model, state, cluster_nets, net_paths, cluster, target);
        if (!candidate.feasible
            || !less_ranked(candidate.evaluation.ranked,
                            state.evaluation.ranked)) {
          continue;
        }
        if (!found
            || less_ranked(candidate.evaluation.ranked,
                           best.evaluation.ranked)
            || (candidate.evaluation.ranked == best.evaluation.ranked
                && candidate.target < best.target)) {
          found = true;
          best = std::move(candidate);
        }
      }
      if (!found) {
        continue;
      }
      const Evaluation before = state.evaluation;
      apply_proxy_delta(model, state, best);
      moves.push_back(NativeMove{
          static_cast<int>(moves.size()),
          0,
          sweep,
          cluster,
          best.source,
          best.target,
          -1,
          -1,
          -1,
          before,
          state.evaluation});
    }
    if (static_cast<int>(moves.size()) >= model.max_moves
        || moves.size() == sweep_start) {
      break;
    }
  }

  std::vector<int> donor_order(model.clusters);
  std::iota(donor_order.begin(), donor_order.end(), 0);
  std::sort(donor_order.begin(), donor_order.end(), [&](int left, int right) {
    return std::tie(exposure[left], left)
           < std::tie(exposure[right], right);
  });
  std::vector<std::vector<int>> donors(model.parts);
  for (int cluster : donor_order) {
    if (model.cluster[cluster].fixed < 0) {
      donors[state.assignment[cluster]].push_back(cluster);
    }
  }

  int accepted_ejections = 0;
  long long evaluated_ejections = 0;
  long long feasible_ejections = 0;
  long long improving_ejections = 0;
  long long wns_improving_ejections = 0;
  const int critical_limit = std::min(
      model.ejection_critical_limit, static_cast<int>(order.size()));
  for (int order_index = 0;
       order_index < critical_limit
           && accepted_ejections < model.max_ejections
           && static_cast<int>(moves.size()) < model.max_moves;
       ++order_index) {
    const int cluster = order[order_index];
    if (model.cluster[cluster].fixed >= 0) {
      continue;
    }
    const int source = state.assignment[cluster];
    bool found = false;
    ProxyDelta best;
    for (int target = 0; target < model.parts; ++target) {
      if (target == source) {
        continue;
      }
      int considered = 0;
      for (int partner : donors[target]) {
        if (considered >= model.ejection_donor_limit) {
          break;
        }
        if (partner == cluster || state.assignment[partner] != target
            || model.cluster[partner].fixed >= 0) {
          continue;
        }
        ++considered;
        for (int partner_target = 0;
             partner_target < model.parts;
             ++partner_target) {
          if (partner_target == target) {
            continue;
          }
          ProxyDelta candidate = evaluate_proxy_ejection(
              model,
              state,
              cluster_nets,
              net_paths,
              cluster,
              partner,
              partner_target);
          ++evaluated_ejections;
          if (candidate.feasible) {
            ++feasible_ejections;
            if (candidate.evaluation.ranked[0]
                < state.evaluation.ranked[0]) {
              ++wns_improving_ejections;
            }
            if (less_ranked(candidate.evaluation.ranked,
                            state.evaluation.ranked)) {
              ++improving_ejections;
            }
          }
          if (!candidate.feasible
              || !less_ranked(candidate.evaluation.ranked,
                              state.evaluation.ranked)) {
            continue;
          }
          if (!found
              || less_ranked(candidate.evaluation.ranked,
                             best.evaluation.ranked)
              || (candidate.evaluation.ranked == best.evaluation.ranked
                  && std::tie(candidate.target,
                              candidate.partner,
                              candidate.partner_target)
                         < std::tie(best.target,
                                    best.partner,
                                    best.partner_target))) {
            found = true;
            best = std::move(candidate);
          }
        }
      }
    }
    if (!found) {
      continue;
    }
    const Evaluation before = state.evaluation;
    apply_proxy_delta(model, state, best);
    moves.push_back(NativeMove{
        static_cast<int>(moves.size()),
        1,
        0,
        cluster,
        best.source,
        best.target,
        best.partner,
        best.partner_source,
        best.partner_target,
        before,
        state.evaluation});
    ++accepted_ejections;
  }
  std::cerr << "PATRON_EJECTION_STATS evaluated=" << evaluated_ejections
            << " feasible=" << feasible_ejections
            << " improving=" << improving_ejections
            << " wns_improving=" << wns_improving_ejections
            << " accepted=" << accepted_ejections << '\n';

  long long evaluated_comoves = 0;
  long long feasible_comoves = 0;
  long long improving_comoves = 0;
  long long wns_improving_comoves = 0;
  int accepted_comoves = 0;
  for (int order_index = 0;
       order_index < critical_limit
           && accepted_comoves < model.max_ejections
           && static_cast<int>(moves.size()) < model.max_moves;
       ++order_index) {
    const int cluster = order[order_index];
    if (model.cluster[cluster].fixed >= 0) {
      continue;
    }
    const int source = state.assignment[cluster];
    std::set<int> neighbor_set;
    for (int net : cluster_nets[cluster]) {
      neighbor_set.insert(model.net[net].drivers.begin(),
                          model.net[net].drivers.end());
      neighbor_set.insert(model.net[net].sinks.begin(),
                          model.net[net].sinks.end());
    }
    std::vector<int> neighbors;
    for (int neighbor : neighbor_set) {
      if (neighbor != cluster && state.assignment[neighbor] == source
          && model.cluster[neighbor].fixed < 0) {
        neighbors.push_back(neighbor);
      }
    }
    std::sort(neighbors.begin(), neighbors.end(), [&](int left, int right) {
      return std::tie(exposure[right], left)
             < std::tie(exposure[left], right);
    });
    if (static_cast<int>(neighbors.size()) > model.ejection_donor_limit) {
      neighbors.resize(model.ejection_donor_limit);
    }
    bool found = false;
    ProxyDelta best;
    for (int target = 0; target < model.parts; ++target) {
      if (target == source) {
        continue;
      }
      for (int partner : neighbors) {
        ProxyDelta candidate = evaluate_proxy_comove(
            model,
            state,
            cluster_nets,
            net_paths,
            cluster,
            partner,
            target);
        ++evaluated_comoves;
        if (candidate.feasible) {
          ++feasible_comoves;
          if (candidate.evaluation.ranked[0]
              < state.evaluation.ranked[0]) {
            ++wns_improving_comoves;
          }
          if (less_ranked(candidate.evaluation.ranked,
                          state.evaluation.ranked)) {
            ++improving_comoves;
          }
        }
        if (!candidate.feasible
            || !less_ranked(candidate.evaluation.ranked,
                            state.evaluation.ranked)) {
          continue;
        }
        if (!found
            || less_ranked(candidate.evaluation.ranked,
                           best.evaluation.ranked)
            || (candidate.evaluation.ranked == best.evaluation.ranked
                && std::tie(candidate.target, candidate.partner)
                       < std::tie(best.target, best.partner))) {
          found = true;
          best = std::move(candidate);
        }
      }
    }
    if (!found) {
      continue;
    }
    const Evaluation before = state.evaluation;
    apply_proxy_delta(model, state, best);
    moves.push_back(NativeMove{
        static_cast<int>(moves.size()),
        2,
        0,
        cluster,
        best.source,
        best.target,
        best.partner,
        best.partner_source,
        best.partner_target,
        before,
        state.evaluation});
    ++accepted_comoves;
  }
  std::cerr << "PATRON_COMOVE_STATS evaluated=" << evaluated_comoves
            << " feasible=" << feasible_comoves
            << " improving=" << improving_comoves
            << " wns_improving=" << wns_improving_comoves
            << " accepted=" << accepted_comoves << '\n';

  long long evaluated_corridors = 0;
  long long feasible_corridors = 0;
  long long improving_corridors = 0;
  long long wns_improving_corridors = 0;
  bool found_corridor = false;
  ProxyDelta best_corridor;
  std::vector<int> best_corridor_clusters;
  std::vector<int> current_paths(model.paths);
  std::iota(current_paths.begin(), current_paths.end(), 0);
  std::sort(current_paths.begin(), current_paths.end(), [&](int left, int right) {
    return std::tie(state.path[left].normalized_slack, left)
           < std::tie(state.path[right].normalized_slack, right);
  });
  if (!current_paths.empty()) {
    std::cerr << "PATRON_CURRENT_WORST path=" << current_paths.front()
              << " normalized_slack="
              << state.path[current_paths.front()].normalized_slack
              << " nets=" << model.path[current_paths.front()].nets.size()
              << '\n';
    const long long worst_rank = rank_float(
        state.path[current_paths.front()].normalized_slack);
    int tied_paths = 0;
    std::set<int> critical_domains;
    for (int path : current_paths) {
      if (rank_float(state.path[path].normalized_slack) != worst_rank) {
        break;
      }
      ++tied_paths;
      critical_domains.insert(
          state.path[path].dependency_domains.begin(),
          state.path[path].dependency_domains.end());
    }
    std::cerr << "PATRON_CURRENT_FRONTIER tied_paths=" << tied_paths
              << " domains=";
    for (int domain : critical_domains) {
      const int ratio = state.domain_ratio[domain];
      const int lower_ratio = ratio <= 1
                                  ? 1
                                  : std::max(1, ratio - model.ratio_quantum);
      const int lower_threshold = model.domain[domain].is_sll
                                      ? state.domain_load[domain]
                                      : model.domain[domain].lanes * lower_ratio;
      std::cerr << domain << ':' << state.domain_load[domain] << ':'
                << ratio << ':' << lower_threshold << ',';
    }
    std::cerr << '\n';
  }
  for (int rank = 0; rank < std::min(16, model.paths); ++rank) {
    const int path = current_paths[rank];
    std::cerr << "PATRON_CURRENT_PATH rank=" << rank
              << " path=" << path
              << " normalized_slack=" << state.path[path].normalized_slack
              << " nets=" << model.path[path].nets.size() << '\n';
  }

  const char* cover_diagnostic_value
      = std::getenv("EMUFLOW_PATRON_COVER_DIAGNOSTIC");
  const bool cover_diagnostic = cover_diagnostic_value != nullptr
                                && std::string(cover_diagnostic_value) == "1";
  const bool flow_apply = model.flow_refinement;
  if (cover_diagnostic && model.parts <= 8) {
    std::vector<int> permutation(model.parts);
    std::iota(permutation.begin(), permutation.end(), 0);
    do {
      bool identity = true;
      for (int part = 0; part < model.parts; ++part) {
        identity = identity && permutation[part] == part;
      }
      if (identity) {
        continue;
      }
      std::vector<int> candidate_assignment(model.clusters);
      bool fixed_compatible = true;
      for (int cluster = 0; cluster < model.clusters; ++cluster) {
        candidate_assignment[cluster] = permutation[state.assignment[cluster]];
        if (model.cluster[cluster].fixed >= 0
            && model.cluster[cluster].fixed != candidate_assignment[cluster]) {
          fixed_compatible = false;
          break;
        }
      }
      if (!fixed_compatible) {
        continue;
      }
      bool capacity_compatible = true;
      for (int source = 0; source < model.parts; ++source) {
        const int target = permutation[source];
        for (int dim = 0; dim < model.dimensions; ++dim) {
          if (state.resource_load[source][dim]
                  > model.hard_capacity[target][dim] + 1.0e-9
              || state.resource_load[source][dim]
                     > model.balance_capacity[target][dim] + 1.0e-9) {
            capacity_compatible = false;
            break;
          }
        }
      }
      if (!capacity_compatible) {
        continue;
      }
      ProxyState candidate = build_proxy_state(model, &candidate_assignment);
      std::cerr << "PATRON_BLOCK_PERMUTATION map=";
      for (int part : permutation) {
        std::cerr << part << ',';
      }
      std::cerr << " improving="
                << (less_ranked(candidate.evaluation.ranked,
                                state.evaluation.ranked)
                        ? 1
                        : 0)
                << " rank=";
      for (long long value : candidate.evaluation.ranked) {
        std::cerr << value << ',';
      }
      std::cerr << '\n';
    } while (std::next_permutation(permutation.begin(), permutation.end()));
  }

  struct CoverMove {
    int cluster = -1;
    int target = -1;
    int reduction = 0;
    long long tns_cost = 0;
  };
  struct CoverOperation {
    std::vector<std::pair<int, int>> changes;
    int reduction = 0;
    long long tns_cost = 0;
  };
  std::set<int> frontier_domains;
  const long long frontier_rank = current_paths.empty()
                                      ? 0
                                      : rank_float(state.path[
                                            current_paths.front()]
                                                        .normalized_slack);
  for (int path : current_paths) {
    if (rank_float(state.path[path].normalized_slack) != frontier_rank) {
      break;
    }
    frontier_domains.insert(state.path[path].dependency_domains.begin(),
                            state.path[path].dependency_domains.end());
  }
  int cover_domain = -1;
  int cover_threshold = 0;
  int cover_deficit = std::numeric_limits<int>::max();
  for (int domain : frontier_domains) {
    if (model.domain[domain].is_sll || state.domain_ratio[domain] <= 1) {
      continue;
    }
    const int lower_ratio = std::max(
        1, state.domain_ratio[domain] - model.ratio_quantum);
    const int threshold = model.domain[domain].lanes * lower_ratio;
    const int deficit = state.domain_load[domain] - threshold;
    if (deficit > 0
        && std::tie(deficit, domain)
               < std::tie(cover_deficit, cover_domain)) {
      cover_domain = domain;
      cover_threshold = threshold;
      cover_deficit = deficit;
    }
  }
  if ((cover_diagnostic || flow_apply) && cover_domain >= 0) {
    std::vector<int> flow_assignment = diagnose_flow_corridors(
        model, state, cluster_nets, net_paths, exposure, cover_domain);
    if (flow_apply) {
      if (!flow_assignment.empty()) {
        ProxyState refined = build_proxy_state(model, &flow_assignment);
        long long evaluated_tail_moves = 0;
        long long feasible_tail_moves = 0;
        int accepted_tail_moves = 0;
        for (int iteration = 0;
             model.flow_version >= 8
                 && iteration < model.flow_max_tail_moves;
             ++iteration) {
          require(!refined.slack_path_order.empty(),
                  "flow tail repair has no timing paths");
          std::set<int> candidate_clusters;
          const auto add_path_candidates = [&](int path) {
            if (model.path[path].start_cluster >= 0) {
              candidate_clusters.insert(model.path[path].start_cluster);
            }
            if (model.path[path].end_cluster >= 0) {
              candidate_clusters.insert(model.path[path].end_cluster);
            }
            for (int net : model.path[path].nets) {
              candidate_clusters.insert(model.net[net].drivers.begin(),
                                        model.net[net].drivers.end());
              candidate_clusters.insert(model.net[net].sinks.begin(),
                                        model.net[net].sinks.end());
            }
          };
          const long long worst_rank = refined.ranked_path_order.begin()->first;
          int exact_frontier_paths = 0;
          for (const auto& ranked_path : refined.ranked_path_order) {
            if (ranked_path.first != worst_rank
                || exact_frontier_paths
                       >= model.flow_max_frontier_paths) {
              break;
            }
            ++exact_frontier_paths;
            add_path_candidates(ranked_path.second);
          }
          bool found = false;
          ProxyDelta best;
          const auto evaluate_candidates = [&]() {
            for (int cluster : candidate_clusters) {
              if (model.cluster[cluster].fixed >= 0) {
                continue;
              }
              for (int target = 0; target < model.parts; ++target) {
                if (target == refined.assignment[cluster]) {
                  continue;
                }
                ProxyDelta candidate = evaluate_proxy_move(
                    model,
                    refined,
                    cluster_nets,
                    net_paths,
                    cluster,
                    target);
                ++evaluated_tail_moves;
                if (!candidate.feasible) {
                  continue;
                }
                ++feasible_tail_moves;
                if (!less_ranked(candidate.evaluation.ranked,
                                 refined.evaluation.ranked)) {
                  continue;
                }
                if (!found
                    || less_ranked(candidate.evaluation.ranked,
                                   best.evaluation.ranked)
                    || (candidate.evaluation.ranked
                            == best.evaluation.ranked
                        && std::tie(candidate.cluster, candidate.target)
                               < std::tie(best.cluster, best.target))) {
                  found = true;
                  best = std::move(candidate);
                }
              }
            }
          };
          evaluate_candidates();
          if (!found && model.flow_version >= 9) {
            // A locally immovable exact-worst path must not terminate timing
            // closure while another near-critical path can still improve the
            // global TNS without degrading WNS.  Expand deterministically to
            // the ranked path window only after the exact frontier stalls.
            candidate_clusters.clear();
            const int frontier_paths = std::min(
                model.flow_max_frontier_paths, model.paths);
            int ranked_count = 0;
            for (const auto& ranked_path : refined.ranked_path_order) {
              if (ranked_count >= frontier_paths) {
                break;
              }
              add_path_candidates(ranked_path.second);
              ++ranked_count;
            }
            evaluate_candidates();
          }
          if (!found) {
            break;
          }
          apply_proxy_delta(model, refined, best);
          ++accepted_tail_moves;
        }
        std::cerr << "PATRON_FLOW_TAIL_REPAIR evaluated="
                  << evaluated_tail_moves
                  << " feasible=" << feasible_tail_moves
                  << " accepted=" << accepted_tail_moves
                  << " rank=";
        for (long long value : refined.evaluation.ranked) {
          std::cerr << value << ',';
        }
        std::cerr << '\n';
        require(refined.evaluation.ranked.size() >= 2
                    && state.evaluation.ranked.size() >= 2
                    && refined.evaluation.ranked[0]
                           < state.evaluation.ranked[0]
                    && refined.evaluation.ranked[1]
                           < state.evaluation.ranked[1],
                "flow refinement did not improve WNS and TNS");
        NativeBatch batch;
        batch.before = state.evaluation;
        batch.after = refined.evaluation;
        for (int cluster = 0; cluster < model.clusters; ++cluster) {
          if (state.assignment[cluster] != refined.assignment[cluster]) {
            batch.changes.emplace_back(cluster,
                                       state.assignment[cluster],
                                       refined.assignment[cluster]);
          }
        }
        require(!batch.changes.empty(), "flow refinement batch is empty");
        batches.push_back(std::move(batch));
        state = std::move(refined);
      }
    }
  }
  if (cover_diagnostic && !flow_apply && cover_domain >= 0) {
    std::vector<CoverMove> raw_cover_moves;
    std::vector<CoverOperation> cover_operations;
    long long evaluated_cover_moves = 0;
    long long feasible_direct_cover_moves = 0;
    for (int cluster = 0; cluster < model.clusters; ++cluster) {
      if (model.cluster[cluster].fixed >= 0) {
        continue;
      }
      for (int target = 0; target < model.parts; ++target) {
        if (target == state.assignment[cluster]) {
          continue;
        }
        ProxyDelta candidate = evaluate_proxy_changes(
            model,
            state,
            cluster_nets,
            net_paths,
            cluster,
            target,
            -1,
            -1,
            {},
            false);
        ++evaluated_cover_moves;
        if (!candidate.feasible) {
          continue;
        }
        const auto delta = candidate.domain_delta.find(cover_domain);
        if (delta == candidate.domain_delta.end() || delta->second >= 0) {
          continue;
        }
        const CoverMove move{
            cluster,
            target,
            -delta->second,
            candidate.evaluation.ranked[1] - state.evaluation.ranked[1]};
        raw_cover_moves.push_back(move);
        ProxyDelta direct = evaluate_proxy_move(
            model, state, cluster_nets, net_paths, cluster, target);
        if (direct.feasible) {
          ++feasible_direct_cover_moves;
          cover_operations.push_back(CoverOperation{
              {{cluster, target}},
              move.reduction,
              move.tns_cost});
        }
      }
    }
    std::map<int, int> best_raw_reduction_by_cluster;
    for (const CoverMove& move : raw_cover_moves) {
      best_raw_reduction_by_cluster[move.cluster] = std::max(
          best_raw_reduction_by_cluster[move.cluster], move.reduction);
    }
    const int maximum_raw_reduction = std::accumulate(
        best_raw_reduction_by_cluster.begin(),
        best_raw_reduction_by_cluster.end(),
        0,
        [](int total, const std::pair<const int, int>& item) {
          return total + item.second;
        });
    std::cerr << "PATRON_COVER_INPUT domain=" << cover_domain
              << " load=" << state.domain_load[cover_domain]
              << " threshold=" << cover_threshold
              << " deficit=" << cover_deficit
              << " evaluated=" << evaluated_cover_moves
              << " raw_reducing_moves=" << raw_cover_moves.size()
              << " raw_reducing_clusters="
              << best_raw_reduction_by_cluster.size()
              << " maximum_raw_reduction=" << maximum_raw_reduction
              << " feasible_direct_moves=" << feasible_direct_cover_moves
              << '\n';

    std::sort(raw_cover_moves.begin(), raw_cover_moves.end(),
              [](const CoverMove& left, const CoverMove& right) {
      if (left.reduction != right.reduction) {
        return left.reduction > right.reduction;
      }
      if (left.tns_cost != right.tns_cost) {
        return left.tns_cost < right.tns_cost;
      }
      return std::tie(left.cluster, left.target)
             < std::tie(right.cluster, right.target);
    });
    std::vector<std::vector<int>> cover_donors(model.parts);
    for (int cluster : donor_order) {
      if (model.cluster[cluster].fixed < 0) {
        cover_donors[state.assignment[cluster]].push_back(cluster);
      }
    }
    constexpr int kCoverPrimaryLimit = 512;
    constexpr int kCoverDonorLimit = 128;
    long long evaluated_cover_ejections = 0;
    long long feasible_cover_ejections = 0;
    const int primary_limit = std::min(
        kCoverPrimaryLimit, static_cast<int>(raw_cover_moves.size()));
    for (int move_index = 0; move_index < primary_limit; ++move_index) {
      const CoverMove& move = raw_cover_moves[move_index];
      const int source = state.assignment[move.cluster];
      bool found = false;
      CoverOperation best;
      int considered_donors = 0;
      for (int donor : cover_donors[move.target]) {
        if (considered_donors >= kCoverDonorLimit) {
          break;
        }
        if (donor == move.cluster) {
          continue;
        }
        ++considered_donors;
        std::vector<int> donor_targets;
        donor_targets.push_back(source);
        for (int target = 0; target < model.parts; ++target) {
          if (target != source && target != move.target) {
            donor_targets.push_back(target);
          }
        }
        for (int donor_target : donor_targets) {
          ProxyDelta candidate = evaluate_proxy_changes(
              model,
              state,
              cluster_nets,
              net_paths,
              move.cluster,
              move.target,
              donor,
              donor_target);
          ++evaluated_cover_ejections;
          if (!candidate.feasible) {
            continue;
          }
          ++feasible_cover_ejections;
          const auto delta = candidate.domain_delta.find(cover_domain);
          if (delta == candidate.domain_delta.end() || delta->second >= 0) {
            continue;
          }
          CoverOperation operation{
              {{move.cluster, move.target}, {donor, donor_target}},
              -delta->second,
              candidate.evaluation.ranked[1]
                  - state.evaluation.ranked[1]};
          if (!found || operation.reduction > best.reduction
              || (operation.reduction == best.reduction
                  && std::tie(operation.tns_cost, operation.changes)
                         < std::tie(best.tns_cost, best.changes))) {
            found = true;
            best = std::move(operation);
          }
        }
      }
      if (found) {
        cover_operations.push_back(std::move(best));
      }
    }
    std::cerr << "PATRON_COVER_EJECTION_INPUT primaries=" << primary_limit
              << " donor_limit=" << kCoverDonorLimit
              << " evaluated=" << evaluated_cover_ejections
              << " feasible=" << feasible_cover_ejections
              << " operations=" << cover_operations.size() << '\n';

    constexpr int kCoverGroupLimit = 256;
    std::set<std::vector<std::pair<int, int>>> seen_cover_groups;
    long long cover_transition_options = 0;
    long long evaluated_cover_groups = 0;
    long long reducing_cover_groups = 0;
    long long feasible_cover_groups = 0;
    long long skipped_large_cover_groups = 0;
    int maximum_cover_group_size = 0;
    int maximum_cover_group_reduction = 0;
    const auto evaluate_cover_group = [&](const std::vector<int>& members,
                                          int source,
                                          int target) {
      ++cover_transition_options;
      std::map<int, int> change_map;
      for (int cluster : members) {
        if (state.assignment[cluster] != source) {
          continue;
        }
        if (model.cluster[cluster].fixed >= 0) {
          return;
        }
        change_map[cluster] = target;
      }
      std::vector<std::pair<int, int>> changes(
          change_map.begin(), change_map.end());
      if (changes.empty()) {
        return;
      }
      maximum_cover_group_size = std::max(
          maximum_cover_group_size, static_cast<int>(changes.size()));
      if (static_cast<int>(changes.size()) > kCoverGroupLimit) {
        ++skipped_large_cover_groups;
        return;
      }
      if (!seen_cover_groups.insert(changes).second) {
        return;
      }
      std::vector<std::pair<int, int>> extras;
      if (changes.size() > 1) {
        extras.assign(changes.begin() + 1, changes.end());
      }
      ProxyDelta raw = evaluate_proxy_changes(
          model,
          state,
          cluster_nets,
          net_paths,
          changes[0].first,
          changes[0].second,
          -1,
          -1,
          extras,
          false);
      ++evaluated_cover_groups;
      if (!raw.feasible) {
        return;
      }
      const auto raw_delta = raw.domain_delta.find(cover_domain);
      if (raw_delta == raw.domain_delta.end() || raw_delta->second >= 0) {
        return;
      }
      ++reducing_cover_groups;
      maximum_cover_group_reduction = std::max(
          maximum_cover_group_reduction, -raw_delta->second);
      ProxyDelta feasible = evaluate_proxy_changes(
          model,
          state,
          cluster_nets,
          net_paths,
          changes[0].first,
          changes[0].second,
          -1,
          -1,
          extras);
      if (!feasible.feasible) {
        return;
      }
      ++feasible_cover_groups;
      cover_operations.push_back(CoverOperation{
          changes,
          -raw_delta->second,
          feasible.evaluation.ranked[1] - state.evaluation.ranked[1]});
    };
    for (int net = 0; net < model.nets; ++net) {
      for (const Transition& transition : state.net[net].transitions) {
        const Route& route = model.route[transition.source][transition.sink];
        const bool uses_cover_domain = std::any_of(
            route.arcs.begin(), route.arcs.end(), [&](const Arc& arc) {
              return arc.domain == cover_domain;
            });
        if (!uses_cover_domain) {
          continue;
        }
        evaluate_cover_group(model.net[net].sinks,
                             transition.sink,
                             transition.source);
        evaluate_cover_group(model.net[net].drivers,
                             transition.source,
                             transition.sink);
      }
    }
    std::cerr << "PATRON_COVER_GROUP_INPUT options="
              << cover_transition_options
              << " unique=" << seen_cover_groups.size()
              << " evaluated=" << evaluated_cover_groups
              << " reducing=" << reducing_cover_groups
              << " feasible=" << feasible_cover_groups
              << " maximum_group_size=" << maximum_cover_group_size
              << " maximum_group_reduction="
              << maximum_cover_group_reduction
              << " skipped_large=" << skipped_large_cover_groups
              << " operations=" << cover_operations.size() << '\n';

    for (int heuristic = 0; heuristic < 3; ++heuristic) {
      std::vector<CoverOperation> ordered = cover_operations;
      std::sort(ordered.begin(), ordered.end(), [&](const CoverOperation& left,
                                                    const CoverOperation& right) {
        if (heuristic == 0) {
          if (left.reduction != right.reduction) {
            return left.reduction > right.reduction;
          }
          return std::tie(left.tns_cost, left.changes)
                 < std::tie(right.tns_cost, right.changes);
        }
        if (heuristic == 1) {
          const long double left_ratio
              = static_cast<long double>(left.tns_cost) / left.reduction;
          const long double right_ratio
              = static_cast<long double>(right.tns_cost) / right.reduction;
          if (left_ratio != right_ratio) {
            return left_ratio < right_ratio;
          }
          if (left.tns_cost != right.tns_cost) {
            return left.tns_cost < right.tns_cost;
          }
          if (left.reduction != right.reduction) {
            return left.reduction > right.reduction;
          }
          return left.changes < right.changes;
        }
        if (left.tns_cost != right.tns_cost) {
          return left.tns_cost < right.tns_cost;
        }
        if (left.reduction != right.reduction) {
          return left.reduction > right.reduction;
        }
        return left.changes < right.changes;
      });
      auto projected_load = state.resource_load;
      auto projected_counts = state.part_counts;
      std::set<int> selected_clusters;
      std::vector<std::pair<int, int>> selected;
      int estimated_reduction = 0;
      for (const CoverOperation& candidate : ordered) {
        if (estimated_reduction >= cover_deficit) {
          continue;
        }
        bool disjoint = true;
        for (const auto& change : candidate.changes) {
          if (selected_clusters.count(change.first) != 0) {
            disjoint = false;
            break;
          }
        }
        if (!disjoint) {
          continue;
        }
        auto candidate_load = projected_load;
        auto candidate_counts = projected_counts;
        for (const auto& change : candidate.changes) {
          const int cluster = change.first;
          const int source = state.assignment[cluster];
          const int target = change.second;
          --candidate_counts[source];
          ++candidate_counts[target];
          for (int dim = 0; dim < model.dimensions; ++dim) {
            const double weight = model.cluster[cluster].weight[dim];
            candidate_load[source][dim] -= weight;
            candidate_load[target][dim] += weight;
          }
        }
        bool fits = true;
        if (std::count_if(candidate_counts.begin(), candidate_counts.end(),
                          [](int count) { return count > 0; })
            < model.min_used_parts) {
          fits = false;
        }
        for (int part = 0; fits && part < model.parts; ++part) {
          for (int dim = 0; dim < model.dimensions; ++dim) {
            if (candidate_load[part][dim]
                    > model.hard_capacity[part][dim] + 1.0e-9
                || candidate_load[part][dim]
                       > model.balance_capacity[part][dim] + 1.0e-9) {
              fits = false;
              break;
            }
          }
        }
        if (!fits) {
          continue;
        }
        projected_load = std::move(candidate_load);
        projected_counts = std::move(candidate_counts);
        for (const auto& change : candidate.changes) {
          selected_clusters.insert(change.first);
          selected.push_back(change);
        }
        estimated_reduction += candidate.reduction;
      }
      std::cerr << "PATRON_COVER_SELECTION heuristic=" << heuristic
                << " selected=" << selected.size()
                << " estimated_reduction=" << estimated_reduction;
      if (estimated_reduction < cover_deficit || selected.empty()) {
        std::cerr << " status=insufficient\n";
        continue;
      }
      std::vector<std::pair<int, int>> extras;
      if (selected.size() > 2) {
        extras.assign(selected.begin() + 2, selected.end());
      }
      ProxyDelta batch = evaluate_proxy_changes(
          model,
          state,
          cluster_nets,
          net_paths,
          selected[0].first,
          selected[0].second,
          selected.size() > 1 ? selected[1].first : -1,
          selected.size() > 1 ? selected[1].second : -1,
          extras);
      const auto actual = batch.domain_delta.find(cover_domain);
      std::cerr << " feasible=" << (batch.feasible ? 1 : 0)
                << " actual_reduction="
                << (actual == batch.domain_delta.end() ? 0 : -actual->second)
                << " improving="
                << (batch.feasible
                        && less_ranked(batch.evaluation.ranked,
                                       state.evaluation.ranked)
                    ? 1
                    : 0)
                << " rank=";
      if (batch.feasible) {
        for (long long value : batch.evaluation.ranked) {
          std::cerr << value << ',';
        }
      }
      std::cerr << '\n';
    }
  }
  const int path_limit = 0;
  for (int path_index = 0; path_index < path_limit; ++path_index) {
    const int path = current_paths[path_index];
    std::map<int, int> touch_count;
    for (int net : model.path[path].nets) {
      for (int touched : model.net[net].drivers) {
        ++touch_count[touched];
      }
      for (int touched : model.net[net].sinks) {
        ++touch_count[touched];
      }
    }
    for (int left_part = 0; left_part < model.parts; ++left_part) {
      for (int right_part = left_part + 1;
           right_part < model.parts;
           ++right_part) {
        std::vector<int> corridor;
        bool has_left = false;
        bool has_right = false;
        for (const auto& touched : touch_count) {
          const int cluster = touched.first;
          const int part = state.assignment[cluster];
          if ((part == left_part || part == right_part)
              && model.cluster[cluster].fixed < 0) {
            corridor.push_back(cluster);
            has_left = has_left || part == left_part;
            has_right = has_right || part == right_part;
          }
        }
        if (!has_left || !has_right || corridor.size() < 2) {
          continue;
        }
        std::sort(corridor.begin(), corridor.end(), [&](int left, int right) {
          return std::tie(touch_count[right], exposure[right], left)
                 < std::tie(touch_count[left], exposure[left], right);
        });
        if (corridor.size() > 12) {
          corridor.resize(12);
        }
        const std::uint64_t limit = std::uint64_t{1} << corridor.size();
        for (std::uint64_t mask = 1; mask < limit; ++mask) {
          std::vector<std::pair<int, int>> changes;
          std::vector<int> selected;
          for (std::size_t bit = 0; bit < corridor.size(); ++bit) {
            if ((mask & (std::uint64_t{1} << bit)) == 0) {
              continue;
            }
            const int cluster = corridor[bit];
            const int target = state.assignment[cluster] == left_part
                                   ? right_part
                                   : left_part;
            changes.emplace_back(cluster, target);
            selected.push_back(cluster);
          }
          if (changes.size() < 2) {
            continue;
          }
          std::vector<std::pair<int, int>> extras;
          if (changes.size() > 2) {
            extras.assign(changes.begin() + 2, changes.end());
          }
          ProxyDelta candidate = evaluate_proxy_changes(
              model,
              state,
              cluster_nets,
              net_paths,
              changes[0].first,
              changes[0].second,
              changes[1].first,
              changes[1].second,
              extras);
          ++evaluated_corridors;
          if (!candidate.feasible) {
            continue;
          }
          ++feasible_corridors;
          if (candidate.evaluation.ranked[0]
              < state.evaluation.ranked[0]) {
            ++wns_improving_corridors;
          }
          if (!less_ranked(candidate.evaluation.ranked,
                           state.evaluation.ranked)) {
            continue;
          }
          ++improving_corridors;
          if (!found_corridor
              || less_ranked(candidate.evaluation.ranked,
                             best_corridor.evaluation.ranked)
              || (candidate.evaluation.ranked
                      == best_corridor.evaluation.ranked
                  && selected < best_corridor_clusters)) {
            found_corridor = true;
            best_corridor = std::move(candidate);
            best_corridor_clusters = std::move(selected);
          }
        }
      }
    }
  }
  std::cerr << "PATRON_CURRENT_CORRIDOR_STATS evaluated="
            << evaluated_corridors << " feasible=" << feasible_corridors
            << " improving=" << improving_corridors
            << " wns_improving=" << wns_improving_corridors;
  if (found_corridor) {
    std::cerr << " best_rank=";
    for (long long value : best_corridor.evaluation.ranked) {
      std::cerr << value << ',';
    }
    std::cerr << " clusters=";
    for (int selected : best_corridor_clusters) {
      std::cerr << selected << ',';
    }
  }
  std::cerr << '\n';
  const ProxyState endpoint = build_proxy_state(model, &state.assignment);
  std::string mode = "endpoint-exact-critical-ejection-v6";
  if (model.flow_version == 11) {
    mode = "endpoint-exact-critical-flow-v11";
  } else if (model.flow_version == 10) {
    mode = "endpoint-exact-critical-flow-v10";
  } else if (model.flow_version == 9) {
    mode = "endpoint-exact-critical-flow-v9";
  } else if (model.flow_version == 8) {
    mode = "endpoint-exact-critical-flow-v8";
  } else if (model.flow_version == 7) {
    mode = "endpoint-exact-critical-flow-v7";
  }
  write_output(output_path,
               mode,
               initial,
               endpoint.evaluation,
               moves,
               state.assignment,
               batches,
               model.flow_version);
}

void run(const Model& model, const std::string& output_path) {
  if (model.clusters <= 256 && !model.flow_refinement) {
    run_exact(model, output_path);
  } else {
    run_scalable(model, output_path);
  }
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_patron_refiner INPUT OUTPUT\n";
    return 0;
  }
  if (argc != 3) {
    std::cerr << "usage: emuflow_patron_refiner INPUT OUTPUT\n";
    return 2;
  }
  try {
    Model model = read_model(argv[1]);
    run(model, argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_patron_refiner: " << error.what() << '\n';
    return 1;
  }
}
