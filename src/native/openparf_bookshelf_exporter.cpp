#include <sqlite3.h>

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <tuple>
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
    Statement query(
        db,
        "SELECT DISTINCT c.type,p.port,p.pin_name,p.direction "
        "FROM cell_pins p JOIN cells c ON c.seq=p.cell_seq "
        "WHERE c.type NOT IN ('GND','VCC') "
        "ORDER BY c.type,p.pin_name,p.direction");
    std::string active_type;
    while (query.step()) {
      const std::string type = query.text(0);
      if (type != active_type) {
        if (!active_type.empty()) library << "END CELL\n\n";
        active_type = type;
        library << "CELL " << type << '\n';
      }
      const std::string port = query.text(1);
      const std::string pin_name = query.text(2);
      library << "  PIN " << pin_name << ' ' << uppercase(query.text(3))
              << pin_suffix(type, port, pin_name) << '\n';
    }
    if (!active_type.empty()) library << "END CELL\n";
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
    if (argc != 3) {
      throw std::runtime_error(
          "usage: emuflow_openparf_bookshelf_exporter MAPPED.sqlite OUT_DIR");
    }
    const fs::path output_dir(argv[2]);
    const Counts counts = export_bookshelf(fs::path(argv[1]), output_dir);
    write_report(output_dir, counts);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_openparf_bookshelf_exporter: " << error.what()
              << '\n';
    return 1;
  }
}
