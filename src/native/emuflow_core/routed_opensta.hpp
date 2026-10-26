#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <vector>
namespace emuflow::core {
class SqliteDatabase;
void ValidateRoutedOpenstaGraph(SqliteDatabase& graph, std::int64_t endpoints,
                               std::int64_t delay_types);
void StageRoutedOpensta(const std::vector<ArtifactRef>& inputs,
                       const std::string& emuir, const std::string& liberty,
                       const std::string& summary);
}
