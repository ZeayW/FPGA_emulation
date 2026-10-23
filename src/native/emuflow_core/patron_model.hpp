#pragma once
#include <cstdint>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace emuflow::core::patron {
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

inline void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

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
    Require(begin <= end &&
                end <= static_cast<std::size_t>(
                           std::numeric_limits<std::uint32_t>::max()),
            "PATRON packed member range is too large");
    begin_ = static_cast<std::uint32_t>(begin);
    end_ = static_cast<std::uint32_t>(end);
  }

  void bind(const std::vector<int>& values) {
    Require(end_ <= values.size(), "invalid PATRON packed member range");
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
    Require(!empty() && values_ != nullptr,
            "empty PATRON packed member range");
    return values_[begin_];
  }

 private:
  const int* values_ = nullptr;
  std::uint32_t begin_ = 0;
  std::uint32_t end_ = 0;
};

struct Net {
  PackedIndexRange drivers;
  PackedIndexRange sinks;
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
  Model() = default;
  Model(Model&&) = default;
  Model& operator=(Model&&) = default;
  Model(const Model&) = delete;
  Model& operator=(const Model&) = delete;
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


inline void BindMembers(Model& model) {
  for (auto& net : model.net) { net.drivers.bind(model.net_members); net.sinks.bind(model.net_members); }
  for (auto& path : model.path) path.nets.bind(model.path_nets);
}
}  // namespace emuflow::core::patron

namespace emuflow::native {
emuflow::core::patron::Model ReadTypedPatronModel(const std::string& input);
void RefinePatronModel(const emuflow::core::patron::Model& model,
                       const std::string& output, bool summary_output, bool typed_output);
}
