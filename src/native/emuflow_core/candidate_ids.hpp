#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
struct CandidateEligibility {
  std::uint64_t nets=0, eligible=0;
  std::vector<unsigned char> bits;
};
// In-process phase owners need eligibility, not an unused name-pool export.
CandidateEligibility BuildCandidateEligibility(const std::vector<ArtifactRef>& inputs);
void BuildCandidateIds(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
