#include "physical/vivado/vivado_partition.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/tool_runtime.hpp"
#include "ir/emuir_view.hpp"
#include "physical/measurement_stage.hpp"
#include "physical/report.hpp"
#include "physical/vivado/vivado_inputs.hpp"
#include "physical/vivado/vivado_lineage.hpp"
#include "physical/vivado/vivado_timing_queries.hpp"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <regex>
#include <sstream>
#include <sys/file.h>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Raw = std::map<std::string, std::string>;
using Sources = std::map<std::string, ArtifactRef>;
void Check(bool yes, const std::string &why) {
  if (!yes)
    throw StageError(ErrorCode::kInvalidArtifact, "Vivado partition: " + why);
}
void Need(const StageResult &value) {
  if (!value.ok())
    throw StageError(value.error, value.message);
}
void Stop(const volatile std::sig_atomic_t *cancelled) {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed, "Vivado partition cancelled");
}
std::string Hash(const ArtifactRef &source,
                 const volatile std::sig_atomic_t *cancelled = nullptr) {
  std::ifstream in(source.path(), std::ios::binary);
  Check(bool(in), "cannot read sealed source");
  Sha256 sha;
  std::array<char, 65536> block{};
  while (in) {
    Stop(cancelled);
    in.read(block.data(), block.size());
    sha.update(reinterpret_cast<unsigned char *>(block.data()), in.gcount());
  }
  Check(in.eof(), "cannot finish source seal");
  source.RequireUnchanged();
  static constexpr char digits[] = "0123456789abcdef";
  std::string out;
  for (auto n : sha.finish()) {
    out += digits[n >> 4];
    out += digits[n & 15];
  }
  return out;
}
std::string StringHash(const std::string &text) {
  Sha256 sha;
  sha.update(reinterpret_cast<const unsigned char *>(text.data()), text.size());
  static constexpr char d[] = "0123456789abcdef";
  std::string out;
  for (auto n : sha.finish()) {
    out += d[n >> 4];
    out += d[n & 15];
  }
  return out;
}
std::string JsonNumber(double n) { return JsonRecord(n).Encode(); }
std::int64_t Natural(const std::string &value) {
  const auto n = Doc::IntegerToken(value);
  Check(n >= 0 && n <= INT64_MAX / 4096, "invalid count");
  return n;
}
std::string TextAt(const Raw &fields, const std::string &name) {
  auto i = fields.find(name);
  Check(i != fields.end(), "missing metric " + name);
  return i->second;
}
std::int64_t Integer(const Raw &fields, const std::string &name) {
  return Natural(TextAt(fields, name));
}
double Number(const Raw &fields, const std::string &name) {
  return ParseFiniteDecimal(TextAt(fields, name), "Vivado metric " + name);
}
void Generation(Doc &doc, const ArtifactRef &ref) {
  const auto values = Doc::Array(doc.Token("generation"), 7);
  Check(values.size() == 7, "invalid source generation");
  const auto &g = ref.generation();
  const std::string fields[] = {std::to_string(g.device),
                                std::to_string(g.inode),
                                std::to_string(g.bytes),
                                std::to_string(g.modified_seconds),
                                std::to_string(g.modified_nanoseconds),
                                std::to_string(g.changed_seconds),
                                std::to_string(g.changed_nanoseconds)};
  for (unsigned i = 0; i < 7; ++i)
    Check(values[i] == fields[i], "source generation changed");
}
std::string Stamp(const ArtifactRef &source) {
  const auto &g = source.generation();
  return Doc::Encode(
      {{"path", JsonQuote(source.path())},
       {"generation",
        "[" + std::to_string(g.device) + "," + std::to_string(g.inode) + "," +
            std::to_string(g.bytes) + "," + std::to_string(g.modified_seconds) +
            "," + std::to_string(g.modified_nanoseconds) + "," +
            std::to_string(g.changed_seconds) + "," +
            std::to_string(g.changed_nanoseconds) + "]"}});
}
void Write(const fs::path &path, const std::string &value,
           const std::vector<ArtifactRef> &sources) {
  PublishTextArtifact(path.string(), sources,
                      [&](std::ostream &out) { out << value << '\n'; });
}
Doc Read(const fs::path &path) {
  return Doc(
      Doc::Read(ArtifactRef::Bind(path.string(), "exact-sha256-text-v1")));
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".vivado-partition-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create scratch");
    root = name;
  }
  ~Workspace() {
    std::error_code e;
    fs::remove_all(root, e);
  }
};
struct OutputOwner {
  fs::path root, attempt;
  int descriptor = -1;
  explicit OutputOwner(const fs::path &path) : root(path) {
    Check(path.is_absolute() && !fs::is_symlink(path),
          "output must be an owned absolute directory");
    fs::create_directories(path.parent_path());
    if (!fs::exists(path))
      Check(fs::create_directory(path), "cannot claim output");
    Check(fs::is_directory(path), "output is not a directory");
    const auto lock = path / ".partition-owner.lock";
    Check(!fs::is_symlink(lock), "output lease is a symbolic link");
    descriptor =
        open(lock.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(descriptor >= 0, "cannot open output lease");
    if (flock(descriptor, LOCK_EX | LOCK_NB) != 0) {
      close(descriptor);
      descriptor = -1;
      Check(false, "output already has an active owner");
    }
    try {
      bool fresh = true;
      for (const auto &item : fs::directory_iterator(path))
        if (item.path() != lock) {
          fresh = false;
          break;
        }
      if (fresh)
        attempt = path;
      else {
        auto name = (path / "implementation.generation.XXXXXX").string();
        if (!mkdtemp(name.data())) {
          close(descriptor);
          descriptor = -1;
          Check(false, "cannot claim fresh implementation generation");
        }
        attempt = name;
      }
    } catch (...) {
      if (descriptor >= 0)
        close(descriptor);
      descriptor = -1;
      throw;
    }
  }
  OutputOwner(const OutputOwner &) = delete;
  OutputOwner &operator=(const OutputOwner &) = delete;
  ~OutputOwner() {
    if (descriptor >= 0)
      close(descriptor);
  }
};
bool Line(std::istream &in, std::string &value) {
  value.clear();
  char c;
  while (in.get(c)) {
    if (c == '\n') {
      if (!value.empty() && value.back() == '\r')
        value.pop_back();
      return true;
    }
    Check(value.size() < 1024 * 1024 && c != '\0',
          "tool record exceeds bound or contains NUL");
    value += c;
  }
  Check(in.eof(), "cannot read tool output");
  return !value.empty();
}
Raw Metrics(const fs::path &path) {
  std::ifstream in(path);
  Check(bool(in), "missing implementation metrics");
  std::string line;
  Check(Line(in, line) && line == "metric\tvalue", "invalid metrics header");
  Raw fields;
  while (Line(in, line)) {
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    if (line.empty())
      continue;
    const auto tab = line.find('\t');
    Check(tab != std::string::npos && tab > 0 &&
              line.find('\t', tab + 1) == std::string::npos &&
              fields.size() < 4096,
          "invalid metric row");
    Check(fields.emplace(line.substr(0, tab), line.substr(tab + 1)).second,
          "duplicate metric");
  }
  return fields;
}
Raw Timing(const fs::path &path) {
  std::ifstream in(path);
  Check(bool(in), "missing timing summary");
  bool armed = false;
  std::string line;
  while (Line(in, line)) {
    if (line.find("WNS(ns)") != std::string::npos &&
        line.find("TNS(ns)") != std::string::npos) {
      armed = true;
      continue;
    }
    if (!armed)
      continue;
    std::istringstream row(line);
    std::vector<std::string> fields;
    std::string f;
    while (row >> f) {
      Check(fields.size() < 256, "timing aggregate row exceeds bound");
      fields.push_back(f);
    }
    if (fields.size() < 8)
      continue;
    static const std::regex numeric(
        "[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?(?:"
        "inf(?:inity)?|nan)",
        std::regex::icase);
    if (!std::regex_match(fields[0], numeric) ||
        !std::regex_match(fields[1], numeric))
      continue;
    const auto wns = ParseFiniteDecimal(fields[0], "Vivado WNS"),
               tns = ParseFiniteDecimal(fields[1], "Vivado TNS");
    const auto failed = Natural(fields[2]), total = Natural(fields[3]);
    Check(tns <= 0 && failed <= total && bool(tns < 0) == bool(failed > 0),
          "invalid endpoint timing aggregate");
    return {{"wns_ns", JsonNumber(wns)},
            {"tns_ns", JsonNumber(tns)},
            {"failing_endpoints", std::to_string(failed)},
            {"failing_endpoint_constraints", std::to_string(failed)},
            {"analyzed_endpoints", std::to_string(total)}};
  }
  throw StageError(ErrorCode::kInvalidArtifact,
                   "Vivado timing summary has no complete setup aggregate");
}
void RouteStatus(const fs::path &path) {
  std::ifstream in(path);
  Check(bool(in), "missing route status");
  std::string line;
  const std::regex count("([0-9]+)\\s*$");
  while (Line(in, line)) {
    std::transform(line.begin(), line.end(), line.begin(),
                   [](unsigned char c) { return char(std::tolower(c)); });
    if (line.find("nets with routing errors") != std::string::npos ||
        line.find("unrouted nets") != std::string::npos ||
        line.find("partially routed nets") != std::string::npos) {
      std::smatch match;
      Check(std::regex_search(line, match, count),
            "route-status count is malformed");
      Check(Natural(match[1]) == 0,
            "route-status report contains incomplete routing");
    }
  }
}
std::string Usage(const Raw &counts, Doc &runtime) {
  if (!runtime.Has("resource_budget"))
    return "{}";
  Doc budget(runtime.Token("resource_budget")),
      capacity(budget.Token("capacity"));
  const auto limit = budget.Number("utilization_limit");
  Check(limit > 0 && limit <= 1 && !capacity.Fields().empty(),
        "invalid resource budget");
  Raw used, allowed, device_util, budget_util;
  for (const auto &item : capacity.Fields()) {
    const auto total = Natural(item.second),
               count = Integer(counts, item.first);
    const auto maximum = std::int64_t(std::floor(double(total) * limit));
    Check(count <= maximum,
          "strict whole-device resource budget exceeded: " + item.first);
    used[item.first] = std::to_string(count);
    allowed[item.first] = std::to_string(maximum);
    device_util[item.first] = JsonNumber(total ? double(count) / total : 0);
    budget_util[item.first] = JsonNumber(maximum ? double(count) / maximum : 0);
  }
  if (budget.Has("effective_capacity")) {
    SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(db, budget.Token("effective_capacity"),
                          Doc::Encode(allowed),
                          "effective device capacity disagrees");
  }
  return Doc::Encode(
      {{"physical",
        Doc::Encode({{"schema", JsonQuote("emuflow.device-resource-usage/v1")},
                     {"status", JsonQuote("pass")},
                     {"stage", JsonQuote("post-physical-materialization")},
                     {"fpga", budget.Token("id")},
                     {"part", budget.Token("part")},
                     {"capacity_basis", JsonQuote("whole-device-raw-capacity")},
                     {"capacity", capacity.text()},
                     {"utilization_limit", JsonNumber(limit)},
                     {"allowed", Doc::Encode(allowed)},
                     {"counts", Doc::Encode(used)},
                     {"device_utilization", Doc::Encode(device_util)},
                     {"budget_utilization", Doc::Encode(budget_util)}})}});
}
std::string Query(const std::vector<ArtifactRef> &inputs,
                  const fs::path &request, const fs::path &output, bool resolve,
                  bool boundary, const volatile std::sig_atomic_t *cancelled) {
  CheckedRequestWriter writer(request.string(), inputs);
  writer.Put<std::uint64_t>(resolve ? 0x31525156464d5545ULL
                                    : 0x31425156464d5545ULL);
  writer.Put<std::uint32_t>(1);
  for (const auto &source : inputs)
    writer.Generation(source.generation());
  if (resolve)
    writer.Put<std::uint8_t>(boundary);
  writer.Finish();
  auto sources = inputs;
  sources.push_back(ArtifactRef::Bind(
      request.string(), resolve ? "vivado-resolve-query-request-v1"
                                : "vivado-boundary-query-request-v1"));
  Need(resolve ? ResolveVivadoTimingQuery(sources, output.string(), cancelled)
               : WriteVivadoBoundaryQuery(sources, output.string(), cancelled));
  return Read(output / "summary.json").text();
}
std::string Execute(const Sources &owners, const std::vector<std::string> &args,
                    const fs::path &output, const std::string &script,
                    const std::string &log, std::vector<ArtifactRef> &sources,
                    const volatile std::sig_atomic_t *cancelled) {
  ToolRequest request;
  request.argv = {owners.at("tool").path(),
                  "-mode",
                  "batch",
                  "-nojournal",
                  "-nolog",
                  "-notrace",
                  "-source",
                  owners.at("script." + script).path(),
                  "-tclargs"};
  request.argv.insert(request.argv.end(), args.begin(), args.end());
  request.working_directory = output.string();
  request.environment = {{"TMPDIR", (output / ".tmp").string()},
                         {"XILINX_LOCAL_USER_DATA", "no"}};
  const auto result = RunExternalToolLogged(request, (output / log).string(),
                                            cancelled, sources);
  auto logfile =
      ArtifactRef::Bind((output / log).string(), "exact-sha256-text-v1");
  std::ifstream in(logfile.path());
  std::string line;
  std::uint64_t critical = 0;
  while (Line(in, line))
    critical += line.find("CRITICAL WARNING:") != std::string::npos;
  Check(result.operation.ok() && !critical,
        "tool " + script +
            " failed: exit=" + std::to_string(result.return_code) +
            ", critical_warnings=" + std::to_string(critical) + "\n" +
            result.operation.message + "; log=" + logfile.path());
  sources.push_back(logfile);
  std::vector<JsonRecord> command;
  for (const auto &arg : request.argv)
    command.emplace_back(arg);
  Doc tool_result(ToolResultJson(result));
  return Doc::Encode({{"status", JsonQuote("pass")},
                      {"engine_metrics", tool_result.Token("engine_metrics")},
                      {"command", JsonRecord(command).Encode()},
                      {"log", JsonQuote(logfile.path())},
                      {"log_sha256", JsonQuote(Hash(logfile, cancelled))},
                      {"runtime_seconds", JsonNumber(result.wall_seconds)},
                      {"critical_warnings", "0"}});
}
} // namespace
StageResult RunVivadoPartition(const ArtifactRef &request,
                               const std::string &output,
                               const volatile std::sig_atomic_t *cancelled,
                               std::vector<ArtifactRef> *accepted_artifacts) {
  if (accepted_artifacts)
    accepted_artifacts->clear();
  return RunStage(
      "physical.run-vivado-partition",
      [&] { return std::vector<ArtifactRef>{request}; },
      [&](const auto &initial) {
        Stop(cancelled);
        Doc config(Doc::Read(request));
        config.Keys({"schema", "sources", "fpga", "part", "mapped_report",
                     "runtime", "virtual_io_contract", "place_directive",
                     "route_directive", "threads"});
        Check(config.Text("schema") == "emuflow.vivado-partition-request/v1",
              "request schema disagrees");
        const auto fpga = config.Text("fpga"), part = config.Text("part");
        Check(!fpga.empty() &&
                  std::regex_match(part, std::regex("xc[a-z0-9-]+")),
              "invalid FPGA/part");
        Doc source_list(config.Token("sources"));
        Sources owners;
        std::vector<ArtifactRef> sources = initial;
        const std::map<std::string, std::string> schemas{
            {"ir", "emuir-compact-v2"},
            {"rtl", "exact-sha256-text-v1"},
            {"mapped_index", "xilinx-mapped-index-v2"},
            {"functions", "vivado-source-functions-v1"},
            {"tool", "external-tool-executable-v1"},
            {"boundary_identity", "boundary-identity-selector-v1"},
            {"logic_identity", "logic-segment-identity-selector-v1"},
            {"local_identity", "local-path-timing-selector-v1"},
            {"logic_query", "vivado-timing-query-tsv-v1"},
            {"local_query", "vivado-timing-query-tsv-v1"},
            {"io_adapter", "exact-sha256-text-v1"},
            {"io_xdc", "exact-sha256-text-v1"}};
        for (const auto &item : source_list.Fields()) {
          Doc bound(item.second);
          bound.Keys({"path", "generation"});
          const auto schema = schemas.find(item.first);
          Check(schema != schemas.end() ||
                    item.first.compare(0, 7, "script.") == 0,
                "unknown source owner");
          const fs::path path(bound.Text("path"));
          Check(path.is_absolute() && !fs::is_symlink(path),
                "source must be absolute and regular");
          auto source = ArtifactRef::Bind(
              path.string(), schema == schemas.end() ? "exact-sha256-text-v1"
                                                     : schema->second);
          Generation(bound, source);
          owners.emplace(item.first, source);
          sources.push_back(source);
        }
        for (const auto *key :
             {"ir", "rtl", "mapped_index", "functions", "tool",
              "script.implement_indexed_partition.tcl",
              "script.export_boundary_timing.tcl",
              "script.export_logic_segment_timing.tcl",
              "script.source_inventory.tcl", "script.resource_inventory.tcl",
              "script.timing_contract.tcl"})
          Check(owners.count(key),
                "missing required source " + std::string(key));
        for (const auto *name : {"logic", "local"})
          Check(bool(owners.count(std::string(name) + "_identity")) ==
                    bool(owners.count(std::string(name) + "_query")),
                "timing requires paired identity and query");
        EmuirView source_ir(owners.at("ir"));
        auto design_row = source_ir.store().database().Prepare(
            "SELECT value FROM metadata WHERE key='design_json'");
        Check(design_row.Step(), "source design namespace absent");
        Doc source_design(design_row.TypedText(0));
        for (const auto &item : std::vector<std::pair<std::string, unsigned>>{
                 {"boundary_identity", 2},
                 {"logic_identity", 3},
                 {"local_identity", 4}})
          if (owners.count(item.first)) {
            auto identity = BindPhysicalIdentity(owners.at(item.first),
                                                 item.second, sources);
            SqliteStore store(identity);
            auto row = store.database().Prepare(
                "SELECT value FROM metadata WHERE key='header'");
            Check(row.Step(), "identity header absent");
            Doc header(row.TypedText(0));
            Check(header.Text("fpga") == fpga,
                  "identity FPGA differs from partition");
            Check(source_design.Text("name") ==
                      header.Text("design") + "__" + fpga,
                  "identity design differs from physical source");
          }
        Doc mapped(config.Token("mapped_report")),
            runtime(config.Token("runtime"));
        Check(mapped.Text("optimization_policy") ==
                  "vivado-optimized-source-lineage-v1",
              "unsupported optimization policy");
        const auto original = Natural(mapped.Token("original_cells")),
                   transport = Natural(mapped.Token("transport_cells")),
                   expected = original + transport;
        Check(Natural(mapped.Token("emitted_instances")) == expected,
              "mapped source count disagrees");
        Check(Hash(owners.at("mapped_index"), cancelled) ==
                  mapped.Text("mapped_sha256"),
              "mapped source seal changed");
        Check(Hash(owners.at("rtl"), cancelled) == mapped.Text("rtl_sha256"),
              "mapped RTL source seal changed");
        Check(Hash(owners.at("functions"), cancelled) ==
                  mapped.Text("functions_sha256"),
              "source function seal changed");
        if (runtime.Has("resource_budget")) {
          Doc budget(runtime.Token("resource_budget"));
          Check(budget.Text("id") == fpga && budget.Text("part") == part,
                "device budget belongs to another partition");
        }
        Check(config.Integer("threads") > 0 &&
                  config.Integer("threads") <= 4096,
              "invalid thread count");
        OutputOwner output_owner{fs::path(output)};
        const auto root = output_owner.attempt;
        fs::create_directory(root / ".tmp");
        Workspace work(root);
        const auto input_request = work.root / "inputs.json";
        Write(
            input_request,
            Doc::Encode(
                {{"schema", JsonQuote("emuflow.vivado-input-request/v1")},
                 {"source", Stamp(owners.at("ir"))},
                 {"runtime", runtime.text()},
                 {"virtual_io_contract", config.Token("virtual_io_contract")}}),
            sources);
        Need(PrepareVivadoInputs(ArtifactRef::Bind(input_request.string(),
                                                   "vivado-input-request-v1"),
                                 (root / "inputs").string(), cancelled));
        Doc inputs = Read(root / "inputs/summary.json");
        Check(Natural(inputs.Token("instances")) == expected,
              "mapped cell accounting disagrees");
        for (const auto *file :
             {"runtime.xdc", "physical_top.sv", "fixed-io-query.tsv",
              "fixed-io-timing-map.tsv"})
          if (fs::exists(root / "inputs" / file))
            sources.push_back(ArtifactRef::Bind(
                (root / "inputs" / file).string(), "exact-sha256-text-v1"));
        const bool fixed = inputs.Has("fixed_io");
        Check(!fixed || (owners.count("io_adapter") && owners.count("io_xdc")),
              "fixed I/O requires source generations");
        Doc io(fixed ? inputs.Token("fixed_io") : "{}");
        if (fixed)
          Check(owners.at("io_adapter").path() == io.Text("adapter") &&
                    owners.at("io_xdc").path() == io.Text("xdc"),
                "fixed I/O source owners disagree");
        const auto prefix = inputs.Text("hierarchy_prefix"),
                   top = inputs.Text("top");
        const auto get = [&](const char *key) {
          return fixed ? io.Text(key) : "";
        };
        std::vector<std::string> implementation_args = {
            part,
            owners.at("rtl").path(),
            top,
            inputs.Text("runtime_xdc"),
            root.string(),
            std::to_string(expected),
            prefix,
            get("adapter"),
            get("wrapper"),
            get("xdc"),
            get("query"),
            config.Text("place_directive"),
            config.Text("route_directive"),
            std::to_string(inputs.Integer("dut_clock_count")),
            std::to_string(config.Integer("threads"))};
        const auto implementation =
            Execute(owners, implementation_args, root,
                    "implement_indexed_partition.tcl",
                    "vivado-implementation.log", sources, cancelled);
        auto metrics = Metrics(root / "implementation_metrics.tsv");
        Check(TextAt(metrics, "part") == part &&
                  Integer(metrics, "dut_clock_count") ==
                      inputs.Integer("dut_clock_count"),
              "tool part/clock identity disagrees");
        const auto lineage_request = work.root / "lineage.json",
                   lineage_path = root / "cell-lineage.sqlite",
                   lineage_summary = work.root / "lineage-summary.json";
        Write(lineage_request,
              Doc::Encode(
                  {{"schema", JsonQuote("emuflow.vivado-lineage-import/v1")},
                   {"mapped_sha256", mapped.Token("mapped_sha256")},
                   {"retain_stage", JsonQuote("routed")},
                   {"stages", "[\"synthesized\",\"routed\"]"}}),
              sources);
        ImportVivadoLineage(
            {owners.at("mapped_index"), owners.at("functions"),
             ArtifactRef::Bind(lineage_request.string(),
                               "vivado-lineage-import-request-v1"),
             ArtifactRef::Bind((root / "mapped_cells.tsv").string(),
                               "vivado-lineage-inventory-v2"),
             ArtifactRef::Bind((root / "routed_mapped_cells.tsv").string(),
                               "vivado-lineage-inventory-v2")},
            lineage_path.string(), lineage_summary.string());
        Doc coverage = Read(lineage_summary);
        Doc stages(coverage.Token("stages")), routed(stages.Token("routed"));
        Check(routed.Integer("placed_objects") ==
                  Integer(metrics, "routed_cells"),
              "lineage placement coverage disagrees");
        fs::remove(root / "mapped_cells.tsv");
        fs::remove(root / "routed_mapped_cells.tsv");
        auto lineage =
            ArtifactRef::Bind(lineage_path.string(), "vivado-lineage-store-v2");
        sources.push_back(lineage);
        auto aggregate = Timing(root / "timing_summary.rpt");
        Check(std::abs(
                  ParseFiniteDecimal(aggregate.at("wns_ns"), "aggregate WNS") -
                  Number(metrics, "wns_ns")) <= 0.002,
              "timing aggregate and worst path disagree");
        RouteStatus(root / "route_status.rpt");
        Raw counts;
        for (const auto *key : {"lut", "ff", "carry8", "dsp48", "uram288"})
          counts[key] = std::to_string(Integer(metrics, key));
        counts["bram18k"] =
            std::to_string(Integer(metrics, "ramb18_cells") +
                           2 * Integer(metrics, "ramb36_cells"));
        const auto usage = Usage(counts, runtime);
        Raw timing_stages;
        std::vector<std::string> timing_args = {prefix};
        if (fixed)
          timing_args.push_back(io.Text("timing_map"));
        for (const auto &item : std::vector<std::pair<std::string, unsigned>>{
                 {"boundary", 2}, {"logic", 3}, {"local", 4}}) {
          const auto &name = item.first;
          const auto kind = item.second;
          if (!owners.count(name + "_identity"))
            continue;
          Stop(cancelled);
          const auto label = kind == 2   ? "boundary_timing"
                             : kind == 3 ? "logic_segment_timing"
                                         : "local_path_timing";
          Raw details{{"status", JsonQuote("pass")}};
          ArtifactRef query = owners.at(name + "_identity");
          if (kind == 2) {
            auto identity = BindPhysicalIdentity(owners.at("boundary_identity"),
                                                 2, sources);
            const auto report = Query(
                {owners.at("ir"), identity, owners.at("boundary_identity")},
                work.root / "boundary-query.bin", root / "boundary-query",
                false, true, cancelled);
            details["query"] = report;
            Doc doc(report);
            query = ArtifactRef::Bind(doc.Text("output"),
                                      "vivado-timing-query-tsv-v1");
          } else
            query = owners.at(name + "_query");
          const auto resolution = Query(
              {owners.at("mapped_index"), owners.at("functions"), lineage,
               query},
              work.root / (name + "-resolve.bin"),
              root / (name + "-query-resolved"), true, kind == 2, cancelled);
          details["resolution"] = resolution;
          Doc resolved(resolution);
          sources.push_back(ArtifactRef::Bind(resolved.Text("output"),
                                              "vivado-timing-query-tsv-v1"));
          sources.push_back(ArtifactRef::Bind((root / "routed.dcp").string(),
                                              "exact-sha256-text-v1"));
          const auto measured = root / (std::string(label) + ".tsv");
          std::vector<std::string> args = {(root / "routed.dcp").string(),
                                           resolved.Text("output"),
                                           measured.string()};
          args.insert(args.end(), timing_args.begin(), timing_args.end());
          details["export"] =
              Execute(owners, args, root,
                      kind == 2 ? "export_boundary_timing.tcl"
                                : "export_logic_segment_timing.tcl",
                      name + "-export.log", sources, cancelled);
          details["import"] = ImportVivadoMeasurement(
              owners.at(name + "_identity"),
              ArtifactRef::Bind(measured.string(),
                                "physical-timing-measurements-v1"),
              kind, (root / (std::string(label) + ".json")).string(), fixed,
              sources);
          timing_stages[label] = Doc::Encode(details);
        }
        Raw artifacts;
        for (const auto *name :
             {"synthesized.dcp", "placed.dcp", "routed.dcp", "route_status.rpt",
              "drc.rpt", "timing_summary.rpt", "utilization.rpt",
              "implementation_metrics.tsv"}) {
          auto artifact =
              ArtifactRef::Bind((root / name).string(), "exact-sha256-text-v1");
          Check(artifact.generation().bytes > 0,
                "missing/empty physical artifact");
          artifacts[name] = Doc::Encode(
              {{"path", JsonQuote(artifact.path())},
               {"sha256", JsonQuote(Hash(artifact, cancelled))},
               {"bytes", std::to_string(artifact.generation().bytes)}});
          sources.push_back(artifact);
        }
        Raw account{{"original_cells", std::to_string(original)},
                    {"transport_cells", std::to_string(transport)}};
        for (const auto *key :
             {"source_cells_with_lineage", "unretained_source_cells",
              "source_cells_with_multiple_objects"})
          account[key] = routed.Token(key);
        for (const auto *key : {"routed_cells", "physical_cells",
                                "infrastructure_cells", "optimization_cells"})
          account[key] = std::to_string(Integer(metrics, key));
        Raw closure;
        for (const auto *key :
             {"unrouted_nets", "drc_violations", "drc_warnings"})
          closure[key] = std::to_string(Integer(metrics, key));
        Raw delays;
        for (const auto &item :
             std::vector<std::pair<std::string, std::string>>{
                 {"overall", "critical_path_ns"},
                 {"dut", "dut_delay_ns"},
                 {"fabric", "fabric_delay_ns"},
                 {"cross", "fabric_to_dut_delay_ns"}}) {
          const auto n = Number(metrics, item.second);
          Check(n >= 0, "negative clock-domain delay");
          delays[item.first] = JsonNumber(n);
        }
        Raw timing = aggregate;
        timing["timing_met"] =
            ParseFiniteDecimal(aggregate.at("wns_ns"), "WNS") >= 0 ? "true"
                                                                   : "false";
        for (const auto *key : {"dut_wns_ns", "fabric_wns_ns",
                                "fabric_to_dut_wns_ns", "critical_path_ns"})
          timing[key] = JsonNumber(Number(metrics, key));
        timing["clock_domain_delays_ns"] = Doc::Encode(delays);
        Doc dut_clock(runtime.Token("virtual_dut_clock"));
        const Raw result{
            {"schema", JsonQuote("emuflow.physical-partition-result/v1")},
            {"status", JsonQuote("pass")},
            {"identity", Doc::Encode({{"backend", JsonQuote("vivado")},
                                      {"fpga", JsonQuote(fpga)},
                                      {"part", JsonQuote(part)}})},
            {"analysis_scope",
             "{\"implementation_mode\":\"out-of-context\",\"data_path_delay\":"
             "\"routed-vendor-device\",\"external_clock_sources\":\"abstract-"
             "unbound\",\"absolute_clock_insertion_and_skew\":\"not-"
             "qualified\",\"cell_accounting_policy\":\"vivado-optimized-source-"
             "lineage-v1\",\"board_signoff\":false}"},
            {"cell_accounting", Doc::Encode(account)},
            {"closure", Doc::Encode(closure)},
            {"clocks",
             Doc::Encode({{"fabric_period_ns",
                           JsonNumber(Number(metrics, "fabric_period_ns"))},
                          {"dut_period_ns", JsonNumber(dut_clock.Number(
                                                "nominal_period_ns"))}})},
            {"timing", Doc::Encode(timing)},
            {"resource_usage", usage},
            {"hard_resources", Doc::Encode(counts)},
            {"artifacts", Doc::Encode(artifacts)}};
        Raw report{
            {"schema", JsonQuote("emuflow.vivado-partition-report/v1")},
            {"status", JsonQuote("pass")},
            {"provider", JsonQuote("indexed-phase6-vivado-physical-v1")},
            {"implementation_id", JsonQuote("native-vivado-partition-v1")},
            {"tool",
             Doc::Encode(
                 {{"executable", JsonQuote(owners.at("tool").path())},
                  {"version", JsonQuote(TextAt(metrics, "vivado_version"))},
                  {"threads", config.Token("threads")}})},
            {"implementation", implementation},
            {"cell_coverage", coverage.text()},
            {"result", Doc::Encode(result)},
            {"validation",
             ValidatePhysicalPartitionReport(Doc::Encode(result), "vivado",
                                             fpga, part, original, transport)}};
        for (const auto &item : timing_stages)
          report[item.first] = item.second;
        if (fixed) {
          const auto pairs = Integer(metrics, "virtual_io_pairs");
          Doc contract(config.Token("virtual_io_contract")),
              expected_metrics(contract.Token("metrics"));
          Check(pairs == expected_metrics.Integer("differential_pairs"),
                "fixed differential pair count disagrees");
          report["virtual_io_implementation"] = Doc::Encode(
              {{"status", JsonQuote("pass")},
               {"provider", JsonQuote("vivado-fixed-lvds-routed-boundary-v1")},
               {"qualification", JsonQuote("routed-IBUFDS-OBUFDS-with-fixed-P-"
                                           "N-package-pins-and-IOSTANDARD")},
               {"contract_sha256", JsonQuote(StringHash(contract.text()))},
               {"differential_pairs", std::to_string(pairs)},
               {"physical_ports", std::to_string(2 * pairs)},
               {"top", io.Token("top")},
               {"route_validation",
                Doc::Encode(
                    {{"unrouted_nets", closure.at("unrouted_nets")}})}});
        }
        for (const auto &source : sources)
          source.RequireUnchanged();
        Stop(cancelled);
        Write(output_owner.root / "vivado-partition-report.json",
              Doc::Encode(report), sources);
        fs::remove_all(root / ".tmp");
        if (accepted_artifacts)
          for (const auto &ref : sources) {
            const auto relative = fs::path(ref.path()).lexically_relative(root);
            if (!relative.empty() && *relative.begin() != ".." &&
                ref.path().find(work.root.string() + "/") != 0 &&
                ref.path().find((root / ".tmp").string() + "/") != 0)
              accepted_artifacts->push_back(ref);
          }
      },
      [&] {
        return std::vector<ArtifactRef>{ArtifactRef::Bind(
            (fs::path(output) / "vivado-partition-report.json").string(),
            "vivado-partition-report-v1")};
      });
}
} // namespace emuflow::core
