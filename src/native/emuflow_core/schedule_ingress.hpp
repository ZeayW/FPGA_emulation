#pragma once
#include "emuflow_core/schedule_model.hpp"

namespace emuflow::core {
ExactScheduleModel BuildExactScheduleModel(const std::string& route_store,
                                          const std::string& request);
void ScheduleExactFromStore(const std::string& route_store,
                            const std::string& request,
                            const std::string& output);
}
