// SPDX-License-Identifier: Apache-2.0
//
// Topology-constrained FM refinement for multi-FPGA partitioning.
//
// This is a source-complete post-partition legalizer inspired by TopoPart
// (ICCAD 2021) and the cut-violation-correction refinement described at DATE
// 2024.  It uses an FM-style tentative move sequence with best-prefix rollback.
// The objective is lexicographic: eliminate hop-limit violations first, then
// unreachable endpoints, weighted excess hops, weighted total hops, and cut
// weight.  Every move preserves fixed vertices, minimum used partitions,
// physical capacity, and independently supplied multi-resource balance bounds.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include "emuflow_core/partition_hops.hpp"
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace {

constexpr double kEps = 1.0e-9;

using Cluster=emuflow::core::PartitionHopCluster;
using Net=emuflow::core::PartitionHopNet;
using Input=emuflow::core::PartitionHopModel;

struct Objective {
  long long violations = 0;
  long long unreachable = 0;
  double weighted_excess = 0.0;
  double weighted_hops = 0.0;
  double cut_weight = 0.0;
  int used_part_deficit = 0;
};

bool less_objective(const Objective& left, const Objective& right) {
  if (left.violations != right.violations) {
    return left.violations < right.violations;
  }
  if (left.unreachable != right.unreachable) {
    return left.unreachable < right.unreachable;
  }
  if (std::abs(left.weighted_excess - right.weighted_excess) > kEps) {
    return left.weighted_excess < right.weighted_excess;
  }
  if (std::abs(left.weighted_hops - right.weighted_hops) > kEps) {
    return left.weighted_hops < right.weighted_hops;
  }
  if (std::abs(left.cut_weight - right.cut_weight) > kEps) {
    return left.cut_weight < right.cut_weight;
  }
  return left.used_part_deficit < right.used_part_deficit;
}

bool equal_objective(const Objective& left, const Objective& right) {
  return left.violations == right.violations &&
         left.unreachable == right.unreachable &&
         std::abs(left.weighted_excess - right.weighted_excess) <= kEps &&
         std::abs(left.weighted_hops - right.weighted_hops) <= kEps &&
         std::abs(left.cut_weight - right.cut_weight) <= kEps &&
         left.used_part_deficit == right.used_part_deficit;
}

Objective add(const Objective& left, const Objective& right) {
  return {left.violations + right.violations,
          left.unreachable + right.unreachable,
          left.weighted_excess + right.weighted_excess,
          left.weighted_hops + right.weighted_hops,
          left.cut_weight + right.cut_weight,
          left.used_part_deficit + right.used_part_deficit};
}

Objective subtract(const Objective& left, const Objective& right) {
  return {left.violations - right.violations,
          left.unreachable - right.unreachable,
          left.weighted_excess - right.weighted_excess,
          left.weighted_hops - right.weighted_hops,
          left.cut_weight - right.cut_weight,
          left.used_part_deficit - right.used_part_deficit};
}

void validate_input(const Input& input) {
  if (input.num_parts<=0 || input.num_clusters<=0 || input.num_dimensions<=0 || input.num_nets<0 ||
      input.distance.size()!=static_cast<std::size_t>(input.num_parts) ||
      input.balance_bound.size()!=static_cast<std::size_t>(input.num_parts) ||
      input.hard_capacity.size()!=static_cast<std::size_t>(input.num_parts) ||
      input.clusters.size()!=static_cast<std::size_t>(input.num_clusters) ||
      input.nets.size()!=static_cast<std::size_t>(input.num_nets))
    throw std::runtime_error("invalid hop model dimensions");
  for(int p=0;p<input.num_parts;++p)
    if(input.distance[p].size()!=static_cast<std::size_t>(input.num_parts) ||
       input.balance_bound[p].size()!=static_cast<std::size_t>(input.num_dimensions) ||
       input.hard_capacity[p].size()!=static_cast<std::size_t>(input.num_dimensions))
      throw std::runtime_error("invalid hop board dimensions");
  for(const auto& cluster:input.clusters)
    if(cluster.weights.size()!=static_cast<std::size_t>(input.num_dimensions))
      throw std::runtime_error("invalid hop resource dimensions");
  if (input.num_parts <= 0 || input.num_clusters <= 0 ||
      input.num_dimensions <= 0 || input.num_nets < 0 ||
      input.hop_limit <= 0 || input.min_used_parts <= 0 ||
      input.min_used_parts > input.num_parts) {
    throw std::runtime_error("invalid PARAM record");
  }
  for (int part = 0; part < input.num_parts; ++part) {
    if (input.distance[part][part] != 0) {
      throw std::runtime_error("distance diagonal must be zero");
    }
    for (int sink = 0; sink < input.num_parts; ++sink) {
      if (input.distance[part][sink] < -1) {
        throw std::runtime_error("distance must be -1 or non-negative");
      }
    }
    for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
      if (!std::isfinite(input.balance_bound[part][dimension]) ||
          input.balance_bound[part][dimension] < 0.0 ||
          input.hard_capacity[part][dimension] < 0) {
        throw std::runtime_error("invalid BOUND record");
      }
    }
  }
  for (const Cluster& cluster : input.clusters) {
    if (cluster.part < 0 || cluster.part >= input.num_parts ||
        cluster.fixed_part < -1 || cluster.fixed_part >= input.num_parts ||
        (cluster.fixed_part >= 0 && cluster.fixed_part != cluster.part) ||
        std::any_of(cluster.weights.begin(), cluster.weights.end(),
                    [](long long weight) { return weight < 0; })) {
      throw std::runtime_error("invalid CLUSTER assignment");
    }
  }
  for (const Net& net : input.nets) {
    if (!std::isfinite(net.weight) || net.weight <= 0.0 || net.source < 0 ||
        net.source >= input.num_clusters || net.sinks.empty()) {
      throw std::runtime_error("invalid NET record");
    }
    std::set<int> unique_sinks;
    for (const int sink : net.sinks) {
      if (sink < 0 || sink >= input.num_clusters || sink == net.source ||
          !unique_sinks.insert(sink).second) {
        throw std::runtime_error("invalid NET sink record");
      }
    }
  }
}

