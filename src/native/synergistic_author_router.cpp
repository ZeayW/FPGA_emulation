// Author-derived route-only adapter for EmuFlow.
//
// Algorithm lineage: the source supplied by the authors of "Synergistic
// Die-Level Router for Multi-FPGA System with Time-Division Multiplexing
// Optimization" (DAC 2025), specifically MazeRouter::runConnectionRoute and
// MazeRouter::routeSingleSink in system-level-routing-icisc-date24.zip.
// This is an adaptation of their global hard-connection-first ordering and
// sequential shared-parent-tree maze search, not a call to EmuFlow's TLR router.
// No license for the supplied archive was stated; this file records provenance
// rather than assigning the authors' implementation an invented license.
//
// Deliberate adaptations/fixes:
// * Standard C++17 headers, indexed directed/parallel arcs, arbitrary BoardDB
//   delay coefficients, width-weighted usage, shared capacities and banks.
// * Retain the author's size-dependent ordering and shared-edge half-cost
//   policy. The original net_sink_size was accumulated before shortest paths
//   existed; its effective sink-count tie break is explicit here. Final sink
//   index is a deterministic tie break for otherwise equivalent connections.
// * Board delays replace hardcoded constants. The default author-pressure-v1
//   prices continuous pre-insertion usage as in paper Eq. (2); the explicitly
//   named quantized-ratio-v1 policy preserves the first adapter for ablation.
//   Quantization still constrains feasibility under both cost policies.
// * Hop-constrained search uses (node, hops) labels. No interactive getchar,
//   dangling vector references, unchecked missing parents, or partial output.
// * Bank directions and half-duplex links are physical constraints. This first
//   version commits directions greedily and fails explicitly if ordering
//   cannot find a legal solution; it does not silently invoke another router.
// * PATH and normalized_slack inputs are accepted for protocol compatibility
//   but are not optimization inputs. Python independently evaluates timing.

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <queue>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();

struct Arc {
  int link = -1, from = -1, to = -1, domain = -1, direction_group = -1;
  int opposite = -1, lanes = 0, banks = 0, wires = 0;
  long long capacity = 0;
  double delay = 0.0, beta = 0.0;
  bool sll = false;
};

struct Demand {
  int source = -1;
  std::vector<int> sinks;
  long long width = 0;
};

struct Input {
  int nodes = -1, quantum = 0, frame_slots = 0, minimum = 0, max_hops = 0;
  std::vector<Arc> arcs;
  std::vector<Demand> demands;
};

std::vector<int> parse_indices(const std::string& value) {
  std::vector<int> result;
  if (value == "-") return result;
  std::stringstream stream(value);
  std::string token;
  while (std::getline(stream, token, ',')) {
    if (token.empty()) throw std::runtime_error("empty index in list");
    size_t used = 0;
    const int index = std::stoi(token, &used);
    if (used != token.size()) throw std::runtime_error("invalid index in list");
    result.push_back(index);
  }
  if (!value.empty() && value.back() == ',')
    throw std::runtime_error("trailing comma in index list");
  return result;
}

