#include "physical/handoff.hpp"
#include "physical/openparf/input_records.hpp"
#include "physical/openparf/inputs.hpp"
#include "storage/schema/openparf_input_schema.hpp"
#include <functional>
namespace emuflow::core::openparf_input {
namespace fs = std::filesystem;
ArtifactRef ImportSmallMapped(const ArtifactRef &source,
                              const std::string &selected,
                              const fs::path &path) {
  auto doc = Read(source);
  auto modules = std::get<R::O>(doc.at("modules").value);
  std::string top = selected;
  if (top.empty()) {
    Check(modules.size() == 1, "ambiguous mapped top");
    top = modules.begin()->first;
  }
  Check(modules.count(top), "mapped top absent");
  auto cells = std::get<R::O>(modules.at(top).at("cells").value);
  Check(cells.size() <= 4096, "raw mapped input exceeds bounded reader limit");
  {
    SqliteDatabase db(path.string(), SqliteDatabase::Mode::kCreateExclusive);
    db.Execute(
        "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE "
        "cells(id TEXT PRIMARY KEY,seq INTEGER UNIQUE,type TEXT,record_json "
        "TEXT);CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port "
        "TEXT,port_bit INTEGER,port_width INTEGER,direction INTEGER,value_kind "
        "INTEGER,net_bit INTEGER,constant TEXT,PRIMARY "
        "KEY(cell_id,port,port_bit)) WITHOUT ROWID;CREATE TABLE netnames(id "
        "TEXT PRIMARY KEY,seq INTEGER,record_json TEXT);BEGIN");
    auto m = db.Prepare("INSERT INTO metadata VALUES(?,?)");
    for (auto v :
         O{{"schema", "emuflow.xilinx-mapped-index/v2"}, {"top_json", top}}) {
      m.BindText(1, v.first);
      m.BindText(2, v.first == "schema" ? v.second.text() : v.second.encode());
      m.Execute();
      m.Reset();
    }
    I seq = 0;
    for (auto &c : cells) {
      auto record = c.second;
      auto type = record.at("type").text();
      auto row = db.Prepare("INSERT INTO cells VALUES(?,?,?,?)");
      row.BindText(1, c.first);
      row.BindInteger(2, seq);
      row.BindText(3, type);
      row.BindText(4, record.encode());
      row.Execute();
      if (record.has("connections"))
        for (auto &pin : std::get<O>(record.at("connections").value)) {
          I index = 0;
          auto direction = record.at("port_directions").at(pin.first).text();
          Check(direction == "input" || direction == "output" ||
                    direction == "inout",
                "invalid pin direction");
          for (auto &bit : pin.second.array()) {
            auto r =
                db.Prepare("INSERT INTO cell_pins VALUES(?,?,?,?,?,?,?,?,?)");
            r.BindInteger(1, seq);
            r.BindText(2, c.first);
            r.BindText(3, pin.first);
            r.BindInteger(4, index++);
            r.BindInteger(5, pin.second.array().size());
            r.BindInteger(6, direction == "output"  ? 1
                             : direction == "input" ? 0
                                                    : 2);
            bool net = std::holds_alternative<I>(bit.value);
            r.BindInteger(7, net ? 0 : 1);
            if (net) {
              r.BindInteger(8, bit.integer());
              r.BindNull(9);
            } else {
              r.BindNull(8);
              r.BindText(9, bit.text());
            }
            r.Execute();
          }
        }
      ++seq;
    }
    db.Execute("CREATE INDEX pins_net ON cell_pins(net_bit,direction);COMMIT");
  }
  source.RequireUnchanged();
  return ArtifactRef::Bind(path.string(), "xilinx-mapped-index-v2");
}
SourceInfo DeriveSource(const ArtifactRef &mapped, const std::string &top,
                        const volatile std::sig_atomic_t *flag) {
  SqliteStore owner(mapped);
  auto &db = owner.database();
  auto selected = R::Parse(Meta(db, "top_json")).text();
  Check(top.empty() || selected == top, "mapped top disagrees");
  MappedPhysicalSemantics semantics(mapped);
  SourceInfo result;
  db.Execute("CREATE TEMP TABLE op_macro_owned(instance TEXT PRIMARY KEY,owner "
             "TEXT) WITHOUT ROWID;CREATE TEMP TABLE op_mux_child(instance TEXT "
             "PRIMARY KEY) WITHOUT ROWID");
  auto add = [&](Macro macro) {
    std::set<std::string> local;
    for (auto &a : macro.members) {
      Check(local.insert(a.instance).second, "physical macro reuses a member");
      Check(semantics.CellType(a.instance) == a.type,
            "physical macro type mismatch");
      auto r = db.Prepare("INSERT INTO op_macro_owned VALUES(?,?)");
      r.BindText(1, a.instance);
      r.BindText(2, macro.id);
      r.Execute();
    }
    if (macro.kind != "ramb18-half-site-occupancy")
      semantics.ValidateSlice(macro.members);
    result.macros.push_back(std::move(macro));
  };
  auto driver = [&](const std::string &instance, const std::string &port) {
    auto bits = semantics.Bits(instance, port);
    Check(bits.size() == 1 && std::holds_alternative<I>(bits[0]),
          "mux " + instance + " port " + port + " has no local driver");
    auto d = semantics.Driver(std::get<I>(bits[0]));
    Check(bool(d), "mux has no local driver");
    return *d;
  };
  I index = 0;
  auto carries =
      db.Prepare("SELECT id FROM cells WHERE type='CARRY8' ORDER BY id");
  while (carries.Step()) {
    Stop(flag);
    auto name = carries.TypedText(0);
    Macro macro{Pad("carry8-lut6_2-", index++),
                "carry8-lut6_2",
                {{name, "CARRY8", "CARRY8"}}};
    auto di = semantics.Bits(name, "DI"), s = semantics.Bits(name, "S");
    Check(di.size() == 8 && s.size() == 8,
          "CARRY8 must expose eight DI and S bits");
    for (unsigned b = 0; b < 8; ++b) {
      Check(std::holds_alternative<I>(di[b]) && std::holds_alternative<I>(s[b]),
            "incomplete LUT6_2 O5/O6 topology");
      auto a = semantics.Driver(std::get<I>(di[b])),
           other = semantics.Driver(std::get<I>(s[b]));
      Check(a && a == other && semantics.CellType(*a) == "LUT6_2",
            "incomplete LUT6_2 O5/O6 topology");
      auto o5 = semantics.Bits(*a, "O5"), o6 = semantics.Bits(*a, "O6");
      Check(o5 == std::vector<PhysicalBit>{di[b]} &&
                o6 == std::vector<PhysicalBit>{s[b]},
            "incomplete LUT6_2 O5/O6 topology");
      macro.members.push_back(
          {*a, "LUT6_2", std::string(1, char('A' + b)) + "6LUT"});
    }
    add(std::move(macro));
  }
  auto muxes = db.Prepare("SELECT id FROM cells WHERE type IN "
                          "('MUXF7','MUXF8','MUXF9') ORDER BY id");
  while (muxes.Step())
    for (auto port : {"I0", "I1"}) {
      auto d = driver(muxes.TypedText(0), port);
      if (Mux(semantics.CellType(d))) {
        auto r = db.Prepare("INSERT OR IGNORE INTO op_mux_child VALUES(?)");
        r.BindText(1, d);
        r.Execute();
      }
    }
  auto roots = db.Prepare(
      "SELECT id,type FROM cells WHERE type IN ('MUXF7','MUXF8','MUXF9') AND "
      "id NOT IN (SELECT instance FROM op_mux_child) ORDER BY id");
  index = 0;
  while (roots.Step()) {
    Stop(flag);
    auto root = roots.TypedText(0), type = roots.TypedText(1);
    auto kind = type;
    std::transform(kind.begin(), kind.end(), kind.begin(),
                   [](auto c) { return char(std::tolower(c)); });
    Macro macro{Pad("mux-cone-", index++), kind + "-cone", {}};
    std::function<void(std::string, int, int)> pack = [&](std::string name,
                                                          int level,
                                                          int offset) {
      Check(semantics.CellType(name) == "MUXF" + std::to_string(level),
            "mux cone stage type disagrees");
      auto l = driver(name, "I0"), r = driver(name, "I1");
      if (level == 7) {
        Check(Lut(semantics.CellType(l)) && Lut(semantics.CellType(r)),
              "MUXF7 is not driven by two LUTs");
        macro.members.push_back({l, semantics.CellType(l),
                                 std::string(1, char('A' + offset)) + "6LUT"});
        macro.members.push_back({r, semantics.CellType(r),
                                 std::string(1, char('B' + offset)) + "6LUT"});
      } else {
        pack(l, level - 1, offset);
        pack(r, level - 1, offset + (level == 9 ? 4 : 2));
      }
      auto bel = level == 9   ? std::string("F9MUX")
                 : level == 8 ? (offset == 0 ? std::string("F8MUX_BOT")
                                             : std::string("F8MUX_TOP"))
                              : std::string("F7MUX_") + char('A' + offset) +
                                    char('B' + offset);
      macro.members.push_back({name, semantics.CellType(name), bel});
    };
    pack(root, type.back() - '0', 0);
    add(std::move(macro));
  }
  auto bram =
      db.Prepare("SELECT id FROM cells WHERE type='RAMB18E2' ORDER BY id");
  index = 0;
  while (bram.Step())
    add({Pad("ramb18-half-site-occupancy-", index++),
         "ramb18-half-site-occupancy",
         {{bram.TypedText(0), "RAMB18E2", ""}}});
  auto unowned =
      db.Prepare("SELECT id FROM cells WHERE type IN "
                 "('MUXF7','MUXF8','MUXF9','CARRY8','LUT6_2') AND id NOT IN "
                 "(SELECT instance FROM op_macro_owned) LIMIT 1");
  Check(!unowned.Step(),
        "physical macro topology is incomplete; unrooted instances");
  auto cells = db.Prepare("SELECT type FROM cells");
  while (cells.Step()) {
    auto t = cells.TypedText(0);
    if (t != "GND" && t != "VCC")
      Resource(t);
  }
  semantics.DeriveCascades(
      [&](const auto &raw) {
        auto r = R::Parse(raw);
        result.cascades.push_back(std::move(r));
      },
      flag);
  semantics.RequireUnchanged();
  return result;
}
void ProduceSource(const ArtifactRef &mapped, const ArtifactRef &identity,
                   const std::string &top, const std::string &output,
                   const volatile std::sig_atomic_t *flag) {
  Check(fs::path(output).parent_path() ==
            fs::path(identity.path()).parent_path(),
        "atomic-source selector and mapped input must share a directory");
  Check(!fs::is_symlink(output), "atomic source selector is a symbolic link");
  auto info = DeriveSource(mapped, top, flag);
  for (auto &c : info.cascades)
    Check(c.at("cell_type").text() == "CARRY8",
          "atomic source rejects non-CARRY8 dedicated cascade connectivity");
  SqliteStore owner(mapped);
  auto &m = owner.database();
  auto selected = R::Parse(Meta(m, "top_json")).text();
  Scratch work(fs::path(output).parent_path());
  auto path = work.root / "source.sqlite";
  Sha256 cluster_hash, assignment_hash, cascade_hash, constant_hash;
  I physical = 0, constants = 0, assignments = 0, singletons = 0;
  {
    SqliteDatabase db(path.string(), SqliteDatabase::Mode::kCreateExclusive);
    db.Execute("PRAGMA application_id=1162696001;PRAGMA user_version=1;");
    db.Execute(kOpenparfAtomicSourceDDL);
    db.Execute("BEGIN");
    I group = 0;
    for (auto &macro : info.macros) {
      bool half = macro.kind == "ramb18-half-site-occupancy";
      A entries;
      I seq = 0;
      for (auto &member : macro.members) {
        R a = O{{"instance", member.instance},
                {"cell_type", member.type},
                {"bel_candidates",
                 half ? R(A{"RAMB18E2_L", "RAMB18E2_U"}) : R(A{member.bel})}};
        if (!half)
          a["bel"] = member.bel;
        auto r = db.Prepare("INSERT INTO assignments VALUES(?,?,?,?,?,?)");
        r.BindInteger(1, group);
        r.BindInteger(2, seq++);
        r.BindText(3, member.instance);
        r.BindText(4, member.type);
        if (half)
          r.BindNull(5);
        else
          r.BindText(5, member.bel);
        r.BindText(6, a.at("bel_candidates").encode());
        r.Execute();
        Feed(assignment_hash, a.encode() + "\n");
        entries.push_back(a);
        ++assignments;
      }
      R templates = half ? R(A{"RAMB18E2"}) : R(A{"SLICEL", "SLICEM"});
      auto kind = half                            ? "hard"
                  : macro.kind == "carry8-lut6_2" ? "carry"
                                                  : "slice";
      auto r = db.Prepare("INSERT INTO clusters VALUES(?,?,?,?,?,NULL)");
      r.BindInteger(1, group++);
      r.BindText(2, macro.id);
      r.BindText(3, kind);
      r.BindText(4, templates.encode());
      r.BindText(5, "null");
      r.Execute();
      Feed(cluster_hash, R(O{{"id", macro.id},
                             {"kind", kind},
                             {"site_templates", templates},
                             {"control_set", nullptr},
                             {"assignments", entries}})
                                 .encode() +
                             "\n");
    }
    I chain = 0;
    for (auto &c : info.cascades) {
      auto r = db.Prepare("INSERT INTO cascades VALUES(?,?,?)");
      r.BindInteger(1, chain++);
      r.BindText(2, c.at("id").text());
      r.BindText(3, c.encode());
      r.Execute();
      Feed(cascade_hash, c.encode() + "\n");
    }
    auto cells = m.Prepare("SELECT id,type FROM cells ORDER BY id");
    auto owned =
        db.Prepare("SELECT cell_type FROM assignments WHERE instance=?");
    while (cells.Step()) {
      Stop(flag);
      auto name = cells.TypedText(0), type = cells.TypedText(1);
      if (type == "GND" || type == "VCC") {
        auto r = db.Prepare("INSERT INTO constants VALUES(?,?)");
        r.BindInteger(1, constants++);
        r.BindText(2, name);
        r.Execute();
        Feed(constant_hash, R(name).encode() + "\n");
      } else {
        ++physical;
        owned.BindText(1, name);
        if (owned.Step())
          Check(owned.TypedText(0) == type, "macro type disagrees");
        else
          ++singletons;
        owned.Reset();
      }
    }
    auto sha = FileHash(identity, flag);
    R summary = O{{"physical_atoms", I(info.macros.size()) + singletons},
                  {"constant_cells", constants}};
    if (!info.macros.empty())
      summary["physical_macros"] = info.macros.size();
    R header = O{
        {"schema", "emuflow.openparf-atomic-source/v1"},
        {"status", "pass"},
        {"top", selected},
        {"source", O{{"mapped_sha256", sha}}},
        {"policy", O{{"provider", "mapped-singleton-atoms-no-site-packing-v1"},
                     {"dedicated_or_relative_constraints", "fail-closed"}}},
        {"summary", summary}};
    R counts = O{{"physical_cells", physical},
                 {"macro_clusters", info.macros.size()},
                 {"macro_assignments", assignments},
                 {"singleton_clusters", singletons},
                 {"cascade_chains", info.cascades.size()},
                 {"constant_cells", constants}};
    R descriptor =
        O{{"artifact", fs::path(identity.path()).filename().string()},
          {"bytes", R(identity.generation().bytes)},
          {"sha256", sha}};
    auto logical = Hash(R(O{{"header", header},
                            {"counts", counts},
                            {"mapped", descriptor},
                            {"cluster_sha256", Hex(cluster_hash)},
                            {"assignment_sha256", Hex(assignment_hash)},
                            {"cascade_sha256", Hex(cascade_hash)},
                            {"constant_sha256", Hex(constant_hash)}})
                            .encode());
    Metadata(db, "header", header);
    Metadata(db, "counts", counts);
    Metadata(db, "mapped", descriptor);
    Metadata(db, "logical_sha256", logical);
    db.Execute("COMMIT");
    info.selector = O{{"schema", "emuflow.openparf-atomic-source-storage/v1"},
                      {"logical_schema", "emuflow.openparf-atomic-source/v1"},
                      {"application_id", I(0x454d5541)},
                      {"user_version", 1},
                      {"header", header},
                      {"counts", counts},
                      {"mapped", descriptor},
                      {"logical_sha256", logical}};
  }
  auto store =
      ArtifactRef::Bind(path.string(), "openparf-atomic-source-store-v1");
  auto sha = FileHash(store, flag);
  auto final = fs::path(output).parent_path() /
               ("openparf-atomic-source." + sha + ".sqlite");
  std::vector<ArtifactRef> refs{mapped, identity};
  std::optional<fs::path> old;
  if (fs::exists(output) && !fs::is_symlink(output)) {
    auto previous =
        Read(ArtifactRef::Bind(output, "openparf-atomic-source-selector-v1"));
    if (previous.has("artifact")) {
      auto name = previous.at("artifact").text();
      if (fs::path(name).filename() == name &&
          name.rfind("openparf-atomic-source.", 0) == 0)
        old = fs::path(output).parent_path() / name;
    }
  }
  Check(!fs::is_symlink(final), "source store is a symbolic link");
  bool created = false;
  try {
    if (fs::exists(final))
      Check(FileHash(ArtifactRef::Bind(final.string(),
                                       "openparf-atomic-source-store-v1"),
                     flag) == sha,
            "source store seal disagrees");
    else {
      AtomicBinaryWriter writer(final.string(), refs);
      std::ifstream in(path, std::ios::binary);
      Copy(in, writer.stream());
      writer.Commit();
      created = true;
    }
    info.selector["artifact"] = final.filename().string();
    info.selector["store_bytes"] = R(store.generation().bytes);
    info.selector["store_sha256"] = sha;
    if (const auto fail =
            std::getenv("EMUFLOW_TEST_OPENPARF_SOURCE_PUBLICATION")) {
      Check(std::string(fail) == "before-selector" && physical <= 4096,
            "invalid publication fault injection");
      throw StageError(ErrorCode::kPublicationFailed,
                       "injected selector commit failure");
    }
    StagedArtifact staged_selector(output);
    Json(staged_selector.path(), info.selector, refs);
    AuditOpenparfSource(
        ArtifactRef::Bind(staged_selector.path(),
                          "openparf-atomic-source-selector-v1"),
        ArtifactRef::Bind(final.string(), "openparf-atomic-source-store-v1"),
        flag);
    PublishTemporaryFile(staged_selector.path(), output, refs);
  } catch (...) {
    if (created) {
      std::error_code e;
      fs::remove(final, e);
    }
    throw;
  }
  if (old && *old != final) {
    std::error_code e;
    fs::remove(*old, e);
  }
}
} // namespace emuflow::core::openparf_input
namespace emuflow::core {
StageResult BuildOpenparfSource(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *flag) {
  using namespace openparf_input;
  std::optional<BoundRequest> inputs;
  return RunStage(
      "physical.build-openparf-source",
      [&] {
        inputs = BindRequest(request);
        return inputs->refs;
      },
      [&](const auto &) {
        Scratch scratch(std::filesystem::path(output).parent_path());
        auto identity = inputs->get("mapped");
        auto mapped =
            identity.schema() == "xilinx-mapped-index-v2"
                ? identity
                : ImportSmallMapped(identity, inputs->config.at("top").text(),
                                    scratch.root / "mapped.sqlite");
        ProduceSource(mapped, identity, inputs->config.at("top").text(), output,
                      flag);
      },
      [&] {
        auto selector =
            ArtifactRef::Bind(output, "openparf-atomic-source-selector-v1");
        auto value = Read(selector);
        return std::vector<ArtifactRef>{
            selector,
            ArtifactRef::Bind((std::filesystem::path(output).parent_path() /
                               value.at("artifact").text())
                                  .string(),
                              "openparf-atomic-source-store-v1")};
      });
}
} // namespace emuflow::core