Objective net_objective(const Input& input, const Net& net,
                        const std::vector<int>& assignment) {
  const int source_part = assignment[net.source];
  std::set<int> sink_parts;
  for (const int sink : net.sinks) {
    const int part = assignment[sink];
    if (part != source_part) {
      sink_parts.insert(part);
    }
  }
  Objective result;
  if (!sink_parts.empty()) {
    result.cut_weight = net.weight;
  }
  for (const int sink_part : sink_parts) {
    const int hops = input.distance[source_part][sink_part];
    if (hops < 0) {
      ++result.violations;
      ++result.unreachable;
      result.weighted_excess +=
          net.weight * (input.hop_limit + input.num_parts + 1);
      result.weighted_hops +=
          net.weight * (input.hop_limit + input.num_parts + 1);
      continue;
    }
    if (hops > input.hop_limit) {
      ++result.violations;
      result.weighted_excess +=
          net.weight * (hops - input.hop_limit);
    }
    result.weighted_hops += net.weight * hops;
  }
  return result;
}

Objective total_objective(const Input& input,
                          const std::vector<int>& assignment) {
  Objective result;
  std::vector<int> counts(input.num_parts, 0);
  for (const int part : assignment) {
    ++counts[part];
  }
  const int used = std::count_if(
      counts.begin(), counts.end(), [](int count) { return count > 0; });
  result.used_part_deficit = std::max(0, input.min_used_parts - used);
  for (const Net& net : input.nets) {
    result = add(result, net_objective(input, net, assignment));
  }
  return result;
}

struct Move {
  int cluster = -1;
  int source = -1;
  int target = -1;
  int second_cluster = -1;
  int second_source = -1;
  int second_target = -1;
  Objective objective;
};

