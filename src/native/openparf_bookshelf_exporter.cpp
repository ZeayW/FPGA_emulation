#include <sqlite3.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace fs = std::filesystem;

namespace {

class Database {
 public:
  explicit Database(const fs::path& path) {
    const int status = sqlite3_open_v2(
        path.c_str(), &db_, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr);
    if (status != SQLITE_OK) {
      const std::string message = db_ == nullptr ? "unknown SQLite error"
                                                 : sqlite3_errmsg(db_);
      if (db_ != nullptr) sqlite3_close(db_);
      throw std::runtime_error("cannot open mapped SQLite: " + message);
    }
  }
  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;
  ~Database() { sqlite3_close(db_); }
  sqlite3* get() const { return db_; }

 private:
  sqlite3* db_ = nullptr;
};

class Statement {
 public:
  Statement(sqlite3* db, const char* sql) : db_(db) {
    if (sqlite3_prepare_v2(db, sql, -1, &statement_, nullptr) != SQLITE_OK) {
      throw std::runtime_error(std::string("cannot prepare SQLite query: ") +
                               sqlite3_errmsg(db));
    }
  }
  Statement(const Statement&) = delete;
  Statement& operator=(const Statement&) = delete;
  ~Statement() { sqlite3_finalize(statement_); }
  bool step() {
    const int status = sqlite3_step(statement_);
    if (status == SQLITE_ROW) return true;
    if (status == SQLITE_DONE) return false;
    throw std::runtime_error(std::string("SQLite query failed: ") +
                             sqlite3_errmsg(db_));
  }
  std::int64_t integer(int column) const {
    return sqlite3_column_int64(statement_, column);
  }
  std::string text(int column) const {
    const auto* value = sqlite3_column_text(statement_, column);
    if (value == nullptr) throw std::runtime_error("unexpected SQLite NULL");
    return reinterpret_cast<const char*>(value);
  }

 private:
  sqlite3* db_ = nullptr;
  sqlite3_stmt* statement_ = nullptr;
};

void execute(sqlite3* db, const char* sql) {
  char* detail = nullptr;
  if (sqlite3_exec(db, sql, nullptr, nullptr, &detail) != SQLITE_OK) {
    const std::string message = detail == nullptr ? sqlite3_errmsg(db) : detail;
    sqlite3_free(detail);
    throw std::runtime_error("SQLite write failed: " + message);
  }
}

struct Pin {
  std::string atom;
  std::string name;
  std::string direction;
};

bool is_constant(const std::string& type) {
  return type == "GND" || type == "VCC";
}

bool is_ff(const std::string& type) {
  return type == "FDCE" || type == "FDPE" || type == "FDRE" ||
         type == "FDSE";
}

std::string resource_for(const std::string& type) {
  if (is_ff(type)) return "FF";
  if (type == "LUT1" || type == "LUT2" || type == "LUT3" ||
      type == "LUT4" || type == "LUT5" || type == "LUT6" ||
      type == "LUT6_2") {
    return "LUT";
  }
  if (type == "CARRY8" || type == "MUXF7" || type == "MUXF8" ||
      type == "MUXF9" || type == "DSP48E2" || type == "RAMB18E2" ||
      type == "RAMB36E2" || type == "URAM288") {
    return type;
  }
  throw std::runtime_error("unsupported physical primitive " + type);
}

std::unordered_map<std::string, std::string> read_source_overrides(
    const fs::path& path) {
  std::unordered_map<std::string, std::string> result;
  if (path.empty()) return result;
  std::ifstream stream(path);
  if (!stream) throw std::runtime_error("cannot open atomic source overrides");
  std::string line;
  std::uint64_t line_number = 0;
  while (std::getline(stream, line)) {
    ++line_number;
    if (line.empty()) continue;
    const auto split = line.find('\t');
    if (split == std::string::npos || split == 0 || split + 1 == line.size() ||
        line.find('\t', split + 1) != std::string::npos) {
      throw std::runtime_error("invalid atomic source override line " +
                               std::to_string(line_number));
    }
    const std::string instance = line.substr(0, split);
    const std::string cluster = line.substr(split + 1);
    if (!result.emplace(instance, cluster).second) {
      throw std::runtime_error("duplicate atomic source override for " + instance);
    }
  }
  return result;
}

std::string uppercase(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char ch) { return std::toupper(ch); });
  return value;
}

