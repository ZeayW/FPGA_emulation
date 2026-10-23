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
  } catch (...) {
    std::filesystem::remove_all(root);
    throw;
  }
  std::filesystem::remove_all(root);
  return 0;
}
