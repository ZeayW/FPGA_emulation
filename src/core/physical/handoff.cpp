#include "physical/handoff.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "physical/atomic_ownership.hpp"
#include "physical/device.hpp"
#include "physical/handoff_audit.hpp"
#include "storage/schema/physical_handoff_schema.hpp"
#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using I = std::int64_t;
using Object = std::map<std::string, JsonRecord>;
using Array = std::vector<JsonRecord>;
void Check(bool ok, const std::string &reason) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical handoff bridge: " + reason);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "physical handoff bridge cancelled");
}
std::string Hash(Sha256 &digest) {
  static const char *digits = "0123456789abcdef";
  std::string value;
  for (auto c : digest.finish()) {
    value += digits[c >> 4];
    value += digits[c & 15];
  }
  return value;
}
void Feed(Sha256 &digest, const std::string &value) {
  digest.update(reinterpret_cast<const unsigned char *>(value.data()),
                value.size());
}
std::string FileHash(const ArtifactRef &source,
                     const volatile std::sig_atomic_t *cancelled = nullptr) {
  std::ifstream stream(source.path(), std::ios::binary);
  Check(bool(stream), "source cannot be read");
  Sha256 digest;
  std::array<char, 65536> bytes;
  while (stream) {
    Stop(cancelled);
    stream.read(bytes.data(), bytes.size());
    digest.update(reinterpret_cast<const unsigned char *>(bytes.data()),
                  stream.gcount());
  }
  Check(stream.eof(), "source hash failed");
  source.RequireUnchanged();
  return Hash(digest);
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value "
                        "END FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step() && !row.IsNull(0), "metadata missing/oversized: " + key);
  return row.TypedText(0);
}
I Count(SqliteDatabase &db, const std::string &sql) {
  auto row = db.Prepare(sql);
  Check(row.Step(), "count absent");
  return row.TypedInteger(0);
}
void None(SqliteDatabase &db, const std::string &sql,
          const std::string &error) {
  Check(!db.Prepare(sql).Step(), error);
}
void Attach(SqliteDatabase &db, const ArtifactRef &ref,
            const std::string &alias) {
  auto row = db.Prepare("ATTACH DATABASE ? AS " + alias);
  row.BindText(1, SqliteImmutableUri(ref.path()));
  row.Execute();
}
// Single records, local sites and control documents only. No physical design
// is ever reconstituted as a JSON tree.
JsonRecord Record(SqliteDatabase &db, const std::string &text,
                  unsigned depth = 0) {
  Check(depth <= 64 && text.size() <= 8 * 1024 * 1024, "record exceeds bound");
  auto type =
      db.Prepare("SELECT json_valid(?1),json_type(?1),json_extract(?1,'$')");
  type.BindText(1, text);
  Check(type.Step() && type.Integer(0), "invalid record JSON");
  const auto kind = type.TypedText(1);
  if (kind == "null")
    return JsonRecord();
  if (kind == "true" || kind == "false")
    return JsonRecord(kind == "true");
  if (kind == "text")
    return JsonRecord(type.TypedText(2));
  if (kind == "integer")
    return JsonRecord(type.TypedInteger(2));
  if (kind == "real")
    return JsonRecord(ParseFiniteDecimal(text, "physical record number"));
  auto rows = db.Prepare("SELECT key,CASE WHEN type IN ('object','array') THEN "
                         "value WHEN type IN ('true','false','null') THEN type "
                         "ELSE json_quote(value) END FROM json_each(?)");
  rows.BindText(1, text);
  Object object;
  Array array;
  I population = 0;
  while (rows.Step()) {
    Check(++population <= 16384, "local record population exceeds bound");
    auto value = Record(db, rows.TypedText(1), depth + 1);
    if (kind == "object")
      Check(object.emplace(rows.TypedText(0), std::move(value)).second,
            "duplicate record field");
    else
      array.emplace_back(std::move(value));
  }
  return kind == "object" ? JsonRecord(std::move(object))
                          : JsonRecord(std::move(array));
}
std::string Canonical(SqliteDatabase &db, const std::string &text) {
  return Record(db, text).EncodeUtf8();
}
std::string Text(const std::string &text, const std::string &key) {
  return BoundedJson(text).Text(key);
}
I Number(const std::string &text, const std::string &key) {
  return BoundedJson(text).Integer(key);
}
struct Workspace {
  fs::path path;
  explicit Workspace(const fs::path &parent) {
    auto value = (parent / ".physical-handoff-XXXXXX").string();
    Check(mkdtemp(value.data()) != nullptr, "private workspace unavailable");
    path = value;
  }
  ~Workspace() {
    if (!path.empty()) {
      std::error_code error;
      fs::remove_all(path, error);
    }
  }
};
struct Inputs {
  std::vector<ArtifactRef> refs;
  std::map<std::string, std::size_t> roles;
  std::string top;
  const ArtifactRef &Get(const std::string &role) const {
    return refs.at(roles.at(role));
  }
  const ArtifactRef *Optional(const std::string &role) const {
    auto found = roles.find(role);
    return found == roles.end() ? nullptr : &refs.at(found->second);
  }
};
Inputs Read(const ArtifactRef &request) {
  Check(request.schema() == "physical-handoff-bridge-request-v1",
        "request schema disagrees");
  CheckedRequestReader reader(request.path(), 4 * 1024 * 1024);
  Check(reader.Read<std::uint64_t>() == 0x31424850464d5545ULL &&
            reader.Read<std::uint32_t>() == 1,
        "request version disagrees");
  const auto count = reader.Read<std::uint32_t>();
  Check(count >= 6 && count <= 16, "source role population invalid");
  const std::map<std::string, std::string> allowed{
      {"mapped", "xilinx-mapped-index-v2"},
      {"certificate", "openparf-placement-certificate-v3"},
      {"certificate_selector", "openparf-placement-selector-v2"},
      {"atomic_source", "openparf-atomic-source-store-v1"},
      {"atomic_selector", "openparf-atomic-source-selector-v1"},
      {"architecture", "physical-input-document-v1"},
      {"constraints", "physical-input-document-v1"},
      {"native", "physical-input-document-v1"},
      {"provider", "physical-input-document-v1"}};
  Inputs result;
  for (unsigned i = 0; i < count; ++i) {
    auto role = reader.ReadString32(false, 128),
         path = reader.ReadString32(false, 32768),
         schema = reader.ReadString32(false, 128);
    Check(allowed.count(role) && allowed.at(role) == schema &&
              !result.roles.count(role),
          "source role/schema invalid or repeated");
    const auto generation = ReadFileGeneration(reader);
    auto ref = ArtifactRef::Bind(path, schema);
    if (!(generation == ref.generation()))
      throw StageError(ErrorCode::kMixedGeneration,
                       "physical bridge source generation changed");
    result.roles.emplace(role, result.refs.size());
    result.refs.push_back(std::move(ref));
  }
  for (const char *role : {"mapped", "certificate", "certificate_selector",
                           "atomic_source", "atomic_selector", "architecture"})
    Check(result.roles.count(role), "required source role missing");
  BoundedJson config(reader.ReadString32(false, 1024 * 1024));
  config.Keys({"schema", "top"});
  Check(config.Text("schema") == "emuflow.physical-handoff-build/v1",
        "control schema disagrees");
  result.top = config.Text("top");
  reader.RequireEnd();
  result.refs.push_back(request);
  return result;
}
void MatchFile(const ArtifactRef &selector, const ArtifactRef &payload,
               const std::string &name, I bytes, const std::string &hash,
               const volatile std::sig_atomic_t *cancelled) {
  Check(fs::path(name).filename() == name &&
            fs::path(payload.path()).parent_path() ==
                fs::path(selector.path()).parent_path() &&
            fs::path(payload.path()).filename() == name,
        "selector payload ownership disagrees");
  Check(bytes >= 0 && std::uint64_t(bytes) == payload.generation().bytes &&
            FileHash(payload, cancelled) == hash,
        "selector payload file seal disagrees");
}
void HashLine(Sha256 &digest, const JsonRecord &record) {
  Feed(digest, record.EncodeUtf8());
  Feed(digest, "\n");
}
void ValidateAtomicSource(const Inputs &inputs, SqliteDatabase &json,
                          const volatile std::sig_atomic_t *cancelled) {
  BoundedJson selector(BoundedJson::Read(inputs.Get("atomic_selector")));
  selector.Keys({"schema", "logical_schema", "application_id", "user_version",
                 "header", "counts", "mapped", "logical_sha256", "artifact",
                 "store_bytes", "store_sha256"});
  Check(selector.Text("schema") ==
                "emuflow.openparf-atomic-source-storage/v1" &&
            selector.Text("logical_schema") ==
                "emuflow.openparf-atomic-source/v1" &&
            selector.Integer("application_id") == 0x454d5541 &&
            selector.Integer("user_version") == 1,
        "atomic selector identity disagrees");
  BoundedJson header(selector.Token("header")),
      counts_contract(selector.Token("counts")),
      mapped_contract(selector.Token("mapped"));
  header.Keys({"schema", "status", "top", "source", "policy", "summary"});
  counts_contract.Keys({"physical_cells", "macro_clusters", "macro_assignments",
                        "singleton_clusters", "cascade_chains",
                        "constant_cells"});
  mapped_contract.Keys({"artifact", "bytes", "sha256"});
  Check(header.Text("schema") == "emuflow.openparf-atomic-source/v1" &&
            header.Text("status") == "pass" && !header.Text("top").empty(),
        "atomic header invalid");
  BoundedJson source_contract(header.Token("source")),
      policy(header.Token("policy"));
  source_contract.Keys({"mapped_sha256"});
  policy.Keys({"provider", "dedicated_or_relative_constraints"});
  Check(policy.Text("provider") ==
                "mapped-singleton-atoms-no-site-packing-v1" &&
            policy.Text("dedicated_or_relative_constraints") == "fail-closed",
        "atomic source policy disagrees");
  for (const auto &item : counts_contract.Fields())
    Check(counts_contract.Integer(item.first) >= 0,
          "atomic source count negative");
  Check(fs::path(mapped_contract.Text("artifact")).filename() ==
                mapped_contract.Text("artifact") &&
            mapped_contract.Integer("bytes") >= 0,
        "atomic mapped descriptor invalid");
  MatchFile(inputs.Get("atomic_selector"), inputs.Get("atomic_source"),
            selector.Text("artifact"), selector.Integer("store_bytes"),
            selector.Text("store_sha256"), cancelled);
  SqliteStore store(inputs.Get("atomic_source"));
  auto &db = store.database();
  for (const char *key : {"header", "counts", "mapped", "logical_sha256"})
    Check(Canonical(json, Meta(db, key)) ==
              Canonical(json, selector.Token(key)),
          "atomic source metadata disagrees");
  Check(Count(db, "SELECT count(*) FROM metadata") == 4,
        "atomic source has extra metadata");
  Sha256 clusters, assignments, cascades, constants;
  I groups = 0, macros = 0;
  auto rows = db.Prepare("SELECT "
                         "seq,cluster_id,kind,site_templates_json,control_set_"
                         "json,site_mode FROM clusters ORDER BY seq");
  auto members = db.Prepare(
      "SELECT cluster_seq,seq,instance,cell_type,bel,bel_candidates_json FROM "
      "assignments ORDER BY cluster_seq,seq");
  bool have = members.Step();
  while (rows.Step()) {
    Stop(cancelled);
    Check(rows.TypedInteger(0) == groups++,
          "atomic cluster sequence incomplete");
    const auto id = rows.TypedText(1), kind = rows.TypedText(2);
    Check(!id.empty() && id.rfind("atomic-source-", 0) != 0 &&
              (kind == "slice" || kind == "carry" || kind == "hard"),
          "atomic macro identity invalid");
    const auto templates = BoundedJson::Array(rows.TypedText(3));
    Check(!templates.empty(), "atomic macro templates empty");
    for (const auto &item : templates)
      Check(!DecodeJsonString(item).empty(),
            "atomic macro template identity invalid");
    Array entries;
    I seq = 0;
    while (have && members.TypedInteger(0) == rows.TypedInteger(0)) {
      Check(members.TypedInteger(1) == seq++ && entries.size() < 256,
            "atomic macro sequence/bound invalid");
      Check(!members.TypedText(2).empty() && !members.TypedText(3).empty(),
            "atomic assignment identity invalid");
      const auto candidates = BoundedJson::Array(members.TypedText(5));
      Check(!candidates.empty(), "atomic BEL candidates empty");
      for (const auto &item : candidates)
        Check(!DecodeJsonString(item).empty(), "atomic BEL candidate invalid");
      Object entry{{"instance", members.TypedText(2)},
                   {"cell_type", members.TypedText(3)},
                   {"bel_candidates", Record(json, members.TypedText(5))}};
      if (!members.IsNull(4)) {
        Check(!members.TypedText(4).empty(), "atomic concrete BEL empty");
        entry.emplace("bel", members.TypedText(4));
      }
      HashLine(assignments, JsonRecord(entry));
      entries.emplace_back(std::move(entry));
      ++macros;
      have = members.Step();
    }
    Check(!entries.empty(), "atomic macro is empty");
    Object cluster{{"id", id},
                   {"kind", kind},
                   {"site_templates", Record(json, rows.TypedText(3))},
                   {"control_set", Record(json, rows.TypedText(4))},
                   {"assignments", std::move(entries)}};
    if (!rows.IsNull(5))
      cluster.emplace("site_mode", rows.TypedText(5));
    HashLine(clusters, JsonRecord(cluster));
  }
  Check(!have, "atomic assignment has no source macro");
  I chain = 0;
  auto chains = db.Prepare(
      "SELECT seq,cascade_id,record_json FROM cascades ORDER BY seq");
  while (chains.Step()) {
    Stop(cancelled);
    Check(chains.TypedInteger(0) == chain++,
          "atomic cascade sequence incomplete");
    const auto raw = chains.TypedText(2);
    Check(Canonical(json, raw) == raw && Text(raw, "id") == chains.TypedText(1),
          "atomic cascade record invalid");
    Feed(cascades, raw);
    Feed(cascades, "\n");
  }
  I constant = 0;
  auto zero = db.Prepare("SELECT seq,instance FROM constants ORDER BY seq");
  while (zero.Step()) {
    Stop(cancelled);
    Check(zero.TypedInteger(0) == constant++,
          "atomic constant sequence incomplete");
    HashLine(constants, JsonRecord(zero.TypedText(1)));
  }
  const auto counts = selector.Token("counts");
  Check(Number(counts, "macro_clusters") == groups &&
            Number(counts, "macro_assignments") == macros &&
            Number(counts, "cascade_chains") == chain &&
            Number(counts, "constant_cells") == constant,
        "atomic source row coverage disagrees");
  Object source_summary{
      {"physical_atoms", Number(counts, "singleton_clusters") + groups},
      {"constant_cells", constant}};
  if (groups)
    source_summary.emplace("physical_macros", groups);
  Check(Canonical(json, header.Token("summary")) ==
            JsonRecord(source_summary).EncodeUtf8(),
        "atomic source summary disagrees");
  Object logical{{"header", Record(json, selector.Token("header"))},
                 {"counts", Record(json, counts)},
                 {"mapped", Record(json, selector.Token("mapped"))},
                 {"cluster_sha256", Hash(clusters)},
                 {"assignment_sha256", Hash(assignments)},
                 {"cascade_sha256", Hash(cascades)},
                 {"constant_sha256", Hash(constants)}};
  Sha256 digest;
  Feed(digest, JsonRecord(logical).EncodeUtf8());
  Check(Hash(digest) == selector.Text("logical_sha256"),
        "atomic source logical seal disagrees");
}
std::string ValidateCertificate(const Inputs &inputs, SqliteDatabase &json,
                                const volatile std::sig_atomic_t *cancelled) {
  BoundedJson selector(BoundedJson::Read(inputs.Get("certificate_selector")));
  Check(!selector.Has("clusters"),
        "inline certificate production route retired");
  const auto descriptor = selector.Token("cluster_storage");
  BoundedJson d(descriptor);
  Check(d.Text("schema") == "emuflow.openparf-atomic-placement-database/v3" &&
            d.Integer("application_id") == 0x454d5550 &&
            d.Integer("user_version") == 3,
        "certificate descriptor identity disagrees");
  MatchFile(inputs.Get("certificate_selector"), inputs.Get("certificate"),
            d.Text("file"), d.Integer("bytes"), d.Text("sha256"), cancelled);
  auto fields = selector.Fields();
  fields.erase("cluster_storage");
  const auto header = BoundedJson::Encode(fields);
  SqliteStore store(inputs.Get("certificate"));
  auto &db = store.database();
  Check(Meta(db, "logical_sha256") == d.Text("logical_sha256") &&
            Meta(db, "clusters") == std::to_string(d.Integer("clusters")) &&
            Meta(db, "assignments") ==
                std::to_string(d.Integer("assignments")) &&
            Count(db, "SELECT count(*) FROM metadata") == 4,
        "certificate metadata disagrees");
  Sha256 digest;
  const auto append = [&](const std::string &kind, const JsonRecord &value) {
    const auto encoded =
        JsonRecord(Object{{"kind", kind}, {"value", value}}).EncodeUtf8();
    std::array<unsigned char, 8> bytes{};
    auto n = std::uint64_t(encoded.size());
    for (int k = 7; k >= 0; --k) {
      bytes[k] = n & 255;
      n >>= 8;
    }
    digest.update(bytes.data(), 8);
    Feed(digest, encoded);
  };
  append("header/v1", Record(json, header));
  I groups = 0, assigned = 0;
  auto rows = db.Prepare("SELECT cluster_index,cluster,site,site_type,x,y FROM "
                         "clusters ORDER BY cluster_index");
  while (rows.Step()) {
    Stop(cancelled);
    Check(rows.TypedInteger(0) == groups++,
          "certificate cluster sequence incomplete");
    append("cluster/v1", Array{rows.TypedInteger(0), rows.TypedText(1),
                               rows.TypedText(2), rows.TypedText(3),
                               rows.TypedInteger(4), rows.TypedInteger(5)});
  }
  auto a =
      db.Prepare("SELECT "
                 "cluster_index,assignment_index,instance,cell_type,bel,"
                 "physical_site,placement_mode,source_cluster,bram_tile_group "
                 "FROM assignments ORDER BY cluster_index,assignment_index");
  I previous = -1, ordinal = 0;
  while (a.Step()) {
    Stop(cancelled);
    const auto group = a.TypedInteger(0);
    Check(group >= 0 && group < groups, "certificate assignment owner absent");
    if (previous != group) {
      Check(group == previous + 1, "certificate cluster has no assignments");
      ordinal = 0;
      previous = group;
    }
    Check(a.TypedInteger(1) == ordinal++,
          "certificate assignment sequence incomplete");
    Array value{group,
                a.TypedInteger(1),
                a.TypedText(2),
                a.TypedText(3),
                a.TypedText(4),
                a.TypedText(5),
                a.TypedText(6),
                a.TypedText(7),
                a.IsNull(8) ? JsonRecord() : JsonRecord(a.TypedText(8))};
    append("assignment/v1", JsonRecord(value));
    ++assigned;
  }
  Check(groups > 0 && groups == d.Integer("clusters") &&
            previous == groups - 1 && assigned == d.Integer("assignments") &&
            Hash(digest) == d.Text("logical_sha256"),
        "certificate logical seal/coverage disagrees");
  return header;
}
const std::set<std::string> kFF{"FDCE", "FDPE", "FDRE", "FDSE"};
const std::set<std::string> kLut{"LUT1", "LUT2", "LUT3",  "LUT4",
                                 "LUT5", "LUT6", "LUT6_2"};
