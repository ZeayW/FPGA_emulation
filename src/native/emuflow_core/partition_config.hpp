#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Source-backed normalization of the maintained partition policy. The request
// contains board-sized capacities/topology and overrides; caller constraints
// remain an original generation-bound input file.
// Source identities/resources are queried here, never supplied by a host catalog.
std::string NormalizePartitionConfiguration(const ArtifactRef& source,
    const std::string& configuration,const std::string& raw_constraints);
void PreparePartitionConfiguration(const std::vector<ArtifactRef>& inputs,
                                   const std::string& output);
}
