#include "frontend/report.hpp"
#include "common/artifact_io.hpp"
#include "common/json_record.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <limits>
#include <set>
namespace emuflow::core {
namespace fs = std::filesystem;
namespace {
using Fields = std::map<std::string, std::string>;
void Check(bool ok, const std::string &message) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "frontend " + message);
}
std::string Encode(const Fields &fields) {
  std::string text = "{";
  for (const auto &field : fields) {
    if (text.size() > 1)
      text += ',';
    text += JsonQuoteUtf8(field.first) + ":" + field.second;
  }
  return text + '}';
}
std::string Array(const std::vector<std::string> &values) {
  std::string text = "[";
  for (const auto &value : values) {
    if (text.size() > 1)
      text += ',';
    text += JsonQuoteUtf8(value);
  }
  return text + ']';
}
void ValidJson(SqliteDatabase &db, const std::string &text,
               const std::string &kind) {
  auto row = db.Prepare("SELECT json_valid(?1),json_type(?1)");
  row.BindText(1, text);
  Check(row.Step() && row.Integer(0) && row.Text(1) == kind,
        "invalid " + kind + " control document");
  auto duplicate =
      db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP "
                 "BY parent,key HAVING count(*)>1 LIMIT 1");
  duplicate.BindText(1, text);
  Check(!duplicate.Step(), "duplicate control key");
}
std::string Field(SqliteDatabase &db, const std::string &text,
                  const std::string &key, const std::string &kind) {
  auto row = db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
  row.BindText(1, text);
  row.BindText(2, "$." + key);
  Check(row.Step() && row.Text(0) == kind, "invalid control field " + key);
  return row.TypedText(1);
}
std::string Metadata(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT value FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), "missing metadata " + key);
  auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return value;
}
struct ClockFacts {
  std::uint64_t nets = 0, fabric = 0, ffs = 0, maximum = 0;
};
// One ordered endpoint join; no Python endpoint population or
// one-query-per-pin.
ClockFacts VisitClocks(
    SqliteDatabase &db,
    const std::function<void(const std::string &,
                             const std::vector<std::string> &, std::uint64_t)>
        &visit) {
  auto rows = db.Prepare(R"SQL(WITH clocked AS (
    SELECT e.net_seq,COUNT(*) AS sinks FROM endpoints e JOIN instances i ON i.id=e.instance_id
    WHERE e.role=1 AND (((substr(i.type,1,2)='FD' OR substr(i.type,1,6)='$_DFF_') AND e.port='C')
      OR (i.type IN ('VTR_SP_RAM','VTR_DP_RAM') AND e.port='clk')) GROUP BY e.net_seq),
  drivers AS (SELECT e.net_seq,e.instance_id FROM endpoints e JOIN instances i ON i.id=e.instance_id
    WHERE e.role=0 AND (substr(i.type,1,3)='LUT' OR i.type='$lut'))
  SELECT c.net_seq,n.id,c.sinks,d.instance_id FROM clocked c JOIN nets n ON n.seq=c.net_seq
  LEFT JOIN drivers d ON d.net_seq=c.net_seq ORDER BY c.net_seq,d.instance_id)SQL");
  ClockFacts result;
  std::int64_t current = -1;
  std::string name;
  std::uint64_t count = 0;
  std::vector<std::string> drivers;
  const auto finish = [&] {
    if (current < 0)
      return;
    ++result.nets;
    if (!drivers.empty()) {
      ++result.fabric;
      result.ffs += count;
      result.maximum = std::max(result.maximum, count);
      if (visit)
        visit(name, drivers, count);
    }
  };
  while (rows.Step()) {
    if (rows.TypedInteger(0) != current) {
      finish();
      current = rows.TypedInteger(0);
      name = rows.TypedText(1);
      count = rows.TypedInteger(2);
      drivers.clear();
    }
    if (!rows.IsNull(3))
      drivers.push_back(rows.TypedText(3));
  }
  finish();
  return result;
}
} // namespace
std::string BuildFrontendReport(const fs::path &root,
                                const FrontendReportConfiguration &c,
                                const std::string &importer,
                                const ToolResult *tool) {
  SqliteDatabase control(":memory:", SqliteDatabase::Mode::kReadOnly);
  ValidJson(control, c.platform, "object");
  ValidJson(control, c.synthesis, "object");
  ValidJson(control, importer, "object");
  const auto platform = Field(control, c.platform, "platform.name", "text");
  SqliteStore store(ArtifactRef::Bind((root / "design.emuir.sqlite").string(),
                                      "emuir-compact-v2"));
  auto &db = store.database();
  const auto design =
      Field(control, Metadata(db, "design_json"), "name", "text");
  auto warnings = Metadata(db, "warnings_json");
  ValidJson(control, warnings, "array");
  Fields totals;
  std::map<std::string, std::int64_t> amounts;
  auto resources = db.Prepare(
      "SELECT r.key,SUM(r.value*g.amount) FROM (SELECT resources_seq,COUNT(*) "
      "AS amount FROM cell_records GROUP BY resources_seq) g JOIN payloads p "
      "ON p.seq=g.resources_seq,json_each(p.value) r GROUP BY r.key");
  while (resources.Step()) {
    auto name = resources.TypedText(0);
    auto amount = resources.TypedInteger(1);
    Check(amount >= 0 &&
              std::find(kResourceFields.begin(), kResourceFields.end(), name) !=
                  kResourceFields.end(),
          "invalid resource total");
    amounts[name] = amount;
    if (amount)
      totals[name] = std::to_string(amount);
  }
  Fields classes;
  auto counts =
      db.Prepare("SELECT cut_class,COUNT(*) FROM nets GROUP BY cut_class");
  while (counts.Step())
    classes[counts.TypedText(0)] = std::to_string(counts.TypedInteger(1));
  std::map<std::string, std::int64_t> aggregate;
  Fields fits;
  bool single = false;
  auto fpgas =
      control.Prepare("SELECT "
                      "json_extract(value,'$.id'),json_extract(value,'$."
                      "effective_capacity') FROM json_each(?1,'$.fpgas')");
  fpgas.BindText(1, c.platform);
  while (fpgas.Step()) {
    auto id = fpgas.TypedText(0), capacity = fpgas.TypedText(1);
    Check(!id.empty() && !fits.count(id), "invalid FPGA catalog");
    ValidJson(control, capacity, "object");
    std::map<std::string, std::int64_t> available;
    auto values = control.Prepare("SELECT key,type,value FROM json_each(?)");
    values.BindText(1, capacity);
    while (values.Step()) {
      const auto name = values.TypedText(0);
      Check(values.Text(1) == "integer" &&
                std::find(kResourceFields.begin(), kResourceFields.end(),
                          name) != kResourceFields.end(),
            "invalid effective capacity");
      const auto count = values.TypedInteger(2);
      Check(count >= 0 && aggregate[name] <= INT64_MAX - count,
            "capacity overflow");
      available[name] = count;
      aggregate[name] += count;
    }
    bool fit = true;
    for (const auto &item : amounts)
      fit = fit && item.second <= available[item.first];
    fits[id] = fit ? "true" : "false";
    single = single || fit;
  }
  Check(!fits.empty(), "empty FPGA catalog");
  bool fits_platform = true;
  for (const auto &item : amounts)
    fits_platform =
        fits_platform &&
        item.second <=
            (aggregate.count(item.first) ? aggregate.at(item.first) : 0);
  Fields aggregate_json;
  for (const auto &item : aggregate)
    aggregate_json[item.first] = std::to_string(item.second);
  const auto clock = VisitClocks(db, {});
  Check(fits_platform, "capacity check failed");
  Check(!c.require_no_fabric_clock || !clock.fabric,
        "clock topology check failed");
  const auto normalization =
      Field(control, importer, "normalization", "object");
  // The source-to-compact producer and its independent source cursor must both
  // have succeeded before any report or public directory is committed.
  Check(Field(control, importer, "status", "text") == "pass" &&
            Field(control, Field(control, importer, "validation", "object"),
                  "status", "text") == "pass",
        "source audit failed");
  auto native_import =
      "{\"implementation_id\":\"native-yosys-emuir-v1\",\"records\":" +
      Field(control, importer, "emuir_records", "object") +
      ",\"validation\":" + Field(control, importer, "validation", "object") +
      "}";
  auto transform = normalization;
  auto update = control.Prepare(
      "SELECT "
      "json_set(?1,'$.input_sha256',json(?4),'$.hierarchical_interchange',json("
      "?2),'$.native_emuir_import',json(?3))");
  update.BindText(1, transform);
  update.BindText(4, c.input_sha256.empty() ? "null"
                                            : JsonQuoteUtf8(c.input_sha256));
  update.BindText(
      2, Field(control, importer, "hierarchical_interchange", "object"));
  update.BindText(3, native_import);
  Check(update.Step(), "cannot construct normalization report");
  transform = update.TypedText(0);
  auto synthesis = c.synthesis;
  if (tool) {
    auto synth =
        control.Prepare("SELECT "
                        "json_set(?1,'$.tool_report.normalization',json(?2),'$."
                        "tool_report.primitive_audit',json(?3))");
    synth.BindText(1, synthesis);
    synth.BindText(2, transform);
    synth.BindText(3,
                   Field(control, normalization, "primitive_audit", "object"));
    Check(synth.Step(), "cannot construct synthesis report");
    synthesis = synth.TypedText(0);
  }
  Fields summary{{"schema", JsonQuoteUtf8("emuflow.phase1-report/v1")},
                 {"phase", "1"},
                 {"status", JsonQuoteUtf8("pass")},
                 {"design", JsonQuoteUtf8(design)},
                 {"platform", JsonQuoteUtf8(platform)},
                 {"synthesis", synthesis}};
  const auto summary_text = Encode(summary);
  Check(summary_text.size() <= 512 * 1024, "frontend summary exceeds bound");
  Fields report = summary;
  report["resource_totals"] = Encode(totals);
  report["fits_on_fpga"] = Encode(fits);
  report["fits_on_platform"] = "true";
  report["fit_scope"] =
      JsonQuoteUtf8(single ? "single_fpga" : "aggregate_platform");
  report["aggregate_effective_capacity"] = Encode(aggregate_json);
  report["cut_classes"] = Encode(classes);
  report["require_no_fabric_clock"] =
      c.require_no_fabric_clock ? "true" : "false";
  report["warnings"] = warnings;
  report["indexed_staging_transform"] = transform;
  report["artifacts"] =
      "{\"emuir\":\"design.emuir.sqlite\",\"platform\":\"platform.normalized."
      "json\",\"report\":\"phase1_report.json\"}";
  // Details remain a streamed derived report. They are never deserialized by
  // the controller or carried to a subsequent phase.
  PublishTextArtifact(
      (root / "phase1_report.json").string(), c.sources,
      [&](std::ostream &out) {
        out << "{\"clock_topology\":{\"status\":"
            << JsonQuoteUtf8(clock.fabric ? "unsafe" : "pass")
            << ",\"ff_clock_nets\":" << clock.nets
            << ",\"fabric_logic_clock_nets\":" << clock.fabric
            << ",\"fabric_logic_clocked_ffs\":" << clock.ffs
            << ",\"maximum_fabric_clock_fanout\":" << clock.maximum
            << ",\"fabric_clock_details\":[";
        bool first = true;
        VisitClocks(db, [&](const auto &name, const auto &drivers, auto count) {
          if (!first)
            out << ',';
          first = false;
          out << "{\"net\":" << JsonQuoteUtf8(name)
              << ",\"lut_drivers\":" << Array(drivers)
              << ",\"clocked_ffs\":" << count << '}';
        });
        out << "]}";
        for (const auto &field : report)
          out << ',' << JsonQuoteUtf8(field.first) << ':' << field.second;
        out << "}\n";
      });
  PublishTextArtifact((root / "platform.normalized.json").string(), c.sources,
                      [&](std::ostream &out) { out << c.platform << '\n'; });
  PublishTextArtifact((root / "frontend-summary.json").string(), c.sources,
                      [&](std::ostream &out) { out << summary_text << '\n'; });
  if (tool)
    PublishToolResult((root / "yosys-execution.json").string(), *tool);
  return summary_text;
}
} // namespace emuflow::core
