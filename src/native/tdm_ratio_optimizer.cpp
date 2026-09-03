// SPDX-License-Identifier: Apache-2.0
//
// Continuous Lagrangian/KKT TDM-ratio optimization and discrete legalization.
// Based on Pui & Young, TODAES 2020, and Chen et al., ASP-DAC 2026.

#include <algorithm>
#include <cmath>
#include <cstdlib>
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

constexpr double kEps = 1.0e-12;

struct Domain {
  int lanes = 0;
};

struct Hop {
  int domain = -1;
  int direction = -1;
  int compatibility = -1;
  double base_delay_ns = 0.0;
  double beta_ns = 0.0;
};

struct TimingPath {
  double clock_period_ns = 0.0;
  double required_time_ns = 0.0;
  double fixed_delay_ns = 0.0;
  std::vector<int> hops;
};

struct Input {
  int max_iterations = 500;
  int max_ratio = 1;
  int ratio_quantum = 8;
  int min_ratio = 1;
  bool harmonic_legalization = false;
  int post_refinement_iterations = 200;
  int exact_domain_limit = 2048;
  double convergence = 1.0e-8;
  double positive_scale = 1.0;
  double negative_scale = 1.0;
  double max_period = 1.0;
  std::vector<Domain> domains;
  std::vector<Hop> hops;
  std::vector<TimingPath> paths;
  std::vector<double> seed_ratios;
};

std::vector<int> parse_list(const std::string& text) {
  std::vector<int> result;
  if (text == "-") {
    return result;
  }
  std::stringstream stream(text);
  std::string token;
  while (std::getline(stream, token, ',')) {
    if (!token.empty()) {
      result.push_back(std::stoi(token));
    }
  }
  return result;
}

Input read_input(const std::string& path) {
  std::ifstream stream(path);
  if (!stream) {
    throw std::runtime_error("cannot open input: " + path);
  }
  std::string line;
  std::getline(stream, line);
  const bool input_v8 = line == "EMUFLOW_TDM_RATIO_INPUT_V8";
  const bool input_v7 = line == "EMUFLOW_TDM_RATIO_INPUT_V7";
  const bool input_v4 = line == "EMUFLOW_TDM_RATIO_INPUT_V4";
  const bool input_v6 = line == "EMUFLOW_TDM_RATIO_INPUT_V6";
  const bool input_v5 = line == "EMUFLOW_TDM_RATIO_INPUT_V5";
  const bool input_v3 = input_v5 || line == "EMUFLOW_TDM_RATIO_INPUT_V3";
  if (!input_v8 && !input_v7 && !input_v6 && !input_v4 && !input_v5 && !input_v3 &&
      line != "EMUFLOW_TDM_RATIO_INPUT_V2") {
    throw std::runtime_error("invalid input header");
  }
  Input input;
  while (std::getline(stream, line)) {
    if (line.empty() || line[0] == '#') {
      continue;
    }
    std::stringstream record(line);
    std::string kind;
    record >> kind;
    if (kind == "PARAM") {
      if (input_v8 || input_v7) {
        record >> input.max_iterations >> input.max_ratio >>
            input.ratio_quantum >> input.min_ratio >>
            input.post_refinement_iterations >> input.exact_domain_limit >>
            input.convergence >> input.positive_scale >>
            input.negative_scale >> input.max_period;
      } else if (input_v3) {
        int harmonic_legalization = 0;
        record >> input.max_iterations >> input.max_ratio >>
            input.ratio_quantum >> input.min_ratio >>
            harmonic_legalization >> input.post_refinement_iterations >>
            input.exact_domain_limit >> input.convergence >>
            input.positive_scale >> input.negative_scale >> input.max_period;
        if (harmonic_legalization != 0 && harmonic_legalization != 1) {
          throw std::runtime_error(
              "harmonic legalization flag must be zero or one");
        }
        input.harmonic_legalization = harmonic_legalization != 0;
      } else {
        record >> input.max_iterations >> input.max_ratio >>
            input.ratio_quantum >> input.post_refinement_iterations >>
            input.exact_domain_limit >> input.convergence >>
            input.positive_scale >>
            input.negative_scale >> input.max_period;
      }
    } else if (kind == "DOMAIN") {
      int index = -1;
      Domain domain;
      record >> index >> domain.lanes;
      if (index != static_cast<int>(input.domains.size())) {
        throw std::runtime_error("DOMAIN indices must be contiguous");
      }
      input.domains.push_back(domain);
    } else if (kind == "HOP") {
      int index = -1;
      Hop hop;
      record >> index >> hop.domain >> hop.direction;
      if (input_v8 || input_v7 || input_v6) {
        record >> hop.compatibility;
      } else {
        hop.compatibility = 0;
      }
      record >> hop.base_delay_ns >> hop.beta_ns;
      if (index != static_cast<int>(input.hops.size())) {
        throw std::runtime_error("HOP indices must be contiguous");
      }
      input.hops.push_back(hop);
    } else if (kind == "PATH") {
      int index = -1;
      TimingPath timing_path;
      std::string hops;
      record >> index >> timing_path.clock_period_ns;
      if (input_v8) {
        record >> timing_path.required_time_ns;
      } else {
        timing_path.required_time_ns = timing_path.clock_period_ns;
      }
      record >> timing_path.fixed_delay_ns >> hops;
      timing_path.hops = parse_list(hops);
      if (index != static_cast<int>(input.paths.size())) {
        throw std::runtime_error("PATH indices must be contiguous");
      }
      input.paths.push_back(std::move(timing_path));
    } else if (kind == "SEED" &&
               (input_v8 || input_v7 || input_v6 || input_v4 || input_v5)) {
      int index = -1;
      double ratio = 0.0;
      record >> index >> ratio;
      if (index != static_cast<int>(input.seed_ratios.size())) {
        throw std::runtime_error("SEED indices must be contiguous");
      }
      input.seed_ratios.push_back(ratio);
    } else {
      throw std::runtime_error("unknown input record: " + kind);
    }
    if (!record) {
      throw std::runtime_error("malformed input record: " + line);
    }
  }
  if (input.domains.empty() || input.hops.empty() || input.paths.empty() ||
      input.max_ratio <= 0 || input.ratio_quantum <= 0 ||
      input.min_ratio <= 0 || input.min_ratio > input.max_ratio ||
      input.max_iterations <= 0 || input.post_refinement_iterations < 0 ||
      input.exact_domain_limit < 0 ||
      input.convergence <= 0.0 || input.positive_scale <= 0.0 ||
      input.negative_scale <= 0.0 || input.max_period <= 0.0) {
    throw std::runtime_error("incomplete ratio-optimization input");
  }
  if (input.max_ratio != 1 &&
      input.max_ratio % input.ratio_quantum != 0) {
    throw std::runtime_error(
        "maximum ratio must be one or a ratio-quantum multiple");
  }
  if (input.min_ratio != 1 &&
      input.min_ratio % input.ratio_quantum != 0) {
    throw std::runtime_error(
        "minimum ratio must be one or a ratio-quantum multiple");
  }
  for (const Domain& domain : input.domains) {
    if (domain.lanes <= 0) {
      throw std::runtime_error("domain lane count must be positive");
    }
  }
  for (const Hop& hop : input.hops) {
    if (hop.domain < 0 ||
        hop.domain >= static_cast<int>(input.domains.size()) ||
        hop.direction < 0 || hop.compatibility < 0 ||
        hop.beta_ns <= 0.0 ||
        hop.base_delay_ns < 0.0) {
      throw std::runtime_error("invalid hop");
    }
  }
  for (const TimingPath& timing_path : input.paths) {
    if (timing_path.clock_period_ns <= 0.0 ||
        timing_path.required_time_ns <= 0.0 ||
        timing_path.fixed_delay_ns < 0.0 || timing_path.hops.empty()) {
      throw std::runtime_error("invalid timing path");
    }
    std::set<int> unique_hops;
    for (int hop : timing_path.hops) {
      if (hop < 0 || hop >= static_cast<int>(input.hops.size()) ||
          !unique_hops.insert(hop).second) {
        throw std::runtime_error(
            "timing path contains an invalid or duplicate hop");
      }
    }
  }
  if ((input_v8 || input_v7 || input_v6 || input_v4 || input_v5) &&
      !input.seed_ratios.empty() &&
      input.seed_ratios.size() != input.hops.size()) {
    throw std::runtime_error("seeded input requires one SEED per hop");
  }
  for (double ratio : input.seed_ratios) {
    if (!std::isfinite(ratio) || ratio < input.min_ratio ||
        ratio > input.max_ratio) {
      throw std::runtime_error("invalid continuous seed ratio");
    }
  }
  return input;
}

