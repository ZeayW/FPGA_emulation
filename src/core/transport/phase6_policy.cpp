#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "ir/emuir_view.hpp"
#include "transport/transport.hpp"
#include <filesystem>
#include <set>
namespace emuflow::core {
namespace {
using D = BoundedJson;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Phase 6 configuration: " + why);
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
StageResult PreparePhase6Policy(const ArtifactRef &source,
                                const ArtifactRef &configuration,
                                const std::string &output,
                                const volatile std::sig_atomic_t *cancelled) {
  std::vector<ArtifactRef> sources;
  std::map<std::string, std::string> policy;
  return RunStage(
      "phase6.prepare-policy",
      [&] {
        Check(source.schema() == "emuir-compact-v2" &&
                  configuration.schema() == "phase6-configuration-v1",
              "input schema disagrees");
        D doc(D::Read(configuration));
        doc.Keys({"schema", "configuration_sources", "clock_periods",
                  "virtual_board", "transport_cost", "small_oracle"});
        Check(doc.Text("schema") == "emuflow.phase6-configuration/v1",
              "configuration version disagrees");
        policy = doc.Fields();
        sources = {source, configuration};
        SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
        const auto bind = [&](const std::string &token,
                              const std::string &schema) {
          D ref(token);
          ref.Keys({"path", "schema", "generation"});
          Check(ref.Text("schema") == schema &&
                    std::filesystem::path(ref.Text("path")).is_absolute(),
                "source schema/path disagrees");
          auto result = ArtifactRef::Bind(ref.Text("path"), schema);
          RequireEquivalentJson(
              compare, ref.Token("generation"), Generation(result),
              "Phase 6 configuration source generation changed");
          sources.push_back(result);
        };
        const auto refs = D::Array(doc.Token("configuration_sources"), 2);
        Check(!refs.empty(), "board source is missing");
        bind(refs[0], "boarddb-v1");
        if (refs.size() == 2)
          bind(refs[1], "transport-cost-input-v1");
        Check((doc.Token("transport_cost") != "null") == bool(refs.size() == 2),
              "transport cost source coverage disagrees");
        D oracle(doc.Token("small_oracle"));
        oracle.Keys({"python", "source", "cycles", "seed"});
        bind(oracle.Token("python"), "external-tool-executable-v1");
        bind(oracle.Token("source"), "phase6-oracle-source-v1");
        return sources;
      },
      [&](const auto &refs) {
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 6 configuration cancelled");
        Check(!std::filesystem::exists(output) &&
                  !std::filesystem::is_symlink(output),
              "policy output already exists");
        EmuirView ir(source);
        auto &db = ir.store().database();
        std::set<std::string> clocks;
        auto q = db.Prepare("SELECT record_json FROM clocks ORDER BY seq");
        while (q.Step()) {
          D c(q.TypedText(0));
          Check(clocks.size() < 4096 && clocks.insert(c.Text("id")).second,
                "duplicate/excessive source clocks");
        }
        if (policy.at("clock_periods") == "null") {
          Check(clocks.size() == 1,
                "multi-clock Phase 6 requires explicit periods");
          policy["clock_periods"] = D::Encode({{*clocks.begin(), "1.0"}});
        } else {
          D periods(policy.at("clock_periods"));
          Check(!periods.Fields().empty(), "empty clock periods");
          for (const auto &p : periods.Fields())
            Check(clocks.count(p.first) && periods.Number(p.first) > 0,
                  "clock period/identity is invalid");
        }
        policy["schema"] = JsonQuote("emuflow.phase6-compile-policy/v1");
        policy["source"] = Binding(source);
        std::filesystem::create_directories(
            std::filesystem::path(output).parent_path());
        PublishTextArtifact(output, refs, [&](std::ostream &stream) {
          stream << D::Encode(policy) << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "phase6-compile-policy-v1")};
      });
}
} // namespace emuflow::core
