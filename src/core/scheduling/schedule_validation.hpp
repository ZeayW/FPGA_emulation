#pragma once
#include "common/stage_runtime.hpp"

namespace emuflow::core {
class SqliteDatabase;
// Independent ordered typed-record seal audit. No route graph or hop vectors.
void AuditScheduleStoreSeal(SqliteDatabase& db,const std::string& sections,
    const std::string& logical_sha256,const std::string& pretty_sha256);
// Independent reconstruction: does not call the schedule solver, model builder,
// producer certificate builder or publication code. Inputs are immutable owners.
void ValidateExactSchedule(const std::vector<ArtifactRef>& inputs,
                           const std::string& summary);
}
