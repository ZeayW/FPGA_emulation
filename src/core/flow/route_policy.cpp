#include "flow/route_policy.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "routing/policy.hpp"
#include "storage/store.hpp"
#include "timing/cut_qualification.hpp"
#include "timing/sta_projection.hpp"
#include "transport/transport.hpp"
#include <filesystem>
#include <optional>
#include <set>
#include <unistd.h>
namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
namespace fs = std::filesystem;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "native routing preparation: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "routing preparation cancelled");
}
std::string Array(const std::vector<std::string> &values) {
  std::string out = "[";
  for (const auto &v : values) {
    if (out.size() > 1)
      out += ',';
    out += v;
  }
  return out + "]";
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Receipt(const ArtifactRef &ref) {
  std::vector<JsonRecord> values;
  const auto &g = ref.generation();
  for (const auto &n :
       {std::to_string(g.device), std::to_string(g.inode),
        std::to_string(g.bytes), std::to_string(g.modified_seconds),
        std::to_string(g.modified_nanoseconds),
        std::to_string(g.changed_seconds),
        std::to_string(g.changed_nanoseconds)})
    values.emplace_back(n);
  return JsonRecord(std::vector<JsonRecord>{"tuple", values}).Encode();
}
std::string Path(const fs::path &p) {
  return "[\"path\"," + JsonQuote(p.string()) + "]";
}
std::string Record(const std::string &role, const ArtifactRef &ref) {
  return D::Encode({{"role", JsonQuote(role)},
                    {"path", JsonQuote(ref.path())},
                    {"schema", JsonQuote(ref.schema())},
                    {"generation", Generation(ref)}});
}
std::string Pack(const std::string &token) {
  if (token[0] == '{') {
    O fields;
    for (const auto &f : D(token).Fields())
      fields[f.first] = Pack(f.second);
    return "[\"map\"," + D::Encode(fields) + "]";
  }
  if (token[0] == '[') {
    std::vector<std::string> rows;
    for (const auto &v : D::Array(token))
      rows.push_back(Pack(v));
    return "[\"list\"," + Array(rows) + "]";
  }
  return token;
}
std::string Unpack(const std::string &token) {
  if (token[0] != '[')
    return token;
  auto parts = D::Array(token, 2);
  Check(parts.size() == 2, "invalid packed state");
  const auto kind = DecodeJsonString(parts[0]);
  if (kind == "map") {
    O fields;
    for (const auto &f : D(parts[1]).Fields())
      fields[f.first] = Unpack(f.second);
    return D::Encode(fields);
  }
  if (kind == "list" || kind == "tuple") {
    std::vector<std::string> rows;
    for (const auto &v : D::Array(parts[1]))
      rows.push_back(Unpack(v));
    return Array(rows);
  }
  if (kind == "path")
    return parts[1];
  Check(false, "unknown packed state kind");
  return {};
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto name = (parent / ".route-preparation-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create private workspace");
    root = name;
  }
  ~Work() {
    std::error_code e;
    fs::remove_all(root, e);
  }
};
struct State {
  O top, fields;
  std::vector<ArtifactRef> refs;
  SqliteDatabase compare{":memory:", SqliteDatabase::Mode::kReadOnly};
  bool Has(const std::string &k) const {
    auto p = fields.find(k);
    return p != fields.end() && p->second != "null";
  }
  std::string Value(const std::string &k) const {
    auto p = fields.find(k);
    Check(p != fields.end(), "missing state " + k);
    return p->second;
  }
  std::string File(const std::string &k) const {
    const auto v = D::Array(Value(k), 2);
    Check(v.size() == 2 && v[0] == "\"path\"", "invalid path state " + k);
    auto path = DecodeJsonString(v[1]);
    Check(fs::path(path).is_absolute(), "relative path state");
    return path;
  }
  bool Flag(const std::string &k) const {
    const auto v = Value(k);
    Check(v == "true" || v == "false", "invalid boolean state " + k);
    return v == "true";
  }
  ArtifactRef Bind(const std::string &name, const std::string &schema,
                   const std::string &generation = "") {
    const auto ref = ArtifactRef::Bind(File(name), schema);
    if (!generation.empty())
      RequireEquivalentJson(compare, Value(generation), Receipt(ref),
                            "native routing preparation generation changed: " +
                                name);
    refs.push_back(ref);
    return ref;
  }
  void SetBinding(const std::string &name, const std::string &generation,
                  const ArtifactRef &ref) {
    fields[name] = Path(ref.path());
    fields[generation] = Receipt(ref);
  }
  void Read(const ArtifactRef &previous) {
    Check(previous.schema() == "native-compile-state-v1",
          "predecessor schema disagrees");
    D doc(D::Read(previous));
    top = doc.Fields();
    Check(doc.Text("schema") == "emuflow.native-compile-state/v1" &&
              doc.Text("stage") == "partition" && doc.Text("status") == "pass",
          "predecessor stage disagrees");
    auto value = D::Array(doc.Token("state"), 2);
    Check(value.size() == 2 && value[0] == "\"map\"",
          "predecessor state shape disagrees");
    fields = D(value[1]).Fields();
    refs = {previous};
  }
  void Accept(const StageResult &step) {
    if (!step.ok())
      throw StageError(step.error, step.stage + ": " + step.message);
    refs.insert(refs.end(), step.inputs.begin(), step.inputs.end());
    refs.insert(refs.end(), step.outputs.begin(), step.outputs.end());
  }
};
} // namespace
StageResult PrepareRouting(const ArtifactRef &previous,
                           const std::string &directory,
                           const std::string &next,
                           const volatile std::sig_atomic_t *cancelled) {
  const auto root = fs::absolute(directory);
  State state;
  std::optional<ArtifactRef> config, emuir, assignment, transport, selector,
      paths, projected_constraints;
  std::map<std::string, ArtifactRef> static_refs;
  bool internal = false, cuts = false, baseline = false;
  return RunStage(
      "route-policy",
      [&] {
        state.Read(previous);
        internal = state.Flag("internal_timing_database");
        cuts = state.Flag("has_inter_fpga_cuts");
        baseline =
            DecodeJsonString(state.Value("phase6_provider")) == "baseline";
        Check(state.Has("native_phase4_configuration"),
              "native route configuration is missing; Python fallback is "
              "forbidden");
        config =
            state.Bind("native_phase4_configuration", "phase4-configuration-v1",
                       "native_phase4_configuration_generation");
        Check(fs::path(config->path()) ==
                  root / ".native-phase4-configuration/request.json",
              "route configuration escapes compiler ownership");
        D settings(D::Read(*config));
        settings.Keys({"schema", "board", "sources", "options"});
        Check(settings.Text("schema") == "emuflow.phase4-configuration/v1",
              "route configuration version disagrees");
        const std::map<std::string, std::string> schemas = {
            {"board", "boarddb-v1"},
            {"constraints", "system-route-constraints-input-v1"},
            {"external-timing", "system-routing-paths-v1"},
            {"timing-model", "phase2-source-v1"},
            {"architecture-timing-db", "phase2-source-v1"},
            {"architecture-contract", "phase2-source-v1"}};
        for (const auto &token : D::Array(settings.Token("sources"), 6)) {
          D r(token);
          r.Keys({"role", "path", "schema", "generation"});
          const auto role = r.Text("role");
          Check(schemas.count(role) && schemas.at(role) == r.Text("schema") &&
                    fs::path(r.Text("path")).is_absolute(),
                "unowned circuit/unknown source in routing configuration");
          auto ref = ArtifactRef::Bind(r.Text("path"), r.Text("schema"));
          RequireEquivalentJson(
              state.compare, r.Token("generation"), Generation(ref),
              "routing configuration source generation changed");
          Check(static_refs.emplace(role, ref).second,
                "duplicate static route source");
          state.refs.push_back(ref);
        }
        Check(static_refs.count("board") &&
                  static_refs.at("board").path() ==
                      fs::canonical(state.File("platform_path")),
              "route board namespace disagrees");
        Check(bool(static_refs.count("timing-model")) == internal &&
                  static_refs.count("architecture-timing-db") ==
                      static_refs.count("architecture-contract"),
              "timing model source coverage disagrees");
        emuir = state.Bind("ir_path", "emuir-compact-v2");
        assignment =
            ArtifactRef::Bind((root / "partition/assignment.sqlite").string(),
                              "assignment-index-v4");
        state.refs.push_back(*assignment);
        Check(fs::path(state.File("ir_path")) ==
                      root / "frontend/phase1/design.emuir.sqlite" &&
                  fs::path(state.File("phase3_root")) == root / "partition" &&
                  fs::path(state.File("assignment_path")) ==
                      root / "partition/assignment.json",
              "routing circuit input escapes compiler ownership");
        if (internal) {
          selector = state.Bind("path_database_path", "sta-path-selector-v1");
          Check(fs::path(selector->path()) ==
                    root / "timing/path-database.json",
                "timing selector escapes compiler ownership");
          D manifest(D::Read(*selector));
          D payload(D(manifest.Token("payloads")).Token("paths"));
          const auto member = payload.Text("path");
          Check(fs::path(member).filename() == member && member != "." &&
                    member != "..",
                "timing payload is not a sibling");
          paths = ArtifactRef::Bind(
              (fs::path(selector->path()).parent_path() / member).string(),
              "timing-path-store-v1");
          state.refs.push_back(*paths);
        } else {
          Check(bool(static_refs.count("external-timing")) ==
                    state.Has("timing_paths"),
                "external timing source coverage disagrees");
          if (state.Has("timing_paths"))
            Check(static_refs.at("external-timing").path() ==
                      fs::canonical(state.File("timing_paths")),
                  "external timing path disagrees");
        }
        if (state.Has("copied_link_timing_path")) {
          projected_constraints = state.Bind(
              "effective_route_constraints", "phase3-configuration-source-v1",
              "native_effective_route_constraints_generation");
          Check(fs::path(projected_constraints->path()) ==
                    root / "timing/board-link-route-constraints.json",
                "projected constraints escape compiler ownership");
        } else
          Check(bool(static_refs.count("constraints")) ==
                        state.Has("effective_route_constraints") &&
                    (!state.Has("effective_route_constraints") ||
                     static_refs.at("constraints").path() ==
                         fs::canonical(
                             state.File("effective_route_constraints"))),
                "route constraint source disagrees");
        if (baseline) {
          Check(state.Has("native_phase6_configuration"),
                "native transport configuration is missing; Python fallback is "
                "forbidden");
          transport = state.Bind("native_phase6_configuration",
                                 "phase6-configuration-v1",
                                 "native_phase6_configuration_generation");
          Check(fs::path(transport->path()) ==
                    root / ".native-phase6-configuration/request.json",
                "transport configuration escapes compiler ownership");
        }
        return state.refs;
      },
      [&](const auto &) {
        Stop(cancelled);
        Work work(root);
        const auto timing_root = root / "timing";
        std::optional<ArtifactRef> projected;
        std::string timing_report = state.Has("timing_report")
                                        ? Unpack(state.Value("timing_report"))
                                        : "null";
        O timing_fields =
            timing_report == "null" ? O{} : D(timing_report).Fields();
        if (internal) {
          std::vector<std::string> rows;
          for (const auto *role : {"timing-model", "architecture-timing-db",
                                   "architecture-contract"})
            if (static_refs.count(role))
              rows.push_back(Record(role, static_refs.at(role)));
          rows.push_back(Record("emuir", *emuir));
          rows.push_back(Record("assignment", *assignment));
          rows.push_back(Record("selector", *selector));
          rows.push_back(Record("paths", *paths));
          const auto request = work.root / "qualification.json";
          PublishTextArtifact(
              request.string(), state.refs, [&](std::ostream &stream) {
                stream << D::Encode(
                              {{"schema",
                                JsonQuote(
                                    "emuflow.cut-qualification-request/v1")},
                               {"sources", Array(rows)},
                               {"verify_payload", "false"}})
                       << '\n';
              });
          const auto target = timing_root / "cut-segment-qualification.json";
          state.Accept(QualifyCutSegments(
              ArtifactRef::Bind(request.string(),
                                "cut-qualification-request-v1"),
              target.string(), cancelled));
          timing_fields["cut_segment_qualification"] =
              D::Read(ArtifactRef::Bind(target.string(),
                                        "cut-qualification-report-v2"));
          state.fields["cut_segment_qualification_path"] = Path(target);
          std::string summary;
          if (cuts) {
            const auto request = work.root / "projection.bin",
                       output = timing_root / "cut-timing-paths.json",
                       report = work.root / "projection.json";
            CheckedRequestWriter w(request.string(), state.refs);
            w.Put(std::uint64_t(0x31525053464d5545ULL));
            w.Put(std::uint32_t(1));
            for (const auto &ref : std::vector<ArtifactRef>{
                     *selector, *paths, *assignment, *assignment})
              w.Generation(ref.generation());
            w.Text(D::Encode({{"verify_payload", "false"},
                              {"output", JsonQuote(output.string())}}));
            w.Finish();
            state.Accept(ProjectStaPaths(
                {*selector, *paths, *assignment, *assignment,
                 ArtifactRef::Bind(request.string(),
                                   "sta-projection-request-v1")},
                output.string(), report.string(), cancelled));
            D proof(D::Read(ArtifactRef::Bind(report.string(),
                                              "sta-projection-summary-v1")));
            Check(proof.Text("schema") == "emuflow.sta-projection-report/v1" &&
                      proof.Text("status") == "pass" &&
                      proof.Text("implementation_id") ==
                          "native-sta-path-projection-v1",
                  "projection producer identity disagrees");
            summary = proof.Token("summary");
            projected =
                ArtifactRef::Bind(output.string(), "system-routing-paths-v1");
            state.fields["projected_timing_paths"] = Path(output);
          } else {
            SqliteStore ir(*emuir);
            auto q =
                ir.database().Prepare("SELECT json_extract(value,'$.name') "
                                      "FROM metadata WHERE key='design_json'");
            Check(q.Step(), "source design is missing");
            summary = D::Encode(
                {{"status", JsonQuote("pass")},
                 {"design", JsonQuote(q.TypedText(0))},
                 {"mode", JsonQuote("not-required-no-inter-fpga-cuts")},
                 {"projected_paths", "0"},
                 {"compressed_paths", "0"},
                 {"cut_nets", "0"},
                 {"covered_cut_nets", "0"},
                 {"uncovered_cut_nets", "0"}});
            state.fields["projected_timing_paths"] = "null";
          }
          timing_fields["cut_path_projection"] = summary;
        } else {
          state.fields["projected_timing_paths"] = state.Value("timing_paths");
          if (static_refs.count("external-timing"))
            projected = static_refs.at("external-timing");
        }
        const auto policy_root = root / ".native-phase4-request";
        Check(!fs::exists(policy_root) && !fs::is_symlink(policy_root),
              "route policy directory is not fresh");
        fs::create_directory(policy_root);
        const auto result = PreparePhase4Policy(
            *config, (policy_root / "phase4-policy.json").string(), cancelled,
            &*assignment, projected ? &*projected : nullptr,
            projected_constraints ? &*projected_constraints : nullptr, !cuts);
        state.Accept(result);
        Check(result.outputs.size() == 1,
              "route policy producer coverage disagrees");
        state.SetBinding("native_phase4_policy", "native_phase4_generation",
                         result.outputs[0]);
        if (baseline) {
          const auto policy_root = root / ".native-phase6-request";
          Check(!fs::exists(policy_root) && !fs::is_symlink(policy_root),
                "transport policy directory is not fresh");
          fs::create_directory(policy_root);
          const auto result = PreparePhase6Policy(
              *emuir, *transport, (policy_root / "phase6-policy.json").string(),
              cancelled);
          state.Accept(result);
          Check(result.outputs.size() == 1,
                "transport policy producer coverage disagrees");
          state.SetBinding("native_phase6_policy", "native_phase6_generation",
                           result.outputs[0]);
        }
        state.fields["effective_route_provider"] =
            JsonQuote("timing-aware-global-candidate-v1");
        state.fields["effective_tdm_provider"] =
            JsonQuote("deterministic-dependency-earliest-slot-v1");
        state.fields["phase4_root"] = Path(root / "system-route");
        state.fields["phase5_root"] = Path(root / "tdm");
        state.fields["frame_search_report"] = "null";
        if (timing_report != "null") {
          state.fields["timing_report"] = Pack(D::Encode(timing_fields));
          O summary;
          for (const auto *key :
               {"status", "mode", "backend", "sta", "optimization_enabled",
                "partition_weights", "partition_weights_applied",
                "partition_provider_weights_applied",
                "hop_refinement_weights_applied", "partition_weight_consumers",
                "cut_path_projection"})
            if (timing_fields.count(key))
              summary[key] = timing_fields.at(key);
          PublishTextArtifact(
              (timing_root / "orchestration-summary.json").string(), state.refs,
              [&](std::ostream &stream) {
                stream << D::Encode(summary) << '\n';
              });
        }
        for (const auto *name :
             {"native_phase4_configuration",
              "native_phase4_configuration_generation",
              "native_phase6_configuration",
              "native_phase6_configuration_generation",
              "native_effective_route_constraints_generation"})
          state.fields.erase(name);
        state.top["state"] = "[\"map\"," + D::Encode(state.fields) + "]";
        state.top["stage"] = JsonQuote("route-policy");
        const auto value = D::Encode(state.top);
        D checked(value);
        Stop(cancelled);
        PublishTextArtifact(next, state.refs, [&](std::ostream &stream) {
          stream << value << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(next, "native-compile-state-v1")};
      });
}
} // namespace emuflow::core