class Optimizer {
 public:
  explicit Optimizer(Input input)
      : input_(std::move(input)),
        continuous_(input_.hops.size(), input_.min_ratio),
        discrete_(input_.hops.size(), input_.min_ratio),
        lane_(input_.hops.size(), -1),
        path_mu_(input_.paths.size(), 0.0),
        edge_mu_(input_.hops.size(), 0.0),
        hop_paths_(input_.hops.size()),
        domain_hops_(input_.domains.size()) {
    for (int hop = 0; hop < static_cast<int>(input_.hops.size()); ++hop) {
      domain_hops_[input_.hops[hop].domain].push_back(hop);
    }
    for (int path = 0; path < static_cast<int>(input_.paths.size()); ++path) {
      for (int hop : input_.paths[path].hops) {
        hop_paths_[hop].push_back(path);
      }
    }
    allowed_ratios_ = build_allowed_ratios();
    if (input_.seed_ratios.empty()) {
      initialize_path_multipliers();
    } else {
      continuous_ = input_.seed_ratios;
    }
  }

  void run() {
    if (input_.seed_ratios.empty()) {
      progress("lagrangian:start");
      double best_objective = -std::numeric_limits<double>::infinity();
      std::vector<double> best_ratios = continuous_;
      int stale = 0;
      for (int iteration = 0; iteration < input_.max_iterations;
           ++iteration) {
        aggregate_edge_multipliers();
        solve_kkt_ratios();
        const double objective = worst_normalized_slack(continuous_);
        if (objective > best_objective + input_.convergence) {
          best_objective = objective;
          best_ratios = continuous_;
          stale = 0;
        } else {
          ++stale;
        }
        completed_iterations_ = iteration + 1;
        if (stale >= 100) {
          break;
        }
        update_path_multipliers(iteration);
      }
      continuous_ = best_ratios;
      progress("lagrangian:done");
    } else {
      progress("external-continuous-seed");
    }
    legalize();
    progress("legalization:done");
    select_uniform_minimax_seed();
    progress("uniform-minimax:done");
    global_budget_minimax_refine();
    progress("global-minimax:done");
    group_minimax_refine();
    progress("group-minimax-1:done");
    post_refine();
    progress("post-refinement:done");
    group_minimax_refine();
    progress("group-minimax-2:done");
  }

  void write(const std::string& path) const {
    std::ofstream output(path);
    if (!output) {
      throw std::runtime_error("cannot open output: " + path);
    }
    output << "EMUFLOW_TDM_RATIO_OUTPUT_V1\n";
    output << std::setprecision(17);
    for (int index = 0; index < static_cast<int>(input_.hops.size()); ++index) {
      output << "HOP " << index << ' ' << continuous_[index] << ' '
             << discrete_[index] << ' ' << lane_[index] << '\n';
    }
    for (int index = 0; index < static_cast<int>(input_.paths.size()); ++index) {
      const auto [delay, slack, normalized] =
          path_metrics_discrete(input_.paths[index]);
      output << "PATH " << index << ' ' << delay << ' ' << slack << ' '
             << normalized << '\n';
    }
    output << "METRIC iterations " << completed_iterations_ << '\n';
    output << "METRIC continuous_worst_normalized_slack "
           << worst_normalized_slack(continuous_) << '\n';
    output << "METRIC discrete_worst_normalized_slack "
           << worst_normalized_slack_discrete() << '\n';
    output << "METRIC max_discrete_ratio "
           << *std::max_element(discrete_.begin(), discrete_.end()) << '\n';
    output << "METRIC post_refinement_swaps "
           << post_refinement_swaps_ << '\n';
    output << "METRIC dp_legalized_domains "
           << dp_legalized_domains_ << '\n';
    output << "METRIC greedy_legalized_domains "
           << greedy_legalized_domains_ << '\n';
    output << "METRIC group_minimax_improvements "
           << group_minimax_improvements_ << '\n';
    output << "METRIC global_minimax_improvements "
           << global_minimax_improvements_ << '\n';
    output << "METRIC global_minimax_weight_exponent "
           << global_minimax_weight_exponent_ << '\n';
  }

 private:
  void progress(const char* stage) const {
    if (std::getenv("EMUFLOW_TDM_PROGRESS") != nullptr) {
      std::cerr << "emuflow_tdm_ratio_optimizer: " << stage << '\n';
    }
  }

  double normalized_slack(const TimingPath& path, double slack) const {
    if (slack >= 0.0) {
      return slack * path.clock_period_ns /
          (input_.positive_scale * input_.max_period);
    }
    return slack / (input_.negative_scale * path.clock_period_ns);
  }

  std::tuple<double, double, double> path_metrics(
      const TimingPath& path, const std::vector<double>& ratios) const {
    double delay = path.fixed_delay_ns;
    for (int hop : path.hops) {
      delay += input_.hops[hop].base_delay_ns +
          input_.hops[hop].beta_ns * (ratios[hop] - 1.0);
    }
    const double slack = path.required_time_ns - delay;
    return {delay, slack, normalized_slack(path, slack)};
  }

