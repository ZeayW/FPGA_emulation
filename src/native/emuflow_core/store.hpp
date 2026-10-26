#pragma once

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/stage_runtime.hpp"

#include <memory>
#include <functional>
#include <optional>

namespace emuflow::core {
class ArtifactScope;
template <typename Tag> class ArtifactId {
 public:
  std::uint64_t sequence() const { return sequence_; }
 private:
  friend class ArtifactScope;
  ArtifactId(std::shared_ptr<const ArtifactRef> owner, std::uint64_t sequence)
      : owner_(std::move(owner)), sequence_(sequence) {}
  std::shared_ptr<const ArtifactRef> owner_;
  std::uint64_t sequence_;
};
struct CellTag {}; struct NetTag {}; struct PathTag {}; struct EndpointTag {};
struct FpgaTag {}; struct LinkTag {};
struct PinTag {};
struct RouteTag {};
using CellId=ArtifactId<CellTag>;
using NetId=ArtifactId<NetTag>;
using PathId=ArtifactId<PathTag>;
using EndpointId=ArtifactId<EndpointTag>;
using FpgaId=ArtifactId<FpgaTag>;
using LinkId=ArtifactId<LinkTag>;
using PinId=ArtifactId<PinTag>;
using RouteId=ArtifactId<RouteTag>;

// One artifact namespace, shared by all public view identities. Extracting an
// integer for a private kernel is explicit; consumers validate its owner first.
class ArtifactScope {
 public:
  explicit ArtifactScope(const ArtifactRef& artifact);
  const ArtifactRef& artifact() const { return *artifact_; }
  template <typename Tag> ArtifactId<Tag> Id(std::uint64_t sequence) const {
    return ArtifactId<Tag>(artifact_,sequence);
  }
  // Ordered kernels already retain an input lease and recheck its generation
  // at the stage boundary. Per-record ownership checks must not issue fstat.
  void RequireOwner(const ArtifactRef& other) const {
    if (other.path()!=artifact_->path() || other.schema()!=artifact_->schema() ||
        !(other.generation()==artifact_->generation()))
      throw StageError(ErrorCode::kMixedGeneration,"identity belongs to another artifact");
  }
  template <typename Tag> void RequireOwner(const ArtifactId<Tag>& identity) const {
    RequireOwner(*identity.owner_);
  }
  template <typename Tag> void Require(const ArtifactId<Tag>& identity) const {
    RequireOwner(identity);
    artifact_->RequireUnchanged();
  }
 private:
  std::shared_ptr<const ArtifactRef> artifact_;
};

// Common reader owner for public and registered private SQLite artifacts.
// Construction validates only the registered bounded envelope. Ordered typed
// cursors retain each phase's independent record/coverage checks.
class SqliteStore : public ArtifactScope {
 public:
  explicit SqliteStore(const ArtifactRef& artifact);
  SqliteDatabase& database() { return database_; }
  void RequireUnchanged() const { artifact().RequireUnchanged(); }
 private:
  SqliteDatabase database_;
};
// Shared by binding and publication. It never reads circuit-sized relations.
void ValidateSqliteSchema(SqliteDatabase& database,const RegisteredSchema& schema);
// Compare the complete installed schema, without integrity/quick checks or row scans.
void RequireSqliteDDL(SqliteDatabase& database,const char* ddl);
// Bounded metadata equality through SQLite JSON1; never a graph JSON parser.
void RequireEquivalentJson(SqliteDatabase& db,const std::string& left,
                           const std::string& right,const std::string& message);

// Exact integer member of an already JSON1-validated record, including >64-bit values.
// Raw scalar/object token from an already JSON1-validated bounded object.
std::string JsonMemberToken(SqliteDatabase& db,const std::string& object,const std::string& name);
std::string JsonIntegerMemberToken(SqliteDatabase& db,const std::string& object,
                                   const std::string& name);

// Source checks and closed statement/transaction ownership precede publication.
// A rejected writer leaves the prior destination intact and retires only its
// exclusive invocation-owned staging and SQLite sidecars.
class SqliteArtifactWriter {
 public:
  SqliteArtifactWriter(const std::string& target,const std::string& schema,
                       std::vector<ArtifactRef> inputs = {});
  SqliteDatabase& database() { return database_; }
  ArtifactRef Commit();
 private:
  std::string target_,schema_;
  std::vector<ArtifactRef> inputs_;
  std::shared_ptr<StagedArtifact> staged_;
  SqliteDatabase database_;
  bool committed_=false;
};

struct InstanceCopy {
  // These IDs belong to this ownership store, not the upstream EmuIR. A
  // consumer maps source sequences only through its explicitly paired source.
  CellId source;
  FpgaId partition;
  std::string identity;
  bool replica;
};
class PartitionStoreView {
 public:
  std::size_t partitions() const { return parts_.size(); }
  FpgaId Partition(std::uint64_t sequence) const;
  const std::string& PartitionName(const FpgaId& partition) const;
 protected:
  explicit PartitionStoreView(const ArtifactRef& artifact);
  SqliteStore store_;
  std::vector<std::string> parts_;
};
struct AssignmentCutSummary {
  std::uint64_t records=0,sinks=0;
  std::string sha256;
};
class AssignmentView : public PartitionStoreView {
 public:
  explicit AssignmentView(const ArtifactRef& artifact);
  std::uint64_t source_cells() const { return source_cells_; }
  void VisitCopies(const std::function<void(const InstanceCopy&)>& visitor);
  // Independently validate current cut columns and stream their logical hash.
  AssignmentCutSummary ValidateCuts();
 private:
  std::uint64_t source_cells_=0;
};
struct SplitEndpoint {
  FpgaId partition;
  NetId net;
  std::uint64_t ordinal;
  bool sink;
  std::optional<CellId> cell;
  std::string synthetic_cell;
  PinId pin;
  std::uint64_t bit;
};
class SplitView : public PartitionStoreView {
 public:
  explicit SplitView(const ArtifactRef& artifact);
  void VisitCopies(const FpgaId& partition,const std::function<void(const InstanceCopy&)>& visitor);
  void VisitEndpoints(const FpgaId& partition,const std::function<void(const SplitEndpoint&)>& visitor);
};
}  // namespace emuflow::core
