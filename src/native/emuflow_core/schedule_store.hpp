#pragma once
#include <string>

namespace emuflow::core {
// Whole native producer: RouteStore + bounded board/header requests -> store.
// Model, solution, certificate and record publication stay in one C++ process.
void BuildExactScheduleStore(const std::string& route_store,
                            const std::string& board_request,
                            const std::string& generation_request,
                            const std::string& output);
// Publish the existing fully typed v4 ScheduleStore directly from the native
// input/result, the public RouteStore, and a bounded configuration request. The graph-sized
// output never returns through Python objects or JSON serialization.
void PublishExactScheduleStore(const std::string& input_path,
                               const std::string& result_path,
                               const std::string& route_store_path,
                               const std::string& request_path,
                               const std::string& output_path);
}  // namespace emuflow::core
