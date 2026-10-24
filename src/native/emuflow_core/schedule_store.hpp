#pragma once
#include <string>

namespace emuflow::core {
// Whole native producer: RouteStore + bounded board/header requests -> store.
// Model, solution, certificate and record publication stay in one C++ process.
void BuildExactScheduleStore(const std::string& route_store,
                            const std::string& board_request,
                            const std::string& generation_request,
                            const std::string& output);
}  // namespace emuflow::core
