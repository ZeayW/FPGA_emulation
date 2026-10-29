#include "physical/device.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <array>
#include <cctype>
#include <fstream>
#include <map>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
using I = std::int64_t;
using Raw = std::map<std::string, std::string>;
using Obj = std::map<std::string, JsonRecord>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "physical device: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "physical device cancelled");
}
std::string Text(const std::string &value) {
  Check(!value.empty() && value.find('\0') == std::string::npos,
        "empty or NUL identity");
  (void)JsonQuoteUtf8(value);
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
  std::ifstream in(input.path(), std::ios::binary);
  Check(bool(in), "cannot read source");
  Sha256 hash;
  std::array<char, 65536> block;
  while (in) {
    Stop(flag);
    in.read(block.data(), block.size());
    hash.update(reinterpret_cast<const unsigned char *>(block.data()),
                in.gcount());
  }
  Check(in.eof(), "source read failed");
  input.RequireUnchanged();
  return Digest(hash);
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value "
                        "END FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step() && !row.IsNull(0), "metadata missing or exceeds bound");
  const auto value = row.TypedText(0);
  Check(!row.Step(), "metadata repeats");
  return value;
}
// Source framing is bounded by bytes and recursion, not by the number of
// names in one dedicated-adjacency family. JSON token helpers retain only the
// current record; native device families can legitimately exceed 64k nodes.
class DeviceObject {
public:
  explicit DeviceObject(const std::string &raw) {
    Check(raw.size() <= 32 * 1024 * 1024, "record exceeds bound");
    VisitJsonObjectTokens(raw, [&](const auto &key, const auto &value) {
      Check(fields.emplace(key, value).second, "duplicate record key");
    });
  }
  Raw Fields() const { return fields; }
  bool Has(const std::string &key) const { return fields.count(key); }
  std::string Token(const std::string &key) const {
    auto found = fields.find(key);
    Check(found != fields.end(), "missing record field " + key);
    return found->second;
  }
  std::string Text(const std::string &key) const {
    return DecodeJsonString(Token(key));
  }
  I Integer(const std::string &key) const {
    return BoundedJson::IntegerToken(Token(key));
  }
  void Keys(const std::set<std::string> &keys) const {
    Check(fields.size() == keys.size(), "unknown or absent record field");
    for (const auto &f : fields)
      Check(keys.count(f.first), "unexpected record field");
  }

private:
  Raw fields;
};
std::string Canonical(SqliteDatabase &db, const std::string &token) {
  auto type = db.Prepare("SELECT json_type(?)");
  type.BindText(1, token);
  Check(type.Step(), "invalid JSON scalar");
  const auto kind = type.Text(0);
  if (kind == "object") {
    DeviceObject doc(token);
    std::string out = "{";
    for (const auto &f : doc.Fields()) {
      if (out.size() > 1)
        out += ',';
      out += JsonQuote(f.first) + ":" + Canonical(db, f.second);
    }
    return out + '}';
  }
  if (kind == "array") {
    std::string out = "[";
    VisitJsonArrayTokens(token, [&](const std::string &item) {
      if (out.size() > 1)
        out += ',';
      out += Canonical(db, item);
    });
    return out + ']';
  }
  if (kind == "text")
    return JsonQuote(DecodeJsonString(token));
  Check(kind == "integer" || kind == "true" || kind == "false" ||
            kind == "null",
        "unsupported nonintegral device fact");
  return token;
}
// Structural streaming framing; only one bounded site/family record is held.
class Stream {
public:
  Stream(std::istream &input, SqliteDatabase &db,
         const volatile std::sig_atomic_t *flag)
      : in(input), db(db), flag(flag) {}
  template <class F> void Object(F visit) {
    Expect('{');
    if (Peek() == '}') {
      in.get();
      return;
    }
    std::set<std::string> seen;
    for (;;) {
      Check(Peek() == '"', "JSON object key is not string");
      const auto key = DecodeJsonString(Value());
      Check(seen.size() < 65536 && seen.insert(key).second,
            "duplicate or excessive device keys");
      Expect(':');
      visit(key);
      const auto c = Peek();
      Check(c == ',' || c == '}', "invalid object delimiter");
      in.get();
      if (c == '}')
        break;
    }
  }
  template <class F> void Array(F visit) {
    Expect('[');
    if (Peek() == ']') {
      in.get();
      return;
    }
    for (;;) {
      visit(Value());
      const auto c = Peek();
      Check(c == ',' || c == ']', "invalid array delimiter");
      in.get();
      if (c == ']')
        break;
    }
  }
  std::string Value() {
    std::string out;
    Read(&out, 0);
    return out;
  }
  void Skip() { Read(nullptr, 0); }
  void Finish() {
    Check(Peek() == EOF && !in.bad(), "trailing or unreadable JSON");
  }

private:
  int Peek() {
    Stop(flag);
    while (in && (in.peek() == ' ' || in.peek() == '\t' || in.peek() == '\n' ||
                  in.peek() == '\r'))
      in.get();
    return in.peek();
  }
  void Expect(char c) {
    Check(Peek() == c, "unexpected JSON token");
    in.get();
  }
  void Add(std::string *out, int c) {
    if (out) {
      Check(out->size() < 32 * 1024 * 1024, "device record exceeds bound");
      out->push_back(char(c));
    }
  }
  void Read(std::string *out, unsigned depth) {
    Check(depth < 64, "device JSON depth exceeds bound");
    const auto first = Peek();
    Check(first != EOF, "truncated JSON");
    if (first == '"') {
      Add(out, in.get());
      for (;;) {
        const auto c = in.get();
        Check(c != EOF && c >= 32, "invalid JSON string");
        Add(out, c);
        if (c == '"')
          return;
        if (c == '\\') {
          const auto e = in.get();
          Check(e != EOF, "truncated JSON escape");
          Add(out, e);
          if (e == 'u') {
            for (unsigned i = 0; i < 4; ++i) {
              const auto d = in.get();
              Check(d != EOF && std::isxdigit(static_cast<unsigned char>(d)),
                    "invalid Unicode escape");
              Add(out, d);
            }
          } else
            Check(std::string("\"\\/bfnrt").find(char(e)) != std::string::npos,
                  "invalid JSON escape");
        }
      }
    }
    if (first == '{' || first == '[') {
      Add(out, in.get());
      const auto end = first == '{' ? '}' : ']';
      if (Peek() == end) {
        Add(out, in.get());
        return;
      }
      for (;;) {
        if (first == '{') {
          Check(Peek() == '"', "invalid object key");
          Read(out, depth + 1);
          Expect(':');
          Add(out, ':');
        }
        Read(out, depth + 1);
        const auto c = Peek();
        Check(c == ',' || c == end, "invalid nested delimiter");
        Add(out, in.get());
        if (c == end)
          return;
      }
    }
    std::string token;
    for (;;) {
      const auto c = in.peek();
      if (c == EOF || c == ',' || c == ']' || c == '}' ||
          std::isspace(static_cast<unsigned char>(c)))
        break;
      Check(token.size() < 128, "scalar exceeds bound");
      token += char(in.get());
    }
    auto valid = db.Prepare("SELECT json_valid(?)");
    valid.BindText(1, token);
    Check(!token.empty() && valid.Step() && valid.Integer(0),
          "invalid JSON scalar");
    if (out) {
      Check(out->size() + token.size() <= 32 * 1024 * 1024,
            "record exceeds bound");
      *out += token;
    }
  }
  std::istream &in;
  SqliteDatabase &db;
  const volatile std::sig_atomic_t *flag;
};
std::set<std::string> Strings(const std::string &raw) {
  std::set<std::string> out;
  for (const auto &token : BoundedJson::Array(raw, 65536))
    Check(out.insert(Text(DecodeJsonString(token))).second,
          "duplicate string fact");
  return out;
}
bool Ff(const std::string &type) {
  return type == "FDCE" || type == "FDPE" || type == "FDRE" || type == "FDSE";
}
std::string EncodeBits(const std::vector<PhysicalBit> &bits) {
  std::vector<JsonRecord> out;
  for (const auto &bit : bits)
    if (const auto *n = std::get_if<I>(&bit))
      out.emplace_back(*n);
    else
      out.emplace_back(std::get<std::string>(bit));
  return JsonRecord(out).Encode();
}
} // namespace
DevicePackageGeometry
ReadDevicePackageGeometry(const ArtifactRef &architecture,
                          const std::string &part,
                          const volatile std::sig_atomic_t *cancelled) {
  DevicePackageGeometry result{architecture, part, "", {}};
  SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
  std::ifstream input(architecture.path(), std::ios::binary);
  Check(bool(input), "cannot read package geometry");
  Stream stream(input, db, cancelled);
  Raw header;
  std::size_t banks = 0;
  stream.Object([&](const auto &key) {
    if (key == "schema" || key == "coordinate_transform" ||
        key == "physical_region_model" || key == "part")
      header.emplace(key, stream.Value());
    else if (key == "physical_regions")
      stream.Object([&](const auto &field) {
        if (field != "io_banks") {
          stream.Skip();
          return;
        }
        stream.Array([&](const auto &token) {
          Check(++banks <= 256, "package bank catalog exceeds device bounds");
          BoundedJson bank(token);
          VisitJsonArrayTokens(bank.Token("pins"), [&](const auto &value) {
            BoundedJson pin(value), tile(pin.Token("native_tile"));
            const auto name = Text(pin.Text("package_pin"));
            const auto site = Text(pin.Text("site"));
            Check(tile.Text("coordinate_system") ==
                      "native-device-tile-column-row-v1",
                  "package pin lacks native tile geometry");
            const auto x = tile.Integer("column"), y = tile.Integer("row");
            Check(x >= 0 && y >= 0 &&
                      result.pins.emplace(name, PackagePinGeometry{site, x, y})
                          .second,
                  "package pin geometry is negative or duplicated");
            Check(result.pins.size() <= 16384,
                  "package pin catalog exceeds device bounds");
          });
        });
      });
    else
      stream.Skip();
  });
  stream.Finish();
  Check(header.size() == 4 &&
            DecodeJsonString(header.at("schema")) == "emuflow.archdb/v1" &&
            DecodeJsonString(header.at("part")) == part && !part.empty() &&
            banks && !result.pins.empty(),
        "package schema, part or geometry disagrees");
  BoundedJson transform(header.at("coordinate_transform")),
      model(header.at("physical_region_model"));
  Check(transform.Text("name") == "tile-grid-with-site-stride-v1" &&
            model.Boolean("io_bank_encoded"),
        "package coordinate model disagrees");
  BoundedJson overlay(model.Token("overlay"));
  const auto seal = overlay.Text("sha256");
  Check(overlay.Text("schema") == "emuflow.physical-region-sidecar/v1" &&
            seal.size() == 64 &&
            seal.find_first_not_of("0123456789abcdef") == std::string::npos,
        "package region source seal is invalid");
  architecture.RequireUnchanged();
  result.sha256 = FileHash(architecture, cancelled);
  architecture.RequireUnchanged();
  return result;
}
struct PhysicalDevice::Impl {
  SqliteDatabase &db;
  ArtifactRef architecture;
  std::optional<ArtifactRef> native, provider;
  const volatile std::sig_atomic_t *flag;
  std::string part, sha;
  Impl(SqliteDatabase &db, const ArtifactRef &architecture,
       const ArtifactRef *native, const ArtifactRef *provider,
       const volatile std::sig_atomic_t *flag)
      : db(db), architecture(architecture), flag(flag) {
    Check(bool(native) == bool(provider), "native/provider pair is incomplete");
    if (native)
      this->native = *native;
    if (provider)
      this->provider = *provider;
    sha = FileHash(architecture, flag);
    db.Execute(
        "PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-16384; CREATE TEMP "
        "TABLE pd_site_tiles(name TEXT PRIMARY KEY,x INTEGER,y "
        "INTEGER,site_index INTEGER,has_tile INTEGER) WITHOUT ROWID; CREATE "
        "TEMP "
        "TABLE pd_templates(name TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT "
        "ROWID; CREATE TEMP TABLE pd_sites(name TEXT PRIMARY KEY,type TEXT NOT "
        "NULL,base TEXT NOT NULL,x INTEGER NOT NULL,y INTEGER NOT NULL,slr "
        "TEXT,clock_region TEXT,region TEXT) WITHOUT ROWID; CREATE INDEX "
        "temp.pd_site_bucket ON pd_sites(slr,clock_region,type); CREATE TEMP "
        "TABLE pd_bels(base TEXT,bel TEXT,mode TEXT,cells TEXT,PRIMARY "
        "KEY(base,bel)) WITHOUT ROWID; CREATE TEMP TABLE pd_adjacency(kind "
        "TEXT,source TEXT,target TEXT,PRIMARY KEY(kind,source,target)) WITHOUT "
        "ROWID; CREATE TEMP TABLE pd_bram(anchor TEXT,role TEXT,bel TEXT,site "
        "TEXT,site_type TEXT,native_type TEXT,native_bel TEXT,site_index "
        "INTEGER,tile TEXT,PRIMARY KEY(anchor,role),UNIQUE(site)) WITHOUT "
        "ROWID; CREATE TEMP TABLE pd_native_fields(key TEXT PRIMARY KEY,value "
        "TEXT) WITHOUT ROWID; CREATE TEMP TABLE pd_native_records(field "
        "TEXT,ordinal INTEGER,value TEXT,PRIMARY KEY(field,ordinal)) WITHOUT "
        "ROWID;");
    Architecture();
    Templates();
    if (native)
      Native();
    RequireUnchanged();
  }
  void RequireUnchanged() const {
    architecture.RequireUnchanged();
    if (native)
      native->RequireUnchanged();
    if (provider)
      provider->RequireUnchanged();
  }
  void AddBel(const std::string &base, const std::string &mode,
              const std::string &raw) {
    BoundedJson b(raw);
    const auto name = Text(b.Text("name"));
    const auto cells = b.Token("compatible_cells");
    const auto actual_mode =
        b.Has("placement_mode") ? Text(b.Text("placement_mode")) : mode;
    Check(!Strings(cells).empty(), "BEL lacks compatible cells");
    auto find =
        db.Prepare("SELECT mode,cells FROM pd_bels WHERE base=? AND bel=?");
    find.BindText(1, base);
    find.BindText(2, name);
    if (find.Step()) {
      Check(find.TypedText(0) == actual_mode &&
                Strings(find.TypedText(1)) == Strings(cells),
            "ambiguous BEL template");
      return;
    }
    auto row = db.Prepare("INSERT INTO pd_bels VALUES(?,?,?,?)");
    row.BindText(1, base);
    row.BindText(2, name);
    row.BindText(3, actual_mode);
    row.BindText(4, cells);
    row.Execute();
  }
  void Architecture() {
    std::ifstream input(architecture.path());
    Stream stream(input, db, flag);
    std::set<std::string> seen;
    stream.Object([&](const auto &key) {
      seen.insert(key);
      if (key == "sites")
        stream.Array([&](const auto &raw) {
          Stop(flag);
          BoundedJson s(raw);
          const auto name = Text(s.Text("name")), type = Text(s.Text("type"));
          const auto x = s.Integer("x"), y = s.Integer("y");
          Check(x >= 0 && y >= 0, "negative site geometry");
          const auto base =
              s.Has("template") ? Text(s.Text("template")) : "@site:" + name;
          std::string slr, clock, region = "null";
          if (s.Has("physical_region") &&
              s.Token("physical_region") != "null") {
            region = s.Token("physical_region");
            BoundedJson r(region);
            if (r.Has("slr") && r.Token("slr") != "null")
              slr = r.Text("slr");
            if (r.Has("clock_region") && r.Token("clock_region") != "null")
              clock = r.Text("clock_region");
          }
          auto row = db.Prepare("INSERT INTO pd_sites VALUES(?,?,?,?,?,?,?,?)");
          row.BindText(1, name);
          row.BindText(2, type);
          row.BindText(3, base);
          row.BindInteger(4, x);
          row.BindInteger(5, y);
          row.BindText(6, slr.empty() ? "@unspecified-slr" : slr);
          row.BindText(7, clock);
          row.BindText(8, region);
          row.Execute();
          auto gx = x, gy = y, site_index = I(0);
          bool has_tile = false;
          if (s.Has("tile") && s.Token("tile") != "null") {
            BoundedJson tile(s.Token("tile"));
            if (tile.Has("grid_col") && tile.Has("grid_row")) {
              gx = tile.Integer("grid_col");
              gy = tile.Integer("grid_row");
              Check(gx >= 0 && gy >= 0, "negative tile geometry");
              has_tile = true;
            }
            if (tile.Has("site_index"))
              site_index = tile.Integer("site_index");
          }
          auto geometry =
              db.Prepare("INSERT INTO pd_site_tiles VALUES(?,?,?,?,?)");
          geometry.BindText(1, name);
          geometry.BindInteger(2, gx);
          geometry.BindInteger(3, gy);
          geometry.BindInteger(4, site_index);
          geometry.BindInteger(5, has_tile ? 1 : 0);
          geometry.Execute();
          if (!s.Has("template")) {
            Check(s.Has("bels"), "site lacks BEL contract");
            for (const auto &b : BoundedJson::Array(s.Token("bels"), 65536))
              AddBel(base, type, b);
          }
        });
      else if (key == "site_templates")
        stream.Object([&](const auto &name) {
          auto row = db.Prepare("INSERT INTO pd_templates VALUES(?,?)");
          row.BindText(1, Text(name));
          row.BindText(2, stream.Value());
          row.Execute();
        });
      else if (key == "part")
        part = Text(DecodeJsonString(stream.Value()));
      else if (key == "schema") {
        Check(DecodeJsonString(stream.Value()) == "emuflow.archdb/v1",
              "architecture schema disagrees");
      } else
        stream.Skip();
    });
    stream.Finish();
    Check(seen.count("schema") && seen.count("sites") && !part.empty(),
          "architecture header is incomplete");
  }
  void Templates() {
    auto rows = db.Prepare("SELECT name,value FROM pd_templates ORDER BY name");
    while (rows.Step()) {
      Stop(flag);
      const auto base = rows.TypedText(0);
      BoundedJson root(rows.TypedText(1));
      std::set<std::string> modes{base};
      if (root.Has("alternative_templates")) {
        const auto alternatives = Strings(root.Token("alternative_templates"));
        modes.insert(alternatives.begin(), alternatives.end());
      }
      for (const auto &mode : modes) {
        auto row = db.Prepare("SELECT value FROM pd_templates WHERE name=?");
        row.BindText(1, mode);
        Check(row.Step(), "unknown alternative template");
        BoundedJson current(row.TypedText(0));
        for (const auto &bel : BoundedJson::Array(current.Token("bels"), 65536))
          AddBel(base, mode, bel);
      }
    }
    Check(!Exists("SELECT 1 FROM pd_sites s LEFT JOIN pd_templates t ON "
                  "t.name=s.base WHERE substr(s.base,1,6)!='@site:' AND t.name "
                  "IS NULL LIMIT 1"),
          "site references unknown template");
  }
  bool Exists(const std::string &sql) {
    auto row = db.Prepare(sql);
    return row.Step();
  }
  std::string NativeArray(const std::string &field, Stream &stream) {
    I ordinal = 0;
    stream.Array([&](const std::string &raw) {
      DeviceObject record(raw);
      const auto canonical = Canonical(db, raw);
      auto row = db.Prepare("INSERT INTO pd_native_records VALUES(?,?,?)");
      row.BindText(1, field);
      row.BindInteger(2, ordinal++);
      row.BindText(3, canonical);
      row.Execute();
    });
    return "@array";
  }
  void Native() {
    std::ifstream input(native->path());
    Stream stream(input, db, flag);
    std::string seal;
    bool payload = false, schema = false;
    stream.Object([&](const auto &key) {
      if (key == "schema") {
        Check(DecodeJsonString(stream.Value()) ==
                  "emuflow.xilinx-native-device-constraints/v3",
              "native constraints schema disagrees");
        schema = true;
      } else if (key == "payload_sha256")
        seal = DecodeJsonString(stream.Value());
      else if (key == "payload") {
        payload = true;
        stream.Object([&](const auto &field) {
          std::string value;
          if (field == "site_capacity" || field == "bram_tile_groups" ||
              field == "dedicated_adjacency")
            value = NativeArray(field, stream);
          else {
            Check(field == "source" || field == "capabilities" ||
                      field == "summary",
                  "unknown native payload fact");
            value = Canonical(db, stream.Value());
          }
          auto row = db.Prepare("INSERT INTO pd_native_fields VALUES(?,?)");
          row.BindText(1, field);
          row.BindText(2, value);
          row.Execute();
        });
      } else {
        Check(false, "unknown native envelope field");
      }
    });
    stream.Finish();
    Check(schema && payload, "incomplete native constraints");
    Sha256 hash;
    Hash(hash, "{");
    auto fields =
        db.Prepare("SELECT key,value FROM pd_native_fields ORDER BY key");
    bool first = true;
    while (fields.Step()) {
      if (!first)
        Hash(hash, ",");
      first = false;
      Hash(hash, JsonQuote(fields.TypedText(0)) + ":");
      if (fields.TypedText(1) == "@array") {
        Hash(hash, "[");
        auto rows = db.Prepare("SELECT value FROM pd_native_records WHERE "
                               "field=? ORDER BY ordinal");
        rows.BindText(1, fields.TypedText(0));
        bool initial = true;
        while (rows.Step()) {
          Stop(flag);
          if (!initial)
            Hash(hash, ",");
          initial = false;
          Hash(hash, rows.TypedText(0));
        }
        Hash(hash, "]");
      } else
        Hash(hash, fields.TypedText(1));
    }
    Hash(hash, "}");
    Check(Digest(hash) == seal, "native payload seal disagrees");
    NativeSource();
    NativeFacts();
  }
  std::string Field(const std::string &key) {
    auto row = db.Prepare("SELECT value FROM pd_native_fields WHERE key=?");
    row.BindText(1, key);
    Check(row.Step(), "native payload field absent");
    return row.TypedText(0);
  }
  void NativeSource() {
    BoundedJson source(Field("source")), manifest(BoundedJson::Read(*provider));
    Check(manifest.Text("schema") == "emuflow.rapidwright-device-provider/v1" &&
              manifest.Text("provider_id") == "rapidwright-xilinx-device-v1",
          "provider identity disagrees");
    BoundedJson device(manifest.Token("device")),
        release(manifest.Token("release")),
        database(manifest.Token("rapidwright_device_database")),
        interchange(manifest.Token("fpga_interchange")),
        license(manifest.Token("license")),
        generator(source.Token("generator"));
    const auto lower = [](std::string value) {
      for (char &c : value)
        c = char(std::tolower(static_cast<unsigned char>(c)));
      return value;
    };
    const auto hexadecimal = [&](const std::string &value, unsigned length) {
      const auto text = lower(value);
      Check(text.size() == length &&
                text.find_first_not_of("0123456789abcdef") == std::string::npos,
            "invalid provider digest");
      return text;
    };
    const auto revision = hexadecimal(release.Text("revision"), 40),
               md5 = hexadecimal(database.Text("md5"), 32);
    Check(release.Text("tag") == "v" + release.Text("version") + "-beta" &&
              interchange.Text("schema_license") == "Apache-2.0",
          "provider release/interchange identity disagrees");
    (void)hexadecimal(interchange.Text("schema_revision"), 40);
    Text(interchange.Text("generator_class"));
    Text(database.Text("resource"));
    Check(database.Text("redistribution") ==
              "external-dependency-not-redistributed",
          "provider database redistribution disagrees");
    for (const auto &field : std::map<std::string, std::string>{
             {"source_code", "Apache-2.0"},
             {"device_data", "Xilinx-EULA"},
             {"runtime", "mixed-license-external"},
             {"redistribution", "external-dependency-not-redistributed"}})
      Check(license.Text(field.first) == field.second,
            "provider license identity disagrees");
    Check(!license.Boolean("generated_device_data_committable"),
          "provider generated-data qualification disagrees");
    BoundedJson resources(manifest.Token("expected_physical_resources")),
        evidence(manifest.Token("resource_evidence"));
    Check(!resources.Fields().empty(), "provider resource inventory absent");
    for (const auto &resource : resources.Fields())
      Check(resources.Integer(resource.first) > 0,
            "provider resource amount invalid");
    for (const auto *key : {"document", "revision", "url", "table"})
      Text(evidence.Text(key));
    source.Keys({"architecture_sha256", "device", "device_database_md5",
                 "full_part", "generator", "provider_manifest_sha256",
                 "route_backend"});
    generator.Keys({"revision", "version"});
    Check(source.Text("architecture_sha256") == sha &&
              source.Text("provider_manifest_sha256") ==
                  FileHash(*provider, flag) &&
              source.Text("device") == lower(device.Text("device")) &&
              source.Text("device_database_md5") == md5 &&
              source.Text("full_part") == lower(device.Text("part")) &&
              lower(source.Text("full_part")) == lower(part) &&
              generator.Text("revision") == revision &&
              generator.Text("version") == release.Text("version"),
          "native ArchitectureDB/provider source seal disagrees");
    Check(source.Text("route_backend") ==
              "rapidwright-native-device-database-v1",
          "native route backend disagrees");
    BoundedJson capabilities(Field("capabilities"));
    capabilities.Keys(
        {"bram_tile_groups", "clock_region_site_capacity", "slr_site_capacity",
         "half_column_clock_capacity", "dedicated_adjacency.BRAM_CASCADE",
         "dedicated_adjacency.CARRY_NEXT", "dedicated_adjacency.DSP_CASCADE",
         "dedicated_adjacency.URAM_CASCADE"});
    for (const auto *key : {"bram_tile_groups", "clock_region_site_capacity",
                            "slr_site_capacity"})
      Check(capabilities.Text(key) == "native_supported",
            "required native capability unsupported");
    Check(capabilities.Text("half_column_clock_capacity") == "unverified",
          "half-column capability claim is unsupported");
  }

