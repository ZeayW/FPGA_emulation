// Stream the typed mapped-pin relation directly to OpenPARF Bookshelf.
// Python owns the validated physical contract and atomic publication. This
// process owns the graph-sized pin traversal, ordering, and text emission.
#include <sqlite3.h>

#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

namespace {
using Integer = sqlite3_int64;

struct Database {
  sqlite3* db = nullptr;
  Database(const std::string& path, int flags) {
    if (sqlite3_open_v2(path.c_str(), &db, flags, nullptr) != SQLITE_OK) {
      const std::string error = db ? sqlite3_errmsg(db) : "allocation failed";
      sqlite3_close(db);
      db = nullptr;
      throw std::runtime_error("cannot open Bookshelf relation: " + error);
    }
  }
  ~Database() { sqlite3_close(db); }
  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;
  void Exec(const char* sql) {
    if (sqlite3_exec(db, sql, nullptr, nullptr, nullptr) != SQLITE_OK)
      throw std::runtime_error(sqlite3_errmsg(db));
  }
};

struct Statement {
  sqlite3_stmt* stmt = nullptr;
  explicit Statement(Database& db, const char* sql) {
    if (sqlite3_prepare_v2(db.db, sql, -1, &stmt, nullptr) != SQLITE_OK)
      throw std::runtime_error(sqlite3_errmsg(db.db));
  }
  ~Statement() { sqlite3_finalize(stmt); }
  Statement(const Statement&) = delete;
  Statement& operator=(const Statement&) = delete;
  bool Next() {
    const int result = sqlite3_step(stmt);
    if (result == SQLITE_ROW) return true;
    if (result != SQLITE_DONE)
      throw std::runtime_error(sqlite3_errmsg(sqlite3_db_handle(stmt)));
    return false;
  }
  std::string Text(int column) const {
    if (sqlite3_column_type(stmt, column) != SQLITE_TEXT)
      throw std::runtime_error("Bookshelf relation has invalid text");
    return std::string(reinterpret_cast<const char*>(sqlite3_column_text(stmt, column)),
                       sqlite3_column_bytes(stmt, column));
  }
  Integer Int(int column) const {
    if (sqlite3_column_type(stmt, column) != SQLITE_INTEGER)
      throw std::runtime_error("Bookshelf relation has invalid integer");
    return sqlite3_column_int64(stmt, column);
  }
  bool Null(int column) const {
    return sqlite3_column_type(stmt, column) == SQLITE_NULL;
  }
  void Bind(int column, Integer value) {
    if (sqlite3_bind_int64(stmt, column, value) != SQLITE_OK)
      throw std::runtime_error("cannot bind Bookshelf integer");
  }
  void Bind(int column, const std::string& value) {
    if (sqlite3_bind_text(stmt, column, value.data(), static_cast<int>(value.size()),
                          SQLITE_TRANSIENT) != SQLITE_OK)
      throw std::runtime_error("cannot bind Bookshelf text");
  }
  void Insert() {
    if (Next()) throw std::runtime_error("unexpected Bookshelf insert result");
    if (sqlite3_reset(stmt) != SQLITE_OK)
      throw std::runtime_error("cannot reset Bookshelf insert");
  }
};

void Token(const std::string& value) {
  if (value.empty() || value.find_first_of(" \t\r\n\0", 0, 5) != std::string::npos)
    throw std::runtime_error("invalid Bookshelf token");
}

void Export(const char* mapped_path, const char* atoms_path,
            const char* nets_path, const char* report_path, const char* spool_path) {
  Database mapped(mapped_path, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI);
  Database atoms(atoms_path, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI);
  Database spool(spool_path, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE);
  Statement schema(mapped, "SELECT value FROM metadata WHERE key='schema'");
  if (!schema.Next() || schema.Text(0) != "emuflow.xilinx-mapped-index/v2")
    throw std::runtime_error("Bookshelf exporter requires mapped index v2");
  mapped.Exec("PRAGMA cache_size=-65536");
  spool.Exec("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
             "PRAGMA temp_store=FILE; PRAGMA cache_size=-65536;"
             "CREATE TABLE endpoints(net INTEGER NOT NULL, seq INTEGER PRIMARY KEY,"
             "atom INTEGER NOT NULL, port TEXT NOT NULL, driver INTEGER NOT NULL);"
             "BEGIN");
  Statement cells(mapped, "SELECT id,type FROM cells ORDER BY id");
  Statement pins(mapped, "SELECT cell_id,port,port_bit,port_width,direction,"
                         "value_kind,net_bit,constant FROM cell_pins "
                         "ORDER BY cell_id,port,port_bit");
  Statement atom(atoms, "SELECT seq,instance,cell_type FROM atoms ORDER BY seq");
  Statement insert(spool, "INSERT INTO endpoints VALUES (?,?,?,?,?)");
  bool has_pin = pins.Next();
  bool has_atom = atom.Next();
  Integer atom_count = 0, endpoint_count = 0;
  std::map<std::pair<std::string, std::string>, int> library;
  while (cells.Next()) {
    const auto name = cells.Text(0), type = cells.Text(1);
    const bool constant = type == "GND" || type == "VCC";
    Integer ordinal = -1;
    if (!constant) {
      if (!has_atom || atom.Int(0) != atom_count + 1 || atom.Text(1) != name ||
          atom.Text(2) != type)
        throw std::runtime_error("OpenPARF atom does not match mapped index");
      Token(type);
      ordinal = atom_count++;
      has_atom = atom.Next();
    }
    if (has_pin && pins.Text(0) < name)
      throw std::runtime_error("mapped pin references missing cell");
    std::string active_port;
    Integer width = 0, next_bit = 0, direction = -1;
    while (has_pin && pins.Text(0) == name) {
      const auto port = pins.Text(1);
      const auto bit = pins.Int(2), pin_width = pins.Int(3), pin_direction = pins.Int(4);
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
        const auto kind = pins.Int(5);
        if (kind == 0 && pins.Null(7)) {
          insert.Bind(1, pins.Int(6));
          insert.Bind(2, endpoint_count++);
          insert.Bind(3, ordinal);
          insert.Bind(4, expanded);
          insert.Bind(5, direction);
          insert.Insert();
        } else if (kind != 1 || !pins.Null(6) || pins.Null(7)) {
          throw std::runtime_error("indexed mapped pin value is invalid");
        }
      }
      has_pin = pins.Next();
    }
    if (next_bit != width)
      throw std::runtime_error("indexed mapped port is incomplete");
  }
  if (has_pin || has_atom)
    throw std::runtime_error("mapped index does not cover every OpenPARF atom/pin");
  spool.Exec("COMMIT; CREATE INDEX endpoints_net_order ON endpoints(net,seq)");

