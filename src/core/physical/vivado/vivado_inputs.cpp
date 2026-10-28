#include "physical/vivado/vivado_inputs.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "ir/emuir_view.hpp"
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <set>
#include <sstream>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Raw = std::map<std::string, std::string>;
void Check(bool yes, const std::string &why) {
  if (!yes)
    throw StageError(ErrorCode::kInvalidArtifact, "Vivado inputs: " + why);
}
void Stop(const volatile std::sig_atomic_t *cancelled) {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed, "Vivado inputs cancelled");
}
std::string Safe(const std::string &value) {
  Check(!value.empty(), "empty identifier");
  (void)JsonQuoteUtf8(value);
  for (unsigned char c : value)
    Check(c > 32 && c != 127, "whitespace/control in identifier");
  return value;
}
std::string Id(const std::string &value) { return "\\" + Safe(value) + " "; }
std::string Tcl(const std::string &value) {
  Safe(value);
  Check(value.find_first_of("{}\\") == std::string::npos,
        "unsafe clock identifier");
  return "{" + value + "}";
}
std::string Hex(const std::string &value) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string out;
  for (unsigned char c : value) {
    out += hex[c >> 4];
    out += hex[c & 15];
  }
  return out;
}
std::string Hash(const ArtifactRef &source) {
  std::ifstream in(source.path(), std::ios::binary);
  Check(bool(in), "cannot open sealed source");
  Sha256 sha;
  std::array<char, 65536> b{};
  while (in) {
    in.read(b.data(), b.size());
    sha.update(reinterpret_cast<unsigned char *>(b.data()), in.gcount());
  }
  Check(in.eof(), "cannot read sealed source");
  source.RequireUnchanged();
  std::ostringstream out;
  out << std::hex << std::setfill('0');
  for (auto n : sha.finish())
    out << std::setw(2) << unsigned(n);
  return out.str();
}
std::string Decimal(double value) {
  std::ostringstream out;
  out.imbue(std::locale::classic());
  out << std::fixed << std::setprecision(9) << value;
  return out.str();
}
std::string Metadata(SqliteDatabase &db, const char *key) {
  auto q = db.Prepare("SELECT value FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step(), "missing source metadata");
  const auto text = q.TypedText(0);
  Check(text.size() <= 8 * 1024 * 1024 && !q.Step(), "invalid source metadata");
  return text;
}
void Generation(Doc &source, const ArtifactRef &actual) {
  const auto expected = Doc::Array(source.Token("generation"), 7);
  Check(expected.size() == 7, "invalid source generation");
  const auto &g = actual.generation();
  const std::string fields[] = {std::to_string(g.device),
                                std::to_string(g.inode),
                                std::to_string(g.bytes),
                                std::to_string(g.modified_seconds),
                                std::to_string(g.modified_nanoseconds),
                                std::to_string(g.changed_seconds),
                                std::to_string(g.changed_nanoseconds)};
  for (unsigned i = 0; i < 7; ++i)
    Check(expected[i] == fields[i], "source generation changed");
}
struct Workspace {
  fs::path path;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".vivado-inputs-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create scratch");
    path = name;
  }
  ~Workspace() {
    std::error_code e;
    fs::remove_all(path, e);
  }
};
} // namespace
StageResult PrepareVivadoInputs(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *cancelled) {
  return RunStage(
      "physical.prepare-vivado-inputs",
      [&] { return std::vector<ArtifactRef>{request}; },
      [&](const auto &initial) {
        Stop(cancelled);
        Doc config(Doc::Read(request));
        config.Keys({"schema", "source", "runtime", "virtual_io_contract"});
        Check(config.Text("schema") == "emuflow.vivado-input-request/v1",
              "request schema disagrees");
        Doc src(config.Token("source"));
        src.Keys({"path", "generation"});
        const auto source_path = fs::path(src.Text("path"));
        Check(source_path.is_absolute() && !fs::is_symlink(source_path),
              "invalid indexed source");
        auto source =
            ArtifactRef::Bind(source_path.string(), "emuir-compact-v2");
        Generation(src, source);
        std::vector<ArtifactRef> sources = initial;
        sources.push_back(source);
        EmuirView ir(source);
        auto &db = ir.store().database();
        Doc design(Metadata(db, "design_json"));
        const auto source_top = Safe(design.Text("top"));
        const fs::path target(output);
        Check(target.is_absolute() && !fs::exists(target) &&
                  !fs::is_symlink(target),
              "output must be a fresh directory");
        fs::create_directories(target.parent_path());
        Workspace work(target.parent_path());
        const auto stage = work.path / "stage";
        fs::create_directory(stage);
        const auto write = [&](const std::string &filename,
                               const std::string &text) {
          PublishTextArtifact((stage / filename).string(), sources,
                              [&](std::ostream &out) { out << text; });
        };
        // Top-port membership is indexed in the borrowed source, never a Python
        // graph.
        db.Execute(
            "CREATE TEMP TABLE vivado_ports(id TEXT PRIMARY KEY,direction TEXT "
            "NOT NULL,width INTEGER NOT NULL,ordinal INTEGER NOT NULL) WITHOUT "
            "ROWID; CREATE TEMP TABLE vivado_buses(id TEXT PRIMARY KEY,width "
            "INTEGER NOT NULL,ordinal INTEGER NOT NULL) WITHOUT ROWID; CREATE "
            "TEMP TABLE vivado_physical(id TEXT PRIMARY KEY,direction TEXT NOT "
            "NULL,ordinal INTEGER NOT NULL) WITHOUT ROWID; CREATE TEMP TABLE "
            "vivado_bits(id TEXT NOT NULL,bit INTEGER NOT NULL,PRIMARY "
            "KEY(id,bit)) WITHOUT ROWID");
        auto ports = db.Prepare(
                 "SELECT seq,record_json FROM port_records ORDER BY seq"),
             put = db.Prepare("INSERT INTO temp.vivado_ports VALUES(?,?,?,?)");
        while (ports.Step()) {
          Stop(cancelled);
          Doc port(ports.TypedText(1));
          const auto name = Safe(port.Text("id")),
                     direction = port.Text("direction");
          const auto width = port.Integer("width");
          Check(width > 0 && (direction == "input" || direction == "output" ||
                              direction == "inout"),
                "invalid source port");
          put.Reset();
          put.BindText(1, name);
          put.BindText(2, direction);
          put.BindInteger(3, width);
          put.BindInteger(4, ports.TypedInteger(0));
          put.Execute();
        }
        auto find = db.Prepare(
            "SELECT direction,width FROM temp.vivado_ports WHERE id=?");
        const auto has = [&](const std::string &name) {
          find.Reset();
          find.BindText(1, name);
          return find.Step();
        };
        Doc runtime(config.Token("runtime")),
            timing(runtime.Token("timing_model")),
            fabric(runtime.Token("fabric_clock")),
            dut_clocks(runtime.Token("virtual_dut_clocks"));
        const auto fabric_port = timing.Text("fabric_clock_port");
        Check(has(fabric_port), "partition lacks required fabric clock port");
        const auto period = fabric.Number("period_ns"),
                   cross = timing.Number("fabric_to_dut_max_delay_ns");
        Check(period > 0 && cross >= 0, "invalid runtime clock periods");
        std::string xdc =
            "# EmuFlow provider-neutral runtime timing contract.\ncreate_clock "
            "-name emuflow_fabric_clk -period " +
            Decimal(period) + " [get_ports " + Tcl(fabric_port) + "]\n";
        std::int64_t dut_count = 0, ordinal = 0;
        std::set<std::string> clocks;
        for (const auto &token :
             Doc::Array(timing.Token("dut_clock_ports"), 4096)) {
          const auto name = DecodeJsonString(token);
          Check(clocks.insert(name).second, "duplicate DUT clock");
          const auto index = ordinal++;
          if (!has(name))
            continue;
          Check(find.TypedText(0) == "input" && find.TypedInteger(1) == 1,
                "invalid DUT clock port");
          Doc record(dut_clocks.Token(name));
          const auto dut = record.Number("nominal_period_ns");
          Check(dut > 0, "invalid DUT clock period");
          const auto clock = "emuflow_dut_clk_" + std::to_string(index);
          xdc += "create_clock -name " + clock + " -period " + Decimal(dut) +
                 " [get_ports " + Tcl(name) +
                 "]\nset_max_delay -datapath_only " + Decimal(cross) +
                 " -from [get_clocks {emuflow_fabric_clk}] -to [get_clocks {" +
                 clock + "}]\n";
          ++dut_count;
        }
        Raw report{
            {"schema", JsonQuote("emuflow.vivado-input-report/v1")},
            {"status", JsonQuote("pass")},
            {"implementation_id", JsonQuote("native-vivado-inputs-v1")},
            {"source_top", JsonQuote(source_top)},
            {"top", JsonQuote(source_top)},
            {"hierarchy_prefix", JsonQuote("")},
            {"instances", std::to_string(ir.counts().cells)},
            {"dut_clock_count", std::to_string(dut_count)},
            {"runtime_xdc", JsonQuote((target / "runtime.xdc").string())}};
        if (config.Token("virtual_io_contract") != "null") {
          Doc contract(config.Token("virtual_io_contract"));
          Check(contract.Text("status") == "pass", "fixed I/O contract failed");
          Raw owner_paths;
          for (const auto *key : {"adapter", "xdc"}) {
            Doc owner(contract.Token(key));
            const fs::path path(owner.Text("path"));
            Check(path.is_absolute() && !fs::is_symlink(path),
                  "invalid fixed I/O source");
            auto artifact =
                ArtifactRef::Bind(path.string(), "exact-sha256-text-v1");
            Check(Hash(artifact) == owner.Text("sha256"),
                  "fixed I/O source seal changed");
            sources.push_back(artifact);
            owner_paths[key] = JsonQuote(artifact.path());
          }
          Doc adapter(contract.Token("adapter"));
          const auto entries = Doc::Array(contract.Token("entries"), 16384);
          std::int64_t sequence = 0, bus_sequence = 0;
          std::string query = "direction\tp_hex\tn_hex\tp_pin_hex\tn_pin_"
                              "hex\tiostandard_hex\n",
                      map = "logical_hex\tphysical_hex\n";
          auto bit = db.Prepare("INSERT INTO temp.vivado_bits VALUES(?,?)"),
               bus = db.Prepare("INSERT INTO temp.vivado_buses VALUES(?,?,?) "
                                "ON CONFLICT(id) DO NOTHING"),
               physical =
                   db.Prepare("INSERT INTO temp.vivado_physical VALUES(?,?,?)");
          for (const auto &token : entries) {
            Stop(cancelled);
            Doc entry(token);
            const auto direction = entry.Text("direction"),
                       logical = entry.Text("logical_port");
            Check(direction == "tx" || direction == "rx",
                  "invalid fixed I/O direction");
            const auto dir = direction == "tx" ? "output" : "input";
            const auto width = entry.Integer("logical_port_width"),
                       index = entry.Integer("logical_bit");
            Check(has(logical) && find.TypedText(0) == dir &&
                      find.TypedInteger(1) == width && index >= 0 &&
                      index < width,
                  "invalid fixed I/O bus binding");
            bit.Reset();
            bit.BindText(1, logical);
            bit.BindInteger(2, index);
            bit.Execute();
            bus.Reset();
            bus.BindText(1, logical);
            bus.BindInteger(2, width);
            bus.BindInteger(3, bus_sequence++);
            bus.Execute();
            Doc p(entry.Token("p")), n(entry.Token("n"));
            for (auto *polarity : {&p, &n}) {
              const auto name = Safe(polarity->Text("port"));
              Check(!has(name), "fixed I/O port collides with source port");
              physical.Reset();
              physical.BindText(1, name);
              physical.BindText(2, dir);
              physical.BindInteger(3, sequence++);
              physical.Execute();
            }
            query += direction + '\t' + Hex(p.Text("port")) + '\t' +
                     Hex(n.Text("port")) + '\t' + Hex(p.Text("package_pin")) +
                     '\t' + Hex(n.Text("package_pin")) + '\t' +
                     Hex(entry.Text("iostandard")) + '\n';
            const auto name = logical + "[" + std::to_string(index) + "]";
            map += Hex(name) + '\t' + Hex(p.Text("port")) + '\n';
            if (width == 1)
              map += Hex(logical) + '\t' + Hex(p.Text("port")) + '\n';
          }
          Doc metrics(contract.Token("metrics"));
          Check(metrics.Integer("physical_ports") == sequence,
                "fixed I/O physical port census disagrees");
          if (metrics.Has("differential_pairs"))
            Check(metrics.Integer("differential_pairs") ==
                      std::int64_t(entries.size()),
                  "fixed I/O pair census disagrees");
          const auto top = "__emuflow_physical_top";
          Check(source_top != top, "physical top collides with source top");
          std::string wrapper = "module " + std::string(top) + " (\n";
          bool first = true;
          const auto add = [&](const std::string &value) {
            if (!first)
              wrapper += ",\n";
            wrapper += value;
            first = false;
          };
          auto declarations =
              db.Prepare("SELECT p.id,p.direction,p.width FROM "
                         "temp.vivado_ports p LEFT JOIN temp.vivado_buses b "
                         "USING(id) WHERE b.id IS NULL ORDER BY p.ordinal");
          while (declarations.Step())
            add("  " + declarations.TypedText(1) + " wire " +
                (declarations.TypedInteger(2) == 1
                     ? ""
                     : "[" + std::to_string(declarations.TypedInteger(2) - 1) +
                           ":0] ") +
                Id(declarations.TypedText(0)));
          auto pins = db.Prepare(
              "SELECT id,direction FROM temp.vivado_physical ORDER BY ordinal");
          while (pins.Step())
            add("  " + pins.TypedText(1) + " wire " + Id(pins.TypedText(0)));
          wrapper += "\n);\n";
          auto buses = db.Prepare(
              "SELECT id,width FROM temp.vivado_buses ORDER BY ordinal");
          while (buses.Step())
            wrapper += "  wire [" + std::to_string(buses.TypedInteger(1) - 1) +
                       ":0] " + Id(buses.TypedText(0)) + ";\n";
          wrapper += "  " + Id(source_top) + " mapped_partition (\n";
          first = true;
          auto all =
              db.Prepare("SELECT id FROM temp.vivado_ports ORDER BY ordinal");
          while (all.Step()) {
            const auto name = Id(all.TypedText(0));
            add("    ." + name + "(" + name + ")");
          }
          wrapper +=
              "\n  );\n  " + Id(adapter.Text("module")) + " virtual_io (\n";
          first = true;
          auto io =
              db.Prepare("SELECT id FROM (SELECT id,0 AS kind,ordinal FROM "
                         "temp.vivado_buses UNION ALL SELECT id,1,ordinal FROM "
                         "temp.vivado_physical) ORDER BY kind,ordinal");
          while (io.Step()) {
            const auto name = Id(io.TypedText(0));
            add("    ." + name + "(" + name + ")");
          }
          wrapper += "\n  );\nendmodule\n";
          const Raw top_report{
              {"status", JsonQuote("pass")},
              {"top", JsonQuote(top)},
              {"hierarchy_prefix", JsonQuote("mapped_partition")},
              {"output", JsonQuote((target / "physical_top.sv").string())},
              {"differential_pairs", std::to_string(entries.size())},
              {"physical_ports", std::to_string(sequence)}};
          report["top"] = JsonQuote(top);
          report["hierarchy_prefix"] = JsonQuote("mapped_partition");
          report["fixed_io"] = Doc::Encode(
              {{"adapter", owner_paths.at("adapter")},
               {"xdc", owner_paths.at("xdc")},
               {"wrapper", JsonQuote((target / "physical_top.sv").string())},
               {"query", JsonQuote((target / "fixed-io-query.tsv").string())},
               {"timing_map",
                JsonQuote((target / "fixed-io-timing-map.tsv").string())},
               {"top", Doc::Encode(top_report)}});
          write("physical_top.sv", wrapper);
          write("fixed-io-query.tsv", query);
          write("fixed-io-timing-map.tsv", map);
        }
        write("runtime.xdc", xdc);
        write("summary.json", Doc::Encode(report) + '\n');
        for (const auto &input : sources)
          input.RequireUnchanged();
        Stop(cancelled);
        const auto publication = work.path.parent_path() /
                                 (work.path.filename().string() + "-publish");
        fs::rename(stage, publication);
        try {
          PublishNewDirectory(publication.string(), target.string(), sources);
        } catch (...) {
          std::error_code e;
          fs::remove_all(publication, e);
          throw;
        }
      },
      [&] {
        std::vector<ArtifactRef> result{
            ArtifactRef::Bind((fs::path(output) / "summary.json").string(),
                              "vivado-input-report-v1")};
        for (const auto *name :
             {"runtime.xdc", "physical_top.sv", "fixed-io-query.tsv",
              "fixed-io-timing-map.tsv"})
          if (fs::is_regular_file(fs::path(output) / name))
            result.push_back(ArtifactRef::Bind(
                (fs::path(output) / name).string(), "exact-sha256-text-v1"));
        return result;
      });
}
} // namespace emuflow::core
