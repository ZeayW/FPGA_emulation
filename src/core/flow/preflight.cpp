#include "flow/preflight.hpp"
#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/tool_runtime.hpp"
#include "frontend/configuration.hpp"
#include "partition/phase3.hpp"
#include "physical/openparf/runtime.hpp"
#include "timing/phase2.hpp"
#include "transport/chimew/geometry.hpp"
#include "transport/transport.hpp"
#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <optional>
#include <regex>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
namespace fs = std::filesystem;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidRequest, "compile admission: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "compile admission cancelled");
}
std::string Env(const std::string &key) {
  const auto *value = std::getenv(key.c_str());
  return value ? value : "";
}
std::string Array(const std::vector<std::string> &rows) {
  std::string text = "[";
  for (const auto &row : rows) {
    if (text.size() > 1)
      text += ',';
    text += row;
  }
  return text + ']';
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Receipt(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  std::vector<JsonRecord> parts;
  for (const auto &n :
       {std::to_string(g.device), std::to_string(g.inode),
        std::to_string(g.bytes), std::to_string(g.modified_seconds),
        std::to_string(g.modified_nanoseconds),
        std::to_string(g.changed_seconds),
        std::to_string(g.changed_nanoseconds)})
    parts.emplace_back(n);
  return JsonRecord(std::vector<JsonRecord>{"tuple", parts}).Encode();
}
std::string Record(const std::string &role, const ArtifactRef &ref) {
  return D::Encode({{"role", JsonQuote(role)},
                    {"path", JsonQuote(ref.path())},
                    {"schema", JsonQuote(ref.schema())},
                    {"generation", Generation(ref)}});
}
std::string PackedPath(const fs::path &path) {
  return "[\"path\"," + JsonQuote(path.string()) + "]";
}
std::string Pack(const std::string &token) {
  if (token[0] == '{') {
    O fields;
    for (const auto &field : D(token).Fields())
      fields[field.first] = Pack(field.second);
    return "[\"map\"," + D::Encode(fields) + "]";
  }
  if (token[0] == '[') {
    std::vector<std::string> rows;
    for (const auto &value : D::Array(token))
      rows.push_back(Pack(value));
    return "[\"list\"," + Array(rows) + "]";
  }
  return token;
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto path = (parent / ".compile-admission-XXXXXX").string();
    Check(mkdtemp(path.data()),
          "cannot create private configuration workspace");
    root = path;
  }
  ~Work() {
    std::error_code error;
    fs::remove_all(root, error);
  }
};
std::vector<std::string> Split(const std::string &text) {
  std::vector<std::string> result;
  std::size_t begin = 0;
  while (begin <= text.size()) {
    const auto end = text.find(':', begin);
    const auto item = text.substr(begin, end - begin);
    if (!item.empty())
      result.push_back(item);
    if (end == std::string::npos)
      break;
    begin = end + 1;
  }
  return result;
}
std::string Join(const std::vector<std::string> &values) {
  std::set<std::string> seen;
  std::string result;
  for (const auto &v : values)
    if (!v.empty() && seen.insert(v).second) {
      if (!result.empty())
        result += ':';
      result += v;
    }
  return result;
}