  std::tuple<double, double, double> path_metrics_discrete(
      const TimingPath& path) const {
    double delay = path.fixed_delay_ns;
    for (int hop : path.hops) {
      delay += input_.hops[hop].base_delay_ns +
          input_.hops[hop].beta_ns * (discrete_[hop] - 1.0);
    }
    const double slack = path.required_time_ns - delay;
    return {delay, slack, normalized_slack(path, slack)};
  }

  std::tuple<double, double, double> path_metrics_swapped(
      const TimingPath& path, int lhs, int rhs) const {
    double delay = path.fixed_delay_ns;
    for (int hop : path.hops) {
      int ratio = discrete_[hop];
      if (hop == lhs) {
        ratio = discrete_[rhs];
      } else if (hop == rhs) {
        ratio = discrete_[lhs];
      }
      delay += input_.hops[hop].base_delay_ns +
          input_.hops[hop].beta_ns * (ratio - 1.0);
    }
    const double slack = path.required_time_ns - delay;
    return {delay, slack, normalized_slack(path, slack)};
  }

  double delay_normalization_scale(const TimingPath& path,
                                   double slack) const {
    if (slack >= 0.0) {
      return path.clock_period_ns /
          (input_.positive_scale * input_.max_period);
    }
    return 1.0 / (input_.negative_scale * path.clock_period_ns);
  }

  double worst_normalized_slack(const std::vector<double>& ratios) const {
    double worst = std::numeric_limits<double>::infinity();
    for (const TimingPath& path : input_.paths) {
      worst = std::min(worst, std::get<2>(path_metrics(path, ratios)));
    }
    return worst;
  }

  double worst_normalized_slack_discrete() const {
    std::vector<double> ratios(discrete_.begin(), discrete_.end());
    return worst_normalized_slack(ratios);
  }

  bool lexicographically_improves(
      const std::map<int, double>& candidate_metrics,
      const std::vector<double>& current_metrics) const {
    // Unaffected paths cancel.  At the smallest metric whose multiplicity
    // changes, a candidate is lexicographically better exactly when it has
    // fewer paths at that value.  This crosses plateaus with several tied
    // critical groups without ever worsening the sorted slack vector.
    std::map<double, int> multiplicity_delta;
    for (const auto& [path, candidate] : candidate_metrics) {
      --multiplicity_delta[current_metrics[path]];
      ++multiplicity_delta[candidate];
    }
    for (const auto& [metric, delta] : multiplicity_delta) {
      (void) metric;
      if (delta != 0) {
        return delta < 0;
      }
    }
    return false;
  }

  void initialize_path_multipliers() {
    // A normalized exponential distribution is a feasible dual path flow:
    // mu >= 0 and sum(mu) = 1. It favors initially critical paths.
    std::vector<double> scores;
    scores.reserve(input_.paths.size());
    const std::vector<double> unit_ratios(
        input_.hops.size(), static_cast<double>(input_.min_ratio));
    double maximum = -std::numeric_limits<double>::infinity();
    double minimum = std::numeric_limits<double>::infinity();
    for (const TimingPath& path : input_.paths) {
      const double normalized =
          std::get<2>(path_metrics(path, unit_ratios));
      const double score = -normalized;
      scores.push_back(score);
      maximum = std::max(maximum, score);
      minimum = std::min(minimum, score);
    }
    const double range = std::max(1.0, maximum - minimum);
    double total = 0.0;
    for (double& score : scores) {
      score = std::exp(std::max(-60.0, 8.0 * (score - maximum) / range));
      total += score;
    }
    for (int index = 0; index < static_cast<int>(scores.size()); ++index) {
      path_mu_[index] = scores[index] / total;
    }
  }

  void aggregate_edge_multipliers() {
    std::fill(edge_mu_.begin(), edge_mu_.end(), 0.0);
    for (int path = 0; path < static_cast<int>(input_.paths.size()); ++path) {
      const double slack =
          std::get<1>(path_metrics(input_.paths[path], continuous_));
      const double scale =
          delay_normalization_scale(input_.paths[path], slack);
      for (int hop : input_.paths[path].hops) {
        edge_mu_[hop] += path_mu_[path] * scale;
      }
    }
  }

  void solve_kkt_ratios() {
    for (int domain = 0; domain < static_cast<int>(input_.domains.size());
         ++domain) {
      const std::vector<int>& domain_hops = domain_hops_[domain];
      if (static_cast<double>(domain_hops.size()) / input_.min_ratio <=
          input_.domains[domain].lanes + kEps) {
        for (int hop : domain_hops) {
          continuous_[hop] = input_.min_ratio;
        }
        continue;
      }
      if (static_cast<double>(domain_hops.size()) / input_.max_ratio >
          input_.domains[domain].lanes + kEps) {
        throw std::runtime_error(
            "domain cannot fit within maximum continuous ratio");
      }
      std::vector<double> root_weights;
      root_weights.reserve(domain_hops.size());
      for (int hop : domain_hops) {
        root_weights.push_back(std::sqrt(
            input_.hops[hop].beta_ns *
            std::max(edge_mu_[hop], kEps)));
      }
      auto usage = [&](double root_lambda) {
        double usage = 0.0;
        for (double root_weight : root_weights) {
          const double ratio = std::clamp(
              root_lambda / root_weight,
              static_cast<double>(input_.min_ratio),
              static_cast<double>(input_.max_ratio));
          usage += 1.0 / ratio;
        }
        return usage;
      };
      double root_lambda =
          std::accumulate(
              root_weights.begin(), root_weights.end(), 0.0) /
          input_.domains[domain].lanes;
      std::vector<int> previous_status(domain_hops.size(), 2);
      bool active_set_converged = false;
      for (int round = 0; round < 32; ++round) {
        std::vector<int> status(domain_hops.size(), 0);
        double fixed_usage = 0.0;
        double free_root_sum = 0.0;
        for (int index = 0; index < static_cast<int>(domain_hops.size());
             ++index) {
          const double ratio = root_lambda / root_weights[index];
          if (ratio <= input_.min_ratio) {
            status[index] = -1;
            fixed_usage += 1.0 / input_.min_ratio;
          } else if (ratio >= input_.max_ratio) {
            status[index] = 1;
            fixed_usage += 1.0 / input_.max_ratio;
          } else {
            free_root_sum += root_weights[index];
          }
        }
        const double remaining =
            input_.domains[domain].lanes - fixed_usage;
        if (remaining <= 0.0 && free_root_sum > 0.0) {
          break;
        }
        const double updated =
            free_root_sum > 0.0
                ? free_root_sum / remaining
                : root_lambda;
        if (
            status == previous_status &&
            std::abs(updated - root_lambda) <=
                kEps * std::max(1.0, root_lambda)
        ) {
          root_lambda = updated;
          active_set_converged = true;
          break;
        }
        previous_status = std::move(status);
        root_lambda = updated;
      }
      if (
          !active_set_converged ||
          usage(root_lambda) >
              input_.domains[domain].lanes + 1.0e-9
      ) {
        double low = 0.0;
        double high = std::max(1.0, root_lambda);
        while (usage(high) > input_.domains[domain].lanes) {
          high *= 2.0;
          if (!std::isfinite(high)) {
            throw std::runtime_error("KKT lambda search diverged");
          }
        }
        for (int round = 0; round < 64; ++round) {
          const double middle = (low + high) * 0.5;
          if (usage(middle) > input_.domains[domain].lanes) {
            low = middle;
          } else {
            high = middle;
          }
        }
        root_lambda = high;
      }
      for (int index = 0; index < static_cast<int>(domain_hops.size());
           ++index) {
        const int hop = domain_hops[index];
        continuous_[hop] = std::clamp(
            root_lambda / root_weights[index],
            static_cast<double>(input_.min_ratio),
            static_cast<double>(input_.max_ratio));
      }
    }
  }

