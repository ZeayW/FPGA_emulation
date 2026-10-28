#pragma once
#include "emuflow_core/stage_runtime.hpp"
namespace emuflow::core {
// Complete original member and source-copy observation ledger. Actual native
// object collections remain owned by the supplied immutable lineage stores.
// Absence, changed types and replicas are facts, not implementation failures,
// removal certificates, pin equivalence or numerical timing measurements.
void BuildOriginalLineageIndex(const std::vector<ArtifactRef>& inputs,
                              const std::string& output);
}