Input read_input(const std::string& filename) {
  std::ifstream input(filename);
  if (!input) throw std::runtime_error("cannot open input: " + filename);
  std::string header;
  std::getline(input, header);
  const bool v9 = header == "EMUFLOW_TLR_INPUT_V9_BANKS_V1";
  if (!v9 && header != "EMUFLOW_TLR_INPUT_V7_BANKS_V1")
    throw std::runtime_error("unsupported input header: " + header);
  Input model;
  bool have_params = false;
  std::string line;
  while (std::getline(input, line)) {
    if (line.empty() || line[0] == '#') continue;
    std::stringstream stream(line);
    std::string kind;
    stream >> kind;
    if (kind == "PARAM") {
      if (have_params) throw std::runtime_error("duplicate PARAM");
      int mode, iterations, reroutes, tree_sum, hard_sll;
      double load, timing, history, tdm, positive, negative, period;
      stream >> model.nodes >> mode >> iterations >> reroutes >> load >> timing
          >> history >> tdm >> model.quantum >> model.frame_slots >> positive
          >> negative >> period >> tree_sum >> model.minimum >> hard_sll
          >> model.max_hops;
      if (!stream || model.nodes < 0 || model.quantum <= 0 ||
          model.frame_slots <= 0 || model.minimum <= 0 || model.max_hops < 0)
        throw std::runtime_error("invalid PARAM");
      // The common writer appends candidate_workers even to V7 inputs. It is
      // accepted here, but this algorithm is deliberately single threaded.
      std::string worker_token;
      if (stream >> worker_token) {
        size_t used = 0;
        const int workers = std::stoi(worker_token, &used);
        if (used != worker_token.size() || workers != 1)
          throw std::runtime_error("author router requires one worker");
      } else if (v9) {
        throw std::runtime_error("V9 PARAM is missing candidate workers");
      }
      have_params = true;
    } else if (kind == "ARC") {
      Arc arc;
      int index, sll;
      stream >> index >> arc.link >> arc.from >> arc.to >> arc.domain
          >> arc.direction_group >> arc.opposite >> arc.capacity >> arc.lanes
          >> arc.delay >> arc.beta >> sll >> arc.banks >> arc.wires;
      if (!stream || index != static_cast<int>(model.arcs.size()) ||
          arc.capacity <= 0 || arc.lanes <= 0 || arc.domain < 0 ||
          arc.link < 0 || arc.direction_group < -1 || arc.opposite < -1 ||
          !std::isfinite(arc.delay) || arc.delay < 0.0 ||
          !std::isfinite(arc.beta) || arc.beta < 0.0 ||
          (sll != 0 && sll != 1) || arc.banks < 0 || arc.wires < 0 ||
          ((arc.banks == 0) != (arc.wires == 0)))
        throw std::runtime_error("invalid or noncontiguous ARC");
      arc.sll = sll != 0;
      model.arcs.push_back(arc);
    } else if (kind == "DEMAND") {
      Demand demand;
      int index;
      std::string sinks;
      double slack, prediction;
      stream >> index >> demand.source >> sinks >> demand.width >> slack >> prediction;
      if (!stream || index != static_cast<int>(model.demands.size()) ||
          demand.width <= 0 || !std::isfinite(slack) || !std::isfinite(prediction))
        throw std::runtime_error("invalid or noncontiguous DEMAND");
      demand.sinks = parse_indices(sinks);
      model.demands.push_back(std::move(demand));
    } else if (kind == "PATH") {
      // Timing is evaluated against the complete path population by the
      // independent Python consumer; this route-only kernel does not use it.
      int index;
      double period, slack, fixed;
      std::string demands;
      stream >> index >> period >> slack >> fixed >> demands;
      if (!stream || index < 0 || !std::isfinite(period) || period <= 0 ||
          !std::isfinite(slack) || !std::isfinite(fixed))
        throw std::runtime_error("invalid PATH");
      (void)parse_indices(demands);
    } else {
      throw std::runtime_error("unsupported record: " + kind);
    }
    std::string trailing;
    if (stream >> trailing) throw std::runtime_error("unexpected trailing fields in " + kind);
  }
  if (!have_params) throw std::runtime_error("missing PARAM");
  for (const Arc& arc : model.arcs) {
    if (arc.from < 0 || arc.from >= model.nodes || arc.to < 0 ||
        arc.to >= model.nodes || arc.from == arc.to)
      throw std::runtime_error("ARC endpoint outside node range or self loop");
  }
  for (Demand& demand : model.demands) {
    if (demand.source < 0 || demand.source >= model.nodes)
      throw std::runtime_error("DEMAND source outside node range");
    std::set<int> seen;
    std::vector<int> unique;
    for (const int sink : demand.sinks) {
      if (sink < 0 || sink >= model.nodes)
        throw std::runtime_error("DEMAND sink outside node range");
      if (sink != demand.source && seen.insert(sink).second) unique.push_back(sink);
    }
    demand.sinks = std::move(unique);
  }
  return model;
}

struct Domain {
  long long capacity = 0, usage = 0;
  int lanes = 0, opposite = -1, banks = 0, wires = 0;
  int bank_link = -1;
  bool bank_forward = false;
  int sll_link = -1, sll_orientation = -1;
  bool sll = false;
};

struct Tree {
  std::vector<int> parent_arc;
};

enum class CostPolicy { kAuthorPressure, kQuantizedRatio };

