#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
class SqliteDatabase;
// Validate and seal the caller-owned relation before its writer commits.
// The stage retains database ownership; no second route file is required.
std::string SealSystemRouteDatabase(SqliteDatabase& database,const std::string& request);
// The producer-owned v3 relation file stays immutable during this operation.
// Only a bounded logical/section seal is returned; no route JSON is exported.
void SealSystemRoutes(const std::vector<ArtifactRef>& inputs,
                      const std::string& output);
}
