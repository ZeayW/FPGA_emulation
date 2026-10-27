#pragma once
#include "emuflow_core/store.hpp"
#include <string>
#include <vector>
namespace emuflow::core {
// The owner stores only derived partition memberships and shadow coordinates.
// Original circuit, assignment and schedule facts retain their original owners.
// A successful producer may return its bounded, source-derived validation to
// the enclosing stage. It is valid only when the returned operation succeeds;
// reopening/auditing an existing artifact still uses ValidateSplitOverlay.
StageResult BuildSplitOverlay(const std::vector<ArtifactRef>& inputs,const std::string& output,
                             std::string* validated_summary=nullptr);
std::vector<ArtifactRef> PrepareSplitProjection(SqliteDatabase& database,
    const ArtifactRef& split,const std::string& alias="main",const std::string& prefix="");
StageResult AuditSplitOverlay(const ArtifactRef& split,const std::string& summary);
std::string ValidateSplitOverlay(const ArtifactRef& split);
}