class Router {
 public:
  explicit Router(Input input, CostPolicy policy)
      : input_(std::move(input)), adjacency_(input_.nodes), policy_(policy) {
    for (int index = 0; index < static_cast<int>(input_.arcs.size()); ++index) {
      const Arc& arc = input_.arcs[index];
      adjacency_[arc.from].push_back(index);
      Domain& domain = domains_[arc.domain];
      if (domain.capacity && (domain.capacity != arc.capacity ||
          domain.lanes != arc.lanes || domain.sll != arc.sll ||
          domain.banks != arc.banks || domain.wires != arc.wires))
        throw std::runtime_error("inconsistent capacity-domain metadata");
      domain.capacity = arc.capacity;
      domain.lanes = arc.lanes;
      domain.sll = arc.sll;
      domain.banks = arc.banks;
      domain.wires = arc.wires;
      if (arc.sll) {
        // Shared capacity is explicit in the common protocol: opposite arcs
        // have the same capacity-domain id. Independently capacitated duplex
        // directions must remain separate even when their physical link ids
        // match. Round layers preserve link ids and node-order orientation.
        const bool shared = arc.opposite >= 0 &&
            arc.opposite < static_cast<int>(input_.arcs.size()) &&
            input_.arcs[arc.opposite].domain == arc.domain;
        const int orientation = shared ? 0 : (arc.from < arc.to ? 1 : 2);
        if (domain.sll_link >= 0 && (domain.sll_link != arc.link || domain.sll_orientation != orientation))
          throw std::runtime_error("inconsistent physical SLL capacity domain");
        domain.sll_link = arc.link;
        domain.sll_orientation = orientation;
      }
      if (arc.direction_group >= 0) {
        const auto inserted = group_link_.emplace(arc.direction_group, arc.link);
        if (!inserted.second && inserted.first->second != arc.link)
          throw std::runtime_error("direction group spans different physical links");
        locks_.emplace(arc.direction_group, -1);
      }
      if (arc.banks) {
        if (arc.sll || arc.opposite < 0 ||
            arc.opposite >= static_cast<int>(input_.arcs.size()))
          throw std::runtime_error("bank ARC requires a reverse TDM ARC");
        const Arc& opposite = input_.arcs[arc.opposite];
        if (opposite.opposite != index || opposite.from != arc.to ||
            opposite.to != arc.from || opposite.link != arc.link ||
            opposite.banks != arc.banks || opposite.wires != arc.wires ||
            opposite.domain == arc.domain)
          throw std::runtime_error("inconsistent bank reverse ARC");
        if (domain.bank_link >= 0 && (domain.bank_link != arc.link ||
            domain.bank_forward != (arc.from < arc.to) || domain.opposite != opposite.domain))
          throw std::runtime_error("inconsistent physical bank domain");
        domain.opposite = opposite.domain;
        domain.bank_link = arc.link;
        domain.bank_forward = arc.from < arc.to;
      }
    }
    // Layered transport-round inputs have different capacity domains but the
    // same physical link id. A bank's direction must be fixed across all such
    // layers; selecting a separate split per round can publish illegal routes.
    for (const auto& item : domains_) {
      const Domain& domain = item.second;
      if (domain.banks && domain.bank_forward)
        bank_layers_[domain.bank_link].emplace_back(item.first, domain.opposite);
      if (domain.sll)
        sll_layers_[{domain.sll_link, domain.sll_orientation}].push_back(item.first);
    }
    for (const auto& group : bank_layers_) {
      const Domain& reference = domains_.at(group.second.front().first);
      for (const auto& layer : group.second) {
        const Domain& forward = domains_.at(layer.first);
        const Domain& reverse = domains_.at(layer.second);
        if (forward.banks != reference.banks || forward.wires != reference.wires ||
            reverse.banks != reference.banks || reverse.wires != reference.wires)
          throw std::runtime_error("physical bank metadata differs between layers");
      }
    }
    for (const auto& group : sll_layers_) {
      const long long capacity = domains_.at(group.second.front()).capacity;
      for (const int domain : group.second)
        if (domains_.at(domain).capacity != capacity)
          throw std::runtime_error("physical SLL capacity differs between layers");
    }
    for (auto& adjacent : adjacency_) {
      std::sort(adjacent.begin(), adjacent.end(), [&](int left, int right) {
        const Arc& a = input_.arcs[left];
        const Arc& b = input_.arcs[right];
        return std::tie(a.to, a.link, left) < std::tie(b.to, b.link, right);
      });
    }
    trees_.resize(input_.demands.size());
    for (Tree& tree : trees_) tree.parent_arc.assign(input_.nodes, -1);
  }

