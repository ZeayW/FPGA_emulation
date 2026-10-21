#include <cstdint>
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
  const auto root = std::filesystem::temp_directory_path() /
                    ("emuflow-core-artifact-" + std::to_string(ProcessId()));
  std::filesystem::create_directories(root);
  const auto target = root / "artifact.bin";
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
      writer.Commit();
    }
    {
      emuflow::core::BinaryReader reader(target.string());
      if (reader.Read<std::uint32_t>() != 42) {
        throw std::runtime_error("round-trip value disagrees");
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
  } catch (...) {
    std::filesystem::remove_all(root);
    throw;
  }
  std::filesystem::remove_all(root);
  return 0;
}
