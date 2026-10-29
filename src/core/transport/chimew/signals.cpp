#include "transport/chimew/signals.hpp"
#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/bounded_json.hpp"
#include "common/json_record_stream.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
namespace fs = std::filesystem;
void Check(bool valid, const std::string &why) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact, "Chimew signals: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "Chimew signals cancelled");
}
O Fields(const std::string &text) {
  O out;
  VisitJsonObjectTokens(text, [&](const auto &key, const auto &value) {
    Check(out.emplace(key, value).second, "duplicate record field");
  });
  return out;
}
std::string Gen(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value "
                        "END FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step() && !row.IsNull(0), "missing or excessive metadata " + key);
  const auto text = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return text;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".chimew-signals-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create workspace");
    root = name;
  }
  ~Workspace() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
class Signals {
public:
  const volatile std::sig_atomic_t *cancelled;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  std::optional<ArtifactRef> schedule, route, geometry;
  std::string design, platform, scope;
  double scale = 0;
  int regions = 0;
  I count = 0, paths = 0, exact_hops = 0, fallbacks = 0;
  std::map<std::string, std::array<I, 4>> bounds;
  std::map<std::pair<std::string, std::string>, std::array<I, 2>> pins;
  std::map<std::string, int> fpga_order;
  std::map<std::tuple<std::string, I, std::string>, std::string> wire_pins;
  double coordinate_scale = 0;
  explicit Signals(const volatile std::sig_atomic_t *flag) : cancelled(flag) {}
  ArtifactRef Bind(const std::string &path, const std::string &schema) {
    auto ref = ArtifactRef::Bind(path, schema);
    sources.push_back(ref);
    return ref;
  }
  ArtifactRef Selector(const ArtifactRef &ref, bool is_schedule) {
    D doc(D::Read(ref));
    const std::string logical =
        is_schedule ? "emuflow.tdm-schedule/v1" : "emuflow.system-routes/v1";
    Check(doc.Text("schema") == (is_schedule
                                     ? "emuflow.tdm-schedule-storage/v4"
                                     : "emuflow.system-route-storage/v1") &&
              doc.Text("logical_schema") == logical,
          "selector schema disagrees");
    const auto hash = doc.Text("store_sha256");
    Check(hash.size() == 64 &&
              hash.find_first_not_of("0123456789abcdef") == std::string::npos,
          "invalid selector seal");
    const auto name =
        std::string(is_schedule ? "schedule." : "routes.") + hash + ".sqlite";
    const fs::path member = doc.Text("artifact"),
                   path = fs::path(ref.path()).parent_path() / member;
    Check(member == name && !fs::is_symlink(path), "selector escapes owner");
    auto store = Bind(path.string(), is_schedule ? "tdm-schedule-store-v4"
                                                 : "system-route-store-v3");
    Check(doc.Integer("store_bytes") >= 0 &&
              std::uint64_t(doc.Integer("store_bytes")) ==
                  store.generation().bytes &&
              Sha256File(store, cancelled) == hash,
          "selector content seal disagrees");
    Check(doc.Integer("application_id") ==
                  (is_schedule ? 0x454d5554 : 0x454d5552) &&
              doc.Integer("user_version") == 3,
          "selector envelope disagrees");
    SqliteStore owner(store);
    auto &db = owner.database();
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(compare, doc.Token("logical_sha256"),
                          Meta(db, "logical_sha256"),
                          "Chimew source logical seal disagrees");
    if (is_schedule) {
      D summary(Meta(db, "native_publication_summary"));
      RequireEquivalentJson(compare, doc.Token("sections"),
                            summary.Token("sections"),
                            "Chimew schedule section seal disagrees");
    } else {
      RequireEquivalentJson(compare, doc.Token("header"), Meta(db, "header"),
                            "Chimew route header differs");
      RequireEquivalentJson(compare, doc.Token("sections"),
                            Meta(db, "sections"),
                            "Chimew route section seal disagrees");
    }
    return store;
  }
  void Read(const ArtifactRef &request) {
    sources = {request};
    D doc(D::Read(request));
    doc.Keys(
        {"schema", "sources", "weight_scale", "path_scope", "region_count"});
    Check(doc.Text("schema") == "emuflow.chimew-signal-input/v1",
          "request schema disagrees");
    scale = doc.Number("weight_scale");
    scope = doc.Text("path_scope");
    const auto n = doc.Integer("region_count");
    Check(scale >= 0, "timing weight scale is invalid");
    Check(scope == "exact-hop" || scope == "whole-net",
          "timing path scope is invalid");
    Check(n >= 2 && n <= 31, "region count must be in [2, 31]");
    regions = int(n);
    const O schemas{{"geometry", "chimew-geometry-selector-v1"},
                    {"schedule", "tdm-schedule-selector-v4"},
                    {"routes", "system-route-selector-v1"},
                    {"timing", "sta-path-selector-v1"}};
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    for (const auto &token : D::Array(doc.Token("sources"), 4)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      const auto role = row.Text("role");
      Check(schemas.count(role) && schemas.at(role) == row.Text("schema") &&
                fs::path(row.Text("path")).is_absolute(),
            "source role disagrees");
      auto ref = Bind(row.Text("path"), row.Text("schema"));
      RequireEquivalentJson(compare, row.Token("generation"), Gen(ref),
                            "Chimew signal source generation changed");
      Check(roles.emplace(role, ref).second, "duplicate source role");
    }
    Check(roles.count("geometry") && roles.count("schedule") &&
              roles.count("routes"),
          "required source is missing");
    schedule = Selector(roles.at("schedule"), true);
    route = Selector(roles.at("routes"), false);
    SqliteStore schedule_owner(*schedule), route_owner(*route);
    D sh(Meta(schedule_owner.database(), "header")),
        rh(Meta(route_owner.database(), "header"));
    design = sh.Text("design");
    platform = sh.Text("platform");
    Check(rh.Text("design") == design && rh.Text("platform") == platform,
          "route and schedule namespaces disagree");
    D selected(D::Read(roles.at("geometry")));
    Check(selected.Text("schema") == "emuflow.chimew-geometry-storage/v1" &&
              selected.Text("artifact") == "geometry.sqlite",
          "geometry selector disagrees");
    auto ref = Bind((fs::path(roles.at("geometry").path()).parent_path() /
                     "geometry.sqlite")
                        .string(),
                    "chimew-geometry-store-v1");
    Check(selected.Integer("store_bytes") >= 0 &&
              std::uint64_t(selected.Integer("store_bytes")) ==
                  ref.generation().bytes &&
              selected.Text("store_sha256") == Sha256File(ref, cancelled),
          "geometry content seal disagrees");
    geometry = ref;
    SqliteStore owner(ref);
    D header(Meta(owner.database(), "header"));
    RequireEquivalentJson(compare, header.text(), selected.Token("header"),
                          "Chimew geometry header differs");
    Check(header.Text("implementation_id") ==
              "native-indexed-xilinx-geometry-v1",
          "geometry producer disagrees");
    bool have_schedule = false;
    std::map<std::string, ArtifactRef> originals;
    for (const auto &token : D::Array(header.Token("input_artifacts"), 16384)) {
      D row(token);
      auto input = Bind(row.Text("path"), row.Text("schema"));
      RequireEquivalentJson(compare, row.Token("generation"), Gen(input),
                            "Chimew geometry source generation changed");
      if (input.schema() == "tdm-schedule-selector-v4") {
        Check(input.path() == roles.at("schedule").path() &&
                  input.generation() == roles.at("schedule").generation(),
              "geometry belongs to a different schedule");
        have_schedule = true;
      }
      if (input.schema() == "boarddb-v1" ||
          input.schema() == "hp-package-pin-inventory-v1" ||
          input.schema() == "fixed-wiring-csv-v1")
        Check(originals.emplace(input.schema(), input).second,
              "duplicate board geometry source");
    }
    Check(have_schedule && originals.count("boarddb-v1") &&
              originals.count("hp-package-pin-inventory-v1"),
          "geometry lacks its schedule/board owner");
    for (const auto &pair : Fields(header.Token("bounds"))) {
      const auto box = D::Array(pair.second, 4);
      Check(box.size() == 4, "invalid geometry bounds");
      std::array<I, 4> b{};
      for (int i = 0; i < 4; ++i)
        b[i] = D::IntegerToken(box[i]);
      Check(b[0] >= 0 && b[1] >= b[0] && b[2] >= 0 && b[3] >= b[2],
            "invalid native coordinate bounds");
      bounds.emplace(pair.first, b);
      coordinate_scale = std::max(coordinate_scale, double(b[1]) + 1);
    }
    for (const auto &token : D::Array(header.Token("pins"), 65536)) {
      const auto p = D::Array(token, 4);
      Check(p.size() == 4, "invalid fixed pin");
      Check(pins.emplace(std::make_pair(DecodeJsonString(p[0]),
                                        DecodeJsonString(p[1])),
                         std::array<I, 2>{D::IntegerToken(p[2]),
                                          D::IntegerToken(p[3])})
                .second,
            "duplicate fixed pin");
    }
    const auto normalized = NormalizeBoard(D::Read(originals.at("boarddb-v1")));
    D board(normalized);
    int order = 0;
    for (const auto &token : D::Array(board.Token("fpgas"), 4096)) {
      D fpga(token);
      const auto id = fpga.Text("id");
      Check(bounds.count(id) && fpga_order.emplace(id, order++).second,
            "board and geometry FPGA coverage differs");
    }
    Check(bounds.size() == fpga_order.size(),
          "geometry contains foreign FPGA bounds");
    D wires(BuildVirtualBoard(
        normalized, originals.at("hp-package-pin-inventory-v1"),
        originals.count("fixed-wiring-csv-v1")
            ? std::optional<ArtifactRef>(originals.at("fixed-wiring-csv-v1"))
            : std::nullopt,
        cancelled));
    VisitJsonArrayTokens(wires.Token("wires"), [&](const auto &token) {
      D wire(token);
      for (const auto &end : D::Array(wire.Token("endpoints"), 2)) {
        D e(end), p(e.Token("p"));
        Check(
            wire_pins
                .emplace(std::make_tuple(wire.Text("link"),
                                         wire.Integer("wire"), e.Text("fpga")),
                         p.Text("package_pin"))
                .second,
            "duplicate fixed wire endpoint");
      }
    });
  }
  void Attach(SqliteDatabase &db, const ArtifactRef &ref, const char *name) {
    auto row = db.Prepare(std::string("ATTACH DATABASE ? AS ") + name);
    row.BindText(1, SqliteImmutableUri(ref.path()));
    row.Execute();
  }
  void Weights(SqliteDatabase &db) {
    if (!roles.count("timing"))
      return;
    auto add = db.Prepare("INSERT INTO projected VALUES(?,?,?)");
    std::ifstream input(roles.at("timing").path(), std::ios::binary);
    Check(bool(input), "cannot read projected timing paths");
    JsonRecordStream stream(input, db, cancelled);
    std::string schema, path_design;
    bool have_paths = false;
    stream.Object([&](const std::string &key) {
      if (key == "schema")
        schema = DecodeJsonString(stream.Value());
      else if (key == "design")
        path_design = DecodeJsonString(stream.Value());
      else if (key == "paths") {
        have_paths = true;
        stream.Array([&](const std::string &record) {
          Stop(cancelled);
          auto row = Fields(record);
          Check(row.count("clock_period_ns") && row.count("slack_ns") &&
                    row.count("cut_nets"),
                "projected timing path fields are missing");
          const auto period = ParseFiniteDecimal(row.at("clock_period_ns"),
                                                 "Chimew timing period"),
                     slack = ParseFiniteDecimal(row.at("slack_ns"),
                                                "Chimew timing slack");
          Check(period > 0, "timing period is invalid");
          Check(!row.at("cut_nets").empty() &&
                    row.at("cut_nets").front() == '[',
                "cut nets must be an array");
          VisitJsonArrayTokens(row.at("cut_nets"), [&](const auto &net) {
            Check(!DecodeJsonString(net).empty(), "cut net is empty");
          });
          const auto normalized =
              row.count("normalized_slack") &&
                      row.at("normalized_slack") != "null"
                  ? ParseFiniteDecimal(row.at("normalized_slack"),
                                       "Chimew normalized slack")
                  : slack / period;
          Check(std::isfinite(normalized), "normalized slack is invalid");
          add.BindInteger(1, paths++);
          add.BindText(2, record);
          add.BindReal(3, normalized);
          add.Execute();
          add.Reset();
        });
      } else
        stream.Skip();
    });
    stream.Finish();
    Check(schema == "emuflow.sta-paths/v1" && path_design == design &&
              have_paths && paths > 0,
          "projected timing path schema/design/coverage disagrees");
    auto worst = db.Prepare("SELECT max(max(0.0,-normalized)) FROM projected");
    Check(worst.Step(), "missing timing normalization");
    const auto deficit = worst.TypedReal(0);
    auto path =
        db.Prepare("SELECT record_json,normalized FROM projected ORDER BY seq");
    auto transition = db.Prepare(
        "SELECT record_json FROM routed.timing_paths WHERE path_id=?");
    auto net_entries =
        db.Prepare("SELECT seq FROM signals WHERE net=? ORDER BY seq");
    auto edges =
        db.Prepare("SELECT e.source_fpga,e.sink_fpga,e.link_id FROM "
                   "routed.routes r JOIN routed.route_edges e ON "
                   "e.route_seq=r.seq WHERE r.net_id=? ORDER BY e.seq");
    auto exact = db.Prepare("SELECT seq FROM signals WHERE net=? AND "
                            "source_fpga=? AND sink_fpga=? AND link_id=?");
    auto update = db.Prepare(
        "UPDATE signals SET criticality=max(criticality,?) WHERE seq=?");
    while (path.Step()) {
      Stop(cancelled);
      const auto record = Fields(path.TypedText(0));
      const auto normalized = path.TypedReal(1);
      const auto value =
          deficit > 0
              ? std::max(0.0, -normalized) / deficit
              : std::max(
                    0.0,
                    std::min(1.0,
                             1.0 - ParseFiniteDecimal(record.at("slack_ns"),
                                                      "Chimew slack") /
                                       ParseFiniteDecimal(
                                           record.at("clock_period_ns"),
                                           "Chimew period")));
      std::set<I> selected;
      bool found = false;
      if (scope == "exact-hop" && record.count("id") &&
          record.at("id") != "null") {
        transition.BindText(1, DecodeJsonString(record.at("id")));
        if (transition.Step()) {
          found = true;
          const auto routed = Fields(transition.TypedText(0));
          if (routed.count("cut_transitions"))
            VisitJsonArrayTokens(
                routed.at("cut_transitions"), [&](const auto &token) {
                  D t(token);
                  const auto net = t.Text("net"), source = t.Text("from");
                  auto current = t.Text("to");
                  std::map<std::string, std::pair<std::string, std::string>>
                      parents;
                  edges.BindText(1, net);
                  while (edges.Step())
                    Check(parents
                              .emplace(edges.TypedText(1),
                                       std::make_pair(edges.TypedText(0),
                                                      edges.TypedText(2)))
                              .second,
                          "route has duplicate tree parents");
                  edges.Reset();
                  auto exists =
                      db.Prepare("SELECT 1 FROM routed.routes WHERE net_id=?");
                  exists.BindText(1, net);
                  Check(exists.Step(), "timing path route is absent");
                  std::set<std::string> visited;
                  while (current != source) {
                    Check(visited.insert(current).second,
                          "timing route contains a cycle");
                    const auto edge = parents.find(current);
                    Check(edge != parents.end(),
                          "route tree does not reach the timing target");
                    exact.BindText(1, net);
                    exact.BindText(2, edge->second.first);
                    exact.BindText(3, current);
                    exact.BindText(4, edge->second.second);
                    Check(exact.Step(),
                          "timing hop does not identify one schedule entry");
                    const auto seq = exact.TypedInteger(0);
                    Check(!exact.Step(),
                          "timing hop identifies multiple schedule entries");
                    exact.Reset();
                    selected.insert(seq);
                    current = edge->second.first;
                  }
                });
          Check(!transition.Step(), "duplicate route timing path");
        }
        transition.Reset();
      }
      if (found)
        exact_hops += selected.size();
      else {
        ++fallbacks;
        VisitJsonArrayTokens(record.at("cut_nets"), [&](const auto &token) {
          net_entries.BindText(1, DecodeJsonString(token));
          while (net_entries.Step())
            selected.insert(net_entries.TypedInteger(0));
          net_entries.Reset();
        });
      }
      for (auto seq : selected) {
        update.BindReal(1, value);
        update.BindInteger(2, seq);
        update.Execute();
        update.Reset();
      }
    }
    auto weights =
        db.Prepare("SELECT seq,criticality FROM signals ORDER BY seq");
    auto set = db.Prepare(
        "UPDATE signals SET timing_weight=?,protected=? WHERE seq=?");
    while (weights.Step()) {
      const double weight = 1.0 + scale * std::pow(weights.TypedReal(1), 2.0);
      Check(std::isfinite(weight), "timing weight is not finite");
      const bool guard =
          weight > 1.0 && std::abs(weight - 1.0) >
                              std::max(1e-12, 1e-9 * std::max(weight, 1.0));
      set.BindReal(1, weight);
      set.BindInteger(2, guard);
      set.BindInteger(3, weights.TypedInteger(0));
      set.Execute();
      set.Reset();
    }
  }
  void Build(SqliteDatabase &db) {
    db.Execute(
        "PRAGMA application_id=0x454d4353;PRAGMA user_version=1;PRAGMA "
        "temp_store=FILE;PRAGMA cache_size=-16384;CREATE TABLE metadata(key "
        "TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;CREATE TABLE "
        "signals(seq INTEGER PRIMARY KEY,entry_id TEXT NOT NULL UNIQUE,net "
        "TEXT NOT NULL,link_id TEXT NOT NULL,source_fpga TEXT NOT "
        "NULL,sink_fpga TEXT NOT NULL,lane INTEGER NOT NULL,slot INTEGER NOT "
        "NULL,tdm_ratio INTEGER NOT NULL,source_x REAL NOT NULL,source_y REAL "
        "NOT NULL,sink_x REAL NOT NULL,sink_y REAL NOT NULL,source_slls TEXT "
        "NOT NULL,sink_slls TEXT NOT NULL,encoding INTEGER NOT "
        "NULL,criticality REAL NOT NULL DEFAULT 0,timing_weight REAL NOT NULL "
        "DEFAULT 1,protected INTEGER NOT NULL DEFAULT 0);CREATE INDEX "
        "signals_net ON signals(net);CREATE INDEX signals_hop ON "
        "signals(net,source_fpga,sink_fpga,link_id);CREATE TEMP TABLE "
        "projected(seq INTEGER PRIMARY KEY,record_json TEXT NOT "
        "NULL,normalized REAL NOT NULL);BEGIN");
    Attach(db, *schedule, "scheduled");
    Attach(db, *geometry, "geometric");
    Attach(db, *route, "routed");
    auto rows = db.Prepare(
        "SELECT "
        "e.seq,e.entry_id,e.net,e.link_id,e.source_fpga,e.sink_fpga,e.lane,e."
        "slot,e.tdm_ratio,t.x,t.y,t.normal_y,r.x,r.y,r.normal_y FROM "
        "scheduled.schedule_entries e LEFT JOIN geometric.points t ON "
        "t.entry_id=e.entry_id AND t.kind='tx' AND t.fpga=e.source_fpga AND "
        "t.net=e.net LEFT JOIN geometric.points r ON r.entry_id=e.entry_id AND "
        "r.kind='rx' AND r.fpga=e.sink_fpga AND r.net=e.net ORDER BY e.seq");
    auto add = db.Prepare(
        "INSERT INTO "
        "signals(seq,entry_id,net,link_id,source_fpga,sink_fpga,lane,slot,tdm_"
        "ratio,source_x,source_y,sink_x,sink_y,source_slls,sink_slls,encoding) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
    while (rows.Step()) {
      Stop(cancelled);
      Check(rows.TypedInteger(0) == count, "schedule order is incomplete");
      for (int i = 9; i < 15; ++i)
        Check(!rows.IsNull(i), "geometry does not cover scheduled endpoint");
      const auto id = rows.TypedText(1), link = rows.TypedText(3),
                 a = rows.TypedText(4), b = rows.TypedText(5);
      Check(!id.empty() && fpga_order.count(a) && fpga_order.count(b) && a != b,
            "scheduled endpoint identity is invalid");
      Check(rows.TypedInteger(6) >= 0 && rows.TypedInteger(7) >= 0 &&
                !rows.IsNull(8) && rows.TypedInteger(8) > 0,
            "explicit Phase5 lane/slot/ratio is invalid");
      std::array<std::string, 2> crossed;
      std::uint64_t encoding = 0;
      for (int side = 0; side < 2; ++side) {
        const auto fpga = side ? b : a;
        const auto wire = wire_pins.find({link, rows.TypedInteger(6), fpga});
        Check(wire != wire_pins.end(), "scheduled lane lacks a fixed wire");
        const auto pin = pins.find({fpga, wire->second});
        Check(pin != pins.end(), "fixed channel pin lacks geometry");
        const auto box = bounds.at(fpga);
        const double anchor = (pin->second[1] - box[2]) /
                              double(std::max<I>(1, box[3] - box[2])),
                     position = rows.TypedReal(side ? 14 : 11);
        std::vector<JsonRecord> slls;
        for (int i = 0; i < regions - 1; ++i) {
          const double boundary = double(i + 1) / regions;
          if (std::min(position, anchor) < boundary &&
              boundary <= std::max(position, anchor)) {
            slls.emplace_back(i);
            encoding |= std::uint64_t(1) << (side * (regions - 1) + i);
          }
        }
        crossed[side] = JsonRecord(slls).Encode();
      }
      for (int i = 0; i < 9; ++i) {
        if (i == 0 || i >= 6)
          add.BindInteger(i + 1, rows.TypedInteger(i));
        else
          add.BindText(i + 1, rows.TypedText(i));
      }
      add.BindReal(10, rows.TypedReal(9) + fpga_order.at(a) * coordinate_scale);
      add.BindReal(11, rows.TypedReal(10));
      add.BindReal(12,
                   rows.TypedReal(12) + fpga_order.at(b) * coordinate_scale);
      add.BindReal(13, rows.TypedReal(13));
      add.BindText(14, crossed[0]);
      add.BindText(15, crossed[1]);
      add.BindInteger(16, encoding);
      add.Execute();
      add.Reset();
      ++count;
    }
    D manifest(D::Read(roles.at("schedule"))),
        sections(manifest.Token("sections")),
        section(sections.Token("entries"));
    Check(count > 0 && count == section.Integer("records"),
          "schedule entry coverage disagrees");
    auto points = db.Prepare("SELECT count(*) FROM geometric.points");
    Check(points.Step() && points.TypedInteger(0) == 2 * count,
          "geometry has missing or extra endpoints");
    Weights(db);
    std::string input_refs = "[";
    for (std::size_t i = 1; i < sources.size(); ++i) {
      if (i > 1)
        input_refs += ',';
      input_refs += D::Encode({{"path", JsonQuote(sources[i].path())},
                               {"schema", JsonQuote(sources[i].schema())},
                               {"generation", Gen(sources[i])}});
    }
    input_refs += ']';
    const auto header =
        D::Encode({{"schema", JsonQuote("emuflow.chimew-signals/v1")},
                   {"input_artifacts", input_refs},
                   {"implementation_id",
                    JsonQuote("native-chimew-signal-preparation-v1")},
                   {"design", JsonQuote(design)},
                   {"platform", JsonQuote(platform)},
                   {"signals", std::to_string(count)},
                   {"weighted", roles.count("timing") ? "true" : "false"},
                   {"weight_scale", JsonRecord(scale).Encode()},
                   {"path_scope", JsonQuote(scope)},
                   {"region_count", std::to_string(regions)},
                   {"exact_path_hops", std::to_string(exact_hops)},
                   {"whole_net_fallbacks", std::to_string(fallbacks)}});
    auto meta = db.Prepare("INSERT INTO metadata VALUES('header',?)");
    meta.BindText(1, header);
    meta.Execute();
    db.Execute("COMMIT");
  }
};
} // namespace
StageResult PrepareChimewSignals(const ArtifactRef &request,
                                 const std::string &directory,
                                 const volatile std::sig_atomic_t *cancelled) {
  Signals signals(cancelled);
  std::optional<Workspace> work;
  const auto target = fs::absolute(directory);
  return RunStage(
      "phase6.prepare-chimew-signals",
      [&] {
        Check(request.schema() == "chimew-signal-input-v1",
              "request role disagrees");
        signals.Read(request);
        Check(!fs::exists(target) && !fs::is_symlink(target),
              "output directory already exists");
        fs::create_directories(target.parent_path());
        work.emplace(target.parent_path());
        return signals.sources;
      },
      [&](const auto &sources) {
        std::string header;
        ArtifactRef result = [&] {
          SqliteArtifactWriter writer((work->root / "signals.sqlite").string(),
                                      "chimew-signal-store-v1", sources);
          signals.Build(writer.database());
          header = Meta(writer.database(), "header");
          return writer.Commit();
        }();
        const auto selector = D::Encode(
            {{"schema", JsonQuote("emuflow.chimew-signal-storage/v1")},
             {"artifact", JsonQuote("signals.sqlite")},
             {"store_bytes", std::to_string(result.generation().bytes)},
             {"store_sha256", JsonQuote(Sha256File(result, cancelled))},
             {"header", header}});
        PublishTextArtifact(
            (work->root / "signals.json").string(), sources,
            [&](std::ostream &out) { out << selector << '\n'; });
        Stop(cancelled);
        PublishNewDirectory(work->root.string(), target.string(), sources);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind((target / "signals.sqlite").string(),
                              "chimew-signal-store-v1"),
            ArtifactRef::Bind((target / "signals.json").string(),
                              "chimew-signal-selector-v1")};
      });
}
} // namespace emuflow::core