const std::set<std::string> kMux{"MUXF7", "MUXF8", "MUXF9"};
const std::set<std::string> kHard{"DSP48E2", "RAMB18E2", "RAMB36E2", "URAM288"};
const std::vector<std::string> &FFBels() {
  static const auto result = [] {
    std::vector<std::string> names;
    for (char c = 'A'; c <= 'H'; ++c) {
      names.push_back(std::string(1, c) + "FF");
      names.push_back(std::string(1, c) + "FF2");
    }
    return names;
  }();
  return result;
}
I FFIndex(const std::string &bel) {
  const auto &names = FFBels();
  const auto it = std::find(names.begin(), names.end(), bel);
  Check(it != names.end(), "invalid FF BEL");
  return it - names.begin();
}
struct Assignment {
  PhysicalAssignment physical;
  std::string mode, site;
  I ordinal;
};
std::vector<PhysicalAssignment> Physical(const std::vector<Assignment> &rows) {
  std::vector<PhysicalAssignment> result;
  for (const auto &row : rows)
    result.push_back(row.physical);
  return result;
}
struct Legalizer {
  SqliteDatabase &db;
  MappedPhysicalSemantics &semantics;
  Sha256 digest;
  I moves = 0, sites = 0;
  void Repair(const std::string &cluster, const Assignment &row,
              const std::string &from, const std::string &to) {
    auto put = db.Prepare("INSERT INTO site_pin_repair VALUES(?,?,?,?,?)");
    put.BindInteger(1, moves++);
    put.BindText(2, cluster);
    put.BindText(3, row.physical.instance);
    put.BindText(4, from);
    put.BindText(5, to);
    put.Execute();
    HashLine(digest, JsonRecord(Object{{"cluster", cluster},
                                       {"instance", row.physical.instance},
                                       {"from_bel", from},
                                       {"to_bel", to}}));
  }
  void Run(const std::string &cluster, std::vector<Assignment> &rows) {
    bool repaired = false;
    for (;;) {
      auto current = Physical(rows);
      const auto conflicts = semantics.SitePinConflicts(current);
      if (conflicts.empty())
        break;
      const auto resource = conflicts.front().resource;
      Check(!resource.empty(), "site pin conflict resource missing");
      const auto from = resource.substr(0, 1) + "FF";
      const auto source_it =
          std::find_if(rows.begin(), rows.end(), [&](const auto &row) {
            return kFF.count(row.physical.type) && row.physical.bel == from;
          });
      Check(source_it != rows.end(), "site pin conflict lacks primary FF");
      const auto source = std::size_t(source_it - rows.begin());
      std::map<std::string, std::size_t> owners;
      for (std::size_t i = 0; i < rows.size(); ++i)
        if (kFF.count(rows[i].physical.type))
          Check(owners.emplace(rows[i].physical.bel, i).second,
                "site reuses FF BEL");
      std::vector<std::string> candidates;
      for (const auto &bel : FFBels())
        if (!owners.count(bel))
          candidates.push_back(bel);
      const auto index = FFIndex(from);
      std::sort(candidates.begin(), candidates.end(),
                [&](const auto &a, const auto &b) {
                  const auto key = [&](const auto &bel) {
                    const auto target = FFIndex(bel);
                    return std::make_tuple(bel != resource.substr(0, 1) + "FF2",
                                           (target < 8) != (index < 8),
                                           std::abs(target - index), bel);
                  };
                  return key(a) < key(b);
                });
      const auto improves = [&] {
        try {
          const auto candidate = Physical(rows);
          (void)semantics.ControlSets(candidate);
          return semantics.SitePinConflicts(candidate).size() <
                 conflicts.size();
        } catch (const StageError &) {
          return false;
        }
      };
      bool accepted = false;
      for (const auto &target : candidates) {
        rows[source].physical.bel = target;
        if (improves()) {
          Repair(cluster, rows[source], from, target);
          accepted = true;
          break;
        }
        rows[source].physical.bel = from;
      }
      if (!accepted)
        for (const auto &target : owners) {
          if (target.first == from)
            continue;
          rows[source].physical.bel = target.first;
          rows[target.second].physical.bel = from;
          if (improves()) {
            Repair(cluster, rows[source], from, target.first);
            Repair(cluster, rows[target.second], target.first, from);
            accepted = true;
            break;
          }
          rows[source].physical.bel = from;
          rows[target.second].physical.bel = target.first;
        }
      Check(accepted,
            "site pin conflict cannot be legalized within certified site " +
                cluster + " on " + resource);
      repaired = true;
    }
    if (repaired)
      ++sites;
  }
  JsonRecord Summary() {
    return Object{
        {"provider", "ultrascaleplus-slice-site-pin-legalizer-v1"},
        {"moved_ffs", moves},
        {"sites", sites},
        {"repairs", Object{{"schema", "emuflow.xilinx-site-pin-repairs/v1"},
                           {"records", moves},
                           {"sha256", Hash(digest)}}}};
  }
};
JsonRecord Counter(const std::map<std::string, I> &counts) {
  Object result;
  for (const auto &item : counts)
    result.emplace(item.first, item.second);
  return JsonRecord(result);
}
struct Stats {
  I clusters = 0, assignments = 0, constants = 0, luts = 0, ffs = 0, carry = 0,
    cascades = 0, cascade_links = 0, hard_edges = 0, carry_edges = 0;
  std::map<std::string, I> kinds, types, hard;
};
void BramGroup(SqliteDatabase &db, const std::string &anchor,
               const Assignment &assignment,
               const std::optional<std::string> &raw, bool native) {
  const auto &type = assignment.physical.type;
  const auto &bel = assignment.physical.bel;
  if (type != "RAMB18E2" && type != "RAMB36E2") {
    Check(!raw, "non-BRAM assignment has BRAM tile group");
    return;
  }
  if (!raw) {
    Check(!native, "native BRAM tile group missing");
    return;
  }
  Check(native, "BRAM tile group requires source-sealed native constraints");
  BoundedJson group(*raw);
  group.Keys({"anchor", "claims", "role", "tile"});
  const auto role = type == "RAMB18E2" && bel == "RAMB18E2_L"   ? "lower"
                    : type == "RAMB18E2" && bel == "RAMB18E2_U" ? "upper"
                    : type == "RAMB36E2" && bel == "RAMB36E2"   ? "whole"
                                                                : "";
  Check(!std::string(role).empty() && group.Text("anchor") == anchor &&
            group.Text("role") == role,
        "BRAM group role/anchor invalid");
  auto view = db.Prepare("SELECT b.tile,b.site,b.site_type,b.bel FROM pd_bram "
                         "b WHERE b.anchor=? AND b.role=?");
  view.BindText(1, anchor);
  view.BindText(2, role);
  Check(view.Step() && view.TypedText(0) == group.Text("tile") &&
            view.TypedText(1) == assignment.site &&
            view.TypedText(2) == assignment.mode && view.TypedText(3) == bel &&
            !view.Step(),
        "BRAM group disagrees with native view");
  Array claims;
  if (std::string(role) == "whole") {
    claims.emplace_back("bram:" + group.Text("tile") + ":lower");
    claims.emplace_back("bram:" + group.Text("tile") + ":upper");
  } else
    claims.emplace_back("bram:" + group.Text("tile") + ":" + role);
  Check(Canonical(db, group.Token("claims")) == JsonRecord(claims).EncodeUtf8(),
        "BRAM tile claims disagree");
}
void ProduceClusters(SqliteDatabase &db, const Inputs &inputs,
                     PhysicalDevice &device, MappedPhysicalSemantics &semantics,
                     Legalizer &legalizer, Stats &stats, bool native,
                     const volatile std::sig_atomic_t *cancelled) {
  Attach(db, inputs.Get("certificate"), "certificate");
  Attach(db, inputs.Get("mapped"), "mapped");
  Attach(db, inputs.Get("atomic_source"), "atomic");
  None(db,
       "SELECT 1 FROM mapped.cells WHERE type NOT IN "
       "('LUT1','LUT2','LUT3','LUT4','LUT5','LUT6','LUT6_2','FDCE','FDPE','"
       "FDRE','FDSE','MUXF7','MUXF8','MUXF9','CARRY8','DSP48E2','RAMB18E2','"
       "RAMB36E2','URAM288','GND','VCC') LIMIT 1",
       "unsupported mapped primitive");
  stats.constants = Count(
      db, "SELECT count(*) FROM mapped.cells WHERE type IN ('GND','VCC')");
  auto clusters = db.Prepare("SELECT cluster_index,cluster,site,site_type,x,y "
                             "FROM certificate.clusters ORDER BY cluster");
  auto assignments =
      db.Prepare("SELECT "
                 "assignment_index,instance,cell_type,bel,physical_site,"
                 "placement_mode,bram_tile_group FROM certificate.assignments "
                 "WHERE cluster_index=? ORDER BY assignment_index");
  while (clusters.Step()) {
    Stop(cancelled);
    const auto id = clusters.TypedText(1), anchor = clusters.TypedText(2);
    const auto site = device.Site(anchor);
    Check(id == "openparf:" + anchor && clusters.TypedText(3) == site.type &&
              clusters.TypedInteger(4) == site.x &&
              clusters.TypedInteger(5) == site.y,
          "certified site metadata/group identity disagrees");
    assignments.Reset();
    assignments.BindInteger(1, clusters.TypedInteger(0));
    std::vector<Assignment> rows;
    std::set<std::string> bels, modes;
    I lut = 0, ff = 0, carry = 0, dual = 0, hard = 0;
    bool ram18 = true, mux = false;
    while (assignments.Step()) {
      Stop(cancelled);
      Check(rows.size() < 64,
            "certified site exceeds bounded physical capacity");
      Assignment item{{assignments.TypedText(1), assignments.TypedText(2),
                       assignments.TypedText(3)},
                      assignments.TypedText(5),
                      assignments.TypedText(4),
                      assignments.TypedInteger(0)};
      Check(!item.physical.instance.empty() &&
                semantics.CellType(item.physical.instance) ==
                    item.physical.type,
            "mapped certificate cell/type mismatch");
      Check(bels.insert(item.physical.bel).second, "certificate reuses BEL");
      const auto binding = device.ResolveBel(anchor, item.physical.bel,
                                             item.physical.type, {item.mode});
      Check(binding.mode == item.mode && binding.physical_site == item.site,
            "certificate physical site/mode disagrees");
      BramGroup(db, anchor, item,
                assignments.IsNull(6)
                    ? std::nullopt
                    : std::optional<std::string>(assignments.TypedText(6)),
                native);
      modes.insert(item.mode);
      const auto &type = item.physical.type;
      if (kLut.count(type)) {
        ++lut;
        ++stats.luts;
        if (type == "LUT6_2")
          ++dual;
      } else if (kFF.count(type)) {
        ++ff;
        ++stats.ffs;
      } else if (kHard.count(type)) {
        ++hard;
        ++stats.hard[type];
        ram18 = ram18 && type == "RAMB18E2";
      } else if (type == "CARRY8")
        ++carry;
      else if (kMux.count(type))
        mux = true;
      else
        Check(false, "unsupported certified primitive");
      rows.push_back(std::move(item));
    }
    Check(!rows.empty(), "certificate site is empty");
    std::string kind = "slice", site_mode;
    if (hard) {
      Check(I(rows.size()) == hard, "site mixes hard and slice resources");
      kind = "hard";
      if (ram18) {
        Check(rows.size() <= 2 &&
                  std::all_of(rows.begin(), rows.end(),
                              [](const auto &item) {
                                return item.physical.bel == "RAMB18E2_L" ||
                                       item.physical.bel == "RAMB18E2_U";
                              }),
              "RAMB18 shared tile invalid");
        site_mode = "RAMB18E2x" + std::to_string(rows.size());
      } else
        Check(rows.size() == 1, "hard site is not singleton");
    } else {
      Check(lut <= 8 && ff <= 16, "slice exceeds LUT/FF capacity");
      if (carry || dual) {
        std::set<std::string> expected{"CARRY8"};
        for (char c = 'A'; c <= 'H'; ++c)
          expected.insert(std::string(1, c) + "6LUT");
        Check(rows.size() == 9 && carry == 1 && dual == 8 && bels == expected,
              "exclusive CARRY8/LUT6_2 macro invalid");
        kind = "carry";
        ++stats.carry;
      }
    }
    auto controls = semantics.ControlSets(Physical(rows));
    Array templates;
    for (const auto &mode : modes)
      templates.emplace_back(mode);
    auto put =
        db.Prepare("INSERT INTO cluster VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)");
    put.BindInteger(1, stats.clusters);
    put.BindText(2, id);
    put.BindText(3, kind);
    put.BindText(4, JsonRecord(templates).Encode());
    put.BindText(5, controls.size() == 1 ? JsonRecord(controls.front()).Encode()
                                         : "null");
    if (controls.size() > 1) {
      Array values;
      for (const auto &value : controls)
        values.emplace_back(value);
      put.BindText(6, JsonRecord(values).Encode());
    } else
      put.BindNull(6);
    if (site_mode.empty())
      put.BindNull(7);
    else
      put.BindText(7, site_mode);
    put.BindText(8, anchor);
    put.BindText(9, site.type);
    put.BindInteger(10, site.x);
    put.BindInteger(11, site.y);
    put.BindInteger(12, 0);
    put.BindText(13, site.region_json);
    put.Execute();
    if (kind == "slice" && mux)
      legalizer.Run(id, rows);
    semantics.ValidateSlice(Physical(rows));
    std::sort(rows.begin(), rows.end(), [](const auto &a, const auto &b) {
      return a.physical.instance < b.physical.instance;
    });
    auto member = db.Prepare("INSERT INTO assignment VALUES(?,?,?,?,?,?,?,?)");
    for (std::size_t n = 0; n < rows.size(); ++n) {
      const auto &row = rows[n];
      member.BindInteger(1, stats.clusters);
      member.BindText(2, row.physical.instance);
      member.BindText(3, row.physical.type);
      member.BindText(4, row.physical.bel);
      member.BindText(5, row.mode);
      member.BindText(6, row.site);
      member.BindInteger(7, n);
      member.BindInteger(8, row.ordinal);
      member.Execute();
      ++stats.assignments;
    }
    ++stats.clusters;
    ++stats.kinds[kind];
    ++stats.types[site.type];
  }
  const auto expected = Count(
      db, "SELECT count(*) FROM mapped.cells WHERE type NOT IN ('GND','VCC')");
  Check(stats.assignments == expected,
        "physical assignment coverage incomplete");
  db.Execute(
      "INSERT INTO constant SELECT row_number() OVER(ORDER BY id)-1,id FROM "
      "mapped.cells WHERE type IN ('GND','VCC') ORDER BY id;INSERT INTO "
      "cascade SELECT seq,record_json FROM atomic.cascades ORDER BY seq");
  stats.cascades = Count(db, "SELECT count(*) FROM cascade");
  stats.cascade_links = Count(db, "SELECT "
                                  "coalesce(sum(max(0,json_array_length(value_"
                                  "json,'$.instances')-1)),0) FROM cascade");
  stats.hard_edges = Count(
      db,
      "SELECT "
      "coalesce(sum(max(0,json_array_length(value_json,'$.instances')-1)),0) "
      "FROM cascade WHERE json_extract(value_json,'$.cell_type')!='CARRY8'");
  stats.carry_edges = stats.cascade_links - stats.hard_edges;
  Check(stats.cascades == 0 || native,
        "cascade bridge requires source-sealed native constraints");
}
JsonRecord LocalSummary(SqliteDatabase &db) {
  auto rows = db.Prepare(
      "SELECT p.slr,p.clock_region,p.type,count(*),(SELECT count(*) FROM "
      "cluster c JOIN pd_sites o ON o.name=c.site WHERE o.slr=p.slr AND "
      "o.clock_region=p.clock_region AND o.type=p.type) FROM pd_sites p WHERE "
      "p.clock_region!='' GROUP BY p.slr,p.clock_region,p.type");
  I groups = 0;
  std::optional<double> utilization, reservation;
  while (rows.Step()) {
    const auto capacity = rows.TypedInteger(3), used = rows.TypedInteger(4);
    Check(capacity > 0 && used <= capacity, "clock-region site limit exceeded");
    const auto value = double(used) / capacity;
    utilization = std::max(utilization.value_or(0), value);
    reservation = std::max(reservation.value_or(0), value);
    ++groups;
  }
  return Object{{"clock_region_site_groups", groups},
                {"maximum_clock_region_site_utilization",
                 utilization ? JsonRecord(*utilization) : JsonRecord()},
                {"maximum_clock_region_site_reservation",
                 reservation ? JsonRecord(*reservation) : JsonRecord()}};
}
Object JsonFields(SqliteDatabase &db, const std::string &text) {
  Object result;
  auto rows = db.Prepare("SELECT key,CASE WHEN type IN ('object','array') THEN "
                         "value WHEN type IN ('true','false','null') THEN type "
                         "ELSE json_quote(value) END FROM json_each(?)");
  rows.BindText(1, text);
  while (rows.Step())
    Check(
        result.emplace(rows.TypedText(0), Record(db, rows.TypedText(1))).second,
        "duplicate object field");
  return result;
}
} // namespace

