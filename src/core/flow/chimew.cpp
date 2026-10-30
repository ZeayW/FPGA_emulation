#include "flow/chimew.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "flow/signoff.hpp"
#include "physical/flow/controller.hpp"
#include "scheduling/schedule_view.hpp"
#include "transport/chimew/geometry.hpp"
#include "transport/chimew/lookahead.hpp"
#include "transport/chimew/pipeline.hpp"
#include "transport/chimew/signals.hpp"
#include "transport/transport.hpp"
#include <cerrno>
#include <fcntl.h>
#include <filesystem>
#include <map>
#include <optional>
#include <sys/file.h>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using D = BoundedJson;
using O = std::map<std::string, std::string>;
void Check(bool valid, const std::string &why) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact, "Chimew compile: " + why);
}
void Accept(const StageResult &result) {
  if (!result.ok())
    throw StageError(result.error, result.stage + ": " + result.message);
}
std::string Array(const std::vector<std::string> &rows) {
  std::string value = "[";
  for (const auto &row : rows) {
    if (value.size() > 1)
      value += ',';
    value += row;
  }
  return value + ']';
}
std::string Gen(const ArtifactRef &ref) {
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
                    {"generation", Gen(ref)}});
}
std::string Role(const std::string &role, const ArtifactRef &ref) {
  auto row = D(Record(ref)).Fields();
  row["role"] = JsonQuote(role);
  return D::Encode(row);
}
std::string Source(const ArtifactRef &ref) {
  return D::Encode(
      {{"path", JsonQuote(ref.path())}, {"schema", JsonQuote(ref.schema())}});
}
ArtifactRef Bind(const fs::path &p, const std::string &schema) {
  Check(!fs::is_symlink(p), "source is a symbolic link");
  return ArtifactRef::Bind(p.string(), schema);
}
std::string Read(const fs::path &path) {
  return D::Read(Bind(path, "physical-input-document-v1"));
}
ArtifactRef Write(const fs::path &path, const O &fields,
                  const std::string &schema,
                  const std::vector<ArtifactRef> &refs) {
  PublishTextArtifact(path.string(), refs,
                      [&](auto &out) { out << D::Encode(fields) << '\n'; });
  return Bind(path, schema);
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto p = (parent / ".chimew-compile-XXXXXX").string();
    Check(mkdtemp(p.data()), "cannot create private workspace");
    root = p;
  }
  ~Work() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
