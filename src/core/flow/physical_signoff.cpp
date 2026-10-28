#include "flow/signoff.hpp"

#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "physical/board_timing.hpp"
#include "physical/evidence_catalog.hpp"
#include "physical/physical_coverage.hpp"
#include "scheduling/phase5.hpp"
#include "storage/store.hpp"
#include "timing/global_events.hpp"
#include "timing/physical_global_sta.hpp"
#include "transport/runtime_calendar.hpp"

#include <array>
#include <filesystem>
#include <fstream>
#include <map>
#include <regex>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Raw = std::map<std::string, std::string>;
using Object = std::map<std::string, JsonRecord>;
using Array = std::vector<JsonRecord>;
void PCheck(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact, "physical Phase 7C: " + why);
}
struct PWorkspace {
  fs::path root;
  explicit PWorkspace(const fs::path &p) {
    auto n = (p / ".physical-phase7c-XXXXXX").string();
    PCheck(mkdtemp(n.data()), "cannot create workspace");
    root = n;
  }
  ~PWorkspace() {
    if (!root.empty()) {
      std::error_code e;
      fs::remove_all(root, e);
    }
  }
};
ArtifactRef PBind(const fs::path &path,
                  const std::string &schema = "exact-sha256-text-v1") {
  PCheck(path.is_absolute() && !fs::is_symlink(path) &&
             fs::is_regular_file(path),
         "source is unavailable: " + path.string());
  return ArtifactRef::Bind(path.string(), schema);
}
BoundedJson PRead(const ArtifactRef &source,
                  std::size_t maximum = 8 * 1024 * 1024) {
  return BoundedJson(BoundedJson::Read(source, maximum), maximum);
}
Raw PFields(const std::string &value) { return BoundedJson(value).Fields(); }
std::string PGet(const Raw &value, const std::string &key) {
  auto i = value.find(key);
  PCheck(i != value.end(), "missing field " + key);
  return i->second;
}
std::string PText(const Raw &value, const std::string &key) {
  return DecodeJsonString(PGet(value, key));
}
std::int64_t PInteger(const Raw &value, const std::string &key) {
  return BoundedJson::IntegerToken(PGet(value, key));
}
double PNumber(const Raw &value, const std::string &key) {
  return ParseFiniteDecimal(PGet(value, key), "physical Phase 7C " + key);
}
std::string PObject(const Raw &value) { return BoundedJson::Encode(value); }
std::string Hex(const std::array<unsigned char, 32> &value) {
  static constexpr char d[] = "0123456789abcdef";
  std::string out;
  for (auto b : value) {
    out += d[b >> 4];
    out += d[b & 15];
  }
  return out;
}
std::string PHash(const ArtifactRef &source) {
  std::ifstream in(source.path(), std::ios::binary);
  PCheck(bool(in), "cannot hash source");
  Sha256 h;
  std::array<char, 65536> b{};
  while (in) {
    in.read(b.data(), b.size());
    h.update(reinterpret_cast<unsigned char *>(b.data()), in.gcount());
  }
  PCheck(in.eof(), "cannot finish source hash");
  source.RequireUnchanged();
  return Hex(h.finish());
}
std::string PMeta(const ArtifactRef &source, const std::string &key) {
  SqliteStore store(source);
  auto q = store.database().Prepare("SELECT value FROM metadata WHERE key=?");
  q.BindText(1, key);
  PCheck(q.Step(), "missing metadata " + key);
  return q.TypedText(0);
}
ArtifactRef PStore(const fs::path &selector_path,
                   const std::string &selector_schema,
                   const std::string &store_schema) {
  auto selector = PBind(selector_path, selector_schema);
  auto doc = PRead(selector);
  const auto artifact = doc.Text("artifact");
  PCheck(fs::path(artifact).filename() == artifact && artifact != "." &&
             artifact != "..",
         "selector payload is unsafe");
  auto store = PBind(selector_path.parent_path() / artifact, store_schema);
  PCheck(doc.Integer("store_bytes") >= 0 &&
             std::uint64_t(doc.Integer("store_bytes")) ==
                 store.generation().bytes &&
             doc.Text("store_sha256") == PHash(store),
         "selector payload seal disagrees");
  return store;
}
void PPass(BoundedJson &report, const std::string &owner,
           const std::string &design, const std::string &platform) {
  PCheck(report.Text("status") == "pass" && report.Text("design") == design &&
             report.Text("platform") == platform,
         owner + " namespace/status disagrees");
  auto validation = BoundedJson(report.Token("validation"));
  PCheck(validation.Text("status") == "pass", owner + " validation failed");
}
void PWrite(const fs::path &path, const std::string &value) {
  PublishTextArtifact(path.string(), {},
                      [&](std::ostream &out) { out << value << '\n'; });
}
std::string Canonical(const std::string &token, bool ascii = false) {
  if (!token.empty() && token[0] == '{') {
    Raw fields = PFields(token);
    std::string result = "{";
    for (const auto &entry : fields) {
      if (result.size() > 1)
        result += ',';
      result += QuoteJsonString(entry.first, ascii) + ":" +
                Canonical(entry.second, ascii);
    }
    return result + "}";
  }
  if (!token.empty() && token[0] == '[') {
    std::string result = "[";
    for (const auto &item : BoundedJson::Array(token)) {
      if (result.size() > 1)
        result += ',';
      result += Canonical(item, ascii);
    }
    return result + "]";
  }
  if (!token.empty() && token[0] == '\"')
    return QuoteJsonString(DecodeJsonString(token), ascii);
  return token;
}
std::string ContractHash(const std::string &token) {
  Sha256 hash;
  VisitPrettyCanonicalJson(
      Canonical(token, true), 0, [&](std::string_view text) {
        hash.update(reinterpret_cast<const unsigned char *>(text.data()),
                    text.size());
      });
  hash.update(reinterpret_cast<const unsigned char *>("\n"), 1);
  return Hex(hash.finish());
}
std::string Coverage(const ArtifactRef &contract, const ArtifactRef &selector,
                     const ArtifactRef &schedule, const ArtifactRef &assignment,
                     const PhysicalEvidenceCatalog &physical,
                     const PhysicalBoardTiming &board, const fs::path &stage,
                     std::vector<ArtifactRef> &sources) {
  auto timing = PRead(contract), selected = PRead(selector);
  const auto descriptor = PMeta(assignment, "semantic_contract_binary");
  BoundedJson exact_doc(descriptor);
  const auto name = exact_doc.Text("artifact");
  PCheck(fs::path(name).filename() == name && name != "." && name != "..",
         "exact binary path is unsafe");
  auto exact = PBind(fs::path(assignment.path()).parent_path() / name,
                     "phase3-exact-data-v2");
  std::vector<ArtifactRef> inputs{selector, schedule, assignment, exact};
  std::vector<std::string> fpgas;
  auto add = [&](const ArtifactRef &source) {
    const auto index = inputs.size();
    inputs.push_back(source);
    return std::to_string(index);
  };
  std::string records = "[";
  for (std::size_t i = 0; i < physical.boundary_timing.size(); ++i) {
    if (i)
      records += ',';
    const auto &timing = physical.boundary_timing[i];
    const auto &identity = physical.boundary_identities[i];
    const auto &logic = physical.logic_segment_timing[i];
    add(identity.selector);
    const auto id = add(identity.store);
    add(timing.selector);
    const auto t = add(timing.store);
    add(logic.selector);
    const auto l = add(logic.store);
    records += PObject({{"fpga", JsonQuote(timing.fpga)},
                        {"identity", id},
                        {"timing", t},
                        {"logic", l}});
  }
  records += ']';
  const auto config = PObject(
      {{"schema", JsonQuote("emuflow.physical-coverage-request/v1")},
       {"contract", timing.text()},
       {"contract_sha256", JsonQuote(ContractHash(timing.text()))},
       {"schedule_sha256", selected.Token("logical_pretty_sha256")},
       {"schedule_logical_sha256", selected.Token("logical_sha256")},
       {"schedule_sections", selected.Token("sections")},
       {"schedule", "1"},
       {"assignment", "2"},
       {"exact", "3"},
       {"fixture", "false"},
       {"physical_source",
        PObject({{"provider", JsonQuote(physical.provider)},
                 {"qualification", JsonQuote(physical.qualification)}})},
       {"fpgas", records},
       {"board_links", board.coverage_links},
       {"board_signoff", board.final_signoff ? "true" : "false"}});
  const auto request = [&](const std::vector<ArtifactRef> &refs,
                           const fs::path &path) {
    CheckedRequestWriter out(path.string());
    out.Put<std::uint64_t>(0x31564350464d5545ULL);
    out.Put<std::uint32_t>(1);
    out.Put<std::uint32_t>(refs.size());
    for (const auto &source : refs)
      out.Text(source.schema());
    for (const auto &source : refs)
      out.Generation(source.generation());
    out.Text(config);
    out.Finish();
    return PBind(path, "physical-coverage-request-v1");
  };
  auto producer = inputs;
  producer.push_back(request(inputs, stage / "coverage-request.bin"));
  const auto store_path = stage / "coverage.sqlite";
  BuildPhysicalCoverage(producer, store_path.string());
  auto store = PBind(store_path, "physical-coverage-store-v1");
  auto audit = inputs;
  audit.push_back(store);
  audit.push_back(request(audit, stage / "coverage-audit.bin"));
  AuditPhysicalCoverage(audit, (stage / "coverage-audit.json").string());
  const auto header = PMeta(store, "header");
  auto observed = PRead(PBind(stage / "coverage-audit.json"));
  PCheck(Canonical(header) == Canonical(observed.text()),
         "independent coverage audit disagrees");
  PWrite(stage / "cross_layer_physical_binding.json",
         PObject({{"schema",
                   JsonQuote(
                       "emuflow.cross-layer-physical-binding-certificate/v1")},
                  {"header", header},
                  {"sections", PMeta(store, "sections")},
                  {"sources", PMeta(store, "sources")},
                  {"logical_sha256", PMeta(store, "logical_sha256")}}));
  sources.insert(sources.end(), inputs.begin(), inputs.end());
  BoundedJson result(header);
  auto metrics = PFields(result.Token("metrics"));
  metrics["status"] = result.Token("status");
  for (const auto &name : {"coverage-request.bin", "coverage-audit.bin",
                           "coverage-audit.json", "coverage.sqlite"})
    fs::remove(stage / name);
  return PObject(metrics);
}
std::string Timing(const PhysicalGlobalStaResult &run, const Raw &runtime) {
  BoundedJson measurement(run.measurement_header),
      reduction(measurement.Token("reduction")), binding(run.event_binding),
      index(run.index_header);
  const auto source = binding.Token("source_binding");
  PCheck(source != "null",
         "physical timing lacks original-path source binding");
  BoundedJson origin(source);
  const auto paths = reduction.Integer("paths"),
             local = reduction.Integer("local_paths"),
             bounds = reduction.Integer("bounds_paths"),
             failures = reduction.Integer("event_failures"),
             checks = reduction.Integer("checks");
  PCheck(origin.Integer("original_paths") == paths &&
             origin.Text("original_path_ids_sha256") ==
                 index.Text("path_ids_sha256"),
         "original path population/identity coverage disagrees");
  const auto maximum = reduction.Number("maximum_delay_ns");
  BoundedJson target(reduction.Token("target")),
      measured(reduction.Token("runtime"));
  const auto status =
      failures || measured.Number("wns_ns") < 0 ? "fail" : "pass";
  Raw periods;
  double fastest = 0;
  for (const auto &clock : PFields(PGet(runtime, "virtual_dut_clocks"))) {
    BoundedJson c(clock.second);
    const auto p = c.Number("nominal_period_ns");
    periods.emplace(clock.first, JsonRecord(p).Encode());
    if (!fastest || p < fastest)
      fastest = p;
  }
  const auto metric = [](BoundedJson &value) {
    return PObject(
        {{"wns_ns", value.Token("wns_ns")},
         {"original_path_tns_ns", value.Token("original_path_tns_ns")},
         {"negative_paths", value.Token("negative_paths")}});
  };
  const auto clock = [](BoundedJson &value, bool closure) {
    return PObject(
        {{"closure_gate", closure ? "true" : "false"},
         {"worst_path", value.Token("worst_path")},
         {"worst_slack_bound_ns", value.Token("wns_ns")},
         {"tns_bound_ns", value.Token("original_path_tns_ns")},
         {"total_negative_slack_bound_ns", value.Token("original_path_tns_ns")},
         {"negative_slack_paths", value.Token("negative_paths")}});
  };
  auto runtime_clock = PFields(clock(measured, true));
  runtime_clock.emplace("period_ns", JsonRecord(fastest).Encode());
  runtime_clock.emplace("frequency_mhz", JsonRecord(1000.0 / fastest).Encode());
  runtime_clock.emplace("period_scope", JsonQuote("fastest-domain-summary"));
  runtime_clock.emplace("clock_periods_ns", PObject(periods));
  runtime_clock.emplace("minimum_safe_period_bound_ns",
                        JsonRecord(maximum).Encode());
  runtime_clock.emplace("maximum_safe_frequency_bound_mhz",
                        maximum > 0 ? JsonRecord(1000.0 / maximum).Encode()
                                    : "null");
  return PObject(
      {{"schema", JsonQuote("emuflow.system-timing/v2")},
       {"status", JsonQuote(status)},
       {"qualification", JsonQuote("opensta-fixed-event-physical-bounds")},
       {"timing_scope", JsonQuote("whole-original-design")},
       {"source_binding", source},
       {"path_exactness",
        PObject(
            {{"physical_model", JsonQuote("routed-staging-chain-upper-bounds")},
             {"endpoint_exact_logic_paths", std::to_string(paths - bounds)},
             {"cone_bound_logic_paths", std::to_string(bounds)},
             {"fallback_logic_paths", "0"},
             {"discontinuous_compressed_paths", "0"},
             {"physical_logic_segment_bounds", "true"},
             {"fixed_slot_event_failures", std::to_string(failures)}})},
       {"summary",
        PObject(
            {{"timing_paths", std::to_string(paths)},
             {"original_paths", origin.Token("original_paths")},
             {"original_local_paths", std::to_string(local)},
             {"original_cross_fpga_paths", std::to_string(paths - local)},
             {"compressed_representative_paths",
              binding.Token("compressed_representative_paths")},
             {"original_path_coverage", "1.0"},
             {"original_path_ids_sha256",
              origin.Token("original_path_ids_sha256")},
             {"maximum_system_delay_bound_ns", JsonRecord(maximum).Encode()}})},
       {"target_clock", clock(target, false)},
       {"runtime_clock", PObject(runtime_clock)},
       {"global_opensta",
        PObject(
            {{"schema", JsonQuote("emuflow.global-opensta-check/v1")},
             {"authority", JsonQuote("opensta")},
             {"execution", JsonQuote("standalone")},
             {"status", JsonQuote(status)},
             {"timing_scope", JsonQuote("whole-original-design")},
             {"original_paths", std::to_string(paths)},
             {"checks", std::to_string(checks)},
             {"event_failures", std::to_string(failures)},
             {"metrics", PObject({{"target", metric(target)},
                                  {"runtime", metric(measured)}})},
             {"tns_definition",
              JsonQuote(
                  "sum-negative-slack-once-per-original-TimingPathDB-path")},
             {"implementation_id", JsonQuote("native-global-measurements-v1")},
             {"measurement_logical_sha256",
              reduction.Token("measurements_sha256")}})}});
}
void Generation(CheckedRequestWriter &request, const ArtifactRef &source) {
  request.Generation(source.generation());
}
std::string EngineIdentity(const PhysicalGlobalStaResult &global,
                           const ArtifactRef &executable) {
  std::ifstream input(global.opensta_log);
  std::string line;
  const std::regex banner("^OpenSTA[ \\t]+([^ \\t]+)[ \\t]+([^ \\t]+).*$");
  std::smatch match;
  for (int i = 0; i < 32 && std::getline(input, line); ++i)
    if (std::regex_match(line, match, banner)) {
      const auto version = match[1].str(), revision = match[2].str(),
                 sha = global.executable_sha256;
      Raw identity{{"name", JsonQuote("OpenSTA")},
                   {"version", JsonQuote(version)},
                   {"executable_sha256", JsonQuote(sha)}};
      std::smatch semantic;
      PCheck(
          std::regex_match(version, semantic,
                           std::regex("([0-9]+)\\.([0-9]+)\\.([0-9]+)")) &&
              (std::stoull(semantic[1]) > 3 || (std::stoull(semantic[1]) == 3 &&
                                                std::stoull(semantic[2]) >= 1)),
          "OpenSTA 3.1.0 or newer is required");
      if (!global.launcher_sha256.empty())
        identity.emplace("launcher_sha256", JsonQuote(global.launcher_sha256));
      if (std::regex_match(revision, std::regex("[0-9a-f]{7,40}")))
        identity.emplace("revision", JsonQuote(revision));
      else {
        identity.emplace("revision", JsonQuote("binary-sha256:" + sha));
        identity.emplace("reported_revision", JsonQuote(revision));
      }
      return PObject(identity);
    }
  throw StageError(
      ErrorCode::kInvalidArtifact,
      "physical Phase 7C: OpenSTA log lacks engine version/revision");
}
std::string PhysicalMetrics(BoundedJson &physical, BoundedJson &board,
                            const Raw &runtime) {
  std::set<std::string> expected, seen;
  for (const auto &token : BoundedJson::Array(board.Token("fpgas")))
    expected.insert(BoundedJson(token).Text("id"));
  std::map<std::string, std::int64_t> totals;
  std::map<std::string, double> worst;
  double tns = 0;
  std::int64_t active = 0;
  const auto fabric =
                 PNumber(PFields(PGet(runtime, "fabric_clock")), "period_ns"),
             dut = PNumber(PFields(PGet(runtime, "virtual_dut_clock")),
                           "nominal_period_ns");
  const auto count = [](BoundedJson &doc, const std::string &name) {
    const auto value = doc.Integer(name);
    PCheck(value >= 0, "negative physical count " + name);
    return value;
  };
  for (const auto &token : BoundedJson::Array(physical.Token("fpgas"))) {
    BoundedJson item(token);
    const auto fpga = item.Text("fpga");
    PCheck(expected.count(fpga) && seen.insert(fpga).second,
           "physical FPGA coverage disagrees");
    const bool enabled = !item.Has("active") || item.Boolean("active");
    Raw accounting;
    for (const auto *field :
         {"original_cells", "transport_cells", "routed_cells", "physical_cells",
          "infrastructure_cells", "unrouted_nets", "drc_violations"})
      accounting[field] = std::to_string(count(item, field));
    const auto optimization =
        item.Has("optimization_cells") ? count(item, "optimization_cells") : 0;
    const auto original = PInteger(accounting, "original_cells"),
               transport = PInteger(accounting, "transport_cells"),
               routed = PInteger(accounting, "routed_cells"),
               infra = PInteger(accounting, "infrastructure_cells"),
               objects = PInteger(accounting, "physical_cells");
    PCheck(objects == routed + infra + optimization,
           "physical cell census disagrees");
    PCheck(item.Integer("unrouted_nets") == 0 &&
               item.Integer("drc_violations") == 0,
           "route/DRC did not close");
    BoundedJson scope(item.Has("analysis_scope") ? item.Token("analysis_scope")
                                                 : "{}");
    if (scope.Has("cell_accounting_policy") &&
        scope.Token("cell_accounting_policy") != "null") {
      PCheck(item.Text("backend") == "vivado" &&
                 scope.Text("cell_accounting_policy") ==
                     "vivado-optimized-source-lineage-v1",
             "invalid lineage policy");
      const auto retained = count(item, "source_cells_with_lineage"),
                 missing = count(item, "unretained_source_cells"),
                 multiple = count(item, "source_cells_with_multiple_objects");
      PCheck(retained + missing == original + transport && multiple <= retained,
             "source lineage census disagrees");
    } else
      PCheck(routed == original + transport, "source cell census disagrees");
    BoundedJson clocks(item.Token("clocks")), timing(item.Token("timing"));
    PCheck(std::abs(clocks.Number("fabric_period_ns") - fabric) <= 1e-6 &&
               clocks.Number("dut_period_ns") > 0 &&
               clocks.Number("dut_period_ns") <= dut + 1e-6,
           "physical clock contract disagrees");
    const auto slack = item.Number("wns_ns");
    if (!enabled) {
      PCheck(original + transport + routed + infra + objects + optimization ==
                     0 &&
                 slack == 0 && !timing.Boolean("applicable") &&
                 timing.Boolean("timing_met"),
             "inactive FPGA census disagrees");
      for (const auto *field :
           {"dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns", "tns_ns"})
        PCheck(timing.Number(field) == 0, "inactive FPGA timing disagrees");
      PCheck(count(timing, "failing_endpoints") == 0 &&
                 count(timing, "failing_endpoint_constraints") == 0,
             "inactive FPGA failures disagree");
      BoundedJson presence(item.Token("clock_domain_presence")),
          delays(item.Token("clock_domain_delays_ns"));
      for (const auto *field : {"fabric", "dut", "cross"})
        PCheck(!presence.Boolean(field), "inactive FPGA clock domain present");
      for (const auto *field : {"overall", "fabric", "dut", "cross"})
        PCheck(delays.Number(field) == 0, "inactive FPGA has delay");
      continue;
    }
    ++active;
    for (const auto &field : accounting)
      totals[field.first] += PInteger(accounting, field.first);
    totals["optimization_cells"] += optimization;
    if (timing.Has("timing_met"))
      PCheck(timing.Boolean("timing_met") == bool(slack >= 0),
             "timing closure claim disagrees");
    if (timing.Has("tns_ns")) {
      const auto value = timing.Number("tns_ns");
      PCheck(value <= 0, "positive physical TNS");
      tns += value;
      totals["failing_endpoints"] += count(timing, "failing_endpoints");
      totals["failing_endpoint_constraints"] +=
          count(timing, "failing_endpoint_constraints");
    }
    bool has_dut = true;
    if (item.Has("clock_domain_presence")) {
      BoundedJson presence(item.Token("clock_domain_presence"));
      presence.Keys({"fabric", "dut", "cross"});
      PCheck(presence.Boolean("fabric") &&
                 presence.Boolean("cross") == presence.Boolean("dut"),
             "physical clock domains disagree");
      has_dut = presence.Boolean("dut");
    }
    const auto add = [&](const std::string &field, double value) {
      auto it = worst.find(field);
      if (it == worst.end())
        worst.emplace(field, value);
      else
        it->second = std::min(it->second, value);
    };
    add("worst_wns_ns", slack);
    add("worst_local_backend_wns_ns", slack);
    add("worst_fabric_wns_ns", timing.Number("fabric_wns_ns"));
    const auto dut_slack = timing.Number("dut_wns_ns"),
               cross = timing.Number("fabric_to_dut_wns_ns");
    if (has_dut) {
      add("worst_dut_wns_ns", dut_slack);
      add("worst_fabric_to_dut_wns_ns", cross);
    }
  }
  PCheck(seen == expected && active > 0,
         "physical FPGA population is incomplete");
  Raw result{{"status", JsonQuote("pass")},
             {"scope", JsonQuote("per-fpga-local-physical-closure")},
             {"fpgas", std::to_string(seen.size())},
             {"active_fpgas", std::to_string(active)},
             {"inactive_fpgas", std::to_string(seen.size() - active)},
             {"total_tns_ns", JsonRecord(tns).Encode()},
             {"timing_met", worst.at("worst_wns_ns") >= 0 ? "true" : "false"}};
  for (const auto *field :
       {"original_cells", "transport_cells", "routed_cells", "physical_cells",
        "infrastructure_cells", "optimization_cells", "unrouted_nets",
        "drc_violations", "failing_endpoints", "failing_endpoint_constraints"})
    result[field] = std::to_string(totals[field]);
  for (const auto *field :
       {"worst_wns_ns", "worst_local_backend_wns_ns", "worst_dut_wns_ns",
        "worst_fabric_wns_ns", "worst_fabric_to_dut_wns_ns"})
    result[field] =
        worst.count(field) ? JsonRecord(worst.at(field)).Encode() : "null";
  return PObject(result);
}
} // namespace