struct Result {
  std::vector<int> assignment;
  Objective initial;
  Objective final;
  int rounds = 0;
  int tentative_moves = 0;
  bool exact_fallback = false;
  bool scale_guard = false;
  std::vector<std::tuple<int, int, int>> committed_moves;
};

bool exact_feasible_fallback(const Input& input, std::vector<int>* assignment,
                             Objective* objective) {
  // This oracle is intentionally limited to tiny adversarial instances.  It
  // proves the test/reference cases and distinguishes a genuinely infeasible
  // constraint set from an FM local minimum without affecting large designs.
  if (input.num_clusters > 10) {
    return false;
  }
  constexpr long long kMaximumExactAssignments = 2000000;
  long long assignment_bound = 1;
  for (const Cluster& cluster : input.clusters) {
    const int choices = cluster.fixed_part >= 0 ? 1 : input.num_parts;
    if (assignment_bound > kMaximumExactAssignments / choices) {
      return false;
    }
    assignment_bound *= choices;
  }
  std::vector<int> candidate(input.num_clusters, -1);
  std::vector<std::vector<long long>> loads(
      input.num_parts,
      std::vector<long long>(input.num_dimensions, 0));
  std::vector<int> counts(input.num_parts, 0);
  bool found = false;
  Objective best;
  std::vector<int> best_assignment;

  const auto search = [&](const auto& self, int cluster) -> void {
    if (cluster == input.num_clusters) {
      const int used = std::count_if(
          counts.begin(), counts.end(), [](int count) { return count > 0; });
      if (used < input.min_used_parts) {
        return;
      }
      const Objective value = total_objective(input, candidate);
      if (value.violations != 0 || value.used_part_deficit != 0) {
        return;
      }
      if (!found || less_objective(value, best) ||
          (equal_objective(value, best) && candidate < best_assignment)) {
        found = true;
        best = value;
        best_assignment = candidate;
      }
      return;
    }
    const Cluster& record = input.clusters[cluster];
    const int first = record.fixed_part >= 0 ? record.fixed_part : 0;
    const int last =
        record.fixed_part >= 0 ? record.fixed_part + 1 : input.num_parts;
    for (int part = first; part < last; ++part) {
      bool fits = true;
      for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
        const long long projected =
            loads[part][dimension] + record.weights[dimension];
        if (projected > input.hard_capacity[part][dimension] ||
            projected > input.balance_bound[part][dimension] + kEps) {
          fits = false;
          break;
        }
      }
      if (!fits) {
        continue;
      }
      candidate[cluster] = part;
      ++counts[part];
      for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
        loads[part][dimension] += record.weights[dimension];
      }
      self(self, cluster + 1);
      for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
        loads[part][dimension] -= record.weights[dimension];
      }
      --counts[part];
    }
  };
  search(search, 0);
  if (found) {
    *assignment = std::move(best_assignment);
    *objective = best;
  }
  return found;
}