struct SharedPipeline {
  int fd = -1;
  SharedPipeline(const fs::path &root,
                 const volatile std::sig_atomic_t *cancelled) {
    const auto lock = root.parent_path() /
                      ("." + root.filename().string() + ".pipeline.lock");
    fd = open(lock.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    Check(fd >= 0, "pipeline publication lock is unavailable");
    while (flock(fd, LOCK_SH) != 0) {
      if (errno == EINTR && !(cancelled && *cancelled))
        continue;
      close(fd);
      fd = -1;
      throw StageError(ErrorCode::kExecutionFailed,
                       "cannot lease Chimew pipeline");
    }
  }
  ~SharedPipeline() {
    if (fd >= 0) {
      flock(fd, LOCK_UN);
      close(fd);
    }
  }
};
struct State {
  O fields;
  std::vector<ArtifactRef> refs;
  explicit State(const ArtifactRef &source) {
    Check(source.schema() == "native-compile-state-v1", "wrong state role");
    D doc(D::Read(source));
    Check(doc.Text("schema") == "emuflow.native-compile-state/v1" &&
              doc.Text("stage") == "tdm" && doc.Text("status") == "pass",
          "invalid predecessor state");
    const auto envelope = D::Array(doc.Token("state"), 2);
    Check(envelope.size() == 2 && DecodeJsonString(envelope[0]) == "map",
          "invalid state envelope");
    fields = D(envelope[1]).Fields();
    refs.push_back(source);
    Check(Text("phase6_provider") == "chimew" &&
              fields.at("physical") == "true",
          "Chimew requires full physical execution");
  }
  std::string Text(const std::string &name) {
    return DecodeJsonString(fields.at(name));
  }
  bool Has(const std::string &name) {
    return fields.count(name) && fields.at(name) != "null";
  }
  std::string Path(const std::string &name) {
    const auto row = D::Array(fields.at(name), 2);
    Check(row.size() == 2 && DecodeJsonString(row[0]) == "path",
          "missing path binding " + name);
    auto value = DecodeJsonString(row[1]);
    Check(fs::path(value).is_absolute(), "relative path binding");
    return value;
  }
  ArtifactRef File(const std::string &name, const std::string &schema,
                   bool generation = false) {
    auto ref = Bind(Path(name), schema);
    if (generation) {
      auto row = D::Array(fields.at(name == "native_phase6_policy"
                                        ? "native_phase6_generation"
                                    : name == "native_physical_policy"
                                        ? "native_physical_generation"
                                        : name + "_generation"),
                          2);
      Check(row.size() == 2 && DecodeJsonString(row[0]) == "tuple",
            "invalid generation binding");
      const auto values = D::Array(row[1], 7), actual = D::Array(Gen(ref), 7);
      Check(values.size() == 7, "invalid generation count");
      for (std::size_t i = 0; i < 7; ++i)
        Check(DecodeJsonString(values[i]) == actual[i],
              "configuration source generation changed");
    }
    refs.push_back(ref);
    return ref;
  }
};
O PinMetrics(const fs::path &selector) {
  D manifest(Read(selector));
  const auto member = manifest.Text("artifact");
  Check(fs::path(member).filename() == member, "pin selector escapes owner");
  auto ref = Bind(selector.parent_path() / member, "pin-plan-store-v1");
  SqliteStore store(ref);
  auto q =
      store.database().Prepare("SELECT value FROM metadata WHERE key='header'");
  Check(q.Step(), "missing pin header");
  D header(q.TypedText(0));
  return D(header.Token("metrics")).Fields();
}
} // namespace
StageResult CompileChimewPhase6(const ArtifactRef &previous,
                                const std::string &directory,
                                const volatile std::sig_atomic_t *cancelled) {
  std::optional<State> state;
  std::optional<ArtifactRef> policy, physical_policy, config, original,
      assignment, board, schedule, routes, paths;
  std::map<std::string, ArtifactRef> assets;
  std::string package;
  const auto root = fs::absolute(directory);
  const auto comparison = root / "phase6-comparison",
             baseline = comparison / "baseline";
  return RunStage(
      "phase6.compile-chimew",
      [&] {
        state.emplace(previous);
        auto &s = *state;
        Check(fs::canonical(s.Path("output_dir")) == fs::canonical(root),
              "flow directory escapes state owner");
        for (const auto &row : std::vector<std::pair<std::string, fs::path>>{
                 {"phase3_root", root / "partition"},
                 {"phase4_root", root / "system-route"},
                 {"phase5_root", root / "tdm"},
                 {"ir_path", root / "frontend/phase1/design.emuir.sqlite"}})
          Check(fs::weakly_canonical(s.Path(row.first)) == row.second,
                "upstream path escapes compiler ownership");
        policy =
            s.File("native_phase6_policy", "phase6-compile-policy-v1", true);
        physical_policy =
            s.File("native_physical_policy", "physical-flow-policy-v1", true);
        config = s.File("native_chimew_configuration",
                        "chimew-compile-configuration-v1", true);
        Check(fs::path(policy->path()) ==
                      root / ".native-phase6-request/phase6-policy.json" &&
                  fs::path(physical_policy->path()) ==
                      root / ".native-physical-request/physical-policy.json" &&
                  fs::path(config->path()) ==
                      root / ".native-chimew-configuration/request.json",
              "configuration escapes compiler ownership");
        D settings(D::Read(*config));
        settings.Keys({"schema", "sources", "package_geometry"});
        Check(settings.Text("schema") ==
                  "emuflow.chimew-compile-configuration/v1",
              "configuration schema disagrees");
        package = settings.Token("package_geometry");
        SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
        const O allowed{{"architecture", "physical-input-document-v1"},
                        {"board", "boarddb-v1"},
                        {"inventory", "hp-package-pin-inventory-v1"},
                        {"wiring", "fixed-wiring-csv-v1"}};
        for (const auto &item : D::Array(settings.Token("sources"), 4)) {
          D row(item);
          row.Keys({"role", "path", "schema", "generation"});
          const auto role = row.Text("role");
          Check(allowed.count(role) && row.Text("schema") == allowed.at(role),
                "unknown Chimew static source");
          auto ref = Bind(row.Text("path"), row.Text("schema"));
          RequireEquivalentJson(compare, row.Token("generation"), Gen(ref),
                                "Chimew static source changed");
          Check(assets.emplace(role, ref).second, "duplicate static source");
          s.refs.push_back(ref);
        }
        Check(assets.count("architecture") && assets.count("board") &&
                  assets.count("inventory"),
              "incomplete Chimew static sources");
        board = s.File("platform_path", "boarddb-v1");
        Check(board->path() == assets.at("board").path() &&
                  board->generation() == assets.at("board").generation(),
              "board namespace changed");
        original = s.File("ir_path", "emuir-compact-v2");
        assignment =
            Bind(root / "partition/assignment.sqlite", "assignment-index-v4");
        routes =
            Bind(root / "system-route/routes.json", "system-route-selector-v1");
        schedule = Bind(root / "tdm/schedule.json", "tdm-schedule-selector-v4");
        paths = s.File("path_database_path", "sta-path-selector-v1");
        s.refs.insert(s.refs.end(), {*assignment, *routes, *schedule, *paths});
        auto owner = OpenScheduleStore(*schedule, cancelled);
        SqliteStore store(owner.store);
        auto q =
            store.database().Prepare("SELECT 1 FROM schedule_entries LIMIT 1");
        Check(q.Step(),
              "Chimew requires at least one scheduled inter-FPGA signal");
        s.refs.push_back(owner.store);
        Check(!fs::exists(comparison) && !fs::is_symlink(comparison) &&
                  !fs::exists(root / "split") &&
                  !fs::is_symlink(root / "split"),
              "comparison outputs are not fresh");
        return s.refs;
      },
      [&](const auto &refs) {
        Work work(root);
        fs::create_directories(baseline);
        auto &s = *state;
        const auto phase6 =
            [&](const fs::path &out, const ArtifactRef &selected,
                const std::vector<std::pair<std::string, ArtifactRef>> &pins =
                    std::vector<std::pair<std::string, ArtifactRef>>{}) {
              auto result = CompilePhase6(
                  {*original, *assignment, selected, *board, *policy},
                  out.string(), cancelled, pins);
              Accept(result);
              return result.metrics.wall_seconds;
            };
        const auto physical = [&](const fs::path &split, const fs::path &out,
                                  const ArtifactRef &selected) {
          auto dynamic = std::vector<std::pair<std::string, ArtifactRef>>{
              {"split-manifest",
               Bind(split / "manifest.json", "physical-input-document-v1")},
              {"board", *board},
              {"schedule-selector", selected},
              {"original", *original},
              {"assignment-selector", Bind(root / "partition/assignment.json",
                                           "physical-input-document-v1")},
              {"route-selector", *routes},
              {"path-selector", *paths},
              {"schedule-board", Bind(root / "tdm/schedule-board.bin",
                                      "exact-schedule-board-request-v1")},
              {"phase5-report",
               Bind(root / "tdm/phase5_report.json", "phase5-report-v1")}};
          auto request = PreparePhysicalFlowCompileRequest(
              *physical_policy, dynamic,
              (work.root / "baseline-physical.json").string());
          auto result = RunPhysicalFlow(request, out.string(), cancelled);
          Accept(result);
          return result.metrics.wall_seconds;
        };
        const auto baseline_phase6_seconds =
            phase6(baseline / "split", *schedule);
        const auto baseline_physical_seconds =
            physical(baseline / "split", baseline / "physical", *schedule);
        SignoffPhysical(
            root, s.Text("global_sta_executable"), cancelled,
            s.Has("global_sta_launcher_reference")
                ? s.Path("global_sta_launcher_reference")
                : "",
            {baseline / "split", baseline / "physical", baseline / "runtime"});
        std::vector<std::string> geometry_sources{
            Role("board", *board), Role("original", *original),
            Role("schedule", *schedule),
            Role("architecture", assets.at("architecture")),
            Role("inventory", assets.at("inventory"))};
        if (assets.count("wiring"))
          geometry_sources.push_back(Role("wiring", assets.at("wiring")));
        const auto lookahead = comparison / "lookahead";
        fs::create_directories(lookahead / "sources");
        auto request =
            Write(work.root / "geometry.json",
                  {{"schema", JsonQuote("emuflow.chimew-geometry-input/v1")},
                   {"sources", Array(geometry_sources)},
                   {"package_geometry", package},
                   {"physical_report",
                    Read(baseline /
                         "physical/multi-fpga-physical-flow-report.json")}},
                  "chimew-geometry-input-v1", refs);
        auto geometry = PrepareChimewGeometry(
            request, (lookahead / "sources/geometry").string(), cancelled);
        Accept(geometry);
        Check(geometry.outputs.size() == 2,
              "geometry output coverage disagrees");
        std::vector<std::string> signal_sources{
            Role("geometry", geometry.outputs[1]), Role("schedule", *schedule),
            Role("routes", *routes)};
        if (s.fields.at("timing_driven") == "true" &&
            s.Has("projected_timing_paths"))
          signal_sources.push_back(
              Role("timing",
                   s.File("projected_timing_paths", "sta-path-selector-v1")));
        request =
            Write(work.root / "signals.json",
                  {{"schema", JsonQuote("emuflow.chimew-signal-input/v1")},
                   {"sources", Array(signal_sources)},
                   {"weight_scale", "9.0"},
                   {"path_scope", JsonQuote("exact-hop")},
                   {"region_count", s.fields.at("phase6_chimew_region_count")}},
                  "chimew-signal-input-v1", refs);
        auto signals = PrepareChimewSignals(
            request, (lookahead / "sources/signals").string(), cancelled);
        Accept(signals);
        Check(signals.outputs.size() == 2, "signal output coverage disagrees");
        request =
            Write(work.root / "lookahead.json",
                  {{"schema", JsonQuote("emuflow.chimew-lookahead-input/v1")},
                   {"signals", Record(signals.outputs[1])}},
                  "chimew-lookahead-input-v1", refs);
        auto prepared = PrepareChimewLookahead(
            request, (lookahead / "preparation").string(), cancelled);
        Accept(prepared);
        Check(prepared.outputs.size() == 1,
              "lookahead output coverage disagrees");
        D prepared_report(D::Read(prepared.outputs[0]));
        D artifacts(prepared_report.Token("artifacts"));
        O pipeline_sources{{"board", Source(*board)},
                           {"inventory", Source(assets.at("inventory"))}};
        if (assets.count("wiring"))
          pipeline_sources["wiring"] = Source(assets.at("wiring"));
        for (const auto &names :
             std::vector<std::pair<std::string, std::string>>{
                 {"schedule", "schedule"},
                 {"crossings", "crossings"},
                 {"positions", "positions"},
                 {"rudy_input", "rudy_input"},
                 {"bank_input", "bank_channel_input"},
                 {"electrical", "electrical_map"}}) {
          D record(artifacts.Token(names.second));
          auto ref =
              Bind(record.Text("path"), names.first == "bank_input"
                                            ? "chimew-bank-input-selector-v1"
                                            : "physical-input-document-v1");
          Check(Sha256File(ref, cancelled) == record.Text("sha256"),
                "lookahead output seal changed");
          pipeline_sources[names.first] = Source(ref);
        }
        for (const auto &source : D(prepared_report.Token("sources")).Fields())
          pipeline_sources["source_" + source.first] = Source(Bind(
              DecodeJsonString(source.second), "physical-input-document-v1"));
        request =
            Write(work.root / "pipeline.json",
                  {{"schema", JsonQuote("emuflow.chimew-pipeline-input/v1")},
                   {"sources", D::Encode(pipeline_sources)},
                   {"region_count", s.fields.at("phase6_chimew_region_count")}},
                  "chimew-pipeline-input-v1", refs);
        const auto pipeline = comparison / "chimew";
        Accept(RunChimewPipeline(request, pipeline.string(), cancelled));
        SharedPipeline lease(pipeline, cancelled);
        O audit_sources{{"inventory", Source(assets.at("inventory"))}};
        if (assets.count("wiring"))
          audit_sources["wiring"] = Source(assets.at("wiring"));
        auto audit =
            Write(work.root / "audit-pipeline.json",
                  {{"schema",
                    JsonQuote("emuflow.chimew-pipeline-validation-input/v1")},
                   {"sources", D::Encode(audit_sources)}},
                  "chimew-pipeline-validation-input-v1", refs);
        Accept(AuditChimewPipeline(
            audit, pipeline.string(),
            (work.root / "pipeline-validation.json").string(), cancelled));
        D pipeline_report(Read(pipeline / "pipeline_report.json"));
        D adapter(pipeline_report.Token("artifacts"));
        const auto member = [&](const char *name, const char *schema) {
          D record(adapter.Token(name));
          const auto relative = fs::path(record.Text("path"));
          Check(!relative.empty() && !relative.is_absolute() &&
                    *relative.begin() != "..",
                "adapter path escapes pipeline");
          auto ref = Bind(pipeline / relative, schema);
          Check(Sha256File(ref, cancelled) == record.Text("sha256"),
                "adapter source changed");
          return ref;
        };
        auto plan = member("adapter_pin_plan", "pin-plan-selector-v1");
        const auto selected = Bind(D(artifacts.Token("schedule")).Text("path"),
                                   "tdm-schedule-selector-v4");
        Check(selected.path() == schedule->path() &&
                  selected.generation() == schedule->generation(),
              "Chimew changed the frozen schedule");
        std::vector<std::pair<std::string, ArtifactRef>> pins{
            {"board", *board},
            {"inventory", assets.at("inventory")},
            {"schedule", Bind(selected.path(), "physical-input-document-v1")},
            {"plan", plan},
            {"positions",
             member("adapter_position_hints", "position-hints-selector-v1")},
            {"binding", member("adapter_electrical_binding",
                               "chimew-binding-selector-v1")}};
        if (assets.count("wiring"))
          pins.emplace_back("wiring", assets.at("wiring"));
        const auto candidate_seconds = phase6(root / "split", selected, pins);
        O frozen;
        for (const auto &entry :
             std::vector<std::pair<std::string, ArtifactRef>>{
                 {"emuir", *original},
                 {"assignment", Bind(root / "partition/assignment.json",
                                     "assignment-selector-v4")},
                 {"routes", *routes},
                 {"schedule", *schedule},
                 {"platform", *board}})
          frozen[entry.first + "_sha256"] =
              JsonQuote(Sha256File(entry.second, cancelled));
        Write(
            comparison / "comparison-pending.json",
            {{"schema", JsonQuote("emuflow.phase6-ab-comparison/v3")},
             {"status", JsonQuote("pending-physical-comparison")},
             {"selected_provider", JsonQuote("chimew")},
             {"baseline_provider", JsonQuote("baseline")},
             {"qualification",
              JsonQuote("xilinx-placed-lookahead+fixed-lvds-wiring")},
             {"frozen_upstream", D::Encode(frozen)},
             {"claim_boundary",
              D::Encode(
                  {{"lookahead",
                    JsonQuote(
                        "normalized virtual regions from the selected Xilinx "
                        "physical prepass, not final device SLR/SLL closure")},
                   {"electrical",
                    JsonQuote("fixed source-qualified LVDS P/N wiring; not "
                              "board electrical signoff")}})},
             {"baseline",
              D::Encode(
                  {{"phase6", Read(baseline / "split/phase6_report.json")},
                   {"physical",
                    Read(baseline /
                         "physical/multi-fpga-physical-flow-report.json")},
                   {"runtime", Read(baseline / "runtime/phase7c_report.json")},
                   {"phase6_runtime_seconds",
                    JsonRecord(baseline_phase6_seconds).Encode()},
                   {"physical_runtime_seconds",
                    JsonRecord(baseline_physical_seconds).Encode()}})},
             {"lookahead", prepared_report.text()},
             {"chimew_pipeline", pipeline_report.text()},
             {"chimew_phase6_runtime_seconds",
              JsonRecord(candidate_seconds).Encode()},
             {"pin_plan_metrics", D::Encode(PinMetrics(plan.path()))}},
            "phase6-comparison-input-v1", refs);
      },
      [&] {
        return std::vector<ArtifactRef>{
            Bind(root / "split/phase6_report.json", "phase6-report-v1"),
            Bind(comparison / "comparison-pending.json",
                 "phase6-comparison-input-v1")};
      });
}
ArtifactRef CompleteChimewPhysicalComparisonInput(const std::string &directory,
                                                  double physical_seconds) {
  Check(physical_seconds >= 0, "negative physical runtime");
  const auto root = fs::absolute(directory) / "phase6-comparison";
  auto source =
      Bind(root / "comparison-pending.json", "phase6-comparison-input-v1");
  auto fields = D(D::Read(source)).Fields();
  Check(DecodeJsonString(fields.at("status")) ==
                "pending-physical-comparison" &&
            !fields.count("chimew_physical_runtime_seconds"),
        "invalid pending comparison");
  fields["chimew_physical_runtime_seconds"] =
      JsonRecord(physical_seconds).Encode();
  return Write(root / "comparison-input.json", fields,
               "phase6-comparison-input-v1", {source});
}
} // namespace emuflow::core