  void run() {
    struct Connection { int demand, sink; double distance; };
    std::vector<Connection> connections;
    std::map<int, std::vector<double>> distance_cache;
    // Preserve the original integer-density branches. Empty graphs are valid
    // only with no demands and must not divide by zero.
    const size_t density = input_.nodes ? input_.demands.size() / input_.nodes : 0;
    tiny_ = density < 50;
    const bool prefer_tdm = density > 10000;
    for (int index = 0; index < static_cast<int>(input_.demands.size()); ++index) {
      const Demand& demand = input_.demands[index];
      auto found = distance_cache.find(demand.source);
      if (found == distance_cache.end())
        found = distance_cache.emplace(demand.source, ordering_distances(demand.source, prefer_tdm)).first;
      for (const int sink : demand.sinks) {
        const double distance = found->second[sink];
        if (!std::isfinite(distance))
          throw std::runtime_error("unreachable demand " + std::to_string(index) +
                                   " sink " + std::to_string(sink));
        connections.push_back({index, sink, distance});
      }
    }
    std::sort(connections.begin(), connections.end(), [&](const Connection& a, const Connection& b) {
      if (a.distance != b.distance) return a.distance > b.distance;
      const size_t left = input_.demands[a.demand].sinks.size();
      const size_t right = input_.demands[b.demand].sinks.size();
      if (left != right) return left < right;
      return std::tie(a.demand, a.sink) < std::tie(b.demand, b.sink);
    });
    connections_ = connections.size();
    for (const Connection& connection : connections) {
      if (trees_[connection.demand].parent_arc[connection.sink] == -1)
        route_sink(connection.demand, connection.sink);
      ++routed_connections_;
    }
    // Unused half-duplex links still receive a deterministic orientation so
    // artifact consumers can require complete direction-lock coverage.
    for (auto& lock : locks_) {
      if (lock.second >= 0) continue;
      for (int index = 0; index < static_cast<int>(input_.arcs.size()); ++index)
        if (input_.arcs[index].direction_group == lock.first) {
          lock.second = index;
          break;
        }
    }
    validate_final();
  }

  void write_output(const std::string& filename) const {
    const std::string temporary = filename + ".tmp";
    std::ofstream output(temporary);
    if (!output) throw std::runtime_error("cannot create output: " + temporary);
    output << "EMUFLOW_SYNERGISTIC_ROUTE_OUTPUT_V1\n";
    size_t edges = 0;
    for (size_t index = 0; index < trees_.size(); ++index) {
      std::vector<int> arcs;
      for (const int arc : trees_[index].parent_arc) if (arc >= 0) arcs.push_back(arc);
      std::sort(arcs.begin(), arcs.end());
      edges += arcs.size();
      output << "ROUTE " << index << ' ';
      if (arcs.empty()) output << '-';
      for (size_t i = 0; i < arcs.size(); ++i) output << (i ? "," : "") << arcs[i];
      output << '\n';
    }
    output << "METRIC connections " << connections_ << '\n'
           << "METRIC routed_connections " << routed_connections_ << '\n'
           << "METRIC search_expansions " << expansions_ << '\n'
           << "METRIC tree_edges " << edges << '\n';
    output.close();
    if (!output) {
      std::remove(temporary.c_str());
      throw std::runtime_error("error writing output");
    }
    if (std::rename(temporary.c_str(), filename.c_str()) != 0) {
      std::remove(temporary.c_str());
      throw std::runtime_error("cannot publish output: " + filename);
    }
  }

 private:
  std::vector<double> ordering_distances(int source, bool prefer_tdm) const {
    std::vector<double> distances(input_.nodes, kInf);
    using Entry = std::pair<double, int>;
    std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> queue;
    distances[source] = 0.0;
    queue.emplace(0.0, source);
    while (!queue.empty()) {
      const auto now = queue.top(); queue.pop();
      if (now.first != distances[now.second]) continue;
      for (const int index : adjacency_[now.second]) {
        const Arc& arc = input_.arcs[index];
        const double weight = (arc.sll != prefer_tdm) ? 1.0 : input_.nodes + 1.0;
        const double proposed = now.first + weight;
        if (proposed < distances[arc.to]) {
          distances[arc.to] = proposed;
          queue.emplace(proposed, arc.to);
        }
      }
    }
    return distances;
  }

