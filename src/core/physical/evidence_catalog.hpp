#pragma once

#include "common/stage_runtime.hpp"

#include <string>
#include <vector>

namespace emuflow::core {

struct PhysicalEvidenceOwner {
  std::string fpga;
  ArtifactRef selector;
  ArtifactRef store;
};

struct PhysicalEvidenceCatalog {
  std::string design;
  std::string platform;
  std::string provider;
  std::string qualification;
  double clock_uncertainty_ns = 0.0;
  std::vector<PhysicalEvidenceOwner> boundary_timing;
  std::vector<PhysicalEvidenceOwner> boundary_identities;
  std::vector<PhysicalEvidenceOwner> logic_segment_timing;
  std::vector<PhysicalEvidenceOwner> local_path_timing;
};

// Resolve the bounded Phase 7 physical summary to current typed store owners.
// Every selector and payload seal is checked before a graph cursor is opened.
PhysicalEvidenceCatalog ResolvePhysicalEvidenceCatalog(
    const ArtifactRef& summary);

}  // namespace emuflow::core
