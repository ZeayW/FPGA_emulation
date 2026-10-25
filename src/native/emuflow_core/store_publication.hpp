#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Adopt one caller-owned, closed SQLite temporary without copying its rows or
// file. The request binds the staged generation and the producer's sources.
StageResult PublishSqliteStore(const ArtifactRef& request,
                              const std::string& destination,
                              const std::string& report = {});
}  // namespace emuflow::core
