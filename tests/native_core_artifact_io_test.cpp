#include <cstdint>
#include <array>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <type_traits>

#if defined(_WIN32)
#include <process.h>
#else
#include <unistd.h>
#endif

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"
#include <set>
#include "emuflow_core/stage_runtime.hpp"

namespace {
struct CellTag {};
struct NetTag {};
using CellId = emuflow::core::TypedId<CellTag>;
using NetId = emuflow::core::TypedId<NetTag>;

std::uint64_t ProcessId() {
#if defined(_WIN32)
  return static_cast<std::uint64_t>(_getpid());
#else
  return static_cast<std::uint64_t>(getpid());
#endif
}
}  // namespace

int main() {
  static_assert(!std::is_convertible<CellId, NetId>::value,
                "typed artifact identities must not mix implicitly");
  {
    emuflow::core::Sha256 empty;
    const std::array<unsigned char, 32> empty_expected = {
        0xe3, 0xb0, 0xc4, 0x42, 0x98, 0xfc, 0x1c, 0x14,
        0x9a, 0xfb, 0xf4, 0xc8, 0x99, 0x6f, 0xb9, 0x24,
        0x27, 0xae, 0x41, 0xe4, 0x64, 0x9b, 0x93, 0x4c,
        0xa4, 0x95, 0x99, 0x1b, 0x78, 0x52, 0xb8, 0x55};
    if (empty.finish() != empty_expected) {
      throw std::runtime_error("empty SHA-256 vector disagrees");
    }
    emuflow::core::Sha256 abc;
    const std::array<unsigned char, 3> payload = {'a', 'b', 'c'};
    abc.update(payload.data(), payload.size());
    const std::array<unsigned char, 32> abc_expected = {
        0xba, 0x78, 0x16, 0xbf, 0x8f, 0x01, 0xcf, 0xea,
        0x41, 0x41, 0x40, 0xde, 0x5d, 0xae, 0x22, 0x23,
        0xb0, 0x03, 0x61, 0xa3, 0x96, 0x17, 0x7a, 0x9c,
        0xb4, 0x10, 0xff, 0x61, 0xf2, 0x00, 0x15, 0xad};
    if (abc.finish() != abc_expected) {
      throw std::runtime_error("SHA-256 vector disagrees");
    }
    std::uint32_t crc = 0;
    const std::array<unsigned char, 9> digits = {
        '1', '2', '3', '4', '5', '6', '7', '8', '9'};
    emuflow::core::UpdateCrc32(crc, digits.data(), digits.size());
    if (crc != 0xcbf43926U) {
      throw std::runtime_error("CRC-32 vector disagrees");
    }
  }
  const auto root = std::filesystem::temp_directory_path() /
                    ("emuflow-core-artifact-" + std::to_string(ProcessId()));
  std::filesystem::create_directories(root);
  const auto target = root / "artifact.bin";
  const auto database_path = root / "artifact.sqlite";
  try {
    std::string staged_path;
    {
      emuflow::core::StagedArtifact staged(target.string(),{".spool"});
      staged_path=staged.path();
      std::ofstream sidecar(staged_path+".spool"); sidecar<<"private"; sidecar.close();
      if (!std::filesystem::exists(staged_path)) throw std::runtime_error("exclusive staging is missing");
    }
    if (std::filesystem::exists(staged_path) || std::filesystem::exists(staged_path+".spool"))
      throw std::runtime_error("staged artifacts were retained");
    {
      std::ofstream original(target, std::ios::binary | std::ios::trunc);
      original << "old";
    }
    {
      emuflow::core::AtomicBinaryWriter abandoned(target.string());
      abandoned.Write<std::uint32_t>(7);
    }
    {
      std::ifstream unchanged(target, std::ios::binary);
      std::string value((std::istreambuf_iterator<char>(unchanged)), {});
      if (value != "old") throw std::runtime_error("abandoned write was published");
    }
    {
      emuflow::core::AtomicBinaryWriter writer(target.string());
      writer.Write<std::uint32_t>(42);
      const std::array<std::uint32_t, 2> trailing = {7, 9};
      writer.WriteBytes(trailing.data(),
                        trailing.size() * sizeof(std::uint32_t));
      writer.Commit();
    }
    {
      emuflow::core::BinaryReader reader(target.string());
      if (reader.Read<std::uint32_t>() != 42) {
        throw std::runtime_error("round-trip value disagrees");
      }
      if (reader.Read<std::uint32_t>() != 7 ||
          reader.Read<std::uint32_t>() != 9) {
        throw std::runtime_error("bulk round-trip value disagrees");
      }
      reader.RequireEnd();
    }
    {
      emuflow::core::BinaryReader reader(target.string());
      reader.Seek(8);
      if (reader.Read<std::uint32_t>() != 9) throw std::runtime_error("indexed read disagrees");
      reader.Seek(0);
      if (reader.Read<std::uint32_t>() != 42) throw std::runtime_error("reader rewind disagrees");
      reader.Seek(reader.size());
      reader.RequireEnd();
      bool rejected = false;
      try { reader.Seek(reader.size() + 1); } catch (const std::runtime_error&) { rejected = true; }
      if (!rejected) throw std::runtime_error("out-of-bounds seek was accepted");
    }
    {
      emuflow::core::AtomicBinaryWriter writer(target.string());
      writer.Write<std::uint64_t>(0x1234);
      writer.Write<std::uint32_t>(7);
      writer.Write<std::uint32_t>(42);
      writer.Commit();
      emuflow::core::BinaryReader reader(target.string());
      emuflow::core::ReadAndValidateHeader(
          reader, emuflow::core::ArtifactSchema{0x1234, 7, "test artifact"});
      if (reader.Read<std::uint32_t>() != 42 || reader.remaining() != 0) {
        throw std::runtime_error("schema round-trip value disagrees");
      }
      reader.RequireEnd();
    }
    {
      std::ofstream trailing(target, std::ios::binary | std::ios::app);
      trailing.put('\0');
    }
    bool rejected = false;
    try {
      emuflow::core::BinaryReader reader(target.string());
      (void)reader.Read<std::uint32_t>();
      reader.RequireEnd();
    } catch (const std::runtime_error&) {
      rejected = true;
    }
    if (!rejected) throw std::runtime_error("trailing bytes were accepted");
    {
      emuflow::core::SqliteDatabase database(
          database_path.string(),
          emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      database.Execute("CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)");
      {
        emuflow::core::SqliteTransaction transaction(database);
        auto insert = database.Prepare("INSERT INTO records VALUES(?,?)");
        insert.BindInteger(1, 7);
        insert.BindText(2, "typed");
        insert.Execute();
        transaction.Commit();
      }
      auto select = database.Prepare(
          "SELECT id,value FROM records ORDER BY id");
      if (!select.Step() || select.Integer(0) != 7 ||
          select.Text(1) != "typed" || select.Blob(1) != "typed" || select.Step()) {
        throw std::runtime_error("SQLite RAII round trip disagrees");
      }
    }
    {
      emuflow::core::SqliteDatabase database(
          database_path.string(),
          emuflow::core::SqliteDatabase::Mode::kReadOnly);
      auto count = database.Prepare("SELECT COUNT(*) FROM records");
      if (!count.Step() || count.Integer(0) != 1) {
        throw std::runtime_error("SQLite read-only view disagrees");
      }
    }

    // Every registered public SQLite envelope uses the same reader/writer.
    using namespace emuflow::core;
    std::set<std::string> registered_names, round_trip_names;
    std::vector<ArtifactRef> stores;
    for (const auto& schema:SchemaRegistry()) {
      if (!registered_names.insert(schema.name).second) throw std::runtime_error("duplicate schema name");
      if (schema.encoding!=ArtifactEncoding::kSqlite || !schema.sqlite_schema_query) continue;
      if (std::string(schema.sqlite_schema_query).find("pragma_table_info")!=std::string::npos) continue;
      round_trip_names.insert(schema.name);
      auto path=(root/(std::string(schema.name)+".sqlite")).string();
      SqliteArtifactWriter writer(path,schema.name);
      {
        auto& db=writer.database();
        const std::string query=schema.sqlite_schema_query;
        if (std::string(schema.name)=="transport-store-v1") {
          db.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
                     "INSERT INTO metadata VALUES ('platform','\"board\"'),('design','\"design\"'),('frame_slots','4')");
        } else if (query.find("FROM manifest")!=std::string::npos) {
          db.Execute("CREATE TABLE manifest(singleton INTEGER PRIMARY KEY,schema TEXT)");
          auto put=db.Prepare("INSERT INTO manifest VALUES (1,?)");put.BindText(1,schema.sqlite_schema_value);put.Execute();
        } else {
          db.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)");
          const bool raw=query.find("SELECT value ")==0;
          const bool header=query.find("'header'")!=std::string::npos;
          const std::string key=header ? "header" : query.find("'logical_schema'")!=std::string::npos ? "logical_schema" :
              query.find("'storage_schema'")!=std::string::npos ? "storage_schema" : "schema";
          const auto value=std::string(schema.name)=="chimew-bank-input-store-v1" ?
              "{\"schema\":\"emuflow.chimew-bank-channel-input/v2\"}" : std::string(schema.name)=="chimew-binding-store-v1" ?
              "{\"schema\":\"emuflow.chimew-phase6-electrical-binding/v3\"}" : raw ? std::string(schema.sqlite_schema_value) : header ?
              "{\"schema\":"+JsonQuote(schema.sqlite_schema_value)+"}" : JsonQuote(schema.sqlite_schema_value);
          auto put=db.Prepare("INSERT INTO metadata VALUES (?,?)");put.BindText(1,key);put.BindText(2,value);put.Execute();
        }
        db.Execute("CREATE TABLE records(seq INTEGER PRIMARY KEY,value TEXT UNIQUE);INSERT INTO records VALUES (0,'one'),(1,'two')");
      }
      auto ref=writer.Commit();
      SqliteStore view(ref);
      auto rows=view.database().Prepare("SELECT seq,value FROM records ORDER BY seq");
      for (int i=0;i<2;++i) if (!rows.Step() || rows.TypedInteger(0)!=i || rows.TypedText(1)!=(i ? "two" : "one"))
        throw std::runtime_error("shared store ordered round trip disagrees");
      if (rows.Step()) throw std::runtime_error("shared store added rows");
      stores.push_back(ref);
    }
    const std::set<std::string> expected_public_stores = {
        "chimew-bank-input-store-v1",
        "position-hints-store-v1", "chimew-bank-report-store-v1",
        "transport-store-v1", "chimew-binding-store-v1", "pin-plan-store-v1",
        "openparf-atomic-source-store-v1", "openparf-placement-certificate-v3",
        "xilinx-physical-handoff-v1", "vivado-lineage-store-v2",
        "vivado-source-functions-v1", "emuir-compact-v2", "emuir-indexed-v1",
        "assignment-index-v3", "assignment-index-v4", "split-netlists-v2",
        "boundary-identity-store-v1", "boundary-timing-store-v1",
        "logic-segment-identity-store-v1", "logic-segment-timing-store-v1",
        "local-path-timing-store-v1", "xilinx-mapped-index-v2",
        "system-route-store-v3", "tdm-schedule-store-v4"};
    if (round_trip_names != expected_public_stores || stores.size() != round_trip_names.size())
      throw std::runtime_error("public store registry coverage is incomplete");
    ArtifactScope a(stores[0]),b(stores[1]);
    auto first=a.Id<emuflow::core::NetTag>(0); a.Require(first);
    bool foreign=false;try { b.Require(first); } catch (const StageError& e) { foreign=e.code()==ErrorCode::kMixedGeneration; }
    if (!foreign) throw std::runtime_error("equal cross-store integers were treated as aliases");
    // A borrowed statement retains its connection and staging after the writer
    // is abandoned. Its last release closes SQLite before retiring scratch.
    SqliteStatement borrowed;std::string borrowed_path;
    {
      SqliteArtifactWriter abandoned((root/"borrow.sqlite").string(),"emuir-indexed-v1");
      auto& db=abandoned.database();
      db.Execute("CREATE TABLE x(value TEXT);INSERT INTO x VALUES ('retained')");
      { auto name=db.Prepare("PRAGMA database_list");if (!name.Step()) throw std::runtime_error("missing connection path");borrowed_path=name.TypedText(2); }
      borrowed=db.Prepare("SELECT value FROM x");
    }
    if (!std::filesystem::exists(borrowed_path) || !borrowed.Step() || borrowed.TypedText(0)!="retained")
      throw std::runtime_error("borrowed statement lost its store owner");
    borrowed=SqliteStatement{};
    if (std::filesystem::exists(borrowed_path)) throw std::runtime_error("closed cursor retained private staging");
    // Rejected publication preserves the old generation, including active
    // transactions, active statements, tampered headers and logical envelopes.
    const auto prior=stores.back();
    for (int invalid=0;invalid<4;++invalid) {
      bool rejected=false;
      try {
        SqliteArtifactWriter writer(prior.path(),prior.schema());auto& db=writer.database();
        db.Execute("CREATE TABLE metadata(key TEXT,value TEXT);INSERT INTO metadata VALUES ('logical_schema','\"emuflow.tdm-schedule/v1\"')");
        SqliteStatement held;
        if (invalid==0) db.Execute("BEGIN");
        if (invalid==1) held=db.Prepare("SELECT 1");
        if (invalid==2) db.Execute("PRAGMA user_version=999");
        if (invalid==3) db.Execute("UPDATE metadata SET value='\"wrong\"'");
        writer.Commit();
      } catch (const std::exception&) { rejected=true; }
      if (!rejected) throw std::runtime_error("invalid SQLite publication succeeded");
      prior.RequireUnchanged();
    }
    bool alias=false;
    try { SqliteArtifactWriter writer(prior.path(),prior.schema(),{prior}); }
    catch (const StageError& e) { alias=e.code()==ErrorCode::kInvalidRequest; }
    if (!alias) throw std::runtime_error("shared writer replaced its source");
    const auto stage_path = (root / "stage.bin").string();
    const auto report_path = (root / "stage-result.json").string();
    {
      emuflow::core::AtomicBinaryWriter writer(stage_path);
      writer.Write(std::uint64_t{0x3142534d44544545ULL});
      writer.Write(std::uint32_t{1});
      writer.Commit();
    }
    using emuflow::core::ArtifactRef;
    using emuflow::core::ErrorCode;
    const auto reference = ArtifactRef::Bind(stage_path, "exact-schedule-board-request-v1");
    reference.RequireUnchanged();
    const auto success = emuflow::core::RunStage("test.stage", [&]() {
      return std::vector<ArtifactRef>{reference};
    }, [](const auto&) {});
    if (!success.ok() || success.inputs.size() != 1 || success.metrics.wall_seconds < 0) {
      throw std::runtime_error("native stage success contract disagrees");
    }
    emuflow::core::PublishStageResult(report_path, success);
    bool alias_rejected = false;
    try { emuflow::core::PublishStageResult(stage_path, success); }
    catch (const emuflow::core::StageError& e) { alias_rejected = e.code() == ErrorCode::kInvalidRequest; }
    if (!alias_rejected) throw std::runtime_error("stage report replaced its input");
    reference.RequireUnchanged();
    const auto old_time = std::filesystem::last_write_time(stage_path);
    {
      emuflow::core::AtomicBinaryWriter writer(stage_path);
      writer.Write(std::uint64_t{0x3142534d44544545ULL});
      writer.Write(std::uint32_t{1}); writer.Commit();
    }
    std::filesystem::last_write_time(stage_path, old_time);
    bool stale_body_ran = false;
    const auto stale = emuflow::core::RunStage("test.stale", [&]() {
      return std::vector<ArtifactRef>{reference};
    }, [&](const auto&) { stale_body_ran = true; });
    if (stale.error != ErrorCode::kMixedGeneration || stale_body_ran) {
      throw std::runtime_error("replaced input generation entered a native stage");
    }
    const auto current_reference = ArtifactRef::Bind(stage_path, "exact-schedule-board-request-v1");
    const auto changed = emuflow::core::RunStage("test.changed", [&]() {
      return std::vector<ArtifactRef>{current_reference};
    }, [&](const auto&) {
      std::ofstream writer(stage_path, std::ios::binary | std::ios::app);
      writer.put('x');
    });
    if (changed.error != ErrorCode::kMixedGeneration) {
      throw std::runtime_error("changed native input was accepted");
    }
    const auto unknown = emuflow::core::RunStage("test.unknown", [&]() {
      return std::vector<ArtifactRef>{ArtifactRef::Bind(stage_path, "unknown-version")};
    }, [](const auto&) {});
    if (unknown.error != ErrorCode::kInvalidRequest) {
      throw std::runtime_error("unknown schema silently fell back");
    }
    const auto malformed = emuflow::core::RunStage("test.bad-envelope", [&]() {
      return std::vector<ArtifactRef>{ArtifactRef::Bind(stage_path, "exact-topology-v3")};
    }, [](const auto&) {});
    if (malformed.error != ErrorCode::kInvalidArtifact) {
      throw std::runtime_error("mixed native envelope was accepted");
    }
    const auto pipe = ArtifactRef::Bind("-", "patron-input-v1", true);
    if (!pipe.is_pipe()) throw std::runtime_error("native pipe was treated as a file");
    // File consumers must never observe a partial result or an output from
    // changed source generations, including when a producer fails late.
    {
      const auto text_source = (root / "text-source").string();
      const auto text_output = (root / "text-output").string();
      { std::ofstream file(text_source); file << "source"; }
      const auto read_text = [](const std::string& path) {
        std::ifstream file(path); return std::string(std::istreambuf_iterator<char>(file), {});
      };
      const auto attempt = [&](const std::function<void(std::ostream&)>& produce) {
        return emuflow::core::RunStage("test.publish-text", [&]() {
          return std::vector<ArtifactRef>{ArtifactRef::Bind(text_source,"chimew-rudy-input-v1")};
        }, [&](const auto& inputs) {
          emuflow::core::PublishTextArtifact(text_output,inputs,produce);
        });
      };
      { std::ofstream file(text_output); file << "old"; }
      const auto committed = attempt([&](std::ostream& output) {
        output << "new";
        if (read_text(text_output) != "old") throw std::runtime_error("uncommitted text became visible");
      });
      if (!committed.ok() || read_text(text_output) != "new")
        throw std::runtime_error("shared text publication changed bytes");
      const auto failed = attempt([](std::ostream& output) {
        output << "partial"; throw std::runtime_error("injected producer failure");
      });
      if (failed.ok() || read_text(text_output) != "new")
        throw std::runtime_error("failed text producer replaced its prior result");
      const auto changed = attempt([&](std::ostream& output) {
        output << "untrusted";
        std::ofstream mutation(text_source,std::ios::app); mutation << "mutation";
      });
      if (changed.error != ErrorCode::kMixedGeneration || read_text(text_output) != "new")
        throw std::runtime_error("changed text source was published");
      const auto unwritable = attempt([](std::ostream& output) {
        output << "partial"; output.setstate(std::ios::badbit);
      });
      if (unwritable.error != ErrorCode::kPublicationFailed || read_text(text_output) != "new")
        throw std::runtime_error("failed output stream replaced its prior result");
      bool alias_rejected = false;
      try {
        emuflow::core::PublishTextArtifact(text_source,
            {ArtifactRef::Bind(text_source,"chimew-rudy-input-v1")},
            [](std::ostream&) { throw std::runtime_error("aliased producer executed"); });
      } catch (const emuflow::core::StageError& error) {
        alias_rejected = error.code() == ErrorCode::kInvalidRequest;
      }
      if (!alias_rejected) throw std::runtime_error("shared text publisher accepted source alias");
      for (const auto& file : std::filesystem::directory_iterator(root))
        if (file.path().filename().string().find("text-output.tmp.") == 0)
          throw std::runtime_error("failed text publication retained private scratch");
    }
    // The binary writer checks its bound generation at the actual commit,
    // including inode replacement with unchanged bytes and timestamp.
    {
      const auto source=(root/"binary-source").string();
      const auto destination=(root/"binary-output").string();
      const auto read=[](const std::string& path) {
        std::ifstream input(path,std::ios::binary);
        return std::string(std::istreambuf_iterator<char>(input),{});
      };
      { std::ofstream file(source); file<<"source"; }
      { std::ofstream file(destination); file<<"previous"; }
      for (int mutation=0;mutation<3;++mutation) {
        const auto ref=ArtifactRef::Bind(source,"chimew-rudy-input-v1");
        std::string temporary;
        bool rejected=false;
        try {
          emuflow::core::AtomicBinaryWriter writer(destination,{ref});
          temporary=writer.temporary_path();
          writer.stream()<<"untrusted";
          if (mutation==0) {
            std::ofstream changed(source,std::ios::app); changed<<"mutation";
          } else if (mutation==1) {
            const auto timestamp=std::filesystem::last_write_time(source);
            std::filesystem::copy_file(source,source+".replacement");
            std::filesystem::last_write_time(source+".replacement",timestamp);
            std::filesystem::rename(source+".replacement",source);
          } else {
            std::filesystem::remove(source);
          }
          writer.Commit();
        } catch (const emuflow::core::StageError& error) {
          rejected=error.code()==ErrorCode::kMixedGeneration;
        }
        if (!rejected || read(destination)!="previous" || std::filesystem::exists(temporary))
          throw std::runtime_error("changed binary source replaced its prior result or retained scratch");
      }
      { std::ofstream file(source); file<<"fresh"; }
      const auto alias=source+".alias";
      std::filesystem::create_hard_link(source,alias);
      const auto ref=ArtifactRef::Bind(source,"chimew-rudy-input-v1");
      bool alias_rejected=false;
      try { emuflow::core::AtomicBinaryWriter writer(alias,{ref}); }
      catch (const emuflow::core::StageError& error) {
        alias_rejected=error.code()==ErrorCode::kInvalidRequest;
      }
      if (!alias_rejected || read(source)!="fresh")
        throw std::runtime_error("binary writer accepted a hard-link source alias");
      {
        emuflow::core::AtomicBinaryWriter writer(destination,{ref});
        writer.stream()<<"accepted"; writer.Commit();
      }
      if (read(destination)!="accepted") throw std::runtime_error("bound binary commit failed");
      const auto staged=destination+".private";
      { std::ofstream file(staged); file<<"untrusted"; }
      { std::ofstream changed(source,std::ios::app); changed<<"changed"; }
      bool rejected=false;
      try { emuflow::core::PublishTemporaryFile(staged,destination,{ref}); }
      catch (const emuflow::core::StageError& error) {
        rejected=error.code()==ErrorCode::kMixedGeneration;
      }
      if (!rejected || read(destination)!="accepted")
        throw std::runtime_error("direct publication replaced an artifact from a changed source");
      std::filesystem::remove(staged);
    }
    const auto sqlite_artifact = (root / "source-artifact.sqlite").string();
    {
      emuflow::core::SqliteDatabase db(sqlite_artifact, emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      db.Execute("PRAGMA application_id=0x454d5549; PRAGMA user_version=2; CREATE TABLE records(id INTEGER);"
                 "CREATE TABLE metadata(key TEXT,value TEXT);INSERT INTO metadata VALUES('storage_schema','emuflow.emuir-sqlite/v2')");
    }
    const auto sqlite_ref = ArtifactRef::Bind(sqlite_artifact, "emuir-compact-v2");
    {
      std::ofstream wal(sqlite_artifact + "-wal"); wal << "active-writer";
    }
    bool wal_rejected = false;
    try { sqlite_ref.RequireUnchanged(); }
    catch (const emuflow::core::StageError& e) { wal_rejected = e.code() == ErrorCode::kMixedGeneration; }
    if (!wal_rejected) throw std::runtime_error("mutable SQLite sidecar was accepted");
    std::filesystem::remove(sqlite_artifact + "-wal");
  } catch (...) {
    std::filesystem::remove_all(root);
    throw;
  }
  std::filesystem::remove_all(root);
  return 0;
}