Result refine(const Input& input) {
  std::vector<int> assignment;
  assignment.reserve(input.clusters.size());
  for (const Cluster& cluster : input.clusters) {
    assignment.push_back(cluster.part);
  }
  std::vector<std::vector<long long>> loads(
      input.num_parts,
      std::vector<long long>(input.num_dimensions, 0));
  std::vector<int> counts(input.num_parts, 0);
  for (int cluster = 0; cluster < input.num_clusters; ++cluster) {
    const int part = assignment[cluster];
    ++counts[part];
    for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
      loads[part][dimension] += input.clusters[cluster].weights[dimension];
    }
  }

  std::vector<std::vector<int>> incident(input.num_clusters);
  for (int net = 0; net < input.num_nets; ++net) {
    incident[input.nets[net].source].push_back(net);
    for (const int sink : input.nets[net].sinks) {
      incident[sink].push_back(net);
    }
  }
  for (auto& nets : incident) {
    std::sort(nets.begin(), nets.end());
    nets.erase(std::unique(nets.begin(), nets.end()), nets.end());
  }

  const auto fits = [&](int cluster, int target) {
    for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
      const long long projected =
          loads[target][dimension] +
          input.clusters[cluster].weights[dimension];
      if (projected > input.hard_capacity[target][dimension] ||
          projected > input.balance_bound[target][dimension] + kEps) {
        return false;
      }
    }
    return true;
  };

  Result result;
  result.assignment = assignment;
  result.initial = total_objective(input, assignment);
  Objective incumbent = result.initial;

  // The current bounded-donor FM implementation is a legalization fallback,
  // not the planned multilevel large-design partitioner.  Its move search is
  // intentionally rejected before entering the quadratic region; callers can
  // then select the topology-aware constructive provider instead of silently
  // spending hours in a post-partition repair pass.  A hop-legal large input
  // still passes through and is independently audited normally.
  constexpr int kMaximumFmClusters = 50000;
  if (input.num_clusters > kMaximumFmClusters &&
      (result.initial.violations > 0 ||
       result.initial.used_part_deficit > 0)) {
    result.final = result.initial;
    result.scale_guard = true;
    return result;
  }

  // Each round is an FM pass. Moves are tentatively accepted even if the
  // immediate objective is worse; only the best improving prefix is committed.
  for (int round = 0;
       round < 32 &&
       (incumbent.violations > 0 || incumbent.used_part_deficit > 0);
       ++round) {
    const std::vector<int> round_start = assignment;
    const auto round_loads = loads;
    const auto round_counts = counts;
    const Objective round_objective = incumbent;
    std::vector<bool> locked(input.num_clusters, false);
    std::vector<std::tuple<int, int, int>> sequence;
    std::vector<int> best_assignment = round_start;
    std::vector<std::vector<long long>> best_loads = round_loads;
    std::vector<int> best_counts = round_counts;
    Objective best_objective = round_objective;
    int best_prefix = 0;
    Objective working = round_objective;

    for (int step = 0; step < input.num_clusters; ++step) {
      std::set<int> candidates;
      for (const Net& net : input.nets) {
        if (net_objective(input, net, assignment).violations == 0) {
          continue;
        }
        candidates.insert(net.source);
        candidates.insert(net.sinks.begin(), net.sinks.end());
      }
      if (working.used_part_deficit > 0) {
        for (int cluster = 0; cluster < input.num_clusters; ++cluster) {
          candidates.insert(cluster);
        }
      }
      Move selected;
      bool found = false;
      const auto consider = [&](const Move& candidate) {
        const auto key = std::make_tuple(
            candidate.cluster, candidate.target, candidate.second_cluster);
        const auto selected_key = std::make_tuple(
            selected.cluster, selected.target, selected.second_cluster);
        if (!found || less_objective(candidate.objective, selected.objective) ||
            (equal_objective(candidate.objective, selected.objective) &&
             key < selected_key)) {
          found = true;
          selected = candidate;
        }
      };
      for (const int cluster : candidates) {
        if (locked[cluster] || input.clusters[cluster].fixed_part >= 0) {
          continue;
        }
        const int source = assignment[cluster];
        Objective incident_before;
        for (const int net : incident[cluster]) {
          incident_before = add(
              incident_before,
              net_objective(input, input.nets[net], assignment));
        }
        for (int target = 0; target < input.num_parts; ++target) {
          if (target == source) {
            continue;
          }
          if (fits(cluster, target)) {
            assignment[cluster] = target;
            Objective incident_after;
            for (const int net : incident[cluster]) {
              incident_after = add(
                  incident_after,
                  net_objective(input, input.nets[net], assignment));
            }
            assignment[cluster] = source;
            Objective candidate_objective = add(
                subtract(working, incident_before), incident_after);
            int used = std::count_if(
                counts.begin(), counts.end(),
                [](int count) { return count > 0; });
            if (counts[source] == 1) {
              --used;
            }
            if (counts[target] == 0) {
              ++used;
            }
            candidate_objective.used_part_deficit =
                std::max(0, input.min_used_parts - used);
            consider({cluster, source, target, -1, -1, -1,
                      candidate_objective});
          }

          // Evaluate an atomic two-cluster exchange as the topology-
          // correction analogue of pairwise FM.  General source/target swaps
          // preserve tight balance; a singleton source may additionally draw
          // a donor from another non-singleton part to keep every required
          // FPGA populated.
          std::vector<std::pair<double, int>> donor_candidates;
          for (int donor = 0; donor < input.num_clusters; ++donor) {
            if (donor == cluster || locked[donor] ||
                input.clusters[donor].fixed_part >= 0) {
              continue;
            }
            const int donor_source = assignment[donor];
            if (donor_source == source ||
                (counts[source] != 1 && donor_source != target) ||
                (counts[source] == 1 && donor_source != target &&
                 counts[donor_source] == 1)) {
              continue;
            }
            double resource_distance = 0.0;
            for (int dimension = 0; dimension < input.num_dimensions;
                 ++dimension) {
              resource_distance +=
                  std::abs(
                      static_cast<double>(
                          input.clusters[donor].weights[dimension] -
                          input.clusters[cluster].weights[dimension])) /
                  std::max(
                      1.0,
                      static_cast<double>(
                          input.clusters[cluster].weights[dimension]));
            }
            donor_candidates.emplace_back(resource_distance, donor);
          }
          std::sort(donor_candidates.begin(), donor_candidates.end());
          constexpr std::size_t kMaximumPairDonors = 64;
          if (donor_candidates.size() > kMaximumPairDonors) {
            donor_candidates.resize(kMaximumPairDonors);
          }
          for (const auto& [unused_distance, donor] : donor_candidates) {
            static_cast<void>(unused_distance);
            const int donor_source = assignment[donor];
            std::set<int> affected_parts = {source, target, donor_source};
            bool pair_fits = true;
            for (const int part : affected_parts) {
              for (int dimension = 0; dimension < input.num_dimensions;
                   ++dimension) {
                long long projected = loads[part][dimension];
                if (part == source) {
                  projected -= input.clusters[cluster].weights[dimension];
                  projected += input.clusters[donor].weights[dimension];
                }
                if (part == target) {
                  projected += input.clusters[cluster].weights[dimension];
                }
                if (part == donor_source) {
                  projected -= input.clusters[donor].weights[dimension];
                }
                if (projected < 0 ||
                    projected > input.hard_capacity[part][dimension] ||
                    projected > input.balance_bound[part][dimension] + kEps) {
                  pair_fits = false;
                  break;
                }
              }
              if (!pair_fits) {
                break;
              }
            }
            if (!pair_fits) {
              continue;
            }
            std::vector<int> affected_nets = incident[cluster];
            affected_nets.insert(
                affected_nets.end(), incident[donor].begin(),
                incident[donor].end());
            std::sort(affected_nets.begin(), affected_nets.end());
            affected_nets.erase(
                std::unique(affected_nets.begin(), affected_nets.end()),
                affected_nets.end());
            Objective pair_before;
            for (const int net : affected_nets) {
              pair_before = add(
                  pair_before,
                  net_objective(input, input.nets[net], assignment));
            }
            assignment[cluster] = target;
            assignment[donor] = source;
            Objective pair_after;
            for (const int net : affected_nets) {
              pair_after = add(
                  pair_after,
                  net_objective(input, input.nets[net], assignment));
            }
            assignment[cluster] = source;
            assignment[donor] = donor_source;
            Objective pair_objective = add(
                subtract(working, pair_before), pair_after);
            pair_objective.used_part_deficit = 0;
            consider({cluster, source, target, donor, donor_source, source,
                      pair_objective});
          }
        }
      }
      if (!found) {
        break;
      }
      const int cluster = selected.cluster;
      assignment[cluster] = selected.target;
      locked[cluster] = true;
      --counts[selected.source];
      ++counts[selected.target];
      for (int dimension = 0; dimension < input.num_dimensions; ++dimension) {
        const long long weight = input.clusters[cluster].weights[dimension];
        loads[selected.source][dimension] -= weight;
        loads[selected.target][dimension] += weight;
      }
      if (selected.second_cluster >= 0) {
        const int donor = selected.second_cluster;
        assignment[donor] = selected.second_target;
        locked[donor] = true;
        --counts[selected.second_source];
        ++counts[selected.second_target];
        for (int dimension = 0; dimension < input.num_dimensions;
             ++dimension) {
          const long long weight = input.clusters[donor].weights[dimension];
          loads[selected.second_source][dimension] -= weight;
          loads[selected.second_target][dimension] += weight;
        }
      }
      working = selected.objective;
      sequence.emplace_back(cluster, selected.source, selected.target);
      if (selected.second_cluster >= 0) {
        sequence.emplace_back(
            selected.second_cluster,
            selected.second_source,
            selected.second_target);
      }
      ++result.tentative_moves;
      if (working.used_part_deficit == 0 &&
          less_objective(working, best_objective)) {
        best_objective = working;
        best_assignment = assignment;
        best_loads = loads;
        best_counts = counts;
        best_prefix = static_cast<int>(sequence.size());
      }
      if (working.violations == 0 && working.used_part_deficit == 0) {
        break;
      }
    }

    if (!less_objective(best_objective, round_objective)) {
      assignment = round_start;
      loads = round_loads;
      counts = round_counts;
      break;
    }
    assignment = std::move(best_assignment);
    loads = std::move(best_loads);
    counts = std::move(best_counts);
    incumbent = best_objective;
    result.committed_moves.insert(
        result.committed_moves.end(), sequence.begin(),
        sequence.begin() + best_prefix);
    ++result.rounds;
  }
  result.assignment = assignment;
  result.final = total_objective(input, assignment);
  if (result.final.violations > 0 ||
      result.final.used_part_deficit > 0) {
    std::vector<int> exact_assignment;
    Objective exact_objective;
    if (exact_feasible_fallback(
            input, &exact_assignment, &exact_objective)) {
      const std::vector<int> original = [&]() {
        std::vector<int> value;
        for (const Cluster& cluster : input.clusters) {
          value.push_back(cluster.part);
        }
        return value;
      }();
      result.committed_moves.clear();
      for (int cluster = 0; cluster < input.num_clusters; ++cluster) {
        if (original[cluster] != exact_assignment[cluster]) {
          result.committed_moves.emplace_back(
              cluster, original[cluster], exact_assignment[cluster]);
        }
      }
      result.assignment = std::move(exact_assignment);
      result.final = exact_objective;
      result.exact_fallback = true;
    }
  }
  return result;
}

}  // namespace

emuflow::core::PartitionHopSolution emuflow::core::ComputePartitionHops(
    const PartitionHopModel& model) {
  validate_input(model);
  auto result=refine(model);
  PartitionHopSolution out;
  out.assignment=std::move(result.assignment);
  out.pass=result.final.violations==0 && result.final.used_part_deficit==0;
  const auto objective=[&](const std::string& prefix,const Objective& value) {
    out.metrics[prefix+"_violations"]=value.violations;
    out.metrics[prefix+"_unreachable"]=value.unreachable;
    out.metrics[prefix+"_weighted_excess"]=value.weighted_excess;
    out.metrics[prefix+"_weighted_hops"]=value.weighted_hops;
    out.metrics[prefix+"_cut_weight"]=value.cut_weight;
    out.metrics[prefix+"_used_part_deficit"]=value.used_part_deficit;
  };
  objective("initial",result.initial);objective("final",result.final);
  out.metrics["rounds"]=result.rounds;
  out.metrics["tentative_moves"]=result.tentative_moves;
  out.metrics["committed_moves"]=result.committed_moves.size();
  out.metrics["exact_fallback"]=result.exact_fallback;
  out.metrics["scale_guard"]=result.scale_guard;
  return out;
}
