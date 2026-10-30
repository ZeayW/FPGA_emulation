#include "transport/chimew/qualification.hpp"
#include "common/canonical_json.hpp"
#include "common/hash.hpp"
#include "common/json_record_stream.hpp"
#include "scheduling/schedule_validation.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <memory>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
namespace fs = std::filesystem;
void Check(bool ok, const std::string &message) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Chimew qualification: " + message);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "Chimew qualification cancelled");
}
O Fields(const std::string &token) {
  O result;
  VisitJsonObjectTokens(token, [&](const auto &k, const auto &v) {
    Check(result.emplace(k, v).second, "duplicate record member");
  });
  return result;
}
bool Close(double a, double b) {
  return std::isfinite(a) && std::isfinite(b) &&
         std::abs(a - b) <=
             std::max(1e-9, 1e-10 * std::max(std::abs(a), std::abs(b)));
}
void Hash(Sha256 &h, const std::string &s) {
  h.update(reinterpret_cast<const unsigned char *>(s.data()), s.size());
}
std::string Hex(Sha256 &h) {
  std::string out;
  for (auto c : h.finish()) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
std::string Digest(const std::string &s) {
  Sha256 h;
  Hash(h, s);
  return Hex(h);
}
std::string Number(double v) {
  Check(std::isfinite(v), "non-finite arithmetic");
  return JsonRecord(v).Encode();
}
void Nonempty(const std::string &s) { Check(!s.empty(), "empty identity"); }
std::string Sha(const std::string &s) {
  std::string out = s;
  for (auto &c : out)
    c = char(std::tolower(static_cast<unsigned char>(c)));
  Check(out.size() == 64 &&
            out.find_first_not_of("0123456789abcdef") == std::string::npos,
        "invalid SHA-256");
  return out;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto q = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value END "
                      "FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step() && !q.IsNull(0), "missing metadata " + key);
  auto value = q.TypedText(0);
  Check(!q.Step(), "duplicate metadata");
  return value;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".chimew-qualification-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create workspace");
    root = name;
  }
  ~Workspace() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