  void NativeFacts();
};
void PhysicalDevice::Impl::NativeFacts() {
  db.Execute("CREATE TEMP TABLE pd_native_capacity(slr TEXT,clock_region "
             "TEXT,type TEXT,sites INTEGER,PRIMARY KEY(slr,clock_region,type)) "
             "WITHOUT ROWID;CREATE TEMP TABLE pd_native_chain_site(kind "
             "TEXT,site TEXT,PRIMARY KEY(kind,site)) WITHOUT ROWID;CREATE TEMP "
             "TABLE pd_bram_groups(anchor TEXT PRIMARY KEY,tile TEXT UNIQUE) "
             "WITHOUT ROWID;");
  auto records = db.Prepare(
      "SELECT field,value FROM pd_native_records ORDER BY field,ordinal");
  std::set<std::string> families;
  I edge_total = 0;
  const std::map<std::string, std::pair<std::string, std::string>> contracts{
      {"BRAM_CASCADE",
       {"ramb36e2-72-data-parity-2-ecc-cascade-sitepins-v2", "RAMB36E2"}},
      {"CARRY_NEXT", {"carry8-co7-ci-all-v1", "CARRY8"}},
      {"DSP_CASCADE", {"dsp48e2-all-cascade-sitepins-v1", "DSP_ALU"}},
      {"URAM_CASCADE", {"uram288-all-cascade-sitepins-v1", "URAM_288K_INST"}}};
  BoundedJson capabilities(Field("capabilities"));
  while (records.Step()) {
    Stop(flag);
    const auto field = records.TypedText(0);
    DeviceObject row(records.TypedText(1));
    if (field == "site_capacity") {
      row.Keys({"slr", "clock_region", "site_type", "sites"});
      const auto count = row.Integer("sites");
      Check(count > 0, "zero native capacity");
      auto insert =
          db.Prepare("INSERT INTO pd_native_capacity VALUES(?,?,?,?)");
      insert.BindText(1, Text(row.Text("slr")));
      insert.BindText(2, Text(row.Text("clock_region")));
      insert.BindText(3, Text(row.Text("site_type")));
      insert.BindInteger(4, count);
      insert.Execute();
    } else if (field == "bram_tile_groups") {
      row.Keys({"anchor", "lower", "upper", "whole", "tile"});
      const auto anchor = Text(row.Text("anchor")),
                 tile = Text(row.Text("tile"));
      auto group = db.Prepare("INSERT INTO pd_bram_groups VALUES(?,?)");
      group.BindText(1, anchor);
      group.BindText(2, tile);
      group.Execute();
      auto known = db.Prepare(
          "SELECT s.base FROM pd_sites s JOIN pd_bels b ON b.base=s.base WHERE "
          "s.name=? AND b.bel IN ('RAMB18E2_L','RAMB18E2_U','RAMB36E2') GROUP "
          "BY s.base HAVING count(*)=3");
      known.BindText(1, anchor);
      Check(known.Step(), "unknown BRAM tile anchor");
      std::set<I> indices;
      for (const auto *role : {"lower", "upper", "whole"}) {
        BoundedJson view(row.Token(role));
        view.Keys({"bel", "native_bel", "native_site_type", "site",
                   "site_index", "site_type"});
        const std::string r = role,
                          bel = r == "lower"   ? "RAMB18E2_L"
                                : r == "upper" ? "RAMB18E2_U"
                                               : "RAMB36E2",
                          type = r == "lower"   ? "RAMB180"
                                 : r == "upper" ? "RAMB181"
                                                : "RAMB36",
                          ntype = r == "lower"   ? "RAMBFIFO18"
                                  : r == "upper" ? "RAMB181"
                                                 : "RAMBFIFO36",
                          nbel = r == "lower"   ? "RAMBFIFO18"
                                 : r == "upper" ? "RAMB18E2_U"
                                                : "RAMBFIFO36E2";
        Check(r != "upper" || view.Text("site") == anchor,
              "BRAM ArchitectureDB anchor is not its native upper view");
        const auto index = view.Integer("site_index");
        Check(index >= 0 && indices.insert(index).second &&
                  view.Text("bel") == bel && view.Text("site_type") == type &&
                  view.Text("native_site_type") == ntype &&
                  view.Text("native_bel") == nbel,
              "invalid native BRAM view");
        auto insert =
            db.Prepare("INSERT INTO pd_bram VALUES(?,?,?,?,?,?,?,?,?)");
        insert.BindText(1, anchor);
        insert.BindText(2, r);
        insert.BindText(3, bel);
        insert.BindText(4, Text(view.Text("site")));
        insert.BindText(5, type);
        insert.BindText(6, ntype);
        insert.BindText(7, nbel);
        insert.BindInteger(8, index);
        insert.BindText(9, tile);
        insert.Execute();
      }
    } else if (field == "dedicated_adjacency") {
      row.Keys({"chains", "edge_count", "endpoint_contract", "kind",
                "native_proof_sha256", "proof_method"});
      const auto kind = row.Text("kind");
      const auto contract = contracts.find(kind);
      Check(contract != contracts.end() && families.insert(kind).second,
            "duplicate or unknown adjacency family");
      const auto proof = row.Text("native_proof_sha256");
      Check(proof.size() == 64 &&
                proof.find_first_not_of("0123456789abcdef") ==
                    std::string::npos &&
                row.Text("endpoint_contract") == contract->second.first &&
                row.Text("proof_method") ==
                    "rapidwright-dedicated-sitepin-vector-directed-path-v2" &&
                capabilities.Text("dedicated_adjacency." + kind) ==
                    "native_supported",
            "unqualified dedicated adjacency");
      I edges = 0;
      const auto chains = BoundedJson::Array(row.Token("chains"), 65536);
      Check(!chains.empty(), "empty native family");
      for (const auto &chain : chains) {
        const auto tokens = BoundedJson::Array(chain, 65536);
        Check(tokens.size() >= 2, "native chain is too short");
        std::string previous, slr, clock;
        for (const auto &token : tokens) {
          const auto site = Text(DecodeJsonString(token));
          auto fact = db.Prepare(
              "SELECT s.slr,s.clock_region FROM pd_sites s JOIN pd_bels b ON "
              "b.base=s.base WHERE s.name=? AND b.bel=?");
          fact.BindText(1, site);
          fact.BindText(2, contract->second.second);
          Check(fact.Step(), "native adjacency references absent site/BEL");
          const auto current_slr = fact.TypedText(0),
                     current_clock = fact.TypedText(1);
          Check(current_slr != "@unspecified-slr" && !current_clock.empty() &&
                    (slr.empty() || slr == current_slr) &&
                    (kind != "BRAM_CASCADE" || clock.empty() ||
                     clock == current_clock),
                "native chain crosses SLR/BRAM clock region");
          slr = current_slr;
          clock = current_clock;
          auto occupied =
              db.Prepare("INSERT INTO pd_native_chain_site VALUES(?,?)");
          occupied.BindText(1, kind);
          occupied.BindText(2, site);
          occupied.Execute();
          if (!previous.empty()) {
            auto edge = db.Prepare("INSERT INTO pd_adjacency VALUES(?,?,?)");
            edge.BindText(1, kind);
            edge.BindText(2, previous);
            edge.BindText(3, site);
            edge.Execute();
            ++edges;
          }
          previous = site;
        }
      }
      Check(edges == row.Integer("edge_count"), "native edge count disagrees");
      edge_total += edges;
    }
  }
  Check(!Exists(
            "SELECT slr,clock_region,type,count(*) FROM pd_sites GROUP BY "
            "slr,clock_region,type EXCEPT SELECT * FROM pd_native_capacity") &&
            !Exists("SELECT * FROM pd_native_capacity EXCEPT SELECT "
                    "slr,clock_region,type,count(*) FROM pd_sites GROUP BY "
                    "slr,clock_region,type"),
        "native capacity differs from whole architecture");
  Check(
      !Exists(
          "SELECT s.name FROM pd_sites s JOIN pd_bels b ON b.base=s.base WHERE "
          "b.bel IN ('RAMB18E2_L','RAMB18E2_U','RAMB36E2') GROUP BY s.name "
          "HAVING count(*)=3 EXCEPT SELECT anchor FROM pd_bram_groups") &&
          !Exists("SELECT anchor FROM pd_bram_groups EXCEPT SELECT s.name FROM "
                  "pd_sites s JOIN pd_bels b ON b.base=s.base WHERE b.bel IN "
                  "('RAMB18E2_L','RAMB18E2_U','RAMB36E2') GROUP BY s.name "
                  "HAVING count(*)=3"),
      "native BRAM anchor coverage differs");
  for (const auto &item : contracts) {
    const auto status = capabilities.Text("dedicated_adjacency." + item.first);
    Check((status == "native_supported" && families.count(item.first)) ||
              (status == "core_missing" && !families.count(item.first)),
          "native capability/family coverage differs");
  }
  BoundedJson summary(Field("summary"));
  summary.Keys({"bram_tile_groups", "capacity_buckets", "clock_regions",
                "dedicated_edges", "dedicated_edges_by_kind", "sites", "slrs"});
  for (const auto &item : std::map<std::string, std::string>{
           {"bram_tile_groups", "SELECT count(*) FROM pd_bram_groups"},
           {"capacity_buckets", "SELECT count(*) FROM pd_native_capacity"},
           {"clock_regions",
            "SELECT count(DISTINCT clock_region) FROM pd_sites"},
           {"sites", "SELECT count(*) FROM pd_sites"},
           {"slrs", "SELECT count(DISTINCT slr) FROM pd_sites"}}) {
    auto count = db.Prepare(item.second);
    Check(count.Step() && count.TypedInteger(0) == summary.Integer(item.first),
          "native device summary disagrees");
  }
  Check(summary.Integer("dedicated_edges") == edge_total,
        "native edge total disagrees");
  BoundedJson by_kind(summary.Token("dedicated_edges_by_kind"));
  for (const auto &item : contracts) {
    auto count = db.Prepare("SELECT count(*) FROM pd_adjacency WHERE kind=?");
    count.BindText(1, item.first);
    Check(count.Step() && count.TypedInteger(0) == by_kind.Integer(item.first),
          "native edge family summary disagrees");
  }
}
PhysicalDevice::PhysicalDevice(SqliteDatabase &scratch,
                               const ArtifactRef &architecture,
                               const ArtifactRef *native,
                               const ArtifactRef *provider,
                               const volatile std::sig_atomic_t *cancelled)
    : impl_(std::make_unique<Impl>(scratch, architecture, native, provider,
                                   cancelled)) {}