  void update_path_multipliers(int iteration) {
    std::vector<double> costs(input_.paths.size());
    double maximum = -std::numeric_limits<double>::infinity();
    double minimum = std::numeric_limits<double>::infinity();
    for (int path = 0; path < static_cast<int>(input_.paths.size()); ++path) {
      const double normalized =
          std::get<2>(path_metrics(input_.paths[path], continuous_));
      costs[path] = -normalized;
      maximum = std::max(maximum, costs[path]);
      minimum = std::min(minimum, costs[path]);
    }
    const double rate = 0.2 * std::pow(0.5, 0.01 * iteration);
    const double range = std::max(1.0, maximum - minimum);
    double total = 0.0;
    for (int path = 0; path < static_cast<int>(costs.size()); ++path) {
      const double exponent =
          std::clamp(
              8.0 * rate * (costs[path] - maximum) / range, -60.0, 0.0);
      path_mu_[path] *= std::exp(exponent);
      path_mu_[path] = std::max(path_mu_[path], kEps);
      total += path_mu_[path];
    }
    for (double& value : path_mu_) {
      value /= total;
    }
  }

  std::vector<int> build_allowed_ratios() const {
    std::vector<int> result;
    if (input_.min_ratio == 1) {
      result.push_back(1);
    }
    const int first = input_.min_ratio == 1
        ? input_.ratio_quantum
        : input_.min_ratio;
    for (int ratio = first; ratio <= input_.max_ratio;
         ratio += input_.ratio_quantum) {
      result.push_back(ratio);
    }
    return result;
  }

  static void stable_radix_sort_bounds(
      std::vector<std::pair<int, int>>& values) {
    if (values.size() < 4096) {
      std::stable_sort(values.begin(), values.end());
      return;
    }
    constexpr int kRadixBits = 16;
    constexpr int kRadixSize = 1 << kRadixBits;
    constexpr int kRadixMask = kRadixSize - 1;
    std::vector<std::pair<int, int>> scratch(values.size());
    std::vector<std::size_t> offsets(kRadixSize);
    for (int shift : {0, kRadixBits}) {
      std::fill(offsets.begin(), offsets.end(), 0);
      for (const auto& value : values) {
        ++offsets[(value.first >> shift) & kRadixMask];
      }
      std::size_t prefix = 0;
      for (std::size_t& offset : offsets) {
        const std::size_t count = offset;
        offset = prefix;
        prefix += count;
      }
      for (const auto& value : values) {
        scratch[offsets[(value.first >> shift) & kRadixMask]++] = value;
      }
      values.swap(scratch);
    }
  }

  double harmonic_usage(int domain, int changed_hop = -1,
                        int changed_ratio = -1) const {
    double usage = 0.0;
    for (int hop : domain_hops_[domain]) {
      const int ratio = hop == changed_hop ? changed_ratio : discrete_[hop];
      usage += 1.0 / static_cast<double>(ratio);
    }
    return usage;
  }

  void legalize_harmonic() {
    for (int domain = 0; domain < static_cast<int>(input_.domains.size());
         ++domain) {
      for (int hop : domain_hops_[domain]) {
        int ratio = static_cast<int>(std::ceil(
            continuous_[hop] / input_.ratio_quantum - kEps)) *
            input_.ratio_quantum;
        ratio = std::clamp(ratio, input_.min_ratio, input_.max_ratio);
        discrete_[hop] = ratio;
        lane_[hop] = 0;
      }
      if (harmonic_usage(domain) > input_.domains[domain].lanes + 1.0e-9) {
        throw std::runtime_error(
            "harmonic discrete legalization exceeded domain capacity");
      }
    }
  }

  int groups_for_bound(const std::vector<int>& ordered,
                       const std::vector<int>& allowed, double bound,
                       bool assign, int lane_offset = 0) {
    int position = 0;
    int lane = 0;
    while (position < static_cast<int>(ordered.size())) {
      const double continuous = continuous_[ordered[position]];
      int choice = -1;
      const auto upper = std::upper_bound(
          allowed.begin(), allowed.end(), continuous + bound + kEps);
      if (upper != allowed.begin()) {
        const int candidate = *std::prev(upper);
        if (std::abs(candidate - continuous) <= bound + kEps) {
          choice = candidate;
        }
      }
      if (choice < 0) {
        return std::numeric_limits<int>::max();
      }
      const int end = std::min(
          static_cast<int>(ordered.size()), position + choice);
      int compatible_end = position + 1;
      while (compatible_end < end &&
             std::abs(continuous_[ordered[compatible_end]] - choice) <=
                 bound + kEps) {
        ++compatible_end;
      }
      if (assign) {
        for (int index = position; index < compatible_end; ++index) {
          discrete_[ordered[index]] = choice;
          lane_[ordered[index]] = lane_offset + lane;
        }
      }
      position = compatible_end;
      ++lane;
    }
    return lane;
  }