// A record spool holds current logical JSON inputs. Arrays are never loaded as
// a complete document, and canonical commitments retain the original key order
// contract independently of the producer or solver.
struct Document {
  SqliteDatabase &db;
  I id;
  O header, guard;
  std::set<std::string> arrays;
  std::string logical;
  bool streamed_guard = false;
  const volatile std::sig_atomic_t *cancelled;
  Document(SqliteDatabase &db, I id, const volatile std::sig_atomic_t *flag)
      : db(db), id(id), cancelled(flag) {}
  void Add(const std::string &field, I seq, const std::string &token) {
    auto q = db.Prepare("INSERT INTO records VALUES(?,?,?,?)");
    q.BindInteger(1, id);
    q.BindText(2, field);
    q.BindInteger(3, seq);
    q.BindText(4, CanonicalJsonToken(token));
    q.Execute();
  }
  template <class F> void Each(const std::string &field, F visit) const {
    Check(arrays.count(field), "missing array " + field);
    auto q = db.Prepare("SELECT seq,token FROM records WHERE document=? AND "
                        "field=? ORDER BY seq");
    q.BindInteger(1, id);
    q.BindText(2, field);
    I seq = 0;
    while (q.Step()) {
      Stop(cancelled);
      Check(q.TypedInteger(0) == seq, "noncontiguous records");
      visit(seq++, q.TypedText(1));
    }
  }
  void Read(const ArtifactRef &ref, bool split_guard = false) {
    std::ifstream in(ref.path());
    Check(bool(in), "cannot read input");
    JsonRecordStream stream(in, db, cancelled);
    stream.Object([&](const auto &key) {
      if (key == "entries" || key == "nets" || key == "bins") {
        Check(arrays.insert(key).second, "duplicate array");
        header.emplace(key, "[]");
        I seq = 0;
        stream.Array([&](const auto &token) { Add(key, seq++, token); });
      } else if (key == "timing_guard" && split_guard) {
        if (stream.ConsumeNull()) {
          header.emplace(key, "null");
          return;
        }
        // The protected list can itself be design-sized.
        streamed_guard = true;
        header.emplace(key, "{}");
        stream.Object([&](const auto &k) {
          if (k == "protected_entries") {
            arrays.insert("timing_guard.protected_entries");
            guard.emplace(k, "[]");
            I seq = 0;
            stream.Array([&](const auto &token) {
              Add("timing_guard.protected_entries", seq++, token);
            });
          } else
            guard.emplace(k, CanonicalJsonToken(stream.Value()));
        });
      } else
        header.emplace(key, CanonicalJsonToken(stream.Value()));
    });
    stream.Finish();
    ref.RequireUnchanged();
  }
  void HashArray(Sha256 &h, const std::string &field) const {
    Hash(h, "[");
    Each(field, [&](I seq, const auto &token) {
      if (seq)
        Hash(h, ",");
      Hash(h, token);
    });
    Hash(h, "]");
  }
  std::string Commitment() const {
    if (!logical.empty())
      return logical;
    Sha256 h;
    Hash(h, "{");
    bool comma = false;
    for (const auto &p : header) {
      if (comma)
        Hash(h, ",");
      comma = true;
      Hash(h, JsonQuote(p.first) + ":");
      if (arrays.count(p.first))
        HashArray(h, p.first);
      else if (p.first == "timing_guard" && streamed_guard) {
        Hash(h, "{");
        bool gc = false;
        for (const auto &g : guard) {
          if (gc)
            Hash(h, ",");
          gc = true;
          Hash(h, JsonQuote(g.first) + ":");
          if (g.first == "protected_entries")
            HashArray(h, "timing_guard.protected_entries");
          else
            Hash(h, g.second);
        }
        Hash(h, "}");
      } else
        Hash(h, p.second);
    }
    Hash(h, "}");
    return Hex(h);
  }
  D Header() const {
    O bounded = header;
    for (const auto &f : arrays)
      bounded.erase(f);
    bounded.erase("timing_guard");
    return D(D::Encode(bounded));
  }
};
using Domain = std::tuple<std::string, std::string, std::string>;
using Lane = std::tuple<std::string, std::string, std::string, I>;
struct Signal {
  Domain domain;
  I ratio = 1, lane = 0, slot = 0;
  bool explicit_slot = false;
  std::uint64_t encoding = 0;
  double y = 0;
  I initial = -1, refined = -1;
};
using Signals = std::map<std::string, Signal>;
struct Chain {
  const volatile std::sig_atomic_t *cancelled;
  SqliteDatabase db;
  std::vector<ArtifactRef> sources;
  std::map<std::string, std::unique_ptr<Document>> docs;
  std::map<std::string, ArtifactRef> roles;
  Signals signals;
  std::string design, platform;
  O artifacts;
  std::set<Lane> protected_lanes;
  bool guarded = false;
  double rudy_peak = 0;
  I rudy_overloaded = 0, groups = 0;
  std::string certificate;
  explicit Chain(const fs::path &work, const volatile std::sig_atomic_t *flag)
      : cancelled(flag), db((work / "records.sqlite").string(),
                            SqliteDatabase::Mode::kCreateExclusive) {
    db.Execute(
        "CREATE TABLE records(document INTEGER,field TEXT,seq INTEGER,token "
        "TEXT,PRIMARY KEY(document,field,seq)) WITHOUT ROWID");
  }
  ArtifactRef Bind(const std::string &path, const std::string &schema) {
    Check(!fs::is_symlink(path), "input selector is a symlink");
    auto ref = ArtifactRef::Bind(path, schema);
    sources.push_back(ref);
    return ref;
  }
  void Read(const ArtifactRef &request) {
    sources = {request};
    D input(D::Read(request));
    input.Keys(
        input.Has("source_binding")
            ? std::set<std::string>{"schema", "artifacts", "source_binding"}
            : std::set<std::string>{"schema", "artifacts"});
    Check(input.Text("schema") == "emuflow.chimew-qualification-input/v1",
          "request schema differs");
    const std::set<std::string> required{
        "schedule",    "crossings",          "initial_grouping",
        "positions",   "refined_grouping",   "rudy_input",
        "rudy_report", "bank_channel_input", "bank_channel_report"};
    for (const auto &role : Fields(input.Token("artifacts"))) {
      Check(required.count(role.first), "unknown artifact role");
      D source(role.second);
      source.Keys({"path", "schema"});
      const auto expected = role.first == "bank_channel_input"
                                ? "chimew-bank-input-selector-v1"
                            : role.first == "bank_channel_report"
                                ? "chimew-bank-report-selector-v1"
                                : "physical-input-document-v1";
      Check(source.Text("schema") == expected, "artifact role schema differs");
      roles.emplace(role.first, Bind(source.Text("path"), expected));
      if (role.first.rfind("bank_channel_", 0) != 0) {
        auto document =
            std::make_unique<Document>(db, I(docs.size()), cancelled);
        document->Read(roles.at(role.first), role.first == "crossings");
        docs.emplace(role.first, std::move(document));
      }
    }
    Check(roles.size() == required.size(), "artifact chain is incomplete");
    ReadSchedule();
    Crossings();
    Positions();
    Grouping(false);
    Grouping(true);
    Rudy();
    std::map<I, std::set<std::string>> expected_members;
    for (const auto &p : signals)
      expected_members[p.second.refined].insert(p.first);
    std::set<I> covered;
    auto bank = ValidateChimewBankAssignment(
        roles.at("bank_channel_input"), roles.at("bank_channel_report"),
        [&](std::size_t, const auto &raw) {
          D group(raw);
          std::set<std::string> members;
          std::optional<I> owner;
          VisitJsonArrayTokens(group.Token("members"), [&](const auto &token) {
            D member(token);
            auto id = member.Text("id");
            Check(signals.count(id) && members.insert(id).second,
                  "bank groups do not match refined signal groups");
            const auto &s = signals.at(id);
            if (owner)
              Check(*owner == s.refined,
                    "bank groups do not match refined signal groups");
            else
              owner = s.refined;
            D point(member.Token("fanout"));
            Check(Close(point.Number("y"), s.y),
                  "bank input fanout y does not match lookahead placement");
          });
          Check(owner && covered.insert(*owner).second &&
                    members == expected_members.at(*owner),
                "bank groups do not match refined signal groups");
        },
        cancelled);
    sources.insert(sources.end(), bank.inputs.begin(), bank.inputs.end());
    Check(covered.size() == expected_members.size() && bank.design == design &&
              bank.platform == platform,
          "bank grouping coverage or identity differs");
    D bp(bank.provenance);
    auto position = docs.at("positions")->Header(),
         rudy = docs.at("rudy_input")->Header();
    D pp(position.Token("provenance")), rp(rudy.Token("provenance"));
    auto refined = docs.at("refined_grouping")->Header();
    Check(CanonicalJsonToken(refined.Token("placement_provenance")) ==
                  CanonicalJsonToken(position.Token("provenance")) &&
              Sha(rp.Text("placement_sha256")) ==
                  Sha(pp.Text("placement_sha256")) &&
              bp.Text("placement_sha256") == pp.Text("placement_sha256") &&
              bp.Text("architecture_sha256") ==
                  Sha(rp.Text("architecture_sha256")) &&
              bp.Text("grouping_sha256") ==
                  docs.at("refined_grouping")->Commitment(),
          "lookahead provenance chain is broken");
    for (const auto &p : docs)
      artifacts[p.first] = JsonQuote(p.second->Commitment());
    artifacts["bank_channel_input"] = JsonQuote(bank.input_sha256);
    artifacts["bank_channel_report"] = JsonQuote(bank.report_sha256);
    D crossing(docs.at("crossings")->Header().Token("provenance"));
    O provenance{{"routing_sha256", crossing.Token("routing_sha256")},
                 {"placement_sha256", pp.Token("placement_sha256")},
                 {"netlist_sha256", JsonQuote(Sha(rp.Text("netlist_sha256")))},
                 {"architecture_sha256",
                  JsonQuote(Sha(rp.Text("architecture_sha256")))}};
    bool bound = input.Has("source_binding");
    O cert{
        {"schema", JsonQuote(bound ? "emuflow.chimew-phase6-qualification/v2"
                                   : "emuflow.chimew-phase6-qualification/v1")},
        {"status", JsonQuote("pass")},
        {"design", JsonQuote(design)},
        {"platform", JsonQuote(platform)},
        {"provider",
         JsonQuote(
             bound ? "chimew-paper-kernel-chain-plus-emuflow-byte-provenance-v2"
                   : "chimew-paper-kernel-chain-plus-emuflow-provenance-v1")},
        {"claim_boundary",
         JsonQuote("paper kernels plus EmuFlow provenance/legality closure; "
                   "vendor DRC, timing sign-off, bitstream, and hardware "
                   "qualification remain external")},
        {"provenance", D::Encode(provenance)},
        {"artifacts", D::Encode(artifacts)},
        {"metrics",
         D::Encode({{"signals", std::to_string(signals.size())},
                    {"groups", std::to_string(expected_members.size())},
                    {"rudy_peak_utilization",
                     D(docs.at("rudy_report")->Header().Token("metrics"))
                         .Token("peak_utilization")},
                    {"rudy_overloaded_bins", std::to_string(rudy_overloaded)},
                    {"certified_matchings",
                     D(bank.metrics).Token("certified_matchings")},
                    {"artifact_chain_disagreements", "0"}})}};
    if (bound) {
      auto binding = SourceBinding(input.Token("source_binding"), provenance);
      cert["source_binding"] = binding;
      cert["source_binding_sha256"] = JsonQuote(Digest(binding));
    }
    cert["qualification_sha256"] = JsonQuote(Digest(D::Encode(cert)));
    certificate = D::Encode(cert);
  }
  void Identity(D &doc, const char *schema, const char *provider = nullptr) {
    Check(doc.Text("schema") == schema &&
              (!provider || doc.Text("provider") == provider),
          "artifact schema/provider differs");
    Check(doc.Text("design") == design && doc.Text("platform") == platform,
          "artifact identity differs from schedule");
  }
  void ReadSchedule() {
    auto &doc = *docs.at("schedule");
    auto head = doc.Header();
    if (head.Text("schema") == "emuflow.tdm-schedule-storage/v4") {
      auto selector =
          Bind(roles.at("schedule").path(), "tdm-schedule-selector-v4");
      const auto digest = Sha(head.Text("store_sha256"));
      Check(head.Text("artifact") == "schedule." + digest + ".sqlite",
            "schedule escapes owner");
      auto store =
          Bind((fs::path(selector.path()).parent_path() / head.Text("artifact"))
                   .string(),
               "tdm-schedule-store-v4");
      Check(head.Integer("store_bytes") >= 0 &&
                std::uint64_t(head.Integer("store_bytes")) ==
                    store.generation().bytes &&
                Sha256File(store, cancelled) == digest,
            "schedule content seal differs");
      SqliteStore owner(store);
      auto &data = owner.database();
      auto logical = Meta(data, "logical_sha256"),
           pretty = head.Token("logical_pretty_sha256");
      RequireEquivalentJson(db, head.Token("logical_sha256"), logical,
                            "schedule logical binding differs");
      AuditScheduleStoreSeal(data, head.Token("sections"),
                             DecodeJsonString(logical),
                             DecodeJsonString(pretty));
      doc.header = Fields(Meta(data, "header"));
      doc.logical = DecodeJsonString(logical);
      doc.arrays = {"entries"};
      auto q =
          data.Prepare("SELECT "
                       "seq,entry_id,link_id,source_fpga,sink_fpga,tdm_ratio,"
                       "lane,slot FROM schedule_entries ORDER BY seq");
      while (q.Step()) {
        O row{{"id", JsonQuote(q.TypedText(1))},
              {"link", JsonQuote(q.TypedText(2))},
              {"from", JsonQuote(q.TypedText(3))},
              {"to", JsonQuote(q.TypedText(4))},
              {"lane", std::to_string(q.TypedInteger(6))},
              {"slot", std::to_string(q.TypedInteger(7))}};
        if (!q.IsNull(5))
          row["tdm_ratio"] = std::to_string(q.TypedInteger(5));
        doc.Add("entries", q.TypedInteger(0), D::Encode(row));
      }
    }
    auto effective = doc.Header();
    Check(effective.Text("schema") == "emuflow.tdm-schedule/v1" &&
              !doc.header.count("chimew_timing_guard"),
          "schedule schema/guard owner differs");
    design = effective.Text("design");
    platform = effective.Text("platform");
    Nonempty(design);
    Nonempty(platform);
    doc.Each("entries", [&](I, const auto &token) {
      D e(token);
      Signal s;
      s.domain = {e.Text("link"), e.Text("from"), e.Text("to")};
      s.ratio = e.Has("tdm_ratio") ? e.Integer("tdm_ratio") : 1;
      s.lane = e.Integer("lane");
      s.explicit_slot = e.Has("slot");
      s.slot = s.explicit_slot ? e.Integer("slot") : 0;
      auto id = e.Text("id");
      Nonempty(id);
      Check(s.ratio > 0 && s.lane >= 0 && s.slot >= 0 &&
                signals.emplace(id, s).second,
            "invalid schedule entry");
    });
  }
  void Crossings() {
    auto &doc = *docs.at("crossings");
    auto h = doc.Header();
    Check(h.Text("schema") == "emuflow.chimew-crossing-encodings/v1",
          "crossing schema differs");
    const auto provider = h.Text("provider");
    Check(provider == "source-qualified-physical-sll-routing-v1" ||
              (provider == "academic-virtual-region-routing-lookahead-v1" &&
               h.Text("qualification") == "academic-virtual-region-lookahead" &&
               h.Text("coordinate_system") == "normalized-placement-y"),
          "crossing policy differs");
    Check(h.Text("design") == design && h.Text("platform") == platform,
          "crossing identity differs");
    I bits = h.Integer("slls_per_fpga");
    Check(bits > 0 && bits <= 31, "SLL width out of range");
    D provenance(h.Token("provenance"));
    Nonempty(provenance.Text("producer"));
    Nonempty(provenance.Text("producer_version"));
    Sha(provenance.Text("routing_sha256"));
    std::set<std::string> seen;
    I total = 0;
    doc.Each("entries", [&](I, const auto &token) {
      D row(token);
      auto id = row.Text("schedule_entry");
      Check(signals.count(id) && seen.insert(id).second,
            "crossing coverage differs");
      std::uint64_t encoding = 0;
      for (const auto &field :
           std::vector<std::string>{"source_slls", "sink_slls"}) {
        std::set<I> indexes;
        std::uint64_t mask = 0;
        VisitJsonArrayTokens(row.Token(field), [&](const auto &v) {
          I value = D::IntegerToken(v);
          Check(value >= 0 && value < bits && indexes.insert(value).second,
                "invalid SLL index");
          mask |= std::uint64_t(1) << value;
        });
        encoding |= mask << (field == "source_slls" ? 0 : bits);
      }
      Check(row.Integer("encoding") >= 0 &&
                std::uint64_t(row.Integer("encoding")) == encoding,
            "crossing encoding is not independently derived");
      signals.at(id).encoding = encoding;
      total += __builtin_popcountll(encoding);
    });
    D metrics(h.Token("metrics"));
    Check(seen.size() == signals.size() &&
              metrics.Integer("signals") == I(signals.size()) &&
              metrics.Integer("physical_sll_crossings") == total,
          "crossing metrics or coverage differs");
    guarded = doc.header.count("timing_guard") &&
              doc.header.at("timing_guard") != "null";
    if (guarded) {
      D guard(D::Encode(doc.guard));
      Check(guard.Text("provider") == "emuflow-chimew-critical-lane-guard-v1" &&
                guard.Text("scope") ==
                    "EmuFlow extension, not a Chimew paper claim",
            "invalid timing guard policy");
      std::string previous;
      doc.Each("timing_guard.protected_entries", [&](I, const auto &v) {
        auto id = DecodeJsonString(v);
        Check(!id.empty() && (previous.empty() || previous < id) &&
                  signals.count(id),
              "invalid timing guard entries");
        previous = id;
        const auto &s = signals.at(id);
        protected_lanes.insert({std::get<0>(s.domain), std::get<1>(s.domain),
                                std::get<2>(s.domain), s.lane});
      });
    }
  }
  void Positions() {
    auto &doc = *docs.at("positions");
    auto h = doc.Header();
    Identity(h, "emuflow.chimew-lookahead-positions/v1",
             "source-qualified-physical-site-lookahead-v1");
    Check(h.Text("coordinate_system") == "physical-site-y",
          "normalized y positions rejected");
    D p(h.Token("provenance"));
    Nonempty(p.Text("producer"));
    Nonempty(p.Text("producer_version"));
    Sha(p.Text("placement_sha256"));
    std::set<std::string> seen;
    doc.Each("entries", [&](I, const auto &token) {
      D r(token);
      auto id = r.Text("schedule_entry");
      Check(signals.count(id) && seen.insert(id).second,
            "position coverage differs");
      signals.at(id).y = r.Number("source_y");
    });
    Check(seen.size() == signals.size() &&
              D(h.Token("metrics")).Integer("signals") == I(signals.size()),
          "position metrics or coverage differs");
  }
  double Objective(bool after) {
    std::map<I, std::vector<double>> values;
    for (const auto &p : signals)
      values[after ? p.second.refined : p.second.initial].push_back(p.second.y);
    double objective = 0;
    for (auto &p : values) {
      std::sort(p.second.begin(), p.second.end());
      double prefix = 0;
      I index = 0;
      for (auto y : p.second) {
        objective += y * index++ - prefix;
        prefix += y;
      }
    }
    Check(std::isfinite(objective), "non-finite position objective");
    return objective;
  }
  void Grouping(bool after) {
    auto &doc = *docs.at(after ? "refined_grouping" : "initial_grouping");
    auto h = doc.Header();
    Identity(h,
             after ? "emuflow.chimew-refined-signal-groups/v1"
                   : "emuflow.chimew-signal-groups/v1",
             after ? "chimew-section3.3.2-bounded-inference-v1"
                   : "chimew-algorithm1-encoding-grouping-v1");
    std::set<std::string> seen;
    doc.Each("entries", [&](I, const auto &token) {
      D r(token);
      auto id = r.Text("schedule_entry");
      I group = r.Integer("group");
      Check(group >= 0 && signals.count(id) && seen.insert(id).second,
            "group coverage differs");
      auto &s = signals.at(id);
      Check(r.Integer("encoding") >= 0 &&
                std::uint64_t(r.Integer("encoding")) == s.encoding,
            "group encoding differs");
      if (after) {
        Check(Close(r.Number("source_y"), s.y),
              "refined source position differs");
        s.refined = group;
      } else {
        s.initial = group;
        if (guarded) {
          Lane lane{std::get<0>(s.domain), std::get<1>(s.domain),
                    std::get<2>(s.domain), s.lane};
          std::string expected = "null";
          if (protected_lanes.count(lane))
            expected = JsonRecord(std::vector<JsonRecord>{
                                      std::get<0>(lane), std::get<1>(lane),
                                      std::get<2>(lane), std::get<3>(lane)})
                           .Encode();
          RequireEquivalentJson(db,
                                r.Has("timing_guard_lane")
                                    ? r.Token("timing_guard_lane")
                                    : "null",
                                expected,
                                "Chimew initial groups do not preserve "
                                "declared lookahead guards");
        }
      }
    });
    Check(seen.size() == signals.size(), "groups must cover schedule exactly");
    std::map<I, std::vector<const Signal *>> members;
    std::map<I, std::uint64_t> masks;
    for (const auto &p : signals) {
      I group = after ? p.second.refined : p.second.initial;
      members[group].push_back(&p.second);
      masks[group] |= p.second.encoding;
    }
    I crossings = 0, moved = 0;
    for (const auto &p : members) {
      const auto &first = *p.second.front();
      std::set<I> slots;
      I explicit_slots = 0;
      for (const auto *s : p.second) {
        Check(s->domain == first.domain && s->ratio == first.ratio,
              "group crosses a grouping domain");
        if (s->explicit_slot) {
          ++explicit_slots;
          slots.insert(s->slot);
        }
      }
      Check(p.second.size() <= std::size_t(first.ratio),
            "group exceeds its TDM ratio");
      if (!after && explicit_slots)
        Check(explicit_slots == I(p.second.size()) &&
                  slots.size() == p.second.size(),
              "group reuses a frozen Phase 5 slot");
      crossings += __builtin_popcountll(masks[p.first]);
    }
    D metrics(h.Token("metrics"));
    Check(metrics.Integer("signals") == I(signals.size()) &&
              metrics.Integer("groups") == I(members.size()) &&
              metrics.Integer("group_physical_sll_crossings") == crossings,
          "group metrics differ");
    if (after) {
      std::map<I, std::uint64_t> initial;
      for (const auto &p : signals) {
        initial[p.second.initial] |= p.second.encoding;
        moved += p.second.initial != p.second.refined;
      }
      Check(initial == masks, "refinement changed a group SLL encoding");
      double before = Objective(false), value = Objective(true);
      Check(Close(metrics.Number("pairwise_source_y_before"), before) &&
                Close(metrics.Number("pairwise_source_y_after"), value) &&
                metrics.Integer("moved_signals") == moved &&
                metrics.Integer("oracle_disagreements") == 0 &&
                value <= before + 1e-9,
            "refined grouping metrics or objective differ");
      groups = I(members.size());
    }
  }
  void Rudy() {
    auto &input = *docs.at("rudy_input");
    auto &report = *docs.at("rudy_report");
    auto h = input.Header(), rh = report.Header();
    Identity(h, "emuflow.chimew-rudy-input/v1",
             "source-qualified-lookahead-placement-rudy-v1");
    Identity(rh, "emuflow.chimew-rudy-report/v1",
             "chimew-section2.3-rudy-gate-v1");
    Check(h.Text("coordinate_system") == "physical-site-xy" &&
              h.Text("degenerate_bbox_policy") == "reject",
          "invalid RUDY physical policy");
    D p(h.Token("provenance"));
    O provenance{{"producer", p.Token("producer")},
                 {"producer_version", p.Token("producer_version")}};
    Nonempty(p.Text("producer"));
    Nonempty(p.Text("producer_version"));
    for (auto key :
         {"placement_sha256", "netlist_sha256", "architecture_sha256"})
      provenance[key] = JsonQuote(Sha(p.Text(key)));
    RequireEquivalentJson(db, rh.Token("provenance"), D::Encode(provenance),
                          "Chimew RUDY report provenance does not agree");
    D grid(h.Token("grid"));
    double ox = grid.Number("origin_x"), oy = grid.Number("origin_y"),
           bw = grid.Number("bin_width"), bh = grid.Number("bin_height"),
           pitch = h.Number("wire_pitch_per_layer"),
           maximum = h.Number("max_utilization");
    I cols = grid.Integer("columns"), rows = grid.Integer("rows");
    Check(cols > 0 && rows > 0 && cols <= INT32_MAX / rows && bw > 0 &&
              bh > 0 && pitch > 0 && maximum > 0,
          "invalid RUDY grid");
    std::vector<double> capacities, loads(std::size_t(cols * rows), 0);
    VisitJsonArrayTokens(grid.Token("capacities"), [&](const auto &v) {
      double value = ParseFiniteDecimal(v, "capacity");
      Check(value > 0 && capacities.size() < loads.size(), "invalid capacity");
      capacities.push_back(value);
    });
    Check(capacities.size() == loads.size(), "RUDY capacity coverage differs");
    std::vector<JsonRecord> capacity_values;
    for (auto c : capacities)
      capacity_values.emplace_back(c);
    O normalized{
        {"origin", JsonRecord(std::vector<JsonRecord>{ox, oy}).Encode()},
        {"bin", JsonRecord(std::vector<JsonRecord>{bw, bh}).Encode()},
        {"shape", JsonRecord(std::vector<JsonRecord>{cols, rows}).Encode()},
        {"wire_pitch", Number(pitch)},
        {"max_utilization", Number(maximum)},
        {"provenance", D::Encode(provenance)},
        {"capacities", JsonRecord(capacity_values).Encode()},
        {"nets", "[]"}};
    // Canonical normalized net records are spooled beside the original input.
    input.arrays.insert("normalized_nets");
    std::set<std::string> ids;
    I nets = 0, pins = 0;
    double wire_area = 0;
    input.Each("nets", [&](I seq, const auto &token) {
      D net(token);
      auto id = net.Text("id");
      Nonempty(id);
      Check(ids.insert(id).second, "duplicate RUDY net");
      double lx = std::numeric_limits<double>::infinity(), ly = lx, ux = -lx,
             uy = -lx;
      std::vector<JsonRecord> points;
      VisitJsonArrayTokens(net.Token("pins"), [&](const auto &value) {
        D point(value);
        double x = point.Number("x"), y = point.Number("y");
        Check(x >= ox && x <= ox + cols * bw && y >= oy && y <= oy + rows * bh,
              "RUDY pin lies outside grid");
        lx = std::min(lx, x);
        ly = std::min(ly, y);
        ux = std::max(ux, x);
        uy = std::max(uy, y);
        points.emplace_back(std::vector<JsonRecord>{x, y});
      });
      Check(points.size() >= 2 && ux > lx && uy > ly,
            "RUDY net has a zero-area bounding box");
      pins += I(points.size());
      ++nets;
      input.Add("normalized_nets", seq,
                D::Encode({{"id", JsonQuote(id)},
                           {"pins", JsonRecord(points).Encode()}}));
      double width = ux - lx, height = uy - ly, area = (width + height) * pitch,
             density = area / (width * height);
      Check(std::isfinite(density), "non-finite RUDY density");
      wire_area += area;
      I c0 = std::max<I>(0, I(std::floor((lx - ox) / bw))),
        c1 = std::min<I>(cols - 1, I(std::ceil((ux - ox) / bw)) - 1),
        r0 = std::max<I>(0, I(std::floor((ly - oy) / bh))),
        r1 = std::min<I>(rows - 1, I(std::ceil((uy - oy) / bh)) - 1);
      for (I r = r0; r <= r1; ++r) {
        double by = oy + r * bh,
               y = std::max(0.0, std::min(uy, by + bh) - std::max(ly, by));
        for (I c = c0; c <= c1; ++c) {
          double bx = ox + c * bw,
                 x = std::max(0.0, std::min(ux, bx + bw) - std::max(lx, bx));
          loads[std::size_t(r * cols + c)] += density * x * y;
        }
      }
    });
    Check(nets > 0, "RUDY requires nets");
    D im(h.Token("metrics"));
    Check(im.Integer("nets") == nets && im.Integer("pins") == pins,
          "RUDY input metrics differ");
    Sha256 hash;
    Hash(hash, "{");
    bool comma = false;
    for (const auto &p : normalized) {
      if (comma)
        Hash(hash, ",");
      comma = true;
      Hash(hash, JsonQuote(p.first) + ":");
      if (p.first == "nets")
        input.HashArray(hash, "normalized_nets");
      else
        Hash(hash, p.second);
    }
    Hash(hash, "}");
    Check(rh.Text("input_sha256") == Hex(hash),
          "RUDY report provenance does not agree");
    double total = 0, peak = 0;
    I bins = 0;
    rudy_peak = 0;
    rudy_overloaded = 0;
    report.Each("bins", [&](I seq, const auto &token) {
      Check(std::size_t(seq) < loads.size(), "RUDY bin coverage differs");
      D bin(token);
      double load = loads[std::size_t(seq)],
             capacity = capacities[std::size_t(seq)], util = load / capacity;
      Check(bin.Integer("column") == seq % cols &&
                bin.Integer("row") == seq / cols &&
                Close(bin.Number("capacity"), capacity) &&
                Close(bin.Number("load"), load) &&
                Close(bin.Number("utilization"), util),
            "RUDY bin does not agree");
      total += load;
      peak = std::max(peak, load);
      rudy_peak = std::max(rudy_peak, util);
      rudy_overloaded += util > maximum + 1e-12;
      ++bins;
    });
    Check(std::size_t(bins) == loads.size(), "RUDY bins do not cover grid");
    D m(rh.Token("metrics"));
    Check(m.Integer("nets") == nets && m.Integer("pins") == pins &&
              Close(m.Number("total_wire_area"), wire_area) &&
              Close(m.Number("total_bin_load"), total) &&
              Close(m.Number("peak_load"), peak) &&
              Close(m.Number("peak_utilization"), rudy_peak) &&
              m.Integer("overloaded_bins") == rudy_overloaded &&
              m.Integer("oracle_disagreements") == 0,
          "RUDY report metrics do not agree");
    Check(rh.Text("gate_status") == (rudy_overloaded ? "rejected" : "pass"),
          "RUDY gate status does not agree");
    Check(!rudy_overloaded, "RUDY qualification gate did not pass");
  }
  std::string SourceBinding(const std::string &token, const O &provenance) {
    D binding(token);
    binding.Keys({"scope", "digests"});
    Check(binding.Text("scope") == "byte-bound-source-artifacts",
          "invalid source binding scope");
    O digests = Fields(binding.Token("digests"));
    std::set<std::string> required{"routing", "placement", "netlist",
                                   "architecture", "package_pins"};
    for (const auto &p : digests) {
      Check(required.count(p.first) || p.first == "timing_paths",
            "unknown source binding");
      auto sha = DecodeJsonString(p.second);
      Check(sha == Sha(sha), "source binding must be lowercase");
      required.erase(p.first);
      if (provenance.count(p.first + "_sha256"))
        Check(p.second == provenance.at(p.first + "_sha256"),
              "source binding provenance does not agree");
    }
    Check(required.empty(), "source binding inventory is incomplete");
    return D::Encode({{"scope", JsonQuote("byte-bound-source-artifacts")},
                      {"digests", D::Encode(digests)}});
  }
};
} // namespace
StageResult QualifyChimewChain(const ArtifactRef &request,
                               const std::string &output,
                               const volatile std::sig_atomic_t *cancelled) {
  std::optional<Workspace> workspace;
  std::unique_ptr<Chain> chain;
  return RunStage(
      "phase6.qualify-chimew-chain",
      [&] {
        auto parent = fs::path(output).parent_path();
        if (parent.empty())
          parent = ".";
        fs::create_directories(parent);
        workspace.emplace(parent);
        chain = std::make_unique<Chain>(workspace->root, cancelled);
        chain->Read(request);
        return chain->sources;
      },
      [&](const auto &refs) {
        PublishTextArtifact(output, refs, [&](std::ostream &out) {
          out << chain->certificate << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "chimew-qualification-v1")};
      });
}
} // namespace emuflow::core
