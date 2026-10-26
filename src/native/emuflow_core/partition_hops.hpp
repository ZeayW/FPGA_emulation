#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <map>

namespace emuflow::core {
// Internal C++ model shared with the existing topology-constrained FM kernel.
// Circuit records never cross an interpreter or external request boundary.
struct PartitionHopCluster {
  int part=-1, fixed_part=-1;
  std::vector<long long> weights;
};
struct PartitionHopNet {
  double weight=1;
  int source=-1;
  std::vector<int> sinks;
};
struct PartitionHopModel {
  int num_parts=0, num_clusters=0, num_dimensions=0, num_nets=0;
  int hop_limit=0, min_used_parts=0;
  std::vector<std::vector<int>> distance;
  std::vector<std::vector<double>> balance_bound;
  std::vector<std::vector<long long>> hard_capacity;
  std::vector<PartitionHopCluster> clusters;
  std::vector<PartitionHopNet> nets;
};
struct PartitionHopSolution {
  std::vector<int> assignment;
  std::map<std::string,double> metrics;
  bool pass=false;
};
PartitionHopSolution ComputePartitionHops(const PartitionHopModel& model);
// Source/primary/original ownership -> native model/search, independent source
// audits, and one published original ownership plus bounded summary.
StageResult RefinePartitionHopsFromPrimary(const std::vector<ArtifactRef>& inputs,
                                         const std::string& directory, bool audit_only=false);
}
