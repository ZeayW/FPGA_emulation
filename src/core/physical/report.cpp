#include "physical/report.hpp"
#include "common/bounded_json.hpp"
#include <algorithm>
#include <limits>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
using Doc = BoundedJson;
using Raw = std::map<std::string, std::string>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "physical report: " + why);
}
void Same(const std::string &a, const std::string &b, const std::string &why) {
  SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
  RequireEquivalentJson(db, a, b, "physical report: " + why);
}
std::string Number(double n) { return JsonRecord(n).Encode(); }
std::int64_t Count(Doc &doc, const std::string &key) {
  const auto n = doc.Integer(key);
  Check(n >= 0 && n <= INT64_MAX / 4096, "invalid count " + key);
  return n;
}
void Pass(Doc &doc, const std::string &owner) {
  Check(doc.Text("status") == "pass", owner + " did not pass");
}
void Zero(Doc &doc, const std::string &field) {
  Check(doc.Number(field) == 0, "inactive/nonclosed field " + field);
}
void Presence(Doc &p, bool inactive) {
  p.Keys({"fabric", "dut", "cross"});
  const bool fabric = p.Boolean("fabric"), dut = p.Boolean("dut"),
             cross = p.Boolean("cross");
  Check(inactive ? !(fabric || dut || cross) : fabric && dut == cross,
        "clock-domain presence disagrees");
}
std::string Backend(Doc &descriptor) {
  const auto id = descriptor.Text("id");
  Check(id == "vivado" || id == "rapidwright", "unsupported backend");
  const Raw expected{
      {"schema", JsonQuote("emuflow.physical-backend/v1")},
      {"id", JsonQuote(id)},
      {"implementation_engine",
       JsonQuote(id == "vivado" ? "vivado"
                                : "emuflow-pack-place+rapidwright-rwroute")},
      {"timing_engine",
       JsonQuote(id == "vivado" ? "vivado"
                                : "rapidwright-lightweight+opensta")},
      {"architecture_class", JsonQuote("xilinx-commercial-device")},
      {"source_model",
       JsonQuote(id == "vivado" ? "external-proprietary-provider"
                                : "external-mixed-license-provider")},
      {"qualification",
       JsonQuote(id == "vivado"
                     ? "vendor-device-implementation-not-board-signoff"
                     : "research-timing-not-vivado-signoff")},
      {"capabilities", "{\"packing\":true,\"placement\":true,\"routing\":true,"
                       "\"timing\":true,\"bitstream\":false}"}};
  Same(descriptor.text(), Doc::Encode(expected),
       "backend descriptor disagrees");
  return id;
}
void Result(Doc &value, const std::string &backend, const std::string &fpga,
            const std::string &part, std::int64_t original,
            std::int64_t transport) {
  Pass(value, "partition result");
  Check(value.Text("schema") == "emuflow.physical-partition-result/v1",
        "partition schema disagrees");
  Same(value.Token("identity"),
       Doc::Encode({{"backend", JsonQuote(backend)},
                    {"fpga", JsonQuote(fpga)},
                    {"part", JsonQuote(part)}}),
       "partition identity disagrees");
  Doc account(value.Token("cell_accounting"));
  const auto routed = Count(account, "routed_cells"),
             infra = Count(account, "infrastructure_cells"),
             optimization = Count(account, "optimization_cells"),
             physical = Count(account, "physical_cells");
  Check(Count(account, "original_cells") == original &&
            Count(account, "transport_cells") == transport &&
            physical == routed + infra + optimization,
        "cell census disagrees");
  Doc scope(value.Has("analysis_scope") ? value.Token("analysis_scope") : "{}");
  if (scope.Has("cell_accounting_policy") &&
      scope.Token("cell_accounting_policy") != "null") {
    Check(backend == "vivado" && scope.Text("cell_accounting_policy") ==
                                     "vivado-optimized-source-lineage-v1",
          "source lineage policy disagrees");
    const auto retained = Count(account, "source_cells_with_lineage"),
               missing = Count(account, "unretained_source_cells");
    Check(retained + missing == original + transport &&
              Count(account, "source_cells_with_multiple_objects") <= retained,
          "source lineage census disagrees");
  } else
    Check(routed == original + transport, "source cell census disagrees");
  Doc closure(value.Token("closure"));
  Check(Count(closure, "unrouted_nets") == 0 &&
            Count(closure, "drc_violations") == 0,
        "partition route/DRC did not close");
  if (value.Has("hard_resources") && value.Token("hard_resources") != "null") {
    Doc resources(value.Token("hard_resources"));
    for (const auto &f : resources.Fields())
      (void)Count(resources, f.first);
  }
  Doc timing(value.Token("timing"));
  const auto implementation = value.Has("implementation_status")
                                  ? value.Token("implementation_status")
                                  : "null";
  Check(!Doc(value.Token("artifacts")).Fields().empty(),
        "partition artifacts are missing");
  if (implementation == JsonQuote("inactive")) {
    Check(original + transport + routed + infra + optimization + physical ==
                  0 &&
              !timing.Boolean("applicable") && timing.Boolean("timing_met"),
          "inactive partition is inconsistent");
    for (const auto *name : {"wns_ns", "tns_ns", "dut_wns_ns", "fabric_wns_ns",
                             "fabric_to_dut_wns_ns", "critical_path_ns"})
      Zero(timing, name);
    Check(Count(timing, "failing_endpoints") == 0 &&
              Count(timing, "failing_endpoint_constraints") == 0,
          "inactive endpoint failures");
    Doc presence(timing.Token("clock_domain_presence")),
        delays(timing.Token("clock_domain_delays_ns")),
        clocks(value.Token("clocks"));
    Presence(presence, true);
    delays.Keys({"overall", "fabric", "dut", "cross"});
    for (const auto &field : delays.Fields())
      Zero(delays, field.first);
    Check(clocks.Number("fabric_period_ns") > 0 &&
              clocks.Number("dut_period_ns") > 0,
          "inactive clock contract is invalid");
    return;
  }
  Check(implementation == "null" || implementation == JsonQuote("implemented"),
        "unknown implementation state");
  for (const auto *field :
       {"wns_ns", "dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns"})
    (void)timing.Number(field);
  if (timing.Has("clock_domain_presence") &&
      timing.Token("clock_domain_presence") != "null") {
    Doc presence(timing.Token("clock_domain_presence")),
        delays(timing.Token("clock_domain_delays_ns"));
    Presence(presence, false);
    for (const auto *field : {"overall", "fabric", "dut", "cross"})
      Check(delays.Number(field) >= 0, "negative clock-domain delay");
    if (!presence.Boolean("dut"))
      Check(delays.Number("dut") == 0 && delays.Number("cross") == 0,
            "absent DUT clock has delay");
  }
  bool endpoint = backend == "rapidwright";
  for (const auto *field :
       {"tns_ns", "failing_endpoints", "failing_endpoint_constraints"})
    endpoint |= timing.Has(field) && timing.Token(field) != "null";
  if (endpoint) {
    const auto tns = timing.Number("tns_ns");
    const auto failed = Count(timing, "failing_endpoints"),
               constraints = Count(timing, "failing_endpoint_constraints");
    Check(tns <= 0 && failed <= constraints &&
              bool(tns < 0) == bool(constraints > 0),
          "endpoint timing census disagrees");
  }
  Check(timing.Boolean("timing_met") == bool(timing.Number("wns_ns") >= 0),
        "timing closure claim disagrees");
}
} // namespace

