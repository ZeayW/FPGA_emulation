#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "routing/policy.hpp"
#include <filesystem>
#include <map>
namespace emuflow::core {
namespace {
using D = BoundedJson;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Phase 4 configuration: " + why);
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Binding(const ArtifactRef &ref) {
  return D::Encode({{"path", JsonQuote(ref.path())},
                    {"schema", JsonQuote(ref.schema())},
                    {"generation", Generation(ref)}});
}
} // namespace
StageResult PreparePhase4Policy(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *cancelled,
                                const ArtifactRef *compiled_assignment,
                                const ArtifactRef *compiled_timing,
                                const ArtifactRef *compiled_constraints,
                                bool no_cuts) {
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  std::string board;
  std::map<std::string, std::string> options;
  return RunStage(
      "phase4.prepare-policy",
      [&] {
        Check(request.schema() == "phase4-configuration-v1",
              "request schema disagrees");
        D doc(D::Read(request));
        doc.Keys({"schema", "board", "sources", "options"});
        Check(doc.Text("schema") == "emuflow.phase4-configuration/v1",
              "request version disagrees");
        board = doc.Token("board");
        D b(board);
        Check(b.Text("schema") == "emuflow.boarddb/v1",
              "normalized board schema disagrees");
        D settings(doc.Token("options"));
        settings.Keys({"frame_slots", "max_iterations", "candidate_workers",
                       "timing_driven"});
        options = settings.Fields();
        Check(settings.Integer("candidate_workers") > 0 &&
                  settings.Integer("candidate_workers") <= INT32_MAX,
              "candidate_workers must be a positive integer");
        (void)settings.Boolean("timing_driven");
        for (const auto *k : {"frame_slots", "max_iterations"})
          if (settings.Token(k) != "null")
            Check(settings.Integer(k) > 0,
                  "route domain/effort must be positive");
        sources = {request};
        SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
        const std::map<std::string, std::string> schemas = {
            {"board", "boarddb-v1"},
            {"constraints", "system-route-constraints-input-v1"},
            {"assignment", "assignment-index-v4"},
            {"timing", "system-routing-paths-v1"},
            {"external-timing", "system-routing-paths-v1"},
            {"timing-model", "phase2-source-v1"},
            {"architecture-timing-db", "phase2-source-v1"},
            {"architecture-contract", "phase2-source-v1"}};
        for (const auto &token : D::Array(doc.Token("sources"), 8)) {
          D row(token);
          row.Keys({"role", "path", "schema", "generation"});
          const auto role = row.Text("role");
          Check(schemas.count(role) && row.Text("schema") == schemas.at(role) &&
                    std::filesystem::path(row.Text("path")).is_absolute(),
                "unknown source binding");
          auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
          RequireEquivalentJson(
              compare, row.Token("generation"), Generation(ref),
              "Phase 4 configuration source generation changed");
          Check(roles.emplace(role, ref).second, "duplicate source role");
          sources.push_back(ref);
        }
        const auto add = [&](const char *role, const ArtifactRef *ref) {
          if (ref) {
            Check(ref->schema() == schemas.at(role) && !roles.count(role),
                  "compiled source was supplied by host");
            roles.emplace(role, *ref);
            sources.push_back(*ref);
          }
        };
        add("assignment", compiled_assignment);
        add("timing", compiled_timing);
        Check(roles.count("board") && roles.count("assignment"),
              "board/assignment source is missing");
        if (compiled_constraints) {
          Check(compiled_constraints->schema() ==
                        "system-route-constraints-input-v1" ||
                    compiled_constraints->schema() ==
                        "phase3-configuration-source-v1",
                "compiled route constraint schema disagrees");
          sources.push_back(*compiled_constraints);
        }
        return sources;
      },
      [&](const auto &refs) {
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 4 configuration cancelled");
        Check(!std::filesystem::exists(output) &&
                  !std::filesystem::is_symlink(output),
              "policy output already exists");
        const auto constraints =
            compiled_constraints         ? D::Read(*compiled_constraints)
            : roles.count("constraints") ? D::Read(roles.at("constraints"))
                                         : "null";
        const auto normalized = NormalizeRoutePolicy(
            board, constraints,
            options.at("frame_slots") == "null"
                ? std::optional<std::int64_t>{}
                : D::IntegerToken(options.at("frame_slots")),
            options.at("max_iterations") == "null"
                ? std::optional<std::int64_t>{}
                : D::IntegerToken(options.at("max_iterations")));
        std::string inputs = "[" + Binding(roles.at("assignment")) + "," +
                             Binding(roles.at("board"));
        if (roles.count("timing"))
          inputs += "," + Binding(roles.at("timing"));
        inputs += ']';
        const auto policy =
            D::Encode({{"schema", JsonQuote("emuflow.phase4-policy/v1")},
                       {"constraints", normalized},
                       {"candidate_workers",
                        no_cuts ? "1" : options.at("candidate_workers")},
                       {"timing_driven", options.at("timing_driven")},
                       {"sources", inputs}});
        std::filesystem::create_directories(
            std::filesystem::path(output).parent_path());
        PublishTextArtifact(output, refs, [&](std::ostream &stream) {
          stream << policy << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "phase4-policy-v1")};
      });
}
} // namespace emuflow::core
