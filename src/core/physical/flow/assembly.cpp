#include "common/artifact_io.hpp"
#include "physical/evidence_catalog.hpp"
#include "physical/flow/context.hpp"
#include "physical/physical_evidence.hpp"
#include "physical/report.hpp"
#include <cmath>
namespace emuflow::core::physical_flow {
using namespace openparf_input;
namespace {
bool Active(const R &r) { return std::get<bool>(r.at("active").value); }
const R &Stage(const R &record, const std::string &backend,
               const std::string &name) {
  const auto &stages = record.at("stages");
  return Active(record)
             ? stages
                   .at(backend == "vivado" ? "vivado_implementation"
                                           : "rapidwright_implementation")
                   .at(name)
             : stages.at(name);
}
R Item(const R &result) {
  const bool active = !result.has("implementation_status") ||
                      result.at("implementation_status").text() != "inactive";
  R row = result.at("cell_accounting");
  row["fpga"] = result.at("identity").at("fpga");
  row["backend"] = result.at("identity").at("backend");
  row["active"] = active;
  for (const auto &field : std::get<O>(result.at("closure").value))
    row[field.first] = field.second;
  row["wns_ns"] = result.at("timing").at("wns_ns");
  row["clocks"] = result.at("clocks");
  R timing = O{{"applicable", active}};
  for (const auto *key :
       {"dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns", "timing_met",
        "tns_ns", "failing_endpoints", "failing_endpoint_constraints"})
    if (result.at("timing").has(key))
      timing[key] = result.at("timing").at(key);
  row["timing"] = timing;
  for (const auto *key :
       {"critical_path_ns", "clock_domain_delays_ns", "clock_domain_presence"})
    if (result.at("timing").has(key))
      row[key] = result.at("timing").at(key);
  for (const auto *key : {"analysis_scope", "resource_usage", "hard_resources"})
    if (result.has(key))
      row[key] = result.at(key);
  return row;
}
void Budget(const R &result, const Partition &part) {
  const auto &value = result.at("resource_usage").at("physical"),
             &capacity = part.budget.at("capacity"),
             &counts = value.at("counts");
  const auto number = [](const R &r) {
    return ParseFiniteDecimal(r.encode(), "physical budget");
  };
  const auto limit = number(part.budget.at("utilization_limit"));
  Check(limit > 0 && limit <= 1, "invalid utilization limit");
  O allowed, used, device, utilization;
  for (const auto &field : std::get<O>(capacity.value)) {
    const I maximum = field.second.integer();
    Check(maximum >= 0 && counts.has(field.first),
          "physical resource dimension is missing");
    const I n = counts.at(field.first).integer(),
            budget = I(std::floor(double(maximum) * limit));
    Check(n >= 0 && n <= budget,
          "physical resource budget exceeded: " + part.id + "." + field.first);
    allowed[field.first] = budget;
    used[field.first] = n;
    device[field.first] = maximum ? double(n) / maximum : 0.0;
    utilization[field.first] = budget ? double(n) / budget : 0.0;
  }
  const R expected = O{{"schema", "emuflow.device-resource-usage/v1"},
                       {"status", "pass"},
                       {"stage", "post-physical-materialization"},
                       {"fpga", part.id},
                       {"part", part.part},
                       {"capacity_basis", "whole-device-raw-capacity"},
                       {"capacity", capacity},
                       {"utilization_limit", limit},
                       {"allowed", allowed},
                       {"counts", used},
                       {"device_utilization", device},
                       {"budget_utilization", utilization}};
  SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
  RequireEquivalentJson(
      compare, value.encode(), expected.encode(),
      "physical budget evidence differs from original BoardDB");
}
R Evidence(const fs::path &path, const fs::path &root, unsigned kind,
           std::vector<ArtifactRef> &sources) {
  const std::map<unsigned, std::pair<const char *, const char *>> types{
      {1, {"boundary-identity-selector-v1", "emuflow.boundary-identity/v1"}},
      {2, {"boundary-timing-selector-v1", "emuflow.boundary-timing/v1"}},
      {3,
       {"logic-segment-timing-selector-v1", "emuflow.logic-segment-timing/v1"}},
      {4, {"local-path-timing-selector-v1", "emuflow.local-path-timing/v1"}}};
  const auto info = types.at(kind);
  const auto ref = ArtifactRef::Bind(path.string(), info.first);
  const auto relative = fs::path(ref.path()).lexically_relative(root);
  Check(!relative.empty() && *relative.begin() != ".." && !fs::is_symlink(path),
        "physical evidence escapes implementation owner");
  auto selector = Read(ref);
  Check(selector.at("logical_schema").text() == info.second,
        "physical evidence logical schema disagrees");
  const auto store = Resolve(ref, "artifact",
                             kind == 1   ? "boundary-identity-store-v1"
                             : kind == 2 ? "boundary-timing-store-v1"
                             : kind == 3 ? "logic-segment-timing-store-v1"
                                         : "local-path-timing-store-v1");
  SqliteStore db(store);
  (void)AuditPhysicalEvidenceStore(db.database(), kind);
  sources.push_back(ref);
  sources.push_back(store);
  return O{{"schema", "emuflow.physical-evidence-reference/v2"},
           {"logical_schema", info.second},
           {"path", relative.string()},
           {"bytes", ref.generation().bytes},
           {"sha256", FileHash(ref)}};
}
R Execution(const Context &c) {
  R value = O{{"seed", c.seed},
              {"requested_workers", c.workers},
              {"effective_workers", std::min<I>(c.workers, c.selected.size())},
              {"ordering", "boarddb-fpga-order"},
              {"pack_place_resume", c.resume},
              {"route_resume", c.resume}};
  if (c.backend == "rapidwright")
    value["rapidwright_placer"] = "openparf-native";
  return value;
}
} // namespace
R Context::Batch(const std::vector<R> &records) {
  A expected, chosen, results;
  for (const auto &part : partitions)
    expected.push_back(part.id);
  for (const auto &record : records) {
    const auto id = record.at("fpga").text();
    chosen.push_back(id);
    const auto ref = ArtifactRef::Bind(
        (lease->generation / id / "partition-record.json").string(),
        "physical-input-document-v1");
    results.push_back(
        O{{"fpga", id}, {"path", ref.path()}, {"sha256", FileHash(ref)}});
  }
  O provenance;
  for (const auto &pair : std::vector<std::pair<std::string, std::string>>{
           {"split_manifest", "split-manifest"},
           {"platform", "board"},
           {"schedule", "schedule-selector"}})
    provenance[pair.first] = O{{"path", owners.at(pair.second).path()},
                               {"sha256", FileHash(owners.at(pair.second))}};
  R report =
      O{{"schema", "emuflow.physical-partition-batch/v1"},
        {"status", "pass"},
        {"scope", "selected-physical-partitions; whole-design Phase7C pending"},
        {"implementation_id", "native-multi-fpga-physical-v1"},
        {"backend", descriptor},
        {"architecture", architecture},
        {"execution", Execution(*this)},
        {"expected_fpgas", expected},
        {"selected_fpgas", chosen},
        {"source", provenance},
        {"records", results},
        {"summary",
         O{{"status", "pass"}, {"completed_partitions", records.size()}}}};
  Json(lease->root / "physical-partition-batch.json", report, sources);
  return report;
}
R Context::Assemble(const std::vector<R> &records) {
  Stop();
  Check(records.size() == partitions.size(),
        "incomplete physical partition population");
  A expected, items;
  O identity_refs, boundary_refs, logic_refs, local_refs, io_records;
  std::set<std::string> semantic, routes, schedules;
  I io_pairs = 0;
  const bool fixed = manifest.has("virtual_board_binding") &&
                     !manifest.at("virtual_board_binding").null();
  for (std::size_t i = 0; i < records.size(); ++i) {
    const auto &record = records[i];
    const auto &part = partitions[i];
    Check(record.at("fpga").text() == part.id &&
              record.at("part").text() == part.part &&
              record.at("status").text() == "pass",
          "physical record namespace/order disagrees");
    expected.push_back(part.id);
    items.push_back(Item(record.at("physical_result")));
    if (Active(record))
      Budget(record.at("physical_result"), part);
    if (fixed && Active(record)) {
      const auto &io = record.at("stages").at("virtual_io_implementation");
      Check(io.at("status").text() == "pass",
            "missing fixed differential I/O implementation");
      io_records[part.id] = io;
      io_pairs += io.at("differential_pairs").integer();
    }
    identity_refs[part.id] = Evidence(record.at("stages")
                                          .at("placement_ir")
                                          .at("boundary_identity")
                                          .at("output")
                                          .text(),
                                      lease->root, 1, sources);
    boundary_refs[part.id] = Evidence(Stage(record, backend, "boundary_timing")
                                          .at("import")
                                          .at("output")
                                          .text(),
                                      lease->root, 2, sources);
    if (queries) {
      const auto logic_path = Stage(record, backend, "logic_segment_timing")
                                  .at("import")
                                  .at("output")
                                  .text();
      logic_refs[part.id] = Evidence(logic_path, lease->root, 3, sources);
      const auto selector = Document(logic_path),
                 origin = selector.at("source");
      Check(origin.at("design").text() == design &&
                origin.at("platform").text() == platform &&
                origin.at("fpga").text() == part.id,
            "logic timing namespace disagrees");
      semantic.insert(origin.at("semantic_contract_sha256").encode());
      routes.insert(origin.at("system_routes_sha256").text());
      schedules.insert(origin.at("tdm_schedule_sha256").text());
      local_refs[part.id] = Evidence(Stage(record, backend, "local_path_timing")
                                         .at("import")
                                         .at("output")
                                         .text(),
                                     lease->root, 4, sources);
    }
  }
  R summary = O{{"schema", "emuflow.phase7b-physical-summary/v1"},
                {"status", "pass"},
                {"design", design},
                {"platform", platform},
                {"provider", backend + "-physical-summary-v1"},
                {"qualification", descriptor.at("qualification")},
                {"backend", descriptor},
                {"fpgas", items},
                {"boundary_identities", identity_refs},
                {"boundary_timing", boundary_refs}};
  if (fixed)
    summary["virtual_io_implementation"] =
        O{{"status", "pass"},
          {"provider", backend == "vivado"
                           ? "vivado-fixed-lvds-routed-boundary-v1"
                           : "rapidwright-fixed-lvds-iob-routed-boundary-v1"},
          {"fpgas", io_records},
          {"differential_pairs", io_pairs}};
  if (queries) {
    Check(semantic.size() == 1 && routes.size() == 1 && schedules.size() == 1,
          "physical timing semantic/route/schedule seals disagree");
    summary["logic_segment_timing"] = logic_refs;
    summary["local_path_timing"] = local_refs;
    summary["logic_segment_system_routes_sha256"] = *routes.begin();
    summary["logic_segment_tdm_schedule_sha256"] = *schedules.begin();
    if (*semantic.begin() != "null")
      summary["logic_segment_semantic_contract_sha256"] =
          R::Parse(*semantic.begin());
  }
  summary["validation"] = R::Parse(ValidatePhysicalSummaryReport(
      summary.encode(), board.encode(), runtime.encode()));
  R report = O{
      {"schema", "emuflow.multi-fpga-physical-flow/v1"},
      {"status", "pass"},
      {"provider", "phase6-emuir+physical-backend-v1"},
      {"implementation_id", "native-multi-fpga-physical-v1"},
      {"backend", descriptor},
      {"design", design},
      {"platform", platform},
      {"split_manifest", O{{"path", owners.at("split-manifest").path()},
                           {"sha256", FileHash(owners.at("split-manifest"))}}},
      {"architecture", architecture},
      {"execution", Execution(*this)},
      {"expected_fpgas", expected},
      {"fpgas", A(records.begin(), records.end())},
      {"physical_summary_ref", "physical-summary.json"}};
  report["summary"] =
      R::Parse(ValidatePhysicalFlowReport(report.encode()).summary);
  StagedArtifact pending_summary(
      (lease->root / "physical-summary.json").string()),
      pending_report(
          (lease->root / "multi-fpga-physical-flow-report.json").string());
  Json(pending_summary.path(), summary, sources);
  Json(pending_report.path(), report, sources);
  if (queries)
    (void)ResolvePhysicalEvidenceCatalog(
        ArtifactRef::Bind(pending_summary.path(), "physical-summary-v1"));
  const auto backup = lease->generation / "publication-backup";
  fs::create_directories(backup);
  Stop();
  PublishArtifactGroup(
      {{pending_summary.path(), lease->root / "physical-summary.json"},
       {pending_report.path(),
        lease->root / "multi-fpga-physical-flow-report.json"}},
      backup, sources, [&] { Stop(); });
  return report;
}
} // namespace emuflow::core::physical_flow
