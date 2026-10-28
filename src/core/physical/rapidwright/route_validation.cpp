#include "physical/rapidwright/route_validation.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <limits>
#include <memory>
#include <optional>
#include <regex>
#include <unistd.h>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using I = std::int64_t;
using Fields = std::map<std::string, std::string>;
void Check(bool good, const std::string &why) {
  if (!good)
    throw StageError(ErrorCode::kInvalidArtifact, "XilinxRouteDB " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "route certificate validation cancelled");
}
std::string Hex(Sha256 &h) {
  std::string out;
  for (auto b : h.finish()) {
    out += "0123456789abcdef"[b >> 4];
    out += "0123456789abcdef"[b & 15];
  }
  return out;
}
bool Digest(const std::string &s) {
  return s.size() == 64 &&
         s.find_first_not_of("0123456789abcdef") == std::string::npos;
}
std::string Hash(const ArtifactRef &ref,
                 const volatile std::sig_atomic_t *flag) {
  std::ifstream in(ref.path(), std::ios::binary);
  Check(bool(in), "cannot read source");
  Sha256 h;
  std::array<char, 65536> buf;
  while (in) {
    Stop(flag);
    in.read(buf.data(), buf.size());
    h.update(reinterpret_cast<const unsigned char *>(buf.data()), in.gcount());
  }
  Check(in.eof(), "source read failed");
  ref.RequireUnchanged();
  return Hex(h);
}
std::string Read(const ArtifactRef &ref, std::size_t bound) {
  Check(ref.generation().bytes <= bound, "manifest exceeds bound");
  std::ifstream in(ref.path(), std::ios::binary);
  Check(bool(in), "manifest is unreadable");
  std::string out(ref.generation().bytes, '\0');
  in.read(out.data(), out.size());
  Check(bool(in), "manifest is truncated");
  ref.RequireUnchanged();
  return out;
}
struct Scratch {
  fs::path root;
  Scratch() {
    auto text = (fs::temp_directory_path() / "route-audit-XXXXXX").string();
    Check(mkdtemp(text.data()), "scratch allocation failed");
    root = text;
  }
  ~Scratch() {
    std::error_code e;
    fs::remove_all(root, e);
  }
};
class Json {
  SqliteDatabase &db;

public:
  explicit Json(SqliteDatabase &value) : db(value) {}
  void Valid(const std::string &text, const std::string &kind) {
    auto r = db.Prepare("SELECT json_valid(?1),json_type(?1)");
    r.BindText(1, text);
    Check(r.Step() && r.Integer(0) && r.Text(1) == kind,
          "invalid JSON " + kind);
    auto d = db.Prepare("SELECT 1 FROM json_tree(?) WHERE parent IS NOT NULL "
                        "GROUP BY parent,key HAVING count(*)>1 LIMIT 1");
    d.BindText(1, text);
    Check(!d.Step(), "duplicate JSON field");
  }
  std::string Type(const std::string &text, const std::string &path) {
    auto r = db.Prepare("SELECT json_type(?1,?2)");
    r.BindText(1, text);
    r.BindText(2, path);
    Check(r.Step(), "invalid JSON field");
    return r.IsNull(0) ? "missing" : r.TypedText(0);
  }
  std::string Token(const std::string &text, const std::string &path) {
    auto r = db.Prepare(
        "SELECT CASE json_type(?1,?2) WHEN 'object' THEN json_extract(?1,?2) "
        "WHEN 'array' THEN json_extract(?1,?2) WHEN 'true' THEN 'true' WHEN "
        "'false' THEN 'false' WHEN 'null' THEN 'null' ELSE "
        "json_quote(json_extract(?1,?2)) END");
    r.BindText(1, text);
    r.BindText(2, path);
    Check(r.Step(), "missing field " + path);
    return r.TypedText(0);
  }
  std::string Text(const std::string &text, const std::string &path,
                   bool nonempty = true) {
    Check(Type(text, path) == "text", "invalid text " + path);
    auto value = DecodeJsonString(Token(text, path));
    Check(!nonempty || !value.empty(), "empty text " + path);
    return value;
  }
  I Int(const std::string &text, const std::string &path) {
    Check(Type(text, path) == "integer", "invalid integer " + path);
    return BoundedJson::IntegerToken(Token(text, path));
  }
  I OptionalInt(const std::string &text, const std::string &path,
                I fallback = 0) {
    return Type(text, path) == "missing" ? fallback : Int(text, path);
  }
  bool Bool(const std::string &text, const std::string &path) {
    const auto type = Type(text, path);
    Check(type == "true" || type == "false", "invalid boolean " + path);
    return type == "true";
  }
  double Number(const std::string &text, const std::string &path) {
    auto type = Type(text, path);
    Check(type == "integer" || type == "real",
          "route delay is invalid " + path);
    auto value = ParseFiniteDecimal(Token(text, path), "route delay");
    Check(value >= 0, "route delay is invalid " + path);
    return value;
  }
  I Size(const std::string &text, const std::string &path) {
    Check(Type(text, path) == "array", "invalid collection " + path);
    auto r = db.Prepare("SELECT json_array_length(?1,?2)");
    r.BindText(1, text);
    r.BindText(2, path);
    Check(r.Step(), "array absent");
    return r.TypedInteger(0);
  }
  template <class F>
  void Each(const std::string &text, const std::string &path, F visit) {
    Check(Type(text, path) == "array", "invalid collection " + path);
    auto rows = db.Prepare("SELECT value,type FROM json_each(?1,?2) ORDER BY "
                           "CAST(key AS INTEGER)");
    rows.BindText(1, text);
    rows.BindText(2, path);
    while (rows.Step()) {
      Check(rows.TypedText(1) == "object", "invalid record in " + path);
      visit(rows.TypedText(0));
    }
  }
  bool Same(const std::string &a, const std::string &b) {
    const std::string query =
        "SELECT 1 FROM (SELECT fullkey,type,atom FROM json_tree(?1) EXCEPT "
        "SELECT fullkey,type,atom FROM json_tree(?2)) UNION ALL SELECT 1 FROM "
        "(SELECT fullkey,type,atom FROM json_tree(?2) EXCEPT SELECT "
        "fullkey,type,atom FROM json_tree(?1)) LIMIT 1";
    auto r = db.Prepare(query);
    r.BindText(1, a);
    r.BindText(2, b);
    return !r.Step();
  }
};
I Count(SqliteDatabase &db, const std::string &sql) {
  auto r = db.Prepare(sql);
  Check(r.Step(), "aggregate absent");
  return r.TypedInteger(0);
}
void None(SqliteDatabase &db, const std::string &sql, const std::string &why) {
  Check(!db.Prepare(sql).Step(), why);
}
std::string Encode(const Fields &fields) { return BoundedJson::Encode(fields); }
std::string Generations(const FileGeneration &g) {
  return "[" + std::to_string(g.device) + "," + std::to_string(g.inode) + "," +
         std::to_string(g.bytes) + "," + std::to_string(g.modified_seconds) +
         "," + std::to_string(g.modified_nanoseconds) + "," +
         std::to_string(g.changed_seconds) + "," +
         std::to_string(g.changed_nanoseconds) + "]";
}
struct Inputs {
  std::vector<ArtifactRef> refs;
  std::map<std::string, std::size_t> roles;
  std::string config;
  const ArtifactRef &Get(const std::string &role) const {
    return refs.at(roles.at(role));
  }
  bool Has(const std::string &role) const { return roles.count(role); }
};
Inputs Sources(const ArtifactRef &request) {
  CheckedRequestReader r(request.path(), 8 * 1024 * 1024);
  Check(r.Read<std::uint64_t>() == 0x31565250464d5545ULL &&
            r.Read<std::uint32_t>() == 1,
        "validation request version disagrees");
  const auto count = r.Read<std::uint32_t>();
  Check(count >= 2 && count <= 6, "source role population invalid");
  Inputs result;
  for (unsigned i = 0; i < count; ++i) {
    auto role = r.ReadString32(false, 32), path = r.ReadString32(false, 32768),
         schema = r.ReadString32(false, 128);
    Check((role == "route" || role == "timing" || role == "mapped" ||
           role == "packed" || role == "placement" || role == "input") &&
              !result.roles.count(role),
          "source role invalid or repeated");
    Check(schema == (role == "route" ? "xilinx-route-manifest-v2"
                                     : "physical-input-document-v1"),
          "source schema disagrees");
    auto generation = ReadFileGeneration(r);
    auto ref = ArtifactRef::Bind(path, schema);
    if (!(generation == ref.generation()))
      throw StageError(ErrorCode::kMixedGeneration,
                       "route validation input generation changed");
    result.roles[role] = result.refs.size();
    result.refs.push_back(ref);
  }
  Check(result.Has("route") && result.Has("timing"),
        "route/timing source absent");
  result.config = r.ReadString32(false, 8 * 1024 * 1024);
  r.RequireEnd();
  result.refs.push_back(request);
  return result;
}
struct Lines {
  ArtifactRef source;
  std::ifstream in;
  Sha256 hash;
  I count = 0;
  const volatile std::sig_atomic_t *flag;
  Lines(ArtifactRef ref, const volatile std::sig_atomic_t *f)
      : source(ref), in(ref.path(), std::ios::binary), flag(f) {
    Check(bool(in), "payload missing");
  }
  bool Next(std::string &out) {
    Stop(flag);
    out.clear();
    char c;
    while (in.get(c)) {
      Check(c != '\0' && out.size() < 512ULL * 1024 * 1024,
            "payload record exceeds bound");
      out += c;
      if (c == '\n') {
        hash.update(reinterpret_cast<const unsigned char *>(out.data()),
                    out.size());
        ++count;
        return true;
      }
    }
    Check(!in.bad() && out.empty(), "payload has a truncated record");
    return false;
  }
  void Finish(const std::string &expected, I records) {
    Check(count == records, "payload record count disagrees");
    Check(Hex(hash) == expected, "payload digest disagrees");
    source.RequireUnchanged();
  }
};
ArtifactRef Payload(const ArtifactRef &route, const std::string &manifest,
                    Json &j, const std::string &name, Inputs &inputs) {
  const auto base = "$.payloads." + name;
  Check(j.Type(manifest, base) == "object", "payloads." + name + " is invalid");
  const auto path = j.Text(manifest, base + ".path"),
             format = j.Text(manifest, base + ".format");
  Check(fs::path(path).filename() == path && path != "." && path != ".." &&
            !fs::path(path).is_absolute() &&
            format == (name == "nets" ? "jsonl-object/v1" : "jsonl-array/v1") &&
            Digest(j.Text(manifest, base + ".sha256")) &&
            j.Int(manifest, base + ".records") >= 0,
        "payloads." + name + " is invalid");
  auto ref = ArtifactRef::Bind(
      (fs::path(route.path()).parent_path() / path).string(),
      name == "nets" ? "xilinx-route-nets-v1" : "physical-input-document-v1");
  Check(ref.path() != route.path(), "payload aliases manifest");
  inputs.roles[name] = inputs.refs.size();
  inputs.refs.push_back(ref);
  return ref;
}
void Schema(SqliteDatabase &db) {
  db.Execute(R"SQL(
PRAGMA temp_store=FILE;PRAGMA cache_size=-8192;
CREATE TABLE seen_net(name TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE pips(tile TEXT,w1 TEXT,w2 TEXT,net TEXT,PRIMARY KEY(tile,w1,w2)) WITHOUT ROWID;
CREATE TABLE edge(source TEXT,target TEXT,PRIMARY KEY(source,target)) WITHOUT ROWID;
CREATE INDEX edge_target ON edge(target);
CREATE TABLE root(node TEXT PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE sink(node TEXT NOT NULL);
CREATE TABLE gates(logical TEXT PRIMARY KEY,site TEXT UNIQUE,record TEXT) WITHOUT ROWID;
CREATE TABLE io(identity TEXT PRIMARY KEY,record TEXT) WITHOUT ROWID;
CREATE TABLE expected_io(identity TEXT PRIMARY KEY,record TEXT) WITHOUT ROWID;
)SQL");
}
struct Totals {
  I nets = 0, sinks = 0, pips = 0, static_nets = 0, static_sinks = 0,
    clocks = 0, internal = 0, extended = 0, with_pips = 0, timed = 0,
    boundary = 0, excluded = 0;
  double maximum = 0;
};
void Pip(SqliteDatabase &db, Json &j, const std::string &net,
         const std::string &raw, bool array, Totals &totals) {
  const auto tile = j.Text(raw, array ? "$[1]" : "$.tile"),
             w1 = j.Text(raw, array ? "$[2]" : "$.start_wire"),
             w2 = j.Text(raw, array ? "$[3]" : "$.end_wire"),
             start = j.Text(raw, array ? "$[4]" : "$.start_node"),
             end = j.Text(raw, array ? "$[5]" : "$.end_node");
  Check(start != end, "invalid directed adjacency");
  if (array) {
    Check(j.Size(raw, "$") == 8 && j.Text(raw, "$[0]") == net,
          "payloads.pips net ownership disagrees");
    j.Bool(raw, "$[6]");
    j.Bool(raw, "$[7]");
  }
  auto owner =
      db.Prepare("SELECT net FROM pips WHERE tile=? AND w1=? AND w2=?");
  owner.BindText(1, tile);
  owner.BindText(2, std::min(w1, w2));
  owner.BindText(3, std::max(w1, w2));
  if (owner.Step())
    Check(false, owner.TypedText(0) == net ? "duplicate PIP"
                                           : "routing conflict on PIP");
  auto add = db.Prepare("INSERT INTO pips VALUES(?,?,?,?)");
  add.BindText(1, tile);
  add.BindText(2, std::min(w1, w2));
  add.BindText(3, std::max(w1, w2));
  add.BindText(4, net);
  add.Execute();
  auto edge = db.Prepare("INSERT OR IGNORE INTO edge VALUES(?,?)");
  edge.BindText(1, start);
  edge.BindText(2, end);
  edge.Execute();
  ++totals.pips;
}
void Net(SqliteDatabase &db, Json &j, const std::string &raw, Lines *pips,
         Totals &t, const volatile std::sig_atomic_t *flag) {
  Stop(flag);
  j.Valid(raw, "object");
  const auto name = j.Text(raw, "$.net"), kind = j.Text(raw, "$.kind"),
             qualification = j.Text(raw, "$.qualification");
  Check(!j.Bool(raw, "$.has_gap"), "RWRoute reports gap routing");
  auto seen = db.Prepare("SELECT 1 FROM seen_net WHERE name=?");
  seen.BindText(1, name);
  Check(!seen.Step(), "duplicate or empty net identity");
  auto add = db.Prepare("INSERT INTO seen_net VALUES(?)");
  add.BindText(1, name);
  add.Execute();
  db.Execute("DELETE FROM edge;DELETE FROM root;DELETE FROM sink");
  const bool stat = kind == "static_gnd" || kind == "static_vcc";
  if (kind == "signal")
    Check(qualification == "ordinary-fabric-signal",
          "signal qualification is invalid");
  else if (kind == "clock") {
    Check(qualification == "dedicated-bufgce-clock" ||
              qualification ==
                  "dedicated-bufgce-clock-with-fabric-cascade-spur" ||
              qualification == "dedicated-bufgce-clock-extended-lcb-last-mile",
          "clock qualification is invalid");
    ++t.clocks;
    t.extended +=
        qualification == "dedicated-bufgce-clock-extended-lcb-last-mile";
  } else if (kind == "clock_internal") {
    Check(qualification == "lut2-prefilter-to-bufgce-v1" &&
              std::regex_match(name,
                               std::regex("__emuflow_clock_prefilter_c[0-9]+")),
          "internal clock qualification is invalid");
    ++t.internal;
  } else
    Check(stat && qualification == "device-tied-static",
          "unsupported routed net kind or static qualification");
  I source_count = 0, sink_count = 0, alternate = 0;
  std::string source_site;
  j.Each(raw, "$.pins", [&](const std::string &pin) {
    j.Text(pin, "$.site");
    j.Text(pin, "$.pin");
    auto node = j.Text(pin, "$.node");
    bool output = j.Bool(pin, "$.is_output");
    auto delay_type = j.Type(pin, "$.route_delay_ps");
    if (stat || output)
      Check(delay_type == "missing" || delay_type == "null",
            "static/source pin has a route delay");
    else {
      const auto delay = j.Number(pin, "$.route_delay_ps");
      t.maximum = std::max(t.maximum, delay);
      ++t.timed;
    }
    auto q = db.Prepare(output ? "INSERT INTO root VALUES(?)"
                               : "INSERT INTO sink VALUES(?)");
    q.BindText(1, node);
    q.Execute();
    if (output) {
      ++source_count;
      source_site = j.Text(pin, "$.site");
    } else
      ++sink_count;
  });
  Check(source_count + sink_count > 0, "routed net has no site pins");
  if (j.Type(raw, "$.alternate_sources") != "missing")
    j.Each(raw, "$.alternate_sources", [&](const std::string &pin) {
      j.Text(pin, "$.site");
      j.Text(pin, "$.pin");
      Check(j.Bool(pin, "$.is_output") &&
                j.Type(pin, "$.route_delay_ps") == "missing",
            "alternate_sources: invalid physical source");
      auto node = j.Text(pin, "$.node");
      auto q = db.Prepare("INSERT OR IGNORE INTO root VALUES(?)");
      q.BindText(1, node);
      q.Execute();
      ++alternate;
    });
  if (stat) {
    Check(source_count == 0 && alternate == 0 && sink_count > 0 &&
              j.Size(raw, "$.roots") > 0,
          "static route roots/sinks are invalid");
    auto roots = db.Prepare("SELECT value,type FROM json_each(?,'$.roots')");
    roots.BindText(1, raw);
    I count = 0;
    while (roots.Step()) {
      Check(roots.TypedText(1) == "text" && !roots.TypedText(0).empty(),
            "invalid static root");
      auto q = db.Prepare("INSERT OR IGNORE INTO root VALUES(?)");
      q.BindText(1, roots.TypedText(0));
      q.Execute();
      ++count;
    }
    Check(count == Count(db, "SELECT count(*) FROM root"),
          "duplicate static route roots");
    ++t.static_nets;
    t.static_sinks += sink_count;
  } else {
    Check(source_count == 1 && sink_count > 0 &&
              j.Type(raw, "$.roots") == "missing" &&
              Count(db, "SELECT count(*) FROM root") == 1 + alternate,
          "routed net lacks one source and sinks or conflicting physical route "
          "source");
    None(db, "SELECT 1 FROM root JOIN sink USING(node) LIMIT 1",
         "duplicate or conflicting physical route source");
    if (kind == "clock") {
      auto gate = db.Prepare("SELECT 1 FROM gates WHERE site=?");
      gate.BindText(1, source_site);
      Check(gate.Step(), "clock source is not a declared BUFGCE site");
    }
  }
  Check(j.Bool(raw, "$.source_present") == bool(source_count),
        "source-presence summary disagrees");
  Check(j.Int(raw, "$.sink_count") == sink_count,
        "sink-count summary disagrees");
  I count = 0;
  if (pips) {
    const auto required = j.Int(raw, "$.pip_count");
    Check(required >= 0, "pip count is invalid");
    for (; count < required; ++count) {
      std::string line;
      Check(pips->Next(line), "payloads.pips ends inside net " + name);
      j.Valid(line, "array");
      Pip(db, j, name, line, true, t);
    }
  } else
    j.Each(raw, "$.pips", [&](const std::string &pip) {
      Pip(db, j, name, pip, false, t);
      ++count;
    });
  t.with_pips += count > 0;
  None(db,
       "WITH RECURSIVE reachable(node) AS (SELECT node FROM root UNION SELECT "
       "edge.target FROM edge JOIN reachable ON edge.source=reachable.node) "
       "SELECT 1 FROM sink WHERE node NOT IN (SELECT node FROM reachable) "
       "LIMIT 1",
       "sinks are not source-connected");
  ++t.nets;
  t.sinks += sink_count;
}
void Gates(SqliteDatabase &db, Json &j, const std::string &manifest, I expected,
           I prefiltered) {
  I count = 0, pre = 0;
  if (j.Type(manifest, "$.clock_gates") != "missing")
    j.Each(manifest, "$.clock_gates", [&](const std::string &raw) {
      BoundedJson gate(raw);
      const std::set<std::string> base{
          "safe_cell",     "logical_cell", "logical_type", "physical_type",
          "physical_site", "physical_bel", "ce_type",      "qualification"};
      auto extended = base;
      extended.insert({"input_site", "implementation", "cascaded_input"});
      auto fields = gate.Fields();
      std::set<std::string> keys;
      for (const auto &f : fields)
        keys.insert(f.first);
      Check(keys == base || keys == extended,
            "clock_gates record keys are invalid");
      const auto impl = gate.Has("implementation") ? gate.Text("implementation")
                                                   : "direct-bufgce";
      bool direct = impl == "direct-bufgce",
           filtered = impl == "lut2-prefilter-bufgce-tied-high";
      Check(gate.Text("logical_type") == "LUT2" &&
                gate.Text("physical_type") == "BUFGCE" &&
                (direct || filtered) &&
                gate.Text("ce_type") ==
                    std::string(direct ? "ASYNC" : "ASYNC_TIED_HIGH") &&
                (!filtered || keys == extended) &&
                gate.Text("physical_bel") == "BUFCE" &&
                gate.Text("qualification") ==
                    "lut2-init-1000-fdre-latched-enable-v1",
            "clock_gates record is invalid");
      j.Text(raw, "$.safe_cell");
      if (gate.Has("cascaded_input"))
        Check(!gate.Boolean("cascaded_input") || filtered,
              "clock gate cascade implementation disagrees");
      if (gate.Has("input_site"))
        j.Text(raw, "$.input_site");
      auto insert = db.Prepare("INSERT OR IGNORE INTO gates VALUES(?,?,?)");
      insert.BindText(1, j.Text(raw, "$.logical_cell"));
      insert.BindText(2, j.Text(raw, "$.physical_site"));
      insert.BindText(3, raw);
      insert.Execute();
      ++count;
      pre += filtered;
    });
  Check(count == expected && pre == prefiltered &&
            count == Count(db, "SELECT count(*) FROM gates"),
        "clock-gate/prefilter accounting disagrees or sites repeat");
}
void VirtualIo(SqliteDatabase &db, Json &j, const std::string &manifest,
               const std::string &config) {
  if (j.Type(config, "$.virtual_io") != "null") {
    Check(j.Type(config, "$.virtual_io") == "object",
          "virtual I/O contract invalid");
    j.Each(config, "$.virtual_io.entries", [&](const std::string &row) {
      auto r = db.Prepare("INSERT OR IGNORE INTO expected_io VALUES(?,?)");
      r.BindText(1, j.Text(row, "$.id"));
      r.BindText(2, row);
      r.Execute();
    });
    Check(j.Size(config, "$.virtual_io.entries") ==
              Count(db, "SELECT count(*) FROM expected_io"),
          "virtual I/O contract repeats identity");
  }
  if (j.Type(manifest, "$.virtual_io") != "missing")
    j.Each(manifest, "$.virtual_io", [&](const std::string &row) {
      auto r = db.Prepare("INSERT OR IGNORE INTO io VALUES(?,?)");
      r.BindText(1, j.Text(row, "$.id"));
      r.BindText(2, row);
      r.Execute();
    });
  const I count = j.Type(manifest, "$.virtual_io") == "missing"
                      ? 0
                      : j.Size(manifest, "$.virtual_io");
  Check(count == Count(db, "SELECT count(*) FROM io") &&
            count == Count(db, "SELECT count(*) FROM expected_io"),
        "virtual I/O coverage disagrees");
  None(db,
       "SELECT 1 FROM io LEFT JOIN expected_io USING(identity) WHERE "
       "expected_io.identity IS NULL LIMIT 1",
       "virtual I/O coverage disagrees");
  auto rows = db.Prepare(
      "SELECT e.record,o.record FROM expected_io e JOIN io o USING(identity)");
  while (rows.Step()) {
    const auto expected = rows.TypedText(0), observed = rows.TypedText(1);
    for (const auto &field : std::vector<std::pair<std::string, std::string>>{
             {"id", "id"},
             {"direction", "direction"},
             {"logical_port", "logical_port"},
             {"logical_bit", "logical_bit"},
             {"iostandard", "iostandard"},
             {"p_port", "p.port"},
             {"n_port", "n.port"},
             {"p_package_pin", "p.package_pin"},
             {"n_package_pin", "n.package_pin"},
             {"p_site", "p.site"},
             {"n_site", "n.site"}})
      Check(j.Same(j.Token(observed, "$." + field.first),
                   j.Token(expected, "$." + field.second)),
            "virtual I/O differs from XDC contract");
    j.Number(observed, "$.route_delay_ps");
  }
}
void MappedAccounting(Inputs &inputs, SqliteDatabase &db, Json &j,
                      const std::string &manifest, I route_cells, I dsp) {
  if (!inputs.Has("mapped") || !inputs.Has("packed"))
    return;
  std::ifstream prefix(inputs.Get("mapped").path(), std::ios::binary);
  char magic[16]{};
  prefix.read(magic, 16);
  const bool indexed =
      std::string(magic, 16) == std::string("SQLite format 3\0", 16);
  std::unique_ptr<SqliteStore> mapped_store;
  std::string records;
  if (indexed) {
    auto ref = ArtifactRef::Bind(inputs.Get("mapped").path(),
                                 "xilinx-mapped-index-v2");
    mapped_store = std::make_unique<SqliteStore>(ref);
  } else {
    // Explicitly bounded read-only evidence. No legacy producer or large-design
    // fallback is installed; current production always supplies the typed
    // owner.
    const auto raw = Read(inputs.Get("mapped"), 8 * 1024 * 1024);
    j.Valid(raw, "object");
    const auto packed = Read(inputs.Get("packed"), 8 * 1024 * 1024);
    j.Valid(packed, "object");
    const auto top = j.Text(packed, "$.top");
    auto module = db.Prepare("SELECT json_extract(value,'$.cells') FROM "
                             "json_each(?,'$.modules') WHERE key=?");
    module.BindText(1, raw);
    module.BindText(2, top);
    Check(module.Step() && !module.IsNull(0), "mapped top is invalid");
    records = module.TypedText(0);
    Check(!module.Step(), "mapped top repeats");
    auto count = db.Prepare("SELECT count(*) FROM json_each(?)");
    count.BindText(1, records);
    Check(count.Step() && count.TypedInteger(0) <= 4096,
          "legacy mapped evidence exceeds bound");
  }
  auto query = [&](const std::string &type) {
    auto r = indexed
                 ? mapped_store->database().Prepare(
                       type.empty() ? "SELECT count(*) FROM cells"
                                    : "SELECT count(*) FROM cells WHERE type=?")
                 : db.Prepare(type.empty()
                                  ? "SELECT count(*) FROM json_each(?)"
                                  : "SELECT count(*) FROM json_each(?1) WHERE "
                                    "json_extract(value,'$.type')=?2");
    if (!indexed)
      r.BindText(1, records);
    if (!type.empty())
      r.BindText(indexed ? 1 : 2, type);
    Check(r.Step(), "mapped count absent");
    return r.TypedInteger(0);
  };
  Check(route_cells ==
            query("") + query("LUT6_2") + Count(db, "SELECT count(*) FROM io"),
        "route-cell accounting disagrees");
  Check(dsp == query("DSP48E2"), "DSP48E2 transform count disagrees");
  auto gates = db.Prepare("SELECT logical FROM gates ORDER BY logical");
  while (gates.Step()) {
    auto cell =
        indexed
            ? mapped_store->database().Prepare(
                  "SELECT type,json_extract(record_json,'$.parameters.INIT') "
                  "FROM cells WHERE id=?")
            : db.Prepare("SELECT "
                         "json_extract(value,'$.type'),json_extract(value,'$."
                         "parameters.INIT') FROM json_each(?1) WHERE key=?2");
    if (!indexed)
      cell.BindText(1, records);
    cell.BindText(indexed ? 1 : 2, gates.TypedText(0));
    Check(cell.Step() && cell.TypedText(0) == "LUT2" && !cell.IsNull(1),
          "clock-gate logical identity disagrees");
    const auto init = cell.Text(1);
    std::string bits = init;
    bits.erase(0, bits.find_first_not_of('0'));
    Check(bits == "1000" || init == "8" || init == "0x8",
          "clock-gate logical identity disagrees");
  }
  inputs.Get("mapped").RequireUnchanged();
}
void Exclusions(SqliteDatabase &db, Json &j, const std::string &manifest,
                Totals &t) {
  j.Each(manifest, "$.excluded_nets", [&](const std::string &raw) {
    BoundedJson record(raw);
    const auto name = j.Text(raw, "$.net"), reason = j.Text(raw, "$.reason");
    if (reason == "boundary_clock") {
      record.Keys({"net", "reason", "qualification"});
      Check(record.Text("qualification") == "ideal-boundary-clock",
            "boundary clock qualification is invalid");
      ++t.boundary;
    } else if (reason == "clock_gate_cascade_internalized") {
      record.Keys({"net", "reason", "qualification"});
      Check(record.Text("qualification") == "lut2-prefilter-fabric-edge-v1",
            "internalized clock-edge qualification is invalid");
    } else {
      record.Keys({"net", "reason"});
      Check(reason == "boundary_or_driverless" ||
                reason == "multiple_driver_or_sinkless" ||
                reason == "intra_site",
            "invalid exclusion record");
    }
    auto seen = db.Prepare("SELECT 1 FROM seen_net WHERE name=?");
    seen.BindText(1, name);
    Check(!seen.Step(), "invalid or conflicting exclusion");
    auto insert = db.Prepare("INSERT INTO seen_net VALUES(?)");
    insert.BindText(1, name);
    insert.Execute();
    ++t.excluded;
  });
}
} // namespace
StageResult ValidateRouteCertificate(const ArtifactRef &request_ref,
                                     const std::string &summary,
                                     const volatile std::sig_atomic_t *flag) {
  std::optional<Inputs> input;
  std::string manifest;
  return RunStage(
      "physical.validate-route",
      [&] {
        input = Sources(request_ref);
        auto &inputs = *input;
        manifest = Read(inputs.Get("route"), 256ULL * 1024 * 1024);
        SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
        Json j(db);
        j.Valid(manifest, "object");
        auto schema = j.Text(manifest, "$.schema");
        Check(schema == "emuflow.xilinx-route-db/v1" ||
                  schema == "emuflow.xilinx-route-db/v2",
              "header is invalid");
        if (schema == "emuflow.xilinx-route-db/v2") {
          BoundedJson payloads(j.Token(manifest, "$.payloads"));
          payloads.Keys({"nets", "pips"});
          Payload(inputs.Get("route"), manifest, j, "nets", inputs);
          Payload(inputs.Get("route"), manifest, j, "pips", inputs);
          Check(inputs.Get("nets").path() != inputs.Get("pips").path(),
                "payload owners alias");
        } else
          Check(inputs.Get("route").generation().bytes <= 8 * 1024 * 1024 &&
                    j.Size(manifest, "$.nets") <= 4096,
                "inline route evidence exceeds bounded reader limit");
        return inputs.refs;
      },
      [&](const auto &refs) {
        auto &inputs = *input;
        Scratch scratch;
        SqliteDatabase db((scratch.root / "audit.sqlite").string(),
                          SqliteDatabase::Mode::kCreateExclusive);
        Schema(db);
        SqliteTransaction transaction(db);
        Json j(db);
        j.Valid(inputs.config, "object");
        const auto config = inputs.config;
        BoundedJson configuration(config);
        configuration.Keys({"schema", "virtual_io", "virtual_io_sha256"});
        Check(configuration.Text("schema") ==
                  "emuflow.route-validation-request/v1",
              "configuration schema disagrees");
        Check(j.Text(manifest, "$.status") == "candidate" ||
                  j.Text(manifest, "$.status") == "pass",
              "status is invalid");
        Check(j.Text(manifest, "$.provider") == "rapidwright-rwroute-2026.1.0",
              "provider identity is invalid");
        const auto cells = j.Int(manifest, "$.cells"),
                   physical =
                       j.Int(manifest, "$.materialization.physical_cells"),
                   dsp = j.Int(manifest,
                               "$.materialization.transformed_dsp48e2_cells"),
                   gates = j.OptionalInt(
                       manifest,
                       "$.materialization.dedicated_clock_gate_cells"),
                   pre = j.OptionalInt(
                       manifest,
                       "$.materialization.fabric_prefilter_clock_gate_cells");
        Check(cells >= 0 &&
                  j.Int(manifest, "$.materialization.route_cells") == cells,
              "materialized route-cell count is invalid");
        Check(dsp >= 0 && dsp <= cells && gates >= 0 && pre >= 0 &&
                  pre <= gates && dsp <= std::numeric_limits<I>::max() / 7 &&
                  cells <= std::numeric_limits<I>::max() - 7 * dsp - pre &&
                  physical == cells + 7 * dsp + pre &&
                  j.Text(manifest, "$.materialization.router") ==
                      "CUFR-HUS-non-timing-driven-uturn-enabled-parallel-"
                      "unroutable-recovery-extended-lcb-clock",
              "transformed-cell accounting is invalid");
        Gates(db, j, manifest, gates, pre);
        VirtualIo(db, j, manifest, config);
        MappedAccounting(inputs, db, j, manifest, cells, dsp);
        for (const auto &field :
             std::vector<std::pair<std::string, std::string>>{
                 {"mapped_sha256", "mapped"},
                 {"packed_sha256", "packed"},
                 {"placement_sha256", "placement"}}) {
          auto digest = j.Text(manifest, "$.source." + field.first);
          Check(Digest(digest), "source." + field.first + " is invalid");
          if (inputs.Has(field.second))
            Check(Hash(inputs.Get(field.second), flag) == digest,
                  "source." + field.first + " does not match input");
        }
        const auto io_kind =
            j.Type(manifest, "$.source.virtual_io_contract_sha256");
        auto expected_io = j.Type(config, "$.virtual_io_sha256");
        Check((expected_io == "null" &&
               (io_kind == "null" || io_kind == "missing")) ||
                  (expected_io == "text" && io_kind == "text" &&
                   j.Text(manifest, "$.source.virtual_io_contract_sha256") ==
                       j.Text(config, "$.virtual_io_sha256")),
              "virtual I/O source seal disagrees");
        const auto input_digest =
            j.Text(manifest, "$.source.rwroute_input_sha256");
        Check(Digest(input_digest), "source.rwroute_input_sha256 is invalid");
        if (inputs.Has("input"))
          Check(Hash(inputs.Get("input"), flag) == input_digest,
                "source.rwroute_input_sha256 does not match the routed input");
        Totals t;
        std::unique_ptr<Lines> nets, pips;
        if (inputs.Has("nets")) {
          nets = std::make_unique<Lines>(inputs.Get("nets"), flag);
          pips = std::make_unique<Lines>(inputs.Get("pips"), flag);
          std::string raw;
          while (nets->Next(raw))
            Net(db, j, raw, pips.get(), t, flag);
          Check(!pips->Next(raw), "payloads.pips has unbound records");
          nets->Finish(j.Text(manifest, "$.payloads.nets.sha256"),
                       j.Int(manifest, "$.payloads.nets.records"));
          pips->Finish(j.Text(manifest, "$.payloads.pips.sha256"),
                       j.Int(manifest, "$.payloads.pips.records"));
        } else
          j.Each(manifest, "$.nets", [&](const std::string &net) {
            Net(db, j, net, nullptr, t, flag);
          });
        Exclusions(db, j, manifest, t);
        Fields expected{
            {"candidate_nets", std::to_string(t.nets - t.static_nets)},
            {"certificate_nets", std::to_string(t.nets)},
            {"static_nets", std::to_string(t.static_nets)},
            {"static_sinks", std::to_string(t.static_sinks)},
            {"nets_with_pips", std::to_string(t.with_pips)},
            {"pips", std::to_string(t.pips)},
            {"excluded_nets", std::to_string(t.excluded)},
            {"boundary_clock_nets", std::to_string(t.boundary)}};
        if (t.internal ||
            j.Type(manifest, "$.summary.internal_clock_nets") != "missing")
          expected["internal_clock_nets"] = std::to_string(t.internal);
        if (t.extended ||
            j.Type(manifest, "$.summary.extended_lcb_clock_nets") != "missing")
          expected["extended_lcb_clock_nets"] = std::to_string(t.extended);
        Check(j.Same(j.Token(manifest, "$.summary"), Encode(expected)),
              "summary disagrees with route certificate");
        BoundedJson timing_contract(BoundedJson::Read(inputs.Get("timing")));
        timing_contract.Keys({"schema", "provider", "timing"});
        Check(timing_contract.Text("schema") ==
                      "emuflow.rapidwright-timing-contract/v1" &&
                  timing_contract.Text("provider") ==
                      j.Text(manifest, "$.provider"),
              "timing contract identity disagrees");
        BoundedJson timing_expected(timing_contract.Token("timing"));
        for (const auto &f : timing_expected.Fields())
          Check(j.Same(j.Token(manifest, "$.timing." + f.first), f.second),
                "timing." + f.first + " is invalid");
        Check(j.Int(manifest, "$.timing.routed_endpoints") == t.timed,
              "timed endpoint count disagrees");
        const auto coefficients =
            j.Token(manifest, "$.timing.logic_coefficients_ps");
        BoundedJson coefficient_fields(coefficients);
        coefficient_fields.Keys({"ff_clock_to_q", "carry_co", "lut_a1",
                                 "lut_a2", "lut_a3", "lut_a4", "lut_a5",
                                 "lut_a6"});
        for (const auto &field : coefficient_fields.Fields())
          j.Number(coefficients, "$." + field.first);
        const double reported =
            j.Number(manifest, "$.timing.maximum_route_delay_ps");
        Check(std::abs(reported - t.maximum) <=
                  std::max(1e-3, 1e-6 * std::max(reported, t.maximum)),
              "maximum route delay disagrees");
        Fields generations{
            {"route", Generations(inputs.Get("route").generation())}};
        if (inputs.Has("nets")) {
          generations["nets"] = Generations(inputs.Get("nets").generation());
          generations["pips"] = Generations(inputs.Get("pips").generation());
        }
        Fields result{
            {"status", JsonQuote("pass")},
            {"schema", JsonQuote("emuflow.xilinx-route-validation/v1")},
            {"implementation_id",
             JsonQuote("native-xilinx-route-validation-v1")},
            {"nets", std::to_string(t.nets)},
            {"sinks", std::to_string(t.sinks)},
            {"pips", std::to_string(t.pips)},
            {"route_sha256", JsonQuote(Hash(inputs.Get("route"), flag))},
            {"timing_contract_sha256",
             JsonQuote(Hash(inputs.Get("timing"), flag))},
            {"excluded_nets", std::to_string(t.excluded)},
            {"boundary_clock_nets", std::to_string(t.boundary)},
            {"timed_endpoints", std::to_string(t.timed)},
            {"maximum_route_delay_ps", JsonRecord(t.maximum).Encode()},
            {"timing_provider", JsonQuote("rapidwright-lightweight")},
            {"hold_analysis", JsonQuote("unavailable")},
            {"route_cells", std::to_string(cells)},
            {"physical_cells", std::to_string(physical)},
            {"virtual_io_cells",
             std::to_string(Count(db, "SELECT count(*) FROM io"))},
            {"transformed_dsp48e2_cells", std::to_string(dsp)},
            {"dedicated_clock_gate_cells", std::to_string(gates)},
            {"fabric_prefilter_clock_gate_cells", std::to_string(pre)},
            {"static_nets", std::to_string(t.static_nets)},
            {"static_sinks", std::to_string(t.static_sinks)},
            {"clock_nets", std::to_string(t.clocks)},
            {"internal_clock_nets", std::to_string(t.internal)},
            {"extended_lcb_clock_nets", std::to_string(t.extended)},
            {"source_generations", Encode(generations)}};
        if (inputs.Has("mapped") && inputs.Has("packed") &&
            inputs.Has("placement"))
          result["source_sha256"] = Encode(
              {{"mapped_sha256", j.Token(manifest, "$.source.mapped_sha256")},
               {"packed_sha256", j.Token(manifest, "$.source.packed_sha256")},
               {"placement_sha256",
                j.Token(manifest, "$.source.placement_sha256")}});
        transaction.Commit();
        Stop(flag);
        PublishTextArtifact(summary, refs, [&](std::ostream &out) {
          out << Encode(result) << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "xilinx-route-validation-report-v1")};
      });
}
} // namespace emuflow::core
