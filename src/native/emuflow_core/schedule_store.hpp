#pragma once
#include <string>

namespace emuflow::core {
// Publish the existing fully typed v4 ScheduleStore directly from the native
// input/result, the public RouteStore, and a bounded configuration request. The graph-sized
// output never returns through Python objects or JSON serialization.
void PublishExactScheduleStore(const std::string& input_path,
                               const std::string& result_path,
                               const std::string& route_store_path,
                               const std::string& request_path,
                               const std::string& output_path);
}  // namespace emuflow::core
