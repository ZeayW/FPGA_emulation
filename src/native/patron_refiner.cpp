#include "bank_allocation.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
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
constexpr std::uint64_t kTypedOutputMagic = 0x3154554f54415045ULL;
constexpr std::uint32_t kTypedOutputVersion = 1;

struct Domain {
  int lanes = 0;
  long long capacity_bits = 0;
  bool is_sll = false;
  double ratio_delay_slope_ns = 0.0;
  int opposite = -1;
  int banks = 0;
  int wires = 0;
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
};

void require(bool condition, const char* message);

class PackedIndexRange {
 public:
  using const_iterator = const int*;
  using const_reverse_iterator = std::reverse_iterator<const_iterator>;

  PackedIndexRange() = default;
  PackedIndexRange(const std::vector<int>& values,
                   std::size_t begin,
                   std::size_t end) {
    set_offsets(begin, end);
    bind(values);
  }

  void set_offsets(std::size_t begin, std::size_t end) {
    require(begin <= end &&
                end <= static_cast<std::size_t>(
                           std::numeric_limits<std::uint32_t>::max()),
            "PATRON packed member range is too large");
    begin_ = static_cast<std::uint32_t>(begin);
    end_ = static_cast<std::uint32_t>(end);
  }

  void bind(const std::vector<int>& values) {
    require(end_ <= values.size(), "invalid PATRON packed member range");
    values_ = values.empty() ? nullptr : values.data();
  }

  const_iterator begin() const {
    return values_ == nullptr ? nullptr : values_ + begin_;
  }
  const_iterator end() const {
    return values_ == nullptr ? nullptr : values_ + end_;
  }
  const_reverse_iterator rbegin() const {
    return const_reverse_iterator(end());
  }
  const_reverse_iterator rend() const {
    return const_reverse_iterator(begin());
  }
  std::size_t size() const { return end_ - begin_; }
  bool empty() const { return begin_ == end_; }
  int front() const {
    require(!empty() && values_ != nullptr,
            "empty PATRON packed member range");
    return values_[begin_];
  }

 private:
  const int* values_ = nullptr;
  std::uint32_t begin_ = 0;
  std::uint32_t end_ = 0;
};

class PackedAdjacency {
 public:
  PackedAdjacency() = default;
  PackedAdjacency(std::vector<std::uint32_t> offsets,
                  std::vector<int> values)
      : offsets_(std::move(offsets)), values_(std::move(values)) {
    require(!offsets_.empty() && offsets_.front() == 0 &&
                offsets_.back() == values_.size(),
            "invalid PATRON packed adjacency");
  }

  PackedIndexRange operator[](int row) const {
    require(row >= 0 &&
                static_cast<std::size_t>(row + 1) < offsets_.size(),
            "invalid PATRON adjacency lookup");
    return PackedIndexRange(
        values_, offsets_[row], offsets_[static_cast<std::size_t>(row) + 1]);
  }

 private:
  std::vector<std::uint32_t> offsets_;
  std::vector<int> values_;
};

template <typename Key>
int ordered_path(const std::pair<Key, int>& record) {
  return record.second;
}

template <typename First, typename Second>
int ordered_path(const std::tuple<First, Second, int>& record) {
  return std::get<2>(record);
}

template <typename Record>
class SparsePathOrder {
 public:
  void append(const Record& record) {
    require(dirty_.empty(), "PATRON path order is already finalized");
    base_.push_back(record);
  }

  void finalize(int paths) {
    require(paths > 0 && base_.size() == static_cast<std::size_t>(paths) &&
                dirty_.empty(),
            "invalid PATRON path order population");
    std::sort(base_.begin(), base_.end());
    dirty_.assign(paths, 0);
    for (const Record& record : base_) {
      const int path = ordered_path(record);
      require(path >= 0 && path < paths && !dirty_[path],
              "invalid PATRON ordered path identity");
      dirty_[path] = 1;
    }
    std::fill(dirty_.begin(), dirty_.end(), 0);
  }

  void update(const Record& old_record, const Record& new_record) {
    const int path = ordered_path(old_record);
    require(path >= 0 && path < static_cast<int>(dirty_.size()) &&
                ordered_path(new_record) == path,
            "invalid PATRON path-order update");
    if (dirty_[path]) {
      require(changed_.erase(old_record) == 1,
              "missing PATRON sparse path-order record");
    } else {
      dirty_[path] = 1;
    }
    require(changed_.insert(new_record).second,
            "duplicate PATRON sparse path-order record");
  }

  template <typename Callback>
  void for_each_ascending(Callback callback) const {
    std::size_t base_index = 0;
    const auto skip_dirty_base = [&]() {
      while (base_index < base_.size() &&
             dirty_[ordered_path(base_[base_index])]) {
        ++base_index;
      }
    };
    skip_dirty_base();
    auto changed = changed_.begin();
    while (base_index < base_.size() || changed != changed_.end()) {
      const bool use_changed =
          base_index == base_.size() ||
          (changed != changed_.end() && *changed < base_[base_index]);
      const Record& record = use_changed ? *changed : base_[base_index];
      if (!callback(record)) return;
      if (use_changed) {
        ++changed;
      } else {
        ++base_index;
        skip_dirty_base();
      }
    }
  }

  template <typename Callback>
  void for_each_descending(Callback callback) const {
    std::size_t base_index = base_.size();
    const auto skip_dirty_base = [&]() {
      while (base_index > 0 &&
             dirty_[ordered_path(base_[base_index - 1])]) {
        --base_index;
      }
    };
    skip_dirty_base();
    auto changed = changed_.rbegin();
    while (base_index > 0 || changed != changed_.rend()) {
      const bool use_changed =
          base_index == 0 ||
          (changed != changed_.rend() && base_[base_index - 1] < *changed);
      const Record& record =
          use_changed ? *changed : base_[base_index - 1];
      if (!callback(record)) return;
      if (use_changed) {
        ++changed;
      } else {
        --base_index;
        skip_dirty_base();
      }
    }
  }

  Record minimum() const {
    Record result{};
    bool found = false;
    for_each_ascending([&](const Record& record) {
      result = record;
      found = true;
      return false;
    });
    require(found, "empty PATRON path order");
    return result;
  }

  Record maximum() const {
    Record result{};
    bool found = false;
    for_each_descending([&](const Record& record) {
      result = record;
      found = true;
      return false;
    });
    require(found, "empty PATRON path order");
    return result;
  }

  bool empty() const { return base_.empty(); }

 private:
  std::vector<Record> base_;
  std::vector<unsigned char> dirty_;
  std::set<Record> changed_;
};

class SparsePathMembership {
 public:
  void append(int path) {
    require(!finalized_ && path >= 0,
            "invalid PATRON domain/path population");
    base_.push_back(path);
  }

  void finalize() {
    require(!finalized_, "PATRON domain/path population is finalized");
    std::sort(base_.begin(), base_.end());
    base_.erase(std::unique(base_.begin(), base_.end()), base_.end());
    finalized_ = true;
  }

  void remove(int path) {
    require(finalized_ && path >= 0,
            "invalid PATRON domain/path removal");
    if (added_.erase(path) == 1) return;
    require(std::binary_search(base_.begin(), base_.end(), path) &&
                removed_.insert(path).second,
            "missing PATRON domain/path dependency");
  }

  void add(int path) {
    require(finalized_ && path >= 0,
            "invalid PATRON domain/path insertion");
    if (removed_.erase(path) == 1) return;
    require(!std::binary_search(base_.begin(), base_.end(), path) &&
                added_.insert(path).second,
            "duplicate PATRON domain/path dependency");
  }

  void append_to(std::vector<int>& output) const {
    require(finalized_, "PATRON domain/path population is not finalized");
    for (int path : base_) {
      if (!removed_.count(path)) output.push_back(path);
    }
    output.insert(output.end(), added_.begin(), added_.end());
  }

 private:
  bool finalized_ = false;
  std::vector<int> base_;
  std::set<int> added_;
  std::set<int> removed_;
};

struct Net {
  PackedIndexRange drivers;
  PackedIndexRange sinks;
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
  PackedIndexRange nets;
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
  int max_ratio = 0;
  int ratio_quantum = 1;
  int min_ratio = 1;
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
  std::vector<double> cluster_weights;
  std::vector<Domain> domain;
  std::vector<std::vector<Route>> route;
  std::vector<Net> net;
  std::vector<Path> path;
  std::vector<int> net_members;
  std::vector<int> path_nets;
};

double cluster_weight(const Model& model, int cluster, int dimension) {
  return model.cluster_weights[
      static_cast<std::size_t>(cluster) * model.dimensions + dimension];
}

void bind_packed_members(Model& model) {
  for (Net& net : model.net) {
    net.drivers.bind(model.net_members);
    net.sinks.bind(model.net_members);
  }
  for (Path& path : model.path) {
    path.nets.bind(model.path_nets);
  }
}

struct Evaluation {
  bool feasible = false;
  std::vector<double> objective;
  std::vector<long long> ranked;
  int capacity_violations = 0;
  long long capacity_excess_bits = 0;
};

class ProxyNetPayload {
 public:
  void add_transition(const Transition& transition) {
    require(!finished_ &&
                transitions_ < std::numeric_limits<std::uint32_t>::max(),
            "invalid PATRON proxy-net transition count");
    values_.push_back(transition.source);
    values_.push_back(transition.sink);
    values_.push_back(transition.sink_clusters);
    ++transitions_;
  }

  void finish(const std::map<int, int>& sink_counts,
              const std::map<int, int>& domain_counts) {
    require(!finished_ &&
                sink_counts.size() <=
                    std::numeric_limits<std::uint32_t>::max() &&
                domain_counts.size() <=
                    std::numeric_limits<std::uint32_t>::max(),
            "invalid PATRON proxy-net payload");
    sinks_ = static_cast<std::uint32_t>(sink_counts.size());
    domains_ = static_cast<std::uint32_t>(domain_counts.size());
    for (const auto& item : sink_counts) {
      values_.push_back(item.first);
      values_.push_back(item.second);
    }
    for (const auto& item : domain_counts) {
      values_.push_back(item.first);
      values_.push_back(item.second);
    }
    finished_ = true;
  }

  template <typename Callback>
  void for_each_transition(Callback callback) const {
    require(finished_, "PATRON proxy-net payload is not finalized");
    for (std::uint32_t index = 0; index < transitions_; ++index) {
      const std::size_t offset = static_cast<std::size_t>(index) * 3;
      callback(Transition{
          values_[offset], values_[offset + 1], values_[offset + 2]});
    }
  }

  template <typename Callback>
  void for_each_domain(Callback callback) const {
    require(finished_, "PATRON proxy-net payload is not finalized");
    const std::size_t begin =
        static_cast<std::size_t>(transitions_) * 3 +
        static_cast<std::size_t>(sinks_) * 2;
    for (std::uint32_t index = 0; index < domains_; ++index) {
      const std::size_t offset = begin + static_cast<std::size_t>(index) * 2;
      callback(values_[offset], values_[offset + 1]);
    }
  }

  int sink_count(int part) const {
    return lookup(part, static_cast<std::size_t>(transitions_) * 3, sinks_);
  }

  int domain_count(int domain) const {
    const std::size_t begin =
        static_cast<std::size_t>(transitions_) * 3 +
        static_cast<std::size_t>(sinks_) * 2;
    return lookup(domain, begin, domains_);
  }

 private:
  int lookup(int key, std::size_t begin, std::uint32_t count) const {
    require(finished_, "PATRON proxy-net payload is not finalized");
    std::size_t low = 0;
    std::size_t high = count;
    while (low < high) {
      const std::size_t middle = low + (high - low) / 2;
      const int candidate = values_[begin + middle * 2];
      if (candidate < key) {
        low = middle + 1;
      } else {
        high = middle;
      }
    }
    return low < count && values_[begin + low * 2] == key
               ? values_[begin + low * 2 + 1]
               : 0;
  }

  std::vector<int> values_;
  std::uint32_t transitions_ = 0;
  std::uint32_t sinks_ = 0;
  std::uint32_t domains_ = 0;
  bool finished_ = false;
};

struct ProxyNetState {
  bool feasible = true;
  double delay_ns = 0.0;
  std::pair<int, int> worst_transition = {-1, -1};
  ProxyNetPayload payload;
  long long hops = 0;
  long long cuts = 0;
};

class SmallDomainSet {
 public:
  SmallDomainSet() = default;
  SmallDomainSet(std::vector<int> values) { assign(std::move(values)); }