std::string ValidatePhysicalPartitionReport(const std::string &document,
                                            const std::string &backend,
                                            const std::string &fpga,
                                            const std::string &part,
                                            std::int64_t original,
                                            std::int64_t transport) {
  Doc value(document);
  Result(value, backend, fpga, part, original, transport);
  Doc account(value.Token("cell_accounting")), timing(value.Token("timing"));
  Raw summary{{"status", JsonQuote("pass")},
              {"backend", JsonQuote(backend)},
              {"fpga", JsonQuote(fpga)},
              {"part", JsonQuote(part)},
              {"routed_cells", account.Token("routed_cells")},
              {"physical_cells", account.Token("physical_cells")}};
  if (value.Has("implementation_status") &&
      value.Token("implementation_status") == JsonQuote("inactive")) {
    summary["active"] = "false";
    summary["wns_ns"] = "null";
    summary["tns_ns"] = "null";
    summary["failing_endpoints"] = "0";
  } else {
    summary["wns_ns"] = timing.Token("wns_ns");
    if (timing.Has("tns_ns")) {
      summary["tns_ns"] = timing.Token("tns_ns");
      summary["failing_endpoints"] = timing.Token("failing_endpoints");
    }
  }
  return Doc::Encode(summary);
}

PhysicalReportFacts ValidatePhysicalFlowReport(const std::string &document) {
  Doc report(document);
  Check(report.Text("schema") == "emuflow.multi-fpga-physical-flow/v1",
        "schema disagrees");
  Pass(report, "flow");
  Doc descriptor(report.Token("backend"));
  const auto backend = Backend(descriptor);
  const auto expected = Doc::Array(report.Token("expected_fpgas"), 256),
             records = Doc::Array(report.Token("fpgas"), 256);
  Check(!expected.empty() && expected.size() == records.size(),
        "FPGA coverage disagrees");
  std::set<std::string> names, seen;
  for (const auto &token : expected)
    Check(names.insert(DecodeJsonString(token)).second,
          "duplicate expected FPGA");
  std::int64_t original = 0, transport = 0, active = 0, failed = 0,
               constraints = 0, unrouted = 0, drc = 0;
  double critical = 0, worst = 0, tns = 0;
  bool have = false;
  for (const auto &token : records) {
    Doc item(token);
    const auto fpga = item.Text("fpga");
    Check(names.count(fpga) && seen.insert(fpga).second,
          "FPGA identity disagrees");
    Pass(item, "FPGA");
    Doc stages(item.Token("stages"));
    Check(stages.Has("transport_synthesis") && stages.Has("placement_ir"),
          "missing physical stages");
    for (const auto &stage : stages.Fields()) {
      Doc status(stage.second);
      Pass(status, stage.first);
    }
    const auto cells = Count(item, "original_cells"),
               communication = Count(item, "transport_cells");
    Doc lowering(stages.Token("placement_ir")),
        boundary(lowering.Token("boundary_identity")),
        validation(boundary.Token("validation"));
    Pass(validation, "boundary identity");
    Check(Count(lowering, "instances") == cells + communication,
          "lowering census disagrees");
    const auto part = item.Text("part");
    Check(!part.empty(), "part is empty");
    Doc result(item.Token("physical_result"));
    Result(result, backend, fpga, part, cells, communication);
    const bool enabled = !item.Has("active") || item.Boolean("active");
    const bool inactive =
        result.Has("implementation_status") &&
        result.Token("implementation_status") == JsonQuote("inactive");
    if (!enabled)
      Check(inactive && cells + communication == 0 &&
                stages.Has("boundary_timing") &&
                !stages.Has("rapidwright_implementation") &&
                !stages.Has("vivado_implementation"),
            "inactive stages disagree");
    else {
      Check(!inactive, "active FPGA is marked inactive");
      if (backend == "rapidwright") {
        Doc implementation(stages.Token("rapidwright_implementation"));
        for (const auto *key :
             {"mapped_netlist", "packing", "placement", "route",
              "routed_timing", "opensta", "boundary_timing"})
          Check(implementation.Has(key), "missing RapidWright stage");
      } else
        Check(stages.Has("mapped_verilog") &&
                  stages.Has("vivado_implementation"),
              "missing Vivado stages");
    }
    original += cells;
    transport += communication;
    active += enabled;
    Doc timing(result.Token("timing"));
    const auto delay =
        timing.Has("critical_path_ns") ? timing.Number("critical_path_ns") : 0;
    Check(delay >= 0, "negative critical path");
    critical = std::max(critical, delay);
    if (enabled) {
      const auto physical_delay = item.Number("critical_path_ns"),
                 wns = timing.Number("wns_ns");
      Check(physical_delay >= 0, "negative comparison critical path");
      if (!have) {
        worst = wns;
        have = true;
      } else
        worst = std::min(worst, wns);
      tns += timing.Number("tns_ns");
      failed += Count(timing, "failing_endpoints");
      constraints += Count(timing, "failing_endpoint_constraints");
      Doc closure(result.Token("closure"));
      unrouted += Count(closure, "unrouted_nets");
      drc += Count(closure, "drc_violations");
    }
  }
  Check(seen == names && active > 0,
        "no active FPGA or incomplete FPGA coverage");
  Check(!report.Has("physical_summary") ||
            report.Token("physical_summary") == "null",
        "embedded physical summaries are retired");
  Check(report.Text("physical_summary_ref") == "physical-summary.json",
        "summary reference disagrees");
  Raw summary{{"status", JsonQuote("pass")},
              {"fpgas", std::to_string(names.size())},
              {"active_fpgas", std::to_string(active)},
              {"inactive_fpgas", std::to_string(names.size() - active)},
              {"original_cells", std::to_string(original)},
              {"transport_cells", std::to_string(transport)},
              {"merged_cells", std::to_string(original + transport)},
              {"backend", JsonQuote(backend)},
              {"worst_critical_path_ns", Number(critical)}};
  double comparison_critical = 0;
  for (const auto &token : records) {
    Doc item(token);
    if (!item.Has("active") || item.Boolean("active"))
      comparison_critical =
          std::max(comparison_critical, item.Number("critical_path_ns"));
  }
  Raw metrics{{"worst_critical_path_ns", Number(comparison_critical)},
              {"worst_wns_ns", Number(worst)},
              {"total_tns_ns", Number(tns)},
              {"failing_endpoints", std::to_string(failed)},
              {"failing_endpoint_constraints", std::to_string(constraints)},
              {"unrouted_nets", std::to_string(unrouted)},
              {"drc_violations", std::to_string(drc)}};
  if (report.Has("summary"))
    Same(report.Token("summary"), Doc::Encode(summary),
         "published flow summary disagrees");
  return {Doc::Encode(summary), Doc::Encode(metrics)};
}
} // namespace emuflow::core
