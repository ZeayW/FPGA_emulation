#pragma once
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "physical/device.hpp"
#include <array>
#include <cstdlib>
#include <filesystem>
#include <unistd.h>
#include <variant>
namespace emuflow::core::openparf_input {
using I = std::int64_t;
inline void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "OpenPARF input: " + why);
}
inline void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "OpenPARF input cancelled");
}
// Contract records only. Circuit populations are read from the mapped/source
// relations, never from a host-built JSON tree.
struct R {
  using O = std::map<std::string, R>;
  using A = std::vector<R>;
  std::variant<std::nullptr_t, bool, I, double, std::string, O, A> value =
      nullptr;
  R() = default;
  R(std::nullptr_t) {}
  R(bool v) : value(v) {}
  template <class T,
            std::enable_if_t<std::is_integral_v<T> && !std::is_same_v<T, bool>,
                             int> = 0>
  R(T v) : value(I(v)) {}
  R(double v) : value(v) {}
  R(const char *v) : value(std::string(v)) {}
  R(std::string v) : value(std::move(v)) {}
  R(O v) : value(std::move(v)) {}
  R(A v) : value(std::move(v)) {}
  bool null() const { return std::holds_alternative<std::nullptr_t>(value); }
  bool has(const std::string &k) const {
    auto p = std::get_if<O>(&value);
    return p && p->count(k);
  }
  const R &at(const std::string &k) const {
    Check(has(k), "missing field " + k);
    return std::get<O>(value).at(k);
  }
  R &operator[](const std::string &k) {
    if (null())
      value = O{};
    Check(std::holds_alternative<O>(value), "expected object");
    return std::get<O>(value)[k];
  }
  const A &array() const {
    Check(std::holds_alternative<A>(value), "expected array");
    return std::get<A>(value);
  }
  A &array() {
    Check(std::holds_alternative<A>(value), "expected array");
    return std::get<A>(value);
  }
  std::string text() const {
    Check(std::holds_alternative<std::string>(value), "expected string");
    return std::get<std::string>(value);
  }
  I integer() const {
    Check(std::holds_alternative<I>(value), "expected integer");
    return std::get<I>(value);
  }
  std::string encode() const {
    if (null())
      return "null";
    if (auto p = std::get_if<bool>(&value))
      return *p ? "true" : "false";
    if (auto p = std::get_if<I>(&value))
      return std::to_string(*p);
    if (auto p = std::get_if<double>(&value))
      return JsonRecord(*p).EncodeUtf8();
    if (auto p = std::get_if<std::string>(&value))
      return JsonQuoteUtf8(*p);
    if (auto p = std::get_if<O>(&value)) {
      std::string out = "{";
      for (const auto &f : *p) {
        if (out.size() > 1)
          out += ',';
        out += JsonQuoteUtf8(f.first) + ":" + f.second.encode();
      }
      return out + '}';
    }
    std::string out = "[";
    for (const auto &f : std::get<A>(value)) {
      if (out.size() > 1)
        out += ',';
      out += f.encode();
    }
    return out + ']';
  }
  static R Parse(const std::string &raw, unsigned depth = 0) {
    Check(raw.size() <= 32 * 1024 * 1024 && depth < 64 && !raw.empty(),
          "record bound exceeded");
    if (raw[0] == '{') {
      O out;
      VisitJsonObjectTokens(raw, [&](const auto &k, const auto &v) {
        Check(out.emplace(k, Parse(v, depth + 1)).second, "duplicate key");
      });
      return out;
    }
    if (raw[0] == '[') {
      A out;
      VisitJsonArrayTokens(raw, [&](const auto &v) {
        Check(out.size() < 1048576, "array bound exceeded");
        out.push_back(Parse(v, depth + 1));
      });
      return out;
    }
    if (raw[0] == '"')
      return DecodeJsonString(raw);
    if (raw == "null")
      return {};
    if (raw == "true")
      return true;
    if (raw == "false")
      return false;
    if (raw.find_first_of(".eE") != std::string::npos)
      return ParseFiniteDecimal(raw, "OpenPARF number");
    return BoundedJson::IntegerToken(raw);
  }
};
using O = R::O;
using A = R::A;
inline R Read(const ArtifactRef &ref) {
  auto text = BoundedJson::Read(ref);
  BoundedJson valid(text);
  return R::Parse(text);
}
inline std::string Hex(Sha256 &hash) {
  std::string out;
  for (auto c : hash.finish()) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
inline void Feed(Sha256 &hash, const std::string &text) {
  hash.update(reinterpret_cast<const unsigned char *>(text.data()),
              text.size());
}
inline std::string Hash(const std::string &text) {
  Sha256 h;
  Feed(h, text);
  return Hex(h);
}
inline std::string FileHash(const ArtifactRef &ref,
                            const volatile std::sig_atomic_t *flag = nullptr) {
  std::ifstream in(ref.path(), std::ios::binary);
  Check(bool(in), "cannot hash source");
  Sha256 hash;
  std::array<char, 65536> bytes;
  while (in) {
    Stop(flag);
    in.read(bytes.data(), bytes.size());
    hash.update(reinterpret_cast<const unsigned char *>(bytes.data()),
                in.gcount());
  }
  Check(in.eof(), "source read failed");
  ref.RequireUnchanged();
  return Hex(hash);
}
inline void Copy(std::istream &in, std::ostream &out) {
  std::array<char, 65536> bytes;
  while (in) {
    in.read(bytes.data(), bytes.size());
    out.write(bytes.data(), in.gcount());
  }
  Check(in.eof() && bool(out), "artifact copy failed");
}
inline void Write(const std::filesystem::path &path, const std::string &value,
                  const std::vector<ArtifactRef> &refs = {}) {
  PublishTextArtifact(path.string(), refs, [&](auto &out) { out << value; });
}
inline void Json(const std::filesystem::path &path, const R &value,
                 const std::vector<ArtifactRef> &refs = {}) {
  Write(path, value.encode() + "\n", refs);
}
inline std::string Pad(const std::string &prefix, I index) {
  auto d = std::to_string(index);
  return prefix + std::string(d.size() < 6 ? 6 - d.size() : 0, '0') + d;
}
inline bool Lut(const std::string &t) {
  return t.size() == 4 && t.substr(0, 3) == "LUT" && t[3] >= '1' && t[3] <= '6';
}
inline bool Ff(const std::string &t) {
  return t == "FDRE" || t == "FDSE" || t == "FDCE" || t == "FDPE";
}
inline bool Mux(const std::string &t) {
  return t == "MUXF7" || t == "MUXF8" || t == "MUXF9";
}
inline bool Hard(const std::string &t) {
  return t == "DSP48E2" || t == "RAMB18E2" || t == "RAMB36E2" || t == "URAM288";
}
inline std::string Resource(const std::string &t) {
  if (Lut(t) || t == "LUT6_2")
    return "LUT";
  if (Ff(t))
    return "FF";
  Check(Mux(t) || Hard(t) || t == "CARRY8", "does not support primitives: " + t);
  return t;
}
inline I Count(SqliteDatabase &db, const std::string &sql) {
  auto r = db.Prepare(sql);
  Check(r.Step(), "missing aggregate");
  return r.TypedInteger(0);
}
inline std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto r = db.Prepare("SELECT value FROM metadata WHERE key=?");
  r.BindText(1, key);
  Check(r.Step(), "missing metadata " + key);
  return r.TypedText(0);
}
inline void Metadata(SqliteDatabase &db, const std::string &key,
                     const R &value) {
  auto r = db.Prepare("INSERT INTO metadata VALUES (?,?)");
  r.BindText(1, key);
  r.BindText(2, value.encode());
  r.Execute();
}
struct Macro {
  std::string id, kind;
  std::vector<PhysicalAssignment> members;
};
struct SourceInfo {
  R selector;
  std::vector<Macro> macros;
  std::vector<R> cascades;
};
struct Scratch {
  std::filesystem::path root;
  explicit Scratch(const std::filesystem::path &parent) {
    std::filesystem::create_directories(parent);
    auto p = (parent / ".openparf-input-XXXXXX").string();
    std::vector<char> v(p.begin(), p.end());
    v.push_back(0);
    auto r = mkdtemp(v.data());
    Check(r, "cannot create input staging");
    root = r;
  }
  ~Scratch() {
    std::error_code e;
    std::filesystem::remove_all(root, e);
  }
};
struct BoundRequest {
  R config;
  std::map<std::string, ArtifactRef> roles;
  std::vector<ArtifactRef> refs;
  const ArtifactRef &get(const std::string &role) const {
    auto r = roles.find(role);
    Check(r != roles.end(), "missing source " + role);
    return r->second;
  }
};
inline BoundRequest BindRequest(const ArtifactRef &request) {
  BoundRequest out;
  out.config = Read(request);
  Check(out.config.at("schema").text() == "emuflow.openparf-input-request/v1",
        "request schema disagrees");
  for (const auto &source : out.config.at("sources").array()) {
    auto ref =
        ArtifactRef::Bind(source.at("path").text(), source.at("schema").text());
    auto g = ref.generation();
    A generation{R(g.device),
                 R(g.inode),
                 R(g.bytes),
                 R(g.modified_seconds),
                 R(g.modified_nanoseconds),
                 R(g.changed_seconds),
                 R(g.changed_nanoseconds)};
    Check(source.at("generation").encode() == R(generation).encode(),
          "source generation changed");
    Check(out.roles.emplace(source.at("role").text(), ref).second,
          "repeated source role");
    out.refs.push_back(ref);
  }
  if (out.roles.count("packed")) {
    auto selector = Read(out.get("packed"));
    if (selector.at("schema").text() ==
        "emuflow.openparf-atomic-source-storage/v1") {
      auto name = selector.at("artifact").text();
      Check(std::filesystem::path(name).filename() == name,
            "invalid atomic source artifact path");
      auto ref = ArtifactRef::Bind(
          (std::filesystem::path(out.get("packed").path()).parent_path() / name)
              .string(),
          "openparf-atomic-source-store-v1");
      Check(out.roles.emplace("atomic_store", ref).second,
            "duplicate atomic source role");
      out.refs.push_back(ref);
    }
  }
  out.refs.push_back(request);
  return out;
}
// Only historical small records are projected here; production binds v2.
ArtifactRef ImportSmallMapped(const ArtifactRef &source, const std::string &top,
                              const std::filesystem::path &path);
SourceInfo DeriveSource(const ArtifactRef &mapped, const std::string &top,
                        const volatile std::sig_atomic_t *flag);
void ProduceSource(const ArtifactRef &mapped, const ArtifactRef &identity,
                   const std::string &top, const std::string &output,
                   const volatile std::sig_atomic_t *flag);
} // namespace emuflow::core::openparf_input