std::string pin_suffix(const std::string& type, const std::string& port,
                       const std::string& pin_name) {
  if (is_ff(type) && port == "C") return " CLOCK";
  if (is_ff(type) && port == "CE") return " CTRL_CE";
  if ((type == "FDCE" && port == "CLR") ||
      (type == "FDPE" && port == "PRE") ||
      ((type == "FDRE" || type == "FDSE") && port == "R") ||
      (type == "FDSE" && port == "S")) {
    return " CTRL_SR";
  }
  if (type == "CARRY8" && (pin_name == "CI" || pin_name == "CO[7]")) {
    return " CAS";
  }
  return {};
}

fs::path temporary_for(const fs::path& final) {
  return final.parent_path() / ("." + final.filename().string() + ".tmp");
}

void replace_file(const fs::path& temporary, const fs::path& final) {
  std::error_code ignored;
  fs::remove(final, ignored);
  fs::rename(temporary, final);
}

void require_schema(sqlite3* db) {
  Statement schema(db,
                   "SELECT value FROM metadata WHERE key='schema' LIMIT 1");
  if (!schema.step() || schema.text(0) != "emuflow.xilinx-mapped-index/v2") {
    throw std::runtime_error(
        "native OpenPARF export requires Xilinx mapped index v2");
  }
  Statement pins(db,
                 "SELECT 1 FROM sqlite_master WHERE type='table' "
                 "AND name='cell_pins'");
  if (!pins.step()) {
    throw std::runtime_error("mapped index omits normalized cell_pins");
  }
}

struct Counts {
  std::uint64_t atoms = 0;
  std::uint64_t nets = 0;
  std::uint64_t dropped = 0;
};

Counts export_bookshelf(const fs::path& database_path,
                        const fs::path& output_dir) {
  fs::create_directories(output_dir);
  Database database(database_path);
  sqlite3* db = database.get();
  require_schema(db);

  std::int64_t maximum_seq = -1;
  {
    Statement query(db, "SELECT COALESCE(max(seq),-1) FROM cells");
    if (query.step()) maximum_seq = query.integer(0);
  }
  std::vector<std::string> atom_names(
      maximum_seq < 0 ? 0 : static_cast<std::size_t>(maximum_seq + 1));
  Counts counts;

  const fs::path nodes_final = output_dir / "design.nodes";
  const fs::path nodes_temp = temporary_for(nodes_final);
  {
    std::ofstream nodes(nodes_temp);
    if (!nodes) throw std::runtime_error("cannot create design.nodes");
    Statement query(db,
                    "SELECT seq,id,type FROM cells "
                    "WHERE type NOT IN ('GND','VCC') ORDER BY id");
    while (query.step()) {
      const auto seq = query.integer(0);
      if (seq < 0 || seq > maximum_seq) {
        throw std::runtime_error("mapped cell sequence is out of range");
      }
      const std::string atom = "a" + std::to_string(counts.atoms++);
      atom_names[static_cast<std::size_t>(seq)] = atom;
      nodes << atom << ' ' << query.text(2) << '\n';
    }
    if (!nodes) throw std::runtime_error("failed writing design.nodes");
  }
  replace_file(nodes_temp, nodes_final);

  const fs::path library_final = output_dir / "design.lib";
  const fs::path library_temp = temporary_for(library_final);
  {
    std::ofstream library(library_temp);
    if (!library) throw std::runtime_error("cannot create design.lib");
    std::map<std::string,
             std::vector<std::tuple<std::string, std::string, std::string>>>
        models;
    Statement query(
        db,
        "SELECT DISTINCT c.type,p.port,p.pin_name,p.direction "
        "FROM cell_pins p JOIN cells c ON c.seq=p.cell_seq "
        "WHERE c.type NOT IN ('GND','VCC') "
        "ORDER BY c.type,p.pin_name,p.direction");
    while (query.step()) {
      const std::string type = query.text(0);
      const std::string port = query.text(1);
      const std::string pin_name = query.text(2);
      models[type].emplace_back(port, pin_name, query.text(3));
    }
    const bool uses_slice_mux =
        models.count("MUXF7") || models.count("MUXF8") ||
        models.count("MUXF9");
    if (uses_slice_mux) {
      for (const std::string type : {"MUXF7", "MUXF8", "MUXF9"}) {
        if (models.count(type)) continue;
        models[type] = {
            {"I0", "I0", "input"}, {"I1", "I1", "input"},
            {"O", "O", "output"}, {"S", "S", "input"}};
      }
    }
    bool first_type = true;
    for (const auto& [type, pins] : models) {
      if (!first_type) library << '\n';
      first_type = false;
      library << "CELL " << type << '\n';
      for (const auto& [port, pin_name, direction] : pins) {
        library << "  PIN " << pin_name << ' ' << uppercase(direction)
                << pin_suffix(type, port, pin_name) << '\n';
      }
      library << "END CELL\n";
    }
    if (!library) throw std::runtime_error("failed writing design.lib");
  }
  replace_file(library_temp, library_final);

  const fs::path nets_final = output_dir / "design.nets";
  const fs::path nets_temp = temporary_for(nets_final);
  {
    std::ofstream nets(nets_temp);
    if (!nets) throw std::runtime_error("cannot create design.nets");
    Statement query(
        db,
        "SELECT p.net_bit,p.cell_seq,p.pin_name,p.direction "
        "FROM cell_pins p JOIN cells c ON c.seq=p.cell_seq "
        "WHERE p.net_bit IS NOT NULL AND c.type NOT IN ('GND','VCC') "
        "ORDER BY p.net_bit,p.cell_seq,p.pin_seq");
    std::int64_t active_net = -1;
    std::vector<Pin> pins;
    auto emit = [&]() {
      if (pins.empty()) return;
      std::vector<Pin> drivers;
      std::vector<Pin> sinks;
      for (auto& pin : pins) {
        if (pin.direction == "output") {
          drivers.push_back(std::move(pin));
        } else {
          sinks.push_back(std::move(pin));
        }
      }
      if (drivers.size() > 1) {
        throw std::runtime_error(
            "mapped net has multiple atomic-resource drivers");
      }
      if (drivers.size() + sinks.size() < 2) {
        ++counts.dropped;
        pins.clear();
        return;
      }
      nets << "net n" << counts.nets++ << ' '
           << (drivers.size() + sinks.size()) << '\n';
      for (const auto& pin : drivers) {
        nets << "  " << pin.atom << ' ' << pin.name << '\n';
      }
      for (const auto& pin : sinks) {
        nets << "  " << pin.atom << ' ' << pin.name << '\n';
      }
      nets << "endnet\n";
      pins.clear();
    };
    while (query.step()) {
      const auto net = query.integer(0);
      if (active_net != -1 && net != active_net) emit();
      active_net = net;
      const auto seq = query.integer(1);
      if (seq < 0 || seq > maximum_seq ||
          atom_names[static_cast<std::size_t>(seq)].empty()) {
        throw std::runtime_error("mapped pin references a non-physical cell");
      }
      pins.push_back({atom_names[static_cast<std::size_t>(seq)], query.text(2),
                      query.text(3)});
    }
    emit();
    if (!nets) throw std::runtime_error("failed writing design.nets");
  }
  replace_file(nets_temp, nets_final);
  return counts;
}

