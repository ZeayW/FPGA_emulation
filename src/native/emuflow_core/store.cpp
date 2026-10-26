#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"

#include <filesystem>
#include <tuple>
#include <sstream>

namespace emuflow::core {
namespace {
const RegisteredSchema& SqliteSchema(const std::string& name) {
  const auto& schema=FindSchema(name);
  if (schema.encoding!=ArtifactEncoding::kSqlite)
    throw StageError(ErrorCode::kInvalidRequest,"SQLite store requires a registered SQLite schema");
  return schema;
}
const std::string& CheckedPath(const ArtifactRef& artifact) {
  SqliteSchema(artifact.schema());
  if (artifact.is_pipe()) throw StageError(ErrorCode::kInvalidArtifact,"SQLite store cannot consume a pipe");
  artifact.RequireUnchanged();
  return artifact.path();
}
}  // namespace
void ValidateSqliteSchema(SqliteDatabase& db,const RegisteredSchema& schema) {
  {
    auto application=db.Prepare("PRAGMA application_id"),version=db.Prepare("PRAGMA user_version");
    if (!application.Step() || !version.Step() || application.TypedInteger(0)!=static_cast<std::int64_t>(schema.magic) ||
        version.TypedInteger(0)!=schema.version)
      throw StageError(ErrorCode::kInvalidArtifact,"SQLite storage envelope disagrees with registered artifact");
  }
  if (!schema.sqlite_schema_query) return;
  auto row=db.Prepare(schema.sqlite_schema_query);
  if (!row.Step() || row.TypedText(0)!=schema.sqlite_schema_value || row.Step())
    throw StageError(ErrorCode::kInvalidArtifact,"SQLite logical schema disagrees with registered artifact");
}
void RequireSqliteDDL(SqliteDatabase& db,const char* ddl) {
  SqliteDatabase expected(":memory:",SqliteDatabase::Mode::kCreateExclusive);expected.Execute(ddl);
  const char* sql="SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name";
  auto observed=db.Prepare(sql),reference=expected.Prepare(sql);
  const auto normalize=[](const std::string& source) {
    std::istringstream input(source);std::string result,token;
    while(input>>token){if(!result.empty())result+=' ';result+=token;}return result;
  };
  while(reference.Step()) {
    if(!observed.Step())throw StageError(ErrorCode::kInvalidArtifact,"SQLite store has missing schema objects");
    for(int i=0;i<4;++i)if(normalize(observed.TypedText(i))!=normalize(reference.TypedText(i)))
      throw StageError(ErrorCode::kInvalidArtifact,"SQLite store schema objects disagree");
  }
  if(observed.Step())throw StageError(ErrorCode::kInvalidArtifact,"SQLite store has unexpected schema objects");
  // Cross-check SQLite's effective catalog as well as its declared DDL. This
  // retains column/constraint/index checks for every physical evidence owner.
  auto objects=expected.Prepare("SELECT type,name FROM sqlite_master WHERE type IN ('table','index') ORDER BY type,name");
  while(objects.Step()) {
    const auto type=objects.TypedText(0),name=objects.TypedText(1);
    std::string quoted="\"";for(char c:name){quoted+=c;if(c=='\"')quoted+=c;}quoted+='\"';
    const auto pragma=std::string("PRAGMA ")+(type=="table"?"table_xinfo(":"index_xinfo(")+quoted+")";
    auto actual=db.Prepare(pragma),wanted=expected.Prepare(pragma);
    const auto columns=type=="table"?7:6;
    while(wanted.Step()) {
      if(!actual.Step())throw StageError(ErrorCode::kInvalidArtifact,"SQLite store catalog has missing columns/indexes");
      for(int i=0;i<columns;++i)if(actual.IsNull(i)!=wanted.IsNull(i) || (!actual.IsNull(i) && actual.Text(i)!=wanted.Text(i)))
        throw StageError(ErrorCode::kInvalidArtifact,"SQLite store effective columns/indexes disagree");
    }
    if(actual.Step())throw StageError(ErrorCode::kInvalidArtifact,"SQLite store catalog has extra columns/indexes");
  }
}

void RequireEquivalentJson(SqliteDatabase& db,const std::string& left,const std::string& right,const std::string& message) {
  if(left.size()>8*1024*1024 || right.size()>8*1024*1024)throw StageError(ErrorCode::kInvalidArtifact,"JSON metadata exceeds bound");
  const char* sql="SELECT 1 FROM (SELECT fullkey,CASE WHEN type IN ('integer','real') THEN 'number' ELSE type END AS type,atom FROM json_tree(?1) "
    "EXCEPT SELECT fullkey,CASE WHEN type IN ('integer','real') THEN 'number' ELSE type END,atom FROM json_tree(?2)) LIMIT 1";
  for(unsigned reverse=0;reverse<2;++reverse) {
    auto row=db.Prepare(sql);row.BindText(1,reverse?right:left);row.BindText(2,reverse?left:right);if(row.Step())throw StageError(ErrorCode::kInvalidArtifact,message);
  }
}

namespace {
std::string CheckedTarget(const std::string& target,const std::string& name,
                          const std::vector<ArtifactRef>& inputs) {
  SqliteSchema(name);
  if (target.empty()) throw StageError(ErrorCode::kInvalidRequest,"SQLite destination is missing");
  auto normalized=std::filesystem::weakly_canonical(target);
  for (const auto& input:inputs) {
    input.RequireUnchanged();
    std::error_code error;
    if (normalized==input.path() || std::filesystem::equivalent(target,input.path(),error))
      throw StageError(ErrorCode::kInvalidRequest,"SQLite destination aliases an input");
  }
  return normalized.string();
}
}  // namespace
// JSON1 is the parser/type authority. Locate only the already validated raw
// member span when an integer is too large for SQLite's signed scalar API;
// converting its coerced REAL back to text would lose parameter bits.
void VisitJsonObjectTokens(const std::string& object,
    const std::function<void(const std::string&,const std::string&)>& visitor) {
  const auto Check=[](bool valid,const std::string& message){if(!valid)throw StageError(ErrorCode::kInvalidArtifact,message);};
  std::size_t position=0;
  const auto whitespace=[&]() {
    while (position<object.size() && (object[position]==' ' || object[position]=='\n' ||
           object[position]=='\r' || object[position]=='\t')) ++position;
  };
  whitespace();Check(position<object.size() && object[position++]=='{',"invalid JSON object token");
  const auto string_end=[&]() {
    Check(position<object.size() && object[position]=='"',"invalid parameter member token");
    ++position;
    while (position<object.size()) {
      const auto ch=object[position++];
      if (ch=='\\') { Check(position<object.size(),"truncated parameter string"); ++position; }
      else if (ch=='"') return;
    }
    Check(false,"truncated parameter member token");
  };
  while (position<object.size()) {
    whitespace();Check(position<object.size(),"truncated JSON member object"); if (object[position]=='}') break;
    const auto begin=position; string_end();
    const auto key=DecodeJsonString(object.substr(begin,position-begin));
    whitespace(); Check(position<object.size() && object[position++]==':',"invalid parameter member");
    whitespace(); const auto start=position;
    unsigned depth=0;
    while (position<object.size()) {
      const auto ch=object[position];
      if (ch=='"') { string_end(); continue; }
      if (ch=='[' || ch=='{') ++depth;
      else if (ch==']' || ch=='}') { if (!depth) break; --depth; }
      else if (ch==',' && !depth) break;
      ++position;
    }
    auto token=object.substr(start,position-start);
    while (!token.empty() && (token.back()==' ' || token.back()=='\r' || token.back()=='\n' || token.back()=='\t'))token.pop_back();
    Check(!token.empty(),"empty JSON member token");visitor(key,token);
    if (position<object.size() && object[position]==',') ++position;
    else break;
  }
}
std::string JsonMemberToken(SqliteDatabase& db,const std::string& object,const std::string& name) {
  (void)db;std::string result;bool found=false;
  VisitJsonObjectTokens(object,[&](const auto& key,const auto& token){if(key==name && !found){result=token;found=true;}});
  if(!found)throw StageError(ErrorCode::kInvalidArtifact,"primitive integer parameter token is missing");
  return result;
}
std::string JsonIntegerMemberToken(SqliteDatabase& db,const std::string& object,const std::string& name) {
  const auto token=JsonMemberToken(db,object,name);
  if(token.empty() || token.find_first_not_of("-0123456789")!=std::string::npos)
    throw StageError(ErrorCode::kInvalidArtifact,"invalid primitive integer parameter token");
  return token=="-0" ? "0":token;
}
ArtifactScope::ArtifactScope(const ArtifactRef& artifact)
    :artifact_(std::make_shared<const ArtifactRef>(artifact)) { artifact_->RequireUnchanged(); }
SqliteStore::SqliteStore(const ArtifactRef& artifact) try
    :ArtifactScope(artifact),database_(CheckedPath(artifact),FindSchema(artifact.schema()).immutable_read_only
        ? SqliteDatabase::Mode::kImmutableReadOnly : SqliteDatabase::Mode::kReadOnly) {
  // ArtifactRef already validated the registered envelope. Its unchanged
  // generation is the proof; opening a view must not replay that validation.
  RequireUnchanged();
} catch (const StageError&) { throw; }
catch (const std::exception& error) {
  throw StageError(ErrorCode::kInvalidArtifact,error.what());
}
SqliteArtifactWriter::SqliteArtifactWriter(const std::string& target,const std::string& schema,
                                          std::vector<ArtifactRef> inputs)
    :target_(CheckedTarget(target,schema,inputs)),schema_(schema),inputs_(std::move(inputs)),
      staged_(std::make_shared<StagedArtifact>(target_,std::vector<std::string>{".sqlite",".sqlite-journal",".sqlite-wal",".sqlite-shm"})),
      database_(staged_->path()+".sqlite",SqliteDatabase::Mode::kCreateExclusive) {
  database_.KeepAlive(staged_);
  const auto& registered=SqliteSchema(schema_);
  database_.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
      "PRAGMA application_id="+std::to_string(registered.magic)+"; PRAGMA user_version="+
      std::to_string(registered.version));
}
ArtifactRef SqliteArtifactWriter::Commit() {
  if (committed_) throw StageError(ErrorCode::kPublicationFailed,"SQLite artifact is already published");
  if (database_.InTransaction())
    throw StageError(ErrorCode::kPublicationFailed,"SQLite publication has an active transaction");
  ValidateSqliteSchema(database_,FindSchema(schema_));
  for (const auto& input:inputs_) input.RequireUnchanged();
  database_.Close();  // Reject live statements before rename, including readers.
  RequireFinalizedSqlite(target_);
  PublishTemporaryFile(staged_->path()+".sqlite",target_,inputs_);
  committed_=true;
  return ArtifactRef::Bind(target_,schema_);
}
namespace {
void RequireRecord(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
const ArtifactRef& AssignmentArtifact(const ArtifactRef& artifact) {
  const auto& name=artifact.schema();
  RequireRecord(name=="assignment-index-v3" || name=="assignment-index-v4",
      "assignment view requires registered v3/v4 storage");
  return artifact;
}
const ArtifactRef& SplitArtifact(const ArtifactRef& artifact) {
  RequireRecord(artifact.schema()=="split-netlists-v2","split view requires registered v2 storage");
  return artifact;
}
}  // namespace
PartitionStoreView::PartitionStoreView(const ArtifactRef& artifact):store_(artifact) {
  auto rows=store_.database().Prepare("SELECT part_index,fpga FROM parts ORDER BY part_index");
  while (rows.Step()) {
    const auto index=rows.TypedInteger(0);auto name=rows.TypedText(1);
    RequireRecord(parts_.size()<65536 && index==static_cast<std::int64_t>(parts_.size()) && !name.empty() &&
        (parts_.empty() || parts_.back()<name),"partition catalog is not canonical");
    parts_.push_back(std::move(name));
  }
  RequireRecord(!parts_.empty(),"partition catalog is empty");
  store_.RequireUnchanged();
}
FpgaId PartitionStoreView::Partition(std::uint64_t sequence) const {
  RequireRecord(sequence<parts_.size(),"partition identity is out of bounds");
  return store_.Id<FpgaTag>(sequence);
}
const std::string& PartitionStoreView::PartitionName(const FpgaId& partition) const {
  store_.Require(partition);
  RequireRecord(partition.sequence()<parts_.size(),"partition identity is out of bounds");
  return parts_[partition.sequence()];
}
AssignmentView::AssignmentView(const ArtifactRef& artifact):PartitionStoreView(AssignmentArtifact(artifact)) {
  auto row=store_.database().Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='source_instance_count'");
  RequireRecord(row.Step() && row.TypedInteger(0)>=0,"assignment source count is invalid");
  source_cells_=row.Integer(0);
  RequireRecord(!row.Step(),"assignment source count repeats");
  store_.RequireUnchanged();
}
void AssignmentView::VisitCopies(const std::function<void(const InstanceCopy&)>& visitor) {
  store_.RequireUnchanged();
  auto rows=store_.database().Prepare("SELECT source_instance_seq,part_index,copy_id,is_replica FROM copies "
      "ORDER BY source_instance_seq,is_replica,copy_id");
  std::uint64_t primary=0;
  while (rows.Step()) {
    const auto source=rows.TypedInteger(0),part=rows.TypedInteger(1),replica=rows.TypedInteger(3);
    auto name=rows.TypedText(2);
    RequireRecord(source>=0 && static_cast<std::uint64_t>(source)<source_cells_ && part>=0 &&
        static_cast<std::uint64_t>(part)<parts_.size() && (replica==0 || replica==1) && !name.empty(),
        "assignment copy identity is invalid");
    if (!replica) RequireRecord(static_cast<std::uint64_t>(source)==primary++,"assignment primary coverage is invalid");
    visitor({store_.Id<CellTag>(source),Partition(part),std::move(name),replica!=0});
  }
  RequireRecord(primary==source_cells_,"assignment primary coverage is incomplete");
  store_.RequireUnchanged();
}
SplitView::SplitView(const ArtifactRef& artifact):PartitionStoreView(SplitArtifact(artifact)) {}
void SplitView::VisitCopies(const FpgaId& partition,const std::function<void(const InstanceCopy&)>& visitor) {
  (void)PartitionName(partition);
  auto rows=store_.database().Prepare("SELECT c.source_instance_seq,coalesce(c.copy_id,s.original_id),c.is_replica,"
      "s.source_instance_seq FROM instance_copies c LEFT JOIN source_instances s "
      "ON s.source_instance_seq=c.source_instance_seq WHERE c.part_index=? ORDER BY c.source_instance_seq,c.is_replica");
  rows.BindInteger(1,partition.sequence());
  while (rows.Step()) {
    const auto source=rows.TypedInteger(0),replica=rows.TypedInteger(2);auto name=rows.TypedText(1);
    RequireRecord(source>=0 && !rows.IsNull(3) && !name.empty() && (replica==0 || replica==1),"split instance copy is invalid");
    visitor({store_.Id<CellTag>(source),partition,std::move(name),replica!=0});
  }
  store_.RequireUnchanged();
}
void SplitView::VisitEndpoints(const FpgaId& partition,const std::function<void(const SplitEndpoint&)>& visitor) {
  (void)PartitionName(partition);
  auto rows=store_.database().Prepare("SELECT e.source_net_seq,e.role,e.ordinal,e.source_instance_seq,"
      "e.synthetic_instance_id,e.port_seq,e.bit,e.synthetic,n.source_net_seq,s.source_instance_seq,p.seq "
      "FROM endpoint_segments e LEFT JOIN source_nets n ON n.source_net_seq=e.source_net_seq "
      "LEFT JOIN source_instances s ON s.source_instance_seq=e.source_instance_seq "
      "LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.part_index=? ORDER BY e.source_net_seq,e.role,e.ordinal");
  rows.BindInteger(1,partition.sequence());
  std::tuple<std::int64_t,std::int64_t,std::int64_t> previous{-1,-1,-1};
  while (rows.Step()) {
    const auto net=rows.TypedInteger(0),role=rows.TypedInteger(1),ordinal=rows.TypedInteger(2);
    const auto pin=rows.TypedInteger(5),bit=rows.TypedInteger(6),synthetic=rows.TypedInteger(7);
    const auto key=std::make_tuple(net,role,ordinal);
    RequireRecord(net>=0 && (role==0 || role==1) && ordinal>=0 && pin>0 && bit>=0 &&
        (synthetic==0 || synthetic==1) && !rows.IsNull(8) && !rows.IsNull(10) && previous<key,"split endpoint coordinate is invalid");
    std::optional<CellId> cell;std::string synthetic_cell;
    if (!rows.IsNull(3)) {
      const auto source=rows.TypedInteger(3);
      RequireRecord(source>=0 && !rows.IsNull(9) && !synthetic && rows.IsNull(4),"split endpoint cell is invalid");
      cell=store_.Id<CellTag>(source);
    } else if (synthetic) {
      synthetic_cell=rows.TypedText(4);RequireRecord(!synthetic_cell.empty(),"split synthetic endpoint is missing");
    } else RequireRecord(rows.IsNull(4),"split top endpoint has a synthetic identity");
    visitor({partition,store_.Id<NetTag>(net),static_cast<std::uint64_t>(ordinal),role==1,
        std::move(cell),std::move(synthetic_cell),store_.Id<PinTag>(pin),static_cast<std::uint64_t>(bit)});
    previous=key;
  }
  store_.RequireUnchanged();
}
}  // namespace emuflow::core
