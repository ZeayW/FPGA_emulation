#pragma once
#include "common/stage_runtime.hpp"
#include <string>
#include <csignal>

namespace emuflow::core {
class SqliteDatabase;
// Stream cut-source memberships and one partition's consumer segments into
// private lowering relations; never load captures or a semantic-contract tree.
void ProjectExactBoundarySegments(const ArtifactRef& artifact,
    const std::string& descriptor,const std::string& fpga,SqliteDatabase& destination);
// Stream every assignment semantic segment into an existing private
// expected_logic(segment,fpga,kind,constant) relation. No net/name catalog.
void ProjectExactCoverageSegments(const ArtifactRef& artifact,
    const std::string& descriptor,SqliteDatabase& destination);
// Project one partition's query semantics into exact_query_nodes(number,net),
// exact_query_segments(number,kind,source,target,constant), and
// exact_query_captures(number,net,kind,endpoint,port,bit) private SQL relations.
void ProjectExactQueryRecords(const ArtifactRef& artifact,const std::string& descriptor,
    const std::string& fpga,SqliteDatabase& destination,
    const volatile std::sig_atomic_t* cancelled=nullptr);
// Independent physical-section validation. Only bounded metadata and integer
// coordinates are retained; net/endpoint identities never form a name catalog.
std::string ValidateExactBinaryArtifact(const ArtifactRef& artifact,
                                       const std::string& descriptor);
// Read and bind the bounded manifest/count contract without rescanning sections.
std::string ReadExactBinaryCounts(const ArtifactRef& artifact,const std::string& descriptor);
void ValidateExactBinary(const std::vector<ArtifactRef>& inputs,
                         const std::string& summary);
// Complete native file/manifest/descriptor production from final native records.
// The independent section validator runs before publication.
StageResult PublishExactBinary(const std::vector<ArtifactRef>& inputs,
                               const std::string& directory);
}