  long long ratio(long long usage, long long lanes) const {
    if (usage <= 0) return 1;
    if (lanes <= 0) return std::numeric_limits<long long>::max();
    long long raw = std::max<long long>(input_.minimum, usage / lanes + (usage % lanes != 0));
    if (raw == 1) return 1;
    if (raw > input_.frame_slots) return std::numeric_limits<long long>::max();
    return ((raw - 1) / input_.quantum + 1) * static_cast<long long>(input_.quantum);
  }

  long long maximum_ratio(const Domain& domain) const {
    if (domain.sll) return 1;
    const long long slots = std::min<long long>(input_.frame_slots, domain.capacity / domain.lanes);
    if (slots < input_.minimum) return 0;
    if (slots < input_.quantum) return input_.minimum == 1 ? 1 : 0;
    return slots / input_.quantum * input_.quantum;
  }

  struct BankEstimate {
    long long first = 0, second = 0;
    int first_banks = 0;
  };

  BankEstimate bank_ratios(int domain_id, long long addition) const {
    const Domain& target = domains_.at(domain_id);
    const auto& layers = bank_layers_.at(target.bank_link);
    using Objective = std::tuple<long long, long long, double, double, int>;
    Objective best{std::numeric_limits<long long>::max(), std::numeric_limits<long long>::max(), kInf, kInf, target.banks + 1};
    BankEstimate answer;
    for (int left = 0; left <= target.banks; ++left) {
      const long long left_lanes = static_cast<long long>(left) * target.wires;
      const long long right_lanes = static_cast<long long>(target.banks - left) * target.wires;
      long long largest_ratio = 0, sum_ratio = 0;
      double largest_pressure = 0, sum_pressure = 0;
      bool feasible = true;
      BankEstimate proposed;
      for (const auto& layer : layers) {
        const Domain& forward = domains_.at(layer.first);
        const Domain& reverse = domains_.at(layer.second);
        const long long forward_usage = forward.usage + (layer.first == domain_id ? addition : 0);
        const long long reverse_usage = reverse.usage + (layer.second == domain_id ? addition : 0);
        const long long a = ratio(forward_usage, left_lanes), b = ratio(reverse_usage, right_lanes);
        if (a > maximum_ratio(forward) || b > maximum_ratio(reverse)) { feasible = false; break; }
        const double p = forward_usage ? static_cast<double>(forward_usage) / left_lanes : 0;
        const double q = reverse_usage ? static_cast<double>(reverse_usage) / right_lanes : 0;
        largest_ratio = std::max({largest_ratio, a, b});
        sum_ratio += a + b;
        largest_pressure = std::max({largest_pressure, p, q});
        sum_pressure += p + q;
        if (layer.first == domain_id) proposed = {a, b, left};
        else if (layer.second == domain_id) proposed = {b, a, target.banks - left};
      }
      if (!feasible) continue;
      const Objective candidate{largest_ratio, sum_ratio, largest_pressure, sum_pressure, left};
      if (candidate < best) { best = candidate; answer = proposed; }
    }
    return answer;
  }

  bool capacity_allows(int arc_index, long long addition) const {
    const Arc& arc = input_.arcs[arc_index];
    const Domain& domain = domains_.at(arc.domain);
    if (addition > domain.capacity - domain.usage) return false;
    if (domain.sll) return sll_allows(domain, addition);
    if (domain.banks) return bank_ratios(arc.domain, addition).first > 0;
    return ratio(domain.usage + addition, domain.lanes) <= maximum_ratio(domain);
  }

  bool sll_allows(const Domain& domain, long long addition) const {
    long long used = 0;
    for (const int other : sll_layers_.at({domain.sll_link, domain.sll_orientation})) {
      const long long usage = domains_.at(other).usage;
      if (usage > domain.capacity - used) return false;
      used += usage;
    }
    return addition <= domain.capacity - used;
  }

  bool direction_allows(int arc_index) const {
    const Arc& arc = input_.arcs[arc_index];
    if (arc.direction_group < 0) return true;
    const int locked = locks_.at(arc.direction_group);
    return locked < 0 || ((arc.from < arc.to) ==
        (input_.arcs[locked].from < input_.arcs[locked].to));
  }

