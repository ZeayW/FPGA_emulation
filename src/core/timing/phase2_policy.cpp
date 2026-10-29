#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/tool_runtime.hpp"
#include "storage/store.hpp"
#include "timing/cell_model.hpp"
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
struct Inputs {
  ArtifactRef request;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  O options;
  const ArtifactRef *compiled_emuir;
  explicit Inputs(const ArtifactRef &r, const ArtifactRef *ir)
      : request(r), compiled_emuir(ir) {}
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
    if (compiled_emuir) {
      Check(compiled_emuir->schema() == "emuir-compact-v2" &&
                !roles.count("emuir"),
            "compile timing configuration must bind the published frontend IR");
      roles.emplace("emuir", *compiled_emuir);
      sources.push_back(*compiled_emuir);
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
std::string
QualifyOpenstaEngine(const ArtifactRef &executable, const ArtifactRef &launcher,
                     const ArtifactRef &runtime_check,
                     const std::optional<ArtifactRef> &container_binary,
                     const volatile std::sig_atomic_t *cancelled) {
  Inputs input(executable, nullptr);
  input.roles = {{"opensta-executable", executable},
                 {"runtime-launcher", launcher},
                 {"runtime-check-script", runtime_check}};
  input.sources = {executable, launcher, runtime_check};
  if (container_binary) {
    input.roles.emplace("container-binary", *container_binary);
    input.sources.push_back(*container_binary);
  }
  for (const auto &ref : input.sources)
    ref.RequireUnchanged();
  const auto result = Engine(input, cancelled);
  for (const auto &ref : input.sources)
    ref.RequireUnchanged();
  return result;
}
StageResult PreparePhase2Policy(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *cancelled,
                                const ArtifactRef *compiled_emuir) {
  Inputs input(request, compiled_emuir);
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
        const auto shared =
            BuildTimingCellModel(db, input.roles.at("timing-model"),
                                 input.roles.count("architecture-timing-db")
                                     ? &input.roles.at("architecture-timing-db")
                                     : nullptr,
                                 input.roles.count("architecture-contract")
                                     ? &input.roles.at("architecture-contract")
                                     : nullptr);
        const auto &used = shared.used_types;
        const auto &model = shared.json;
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
