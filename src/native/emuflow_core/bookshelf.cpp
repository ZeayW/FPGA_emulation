// Typed physical export owned by libemuflow_core. The Python adapter consumes
// only a primitive-library signature and counts, never graph-sized pin data.
#include "emuflow_core/bookshelf.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/sqlite.hpp"

#include <filesystem>
#include <map>

namespace emuflow::core {
namespace {
using Integer = std::int64_t;
struct ScratchRelation {
  std::string path;
  bool owned = false;
  ~ScratchRelation() {
    if (owned) { std::error_code error; std::filesystem::remove(path, error); }
  }
};
void Token(const std::string& value) {
  if (value.empty() || value.find_first_of(" \t\r\n\0", 0, 5) != std::string::npos)
    throw std::runtime_error("invalid Bookshelf token");
}
}  // namespace

void ExportBookshelf(const ArtifactRef& mapped_ref, const ArtifactRef& atoms_ref,
                     const std::string& nets_path, const std::string& report_path,
                     const std::string& spool_path) {
  if (mapped_ref.schema() != "xilinx-mapped-index-v2" ||
      atoms_ref.schema() != "openparf-atom-index-private-v1")
    throw StageError(ErrorCode::kInvalidArtifact,"Bookshelf input schema mismatch");
  mapped_ref.RequireUnchanged();
  atoms_ref.RequireUnchanged();
  SqliteDatabase mapped(mapped_ref.path(), SqliteDatabase::Mode::kImmutableReadOnly);
  SqliteDatabase atoms(atoms_ref.path(), SqliteDatabase::Mode::kImmutableReadOnly);
  ScratchRelation scratch{spool_path};
  SqliteDatabase spool(spool_path, SqliteDatabase::Mode::kCreateExclusive);
  scratch.owned = true;
  auto schema = mapped.Prepare("SELECT value FROM metadata WHERE key='schema'");
  if (!schema.Step() || schema.TypedText(0) != "emuflow.xilinx-mapped-index/v2" || schema.Step())
    throw StageError(ErrorCode::kInvalidArtifact,"Bookshelf exporter requires mapped index v2");
  mapped.Execute("PRAGMA cache_size=-65536");
  spool.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                "PRAGMA temp_store=FILE; PRAGMA cache_size=-65536;"
                "CREATE TABLE endpoints(net INTEGER NOT NULL, seq INTEGER PRIMARY KEY,"
                "atom INTEGER NOT NULL, port TEXT NOT NULL, driver INTEGER NOT NULL);"
                "BEGIN");
  auto cells = mapped.Prepare("SELECT id,type FROM cells ORDER BY id");
  auto pins = mapped.Prepare("SELECT cell_id,port,port_bit,port_width,direction,"
                         "value_kind,net_bit,constant FROM cell_pins "
                         "ORDER BY cell_id,port,port_bit");
  auto atom = atoms.Prepare("SELECT seq,instance,cell_type FROM atoms ORDER BY seq");
  auto insert = spool.Prepare("INSERT INTO endpoints VALUES (?,?,?,?,?)");
  bool has_pin = pins.Step();
  bool has_atom = atom.Step();
  Integer atom_count = 0, endpoint_count = 0;
  std::map<std::pair<std::string, std::string>, int> library;
  while (cells.Step()) {
    const auto name = cells.TypedText(0), type = cells.TypedText(1);
    const bool constant = type == "GND" || type == "VCC";
    Integer ordinal = -1;
    if (!constant) {
      if (!has_atom || atom.TypedInteger(0) != atom_count + 1 || atom.TypedText(1) != name ||
          atom.TypedText(2) != type)
        throw std::runtime_error("OpenPARF atom does not match mapped index");
      Token(type);
      ordinal = atom_count++;
      has_atom = atom.Step();
    }
    if (has_pin && pins.TypedText(0) < name)
      throw std::runtime_error("mapped pin references missing cell");
    std::string active_port;
    Integer width = 0, next_bit = 0, direction = -1;
    while (has_pin && pins.TypedText(0) == name) {
      const auto port = pins.TypedText(1);
      const auto bit = pins.TypedInteger(2), pin_width = pins.TypedInteger(3), pin_direction = pins.TypedInteger(4);
      if (port != active_port || next_bit == 0) {
        if (next_bit != width)
          throw std::runtime_error("indexed mapped port is incomplete");
        active_port = port;
        width = pin_width;
        direction = pin_direction;
        next_bit = 0;
      }
      if (width <= 0 || width != pin_width || bit != next_bit || bit >= width ||
          direction != pin_direction)
        throw std::runtime_error("indexed mapped pin row is invalid");
      ++next_bit;
      if (!constant) {
        if (direction != 0 && direction != 1)
          throw std::runtime_error("mapped cell has unsupported pin direction");
        const auto expanded = width == 1 ? port : port + "[" + std::to_string(bit) + "]";
        Token(expanded);
        const auto result = library.emplace(std::make_pair(type, expanded), direction);
        if (!result.second && result.first->second != direction)
          throw std::runtime_error("primitive has inconsistent pin direction");
        const auto kind = pins.TypedInteger(5);
        if (kind == 0 && pins.IsNull(7)) {
          insert.BindInteger(1, pins.TypedInteger(6));
          insert.BindInteger(2, endpoint_count++);
          insert.BindInteger(3, ordinal);
          insert.BindText(4, expanded);
          insert.BindInteger(5, direction);
          insert.Execute();
        } else if (kind != 1 || !pins.IsNull(6) || pins.IsNull(7)) {
          throw std::runtime_error("indexed mapped pin value is invalid");
        }
      }
      has_pin = pins.Step();
    }
    if (next_bit != width)
      throw std::runtime_error("indexed mapped port is incomplete");
  }
  if (has_pin || has_atom)
    throw std::runtime_error("mapped index does not cover every OpenPARF atom/pin");
  spool.Execute("COMMIT; CREATE INDEX endpoints_net_order ON endpoints(net,driver DESC,seq)");