  bool exact_displacement_dp(
      const std::map<std::pair<int, int>, std::vector<int>>&
          ordered_by_direction,
      const std::vector<int>& allowed, double bound, int lane_budget) {
    std::vector<int> ordered;
    std::vector<std::pair<int, int>> direction;
    for (const auto& [direction_id, group] : ordered_by_direction) {
      for (int hop : group) {
        ordered.push_back(hop);
        direction.push_back(direction_id);
      }
    }
    // The TODAES 2020 DP is exact.  Precompute the best legal ratio and total
    // displacement for every contiguous interval, then solve the lane
    // segmentation in O(lanes * signals^2).  Earlier code enumerated every
    // ratio and every interval end inside the DP and therefore had to stop at
    // 256 signals.  The interval formulation keeps exactness while covering
    // the hundreds-to-low-thousands signal domains seen in real designs.
    if (ordered.empty() ||
        static_cast<int>(ordered.size()) > input_.exact_domain_limit) {
      return false;
    }

    const int count = static_cast<int>(ordered.size());
    std::vector<double> values;
    values.reserve(count);
    for (int hop : ordered) {
      values.push_back(continuous_[hop]);
    }
    std::vector<double> prefix(count + 1, 0.0);
    for (int index = 0; index < count; ++index) {
      prefix[index + 1] = prefix[index] + values[index];
    }
    const double infinity = std::numeric_limits<double>::infinity();
    std::vector<std::vector<double>> interval_cost(
        count, std::vector<double>(count + 1, infinity));
    std::vector<std::vector<int>> interval_ratio(
        count, std::vector<int>(count + 1, -1));
    for (int start = 0; start < count; ++start) {
      for (int end = start + 1; end <= count; ++end) {
        if (direction[end - 1] != direction[start]) {
          break;
        }
        const int length = end - start;
        if (length > allowed.back()) {
          break;
        }
        const double lower = std::max(
            static_cast<double>(length), values[end - 1] - bound);
        const double upper = values[start] + bound;
        if (lower > upper + kEps) {
          continue;
        }
        auto first = std::lower_bound(
            allowed.begin(), allowed.end(), lower - kEps);
        auto last = std::upper_bound(
            allowed.begin(), allowed.end(), upper + kEps);
        if (first == allowed.end() || first >= last) {
          continue;
        }

        const double median = values[(start + end - 1) / 2];
        std::set<int> candidates = {*first, *std::prev(last)};
        auto near = std::lower_bound(first, last, median);
        if (near != last) {
          candidates.insert(*near);
        }
        if (near != first) {
          candidates.insert(*std::prev(near));
        }
        for (int ratio : candidates) {
          const auto split_iterator = std::upper_bound(
              values.begin() + start, values.begin() + end,
              static_cast<double>(ratio));
          const int split = static_cast<int>(
              split_iterator - values.begin());
          const double left =
              ratio * static_cast<double>(split - start) -
              (prefix[split] - prefix[start]);
          const double right =
              (prefix[end] - prefix[split]) -
              ratio * static_cast<double>(end - split);
          const double cost = left + right;
          if (cost + kEps < interval_cost[start][end] ||
              (std::abs(cost - interval_cost[start][end]) <= kEps &&
               (interval_ratio[start][end] < 0 ||
                ratio < interval_ratio[start][end]))) {
            interval_cost[start][end] = cost;
            interval_ratio[start][end] = ratio;
          }
        }
      }
    }

    std::vector<std::vector<double>> dp(
        count + 1, std::vector<double>(lane_budget + 1, infinity));
    struct Choice {
      int previous = -1;
      int ratio = -1;
    };
    std::vector<std::vector<Choice>> parent(
        count + 1, std::vector<Choice>(lane_budget + 1));
    dp[0][0] = 0.0;
    for (int position = 0; position < count; ++position) {
      for (int used = 0; used < lane_budget; ++used) {
        if (!std::isfinite(dp[position][used])) {
          continue;
        }
        for (int next = position + 1; next <= count; ++next) {
          const double displacement = interval_cost[position][next];
          if (!std::isfinite(displacement)) {
            if (direction[next - 1] != direction[position]) {
              break;
            }
            continue;
          }
          const int ratio = interval_ratio[position][next];
          const double candidate =
              dp[position][used] + displacement;
          const Choice old = parent[next][used + 1];
          if (candidate + kEps < dp[next][used + 1] ||
              (std::abs(candidate - dp[next][used + 1]) <= kEps &&
               (old.ratio < 0 || ratio < old.ratio))) {
            dp[next][used + 1] = candidate;
            parent[next][used + 1] = {position, ratio};
          }
        }
      }
    }
    int best_lanes = -1;
    double best_cost = infinity;
    for (int used = 1; used <= lane_budget; ++used) {
      if (dp[count][used] + kEps < best_cost ||
          (std::abs(dp[count][used] - best_cost) <= kEps &&
           (best_lanes < 0 || used < best_lanes))) {
        best_cost = dp[count][used];
        best_lanes = used;
      }
    }
    if (best_lanes < 0 || !std::isfinite(best_cost)) {
      return false;
    }

    int position = count;
    int used = best_lanes;
    while (position > 0) {
      const Choice choice = parent[position][used];
      if (choice.previous < 0 || choice.ratio < 0) {
        throw std::runtime_error(
            "exact displacement DP has a broken parent chain");
      }
      for (int index = choice.previous; index < position; ++index) {
        discrete_[ordered[index]] = choice.ratio;
        lane_[ordered[index]] = used - 1;
      }
      position = choice.previous;
      --used;
    }
    ++dp_legalized_domains_;
    return true;
  }

  void legalize() {
    if (input_.harmonic_legalization) {
      legalize_harmonic();
      return;
    }
    const std::vector<int>& allowed = allowed_ratios_;
    if (allowed.back() != input_.max_ratio) {
      throw std::runtime_error(
          "maximum ratio must be 1 or a multiple of ratio quantum");
    }
    for (int domain = 0; domain < static_cast<int>(input_.domains.size());
         ++domain) {
      std::map<std::pair<int, int>, std::vector<int>> ordered_by_direction;
      for (int hop : domain_hops_[domain]) {
        ordered_by_direction[{input_.hops[hop].direction, input_.hops[hop].compatibility}].push_back(hop);
      }
      int total_hops = 0;
      for (auto& [direction, ordered] : ordered_by_direction) {
        (void) direction;
        total_hops += ordered.size();
        std::stable_sort(
            ordered.begin(), ordered.end(), [&](int lhs, int rhs) {
              if (continuous_[lhs] != continuous_[rhs]) {
                return continuous_[lhs] < continuous_[rhs];
              }
              return lhs < rhs;
            });
      }
      if (static_cast<long long>(total_hops) >
          static_cast<long long>(input_.domains[domain].lanes) *
              input_.max_ratio) {
        throw std::runtime_error("domain cannot fit within maximum ratio");
      }
      auto group_count = [&](double bound) {
        int total = 0;
        for (const auto& [direction, ordered] : ordered_by_direction) {
          (void) direction;
          const int groups =
              groups_for_bound(ordered, allowed, bound, false);
          if (groups == std::numeric_limits<int>::max()) {
            return groups;
          }
          total += groups;
        }
        return total;
      };
      double low = 0.0;
      double high = input_.max_ratio;
      if (group_count(high) > input_.domains[domain].lanes) {
        throw std::runtime_error(
            "direction-separated groups cannot fit domain lane budget");
      }
      for (int round = 0; round < 64; ++round) {
        const double middle = (low + high) * 0.5;
        if (group_count(middle) <= input_.domains[domain].lanes) {
          high = middle;
        } else {
          low = middle;
        }
      }
      if (exact_displacement_dp(
              ordered_by_direction, allowed, high + 1.0e-8,
              input_.domains[domain].lanes)) {
        continue;
      }
      ++greedy_legalized_domains_;
      int lane_offset = 0;
      for (const auto& [direction, ordered] : ordered_by_direction) {
        (void) direction;
        const int groups = groups_for_bound(
            ordered, allowed, high, true, lane_offset);
        lane_offset += groups;
      }
      if (lane_offset > input_.domains[domain].lanes) {
        throw std::runtime_error("discrete legalization exceeded lane budget");
      }
    }
  }

