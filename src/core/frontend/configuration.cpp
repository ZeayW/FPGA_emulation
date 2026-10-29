#include "frontend/configuration.hpp"
#include "board/model.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include <algorithm>
#include <filesystem>
#include <map>
#include <optional>
#include <regex>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using D = BoundedJson;
using O = std::map<std::string, std::string>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidRequest,
                     "frontend configuration: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "frontend configuration cancelled");
}
std::string Array(const std::vector<std::string> &values) {
  std::string out = "[";
  for (const auto &v : values) {
    if (out.size() > 1)
      out += ',';
    out += v;
  }
  return out + ']';
}
std::string Strings(const std::vector<std::string> &values) {
  std::vector<std::string> out;
  for (const auto &v : values)
    out.push_back(JsonQuote(v));
  return Array(out);
}
std::vector<std::string> Names(const std::string &value) {
  std::vector<std::string> out;
  for (const auto &v : D::Array(value))
    out.push_back(DecodeJsonString(v));
  return out;
}
std::string Absolute(const std::string &path) {
  Check(!path.empty() && fs::path(path).is_absolute(),
        "source/output paths must be absolute");
  return path;
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Source(const std::string &role, const ArtifactRef &ref) {
  return D::Encode({{"role", JsonQuote(role)},
                    {"path", JsonQuote(ref.path())},
                    {"schema", JsonQuote(ref.schema())},
                    {"generation", Generation(ref)}});
}
void Catalog(CheckedRequestWriter &writer,
             const std::vector<std::string> &values) {
  writer.Put<std::uint32_t>(values.size());
  for (const auto &v : values)
    writer.Text(v);
}
struct Configuration {
  std::string mode, output, top, platform, periods, profile, strategy, cwd,
      script, synthesis, engine_invocation;
  std::vector<std::string> clocks, rtl, includes, defines, records;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> bound;
  std::optional<ArtifactRef> library, engine;
  const volatile std::sig_atomic_t *cancelled;
  explicit Configuration(const volatile std::sig_atomic_t *flag)
      : cancelled(flag) {}
  ArtifactRef Bind(const std::string &path, const std::string &schema) {
    Stop(cancelled);
    auto ref = ArtifactRef::Bind(Absolute(path), schema);
    auto p = bound.find(ref.path());
    if (p == bound.end()) {
      Check(bound.size() < 16384, "source catalog exceeds bound");
      bound.emplace(ref.path(), ref);
      sources.push_back(ref);
    } else
      Check(p->second.generation() == ref.generation(),
            "source generation changed during configuration");
    return ref;
  }
  void Add(const std::string &role, const std::string &path,
           const std::string &schema) {
    records.push_back(Source(role, Bind(path, schema)));
  }
  void Prepared(const fs::path &frontend, const fs::path &timing) {
    auto phase1 =
        fs::is_directory(frontend / "phase1") ? frontend / "phase1" : frontend;
    Add("emuir", (phase1 / "design.emuir.sqlite").string(), "emuir-compact-v2");
    auto report = phase1 / "phase1_report.json";
    if (!fs::is_regular_file(report))
      report = phase1 / "phase1_report.source.json";
    Add("phase1-report", report.string(), "frontend-input-report-v1");
    Add("phase1-board", (phase1 / "platform.normalized.json").string(),
        "boarddb-v1");
    for (bool paths : {true, false}) {
      const auto selector =
          Bind((timing /
                (paths ? "path-database.json" : "partition-net-weights.json"))
                   .string(),
               paths ? "sta-path-selector-v1" : "net-weight-selector-v2");
      D manifest(D::Read(selector));
      D payloads(manifest.Token("payloads"));
      const auto member =
          D(payloads.Token(paths ? "paths" : "weights")).Text("path");
      Check(!member.empty() && fs::path(member).filename() == member &&
                member != "." && member != "..",
            "prepared payload must be a sibling of its manifest");
      records.push_back(
          Source(paths ? "path-selector" : "weight-selector", selector));
      Add(paths ? "path-store" : "weight-store", (timing / member).string(),
          paths ? "timing-path-store-v1" : "net-weight-store-v1");
    }
    const auto report2 = timing / "experiment-timing-report.json";
    if (fs::is_regular_file(report2))
      Add("timing-report", report2.string(), "frontend-input-report-v1");
  }
  void Rtl(D &config) {
    Check(!rtl.empty() && !top.empty(),
          "native RTL frontend requires sources and top");
    Check(std::regex_match(top, std::regex("[A-Za-z_][A-Za-z0-9_$]*")),
          "unsupported Yosys identifier");
    const std::set<std::string> strategies{
        "hierarchical-abc9-v1", "hierarchical-classic-v1", "flatten-classic-v1",
        "flatten-abc9-v1"};
    Check(strategies.count(strategy), "unsupported Xilinx mapping strategy");
    library = Bind(config.Text("library"), "xilinx-primitive-library-v1");
    engine_invocation = Absolute(config.Text("engine"));
    engine = Bind(engine_invocation, "external-tool-executable-v1");
    Check(access(engine->path().c_str(), X_OK) == 0,
          "Yosys engine is not executable");
    std::vector<std::string> resolved;
    for (const auto &path : rtl)
      resolved.push_back(Bind(path, "frontend-source-v1").path());
    rtl = std::move(resolved);
    for (auto &path : includes) {
      Absolute(path);
      Check(fs::is_directory(path), "Verilog include directory does not exist");
      path = fs::canonical(path).string();
      Check(std::regex_match(path, std::regex("[A-Za-z0-9_./:+-]+")),
            "unsupported Yosys include directory; paths must not contain "
            "whitespace or command separators");
    }
    for (const auto &value : defines)
      Check(std::regex_match(value,
                             std::regex("[A-Za-z_][A-Za-z0-9_]*(=[^\\s;]+)?")),
            "unsupported Yosys define");
    const auto checksums = config.Token("source_checksums");
    if (checksums != "null") {
      const auto declared = D(checksums).Fields();
      Check(!declared.empty() && declared.size() <= 16384,
            "invalid design source checksum inventory");
      for (const auto &item : declared) {
        const auto digest = DecodeJsonString(item.second);
        Check(std::regex_match(digest, std::regex("[0-9a-f]{64}")),
              "invalid design source checksum reference");
        const auto source = Bind(item.first, "frontend-source-v1");
        Check(Sha256File(source, cancelled) == digest,
              "design RTL/include changed after run preparation");
        source.RequireUnchanged();
      }
      for (const auto &path : rtl)
        Check(declared.count(path),
              "design source checksum coverage is incomplete");
    }
    script = "read_verilog -sv";
    for (const auto &path : includes)
      script += " -I" + path;
    for (const auto &value : defines)
      script += " -D" + value;
    for (const auto &path : rtl)
      script += ' ' + JsonQuote(path);
    script += "; hierarchy -check -top " + top +
              "; synth_xilinx -family xcup -top " + top +
              " -noiopad -noclkbuf -uram -nolutram -nosrl -run begin:check";
    if (strategy.rfind("flatten-", 0) == 0)
      script += " -flatten";
    if (strategy.find("-abc9-") != std::string::npos)
      script += " -abc9";
    script += "; blackbox =A:whitebox; check; delete t:$scopeinfo; setattr "
              "-set KEEP \"yes\" c:*; setattr -set DONT_TOUCH \"yes\" c:*; "
              "write_json -noscopeinfo";
    const auto tool = D::Encode(
        {{"status", "\"pass\""},
         {"provider", "\"yosys-synth-xilinx\""},
         {"family", "\"xcup\""},
         {"policy", "\"native\""},
         {"mapping_profile", JsonQuote(profile)},
         {"mapping_strategy", JsonQuote(strategy)},
         {"interchange", "\"stdout-hierarchical-to-indexed-phase1\""}});
    synthesis =
        D::Encode({{"provider", "\"yosys\""},
                   {"mode", "\"xilinx-ultrascaleplus-open\""},
                   {"mapping_profile", JsonQuote(profile)},
                   {"sources", Strings(rtl)},
                   {"include_dirs", Strings(includes)},
                   {"defines", Strings(defines)},
                   {"interchange_retention", "\"never-materialized-stream\""},
                   {"tool_report", tool}});
    Check(synthesis.size() <= 1024 * 1024,
          "frontend synthesis configuration exceeds bound");
  }
  void Read(const ArtifactRef &request) {
    Check(request.schema() == "frontend-configuration-request-v1",
          "request schema disagrees");
    sources.push_back(request);
    D config(D::Read(request, 4 * 1024 * 1024));
    config.Keys({"schema",
                 "mode",
                 "output_dir",
                 "working_directory",
                 "top",
                 "clocks",
                 "clock_periods",
                 "mapping_profile",
                 "mapping_strategy",
                 "platform",
                 "board",
                 "board_generation",
                 "library",
                 "engine",
                 "rtl_sources",
                 "include_dirs",
                 "defines",
                 "source_checksums",
                 "prepared_frontend",
                 "prepared_timing",
                 "yosys_json"});
    Check(config.Text("schema") == "emuflow.frontend-configuration/v1",
          "request version disagrees");
    mode = config.Text("mode");
    output = Absolute(config.Text("output_dir"));
    cwd = Absolute(config.Text("working_directory"));
    Check(fs::is_directory(cwd), "invocation working directory is unavailable");
    top = config.Text("top");
    profile = config.Text("mapping_profile");
    Check(profile == "xilinx-ultrascaleplus-open-v1",
          "unsupported mapping profile");
    platform = config.Token("platform");
    Check(platform.size() <= 1024 * 1024,
          "frontend board configuration exceeds bound");
    D board(platform);
    Check(board.Text("schema") == "emuflow.boarddb/v1",
          "invalid BoardDB version");
    periods = config.Token("clock_periods");
    D periods_doc(periods);
    clocks = Names(config.Token("clocks"));
    std::sort(clocks.begin(), clocks.end());
    clocks.erase(std::unique(clocks.begin(), clocks.end()), clocks.end());
    rtl = Names(config.Token("rtl_sources"));
    includes = Names(config.Token("include_dirs"));
    defines = Names(config.Token("defines"));
    const auto raw_strategy = config.Token("mapping_strategy");
    strategy = raw_strategy == "null" ? "hierarchical-abc9-v1"
                                      : DecodeJsonString(raw_strategy);
    const auto p1 = config.Token("prepared_frontend"),
               p2 = config.Token("prepared_timing"),
               json = config.Token("yosys_json");
    if (mode == "rtl") {
      Check(p1 == "null" && p2 == "null" && json == "null",
            "RTL mode cannot adopt supplied inputs");
      Rtl(config);
    } else {
      Check(config.Token("source_checksums") == "null",
            "source checksums apply only to original RTL compilation");
      Check(rtl.empty(),
            "supplied/prepared input cannot be combined with RTL sources");
      if (mode == "prepared") {
        Check(p1 != "null" && p2 != "null" && json == "null",
              "prepared continuation requires both Phase 1 and Phase 2 "
              "boundaries and no synthesis inputs");
        Prepared(Absolute(DecodeJsonString(p1)),
                 Absolute(DecodeJsonString(p2)));
      } else {
        Check(mode == "yosys-json" && p1 == "null" && p2 == "null" &&
                  json != "null" && includes.empty() && defines.empty(),
              "supplied Yosys JSON requires no RTL, include directories or "
              "defines");
        Add("json", DecodeJsonString(json), "yosys-source-document-v1");
        Add("library", config.Text("library"), "xilinx-primitive-library-v1");
      }
    }
    const auto board_ref = Bind(config.Text("board"), "boarddb-v1");
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(compare, config.Token("board_generation"),
                          Generation(board_ref),
                          "frontend BoardDB changed during configuration");
    const auto normalized_board = NormalizeBoard(D::Read(board_ref));
    RequireEquivalentJson(
        compare, platform, normalized_board,
        "frontend normalized BoardDB differs from its source");
    platform = normalized_board;
    if (mode != "rtl")
      records.push_back(Source("board", board_ref));
    if (mode == "rtl") {
      // Complete checked binary size, including both catalogs and every lease.
      std::uint64_t bytes = 96;
      const auto text = [&](const std::string &value) {
        bytes += 4 + value.size();
      };
      for (const auto &value :
           {(fs::path(output) / "frontend/phase1").string(), top, platform,
            synthesis, cwd, library->path(), engine_invocation,
            std::string("-q"), std::string("-p"), script})
        text(value);
      for (const auto &clock : clocks)
        text(clock);
      for (const auto &source : sources)
        if (source.path() != sources.front().path() &&
            source.path() != library->path()) {
          text(source.path());
          bytes += 56;
        }
      Check(bytes <= 4 * 1024 * 1024, "native frontend request exceeds bound");
    }
    for (const auto &source : sources)
      source.RequireUnchanged();
  }
  void Write(const fs::path &target) {
    Stop(cancelled);
    if (mode == "rtl") {
      CheckedRequestWriter writer(target.string(), sources);
      writer.Put<std::uint64_t>(0x31544e52464d5545ULL);
      writer.Put<std::uint32_t>(1);
      writer.Text((fs::path(output) / "frontend/phase1").string());
      writer.Text(top);
      Catalog(writer, clocks);
      writer.Text(platform);
      writer.Text(synthesis);
      writer.Put<std::uint32_t>(0);
      Catalog(writer, {engine_invocation, "-q", "-p", script});
      writer.Text(cwd);
      writer.Put<std::uint64_t>(0);
      writer.Text(library->path());
      writer.Generation(library->generation());
      std::vector<ArtifactRef> rest;
      for (const auto &source : sources)
        if (source.path() != sources.front().path() &&
            source.path() != library->path())
          rest.push_back(source);
      writer.Put<std::uint32_t>(rest.size());
      for (const auto &source : rest) {
        writer.Text(source.path());
        writer.Generation(source.generation());
      }
      writer.Finish();
    } else {
      const auto value =
          D::Encode({{"schema", "\"emuflow.frontend-input-request/v1\""},
                     {"mode", JsonQuote(mode)},
                     {"sources", Array(records)},
                     {"top", JsonQuote(top)},
                     {"clocks", Strings(clocks)},
                     {"clock_periods", periods},
                     {"mapping_profile", JsonQuote(profile)},
                     {"platform", platform}});
      PublishTextArtifact(target.string(), sources,
                          [&](std::ostream &out) { out << value << '\n'; });
    }
  }
};
} // namespace
StageResult
PrepareFrontendConfiguration(const ArtifactRef &request,
                             const std::string &directory,
                             const volatile std::sig_atomic_t *cancelled) {
  Configuration config(cancelled);
  fs::path target;
  return RunStage(
      "phase1.prepare-configuration",
      [&] {
        Absolute(directory);
        config.Read(request);
        target = fs::path(directory) / (config.mode == "rtl"
                                            ? "native-frontend-request.bin"
                                            : "inputs.json");
        Check(!fs::exists(target) && !fs::is_symlink(target),
              "request destination already exists");
        return config.sources;
      },
      [&](const auto &) {
        fs::create_directories(directory);
        config.Write(target);
      },
      [&] {
        return std::vector<ArtifactRef>{ArtifactRef::Bind(
            target.string(), config.mode == "rtl"
                                 ? "frontend-request-v1"
                                 : "frontend-input-request-v1")};
      });
}
} // namespace emuflow::core
