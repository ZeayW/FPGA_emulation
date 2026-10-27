#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Request declares a bounded source catalog; all large relations remain typed.
std::vector<std::string> PhysicalCoverageInputSchemas(const std::string& request);
void BuildPhysicalCoverage(const std::vector<ArtifactRef>& inputs,const std::string& output);
void AuditPhysicalCoverage(const std::vector<ArtifactRef>& inputs,const std::string& summary);
}