  bool build_minimax_groups(int domain, double target, bool assign) {
    const std::vector<int>& allowed = allowed_ratios_;
    std::map<std::pair<int, int>, std::vector<std::pair<int, int>>> by_direction;
    for (int hop : domain_hops_[domain]) {
      int maximum = input_.max_ratio;
      for (int path_index : hop_paths_[hop]) {
        const TimingPath& path = input_.paths[path_index];
        int domain_hops = 0;
        for (int path_hop : path.hops) {
          if (input_.hops[path_hop].domain == domain) {
            ++domain_hops;
          }
        }
        if (domain_hops != 1) {
          return false;
        }
        double other_delay = path.fixed_delay_ns;
        for (int path_hop : path.hops) {
          if (path_hop == hop) {
            continue;
          }
          other_delay += input_.hops[path_hop].base_delay_ns +
              input_.hops[path_hop].beta_ns *
                  (discrete_[path_hop] - 1.0);
        }
        const auto meets_target = [&](int ratio) {
          const double delay = other_delay + input_.hops[hop].base_delay_ns +
              input_.hops[hop].beta_ns * (ratio - 1.0);
          const double slack = path.required_time_ns - delay;
          return normalized_slack(path, slack) + input_.convergence >= target;
        };
        int low = 0;
        int high = static_cast<int>(allowed.size());
        while (low < high) {
          const int middle = low + (high - low) / 2;
          if (meets_target(allowed[middle])) {
            low = middle + 1;
          } else {
            high = middle;
          }
        }
        const int path_maximum = low == 0 ? -1 : allowed[low - 1];
        if (path_maximum < input_.min_ratio) {
          return false;
        }
        maximum = std::min(maximum, path_maximum);
      }
      by_direction[{input_.hops[hop].direction, input_.hops[hop].compatibility}].emplace_back(maximum, hop);
    }

    struct Group {
      std::pair<int, int> direction{0, 0};
      std::vector<std::pair<int, int>> items;
      int ratio = 0;
    };
    const auto minimum_ratio_for_count = [&](int count) {
      const auto found = std::lower_bound(allowed.begin(), allowed.end(), count);
      return found == allowed.end() ? input_.max_ratio + 1 : *found;
    };
    std::vector<Group> groups;
    for (auto& [direction, bounded] : by_direction) {
      stable_radix_sort_bounds(bounded);
      int position = 0;
      while (position < static_cast<int>(bounded.size())) {
        const int bound = bounded[position].first;
        const auto upper = std::upper_bound(
            allowed.begin(), allowed.end(), bound);
        if (upper == allowed.begin()) {
          return false;
        }
        const int group_capacity = *std::prev(upper);
        const int count = std::min(
            group_capacity,
            static_cast<int>(bounded.size()) - position);
        const int ratio = minimum_ratio_for_count(count);
        if (ratio > bound) {
          return false;
        }
        Group group;
        group.direction = direction;
        group.ratio = ratio;
        group.items.insert(
            group.items.end(), bounded.begin() + position,
            bounded.begin() + position + count);
        groups.push_back(std::move(group));
        position += count;
      }
    }
    const int lane_budget = input_.domains[domain].lanes;
    if (static_cast<int>(groups.size()) > lane_budget) {
      return false;
    }

    // The initial bounded packing above proves feasibility but deliberately
    // uses the fewest lanes.  Leaving legal lanes idle can inflate a group's
    // ratio by hundreds on contest-scale domains.  Repeatedly split the
    // current worst group so that every available lane contributes to the
    // minimax objective while retaining each group's direction constraint.
    while (static_cast<int>(groups.size()) < lane_budget) {
      int selected = -1;
      for (int index = 0; index < static_cast<int>(groups.size()); ++index) {
        if (groups[index].items.size() <= 1) {
          continue;
        }
        if (selected < 0 ||
            std::make_tuple(groups[index].ratio, groups[index].items.size()) >
                std::make_tuple(groups[selected].ratio,
                                groups[selected].items.size())) {
          selected = index;
        }
      }
      if (selected < 0) {
        break;
      }
      Group right;
      right.direction = groups[selected].direction;
      const int middle = static_cast<int>(groups[selected].items.size()) / 2;
      right.items.insert(
          right.items.end(), groups[selected].items.begin() + middle,
          groups[selected].items.end());
      groups[selected].items.erase(
          groups[selected].items.begin() + middle,
          groups[selected].items.end());
      groups[selected].ratio = minimum_ratio_for_count(
          static_cast<int>(groups[selected].items.size()));
      right.ratio = minimum_ratio_for_count(
          static_cast<int>(right.items.size()));
      groups.insert(groups.begin() + selected + 1, std::move(right));
    }

    if (assign) {
      for (int lane = 0; lane < static_cast<int>(groups.size()); ++lane) {
        for (const auto& [bound, hop] : groups[lane].items) {
          if (groups[lane].ratio > bound) {
            throw std::runtime_error("split minimax group exceeds bound");
          }
          discrete_[hop] = groups[lane].ratio;
          lane_[hop] = lane;
        }
      }
    }
    return true;
  }

  double delay_limit_for_normalized_target(
      const TimingPath& path, double target) const {
    const double required_slack = target >= 0.0
        ? target * input_.positive_scale * input_.max_period /
              path.clock_period_ns
        : target * input_.negative_scale * path.clock_period_ns;
    return path.required_time_ns - required_slack;
  }

  bool build_global_budget_solution(double target, double weight_exponent) {
    const std::vector<int>& allowed = allowed_ratios_;
    std::vector<int> maximum(input_.hops.size(), input_.max_ratio);
    for (const TimingPath& path : input_.paths) {
      double minimum_delay = path.fixed_delay_ns;
      double total_weight = 0.0;
      for (int hop : path.hops) {
        minimum_delay += input_.hops[hop].base_delay_ns +
            input_.hops[hop].beta_ns * (input_.min_ratio - 1.0);
        total_weight += std::pow(
            std::max(1.0, continuous_[hop] - input_.min_ratio + 1.0),
            weight_exponent);
      }
      const double budget =
          delay_limit_for_normalized_target(path, target) - minimum_delay;
      if (budget < -input_.convergence) {
        return false;
      }
      if (path.hops.empty()) {
        continue;
      }
      for (int hop : path.hops) {
        const double weight = std::pow(
            std::max(1.0, continuous_[hop] - input_.min_ratio + 1.0),
            weight_exponent);
        const double raw_bound = input_.min_ratio +
            std::max(0.0, budget) * weight /
                (total_weight * input_.hops[hop].beta_ns);
        const auto upper = std::upper_bound(
            allowed.begin(), allowed.end(),
            static_cast<int>(std::floor(raw_bound + input_.convergence)));
        if (upper == allowed.begin()) {
          return false;
        }
        maximum[hop] = std::min(maximum[hop], *std::prev(upper));
      }
    }

    for (int hop = 0; hop < static_cast<int>(input_.hops.size()); ++hop) {
      discrete_[hop] = maximum[hop];
    }
    for (int domain = 0; domain < static_cast<int>(input_.domains.size());
         ++domain) {
      if (!build_minimax_groups(domain, target, true)) {
        return false;
      }
    }
    return worst_normalized_slack_discrete() + input_.convergence >= target;
  }

