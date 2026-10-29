#include "physical/rapidwright/partition.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "ir/resource_view.hpp"
#include "physical/flow/context.hpp"
#include "physical/mapped_index.hpp"
#include "physical/measurement_stage.hpp"
#include "physical/placement_lowering.hpp"
#include "physical/report.hpp"
#include "physical/transport/synthesis.hpp"
#include "physical/vivado/vivado_export.hpp"
#include "physical/vivado/vivado_partition.hpp"
#include "storage/schema/emuir_compact_schema.hpp"
#include <algorithm>
namespace emuflow::core::physical_flow {
using namespace openparf_input;
namespace {
void EmptyTransport(const fs::path &path, const std::string &design,
                    const std::string &fpga,
                    const std::vector<ArtifactRef> &sources) {
  SqliteArtifactWriter writer(path.string(), "emuir-compact-v2", sources);
  auto &db = writer.database();
  db.Execute(kEmuirCompactDDL);
  db.Execute("CREATE INDEX endpoint_owner ON "
             "endpoint_records(instance_seq,port_seq,bit)");
  db.Execute("INSERT INTO payloads VALUES(0,'{}')");
  const auto name = design + "__" + fpga + "__inactive_transport";
  const R info = O{{"name", name},
                   {"top", name},
                   {"source_format", "inactive-empty-partition-v1"}};
  const std::map<std::string, std::string> fields{
      {"storage_schema", "emuflow.emuir-sqlite/v2"},
      {"logical_schema", "emuflow.emuir/v1"},
      {"design_json", info.encode()},
      {"warnings_json", "[]"},
      {"record_counts_json",
       "{\"cell_records\":0,\"net_records\":0,\"endpoint_records\":0,"
       "\"payloads\":1,\"pin_names\":0,\"alias_records\":0,\"port_records\":0,"
       "\"clocks\":0}"}};
  {
    auto insert = db.Prepare("INSERT INTO metadata VALUES(?,?)");
    for (const auto &row : fields) {
      insert.BindText(1, row.first);
      insert.BindText(2, row.second);
      insert.Execute();
    }
  }
  writer.Commit();
}
R Reservation(const std::string &fpga, const R &used, const R &reservations) {
  const R reserved = reservations.has(fpga) ? reservations.at(fpga) : R(O{});
  O counts, budget, headroom;
  std::set<std::string> fields;
  Check(std::holds_alternative<O>(used.value) &&
            std::holds_alternative<O>(reserved.value),
        "invalid transport resource records");
  for (const auto &r : std::get<O>(used.value))
    fields.insert(r.first);
  for (const auto &r : std::get<O>(reserved.value))
    fields.insert(r.first);
  for (const auto &key : fields) {
    const I actual = used.has(key) ? used.at(key).integer() : 0,
            limit = reserved.has(key) ? reserved.at(key).integer() : 0;
    Check(actual >= 0 && limit >= 0 && actual <= limit,
          "transport exceeds sealed Phase 3 reservation: " + fpga + "." + key);
    counts[key] = actual;
    budget[key] = limit;
    headroom[key] = limit - actual;
  }
  return O{{"status", "pass"},
           {"provider", "phase3-sealed-transport-resource-reservation-v1"},
           {"used", counts},
           {"reserved", budget},
           {"headroom", headroom},
           {"violations", 0},
           {"automatic_relaxation", false}};
}
void Retire(const ArtifactRef &ref, R &report, const std::string &consumer,
            const fs::path &report_path,
            const std::vector<ArtifactRef> &sources) {
  ref.RequireUnchanged();
  Check(report.at("status").text() == "pass" &&
            fs::path(report.at("output").text()) == ref.path() &&
            !fs::is_symlink(ref.path()) && !fs::exists(ref.path() + "-wal") &&
            !fs::exists(ref.path() + "-journal"),
        "consumed physical input is not the closed producer output");
  report["intermediate_lifecycle"] = O{{"state", "retired"},
                                       {"last_consumer", consumer},
                                       {"bytes", ref.generation().bytes}};
  Check(fs::remove(ref.path()), "cannot retire consumed physical input");
  if (!report_path.empty())
    Json(report_path, report, sources);
}
R VivadoInputs(const ArtifactRef &merged, const ArtifactRef &library,
               const fs::path &root, const std::vector<ArtifactRef> &sources,
               const volatile std::sig_atomic_t *flag) {
  fs::create_directories(root);
  {
    SqliteStore input(merged);
    auto &db = input.database();
    auto bad =
        db.Prepare("SELECT n.id FROM net_records n WHERE NOT EXISTS(SELECT 1 "
                   "FROM endpoint_records d WHERE d.net_seq=n.seq AND "
                   "d.role=0) AND EXISTS(SELECT 1 FROM endpoint_records s "
                   "WHERE s.net_seq=n.seq AND s.role=1) LIMIT 1");
    Check(!bad.Step(), "Vivado source has a used undriven net");
  }
  const auto index = root / "partition.mapped.sqlite",
             index_report = root / "mapped-index-report.json",
             ir_request = root / "mapped-request.bin";
  {
    CheckedRequestWriter writer(ir_request.string(), sources);
    writer.Put<std::uint64_t>(0x31584d50464d5545ULL);
    writer.Put<std::uint32_t>(1);
    writer.Generation(merged.generation());
    writer.Generation(library.generation());
    writer.Finish();
  }
  Keep(BuildMappedIndex({merged, library,
                         ArtifactRef::Bind(ir_request.string(),
                                           "xilinx-mapped-index-request-v1")},
                        index.string(), index_report.string(), flag));
  const auto mapped = Document(index_report);
  const auto mapped_ref =
      ArtifactRef::Bind(index.string(), "xilinx-mapped-index-v2");
  const auto request = root / "export-request.bin", rtl = root / "partition.v",
             functions = root / "partition.functions.sqlite",
             summary = root / "export-summary.json";
  {
    CheckedRequestWriter writer(request.string(), sources);
    writer.Put<std::uint64_t>(0x31585656464d5545ULL);
    writer.Put<std::uint32_t>(1);
    writer.Generation(mapped_ref.generation());
    writer.Text(mapped.at("index_sha256").text());
    writer.Put<std::uint64_t>(mapped.at("cells").integer());
    writer.Finish();
  }
  ExportVivado({mapped_ref, ArtifactRef::Bind(request.string(),
                                              "vivado-export-request-v1")},
               rtl.string(), functions.string(), summary.string());
  const auto exported = Document(summary);
  R result =
      O{{"schema", "emuflow.vivado-netlist-report/v1"},
        {"status", "pass"},
        {"provider", "compact-emuir-to-vivado-indexed-primitives-v1"},
        {"implementation_id", "native-vivado-export-v1"},
        {"top", mapped.at("top")},
        {"source_instances", mapped.at("cells")},
        {"emitted_instances", exported.at("cells")},
        {"emitted_inventory", mapped.at("audit").at("cell_types")},
        {"output", rtl.string()},
        {"output_sha256", exported.at("rtl_sha256")},
        {"mapped_index",
         O{{"path", index.string()}, {"sha256", mapped.at("index_sha256")}}},
        {"source_functions",
         O{{"path", functions.string()},
           {"sha256", FileHash(ArtifactRef::Bind(
                          functions.string(), "vivado-source-functions-v1"))}}},
        {"optimization_policy", "vivado-optimized-source-lineage-v1"},
        {"primitive_audit", mapped.at("audit")},
        {"source_validation", O{{"schema", "emuflow.vivado-source-check/v1"},
                                {"status", "pass"},
                                {"policy", "reject-used-undriven-nets"},
                                {"used_undriven_nets", 0},
                                {"source_declared_undriven_nets", 0},
                                {"examples", A{}}}}};
  Json(root / "mapped-verilog-report.json", result, sources);
  return result;
}
R InactiveResult(const Partition &part, const std::string &backend,
                 const R &runtime, const O &artifacts) {
  O sealed;
  for (const auto &row : artifacts) {
    const auto ref =
        ArtifactRef::Bind(row.second.text(), "physical-input-document-v1");
    sealed[row.first] = O{{"path", ref.path()}, {"sha256", FileHash(ref)}};
  }
  return O{
      {"schema", "emuflow.physical-partition-result/v1"},
      {"status", "pass"},
      {"implementation_status", "inactive"},
      {"identity",
       O{{"backend", backend}, {"fpga", part.id}, {"part", part.part}}},
      {"cell_accounting", O{{"original_cells", 0},
                            {"transport_cells", 0},
                            {"routed_cells", 0},
                            {"physical_cells", 0},
                            {"infrastructure_cells", 0},
                            {"optimization_cells", 0}}},
      {"closure",
       O{{"unrouted_nets", 0}, {"drc_violations", 0}, {"drc_warnings", 0}}},
      {"clocks",
       O{{"fabric_period_ns", runtime.at("fabric_clock").at("period_ns")},
         {"dut_period_ns",
          runtime.at("virtual_dut_clock").at("nominal_period_ns")}}},
      {"timing",
       O{{"applicable", false},
         {"wns_ns", 0.0},
         {"tns_ns", 0.0},
         {"failing_endpoints", 0},
         {"failing_endpoint_constraints", 0},
         {"timing_met", true},
         {"dut_wns_ns", 0.0},
         {"fabric_wns_ns", 0.0},
         {"fabric_to_dut_wns_ns", 0.0},
         {"critical_path_ns", 0.0},
         {"clock_domain_delays_ns",
          O{{"overall", 0.0}, {"fabric", 0.0}, {"dut", 0.0}, {"cross", 0.0}}},
         {"clock_domain_presence",
          O{{"fabric", false}, {"dut", false}, {"cross", false}}}}},
      {"hard_resources", O{}},
      {"artifacts", sealed}};
}
} // namespace
WorkerResult Context::Implement(const Partition &part) {
  std::vector<ArtifactRef> proof;
  Stop();
  const auto root = lease->generation / part.id;
  fs::create_directories(root);
  R transport;
  ArtifactRef transport_ir = owners.at("transport");
  if (part.active()) {
    A rows{
        Source("runtime", Relative(manifest.at("runtime_controller_rtl").text(),
                                   "yosys-source-document-v1")),
        Source("rtl", Relative(part.split_item.at("transport_rtl").text(),
                               "yosys-source-document-v1")),
        Source("yosys", owners.at("yosys"))};
    if (owners.count("yosys-container"))
      rows.push_back(Source("container", owners.at("yosys-container")));
    const auto request =
        Request(root / "transport-request.json",
                O{{"schema", "emuflow.transport-synthesis-request/v1"},
                  {"sources", rows},
                  {"maximum_fanout", 512},
                  {"timeout_ms", 0}},
                "transport-synthesis-request-v1", sources);
    auto result = SynthesizeTransport(
        request, (root / "transport-synthesis").string(), cancelled);
    Keep(result);
    transport =
        Document(root / "transport-synthesis/transport-synthesis-report.json");
    transport_ir = result.outputs.at(1);
  } else {
    const auto path = root / "transport.emuir.sqlite";
    EmptyTransport(path, design, part.id, sources);
    transport_ir = ArtifactRef::Bind(path.string(), "emuir-compact-v2");
    transport = O{{"status", "pass"},
                  {"provider", "inactive-empty-partition-v1"},
                  {"execution", "not-run-empty-partition"},
                  {"instances", 0},
                  {"nets", 0},
                  {"output", path.string()},
                  {"sha256", FileHash(transport_ir)}};
  }
  std::vector<ArtifactRef> inputs{owners.at("split"), owners.at("transport"),
                                  transport_ir};
  A rows;
  for (const auto &ref : inputs)
    rows.push_back(O{{"path", ref.path()},
                     {"schema", ref.schema()},
                     {"generation", Generation(ref)}});
  const auto request = Request(root / "lowering-request.json",
                               O{{"schema", "emuflow.placement-lowering/v1"},
                                 {"fpga", part.id},
                                 {"sources", rows},
                                 {"output_name", "placement.emuir.sqlite"},
                                 {"boundary_name", "boundary-identities.json"},
                                 {"report_name", "placement-ir-report.json"}},
                               "placement-lowering-request-v1", sources);
  inputs.push_back(request);
  auto lowered =
      LowerPlacement(inputs, (root / "lowering").string(), cancelled);
  Keep(lowered);
  proof.push_back(lowered.outputs.at(1));
  auto lowering = Document(root / "lowering/placement-ir-report.json");
  lowered.outputs.at(2).RequireUnchanged();
  Check(lowering.at("instances").integer() ==
            part.cells + lowering.at("transport_instances").integer(),
        "lowering census disagrees with split owner");
  if (!part.active())
    Check(lowering.at("instances").integer() == 0 &&
              lowering.at("nets").integer() == 0,
          "inactive partition produced physical work");
  Retire(transport_ir, transport, "placement-ir-lowering", {}, sources);
  const auto merged = lowered.outputs.at(0), boundary = lowered.outputs.at(1);
  const I transport_cells = lowering.at("transport_instances").integer();
  O stages{{"transport_synthesis", transport}, {"placement_ir", lowering}};
  if (manifest.has("resource_reservations_by_fpga") &&
      !std::get<O>(manifest.at("resource_reservations_by_fpga").value).empty())
    stages["transport_resource_reservation"] =
        Reservation(part.id, part.active() ? transport.at("resources") : R(O{}),
                    manifest.at("resource_reservations_by_fpga"));
  R logic, local;
  if (queries) {
    logic = Query(part, merged, boundary, root / "logic-query", false, proof);
    local = Query(part, merged, boundary, root / "local-query", true, proof);
  }
  R physical, mapped;
  if (!part.active()) {
    auto refs = sources;
    O artifacts{{"placement_ir", merged.path()},
                {"boundary_identity", boundary.path()}};
    const auto timing = root / "boundary-timing.json";
    auto imported = R::Parse(
        PublishInactivePhysicalTiming(boundary, 2, timing.string(), refs));
    imported["output"] = timing.string();
    stages["boundary_timing"] =
        O{{"status", "pass"},
          {"execution", "not-applicable-empty-partition"},
          {"import", imported}};
    artifacts["boundary_timing"] = timing.string();
    if (queries)
      for (const auto &spec :
           std::vector<std::tuple<const char *, unsigned, R>>{
               {"logic", 3, logic}, {"local", 4, local}}) {
        const std::string name = std::get<0>(spec);
        const auto kind = std::get<1>(spec);
        auto query = std::get<2>(spec);
        const auto identity =
            ArtifactRef::Bind(query.at("identity").text(),
                              kind == 3 ? "logic-segment-identity-selector-v1"
                                        : "local-path-timing-selector-v1");
        const auto timing =
            root / (name == "logic" ? "logic-segment-timing.json"
                                    : "local-path-timing.json");
        auto imported = R::Parse(PublishInactivePhysicalTiming(
            identity, kind, timing.string(), refs));
        imported["output"] = timing.string();
        const auto key =
            name == "logic" ? "logic_segment_timing" : "local_path_timing";
        stages[key] = O{{"status", "pass"},
                        {"execution", "not-applicable-empty-partition"},
                        {"query", query},
                        {"import", imported}};
        artifacts[name + "_identity"] = identity.path();
        artifacts[name + "_timing"] = timing.string();
      }
    proof.insert(proof.end(), refs.begin(), refs.end());
    physical = InactiveResult(part, backend, runtime, artifacts);
  } else {
    R runtime_config = runtime;
    runtime_config["resource_budget"] = part.budget;
    R backend_config = config.at("backend_options");
    backend_config["schema"] = backend == "rapidwright"
                                   ? "emuflow.rapidwright-partition-request/v1"
                                   : "emuflow.vivado-partition-request/v1";
    backend_config["fpga"] = part.id;
    backend_config["part"] = part.part;
    backend_config["runtime"] = runtime_config;
    const auto result_root = lease->root / part.id / backend;
    if (backend == "rapidwright") {
      A rows;
      for (const auto &owner : owners)
        if (owner.first.rfind("backend.", 0) == 0)
          rows.push_back(Source(owner.first.substr(8), owner.second));
      rows.push_back(Source("ir", merged));
      rows.push_back(Source("library", owners.at("library")));
      rows.push_back(Source("boundary", boundary));
      if (queries) {
        rows.push_back(Source(
            "logic", ArtifactRef::Bind(logic.at("identity").text(),
                                       "logic-segment-identity-selector-v1")));
        rows.push_back(Source(
            "local", ArtifactRef::Bind(local.at("identity").text(),
                                       "local-path-timing-selector-v1")));
      }
      backend_config["sources"] = rows;
      backend_config["original_cells"] = part.cells;
      backend_config["transport_cells"] = transport_cells;
      backend_config["virtual_io"] = part.io;
      backend_config["resume"] = resume;
      auto request = Request(root / "backend-request.json", backend_config,
                             "rapidwright-partition-request-v1", sources);
      const auto accepted =
          RunRapidwrightPartition(request, result_root.string(), cancelled);
      Keep(accepted);
      proof.insert(proof.end(), accepted.outputs.begin(),
                   accepted.outputs.end());
      auto result = Document(result_root / "partition-report.json");
      if (queries) {
        result["logic_segment_timing"]["query"] = logic;
        result["local_path_timing"]["query"] = local;
      }
      physical = result.at("result");
      stages["rapidwright_implementation"] = result;
      if (!part.io.null())
        stages["virtual_io_implementation"] =
            result.at("virtual_io_implementation");
    } else {
      mapped = VivadoInputs(merged, owners.at("library"), root / "mapped",
                            sources, cancelled);
      O rows;
      const auto put = [&](const std::string &name, const ArtifactRef &ref) {
        rows[name] = O{{"path", ref.path()}, {"generation", Generation(ref)}};
      };
      for (const auto &owner : owners)
        if (owner.first.rfind("backend.", 0) == 0)
          put(owner.first.substr(8), owner.second);
      put("ir", merged);
      put("rtl", ArtifactRef::Bind(mapped.at("output").text(),
                                   "exact-sha256-text-v1"));
      put("mapped_index",
          ArtifactRef::Bind(mapped.at("mapped_index").at("path").text(),
                            "xilinx-mapped-index-v2"));
      put("functions",
          ArtifactRef::Bind(mapped.at("source_functions").at("path").text(),
                            "vivado-source-functions-v1"));
      put("boundary_identity", boundary);
      if (queries) {
        put("logic_identity",
            ArtifactRef::Bind(logic.at("identity").text(),
                              "logic-segment-identity-selector-v1"));
        put("logic_query", ArtifactRef::Bind(logic.at("query").text(),
                                             "logic-segment-query-tsv-v1"));
        put("local_identity",
            ArtifactRef::Bind(local.at("identity").text(),
                              "local-path-timing-selector-v1"));
        put("local_query", ArtifactRef::Bind(local.at("query").text(),
                                             "local-path-query-tsv-v1"));
      }
      if (!part.io.null()) {
        put("io_adapter",
            ArtifactRef::Bind(part.io.at("adapter").at("path").text(),
                              "exact-sha256-text-v1"));
        put("io_xdc", ArtifactRef::Bind(part.io.at("xdc").at("path").text(),
                                        "exact-sha256-text-v1"));
      }
      backend_config["sources"] = rows;
      backend_config["virtual_io_contract"] = part.io;
      backend_config["mapped_report"] =
          O{{"optimization_policy", mapped.at("optimization_policy")},
            {"mapped_sha256", mapped.at("mapped_index").at("sha256")},
            {"rtl_sha256", mapped.at("output_sha256")},
            {"functions_sha256", mapped.at("source_functions").at("sha256")},
            {"emitted_instances", mapped.at("emitted_instances")},
            {"original_cells", part.cells},
            {"transport_cells", transport_cells}};
      auto request = Request(root / "backend-request.json", backend_config,
                             "vivado-partition-request-v1", sources);
      std::vector<ArtifactRef> retained;
      const auto accepted = RunVivadoPartition(request, result_root.string(),
                                               cancelled, &retained);
      Keep(accepted);
      proof.insert(proof.end(), retained.begin(), retained.end());
      proof.insert(proof.end(), accepted.outputs.begin(),
                   accepted.outputs.end());
      auto result = Document(result_root / "vivado-partition-report.json");
      if (queries) {
        result["logic_segment_timing"]["query"] = logic;
        result["local_path_timing"]["query"] = local;
      }
      physical = result.at("result");
      stages["vivado_implementation"] = result;
      stages["mapped_verilog"] = mapped;
      if (!part.io.null())
        stages["virtual_io_implementation"] =
            result.at("virtual_io_implementation");
    }
  }
  (void)ValidatePhysicalPartitionReport(physical.encode(), backend, part.id,
                                        part.part, part.cells, transport_cells);
  Retire(merged, lowering,
         part.active() ? backend + "-physical-and-timing-validation"
                       : "empty-partition-query-and-timing-validation",
         root / "lowering/placement-ir-report.json", sources);
  stages["placement_ir"] = lowering;
  if (!part.active())
    physical["artifacts"]["placement_ir"]["retention"] =
        "retired-consumed-intermediate";
  if (backend == "vivado" && part.active()) {
    Retire(
        ArtifactRef::Bind(mapped.at("output").text(), "exact-sha256-text-v1"),
        mapped, "vivado-physical-and-timing-validation",
        root / "mapped/mapped-verilog-report.json", sources);
    stages["mapped_verilog"] = mapped;
  }
  R record =
      O{{"fpga", part.id},
        {"part", part.part},
        {"status", "pass"},
        {"active", part.active()},
        {"original_cells", part.cells},
        {"transport_cells", transport_cells},
        {"critical_path_ns", physical.at("timing").at("critical_path_ns")},
        {"stages", stages},
        {"physical_result", physical}};
  for (const auto &ref : proof)
    ref.RequireUnchanged();
  Json(root / "partition-record.json", record, sources);
  proof.push_back(ArtifactRef::Bind((root / "partition-record.json").string(),
                                    "physical-input-document-v1"));
  return {record, std::move(proof)};
}
} // namespace emuflow::core::physical_flow
