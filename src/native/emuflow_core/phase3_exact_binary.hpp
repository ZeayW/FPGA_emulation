#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <string>

namespace emuflow::core {
// Independent physical-section validation. Only bounded metadata and integer
// coordinates are retained; net/endpoint identities never form a name catalog.
std::string ValidateExactBinaryArtifact(const ArtifactRef& artifact,
                                       const std::string& descriptor);
void ValidateExactBinary(const std::vector<ArtifactRef>& inputs,
                         const std::string& summary);
// Complete native file/manifest/descriptor production from final native records.
// The independent section validator runs before publication.
StageResult PublishExactBinary(const std::vector<ArtifactRef>& inputs,
                               const std::string& directory);
}