  void global_budget_minimax_refine() {
    if (input_.harmonic_legalization || input_.paths.empty()) {
      return;
    }
    const double before = worst_normalized_slack_discrete();
    std::vector<int> best_ratio = discrete_;
    std::vector<int> best_lane = lane_;
    double best = before;

    std::vector<int> minimum(input_.hops.size(), input_.min_ratio);
    const double upper = worst_normalized_slack(
        std::vector<double>(minimum.begin(), minimum.end()));
    for (double exponent : {-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0}) {
      discrete_ = best_ratio;
      lane_ = best_lane;
      if (!build_global_budget_solution(before, exponent)) {
        continue;
      }
      double low = before;
      double high = upper;
      std::vector<int> scheme_ratio = discrete_;
      std::vector<int> scheme_lane = lane_;
      for (int iteration = 0; iteration < 28; ++iteration) {
        const double middle = (low + high) * 0.5;
        if (build_global_budget_solution(middle, exponent)) {
          low = middle;
          scheme_ratio = discrete_;
          scheme_lane = lane_;
        } else {
          high = middle;
        }
      }
      discrete_ = scheme_ratio;
      lane_ = scheme_lane;
      const double result = worst_normalized_slack_discrete();
      if (result > best + input_.convergence) {
        best = result;
        best_ratio = discrete_;
        best_lane = lane_;
        global_minimax_weight_exponent_ = exponent;
      }
    }
    discrete_ = best_ratio;
    lane_ = best_lane;
    if (best > before + input_.convergence) {
      ++global_minimax_improvements_;
    }
  }

  void group_minimax_refine() {
    if (input_.harmonic_legalization) {
      return;
    }
    for (int round = 0; round < 4; ++round) {
      bool changed = false;
      for (int domain = 0; domain < static_cast<int>(input_.domains.size());
           ++domain) {
        const double before = worst_normalized_slack_discrete();
        std::vector<int> saved_ratio;
        std::vector<int> saved_lane;
        for (int hop : domain_hops_[domain]) {
          saved_ratio.push_back(discrete_[hop]);
          saved_lane.push_back(lane_[hop]);
        }
        double upper = std::numeric_limits<double>::infinity();
        for (int hop : domain_hops_[domain]) {
          discrete_[hop] = input_.min_ratio;
        }
        upper = worst_normalized_slack_discrete();
        for (int index = 0;
             index < static_cast<int>(domain_hops_[domain].size()); ++index) {
          discrete_[domain_hops_[domain][index]] = saved_ratio[index];
        }
        if (upper <= before + input_.convergence ||
            !build_minimax_groups(domain, before, false)) {
          continue;
        }
        double low = before;
        double high = upper;
        for (int iteration = 0; iteration < 32; ++iteration) {
          const double middle = (low + high) * 0.5;
          if (build_minimax_groups(domain, middle, false)) {
            low = middle;
          } else {
            high = middle;
          }
        }
        if (!build_minimax_groups(domain, low, true)) {
          throw std::runtime_error("minimax group reconstruction failed");
        }
        const double after = worst_normalized_slack_discrete();
        if (after + input_.convergence < before) {
          for (int index = 0;
               index < static_cast<int>(domain_hops_[domain].size()); ++index) {
            const int hop = domain_hops_[domain][index];
            discrete_[hop] = saved_ratio[index];
            lane_[hop] = saved_lane[index];
          }
          continue;
        }
        if (after > before + input_.convergence) {
          ++group_minimax_improvements_;
          changed = true;
        }
      }
      if (!changed) {
        break;
      }
    }
  }

  int uniform_ratio(int signals, int lanes) const {
    if (signals <= lanes && input_.min_ratio == 1) {
      return 1;
    }
    const int raw = std::max(
        input_.min_ratio, (signals + lanes - 1) / lanes);
    return ((raw + input_.ratio_quantum - 1) /
            input_.ratio_quantum) * input_.ratio_quantum;
  }

  bool assign_uniform_minimax_domain(int domain) {
    std::map<std::pair<int, int>, std::vector<int>> by_direction;
    for (int hop : domain_hops_[domain]) {
      by_direction[{input_.hops[hop].direction, input_.hops[hop].compatibility}].push_back(hop);
    }
    if (by_direction.empty() || by_direction.size() > 2) {
      return by_direction.empty();
    }
    std::vector<std::pair<std::pair<int, int>, std::vector<int>>> groups(
        by_direction.begin(), by_direction.end());
    const int budget = input_.domains[domain].lanes;
    std::vector<int> lane_budget(groups.size(), budget);
    if (groups.size() == 2) {
      std::tuple<int, int, int> best{
          std::numeric_limits<int>::max(),
          std::numeric_limits<int>::max(), -1};
      for (int first = 1; first < budget; ++first) {
        const int second = budget - first;
        const int first_ratio = uniform_ratio(
            static_cast<int>(groups[0].second.size()), first);
        const int second_ratio = uniform_ratio(
            static_cast<int>(groups[1].second.size()), second);
        if (first_ratio > input_.max_ratio ||
            second_ratio > input_.max_ratio) {
          continue;
        }
        const std::tuple<int, int, int> score{
            std::max(first_ratio, second_ratio),
            first_ratio + second_ratio, first};
        if (score < best) {
          best = score;
        }
      }
      if (std::get<2>(best) < 0) {
        return false;
      }
      lane_budget = {std::get<2>(best), budget - std::get<2>(best)};
    }
    int lane = 0;
    for (int index = 0; index < static_cast<int>(groups.size()); ++index) {
      std::vector<int>& hops = groups[index].second;
      std::sort(hops.begin(), hops.end());
      const int ratio = uniform_ratio(
          static_cast<int>(hops.size()), lane_budget[index]);
      if (ratio > input_.max_ratio) {
        return false;
      }
      for (int position = 0; position < static_cast<int>(hops.size());) {
        const int end = std::min(
            static_cast<int>(hops.size()), position + ratio);
        for (int item = position; item < end; ++item) {
          discrete_[hops[item]] = ratio;
          lane_[hops[item]] = lane;
        }
        position = end;
        ++lane;
      }
    }
    return lane <= budget;
  }