PhysicalDevice::~PhysicalDevice() = default;
PhysicalSite PhysicalDevice::Site(const std::string &name) {
  auto row =
      impl_->db.Prepare("SELECT name,type,base,slr,clock_region,x,y,region "
                        "FROM pd_sites WHERE name=?");
  row.BindText(1, name);
  Check(row.Step(), "unknown physical site " + name);
  return {row.TypedText(0),    row.TypedText(1),   row.TypedText(2),
          row.TypedText(3),    row.TypedText(4),   row.TypedText(7),
          row.TypedInteger(5), row.TypedInteger(6)};
}
PhysicalBel PhysicalDevice::ResolveBel(const std::string &site,
                                       const std::string &bel,
                                       const std::string &type,
                                       const std::set<std::string> &modes) {
  const auto fact = Site(site);
  auto row = impl_->db.Prepare(
      "SELECT mode,cells FROM pd_bels WHERE base=? AND bel=?");
  row.BindText(1, fact.base);
  row.BindText(2, bel);
  Check(row.Step() && modes.count(row.TypedText(0)) &&
            Strings(row.TypedText(1)).count(type),
        "incompatible physical BEL/template/type");
  PhysicalBel result{bel, row.TypedText(0), site};
  if (type == "RAMB18E2" || type == "RAMB36E2") {
    auto group =
        impl_->db.Prepare("SELECT site FROM pd_bram WHERE anchor=? AND bel=?");
    group.BindText(1, site);
    group.BindText(2, bel);
    if (group.Step())
      result.physical_site = group.TypedText(0);
    else if (impl_->native) {
      auto present =
          impl_->db.Prepare("SELECT 1 FROM pd_bram_groups WHERE anchor=?");
      present.BindText(1, site);
      Check(!present.Step(), "BRAM group has no matching role");
    }
  }
  return result;
}
SqliteDatabase &PhysicalDevice::database() { return impl_->db; }
const std::string &PhysicalDevice::part() const { return impl_->part; }
const std::string &PhysicalDevice::architecture_sha() const {
  return impl_->sha;
}
std::string PhysicalDevice::native_sha() const {
  return impl_->native ? FileHash(*impl_->native, impl_->flag) : "";
}
std::string PhysicalDevice::provider_sha() const {
  return impl_->provider ? FileHash(*impl_->provider, impl_->flag) : "";
}
void PhysicalDevice::RequireUnchanged() const { impl_->RequireUnchanged(); }
void ImportPhysicalPlacementConstraints(
    SqliteDatabase &db, const ArtifactRef *source,
    const volatile std::sig_atomic_t *flag) {
  db.Execute(
      "CREATE TEMP TABLE audit_constraints(cluster_id TEXT PRIMARY KEY,site "
      "TEXT,slr TEXT,clock_region TEXT) WITHOUT ROWID;CREATE TEMP TABLE "
      "audit_allowed_slr(slr TEXT PRIMARY KEY) WITHOUT ROWID;");
  if (!source)
    return;
  source->RequireUnchanged();
  std::ifstream input(source->path());
  Stream stream(input, db, flag);
  bool schema = false;
  stream.Object([&](const auto &key) {
    if (key == "schema") {
      Check(DecodeJsonString(stream.Value()) ==
                "emuflow.xilinx-placement-constraints/v1",
            "constraints schema disagrees");
      schema = true;
    } else if (key == "global") {
      const auto raw = stream.Value();
      if (raw == "null")
        return;
      BoundedJson global(raw);
      global.Keys({"allowed_slrs"});
      const auto allowed = Strings(global.Token("allowed_slrs"));
      Check(!allowed.empty(), "empty global SLR constraint");
      for (const auto &slr : allowed) {
        auto row = db.Prepare("INSERT INTO audit_allowed_slr VALUES(?)");
        row.BindText(1, slr);
        row.Execute();
      }
    } else if (key == "clusters")
      stream.Array([&](const auto &raw) {
        BoundedJson entry(raw);
        auto row = db.Prepare("INSERT INTO audit_constraints VALUES(?,?,?,?)");
        row.BindText(1, Text(entry.Text("cluster")));
        bool any = false;
        unsigned index = 2;
        for (const auto *field : {"site", "slr", "clock_region"}) {
          if (entry.Has(field) && entry.Token(field) != "null") {
            row.BindText(index, Text(entry.Text(field)));
            any = true;
          } else
            row.BindNull(index);
          ++index;
        }
        Check(any, "empty cluster constraint");
        row.Execute();
      });
    else
      stream.Skip();
  });
  stream.Finish();
  Check(schema, "constraints schema absent");
  source->RequireUnchanged();
}
struct MappedPhysicalSemantics::Impl {
  ArtifactRef source;
  SqliteStore owner;
  SqliteDatabase &db;
  std::string cached, type, parameters;
  explicit Impl(const ArtifactRef &mapped)
      : source(mapped), owner(mapped), db(owner.database()) {
    Check(mapped.schema() == "xilinx-mapped-index-v2",
          "requires current mapped owner");
    db.Execute("PRAGMA temp_store=FILE;PRAGMA temp.cache_size=-8192");
  }
  void Find(const std::string &instance) {
    if (cached == instance)
      return;
    auto row = db.Prepare("SELECT type,CASE WHEN length(record_json)<=8388608 "
                          "THEN record_json END FROM cells WHERE id=?");
    row.BindText(1, Text(instance));
    Check(row.Step() && !row.IsNull(1), "mapped cell absent or exceeds bound");
    type = Text(row.TypedText(0));
    BoundedJson record(row.TypedText(1));
    Check(record.Text("type") == type && !row.Step(),
          "mapped cell projection/identity disagrees");
    parameters = record.Has("parameters")
                     ? Canonical(db, record.Token("parameters"))
                     : "{}";
    BoundedJson check(parameters);
    cached = instance;
  }
  std::vector<PhysicalBit> Bits(const std::string &instance,
                                const std::string &port) {
    Find(instance);
    auto rows = db.Prepare(
        "SELECT port_bit,port_width,value_kind,net_bit,constant FROM cell_pins "
        "WHERE cell_id=? AND port=? ORDER BY port_bit");
    rows.BindText(1, instance);
    rows.BindText(2, port);
    std::vector<PhysicalBit> out;
    I width = -1;
    while (rows.Step()) {
      Check(out.size() < 65536 && rows.TypedInteger(0) == I(out.size()),
            "mapped port ordinal invalid");
      if (width < 0)
        width = rows.TypedInteger(1);
      Check(width > 0 && width == rows.TypedInteger(1),
            "mapped port width inconsistent");
      if (rows.TypedInteger(2) == 0) {
        Check(rows.IsNull(4), "mixed net/constant bit");
        out.emplace_back(rows.TypedInteger(3));
      } else {
        Check(rows.TypedInteger(2) == 1 && rows.IsNull(3),
              "invalid constant bit");
        out.emplace_back(rows.TypedText(4));
      }
    }
    Check(width < 0 || I(out.size()) == width, "mapped port incomplete");
    return out;
  }
  std::optional<std::string> Driver(I bit) {
    auto rows =
        db.Prepare("SELECT cell_id FROM cell_pins WHERE value_kind=0 AND "
                   "net_bit=? AND direction=1 ORDER BY cell_id,port,port_bit");
    rows.BindInteger(1, bit);
    if (!rows.Step())
      return {};
    auto value = rows.TypedText(0);
    Check(!rows.Step(), "mapped integer net has multiple output drivers");
    return value;
  }
  std::string IsParameters(const std::string &instance) {
    Find(instance);
    BoundedJson p(parameters);
    Raw kept;
    for (const auto &f : p.Fields())
      if (f.first.compare(0, 3, "IS_") == 0)
        kept.emplace(f);
    return Canonical(db, BoundedJson::Encode(kept));
  }
  std::vector<std::string>
  Controls(const std::vector<PhysicalAssignment> &assignments) {
    Check(assignments.size() <= 64, "site population exceeds bound");
    std::map<unsigned, std::string> half;
    std::map<std::pair<unsigned, unsigned>, std::string> ce;
    std::set<std::string> sets;
    for (const auto &a : assignments) {
      if (!Ff(a.type))
        continue;
      Find(a.instance);
      Check(type == a.type, "FF source type disagrees");
      Check(a.bel.size() >= 3 && a.bel[0] >= 'A' && a.bel[0] <= 'H' &&
                (a.bel.substr(1) == "FF" || a.bel.substr(1) == "FF2"),
            "invalid concrete FF BEL");
      const unsigned letter = a.bel[0] - 'A', h = letter < 4 ? 0 : 1,
                     lane = a.bel.size() == 4 ? 1 : 0;
      const std::string control = a.type == "FDCE"   ? "CLR"
                                  : a.type == "FDPE" ? "PRE"
                                  : a.type == "FDRE" ? "R"
                                                     : "S";
      const auto clock = EncodeBits(Bits(a.instance, "C")),
                 enable = EncodeBits(Bits(a.instance, "CE")),
                 sr = EncodeBits(Bits(a.instance, control)),
                 parameters = IsParameters(a.instance);
      const auto cksr =
          BoundedJson::Encode({{"clock", clock},
                               {"type", JsonQuote(a.type)},
                               {"control_port", JsonQuote(control)},
                               {"control", sr},
                               {"parameters", parameters}});
      auto previous = half.emplace(h, cksr);
      auto previous_ce = ce.emplace(std::make_pair(h, lane), enable);
      Check(previous.first->second == cksr &&
                previous_ce.first->second == enable,
            "UltraScale+ half-slice FF control legality violated");
      sets.insert(BoundedJson::Encode({{"type", JsonQuote(a.type)},
                                       {"clock", clock},
                                       {"enable", enable},
                                       {"control", sr},
                                       {"parameters", parameters}}));
    }
    return {sets.begin(), sets.end()};
  }
  std::vector<SitePinConflict>
  Conflicts(const std::vector<PhysicalAssignment> &assignments) {
    Check(assignments.size() <= 64, "site population exceeds bound");
    const std::map<std::string, std::string> selects{
        {"F7MUX_AB", "AX"}, {"F7MUX_CD", "CX"},  {"F7MUX_EF", "EX"},
        {"F7MUX_GH", "GX"}, {"F8MUX_BOT", "BX"}, {"F8MUX_TOP", "FX"},
        {"F9MUX", "DX"}};
    bool has_mux = false;
    std::set<std::string> local;
    for (const auto &a : assignments) {
      local.insert(a.instance);
      has_mux |= selects.count(a.bel) &&
                 (a.type == "MUXF7" || a.type == "MUXF8" || a.type == "MUXF9");
    }
    if (!has_mux)
      return {};
    std::map<std::string, std::vector<SitePinUse>> mux, ff;
    for (const auto &a : assignments) {
      const bool is_mux =
          (a.type == "MUXF7" || a.type == "MUXF8" || a.type == "MUXF9") &&
          selects.count(a.bel);
      const bool primary =
          Ff(a.type) && a.bel.size() == 3 && a.bel.substr(1) == "FF";
      if (!is_mux && !primary)
        continue;
      const auto bits = Bits(a.instance, is_mux ? "S" : "D");
      if (bits.size() != 1 || !std::holds_alternative<I>(bits[0]))
        continue;
      const auto bit = std::get<I>(bits[0]);
      const auto driver = Driver(bit);
      if (driver && local.count(*driver))
        continue;
      const auto pin =
          is_mux ? selects.at(a.bel) : std::string(1, a.bel[0]) + "X";
      (is_mux ? mux : ff)[pin].push_back({bit, a.instance, a.bel});
    }
    std::vector<SitePinConflict> conflicts;
    for (const auto &f : mux) {
      const auto opposite = ff.find(f.first);
      if (opposite == ff.end())
        continue;
      auto uses = f.second;
      uses.insert(uses.end(), opposite->second.begin(), opposite->second.end());
      std::set<I> nets;
      for (const auto &use : uses)
        nets.insert(use.bit);
      if (nets.size() > 1)
        conflicts.push_back({f.first, uses});
    }
    return conflicts;
  }
};
MappedPhysicalSemantics::MappedPhysicalSemantics(const ArtifactRef &mapped)
    : impl_(std::make_unique<Impl>(mapped)) {}
