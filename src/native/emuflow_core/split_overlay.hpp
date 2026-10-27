#pragma once
#include "emuflow_core/store.hpp"
#include <string>
#include <vector>
namespace emuflow::core {
// The owner stores only derived partition memberships and shadow coordinates.
// Original circuit, assignment and schedule facts retain their original owners.
StageResult BuildSplitOverlay(const std::vector<ArtifactRef>& inputs,const std::string& output);
std::vector<ArtifactRef> PrepareSplitProjection(SqliteDatabase& database,
    const ArtifactRef& split,const std::string& alias="main",const std::string& prefix="");
StageResult AuditSplitOverlay(const ArtifactRef& split,const std::string& summary);
std::string ValidateSplitOverlay(const ArtifactRef& split);
}