  void select_uniform_minimax_seed() {
    if (input_.harmonic_legalization) {
      return;
    }
    const std::vector<int> saved_ratio = discrete_;
    const std::vector<int> saved_lane = lane_;
    const double before = worst_normalized_slack_discrete();
    for (int domain = 0; domain < static_cast<int>(input_.domains.size());
         ++domain) {
      if (!assign_uniform_minimax_domain(domain)) {
        discrete_ = saved_ratio;
        lane_ = saved_lane;
        return;
      }
    }
    if (worst_normalized_slack_discrete() + input_.convergence < before) {
      discrete_ = saved_ratio;
      lane_ = saved_lane;
    }
  }

  void post_refine() {
    std::vector<double> metrics(input_.paths.size());
    std::set<std::pair<double, int>> ordered_metrics;
    using DomainDirection = std::tuple<int, int, int>;
    using RatioHop = std::pair<int, int>;
    std::map<DomainDirection, std::set<RatioHop>> ratio_hops;
    for (int hop = 0; hop < static_cast<int>(input_.hops.size()); ++hop) {
      ratio_hops[{input_.hops[hop].domain, input_.hops[hop].direction, input_.hops[hop].compatibility}]
          .emplace(discrete_[hop], hop);
    }
    for (int path = 0; path < static_cast<int>(input_.paths.size()); ++path) {
      metrics[path] =
          std::get<2>(path_metrics_discrete(input_.paths[path]));
      ordered_metrics.emplace(metrics[path], path);
    }
    const auto candidate_worst = [&](const std::map<int, double>& candidate) {
      double result = std::numeric_limits<double>::infinity();
      auto unaffected = ordered_metrics.begin();
      while (unaffected != ordered_metrics.end() &&
             candidate.find(unaffected->second) != candidate.end()) {
        ++unaffected;
      }
      if (unaffected != ordered_metrics.end()) {
        result = unaffected->first;
      }
      for (const auto& [path, value] : candidate) {
        (void) path;
        result = std::min(result, value);
      }
      return result;
    };
    const auto accept_metrics = [&](const std::map<int, double>& candidate) {
      for (const auto& [path, value] : candidate) {
        const std::size_t erased =
            ordered_metrics.erase({metrics[path], path});
        if (erased != 1) {
          throw std::runtime_error(
              "post-refinement metric index lost path identity");
        }
        metrics[path] = value;
        ordered_metrics.emplace(value, path);
      }
    };
    for (int iteration = 0;
         iteration < input_.post_refinement_iterations; ++iteration) {
      const int critical_path = ordered_metrics.begin()->second;
      const double current_worst = metrics[critical_path];
      std::vector<int> critical_hops = input_.paths[critical_path].hops;
      std::stable_sort(
          critical_hops.begin(), critical_hops.end(), [&](int lhs, int rhs) {
            if (discrete_[lhs] != discrete_[rhs]) {
              return discrete_[lhs] > discrete_[rhs];
            }
            return lhs < rhs;
          });
      bool improved = false;
      if (input_.harmonic_legalization) {
        for (int hop : critical_hops) {
          const int candidate_ratio =
              discrete_[hop] - input_.ratio_quantum;
          if (candidate_ratio < input_.min_ratio ||
              harmonic_usage(
                  input_.hops[hop].domain, hop, candidate_ratio) >
                  input_.domains[input_.hops[hop].domain].lanes + 1.0e-9) {
            continue;
          }
          std::set<int> affected(
              hop_paths_[hop].begin(), hop_paths_[hop].end());
          std::map<int, double> candidate_metrics;
          const int previous_ratio = discrete_[hop];
          discrete_[hop] = candidate_ratio;
          for (int path : affected) {
            candidate_metrics[path] = std::get<2>(
                path_metrics_discrete(input_.paths[path]));
          }
          discrete_[hop] = previous_ratio;
          const double worst = candidate_worst(candidate_metrics);
          if (worst > current_worst + input_.convergence ||
              (std::abs(worst - current_worst) <=
                   input_.convergence &&
               lexicographically_improves(candidate_metrics, metrics))) {
            auto& ordered = ratio_hops[
                {input_.hops[hop].domain, input_.hops[hop].direction, input_.hops[hop].compatibility}];
            ordered.erase({previous_ratio, hop});
            ordered.emplace(candidate_ratio, hop);
            discrete_[hop] = candidate_ratio;
            accept_metrics(candidate_metrics);
            ++post_refinement_swaps_;
            improved = true;
            break;
          }
        }
      }
      if (improved) {
        continue;
      }
      for (int lhs : critical_hops) {
        auto& ordered = ratio_hops[
            {input_.hops[lhs].domain, input_.hops[lhs].direction, input_.hops[lhs].compatibility}];
        const auto candidate_end = ordered.lower_bound({discrete_[lhs], -1});
        for (auto candidate = ordered.begin(); candidate != candidate_end;
             ++candidate) {
          const int rhs = candidate->second;
          std::set<int> affected(
              hop_paths_[lhs].begin(), hop_paths_[lhs].end());
          affected.insert(hop_paths_[rhs].begin(), hop_paths_[rhs].end());
          std::map<int, double> candidate_metrics;
          for (int path : affected) {
            candidate_metrics[path] = std::get<2>(
                path_metrics_swapped(input_.paths[path], lhs, rhs));
          }
          const double worst = candidate_worst(candidate_metrics);
          if (worst > current_worst + input_.convergence ||
              (std::abs(worst - current_worst) <=
                   input_.convergence &&
               lexicographically_improves(candidate_metrics, metrics))) {
            const int lhs_ratio = discrete_[lhs];
            const int rhs_ratio = discrete_[rhs];
            ordered.erase({lhs_ratio, lhs});
            ordered.erase({rhs_ratio, rhs});
            ordered.emplace(rhs_ratio, lhs);
            ordered.emplace(lhs_ratio, rhs);
            std::swap(discrete_[lhs], discrete_[rhs]);
            std::swap(lane_[lhs], lane_[rhs]);
            accept_metrics(candidate_metrics);
            ++post_refinement_swaps_;
            improved = true;
            break;
          }
        }
        if (improved) {
          break;
        }
      }
      if (!improved) {
        break;
      }
    }
  }

  Input input_;
  std::vector<double> continuous_;
  std::vector<int> discrete_;
  std::vector<int> lane_;
  std::vector<double> path_mu_;
  std::vector<double> edge_mu_;
  std::vector<std::vector<int>> hop_paths_;
  std::vector<std::vector<int>> domain_hops_;
  std::vector<int> allowed_ratios_;
  int completed_iterations_ = 0;
  int post_refinement_swaps_ = 0;
  int dp_legalized_domains_ = 0;
  int greedy_legalized_domains_ = 0;
  int group_minimax_improvements_ = 0;
  int global_minimax_improvements_ = 0;
  double global_minimax_weight_exponent_ = 0.0;
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
    Optimizer optimizer(read_input(argv[1]));
    optimizer.run();
    optimizer.write(argv[2]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_tdm_ratio_optimizer: " << error.what() << '\n';
    return 1;
  }
}
