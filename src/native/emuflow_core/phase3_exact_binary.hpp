#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <string>

namespace emuflow::core {
class SqliteDatabase;
// Stream cut-source memberships and one partition's consumer segments into
// private lowering relations; never load captures or a semantic-contract tree.
void ProjectExactBoundarySegments(const ArtifactRef& artifact,
    const std::string& descriptor,const std::string& fpga,SqliteDatabase& destination);
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
