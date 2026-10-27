#pragma once

#include "common/stage_runtime.hpp"

namespace emuflow::core {
// Inputs are finalized, artifact-scoped mapped v2 and private lexical atom
// relations. The caller selects destinations; the operation owns the scratch
// relation and staged text outputs, with the summary published last.
void ExportBookshelf(const ArtifactRef& mapped, const ArtifactRef& atoms,
                     const std::string& nets, const std::string& summary,
                     const std::string& scratch);
}  // namespace emuflow::core
