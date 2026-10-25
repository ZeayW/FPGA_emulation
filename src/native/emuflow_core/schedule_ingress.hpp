#pragma once
#include "emuflow_core/schedule_model.hpp"

namespace emuflow::core {
ExactScheduleModel BuildExactScheduleModel(const std::string& route_store,
                                          const std::string& request,
                                          ExactScheduleBuildContext& context);

}