  AtomicBinaryWriter nets_writer(nets_path), summary_writer(report_path);
  auto& nets = nets_writer.stream();
  Integer emitted = 0, dropped = 0;
  // One covering index supports both degree checks and driver-first emission.
  // Two ordered cursors replace the net-sized fanout vector without building
  // another relation or changing the stable textual order.
  auto degrees = spool.Prepare(
      "SELECT net,COUNT(*),SUM(driver) FROM endpoints GROUP BY net ORDER BY net");
  auto endpoints = spool.Prepare(
      "SELECT net,atom,port,driver FROM endpoints ORDER BY net,driver DESC,seq");
  bool has_endpoint = endpoints.Step();
  while (degrees.Step()) {
    const auto net = degrees.TypedInteger(0), degree = degrees.TypedInteger(1);
    if (degrees.TypedInteger(2) > 1 && degree > 1)
      throw std::runtime_error("mapped net has multiple atomic-resource drivers");
    if (degree == 1) ++dropped;
    else nets << "net n" << emitted++ << ' ' << degree << '\n';
    for (Integer i = 0; i < degree; ++i) {
      if (!has_endpoint || endpoints.TypedInteger(0) != net)
        throw std::runtime_error("Bookshelf endpoint coverage is invalid");
      if (degree > 1)
        nets << "  a" << endpoints.TypedInteger(1) << ' ' << endpoints.TypedText(2) << '\n';
      has_endpoint = endpoints.Step();
    }
    if (degree > 1) nets << "endnet\n";
    if (!nets) throw std::runtime_error("cannot write Bookshelf nets");
  }
  if (has_endpoint) throw std::runtime_error("Bookshelf endpoint coverage is invalid");
  auto& report = summary_writer.stream();
  report << "EMUFLOW_BOOKSHELF_V1\t" << atom_count << '\t' << emitted << '\t' << dropped << '\n';
  for (const auto& item : library)
    report << item.first.first << '\t' << item.first.second << '\t' << item.second << '\n';
  mapped_ref.RequireUnchanged();
  atoms_ref.RequireUnchanged();
  nets_writer.Commit();
  summary_writer.Commit();
}
}  // namespace emuflow::core
