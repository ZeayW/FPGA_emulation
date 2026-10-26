#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include "emuflow_core/sqlite.hpp"
namespace emuflow::core {
struct PhysicalEvidenceSeal {
  std::string header,counts,source,logical_sha256,measurement_counts;
  double maximum_delay_ns=0;
};
// Producers validate canonical typed rows and seal metadata once. Independent
// readers recompute the same record contract against the published selector.
PhysicalEvidenceSeal SealPhysicalEvidence(SqliteDatabase& db,unsigned kind,
    const std::string& header,const std::string& source);
void ImportPhysicalMeasurements(const std::vector<ArtifactRef>& inputs,unsigned kind,
    const std::string& output,const std::string& summary);
void ValidatePhysicalEvidence(const std::vector<ArtifactRef>& inputs,const std::string& output);
}