  std::ofstream nets(nets_path, std::ios::binary);
  if (!nets) throw std::runtime_error("cannot create Bookshelf nets");
  using Pin = std::tuple<Integer, std::string, bool>;
  std::vector<Pin> fanout;
  Integer emitted = 0, dropped = 0;
  auto emit = [&]() {
    if (fanout.empty()) return;
    if (fanout.size() == 1) { ++dropped; return; }
    int drivers = 0;
    for (const auto& pin : fanout) drivers += std::get<2>(pin);
    if (drivers > 1)
      throw std::runtime_error("mapped net has multiple atomic-resource drivers");
    nets << "net n" << emitted++ << ' ' << fanout.size() << '\n';
    for (bool driver : {true, false})
      for (const auto& pin : fanout)
        if (std::get<2>(pin) == driver)
          nets << "  a" << std::get<0>(pin) << ' ' << std::get<1>(pin) << '\n';
    nets << "endnet\n";
    if (!nets) throw std::runtime_error("cannot write Bookshelf nets");
  };
  Statement endpoints(spool, "SELECT net,atom,port,driver FROM endpoints ORDER BY net,seq");
  Integer active_net = 0;
  while (endpoints.Next()) {
    const auto net = endpoints.Int(0);
    if (!fanout.empty() && net != active_net) { emit(); fanout.clear(); }
    active_net = net;
    fanout.emplace_back(endpoints.Int(1), endpoints.Text(2), endpoints.Int(3) != 0);
  }
  emit();
  nets.close();
  if (!nets) throw std::runtime_error("cannot close Bookshelf nets");
  std::ofstream report(report_path, std::ios::binary);
  report << "EMUFLOW_BOOKSHELF_V1\t" << atom_count << '\t' << emitted << '\t' << dropped << '\n';
  for (const auto& item : library)
    report << item.first.first << '\t' << item.first.second << '\t' << item.second << '\n';
  report.close();
  if (!report) throw std::runtime_error("cannot write Bookshelf summary");
}
}  // namespace

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_xilinx_bookshelf_exporter MAPPED ATOMS NETS REPORT SPOOL\n";
    return 0;
  }
  if (argc != 6) return 2;
  try {
    Export(argv[1], argv[2], argv[3], argv[4], argv[5]);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_xilinx_bookshelf_exporter: " << error.what() << '\n';
    return 1;
  }
}