void SignoffPhysical(const fs::path &output, const std::string &opensta,
                     const volatile std::sig_atomic_t *cancelled,
                     const std::string &launcher_reference) {
  PCheck(output.is_absolute() && fs::is_directory(output),
         "flow root is unavailable");
  const auto target = output / "runtime";
  PCheck(!fs::exists(target) && !fs::is_symlink(target),
         "runtime output exists");
  std::vector<ArtifactRef> sources;
  auto source = [&](const fs::path &p,
                    const std::string &s =
                        "exact-sha256-text-v1") -> ArtifactRef {
    sources.push_back(PBind(output / p, s));
    return sources.back();
  };
  auto p3r = source("partition/phase3_report.json", "phase3-report-v1");
  auto p3 = PRead(p3r);
  const auto design = p3.Text("design"), platform = p3.Text("platform");
  auto p4r = source("system-route/phase4_report.json", "phase4-report-v1");
  auto p4 = PRead(p4r);
  auto p5r = source("tdm/phase5_report.json", "phase5-report-v1");
  auto p5 = PRead(p5r);
  auto p6r = source("split/phase6_report.json", "phase6-report-v1");
  auto p6 = PRead(p6r);
  PPass(p3, "Phase 3", design, platform);
  PPass(p4, "Phase 4", design, platform);
  PPass(p5, "Phase 5", design, platform);
  PPass(p6, "Phase 6", design, platform);
  auto schedule = PStore(output / "tdm/schedule.json",
                         "tdm-schedule-selector-v4", "tdm-schedule-store-v4");
  auto routes = PStore(output / "system-route/routes.json",
                       "system-route-selector-v1", "system-route-store-v3");
  auto board =
      source("tdm/schedule-board.bin", "exact-schedule-board-request-v1");
  auto boarddb =
      source("frontend/phase1/platform.normalized.json", "boarddb-v1");
  auto manifest = source("split/manifest.json");
  auto manifest_doc = PRead(manifest);
  const auto runtime_text = BuildVirtualRuntime(
      schedule, boarddb, manifest_doc.Token("clock_periods"));
  const auto runtime = PFields(runtime_text);
  auto physical_summary =
      source("physical/physical-summary.json", "physical-summary-v1");
  auto physical = ResolvePhysicalEvidenceCatalog(physical_summary);
  PCheck(physical.design == design && physical.platform == platform,
         "physical namespace disagrees");
  sources.push_back(schedule);
  sources.push_back(routes);
  source("tdm/schedule.json", "tdm-schedule-selector-v4");
  auto routes_selector =
      source("system-route/routes.json", "system-route-selector-v1");
  (void)Phase5ReportControlReference(p5r);
  BoundedJson generation(p5.Token("generation"));
  for (const auto &token : BoundedJson::Array(generation.Token("members"))) {
    BoundedJson member(token);
    const auto name = member.Text("path");
    PCheck(fs::path(name).filename() == name, "Phase 5 member path is unsafe");
    auto binding = source(fs::path("tdm") / name);
    PCheck(member.Integer("bytes") >= 0 &&
               std::uint64_t(member.Integer("bytes")) ==
                   binding.generation().bytes &&
               member.Text("sha256") == PHash(binding),
           "Phase 5 generation member seal disagrees");
  }
  std::optional<ArtifactRef> measured;
  if (fs::exists(output / "timing/board-link-timing.json"))
    measured = source("timing/board-link-timing.json");
  const auto board_timing = ResolvePhysicalBoardTiming(boarddb, measured);
  auto assignment =
           source("partition/assignment.sqlite", "assignment-index-v4"),
       contract = source("tdm/cross_layer_timing.json"),
       schedule_selector =
           source("tdm/schedule.json", "tdm-schedule-selector-v4");
  PWorkspace work(output);
  const auto stage = work.root;
  const auto coverage =
      Coverage(contract, schedule_selector, schedule, assignment, physical,
               board_timing, stage, sources);
  for (const auto &item : physical.local_path_timing) {
    sources.push_back(item.selector);
    sources.push_back(item.store);
  }
  fs::create_directory(stage / "global-opensta");
  auto global = RunPhysicalGlobalSta(
      routes, schedule, board, physical, board_timing, runtime_text, opensta,
      stage / "global-opensta", cancelled, launcher_reference);
  auto origin =
      BoundedJson(BoundedJson(global.event_binding).Token("source_binding"));
  PCheck(origin.Text("routes_sha256") == PHash(routes_selector),
         "original timing source belongs to another route artifact");
  auto timing_fields = PFields(Timing(global, runtime));
  auto sta_fields = PFields(timing_fields.at("global_opensta"));
  auto executable = PBind(opensta, "external-tool-executable-v1");
  sources.push_back(executable);
  sta_fields["engine"] = EngineIdentity(global, executable);
  timing_fields["global_opensta"] = PObject(sta_fields);
  timing_fields["physical_evidence_completeness"] = coverage;
  const auto timing = PObject(timing_fields);
  BoundedJson timing_doc(timing);
  auto physical_doc = PRead(physical_summary), board_document = PRead(boarddb);
  const auto physical_metrics =
      PhysicalMetrics(physical_doc, board_document, runtime);
  {
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(compare, physical_metrics,
                          physical_doc.Token("validation"),
                          "physical summary validation disagrees");
  }
  const auto p3v = PFields(p3.Token("validation")),
             p4v = PFields(p4.Token("validation")),
             p5v = PFields(p5.Token("validation")),
             equiv = PFields(p6.Token("equivalence"));
  Raw qor{
      {"schema", JsonQuote("emuflow.qor-report/v4")},
      {"status",
       JsonQuote(BoundedJson(coverage).Text("status") != "pass" ? "incomplete"
                 : timing_doc.Text("status") == "pass"          ? "pass"
                                                                : "fail")},
      {"design", JsonQuote(design)},
      {"platform", JsonQuote(platform)},
      {"whole_design_timing_complete", "true"},
      {"partition",
       PObject({{"instances", PGet(p3v, "instances")},
                {"used_fpgas", PGet(p3v, "used_fpgas")},
                {"cut_nets", PGet(p3v, "cut_nets")},
                {"cut_sink_endpoints", PGet(p3v, "cut_sink_endpoints")}})},
      {"system_routing",
       PObject({{"demands", PGet(p4v, "demands")},
                {"routed_sinks", PGet(p4v, "routed_sinks")},
                {"bit_hops", PGet(p4v, "total_link_bit_hops")},
                {"max_link_utilization", PGet(p4v, "max_link_utilization")}})},
      {"tdm",
       PObject({{"scheduled_bit_hops", PGet(p5v, "scheduled_bit_hops")},
                {"frame_slots", PGet(p5v, "frame_slots")},
                {"completion_slot", PGet(p5v, "completion_slot")},
                {"max_domain_utilization", PGet(p5v, "max_domain_utilization")},
                {"collisions", PGet(p5v, "collisions")}})},
      {"runtime",
       PObject({{"fabric_frequency_mhz",
                 PGet(PFields(PGet(runtime, "fabric_clock")), "frequency_mhz")},
                {"nominal_virtual_frequency_mhz",
                 PGet(PFields(PGet(runtime, "virtual_dut_clock")),
                      "nominal_frequency_mhz")},
                {"nominal_virtual_period_ns",
                 PGet(PFields(PGet(runtime, "virtual_dut_clock")),
                      "nominal_period_ns")},
                {"shadow_settle_ns",
                 PGet(PFields(PGet(runtime, "frame")), "shadow_settle_ns")},
                {"barrier_release_slot", PGet(PFields(PGet(runtime, "frame")),
                                              "barrier_release_slot")}})},
      {"timing", timing},
      {"equivalence",
       PObject({{"cycles", PGet(equiv, "cycles")},
                {"compared_state_bits", PGet(equiv, "compared_state_bits")},
                {"compared_output_bits", PGet(equiv, "compared_output_bits")},
                {"mismatches", PGet(equiv, "mismatches")},
                {"trace_sha256", PGet(equiv, "trace_sha256")}})},
      {"physical", physical_metrics},
      {"physical_evidence_completeness", coverage},
      {"board_binding", PGet(runtime, "board_binding")}};
  const auto request_path = stage / "qor-request.bin";
  {
    CheckedRequestWriter request(request_path.string());
    request.Put<std::uint64_t>(0x31515047464d5545ULL);
    request.Put<std::uint32_t>(1);
    Generation(request, global.events);
    Generation(request, global.index);
    Generation(request, global.measurements);
    request.Text(Canonical(PObject(qor)));
    request.Finish();
  }
  auto request = PBind(request_path, "global-qor-request-v1");
  const auto store_path = stage / "qor.sqlite";
  PublishGlobalQor({global.events, global.index, global.measurements, request},
                   store_path.string());
  auto store = PBind(store_path, "qor-report-store-v1");
  const auto logical = PMeta(store, "logical_sha256"),
             counts = PMeta(store, "counts");
  const auto validation_path = stage / "qor-validation.bin";
  {
    CheckedRequestWriter out(validation_path.string());
    out.Put<std::uint64_t>(0x31565147464d5545ULL);
    out.Put<std::uint32_t>(1);
    out.Generation(store.generation());
    out.Text(Canonical(PObject(qor)));
    out.Text(DecodeJsonString(logical));
    out.Finish();
  }
  ValidateGlobalQor(
      {store, PBind(validation_path, "global-qor-validation-request-v1")},
      (stage / "qor-validation.json").string());
  const auto digest = PHash(store),
             store_name = "qor_report." + digest + ".sqlite";
  fs::rename(store_path, stage / store_name);
  PWrite(stage / "qor_report.json",
         PObject({{"schema", JsonQuote("emuflow.qor-report-storage/v1")},
                  {"logical_schema", JsonQuote("emuflow.qor-report/v4")},
                  {"application_id", std::to_string(0x454d5551)},
                  {"user_version", "1"},
                  {"header", PObject(qor)},
                  {"counts", counts},
                  {"logical_sha256", logical},
                  {"artifact", JsonQuote(store_name)},
                  {"store_bytes",
                   std::to_string(fs::file_size(stage / store_name))},
                  {"store_sha256", JsonQuote(digest)}}));
  const auto measurement_digest = PHash(global.measurements),
             measurement_name =
                 "measurements." + measurement_digest + ".sqlite";
  BoundedJson measurement(global.measurement_header),
      reduction(measurement.Token("reduction"));
  PWrite(
      stage / "global-opensta/measurements.json",
      PObject({{"schema", JsonQuote("emuflow.global-measurement-storage/v1")},
               {"artifact", JsonQuote(measurement_name)},
               {"store_bytes",
                std::to_string(global.measurements.generation().bytes)},
               {"store_sha256", JsonQuote(measurement_digest)},
               {"source_logical_sha256",
                measurement.Token("source_logical_sha256")},
               {"measurement_logical_sha256",
                reduction.Token("measurements_sha256")}}));
  fs::rename(global.measurements.path(),
             stage / "global-opensta" / measurement_name);
  const auto frame = PFields(PGet(runtime, "frame")),
             clocks = PFields(PGet(runtime, "virtual_dut_clocks")),
             timebase = PFields(PGet(runtime, "virtual_timebase"));
  std::vector<std::string> fpgas;
  for (const auto &token : BoundedJson::Array(board_document.Token("fpgas")))
    fpgas.push_back(BoundedJson(token).Text("id"));
  Raw validation{
      {"status", JsonQuote("pass")},
      {"fpgas", std::to_string(fpgas.size())},
      {"frame_slots", PGet(frame, "slots")},
      {"completion_slot", PGet(frame, "completion_slot")},
      {"barrier_release_slot", PGet(frame, "barrier_release_slot")},
      {"shadow_settle_slots", PGet(frame, "shadow_settle_slots")},
      {"shadow_settle_ns", PGet(frame, "shadow_settle_ns")},
      {"virtual_dut_clocks", std::to_string(clocks.size())},
      {"clock_event_count",
       std::to_string(BoundedJson::Array(PGet(timebase, "events")).size())},
      {"nominal_virtual_frequency_mhz",
       PGet(PFields(PGet(runtime, "virtual_dut_clock")),
            "nominal_frequency_mhz")}};
  PCheck(PText(equiv, "status") == "pass" &&
             PInteger(equiv, "mismatches") == 0 &&
             PInteger(p5v, "collisions") == 0,
         "functional equivalence or schedule legality failed");
  Raw report{
      {"schema", JsonQuote("emuflow.phase7c-report/v3")},
      {"phase", JsonQuote("7C")},
      {"increment", JsonQuote("unified-physical-link-tdm-system-timing")},
      {"status", qor.at("status")},
      {"design", JsonQuote(design)},
      {"platform", JsonQuote(platform)},
      {"provider", PGet(runtime, "provider")},
      {"stage_implementation_id", JsonQuote("native-physical-phase7c-v1")},
      {"validation", PObject(validation)},
      {"functional_equivalence",
       PObject({{"status", JsonQuote("pass")},
                {"cycles", PGet(equiv, "cycles")},
                {"trace_sha256", PGet(equiv, "trace_sha256")}})},
      {"schedule_legality",
       PObject({{"status", JsonQuote("pass")},
                {"collisions", PGet(p5v, "collisions")},
                {"cross_layer_timing",
                 p5.Token("cross_layer_timing_validation")}})},
      {"physical", physical_metrics},
      {"system_timing_summary", timing},
      {"system_timing_ref", PObject({{"artifact", JsonQuote("qor_report")},
                                     {"json_pointer", JsonQuote("/timing")}})},
      {"physical_evidence_completeness", coverage},
      {"target_clock_wns_ns",
       PGet(PFields(timing_doc.Token("target_clock")), "worst_slack_bound_ns")},
      {"target_clock_tns_ns", PGet(PFields(timing_doc.Token("target_clock")),
                                   "total_negative_slack_bound_ns")},
      {"virtual_runtime_wns_ns",
       PGet(PFields(timing_doc.Token("runtime_clock")),
            "worst_slack_bound_ns")},
      {"virtual_runtime_tns_ns",
       PGet(PFields(timing_doc.Token("runtime_clock")),
            "total_negative_slack_bound_ns")},
      {"artifacts",
       PObject({{"runtime_contract", JsonQuote("runtime_contract.json")},
                {"runtime_controller_rtl",
                 JsonQuote("virtual_runtime_controller.sv")},
                {"runtime_controller_testbench",
                 JsonQuote("virtual_runtime_controller_tb.sv")},
                {"runtime_timing_xdc", JsonQuote("runtime_timing.xdc")},
                {"qor_report", JsonQuote("qor_report.json")},
                {"report", JsonQuote("phase7c_report.json")},
                {"cross_layer_physical_binding",
                 JsonQuote("cross_layer_physical_binding.json")}})}};
  PWrite(stage / "runtime_contract.json", runtime_text);
  auto controller = source("split/virtual_runtime_controller.sv");
  fs::copy_file(controller.path(), stage / "virtual_runtime_controller.sv");
  PWrite(stage / "virtual_runtime_controller_tb.sv",
         RuntimeSignoffTestbench(runtime, fpgas));
  PWrite(stage / "runtime_timing.xdc", RuntimeSignoffXdc(runtime));
  PWrite(stage / "phase7c_report.json", PObject(report));
  // Event/index owners and numerical requests are invocation scratch. Terminal
  // replay binds the published measurement logical identity to rebuilt events.
  for (const auto &name :
       {"qor-request.bin", "qor-validation.bin", "qor-validation.json"})
    fs::remove(stage / name);
  for (const auto &name :
       {"events.sqlite", "event-index.sqlite", "event-binding.bin",
        "event-export.bin", "measurement-import.bin"})
    fs::remove(stage / "global-opensta" / name);
  auto model_manifest = PRead(PBind(stage / "global-opensta/event-model.json"));
  auto model_fields = model_manifest.Fields();
  model_fields["directory"] = JsonQuote(
      (target / "global-opensta" / global.model_directory.filename()).string());
  PWrite(stage / "global-opensta/event-model.json", PObject(model_fields));
  sources.insert(sources.end(), global.engine_sources.begin(),
                 global.engine_sources.end());
  for (const auto &source : sources)
    source.RequireUnchanged();
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed,
                     "physical Phase 7C cancelled before publication");
  PublishNewDirectory(stage.string(), target.string(), sources);
  work.root.clear();
}
} // namespace emuflow::core
