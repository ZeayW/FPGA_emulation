#include "physical/openparf/runtime.hpp"
#include "common/tool_runtime.hpp"
#include "physical/openparf/placement_records.hpp"
#include <fcntl.h>
#include <sys/file.h>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using namespace openparf_input;
using openparf_placement::Record;
struct Lease {
  int fd = -1;
  explicit Lease(const fs::path &path) {
    fd = open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open solver output lease");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "solver output already has an active owner");
    }
  }
  ~Lease() {
    if (fd >= 0)
      close(fd);
  }
};
R Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return A{g.device,
           g.inode,
           g.bytes,
           g.modified_seconds,
           g.modified_nanoseconds,
           g.changed_seconds,
           g.changed_nanoseconds};
}
bool Boolean(const R &r, const std::string &key) {
  const auto p = std::get_if<bool>(&r.at(key).value);
  Check(p, "invalid boolean " + key);
  return *p;
}
fs::path Relative(const fs::path &root, const std::string &name) {
  const fs::path p(name);
  Check(!name.empty() && !p.is_absolute(), "unsafe result/input path");
  for (const auto &part : p)
    Check(part != ".." && part != ".", "unsafe result/input path");
  const auto target = fs::weakly_canonical(root / p);
  auto relative = target.lexically_relative(root);
  Check(!relative.empty() && *relative.begin() != "..",
        "result/input escapes solver directory");
  return target;
}
R Relocate(R value, const fs::path &root) {
  if (auto p = std::get_if<std::string>(&value.value)) {
    fs::path path(*p);
    if (path.is_absolute()) {
      auto relative = path.lexically_relative(root);
      if (!relative.empty() && *relative.begin() != "..")
        return "$OPENPARF_OUTPUT/" + relative.string();
    }
  } else if (auto p = std::get_if<O>(&value.value)) {
    for (auto &item : *p)
      item.second = Relocate(std::move(item.second), root);
  } else if (auto p = std::get_if<A>(&value.value)) {
    for (auto &item : *p)
      item = Relocate(std::move(item), root);
  }
  return value;
}
R Fingerprints(const fs::path &root, const ArtifactRef &config,
               const std::optional<ArtifactRef> &manifest,
               const std::vector<ArtifactRef> &runtime,
               std::vector<ArtifactRef> &refs,
               const volatile std::sig_atomic_t *flag) {
  O hashes;
  std::set<std::string> names;
  if (manifest) {
    auto doc = Read(*manifest);
    Check(doc.at("schema").text() == "emuflow.openparf-atomic-manifest/v1" &&
              doc.at("status").text() == "pass",
          "solver manifest identity disagrees");
    for (const auto &file : doc.at("files").array())
      Check(names.insert(file.text()).second,
            "duplicate solver input inventory");
    names.insert("manifest.json");
    Check(names.count("openparf.json"),
          "solver manifest omits its configuration");
  } else
    names.insert(fs::path(config.path()).filename().string());
  for (const auto &name : names) {
    const auto path = Relative(root, name);
    const auto source =
        ArtifactRef::Bind(path.string(), "physical-input-document-v1");
    refs.push_back(source);
    hashes[name] = path == config.path()
                       ? Hash(Record(Relocate(Read(source), root)).Encode())
                       : FileHash(source, flag);
  }
  O tool;
  for (unsigned i = 0; i < runtime.size(); ++i)
    tool[std::to_string(i)] = FileHash(runtime[i], flag);
  return O{{"inputs", hashes}, {"runtime", tool}};
}
// Engine code is a fixed tool population, independent of the design graph.
std::map<std::string, ArtifactRef> Package(const fs::path &installation) {
  std::map<std::string, ArtifactRef> files;
  const auto root = fs::canonical(installation / "openparf");
  std::size_t visited = 0;
  for (const auto &entry : fs::recursive_directory_iterator(root)) {
    Check(++visited <= 32768, "OpenPARF package inventory exceeds bound");
    Check(!(entry.is_symlink() && entry.is_directory()),
          "OpenPARF package has an unsealed directory alias");
    if (!entry.is_regular_file())
      continue;
    const auto path = entry.path();
    const auto suffix = path.extension().string(),
               name = path.filename().string();
    if (suffix != ".py" && suffix != ".json" && suffix != ".so" &&
        suffix != ".dylib" && suffix != ".pyd" &&
        name.find(".so.") == std::string::npos)
      continue;
    files.emplace(
        path.lexically_relative(root).string(),
        ArtifactRef::Bind(path.string(), "physical-input-document-v1"));
  }
  return files;
}
void SamePackage(const fs::path &installation,
                 const std::map<std::string, ArtifactRef> &files) {
  const auto current = Package(installation);
  Check(current.size() == files.size(), "OpenPARF package inventory changed");
  for (const auto &entry : files) {
    entry.second.RequireUnchanged();
    Check(current.count(entry.first) &&
              current.at(entry.first).path() == entry.second.path() &&
              current.at(entry.first).generation() == entry.second.generation(),
          "OpenPARF package file changed");
  }
}
R Execute(const std::vector<std::string> &argv, const fs::path &directory,
          const fs::path &log, const std::vector<ArtifactRef> &refs,
          const R &env, const volatile std::sig_atomic_t *flag,
          std::uint64_t timeout) {
  ToolRequest tool;
  tool.argv = argv;
  tool.working_directory = directory.string();
  tool.timeout_ms = timeout;
  tool.tail_bytes = 8000;
  const auto fields = std::get_if<O>(&env.value);
  Check(fields, "invalid solver environment");
  for (const auto &entry : *fields) {
    Check(std::set<std::string>{"PYTHONPATH", "LD_LIBRARY_PATH"}.count(
              entry.first),
          "unsupported solver environment override");
    tool.environment.emplace_back(entry.first, entry.second.text());
  }
  auto result = RunExternalToolLogged(tool, log.string(), flag, refs);
  if (!result.operation.ok())
    throw StageError(
        result.operation.error,
        "OpenPARF " + std::string(timeout ? "runtime preflight" : "solver") +
            " failed: exit=" + std::to_string(result.return_code) +
            "; log=" + log.string() + "\n" + result.operation.message + "\n" +
            result.diagnostic_tail);
  return O{
      {"wall_seconds", result.wall_seconds},
      {"log", log.string()},
      {"log_sha256",
       FileHash(ArtifactRef::Bind(log.string(), "physical-input-document-v1"),
                flag)},
      {"engine_metrics",
       R::Parse(BoundedJson(ToolResultJson(result)).Token("engine_metrics"))}};
}
struct Reuse {
  fs::path placement, metrics;
  std::vector<ArtifactRef> refs;
};
std::optional<Reuse> Checkpoint(const fs::path &root, const R &fingerprints,
                                bool detailed,
                                const volatile std::sig_atomic_t *flag) {
  const auto file = root / "openparf-result-checkpoint.json";
  if (!fs::exists(file))
    return {};
  try {
    auto checkpoint =
        ArtifactRef::Bind(file.string(), "physical-input-document-v1");
    auto doc = Read(checkpoint);
    Check(doc.at("schema").text() == "emuflow.openparf-result-checkpoint/v2" &&
              doc.at("fingerprints").encode() == fingerprints.encode(),
          "stale solver checkpoint");
    const auto placement = Relative(root, doc.at("placement").text()),
               metrics = Relative(root, doc.at("metrics").text());
    Check(fs::path(placement).replace_extension(".native-metrics.json") ==
              metrics,
          "checkpoint metrics do not belong to placement");
    auto pl = ArtifactRef::Bind(placement.string(),
                                "physical-input-document-v1"),
         m = ArtifactRef::Bind(metrics.string(), "physical-input-document-v1");
    Check(FileHash(pl, flag) == doc.at("placement_sha256").text() &&
              FileHash(m, flag) == doc.at("metrics_sha256").text(),
          "stale solver result");
    (void)ValidateOpenparfMetrics(m, detailed);
    return Reuse{placement, metrics, {checkpoint, pl, m}};
  } catch (const StageError &) {
    Stop(flag);
    return {};
  }
}
} // namespace
StageResult RunOpenparfStage(const ArtifactRef &request,
                             const std::string &report,
                             const volatile std::sig_atomic_t *flag) {
  std::optional<R> config;
  std::vector<ArtifactRef> refs, published;
  std::map<std::string, ArtifactRef> owners;
  return RunStage(
      "physical.openparf-runtime",
      [&] {
        Check(request.schema() == "openparf-runtime-request-v1",
              "runtime request artifact schema disagrees");
        config = Read(request);
        BoundedJson shape(config->encode());
        shape.Keys({"schema", "mode", "sources", "python", "installation",
                    "environment", "allow_detailed_legalization_handoff",
                    "use_checkpoint"});
        Check(config->at("schema").text() ==
                  "emuflow.openparf-runtime-request/v1",
              "runtime request version disagrees");
        refs.push_back(request);
        for (const auto &s : config->at("sources").array()) {
          const auto role = s.at("role").text();
          Check(std::set<std::string>{"config", "manifest", "metrics", "driver"}
                    .count(role),
                "invalid solver source role");
          auto ref = ArtifactRef::Bind(s.at("path").text(),
                                       "physical-input-document-v1");
          Check(Generation(ref).encode() == s.at("generation").encode(),
                "solver source generation changed");
          Check(owners.emplace(role, ref).second,
                "duplicate solver source role");
          refs.push_back(ref);
        }
        return refs;
      },
      [&](const auto &) {
        auto &c = *config;
        const auto target = fs::absolute(report);
        Check(!fs::is_symlink(target), "solver report is a symlink");
        fs::create_directories(target.parent_path());
        for (const auto &r : refs) {
          std::error_code error;
          Check(fs::weakly_canonical(target) != r.path() &&
                    !fs::equivalent(target, r.path(), error),
                "solver report aliases an input");
        }
        const auto mode = c.at("mode").text();
        Check(mode == "run" || mode == "preflight" || mode == "metrics",
              "unsupported OpenPARF runtime mode");
        const bool detailed = Boolean(c, "allow_detailed_legalization_handoff"),
                   use_checkpoint = Boolean(c, "use_checkpoint");
        Lease lease(target.string() + ".lock");
        R result = O{{"schema", "emuflow.openparf-runtime-report/v1"},
                     {"status", "pass"},
                     {"implementation_id", "native-openparf-runtime-v1"},
                     {"mode", mode}};
        if (mode == "metrics") {
          Check(owners.size() == 1 && owners.count("metrics"),
                "metrics validation requires its sole source");
          const auto &m = owners.at("metrics");
          result["metrics"] =
              R::Parse(ValidateOpenparfMetrics(m, detailed).EncodeUtf8());
          published.push_back(m);
        } else {
          const auto python = c.at("python").text(),
                     installation = c.at("installation").text();
          Check(fs::path(python).is_absolute() &&
                    fs::path(installation).is_absolute() &&
                    fs::is_directory(fs::path(installation) / "openparf"),
                "OpenPARF runtime layout is invalid");
          const std::vector<ArtifactRef> runtime = {
              ArtifactRef::Bind(python, "external-tool-executable-v1"),
              ArtifactRef::Bind(
                  (fs::path(installation) / "openparf.py").string(),
                  "physical-input-document-v1")};
          refs.insert(refs.end(), runtime.begin(), runtime.end());
          const auto package = Package(installation);
          for (const auto &entry : package)
            refs.push_back(entry.second);
          result["runtime"] = O{{"status", "pass"},
                                {"installation", installation},
                                {"python", python}};
          fs::path root = target.parent_path();
          if (mode == "run") {
            Check(owners.count("config") && owners.count("driver") &&
                      !owners.count("metrics"),
                  "solver sources are incomplete");
            root = fs::path(owners.at("config").path()).parent_path();
          } else
            Check(owners.empty(), "preflight has unexpected sources");
          R settings;
          std::string benchmark;
          if (mode == "run") {
            settings = Read(owners.at("config"));
            Check(!settings.has("emuflow_continuous_global_guidance"),
                  "continuous guidance placement is retired; use stable native "
                  "placement");
            Check(Boolean(settings, "emuflow_stable_global_placement"),
                  "stable native OpenPARF driver must be enabled");
            benchmark = settings.at("benchmark_name").text();
            Check(!benchmark.empty() && benchmark != "." && benchmark != ".." &&
                      fs::path(benchmark).filename() == benchmark,
                  "invalid OpenPARF benchmark name");
          }
          // A fresh invocation owns all tool outputs. Failed retries retain
          // their diagnostics and cannot truncate a previously accepted result
          // pair.
          auto pattern = (root / "solver.generation.XXXXXX").string();
          Check(mkdtemp(pattern.data()), "cannot create solver generation");
          const fs::path work(pattern);
          result["preflight"] =
              Execute({python, "-c",
                       "import torch; from openparf.flow import place, route"},
                      root, work / "preflight.log", refs, c.at("environment"),
                      flag, 120000);
          Check(fs::canonical(python) == runtime[0].path(),
                "solver interpreter alias changed");
          if (mode == "run") {
            const auto &source = owners.at("config");
            std::optional<ArtifactRef> manifest;
            if (owners.count("manifest")) {
              manifest = owners.at("manifest");
              Check(fs::path(manifest->path()) == root / "manifest.json" &&
                        fs::path(source.path()) == root / "openparf.json",
                    "solver manifest/config layout disagrees");
            }
            auto identity = runtime;
            identity.push_back(owners.at("driver"));
            auto fingerprints =
                Fingerprints(root, source, manifest, identity, refs, flag);
            O implementation;
            for (const auto &entry : package)
              implementation[entry.first] = FileHash(entry.second, flag);
            O inherited;
            for (const auto *key :
                 {"LD_PRELOAD", "PYTHONHOME", "PATH", "CUDA_VISIBLE_DEVICES"})
              if (const auto value = std::getenv(key))
                inherited[key] = std::string(value);
            fingerprints["implementation"] = implementation;
            fingerprints["runtime_control"] =
                O{{"python", python},
                  {"installation", installation},
                  {"environment", c.at("environment")},
                  {"inherited", inherited}};
            auto reused = use_checkpoint
                              ? Checkpoint(root, fingerprints, detailed, flag)
                              : std::optional<Reuse>{};
            fs::path placement, metrics;
            if (reused) {
              placement = reused->placement;
              metrics = reused->metrics;
              refs.insert(refs.end(), reused->refs.begin(), reused->refs.end());
            } else {
              settings["result_dir"] = (work / "results").string();
              const auto execution = work / "openparf.json";
              Json(execution, settings, refs);
              refs.push_back(ArtifactRef::Bind(execution.string(),
                                               "physical-input-document-v1"));
              result["solver"] =
                  Execute({python, owners.at("driver").path(), "--config",
                           execution.string(), "--log",
                           (work / "openparf.log").string()},
                          root, work / "openparf.console.log", refs,
                          c.at("environment"), flag, 0);
              Check(fs::canonical(python) == runtime[0].path(),
                    "solver interpreter alias changed");
              placement = work / "results" / (benchmark + ".pl");
              metrics = placement;
              metrics.replace_extension(".native-metrics.json");
            }
            SamePackage(installation, package);
            auto pl = ArtifactRef::Bind(placement.string(),
                                        "physical-input-document-v1"),
                 m = ArtifactRef::Bind(metrics.string(),
                                       "physical-input-document-v1");
            Check(pl.generation().bytes > 0,
                  "OpenPARF completed without nonempty placement");
            result["metrics"] =
                R::Parse(ValidateOpenparfMetrics(m, detailed).EncodeUtf8());
            refs.push_back(pl);
            refs.push_back(m);
            published.push_back(pl);
            published.push_back(m);
            result["placement"] = placement.string();
            result["metrics_path"] = metrics.string();
            result["reused"] = bool(reused);
            result["metrics_artifact"] = O{{"path", metrics.string()},
                                           {"bytes", m.generation().bytes},
                                           {"sha256", FileHash(m, flag)}};
            if (use_checkpoint) {
              const auto checkpoint = root / "openparf-result-checkpoint.json";
              if (!reused)
                Json(checkpoint,
                     O{{"schema", "emuflow.openparf-result-checkpoint/v2"},
                       {"fingerprints", fingerprints},
                       {"placement",
                        placement.lexically_relative(root).string()},
                       {"metrics", metrics.lexically_relative(root).string()},
                       {"placement_sha256", FileHash(pl, flag)},
                       {"metrics_sha256", FileHash(m, flag)}},
                     refs);
              const auto owner = ArtifactRef::Bind(
                  checkpoint.string(), "physical-input-document-v1");
              result["checkpoint"] = checkpoint.string();
              refs.push_back(owner);
              published.push_back(owner);
            }
          }
          SamePackage(installation, package);
        }
        Stop(flag);
        Json(target, result, refs);
        published.insert(
            published.begin(),
            ArtifactRef::Bind(target.string(), "openparf-runtime-report-v1"));
      },
      [&] {
        for (const auto &r : refs)
          r.RequireUnchanged();
        for (const auto &r : published)
          r.RequireUnchanged();
        return published;
      });
}
} // namespace emuflow::core
