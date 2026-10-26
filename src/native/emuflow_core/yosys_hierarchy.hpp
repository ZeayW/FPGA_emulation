#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <istream>
namespace emuflow::core {
// Producer stream -> private indexed staging. No full document, flattened
// circuit, or alias population crosses the host boundary.
StageResult StageYosysHierarchy(const ArtifactRef& request, std::istream& stream,
                               const std::string& output, const std::string& summary);
}
