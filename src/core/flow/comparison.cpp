#include "flow/comparison.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "physical/report.hpp"
#include <map>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Raw = std::map<std::string, std::string>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "Phase 6 comparison: " + why);
}
void Same(const std::string &a, const std::string &b, const std::string &why) {
  SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
  RequireEquivalentJson(db, a, b, "Phase 6 comparison: " + why);
}
Raw SystemMetrics(Doc &report) {
  Doc timing(report.Token("system_timing_summary"));
  const auto status = timing.Text("status");
  Check(status == "pass" || status == "fail", "missing system timing");
  Raw result;
  for (const auto *domain : {"target_clock", "runtime_clock"}) {
    Doc clock(timing.Token(domain));
    for (const auto *field :
         {"worst_slack_bound_ns", "total_negative_slack_bound_ns",
          "negative_slack_paths"}) {
      (void)clock.Number(field);
      if (std::string(field) == "negative_slack_paths")
        Check(clock.Integer(field) >= 0, "negative path count");
      result.emplace(std::string(domain) + "_" + field, clock.Token(field));
    }
  }
  return result;
}
Raw Delta(const Raw &before, const Raw &after) {
  Raw result;
  for (const auto &entry : before) {
    const auto &b = entry.second;
    const auto &a = after.at(entry.first);
    const bool integer = b.find_first_of(".eE") == std::string::npos &&
                         a.find_first_of(".eE") == std::string::npos;
    result.emplace(
        entry.first,
        integer ? std::to_string(Doc::IntegerToken(a) - Doc::IntegerToken(b))
                : JsonRecord(ParseFiniteDecimal(a, "comparison delta") -
                             ParseFiniteDecimal(b, "comparison delta"))
                      .Encode());
  }
  return result;
}
} // namespace
void FinalizePhase6Comparison(const ArtifactRef &input, const fs::path &root) {
  Check(root.is_absolute() && fs::is_directory(root),
        "flow root is unavailable");
  Check(input.schema() == "phase6-comparison-input-v1" &&
            fs::path(input.path()) ==
                fs::weakly_canonical(root /
                                     "phase6-comparison/comparison-input.json"),
        "input is outside the flow owner");
  Doc request(Doc::Read(input));
  Check(request.Text("schema") == "emuflow.phase6-ab-comparison/v3" &&
            request.Text("status") == "pending-physical-comparison" &&
            request.Text("selected_provider") == "chimew" &&
            request.Text("baseline_provider") == "baseline" &&
            request.Text("qualification") ==
                "xilinx-placed-lookahead+fixed-lvds-wiring",
        "input identity disagrees");
  Doc frozen(request.Token("frozen_upstream"));
  frozen.Keys({"emuir_sha256", "assignment_sha256", "routes_sha256",
               "schedule_sha256", "platform_sha256"});
  for (const auto &entry : frozen.Fields()) {
    const auto hash = DecodeJsonString(entry.second);
    Check(hash.size() == 64 &&
              hash.find_first_not_of("0123456789abcdef") == std::string::npos,
          "invalid upstream seal");
  }
  std::vector<ArtifactRef> sources{input};
  auto read = [&](const fs::path &relative) {
    const auto path = root / relative;
    Check(!fs::is_symlink(path), "report is a symbolic link");
    sources.push_back(ArtifactRef::Bind(path.string(), "exact-sha256-text-v1"));
    return Doc::Read(sources.back());
  };
  const auto baseline_root = fs::path("phase6-comparison/baseline");
  Doc baseline_phase6(read(baseline_root / "split/phase6_report.json")),
      baseline_physical(read(baseline_root /
                             "physical/multi-fpga-physical-flow-report.json")),
      baseline_runtime(read(baseline_root / "runtime/phase7c_report.json"));
  Doc chimew_phase6(read("split/phase6_report.json")),
      chimew_physical(read("physical/multi-fpga-physical-flow-report.json")),
      chimew_runtime(read("runtime/phase7c_report.json"));
  Doc original(request.Token("baseline"));
  Same(original.Token("phase6"), baseline_phase6.text(),
       "baseline split report changed");
  Same(original.Token("physical"), baseline_physical.text(),
       "baseline physical report changed");
  Same(original.Token("runtime"), baseline_runtime.text(),
       "baseline runtime report changed");
  Check(baseline_phase6.Text("status") == "pass" &&
            chimew_phase6.Text("status") == "pass",
        "split arm failed");
  const auto design = chimew_runtime.Text("design"),
             platform = chimew_runtime.Text("platform");
  for (auto *doc : {&baseline_phase6, &chimew_phase6, &baseline_physical,
                    &chimew_physical, &baseline_runtime})
    Check(doc->Text("design") == design && doc->Text("platform") == platform,
          "arm design/platform disagrees");
  const auto baseline = ValidatePhysicalFlowReport(baseline_physical.text()),
             chimew = ValidatePhysicalFlowReport(chimew_physical.text());
  Same(baseline_physical.Token("backend"), chimew_physical.Token("backend"),
       "backend differs");
  Same(baseline_physical.Token("architecture"),
       chimew_physical.Token("architecture"), "device differs");
  Same(baseline_physical.Token("execution"), chimew_physical.Token("execution"),
       "execution options differ");
  Doc pin(request.Token("pin_plan_metrics"));
  Check(pin.Integer("signals") >= 0, "missing pin metrics");
  Raw before = Doc(baseline.metrics).Fields(),
      after = Doc(chimew.metrics).Fields();
  auto pdelta = Delta(before, after);
  pdelta.erase("unrouted_nets");
  pdelta.erase("drc_violations");
  const auto bsystem = SystemMetrics(baseline_runtime),
             csystem = SystemMetrics(chimew_runtime),
             sdelta = Delta(bsystem, csystem);
  auto report = request.Fields();
  for (const auto *field :
       {"phase6_runtime_seconds", "physical_runtime_seconds"})
    Check(original.Number(field) >= 0, "negative baseline runtime");
  Check(request.Number("chimew_phase6_runtime_seconds") >= 0 &&
            request.Number("chimew_physical_runtime_seconds") >= 0,
        "negative Chimew runtime");
  report["status"] = JsonQuote("pass");
  report["chimew"] =
      Doc::Encode({{"phase6", chimew_phase6.text()},
                   {"physical", chimew_physical.text()},
                   {"runtime", chimew_runtime.text()},
                   {"phase6_runtime_seconds",
                    request.Token("chimew_phase6_runtime_seconds")},
                   {"physical_runtime_seconds",
                    request.Token("chimew_physical_runtime_seconds")}});
  report["baseline_physical"] = baseline.metrics;
  report["chimew_physical"] = chimew.metrics;
  report["physical_delta"] = Doc::Encode(pdelta);
  report["baseline_system_timing"] = Doc::Encode(bsystem);
  report["chimew_system_timing"] = Doc::Encode(csystem);
  report["system_timing_delta"] = Doc::Encode(sdelta);
  report["validation"] = Doc::Encode(
      {{"status", JsonQuote("pass")},
       {"critical_path_delta_ns", pdelta.at("worst_critical_path_ns")},
       {"wns_delta_ns", pdelta.at("worst_wns_ns")},
       {"tns_delta_ns", pdelta.at("total_tns_ns")},
       {"failing_endpoint_delta", pdelta.at("failing_endpoints")},
       {"target_clock_wns_delta_ns",
        sdelta.at("target_clock_worst_slack_bound_ns")},
       {"target_clock_tns_delta_ns",
        sdelta.at("target_clock_total_negative_slack_bound_ns")},
       {"runtime_clock_wns_delta_ns",
        sdelta.at("runtime_clock_worst_slack_bound_ns")},
       {"runtime_clock_tns_delta_ns",
        sdelta.at("runtime_clock_total_negative_slack_bound_ns")}});
  for (const auto &source : sources)
    source.RequireUnchanged();
  PublishTextArtifact(
      (root / "phase6-comparison/comparison-report.json").string(), sources,
      [&](std::ostream &stream) { stream << Doc::Encode(report) << '\n'; });
}
} // namespace emuflow::core
