#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "transport/transport.hpp"
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
    throw StageError(ErrorCode::kInvalidRequest,
                     "Phase 6 configuration input: " + why);
}
std::string Array(const std::vector<std::string> &rows) {
  std::string text = "[";
  for (const auto &row : rows) {
    if (text.size() > 1)
      text += ',';
    text += row;
  }
  return text + ']';
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Record(const ArtifactRef &ref) {
  return D::Encode({{"path", JsonQuote(ref.path())},
                    {"schema", JsonQuote(ref.schema())},
                    {"generation", Generation(ref)}});
}
void Cost(D &cost, const std::string &platform) {
  Check(cost.Text("schema") == "emuflow.transport-cost/v2",
        "transport cost schema is invalid");
  Check(!cost.Text("platform").empty() && cost.Text("platform") == platform,
        "transport cost platform disagrees");
  const auto profile = cost.Text("profile");
  Check((profile == "aggressive" || profile == "nominal" ||
         profile == "conservative") &&
            cost.Text("model") == "production-transport-rtl-structural-v2",
        "transport cost profile is invalid");
  std::set<std::string> names;
  for (const auto &token : D::Array(cost.Token("feature_names"))) {
    const auto name = DecodeJsonString(token);
    Check(!name.empty() && names.insert(name).second,
          "transport cost features are invalid");
  }
  Check(!names.empty(), "transport cost features are empty");
  const auto resources = D(cost.Token("resources")).Fields();
  Check(!resources.empty(), "transport cost resources are empty");
  for (const auto &resource : resources) {
    Check(!resource.first.empty(), "transport cost resource entry is invalid");
    D parameters(resource.second);
    parameters.Keys(names);
    for (const auto &name : names)
      Check(parameters.Number(name) >= 0,
            "transport cost parameter is invalid");
  }
  D provenance(cost.Token("provenance"));
  Check(provenance.Text("class") == "open_source_rtl_characterization" &&
            provenance.Text("fit_sha256").size() == 64,
        "transport cost provenance is invalid");
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto value = (parent / ".phase6-configuration-XXXXXX").string();
    Check(mkdtemp(value.data()), "cannot create private directory");
    root = value;
  }
  ~Work() {
    std::error_code error;
    fs::remove_all(root, error);
  }
};
} // namespace
StageResult
PreparePhase6Configuration(const ArtifactRef &request,
                           const std::string &directory,
                           const volatile std::sig_atomic_t *cancelled) {
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  O options;
  std::string board;
  const auto output = fs::absolute(directory);
  return RunStage(
      "phase6.prepare-configuration",
      [&] {
        Check(request.schema() == "phase6-configuration-input-v1",
              "request schema disagrees");
        D doc(D::Read(request));
        doc.Keys({"schema", "sources", "options"});
        Check(doc.Text("schema") == "emuflow.phase6-configuration-input/v1",
              "request version disagrees");
        sources.push_back(request);
        SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
        const std::map<std::string, std::string> schemas{
            {"board", "boarddb-v1"},
            {"inventory", "hp-package-pin-inventory-v1"},
            {"wiring", "fixed-wiring-csv-v1"},
            {"cost", "transport-cost-input-v1"},
            {"python", "external-tool-executable-v1"},
            {"oracle", "phase6-oracle-source-v1"}};
        for (const auto &token : D::Array(doc.Token("sources"), 6)) {
          D row(token);
          row.Keys({"role", "path", "schema", "generation"});
          const auto role = row.Text("role");
          Check(schemas.count(role) && row.Text("schema") == schemas.at(role) &&
                    fs::path(row.Text("path")).is_absolute(),
                "source role/schema/path disagrees");
          auto ref = ArtifactRef::Bind(row.Text("path"), schemas.at(role));
          RequireEquivalentJson(compare, row.Token("generation"),
                                Generation(ref),
                                "Phase 6 configuration input source changed");
          Check(roles.emplace(role, ref).second,
                "duplicate configuration source");
          sources.push_back(ref);
        }
        for (const auto *role : {"board", "python", "oracle"})
          Check(roles.count(role), std::string("missing ") + role + " source");
        Check(access(roles.at("python").path().c_str(), X_OK) == 0,
              "oracle Python executable is unavailable");
        board = NormalizeBoard(D::Read(roles.at("board")));
        D platform(D(board).Token("platform"));
        const bool model = platform.Has("virtual_board_model") &&
                           platform.Token("virtual_board_model") != "null";
        const bool fixed = platform.Has("virtual_board_wiring") &&
                           platform.Token("virtual_board_wiring") != "null";
        Check(bool(roles.count("inventory")) == model &&
                  bool(roles.count("wiring")) == fixed,
              "wiring source coverage disagrees");
        D settings(doc.Token("options"));
        settings.Keys({"clock_periods", "cycles", "seed"});
        options = settings.Fields();
        Check(settings.Integer("cycles") >= 0 && settings.Integer("seed") >= 0,
              "invalid small-design oracle settings");
        if (settings.Token("clock_periods") != "null") {
          D periods(settings.Token("clock_periods"));
          for (const auto &period : periods.Fields())
            Check(!period.first.empty() && periods.Number(period.first) > 0,
                  "invalid clock period");
        }
        Check(!fs::exists(output) && !fs::is_symlink(output),
              "configuration directory already exists");
        return sources;
      },
      [&](const auto &refs) {
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 6 configuration cancelled");
        std::string virtual_board = "null", cost = "null";
        std::vector<std::string> configured{Record(roles.at("board"))};
        if (roles.count("inventory"))
          virtual_board = BuildVirtualBoard(
              board, roles.at("inventory"),
              roles.count("wiring")
                  ? std::optional<ArtifactRef>(roles.at("wiring"))
                  : std::nullopt,
              cancelled);
        if (roles.count("cost")) {
          D db(D::Read(roles.at("cost")));
          D platform(D(board).Token("platform"));
          Cost(db, platform.Text("name"));
          cost = db.text();
          configured.push_back(Record(roles.at("cost")));
        }
        const auto policy = D::Encode(
            {{"schema", JsonQuote("emuflow.phase6-configuration/v1")},
             {"configuration_sources", Array(configured)},
             {"clock_periods", options.at("clock_periods")},
             {"virtual_board", virtual_board},
             {"transport_cost", cost},
             {"small_oracle", D::Encode({{"python", Record(roles.at("python"))},
                                         {"source", Record(roles.at("oracle"))},
                                         {"cycles", options.at("cycles")},
                                         {"seed", options.at("seed")}})}});
        Check(policy.size() <= 8 * 1024 * 1024, "configuration exceeds bound");
        fs::create_directories(output.parent_path());
        Work work(output.parent_path());
        PublishTextArtifact(
            (work.root / "request.json").string(), refs,
            [&](std::ostream &stream) { stream << policy << '\n'; });
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 6 configuration cancelled");
        PublishNewDirectory(work.root.string(), output.string(), refs);
      },
      [&] {
        return std::vector<ArtifactRef>{ArtifactRef::Bind(
            (output / "request.json").string(), "phase6-configuration-v1")};
      });
}
} // namespace emuflow::core