struct Admission {
  ArtifactRef request, native;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> files;
  O options, kinds, state;
  fs::path package, repository, prefix, cwd, output;
  std::string board, python, pattern_policy = "strict-ranges-v1";
  std::optional<ArtifactRef> board_ref, container;
  const volatile std::sig_atomic_t *cancelled;
  Admission(const ArtifactRef &r, const ArtifactRef &n,
            const volatile std::sig_atomic_t *flag)
      : request(r), native(n), cancelled(flag) {}
  bool Has(const std::string &name) const {
    const auto p = options.find(name);
    return p != options.end() && p->second != "null";
  }
  std::string Text(const std::string &name) const {
    return DecodeJsonString(options.at(name));
  }
  bool Flag(const std::string &name) const {
    const auto &v = options.at(name);
    Check(v == "true" || v == "false", "invalid boolean option " + name);
    return v == "true";
  }
  fs::path Absolute(const std::string &text) {
    Check(!text.empty() && text.find('\0') == std::string::npos,
          "empty/NUL source path");
    auto expanded = text;
    if (text == "~" || text.rfind("~/", 0) == 0) {
      Check(!Env("HOME").empty(), "home path is unavailable");
      expanded = Env("HOME") + text.substr(1);
    }
    fs::path p(expanded);
    return (p.is_absolute() ? p : cwd / p).lexically_normal();
  }
  ArtifactRef Bind(const fs::path &path, const std::string &schema) {
    Stop(cancelled);
    const auto ref = ArtifactRef::Bind(path.string(), schema);
    const auto key = ref.path() + "\n" + schema;
    const auto previous = files.find(key);
    if (previous == files.end()) {
      files.emplace(key, ref);
      sources.push_back(ref);
    } else
      Check(previous->second.generation() == ref.generation(),
            "source generation changed during admission");
    return ref;
  }
  fs::path Asset(const fs::path &relative) {
    for (const auto &root : {repository, prefix, repository / "share/emuflow",
                             prefix / "share/emuflow"}) {
      const auto p = root / relative;
      if (fs::is_regular_file(p))
        return fs::canonical(p);
    }
    Check(false, "runtime resource is missing: " + relative.string());
    return {};
  }
  fs::path Package(const fs::path &relative) {
    for (const auto &root : {package, prefix / "lib/emuflow"}) {
      const auto path = root / relative;
      if (fs::is_regular_file(path))
        return fs::canonical(path);
    }
    Check(false, "package resource is missing: " + relative.string());
    return {};
  }
  fs::path Tool(const std::string &name, const std::string &option) {
    fs::path path;
    if (Has(option))
      path = Absolute(Text(option));
    else
      path = prefix / "bin" / name;
    Check(fs::is_regular_file(path) && access(path.c_str(), X_OK) == 0,
          "required " + name + " executable is unavailable: " + path.string());
    path = fs::canonical(path);
    Bind(path, "external-tool-executable-v1");
    return path;
  }
  std::string Source(const std::string &role, const fs::path &path,
                     const std::string &schema) {
    return Record(role, Bind(path, schema));
  }
  std::optional<ArtifactRef> OptionalContainer(const std::string &kind) {
    const auto value = Env("EMUFLOW_" + kind + "_CONTAINER_BINARY");
    if (value.empty())
      return std::nullopt;
    return Bind(Absolute(value), "external-tool-executable-v1");
  }
  void Read() {
    Check(request.schema() == "native-compile-request-v1" &&
              native.schema() == "external-tool-executable-v1",
          "request/tool schema disagrees");
    sources = {request, native};
    D doc(D::Read(request, 1024 * 1024));
    auto expected =
        std::set<std::string>{"schema", "python_executable", "python_source",
                              "working_directory", "options"};
    if (doc.Has("pattern_range_policy"))
      expected.insert("pattern_range_policy");
    doc.Keys(expected);
    Check(doc.Text("schema") == "emuflow.native-compile/v1",
          "request version disagrees");
    cwd = doc.Text("working_directory");
    Check(cwd.is_absolute() && fs::is_directory(cwd),
          "invocation directory must be absolute and available");
    const fs::path python_source = doc.Text("python_source");
    Check(python_source.is_absolute() && fs::is_directory(python_source),
          "package source root is unavailable");
    package = python_source / "emuflow";
    repository = python_source.filename() == "python" &&
                         python_source.parent_path().filename() == "src"
                     ? python_source.parent_path().parent_path()
                     : python_source.parent_path();
    prefix = Env("EMUFLOW_NATIVE_ROOT").empty()
                 ? fs::path(native.path()).parent_path().parent_path()
                 : Absolute(Env("EMUFLOW_NATIVE_ROOT"));
    prefix = fs::weakly_canonical(prefix);
    python = doc.Text("python_executable");
    Check(fs::path(python).is_absolute(),
          "Python host executable must be absolute");
    Bind(python, "external-tool-executable-v1");
    if (doc.Has("pattern_range_policy"))
      pattern_policy = doc.Text("pattern_range_policy");
    Check(pattern_policy == "strict-ranges-v1" ||
              pattern_policy == "empty-ranges-v1",
          "unsupported instance pattern range convention");
    const auto defaults =
        Bind(Package("data/flow/compile-options.json"), "compile-options-v1");
    D contract(D::Read(defaults));
    Check(contract.Text("schema") == "emuflow.compile-options/v1",
          "compile options version disagrees");
    const auto supplied = D(doc.Token("options")).Fields();
    const auto entries = D(contract.Token("options")).Fields();
    for (const auto &field : supplied)
      Check(entries.count(field.first),
            "unknown compile option " + field.first);
    for (const auto &entry : entries) {
      D rule(entry.second);
      const auto name = entry.first, kind = rule.Text("kind");
      kinds[name] = kind;
      std::string value;
      if (supplied.count(name))
        value = supplied.at(name);
      else if (rule.Has("resource")) {
        D resource(rule.Token("resource"));
        value = JsonQuote((resource.Text("scope") == "package"
                               ? Package(resource.Text("path"))
                               : Asset(resource.Text("path")))
                              .string());
      } else
        value = rule.Token("default");
      if (value == "null") {
        Check(rule.Boolean("nullable"), "nonnullable compile option " + name);
        options[name] = value;
        continue;
      }
      if (kind == "path" || kind == "string") {
        const auto text = DecodeJsonString(value);
        if (kind == "path") {
          Check(fs::path(text).is_absolute(),
                "compile " + name + " requires absolute paths");
          value = JsonQuote(Absolute(text).string());
        }
      } else if (kind == "integer")
        (void)D::IntegerToken(value);
      else if (kind == "number")
        (void)D("{\"value\":" + value + "}").Number("value");
      else if (kind == "boolean")
        Check(value == "true" || value == "false",
              "invalid boolean option " + name);
      else if (kind == "object")
        (void)D(value);
      else {
        Check(kind == "path-list" || kind == "string-list",
              "unknown option type");
        std::vector<std::string> rows;
        for (const auto &token : D::Array(value)) {
          const auto text = DecodeJsonString(token);
          if (kind == "path-list") {
            Check(fs::path(text).is_absolute(),
                  "compile " + name + " requires absolute paths");
            rows.push_back(JsonQuote(Absolute(text).string()));
          } else
            rows.push_back(JsonQuote(text));
        }
        value = Array(rows);
      }
      options[name] = value;
    }
    Check(Has("output_dir"),
          "multi-FPGA compilation requires an output directory");
    output = fs::weakly_canonical(Text("output_dir"));
    options["output_dir"] = JsonQuote(output.string());
    Check(!fs::exists(output) ||
              (fs::is_directory(output) && fs::is_empty(output)),
          "multi-FPGA output path must be an empty directory");
    board_ref = Bind(Text("platform_path"), "boarddb-v1");
    board = NormalizeBoard(D::Read(*board_ref));
  }
  void Validate() {
    Check(D::IntegerToken(options.at("patron_algorithm_version")) == 14,
          "only PATRON v14 is supported");
    Check(Text("partition_provider") == "patron",
          "unsupported Phase 3 provider: complete compilation requires "
          "TritonPart initialization followed by PATRON v14");
    Check(!Has("source_checksums") ||
              (!Has("yosys_json") && !Has("prepared_frontend") &&
               !Has("prepared_timing")),
          "source checksums apply only to original RTL compilation");
    Check(!Flag("optimize_frame_slots"),
          "native compile does not support frame search");
    Check(Text("global_timing_engine") == "opensta",
          "only the OpenSTA global timing engine is supported");
    Check(!Has("global_sta_executable") || Flag("physical"),
          "global OpenSTA qualification requires physical implementation");
    const auto backend = Text("physical_backend");
    Check(backend == "rapidwright" || backend == "vivado",
          "unsupported physical backend");
    Check(Text("mapping_profile") == "xilinx-ultrascaleplus-open-v1",
          "unsupported multi-FPGA mapping profile");
    if (Has("xilinx_mapping_strategy")) {
      Check(std::set<std::string>{"hierarchical-abc9-v1",
                                  "hierarchical-classic-v1", "flatten-abc9-v1",
                                  "flatten-classic-v1"}
                .count(Text("xilinx_mapping_strategy")),
            "unsupported Xilinx mapping strategy");
      Check(!Has("yosys_json"),
            "Xilinx mapping strategy requires native Xilinx RTL synthesis");
    }
    Check(!Has("tdm_provider") ||
              Text("tdm_provider") ==
                  "deterministic-dependency-earliest-slot-v1",
          "unsupported Phase 5 provider");
    Check(Text("cut_mode") == "static-exact-combinational",
          "multi-FPGA compilation requires static-exact-combinational");
    Check(!Has("route_provider") ||
              Text("route_provider") == "timing-aware-global-candidate-v1",
          "unsupported Phase 4 routing provider");
    Check(Text("phase6_provider") == "baseline" ||
              Text("phase6_provider") == "chimew",
          "unsupported Phase 6 provider");
    const auto regions =
        D::IntegerToken(options.at("phase6_chimew_region_count"));
    Check(regions >= 2 && regions <= 31,
          "Chimew region count must be in [2, 31]");
    if (Text("phase6_provider") == "chimew")
      Check(Flag("physical") && Has("physical_architecture"),
            "Chimew requires physical implementation and source-qualified "
            "package geometry");
    Check(Has("architecture_timing_db"),
          "Xilinx mapping requires a source-sealed pre-placement architecture "
          "timing database");
    D architecture(
        D::Read(Bind(Text("architecture_timing_db"), "phase2-source-v1")));
    Check(architecture.Text("schema") ==
              "emuflow.xilinx-preplacement-timing-db/v1",
          "architecture timing schema disagrees");
    const bool internal = !Has("timing_paths");
    Check(Text("partition_provider") != "patron" || internal,
          "PATRON requires the internally generated complete timing database");
    Check(internal || (!Flag("timing_driven") &&
                       !Has("architecture_timing_db") && !Flag("physical")),
          "externally projected timing paths conflict with the selected "
          "complete timing flow");
    Check(!internal || (Has("clock_periods") &&
                        !D(options.at("clock_periods")).Fields().empty()),
          "multi-FPGA timing analysis requires at least one --clock-period "
          "CLOCK=PERIOD_NS");
    state["exact_cut_mode"] = "true";
    state["internal_timing_database"] = internal ? "true" : "false";
    state["checked_link_timing"] = "null";
    state["checked_link_timing_validation"] = "null";
    if (Has("board_link_timing_db")) {
      const auto ref =
          Bind(Text("board_link_timing_db"), "phase3-configuration-source-v1");
      state["checked_link_timing"] = Pack(D::Read(ref));
      state["checked_link_timing_validation"] =
          Pack(QualifyBoardLinkTiming(*board_ref, ref));
    }
    if (Has("transport_cost_db")) {
      const auto ref =
          Bind(Text("transport_cost_db"), "transport-cost-input-v1");
      D platform(D(board).Token("platform"));
      ValidateTransportCostDatabase(ref, platform.Text("name"));
    }
    if (!Has("tritonpart_solution"))
      options["openroad"] = JsonQuote(Tool("openroad", "openroad").string());
    const std::vector<std::string> required = {
        "partition-initialization-request-v1",
        "partition-decision-request-v1",
        "partition-configuration-request-v1",
        "partition-hop-request-v1",
        "phase3-stage-request-v1",
        "partition-load-request-v2",
        "partition-finalization-request-v1",
        "assignment-publication-request-v3",
        "assignment-descriptor-request-v1",
        "exact-binary-publication-request-v1",
        "exact-binary-validation-request-v1",
        "partition-publication-request-v1",
        "phase5-stage-request-v1",
        "split-netlists-v3",
        "transport-stage-request-v1",
        "phase6-stage-request-v1",
        "phase6-compile-policy-v1",
        "boarddb-v1",
        "phase6-configuration-input-v1"};
    for (const auto &name : required)
      (void)FindSchema(name);
    container = OptionalContainer("OPENSTA");
    auto qualify = [&](const std::string &option) {
      const auto exe = Tool("sta", option);
      options[option] = JsonQuote(exe.string());
      QualifyOpenstaEngine(Bind(exe, "external-tool-executable-v1"),
                           Bind(Asset("scripts/opensta/container_runtime.sh"),
                                "phase2-source-v1"),
                           Bind(Asset("scripts/opensta/validate_runtime.tcl"),
                                "phase2-source-v1"),
                           container, cancelled);
    };
    if (internal) {
      for (const auto *name : {"opensta-input-export-request-v1",
                               "sta-path-request-v1", "sta-weight-request-v1"})
        (void)FindSchema(name);
      qualify("opensta");
    }
    if (Flag("physical")) {
      for (const auto *name :
           {"placement-lowering-request-v1", "transport-synthesis-request-v1"})
        (void)FindSchema(name);
      if (!Has("global_sta_executable"))
        options["global_sta_executable"] =
            JsonQuote(Tool("sta", "global_sta_executable").string());
      if (!internal ||
          options.at("global_sta_executable") != options.at("opensta"))
        qualify("global_sta_executable");
      state["global_sta_launcher_reference"] =
          PackedPath(Asset("scripts/opensta/container_runtime.sh"));
    }
  }
  ArtifactRef Write(const fs::path &path, const std::string &schema,
                    const std::string &text) {
    fs::create_directories(path.parent_path());
    PublishTextArtifact(path.string(), sources,
                        [&](std::ostream &stream) { stream << text << '\n'; });
    return ArtifactRef::Bind(path.string(), schema);
  }
  void Store(const std::string &name, const fs::path &destination,
             const ArtifactRef &ref) {
    state[name] = PackedPath(destination);
    std::string generation = name;
    const auto suffix = generation.rfind("configuration");
    if (suffix != std::string::npos && suffix + 13 == generation.size())
      generation += "_generation";
    else
      generation = generation.substr(0, generation.rfind('_')) + "_generation";
    state[generation] = Receipt(ref);
  }
  std::vector<std::string> TimingSources() {
    std::vector<std::string> rows{
        Source("timing-model",
               Has("architecture_timing_db")
                   ? Asset("resources/timing/ultrascaleplus-softlogic-v1.json")
                   : fs::path(Text("timing_model")),
               "phase2-source-v1"),
        Source("opensta-executable", Text("opensta"),
               "external-tool-executable-v1"),
        Source("export-script",
               Asset("scripts/opensta/export_timing_path_database.tcl"),
               "phase2-source-v1"),
        Source("runtime-check-script",
               Asset("scripts/opensta/validate_runtime.tcl"),
               "phase2-source-v1"),
        Source("runtime-launcher",
               Asset("scripts/opensta/container_runtime.sh"),
               "phase2-source-v1")};
    if (Has("architecture_timing_db")) {
      rows.push_back(Source("architecture-timing-db",
                            Text("architecture_timing_db"),
                            "phase2-source-v1"));
      rows.push_back(
          Source("architecture-contract",
                 Package("data/rapidwright/xcvu19p-timing-contract.json"),
                 "phase2-source-v1"));
    }
    if (container)
      rows.push_back(Record("container-binary", *container));
    return rows;
  }
  void AddContainer(std::vector<std::string> &rows, const std::string &role,
                    const ArtifactRef &engine, const std::string &kind) {
    const auto launcher =
        Bind(Asset(fs::path("scripts") / kind / "container_runtime.sh"),
             "exact-sha256-text-v1");
    if (Sha256File(engine, cancelled) == Sha256File(launcher, cancelled)) {
      auto upper = kind;
      std::transform(upper.begin(), upper.end(), upper.begin(),
                     [](unsigned char c) { return char(std::toupper(c)); });
      const auto binary = OptionalContainer(upper);
      Check(bool(binary),
            kind + " container engine identity lacks an explicit binary");
      rows.push_back(Record(role, *binary));
    }
  }
  std::string Openparf(const fs::path &root) {
    std::vector<fs::path> choices;
    if (Has("physical_openparf_install")) {
      const auto configured = Absolute(Text("physical_openparf_install"));
      choices = {configured, configured / "openparf"};
    } else
      choices = {prefix / "openparf"};
    fs::path installation;
    for (const auto &p : choices)
      if (fs::is_regular_file(p / "openparf.py") &&
          fs::is_directory(p / "openparf")) {
        installation = fs::canonical(p);
        break;
      }
    Check(!installation.empty(),
          "in-tree OpenPARF installation is unavailable");
    const auto interpreter = Absolute(Has("physical_openparf_python")
                                          ? Text("physical_openparf_python")
                                      : Env("EMUFLOW_OPENPARF_PYTHON").empty()
                                          ? python
                                          : Env("EMUFLOW_OPENPARF_PYTHON"));
    Check(fs::is_regular_file(interpreter) &&
              access(interpreter.c_str(), X_OK) == 0,
          "OpenPARF interpreter is unavailable");
    O environment;
    environment["PYTHONPATH"] =
        JsonQuote(installation.string() +
                  (Env("PYTHONPATH").empty() ? "" : ":" + Env("PYTHONPATH")));
    const auto lib = interpreter.parent_path().parent_path() / "lib";
    std::vector<std::string> torch;
    if (fs::is_directory(lib))
      for (const auto &entry : fs::directory_iterator(lib)) {
        const auto path = entry.path() / "site-packages/torch/lib";
        if (entry.path().filename().string().rfind("python", 0) == 0 &&
            fs::is_directory(path))
          torch.push_back(path.string());
      }
    std::sort(torch.begin(), torch.end());
    auto libraries = Split(Env("EMUFLOW_OPENPARF_LIBRARY_PATH"));
    libraries.push_back(lib.string());
    libraries.insert(libraries.end(), torch.begin(), torch.end());
    for (const auto &value : Split(Env("LD_LIBRARY_PATH"))) {
      const fs::path p(value);
      const bool foreign =
          p.filename() == "lib" && p.parent_path().filename() == "torch" &&
          value.find("site-packages") != std::string::npos &&
          std::find(torch.begin(), torch.end(), value) == torch.end();
      if (!foreign)
        libraries.push_back(value);
    }
    environment["LD_LIBRARY_PATH"] = JsonQuote(Join(libraries));
    const auto input = Write(
        root / "private-openparf-input.json", "openparf-runtime-request-v1",
        D::Encode({{"schema", JsonQuote("emuflow.openparf-runtime-request/v1")},
                   {"mode", JsonQuote("preflight")},
                   {"sources", "[]"},
                   {"python", JsonQuote(interpreter.string())},
                   {"installation", JsonQuote(installation.string())},
                   {"environment", D::Encode(environment)},
                   {"allow_detailed_legalization_handoff", "false"},
                   {"use_checkpoint", "false"}}));
    const auto made = RunOpenparfStage(
        input, (root / "private-openparf/runtime.json").string(), cancelled);
    if (!made.ok())
      throw StageError(made.error,
                       "OpenPARF runtime preflight failed: " + made.message);
    return D::Encode({{"python", JsonQuote(interpreter.string())},
                      {"installation", JsonQuote(installation.string())},
                      {"environment", D::Encode(environment)}});
  }
  void Physical(const fs::path &root) {
    if (!Flag("physical"))
      return;
    Check(D::IntegerToken(options.at("physical_workers")) >= 1 &&
              D::IntegerToken(options.at("physical_workers")) <= 256,
          "physical workers must be between 1 and 256");
    Check(D::IntegerToken(options.at("physical_seed")) >= 0 &&
              D::IntegerToken(options.at("physical_seed")) <= 0xffffffffLL,
          "physical seed is outside uint32 domain");
    const auto backend = Text("physical_backend");
    std::string runtime;
    if (backend == "rapidwright") {
      for (const auto *name :
           {"rwroute-export-request-v2", "routed-segment-request-v1",
            "routed-endpoint-request-v1", "routed-opensta-request-v1"})
        (void)FindSchema(name);
      runtime = Openparf(root);
    } else
      for (const auto *name :
           {"vivado-lineage-store-v2", "vivado-export-request-v1"})
        (void)FindSchema(name);
    const auto engine =
        Bind(Tool("yosys", "yosys"), "external-tool-executable-v1");
    std::vector<std::string> rows{
        Source("library",
               Package("data/rapidwright/"
                       "xilinx-ultrascaleplus-open-v1.primitives.json"),
               "physical-input-document-v1"),
        Record("yosys", engine)};
    AddContainer(rows, "yosys-container", engine, "yosys");
    O settings;
    if (backend == "vivado") {
      rows.push_back(Source("backend.tool", Tool("vivado", "physical_vivado"),
                            "external-tool-executable-v1"));
      const auto script =
          Asset("scripts/vivado/implement_indexed_partition.tcl");
      std::vector<fs::path> scripts;
      for (const auto &entry : fs::directory_iterator(script.parent_path()))
        if (entry.is_regular_file() && entry.path().extension() == ".tcl")
          scripts.push_back(entry.path());
      std::sort(scripts.begin(), scripts.end());
      for (const auto &path : scripts)
        rows.push_back(Source("backend.script." + path.filename().string(),
                              path, "exact-sha256-text-v1"));
      const auto threads = Env("EMUFLOW_VIVADO_THREADS").empty()
                               ? 2
                               : D::IntegerToken(Env("EMUFLOW_VIVADO_THREADS"));
      settings = {
          {"place_directive", options.at("physical_vivado_place_directive")},
          {"route_directive", options.at("physical_vivado_route_directive")},
          {"threads", std::to_string(threads)}};
    } else {
      for (const auto *name :
           {"physical_architecture", "physical_rapidwright_jar",
            "physical_rapidwright_java", "physical_rapidwright_device_data",
            "physical_rapidwright_timing_data",
            "physical_rapidwright_native_constraints",
            "physical_rapidwright_provider_manifest"})
        Check(Has(name),
              std::string("RapidWright backend runtime is incomplete: ") +
                  name);
      const auto java = Absolute(Text("physical_rapidwright_java")),
                 device =
                     fs::canonical(Text("physical_rapidwright_device_data")),
                 timing =
                     fs::canonical(Text("physical_rapidwright_timing_data"));
      const auto sta = Bind(Tool("sta", "physical_rapidwright_opensta"),
                            "external-tool-executable-v1");
      const std::vector<std::tuple<std::string, fs::path, std::string>> inputs{
          {"architecture", Text("physical_architecture"),
           "physical-input-document-v1"},
          {"policy", Package("data/openparf/input-policy.json"),
           "physical-input-document-v1"},
          {"driver", Package("physical/openparf/openparf_native_driver.py"),
           "physical-input-document-v1"},
          {"sta", sta.path(), "external-tool-executable-v1"},
          {"base-model",
           Asset("resources/timing/ultrascaleplus-softlogic-v1.json"),
           "phase2-source-v1"},
          {"export-script",
           Asset("scripts/opensta/export_timing_path_database.tcl"),
           "phase2-source-v1"},
          {"runtime-script", Asset("scripts/opensta/validate_runtime.tcl"),
           "phase2-source-v1"},
          {"java", java, "external-tool-executable-v1"},
          {"javac", java.parent_path() / "javac",
           "external-tool-executable-v1"},
          {"jar", Text("physical_rapidwright_jar"),
           "physical-input-document-v1"},
          {"java_source",
           Has("physical_rapidwright_java_source")
               ? fs::path(Text("physical_rapidwright_java_source"))
               : Asset("scripts/rapidwright/EmuFlowRWRoute.java"),
           "physical-input-document-v1"},
          {"contract", Package("data/rapidwright/xcvu19p-timing-contract.json"),
           "physical-input-document-v1"},
          {"parts", device / "data/parts.db", "physical-input-document-v1"},
          {"device", device / "data/devices/virtexuplus/xcvu19p_db.dat",
           "physical-input-document-v1"},
          {"intersite", timing / "intersite_delay_terms.txt",
           "physical-input-document-v1"},
          {"intrasite", timing / "intrasite_delay_terms.txt",
           "physical-input-document-v1"},
          {"native", Text("physical_rapidwright_native_constraints"),
           "physical-input-document-v1"},
          {"provider", Text("physical_rapidwright_provider_manifest"),
           "physical-input-document-v1"}};
      for (const auto &item : inputs)
        rows.push_back(Source("backend." + std::get<0>(item), std::get<1>(item),
                              std::get<2>(item)));
      AddContainer(rows, "backend.container", sta, "opensta");
      settings = {
          {"openparf_runtime", runtime},
          {"max_heap", JsonQuote(Env("EMUFLOW_RAPIDWRIGHT_MAX_HEAP").empty()
                                     ? "32g"
                                     : Env("EMUFLOW_RAPIDWRIGHT_MAX_HEAP"))},
          {"device_data_root", JsonQuote(device.string())}};
    }
    const auto policy = Write(
        root / ".native-physical-request/physical-policy.json",
        "physical-flow-policy-v1",
        D::Encode({{"schema", JsonQuote("emuflow.physical-flow-policy/v1")},
                   {"sources", Array(rows)},
                   {"backend", options.at("physical_backend")},
                   {"backend_options", D::Encode(settings)},
                   {"workers", options.at("physical_workers")},
                   {"seed", options.at("physical_seed")}}));
    Store("native_physical_policy",
          output / ".native-physical-request/physical-policy.json", policy);
  }
  void StaticPolicies(const fs::path &root) {
    if (!Has("timing_paths") && !Has("prepared_timing")) {
      const O settings{
          {"clocks", options.at("clock_periods")},
          {"timing_io", options.at("timing_io")},
          {"max_paths", options.at("sta_max_paths")},
          {"optimization_enabled", options.at("timing_driven")},
          {"criticality_scale", options.at("timing_criticality_scale")},
          {"criticality_exponent", options.at("timing_criticality_exponent")}};
      const auto path = root / ".native-phase2-configuration/request.json";
      const auto ref = Write(
          path, "phase2-configuration-request-v1",
          D::Encode(
              {{"schema", JsonQuote("emuflow.phase2-configuration-request/v1")},
               {"options", D::Encode(settings)},
               {"sources", Array(TimingSources())}}));
      Store("native_phase2_configuration",
            output / ".native-phase2-configuration/request.json", ref);
    }
    std::vector<std::string> p3{
        Record("board", *board_ref),
        Source("facts", Package("data/partition/structural-policy.json"),
               "phase3-configuration-source-v1"),
        Record("native", native)};
    for (const auto &item :
         std::vector<std::tuple<std::string, std::string, std::string>>{
             {"constraints", "partition_constraints",
              "partition-constraints-input-v1"},
             {"route_constraints", "route_constraints",
              "phase3-configuration-source-v1"},
             {"board_link_timing", "board_link_timing_db",
              "phase3-configuration-source-v1"},
             {"solution", "tritonpart_solution", "tritonpart-solution-v1"}})
      if (Has(std::get<1>(item)))
        p3.push_back(Source(std::get<0>(item), Text(std::get<1>(item)),
                            std::get<2>(item)));
    O config{{"pattern_range_policy", JsonQuote(pattern_policy)},
             {"min_used_fpgas", options.at("min_used_fpgas")},
             {"balance_tolerance", options.at("balance_tolerance")},
             {"provider", options.at("partition_provider")},
             {"seed", options.at("seed")},
             {"openroad", Has("tritonpart_solution") ? JsonQuote("")
                                                     : options.at("openroad")},
             {"timeout_seconds", options.at("partition_timeout_seconds")},
             {"seed_attempts", options.at("partition_seed_attempts")},
             {"num_initial_solutions",
              options.at("partition_num_initial_solutions")},
             {"num_best_initial_solutions",
              options.at("partition_num_best_initial_solutions")},
             {"repair_min_used", options.at("partition_repair_min_used_fpgas")},
             {"unweighted_baseline", "false"},
             {"max_moves", options.at("patron_max_moves")},
             {"max_cross_fpga_dependency_depth",
              options.at("max_cross_fpga_dependency_depth")},
             {"candidate_selection_policy",
              options.at("static_exact_candidate_policy")},
             {"timing_driven", options.at("timing_driven")}};
    auto ref =
        Write(root / ".native-phase3-configuration/request.json",
              "phase3-configuration-request-v1",
              D::Encode({{"schema",
                          JsonQuote("emuflow.phase3-configuration-request/v1")},
                         {"board", board},
                         {"options", D::Encode(config)},
                         {"sources", Array(p3)}}));
    Store("native_phase3_configuration",
          output / ".native-phase3-configuration/request.json", ref);
    std::vector<std::string> p4{Record("board", *board_ref)};
    if (Has("route_constraints"))
      p4.push_back(Source("constraints", Text("route_constraints"),
                          "system-route-constraints-input-v1"));
    if (Has("timing_paths"))
      p4.push_back(Source("external-timing", Text("timing_paths"),
                          "system-routing-paths-v1"));
    else {
      p4.push_back(Source(
          "timing-model",
          Has("architecture_timing_db")
              ? Asset("resources/timing/ultrascaleplus-softlogic-v1.json")
              : fs::path(Text("timing_model")),
          "phase2-source-v1"));
      if (Has("architecture_timing_db")) {
        p4.push_back(Source("architecture-timing-db",
                            Text("architecture_timing_db"),
                            "phase2-source-v1"));
        p4.push_back(
            Source("architecture-contract",
                   Package("data/rapidwright/xcvu19p-timing-contract.json"),
                   "phase2-source-v1"));
      }
    }
    config = {{"frame_slots", options.at("frame_slots")},
              {"max_iterations", options.at("route_max_iterations")},
              {"candidate_workers", options.at("route_candidate_workers")},
              {"timing_driven", options.at("timing_driven")}};
    ref = Write(
        root / ".native-phase4-configuration/request.json",
        "phase4-configuration-v1",
        D::Encode({{"schema", JsonQuote("emuflow.phase4-configuration/v1")},
                   {"board", board},
                   {"options", D::Encode(config)},
                   {"sources", Array(p4)}}));
    Store("native_phase4_configuration",
          output / ".native-phase4-configuration/request.json", ref);
    {
      std::vector<std::string> p6{Record("board", *board_ref)};
      D platform(D(board).Token("platform"));
      if (platform.Has("virtual_board_model") &&
          platform.Token("virtual_board_model") != "null") {
        p6.push_back(Source(
            "inventory", Package("data/io/xcvu19p-fsva3824-2-e.hp-pins.json"),
            "hp-package-pin-inventory-v1"));
        if (platform.Has("virtual_board_wiring") &&
            platform.Token("virtual_board_wiring") != "null")
          p6.push_back(Source("wiring",
                              Asset(fs::path("platforms/virtual") /
                                    platform.Text("virtual_board_wiring")),
                              "fixed-wiring-csv-v1"));
      }
      if (Has("transport_cost_db"))
        p6.push_back(Source("cost", Text("transport_cost_db"),
                            "transport-cost-input-v1"));
      p6.push_back(Source("python", python, "external-tool-executable-v1"));
      p6.push_back(Source("oracle", Package("transport/phase6_oracle.py"),
                          "phase6-oracle-source-v1"));
      config = {{"clock_periods", options.at("clock_periods")},
                {"cycles", options.at("equivalence_cycles")},
                {"seed", options.at("equivalence_seed")}};
      auto input = Write(
          root / "private-phase6-input.json", "phase6-configuration-input-v1",
          D::Encode(
              {{"schema", JsonQuote("emuflow.phase6-configuration-input/v1")},
               {"options", D::Encode(config)},
               {"sources", Array(p6)}}));
      const auto made = PreparePhase6Configuration(
          input, (root / ".native-phase6-configuration").string(), cancelled);
      Check(made.ok(),
            "native Phase 6 static configuration failed: " + made.message);
      Check(made.outputs.size() == 1,
            "Phase 6 configuration output coverage disagrees");
      Store("native_phase6_configuration",
            output / ".native-phase6-configuration/request.json",
            made.outputs.front());
    }
  }
  void Chimew(const fs::path &root) {
    if (Text("phase6_provider") != "chimew")
      return;
    for (const auto *option : {"phase6_chimew_grouper", "phase6_chimew_refiner",
                               "phase6_chimew_rudy", "phase6_chimew_assigner"})
      if (Has(option))
        Check(fs::canonical(Tool("emuflow-native", option)) == native.path(),
              "Chimew kernels must use the unified native executable");
    const auto directory = root / ".native-chimew-configuration";
    fs::create_directory(directory);
    const auto architecture =
        Bind(Text("physical_architecture"), "physical-input-document-v1");
    std::set<std::string> parts;
    for (const auto &item : D::Array(D(board).Token("fpgas"), 4096))
      parts.insert(D(item).Text("part"));
    Check(parts.size() == 1, "Chimew requires a single Xilinx device model");
    const auto made = PrepareChimewPackageGeometry(
        architecture, *parts.begin(), (directory / "package.json").string(),
        cancelled);
    Check(made.ok(), "Chimew package geometry failed: " + made.message);
    const auto package_ref = made.outputs.at(0);
    D catalog(D::Read(package_ref));
    D pins(catalog.Token("pins"));
    const auto inventory =
        Bind(Package("data/io/xcvu19p-fsva3824-2-e.hp-pins.json"),
             "hp-package-pin-inventory-v1");
    std::optional<ArtifactRef> wiring;
    D platform(D(board).Token("platform"));
    if (platform.Has("virtual_board_wiring") &&
        platform.Token("virtual_board_wiring") != "null")
      wiring = Bind(Asset(fs::path("platforms/virtual") /
                          platform.Text("virtual_board_wiring")),
                    "fixed-wiring-csv-v1");
    const auto fixed = BuildVirtualBoard(board, inventory, wiring, cancelled);
    D wired(fixed);
    for (const auto &wire : D::Array(wired.Token("wires"), 16384))
      for (const auto &end : D::Array(D(wire).Token("endpoints"), 2))
        for (const auto *polarity : {"p", "n"}) {
          D pin(D(end).Token(polarity));
          const auto name = pin.Text("package_pin");
          Check(pins.Has(name) &&
                    DecodeJsonString(D::Array(pins.Token(name), 3).at(0)) ==
                        pin.Text("site"),
                "Chimew native package geometry does not cover the fixed P/N "
                "wiring");
        }
    std::vector<std::string> rows{Record("architecture", architecture),
                                  Record("board", *board_ref),
                                  Record("inventory", inventory)};
    if (wiring)
      rows.push_back(Record("wiring", *wiring));
    const auto config =
        Write(directory / "request.json", "chimew-compile-configuration-v1",
              D::Encode({{"schema",
                          JsonQuote("emuflow.chimew-compile-configuration/v1")},
                         {"sources", Array(rows)},
                         {"package_geometry", catalog.text()}}));
    Store("native_chimew_configuration",
          output / ".native-chimew-configuration/request.json", config);
    fs::remove(directory / "package.json");
  }
  void Frontend(const fs::path &root) {
    const auto mode = Has("prepared_frontend") || Has("prepared_timing")
                          ? "prepared"
                      : Has("yosys_json") ? "yosys-json"
                                          : "rtl";
    O config{{"schema", JsonQuote("emuflow.frontend-configuration/v1")},
             {"mode", JsonQuote(mode)},
             {"output_dir", JsonQuote(output.string())},
             {"working_directory", JsonQuote(cwd.string())},
             {"top", Has("top") ? options.at("top") : JsonQuote("")},
             {"clocks", options.at("clocks")},
             {"clock_periods",
              Has("clock_periods") ? options.at("clock_periods") : "{}"},
             {"mapping_profile", options.at("mapping_profile")},
             {"mapping_strategy", options.at("xilinx_mapping_strategy")},
             {"platform", board},
             {"board", JsonQuote(board_ref->path())},
             {"board_generation", Generation(*board_ref)},
             {"library",
              JsonQuote(Package("data/rapidwright/"
                                "xilinx-ultrascaleplus-open-v1.primitives.json")
                            .string())},
             {"engine", std::string(mode) == "rtl"
                            ? JsonQuote(Tool("yosys", "yosys").string())
                            : "null"},
             {"rtl_sources", options.at("sources")},
             {"include_dirs", options.at("include_dirs")},
             {"defines", options.at("defines")},
             {"source_checksums", options.at("source_checksums")},
             {"prepared_frontend", options.at("prepared_frontend")},
             {"prepared_timing", options.at("prepared_timing")},
             {"yosys_json", options.at("yosys_json")}};
    const auto input =
        Write(root / "private-frontend-input.json",
              "frontend-configuration-request-v1", D::Encode(config));
    const auto directory = std::string(mode) == "rtl"
                               ? "frontend"
                               : ".native-frontend-input-request";
    const auto made = PrepareFrontendConfiguration(
        input, (root / directory).string(), cancelled);
    Check(made.ok(), "native frontend configuration failed: " + made.message);
    Check(made.outputs.size() == 1,
          "frontend configuration output coverage disagrees");
    for (const auto &ref : made.inputs)
      if (ref.path() != input.path())
        Bind(ref.path(), ref.schema());
    const auto name = std::string(mode) == "rtl"
                          ? "native_frontend_request"
                          : "native_frontend_input_request";
    const auto file = std::string(mode) == "rtl" ? "native-frontend-request.bin"
                                                 : "inputs.json";
    Store(name, output / directory / file, made.outputs.front());
  }
};
} // namespace
StageResult PrepareCompile(const ArtifactRef &request,
                           const ArtifactRef &native, const std::string &run_id,
                           const std::string &state,
                           const volatile std::sig_atomic_t *cancelled) {
  Admission in(request, native, cancelled);
  std::optional<Work> work;
  return RunStage(
      "preflight",
      [&] {
        in.Read();
        const auto relative = fs::weakly_canonical(fs::absolute(state))
                                  .lexically_relative(in.output);
        Check(relative.empty() || *relative.begin() == "..",
              "control state must be outside the published run directory");
        Check(!run_id.empty() && run_id.size() <= 4096,
              "invalid compiler run identity");
        in.Validate();
        fs::create_directories(in.output.parent_path());
        work.emplace(in.output.parent_path());
        in.Physical(work->root);
        in.Chimew(work->root);
        in.StaticPolicies(work->root);
        in.Frontend(work->root);
        return in.sources;
      },
      [&](const auto &) {
        const auto root = work->root;
        for (const auto &option : in.options) {
          const auto &kind = in.kinds.at(option.first);
          if (option.second == "null")
            in.state[option.first] = "null";
          else if (kind == "path")
            in.state[option.first] =
                PackedPath(DecodeJsonString(option.second));
          else if (kind == "path-list") {
            std::vector<std::string> values;
            for (const auto &token : D::Array(option.second))
              values.push_back(PackedPath(DecodeJsonString(token)));
            in.state[option.first] = "[\"tuple\"," + Array(values) + "]";
          } else if (kind == "string-list")
            in.state[option.first] =
                (option.first == "clocks" ? "[\"tuple\"," : "[\"list\",") +
                option.second + "]";
          else
            in.state[option.first] = Pack(option.second);
        }
        const auto encoded = D::Encode(
            {{"schema", JsonQuote("emuflow.native-compile-state/v1")},
             {"run_id", JsonQuote(run_id)},
             {"stage", JsonQuote("preflight")},
             {"status", JsonQuote("pass")},
             {"request_generation", D::Array(Receipt(request), 2).at(1)},
             {"effective_options", D::Encode(in.options)},
             {"state", "[\"map\"," + D::Encode(in.state) + "]"}});
        Check(encoded.size() <= 8 * 1024 * 1024,
              "preflight control state exceeds bound");
        for (const auto &ref : in.sources)
          ref.RequireUnchanged();
        Check(!fs::exists(in.output) ||
                  (fs::is_directory(in.output) && fs::is_empty(in.output)),
              "output changed during configuration admission");
        Stop(cancelled);
        fs::create_directories(in.output);
        std::vector<std::unique_ptr<Work>> staging;
        std::vector<NewDirectoryPublication> publications;
        for (const auto *name :
             {"frontend", ".native-frontend-input-request",
              ".native-phase2-configuration", ".native-phase3-configuration",
              ".native-phase4-configuration", ".native-phase6-configuration",
              ".native-physical-request", ".native-chimew-configuration"}) {
          if (!fs::is_directory(root / name))
            continue;
          staging.push_back(std::make_unique<Work>(in.output));
          fs::rename(root / name, staging.back()->root);
          publications.push_back({staging.back()->root, in.output / name, {}});
        }
        PublishNewDirectoryGroup(publications, in.sources, [&] {
          for (const auto &ref : in.sources)
            ref.RequireUnchanged();
          PublishTextArtifact(state, in.sources, [&](std::ostream &stream) {
            stream << encoded << '\n';
          });
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(state, "native-compile-state-v1")};
      });
}
} // namespace emuflow::core
