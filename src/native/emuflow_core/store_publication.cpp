#include "emuflow_core/store_publication.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/store.hpp"

#include <filesystem>
#include <optional>

namespace emuflow::core {
namespace {
FileGeneration ReadGeneration(CheckedRequestReader& reader) {
  FileGeneration g;
  g.device=reader.Read<std::uint64_t>(); g.inode=reader.Read<std::uint64_t>();
  g.bytes=reader.Read<std::uint64_t>();
  g.modified_seconds=reader.Read<std::int64_t>(); g.modified_nanoseconds=reader.Read<std::int64_t>();
  g.changed_seconds=reader.Read<std::int64_t>(); g.changed_nanoseconds=reader.Read<std::int64_t>();
  return g;
}
bool SamePath(const std::string& a,const std::string& b) {
  std::error_code error;
  return std::filesystem::equivalent(a,b,error) ||
      std::filesystem::weakly_canonical(a)==std::filesystem::weakly_canonical(b);
}
ArtifactRef ReadBoundSource(CheckedRequestReader& reader,const std::string& path,
                            const std::string& schema) {
  const auto expected=ReadGeneration(reader);
  auto result=ArtifactRef::Bind(path,schema);
  if (!(result.generation()==expected))
    throw StageError(ErrorCode::kMixedGeneration,"SQLite publication source generation changed");
  return result;
}
}  // namespace

StageResult PublishSqliteStore(const ArtifactRef& request,const std::string& destination,
                              const std::string& report) {
  std::optional<ArtifactRef> staged;
  std::string schema;
  return RunStage("native.publish-store",[&]() {
    // Result files are invocation-owned. Reject an existing name before
    // parsing a possibly corrupt request, which may hide additional sources.
    if (!report.empty() && (std::filesystem::exists(report) || std::filesystem::is_symlink(report)))
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication report must be a fresh file");
    if (request.schema()!="sqlite-publication-request-v1")
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication request schema disagrees");
    request.RequireUnchanged();
    CheckedRequestReader reader(request.path(),256*1024);
    if (reader.Read<std::uint64_t>()!=0x31505351464d5545ULL || reader.Read<std::uint32_t>()!=1)
      throw StageError(ErrorCode::kInvalidArtifact,"unsupported SQLite publication request");
    schema=reader.ReadString32(false,256);
    if (FindSchema(schema).encoding!=ArtifactEncoding::kSqlite)
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication requires a registered store schema");
    const auto temporary=reader.ReadString32(false,16384);
    staged=ReadBoundSource(reader,temporary,schema);
    if (std::filesystem::is_symlink(temporary) ||
        std::filesystem::hard_link_count(temporary)!=1)
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication staging must be a private regular file");
    const auto count=reader.Read<std::uint32_t>();
    if (count>64)
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication source count exceeds bound");
    std::vector<ArtifactRef> sources{request};
    for (std::uint32_t i=0;i<count;++i) {
      const auto path=reader.ReadString32(false,16384);
      const auto source_schema=reader.ReadString32(false,256);
      sources.push_back(ReadBoundSource(reader,path,source_schema));
    }
    reader.RequireEnd();
    if (destination.empty() || destination=="-" ||
        !std::filesystem::is_directory(std::filesystem::absolute(destination).parent_path()) ||
        std::filesystem::is_directory(destination))
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication destination is unavailable");
    if (SamePath(destination,temporary))
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication destination aliases staging");
    for (const auto& source:sources) {
      if (SamePath(destination,source.path()) || SamePath(temporary,source.path()))
        throw StageError(ErrorCode::kInvalidRequest,"SQLite publication aliases a source");
      if (!report.empty() && SamePath(report,source.path()))
        throw StageError(ErrorCode::kInvalidRequest,"SQLite publication report aliases a source");
    }
    if (!report.empty() && (SamePath(report,destination) || SamePath(report,temporary)))
      throw StageError(ErrorCode::kInvalidRequest,"SQLite publication report aliases a store");
    // Staging is consumed by rename, so it is not a surviving RunStage input.
    // It remains bound and is checked after fsync by PublishTemporaryFile.
    return sources;
  },[&](const auto& sources) {
    { SqliteStore view(*staged); view.RequireUnchanged(); }
    RequireFinalizedSqlite(destination);
    auto commit_sources=sources;
    commit_sources.push_back(*staged);
    PublishTemporaryFile(staged->path(),destination,commit_sources);
  },[&]() {
    return std::vector<ArtifactRef>{ArtifactRef::Bind(destination,schema)};
  });
}
}  // namespace emuflow::core