  SmallDomainSet(const SmallDomainSet& other) : mask_(other.mask_) {
    if (other.overflow_ != nullptr) {
      overflow_ = std::make_unique<std::vector<int>>(*other.overflow_);
    }
  }
  SmallDomainSet& operator=(const SmallDomainSet& other) {
    if (this == &other) return *this;
    mask_ = other.mask_;
    overflow_.reset();
    if (other.overflow_ != nullptr) {
      overflow_ = std::make_unique<std::vector<int>>(*other.overflow_);
    }
    return *this;
  }
  SmallDomainSet(SmallDomainSet&&) noexcept = default;
  SmallDomainSet& operator=(SmallDomainSet&&) noexcept = default;

  template <typename Callback>
  void for_each(Callback callback) const {
    if (overflow_ != nullptr) {
      for (int domain : *overflow_) callback(domain);
      return;
    }
    std::uint64_t remaining = mask_;
    while (remaining != 0) {
      const int domain = __builtin_ctzll(remaining);
      callback(domain);
      remaining &= remaining - 1;
    }
  }

  void append_to(std::set<int>& output) const {
    for_each([&](int domain) { output.insert(domain); });
  }

 private:
  void assign(std::vector<int> values) {
    require(std::is_sorted(values.begin(), values.end()) &&
                std::adjacent_find(values.begin(), values.end()) ==
                    values.end() &&
                (values.empty() || values.front() >= 0),
            "invalid PATRON path domain set");
    if (values.empty() || values.back() < 64) {
      for (int domain : values) mask_ |= std::uint64_t{1} << domain;
    } else {
      overflow_ = std::make_unique<std::vector<int>>(std::move(values));
    }
  }

  std::uint64_t mask_ = 0;
  std::unique_ptr<std::vector<int>> overflow_;
};