  double arc_cost(int arc_index, bool shared, long long width) const {
    const Arc& arc = input_.arcs[arc_index];
    const Domain& domain = domains_.at(arc.domain);
    const long long addition = shared ? 0 : width;
    double delay = arc.delay;
    if (!domain.sll && policy_ == CostPolicy::kAuthorPressure) {
      // Eq. (2) increases price inside a quantization bucket. Looking only at
      // the next legal integer ratio creates a plateau that can overload one
      // route before another route becomes cheaper (observed on EDA2023 case3).
      // Insertion feasibility remains projected/quantized; optimization uses
      // current pre-insertion load, as in the author's original maze kernel.
      const long long lanes = domain.banks ?
          static_cast<long long>(bank_ratios(arc.domain, addition).first_banks) * domain.wires : domain.lanes;
      if (lanes <= 0) throw std::runtime_error("no directional bank for priced arc");
      const double pressure = static_cast<double>(domain.usage) / lanes;
      delay += arc.beta * (input_.minimum - 1 + pressure);
    } else if (!domain.sll) {
      const long long estimated_ratio = domain.banks ? bank_ratios(arc.domain, addition).first :
          ratio(domain.usage + addition, domain.lanes);
      delay += arc.beta * (estimated_ratio - 1);
    }
    const double cost = (shared && !tiny_) ? 0.5 * delay : delay;
    if (!std::isfinite(cost) || cost < 0) throw std::runtime_error("nonfinite or negative arc cost");
    return cost;
  }

  void route_sink(int demand_index, int sink) {
    const Demand& demand = input_.demands[demand_index];
    Tree& tree = trees_[demand_index];
    const int hop_limit = input_.max_hops ? std::min(input_.max_hops, input_.nodes - 1) : input_.nodes - 1;
    if (hop_limit <= 0) throw std::runtime_error("no legal hops for nonlocal sink");
    struct Label { double cost; int node, hops, previous, arc; };
    std::vector<Label> labels;
    // Copy a popped label before appending; the author's vector reference could
    // become dangling once its initial reserve was exceeded.
    labels.push_back({0.0, demand.source, 0, -1, -1});
    using QueueEntry = std::pair<double, int>;
    std::priority_queue<QueueEntry, std::vector<QueueEntry>, std::greater<QueueEntry>> queue;
    queue.emplace(0.0, 0);
    std::map<std::pair<int, int>, double> best;
    // With no explicit hop constraint the author's one label per vertex is
    // sufficient. Expanding every hop count would needlessly multiply work
    // on large unconstrained boards. Bounded search retains separate labels.
    const auto state_key = [&](int node, int hops) {
      return std::make_pair(node, input_.max_hops ? hops : 0);
    };
    best[state_key(demand.source, 0)] = 0.0;
    int final_label = -1;
    while (!queue.empty()) {
      const auto entry = queue.top(); queue.pop();
      const Label now = labels[entry.second];
      if (now.cost != best.at(state_key(now.node, now.hops))) continue;
      ++expansions_;
      if (now.node == sink) { final_label = entry.second; break; }
      if (now.hops >= hop_limit) continue;
      for (const int arc_index : adjacency_[now.node]) {
        const Arc& arc = input_.arcs[arc_index];
        if (arc.to == demand.source) continue;
        const bool shared = tree.parent_arc[arc.to] == arc_index;
        if (tree.parent_arc[arc.to] >= 0 && !shared) continue;
        if (!direction_allows(arc_index) || !capacity_allows(arc_index, shared ? 0 : demand.width)) continue;
        const double cost = now.cost + arc_cost(arc_index, shared, demand.width);
        if (!std::isfinite(cost)) continue;
        const auto key = state_key(arc.to, now.hops + 1);
        const auto existing = best.find(key);
        if (existing != best.end() && !(cost < existing->second)) continue;
        best[key] = cost;
        labels.push_back({cost, arc.to, now.hops + 1, entry.second, arc_index});
        queue.emplace(cost, static_cast<int>(labels.size()) - 1);
      }
    }
    if (final_label < 0)
      throw std::runtime_error("no legal author-maze route for demand " + std::to_string(demand_index) +
                               " sink " + std::to_string(sink) + " (capacity, direction, or hop bound)");
    std::vector<int> path;
    std::set<int> visited;
    for (int label = final_label; labels[label].previous >= 0; label = labels[label].previous) {
      if (!visited.insert(labels[label].node).second) throw std::runtime_error("cycle in proposed route");
      path.push_back(labels[label].arc);
    }
    std::reverse(path.begin(), path.end());
    for (const int arc_index : path) {
      const Arc& arc = input_.arcs[arc_index];
      if (tree.parent_arc[arc.to] == arc_index) continue;
      if (!direction_allows(arc_index) || !capacity_allows(arc_index, demand.width))
        throw std::runtime_error("proposed path jointly exceeds a shared resource");
      tree.parent_arc[arc.to] = arc_index;
      domains_.at(arc.domain).usage += demand.width;
      if (arc.direction_group >= 0 && locks_.at(arc.direction_group) < 0)
        locks_.at(arc.direction_group) = arc_index;
    }
  }

