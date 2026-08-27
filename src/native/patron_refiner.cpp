#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
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
  double positive_scale = 1.0;
  double negative_scale = 1.0;
  double max_period = 1.0;
  double boundary_fanout_penalty_scale_ns = 0.0;
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
  std::multiset<double> slack_order;
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
                   * model.domain[arc.domain].cycle_ns;
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
  require(token == "EMUFLOW_PATRON_INPUT_V6", "invalid input header");
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
  const double worst = *state.slack_order.begin();
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
    state.slack_order.insert(state.path[path].normalized_slack);
    state.total_negative += std::min(0.0, state.path[path].normalized_slack);
    state.negative_paths += state.path[path].negative ? 1 : 0;
    state.snaking += state.path[path].snaking;
  }
  require(!state.slack_order.empty() && !state.ratio_order.empty(),
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

void erase_one(std::multiset<double>& values, double value) {
  const auto found = values.find(value);
  require(found != values.end(), "missing scalable slack record");
  values.erase(found);
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
    const std::vector<std::pair<int, int>>& extra_changes = {}) {
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
    erase_one(state.slack_order, old.normalized_slack);
    state.slack_order.insert(replacement.normalized_slack);
    candidate_negative += std::min(0.0, replacement.normalized_slack)
                          - std::min(0.0, old.normalized_slack);
    candidate_negative_paths += (replacement.negative ? 1 : 0)
                                - (old.negative ? 1 : 0);
    candidate_snaking += replacement.snaking - old.snaking;
  }
  for (const auto& item : delta.domain_delta) {
    const int domain = item.first;
    erase_one(state.ratio_order, state.domain_ratio[domain]);
    state.ratio_order.insert(tdm_ratio(
        model, state.domain_load[domain] + item.second, model.domain[domain]));
  }

  const double worst = *state.slack_order.begin();
  const int maximum_ratio = *state.ratio_order.rbegin();
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

  for (const auto& item : delta.domain_delta) {
    const int domain = item.first;
    erase_one(
        state.ratio_order,
        tdm_ratio(model,
                  state.domain_load[domain] + item.second,
                  model.domain[domain]));
    state.ratio_order.insert(state.domain_ratio[domain]);
  }
  for (const auto& item : delta.paths) {
    erase_one(state.slack_order, item.second.normalized_slack);
    state.slack_order.insert(state.path[item.first].normalized_slack);
  }
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
    erase_one(state.slack_order, old.normalized_slack);
    state.total_negative += std::min(0.0, item.second.normalized_slack)
                            - std::min(0.0, old.normalized_slack);
    state.negative_paths += (item.second.negative ? 1 : 0)
                            - (old.negative ? 1 : 0);
    state.snaking += item.second.snaking - old.snaking;
    old = item.second;
    for (int domain : old.dependency_domains) {
      state.domain_paths[domain].insert(item.first);
    }
    state.slack_order.insert(old.normalized_slack);
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

void write_output(const std::string& output_path,
                  const std::string& mode,
                  const Evaluation& initial,
                  const Evaluation& final,
                  const std::vector<NativeMove>& moves,
                  const std::vector<int>& assignment) {
  std::ofstream output(output_path);
  require(output.good(), "cannot open output");
  output << "EMUFLOW_PATRON_OUTPUT_V6\n";
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
  write_output(output_path,
               "endpoint-exact-critical-ejection-v6",
               initial,
               endpoint.evaluation,
               moves,
               state.assignment);
}

void run(const Model& model, const std::string& output_path) {
  if (model.clusters <= 256) {
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
