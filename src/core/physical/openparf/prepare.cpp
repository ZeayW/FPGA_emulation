#include "physical/handoff.hpp"
#include "physical/openparf/bookshelf.hpp"
#include "physical/openparf/geometry.hpp"
#include "physical/openparf/input_records.hpp"
#include "physical/openparf/inputs.hpp"
#include "storage/schema/openparf_input_schema.hpp"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <set>
#include <sstream>
#include <sys/file.h>
namespace emuflow::core::openparf_input {
namespace fs = std::filesystem;
namespace {
std::string SourceCluster(I index) { return Pad("atomic-source-", index); }
int Slot(const PhysicalAssignment &m) {
  if (Lut(m.type) || m.type == "LUT6_2")
    return 2 * (m.bel[0] - 'A') + 1;
  if (m.type == "CARRY8")
    return 0;
  const std::map<std::string, int> mux{
      {"F7MUX_AB", 0},  {"F7MUX_CD", 1},  {"F7MUX_EF", 2}, {"F7MUX_GH", 3},
      {"F8MUX_BOT", 0}, {"F8MUX_TOP", 1}, {"F9MUX", 0}};
  auto p = mux.find(m.bel);
  Check(p != mux.end(), "invalid slice macro role");
  return p->second;
}
std::vector<std::string> OrderedResources(const Capacities &demand) {
  std::vector<std::string> out;
  for (auto &p : demand)
    out.push_back(p.first);
  std::sort(out.begin(), out.end(), [](auto a, auto b) {
    auto rank = [](auto r) { return r == "LUT" ? 0 : r == "FF" ? 1 : 2; };
    return std::make_pair(rank(a), a) < std::make_pair(rank(b), b);
  });
  return out;
}
struct Collected {
  Capacities demand;
  std::set<std::string> types;
  std::vector<R> cascades;
  I count = 0;
  std::string top;
};
Collected Collect(const BoundRequest &input, const SourceInfo &derived,
                  const fs::path &path,
                  const volatile std::sig_atomic_t *flag) {
  Collected out;
  auto mapped = input.get("mapped"), identity = input.get("mapped_identity");
  Check(identity.schema() == "xilinx-mapped-index-v2" ||
            identity.schema() == "xilinx-mapped-json-v1",
        "unsupported mapped identity schema");
  if (identity.schema() == "xilinx-mapped-index-v2")
    Check(identity.path() == mapped.path() &&
              identity.generation() == mapped.generation(),
          "mapped physical identity belongs to another artifact");
  else
    Check(identity.generation().bytes <= 8 * 1024 * 1024,
          "raw mapped identity exceeds bounded fixture reader");

  SqliteStore owner(mapped);
  auto &m = owner.database();
  out.top = R::Parse(Meta(m, "top_json")).text();
  auto doc = Read(input.get("packed"));
  Check(doc.at("schema").text() ==
                "emuflow.openparf-atomic-source-storage/v1" ||
            doc.at("schema").text() == "emuflow.openparf-atomic-source/v1" ||
            doc.at("schema").text() == "emuflow.packed-site-netlist/v1",
        "atomic source header is invalid");
  if (doc.has("top"))
    Check(doc.at("top").text() == out.top,
          "atomic source top disagrees with mapped input");
  const bool typed =
      doc.at("schema").text() == "emuflow.openparf-atomic-source-storage/v1";
  std::optional<SqliteStore> store;
  if (typed) {
    auto name = doc.at("artifact").text();
    Check(fs::path(name).filename() == name, "invalid atomic source path");
    auto ref = input.get("atomic_store");
    Check(FileHash(ref, flag) == doc.at("store_sha256").text() &&
              I(ref.generation().bytes) == doc.at("store_bytes").integer(),
          "atomic source file seal disagrees");
    AuditOpenparfSource(input.get("packed"), ref, flag);
    store.emplace(ref);
    for (auto k : {"header", "counts", "mapped", "logical_sha256"})
      Check(R::Parse(Meta(store->database(), k)).encode() == doc.at(k).encode(),
            "atomic source metadata disagrees");
    auto header = doc.at("header");
    auto descriptor = doc.at("mapped");
    Check(descriptor.at("artifact").text() ==
                  fs::path(identity.path()).filename().string() &&
              descriptor.at("bytes").integer() ==
                  I(identity.generation().bytes) &&
              descriptor.at("sha256").text() == FileHash(identity, flag),
          "atomic mapped descriptor disagrees");
    auto constants = store->database().Prepare(
        "SELECT instance FROM constants ORDER BY instance");
    auto mapped_constants = m.Prepare(
        "SELECT id FROM cells WHERE type IN ('GND','VCC') ORDER BY id");
    while (mapped_constants.Step())
      Check(constants.Step() &&
                constants.TypedText(0) == mapped_constants.TypedText(0),
            "atomic source constant coverage disagrees");
    Check(!constants.Step(), "atomic source has an unmatched constant");

    Check(header.at("source").at("mapped_sha256").text() ==
                  FileHash(identity, flag) &&
              header.at("top").text() == out.top,
          "atomic source identity is invalid");
  } else if (doc.at("schema").text() == "emuflow.openparf-atomic-source/v1")
    Check(doc.at("status").text() == "pass" &&
              doc.at("source").at("mapped_sha256").text() ==
                  FileHash(identity, flag),
          "atomic source identity is invalid");
  SqliteDatabase atoms(path.string(), SqliteDatabase::Mode::kCreateExclusive);
  atoms.Execute("CREATE TABLE raw_atoms(instance TEXT PRIMARY KEY,cell_type "
                "TEXT,source_cluster TEXT,resource TEXT) WITHOUT ROWID;BEGIN");
  std::map<std::string, const Macro *> expected;
  for (auto &macro : derived.macros)
    for (auto &member : macro.members)
      expected[member.instance] = &macro;
  std::set<std::string> covered;
  auto cluster = [&](const R &c) {
    Stop(flag);
    auto object = std::get<O>(c.value);
    for (auto &field : object)
      Check(std::set<std::string>{"id", "kind", "site_templates", "site_mode",
                                  "control_set", "control_sets", "assignments"}
                .count(field.first),
            "unsupported relative/physical constraints");
    auto id = c.at("id").text(), kind = c.at("kind").text();
    Check(!id.empty() && (kind == "slice" || kind == "carry" || kind == "hard"),
          "cluster kind '" + kind + "' is invalid");
    auto &entries = c.at("assignments").array();
    Check(!entries.empty() && entries.size() <= 256,
          "cluster has no assignments or exceeds site bound");
    const Macro *macro = nullptr;
    std::set<std::string> names;
    std::set<std::string> types;
    for (auto &a : entries) {
      for (auto &f : std::get<O>(a.value))
        Check(std::set<std::string>{"instance", "cell_type", "bel",
                                    "bel_candidates"}
                  .count(f.first),
              "unsupported relative constraints");
      auto name = a.at("instance").text(), type = a.at("cell_type").text();
      names.insert(name);
      types.insert(type);
      Check(!name.empty(), "empty instance");
      if (Mux(type) || type == "CARRY8" || type == "LUT6_2" ||
          (typed && type == "RAMB18E2")) {
        auto e = expected.find(name);
        Check(e != expected.end(), "slice cluster does not match one complete "
                                   "connectivity-derived macro");
        Check(!macro || macro == e->second,
              "slice cluster does not match one complete connectivity-derived "
              "macro");
        macro = e->second;
      }
      Check(kind != "slice" || Lut(type) || Ff(type) || Mux(type),
            "slice cluster contains hard primitive");
      Check(kind != "carry" || type == "CARRY8" || type == "LUT6_2",
            "carry cluster contains unsupported primitive");
      Check(kind != "hard" || Hard(type),
            "hard cluster contains unsupported primitive");
      auto r = atoms.Prepare("INSERT INTO raw_atoms VALUES(?,?,?,?)");
      r.BindText(1, name);
      r.BindText(2, type);
      r.BindText(3, id);
      r.BindText(4, Resource(type));
      r.Execute();
    }
    if (macro) {
      Check(names.size() == macro->members.size(),
            "slice cluster does not match one complete connectivity-derived "
            "macro");
      Check(kind == (macro->kind == "carry8-lut6_2"                ? "carry"
                     : macro->kind == "ramb18-half-site-occupancy" ? "hard"
                                                                   : "slice"),
            "physical slice macro has an invalid cluster kind");
      for (auto &member : macro->members) {
        Check(names.count(member.instance), "incomplete physical macro");
        auto a = std::find_if(entries.begin(), entries.end(), [&](auto &a) {
          return a.at("instance").text() == member.instance;
        });
        if (member.type == "RAMB18E2")
          Check((!a->has("bel") || a->at("bel").null()) &&
                    a->at("bel_candidates").encode() ==
                        R(A{"RAMB18E2_L", "RAMB18E2_U"}).encode(),
                "RAMB18E2 atomic half-site occupancy is invalid");
        else
          Check(a->has("bel") && a->at("bel").text() == member.bel,
                "slice cluster differs from connectivity-derived BEL roles");
      }
      covered.insert(macro->id);
    }
    if (kind == "hard" && !macro) {
      if (types == std::set<std::string>{"RAMB18E2"}) {
        Check(entries.size() <= 2 && c.has("site_mode") &&
                  c.at("site_mode").text() ==
                      "RAMB18E2x" + std::to_string(entries.size()),
              "RAMB18E2 source packing is invalid");
        std::set<std::string> bels;
        for (auto &a : entries)
          Check(bels.insert(a.at("bel").text()).second &&
                    (a.at("bel").text() == "RAMB18E2_L" ||
                     a.at("bel").text() == "RAMB18E2_U"),
                "RAMB18E2 source packing is invalid");
      } else
        Check(entries.size() == 1 && types.size() == 1 &&
                  (!c.has("site_mode") || c.at("site_mode").null()),
              "hard-resource support requires one cell and no site_mode");
    }
  };
  if (typed) {
    auto &s = store->database();
    auto rows = s.Prepare("SELECT "
                          "seq,cluster_id,kind,site_templates_json,control_set_"
                          "json,site_mode FROM clusters ORDER BY seq");
    while (rows.Step()) {
      A entries;
      auto a = s.Prepare("SELECT instance,cell_type,bel,bel_candidates_json "
                         "FROM assignments WHERE cluster_seq=? ORDER BY seq");
      a.BindInteger(1, rows.TypedInteger(0));
      while (a.Step()) {
        R entry = O{{"instance", a.TypedText(0)},
                    {"cell_type", a.TypedText(1)},
                    {"bel_candidates", R::Parse(a.TypedText(3))}};
        if (!a.IsNull(2))
          entry["bel"] = a.TypedText(2);
        entries.push_back(entry);
      }
      R c = O{{"id", rows.TypedText(1)},
              {"kind", rows.TypedText(2)},
              {"site_templates", R::Parse(rows.TypedText(3))},
              {"control_set", R::Parse(rows.TypedText(4))},
              {"assignments", entries}};
      if (!rows.IsNull(5))
        c["site_mode"] = rows.TypedText(5);
      cluster(c);
    }
    auto cells = m.Prepare("SELECT id,type FROM cells ORDER BY id");
    I rank = 0;
    while (cells.Step()) {
      auto name = cells.TypedText(0), type = cells.TypedText(1);
      if (type == "GND" || type == "VCC") {
        ++rank;
        continue;
      }
      auto find = atoms.Prepare("SELECT 1 FROM raw_atoms WHERE instance=?");
      find.BindText(1, name);
      if (!find.Step()) {
        auto r = atoms.Prepare("INSERT INTO raw_atoms VALUES(?,?,?,?)");
        r.BindText(1, name);
        r.BindText(2, type);
        r.BindText(3, SourceCluster(rank));
        r.BindText(4, Resource(type));
        r.Execute();
      }
      ++rank;
    }
    auto chains = s.Prepare("SELECT record_json FROM cascades ORDER BY seq");
    while (chains.Step())
      out.cascades.push_back(R::Parse(chains.TypedText(0)));
    Check(Count(s, "SELECT count(*) FROM assignments") ==
              doc.at("counts").at("macro_assignments").integer(),
          "atomic source counts disagree");
  } else {
    Check(!doc.at("clusters").array().empty(), "clusters are invalid");
    for (auto &c : doc.at("clusters").array())
      cluster(c);
    if (doc.has("cascade_chains"))
      out.cascades = doc.at("cascade_chains").array();
  }
  Check(covered.size() == derived.macros.size() ||
            (!typed && std::all_of(derived.macros.begin(), derived.macros.end(),
                                   [&](auto &a) {
                                     return a.kind ==
                                                "ramb18-half-site-occupancy" ||
                                            covered.count(a.id);
                                   })),
        "connectivity-derived slice macro coverage is incomplete");
  if (typed) {
    Check(out.cascades.size() == derived.cascades.size(),
          "atomic cascade coverage disagrees");
    for (std::size_t k = 0; k < out.cascades.size(); ++k)
      Check(out.cascades[k].encode() == derived.cascades[k].encode(),
            "atomic cascade differs from mapped connectivity");
  }
  if (!input.roles.count("native"))
    Check(out.cascades.empty(), "adapter rejects all cascade constraints");
  for (auto &c : out.cascades)
    Check(c.at("cell_type").text() != "RAMB18E2",
          "RAMB18E2 dedicated cascades are not yet qualified");
  auto cells = m.Prepare(
      "SELECT id,type FROM cells WHERE type NOT IN ('GND','VCC') ORDER BY id");
  auto actual = atoms.Prepare(
      "SELECT instance,cell_type,resource FROM raw_atoms ORDER BY instance");
  while (cells.Step()) {
    Stop(flag);
    Check(actual.Step() && actual.TypedText(0) == cells.TypedText(0) &&
              actual.TypedText(1) == cells.TypedText(1),
          "requires complete physical cell coverage; packed assignment does "
          "not match mapped JSON");
    ++out.demand[actual.TypedText(2)];
    out.types.insert(actual.TypedText(1));
    ++out.count;
  }
  Check(!actual.Step(), "packed assignment does not match mapped JSON");
  if (typed)
    Check(doc.at("counts").at("physical_cells").integer() == out.count &&
              doc.at("counts").at("singleton_clusters").integer() ==
                  out.count -
                      doc.at("counts").at("macro_assignments").integer(),
          "atomic source physical coverage disagrees");
  if (!typed)
    Check(out.count <= 4096,
          "legacy atomic source exceeds bounded fixture reader");
  Check(out.demand["LUT"] > 0 && out.demand["FF"] > 0,
        "requires at least one LUT and one FF");
  atoms.Execute("CREATE TABLE atoms(seq INTEGER PRIMARY KEY,instance TEXT "
                "UNIQUE,cell_type TEXT,source_cluster TEXT,resource "
                "TEXT);INSERT INTO atoms SELECT row_number() OVER (ORDER BY "
                "instance),instance,cell_type,source_cluster,resource FROM "
                "raw_atoms ORDER BY instance;DROP TABLE raw_atoms;COMMIT");
  if (store)
    store->RequireUnchanged();
  return out;
}
std::string Name(SqliteDatabase &atoms, const std::string &instance) {
  auto r = atoms.Prepare("SELECT seq FROM atoms WHERE instance=?");
  r.BindText(1, instance);
  Check(r.Step(), "macro member absent from atoms");
  return "a" + std::to_string(r.TypedInteger(0) - 1);
}
R MacroGroups(const BoundRequest &input, const SourceInfo &derived,
              const Collected &atoms, SqliteDatabase &atomdb,
              PhysicalDevice &device, const Geometry &g,
              const R &site_descriptor, R &config) {
  A groups, sets, chain_sets;
  std::set<std::string> cascade_owned;
  std::map<std::string, const Macro *> carry;
  for (auto &m : derived.macros)
    if (m.kind == "carry8-lut6_2")
      carry[m.members[0].instance] = &m;
  auto names = [&](const std::vector<PhysicalAssignment> &members) {
    A source, safe;
    for (auto &m : members) {
      source.push_back(m.instance);
      safe.push_back(Name(atomdb, m.instance));
    }
    return std::make_pair(source, safe);
  };
  std::map<std::string, const Tile *> sites;
  I slice_windows = 0;
  for (auto &t : g.tiles) {
    auto p = t.names.find("LUT");
    if (p != t.names.end() && p->second.size() == 1)
      ++slice_windows;
    for (auto &n : t.names)
      for (auto &s : n.second)
        sites[s.second] = &t;
  }
  auto &db = device.database();
  std::map<std::string, std::vector<std::vector<std::string>>> chains;
  if (input.roles.count("native")) {
    auto rows = db.Prepare("SELECT value FROM pd_native_records WHERE "
                           "field='dedicated_adjacency' ORDER BY ordinal");
    while (rows.Step()) {
      auto family = R::Parse(rows.TypedText(0));
      auto kind = family.at("kind").text();
      for (auto &c : family.at("chains").array()) {
        std::vector<std::string> chain;
        for (auto &s : c.array())
          chain.push_back(s.text());
        chains[kind].push_back(std::move(chain));
      }
    }
  }
  bool logical_carry = false;
  for (auto &c : atoms.cascades)
    if (c.at("cell_type").text() == "CARRY8")
      logical_carry = true;
  if (logical_carry && input.roles.count("native")) {
    A filtered;
    std::vector<std::vector<std::string>> runs;
    for (auto &chain : chains["CARRY_NEXT"]) {
      std::vector<std::string> run;
      auto flush = [&] {
        if (run.size() >= 2)
          runs.push_back(run);
        run.clear();
      };
      for (auto &s : chain) {
        auto p = sites.find(s);
        if (p != sites.end() && p->second->names.count("LUT"))
          run.push_back(s);
        else
          flush();
      }
      flush();
    }
    Check(!runs.empty(), "available site domain contains no CARRY_NEXT window");
    for (auto &run : runs) {
      A r;
      for (auto &s : run)
        r.push_back(s);
      filtered.push_back(r);
    }
    chain_sets.emplace_back(O{{"id", "native:CARRY_NEXT"},
                              {"site_resource", "LUT"},
                              {"chains", filtered}});
    for (auto &c : atoms.cascades) {
      if (c.at("cell_type").text() != "CARRY8")
        continue;
      std::vector<PhysicalAssignment> members;
      A unit;
      std::string signature;
      for (auto &instance : c.at("instances").array()) {
        auto name = instance.text();
        Check(cascade_owned.insert(name).second,
              "CARRY8 cascade ownership overlaps");
        Check(carry.count(name),
              "CARRY8 cascade has no complete full-slice macro");
        A current;
        for (auto &m : carry.at(name)->members)
          current.emplace_back(O{
              {"resource", Resource(m.type)}, {"z", Slot(m)}, {"bel", m.bel}});
        if (signature.empty()) {
          unit = current;
          signature = R(current).encode();
        } else
          Check(
              signature == R(current).encode(),
              "CARRY8 cascade units do not share one physical slice template");
        members.insert(members.end(), carry.at(name)->members.begin(),
                       carry.at(name)->members.end());
      }
      I length = c.at("instances").array().size(), windows = 0;
      for (auto &run : runs)
        windows += std::max<I>(0, I(run.size()) - length + 1);
      Check(windows > 0, "CARRY8 cascade has no native legal window");
      auto n = names(members);
      groups.emplace_back(
          O{{"id", c.at("id")},
            {"kind", "site_cascade"},
            {"resource", "SLICE_MACRO"},
            {"owned_resources", A{"CARRY8"}},
            {"instances", n.second},
            {"source_instances", n.first},
            {"window_count", windows},
            {"chain_template", O{{"kind", "directed-site-chain/v1"},
                                 {"chain_set", "native:CARRY_NEXT"},
                                 {"chain_length", length},
                                 {"unit_members", unit}}}});
    }
    config["carry_chain_legalization_flag"] = 0;
  }
  for (auto &macro : derived.macros) {
    if (macro.kind == "ramb18-half-site-occupancy")
      continue;
    if (macro.kind == "carry8-lut6_2" &&
        cascade_owned.count(macro.members[0].instance))
      continue;
    Check(slice_windows > 0, "physical macro has no legal slice window");
    auto n = names(macro.members);
    A unit, owned;
    std::set<std::string> owned_set;
    for (auto &m : macro.members) {
      unit.emplace_back(
          O{{"resource", Resource(m.type)}, {"z", Slot(m)}, {"bel", m.bel}});
      if (Mux(m.type) || m.type == "CARRY8")
        owned_set.insert(m.type);
    }
    for (auto &r : owned_set)
      owned.push_back(r);
    groups.emplace_back(O{{"id", macro.id},
                          {"kind", "site_macro"},
                          {"resource", "SLICE_MACRO"},
                          {"owned_resources", owned},
                          {"instances", n.second},
                          {"source_instances", n.first},
                          {"window_count", slice_windows},
                          {"window_template", O{{"kind", "same-site-slice/v1"},
                                                {"site_resource", "LUT"},
                                                {"members", unit}}}});
  }
  if (input.roles.count("native")) {
    auto bram = BramCandidates(db, g);
    std::map<std::string, std::map<std::string, R>> bram_sites;
    for (auto &p : bram)
      for (auto &c : p.second)
        bram_sites[p.first][c.at("site").text()] = c;
    std::map<std::string, std::vector<R>> candidates;
    for (auto &p : bram)
      candidates[p.first] = p.second;
    for (auto &t : g.tiles)
      for (auto &resource : t.names) {
        if (resource.first != "DSP48E2" && resource.first != "URAM288")
          continue;
        I z = 0;
        for (auto &n : resource.second)
          candidates[resource.first].emplace_back(
              O{{"site", n.second},
                {"resource", resource.first},
                {"x", t.px},
                {"y", t.py},
                {"z", z++},
                {"claims", A{"site:" + n.second}}});
      }
    std::set<std::string> hard_owned;
    std::map<std::string, std::string> family{{"DSP48E2", "DSP_CASCADE"},
                                              {"RAMB36E2", "BRAM_CASCADE"},
                                              {"URAM288", "URAM_CASCADE"}};
    for (auto &c : atoms.cascades) {
      auto resource = c.at("cell_type").text();
      if (resource == "CARRY8")
        continue;
      Check(family.count(resource), "unsupported dedicated cascade");
      std::map<std::string, R> by_name;
      for (auto &candidate : candidates[resource])
        by_name[candidate.at("site").text()] = candidate;
      A windows;
      auto length = c.at("instances").array().size();
      for (auto &chain : chains[family.at(resource)])
        for (std::size_t start = 0; start + length <= chain.size(); ++start) {
          A window;
          for (std::size_t k = 0; k < length; ++k) {
            auto f = by_name.find(chain[start + k]);
            if (f == by_name.end()) {
              window.clear();
              break;
            }
            window.push_back(f->second);
          }
          if (!window.empty())
            windows.push_back(window);
        }
      Check(!windows.empty(),
            "typed hardblock chain has no native legal window");
      A safe;
      for (auto &instance : c.at("instances").array()) {
        Check(hard_owned.insert(instance.text()).second,
              "typed hardblock cascades overlap");
        safe.push_back(Name(atomdb, instance.text()));
      }
      groups.emplace_back(O{{"id", c.at("id")},
                            {"kind", "cascade"},
                            {"resource", resource},
                            {"instances", safe},
                            {"source_instances", c.at("instances")},
                            {"windows", windows}});
    }
    std::set<std::string> used;
    auto rows = atomdb.Prepare(
        "SELECT instance,resource FROM atoms WHERE resource IN "
        "('DSP48E2','RAMB18E2','RAMB36E2','URAM288') ORDER BY seq");
    while (rows.Step()) {
      auto instance = rows.TypedText(0), resource = rows.TypedText(1);
      if (hard_owned.count(instance))
        continue;
      auto &choices = candidates[resource];
      Check(!choices.empty(), "typed hardblock has no legal singleton site");
      if (used.insert(resource).second) {
        A windows;
        for (auto &c : choices) {
          auto normalized = c;
          auto &object = std::get<O>(normalized.value);
          object.erase("role");
          windows.emplace_back(A{normalized});
        }
        sets.emplace_back(O{{"id", "singleton-resource:" + resource},
                            {"resource", resource},
                            {"windows", windows}});
      }
      groups.emplace_back(O{{"id", "singleton:" + instance},
                            {"kind", "singleton"},
                            {"resource", resource},
                            {"instances", A{Name(atomdb, instance)}},
                            {"source_instances", A{instance}},
                            {"window_set", "singleton-resource:" + resource},
                            {"window_count", choices.size()}});
    }
  }
  std::sort(sets.begin(), sets.end(), [](const R &a, const R &b) {
    return a.at("resource").text() < b.at("resource").text();
  });
  R source = O{{"mapped_sha256", FileHash(input.get("mapped_identity"))},
               {"packed_sha256", FileHash(input.get("packed"))},
               {"architecture_sha256", device.architecture_sha()}};
  if (input.roles.count("native")) {
    source["native_constraints_sha256"] = device.native_sha();
    source["provider_manifest_sha256"] = device.provider_sha();
  }
  return O{{"schema", "openparf.physical-macro-groups/v3"},
           {"status", "pass"},
           {"groups", groups},
           {"window_sets", sets},
           {"site_chain_sets", chain_sets},
           {"source", source},
           {"site_database", site_descriptor}};
}
R NameMap(const fs::path &root, const R &header, const BoundRequest &input,
          SqliteDatabase &atoms, const std::vector<R> &cascades) {
  auto temporary = root / "name-map.private.sqlite";
  Sha256 atom_hash, cascade_hash;
  I count = 0, chains = 0;
  R source = O{{"mapped_sha256", FileHash(input.get("mapped_identity"))},
               {"packed_sha256", FileHash(input.get("packed"))},
               {"architecture_sha256", FileHash(input.get("architecture"))}};
  R counts, logical;
  {
    SqliteDatabase db(temporary.string(),
                      SqliteDatabase::Mode::kCreateExclusive);
    db.Execute("PRAGMA application_id=0x454d554e;PRAGMA user_version=1;");
    db.Execute(kOpenparfNameMapDDL);
    db.Execute("BEGIN");
    auto rows =
        atoms.Prepare("SELECT instance,cell_type,source_cluster,resource FROM "
                      "atoms ORDER BY seq");
    while (rows.Step()) {
      auto name = "a" + std::to_string(count);
      auto insert = db.Prepare("INSERT INTO atoms VALUES(?,?,?,?,?,?)");
      insert.BindInteger(1, count++);
      insert.BindText(2, name);
      for (int k = 0; k < 4; ++k)
        insert.BindText(k + 3, rows.TypedText(k));
      insert.Execute();
      Feed(atom_hash, R(O{{"openparf", name},
                          {"instance", rows.TypedText(0)},
                          {"cell_type", rows.TypedText(1)},
                          {"source_cluster", rows.TypedText(2)},
                          {"resource", rows.TypedText(3)}})
                              .encode() +
                          "\n");
    }
    for (auto &c : cascades) {
      if (c.at("cell_type").text() != "CARRY8")
        continue;
      auto insert = db.Prepare("INSERT INTO carry_cascades VALUES(?,?,?)");
      insert.BindInteger(1, chains);
      insert.BindText(2, c.at("id").text());
      insert.BindText(3, c.encode());
      insert.Execute();
      I seq = 0;
      for (auto &name : c.at("instances").array()) {
        auto member = db.Prepare("INSERT INTO cascade_members VALUES(?,?,?)");
        member.BindInteger(1, chains);
        member.BindInteger(2, seq++);
        member.BindText(3, name.text());
        member.Execute();
      }
      ++chains;
      Feed(cascade_hash, c.encode() + "\n");
    }
    counts = O{{"atoms", count}, {"carry_cascade_chains", chains}};
    logical = Hash(R(O{{"header", header},
                       {"counts", counts},
                       {"source", source},
                       {"atom_sha256", Hex(atom_hash)},
                       {"cascade_sha256", Hex(cascade_hash)}})
                       .encode());
    Metadata(db, "header", header);
    Metadata(db, "counts", counts);
    Metadata(db, "source", source);
    Metadata(db, "logical_sha256", logical);
    db.Execute("COMMIT");
  }
  auto ref =
      ArtifactRef::Bind(temporary.string(), "openparf-name-map-store-v1");
  auto sha = FileHash(ref);
  auto artifact = "name-map." + sha + ".sqlite";
  auto bytes = ref.generation().bytes;
  fs::rename(temporary, root / artifact);
  R selector = O{{"schema", "emuflow.openparf-atomic-name-map-storage/v1"},
                 {"logical_schema", "emuflow.openparf-atomic-name-map/v2"},
                 {"application_id", I(0x454d554e)},
                 {"user_version", 1},
                 {"header", header},
                 {"counts", counts},
                 {"source", source},
                 {"logical_sha256", logical},
                 {"artifact", artifact},
                 {"store_bytes", R(bytes)},
                 {"store_sha256", sha}};
  Json(root / "name_map.json", selector);
  return selector;
}
std::string Library(const fs::path &report, bool mux, I atoms, I &nets,
                    I &dropped) {
  std::ifstream in(report);
  std::string magic;
  I count;
  Check(bool(in >> magic >> count >> nets >> dropped) &&
            magic == "EMUFLOW_BOOKSHELF_V1" && count == atoms,
        "invalid native Bookshelf report");
  std::map<std::string, std::map<std::string, int>> ports;
  std::string type, port;
  int direction;
  while (in >> type >> port >> direction) {
    Check(direction == 0 || direction == 1, "invalid primitive pin direction");
    ports[type][port] = direction;
  }
  Check(in.eof(), "truncated Bookshelf report");
  if (mux)
    for (auto t : {"MUXF7", "MUXF8", "MUXF9"})
      for (auto p : {"I0", "I1", "S", "O"})
        ports[t][p] = std::string(p) == "O" ? 1 : 0;
  std::string out;
  for (auto &t : ports) {
    if (!out.empty())
      out += '\n';
    out += "CELL " + t.first + "\n";
    for (auto &p : t.second) {
      std::string suffix;
      if (Ff(t.first)) {
        if (p.first == "C")
          suffix = " CLOCK";
        else if (p.first == "CE")
          suffix = " CTRL_CE";
        else if (p.first == (t.first == "FDCE"   ? "CLR"
                             : t.first == "FDPE" ? "PRE"
                             : t.first == "FDSE" ? "S"
                                                 : "R"))
          suffix = " CTRL_SR";
      }
      if (t.first == "CARRY8" && (p.first == "CI" || p.first == "CO[7]"))
        suffix = " CAS";
      out += "  PIN " + p.first + (p.second ? " OUTPUT" : " INPUT") + suffix +
             "\n";
    }
    out += "END CELL\n";
  }
  return out;
}
void Build(const BoundRequest &input, const std::string &output,
           const volatile std::sig_atomic_t *flag) {
  const auto &mapped = input.get("mapped");
  auto top = input.config.at("top").text();
  auto info = DeriveSource(mapped, top, flag);
  fs::path target = fs::absolute(output).lexically_normal();
  Scratch work(target.parent_path());
  Scratch generation(target.parent_path());
  auto root = generation.root;
  auto atoms_path = work.root / "atoms.sqlite";
  auto collected = Collect(input, info, atoms_path, flag);
  SqliteDatabase atoms(atoms_path.string(), SqliteDatabase::Mode::kReadOnly);
  SqliteDatabase device_db((work.root / "device.sqlite").string(),
                           SqliteDatabase::Mode::kCreateExclusive);
  bool typed = input.roles.count("native");
  Check(typed == bool(input.roles.count("provider")),
        "requires both native constraints and provider manifest");
  PhysicalDevice device(device_db, input.get("architecture"),
                        typed ? &input.get("native") : nullptr,
                        typed ? &input.get("provider") : nullptr, flag);
  bool mux = std::any_of(collected.types.begin(), collected.types.end(),
                         [](auto &t) { return Mux(t); }),
       carry = collected.types.count("CARRY8");
  auto geometry = BuildGeometry(device, collected.demand, mux, carry,
                                input.config.at("allowed_slrs"), flag);
  auto site_descriptor = WriteSiteStore(root / "site-map.sqlite3", geometry,
                                        device.architecture_sha());
  auto policy = Read(input.get("policy"));
  Check(policy.at("schema").text() == "emuflow.openparf-input-policy/v1",
        "input policy schema disagrees");
  auto config = policy.at("config");
  config["aux_input"] = (target / "design.aux").string();
  config["result_dir"] = (target / "results").string();
  config["slr_aware_flag"] =
      !geometry.slr.null() && geometry.slr.at("regions").array().size() > 1 ? 1
                                                                            : 0;
  R models = O{}, resource_map = O{}, categories = O{}, fillers = O{},
    expected_area = O{};
  A ordered_resources;
  for (auto &resource : OrderedResources(collected.demand)) {
    ordered_resources.push_back(resource);
    resource_map[resource] = A{resource};
    categories[resource] = resource == "LUT"      ? "LUTL"
                           : resource == "FF"     ? "FF"
                           : resource == "CARRY8" ? "Carry"
                                                  : "SSSIR";
    fillers[resource] = resource == "LUT" || resource == "FF" ? 65536 : 1;
    if (resource == "LUT" || resource == "FF")
      expected_area[resource] =
          geometry.density.at(resource).at("placeable_area");
  }
  std::vector<std::string> primitives(collected.types.begin(),
                                      collected.types.end());
  auto rank = [](auto t) { return Lut(t) ? 0 : Ff(t) ? 1 : 2; };
  std::sort(primitives.begin(), primitives.end(), [&](auto a, auto b) {
    return std::make_pair(rank(a), a) < std::make_pair(rank(b), b);
  });
  std::string ordered_models = "{";
  for (auto &type : primitives) {
    auto resource = Resource(type);
    double dimension = 1.0 / std::sqrt(double(geometry.units.at(resource)));
    R model = O{{resource, A{dimension, dimension}}};
    if (Ff(type))
      model["isFF"] = 1;
    else if (Lut(type) || type == "LUT6_2")
      model["isLUT"] = type == "LUT6_2" ? 6 : type.back() - '0';
    if (ordered_models.size() > 1)
      ordered_models += ',';
    ordered_models += JsonQuote(type) + ":" + model.encode();
  }
  ordered_models += '}';
  config["gp_resource2area_types_map"] = resource_map;
  config["resource_categories"] = categories;
  config["gp_max_fillers_per_area_type"] = fillers;
  config["emuflow_expected_logic_site_area"] = expected_area;
  config["plot_target_at_names"] = ordered_resources;
  config["gp_model2area_types_map"] = nullptr;
  if (carry) {
    config["architecture_type"] = "ultrascale";
    config["carry_chain_module_name"] = "CARRY8";
    config["carry_chain_at_name"] = "CARRY8";
    config["carry_chain_legalization_flag"] = 1;
    config["align_carry_chain_flag"] = 1;
  }
  auto groups = MacroGroups(input, info, collected, atoms, device, geometry,
                            site_descriptor, config);
  bool has_groups = !groups.at("groups").array().empty();
  if (has_groups) {
    Json(root / "physical-macro-groups.json", groups);
    config["typed_hardblock_chain_constraints"] =
        (target / "physical-macro-groups.json").string();
  }
  ExportBookshelf(
      mapped,
      ArtifactRef::Bind(atoms_path.string(), "openparf-atom-index-private-v1"),
      (root / "design.nets").string(), (work.root / "library.tsv").string(),
      (work.root / "pins.sqlite").string());
  I nets = 0, dropped = 0;
  Write(root / "design.lib", Library(work.root / "library.tsv", mux,
                                     collected.count, nets, dropped));
  std::string resources;
  for (auto &resource : OrderedResources(collected.demand)) {
    std::set<std::string> types;
    for (auto &type : collected.types)
      if (Resource(type) == resource)
        types.insert(type);
    if (mux && Mux(resource))
      types.insert(resource);
    resources += "  " + resource;
    for (auto &type : types)
      resources += " " + type;
    resources += '\n';
  }
  if (mux)
    for (auto type : {"MUXF7", "MUXF8", "MUXF9"})
      if (!collected.demand.count(type))
        resources += "  " + std::string(type) + " " + type + "\n";
  // Resource lines use the stable platform order, including unused mux levels.
  std::map<std::string, std::set<std::string>> declarations;
  for (auto &type : collected.types)
    declarations[Resource(type)].insert(type);
  if (mux)
    for (auto type : {"MUXF7", "MUXF8", "MUXF9"})
      declarations[type].insert(type);
  Capacities declarations_order;
  for (auto &p : declarations)
    declarations_order[p.first] = 1;
  resources.clear();
  for (auto &r : OrderedResources(declarations_order)) {
    resources += "  " + r;
    for (auto &type : declarations[r])
      resources += " " + type;
    resources += '\n';
  }
  Write(root / "design.scl", geometry.prefix + resources + geometry.suffix);
  {
    AtomicBinaryWriter writer((root / "design.nodes").string());
    auto rows = atoms.Prepare("SELECT seq,cell_type FROM atoms ORDER BY seq");
    while (rows.Step()) {
      Stop(flag);
      writer.stream() << "a" << rows.TypedInteger(0) - 1 << ' '
                      << rows.TypedText(1) << '\n';
    }
    writer.Commit();
  }
  Write(root / "design.pl", "");
  Write(root / "design.aux",
        "design : design.nodes design.nets design.pl design.scl design.lib\n");
  std::string config_text = "{";
  for (auto &f : std::get<O>(config.value)) {
    if (config_text.size() > 1)
      config_text += ',';
    config_text += JsonQuote(f.first) + ":" +
                   (f.first == "gp_model2area_types_map" ? ordered_models
                                                         : f.second.encode());
  }
  Write(root / "openparf.json", config_text + "}\n");
  A xs, ys;
  for (auto x : geometry.xs)
    xs.push_back(x);
  for (auto y : geometry.ys)
    ys.push_back(y);
  R header =
      O{{"schema", "emuflow.openparf-atomic-name-map/v2"},
        {"top", collected.top},
        {"coordinate_system",
         O{{"x_axis", xs}, {"y_axis", ys}, {"y_axis_order", geometry.order}}},
        {"site_database", site_descriptor}};
  if (has_groups)
    header["physical_macro_constraints"] =
        O{{"schema", "openparf.physical-macro-groups/v3"},
          {"file", "physical-macro-groups.json"},
          {"groups", groups.at("groups").array().size()}};
  auto name_map = NameMap(root, header, input, atoms, collected.cascades);
  auto name_selector = ArtifactRef::Bind((root / "name_map.json").string(),
                                         "openparf-name-map-selector-v1");
  R demand = O{}, units = O{};
  for (auto &p : collected.demand)
    demand[p.first] = p.second;
  for (auto &p : geometry.units)
    units[p.first] = p.second;
  bool carry_chain = false, mux_macro = false, carry_macro = false;
  for (auto &c : collected.cascades)
    carry_chain |= c.at("cell_type").text() == "CARRY8";
  for (auto &macro : info.macros) {
    carry_macro |= macro.kind == "carry8-lut6_2";
    mux_macro |= macro.kind != "carry8-lut6_2" &&
                 macro.kind != "ramb18-half-site-occupancy";
  }
  A files;
  std::set<std::string> file_names{
      "design.nodes",     "design.nets",
      "design.lib",       "design.scl",
      "design.pl",        "design.aux",
      "openparf.json",    "name_map.json",
      "site-map.sqlite3", name_map.at("artifact").text()};
  if (has_groups)
    file_names.insert("physical-macro-groups.json");
  for (auto &f : file_names)
    files.push_back(f);
  R manifest =
      O{{"schema", "emuflow.openparf-atomic-manifest/v1"},
        {"status", "pass"},
        {"mode", "native-atomic-mixed-resource-qualification"},
        {"part", device.part()},
        {"atoms", collected.count},
        {"nets", nets},
        {"site_database", site_descriptor},
        {"net_export",
         O{{"emitted", nets}, {"dropped_single_endpoint", dropped}}},
        {"resources", demand},
        {"resource_unit_capacity", units},
        {"placement_region", geometry.placement},
        {"clock_region_contract", geometry.clock},
        {"slr_contract", geometry.slr},
        {"site_headroom_contract", geometry.headroom},
        {"density_contract", geometry.density},
        {"routability_contract", policy.at("routability_contract")},
        {"runtime_validation", "unverified"},
        {"name_map_storage",
         O{{"schema", "emuflow.openparf-atomic-name-map-storage/v1"},
           {"logical_sha256", name_map.at("logical_sha256")},
           {"source", name_map.at("source")},
           {"atoms", name_map.at("counts").at("atoms")},
           {"carry_cascade_chains",
            name_map.at("counts").at("carry_cascade_chains")},
           {"selector", O{{"file", "name_map.json"},
                          {"bytes", R(name_selector.generation().bytes)},
                          {"sha256", FileHash(name_selector)}}},
           {"store", O{{"file", name_map.at("artifact")},
                       {"bytes", name_map.at("store_bytes")},
                       {"sha256", name_map.at("store_sha256")}}}}},
        {"constraint_policy",
         O{{"ordinary_slice_clusters_are_repackable", true},
           {"singleton_dsp_bram_uram_use_native_sssir_mcf", !typed},
           {"typed_hardblock_chains_use_internal_legalizer", typed},
           {"mux_site_macros_use_internal_legalizer", mux_macro},
           {"carry8_site_macros_use_internal_legalizer", carry_macro},
           {"carry8_chains_use_native_legalizer",
            carry && !(carry_chain && typed)},
           {"carry8_chains_use_directed_site_legalizer", carry_chain && typed},
           {"dedicated_or_relative_constraints",
            has_groups ? "native-physical-macro-legalizer" : "fail-closed"},
           {"ramb18_half_site",
            typed ? "source-sealed-independent-half-claims" : "fail-closed"},
           {"lut_policy", "8 independent 6LUT BELs; no paired 5LUT use"}}},
        {"files", files}};
  Json(root / "manifest.json", manifest);
  device.RequireUnchanged();
  for (auto &ref : input.refs)
    ref.RequireUnchanged();
  Stop(flag);
  // Stage every member before entering the existing reader lease. Reported
  // publication failures restore the previous complete input generation.
  if (fs::exists(target) && !fs::is_empty(target)) {
    Check(!fs::is_symlink(target), "output directory is a symbolic link");
    // The existing reader lease protects selector-to-site-map consistency.
    auto lock_path = target / ".name_map.json.lock";
    Check(!fs::is_symlink(lock_path), "name-map lock is a symbolic link");
    const int descriptor = open(lock_path.c_str(), O_CREAT | O_RDWR, 0600);
    Check(descriptor >= 0, "cannot acquire input publication lease");
    struct Lease {
      int fd;
      ~Lease() {
        flock(fd, LOCK_UN);
        close(fd);
      }
    } lease{descriptor};
    Check(flock(descriptor, LOCK_EX) == 0,
          "cannot acquire exclusive input lease");
    std::vector<std::string> ordered;
    for (auto &name : file_names)
      if (name != "name_map.json")
        ordered.push_back(name);
    ordered.push_back("name_map.json");
    ordered.push_back("manifest.json");
    auto backup = work.root / "previous";
    fs::create_directory(backup);
    std::set<std::string> existed;
    for (auto &name : ordered) {
      auto destination = target / name;
      Check(!fs::is_symlink(destination),
            "input destination is a symbolic link");
      for (auto &ref : input.refs) {
        std::error_code error;
        Check(!fs::equivalent(destination, ref.path(), error) &&
                  fs::weakly_canonical(destination) !=
                      fs::weakly_canonical(ref.path()),
              "input destination aliases a source");
      }
      if (fs::exists(destination)) {
        Check(fs::is_regular_file(destination),
              "input destination is not regular");
        fs::copy_file(destination, backup / name);
        existed.insert(name);
      }
    }
    std::vector<std::string> committed;
    try {
      for (auto &name : ordered) {
        Stop(flag);
        AtomicBinaryWriter writer((target / name).string(), input.refs);
        std::ifstream in(root / name, std::ios::binary);
        Check(bool(in), "prepared input is missing");
        Copy(in, writer.stream());
        writer.Commit();
        committed.push_back(name);
      }
      for (auto &ref : input.refs)
        ref.RequireUnchanged();
    } catch (...) {
      for (auto it = committed.rbegin(); it != committed.rend(); ++it) {
        if (existed.count(*it)) {
          AtomicBinaryWriter writer((target / *it).string());
          std::ifstream in(backup / *it, std::ios::binary);
          Copy(in, writer.stream());
          writer.Commit();
        } else {
          std::error_code error;
          fs::remove(target / *it, error);
        }
      }
      throw;
    }
    return;
  }
  if (fs::exists(target)) {
    Check(!fs::is_symlink(target), "output directory is a symbolic link");
    fs::remove(target);
  }
  PublishNewDirectory(root.string(), target.string(), input.refs);
}
} // namespace
} // namespace emuflow::core::openparf_input
namespace emuflow::core {
StageResult PrepareOpenparfInput(const ArtifactRef &request,
                                 const std::string &output,
                                 const volatile std::sig_atomic_t *flag) {
  using namespace openparf_input;
  std::optional<BoundRequest> input;
  return RunStage(
      "physical.prepare-openparf-input",
      [&] {
        input = BindRequest(request);
        return input->refs;
      },
      [&](const auto &) { Build(*input, output, flag); },
      [&] {
        return std::vector<ArtifactRef>{ArtifactRef::Bind(
            (std::filesystem::path(output) / "manifest.json").string(),
            "openparf-input-manifest-v1")};
      });
}
} // namespace emuflow::core
