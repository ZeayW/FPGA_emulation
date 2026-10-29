#include "transport/chimew/geometry.hpp"
#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "physical/device.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <list>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
namespace fs = std::filesystem;
constexpr const char *coordinates = "native-device-tile-column-row-v1";
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "Chimew geometry: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "Chimew geometry cancelled");
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
O Fields(const std::string &raw) {
  O fields;
  VisitJsonObjectTokens(raw, [&](const auto &key, const auto &value) {
    Check(fields.emplace(key, value).second, "duplicate bounded record field");
  });
  return fields;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT value FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), "missing source metadata " + key);
  auto text = row.TypedText(0);
  Check(text.size() <= 8 * 1024 * 1024 && !row.Step(),
        "invalid source metadata");
  return text;
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto name = (parent / ".chimew-geometry-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create private output directory");
    root = name;
  }
  ~Work() {
    std::error_code error;
    fs::remove_all(root, error);
  }
};
struct PhysicalOwner {
  std::unique_ptr<SqliteStore> physical;
  bool vivado = false;
  std::array<I, 4> bounds{};
  std::optional<SqliteStatement> source, boundary;
  std::vector<std::string> lookup_plan;
  std::string Location(const std::string &instance, int axis) const {
    if (vivado)
      return "SELECT avg(s.tile_" + std::string(axis ? "row" : "column") +
             ") FROM mapped.cells cell JOIN objects o ON o.origin=cell.seq AND "
             "o.stage='routed'"
             " JOIN sites s ON s.seq=o.site_seq WHERE cell.id=" +
             instance;
    return "SELECT min(s.physical_" + std::string(axis ? "y" : "x") +
           ") FROM assignment a JOIN cluster pc ON pc.ordinal=a.cluster_ordinal"
           " JOIN geometry.physical_sites p ON p.physical_site=pc.site"
           " JOIN geometry.sites s ON s.dense_x=p.dense_x AND "
           "s.dense_y=p.dense_y WHERE a.instance=" +
           instance +
           " GROUP BY a.instance HAVING min(s.physical_x)=max(s.physical_x)"
           " AND min(s.physical_y)=max(s.physical_y)";
  }
  void Prepare() {
    auto &db = physical->database();
    auto plan = db.Prepare("EXPLAIN QUERY PLAN " + Location("?1", 0));
    plan.BindText(1, "selected-instance");
    while (plan.Step())
      lookup_plan.push_back(JsonQuote(plan.TypedText(3)));
    source.emplace(db.Prepare(
        "SELECT avg((" + Location("c.id", 0) + ")),avg((" +
        Location("c.id", 1) +
        ")) FROM source.net_records n JOIN source.endpoint_records e ON "
        "e.net_seq=n.seq AND e.role=?1"
        " JOIN source.cell_records c ON c.seq=e.instance_seq WHERE n.id=?2"));
    boundary.emplace(db.Prepare("SELECT avg((" + Location("b.instance", 0) +
                                ")),avg((" + Location("b.instance", 1) +
                                ")),count((" + Location("b.instance", 0) +
                                ")),count(*) FROM boundary.endpoints e"
                                " JOIN boundary.boundary_registers b ON "
                                "b.endpoint_id=e.id WHERE e.schedule_entry=?1"
                                " AND e.kind=?2 AND e.logical_net=?3"));
  }
};
class Geometry {
public:
  const volatile std::sig_atomic_t *cancelled;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  std::map<std::string, PhysicalOwner> owners;
  std::map<std::string, std::array<I, 4>> bounds;
  std::map<std::pair<std::string, std::string>, std::pair<I, I>> pins;
  std::vector<std::string> physical_sources;
  std::string architecture, architecture_sha, platform;
  std::optional<ArtifactRef> schedule;
  std::uint64_t forwarded = 0, hits = 0, queries = 0, site_rechecks = 0,
                peak = 0;
  explicit Geometry(const volatile std::sig_atomic_t *flag) : cancelled(flag) {}
  ArtifactRef Bind(const std::string &path, const std::string &schema) {
    auto ref = ArtifactRef::Bind(path, schema);
    sources.push_back(ref);
    return ref;
  }
  ArtifactRef Sealed(const std::string &token, const std::string &schema) {
    D row(token);
    auto ref = Bind(row.Text("path"), schema);
    Check(row.Text("sha256") == Hash(ref),
          "placement/source identity seal disagrees");
    return ref;
  }
  std::string Hash(const ArtifactRef &ref) {
    const auto key = Generation(ref);
    auto found = hashes.find(key);
    if (found != hashes.end())
      return found->second;
    return hashes.emplace(key, Sha256File(ref, cancelled)).first->second;
  }
  ArtifactRef Selector(const ArtifactRef &source, const std::string &schema,
                       const std::string &logical, const std::string &storage) {
    D doc(D::Read(source));
    Check(doc.Text("schema") == storage &&
              doc.Text("logical_schema") == logical,
          "selector version disagrees");
    const fs::path member = doc.Text("artifact");
    Check(!member.empty() && member == member.filename() && member != "." &&
              member != "..",
          "selector escapes its owner");
    const auto digest = doc.Text("store_sha256");
    Check(digest.size() == 64 &&
              digest.find_first_not_of("0123456789abcdef") == std::string::npos,
          "invalid selector content seal");
    const auto name = schema == "boundary-identity-store-v1"
                          ? fs::path(source.path()).filename().string() +
                                ".store." + digest + ".sqlite"
                          : "schedule." + digest + ".sqlite";
    const auto candidate = fs::path(source.path()).parent_path() / member;
    Check(member == name && !fs::is_symlink(candidate),
          "selector artifact owner disagrees");
    auto ref =
        Bind((fs::path(source.path()).parent_path() / member).string(), schema);
    Check(ref.generation().bytes == std::uint64_t(doc.Integer("store_bytes")) &&
              Hash(ref) == doc.Text("store_sha256"),
          "selector file seal disagrees");
    SqliteStore store(ref);
    auto &db = store.database();
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    if (schema == "boundary-identity-store-v1") {
      for (const auto *key : {"header", "counts", "logical_sha256"})
        RequireEquivalentJson(compare, doc.Token(key), Meta(db, key),
                              "Chimew boundary selector metadata disagrees");
      Check(doc.Integer("application_id") == 0x454d5542 &&
                doc.Integer("user_version") == 1,
            "boundary selector envelope disagrees");
    } else {
      Check(doc.Integer("application_id") == 0x454d5554 &&
                doc.Integer("user_version") == 3,
            "schedule selector envelope disagrees");
      RequireEquivalentJson(compare, doc.Token("logical_sha256"),
                            Meta(db, "logical_sha256"),
                            "Chimew schedule logical seal disagrees");
      D published(Meta(db, "native_publication_summary"));
      RequireEquivalentJson(compare, doc.Token("sections"),
                            published.Token("sections"),
                            "Chimew schedule publication disagrees");
    }
    source.RequireUnchanged();
    return ref;
  }
  void Attach(SqliteDatabase &db, const ArtifactRef &ref,
              const std::string &alias) {
    auto row = db.Prepare("ATTACH DATABASE ? AS " + alias);
    row.BindText(1, SqliteImmutableUri(ref.path()));
    row.Execute();
    ref.RequireUnchanged();
  }
  void Read(const ArtifactRef &input) {
    sources = {input};
    D doc(D::Read(input));
    doc.Keys({"schema", "sources", "package_geometry", "physical_report"});
    Check(doc.Text("schema") == "emuflow.chimew-geometry-input/v1",
          "input version disagrees");
    const O schemas{{"board", "boarddb-v1"},
                    {"original", "emuir-compact-v2"},
                    {"architecture", "physical-input-document-v1"},
                    {"schedule", "tdm-schedule-selector-v4"},
                    {"inventory", "hp-package-pin-inventory-v1"},
                    {"wiring", "fixed-wiring-csv-v1"}};
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    for (const auto &token : D::Array(doc.Token("sources"), 6)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      const auto role = row.Text("role");
      Check(schemas.count(role) && row.Text("schema") == schemas.at(role) &&
                fs::path(row.Text("path")).is_absolute(),
            "input source role disagrees");
      auto ref = Bind(row.Text("path"), schemas.at(role));
      RequireEquivalentJson(compare, row.Token("generation"), Generation(ref),
                            "Chimew input generation changed");
      Check(roles.emplace(role, ref).second, "duplicate input role");
    }
    for (const auto *role :
         {"board", "original", "architecture", "schedule", "inventory"})
      Check(roles.count(role), std::string("missing source ") + role);
    const auto normalized = NormalizeBoard(D::Read(roles.at("board")));
    D board(normalized);
    D board_platform(board.Token("platform"));
    platform = board_platform.Text("name");
    std::map<std::string, std::string> parts;
    for (const auto &token : D::Array(board.Token("fpgas"), 4096)) {
      D fpga(token);
      Check(parts.emplace(fpga.Text("id"), fpga.Text("part")).second,
            "duplicate FPGA");
    }
    Check(!parts.empty(), "empty board");
    D catalog(doc.Token("package_geometry"));
    catalog.Keys({"schema", "path", "part", "generation", "sha256", "pins"});
    Check(catalog.Text("schema") ==
              "emuflow.xilinx-package-geometry-control/v1",
          "package catalog version disagrees");
    architecture = roles.at("architecture").path();
    architecture_sha = catalog.Text("sha256");
    Check(fs::canonical(catalog.Text("path")) == architecture,
          "package catalog belongs to a different architecture");
    RequireEquivalentJson(compare, catalog.Token("generation"),
                          Generation(roles.at("architecture")),
                          "Chimew package geometry generation changed");
    Check(architecture_sha.size() == 64 &&
              architecture_sha.find_first_not_of("0123456789abcdef") ==
                  std::string::npos,
          "invalid architecture seal");
    for (const auto &part : parts)
      Check(part.second == catalog.Text("part"),
            "package catalog part differs from board");
    std::map<std::string, std::tuple<std::string, I, I>> package;
    for (const auto &pin : Fields(catalog.Token("pins"))) {
      auto point = D::Array(pin.second, 3);
      Check(point.size() == 3 && !pin.first.empty(), "invalid package pin");
      const auto site = DecodeJsonString(point[0]);
      const auto x = D::IntegerToken(point[1]), y = D::IntegerToken(point[2]);
      Check(!site.empty() && x >= 0 && y >= 0,
            "invalid native package coordinate");
      package.emplace(pin.first, std::make_tuple(site, x, y));
    }
    Check(!package.empty() && package.size() <= 16384,
          "package catalog exceeds device bounds");
    auto wires = Fields(BuildVirtualBoard(
        normalized, roles.at("inventory"),
        roles.count("wiring") ? std::optional<ArtifactRef>(roles.at("wiring"))
                              : std::nullopt,
        cancelled));
    VisitJsonArrayTokens(wires.at("wires"), [&](const auto &token) {
      D wire(token);
      for (const auto &end_token : D::Array(wire.Token("endpoints"), 2)) {
        D end(end_token);
        const auto fpga = end.Text("fpga");
        Check(parts.count(fpga), "unknown wire owner");
        for (const auto *polarity : {"p", "n"}) {
          D pin(end.Token(polarity));
          auto found = package.find(pin.Text("package_pin"));
          Check(found != package.end() &&
                    std::get<0>(found->second) == pin.Text("site"),
                "device geometry differs from fixed P/N wiring");
          pins[{fpga, pin.Text("package_pin")}] = std::make_pair(
              std::get<1>(found->second), std::get<2>(found->second));
        }
      }
    });
    SqliteStore original(roles.at("original"));
    schedule =
        Selector(roles.at("schedule"), "tdm-schedule-store-v4",
                 "emuflow.tdm-schedule/v1", "emuflow.tdm-schedule-storage/v4");
    std::set<std::string> seen;
    D report(doc.Token("physical_report"));
    for (const auto &token : D::Array(report.Token("fpgas"), 4096)) {
      Stop(cancelled);
      D record(token);
      const auto fpga = record.Text("fpga");
      Check(parts.count(fpga) && seen.insert(fpga).second &&
                record.Text("status") == "pass",
            "physical prepass FPGA coverage is invalid");
      std::array<I, 4> box{INT64_MAX, -1, INT64_MAX, -1};
      for (const auto &pin : pins)
        if (pin.first.first == fpga) {
          box[0] = std::min(box[0], pin.second.first);
          box[1] = std::max(box[1], pin.second.first);
          box[2] = std::min(box[2], pin.second.second);
          box[3] = std::max(box[3], pin.second.second);
        }
      Check(box[1] >= 0, "FPGA lacks fixed pin geometry");
      if (record.Has("active") && !record.Boolean("active")) {
        bounds.emplace(fpga, box);
        physical_sources.push_back(
            D::Encode({{"fpga", JsonQuote(fpga)}, {"active", "false"}}));
        continue;
      }
      D stages(record.Token("stages"));
      D placement(stages.Token("placement_ir"));
      D boundary(placement.Token("boundary_identity"));
      auto boundary_selector =
          Bind(boundary.Text("output"), "boundary-identity-selector-v1");
      auto identity = Selector(boundary_selector, "boundary-identity-store-v1",
                               "emuflow.boundary-identity/v1",
                               "emuflow.boundary-identity-storage/v1");
      SqliteStore boundary_owner(identity);
      D header(Meta(boundary_owner.database(), "header"));
      Check(header.Text("fpga") == fpga && header.Text("platform") == platform,
            "boundary identity belongs to a different FPGA");
      PhysicalOwner owner;
      std::string mapped_token, physical_token;
      O evidence{
          {"fpga", JsonQuote(fpga)},
          {"boundary", D::Encode({{"path", JsonQuote(identity.path())},
                                  {"sha256", JsonQuote(Hash(identity))}})}};
      std::array<I, 4> placed{};
      if (stages.Has("vivado_implementation")) {
        owner.vivado = true;
        D producer(stages.Token("vivado_implementation"));
        D mapped_stage(stages.Token("mapped_verilog"));
        mapped_token = mapped_stage.Token("mapped_index");
        D coverage(producer.Token("cell_coverage"));
        physical_token = D::Encode({{"path", coverage.Token("output")},
                                    {"sha256", coverage.Token("sha256")}});
        auto physical = Sealed(physical_token, "vivado-lineage-store-v2");
        owner.physical = std::make_unique<SqliteStore>(physical);
        auto &db = owner.physical->database();
        D mapped(mapped_token);
        Check(Meta(db, "coordinate_system") == coordinates &&
                  Meta(db, "mapped_index_sha256") == mapped.Text("sha256") &&
                  Meta(db, "retained_stage") == "routed",
              "Vivado placement/source identity disagrees");
        Attach(db, Sealed(mapped_token, "xilinx-mapped-index-v2"), "mapped");
        auto row = db.Prepare("SELECT "
                              "min(tile_column),max(tile_column),min(tile_row),"
                              "max(tile_row) FROM sites WHERE stage='routed'");
        Check(row.Step(), "missing Vivado placed bounds");
        for (int i = 0; i < 4; ++i)
          placed[i] = row.TypedInteger(i);
      } else if (stages.Has("rapidwright_implementation")) {
        D producer(stages.Token("rapidwright_implementation"));
        D result(producer.Token("result"));
        D artifacts(result.Token("artifacts"));
        mapped_token = artifacts.Token("mapped");
        physical_token = artifacts.Token("physical_handoff");
        owner.physical = std::make_unique<SqliteStore>(
            Sealed(physical_token, "xilinx-physical-handoff-v1"));
        auto &db = owner.physical->database();
        auto packed =
            db.Prepare("SELECT value_json FROM document WHERE name='packed'");
        Check(packed.Step(), "missing handoff packed header");
        D packed_header(packed.TypedText(0));
        D packed_source(packed_header.Token("source"));
        D mapped(mapped_token);
        Check(packed_source.Text("mapped_json_sha256") ==
                      mapped.Text("sha256") &&
                  packed_source.Text("architecture_sha256") == architecture_sha,
              "OpenPARF placement/source identity disagrees");
        (void)Sealed(mapped_token, "xilinx-mapped-index-v2");
        D placement_stage(producer.Token("placement"));
        const auto site_token = placement_stage.Token("site_database");
        D site(site_token);
        Check(site.Text("schema") == "emuflow.openparf-atomic-site-database/v1",
              "invalid site map role");
        auto geometry = Bind(site.Text("path"), "openparf-site-store-v1");
        const auto expected = D::Array(site.Token("generation"), 7),
                   actual = D::Array(Generation(geometry), 7);
        Check(expected.size() == 7, "invalid site map generation");
        for (int i = 0; i < 5; ++i)
          Check(D::IntegerToken(expected[i]) == D::IntegerToken(actual[i]),
                "OpenPARF site map generation differs from its producer");
        if (expected != actual) {
          const auto key = Generation(geometry);
          if (!site_hashes.count(key)) {
            site_hashes.emplace(key, Hash(geometry));
            ++site_rechecks;
          }
          Check(site_hashes.at(key) == site.Text("sha256"),
                "OpenPARF site map content seal disagrees");
        }
        Attach(db, geometry, "geometry");
        auto m = db.Prepare("SELECT value FROM geometry.metadata WHERE key=?");
        for (const auto &field : O{{"coordinate_system", coordinates},
                                   {"architecture_sha256", architecture_sha}}) {
          m.BindText(1, field.first);
          Check(m.Step() && m.TypedText(0) == field.second,
                "OpenPARF site map lacks native tile coordinates");
          m.Reset();
        }
        auto index = db.Prepare("SELECT 1 FROM geometry.sqlite_master WHERE "
                                "type='index' AND name='physical_sites_name'");
        Check(index.Step(), "site map physical-site name index is missing");
        auto row = db.Prepare("SELECT "
                              "min(physical_x),max(physical_x),min(physical_y),"
                              "max(physical_y) FROM geometry.sites");
        Check(row.Step(), "missing OpenPARF placed bounds");
        for (int i = 0; i < 4; ++i)
          placed[i] = row.TypedInteger(i);
        evidence["site_database"] = site_token;
      } else
        Check(false, "physical prepass requires an indexed Xilinx backend");
      auto &db = owner.physical->database();
      Attach(db, roles.at("original"), "source");
      Attach(db, identity, "boundary");
      for (int i = 0; i < 4; ++i) {
        Check(placed[i] >= 0,
              "physical prepass has no native placed source objects");
        box[i] =
            i % 2 ? std::max(box[i], placed[i]) : std::min(box[i], placed[i]);
      }
      owner.bounds = box;
      owner.Prepare();
      owners.emplace(fpga, std::move(owner));
      bounds.emplace(fpga, box);
      evidence["mapped"] = mapped_token;
      evidence["placement"] = physical_token;
      physical_sources.push_back(D::Encode(evidence));
    }
    Check(seen.size() == parts.size(),
          "physical lookahead must cover every FPGA");
    for (const auto &ref : sources)
      ref.RequireUnchanged();
  }
  std::array<double, 3> Point(const std::string &fpga, const std::string &net,
                              const std::string &entry,
                              const std::string &kind) {
    Check(owners.count(fpga), "active hop belongs to an inactive FPGA");
    auto &owner = owners.at(fpga);
    const auto key = std::make_tuple(fpga, net, kind);
    std::array<double, 2> point{};
    auto cached = cache.find(key);
    if (cached != cache.end()) {
      point = cached->second.first;
      lru.splice(lru.end(), lru, cached->second.second);
      ++hits;
    } else {
      ++queries;
      auto &row = *owner.source;
      row.BindInteger(1, kind == "tx" ? 0 : 1);
      row.BindText(2, net);
      Check(row.Step(), "source coordinate query failed");
      if (!row.IsNull(0)) {
        Check(!row.IsNull(1), "source coordinate is incomplete");
        point = {row.TypedReal(0), row.TypedReal(1)};
        row.Reset();
        lru.push_back(key);
        cache.emplace(key, std::make_pair(point, std::prev(lru.end())));
        if (cache.size() > 4096) {
          cache.erase(lru.front());
          lru.pop_front();
        }
        peak = std::max<std::uint64_t>(peak, cache.size());
      } else {
        row.Reset();
        auto &boundary = *owner.boundary;
        boundary.BindText(1, entry);
        boundary.BindText(2, kind);
        boundary.BindText(3, net);
        Check(boundary.Step() && !boundary.IsNull(0) && !boundary.IsNull(1) &&
                  boundary.TypedInteger(2) == boundary.TypedInteger(3),
              "active " + fpga + "/" + entry + "/" + kind +
                  " lacks placed source or boundary coverage");
        point = {boundary.TypedReal(0), boundary.TypedReal(1)};
        boundary.Reset();
        ++forwarded;
      }
    }
    const auto &box = owner.bounds;
    return {point[0], point[1],
            box[2] == box[3] ? 0.5
                             : (point[1] - box[2]) / double(box[3] - box[2])};
  }
  std::string Header(std::uint64_t points) const {
    O boxes;
    O plans;
    std::vector<std::string> inputs;
    for (std::size_t i = 1; i < sources.size(); ++i)
      inputs.push_back(D::Encode({{"path", JsonQuote(sources[i].path())},
                                  {"schema", JsonQuote(sources[i].schema())},
                                  {"generation", Generation(sources[i])}}));
    for (const auto &owner : owners)
      plans[owner.first] = Array(owner.second.lookup_plan);
    for (const auto &box : bounds)
      boxes[box.first] =
          JsonRecord(std::vector<JsonRecord>{box.second[0], box.second[1],
                                             box.second[2], box.second[3]})
              .Encode();
    std::vector<std::string> pin_rows;
    for (const auto &pin : pins)
      pin_rows.push_back(JsonRecord(std::vector<JsonRecord>{
                                        pin.first.first, pin.first.second,
                                        pin.second.first, pin.second.second})
                             .Encode());
    return D::Encode(
        {{"schema", JsonQuote("emuflow.chimew-geometry/v1")},
         {"implementation_id", JsonQuote("native-indexed-xilinx-geometry-v1")},
         {"architecture", D::Encode({{"path", JsonQuote(architecture)},
                                     {"sha256", JsonQuote(architecture_sha)}})},
         {"sources", Array(physical_sources)},
         {"input_artifacts", Array(inputs)},
         {"lookup_plans", D::Encode(plans)},
         {"bounds", D::Encode(boxes)},
         {"pins", Array(pin_rows)},
         {"metrics", D::Encode({{"points", std::to_string(points)},
                                {"forwarded", std::to_string(forwarded)},
                                {"source_queries", std::to_string(queries)},
                                {"source_cache_hits", std::to_string(hits)},
                                {"source_cache_peak", std::to_string(peak)},
                                {"site_map_seal_rechecks",
                                 std::to_string(site_rechecks)}})}});
  }

private:
  using Key = std::tuple<std::string, std::string, std::string>;
  std::list<Key> lru;
  std::map<Key, std::pair<std::array<double, 2>, std::list<Key>::iterator>>
      cache;
  std::map<std::string, std::string> hashes, site_hashes;
};
} // namespace
StageResult
PrepareChimewPackageGeometry(const ArtifactRef &architecture,
                             const std::string &part, const std::string &output,
                             const volatile std::sig_atomic_t *cancelled) {
  return RunStage(
      "phase6.prepare-chimew-package",
      [&] {
        Check(architecture.schema() == "physical-input-document-v1",
              "invalid architecture source role");
        return std::vector<ArtifactRef>{architecture};
      },
      [&](const auto &sources) {
        const auto catalog =
            ReadDevicePackageGeometry(architecture, part, cancelled);
        O pins;
        for (const auto &pin : catalog.pins)
          pins.emplace(pin.first,
                       JsonRecord(std::vector<JsonRecord>{pin.second.site,
                                                          pin.second.column,
                                                          pin.second.row})
                           .Encode());
        const auto frame = D::Encode(
            {{"schema",
              JsonQuote("emuflow.xilinx-package-geometry-control/v1")},
             {"path", JsonQuote(catalog.source.path())},
             {"part", JsonQuote(catalog.part)},
             {"generation", Generation(catalog.source)},
             {"sha256", JsonQuote(catalog.sha256)},
             {"pins", D::Encode(pins)}});
        PublishTextArtifact(output, sources, [&](std::ostream &stream) {
          stream << frame << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "chimew-package-geometry-v1")};
      });
}
StageResult PrepareChimewGeometry(const ArtifactRef &request,
                                  const std::string &directory,
                                  const volatile std::sig_atomic_t *cancelled) {
  Geometry geometry(cancelled);
  std::optional<Work> work;
  const auto output = fs::absolute(directory);
  return RunStage(
      "phase6.prepare-chimew-geometry",
      [&] {
        Check(request.schema() == "chimew-geometry-input-v1",
              "request schema disagrees");
        geometry.Read(request);
        Check(!fs::exists(output) && !fs::is_symlink(output),
              "output directory already exists");
        fs::create_directories(output.parent_path());
        work.emplace(output.parent_path());
        return geometry.sources;
      },
      [&](const auto &sources) {
        std::string header, selector;
        std::uint64_t points = 0, entries = 0;
        {
          SqliteStore schedule(*geometry.schedule);
          auto &db = schedule.database();
          D schedule_selector(D::Read(geometry.roles.at("schedule")));
          D sections(schedule_selector.Token("sections"));
          D section(sections.Token("entries"));
          SqliteArtifactWriter writer((work->root / "geometry.sqlite").string(),
                                      "chimew-geometry-store-v1", sources);
          {
            auto &target = writer.database();
            target.Execute(
                "PRAGMA application_id=0x454d4347;PRAGMA user_version=1;CREATE "
                "TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) "
                "WITHOUT ROWID;CREATE TABLE points(seq INTEGER PRIMARY "
                "KEY,entry_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN "
                "('tx','rx')),fpga TEXT NOT NULL,net TEXT NOT NULL,x REAL NOT "
                "NULL,y REAL NOT NULL,normal_y REAL NOT "
                "NULL,UNIQUE(entry_id,kind));BEGIN");
            auto add =
                target.Prepare("INSERT INTO points VALUES(?,?,?,?,?,?,?,?)");
            auto row =
                db.Prepare("SELECT seq,entry_id,net,source_fpga,sink_fpga FROM "
                           "schedule_entries ORDER BY seq");
            while (row.Step()) {
              Stop(cancelled);
              Check(row.TypedInteger(0) == I(entries++),
                    "schedule entry order is incomplete");
              const auto entry = row.TypedText(1), net = row.TypedText(2);
              Check(!entry.empty() && !net.empty(),
                    "invalid scheduled endpoint identity");
              for (int end = 0; end < 2; ++end) {
                const std::string kind = end ? "rx" : "tx",
                                  fpga = row.TypedText(3 + end);
                const auto point = geometry.Point(fpga, net, entry, kind);
                add.BindInteger(1, points++);
                add.BindText(2, entry);
                add.BindText(3, kind);
                add.BindText(4, fpga);
                add.BindText(5, net);
                for (int i = 0; i < 3; ++i)
                  add.BindReal(6 + i, point[i]);
                add.Execute();
                add.Reset();
              }
            }
            Check(entries == std::uint64_t(section.Integer("records")),
                  "schedule entry coverage disagrees");
            header = geometry.Header(points);
            auto meta =
                target.Prepare("INSERT INTO metadata VALUES('header',?)");
            meta.BindText(1, header);
            meta.Execute();
            target.Execute("COMMIT");
          }
          const auto published = writer.Commit();
          selector = D::Encode(
              {{"schema", JsonQuote("emuflow.chimew-geometry-storage/v1")},
               {"artifact", JsonQuote("geometry.sqlite")},
               {"store_bytes", std::to_string(published.generation().bytes)},
               {"store_sha256", JsonQuote(Sha256File(published, cancelled))},
               {"header", header}});
        }
        PublishTextArtifact(
            (work->root / "geometry.json").string(), sources,
            [&](std::ostream &out) { out << selector << '\n'; });
        Stop(cancelled);
        PublishNewDirectory(work->root.string(), output.string(), sources);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind((output / "geometry.sqlite").string(),
                              "chimew-geometry-store-v1"),
            ArtifactRef::Bind((output / "geometry.json").string(),
                              "chimew-geometry-selector-v1")};
      });
}
} // namespace emuflow::core
