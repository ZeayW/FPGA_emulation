#pragma once
#include "common/stage_runtime.hpp"
#include <istream>
#include <functional>
namespace emuflow::core {
class SqliteDatabase;
// Producer stream -> private indexed staging. No full document, flattened
// circuit, or alias population crosses the host boundary.
StageResult StageYosysHierarchy(const ArtifactRef& request, std::istream& stream,
                               const std::string& output, const std::string& summary,
                               const ArtifactRef* normalization_library=nullptr);
StageResult ImportYosysEmuir(const ArtifactRef& request, std::istream& stream,
                            const std::string& output, const std::string& summary,
                            const ArtifactRef* normalization_library=nullptr,
                            const std::function<void(SqliteDatabase&)>& finalize = {});
// Generic transport is already flattened by the maintained synthesis script.
// Preserve its source wire numbers and canonical unaliased net identities.
StageResult ImportFlatYosysEmuir(const ArtifactRef& request, std::istream& stream,
    const std::string& output, const std::string& summary,
    const std::function<void(SqliteDatabase&)>& finalize = {});
}