StageResult BuildPhysicalHandoff(const ArtifactRef &request_ref,
                                 const std::string &output_directory,
                                 const volatile std::sig_atomic_t *cancelled) {
  std::optional<Inputs> input;
  return RunStage(
      "physical.build-handoff",
      [&] {
        input = Read(request_ref);
        return input->refs;
      },
      [&](const auto &refs) {
        const auto &inputs = *input;
        Stop(cancelled);
        const auto destination = fs::absolute(output_directory);
        Check(!fs::exists(destination) && !fs::is_symlink(destination),
              "output generation exists");
        fs::create_directories(destination.parent_path());
        Workspace work(destination.parent_path());
        SqliteDatabase json(":memory:", SqliteDatabase::Mode::kReadOnly);
        const auto certificate_header =
            ValidateCertificate(inputs, json, cancelled);
        ValidateAtomicSource(inputs, json, cancelled);
        BoundedJson certificate(certificate_header);
        Check(certificate.Text("schema") ==
                      "emuflow.openparf-atomic-placement/v2" &&
                  certificate.Text("status") == "pass" &&
                  certificate.Text("provider") ==
                      "openparf-native-rudy-pin-aware-mcf-direct-lg-ism-atomic-"
                      "v8",
              "atomic placement identity invalid");
        const auto runtime = certificate.Text("runtime_validation");
        Check(runtime == "unverified" || runtime == "native-openparf",
              "atomic runtime qualification invalid");
        const auto mapped_sha = FileHash(inputs.Get("mapped"), cancelled),
                   architecture_sha =
                       FileHash(inputs.Get("architecture"), cancelled),
                   certificate_sha =
                       FileHash(inputs.Get("certificate_selector"), cancelled),
                   atomic_sha =
                       FileHash(inputs.Get("atomic_selector"), cancelled);
        SqliteStore mapped(inputs.Get("mapped"));
        const auto top = DecodeJsonString(Meta(mapped.database(), "top_json"));
        Check(inputs.top.empty() || inputs.top == top,
              "mapped top selector disagrees");
        BoundedJson source(certificate.Token("source"));
        Check(source.Text("mapped_sha256") == mapped_sha &&
                  source.Text("architecture_sha256") == architecture_sha &&
                  source.Text("packed_sha256") == atomic_sha,
              "certificate source seals disagree");
        for (const auto *key : {"native_placement_sha256", "name_map_sha256"}) {
          const auto digest = source.Text(key);
          Check(digest.size() == 64 &&
                    digest.find_first_not_of("0123456789abcdef") ==
                        std::string::npos,
                "invalid certificate lineage digest");
        }
        const bool native = source.Has("native_constraints_sha256") ||
                            source.Has("provider_manifest_sha256");
        Check(native == bool(inputs.Optional("native")) &&
                  native == bool(inputs.Optional("provider")),
              "native certificate source catalog disagrees");
        if (native) {
          Check(inputs.Optional("native") && inputs.Optional("provider"),
                "native certificate requires constraints and provider");
          Check(source.Text("native_constraints_sha256") ==
                        FileHash(inputs.Get("native"), cancelled) &&
                    source.Text("provider_manifest_sha256") ==
                        FileHash(inputs.Get("provider"), cancelled),
                "native certificate source seals disagree");
        }
        const auto ownership_request = work.path / "ownership.bin",
                   ownership_summary = work.path / "ownership.json";
        {
          CheckedRequestWriter writer(ownership_request.string(), refs);
          writer.Put(std::uint64_t(0x314f5341464d5545ULL));
          writer.Put(std::uint32_t(1));
          for (const char *role : {"mapped", "atomic_source", "certificate"})
            writer.Generation(inputs.Get(role).generation());
          writer.Text(mapped_sha);
          writer.Text(
              BoundedJson(BoundedJson::Read(inputs.Get("atomic_selector")))
                  .Text("logical_sha256"));
          writer.Text(BoundedJson(BoundedJson(BoundedJson::Read(inputs.Get(
                                                  "certificate_selector")))
                                      .Token("cluster_storage"))
                          .Text("logical_sha256"));
          writer.Text(top);
          writer.Finish();
        }

        ValidateAtomicOwnership(
            {inputs.Get("mapped"), inputs.Get("atomic_source"),
             inputs.Get("certificate"),
             ArtifactRef::Bind(ownership_request.string(),
                               "atomic-ownership-request-v1")},
            ownership_summary.string());
        const auto ownership_text = BoundedJson::Read(ArtifactRef::Bind(
            ownership_summary.string(), "atomic-ownership-summary-v1"));
        const auto handoff_path = work.path / "physical-handoff.sqlite";
        Stats stats;
        JsonRecord legalization;
        I moved = 0, repaired_sites = 0;
        {
          SqliteArtifactWriter writer(handoff_path.string(),
                                      "xilinx-physical-handoff-v1", refs);
          auto &db = writer.database();
          db.Execute(kPhysicalHandoffDDL);
          db.Execute(
              "PRAGMA journal_mode=OFF;PRAGMA temp_store=FILE;CREATE TABLE "
              "feature(name TEXT PRIMARY KEY) WITHOUT ROWID;INSERT INTO "
              "feature VALUES('canonical-cluster-id-order-v1');INSERT INTO "
              "feature VALUES('typed-site-pin-repairs-v1');");
          {
            PhysicalDevice device(
                db, inputs.Get("architecture"),
                native ? inputs.Optional("native") : nullptr,
                native ? inputs.Optional("provider") : nullptr, cancelled);
            Check(certificate.Text("part") == device.part(),
                  "certificate/device part mismatch");
            MappedPhysicalSemantics semantics(inputs.Get("mapped"));
            SqliteTransaction transaction(db);
            Legalizer legalizer{db, semantics};
            ProduceClusters(db, inputs, device, semantics, legalizer, stats,
                            native, cancelled);
            moved = legalizer.moves;
            repaired_sites = legalizer.sites;
            legalization = legalizer.Summary();
            Object expected_summary{{"atoms", stats.assignments},
                                    {"occupied_sites", stats.clusters},
                                    {"luts", stats.luts},
                                    {"ffs", stats.ffs},
                                    {"hard_resources", Counter(stats.hard)}};
            if (stats.carry)
              expected_summary.emplace("carry8_macros", stats.carry);
            if (stats.hard_edges)
              expected_summary.emplace("native_hardblock_edges",
                                       stats.hard_edges);
            if (stats.carry_edges)
              expected_summary.emplace("native_carry_edges", stats.carry_edges);
            Check(Canonical(json, certificate.Token("summary")) ==
                      JsonRecord(expected_summary).EncodeUtf8(),
                  "atomic placement summary invalid");
            const auto constraint_sha =
                inputs.Optional("constraints")
                    ? JsonRecord(FileHash(inputs.Get("constraints"), cancelled))
                    : JsonRecord();
            Object packed{
                {"schema", "emuflow.packed-site-netlist/v1"},
                {"status", "pass"},
                {"mapping_profile", "xilinx-ultrascaleplus-open-v1"},
                {"top", top},
                {"source",
                 Object{{"mapped_json_sha256", mapped_sha},
                        {"architecture_sha256", architecture_sha},
                        {"openparf_atomic_placement_sha256", certificate_sha},
                        {"source_packed_sha256", atomic_sha}}},
                {"policy",
                 Object{{"provider",
                         "openparf-native-atomic-site-group-bridge-v1"},
                        {"source_grouping",
                         "one-cluster-per-certified-physical-site"},
                        {"repacking", moved > 0},
                        {"site_pin_legalization", legalization},
                        {"ff_control_set_model",
                         "ultrascaleplus-half-slice-cksr-ff-lane-ce-v1"}}},
                {"summary",
                 Object{{"cells", stats.assignments + stats.constants},
                        {"placed_cells", stats.assignments},
                        {"constant_cells", stats.constants},
                        {"clusters", stats.clusters},
                        {"cluster_kinds", Counter(stats.kinds)},
                        {"cascade_chains", stats.cascades},
                        {"cascade_links", stats.cascade_links}}}};
            Object placement_source{
                {"packed_sha256", JsonRecord()},
                {"architecture_sha256", architecture_sha},
                {"guidance_sha256", JsonRecord()},
                {"constraints_sha256", constraint_sha},
                {"openparf_atomic_placement_sha256", certificate_sha}};
            if (native) {
              placement_source.emplace(
                  "native_constraints_sha256",
                  FileHash(inputs.Get("native"), cancelled));
              placement_source.emplace(
                  "provider_manifest_sha256",
                  FileHash(inputs.Get("provider"), cancelled));
            }
            auto summary = JsonFields(db, LocalSummary(db).EncodeUtf8());
            summary.emplace("clusters", stats.clusters);
            summary.emplace("cells", stats.assignments);
            summary.emplace("fixed_clusters", 0);
            summary.emplace("cascade_chains", stats.cascades);
            summary.emplace("mean_guidance_displacement", JsonRecord());
            summary.emplace("max_guidance_displacement", JsonRecord());
            summary.emplace("site_pin_moved_ffs", moved);
            summary.emplace("site_pin_repaired_sites", repaired_sites);
            summary.emplace("site_types", Counter(stats.types));
            Object placement{
                {"schema", "emuflow.xilinx-placement/v1"},
                {"status", "pass"},
                {"part", device.part()},
                {"provider",
                 "openparf-native-mcf-direct-lg-ism-atomic-bridge-v1"},
                {"policy",
                 Object{{"clock_region_site_utilization_limit", 1.0},
                        {"capacity_rounding", "ceil-with-one-site-minimum"},
                        {"packing", "native-openparf-atomic-site-groups-v1"},
                        {"placement_certificate",
                         "emuflow.openparf-atomic-placement/v2"},
                        {"site_pin_legalization",
                         "ultrascaleplus-slice-site-pin-legalizer-v1"}}},
                {"source", std::move(placement_source)},
                {"summary", std::move(summary)}};
            auto document = db.Prepare("INSERT INTO document VALUES(?,?)");
            for (const auto &item : std::map<std::string, JsonRecord>{
                     {"packed", packed}, {"placement", placement}}) {
              document.BindText(1, item.first);
              document.BindText(2, item.second.Encode());
              document.Execute();
            }
            auto manifest = db.Prepare("INSERT INTO manifest "
                                       "VALUES(1,'emuflow.xilinx-physical-"
                                       "handoff-sqlite/v1',?,?,?,?)");
            manifest.BindInteger(1, stats.clusters);
            manifest.BindInteger(2, stats.assignments);
            manifest.BindInteger(3, stats.cascades);
            manifest.BindInteger(4, stats.constants);
            manifest.Execute();
            transaction.Commit();
            device.RequireUnchanged();
            semantics.RequireUnchanged();
          }
          writer.Commit();
        }
        // A fresh device view and mapped cursor audit final published row
        // facts; they do not reuse the normalizer or the legalizer's accepted
        // decisions.
        std::string audit_text;
        {
          SqliteDatabase scratch(":memory:", SqliteDatabase::Mode::kReadOnly);
          PhysicalDevice device(scratch, inputs.Get("architecture"),
                                native ? inputs.Optional("native") : nullptr,
                                native ? inputs.Optional("provider") : nullptr,
                                cancelled);
          audit_text = AuditPhysicalHandoff(
              inputs.Get("mapped"),
              ArtifactRef::Bind(handoff_path.string(),
                                "xilinx-physical-handoff-v1"),
              device, inputs.Optional("constraints"), cancelled);
          device.RequireUnchanged();
        }
        const auto digest =
            FileHash(ArtifactRef::Bind(handoff_path.string(),
                                       "xilinx-physical-handoff-v1"),
                     cancelled);
        const auto audit = JsonFields(json, audit_text);
        Check(audit.count("packing") && audit.count("placement"),
              "independent audit result coverage disagrees");
        Object validation{{"packing", audit.at("packing")},
                          {"placement", audit.at("placement")},
                          {"source_ownership", Record(json, ownership_text)}};
        Object report{
            {"schema", "emuflow.openparf-atomic-physical-bridge-report/v1"},
            {"status", "pass"},
            {"implementation_id", "native-physical-handoff-bridge-v1"},
            {"top", top},
            {"physical_handoff",
             (destination / "physical-handoff.sqlite").string()},
            {"source",
             Object{{"mapped_sha256", mapped_sha},
                    {"architecture_sha256", architecture_sha},
                    {"openparf_atomic_placement_sha256", certificate_sha},
                    {"source_packed_sha256", atomic_sha},
                    {"constraints_sha256",
                     inputs.Optional("constraints")
                         ? JsonRecord(
                               FileHash(inputs.Get("constraints"), cancelled))
                         : JsonRecord()}}},
            {"packed_sha256", digest},
            {"placement_sha256", digest},
            {"physical_handoff_sha256", digest},
            {"cells", stats.assignments + stats.constants},
            {"placed_cells", stats.assignments},
            {"clusters", stats.clusters},
            {"site_pin_moved_ffs", moved},
            {"site_pin_repaired_sites", repaired_sites},
            {"cascade_chains", stats.cascades},
            {"validation", std::move(validation)},
            {"runtime_validation", runtime},
            {"runtime_breakdown",
             Object{
                 {"schema", "emuflow.openparf-bridge-runtime/v1"},
                 {"rows",
                  Object{{"mapped_cells", stats.assignments + stats.constants},
                         {"physical_clusters", stats.clusters},
                         {"placed_assignments", stats.assignments}}},
                 {"bytes",
                  Object{
                      {"placement_certificate",
                       inputs.Get("certificate_selector").generation().bytes},
                      {"placement_certificate_database",
                       inputs.Get("certificate").generation().bytes},
                      {"packed_output", 0},
                      {"placement_output", 0},
                      {"physical_handoff", fs::file_size(handoff_path)}}},
                 {"dataflow",
                  Object{
                      {"certificate", "native-typed-source-cursor"},
                      {"bridge_contracts", "native-single-owner-dual-view-v1"},
                      {"certificate_to_handoff", "native-validated-typed-rows"},
                      {"legalization_working_set", "one-bounded-site"},
                      {"certificate_assignment_ownership",
                       "native-source-certificate-ordered-merge-v1"},
                      {"handoff_publication",
                       "independently-audit-private-generation-before-atomic-"
                       "directory-publish"},
                      {"physical_contract_validation",
                       "native-complete-source-and-device-audit-v1"},
                      {"published_values", "lazy-typed-sqlite-views"},
                      {"site_pin_repairs", "typed-same-owner-records"},
                      {"validation_replay", false}}}}}};
        fs::remove(ownership_request);
        fs::remove(ownership_summary);
        PublishTextArtifact(
            (work.path / "physical-bridge-report.json").string(), refs,
            [&](std::ostream &out) {
              out << JsonRecord(report).Encode() << '\n';
            });
        Stop(cancelled);
        PublishNewDirectory(work.path.string(), destination.string(), refs);
        work.path.clear();
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(
                (fs::absolute(output_directory) / "physical-handoff.sqlite")
                    .string(),
                "xilinx-physical-handoff-v1"),
            ArtifactRef::Bind(
                (fs::absolute(output_directory) / "physical-bridge-report.json")
                    .string(),
                "physical-handoff-bridge-report-v1")};
      });
}
void AuditOpenparfSource(const ArtifactRef &selector, const ArtifactRef &store,
                         const volatile std::sig_atomic_t *flag) {
  Inputs inputs;
  inputs.refs = {selector, store};
  inputs.roles = {{"atomic_selector", 0}, {"atomic_source", 1}};
  SqliteDatabase json(":memory:", SqliteDatabase::Mode::kReadOnly);
  ValidateAtomicSource(inputs, json, flag);
}
} // namespace emuflow::core