struct ProxyPathState {
  double normalized_slack = 0.0;
  bool negative = false;
  long long snaking = 0;
  long long transitions = 0;
  long long hops = 0;
  long long ratio_sum = 0;
  SmallDomainSet dependency_domains;
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
  std::vector<SparsePathMembership> domain_paths;
  SparsePathOrder<std::pair<double, int>> slack_path_order;
  SparsePathOrder<std::pair<long long, int>> hop_path_order;
  SparsePathOrder<std::pair<long long, int>> ratio_sum_path_order;
  SparsePathOrder<std::tuple<long long, long long, int>>
      objective_path_order;
  double total_negative = 0.0;
  long long negative_paths = 0;
  long long snaking = 0;
  long long transitions = 0;
  bool transition_objective = false;
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

void require(bool condition, const char* message);

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

void require(bool condition, const char* message) {
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
  int raw = std::max(model.min_ratio, (load + domain.lanes - 1) / domain.lanes);
  if (raw == 1) {
    return 1;
  }
  raw = ((raw + model.ratio_quantum - 1) / model.ratio_quantum)
        * model.ratio_quantum;
  return model.min_ratio > 1 ? raw : std::min(model.max_ratio, raw);
}

std::vector<int> paired_domain_ratios(const Model& model, const std::vector<int>& loads) {
  std::vector<int> result(model.domains, 1);
  for (int i = 0; i < model.domains; ++i)
    result[i] = tdm_ratio(model, loads[i], model.domain[i]);
  for (int i = 0; i < model.domains; ++i) {
    const Domain& d = model.domain[i];
    if (d.banks == 0 || i >= d.opposite) continue;
    const auto ratios = shared_bank_ratios(loads[i], loads[d.opposite],
        d.banks, d.wires, model.ratio_quantum, model.min_ratio, model.max_ratio + 1);
    result[i] = ratios.first; result[d.opposite] = ratios.second;
  }
  return result;
}

std::pair<int, long long> transport_capacity_pressure(
    const Model& model, const std::vector<int>& loads) {
  require(loads.size() == model.domain.size(),
          "transport pressure domain coverage differs");
  int violations = 0;
  long long excess_bits = 0;
  for (int i = 0; i < model.domains; ++i) {
    const Domain& domain = model.domain[i];
    const long long capacity = domain.capacity_bits > 0
        ? domain.capacity_bits
        : static_cast<long long>(domain.lanes) * model.max_ratio;
    if (loads[i] > capacity) {
      ++violations;
      excess_bits += static_cast<long long>(loads[i]) - capacity;
    }
    if (domain.banks == 0 || i >= domain.opposite) {
      continue;
    }
    const long long shared_capacity =
        static_cast<long long>(domain.banks) * domain.wires
        * model.max_ratio;
    const long long shared_load =
        static_cast<long long>(loads[i]) + loads[domain.opposite];
    if (shared_load > shared_capacity) {
      ++violations;
      excess_bits += shared_load - shared_capacity;
    }
  }
  return {violations, excess_bits};
}

double predicted_route_delay(const Model& model,
                             const Route& route,
                             const std::vector<int>& ratios) {
  double delay = 0.0;
  for (const Arc& arc : route.arcs) {
    delay += arc.delay_ns
             + std::max(0, ratios[arc.domain] - 1)
                   * model.domain[arc.domain].ratio_delay_slope_ns
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

Model read_text_model(const std::string& path) {
  std::ifstream input_file;
  std::istream* input = &std::cin;
  if (path != "-") {
    input_file.open(path);
    require(input_file.good(), "cannot open input");
    input = &input_file;
  }
  std::istream& stream = *input;
  std::string token;
  stream >> token;
  const std::string bank_suffix = "_BANKS_V1";
  const bool bank_input = token.size() > bank_suffix.size() &&
      token.compare(token.size() - bank_suffix.size(), bank_suffix.size(), bank_suffix) == 0;
  if (bank_input) token.resize(token.size() - bank_suffix.size());
  const bool flow_v7 = token == "EMUFLOW_PATRON_INPUT_V7";
  const bool flow_v8 = token == "EMUFLOW_PATRON_INPUT_V8";
  const bool flow_v9 = token == "EMUFLOW_PATRON_INPUT_V9";
  const bool flow_v10 = token == "EMUFLOW_PATRON_INPUT_V10";
  const bool flow_v11 = token == "EMUFLOW_PATRON_INPUT_V11";
  const bool flow_v14 = token == "EMUFLOW_PATRON_INPUT_V14";
  const bool flow_input = flow_v7 || flow_v8 || flow_v9 || flow_v10
                          || flow_v11 || flow_v14;
  require(token == "EMUFLOW_PATRON_INPUT_V6" || flow_input,
          "invalid input header");
  Model model;
  stream >> token;
  require(token == "PARAM", "missing PARAM");
  stream >> model.parts >> model.clusters >> model.dimensions >> model.domains
      >> model.nets >> model.paths >> model.max_hops >> model.max_ratio
      >> model.ratio_quantum >> model.min_used_parts >> model.max_moves
      >> model.positive_scale >> model.negative_scale >> model.max_period
      >> model.boundary_fanout_penalty_scale_ns >> model.max_sweeps
      >> model.ejection_critical_limit >> model.ejection_donor_limit
      >> model.max_ejections;
  if (bank_input) stream >> model.min_ratio;
  require(stream.good() && model.parts > 0 && model.clusters > 0
              && model.dimensions > 0 && model.domains > 0
              && model.nets > 0 && model.paths > 0
              && model.max_ratio > 0 && model.ratio_quantum > 0
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
    if (flow_v8 || flow_v9 || flow_v10 || flow_v11 || flow_v14) {
      stream >> model.flow_max_frontier_paths
          >> model.flow_max_tail_moves;
    }
    if (flow_v10 || flow_v11 || flow_v14) {
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
                && (!(flow_v8 || flow_v9 || flow_v10 || flow_v11
                      || flow_v14)
                    || (model.flow_max_frontier_paths > 0
                        && model.flow_max_tail_moves >= 0)),
            "invalid FLOW");
    require(std::isfinite(model.physical_hop_guard_scale_ns)
                && model.physical_hop_guard_scale_ns >= 0.0,
            "invalid physical hop guard");
    model.flow_refinement = true;
    model.flow_version = flow_v14
                             ? 14
                             : (flow_v11
                                    ? 11
                                    : (flow_v10
                                           ? 10
                                           : (flow_v9 ? 9
                                                      : (flow_v8 ? 8 : 7))));
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
  model.cluster_weights.resize(
      static_cast<std::size_t>(model.clusters) * model.dimensions);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    stream >> token;
    require(token == "CLUSTER", "missing CLUSTER");
    int index = -1;
    stream >> index >> model.cluster[cluster].part
        >> model.cluster[cluster].fixed;
    require(index == cluster, "non-canonical CLUSTER index");
    for (int dimension = 0; dimension < model.dimensions; ++dimension) {
      stream >> model.cluster_weights[
          static_cast<std::size_t>(cluster) * model.dimensions + dimension];
    }
  }

  model.domain.resize(model.domains);
  for (int domain = 0; domain < model.domains; ++domain) {
    stream >> token;
    require(token == "DOMAIN", "missing DOMAIN");
    int index = -1;
    int is_sll = 0;
    stream >> index >> model.domain[domain].lanes >> is_sll
        >> model.domain[domain].ratio_delay_slope_ns;
    if (bank_input) stream >> model.domain[domain].opposite
        >> model.domain[domain].banks >> model.domain[domain].wires
        >> model.domain[domain].capacity_bits;
    require(index == domain && model.domain[domain].lanes > 0
                && (!bank_input || model.domain[domain].capacity_bits > 0),
            "invalid DOMAIN");
    model.domain[domain].is_sll = is_sll != 0;
    const auto& d = model.domain[domain];
    require(std::isfinite(d.ratio_delay_slope_ns) && d.ratio_delay_slope_ns >= 0.0
                && d.banks >= 0 && d.wires >= 0
                && ((d.banks == 0) == (d.wires == 0)), "invalid DOMAIN delay/banks");
  }
  for (int i = 0; i < model.domains; ++i) {
    const auto& d = model.domain[i];
    if (!d.banks) continue;
    require(d.opposite >= 0 && d.opposite < model.domains && d.opposite != i,
            "invalid opposite bank domain");
    const auto& other = model.domain[d.opposite];
    require(other.opposite == i && other.banks == d.banks && other.wires == d.wires,
            "asymmetric bank domain metadata");
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
    const std::size_t driver_begin = model.net_members.size();
    for (int count = 0; count < drivers; ++count) {
      int cluster = -1;
      stream >> cluster;
      require(cluster >= 0 && cluster < model.clusters,
              "invalid NET driver");
      model.net_members.push_back(cluster);
    }
    model.net[net].drivers.set_offsets(
        driver_begin, model.net_members.size());
    int sinks = -1;
    stream >> sinks;
    require(sinks > 0, "invalid NET sinks");
    const std::size_t sink_begin = model.net_members.size();
    for (int count = 0; count < sinks; ++count) {
      int cluster = -1;
      stream >> cluster;
      require(cluster >= 0 && cluster < model.clusters,
              "invalid NET sink");
      model.net_members.push_back(cluster);
    }
    model.net[net].sinks.set_offsets(sink_begin, model.net_members.size());
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
    const std::size_t path_net_begin = model.path_nets.size();
    for (int count = 0; count < nets; ++count) {
      int net = -1;
      stream >> net;
      require(net >= 0 && net < model.nets, "invalid PATH net");
      model.path_nets.push_back(net);
    }
    model.path[path_index].nets.set_offsets(
        path_net_begin, model.path_nets.size());
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
  bind_packed_members(model);
  return model;
}

class TypedInputReader {
 public:
  explicit TypedInputReader(std::istream& stream) : stream_(stream) {}

  void read(void* destination, std::size_t size, const char* label) {
    read_raw(destination, size, label);
    emuflow::core::UpdateCrc32(
        checksum_, static_cast<const unsigned char*>(destination), size);
  }

  void read_raw(void* destination, std::size_t size, const char* label) {
    stream_.read(static_cast<char*>(destination),
                 static_cast<std::streamsize>(size));
    if (stream_.gcount() != static_cast<std::streamsize>(size)) {
      throw std::runtime_error(std::string("truncated PATRON typed input ") +
                               label);
    }
  }

  std::uint8_t u8(const char* label) {
    std::uint8_t value = 0;
    read(&value, sizeof(value), label);
    return value;
  }

  std::uint32_t u32(const char* label) {
    std::array<unsigned char, 4> bytes{};
    read(bytes.data(), bytes.size(), label);
    return static_cast<std::uint32_t>(bytes[0]) |
           (static_cast<std::uint32_t>(bytes[1]) << 8U) |
           (static_cast<std::uint32_t>(bytes[2]) << 16U) |
           (static_cast<std::uint32_t>(bytes[3]) << 24U);
  }

  std::uint32_t raw_u32(const char* label) {
    std::array<unsigned char, 4> bytes{};
    read_raw(bytes.data(), bytes.size(), label);
    return static_cast<std::uint32_t>(bytes[0]) |
           (static_cast<std::uint32_t>(bytes[1]) << 8U) |
           (static_cast<std::uint32_t>(bytes[2]) << 16U) |
           (static_cast<std::uint32_t>(bytes[3]) << 24U);
  }

  std::uint64_t u64(const char* label) {
    std::array<unsigned char, 8> bytes{};
    read(bytes.data(), bytes.size(), label);
    std::uint64_t value = 0;
    for (int index = 0; index < 8; ++index) {
      value |= static_cast<std::uint64_t>(bytes[index]) << (8U * index);
    }
    return value;
  }

  std::int32_t i32(const char* label) {
    const std::uint32_t bits = u32(label);
    std::int32_t value = 0;
    static_assert(sizeof(value) == sizeof(bits));
    std::memcpy(&value, &bits, sizeof(value));
    return value;
  }

  std::int64_t i64(const char* label) {
    const std::uint64_t bits = u64(label);
    std::int64_t value = 0;
    static_assert(sizeof(value) == sizeof(bits));
    std::memcpy(&value, &bits, sizeof(value));
    return value;
  }

  double finite_double(const char* label) {
    const std::uint64_t bits = u64(label);
    double value = 0.0;
    static_assert(sizeof(value) == sizeof(bits));
    std::memcpy(&value, &bits, sizeof(value));
    if (!std::isfinite(value)) {
      throw std::runtime_error(std::string("invalid PATRON typed input ") +
                               label);
    }
    return value;
  }

  void tag(unsigned char expected, const char* label) {
    if (u8(label) != expected) {
      throw std::runtime_error(std::string("invalid PATRON typed input ") +
                               label);
    }
  }

  std::uint32_t checksum() const { return checksum_; }
  std::istream& stream() { return stream_; }

 private:
  std::istream& stream_;
  std::uint32_t checksum_ = 0;
};

int typed_count(std::uint64_t value, const char* label, bool positive) {
  if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()) ||
      (positive && value == 0)) {
    throw std::runtime_error(std::string("invalid PATRON typed input ") +
                             label);
  }
  return static_cast<int>(value);
}

Model read_typed_model(const std::string& path) {
  std::ifstream input_file;
  std::istream* input = &std::cin;
  if (path != "-") {
    input_file.open(path, std::ios::binary);
    require(input_file.good(), "cannot open typed input");
    input = &input_file;
  }
  TypedInputReader reader(*input);
  std::array<unsigned char, 8> magic{};
  reader.read(magic.data(), magic.size(), "header");
  const std::array<unsigned char, 8> expected_magic = {
      'E', 'M', 'P', 'A', 'T', 'R', 'N', '1'};
  require(magic == expected_magic && reader.u32("version") == 1U &&
              reader.u32("endianness") == 0x01020304U,
          "invalid PATRON typed input header");
  const int flow_version =
      typed_count(reader.u32("flow version"), "flow version", true);
  require(flow_version == 6 || flow_version == 9 || flow_version == 10 ||
              flow_version == 11 || flow_version == 14,
          "invalid PATRON typed input flow version");
  const std::uint32_t flags = reader.u32("flags");
  const bool flow_refinement = flow_version != 6;
  const bool physical_feedback = flow_version == 11;
  const std::uint32_t expected_flags =
      1U | (flow_refinement ? 2U : 0U) |
      (physical_feedback ? 4U : 0U);
  require(flags == expected_flags, "invalid PATRON typed input flags");

  Model model;
  model.parts = typed_count(reader.u64("part count"), "part count", true);
  model.clusters =
      typed_count(reader.u64("cluster count"), "cluster count", true);
  model.dimensions =
      typed_count(reader.u64("dimension count"), "dimension count", true);
  model.domains =
      typed_count(reader.u64("domain count"), "domain count", true);
  model.nets = typed_count(reader.u64("net count"), "net count", true);
  model.paths = typed_count(reader.u64("path count"), "path count", true);
  model.max_hops = reader.i32("maximum hops");
  model.max_ratio = reader.i32("maximum TDM ratio");
  model.ratio_quantum = reader.i32("ratio quantum");
  model.min_used_parts = reader.i32("minimum used parts");
  model.max_moves = reader.i32("maximum moves");
  model.max_sweeps = reader.i32("maximum sweeps");
  model.ejection_critical_limit = reader.i32("ejection critical limit");
  model.ejection_donor_limit = reader.i32("ejection donor limit");
  model.max_ejections = reader.i32("maximum ejections");
  model.min_ratio = reader.i32("minimum ratio");
  model.positive_scale = reader.finite_double("positive scale");
  model.negative_scale = reader.finite_double("negative scale");
  model.max_period = reader.finite_double("maximum period");
  model.boundary_fanout_penalty_scale_ns =
      reader.finite_double("boundary fanout penalty");
  model.physical_hop_guard_scale_ns =
      reader.finite_double("physical hop guard");
  model.flow_max_clusters = reader.i32("flow maximum clusters");
  model.flow_corridor_distance = reader.i32("flow corridor distance");
  model.flow_piercing_strategy = reader.i32("flow piercing strategy");
  model.flow_max_legal_candidates =
      reader.i32("flow maximum legal candidates");
  model.flow_max_polish_moves = reader.i32("flow maximum polish moves");
  model.flow_max_frontier_paths =
      reader.i32("flow maximum frontier paths");
  model.flow_max_tail_moves = reader.i32("flow maximum tail moves");
  require(model.max_hops >= -1 && model.max_ratio > 0 &&
              model.ratio_quantum > 0 && model.min_used_parts > 0 &&
              model.min_used_parts <= model.parts && model.max_moves >= 0 &&
              model.max_sweeps > 0 && model.ejection_critical_limit >= 0 &&
              model.ejection_donor_limit >= 0 && model.max_ejections >= 0 &&
              model.min_ratio > 0 && model.positive_scale > 0.0 &&
              model.negative_scale > 0.0 && model.max_period > 0.0 &&
              model.boundary_fanout_penalty_scale_ns >= 0.0 &&
              model.physical_hop_guard_scale_ns >= 0.0,
          "invalid PATRON typed input parameters");
  if (flow_refinement) {
    require(model.flow_max_clusters > 0 &&
                model.flow_max_clusters <= model.clusters &&
                model.flow_corridor_distance >= 0 &&
                model.flow_corridor_distance <= model.clusters &&
                model.flow_piercing_strategy >= 0 &&
                model.flow_piercing_strategy <= 3 &&
                model.flow_max_legal_candidates > 0 &&
                model.flow_max_polish_moves >= 0 &&
                model.flow_max_frontier_paths > 0 &&
                model.flow_max_tail_moves >= 0,
            "invalid PATRON typed input flow configuration");
    model.flow_refinement = true;
    model.flow_version = flow_version;
  } else {
    require(model.flow_max_clusters == 0 &&
                model.flow_corridor_distance == 0 &&
                model.flow_piercing_strategy == 0 &&
                model.flow_max_legal_candidates == 0 &&
                model.flow_max_polish_moves == 0 &&
                model.flow_max_frontier_paths == 0 &&
                model.flow_max_tail_moves == 0 &&
                model.physical_hop_guard_scale_ns == 0.0,
            "invalid PATRON typed input reference configuration");
  }

  model.hard_capacity.assign(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  model.balance_capacity.assign(
      model.parts, std::vector<double>(model.dimensions, 0.0));
  for (int part = 0; part < model.parts; ++part) {
    reader.tag('C', "capacity tag");
    require(reader.u32("capacity index") == static_cast<std::uint32_t>(part),
            "non-canonical PATRON typed capacity index");
    for (int dim = 0; dim < model.dimensions; ++dim) {
      model.hard_capacity[part][dim] =
          reader.finite_double("hard capacity");
      model.balance_capacity[part][dim] =
          reader.finite_double("balance capacity");
      require(model.hard_capacity[part][dim] >= 0.0 &&
                  model.balance_capacity[part][dim] >= 0.0,
              "invalid PATRON typed capacity");
    }
  }

  model.cluster.resize(model.clusters);
  model.cluster_weights.resize(
      static_cast<std::size_t>(model.clusters) * model.dimensions);
  for (int cluster = 0; cluster < model.clusters; ++cluster) {
    reader.tag('L', "cluster tag");
    require(reader.u32("cluster index") ==
                static_cast<std::uint32_t>(cluster),
            "non-canonical PATRON typed cluster index");
    model.cluster[cluster].part = reader.i32("cluster part");
    model.cluster[cluster].fixed = reader.i32("cluster fixed part");
    require(model.cluster[cluster].part >= 0 &&
                model.cluster[cluster].part < model.parts &&
                model.cluster[cluster].fixed >= -1 &&
                model.cluster[cluster].fixed < model.parts,
            "invalid PATRON typed cluster ownership");
    for (int dimension = 0; dimension < model.dimensions; ++dimension) {
      double& weight = model.cluster_weights[
          static_cast<std::size_t>(cluster) * model.dimensions + dimension];
      weight = reader.finite_double("cluster weight");
      require(weight >= 0.0, "invalid PATRON typed cluster weight");
    }
  }

  model.domain.resize(model.domains);
  for (int domain = 0; domain < model.domains; ++domain) {
    reader.tag('D', "domain tag");
    require(reader.u32("domain index") ==
                static_cast<std::uint32_t>(domain),
            "non-canonical PATRON typed domain index");
    model.domain[domain].lanes = reader.i32("domain lanes");
    const std::uint8_t is_sll = reader.u8("domain SLL flag");
    model.domain[domain].ratio_delay_slope_ns =
        reader.finite_double("domain delay slope");
    model.domain[domain].opposite = reader.i32("opposite domain");
    model.domain[domain].banks = reader.i32("domain bank count");
    model.domain[domain].wires = reader.i32("domain wire count");
    model.domain[domain].capacity_bits = reader.i64("domain capacity bits");
    require(model.domain[domain].lanes > 0 && is_sll <= 1 &&
                model.domain[domain].ratio_delay_slope_ns >= 0.0 &&
                model.domain[domain].banks >= 0 &&
                model.domain[domain].wires >= 0 &&
                ((model.domain[domain].banks == 0) ==
                 (model.domain[domain].wires == 0)) &&
                model.domain[domain].capacity_bits > 0,
            "invalid PATRON typed domain");
    model.domain[domain].is_sll = is_sll != 0;
  }
  for (int domain = 0; domain < model.domains; ++domain) {
    const auto& current = model.domain[domain];
    if (!current.banks) continue;
    require(current.opposite >= 0 && current.opposite < model.domains &&
                current.opposite != domain,
            "invalid PATRON typed opposite bank domain");
    const auto& other = model.domain[current.opposite];
    require(other.opposite == domain && other.banks == current.banks &&
                other.wires == current.wires,
            "asymmetric PATRON typed bank metadata");
  }

  model.route.assign(model.parts, std::vector<Route>(model.parts));
  for (int source = 0; source < model.parts; ++source) {
    for (int sink = 0; sink < model.parts; ++sink) {
      reader.tag('R', "route tag");
      require(reader.u32("route source") ==
                  static_cast<std::uint32_t>(source) &&
                  reader.u32("route sink") ==
                      static_cast<std::uint32_t>(sink),
              "non-canonical PATRON typed route identity");
      const int arcs = reader.i32("route arc count");
      require(arcs >= -1, "invalid PATRON typed route arc count");
      Route& route = model.route[source][sink];
      route.reachable = arcs >= 0;
      if (arcs > 0) route.arcs.reserve(arcs);
      for (int index = 0; index < arcs; ++index) {
        Arc arc;
        arc.domain = typed_count(reader.u32("route arc domain"),
                                 "route arc domain", false);
        arc.delay_ns = reader.finite_double("route arc delay");
        require(arc.domain >= 0 && arc.domain < model.domains &&
                    arc.delay_ns >= 0.0,
                "invalid PATRON typed route arc");
        route.arcs.push_back(arc);
      }
    }
  }

  model.net.resize(model.nets);
  for (int net = 0; net < model.nets; ++net) {
    reader.tag('N', "net tag");
    require(reader.u32("net index") == static_cast<std::uint32_t>(net),
            "non-canonical PATRON typed net index");
    const int drivers =
        typed_count(reader.u64("net driver count"), "net driver count", true);
    const std::size_t driver_begin = model.net_members.size();
    for (int count = 0; count < drivers; ++count) {
      const int cluster = typed_count(reader.u32("net driver"),
                                      "net driver", false);
      require(cluster >= 0 && cluster < model.clusters,
              "invalid PATRON typed net driver");
      model.net_members.push_back(cluster);
    }
    model.net[net].drivers.set_offsets(
        driver_begin, model.net_members.size());
    const int sinks =
        typed_count(reader.u64("net sink count"), "net sink count", true);
    const std::size_t sink_begin = model.net_members.size();
    for (int count = 0; count < sinks; ++count) {
      const int cluster = typed_count(reader.u32("net sink"),
                                      "net sink", false);
      require(cluster >= 0 && cluster < model.clusters,
              "invalid PATRON typed net sink");
      model.net_members.push_back(cluster);
    }
    model.net[net].sinks.set_offsets(sink_begin, model.net_members.size());
  }

  model.path.resize(model.paths);
  for (int path_index = 0; path_index < model.paths; ++path_index) {
    reader.tag('P', "path tag");
    require(reader.u32("path index") ==
                static_cast<std::uint32_t>(path_index),
            "non-canonical PATRON typed path index");
    Path& path_record = model.path[path_index];
    path_record.period_ns = reader.finite_double("path period");
    path_record.slack_ns = reader.finite_double("path slack");
    path_record.start_cluster = reader.i32("path start cluster");
    path_record.end_cluster = reader.i32("path end cluster");
    const int path_nets =
        typed_count(reader.u64("path net count"), "path net count", false);
    require(path_record.period_ns > 0.0 &&
                ((path_record.start_cluster < 0 &&
                  path_record.end_cluster < 0) ||
                 (path_record.start_cluster >= 0 &&
                  path_record.start_cluster < model.clusters &&
                  path_record.end_cluster >= 0 &&
                  path_record.end_cluster < model.clusters)),
            "invalid PATRON typed path");
    const std::size_t path_net_begin = model.path_nets.size();
    for (int count = 0; count < path_nets; ++count) {
      const int net =
          typed_count(reader.u32("path net"), "path net", false);
      require(net >= 0 && net < model.nets,
              "invalid PATRON typed path net");
      model.path_nets.push_back(net);
    }
    path_record.nets.set_offsets(path_net_begin, model.path_nets.size());
    if (physical_feedback) {
      path_record.feedback_source = reader.i32("feedback source");
      path_record.feedback_sink = reader.i32("feedback sink");
      path_record.feedback_residual_ns =
          reader.finite_double("feedback residual");
      const bool no_feedback = path_record.feedback_source == -1 &&
                               path_record.feedback_sink == -1;
      const bool valid_feedback =
          path_record.feedback_source >= 0 &&
          path_record.feedback_source < model.parts &&
          path_record.feedback_sink >= 0 &&
          path_record.feedback_sink < model.parts &&
          path_record.feedback_source != path_record.feedback_sink;
      require((no_feedback || valid_feedback) &&
                  path_record.feedback_residual_ns >= 0.0 &&
                  (!no_feedback || path_record.feedback_residual_ns == 0.0),
              "invalid PATRON typed physical feedback");
    }
  }

  std::array<unsigned char, 8> trailer{};
  reader.read_raw(trailer.data(), trailer.size(), "trailer");
  const std::array<unsigned char, 8> expected_trailer = {
      'E', 'M', 'P', 'E', 'N', 'D', '1', 0};
  const std::uint32_t declared_checksum = reader.raw_u32("checksum");
  require(trailer == expected_trailer &&
              declared_checksum == reader.checksum(),
          "invalid PATRON typed input trailer");
  char trailing = 0;
  input->read(&trailing, 1);
  require(input->gcount() == 0 && input->eof(),
          "trailing PATRON typed input bytes");
  bind_packed_members(model);
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
      load[part][dim] += cluster_weight(model, cluster, dim);
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
  std::vector<int> ratios = paired_domain_ratios(model, domain_load);
  int maximum_ratio = 1;
  int maximum_load = 0;
  for (int domain = 0; domain < model.domains; ++domain) {
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
  long long maximum_path_hops = 0;
  long long maximum_path_ratio_sum = 0;
  for (const Path& path : model.path) {
    double transport = 0.0;
    long long path_hops = 0;
    long long path_ratio_sum = 0;
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
        path_hops += static_cast<long long>(route.arcs.size());
        int net_ratio = 1;
        for (const Arc& arc : route.arcs) {
          net_ratio = std::max(net_ratio, ratios[arc.domain]);
        }
        path_ratio_sum += net_ratio;
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
        const Route& route = model.route[transition.first][transition.second];
        path_hops += static_cast<long long>(route.arcs.size());
        int net_ratio = 1;
        for (const Arc& arc : route.arcs) {
          net_ratio = std::max(net_ratio, ratios[arc.domain]);
        }
        path_ratio_sum += net_ratio;
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
    maximum_path_hops = std::max(maximum_path_hops, path_hops);
    maximum_path_ratio_sum = std::max(
        maximum_path_ratio_sum, path_ratio_sum);
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
  const auto pressure = transport_capacity_pressure(model, domain_load);
  result.ranked = {
      maximum_path_hops,
      maximum_path_ratio_sum,
      rank_float(result.objective[0]),
      rank_float(result.objective[1]),
      negative_paths,
      maximum_ratio,
      maximum_load,
      total_snaking,
      total_hops,
      cut_bits,
  };
  result.capacity_violations = pressure.first;
  result.capacity_excess_bits = pressure.second;
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
      state.payload.add_transition(
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
  state.payload.finish(sink_counts, domain_counts);
  return state;
}

bool proxy_transport_feasible(const Model& model,
                              const std::vector<int>& assignment) {
  for (int net = 0; net < model.nets; ++net) {
    if (!build_proxy_net(model, assignment, net).feasible) {
      return false;
    }
  }
  return true;
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
  return state.payload.sink_count(part);
}

ProxyPathState build_proxy_path(
    const Model& model,
    const ProxyState& state,
    const std::map<int, ProxyNetState>& replacements,
    const std::vector<int>& ratios,
    int path_index) {
  const Path& path = model.path[path_index];
  double transport = 0.0;
  long long path_hops = 0;
  long long path_ratio_sum = 0;
  std::vector<int> sequence;
  sequence.reserve(path.nets.size() * 2);
  std::vector<int> dependency_domains;
  dependency_domains.reserve(path.nets.size());
  if (path.start_cluster >= 0) {
    int target = state.assignment[path.end_cluster];
    std::vector<std::pair<int, int>> reverse_transitions;
    reverse_transitions.reserve(path.nets.size());
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
      int net_ratio = 1;
      for (const Arc& arc : route.arcs) {
        dependency_domains.push_back(arc.domain);
        ++path_hops;
        net_ratio = std::max(net_ratio, ratios[arc.domain]);
      }
      path_ratio_sum += net_ratio;
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
      long long worst_hops = 0;
      long long worst_ratio_sum = 0;
      std::pair<int, int> worst = {-1, -1};
      net_state.payload.for_each_transition([&](const Transition& transition) {
        const Route& route = model.route[transition.source][transition.sink];
        const double delay = predicted_transition_delay(
            model, route, ratios, transition.sink_clusters);
        for (const Arc& arc : route.arcs) {
          dependency_domains.push_back(arc.domain);
        }
        const std::pair<int, int> identity = {
            transition.source, transition.sink};
        if (delay > worst_delay
            || (std::abs(delay - worst_delay) <= 1.0e-12
                && (worst.first < 0 || identity < worst))) {
          worst_delay = delay;
          worst = identity;
          worst_hops = static_cast<long long>(route.arcs.size());
          worst_ratio_sum = 1;
          for (const Arc& arc : route.arcs) {
            worst_ratio_sum = std::max(
                worst_ratio_sum, static_cast<long long>(ratios[arc.domain]));
          }
        }
      });
      transport += worst_delay;
      path_hops += worst_hops;
      path_ratio_sum += worst_ratio_sum;
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
  std::vector<bool> seen(model.parts, false);
  long long snaking = 0;
  for (int part : sequence) {
    if (seen[part]) {
      ++snaking;
    }
    seen[part] = true;
  }
  std::sort(dependency_domains.begin(), dependency_domains.end());
  dependency_domains.erase(
      std::unique(dependency_domains.begin(), dependency_domains.end()),
      dependency_domains.end());
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
      static_cast<long long>(sequence.empty() ? 0 : sequence.size() - 1),
      path_hops,
      path_ratio_sum,
      std::move(dependency_domains),
  };
}

Evaluation proxy_evaluation(const Model& model, const ProxyState& state) {
  Evaluation result;
  const double worst = state.slack_path_order.minimum().first;
  const int maximum_ratio = *state.ratio_order.rbegin();
  const int maximum_load = *std::max_element(
      state.domain_load.begin(), state.domain_load.end());
  const long long maximum_path_hops = state.hop_path_order.maximum().first;
  const long long maximum_path_ratio_sum
      = state.ratio_sum_path_order.maximum().first;
  result.objective = {
      -worst,
      -state.total_negative,
      static_cast<double>(state.negative_paths),
      static_cast<double>(maximum_ratio),
      static_cast<double>(maximum_load),
      static_cast<double>(state.transition_objective
                              ? state.transitions
                              : state.snaking),
      static_cast<double>(state.hops),
      static_cast<double>(state.cuts),
  };
  const auto pressure = transport_capacity_pressure(
      model, state.domain_load);
  result.ranked = {
      maximum_path_hops,
      maximum_path_ratio_sum,
      rank_float(result.objective[0]),
      rank_float(result.objective[1]),
      state.negative_paths,
      maximum_ratio,
      maximum_load,
      state.transition_objective ? state.transitions : state.snaking,
      state.hops,
      state.cuts,
  };
  result.capacity_violations = pressure.first;
  result.capacity_excess_bits = pressure.second;
  result.feasible = true;
  return result;
}

ProxyState build_proxy_state(
    const Model& model,
    const std::vector<int>* assignment_override = nullptr) {
  ProxyState state;
  state.transition_objective = model.flow_version == 14;
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
      state.resource_load[part][dim] += cluster_weight(model, cluster, dim);
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
    state.net[net].payload.for_each_domain(
        [&](int domain, int count) { state.domain_load[domain] += count; });
    state.hops += state.net[net].hops;
    state.cuts += state.net[net].cuts;
  }
  state.domain_ratio = paired_domain_ratios(model, state.domain_load);
  for (int domain = 0; domain < model.domains; ++domain) {
    state.ratio_order.insert(state.domain_ratio[domain]);
  }
  state.path.resize(model.paths);
  state.domain_paths.resize(model.domains);
  const std::map<int, ProxyNetState> empty;
  for (int path = 0; path < model.paths; ++path) {
    state.path[path] = build_proxy_path(
        model, state, empty, state.domain_ratio, path);
    state.path[path].dependency_domains.for_each([&](int domain) {
      state.domain_paths[domain].append(path);
    });
    state.slack_path_order.append(
        {state.path[path].normalized_slack, path});
    state.hop_path_order.append({state.path[path].hops, path});
    state.ratio_sum_path_order.append(
        {state.path[path].ratio_sum, path});
    state.objective_path_order.append(
        {state.path[path].hops, state.path[path].ratio_sum, path});
    state.total_negative += std::min(0.0, state.path[path].normalized_slack);
    state.negative_paths += state.path[path].negative ? 1 : 0;
    state.snaking += state.path[path].snaking;
    state.transitions += state.path[path].transitions;
  }
  for (SparsePathMembership& paths : state.domain_paths) {
    paths.finalize();
  }
  state.slack_path_order.finalize(model.paths);
  state.hop_path_order.finalize(model.paths);
  state.ratio_sum_path_order.finalize(model.paths);
  state.objective_path_order.finalize(model.paths);
  require(!state.slack_path_order.empty() && !state.hop_path_order.empty()
              && !state.ratio_sum_path_order.empty()
              && !state.objective_path_order.empty()
              && !state.ratio_order.empty(),
          "scalable state is empty");
  state.evaluation = proxy_evaluation(model, state);
  return state;
}

template <typename Visit>
PackedAdjacency build_ordered_adjacency(int rows, Visit visit) {
  require(rows >= 0, "invalid PATRON adjacency row count");
  std::vector<std::uint32_t> counts(rows, 0);
  visit([&](int row, int value) {
    require(row >= 0 && row < rows && value >= 0,
            "invalid PATRON adjacency identity");
    require(counts[row] < std::numeric_limits<std::uint32_t>::max(),
            "PATRON adjacency row is too large");
    ++counts[row];
  });
  std::vector<std::uint32_t> offsets(
      static_cast<std::size_t>(rows) + 1, 0);
  std::uint64_t total = 0;
  for (int row = 0; row < rows; ++row) {
    total += counts[row];
    require(total <= std::numeric_limits<std::uint32_t>::max(),
            "PATRON adjacency is too large");
    offsets[static_cast<std::size_t>(row) + 1] =
        static_cast<std::uint32_t>(total);
  }
  std::vector<std::uint32_t> cursor(offsets.begin(), offsets.end() - 1);
  std::vector<int> values(static_cast<std::size_t>(total), 0);
  visit([&](int row, int value) {
    const std::uint32_t position = cursor[row]++;
    require(position < offsets[static_cast<std::size_t>(row) + 1],
            "PATRON adjacency count changed between passes");
    values[position] = value;
  });
  std::vector<std::uint32_t> compact_offsets(
      static_cast<std::size_t>(rows) + 1, 0);
  std::size_t output = 0;
  for (int row = 0; row < rows; ++row) {
    require(cursor[row] == offsets[static_cast<std::size_t>(row) + 1],
            "PATRON adjacency count changed between passes");
    bool first = true;
    int previous = -1;
    for (std::uint32_t input = offsets[row];
         input < offsets[static_cast<std::size_t>(row) + 1]; ++input) {
      const int value = values[input];
      require(first || value >= previous,
              "non-canonical PATRON adjacency order");
      if (first || value != previous) {
        values[output++] = value;
      }
      first = false;
      previous = value;
    }
    compact_offsets[static_cast<std::size_t>(row) + 1] =
        static_cast<std::uint32_t>(output);
  }
  values.resize(output);
  return PackedAdjacency(std::move(compact_offsets), std::move(values));
}

PackedAdjacency build_cluster_nets(const Model& model) {
  return build_ordered_adjacency(model.clusters, [&](const auto& emit) {
    for (int net = 0; net < model.nets; ++net) {
      for (int cluster : model.net[net].drivers) {
        emit(cluster, net);
      }
      for (int cluster : model.net[net].sinks) {
        emit(cluster, net);
      }
    }
  });
}

PackedAdjacency build_net_paths(const Model& model) {
  return build_ordered_adjacency(model.nets, [&](const auto& emit) {
    for (int path = 0; path < model.paths; ++path) {
      for (int net : model.path[path].nets) {
        emit(net, path);
      }
    }
  });
}

ProxyDelta evaluate_proxy_move(
    const Model& model,
    ProxyState& state,
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
    int cluster,
    int target);

void apply_proxy_delta(const Model& model,
                       ProxyState& state,
                       const ProxyDelta& delta);

std::vector<int> diagnose_flow_corridors(
    const Model& model,
    const ProxyState& state,
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
    const std::vector<double>& exposure,
    int cover_domain) {
  std::vector<int> selected_assignment;
  std::vector<long long> selected_rank;
  int improving_candidate_generation = 0;
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
          if (!capacity_compatible
              || !proxy_transport_feasible(model, assignment)) {
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
          if (!less_ranked(candidate.evaluation.ranked,
                           state.evaluation.ranked)) {
            return;
          }
          ++improving_candidate_generation;
          if (selected_assignment.empty()
              || less_ranked(candidate.evaluation.ranked, selected_rank)) {
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
  // Keep the already resource-legal assignment as the deterministic
  // secondary objective.  A zero-cost side preference lets an otherwise
  // equivalent net cut move millions of clusters onto one topology side;
  // the later per-FPGA legalizer then cannot recover the original balance.
  // Try every endpoint with the unit displacement cost first.  Only if that
  // complete pass finds no legal improvement do later passes progressively
  // prefer smaller cuts, so the normal search pays no fallback overhead.
  constexpr std::array<long long, 4> movement_costs = {1, 2, 4, 8};
  for (long long movement_cost : movement_costs) {
  const int generation_before_pass = improving_candidate_generation;
  for (int pair_target = 0; pair_target < model.parts; ++pair_target) {
    if (!opposite_side[pair_target]) {
      continue;
    }
    std::set<int> boundary_set;
    for (int net = 0; net < model.nets; ++net) {
      if (state.net[net].payload.domain_count(cover_domain) == 0) {
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
        = static_cast<long long>(corridor_nets.size())
          + static_cast<long long>(corridor.size()) * movement_cost + 1;
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
      if (!opposite_side[state.assignment[cluster]]) {
        flow.add_edge(
            source_node, cluster_node[cluster], movement_cost);
      } else {
        flow.add_edge(
            cluster_node[cluster], sink_node, movement_cost);
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
        candidate_load[source][dim] -= cluster_weight(model, cluster, dim);
        candidate_load[target][dim] += cluster_weight(model, cluster, dim);
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
    const bool minimum_parts_compatible
        = std::count_if(candidate_counts.begin(), candidate_counts.end(),
                        [](int count) { return count > 0; })
          >= model.min_used_parts;
    const bool transport_compatible
        = proxy_transport_feasible(model, candidate_assignment);
    std::cerr << "PATRON_FLOW_CORRIDOR pair=" << edge_left << ':'
              << pair_target
              << " boundary=" << boundary.size()
              << " clusters=" << corridor.size()
              << " cluster_limit=" << maximum_corridor_clusters
              << " distance_limit=" << corridor_distance
              << " nets=" << corridor_nets.size()
              << " movement_cost=" << movement_cost
              << " mincut=" << cut
              << " moved_to_left=" << moved_to_left
              << " moved_to_target=" << moved_to_target
              << " capacity_compatible=" << (capacity_compatible ? 1 : 0)
              << " minimum_parts_compatible="
              << (minimum_parts_compatible ? 1 : 0)
              << " transport_compatible="
              << (transport_compatible ? 1 : 0);
    if (transport_compatible && minimum_parts_compatible) {
      ProxyState candidate = build_proxy_state(model, &candidate_assignment);
      std::cerr << " domain_load=" << candidate.domain_load[cover_domain]
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
    }
    std::cerr << '\n';
    consider_dual_improving(
        candidate_assignment, capacity_compatible && transport_compatible);

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
              relief += std::min(cluster_weight(model, cluster, dim),
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
                            && cluster_weight(model, option.cluster, dim)
                                   > 0.0);
          if (candidate_load[option.target][dim]
                  + cluster_weight(model, option.cluster, dim)
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
          const double weight = cluster_weight(model, option.cluster, dim);
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
      const bool legalized_transport
          = legalized_capacity
            && proxy_transport_feasible(model, candidate_assignment);
      std::cerr << " transport_compatible="
                << (legalized_transport ? 1 : 0);
      if (legalized_transport) {
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
                          -= cluster_weight(model, cluster, dim);
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
                                    cluster_weight(model, left, dim)
                                        / aggregate_slack[dim]);
                                right_constraint = std::max(
                                    right_constraint,
                                    cluster_weight(model, right, dim)
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
                            + cluster_weight(model, cluster, dim);
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
                        += cluster_weight(model, cluster, dim);
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
                const bool region_transport
                    = region_capacity
                      && proxy_transport_feasible(model, region_assignment);
                std::cerr << " transport_compatible="
                          << (region_transport ? 1 : 0);
                if (region_transport) {
                  ProxyState legalized = build_proxy_state(
                      model, &region_assignment);
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

        // A topology-side min-cut moves clusters in both directions.  The
        // one-sided legalization above can repair the destination half, but
        // it leaves every reverse-moving cluster collapsed onto edge_left.
        // Repack both crossing regions together while preserving the cut's
        // left/right decision.  This keeps every resource dimension hard and
        // lets the two physical FPGAs on each side share the moved load.
        const auto run_bilateral_region_legalization
            = [&](const std::string& mode,
                  bool affinity_first,
                  bool raw_target_first,
                  bool repack_full_corridor = false,
                  bool enforce_cut_side = false) {
                std::vector<int> region_assignment
                    = raw_candidate_assignment;
                std::vector<std::vector<double>> region_load
                    = raw_candidate_load;
                std::vector<int> unassigned;
                std::vector<bool> target_side(model.clusters, false);
                for (int cluster : corridor) {
                  const int target = raw_candidate_assignment[cluster];
                  const bool crosses_cut
                      = target >= 0
                        && opposite_side[state.assignment[cluster]]
                               != opposite_side[target];
                  if (target < 0
                      || model.cluster[cluster].fixed >= 0
                      || (!repack_full_corridor && !crosses_cut)) {
                    continue;
                  }
                  target_side[cluster] = opposite_side[target];
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    region_load[target][dim]
                        -= cluster_weight(model, cluster, dim);
                  }
                  region_assignment[cluster] = -1;
                  unassigned.push_back(cluster);
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
                std::vector<std::vector<double>> aggregate_slack(
                    2, std::vector<double>(model.dimensions, 0.0));
                for (int part = 0; part < model.parts; ++part) {
                  const int side = opposite_side[part] ? 1 : 0;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    aggregate_slack[side][dim] += std::max(
                        0.0,
                        std::min(model.hard_capacity[part][dim],
                                 model.balance_capacity[part][dim])
                            - region_load[part][dim]);
                  }
                }
                std::sort(unassigned.begin(), unassigned.end(),
                          [&](int left, int right) {
                            const int left_side = target_side[left] ? 1 : 0;
                            const int right_side = target_side[right] ? 1 : 0;
                            double left_constraint = 0.0;
                            double right_constraint = 0.0;
                            for (int dim = 0; dim < model.dimensions; ++dim) {
                              if (aggregate_slack[left_side][dim]
                                  > 1.0e-12) {
                                left_constraint = std::max(
                                    left_constraint,
                                    cluster_weight(model, left, dim)
                                        / aggregate_slack[left_side][dim]);
                              }
                              if (aggregate_slack[right_side][dim]
                                  > 1.0e-12) {
                                right_constraint = std::max(
                                    right_constraint,
                                    cluster_weight(model, right, dim)
                                        / aggregate_slack[right_side][dim]);
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
                  bool best_preferred_side = false;
                  bool best_raw_target = false;
                  int best_affinity = -1;
                  double best_utilization
                      = std::numeric_limits<double>::infinity();
                  for (int target = 0; target < model.parts; ++target) {
                    const bool preferred_side
                        = opposite_side[target] == target_side[cluster];
                    if (enforce_cut_side && !preferred_side) {
                      continue;
                    }
                    const bool raw_target
                        = target == raw_candidate_assignment[cluster];
                    bool fits = true;
                    double utilization = 0.0;
                    for (int dim = 0; dim < model.dimensions; ++dim) {
                      const double limit = std::min(
                          model.hard_capacity[target][dim],
                          model.balance_capacity[target][dim]);
                      const double projected
                          = region_load[target][dim]
                            + cluster_weight(model, cluster, dim);
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
                    if (preferred_side != best_preferred_side) {
                      better = preferred_side;
                    } else if (raw_target_first
                               && raw_target != best_raw_target) {
                      better = raw_target;
                    } else if (affinity_first) {
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
                      best_preferred_side = preferred_side;
                      best_raw_target = raw_target;
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
                        += cluster_weight(model, cluster, dim);
                  }
                  for (int net : cluster_nets[cluster]) {
                    int occurrences = static_cast<int>(std::count(
                        model.net[net].drivers.begin(),
                        model.net[net].drivers.end(), cluster));
                    occurrences += static_cast<int>(std::count(
                        model.net[net].sinks.begin(),
                        model.net[net].sinks.end(), cluster));
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
                std::cerr << "PATRON_FLOW_BILATERAL_LEGALIZATION pair="
                          << edge_left << ':' << pair_target
                          << " mode=" << mode
                          << " scope="
                          << (repack_full_corridor ? "full-corridor"
                                                   : "crossing-only")
                          << " enforce_cut_side="
                          << (enforce_cut_side ? 1 : 0)
                          << " crossing=" << unassigned.size()
                          << " assigned="
                          << (assigned_all ? unassigned.size() : 0)
                          << " capacity_compatible="
                          << (region_capacity ? 1 : 0);
                const bool region_transport
                    = region_capacity
                      && proxy_transport_feasible(model, region_assignment);
                std::cerr << " transport_compatible="
                          << (region_transport ? 1 : 0);
                if (region_transport) {
                  ProxyState legalized = build_proxy_state(
                      model, &region_assignment);
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
        run_bilateral_region_legalization("affinity-first", true, false);
        run_bilateral_region_legalization("balance-first", false, false);
        run_bilateral_region_legalization("raw-target-first", false, true);
        // The crossing-only repackers retain the original placement of every
        // cluster that the min-cut kept on its current topology side.  On a
        // tight multidimensional balance contract those retained choices can
        // fragment the two FPGA bins on one side even when their aggregate
        // capacity is sufficient.  Repack every movable corridor cluster
        // across the two bins of its min-cut side, and forbid fallback to the
        // opposite side.  This preserves the cut while satisfying the exact
        // per-FPGA resource contract; fixed clusters remain anchored.
        run_bilateral_region_legalization(
            "full-side-balance", false, false, true, true);

        // The all-at-once repacker above may paint itself into a corner even
        // though the original assignment is resource legal: an early greedy
        // choice can consume the only remaining capacity for a later cluster.
        // Build an additional candidate monotonically from the original legal
        // assignment.  Interleave the two crossing directions so each move
        // can free capacity for the other side, and defer any move that is not
        // currently legal.  Every intermediate and final assignment therefore
        // preserves the caller's hard and balance limits without relaxation.
        const auto run_incremental_bilateral
            = [&](const std::string& mode, bool raw_target_first) {
                std::vector<int> incremental_assignment = state.assignment;
                std::vector<std::vector<double>> incremental_load
                    = state.resource_load;
                std::array<std::vector<int>, 2> pending;
                std::vector<bool> desired_side(model.clusters, false);
                for (int cluster : corridor) {
                  const int raw_target = raw_candidate_assignment[cluster];
                  if (raw_target < 0
                      || opposite_side[state.assignment[cluster]]
                             == opposite_side[raw_target]) {
                    continue;
                  }
                  desired_side[cluster] = opposite_side[raw_target];
                  pending[desired_side[cluster] ? 1 : 0].push_back(cluster);
                }
                std::vector<std::vector<double>> side_slack(
                    2, std::vector<double>(model.dimensions, 0.0));
                for (int part = 0; part < model.parts; ++part) {
                  const int side = opposite_side[part] ? 1 : 0;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    side_slack[side][dim] += std::max(
                        0.0,
                        std::min(model.hard_capacity[part][dim],
                                 model.balance_capacity[part][dim])
                            - incremental_load[part][dim]);
                  }
                }
                const auto constraint_score = [&](int cluster) {
                  const int side = desired_side[cluster] ? 1 : 0;
                  double result = 0.0;
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    if (side_slack[side][dim] > 1.0e-12) {
                      result = std::max(
                          result,
                          cluster_weight(model, cluster, dim)
                              / side_slack[side][dim]);
                    } else if (cluster_weight(model, cluster, dim) > 0.0) {
                      result = std::numeric_limits<double>::infinity();
                    }
                  }
                  return result;
                };
                for (auto& side_pending : pending) {
                  std::sort(side_pending.begin(), side_pending.end(),
                            [&](int left, int right) {
                              const double left_score
                                  = constraint_score(left);
                              const double right_score
                                  = constraint_score(right);
                              if (left_score != right_score) {
                                return left_score > right_score;
                              }
                              if (exposure[left] != exposure[right]) {
                                return exposure[left] < exposure[right];
                              }
                              return left < right;
                            });
                }
                int moved[2] = {0, 0};
                constexpr int kIncrementalPasses = 6;
                for (int pass = 0; pass < kIncrementalPasses; ++pass) {
                  std::array<std::vector<int>, 2> deferred;
                  std::size_t cursor[2] = {0, 0};
                  bool progress = false;
                  int next_side = pass % 2;
                  while (cursor[0] < pending[0].size()
                         || cursor[1] < pending[1].size()) {
                    int side = next_side;
                    if (cursor[side] >= pending[side].size()) {
                      side = 1 - side;
                    }
                    if (cursor[side] >= pending[side].size()) {
                      break;
                    }
                    next_side = 1 - side;
                    const int cluster = pending[side][cursor[side]++];
                    int best_target = -1;
                    bool best_raw_target = false;
                    double best_utilization
                        = std::numeric_limits<double>::infinity();
                    for (int target = 0; target < model.parts; ++target) {
                      if ((opposite_side[target] ? 1 : 0) != side) {
                        continue;
                      }
                      bool fits = true;
                      double utilization = 0.0;
                      for (int dim = 0; dim < model.dimensions; ++dim) {
                        const double limit = std::min(
                            model.hard_capacity[target][dim],
                            model.balance_capacity[target][dim]);
                        const double projected
                            = incremental_load[target][dim]
                              + cluster_weight(model, cluster, dim);
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
                      const bool is_raw_target
                          = target == raw_candidate_assignment[cluster];
                      bool better = best_target < 0;
                      if (raw_target_first
                          && is_raw_target != best_raw_target) {
                        better = is_raw_target;
                      } else {
                        better = better
                                 || utilization
                                        < best_utilization - 1.0e-12
                                 || (std::abs(utilization - best_utilization)
                                            <= 1.0e-12
                                     && target < best_target);
                      }
                      if (better) {
                        best_target = target;
                        best_raw_target = is_raw_target;
                        best_utilization = utilization;
                      }
                    }
                    if (best_target < 0) {
                      deferred[side].push_back(cluster);
                      continue;
                    }
                    const int source = incremental_assignment[cluster];
                    require(source >= 0 && source != best_target,
                            "incremental bilateral source is invalid");
                    incremental_assignment[cluster] = best_target;
                    for (int dim = 0; dim < model.dimensions; ++dim) {
                      const double weight
                          = cluster_weight(model, cluster, dim);
                      incremental_load[source][dim] -= weight;
                      incremental_load[best_target][dim] += weight;
                    }
                    ++moved[side];
                    progress = true;
                  }
                  pending = std::move(deferred);
                  if (!progress) {
                    break;
                  }
                }
                bool incremental_capacity = true;
                for (int part = 0; part < model.parts; ++part) {
                  for (int dim = 0; dim < model.dimensions; ++dim) {
                    incremental_capacity
                        = incremental_capacity
                          && incremental_load[part][dim]
                                 <= model.hard_capacity[part][dim] + 1.0e-9
                          && incremental_load[part][dim]
                                 <= model.balance_capacity[part][dim]
                                        + 1.0e-9;
                  }
                }
                require(incremental_capacity,
                        "incremental bilateral legalization broke capacity");
                const bool incremental_transport
                    = proxy_transport_feasible(model,
                                               incremental_assignment);
                std::cerr << "PATRON_FLOW_INCREMENTAL_BILATERAL pair="
                          << edge_left << ':' << pair_target
                          << " mode=" << mode
                          << " moved_to_source_side=" << moved[0]
                          << " moved_to_sink_side=" << moved[1]
                          << " deferred="
                          << pending[0].size() + pending[1].size()
                          << " capacity_compatible=1"
                          << " transport_compatible="
                          << (incremental_transport ? 1 : 0);
                if (incremental_transport) {
                  ProxyState incremental = build_proxy_state(
                      model, &incremental_assignment);
                  std::cerr << " domain_load="
                            << incremental.domain_load[cover_domain]
                            << " improving="
                            << (less_ranked(incremental.evaluation.ranked,
                                            state.evaluation.ranked)
                                    ? 1 : 0)
                            << " rank=";
                  for (long long value : incremental.evaluation.ranked) {
                    std::cerr << value << ',';
                  }
                  consider_dual_improving(incremental_assignment, true);
                }
                std::cerr << '\n';
              };
        run_incremental_bilateral("balanced", false);
        run_incremental_bilateral("raw-target-first", true);

      if (pair_target == edge_right) {
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
                    -= cluster_weight(model, cluster, dim);
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
              variable_totals[dim] += cluster_weight(model, cluster, dim);
            }
          }
          std::vector<long long> balance_weight(model.clusters, 1);
          for (int cluster : parametric_variables) {
            long long weight = 1;
            for (int dim = 0; dim < model.dimensions; ++dim) {
              if (variable_totals[dim] > 0.0) {
                weight += static_cast<long long>(std::llround(
                    1000000.0 * cluster_weight(model, cluster, dim)
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
                    += cluster_weight(model, cluster, dim);
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
              state.net[net].payload.for_each_domain(
                  [&](int domain, int count) {
                    parametric_domain_load[domain] -= count;
                  });
              replacement.payload.for_each_domain(
                  [&](int domain, int count) {
                    parametric_domain_load[domain] += count;
                  });
              parametric_hops += replacement.hops - state.net[net].hops;
              parametric_cuts += replacement.cuts - state.net[net].cuts;
            }
            int parametric_max_ratio = 1;
            int parametric_max_load = 0;
            const auto parametric_ratios = paired_domain_ratios(model, parametric_domain_load);
            for (int domain = 0; domain < model.domains; ++domain) {
              parametric_max_ratio = std::max(
                  parametric_max_ratio,
                  parametric_ratios[domain]);
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
                      score += cluster_weight(model, cluster, dim) / limit;
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
                        += cluster_weight(model, cluster, dim);
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
                const bool piercing_transport
                    = piercing_capacity
                      && proxy_transport_feasible(model, piercing_assignment);
                if (piercing_transport) {
                  ProxyState candidate = build_proxy_state(
                      model, &piercing_assignment);
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
                  if (less_ranked(candidate.evaluation.ranked,
                                  state.evaluation.ranked)
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
                  if (!proxy_transport_feasible(model, assignment)) {
                    std::cerr << "PATRON_FLOW_PARAMETRIC_RESULT pair="
                              << edge_left << ':' << pair_target
                              << " label=" << label
                              << " transport_compatible=0\n";
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
          if (!best_piercing_assignment.empty()
              && proxy_transport_feasible(
                  model, best_piercing_assignment)) {
            ProxyState polished = build_proxy_state(
                model, &best_piercing_assignment);
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
                      || !less_ranked(candidate.evaluation.ranked,
                                      polished.evaluation.ranked)) {
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
                      << (less_ranked(polished.evaluation.ranked,
                                      state.evaluation.ranked)
                              ? 1
                              : 0)
                      << " rank=";
            for (long long value : polished.evaluation.ranked) {
              std::cerr << value << ',';
            }
            std::cerr << '\n';
            if (less_ranked(polished.evaluation.ranked,
                            state.evaluation.ranked)) {
              ++improving_candidate_generation;
              if (selected_assignment.empty()
                  || less_ranked(polished.evaluation.ranked,
                                 selected_rank)) {
                selected_assignment = polished.assignment;
                selected_rank = polished.evaluation.ranked;
              }
            }
          }
        }
      }
    }
    if (!selected_assignment.empty()
        && selected_rank[0] < state.evaluation.ranked[0]) {
      std::cerr << "PATRON_FLOW_PRIMARY_IMPROVEMENT pair="
                << edge_left << ':' << pair_target
                << " maximum_path_hops=" << selected_rank[0]
                << " previous_maximum_path_hops="
                << state.evaluation.ranked[0]
                << " action=accept-before-next-board-edge\n";
      return selected_assignment;
    }
    }
    if (improving_candidate_generation != generation_before_pass) {
      break;
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
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
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
  std::vector<int> changed_clusters = {cluster};
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
    if (std::find(changed_clusters.begin(), changed_clusters.end(), partner)
        != changed_clusters.end()) {
      return delta;
    }
    changed_clusters.push_back(partner);
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
        || std::find(changed_clusters.begin(), changed_clusters.end(),
                     changed_cluster) != changed_clusters.end()) {
      return delta;
    }
    changed_clusters.push_back(changed_cluster);
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
          -= cluster_weight(model, changed_cluster, dim);
      projected_load[changed_target][dim]
          += cluster_weight(model, changed_cluster, dim);
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
  std::vector<int> affected_nets;
  for (const auto& change : changes) {
    const int changed_cluster = std::get<0>(change);
    affected_nets.insert(affected_nets.end(),
                         cluster_nets[changed_cluster].begin(),
                         cluster_nets[changed_cluster].end());
  }
  std::sort(affected_nets.begin(), affected_nets.end());
  affected_nets.erase(
      std::unique(affected_nets.begin(), affected_nets.end()),
      affected_nets.end());
  for (int net : affected_nets) {
    ProxyNetState replacement = build_proxy_net(model, state.assignment, net);
    if (!replacement.feasible) {
      restore_assignment();
      return delta;
    }
    delta.nets.emplace(net, std::move(replacement));
  }
  for (const auto& item : delta.nets) {
    state.net[item.first].payload.for_each_domain(
        [&](int domain, int count) { delta.domain_delta[domain] -= count; });
    item.second.payload.for_each_domain(
        [&](int domain, int count) { delta.domain_delta[domain] += count; });
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
  std::vector<int> projected_loads = state.domain_load;
  for (const auto& item : delta.domain_delta) projected_loads[item.first] += item.second;
  std::vector<int> projected_ratios = paired_domain_ratios(model, projected_loads);
  std::vector<int> ratio_changed_domains;
  for (int domain = 0; domain < model.domains; ++domain) {
    if (projected_ratios[domain] != state.domain_ratio[domain])
      ratio_changed_domains.push_back(domain);
  }
  std::vector<int> affected_paths;
  for (const auto& item : delta.nets) {
    affected_paths.insert(affected_paths.end(),
                          net_paths[item.first].begin(),
                          net_paths[item.first].end());
  }
  for (int domain : ratio_changed_domains) {
    state.domain_paths[domain].append_to(affected_paths);
  }
  std::sort(affected_paths.begin(), affected_paths.end());
  affected_paths.erase(
      std::unique(affected_paths.begin(), affected_paths.end()),
      affected_paths.end());
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
  long long candidate_transitions = state.transitions;
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
    candidate_transitions += replacement.transitions - old.transitions;
  }
  double worst = std::numeric_limits<double>::infinity();
  for (const auto& item : delta.paths) {
    worst = std::min(worst, item.second.normalized_slack);
  }
  state.slack_path_order.for_each_ascending([&](const auto& item) {
    if (!std::binary_search(
            affected_paths.begin(), affected_paths.end(), item.second)) {
      worst = std::min(worst, item.first);
      return false;
    }
    return true;
  });
  require(std::isfinite(worst), "empty scalable candidate path order");
  long long maximum_path_hops = 0;
  long long maximum_path_ratio_sum = 0;
  for (const auto& item : delta.paths) {
    maximum_path_hops = std::max(maximum_path_hops, item.second.hops);
    maximum_path_ratio_sum = std::max(
        maximum_path_ratio_sum, item.second.ratio_sum);
  }
  state.hop_path_order.for_each_descending([&](const auto& item) {
    if (!std::binary_search(
            affected_paths.begin(), affected_paths.end(), item.second)) {
      maximum_path_hops = std::max(maximum_path_hops, item.first);
      return false;
    }
    return true;
  });
  state.ratio_sum_path_order.for_each_descending([&](const auto& item) {
    if (!std::binary_search(
            affected_paths.begin(), affected_paths.end(), item.second)) {
      maximum_path_ratio_sum = std::max(
          maximum_path_ratio_sum, item.first);
      return false;
    }
    return true;
  });
  const int maximum_ratio = *std::max_element(
      projected_ratios.begin(), projected_ratios.end());
  const int maximum_load = *std::max_element(
      projected_loads.begin(), projected_loads.end());
  delta.evaluation.feasible = true;
  delta.evaluation.objective = {
      -worst,
      -candidate_negative,
      static_cast<double>(candidate_negative_paths),
      static_cast<double>(maximum_ratio),
      static_cast<double>(maximum_load),
      static_cast<double>(state.transition_objective
                              ? candidate_transitions
                              : candidate_snaking),
      static_cast<double>(candidate_hops),
      static_cast<double>(candidate_cuts),
  };
  const auto pressure = transport_capacity_pressure(model, projected_loads);
  delta.evaluation.ranked = {
      maximum_path_hops,
      maximum_path_ratio_sum,
      rank_float(delta.evaluation.objective[0]),
      rank_float(delta.evaluation.objective[1]),
      candidate_negative_paths,
      maximum_ratio,
      maximum_load,
      state.transition_objective ? candidate_transitions : candidate_snaking,
      candidate_hops,
      candidate_cuts,
  };
  delta.evaluation.capacity_violations = pressure.first;
  delta.evaluation.capacity_excess_bits = pressure.second;
  delta.feasible = true;

  return delta;
}

ProxyDelta evaluate_proxy_move(
    const Model& model,
    ProxyState& state,
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
    int cluster,
    int target) {
  return evaluate_proxy_changes(
      model, state, cluster_nets, net_paths, cluster, target);
}

ProxyDelta evaluate_proxy_ejection(
    const Model& model,
    ProxyState& state,
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
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
    const PackedAdjacency& cluster_nets,
    const PackedAdjacency& net_paths,
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
      const double weight = cluster_weight(model, cluster, dim);
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
  for (const auto& item : delta.domain_delta)
    state.domain_load[item.first] += item.second;
  const auto new_ratios = paired_domain_ratios(model, state.domain_load);
  for (int domain = 0; domain < model.domains; ++domain) {
    if (new_ratios[domain] == state.domain_ratio[domain]) continue;
    erase_one(state.ratio_order, state.domain_ratio[domain]);
    state.domain_ratio[domain] = new_ratios[domain];
    state.ratio_order.insert(state.domain_ratio[domain]);
  }
  for (const auto& item : delta.paths) {
    ProxyPathState& old = state.path[item.first];
    old.dependency_domains.for_each([&](int domain) {
      state.domain_paths[domain].remove(item.first);
    });
    state.hop_path_order.update(
        {old.hops, item.first}, {item.second.hops, item.first});
    state.ratio_sum_path_order.update(
        {old.ratio_sum, item.first},
        {item.second.ratio_sum, item.first});
    state.objective_path_order.update(
        {old.hops, old.ratio_sum, item.first},
        {item.second.hops, item.second.ratio_sum, item.first});
    state.slack_path_order.update(
        {old.normalized_slack, item.first},
        {item.second.normalized_slack, item.first});
    state.total_negative += std::min(0.0, item.second.normalized_slack)
                            - std::min(0.0, old.normalized_slack);
    state.negative_paths += (item.second.negative ? 1 : 0)
                            - (old.negative ? 1 : 0);
    state.snaking += item.second.snaking - old.snaking;
    state.transitions += item.second.transitions - old.transitions;
    old = item.second;
    old.dependency_domains.for_each([&](int domain) {
      state.domain_paths[domain].add(item.first);
    });
  }
  state.evaluation = proxy_evaluation(model, state);
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

void write_typed_output(const std::string& output_path,
                        const std::string& mode,
                        const Evaluation& initial,
                        const Evaluation& final,
                        const std::vector<NativeMove>& moves,
                        const std::vector<int>& assignment,
                        const std::vector<NativeBatch>& batches,
                        int flow_output_version) {
  require(initial.objective.size() == 8 && final.objective.size() == 8,
          "PATRON typed output objective is incomplete");
  require(initial.ranked.size() == 10 && final.ranked.size() == 10,
          "PATRON typed output rank is incomplete");
  require(mode.size() <= std::numeric_limits<std::uint32_t>::max(),
          "PATRON typed output mode is too large");
  const auto output_version = static_cast<std::uint32_t>(
      flow_output_version == 0 ? 6 : flow_output_version);
  std::uint64_t changes = 0;
  for (const NativeBatch& batch : batches) {
    changes += static_cast<std::uint64_t>(batch.changes.size());
  }

  emuflow::core::AtomicBinaryWriter output(output_path);
  std::uint32_t checksum = 0;
  const auto write = [&](const auto& value) {
    output.Write(value);
    emuflow::core::UpdateCrc32(
        checksum, reinterpret_cast<const unsigned char*>(&value),
        sizeof(value));
  };
  const auto write_bytes = [&](const void* values, std::size_t size) {
    output.WriteBytes(values, size);
    emuflow::core::UpdateCrc32(
        checksum, static_cast<const unsigned char*>(values), size);
  };
  write(kTypedOutputMagic);
  write(kTypedOutputVersion);
  write(output_version);
  write(static_cast<std::uint32_t>(mode.size()));
  write_bytes(mode.data(), mode.size());
  for (const double value : initial.objective) write(value);
  for (const double value : final.objective) write(value);
  write(static_cast<std::int64_t>(initial.capacity_violations));
  write(static_cast<std::int64_t>(initial.capacity_excess_bits));
  write(static_cast<std::int64_t>(final.capacity_violations));
  write(static_cast<std::int64_t>(final.capacity_excess_bits));
  write(static_cast<std::int64_t>(initial.ranked[0]));
  write(static_cast<std::int64_t>(initial.ranked[1]));
  write(static_cast<std::int64_t>(final.ranked[0]));
  write(static_cast<std::int64_t>(final.ranked[1]));
  write(static_cast<std::uint64_t>(moves.size()));
  write(static_cast<std::uint64_t>(batches.size()));
  write(changes);
  write(std::uint32_t{0});
  write(static_cast<std::uint64_t>(assignment.size()));
  for (const int part : assignment) {
    require(part >= 0, "PATRON typed output assignment is invalid");
    write(static_cast<std::uint32_t>(part));
  }
  output.Write(checksum);
  output.Commit();
}

void write_output(const std::string& output_path,
                  const std::string& mode,
                  const Evaluation& initial,
                  const Evaluation& final,
                  const std::vector<NativeMove>& moves,
                  const std::vector<int>& assignment,
                  const std::vector<NativeBatch>& batches = {},
                  int flow_output_version = 0,
                  bool emit_trace = true,
                  bool typed_output = false) {
  if (typed_output) {
    require(!emit_trace,
            "PATRON typed output supports summary-only publication");
    write_typed_output(output_path, mode, initial, final, moves, assignment,
                       batches, flow_output_version);
    return;
  }
  std::ofstream output(output_path);
  require(output.good(), "cannot open output");
  const char* header = "EMUFLOW_PATRON_OUTPUT_V6\n";
  if (flow_output_version == 14) {
    header = "EMUFLOW_PATRON_OUTPUT_V14\n";
  } else if (flow_output_version == 11) {
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
  long long changes = 0;
  for (const NativeBatch& batch : batches) {
    changes += static_cast<long long>(batch.changes.size());
  }
  if (emit_trace) {
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
  }
  output << "SUMMARY " << moves.size() << ' ' << batches.size() << ' '
         << changes << ' ' << (emit_trace ? 1 : 0) << '\n';
  output << "FINAL";
  write_vector(output, final.objective);
  output << '\n';
  require(initial.ranked.size() == 10 && final.ranked.size() == 10,
          "path objective rank is missing");
  output << "CAPACITY " << initial.capacity_violations << ' '
         << initial.capacity_excess_bits << ' '
         << final.capacity_violations << ' '
         << final.capacity_excess_bits << '\n';
  output << "PATH_MAX " << initial.ranked[0] << ' ' << initial.ranked[1]
         << ' ' << final.ranked[0] << ' ' << final.ranked[1] << '\n';
  for (int cluster = 0; cluster < static_cast<int>(assignment.size()); ++cluster) {
    output << "ASSIGN " << cluster << ' ' << assignment[cluster] << '\n';
  }
  output << "END\n";
}

void run_exact(const Model& model,
               const std::string& output_path,
               bool emit_trace,
               bool typed_output) {
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

  require(current.ranked.size() == 10,
          "final assignment objective rank is invalid");
  write_output(output_path,
               "endpoint-exact-global-best-v6",
               initial,
               current,
               moves,
               assignment,
               {},
               0,
               emit_trace,
               typed_output);
}

void run_scalable(const Model& model,
                  const std::string& output_path,
                  bool emit_trace,
                  bool typed_output) {
  ProxyState state = build_proxy_state(model);
  const Evaluation initial = state.evaluation;
  const auto cluster_nets = build_cluster_nets(model);
  const auto net_paths = build_net_paths(model);

  std::vector<double> exposure(model.clusters, 0.0);
  std::vector<long long> hop_exposure(model.clusters, 0);
  std::vector<long long> ratio_sum_exposure(model.clusters, 0);
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
      if (state.path[path].hops > hop_exposure[cluster]) {
        hop_exposure[cluster] = state.path[path].hops;
        ratio_sum_exposure[cluster] = state.path[path].ratio_sum;
      } else if (state.path[path].hops == hop_exposure[cluster]) {
        ratio_sum_exposure[cluster] = std::max(
            ratio_sum_exposure[cluster], state.path[path].ratio_sum);
      }
    }
  }
  std::vector<int> order(model.clusters);
  std::iota(order.begin(), order.end(), 0);
  std::sort(order.begin(), order.end(), [&](int left, int right) {
    return std::tie(hop_exposure[right], ratio_sum_exposure[right],
                    exposure[right], left)
           < std::tie(hop_exposure[left], ratio_sum_exposure[left],
                      exposure[left], right);
  });

  std::vector<NativeMove> moves;
  std::vector<NativeBatch> batches;
  // A whole-design local sweep evaluates every movable cluster against every
  // target partition. On the 2.6M-cluster NVDLA model, one sweep still takes
  // hours because every candidate rebuilds and sorts its affected net/path
  // sets. It also delays the bounded frontier-flow phase that owns v14's two
  // primary objectives (maximum path hops, then maximum path TDM-ratio sum).
  //
  // Keep the historical multi-sweep behavior for qualification-sized models
  // and older algorithms. For large v14 models, use the already-configured
  // critical-cluster budget for the local seed sweep, then proceed to the
  // ejection/comove and exact frontier-flow phases. The order is ranked by
  // maximum path hops, maximum path ratio sum, and timing exposure, so this
  // preserves the intended priority while bounding work. This is a search
  // budget policy only: capacity checks and the lexicographic acceptance
  // predicate are unchanged.
  constexpr int large_v14_cluster_threshold = 500000;
  const bool bounded_large_flow =
      model.flow_version >= 8
      && model.clusters >= large_v14_cluster_threshold;
  const int local_sweep_limit =
      bounded_large_flow ? 1 : model.max_sweeps;
  const int local_cluster_limit =
      bounded_large_flow
      ? std::min(model.ejection_critical_limit, model.clusters)
      : model.clusters;
  for (int sweep = 0; sweep < local_sweep_limit; ++sweep) {
    const std::size_t sweep_start = moves.size();
    for (int order_index = 0;
         order_index < local_cluster_limit;
         ++order_index) {
      const int cluster = order[order_index];
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
    return std::tie(hop_exposure[left], ratio_sum_exposure[left],
                    exposure[left], left)
           < std::tie(hop_exposure[right], ratio_sum_exposure[right],
                      exposure[right], right);
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
            if (candidate.evaluation.ranked[2]
                < state.evaluation.ranked[2]) {
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
          if (candidate.evaluation.ranked[2]
              < state.evaluation.ranked[2]) {
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
    return std::make_tuple(-state.path[left].hops,
                           -state.path[left].ratio_sum,
                           state.path[left].normalized_slack,
                           left)
           < std::make_tuple(-state.path[right].hops,
                             -state.path[right].ratio_sum,
                             state.path[right].normalized_slack,
                             right);
  });
  if (!current_paths.empty()) {
    const long long maximum_hops = state.hop_path_order.maximum().first;
    const long long maximum_ratio_sum
        = state.ratio_sum_path_order.maximum().first;
    int maximum_hop_paths = 0;
    int maximum_ratio_sum_paths = 0;
    std::set<int> critical_domains;
    for (int path : current_paths) {
      if (state.path[path].hops == maximum_hops) {
        ++maximum_hop_paths;
      }
      if (state.path[path].ratio_sum == maximum_ratio_sum) {
        ++maximum_ratio_sum_paths;
      }
      if (state.path[path].hops == maximum_hops
          || state.path[path].ratio_sum == maximum_ratio_sum) {
        state.path[path].dependency_domains.append_to(critical_domains);
      }
    }
    std::cerr << "PATRON_CURRENT_MAXIMA maximum_path_hops="
              << maximum_hops
              << " maximum_path_tdm_ratio_sum=" << maximum_ratio_sum
              << " maximum_hop_paths=" << maximum_hop_paths
              << " maximum_ratio_sum_paths=" << maximum_ratio_sum_paths
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
      if (!proxy_transport_feasible(model, candidate_assignment)) {
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
  const long long frontier_hops = state.hop_path_order.maximum().first;
  const long long frontier_ratio_sum
      = state.ratio_sum_path_order.maximum().first;
  std::vector<int> hop_frontier_domain_paths(model.domains, 0);
  std::vector<int> ratio_frontier_domain_paths(model.domains, 0);
  for (int path : current_paths) {
    if (state.path[path].hops == frontier_hops
        || state.path[path].ratio_sum == frontier_ratio_sum) {
      state.path[path].dependency_domains.append_to(frontier_domains);
    }
    state.path[path].dependency_domains.for_each([&](int domain) {
      if (state.path[path].hops == frontier_hops) {
        ++hop_frontier_domain_paths[domain];
      }
      if (state.path[path].ratio_sum == frontier_ratio_sum) {
        ++ratio_frontier_domain_paths[domain];
      }
    });
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
  int flow_domain = -1;
  const bool path_objective_can_improve
      = frontier_hops > 1 || frontier_ratio_sum > 1;
  for (int domain : frontier_domains) {
    if (!path_objective_can_improve) {
      break;
    }
    if (flow_domain < 0
        || std::tie(hop_frontier_domain_paths[flow_domain],
                    ratio_frontier_domain_paths[flow_domain],
                    state.domain_ratio[flow_domain],
                    state.domain_load[flow_domain])
               < std::tie(hop_frontier_domain_paths[domain],
                          ratio_frontier_domain_paths[domain],
                          state.domain_ratio[domain],
                          state.domain_load[domain])) {
      flow_domain = domain;
    }
  }
  const int refinement_domain = flow_apply ? flow_domain : cover_domain;
  if ((cover_diagnostic || flow_apply) && refinement_domain >= 0) {
    if (flow_apply) {
      std::cerr << "PATRON_PATH_REFINEMENT_DOMAIN domain="
                << refinement_domain
                << " maximum_hop_path_count="
                << hop_frontier_domain_paths[refinement_domain]
                << " maximum_ratio_sum_path_count="
                << ratio_frontier_domain_paths[refinement_domain]
                << " ratio=" << state.domain_ratio[refinement_domain]
                << " load=" << state.domain_load[refinement_domain]
                << '\n';
    }
    std::vector<int> flow_assignment = diagnose_flow_corridors(
        model, state, cluster_nets, net_paths, exposure, refinement_domain);
    if (flow_apply) {
      const int used_parts = static_cast<int>([&]() {
        std::set<int> parts(flow_assignment.begin(), flow_assignment.end());
        return parts.size();
      }());
      if (!flow_assignment.empty()
          && used_parts >= model.min_used_parts
          && proxy_transport_feasible(model, flow_assignment)) {
        ProxyState refined = build_proxy_state(model, &flow_assignment);
        long long evaluated_tail_moves = 0;
        long long feasible_tail_moves = 0;
        long long enumerated_tail_clusters = 0;
        long long selected_tail_clusters = 0;
        int accepted_tail_moves = 0;
        for (int iteration = 0;
             model.flow_version >= 8
                 && iteration < model.flow_max_tail_moves;
             ++iteration) {
          require(!refined.objective_path_order.empty(),
                  "flow tail repair has no objective paths");
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
          const long long maximum_hops
              = refined.hop_path_order.maximum().first;
          const long long maximum_ratio_sum
              = refined.ratio_sum_path_order.maximum().first;
          std::set<int> exact_frontier_paths;
          int selected_hop_paths = 0;
          refined.hop_path_order.for_each_descending([&](const auto& item) {
            if (item.first != maximum_hops
                || selected_hop_paths >= model.flow_max_frontier_paths) {
              return false;
            }
            exact_frontier_paths.insert(item.second);
            ++selected_hop_paths;
            return true;
          });
          int selected_ratio_sum_paths = 0;
          refined.ratio_sum_path_order.for_each_descending(
              [&](const auto& item) {
            if (item.first != maximum_ratio_sum
                || selected_ratio_sum_paths
                       >= model.flow_max_frontier_paths) {
              return false;
            }
            exact_frontier_paths.insert(item.second);
            ++selected_ratio_sum_paths;
            return true;
          });
          for (int path : exact_frontier_paths) {
            add_path_candidates(path);
          }
          bool found = false;
          ProxyDelta best;
          const auto evaluate_candidates = [&]() {
            enumerated_tail_clusters += candidate_clusters.size();
            std::vector<int> evaluation_candidates;
            if (bounded_large_flow) {
              evaluation_candidates.reserve(
                  std::min(model.flow_max_legal_candidates,
                           static_cast<int>(candidate_clusters.size())));
              // `order` is ranked by the two path objectives and timing
              // exposure. Select from that order so high-fanout frontier nets
              // cannot turn a bounded tail repair into another whole-design
              // sweep.
              for (int cluster : order) {
                if (candidate_clusters.count(cluster) == 0
                    || model.cluster[cluster].fixed >= 0) {
                  continue;
                }
                evaluation_candidates.push_back(cluster);
                if (static_cast<int>(evaluation_candidates.size())
                    >= model.flow_max_legal_candidates) {
                  break;
                }
              }
            } else {
              evaluation_candidates.assign(candidate_clusters.begin(),
                                             candidate_clusters.end());
            }
            selected_tail_clusters += evaluation_candidates.size();
            for (int cluster : evaluation_candidates) {
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
            // Expand from the union of the independent maximum-hop and
            // maximum-ratio-sum frontiers only after that union stalls.
            candidate_clusters.clear();
            const int frontier_paths = std::min(
                model.flow_max_frontier_paths, model.paths);
            int ranked_count = 0;
            refined.objective_path_order.for_each_descending(
                [&](const auto& item) {
              if (ranked_count >= frontier_paths) {
                return false;
              }
              add_path_candidates(std::get<2>(item));
              ++ranked_count;
              return true;
            });
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
                  << " enumerated_clusters=" << enumerated_tail_clusters
                  << " selected_clusters=" << selected_tail_clusters
                  << " feasible=" << feasible_tail_moves
                  << " accepted=" << accepted_tail_moves
                  << " rank=";
        for (long long value : refined.evaluation.ranked) {
          std::cerr << value << ',';
        }
        std::cerr << '\n';
        require(less_ranked(refined.evaluation.ranked,
                            state.evaluation.ranked),
                "flow refinement did not improve the ranked objective");
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
            candidate.evaluation.ranked[3] - state.evaluation.ranked[3]};
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
              candidate.evaluation.ranked[3]
                  - state.evaluation.ranked[3]};
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
    const auto evaluate_cover_group = [&](const PackedIndexRange& members,
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
          feasible.evaluation.ranked[3] - state.evaluation.ranked[3]});
    };
    for (int net = 0; net < model.nets; ++net) {
      state.net[net].payload.for_each_transition(
          [&](const Transition& transition) {
            const Route& route =
                model.route[transition.source][transition.sink];
            const bool uses_cover_domain = std::any_of(
                route.arcs.begin(), route.arcs.end(), [&](const Arc& arc) {
                  return arc.domain == cover_domain;
                });
            if (!uses_cover_domain) {
              return;
            }
            evaluate_cover_group(model.net[net].sinks,
                                 transition.sink,
                                 transition.source);
            evaluate_cover_group(model.net[net].drivers,
                                 transition.source,
                                 transition.sink);
          });
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
            const double weight = cluster_weight(model, cluster, dim);
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
          if (candidate.evaluation.ranked[2]
              < state.evaluation.ranked[2]) {
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
  require(endpoint.evaluation.feasible,
          "final scalable assignment is infeasible");
  require(endpoint.evaluation.ranked.size() == 10,
          "final scalable assignment objective rank is invalid");
  std::string mode = "endpoint-exact-critical-ejection-v6";
  if (model.flow_version == 14) {
    mode = "endpoint-exact-critical-flow-v14";
  } else if (model.flow_version == 11) {
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
               model.flow_version,
               emit_trace,
               typed_output);
}

void run(const Model& model,
         const std::string& output_path,
         bool emit_trace,
         bool typed_output) {
  if (model.clusters <= 256 && !model.flow_refinement) {
    run_exact(model, output_path, emit_trace, typed_output);
  } else {
    run_scalable(model, output_path, emit_trace, typed_output);
  }
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_patron_refiner [--summary-output] "
                 "[--typed-output] "
                 "[--typed-input|--legacy-text] INPUT OUTPUT\n";
    return 0;
  }
  bool summary_output = false;
  bool typed_output = false;
  bool typed_input = false;
  bool legacy_text = false;
  std::vector<std::string> positional;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    if (argument == "--summary-output") {
      summary_output = true;
    } else if (argument == "--typed-output") {
      typed_output = true;
    } else if (argument == "--typed-input") {
      typed_input = true;
    } else if (argument == "--legacy-text") {
      legacy_text = true;
    } else {
      positional.push_back(argument);
    }
  }
  if (positional.size() != 2 || (typed_input && legacy_text) ||
      (typed_output && !summary_output)) {
    std::cerr << "usage: emuflow_patron_refiner [--summary-output] "
                 "[--typed-output] "
                 "[--typed-input|--legacy-text] INPUT OUTPUT\n";
    return 2;
  }
  try {
    // The unflagged form remains a checked legacy-text compatibility entry.
    // Production always selects --typed-input explicitly.
    Model model = typed_input ? read_typed_model(positional[0])
                              : read_text_model(positional[0]);
    run(model, positional[1], !summary_output, typed_output);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_patron_refiner: " << error.what() << '\n';
    return 1;
  }
}