MappedPhysicalSemantics::~MappedPhysicalSemantics() = default;
std::string MappedPhysicalSemantics::CellType(const std::string &name) {
  impl_->Find(name);
  return impl_->type;
}
std::string MappedPhysicalSemantics::Parameters(const std::string &name) {
  impl_->Find(name);
  return impl_->parameters;
}
std::vector<PhysicalBit>
MappedPhysicalSemantics::Bits(const std::string &name,
                              const std::string &port) {
  return impl_->Bits(name, port);
}
std::optional<std::string> MappedPhysicalSemantics::Driver(I bit) {
  return impl_->Driver(bit);
}
std::vector<std::string> MappedPhysicalSemantics::ControlSets(
    const std::vector<PhysicalAssignment> &assignments) {
  return impl_->Controls(assignments);
}
std::vector<SitePinConflict> MappedPhysicalSemantics::SitePinConflicts(
    const std::vector<PhysicalAssignment> &assignments) {
  return impl_->Conflicts(assignments);
}
void MappedPhysicalSemantics::ValidateSlice(
    const std::vector<PhysicalAssignment> &assignments) {
  (void)ControlSets(assignments);
  Check(SitePinConflicts(assignments).empty(),
        "slice external site pin assignment invalid");
}
void MappedPhysicalSemantics::RequireUnchanged() const {
  impl_->source.RequireUnchanged();
}
void MappedPhysicalSemantics::DeriveCascades(
    const std::function<void(const std::string &)> &emit,
    const volatile std::sig_atomic_t *cancelled) {
  auto &db = impl_->db;
  db.Execute(
      "CREATE TEMP TABLE IF NOT EXISTS ps_signals(type TEXT,label "
      "TEXT,instance TEXT,role INTEGER,signal TEXT,PRIMARY "
      "KEY(type,label,instance,role)) WITHOUT ROWID;CREATE UNIQUE INDEX IF NOT "
      "EXISTS temp.ps_signal_output ON ps_signals(type,label,signal) WHERE "
      "role=1;CREATE TEMP TABLE IF NOT EXISTS ps_edges(type TEXT,source "
      "TEXT,target TEXT,label TEXT,PRIMARY KEY(type,source,target,label)) "
      "WITHOUT ROWID;CREATE TEMP TABLE IF NOT EXISTS ps_seen(type "
      "TEXT,instance TEXT,PRIMARY KEY(type,instance)) WITHOUT ROWID;DELETE "
      "FROM ps_signals;DELETE FROM ps_edges;DELETE FROM ps_seen;");
  struct Port {
    std::string label, out, in;
    bool last = false;
  };
  std::vector<std::pair<std::string, std::vector<Port>>> families{
      {"CARRY8", {{"carry", "CO", "CI", true}}},
      {"DSP48E2",
       {{"ac", "ACOUT", "ACIN"},
        {"bc", "BCOUT", "BCIN"},
        {"carry", "CARRYCASCOUT", "CARRYCASCIN"},
        {"multsign", "MULTSIGNOUT", "MULTSIGNIN"},
        {"pc", "PCOUT", "PCIN"}}},
      {"RAMB18E2",
       {{"data-a", "CASDOUTA", "CASDINA"},
        {"data-b", "CASDOUTB", "CASDINB"},
        {"parity-a", "CASDOUTPA", "CASDINPA"},
        {"parity-b", "CASDOUTPB", "CASDINPB"}}},
      {"RAMB36E2",
       {{"data-a", "CASDOUTA", "CASDINA"},
        {"data-b", "CASDOUTB", "CASDINB"},
        {"parity-a", "CASDOUTPA", "CASDINPA"},
        {"parity-b", "CASDOUTPB", "CASDINPB"},
        {"dbiterr", "CASOUTDBITERR", "CASINDBITERR"},
        {"sbiterr", "CASOUTSBITERR", "CASINSBITERR"}}},
      {"URAM288", {}}};
  for (const auto *suffix :
       {"ADDR_A", "ADDR_B", "BWE_A", "BWE_B", "DBITERR_A", "DBITERR_B", "DIN_A",
        "DIN_B", "DOUT_A", "DOUT_B", "EN_A", "EN_B", "RDACCESS_A", "RDACCESS_B",
        "RDB_WR_A", "RDB_WR_B", "SBITERR_A", "SBITERR_B"}) {
    std::string label = suffix;
    for (char &c : label)
      c = char(std::tolower(static_cast<unsigned char>(c)));
    families.back().second.push_back({label, "CAS_OUT_" + std::string(suffix),
                                      "CAS_IN_" + std::string(suffix)});
  }
  I chain_number = 0;
  for (const auto &family : families) {
    auto cells = db.Prepare("SELECT id FROM cells WHERE type=? ORDER BY id");
    cells.BindText(1, family.first);
    while (cells.Step()) {
      Stop(cancelled);
      const auto name = cells.TypedText(0);
      for (const auto &port : family.second)
        for (unsigned role = 0; role < 2; ++role) {
          auto bits = Bits(name, role ? port.out : port.in);
          if (bits.empty())
            continue;
          if (role && port.last)
            bits = {bits.back()};
          if (std::any_of(bits.begin(), bits.end(), [](const auto &b) {
                return !std::holds_alternative<I>(b);
              }))
            continue;
          auto row = db.Prepare("INSERT INTO ps_signals VALUES(?,?,?,?,?)");
          row.BindText(1, family.first);
          row.BindText(2, port.label);
          row.BindText(3, name);
          row.BindInteger(4, role);
          row.BindText(5, EncodeBits(bits));
          row.Execute();
        }
    }
    db.Execute(
        "INSERT INTO ps_edges SELECT a.type,a.instance,b.instance,a.label FROM "
        "ps_signals a JOIN ps_signals b ON b.type=a.type AND b.label=a.label "
        "AND b.signal=a.signal AND b.role=0 WHERE a.role=1 AND "
        "a.instance!=b.instance");
    auto bad = db.Prepare(
        "SELECT 1 FROM ps_edges GROUP BY type,source HAVING count(DISTINCT "
        "target)>1 UNION ALL SELECT 1 FROM ps_edges GROUP BY type,target "
        "HAVING count(DISTINCT source)>1 LIMIT 1");
    Check(!bad.Step(), "dedicated cascade branches or merges");
    auto heads =
        db.Prepare("SELECT DISTINCT a.source FROM ps_edges a WHERE a.type=? "
                   "AND NOT EXISTS(SELECT 1 FROM ps_edges p WHERE "
                   "p.type=a.type AND p.target=a.source) ORDER BY a.source");
    heads.BindText(1, family.first);
    while (heads.Step()) {
      Stop(cancelled);
      std::vector<JsonRecord> instances, links;
      std::string current = heads.TypedText(0);
      for (;;) {
        auto visited = db.Prepare("INSERT INTO ps_seen VALUES(?,?)");
        visited.BindText(1, family.first);
        visited.BindText(2, current);
        visited.Execute();
        instances.emplace_back(current);
        Check(instances.size() <= 65536, "cascade chain exceeds record bound");
        auto next = db.Prepare("SELECT DISTINCT target FROM ps_edges WHERE "
                               "type=? AND source=? ORDER BY target");
        next.BindText(1, family.first);
        next.BindText(2, current);
        if (!next.Step())
          break;
        const auto target = next.TypedText(0);
        Check(!next.Step(), "cascade branches");
        std::vector<JsonRecord> labels;
        auto signals = db.Prepare("SELECT label FROM ps_edges WHERE type=? AND "
                                  "source=? AND target=? ORDER BY label");
        signals.BindText(1, family.first);
        signals.BindText(2, current);
        signals.BindText(3, target);
        while (signals.Step())
          labels.emplace_back(signals.TypedText(0));
        links.emplace_back(
            Obj{{"source", current}, {"target", target}, {"signals", labels}});
        current = target;
      }
      const auto digits = std::to_string(chain_number++);
      emit(JsonRecord(
               Obj{{"id",
                    "cascade-" +
                        std::string(digits.size() < 6 ? 6 - digits.size() : 0,
                                    '0') +
                        digits},
                   {"cell_type", family.first},
                   {"instances", instances},
                   {"links", links},
                   {"placement", "dedicated-adjacent-chain"}})
               .Encode());
    }
    auto cycle = db.Prepare(
        "SELECT 1 FROM ps_edges e LEFT JOIN ps_seen s ON s.type=e.type AND "
        "s.instance=e.source WHERE e.type=? AND s.instance IS NULL LIMIT 1");
    cycle.BindText(1, family.first);
    Check(!cycle.Step(), "dedicated cascade has no acyclic head");
    db.Execute(
        "DELETE FROM ps_edges;DELETE FROM ps_signals;DELETE FROM ps_seen;");
  }
  RequireUnchanged();
}
} // namespace emuflow::core
