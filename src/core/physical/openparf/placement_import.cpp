#include "physical/openparf/placement_import.hpp"
#include "physical/handoff.hpp"
#include "physical/openparf/placement_records.hpp"
#include "storage/schema/openparf_input_schema.hpp"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <sys/file.h>
namespace emuflow::core::openparf_placement {
namespace {
namespace fs = std::filesystem;
struct Output {
  fs::path root;
  int lock = -1;
  bool accepted = false;
  explicit Output(const fs::path &report) {
    Check(report.is_absolute() && !fs::is_symlink(report),
          "placement report must be an absolute file destination");
    fs::create_directories(report.parent_path());
    lock = open((report.string() + ".lock").c_str(),
                O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    Check(lock >= 0, "cannot open placement output lease");
    if (flock(lock, LOCK_EX | LOCK_NB) != 0) {
      close(lock);
      lock = -1;
      Check(false, "placement output has an active owner");
    }
    auto name =
        (report.parent_path() / "placement-result.generation.XXXXXX").string();
    if (!mkdtemp(name.data())) {
      close(lock);
      lock = -1;
      Check(false, "cannot create placement generation");
    }
    root = name;
  }
  ~Output() {
    if (!accepted) {
      std::error_code e;
      fs::remove_all(root, e);
    }
    if (lock >= 0)
      close(lock);
  }
};
ArtifactRef Add(BoundRequest &input, const std::string &role,
                const fs::path &path, const std::string &schema) {
  auto ref = ArtifactRef::Bind(path.string(), schema);
  Check(input.roles.emplace(role, ref).second,
        "duplicate placement source role");
  input.refs.push_back(ref);
  return ref;
}
ArtifactRef Payload(BoundRequest &input, const std::string &owner,
                    const std::string &role, const R &descriptor,
                    const std::string &filename, const std::string &schema) {
  const auto &selector = input.get(owner);
  const auto name = descriptor.at(filename).text();
  Check(!name.empty() && fs::path(name).filename() == name && name != "." &&
            name != "..",
        "invalid placement payload path");
  auto ref =
      Add(input, role, fs::path(selector.path()).parent_path() / name, schema);
  Check(fs::path(ref.path()).parent_path() ==
            fs::path(selector.path()).parent_path(),
        "placement payload escapes selector directory");
  return ref;
}
BoundRequest Request(const ArtifactRef &request) {
  BoundedJson config(BoundedJson::Read(request));
  config.Keys({"schema", "sources", "metadata", "legalize_ff", "top"});
  Check(config.Text("schema") == "emuflow.openparf-placement-import-request/v1",
        "placement request version disagrees");
  BoundRequest input;
  input.config = R::Parse(config.text());
  const std::map<std::string, std::string> allowed = {
      {"mapped", "xilinx-mapped-index-v2"},
      {"placement", "physical-input-document-v1"},
      {"name_selector", "openparf-name-map-selector-v1"},
      {"packed", "openparf-atomic-source-selector-v1"},
      {"architecture", "physical-input-document-v1"},
      {"native", "physical-input-document-v1"},
      {"provider", "physical-input-document-v1"},
      {"constraints", "physical-input-document-v1"}};
  for (const auto &s : input.config.at("sources").array()) {
    const auto role = s.at("role").text();
    Check(allowed.count(role) && allowed.at(role) == s.at("schema").text(),
          "invalid placement source role/schema");
    const auto ref =
        Add(input, role, s.at("path").text(), s.at("schema").text());
    const auto &g = ref.generation();
    Check(s.at("generation").encode() ==
              R(A{g.device, g.inode, g.bytes, g.modified_seconds,
                  g.modified_nanoseconds, g.changed_seconds,
                  g.changed_nanoseconds})
                  .encode(),
          "placement input generation changed");
  }
  for (const auto &role :
       {"mapped", "placement", "name_selector", "packed", "architecture"})
    input.get(role);
  Check(input.roles.count("native") == input.roles.count("provider"),
        "placement requires both native constraints and provider");
  input.refs.push_back(request);
  return input;
}
R NameMap(BoundRequest &input, SqliteDatabase &db,
          const volatile std::sig_atomic_t *flag) {
  auto selector = Read(input.get("name_selector"));
  Check(selector.at("schema").text() ==
                "emuflow.openparf-atomic-name-map-storage/v1" &&
            selector.at("logical_schema").text() ==
                "emuflow.openparf-atomic-name-map/v2" &&
            selector.at("application_id").integer() == 0x454d554e &&
            selector.at("user_version").integer() == 1,
        "name-map selector identity disagrees");
  auto ref = Payload(input, "name_selector", "name_store", selector, "artifact",
                     "openparf-name-map-store-v1");
  Check(ref.generation().bytes ==
                std::uint64_t(selector.at("store_bytes").integer()) &&
            FileHash(ref, flag) == selector.at("store_sha256").text(),
        "name-map payload seal disagrees");
  SqliteStore names(ref);
  auto &n = names.database();
  RequireSqliteDDL(n, kOpenparfNameMapDDL);
  for (const auto &field : {"header", "source", "counts", "logical_sha256"})
    Check(R::Parse(Meta(n, field)).encode() == selector.at(field).encode(),
          "name-map metadata disagrees");
  Check(Count(n, "SELECT count(*) FROM metadata") == 4,
        "unexpected name-map metadata");
  auto header = selector.at("header");
  Check(header.at("schema").text() == "emuflow.openparf-atomic-name-map/v2",
        "name-map header identity disagrees");
  const auto source = selector.at("source");
  for (const auto &field : std::map<std::string, std::string>{
           {"mapped_sha256", "mapped"},
           {"packed_sha256", "packed"},
           {"architecture_sha256", "architecture"}})
    Check(FileHash(input.get(field.second), flag) ==
              source.at(field.first).text(),
          "name-map source seal disagrees: " + field.first);
  Sha256 atoms, cascades;
  I count = 0, chains = 0;
  auto rows = n.Prepare(
      "SELECT seq,openparf,instance,cell_type,source_cluster,resource FROM "
      "atoms ORDER BY seq");
  while (rows.Step()) {
    Stop(flag);
    Check(rows.TypedInteger(0) == count++, "name-map atom sequence incomplete");
    Feed(atoms, R(O{{"openparf", rows.TypedText(1)},
                    {"instance", rows.TypedText(2)},
                    {"cell_type", rows.TypedText(3)},
                    {"source_cluster", rows.TypedText(4)},
                    {"resource", rows.TypedText(5)}})
                        .encode() +
                    "\n");
  }
  auto c = n.Prepare("SELECT seq,record_json FROM carry_cascades ORDER BY seq");
  while (c.Step()) {
    Stop(flag);
    Check(c.TypedInteger(0) == chains++, "name-map carry sequence incomplete");
    Feed(cascades, R::Parse(c.TypedText(1)).encode() + "\n");
  }
  Check(count > 0 && selector.at("counts").at("atoms").integer() == count &&
            selector.at("counts").at("carry_cascade_chains").integer() ==
                chains,
        "name-map counts disagree");
  Check(Hash(R(O{{"header", header},
                 {"source", source},
                 {"counts", selector.at("counts")},
                 {"atom_sha256", Hex(atoms)},
                 {"cascade_sha256", Hex(cascades)}})
                 .encode()) == selector.at("logical_sha256").text(),
        "name-map logical seal disagrees");
  Attach(db, ref, "names");
  const auto site = header.at("site_database");
  Check(site.at("schema").text() == "emuflow.openparf-atomic-site-database/v1",
        "site store descriptor identity disagrees");
  auto site_ref = Payload(input, "name_selector", "sites", site, "file",
                          "openparf-site-store-v1");
  Check(FileHash(site_ref, flag) == site.at("sha256").text(),
        "site store seal disagrees");
  SqliteStore site_owner(site_ref);
  auto &sites = site_owner.database();
  RequireSqliteDDL(sites, kOpenparfSitesDDL);
  Check(Meta(sites, "architecture_sha256") ==
            source.at("architecture_sha256").text(),
        "site store architecture source disagrees");
  Check(Count(sites, "SELECT count(*) FROM sites") ==
                site.at("sites").integer() &&
            Meta(sites, "site_count") ==
                std::to_string(site.at("sites").integer()),
        "site store count disagrees");
  Attach(db, site_ref, "site");
  auto packed = Read(input.get("packed"));
  Check(packed.at("schema").text() ==
            "emuflow.openparf-atomic-source-storage/v1",
        "placement requires the typed atomic source");
  auto atomic = Payload(input, "packed", "atomic_store", packed, "artifact",
                        "openparf-atomic-source-store-v1");
  Attach(db, atomic, "atomic");
  return selector;
}
void RawPlacement(SqliteDatabase &db, const ArtifactRef &placement,
                  const volatile std::sig_atomic_t *flag) {
  db.Execute("CREATE TABLE raw(openparf TEXT PRIMARY KEY,x INTEGER NOT NULL,y "
             "INTEGER NOT NULL,z INTEGER NOT NULL) WITHOUT ROWID;");
  auto insert = db.Prepare("INSERT INTO raw VALUES(?,?,?,?)");
  std::ifstream in(placement.path());
  Check(bool(in), "cannot read raw placement");
  std::string line;
  I number = 0;
  while (std::getline(in, line)) {
    Stop(flag);
    ++number;
    Check(line.size() <= 1024 * 1024 && line.find('\0') == std::string::npos,
          "raw placement record exceeds bound");
    std::istringstream row(line);
    std::vector<std::string> fields;
    std::string value;
    while (row >> value) {
      Check(fields.size() < 6, "invalid raw placement field count");
      fields.push_back(value);
    }
    if (fields.empty() || fields.front()[0] == '#')
      continue;
    Check(fields.size() == 4 || fields.size() == 5,
          "invalid raw placement row at line " + std::to_string(number));
    insert.Reset();
    insert.BindText(1, fields[0]);
    for (unsigned k = 0; k < 3; ++k) {
      const auto n =
          ParseFiniteDecimal(fields[k + 1], "raw placement coordinate");
      Check(std::floor(n) == n && std::abs(n) < double(1ULL << 53),
            "placement coordinate is not a discrete bounded integer");
      insert.BindInteger(k + 2, I(n));
    }
    insert.Execute();
  }
  Check(in.eof(), "cannot finish raw placement");
  placement.RequireUnchanged();
  None(db,
       "SELECT 1 FROM raw r LEFT JOIN names.atoms a USING(openparf) WHERE "
       "a.seq IS NULL LIMIT 1",
       "placement has an unknown atom");
  None(db,
       "SELECT 1 FROM names.atoms a LEFT JOIN raw r USING(openparf) WHERE "
       "r.openparf IS NULL LIMIT 1",
       "placement does not cover every atom");
}
std::string Slot(const std::string &resource, const std::string &type, I z) {
  Check(z >= 0 && z < 16, "invalid slice placement slot");
  const auto letter = std::string(1, char('A' + z / 2));
  if (resource == "LUT") {
    Check((z % 2) == 1 && (Lut(type) || type == "LUT6_2"),
          "unqualified even/paired LUT slot");
    return letter + "6LUT";
  }
  Check(resource == "FF" && Ff(type), "FF slot contains a non-FF primitive");
  return letter + (z % 2 ? "FF2" : "FF");
}
void Normalize(SqliteDatabase &db, PhysicalDevice &device,
               MappedPhysicalSemantics &mapped,
               const volatile std::sig_atomic_t *flag) {
  db.Execute("CREATE TABLE normalized(instance TEXT PRIMARY KEY,source_cluster "
             "TEXT NOT NULL,cell_type TEXT NOT NULL,resource TEXT NOT NULL,z "
             "INTEGER NOT NULL,x REAL NOT NULL,y REAL NOT NULL,site TEXT NOT "
             "NULL,anchor TEXT NOT NULL,bel TEXT NOT NULL,mode TEXT NOT "
             "NULL,bram TEXT) WITHOUT ROWID;CREATE UNIQUE INDEX "
             "normalized_slots ON normalized(site,resource,z);CREATE INDEX "
             "normalized_site ON normalized(anchor,instance);");
  auto rows =
      db.Prepare("SELECT "
                 "a.instance,a.source_cluster,a.cell_type,a.resource,r.x,r.y,r."
                 "z,s.placement_x,s.placement_y FROM raw r JOIN names.atoms a "
                 "USING(openparf) LEFT JOIN site.sites s ON s.dense_x=r.x AND "
                 "s.dense_y=r.y ORDER BY a.seq");
  auto names =
      db.Prepare("SELECT physical_site FROM site.physical_sites WHERE "
                 "dense_x=? AND dense_y=? AND resource=? ORDER BY slot");
  auto capacity = db.Prepare("SELECT capacity FROM site.resources WHERE "
                             "dense_x=? AND dense_y=? AND resource=?");
  auto put =
      db.Prepare("INSERT INTO normalized VALUES(?,?,?,?,?,?,?,?,?,?,?,?)");
  while (rows.Step()) {
    Stop(flag);
    const auto instance = rows.TypedText(0), source = rows.TypedText(1),
               type = rows.TypedText(2), resource = rows.TypedText(3);
    const auto x = rows.TypedInteger(4), y = rows.TypedInteger(5),
               z = rows.TypedInteger(6);
    Check(!rows.IsNull(7) && !rows.IsNull(8), "placement uses an unknown site");
    Check(mapped.CellType(instance) == type && Resource(type) == resource,
          "placed atom disagrees with mapped type/resource");
    capacity.Reset();
    capacity.BindInteger(1, x);
    capacity.BindInteger(2, y);
    capacity.BindText(3, resource);
    Check(capacity.Step() && capacity.TypedInteger(0) > 0,
          "placement site lacks required resource");
    names.Reset();
    names.BindInteger(1, x);
    names.BindInteger(2, y);
    names.BindText(3, resource);
    std::vector<std::string> physical;
    while (names.Step())
      physical.push_back(names.TypedText(0));
    Check(!physical.empty(), "placement resource has no physical sites");
    std::string anchor, site, bel, mode;
    R bram;
    if (resource == "LUT" || resource == "FF" || resource == "CARRY8" ||
        Mux(type)) {
      Check(physical.size() == 1,
            "logic placement has no unique physical slice");
      anchor = site = physical[0];
      if (resource == "LUT" || resource == "FF")
        bel = Slot(resource, type, z);
      else if (resource == "CARRY8") {
        Check(type == "CARRY8" && z == 0, "invalid CARRY8 slot");
        bel = "CARRY8";
      } else {
        const std::map<std::string, std::vector<std::string>> mux = {
            {"MUXF7", {"F7MUX_AB", "F7MUX_CD", "F7MUX_EF", "F7MUX_GH"}},
            {"MUXF8", {"F8MUX_BOT", "F8MUX_TOP"}},
            {"MUXF9", {"F9MUX"}}};
        Check(z >= 0 && std::size_t(z) < mux.at(type).size(),
              "invalid MUX slot");
        bel = mux.at(type)[z];
      }
    } else if (resource == "RAMB18E2" || resource == "RAMB36E2") {
      bool native = Count(db, "SELECT count(*) FROM pd_bram") > 0;
      if (native) {
        Check(physical.size() == 1, "native BRAM view has no unique anchor");
        anchor = physical[0];
        const std::string role = type == "RAMB36E2" ? "whole"
                                 : z == 0           ? "lower"
                                 : z == 1           ? "upper"
                                                    : "";
        Check((type == "RAMB18E2" && z >= 0 && z <= 1) ||
                  (type == "RAMB36E2" && z == 0),
              "BRAM uses an uncertified tile view");
        auto view = db.Prepare("SELECT tile,site,bel,site_type FROM pd_bram "
                               "WHERE anchor=? AND role=?");
        view.BindText(1, anchor);
        view.BindText(2, role);
        Check(view.Step(), "BRAM native view missing");
        site = view.TypedText(1);
        bel = view.TypedText(2);
        mode = view.TypedText(3);
        const auto tile = view.TypedText(0);
        Check(!view.Step(), "BRAM native view ambiguous");
        A claims;
        if (role == "whole")
          claims = {"bram:" + tile + ":lower", "bram:" + tile + ":upper"};
        else
          claims = {"bram:" + tile + ":" + role};
        bram = O{{"anchor", anchor},
                 {"tile", tile},
                 {"role", role},
                 {"claims", claims}};
      } else {
        Check(z >= 0 && std::size_t(z) < physical.size(),
              "invalid BRAM site slot");
        anchor = site = physical[z];
      }
    } else {
      Check(Hard(type) && z >= 0 && std::size_t(z) < physical.size(),
            "invalid hard-resource site slot");
      anchor = site = physical[z];
    }
    const auto site_fact = device.Site(anchor);
    auto compatible = db.Prepare(
        "SELECT b.bel,b.mode FROM pd_bels b WHERE b.base=? AND (?='' OR "
        "b.bel=?) AND EXISTS(SELECT 1 FROM json_each(b.cells) WHERE value=?)");
    compatible.BindText(1, site_fact.base);
    compatible.BindText(2, bel);
    compatible.BindText(3, bel);
    compatible.BindText(4, type);
    Check(compatible.Step(), "no compatible physical BEL");
    bel = compatible.TypedText(0);
    const auto declared_mode = compatible.TypedText(1);
    Check(!compatible.Step(), "physical BEL mapping is ambiguous");
    if (mode.empty())
      mode = declared_mode;
    Check(mode == declared_mode, "native BRAM mode differs from architecture");
    const auto binding = device.ResolveBel(anchor, bel, type, {mode});
    Check(binding.physical_site == site,
          "normalized physical site disagrees with native BEL view");
    put.Reset();
    for (const auto &field :
         std::vector<std::pair<int, std::string>>{{1, instance},
                                                  {2, source},
                                                  {3, type},
                                                  {4, resource},
                                                  {8, site},
                                                  {9, anchor},
                                                  {10, bel},
                                                  {11, mode}})
      put.BindText(field.first, field.second);
    put.BindInteger(5, z);
    put.BindReal(6, rows.Real(7));
    put.BindReal(7, rows.Real(8));
    if (bram.null())
      put.BindNull(12);
    else
      put.BindText(12, Record(bram).Encode());
    put.Execute();
  }
}
R Header(SqliteDatabase &db, PhysicalDevice &device, const BoundRequest &input,
         const ArtifactRef &placement, R metadata, const R &ff,
         const volatile std::sig_atomic_t *flag) {
  O hard;
  auto counts =
      db.Prepare("SELECT resource,count(*) FROM normalized GROUP BY resource");
  O summary{{"atoms", Count(db, "SELECT count(*) FROM normalized")},
            {"occupied_sites",
             Count(db, "SELECT count(DISTINCT anchor) FROM normalized")},
            {"luts", 0},
            {"ffs", 0}};
  while (counts.Step()) {
    const auto resource = counts.TypedText(0);
    const auto count = counts.TypedInteger(1);
    if (resource == "LUT")
      summary["luts"] = count;
    else if (resource == "FF")
      summary["ffs"] = count;
    else if (resource == "CARRY8")
      summary["carry8_macros"] = count;
    else if (Hard(resource))
      hard[resource] = count;
  }
  summary["hard_resources"] = hard;
  const auto carry =
                 Count(db, "SELECT "
                           "coalesce(sum(max(0,json_array_length(record_json,'$"
                           ".instances')-1)),0) FROM atomic.cascades WHERE "
                           "json_extract(record_json,'$.cell_type')='CARRY8'"),
             native =
                 Count(db, "SELECT "
                           "coalesce(sum(max(0,json_array_length(record_json,'$"
                           ".instances')-1)),0) FROM atomic.cascades WHERE "
                           "json_extract(record_json,'$.cell_type')!='CARRY8'");
  if (carry)
    summary["native_carry_edges"] = carry;
  if (native)
    summary["native_hardblock_edges"] = native;
  R source = O{{"native_placement_sha256", FileHash(placement, flag)},
               {"name_map_sha256", FileHash(input.get("name_selector"), flag)},
               {"mapped_sha256", FileHash(input.get("mapped"), flag)},
               {"architecture_sha256", device.architecture_sha()},
               {"packed_sha256", FileHash(input.get("packed"), flag)}};
  if (input.roles.count("native")) {
    source["native_constraints_sha256"] = device.native_sha();
    source["provider_manifest_sha256"] = device.provider_sha();
  }
  Check(std::holds_alternative<O>(metadata.value),
        "invalid placement metadata");
  for (const auto &f : std::get<O>(metadata.value))
    Check(f.first == "native_convergence" || f.first == "runtime_validation",
          "unsupported placement metadata");
  const auto runtime = metadata.has("runtime_validation")
                           ? metadata.at("runtime_validation").text()
                           : "unverified";
  Check(runtime == "unverified" || runtime == "native-openparf",
        "invalid placement runtime qualification");
  R out = O{{"schema", "emuflow.openparf-atomic-placement/v2"},
            {"status", "pass"},
            {"part", device.part()},
            {"provider",
             "openparf-native-rudy-pin-aware-mcf-direct-lg-ism-atomic-v8"},
            {"runtime_validation", runtime},
            {"source", source},
            {"summary", summary}};
  if (metadata.has("native_convergence"))
    out["native_convergence"] = metadata.at("native_convergence");
  if (!ff.null())
    out["ff_control_legalization"] = ff;
  return out;
}
R Certificate(SqliteDatabase &db, PhysicalDevice &device,
              const BoundRequest &input, const R &header, const fs::path &root,
              const volatile std::sig_atomic_t *flag) {
  const auto path = root / "certificate.private.sqlite";
  I groups = 0, members = 0;
  Sha256 digest;
  auto append = [&](const std::string &kind, const R &value) {
    const auto encoded =
        Record(R(O{{"kind", kind}, {"value", value}})).EncodeUtf8();
    const auto n = std::uint64_t(encoded.size());
    std::array<unsigned char, 8> bytes{};
    for (unsigned k = 0; k < 8; ++k)
      bytes[7 - k] = n >> (k * 8);
    digest.update(bytes.data(), 8);
    Feed(digest, encoded);
  };
  append("header/v1", header);
  {
    SqliteDatabase out(path.string(), SqliteDatabase::Mode::kCreateExclusive);
    out.Execute("PRAGMA page_size=65536;PRAGMA journal_mode=OFF;PRAGMA "
                "synchronous=FULL;PRAGMA temp_store=FILE;PRAGMA "
                "application_id=0x454d5550;PRAGMA user_version=3;");
    out.Execute(kOpenparfPlacementDDL);
    SqliteTransaction transaction(out);
    {
      auto anchors =
          db.Prepare("SELECT DISTINCT anchor FROM normalized ORDER BY anchor");
      auto cluster = out.Prepare("INSERT INTO clusters VALUES(?,?,?,?,?,?)");
      while (anchors.Step()) {
        Stop(flag);
        const auto site = device.Site(anchors.TypedText(0));
        R row = A{groups, "openparf:" + site.name, site.name, site.type, site.x,
                  site.y};
        append("cluster/v1", row);
        cluster.Reset();
        cluster.BindInteger(1, groups++);
        cluster.BindText(2, "openparf:" + site.name);
        cluster.BindText(3, site.name);
        cluster.BindText(4, site.type);
        cluster.BindInteger(5, site.x);
        cluster.BindInteger(6, site.y);
        cluster.Execute();
      }
    }
    {
      auto rows = db.Prepare(
          "SELECT anchor,instance,cell_type,bel,site,mode,source_cluster,bram "
          "FROM normalized ORDER BY anchor,instance");
      auto assignment =
          out.Prepare("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?)");
      std::string previous;
      I index = -1, ordinal = 0;
      while (rows.Step()) {
        Stop(flag);
        const auto anchor = rows.TypedText(0);
        if (anchor != previous) {
          previous = anchor;
          ++index;
          ordinal = 0;
        }
        R bram = rows.IsNull(7) ? R() : R(rows.TypedText(7));
        R row = A{index,
                  ordinal,
                  rows.TypedText(1),
                  rows.TypedText(2),
                  rows.TypedText(3),
                  rows.TypedText(4),
                  rows.TypedText(5),
                  rows.TypedText(6),
                  bram};
        append("assignment/v1", row);
        assignment.Reset();
        assignment.BindInteger(1, index);
        assignment.BindInteger(2, ordinal++);
        for (int k = 1; k < 7; ++k)
          assignment.BindText(k + 2, rows.TypedText(k));
        if (rows.IsNull(7))
          assignment.BindNull(9);
        else
          assignment.BindText(9, rows.TypedText(7));
        assignment.Execute();
        ++members;
      }
    }
    Check(groups > 0 && members > 0, "placement has no physical clusters");
    auto meta = out.Prepare("INSERT INTO metadata VALUES(?,?)");
    for (const auto &field : std::map<std::string, std::string>{
             {"schema", "emuflow.openparf-atomic-placement-database/v3"},
             {"clusters", std::to_string(groups)},
             {"assignments", std::to_string(members)},
             {"logical_sha256", Hex(digest)}}) {
      meta.Reset();
      meta.BindText(1, field.first);
      meta.BindText(2, field.second);
      meta.Execute();
    }
    transaction.Commit();
  }
  auto ref =
      ArtifactRef::Bind(path.string(), "openparf-placement-certificate-v3");
  const auto hash = FileHash(ref, flag),
             filename = "placement-certificate." + hash + ".sqlite";
  for (const auto &source : input.refs)
    source.RequireUnchanged();
  std::filesystem::rename(path, root / filename);
  ref = ArtifactRef::Bind((root / filename).string(),
                          "openparf-placement-certificate-v3");
  SqliteStore owner(ref);
  const auto logical = Meta(owner.database(), "logical_sha256");
  R result = header;
  result["cluster_storage"] =
      O{{"schema", "emuflow.openparf-atomic-placement-database/v3"},
        {"application_id", 0x454d5550},
        {"user_version", 3},
        {"file", filename},
        {"bytes", ref.generation().bytes},
        {"sha256", hash},
        {"clusters", groups},
        {"assignments", members},
        {"logical_sha256", logical}};
  Json(root / "placement-certificate.json", result, input.refs);
  return result;
}
R Handoff(BoundRequest &input, const R &certificate, const fs::path &root,
          const volatile std::sig_atomic_t *flag) {
  auto sources = input.refs;
  const std::vector<std::pair<std::string, ArtifactRef>> base = {
      {"mapped", input.get("mapped")},
      {"certificate",
       ArtifactRef::Bind(
           (root / certificate.at("cluster_storage").at("file").text())
               .string(),
           "openparf-placement-certificate-v3")},
      {"certificate_selector",
       ArtifactRef::Bind((root / "placement-certificate.json").string(),
                         "openparf-placement-selector-v2")},
      {"atomic_source", input.get("atomic_store")},
      {"atomic_selector", input.get("packed")},
      {"architecture", input.get("architecture")}};
  auto roles = base;
  for (const auto &name : {"native", "provider", "constraints"})
    if (input.roles.count(name))
      roles.emplace_back(name, input.get(name));
  const auto path = root / "handoff.bin";
  CheckedRequestWriter writer(path.string(), sources);
  writer.Put<std::uint64_t>(0x31424850464d5545ULL);
  writer.Put<std::uint32_t>(1);
  writer.Put<std::uint32_t>(roles.size());
  for (const auto &role : roles) {
    writer.Text(role.first);
    writer.Text(role.second.path());
    writer.Text(role.second.schema());
    writer.Generation(role.second.generation());
    sources.push_back(role.second);
  }
  writer.Text(R(O{{"schema", "emuflow.physical-handoff-build/v1"},
                  {"top", input.config.at("top")}})
                  .encode());
  writer.Finish();
  const auto result = BuildPhysicalHandoff(
      ArtifactRef::Bind(path.string(), "physical-handoff-bridge-request-v1"),
      (root / "handoff").string(), flag);
  if (!result.ok())
    throw StageError(result.error, result.message);
  for (const auto &r : result.outputs) {
    r.RequireUnchanged();
    input.refs.push_back(r);
  }
  return Read(
      ArtifactRef::Bind((root / "handoff/physical-bridge-report.json").string(),
                        "physical-handoff-bridge-report-v1"));
}
} // namespace
} // namespace emuflow::core::openparf_placement
namespace emuflow::core {
StageResult ImportOpenparfPlacement(const ArtifactRef &request,
                                    const std::string &report,
                                    const volatile std::sig_atomic_t *flag) {
  using namespace openparf_placement;
  using namespace openparf_input;
  namespace fs = std::filesystem;
  std::optional<BoundRequest> input;
  std::vector<ArtifactRef> published;
  return RunStage(
      "physical.import-openparf-placement",
      [&] {
        input = Request(request);
        return input->refs;
      },
      [&](const auto &) {
        auto &in = *input;
        const auto target = fs::absolute(report);
        for (const auto &source : in.refs) {
          std::error_code error;
          Check(!fs::equivalent(target, source.path(), error) &&
                    target != source.path(),
                "placement output aliases a source");
        }
        Output output(target);
        const auto root = output.root;
        const auto &metadata = in.config.at("metadata");
        if (metadata.has("native_convergence")) {
          const auto &artifact =
              metadata.at("native_convergence").at("artifact");
          auto metrics = Add(in, "metrics", artifact.at("path").text(),
                             "physical-input-document-v1");
          Check(metrics.generation().bytes ==
                        std::uint64_t(artifact.at("bytes").integer()) &&
                    FileHash(metrics, flag) == artifact.at("sha256").text(),
                "native convergence artifact seal disagrees");
        }
        R result;
        {
          SqliteDatabase db((root / "import.sqlite").string(),
                            SqliteDatabase::Mode::kCreateExclusive);
          db.Execute("PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;PRAGMA "
                     "temp_store=FILE;PRAGMA cache_size=-16384;");
          const auto names = NameMap(in, db, flag);
          const auto top =
              R::Parse(
                  Meta(SqliteStore(in.get("mapped")).database(), "top_json"))
                  .text();
          Check(names.at("header").at("top").text() == top &&
                    (in.config.at("top").text().empty() ||
                     in.config.at("top").text() == top),
                "placement top selection disagrees");
          RawPlacement(db, in.get("placement"), flag);
          MappedPhysicalSemantics mapped(in.get("mapped"));
          R ff;
          ArtifactRef placement = in.get("placement");
          const auto legalize =
              std::get_if<bool>(&in.config.at("legalize_ff").value);
          Check(legalize, "invalid FF legalization policy");
          if (*legalize) {
            ff = LegalizeFf(db, mapped, flag);
            ff["placement_before_sha256"] = FileHash(placement, flag);
            if (ff.at("moved_ff_atoms").integer()) {
              const auto path = root / "legalized.pl";
              PublishTextArtifact(path.string(), in.refs, [&](auto &stream) {
                auto rows = db.Prepare(
                    "SELECT openparf,x,y,z FROM raw ORDER BY openparf");
                while (rows.Step()) {
                  Stop(flag);
                  stream << rows.TypedText(0) << ' ' << rows.TypedInteger(1)
                         << ' ' << rows.TypedInteger(2) << ' '
                         << rows.TypedInteger(3) << '\n';
                }
              });
              placement = ArtifactRef::Bind(path.string(),
                                            "physical-input-document-v1");
            }
            ff["placement_after_sha256"] = FileHash(placement, flag);
            if (placement.path() != in.get("placement").path())
              in.refs.push_back(placement);
            Json(root / "ff-control-set-legalization.json", ff, in.refs);
          }
          const bool native = in.roles.count("native");
          PhysicalDevice device(db, in.get("architecture"),
                                native ? &in.get("native") : nullptr,
                                native ? &in.get("provider") : nullptr, flag);
          Normalize(db, device, mapped, flag);
          CheckWindows(db, in, names.at("header"), flag);
          const auto header = Header(db, device, in, placement,
                                     in.config.at("metadata"), ff, flag);
          const auto certificate =
              Certificate(db, device, in, header, root, flag);
          for (const auto &source : std::vector<ArtifactRef>{
                   ArtifactRef::Bind(
                       (root / "placement-certificate.json").string(),
                       "openparf-placement-selector-v2"),
                   ArtifactRef::Bind(
                       (root /
                        certificate.at("cluster_storage").at("file").text())
                           .string(),
                       "openparf-placement-certificate-v3")}) {
            in.refs.push_back(source);
            published.push_back(source);
          }
          device.RequireUnchanged();
          mapped.RequireUnchanged();
          // The final independent authority reopens mapped/device/source facts.
          // It checks topology, control sets, ownership, native adjacency and
          // budgets; it does not reuse the raw importer's accepted
          // normalization decisions.
          const auto handoff = Handoff(in, certificate, root, flag);
          const auto count = in.refs.size();
          Check(count >= 2, "native handoff returned no accepted owners");
          published.push_back(in.refs[count - 2]);
          published.push_back(in.refs[count - 1]);
          const auto &g = published[published.size() - 2].generation();
          R generation = A{g.device,
                           g.inode,
                           g.bytes,
                           g.modified_seconds,
                           g.modified_nanoseconds,
                           g.changed_seconds,
                           g.changed_nanoseconds};
          result =
              O{{"schema", "emuflow.openparf-placement-import-report/v1"},
                {"status", "pass"},
                {"implementation_id", "native-openparf-placement-import-v1"},
                {"placement", placement.path()},
                {"certificate_path",
                 (root / "placement-certificate.json").string()},
                {"certificate", certificate},
                {"bridge", handoff},
                {"handoff_generation", generation}};
          if (!ff.null())
            result["ff_control_legalization"] = ff;
        }
        Stop(flag);
        Json(target, result, in.refs);
        output.accepted = true;
        published.insert(
            published.begin(),
            ArtifactRef::Bind(target.string(),
                              "openparf-placement-import-report-v1"));
        // The transient import relation is no longer an owner after
        // publication.
        std::error_code error;
        fs::remove(root / "import.sqlite", error);
        fs::remove(root / "handoff.bin", error);
      },
      [&] {
        for (const auto &source : input->refs)
          source.RequireUnchanged();
        for (const auto &source : published)
          source.RequireUnchanged();
        return published;
      });
}
} // namespace emuflow::core