void export_atom_map(
    const fs::path& database_path, const fs::path& output_dir,
    const std::unordered_map<std::string, std::string>& overrides,
    std::uint64_t expected_atoms) {
  Database source(database_path);
  sqlite3* input = source.get();
  require_schema(input);
  const fs::path final = output_dir / "atom-map.sqlite3";
  const fs::path temporary = temporary_for(final);
  std::error_code ignored;
  fs::remove(temporary, ignored);
  sqlite3* raw = nullptr;
  if (sqlite3_open_v2(temporary.c_str(), &raw,
                      SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr) !=
      SQLITE_OK) {
    const std::string detail = raw == nullptr ? "unknown SQLite error"
                                               : sqlite3_errmsg(raw);
    if (raw != nullptr) sqlite3_close(raw);
    throw std::runtime_error("cannot create atom map: " + detail);
  }
  try {
    execute(raw, "PRAGMA page_size=65536; PRAGMA journal_mode=OFF; "
                 "PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY; "
                 "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) "
                 "WITHOUT ROWID; "
                 "CREATE TABLE atoms(atom_seq INTEGER PRIMARY KEY,"
                 "openparf TEXT NOT NULL UNIQUE,instance TEXT NOT NULL UNIQUE,"
                 "cell_type TEXT NOT NULL,source_cluster TEXT NOT NULL,"
                 "resource TEXT NOT NULL); BEGIN IMMEDIATE;");
    sqlite3_stmt* insert = nullptr;
    const char* sql =
        "INSERT INTO atoms(atom_seq,openparf,instance,cell_type,source_cluster,"
        "resource) VALUES(?,?,?,?,?,?)";
    if (sqlite3_prepare_v2(raw, sql, -1, &insert, nullptr) != SQLITE_OK) {
      throw std::runtime_error("cannot prepare atom-map insertion");
    }
    Statement cells(input, "SELECT id,type FROM cells ORDER BY id");
    std::uint64_t all_index = 0;
    std::uint64_t atom_index = 0;
    std::unordered_set<std::string> consumed;
    while (cells.step()) {
      const std::string instance = cells.text(0);
      const std::string type = cells.text(1);
      const auto current_index = all_index++;
      if (is_constant(type)) continue;
      const std::string openparf = "a" + std::to_string(atom_index);
      const auto override = overrides.find(instance);
      const std::string source_cluster =
          override == overrides.end()
              ? "atomic-source-" + [&]() {
                  std::string value = std::to_string(current_index);
                  return std::string(value.size() < 6 ? 6 - value.size() : 0,
                                     '0') + value;
                }()
              : override->second;
      if (override != overrides.end()) consumed.insert(instance);
      const std::string resource = resource_for(type);
      sqlite3_bind_int64(insert, 1, static_cast<sqlite3_int64>(atom_index));
      sqlite3_bind_text(insert, 2, openparf.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(insert, 3, instance.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(insert, 4, type.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(insert, 5, source_cluster.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(insert, 6, resource.c_str(), -1, SQLITE_TRANSIENT);
      if (sqlite3_step(insert) != SQLITE_DONE) {
        const std::string detail = sqlite3_errmsg(raw);
        sqlite3_finalize(insert);
        throw std::runtime_error("cannot insert atom-map row: " + detail);
      }
      sqlite3_reset(insert);
      sqlite3_clear_bindings(insert);
      ++atom_index;
    }
    sqlite3_finalize(insert);
    if (atom_index != expected_atoms || consumed.size() != overrides.size()) {
      throw std::runtime_error("atomic source override coverage is invalid");
    }
    sqlite3_stmt* metadata = nullptr;
    if (sqlite3_prepare_v2(raw,
            "INSERT INTO metadata(key,value) VALUES(?,?)", -1, &metadata,
            nullptr) != SQLITE_OK) {
      throw std::runtime_error("cannot prepare atom-map metadata insertion");
    }
    for (const auto& item : std::vector<std::pair<std::string, std::string>>{
             {"schema", "emuflow.openparf-atom-map/v1"},
             {"atoms", std::to_string(atom_index)}}) {
      sqlite3_bind_text(metadata, 1, item.first.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(metadata, 2, item.second.c_str(), -1, SQLITE_TRANSIENT);
      if (sqlite3_step(metadata) != SQLITE_DONE) {
        sqlite3_finalize(metadata);
        throw std::runtime_error("cannot insert atom-map metadata");
      }
      sqlite3_reset(metadata);
      sqlite3_clear_bindings(metadata);
    }
    sqlite3_finalize(metadata);
    execute(raw, "COMMIT;");
    sqlite3_close(raw);
    raw = nullptr;
  } catch (...) {
    if (raw != nullptr) sqlite3_close(raw);
    fs::remove(temporary, ignored);
    throw;
  }
  replace_file(temporary, final);
}

void write_report(const fs::path& output_dir, const Counts& counts) {
  const fs::path final = output_dir / "bookshelf-export-report.json";
  const fs::path temporary = temporary_for(final);
  {
    std::ofstream report(temporary);
    if (!report) throw std::runtime_error("cannot create exporter report");
    report << "{\"schema\":\"emuflow.openparf-bookshelf-export/v1\","
           << "\"status\":\"pass\",\"provider\":"
           << "\"native-sqlite-stream-v1\",\"atoms\":" << counts.atoms
           << ",\"nets\":" << counts.nets
           << ",\"dropped_single_endpoint_nets\":" << counts.dropped
           << "}\n";
  }
  replace_file(temporary, final);
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 3 && argc != 4) {
      throw std::runtime_error(
          "usage: emuflow_openparf_bookshelf_exporter MAPPED.sqlite OUT_DIR "
          "[SOURCE_OVERRIDES.tsv]");
    }
    const fs::path output_dir(argv[2]);
    const Counts counts = export_bookshelf(fs::path(argv[1]), output_dir);
    const auto overrides = read_source_overrides(
        argc == 4 ? fs::path(argv[3]) : fs::path());
    export_atom_map(fs::path(argv[1]), output_dir, overrides, counts.atoms);
    write_report(output_dir, counts);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_openparf_bookshelf_exporter: " << error.what()
              << '\n';
    return 1;
  }
}
