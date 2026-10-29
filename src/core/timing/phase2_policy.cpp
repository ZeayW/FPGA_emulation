#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/tool_runtime.hpp"
#include "storage/store.hpp"
#include "timing/opensta_export.hpp"
#include "timing/phase2.hpp"
#include "timing/routed_model.hpp"
#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <filesystem>
#include <regex>
#include <set>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
namespace fs = std::filesystem;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Phase 2 configuration: " + why);
}
void Stop(const volatile std::sig_atomic_t *cancelled) {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed,
                     "Phase 2 configuration cancelled");
}
std::string Text(const std::string &s) {
  Check(s.find('\0') == std::string::npos, "NUL in identity");
  return JsonQuoteUtf8(s);
}
std::string Array(const std::vector<std::string> &values) {
  std::vector<JsonRecord> records;
  for (const auto &v : values)
    records.emplace_back(v);
  return JsonRecord(records).Encode();
}
std::string Hash(const std::string &value) {
  Sha256 sha;
  sha.update(reinterpret_cast<const unsigned char *>(value.data()),
             value.size());
  std::string out;
  for (auto c : sha.finish()) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Meta(SqliteDatabase &db, const char *name) {
  auto q = db.Prepare("SELECT value FROM metadata WHERE key=?");
  q.BindText(1, name);
  Check(q.Step(), "missing design metadata");
  return q.TypedText(0);
}
std::vector<std::string> Strings(const std::string &json) {
  std::vector<std::string> out;
  for (const auto &token : D::Array(json, 8192))
    out.push_back(DecodeJsonString(token));
  return out;
}
std::string Lower(std::string s) {
  for (auto &c : s)
    c = std::tolower(static_cast<unsigned char>(c));
  return s;
}
struct Inputs {
  ArtifactRef request;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  O options;
  explicit Inputs(const ArtifactRef &r) : request(r) {}
  void Read() {
    Check(request.schema() == "phase2-configuration-request-v1",
          "request schema disagrees");
    D doc(D::Read(request));
    doc.Keys({"schema", "sources", "options"});
    Check(doc.Text("schema") == "emuflow.phase2-configuration-request/v1",
          "request version disagrees");
    D config(doc.Token("options"));
    config.Keys({"clocks", "timing_io", "max_paths", "optimization_enabled",
                 "criticality_scale", "criticality_exponent"});
    Check(config.Integer("max_paths") > 0,
          "OpenSTA max_paths must be positive");
    (void)config.Boolean("optimization_enabled");
    Check(config.Number("criticality_scale") > 0 &&
              config.Number("criticality_exponent") > 0,
          "criticality parameters must be positive and finite");
    options = config.Fields();
    sources = {request};
    const std::map<std::string, std::string> schemas = {
        {"emuir", "emuir-compact-v2"},
        {"timing-model", "phase2-source-v1"},
        {"architecture-timing-db", "phase2-source-v1"},
        {"architecture-contract", "phase2-source-v1"},
        {"opensta-executable", "external-tool-executable-v1"},
        {"container-binary", "external-tool-executable-v1"},
        {"export-script", "phase2-source-v1"},
        {"runtime-check-script", "phase2-source-v1"},
        {"runtime-launcher", "phase2-source-v1"}};
    for (const auto &token : D::Array(doc.Token("sources"), 16)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      auto role = row.Text("role");
      Check(schemas.count(role) && row.Text("schema") == schemas.at(role),
            "unknown source role/schema");
      Check(fs::path(row.Text("path")).is_absolute(), "relative source path");
      auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
      SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
      RequireEquivalentJson(compare, row.Token("generation"), Generation(ref),
                            "Phase 2 configuration source generation changed");
      Check(roles.emplace(role, ref).second, "duplicate source role");
      sources.push_back(ref);
    }
    for (const auto *role :
         {"emuir", "timing-model", "opensta-executable", "export-script",
          "runtime-check-script", "runtime-launcher"})
      Check(roles.count(role), std::string("missing source ") + role);
    Check(bool(roles.count("architecture-timing-db")) ==
              bool(roles.count("architecture-contract")),
          "architecture contract/source coverage disagrees");
  }
};
std::set<std::string> UsedTypes(SqliteDatabase &db, bool architecture) {
  std::set<std::string> used;
  auto q = db.Prepare("SELECT DISTINCT "
                      "c.type,json_type(p.value,'$.IS_C_INVERTED'),json_"
                      "extract(p.value,'$.IS_C_INVERTED') FROM cell_records c "
                      "LEFT JOIN payloads p ON p.seq=c.parameters_seq");
  while (q.Step()) {
    auto name = q.TypedText(0);
    Check(!name.empty(), "empty primitive identity");
    const auto kind = q.IsNull(1) ? "null" : q.TypedText(1);
    if (architecture &&
        std::set<std::string>{"FDCE", "FDPE", "FDRE", "FDSE"}.count(name) &&
        TimingParameterIsOne(kind, kind == "integer" ? q.TypedInteger(2) : 0,
                             kind == "text" ? q.TypedText(2) : ""))
      name += "__NEG";
    used.insert(name);
    Check(used.size() <= 4096, "primitive catalog exceeds bound");
  }
  return used;
}
std::string UsedSummary(const std::set<std::string> &values) {
  std::string bytes;
  for (const auto &v : values) {
    for (int i = 7; i >= 0; --i)
      bytes += char(std::uint64_t(v.size()) >> (8 * i));
    bytes += v;
  }
  return D::Encode({{"count", std::to_string(values.size())},
                    {"sha256", Text(Hash(bytes))}});
}
std::vector<std::string> ScalarPins(SqliteDatabase &db, I seq, int role) {
  auto q = db.Prepare(
      "SELECT p.name,e.bit FROM endpoint_records e LEFT JOIN pin_names p ON "
      "p.seq=e.port_seq WHERE e.instance_seq=? AND e.role=? UNION SELECT "
      "json_extract(k.value,'$.port'),json_extract(k.value,'$.bit') FROM "
      "cell_records c JOIN payloads p ON "
      "p.seq=c.constants_seq,json_each(p.value) k WHERE c.seq=? AND ?=1 ORDER "
      "BY 1,2");
  q.BindInteger(1, seq);
  q.BindInteger(2, role);
  q.BindInteger(3, seq);
  q.BindInteger(4, role);
  std::set<std::pair<std::string, I>> pins;
  std::map<std::string, I> widths;
  while (q.Step()) {
    auto name = q.TypedText(0);
    auto bit = q.TypedInteger(1);
    Check(!name.empty() && bit >= 0 && pins.size() < 8192,
          "primitive pin identity/catalog is invalid");
    pins.emplace(name, bit);
    widths[name] = std::max(widths[name], bit);
  }
  std::vector<std::string> out;
  for (const auto &pin : pins)
    out.push_back(widths.at(pin.first) == 0
                      ? pin.first
                      : pin.first + "__" + std::to_string(pin.second));
  return out;
}
std::map<std::string, double> Delays(Inputs &inputs, SqliteDatabase &db) {
  D arch(D::Read(inputs.roles.at("architecture-timing-db"))),
      contract(D::Read(inputs.roles.at("architecture-contract")));
  Check(arch.Text("schema") == "emuflow.xilinx-preplacement-timing-db/v1" &&
            arch.Text("family") == "UltraScale+" &&
            arch.Text("mapping_profile") == "xilinx-ultrascaleplus-open-v1",
        "Xilinx timing target/schema is invalid");
  Check(contract.Text("schema") == "emuflow.rapidwright-timing-contract/v1",
        "architecture source contract is invalid");
  D source(arch.Token("source")), expected(contract.Token("timing")),
      limitation(arch.Token("limitations"));
  Check(source.Text("provider") == "rapidwright-delay-model-v0.5" &&
            source.Text("revision") == expected.Text("source_revision") &&
            source.Text("qualification") == "analytical_uncharacterized",
        "Xilinx timing source identity is invalid");
  RequireEquivalentJson(db, source.Token("files_sha256"),
                        expected.Token("source_data_sha256"),
                        "Xilinx timing source file seals disagree");
  Check(limitation.Text("authoritative_setup") ==
            "Phase 7 routed timing plus global OpenSTA",
        "Xilinx timing limitations are invalid");
  D raw(arch.Token("delays_ps"));
  raw.Keys({"lut1", "lut2", "lut3", "lut4", "lut5", "lut6", "lut6_2", "muxf7",
            "muxf8", "muxf9", "carry8", "ff_setup", "ff_clock_to_q",
            "bram_setup", "bram_clock_to_q", "uram_setup", "uram_clock_to_q",
            "dsp48e2", "sink_interconnect"});
  std::map<std::string, double> delays;
  for (const auto &item : raw.Fields()) {
    const auto n = raw.Number(item.first);
    Check(n > 0, "Xilinx delay must be positive");
    delays[item.first] = n / 1000.;
  }
  return delays;
}
std::string Model(Inputs &input, SqliteDatabase &db,
                  const std::set<std::string> &used) {
  D base(D::Read(input.roles.at("timing-model")));
  Check(base.Text("schema") == "emuflow.fpga-timing-model/v1" ||
            base.Text("schema") == "emuflow.fpga-timing-model/v2",
        "unsupported timing model schema");
  Check(!base.Text("name").empty() && !base.Text("family").empty(),
        "empty timing model identity");
  D source(base.Token("source"));
  Check(std::set<std::string>{"analytical_uncharacterized",
                              "academic_open_model", "calibrated",
                              "characterized"}
            .count(source.Text("qualification")),
        "unsupported model qualification");
  auto fields = base.Fields();
  auto cells = D(base.Token("cells")).Fields();
  Check(!cells.empty() && cells.size() <= 4096,
        "primitive model exceeds bound");
  // Validate the base model before deriving any architecture specialization.
  (void)RenderOpenstaLiberty(base.text());
  if (input.roles.count("architecture-timing-db")) {
    const auto d = Delays(input, db);
    const auto change = [&](const std::string &name, const O &values) {
      Check(cells.count(name), "base primitive is missing: " + name);
      auto f = D(cells.at(name)).Fields();
      for (const auto &v : values)
        f[v.first] = v.second;
      cells[name] = D::Encode(f);
    };
    const auto number = [](double x) { return JsonRecord(x).Encode(); };
    for (unsigned i = 1; i <= 6; ++i)
      change("LUT" + std::to_string(i),
             {{"delay_ns", number(d.at("lut" + std::to_string(i)) +
                                  d.at("sink_interconnect"))}});
    for (const std::string n : {"MUXF7", "MUXF8"})
      change(n, {{"delay_ns",
                  number(d.at(Lower(n)) + d.at("sink_interconnect"))}});
    for (const std::string n : {"FDCE", "FDPE", "FDRE", "FDSE"}) {
      change(n, {{"setup_ns",
                  number(d.at("ff_setup") + d.at("sink_interconnect"))},
                 {"clock_to_q_ns", number(d.at("ff_clock_to_q"))}});
      if (used.count(n + "__NEG")) {
        auto f = D(cells.at(n)).Fields();
        f["kind"] = Text("falling_edge_ff");
        cells[n + "__NEG"] = D::Encode(f);
      }
    }
    auto representatives = db.Prepare("SELECT type,MIN(seq) FROM cell_records "
                                      "GROUP BY type ORDER BY MIN(seq)");
    while (representatives.Step()) {
      const auto name = representatives.TypedText(0);
      if (cells.count(name))
        continue;
      const auto seq = representatives.TypedInteger(1);
      const auto in = ScalarPins(db, seq, 1), out = ScalarPins(db, seq, 0);
      Check(!in.empty() && !out.empty(),
            "Xilinx timing cell lacks input/output pins: " + name);
      if (std::set<std::string>{"LUT6_2", "MUXF9", "CARRY8", "DSP48E2"}.count(
              name))
        cells[name] =
            D::Encode({{"kind", Text("combinational")},
                       {"inputs", Array(in)},
                       {"outputs", Array(out)},
                       {"delay_ns", number(d.at(Lower(name)) +
                                           d.at("sink_interconnect"))}});
      else if (std::set<std::string>{"RAMB18E2", "RAMB36E2", "URAM288"}.count(
                   name)) {
        const auto first =
            std::find_if(in.begin(), in.end(), [](const auto &pin) {
              return Lower(pin).find("clk") != std::string::npos;
            });
        Check(first != in.end(), "hard block lacks clock: " + name);
        const auto clock = *first;
        std::vector<std::string> control, data;
        for (const auto &pin : in)
          if (pin != clock) {
            const auto s = Lower(pin);
            (s.find("clk") != std::string::npos ||
                     s.find("rst") != std::string::npos || s == "sleep"
                 ? control
                 : data)
                .push_back(pin);
          }
        Check(!data.empty(), "hard block lacks data: " + name);
        const std::string prefix = name == "URAM288" ? "uram" : "bram";
        cells[name] = D::Encode(
            {{"kind", Text("rising_edge_bank")},
             {"clock", Text(clock)},
             {"inputs", Array(data)},
             {"controls", Array(control)},
             {"outputs", Array(out)},
             {"setup_ns",
              number(d.at(prefix + "_setup") + d.at("sink_interconnect"))},
             {"clock_to_q_ns", number(d.at(prefix + "_clock_to_q"))}});
      } else
        Check(false,
              "RapidWright pre-placement TimingDB does not cover " + name);
      Check(cells.size() <= 4096, "primitive model exceeds bound");
    }
    D arch(D::Read(input.roles.at("architecture-timing-db"))),
        origin(arch.Token("source"));
    fields["name"] = Text("rapidwright-ultrascaleplus-preplacement-v1");
    fields["family"] = Text("xcup");
    fields["source"] = D::Encode(
        {{"provider", Text("rapidwright-delay-model-preplacement-v1")},
         {"qualification", Text("analytical_uncharacterized")},
         {"architecture_timing_db",
          Text(input.roles.at("architecture-timing-db").path())},
         {"rapidwright_revision", origin.Token("revision")},
         {"sink_interconnect_delay_ns", number(d.at("sink_interconnect"))},
         {"hard_block_timing", Text("nonzero-conservative-scalar")},
         {"hold_analysis", Text("unavailable")},
         {"authoritative_setup",
          Text("Phase 7 routed timing plus global OpenSTA")}});
  }
  std::size_t arcs = 0;
  for (const auto &entry : cells) {
    Check(!entry.first.empty() && entry.first.size() <= 1024,
          "primitive identity exceeds bound");
    D cell(entry.second);
    std::vector<std::string> names;
    for (const auto *f : {"inputs", "outputs", "controls"})
      if (cell.Has(f)) {
        auto a = Strings(cell.Token(f));
        names.insert(names.end(), a.begin(), a.end());
      }
    for (const auto *f : {"clock", "data", "output"})
      if (cell.Has(f))
        names.push_back(cell.Text(f));
    for (const auto &pin : names)
      Check(!pin.empty() && pin.size() <= 1024, "pin identity exceeds bound");
    if (cell.Text("kind") == "combinational")
      arcs += Strings(cell.Token("inputs")).size() *
              (cell.Has("outputs") ? Strings(cell.Token("outputs")).size() : 1);
    else
      arcs += names.size() + 1;
    Check(arcs <= 32768, "primitive model arcs exceed bound");
  }
  for (const auto &name : used)
    Check(cells.count(name),
          "OpenSTA timing model does not cover mapped primitives: " + name);
  fields["cells"] = D::Encode(cells);
  return D::Encode(fields);
}
std::string ClockMap(SqliteDatabase &db, const std::string &selected) {
  O available;
  auto q = db.Prepare("SELECT record_json FROM clocks ORDER BY seq");
  while (q.Step()) {
    D clock(q.TypedText(0));
    Check(available.size() < 4096 &&
              available.emplace(clock.Text("id"), clock.text()).second,
          "duplicate or excessive DUT clocks");
  }
  O out;
  if (selected == "null") {
    for (const auto &item : available) {
      D clock(item.second);
      if (clock.Has("period_ns") && clock.Token("period_ns") != "null" &&
          clock.Token("period_ns") != "true" &&
          clock.Token("period_ns") != "false")
        out[item.first] = clock.Token("period_ns");
    }
  } else
    out = D(selected).Fields();
  Check(!out.empty() && out.size() <= 4096,
        "OpenSTA requires at least one CLOCK=PERIOD_NS definition");
  for (auto &item : out) {
    Check(available.count(item.first),
          "OpenSTA clocks are absent from EmuIR: " + item.first);
    const auto n = ParseFiniteDecimal(item.second, "clock period");
    Check(item.second != "true" && item.second != "false" && n > 0,
          "clock period must be positive");
    item.second = JsonRecord(n).Encode();
  }
  return D::Encode(out);
}
void ValidateIo(SqliteDatabase &db, const std::string &value,
                const std::string &clock_map) {
  if (value == "null")
    return;
  D groups(value), clocks(clock_map);
  groups.Keys({"input_groups", "output_groups"});
  auto port =
      db.Prepare("SELECT json_extract(record_json,'$.direction') FROM "
                 "port_records WHERE json_extract(record_json,'$.id')=?");
  for (const std::string direction : {"input", "output"}) {
    std::set<std::string> seen;
    for (const auto &token : D::Array(groups.Token(direction + "_groups"))) {
      D g(token);
      g.Keys({"clock", "delay_ns", "ports"});
      Check(clocks.Has(g.Text("clock")) && g.Number("delay_ns") >= 0,
            "OpenSTA timing_io clock/delay is invalid");
      auto pins = Strings(g.Token("ports"));
      Check(!pins.empty(), "OpenSTA timing_io group is empty");
      for (const auto &name : pins) {
        port.Reset();
        port.BindText(1, name);
        Check(!name.empty() && !clocks.Has(name) && seen.insert(name).second &&
                  port.Step() && port.TypedText(0) == direction && !port.Step(),
              "OpenSTA timing_io data port is invalid");
      }
    }
  }
}
std::string Engine(Inputs &input, const volatile std::sig_atomic_t *cancelled) {
  const auto &exe = input.roles.at("opensta-executable");
  const auto digest = Sha256File(exe, cancelled);
  O identity{{"name", Text("OpenSTA")}, {"executable_sha256", Text(digest)}};
  const auto launcher =
      Sha256File(input.roles.at("runtime-launcher"), cancelled);
  if (launcher == digest) {
    Check(input.roles.count("container-binary"),
          "OpenSTA container engine identity lacks an explicit binary");
    identity["launcher_sha256"] = Text(digest);
    identity["executable_sha256"] =
        Text(Sha256File(input.roles.at("container-binary"), cancelled));
  } else
    input.roles.erase("container-binary");
  const auto run = [&](std::vector<std::string> argv) {
    Stop(cancelled);
    ToolRequest request;
    request.argv = std::move(argv);
    request.working_directory = fs::current_path().string();
    request.tail_bytes = 65536;
    if (input.roles.count("container-binary"))
      request.environment.emplace_back(
          "EMUFLOW_OPENSTA_CONTAINER_BINARY",
          input.roles.at("container-binary").path());
    auto result = RunExternalTool(request, cancelled, input.sources);
    Check(result.operation.ok(), "OpenSTA failed: " + result.operation.message +
                                     "\n" + result.diagnostic_tail);
    return result.diagnostic_tail;
  };
  const auto version = run({exe.path(), "-version"});
  std::smatch match;
  Check(std::regex_search(
            version, match,
            std::regex(R"((?:^|\s)([0-9]+)\.([0-9]+)\.([0-9]+)(?:\s|$))")),
        "OpenSTA executable does not report a semantic version");
  const auto major = std::stoull(match[1]), minor = std::stoull(match[2]);
  Check(major > 3 || (major == 3 && minor >= 1),
        "OpenSTA version is unsupported; requires 3.1.0 or newer");
  identity["version"] =
      Text(std::to_string(major) + "." + std::to_string(minor) + "." +
           std::to_string(std::stoull(match[3])));
  const auto runtime = run({exe.path(), "-no_init", "-exit",
                            input.roles.at("runtime-check-script").path()});
  Check(runtime.find("EMUFLOW_OPENSTA_RUNTIME status=pass") !=
            std::string::npos,
        "OpenSTA runtime qualification failed; Tcl 8.6+ with try/finally is "
        "required");
  return D::Encode(identity);
}
} // namespace
StageResult PreparePhase2Policy(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *cancelled) {
  Inputs input(request);
  return RunStage(
      "phase2.prepare-policy",
      [&] {
        input.Read();
        return input.sources;
      },
      [&](const auto &sources) {
        Stop(cancelled);
        const fs::path target = fs::absolute(output);
        Check(!fs::is_symlink(target), "policy output aliases a link");
        for (const auto &source : sources)
          Check(fs::weakly_canonical(target) != fs::path(source.path()),
                "policy output aliases a source");
        SqliteStore ir(input.roles.at("emuir"));
        auto &db = ir.database();
        const auto used =
            UsedTypes(db, input.roles.count("architecture-timing-db"));
        const auto model = Model(input, db, used);
        D library(model), origin(library.Token("source"));
        const auto clocks = ClockMap(db, input.options.at("clocks"));
        ValidateIo(db, input.options.at("timing_io"), clocks);
        const auto engine = Engine(input, cancelled);
        D cells(library.Token("cells"));
        std::vector<std::string> types;
        for (const auto &row : cells.Fields())
          types.push_back(row.first);
        D design(Meta(db, "design_json"));
        O config = {
            {"design", design.Token("name")},
            {"liberty", Text(RenderOpenstaLiberty(model))},
            {"clock_map", clocks},
            {"timing_io", input.options.at("timing_io")},
            {"timing_io_sha256",
             input.options.at("timing_io") == "null"
                 ? "null"
                 : Text(Hash(input.options.at("timing_io")))},
            {"model",
             D::Encode(
                 {{"name", library.Token("name")},
                  {"qualification", origin.Token("qualification")},
                  {"architecture_timing_db",
                   input.roles.count("architecture-timing-db")
                       ? Text(input.roles.at("architecture-timing-db").path())
                       : "null"},
                  {"source", origin.text()},
                  {"cell_types", Array(types)}})},
            {"engine", engine},
            {"used_cell_type_summary", UsedSummary(used)},
            {"export_mode",
             input.roles.count("architecture-timing-db") ? "1" : "0"},
            {"max_paths", input.options.at("max_paths")},
            {"optimization_enabled", input.options.at("optimization_enabled")},
            {"criticality_scale", input.options.at("criticality_scale")},
            {"criticality_exponent", input.options.at("criticality_exponent")}};
        std::vector<std::string> refs;
        for (const auto &role : input.roles)
          refs.push_back(D::Encode({{"role", Text(role.first)},
                                    {"path", Text(role.second.path())},
                                    {"schema", Text(role.second.schema())},
                                    {"generation", Generation(role.second)}}));
        std::string rows = "[";
        for (const auto &ref : refs) {
          if (rows.size() > 1)
            rows += ',';
          rows += ref;
        }
        rows += ']';
        const auto value =
            D::Encode({{"schema", Text("emuflow.phase2-policy/v1")},
                       {"configuration", D::Encode(config)},
                       {"sources", rows}}) +
            "\n";
        Check(value.size() <= 8 * 1024 * 1024, "configuration exceeds bound");
        Stop(cancelled);
        PublishTextArtifact(target.string(), sources,
                            [&](std::ostream &out) { out << value; });
        ir.RequireUnchanged();
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "phase2-policy-v1")};
      });
}
} // namespace emuflow::core