  void validate_final() const {
    std::map<int, long long> recomputed;
    for (size_t index = 0; index < trees_.size(); ++index) {
      const Demand& demand = input_.demands[index];
      const Tree& tree = trees_[index];
      std::set<int> used_arcs;
      for (const int sink : demand.sinks) {
        int node = sink, hops = 0;
        std::set<int> visited;
        while (node != demand.source) {
          if (!visited.insert(node).second || tree.parent_arc[node] < 0)
            throw std::runtime_error("invalid final tree ancestry");
          const int arc_index = tree.parent_arc[node];
          used_arcs.insert(arc_index);
          node = input_.arcs[arc_index].from;
          if (++hops >= input_.nodes || (input_.max_hops && hops > input_.max_hops))
            throw std::runtime_error("invalid final tree hop count");
        }
      }
      size_t selected = 0;
      for (const int arc_index : tree.parent_arc) if (arc_index >= 0) ++selected;
      if (selected != used_arcs.size()) throw std::runtime_error("unused final tree branch");
      for (const int arc_index : used_arcs) {
        if (!direction_allows(arc_index)) throw std::runtime_error("final direction lock violation");
        recomputed[input_.arcs[arc_index].domain] += demand.width;
      }
    }
    for (const auto& item : domains_) {
      if (recomputed[item.first] != item.second.usage || item.second.usage > item.second.capacity)
        throw std::runtime_error("final capacity accounting mismatch");
      if (item.second.banks && !bank_ratios(item.first, 0).first)
        throw std::runtime_error("final shared-bank capacity violation");
      if (item.second.sll && !sll_allows(item.second, 0))
        throw std::runtime_error("final physical SLL capacity violation");
    }
  }

  Input input_;
  std::vector<std::vector<int>> adjacency_;
  std::map<int, Domain> domains_;
  std::map<int, std::vector<std::pair<int, int>>> bank_layers_;
  std::map<std::pair<int, int>, std::vector<int>> sll_layers_;
  std::vector<Tree> trees_;
  std::map<int, int> locks_, group_link_;
  CostPolicy policy_;
  bool tiny_ = true;
  size_t connections_ = 0, routed_connections_ = 0, expansions_ = 0;
};

}  // namespace

int main(int argc, char** argv) {
  const auto usage = [](std::ostream& stream) {
    stream << "usage: emuflow_synergistic_author_router INPUT OUTPUT "
              "[--cost-policy author-pressure-v1|quantized-ratio-v1]\n"
              "default: author-pressure-v1 (continuous pre-insertion pressure); "
              "quantized-ratio-v1 preserves the first adapter for diagnostics.\n";
  };
  if (argc == 2 && std::string(argv[1]) == "--help") {
    usage(std::cout);
    return 0;
  }
  CostPolicy policy = CostPolicy::kAuthorPressure;
  if (argc == 5 && std::string(argv[3]) == "--cost-policy") {
    const std::string name = argv[4];
    if (name == "quantized-ratio-v1") policy = CostPolicy::kQuantizedRatio;
    else if (name != "author-pressure-v1") {
      std::cerr << "unsupported cost policy: " << name << '\n';
      usage(std::cerr);
      return 2;
    }
  } else if (argc != 3) {
    usage(std::cerr);
    return 2;
  }
  if (std::string(argv[1]) == argv[2]) {
    std::cerr << "input and output paths must differ\n";
    return 2;
  }
  try {
    // Outputs belong to this invocation; a failed invocation cannot leave a
    // previous result looking newly successful.
    std::remove(argv[2]);
    Router router(read_input(argv[1]), policy);
    router.run();
    router.write_output(argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::remove((std::string(argv[2]) + ".tmp").c_str());
    std::cerr << "synergistic author router: " << error.what() << '\n';
    return 1;
  }
}
