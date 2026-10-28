#include "physical/openparf/placement_records.hpp"
#include <cmath>
namespace emuflow::core::openparf_placement {
namespace {
I Discrete(const R &r) {
  if (auto p = std::get_if<I>(&r.value))
    return *p;
  if (auto p = std::get_if<double>(&r.value)) {
    Check(std::isfinite(*p) && std::floor(*p) == *p &&
              std::abs(*p) < double(1ULL << 53),
          "window slot is not discrete");
    return I(*p);
  }
  Check(false, "window slot is not numeric");
  return 0;
}
R Selected(SqliteDatabase &db, const std::string &name) {
  auto q = db.Prepare("SELECT instance,resource,z,x,y,site,bel FROM normalized "
                      "WHERE instance=?");
  q.BindText(1, name);
  Check(q.Step(), "typed hardblock group references an unknown instance");
  return O{{"instance", q.TypedText(0)},
           {"resource", q.TypedText(1)},
           {"z", q.TypedInteger(2)},
           {"x", q.Real(3)},
           {"y", q.Real(4)},
           {"site", q.TypedText(5)},
           {"bel", q.TypedText(6)}};
}
void Shape(const R &member, const R &placed) {
  Check(member.at("resource").text() == placed.at("resource").text() &&
            member.at("bel").text() == placed.at("bel").text() &&
            Discrete(member.at("z")) == placed.at("z").integer(),
        "site macro violates its certified slice template");
}
} // namespace
void CheckWindows(SqliteDatabase &db, BoundRequest &input, const R &name_header,
                  const volatile std::sig_atomic_t *flag) {
  if (!name_header.has("physical_macro_constraints"))
    return;
  const auto descriptor = name_header.at("physical_macro_constraints");
  const auto file = descriptor.at("file").text();
  Check(descriptor.at("schema").text() == "openparf.physical-macro-groups/v3" &&
            descriptor.at("groups").integer() > 0 &&
            std::filesystem::path(file).filename() == file && file != "." &&
            file != "..",
        "invalid physical macro constraint descriptor");
  auto ref = ArtifactRef::Bind(
      (std::filesystem::path(input.get("name_selector").path()).parent_path() /
       file)
          .string(),
      "physical-input-document-v1");
  Check(std::filesystem::path(ref.path()).parent_path() ==
            std::filesystem::path(input.get("name_selector").path())
                .parent_path(),
        "macro constraint file escapes input owner");
  input.refs.push_back(ref);
  // External OpenPARF constraints retain their vendor JSON protocol. Parse one
  // group/window-set record at a time through SQLite, rather than a host tree.
  Check(ref.generation().bytes <= 512ULL * 1024 * 1024,
        "macro constraint file exceeds supported engine limit");
  std::ifstream stream(ref.path(), std::ios::binary);
  std::string raw((std::istreambuf_iterator<char>(stream)), {});
  Check(!stream.bad(), "cannot read macro constraints");
  ref.RequireUnchanged();
  auto valid =
      db.Prepare("SELECT "
                 "json_valid(?1),json_extract(?1,'$.schema'),json_extract(?1,'$"
                 ".status'),json_type(?1,'$.groups'),json_array_length(?1,'$."
                 "groups'),json_extract(?1,'$.site_database')");
  valid.BindText(1, raw);
  Check(valid.Step() && valid.Integer(0) &&
            valid.Text(1) == "openparf.physical-macro-groups/v3" &&
            valid.Text(2) == "pass" && valid.Text(3) == "array" &&
            valid.Integer(4) == descriptor.at("groups").integer() &&
            R::Parse(valid.TypedText(5)).encode() ==
                name_header.at("site_database").encode(),
        "invalid physical macro constraint contract");
  auto duplicate =
      db.Prepare("SELECT 1 FROM json_tree(?) WHERE parent IS NOT NULL GROUP BY "
                 "parent,key HAVING count(*)>1 LIMIT 1");
  duplicate.BindText(1, raw);
  Check(!duplicate.Step(), "macro constraints repeat a JSON key");
  std::map<std::string, R> windows, chains;
  std::set<std::string> used_windows, used_chains;
  for (const auto &key : {"window_sets", "site_chain_sets"}) {
    auto type = db.Prepare("SELECT json_type(?1,?2)");
    type.BindText(1, raw);
    type.BindText(2, "$." + std::string(key));
    Check(type.Step() && (type.IsNull(0) || type.Text(0) == "array"),
          "invalid macro shared-set collection");
    auto rows = db.Prepare("SELECT value,type FROM json_each(?1,?2)");
    rows.BindText(1, raw);
    rows.BindText(2, "$." + std::string(key));
    while (rows.Step()) {
      Stop(flag);
      Check(rows.Text(1) == "object", "invalid macro shared-set record");
      auto item = R::Parse(rows.TypedText(0));
      const auto id = item.at("id").text();
      Check(!id.empty(), "empty shared-set identity");
      if (std::string(key) == "window_sets") {
        Check(Hard(item.at("resource").text()) &&
                  !item.at("windows").array().empty(),
              "invalid hardblock window set");
        Check(windows.emplace(id, std::move(item)).second,
              "duplicate hardblock window set");
      } else {
        Check(item.at("site_resource").text() == "LUT" &&
                  !item.at("chains").array().empty(),
              "invalid site-chain set");
        for (const auto &chain : item.at("chains").array()) {
          std::set<std::string> unique;
          Check(chain.array().size() >= 2, "site chain is too short");
          for (const auto &s : chain.array())
            Check(!s.text().empty() && unique.insert(s.text()).second,
                  "site chain repeats a site");
        }
        Check(chains.emplace(id, std::move(item)).second,
              "duplicate site-chain set");
      }
    }
  }
  db.Execute("CREATE TABLE constrained_member(instance TEXT PRIMARY KEY) "
             "WITHOUT ROWID;CREATE TABLE constraint_claim(claim TEXT PRIMARY "
             "KEY) WITHOUT ROWID;");
  auto groups = db.Prepare("SELECT value,type FROM json_each(?,'$.groups') "
                           "ORDER BY CAST(key AS INTEGER)");
  groups.BindText(1, raw);
  while (groups.Step()) {
    Stop(flag);
    Check(groups.Text(1) == "object", "invalid physical group record");
    const auto group = R::Parse(groups.TypedText(0));
    const auto kind = group.at("kind").text(),
               resource = group.at("resource").text();
    Check(Hard(resource) || resource == "SLICE_MACRO",
          "invalid physical group resource");
    std::vector<R> selected;
    for (const auto &member : group.at("source_instances").array()) {
      const auto name = member.text();
      auto add = db.Prepare("INSERT INTO constrained_member VALUES(?)");
      add.BindText(1, name);
      add.Execute();
      selected.push_back(Selected(db, name));
    }
    Check(!selected.empty(), "empty physical group");
    std::vector<std::string> claims;
    std::vector<std::string> unit_sites;
    const R *template_ptr =
        group.has("window_template") && !group.at("window_template").null()
            ? &group.at("window_template")
            : nullptr;
    const R *chain_template =
        group.has("chain_template") && !group.at("chain_template").null()
            ? &group.at("chain_template")
            : nullptr;
    if (chain_template) {
      Check(kind == "site_cascade" && resource == "SLICE_MACRO" &&
                chain_template->at("kind").text() == "directed-site-chain/v1" &&
                !group.has("windows") && !template_ptr &&
                !group.has("window_set"),
            "invalid compact site-chain template");
      const auto id = chain_template->at("chain_set").text();
      Check(chains.count(id), "unknown site-chain set");
      used_chains.insert(id);
      const auto &units = chain_template->at("unit_members").array();
      const auto length = chain_template->at("chain_length").integer();
      Check(length >= 2 && !units.empty() &&
                I(selected.size()) == length * I(units.size()),
            "invalid site-chain member coverage");
      for (I index = 0; index < length; ++index) {
        const auto site = selected[index * units.size()].at("site").text();
        unit_sites.push_back(site);
        for (std::size_t j = 0; j < units.size(); ++j) {
          const auto &placed = selected[index * units.size() + j];
          Check(placed.at("site").text() == site,
                "site-chain unit is split across sites");
          Shape(units[j], placed);
          claims.push_back("site:" + site);
        }
      }
      I matches = 0, count = 0;
      for (const auto &chain : chains.at(id).at("chains").array()) {
        for (I start = 0; start + length <= I(chain.array().size()); ++start) {
          ++count;
          bool equal = true;
          for (I j = 0; j < length; ++j)
            equal &= chain.array()[start + j].text() == unit_sites[j];
          matches += equal;
        }
      }
      Check(count > 0 && count == group.at("window_count").integer() &&
                matches == 1,
            "site cascade does not match one certified directed window");
    } else if (kind == "site_macro" && template_ptr) {
      Check(resource == "SLICE_MACRO" &&
                template_ptr->at("kind").text() == "same-site-slice/v1" &&
                template_ptr->at("site_resource").text() == "LUT" &&
                group.at("window_count").integer() > 0,
            "invalid compact site macro template");
      const auto &members = template_ptr->at("members").array();
      Check(members.size() == selected.size(),
            "site macro member coverage differs");
      const auto site = selected.front().at("site").text();
      for (std::size_t k = 0; k < selected.size(); ++k) {
        Check(selected[k].at("site").text() == site,
              "site macro is split across sites");
        Shape(members[k], selected[k]);
        claims.push_back("site:" + site);
      }
    } else {
      const R::A *choices = nullptr;
      if (group.has("window_set") && !group.at("window_set").null()) {
        const auto id = group.at("window_set").text();
        Check(windows.count(id) && kind == "singleton" &&
                  !group.has("windows") && !template_ptr &&
                  windows.at(id).at("resource").text() == resource,
              "invalid shared window-set reference");
        used_windows.insert(id);
        choices = &windows.at(id).at("windows").array();
        Check(group.at("window_count").integer() == I(choices->size()),
              "shared window count differs");
      } else {
        Check(group.has("windows"), "physical group lacks certified windows");
        choices = &group.at("windows").array();
      }
      const R *matched = nullptr;
      I matches = 0;
      for (const auto &choice : *choices) {
        if (!std::holds_alternative<A>(choice.value) ||
            choice.array().size() != selected.size())
          continue;
        bool equal = true;
        for (std::size_t k = 0; k < selected.size(); ++k) {
          const auto &entry = choice.array()[k];
          equal &= entry.at("site").text() == selected[k].at("site").text() &&
                   Discrete(entry.at("z")) == selected[k].at("z").integer();
        }
        if (equal) {
          ++matches;
          matched = &choice;
        }
      }
      Check(matches == 1,
            "physical placement does not match exactly one certified window");
      for (const auto &entry : matched->array()) {
        Check(!entry.at("claims").array().empty(),
              "window has no occupancy claims");
        for (const auto &claim : entry.at("claims").array()) {
          Check(!claim.text().empty(), "empty occupancy claim");
          claims.push_back(claim.text());
        }
      }
    }
    const std::set<std::string> unique(claims.begin(), claims.end());
    bool internal = claims.size() == unique.size();
    if (kind == "site_macro")
      internal =
          unique == std::set<std::string>{"site:" +
                                          selected.front().at("site").text()} &&
          std::all_of(selected.begin(), selected.end(), [&](const auto &s) {
            return s.at("site").text() == selected.front().at("site").text();
          });
    if (kind == "site_cascade") {
      std::set<std::string> expected;
      for (const auto &s : unit_sites)
        expected.insert("site:" + s);
      internal = expected == unique && expected.size() == unit_sites.size();
    }
    Check(internal,
          "physical window has conflicting internal occupancy claims");
    for (const auto &claim : unique) {
      auto put = db.Prepare("INSERT INTO constraint_claim VALUES(?)");
      put.BindText(1, claim);
      put.Execute();
    }
  }
  Check(used_windows.size() == windows.size() &&
            used_chains.size() == chains.size(),
        "physical constraints have unused shared window/chain sets");
  None(db,
       "SELECT 1 FROM (SELECT instance FROM constrained_member EXCEPT SELECT "
       "instance FROM names.atoms WHERE resource IN "
       "('DSP48E2','RAMB18E2','RAMB36E2','URAM288') OR instance IN (SELECT "
       "instance FROM atomic.assignments)) LIMIT 1",
       "physical constraints have unexpected members");
  None(db,
       "SELECT 1 FROM (SELECT instance FROM names.atoms WHERE resource IN "
       "('DSP48E2','RAMB18E2','RAMB36E2','URAM288') OR instance IN (SELECT "
       "instance FROM atomic.assignments) EXCEPT SELECT instance FROM "
       "constrained_member) LIMIT 1",
       "physical constraint coverage incomplete");
}
} // namespace emuflow::core::openparf_placement
