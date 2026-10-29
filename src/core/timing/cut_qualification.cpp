#include "timing/cut_qualification.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "ir/emuir_view.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "storage/schema/sta_path_schema.hpp"
#include "timing/cell_model.hpp"
#include "timing/opensta_export.hpp"
#include "timing/sta_paths.hpp"
#include <algorithm>
#include <filesystem>
#include <map>
#include <set>
#include <unistd.h>
namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
using U = std::uint32_t;
namespace fs = std::filesystem;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "cut timing qualification: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "cut timing qualification cancelled");
}
std::string Text(const std::string &value) {
  Check(!value.empty() && value.find('\0') == std::string::npos,
        "empty/NUL identity");
  return JsonQuote(value);
}
std::string Names(const std::set<std::string> &values) {
  std::string out = "[";
  for (const auto &value : values) {
    if (out.size() > 1)
      out += ',';
    out += Text(value);
  }
  return out + "]";
}
std::vector<std::string> Strings(const std::string &token) {
  std::vector<std::string> result;
  for (const auto &row : D::Array(token, 8192))
    result.push_back(DecodeJsonString(row));
  return result;
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Meta(SqliteDatabase &db, const std::string &owner,
                 const std::string &key, const std::string &fallback = "null") {
  auto q = db.Prepare("SELECT value FROM " + owner + ".metadata WHERE key=?");
  q.BindText(1, key);
  if (!q.Step())
    return fallback;
  return q.TypedText(0);
}
I Count(SqliteDatabase &db, const std::string &query) {
  auto q = db.Prepare(query);
  Check(q.Step(), "missing relation census");
  return q.TypedInteger(0);
}
void Attach(SqliteDatabase &db, const ArtifactRef &source,
            const std::string &alias) {
  auto q = db.Prepare("ATTACH DATABASE ? AS " + alias);
  q.BindText(1, SqliteImmutableUri(source.path()));
  q.Execute();
}
struct Digest {
  Sha256 hash;
  std::uint64_t records = 0;
  void Add(const std::string &json) {
    const auto size = std::uint64_t(json.size());
    unsigned char n[8];
    for (unsigned i = 0; i < 8; ++i)
      n[i] = (size >> (8 * (7 - i))) & 255;
    hash.update(n, 8);
    hash.update(reinterpret_cast<const unsigned char *>(json.data()),
                json.size());
    ++records;
  }
  std::string Finish() {
    std::string hex;
    for (auto c : hash.finish()) {
      hex += "0123456789abcdef"[c >> 4];
      hex += "0123456789abcdef"[c & 15];
    }
    return D::Encode(
        {{"count", std::to_string(records)}, {"sha256", JsonQuote(hex)}});
  }
};
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto name = (parent / ".cut-qualification-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create qualification workspace");
    root = name;
  }
  ~Work() {
    std::error_code error;
    fs::remove_all(root, error);
  }
};
struct Inputs {
  ArtifactRef request;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  std::string descriptor, semantic = "null";
  bool verify = true;
  explicit Inputs(const ArtifactRef &r) : request(r) {}
  void Read() {
    Check(request.schema() == "cut-qualification-request-v1",
          "request schema disagrees");
    D doc(D::Read(request));
    doc.Keys({"schema", "sources", "verify_payload"});
    Check(doc.Text("schema") == "emuflow.cut-qualification-request/v1",
          "request version disagrees");
    verify = doc.Boolean("verify_payload");
    sources = {request};
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    const std::map<std::string, std::string> schemas = {
        {"emuir", "emuir-compact-v2"},
        {"assignment", "assignment-index-v4"},
        {"selector", "sta-path-selector-v1"},
        {"paths", "timing-path-store-v1"},
        {"timing-model", "phase2-source-v1"},
        {"architecture-timing-db", "phase2-source-v1"},
        {"architecture-contract", "phase2-source-v1"}};
    for (const auto &token : D::Array(doc.Token("sources"), 7)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      const auto role = row.Text("role");
      Check(schemas.count(role) && row.Text("schema") == schemas.at(role) &&
                fs::path(row.Text("path")).is_absolute(),
            "unknown source role/schema");
      auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
      RequireEquivalentJson(compare, row.Token("generation"), Generation(ref),
                            "cut qualification source generation changed");
      Check(roles.emplace(role, ref).second, "duplicate source role");
      sources.push_back(ref);
    }
    for (const auto *role :
         {"emuir", "assignment", "selector", "paths", "timing-model"})
      Check(roles.count(role), std::string("missing source ") + role);
    Check(roles.count("architecture-timing-db") ==
              roles.count("architecture-contract"),
          "architecture source coverage disagrees");
    SqliteStore assignment(roles.at("assignment"));
    auto &db = assignment.database();
    descriptor = Meta(db, "main", "semantic_contract_binary");
    semantic = Meta(db, "main", "semantic_contract_sha256");
    if (descriptor != "null" || semantic != "null") {
      Check(descriptor != "null" && semantic != "null",
            "incomplete exact identity");
      D manifest(descriptor);
      Check(manifest.Text("artifact") == "assignment.exact.bin",
            "exact artifact must be an assignment sibling");
      auto ref = ArtifactRef::Bind(
          (fs::path(roles.at("assignment").path()).parent_path() /
           "assignment.exact.bin")
              .string(),
          "phase3-exact-data-v2");
      (void)ReadExactBinaryCounts(ref, descriptor);
      roles.emplace("exact", ref);
      sources.push_back(ref);
    }
  }
};
struct Cut {
  U net;
  std::string name;
  std::uint64_t direct = 0, top = 0;
  std::set<std::string> pins;
};
struct Edge {
  U from, to;
};
void Reach(U count, const std::vector<Edge> &edges,
           std::vector<unsigned char> &reached, bool reverse,
           const volatile std::sig_atomic_t *cancelled) {
  std::vector<std::uint64_t> offsets(std::size_t(count) + 1);
  for (const auto &e : edges)
    ++offsets[(reverse ? e.to : e.from) + 1];
  for (std::size_t i = 1; i < offsets.size(); ++i)
    offsets[i] += offsets[i - 1];
  std::vector<U> neighbors(edges.size());
  {
    auto next = offsets;
    for (const auto &e : edges)
      neighbors[next[reverse ? e.to : e.from]++] = reverse ? e.from : e.to;
  }
  std::vector<U> pending;
  for (U n = 0; n < count; ++n)
    if (reached[n])
      pending.push_back(n);
  while (!pending.empty()) {
    const auto node = pending.back();
    pending.pop_back();
    if ((node & 4095) == 0)
      Stop(cancelled);
    for (auto i = offsets[node]; i < offsets[node + 1]; ++i) {
      const auto sink = neighbors[i];
      if (!reached[sink]) {
        reached[sink] = 1;
        pending.push_back(sink);
      }
    }
  }
}
std::string Build(Inputs &in, const fs::path &temporary,
                  const volatile std::sig_atomic_t *cancelled) {
  EmuirView ir(in.roles.at("emuir"));
  auto &db = ir.store().database();
  AssignmentView assignment(in.roles.at("assignment"));
  Check(assignment.source_cells() == ir.counts().cells,
        "assignment/source cell namespace disagrees");
  (void)assignment.ValidateCuts();
  Check(ir.counts().nets < UINT32_MAX && ir.counts().cells < UINT32_MAX,
        "source dense coordinate limit exceeded");
  const U net_count = ir.counts().nets;
  {
    SqliteStore paths(in.roles.at("paths"));
    RequireSqliteDDL(paths.database(), kStaPathStoreDDL);
  }
  Attach(db, in.roles.at("assignment"), "assignment");
  Attach(db, in.roles.at("paths"), "timing");
  Check(DecodeJsonString(Meta(db, "assignment", "design")) ==
            D(Meta(db, "main", "design_json")).Text("name"),
        "assignment/source design disagrees");
  D selector(D::Read(in.roles.at("selector")));
  Check(selector.Text("schema") == "emuflow.sta-path-database/v3" &&
            selector.Text("design") ==
                D(Meta(db, "main", "design_json")).Text("name"),
        "path selector/source design disagrees");
  D payload(D(selector.Token("payloads")).Token("paths"));
  const auto member = payload.Text("path");
  Check(
      fs::path(member).filename() == member &&
          fs::canonical(fs::path(in.roles.at("selector").path()).parent_path() /
                        member) == in.roles.at("paths").path() &&
          payload.Text("format") == "sqlite-sta-path/v1" &&
          payload.Integer("records") ==
              Count(db, "SELECT COUNT(*) FROM timing.paths"),
      "path payload identity/count disagrees");
  if (in.verify) {
    Check(Sha256File(in.roles.at("paths"), cancelled) == payload.Text("sha256"),
          "path payload seal disagrees");
    const auto request = temporary / "audit.bin";
    CheckedRequestWriter writer(request.string(), in.sources);
    writer.Put(std::uint64_t(0x31505453464d5545ULL));
    writer.Put(std::uint32_t(1));
    writer.Generation(in.roles.at("emuir").generation());
    writer.Generation(in.roles.at("paths").generation());
    writer.Text(
        D::Encode({{"normalization", selector.Token("normalization")},
                   {"summary", selector.Token("summary")},
                   {"design", selector.Token("design")},
                   {"provider", JsonQuote("opensta-fpga-path-database-v1")}}));
    writer.Finish();
    ValidateStaPaths(
        {in.roles.at("emuir"), in.roles.at("paths"),
         ArtifactRef::Bind(request.string(), "sta-path-request-v1")},
        (temporary / "audit.json").string());
  }
  const auto cell_model =
      BuildTimingCellModel(db, in.roles.at("timing-model"),
                           in.roles.count("architecture-timing-db")
                               ? &in.roles.at("architecture-timing-db")
                               : nullptr,
                           in.roles.count("architecture-contract")
                               ? &in.roles.at("architecture-contract")
                               : nullptr);
  auto models = D(D(cell_model.json).Token("cells")).Fields();
  std::vector<Cut> cuts;
  std::vector<U> cut_index(net_count, UINT32_MAX);
  auto requested = db.Prepare(
      "SELECT c.net_id,n.seq,(SELECT COUNT(*) FROM endpoint_records e WHERE "
      "e.net_seq=n.seq AND e.role=0),(SELECT COUNT(*) FROM endpoint_records e "
      "WHERE e.net_seq=n.seq AND e.role=1 AND e.instance_seq IS NULL) FROM "
      "(SELECT DISTINCT net_id FROM assignment.cut_sinks) c LEFT JOIN "
      "net_records n ON n.id=c.net_id ORDER BY c.net_id");
  while (requested.Step()) {
    Check(!requested.IsNull(1), "cut net is absent from EmuIR");
    const auto net = requested.TypedInteger(1);
    Check(net >= 0 && std::uint64_t(net) < net_count &&
              requested.TypedInteger(2) == 1,
          "cut net must have one driver");
    Check(cuts.size() < UINT32_MAX && cut_index[net] == UINT32_MAX,
          "duplicate cut identity");
    cut_index[net] = U(cuts.size());
    cuts.push_back({U(net),
                    requested.TypedText(0),
                    0,
                    std::uint64_t(requested.TypedInteger(3)),
                    {}});
  }
  std::vector<Edge> edges;
  std::vector<unsigned char> launch(net_count), capture(net_count);
  auto cells =
      db.Prepare("SELECT "
                 "c.seq,c.id,c.type,json_type(p.value,'$.IS_C_INVERTED'),json_"
                 "extract(p.value,'$.IS_C_INVERTED'),k.value FROM cell_records "
                 "c LEFT JOIN payloads p ON p.seq=c.parameters_seq LEFT JOIN "
                 "payloads k ON k.seq=c.constants_seq ORDER BY c.seq");
  auto endpoints =
      db.Prepare("SELECT e.instance_seq,e.net_seq,p.name,e.bit,e.role FROM "
                 "endpoint_records e INDEXED BY endpoint_owner LEFT JOIN "
                 "pin_names p ON p.seq=e.port_seq WHERE e.instance_seq IS NOT "
                 "NULL ORDER BY e.instance_seq,e.port_seq,e.bit");
  bool next = endpoints.Step();
  U expected = 0;
  while (cells.Step()) {
    const auto seq = cells.TypedInteger(0);
    Check(seq == expected++, "cell sequence is not dense");
    if ((seq & 4095) == 0)
      Stop(cancelled);
    std::map<std::pair<std::string, I>, U> wires;
    std::map<std::string, I> widths;
    while (next && endpoints.TypedInteger(0) < seq)
      Check(false, "orphaned endpoint owner");
    while (next && endpoints.TypedInteger(0) == seq) {
      const auto net = endpoints.TypedInteger(1),
                 bit = endpoints.TypedInteger(3),
                 role = endpoints.TypedInteger(4);
      Check(!endpoints.IsNull(2) && net >= 0 &&
                std::uint64_t(net) < net_count && bit >= 0 &&
                (role == 0 || role == 1),
            "invalid endpoint coordinate");
      const auto pin = endpoints.TypedText(2);
      Text(pin);
      auto &value = wires[{pin, bit}];
      value = std::max(value, U(net));
      widths[pin] = std::max(widths[pin], bit);
      next = endpoints.Step();
    }
    Check(!cells.IsNull(5), "missing constant pin payload");
    for (const auto &token : D::Array(cells.TypedText(5), 8192)) {
      D c(token);
      const auto port = c.Text("port");
      const auto bit = c.Integer("bit");
      Check(!port.empty() && bit >= 0, "invalid constant pin");
      widths[port] = std::max(widths[port], bit);
    }
    Check(wires.size() <= 8192 && widths.size() <= 8192,
          "cell pin population exceeds bound");
    std::map<std::string, U> scalar;
    for (const auto &wire : wires) {
      const auto name =
          widths.at(wire.first.first) == 0
              ? wire.first.first
              : wire.first.first + "__" + std::to_string(wire.first.second);
      const auto inserted = scalar.emplace(name, wire.second);
      Check(inserted.second || inserted.first->second == wire.second,
            "ambiguous scalar pin");
    }
    auto type = cells.TypedText(2);
    const auto kind = cells.IsNull(3) ? "null" : cells.TypedText(3);
    if (cell_model.architecture &&
        std::set<std::string>{"FDCE", "FDPE", "FDRE", "FDSE"}.count(type) &&
        TimingParameterIsOne(kind,
                             kind == "integer" ? cells.TypedInteger(4) : 0,
                             kind == "text" ? cells.TypedText(4) : ""))
      type += "__NEG";
    Check(models.count(type), "primitive model is missing: " + type);
    D model(models.at(type));
    const auto category = model.Text("kind");
    const auto timed = [&](const std::string &pin) {
      const auto found = scalar.find(pin);
      if (found == scalar.end())
        return;
      const auto net = found->second;
      capture[net] = 1;
      if (cut_index[net] != UINT32_MAX) {
        auto &cut = cuts[cut_index[net]];
        ++cut.direct;
        cut.pins.insert(cells.TypedText(1) + "/" + pin);
      }
    };
    const auto start = [&](const std::string &pin) {
      const auto found = scalar.find(pin);
      if (found != scalar.end())
        launch[found->second] = 1;
    };
    if (category == "combinational") {
      std::set<std::string> controls;
      if (model.Has("controls")) {
        auto values = Strings(model.Token("controls"));
        controls.insert(values.begin(), values.end());
      }
      auto outputs = model.Has("outputs")
                         ? Strings(model.Token("outputs"))
                         : std::vector<std::string>{model.Text("output")};
      for (const auto &input : Strings(model.Token("inputs")))
        if (!controls.count(input) && scalar.count(input))
          for (const auto &output : outputs)
            if (scalar.count(output))
              edges.push_back({scalar.at(input), scalar.at(output)});
    } else if (category == "rising_edge_ff" || category == "falling_edge_ff") {
      start(model.Text("output"));
      timed(model.Text("data"));
    } else if (category == "rising_edge_bank") {
      for (const auto &pin : Strings(model.Token("outputs")))
        start(pin);
      for (const auto &pin : Strings(model.Token("inputs")))
        timed(pin);
    } else
      Check(category == "constant", "unsupported primitive timing kind");
  }
  Check(!next && expected == ir.counts().cells,
        "cell/endpoint population disagrees");
  Reach(net_count, edges, launch, false, cancelled);
  Reach(net_count, edges, capture, true, cancelled);
  Digest cut_identity, structural_identity;
  std::uint64_t timed = 0;
  for (const auto &cut : cuts) {
    const bool timing = launch[cut.net] && capture[cut.net];
    timed += timing;
    const auto structural =
        D::Encode({{"status", Text(timing ? "timed" : "no_timed_endpoint")},
                   {"direct_timed_endpoints", std::to_string(cut.direct)},
                   {"direct_timed_endpoint_pins", Names(cut.pins)},
                   {"direct_top_level_sinks", std::to_string(cut.top)}});
    cut_identity.Add(Text(cut.name));
    structural_identity.Add("[" + Text(cut.name) + "," + structural + "]");
  }
  Check(Count(db, "SELECT COUNT(*) FROM timing.paths WHERE path_id IS NULL OR "
                  "path_id='' ") == 0,
        "empty original path identity");
  Check(Count(db,
              "SELECT COUNT(*) FROM timing.path_nets n LEFT JOIN timing.paths "
              "p ON p.ordinal=n.path_ordinal WHERE p.ordinal IS NULL") == 0,
        "orphaned original path member");
  const auto associations =
      Count(db, "SELECT COUNT(*) FROM (SELECT DISTINCT p.path_ordinal,p.net_id "
                "FROM timing.path_nets p JOIN (SELECT DISTINCT net_id FROM "
                "assignment.cut_sinks) c ON c.net_id=p.net_id)");
  const auto associated =
      Count(db, "SELECT COUNT(*) FROM (SELECT DISTINCT p.net_id FROM "
                "timing.path_nets p JOIN (SELECT DISTINCT net_id FROM "
                "assignment.cut_sinks) c ON c.net_id=p.net_id)");
  O relations{
      {"cut_sinks",
       std::to_string(Count(db, "SELECT COUNT(*) FROM assignment.cut_sinks"))},
      {"exact_cut_nodes", "0"},
      {"exact_cut_source_segments", "0"},
      {"exact_consumer_segments", "0"}};
  if (in.roles.count("exact")) {
    D counts(ReadExactBinaryCounts(in.roles.at("exact"), in.descriptor));
    relations["exact_cut_nodes"] = counts.Token("cut_nodes");
    relations["exact_cut_source_segments"] = counts.Token("source_segments");
    relations["exact_consumer_segments"] = counts.Token("consumer_segments");
  }
  Stop(cancelled);
  return D::Encode(
      {{"schema", Text("emuflow.cut-segment-timing-qualification/v2")},
       {"status", Text("pass")},
       {"provider", Text("emuir-structural-cut-segment-qualification-v1")},
       {"qualification", Text("pre-route-structural-contract-bound")},
       {"delay_evidence", Text("contract-budget-provisional")},
       {"routed_deadline_gate",
        Text("required-phase7-endpoint-exact-or-conservative-bound")},
       {"storage", Text("indexed-summary")},
       {"semantic_contract_sha256", in.semantic},
       {"cut_net_identity", cut_identity.Finish()},
       {"structural_identity", structural_identity.Finish()},
       {"relation_counts", D::Encode(relations)},
       {"summary",
        D::Encode(
            {{"cut_nets", std::to_string(cuts.size())},
             {"timed_structural_nets", std::to_string(timed)},
             {"no_timed_endpoint_nets", std::to_string(cuts.size() - timed)},
             {"enumerated_member_associations", std::to_string(associations)},
             {"functional_only_nets",
              std::to_string(cuts.size() - associated)}})}});
}
} // namespace
StageResult QualifyCutSegments(const ArtifactRef &request,
                               const std::string &output,
                               const volatile std::sig_atomic_t *cancelled) {
  Inputs input(request);
  return RunStage(
      "timing.qualify-cuts",
      [&] {
        input.Read();
        return input.sources;
      },
      [&](const auto &sources) {
        Stop(cancelled);
        const auto target = fs::absolute(output);
        fs::create_directories(target.parent_path());
        Work work(target.parent_path());
        const auto report = Build(input, work.root, cancelled);
        PublishTextArtifact(
            target.string(), sources,
            [&](std::ostream &stream) { stream << report << '\n'; });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "cut-qualification-report-v2")};
      });
}
} // namespace emuflow::core
