#include "physical/handoff_audit.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "physical/physical_cells.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <set>
#include <unistd.h>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using I = std::int64_t;
using Obj = std::map<std::string, JsonRecord>;
void Check(bool good, const std::string &why) {
  if (!good)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical handoff audit: " + why);
}
void Stop(const volatile std::sig_atomic_t *cancelled) {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed,
                     "physical handoff audit cancelled");
}
void No(SqliteDatabase &db, const std::string &sql, const std::string &why) {
  auto row = db.Prepare(sql);
  Check(!row.Step(), why);
}
I Count(SqliteDatabase &db, const std::string &sql) {
  auto row = db.Prepare(sql);
  Check(row.Step(), "missing aggregate");
  const auto value = row.TypedInteger(0);
  Check(value >= 0 && !row.Step(), "invalid aggregate");
  return value;
}
std::string Digest(Sha256 &hash) {
  const auto bytes = hash.finish();
  std::string out;
  for (auto c : bytes) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
void Hash(Sha256 &hash, const std::string &text) {
  hash.update(reinterpret_cast<const unsigned char *>(text.data()),
              text.size());
}
std::string FileHash(const ArtifactRef &input,
                     const volatile std::sig_atomic_t *flag = nullptr) {
  std::ifstream file(input.path(), std::ios::binary);
  Check(bool(file), "cannot read source");
  Sha256 hash;
  std::array<char, 65536> block;
  while (file) {
    Stop(flag);
    file.read(block.data(), block.size());
    hash.update(reinterpret_cast<const unsigned char *>(block.data()),
                file.gcount());
  }
  Check(file.eof(), "source read failed");
  input.RequireUnchanged();
  return Digest(hash);
}
std::string Document(SqliteDatabase &db, const std::string &name) {
  auto row = db.Prepare("SELECT CASE WHEN length(value_json)<=8388608 THEN "
                        "value_json END FROM document WHERE name=?");
  row.BindText(1, name);
  Check(row.Step() && !row.IsNull(0), "missing or oversized document");
  const auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate document");
  BoundedJson check(value);
  return value;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT value FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), "missing metadata");
  const auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return value;
}
void Attach(SqliteDatabase &db, const ArtifactRef &source,
            const std::string &name) {
  auto row = db.Prepare("ATTACH DATABASE ? AS " + name);
  row.BindText(1, SqliteImmutableUri(source.path()));
  row.Execute();
}
bool Table(SqliteDatabase &db, const std::string &name) {
  auto row =
      db.Prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?");
  row.BindText(1, name);
  return row.Step();
}
bool Null(SqliteStatement &row, unsigned column) {
  return row.IsNull(column) || row.TypedText(column) == "null";
}
bool Ff(const std::string &type) {
  return type == "FDRE" || type == "FDSE" || type == "FDCE" || type == "FDPE";
}
bool Lut(const std::string &type) {
  return type.size() == 4 && type.compare(0, 3, "LUT") == 0 && type[3] >= '1' &&
         type[3] <= '6';
}
bool Mux(const std::string &type) {
  return type == "MUXF7" || type == "MUXF8" || type == "MUXF9";
}
std::set<std::string> Strings(const std::string &raw) {
  std::set<std::string> result;
  for (const auto &item : BoundedJson::Array(raw, 65536)) {
    const auto value = DecodeJsonString(item);
    Check(!value.empty() && result.insert(value).second,
          "invalid/duplicate identity");
  }
  return result;
}
std::string RawArray(const std::vector<std::string> &values) {
  std::vector<JsonRecord> result;
  for (const auto &value : values)
    result.emplace_back(value);
  return JsonRecord(result).Encode();
}
void Repairs(SqliteDatabase &db, const std::string &packed,
             const volatile std::sig_atomic_t *flag) {
  const bool table = Table(db, "site_pin_repair");
  bool feature = false;
  if (Table(db, "feature")) {
    auto row = db.Prepare("SELECT name FROM feature ORDER BY name");
    std::set<std::string> seen;
    while (row.Step()) {
      const auto name = row.TypedText(0);
      Check(seen.insert(name).second &&
                (name == "canonical-cluster-id-order-v1" ||
                 name == "typed-site-pin-repairs-v1"),
            "unknown or repeated feature");
      feature |= name == "typed-site-pin-repairs-v1";
    }
    Check(seen.count("canonical-cluster-id-order-v1"),
          "canonical order feature absent");
  }
  Check(table == feature, "typed repairs table/feature disagree");
  if (!table)
    return;
  BoundedJson header(packed), policy(header.Token("policy")),
      legalization(policy.Token("site_pin_legalization")),
      descriptor(legalization.Token("repairs"));
  descriptor.Keys({"schema", "records", "sha256"});
  Check(descriptor.Text("schema") == "emuflow.xilinx-site-pin-repairs/v1",
        "repair descriptor schema disagrees");
  Sha256 hash;
  auto rows =
      db.Prepare("SELECT "
                 "r.seq,r.cluster_id,r.instance,r.from_bel,r.to_bel,a.cell_"
                 "type,a.bel,c.cluster_id FROM site_pin_repair r LEFT JOIN "
                 "assignment a ON a.instance=r.instance LEFT JOIN cluster c ON "
                 "c.ordinal=a.cluster_ordinal ORDER BY r.seq");
  I count = 0;
  while (rows.Step()) {
    Stop(flag);
    Check(rows.TypedInteger(0) == count++ && !rows.IsNull(5) &&
              Ff(rows.TypedText(5)) && rows.TypedText(1) == rows.TypedText(7),
          "repair source/cluster/ordinal invalid");
    const auto from = rows.TypedText(3), to = rows.TypedText(4);
    const auto valid = [](const std::string &bel) {
      return bel.size() >= 3 && bel[0] >= 'A' && bel[0] <= 'H' &&
             (bel.substr(1) == "FF" || bel.substr(1) == "FF2");
    };
    Check(valid(from) && valid(to) && from != to,
          "invalid repair BEL transition");
    Hash(hash, JsonRecord(Obj{{"cluster", rows.TypedText(1)},
                              {"instance", rows.TypedText(2)},
                              {"from_bel", from},
                              {"to_bel", to}})
                       .EncodeUtf8() +
                   "\n");
  }
  Check(count == descriptor.Integer("records") &&
            Digest(hash) == descriptor.Text("sha256") &&
            legalization.Integer("moved_ffs") == count,
        "repair count/hash disagrees");
  No(db,
     "SELECT 1 FROM (SELECT instance,from_bel,lag(to_bel) OVER(PARTITION BY "
     "instance ORDER BY seq) previous FROM site_pin_repair) WHERE previous IS "
     "NOT NULL AND previous!=from_bel LIMIT 1",
     "repair chain is discontinuous");
  No(db,
     "SELECT 1 FROM site_pin_repair r JOIN assignment a ON "
     "a.instance=r.instance WHERE NOT EXISTS(SELECT 1 FROM site_pin_repair "
     "later WHERE later.instance=r.instance AND later.seq>r.seq) AND "
     "r.to_bel!=a.bel LIMIT 1",
     "repair final BEL differs from placement");
  Check(legalization.Integer("sites") ==
            Count(db, "SELECT count(DISTINCT cluster_id) FROM site_pin_repair"),
        "repair site count disagrees");
}
Obj Structure(const ArtifactRef &source, bool integrity,
              const volatile std::sig_atomic_t *flag) {
  Check(source.schema() == "xilinx-physical-handoff-v1",
        "requires typed handoff source");
  SqliteStore store(source);
  auto &db = store.database();
  if (integrity) {
    auto check = db.Prepare("PRAGMA quick_check");
    Check(check.Step() && check.TypedText(0) == "ok" && !check.Step(),
          "integrity check failed");
  }
  No(db, "PRAGMA foreign_key_check", "foreign keys disagree");
  auto manifest = db.Prepare(
      "SELECT schema,packed_clusters,assignments,cascade_chains,constants FROM "
      "manifest WHERE singleton=1");
  Check(manifest.Step() && manifest.TypedText(0) ==
                               "emuflow.xilinx-physical-handoff-sqlite/v1",
        "manifest schema disagrees");
  Obj result{{"schema", "emuflow.xilinx-physical-handoff-validation/v1"},
             {"status", "pass"},
             {"implementation_id", "native-physical-handoff-structure-v1"},
             {"integrity_check", integrity}};
  unsigned index = 1;
  for (const auto &item : std::vector<std::pair<std::string, std::string>>{
           {"packed_clusters", "cluster"},
           {"assignments", "assignment"},
           {"cascade_chains", "cascade"},
           {"constants", "constant"}}) {
    const auto n = Count(db, "SELECT count(*) FROM " + item.second);
    Check(manifest.TypedInteger(index++) == n, "manifest row counts disagree");
    result.emplace(item.first, n);
  }
  Check(!manifest.Step(), "manifest singleton repeats");
  Check(Count(db, "SELECT count(*) FROM document") == 2,
        "document coverage differs");
  BoundedJson packed(Document(db, "packed")),
      placement(Document(db, "placement"));
  Check(packed.Text("schema") == "emuflow.packed-site-netlist/v1" &&
            placement.Text("schema") == "emuflow.xilinx-placement/v1",
        "document schema disagrees");
  No(db,
     "SELECT 1 FROM cluster WHERE typeof(ordinal)!='integer' OR ordinal<0 OR "
     "typeof(cluster_id)!='text' OR length(cluster_id)=0 OR "
     "typeof(site)!='text' OR length(site)=0 OR typeof(x)!='integer' OR "
     "typeof(y)!='integer' OR typeof(fixed)!='integer' OR fixed NOT IN (0,1) "
     "LIMIT 1",
     "cluster identity/type invalid");
  No(db,
     "SELECT 1 FROM assignment a LEFT JOIN cluster c ON "
     "c.ordinal=a.cluster_ordinal WHERE c.ordinal IS NULL OR "
     "typeof(a.instance)!='text' OR length(a.instance)=0 OR "
     "typeof(a.bel)!='text' OR length(a.bel)=0 OR "
     "typeof(a.physical_site)!='text' OR length(a.physical_site)=0 OR "
     "a.packed_ordinal<0 OR a.placement_ordinal<0 LIMIT 1",
     "assignment owner/identity invalid");
  for (const auto &group : std::vector<std::string>{
           "instance", "cluster_ordinal,bel", "physical_site,bel"})
    No(db,
       "SELECT 1 FROM assignment GROUP BY " + group +
           " HAVING count(*)!=1 LIMIT 1",
       "assignment ownership/site BEL repeats");
  No(db,
     "SELECT 1 FROM cluster GROUP BY cluster_id HAVING count(*)!=1 UNION ALL "
     "SELECT 1 FROM cluster GROUP BY site HAVING count(*)!=1 LIMIT 1",
     "cluster/site repeats");
  No(db,
     "SELECT 1 FROM assignment GROUP BY cluster_ordinal HAVING "
     "min(packed_ordinal)!=0 OR max(packed_ordinal)!=count(*)-1 OR "
     "count(DISTINCT packed_ordinal)!=count(*) OR min(placement_ordinal)!=0 OR "
     "max(placement_ordinal)!=count(*)-1 OR count(DISTINCT "
     "placement_ordinal)!=count(*) LIMIT 1",
     "assignment order is incomplete");
  Repairs(db, packed.text(), flag);
  Stop(flag);
  source.RequireUnchanged();
  result.emplace("physical_handoff_sha256", FileHash(source, flag));
  return result;
}
struct Scratch {
  fs::path root;
  explicit Scratch(const fs::path &parent) {
    auto pattern = (parent / ".handoff-audit-XXXXXX").string();
    Check(mkdtemp(pattern.data()) != nullptr, "cannot create scratch");
    root = pattern;
  }
  ~Scratch() {
    std::error_code error;
    fs::remove_all(root, error);
  }
};
void CheckConstraints(SqliteDatabase &db, const ArtifactRef *constraints,
                      const volatile std::sig_atomic_t *flag) {
  ImportPhysicalPlacementConstraints(db, constraints, flag);
}

} // namespace
std::string
ValidatePhysicalHandoffStore(const ArtifactRef &handoff, bool full_integrity,
                             const volatile std::sig_atomic_t *cancelled) {
  return JsonRecord(Structure(handoff, full_integrity, cancelled)).EncodeUtf8();
}
std::string AuditPhysicalHandoff(const ArtifactRef &mapped,
                                 const ArtifactRef &handoff,
                                 PhysicalDevice &device,
                                 const ArtifactRef *constraints,
                                 const volatile std::sig_atomic_t *cancelled) {
  const std::vector<ArtifactRef> sources =
      constraints ? std::vector<ArtifactRef>{mapped, handoff, *constraints}
                  : std::vector<ArtifactRef>{mapped, handoff};
  for (const auto &source : sources)
    source.RequireUnchanged();
  device.RequireUnchanged();
  BoundedJson structural(
      JsonRecord(Structure(handoff, false, cancelled)).EncodeUtf8());
  const auto digest = structural.Text("physical_handoff_sha256"),
             mapped_sha = FileHash(mapped, cancelled);
  SqliteStore mapped_owner(mapped), handoff_owner(handoff);
  auto &m = mapped_owner.database();
  auto &h = handoff_owner.database();
  const auto top = DecodeJsonString(Meta(m, "top_json"));
  BoundedJson packed(Document(h, "packed")),
      placement(Document(h, "placement")),
      packed_source(packed.Token("source")),
      placed_source(placement.Token("source"));
  Check(packed.Text("status") == "pass" && packed.Text("top") == top &&
            packed.Text("mapping_profile") == "xilinx-ultrascaleplus-open-v1" &&
            packed_source.Text("mapped_json_sha256") == mapped_sha &&
            packed_source.Text("architecture_sha256") ==
                device.architecture_sha(),
        "packed mapped/source namespace disagrees");
  Check(placement.Text("status") == "pass" &&
            placement.Text("part") == device.part() &&
            placement.Text("provider") ==
                "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1",
        "placement namespace/provider disagrees");
  // The immutable owner's digest is projected only by readers. Embedding the
  // file's own SHA in its bytes would be a self-referential, impossible seal.
  Check(placed_source.Token("packed_sha256") == "null" &&
            placed_source.Text("architecture_sha256") ==
                device.architecture_sha(),
        "placement source/self-owner seal disagrees");
  const auto constraint_sha =
      constraints ? FileHash(*constraints, cancelled) : "";
  Check(constraints ? placed_source.Text("constraints_sha256") == constraint_sha
                    : placed_source.Token("constraints_sha256") == "null",
        "placement constraints seal disagrees");
  const auto native_sha = device.native_sha();
  if (!native_sha.empty())
    Check(placed_source.Text("native_constraints_sha256") == native_sha &&
              placed_source.Text("provider_manifest_sha256") ==
                  device.provider_sha(),
          "placement native/provider seal disagrees");
  else
    Check(!placed_source.Has("native_constraints_sha256") &&
              !placed_source.Has("provider_manifest_sha256"),
          "placement claims unbound native facts");
  BoundedJson policy(placement.Token("policy"));
  policy.Keys({"clock_region_site_utilization_limit", "capacity_rounding",
               "packing", "placement_certificate", "site_pin_legalization"});
  Check(policy.Number("clock_region_site_utilization_limit") == 1.0 &&
            policy.Text("capacity_rounding") == "ceil-with-one-site-minimum" &&
            policy.Text("packing") == "native-openparf-atomic-site-groups-v1" &&
            policy.Text("placement_certificate") ==
                "emuflow.openparf-atomic-placement/v2" &&
            policy.Text("site_pin_legalization") ==
                "ultrascaleplus-slice-site-pin-legalizer-v1",
        "routability policy disagrees");
  Scratch scratch(fs::temp_directory_path());
  const auto request = scratch.root / "population.bin",
             population = scratch.root / "population.json";
  {
    CheckedRequestWriter writer(request.string(), sources);
    writer.Put<std::uint64_t>(0x31564350464d5545ULL);
    writer.Put<std::uint32_t>(1);
    writer.Generation(mapped.generation());
    writer.Generation(handoff.generation());
    writer.Text(mapped_sha);
    writer.Text(top);
    writer.Finish();
  }
  ValidatePhysicalCells(
      mapped, handoff,
      ArtifactRef::Bind(request.string(), "physical-cell-check-request-v1"),
      population.string());
  BoundedJson population_result(BoundedJson::Read(ArtifactRef::Bind(
      population.string(), "physical-cell-check-summary-v1")));
  const auto cells = population_result.Integer("cells"),
             assignment_count = population_result.Integer("assignments");
  auto &db = device.database();
  Attach(db, handoff, "ah");
  Attach(db, mapped, "am");
  struct Cleanup {
    SqliteDatabase &db;
    ~Cleanup() {
      try {
        db.Execute("DROP TABLE IF EXISTS temp.audit_constraints;DROP TABLE IF "
                   "EXISTS temp.audit_allowed_slr;DROP TABLE IF EXISTS "
                   "temp.audit_occupied;DETACH DATABASE ah;DETACH DATABASE am");
      } catch (...) {
      }
    }
  } cleanup{db};
  CheckConstraints(db, constraints, cancelled);
  No(db,
     "SELECT 1 FROM audit_constraints c LEFT JOIN ah.cluster p ON "
     "p.cluster_id=c.cluster_id WHERE p.cluster_id IS NULL LIMIT 1",
     "constraint references an unknown cluster");
  db.Execute(
      "CREATE TEMP TABLE audit_occupied(slr TEXT,clock_region TEXT,type "
      "TEXT,used INTEGER,PRIMARY KEY(slr,clock_region,type)) WITHOUT ROWID");
  MappedPhysicalSemantics semantics(mapped);
  auto clusters =
      h.Prepare("SELECT "
                "ordinal,cluster_id,kind,site_templates_json,control_set_json,"
                "control_sets_json,site_mode,site,site_type,x,y,fixed,physical_"
                "region_json FROM cluster ORDER BY cluster_id");
  I cluster_count = 0, fixed = 0;
  std::map<std::string, I> site_types, cluster_kinds;
  while (clusters.Step()) {
    Stop(cancelled);
    const auto ordinal = clusters.TypedInteger(0);
    const auto id = clusters.TypedText(1), kind = clusters.TypedText(2),
               site_name = clusters.TypedText(7);
    const auto site = device.Site(site_name);
    Check(site.type == clusters.TypedText(8) &&
              site.x == clusters.TypedInteger(9) &&
              site.y == clusters.TypedInteger(10),
          "site metadata/geometry differs from architecture");
    if (site.region_json == "null")
      Check(clusters.IsNull(12) || clusters.TypedText(12) == "null",
            "unexpected physical region claim");
    else {
      Check(!clusters.IsNull(12), "missing physical region");
      RequireEquivalentJson(db, clusters.TypedText(12), site.region_json,
                            "physical region claim differs");
    }
    auto constraint = db.Prepare("SELECT site,slr,clock_region FROM "
                                 "audit_constraints WHERE cluster_id=?");
    constraint.BindText(1, id);
    if (constraint.Step()) {
      for (unsigned index = 0; index < 3; ++index)
        if (!constraint.IsNull(index))
          Check(constraint.TypedText(index) == (index == 0 ? site.name
                                                : index == 1
                                                    ? site.slr
                                                    : site.clock_region),
                "fixed cluster constraint violated");
    }
    if (Count(db, "SELECT count(*) FROM audit_allowed_slr")) {
      auto allowed = db.Prepare("SELECT 1 FROM audit_allowed_slr WHERE slr=?");
      allowed.BindText(1, site.slr);
      Check(allowed.Step(), "global SLR constraint violated");
    }
    const auto modes = Strings(clusters.TypedText(3));
    Check(!modes.empty(), "cluster lacks declared site templates");
    auto rows = h.Prepare("SELECT "
                          "instance,cell_type,bel,placement_mode,physical_site,"
                          "packed_ordinal,placement_ordinal FROM assignment "
                          "WHERE cluster_ordinal=? ORDER BY packed_ordinal");
    rows.BindInteger(1, ordinal);
    std::vector<PhysicalAssignment> assignments;
    std::set<std::string> bels;
    std::map<std::string, std::string> local_bels;
    I luts = 0, ffs = 0, carry = 0, dual = 0;
    while (rows.Step()) {
      Check(assignments.size() < 64,
            "site assignment population exceeds bound");
      PhysicalAssignment a{rows.TypedText(0), rows.TypedText(1),
                           rows.TypedText(2)};
      Check(rows.TypedInteger(5) == I(assignments.size()) &&
                bels.insert(a.bel).second,
            "assignment sequence or BEL repeats");
      const auto resolved = device.ResolveBel(site.name, a.bel, a.type, modes);
      Check(resolved.mode == rows.TypedText(3) &&
                resolved.physical_site == rows.TypedText(4),
            "exact BEL mode/native physical site differs");
      local_bels.emplace(a.instance, a.bel);
      assignments.push_back(a);
      luts += Lut(a.type);
      ffs += Ff(a.type);
      carry += a.type == "CARRY8";
      dual += a.type == "LUT6_2";
    }
    Check(!assignments.empty(), "empty physical cluster");
    if (kind == "slice") {
      Check(luts <= 8 && ffs <= 16, "slice exceeds physical capacity");
      const auto controls = semantics.ControlSets(assignments);
      if (controls.size() <= 1) {
        Check(Null(clusters, 5) &&
                  (controls.empty() ? Null(clusters, 4)
                                    : !Null(clusters, 4) &&
                                          DecodeJsonString(clusters.TypedText(
                                              4)) == controls[0]),
              "scalar FF control-set certificate differs");
      } else {
        Check(Null(clusters, 4) && !Null(clusters, 5),
              "multi FF control-set certificate differs");
        RequireEquivalentJson(db, RawArray(controls), clusters.TypedText(5),
                              "multi FF control-set certificate differs");
      }
      Check(semantics.SitePinConflicts(assignments).empty(),
            "slice external MUX/FF site pin assignment invalid");
    } else if (kind == "carry")
      Check(carry == 1 && dual == 8 && assignments.size() == 9,
            "carry cluster shape invalid");
    else if (kind == "hard") {
      if (std::all_of(assignments.begin(), assignments.end(),
                      [](const auto &a) { return a.type == "RAMB18E2"; })) {
        Check(assignments.size() <= 2 &&
                  std::all_of(bels.begin(), bels.end(),
                              [](const auto &b) {
                                return b == "RAMB18E2_L" || b == "RAMB18E2_U";
                              }) &&
                  !clusters.IsNull(6) &&
                  clusters.TypedText(6) ==
                      "RAMB18E2x" + std::to_string(assignments.size()),
              "shared RAMB18 site mode invalid");
      } else
        Check(assignments.size() == 1 && (assignments[0].type == "DSP48E2" ||
                                          assignments[0].type == "RAMB36E2" ||
                                          assignments[0].type == "URAM288"),
              "hard cluster type/population invalid");
    } else
      Check(false, "unknown cluster kind");
    for (const auto &a : assignments) {
      if (Mux(a.type)) {
        std::set<std::string> actual;
        for (const auto *port : {"I0", "I1"}) {
          const auto bits = semantics.Bits(a.instance, port);
          Check(bits.size() == 1 && std::holds_alternative<I>(bits[0]),
                "MUX input is not a scalar mapped net");
          const auto driver = semantics.Driver(std::get<I>(bits[0]));
          Check(driver && local_bels.count(*driver),
                "MUX input lacks a same-site driver");
          actual.insert(local_bels.at(*driver));
        }
        std::set<std::string> expected;
        if (a.type == "MUXF7") {
          const std::map<std::string, std::set<std::string>> by_bel{
              {"F7MUX_AB", {"A6LUT", "B6LUT"}},
              {"F7MUX_CD", {"C6LUT", "D6LUT"}},
              {"F7MUX_EF", {"E6LUT", "F6LUT"}},
              {"F7MUX_GH", {"G6LUT", "H6LUT"}}};
          auto found = by_bel.find(a.bel);
          Check(found != by_bel.end(), "MUXF7 BEL invalid");
          expected = found->second;
        } else if (a.type == "MUXF8") {
          Check(a.bel == "F8MUX_BOT" || a.bel == "F8MUX_TOP",
                "MUXF8 BEL invalid");
          expected = a.bel == "F8MUX_BOT"
                         ? std::set<std::string>{"F7MUX_AB", "F7MUX_CD"}
                         : std::set<std::string>{"F7MUX_EF", "F7MUX_GH"};
        } else {
          Check(a.bel == "F9MUX", "MUXF9 BEL invalid");
          expected = {"F8MUX_BOT", "F8MUX_TOP"};
        }
        Check(actual == expected, "MUX packing topology invalid");
      } else if (a.type == "CARRY8") {
        const auto di = semantics.Bits(a.instance, "DI"),
                   select = semantics.Bits(a.instance, "S");
        Check(di.size() >= 8 && select.size() >= 8,
              "carry data/select width invalid");
        for (unsigned index = 0; index < 8; ++index) {
          Check(std::holds_alternative<I>(di[index]) &&
                    std::holds_alternative<I>(select[index]),
                "carry adapter input is not a mapped net");
          const auto d = semantics.Driver(std::get<I>(di[index])),
                     s = semantics.Driver(std::get<I>(select[index]));
          Check(d && s && *d == *s && local_bels.count(*d) &&
                    local_bels.at(*d) ==
                        std::string(1, char('A' + index)) + "6LUT" &&
                    semantics.CellType(*d) == "LUT6_2",
                "CARRY8 LUT6_2 topology invalid");
        }
      }
    }
    if (!site.clock_region.empty()) {
      auto add = db.Prepare(
          "INSERT INTO audit_occupied VALUES(?,?,?,1) ON "
          "CONFLICT(slr,clock_region,type) DO UPDATE SET used=used+1");
      add.BindText(1, site.slr);
      add.BindText(2, site.clock_region);
      add.BindText(3, site.type);
      add.Execute();
    }
    fixed += clusters.TypedInteger(11);
    ++site_types[site.type];
    ++cluster_kinds[kind];
    ++cluster_count;
  }
  I cascade_count = 0, cascade_links = 0;
  auto declared =
      h.Prepare("SELECT ordinal,value_json FROM cascade ORDER BY ordinal");
  semantics.DeriveCascades(
      [&](const std::string &raw) {
        Stop(cancelled);
        Check(declared.Step() && declared.TypedInteger(0) == cascade_count,
              "dedicated cascade record coverage differs");
        RequireEquivalentJson(
            db, declared.TypedText(1), raw,
            "dedicated cascade certificate differs from mapped connectivity");
        BoundedJson chain(raw);
        const auto members =
            BoundedJson::Array(chain.Token("instances"), 65536);
        cascade_links += I(members.size()) - 1;
        std::set<std::string> occupied;
        std::string previous_cluster, previous_site, slr;
        I clusters_in_chain = 0;
        for (const auto &token : members) {
          const auto instance = DecodeJsonString(token);
          auto position = h.Prepare(
              "SELECT c.cluster_id,c.site FROM assignment a JOIN cluster c ON "
              "c.ordinal=a.cluster_ordinal WHERE a.instance=?");
          position.BindText(1, instance);
          Check(position.Step(), "cascade instance has no placed owner");
          const auto cluster = position.TypedText(0),
                     site_name = position.TypedText(1);
          Check(!position.Step(), "cascade instance has multiple owners");
          if (cluster == previous_cluster)
            continue;
          Check(occupied.insert(cluster).second,
                "dedicated cascade revisits packed cluster");
          const auto site = device.Site(site_name);
          if (clusters_in_chain) {
            Check(!native_sha.empty() && site.slr != "@unspecified-slr" &&
                      site.slr == slr,
                  "dedicated cascade crosses SLR or lacks native source");
            const std::map<std::string, std::string> kinds{
                {"CARRY8", "CARRY_NEXT"},
                {"DSP48E2", "DSP_CASCADE"},
                {"RAMB36E2", "BRAM_CASCADE"},
                {"URAM288", "URAM_CASCADE"}};
            const auto kind = kinds.find(chain.Text("cell_type"));
            Check(kind != kinds.end(),
                  "unsupported dedicated primitive family");
            auto edge = db.Prepare("SELECT 1 FROM pd_adjacency WHERE kind=? "
                                   "AND source=? AND target=?");
            edge.BindText(1, kind->second);
            edge.BindText(2, previous_site);
            edge.BindText(3, site_name);
            Check(edge.Step(),
                  "cascade violates source-sealed native adjacency");
          }
          slr = site.slr;
          previous_site = site_name;
          previous_cluster = cluster;
          ++clusters_in_chain;
        }
        ++cascade_count;
      },
      cancelled);
  Check(!declared.Step(), "extra dedicated cascade certificate");
  auto buckets = db.Prepare(
      "SELECT s.slr,s.clock_region,s.type,count(*),coalesce(u.used,0) FROM "
      "pd_sites s LEFT JOIN audit_occupied u ON u.slr=s.slr AND "
      "u.clock_region=s.clock_region AND u.type=s.type WHERE "
      "s.clock_region!='' GROUP BY s.slr,s.clock_region,s.type ORDER BY "
      "s.slr,s.clock_region,s.type");
  I groups = 0;
  double maximum = 0;
  while (buckets.Step()) {
    const auto capacity = buckets.TypedInteger(3),
               used = buckets.TypedInteger(4);
    Check(capacity > 0 && used >= 0 && used <= capacity,
          "clock-region site reservation exceeds whole device capacity");
    maximum = std::max(maximum, double(used) / double(capacity));
    ++groups;
  }
  BoundedJson packed_summary(packed.Token("summary")),
      placed_summary(placement.Token("summary"));
  Check(packed_summary.Integer("cells") == cells &&
            packed_summary.Integer("placed_cells") == assignment_count &&
            packed_summary.Integer("constant_cells") ==
                population_result.Integer("constants") &&
            packed_summary.Integer("clusters") == cluster_count &&
            packed_summary.Integer("cascade_chains") == cascade_count &&
            packed_summary.Integer("cascade_links") == cascade_links,
        "packed resource/population summary differs");
  Check(placed_summary.Integer("cells") == assignment_count &&
            placed_summary.Integer("clusters") == cluster_count &&
            placed_summary.Integer("fixed_clusters") == fixed &&
            placed_summary.Integer("cascade_chains") == cascade_count &&
            placed_summary.Integer("clock_region_site_groups") == groups,
        "placed population summary differs");
  for (const auto *key : {"maximum_clock_region_site_utilization",
                          "maximum_clock_region_site_reservation"})
    Check(groups ? placed_summary.Number(key) == maximum
                 : placed_summary.Token(key) == "null",
          "local site utilization summary differs");
  Obj types, kinds;
  for (const auto &item : site_types)
    types.emplace(item.first, item.second);
  for (const auto &item : cluster_kinds)
    kinds.emplace(item.first, item.second);
  RequireEquivalentJson(db, placed_summary.Token("site_types"),
                        JsonRecord(types).Encode(),
                        "site type resource summary differs");
  RequireEquivalentJson(db, packed_summary.Token("cluster_kinds"),
                        JsonRecord(kinds).Encode(),
                        "cluster kind resource summary differs");
  for (const auto &source : sources)
    source.RequireUnchanged();
  device.RequireUnchanged();
  semantics.RequireUnchanged();
  const auto packing_base =
      JsonRecord(Obj{{"status", "pass"},
                     {"schema", "emuflow.packed-site-netlist-validation/v1"},
                     {"cells", cells},
                     {"clusters", cluster_count},
                     {"architecture_checked", true},
                     {"packed_sha256", digest}})
          .EncodeUtf8();
  auto packing_fields = BoundedJson(packing_base).Fields();
  packing_fields.emplace("source_population_validation",
                         population_result.text());
  const auto packing_report = BoundedJson::Encode(packing_fields);
  const auto placement_report =
      JsonRecord(Obj{{"status", "pass"},
                     {"schema", "emuflow.xilinx-placement-validation/v1"},
                     {"clusters", cluster_count},
                     {"cells", assignment_count},
                     {"cascade_chains", cascade_count},
                     {"placement_sha256", digest}})
          .EncodeUtf8();
  return BoundedJson::Encode(
      {{"packing", packing_report},
       {"placement", placement_report},
       {"source_population_validation", population_result.text()}});
}
StageResult
RunPhysicalHandoffStructure(const std::vector<ArtifactRef> &inputs,
                            const std::string &summary,
                            const volatile std::sig_atomic_t *cancelled) {
  bool full = false;
  return RunStage(
      "physical.validate-handoff-structure",
      [&] {
        Check(inputs.size() == 2 &&
                  inputs[0].schema() == "xilinx-physical-handoff-v1" &&
                  inputs[1].schema() == "physical-handoff-structure-request-v1",
              "structure source roles disagree");
        CheckedRequestReader reader(inputs[1].path(), 4096);
        Check(reader.Read<std::uint64_t>() == 0x31534850464d5545ULL &&
                  reader.Read<std::uint32_t>() == 1,
              "structure request version disagrees");
        Check(ReadFileGeneration(reader) == inputs[0].generation(),
              "structure source generation changed");
        const auto flag = reader.Read<std::uint8_t>();
        Check(flag <= 1, "invalid integrity flag");
        full = flag;
        reader.RequireEnd();
        return inputs;
      },
      [&](const auto &sources) {
        const auto result =
            ValidatePhysicalHandoffStore(sources[0], full, cancelled);
        PublishTextArtifact(summary, sources,
                            [&](std::ostream &out) { out << result << '\n'; });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "physical-handoff-structure-report-v1")};
      });
}
StageResult
RunPhysicalHandoffAudit(const std::vector<ArtifactRef> &inputs,
                        const std::string &summary,
                        const volatile std::sig_atomic_t *cancelled) {
  unsigned flags = 0;
  return RunStage(
      "physical.audit-handoff",
      [&] {
        Check(inputs.size() >= 4 && inputs.size() <= 7 &&
                  inputs[0].schema() == "xilinx-mapped-index-v2" &&
                  inputs[1].schema() == "xilinx-physical-handoff-v1" &&
                  inputs.back().schema() == "physical-handoff-audit-request-v1",
              "audit source roles disagree");
        CheckedRequestReader reader(inputs.back().path(), 4096);
        Check(reader.Read<std::uint64_t>() == 0x31414850464d5545ULL &&
                  reader.Read<std::uint32_t>() == 1,
              "audit request version disagrees");
        flags = reader.Read<std::uint8_t>();
        Check(flags <= 3 && inputs.size() == 4 + ((flags & 1) ? 2 : 0) +
                                                 ((flags & 2) ? 1 : 0),
              "optional audit source coverage disagrees");
        for (std::size_t i = 0; i + 1 < inputs.size(); ++i) {
          if (i >= 2)
            Check(inputs[i].schema() == "physical-input-document-v1",
                  "audit document source role disagrees");
          if (!(ReadFileGeneration(reader) == inputs[i].generation()))
            throw StageError(ErrorCode::kMixedGeneration,
                             "handoff audit source generation changed");
        }
        reader.RequireEnd();
        return inputs;
      },
      [&](const auto &sources) {
        Scratch scratch(fs::temp_directory_path());
        SqliteDatabase db((scratch.root / "device.sqlite").string(),
                          SqliteDatabase::Mode::kCreateExclusive);
        const ArtifactRef *native = (flags & 1) ? &sources[3] : nullptr;
        const ArtifactRef *provider = (flags & 1) ? &sources[4] : nullptr;
        const ArtifactRef *constraints =
            (flags & 2) ? &sources[(flags & 1) ? 5 : 3] : nullptr;
        PhysicalDevice device(db, sources[2], native, provider, cancelled);
        const auto validation = AuditPhysicalHandoff(
            sources[0], sources[1], device, constraints, cancelled);
        const auto result = BoundedJson::Encode(
            {{"schema", JsonQuote("emuflow.physical-handoff-audit/v1")},
             {"status", JsonQuote("pass")},
             {"implementation_id",
              JsonQuote("native-physical-handoff-audit-v1")},
             {"validation", validation}});
        PublishTextArtifact(summary, sources,
                            [&](std::ostream &out) { out << result << '\n'; });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "physical-handoff-audit-report-v1")};
      });
}
} // namespace emuflow::core
