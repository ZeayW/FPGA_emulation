#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Independent reconstruction: does not call the schedule solver, model builder,
// producer certificate builder or publication code. Inputs are immutable owners.
void ValidateExactSchedule(const std::vector<ArtifactRef>& inputs,
                           const std::string& summary);
}
