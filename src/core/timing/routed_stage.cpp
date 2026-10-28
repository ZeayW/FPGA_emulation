#include "timing/routed_stage.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/exact_sum.hpp"
#include "common/hash.hpp"
#include "common/tool_runtime.hpp"
#include "storage/schema/sta_path_schema.hpp"
#include "timing/opensta_export.hpp"
#include "timing/routed_model.hpp"
#include "timing/routed_opensta.hpp"
#include "timing/sta_paths.hpp"
#include <array>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <optional>
#include <regex>
#include <sys/file.h>
#include <unistd.h>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Fields = std::map<std::string, std::string>;
using I = std::int64_t;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact, "routed OpenSTA: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "routed OpenSTA cancelled");
}
std::string Hash(const ArtifactRef &source,
                 const volatile std::sig_atomic_t *flag) {
  std::ifstream in(source.path(), std::ios::binary);
  Check(bool(in), "cannot read source");
  Sha256 hash;
  std::array<char, 65536> buffer{};
  while (in) {
    Stop(flag);
    in.read(buffer.data(), buffer.size());
    hash.update(reinterpret_cast<unsigned char *>(buffer.data()), in.gcount());
  }
  Check(in.eof(), "source read failed");
  source.RequireUnchanged();
  std::string result;
  for (auto c : hash.finish()) {
    result += "0123456789abcdef"[c >> 4];
    result += "0123456789abcdef"[c & 15];
  }
  return result;
}
Doc Read(const ArtifactRef &source) { return Doc(Doc::Read(source)); }
void Write(const fs::path &path, const std::string &text,
           const std::vector<ArtifactRef> &sources) {
  PublishTextArtifact(path.string(), sources,
                      [&](auto &out) { out << text << '\n'; });
}
std::string Generation(const ArtifactRef &r) {
  const auto &g = r.generation();
  return "[" + std::to_string(g.device) + "," + std::to_string(g.inode) + "," +
         std::to_string(g.bytes) + "," + std::to_string(g.modified_seconds) +
         "," + std::to_string(g.modified_nanoseconds) + "," +
         std::to_string(g.changed_seconds) + "," +
         std::to_string(g.changed_nanoseconds) + "]";
}
std::string Array(const std::vector<std::string> &values) {
  std::vector<JsonRecord> out;
  for (const auto &v : values)
    out.emplace_back(v);
  return JsonRecord(out).Encode();
}
struct Workspace {
  fs::path root;
  bool accepted = false;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".routed-opensta.generation.XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create private generation");
    root = name;
  }
  ~Workspace() {
    std::error_code e;
    if (accepted)
      fs::remove_all(root, e);
    else
      for (const auto &entry : fs::directory_iterator(root, e))
        if (entry.path().extension() != ".log")
          fs::remove_all(entry.path(), e);
  }
};
struct Lease {
  int fd = -1;
  explicit Lease(const fs::path &path) {
    fd = open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open path output lease");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "path output already has an active owner");
    }
  }
  ~Lease() {
    if (fd >= 0)
      close(fd);
  }
};
fs::path Destination(const std::string &value,
                     const std::vector<ArtifactRef> &refs) {
  const fs::path path(value);
  Check(path.is_absolute() && !fs::is_symlink(path),
        "output must be an absolute file destination");
  fs::create_directories(path.parent_path());
  const auto result = fs::canonical(path.parent_path()) / path.filename();
  for (const auto &ref : refs) {
    std::error_code e;
    Check(result != ref.path() && !fs::equivalent(result, ref.path(), e),
          "output aliases an input");
  }
  return result;
}
std::string Hex(const std::string &s) {
  std::string out;
  for (unsigned char c : s) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
ArtifactRef Request(const fs::path &path, std::uint64_t magic,
                    const std::vector<ArtifactRef> &refs,
                    const std::string &text, const std::string &schema) {
  CheckedRequestWriter w(path.string(), refs);
  w.Put<std::uint64_t>(magic);
  w.Put<std::uint32_t>(1);
  for (const auto &r : refs)
    w.Generation(r.generation());
  w.Text(text);
  w.Finish();
  return ArtifactRef::Bind(path.string(), schema);
}
ToolResult Execute(
    const std::vector<std::string> &argv, const fs::path &directory,
    const fs::path &log, const std::vector<ArtifactRef> &refs,
    const volatile std::sig_atomic_t *flag,
    const std::vector<std::pair<std::string, std::string>> &environment = {}) {
  ToolRequest tool;
  tool.argv = argv;
  tool.working_directory = directory.string();
  tool.environment = environment;
  tool.tail_bytes = 8000;
  tool.termination_grace_ms = 10000;
  auto result = RunExternalToolLogged(tool, log.string(), flag, refs);
  if (!result.operation.ok())
    throw StageError(result.operation.error,
                     "OpenSTA failed; log=" + log.string() + "\n" +
                         result.operation.message + "\n" +
                         result.diagnostic_tail);
  return result;
}
std::string Engine(const std::map<std::string, ArtifactRef> &owners,
                   const fs::path &work, const std::vector<ArtifactRef> &refs,
                   const volatile std::sig_atomic_t *flag) {
  std::vector<std::pair<std::string, std::string>> env;
  if (owners.count("container"))
    env.emplace_back("EMUFLOW_OPENSTA_CONTAINER_BINARY",
                     owners.at("container").path());
  auto version = Execute({owners.at("sta").path(), "-version"}, work,
                         work / "version.log", refs, flag, env);
  std::smatch match;
  Check(std::regex_search(
            version.diagnostic_tail, match,
            std::regex(R"((?:^|\s)([0-9]+)\.([0-9]+)\.([0-9]+)(?:\s|$))")),
        "engine has no semantic version");
  Check(std::stoull(match[1]) > 3 ||
            (std::stoull(match[1]) == 3 && std::stoull(match[2]) >= 1),
        "unsupported OpenSTA version");
  const auto runtime = Execute({owners.at("sta").path(), "-no_init", "-exit",
                                owners.at("runtime-script").path()},
                               work, work / "runtime.log", refs, flag, env);
  Check(runtime.diagnostic_tail.find("EMUFLOW_OPENSTA_RUNTIME status=pass") !=
            std::string::npos,
        "OpenSTA Tcl runtime qualification failed");
  Fields identity{
      {"name", JsonQuote("OpenSTA")},
      {"version",
       JsonQuote(match[1].str() + "." + match[2].str() + "." + match[3].str())},
      {"executable_sha256",
       JsonQuote(Hash(
           owners.at(owners.count("container") ? "container" : "sta"), flag))}};
  if (owners.count("container"))
    identity["launcher_sha256"] = JsonQuote(Hash(owners.at("sta"), flag));
  return Doc::Encode(identity);
}
struct PathFacts {
  std::string qor;
  double critical = 0;
  I paths = 0;
};
PathFacts Qor(const ArtifactRef &selector, std::vector<ArtifactRef> &refs,
              const volatile std::sig_atomic_t *flag) {
  auto header = Read(selector);
  Check(header.Text("schema") == "emuflow.sta-path-database/v3",
        "only the current typed path owner is supported");
  Doc payloads(header.Token("payloads")), description(payloads.Token("paths"));
  const auto name = description.Text("path");
  Check(!name.empty() && fs::path(name).filename() == name && name != "." &&
            name != ".." && description.Text("format") == "sqlite-sta-path/v1",
        "invalid path payload descriptor");
  auto owner = ArtifactRef::Bind(
      (fs::path(selector.path()).parent_path() / name).string(),
      "timing-path-store-v1");
  Check(fs::path(owner.path()).parent_path() ==
                fs::path(selector.path()).parent_path() &&
            Hash(owner, flag) == description.Text("sha256"),
        "path payload seal/owner disagrees");
  refs.push_back(owner);
  SqliteStore store(owner);
  auto &db = store.database();
  RequireSqliteDDL(db, kStaPathStoreDDL);
  auto rows =
      db.Prepare("SELECT "
                 "ordinal,slack_ns,fixed_delay_ns,clock_period_ns,"
                 "start_object,end_object,path_id,clock_domain,start_instance,"
                 "start_port,start_bit,"
                 "end_instance,end_port,end_bit FROM paths ORDER BY ordinal");
  I count = 0, failing = 0, structured = 0;
  double worst = 0, maximum = 0;
  ExactSum tns;
  while (rows.Step()) {
    Stop(flag);
    Check(rows.TypedInteger(0) == count, "path ordinal coverage disagrees");
    const auto slack = rows.TypedReal(1), delay = rows.TypedReal(2),
               period = rows.TypedReal(3);
    Check(std::isfinite(slack) && std::isfinite(delay) && delay >= 0 &&
              std::isfinite(period) && period > 0,
          "invalid timing path number");
    if (!count || slack < worst)
      worst = slack;
    ++count;
    maximum = std::max(maximum, delay);
    if (slack < 0) {
      tns.Add(slack);
      ++failing;
    }
    for (const auto field : {6, 7}) {
      const auto name = rows.TypedText(field);
      Check(!name.empty() && name.find('\0') == std::string::npos,
            "empty path/clock identity");
      (void)JsonQuoteUtf8(name);
    }
    Check(rows.IsNull(4) == rows.IsNull(5), "incomplete path endpoint pair");
    if (!rows.IsNull(4)) {
      for (const auto first : {8, 11}) {
        const auto object = rows.TypedText(first == 8 ? 4 : 5),
                   port = rows.TypedText(first + 1);
        Check(!object.empty() && !port.empty() &&
                  rows.TypedInteger(first + 2) >= 0,
              "invalid structured endpoint");
        if (!rows.IsNull(first))
          Check(!rows.TypedText(first).empty(), "empty endpoint instance");
      }
      ++structured;
    } else
      for (const auto field : {8, 9, 10, 11, 12, 13})
        Check(rows.IsNull(field), "unstructured path has endpoint fields");
  }
  Check(count > 0, "no mapped timing paths");
  auto members = db.Prepare(
      "SELECT "
      "path_ordinal,min(net_ordinal),max(net_ordinal),count(*),count(DISTINCT "
      "net_id) FROM path_nets GROUP BY path_ordinal ORDER BY path_ordinal");
  I groups = 0;
  while (members.Step()) {
    Stop(flag);
    Check(members.TypedInteger(0) == groups++ && members.TypedInteger(1) == 0 &&
              members.TypedInteger(2) + 1 == members.TypedInteger(3) &&
              members.TypedInteger(3) == members.TypedInteger(4),
          "path-net membership coverage/order disagrees");
  }
  Check(groups == count, "path has no net membership");
  auto orphan =
      db.Prepare("SELECT 1 FROM path_nets n LEFT JOIN paths p ON "
                 "p.ordinal=n.path_ordinal WHERE p.ordinal IS NULL LIMIT 1");
  Check(!orphan.Step(), "orphan path net");
  auto unique = db.Prepare("SELECT count(DISTINCT net_id) FROM path_nets");
  Check(unique.Step(), "missing path-net inventory");
  const auto nets = unique.TypedInteger(0);
  Doc summary(header.Token("summary"));
  Check(description.Integer("records") == count &&
            summary.Integer("paths") == count &&
            summary.Integer("structured_endpoint_paths") == structured &&
            summary.Integer("unique_path_nets") == nets,
        "path selector counts disagree");
  auto qor = Doc::Encode({{"wns_ns", JsonRecord(worst).Encode()},
                          {"tns_ns", JsonRecord(tns.Finish()).Encode()},
                          {"failing_endpoints", std::to_string(failing)},
                          {"timed_endpoints", std::to_string(count)}});
  Doc declared(summary.Token("qor")), actual(qor);
  for (const auto *key : {"wns_ns", "tns_ns"}) {
    const auto a = declared.Number(key), b = actual.Number(key);
    Check(std::abs(a - b) <=
              std::max(1e-9, 1e-9 * std::max(std::abs(a), std::abs(b))),
          "path QoR disagrees");
  }
  for (const auto *key : {"failing_endpoints", "timed_endpoints"})
    Check(declared.Integer(key) == actual.Integer(key),
          "path count QoR disagrees");
  store.RequireUnchanged();
  return {qor, maximum, count};
}
std::string ValidateSummary(const ArtifactRef &summary,
                            const ArtifactRef &paths,
                            const std::map<std::string, ArtifactRef> &owners,
                            std::vector<ArtifactRef> &refs,
                            const volatile std::sig_atomic_t *flag) {
  auto doc = Read(summary);
  Check(doc.Text("schema") == "emuflow.xilinx-routed-opensta-summary/v1" &&
            doc.Text("status") == "pass",
        "invalid routed OpenSTA summary");
  Doc source(doc.Token("source"));
  for (const auto &entry : std::map<std::string, std::string>{
           {"mapped_sha256", "mapped"},
           {"routed_timing_sha256", "timing"},
           {"timing_path_database_sha256", "paths"}}) {
    const auto hash = source.Text(entry.first);
    Check(hash.size() == 64 &&
              hash.find_first_not_of("0123456789abcdef") == std::string::npos,
          "invalid source digest");
    if (entry.second == "paths")
      Check(hash == Hash(paths, flag), "path database source seal disagrees");
    else if (owners.count(entry.second))
      Check(hash == Hash(owners.at(entry.second), flag),
            entry.second + " source seal disagrees");
  }
  auto facts = Qor(paths, refs, flag);
  Doc actual(facts.qor), reported(doc.Token("qor"));
  for (const auto *key : {"wns_ns", "tns_ns"}) {
    const auto a = reported.Number(key), b = actual.Number(key);
    Check(std::abs(a - b) <=
              std::max(1e-9, 1e-9 * std::max(std::abs(a), std::abs(b))),
          "summary QoR disagrees");
  }
  for (const auto *key : {"failing_endpoints", "timed_endpoints"})
    Check(reported.Integer(key) == actual.Integer(key),
          "summary endpoint count disagrees");
  auto fields = actual.Fields();
  fields["schema"] = JsonQuote("emuflow.xilinx-routed-opensta-validation/v1");
  fields["status"] = JsonQuote("pass");
  fields["summary_sha256"] = JsonQuote(Hash(summary, flag));
  fields["critical_path_ns"] = JsonRecord(facts.critical).Encode();
  return Doc::Encode(fields);
}
struct PayloadOwner {
  fs::path path;
  bool created = false, accepted = false;
  ~PayloadOwner() {
    if (created && !accepted) {
      std::error_code e;
      fs::remove(path, e);
    }
  }
};
struct Original {
  fs::path target, backup;
  std::optional<ArtifactRef> prior;
};
void PublishGroup(const std::vector<std::pair<fs::path, fs::path>> &files,
                  const fs::path &backup, const std::vector<ArtifactRef> &refs,
                  const std::function<void()> &accepted) {
  std::vector<Original> originals;
  for (unsigned i = 0; i < files.size(); ++i) {
    const auto &target = files[i].second;
    Original item{target, backup / ("prior-" + std::to_string(i)), {}};
    if (fs::exists(target)) {
      item.prior =
          ArtifactRef::Bind(target.string(), "physical-input-document-v1");
      fs::copy_file(target, item.backup);
      item.prior->RequireUnchanged();
    }
    originals.push_back(std::move(item));
  }
  std::size_t published = 0;
  try {
    for (unsigned i = 0; i < files.size(); ++i) {
      if (originals[i].prior)
        originals[i].prior->RequireUnchanged();
      PublishTemporaryFile(files[i].first.string(), files[i].second.string(),
                           refs);
      ++published;
    }
    for (const auto &r : refs)
      r.RequireUnchanged();
    accepted();
  } catch (...) {
    for (std::size_t n = published; n; --n) {
      auto &item = originals[n - 1];
      if (item.prior) {
        StagedArtifact restore(item.target.string());
        fs::copy_file(item.backup, restore.path(),
                      fs::copy_options::overwrite_existing);
        PublishTemporaryFile(restore.path(), item.target.string());
      } else {
        std::error_code e;
        fs::remove(item.target, e);
      }
    }
    throw;
  }
}
std::string Metadata(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT value FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), "missing graph metadata " + key);
  return row.TypedText(0);
}
void ExpectedHashes(Doc &config,
                    const std::map<std::string, std::string> &actual) {
  if (config.Token("source_sha256") == "null")
    return;
  Doc expected(config.Token("source_sha256"));
  for (const auto &field : expected.Fields()) {
    Check(std::set<std::string>{"mapped_sha256", "routed_timing_sha256",
                                "timing_path_database_sha256"}
              .count(field.first),
          "unknown cached source identity");
    const auto digest = expected.Text(field.first);
    Check(digest.size() == 64 &&
              digest.find_first_not_of("0123456789abcdef") == std::string::npos,
          "invalid cached source identity");
    if (actual.count(field.first))
      Check(digest == actual.at(field.first),
            "cached source identity disagrees");
  }
}
} // namespace
StageResult RunRoutedOpensta(const ArtifactRef &request,
                             const std::string &report,
                             const volatile std::sig_atomic_t *flag) {
  std::optional<Doc> document;
  std::map<std::string, ArtifactRef> owners;
  std::vector<ArtifactRef> refs, published;
  return RunStage(
      "physical.routed-opensta",
      [&] {
        Check(request.schema() == "routed-opensta-stage-request-v1",
              "invalid stage request schema");
        document.emplace(Doc::Read(request));
        document->Keys({"schema", "mode", "sources", "configuration"});
        Check(document->Text("schema") ==
                  "emuflow.routed-opensta-stage-request/v1",
              "stage request version disagrees");
        const auto mode = document->Text("mode");
        Check(mode == "run" || mode == "validate", "unsupported stage mode");
        refs.push_back(request);
        const std::map<std::string, std::string> schemas{
            {"mapped", "xilinx-mapped-index-v2"},
            {"timing", "xilinx-routed-timing-v2"},
            {"endpoints", "xilinx-routed-endpoints-v1"},
            {"base-model", "phase2-source-v1"},
            {"sta", "external-tool-executable-v1"},
            {"container", "external-tool-executable-v1"},
            {"export-script", "phase2-source-v1"},
            {"runtime-script", "phase2-source-v1"},
            {"paths", "sta-path-selector-v1"},
            {"summary", "physical-input-document-v1"}};
        for (const auto &value : Doc::Array(document->Token("sources"), 16)) {
          Doc row(value);
          row.Keys({"role", "path", "schema", "generation"});
          const auto role = row.Text("role");
          Check(schemas.count(role) && row.Text("schema") == schemas.at(role),
                "invalid source role/schema");
          auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
          const auto supplied = Doc::Array(row.Token("generation")),
                     expected = Doc::Array(Generation(ref));
          Check(supplied.size() == 7, "invalid source generation");
          for (unsigned i = 0; i < 7; ++i)
            Check(Doc::IntegerToken(supplied[i]) ==
                      Doc::IntegerToken(expected[i]),
                  "source generation changed");
          Check(owners.emplace(role, ref).second, "duplicate source role");
          refs.push_back(ref);
        }
        const std::set<std::string> required =
            mode == "run"
                ? std::set<std::string>{"mapped",        "timing",
                                        "base-model",    "sta",
                                        "export-script", "runtime-script"}
                : std::set<std::string>{"paths", "summary"};
        for (const auto &r : required)
          Check(owners.count(r), "missing source role " + r);
        for (const auto &r : owners)
          Check(required.count(r.first) ||
                    (mode == "run"
                         ? r.first == "container" || r.first == "endpoints"
                         : r.first == "mapped" || r.first == "timing"),
                "unexpected source role " + r.first);
        return refs;
      },
      [&](const auto &) {
        const auto report_path = Destination(report, refs);
        Doc config(document->Token("configuration"));
        config.Keys({"clocks", "max_paths", "output_path", "summary_path",
                     "log_path", "source_sha256", "timing_validation"});
        std::string validation;
        if (document->Text("mode") == "validate") {
          Check(fs::weakly_canonical(config.Text("output_path")) ==
                        owners.at("paths").path() &&
                    fs::weakly_canonical(config.Text("summary_path")) ==
                        owners.at("summary").path(),
                "validation configuration belongs to another owner");
          validation = ValidateSummary(owners.at("summary"), owners.at("paths"),
                                       owners, refs, flag);
          auto accepted_summary = Read(owners.at("summary"));
          Doc accepted_sources(accepted_summary.Token("source"));
          ExpectedHashes(
              config,
              {{"timing_path_database_sha256", Hash(owners.at("paths"), flag)},
               {"mapped_sha256", accepted_sources.Text("mapped_sha256")},
               {"routed_timing_sha256",
                accepted_sources.Text("routed_timing_sha256")}});
          Fields result{
              {"schema", JsonQuote("emuflow.routed-opensta-stage-report/v1")},
              {"status", JsonQuote("pass")},
              {"implementation_id",
               JsonQuote("native-routed-opensta-stage-v1")},
              {"mode", JsonQuote("validate")},
              {"validation", validation}};
          Write(report_path, Doc::Encode(result), refs);
          published = {ArtifactRef::Bind(report_path.string(),
                                         "routed-opensta-stage-report-v1"),
                       owners.at("paths"), refs.back(), owners.at("summary")};
          return;
        }
        const auto output = Destination(config.Text("output_path"), refs),
                   summary = Destination(config.Text("summary_path"), refs),
                   log = Destination(config.Text("log_path"), refs);
        Check(std::set<fs::path>{report_path, output, summary, log}.size() == 4,
              "stage destinations alias");
        const auto maximum = config.Integer("max_paths");
        Check(maximum > 0, "max_paths must be positive");
        Lease lease(output.string() + ".routed-opensta.lock");
        Workspace work(output.parent_path());
        const auto engine = Engine(owners, work.root, refs, flag);
        const auto mapped_hash = Hash(owners.at("mapped"), flag),
                   timing_hash = Hash(owners.at("timing"), flag);
        ExpectedHashes(config, {{"mapped_sha256", mapped_hash},
                                {"routed_timing_sha256", timing_hash}});
        if (config.Token("timing_validation") != "null") {
          Doc v(config.Token("timing_validation"));
          Check(v.Text("schema") ==
                        "emuflow.xilinx-routed-timing-validation/v1" &&
                    v.Text("status") == "pass" &&
                    v.Text("timing_sha256") == timing_hash,
                "routed timing validation seal disagrees");
        }
        std::optional<std::string> preloaded_manifest;
        if (!owners.count("endpoints")) {
          const auto &source = owners.at("timing");
          std::ifstream input(source.path(), std::ios::binary);
          Check(bool(input), "missing timing manifest");
          preloaded_manifest =
              std::string(std::istreambuf_iterator<char>(input), {});
          Check(!input.bad(), "cannot read timing manifest");
          source.RequireUnchanged();
          SqliteDatabase query(":memory:", SqliteDatabase::Mode::kReadOnly);
          auto descriptor =
              query.Prepare("SELECT json_extract(?,'$.payloads')");
          descriptor.BindText(1, *preloaded_manifest);
          Check(descriptor.Step() && !descriptor.IsNull(0),
                "missing timing payload descriptor");
          Doc payloads(descriptor.TypedText(0)),
              endpoint(payloads.Token("endpoints"));
          const auto filename = endpoint.Text("path");
          Check(!filename.empty() &&
                    fs::path(filename).filename() == filename &&
                    filename != "." && filename != ".." &&
                    endpoint.Text("format") == "jsonl-object/v1",
                "invalid endpoint payload path");
          owners.emplace(
              "endpoints",
              ArtifactRef::Bind(
                  (fs::path(source.path()).parent_path() / filename).string(),
                  "xilinx-routed-endpoints-v1"));
          refs.push_back(owners.at("endpoints"));
        }
        const auto endpoint_ref = owners.at("endpoints");
        for (const auto &path : {report_path, output, summary, log})
          (void)Destination(path.string(), refs);
        auto base = Read(owners.at("base-model"));
        Doc base_cells(base.Token("cells"));
        std::vector<std::string> base_types;
        for (const auto &c : base_cells.Fields())
          base_types.push_back(c.first);
        Doc clocks(config.Token("clocks"));
        Check(!clocks.Fields().empty() && clocks.Fields().size() <= 4096,
              "invalid clock catalog");
        const auto staging_config = Doc::Encode(
            {{"clocks", clocks.text()},
             {"clock_count", std::to_string(clocks.Fields().size())},
             {"source_path", JsonQuote(owners.at("mapped").path())},
             {"mapped_sha256", JsonQuote(mapped_hash)},
             {"timing_sha256", JsonQuote(timing_hash)},
             {"base_types", Array(base_types)}});
        auto graph_inputs = std::vector<ArtifactRef>{
            owners.at("mapped"), owners.at("timing"), endpoint_ref};
        graph_inputs.push_back(
            Request(work.root / "stage.bin", 0x314f5352464d5545ULL,
                    graph_inputs, staging_config, "routed-opensta-request-v1"));
        const auto graph_path = work.root / "routed.sqlite",
                   route_liberty = work.root / "route-delays.lib",
                   stage_report = work.root / "stage.json";
        Stop(flag);
        StageRoutedOpensta(graph_inputs, graph_path.string(),
                           route_liberty.string(), stage_report.string(),
                           preloaded_manifest ? &*preloaded_manifest : nullptr);
        preloaded_manifest.reset();
        auto staging = Read(ArtifactRef::Bind(stage_report.string(),
                                              "physical-input-document-v1"));
        const auto model = BuildRoutedOpenstaModel(
            owners.at("base-model"), staging.Token("logic_coefficients_ps"),
            staging.Token("pin_sets"));
        Doc model_doc(model.model_json), model_cells(model_doc.Token("cells"));
        for (const auto &v : Doc::Array(staging.Token("original_cell_types")))
          Check(model_cells.Has(DecodeJsonString(v)),
                "base timing model coverage is incomplete");
        const auto graph =
            ArtifactRef::Bind(graph_path.string(), "emuir-compact-v2");
        SqliteStore graph_store(graph);
        auto &db = graph_store.database();
        const auto design = Doc(Metadata(db, "design_json")).Text("name"),
                   top = staging.Text("top");
        const auto clocks_path = work.root / "clocks.tsv",
                   io_path = work.root / "timing-io.tsv",
                   liberty = work.root / "base.lib";
        {
          AtomicBinaryWriter writer(clocks_path.string(), refs);
          writer.stream() << "clock_hex\tperiod_ns\n";
          auto known = db.Prepare(
              "SELECT 1 FROM clocks WHERE json_extract(record_json,'$.id')=?");
          for (const auto &c : clocks.Fields()) {
            known.Reset();
            known.BindText(1, c.first);
            Check(known.Step() && clocks.Number(c.first) > 0,
                  "unknown or invalid physical clock");
            writer.stream() << Hex(c.first) << '\t' << std::setprecision(12)
                            << clocks.Number(c.first) << '\n';
          }
          writer.Commit();
        }
        Write(io_path, "direction\tclock_hex\tdelay_ns\tport_hex", refs);
        PublishTextArtifact(liberty.string(), refs,
                            [&](auto &out) { out << model.liberty; });
        CheckedRequestWriter export_request((work.root / "export.bin").string(),
                                            {graph});
        export_request.Put<std::uint64_t>(0x31455853464d5545ULL);
        export_request.Put<std::uint32_t>(1);
        export_request.Generation(graph.generation());
        export_request.Put<std::uint32_t>(0);
        export_request.Put<std::uint32_t>(3);
        export_request.Finish();
        const auto verilog = work.root / "mapped.v",
                   net_map = work.root / "net-map.tsv",
                   input_report = work.root / "input.json",
                   raw = work.root / "paths.tsv";
        ExportOpenstaInputs(
            {graph, ArtifactRef::Bind((work.root / "export.bin").string(),
                                      "opensta-input-export-request-v1")},
            input_report.string(), verilog.string(), net_map.string());
        std::vector<std::pair<std::string, std::string>> environment{
            {"EMUFLOW_STA_LIBERTY", liberty.string()},
            {"EMUFLOW_STA_EXTRA_LIBERTY",
             staging.Integer("unique_route_delay_cells")
                 ? route_liberty.string()
                 : ""},
            {"EMUFLOW_STA_VERILOG", verilog.string()},
            {"EMUFLOW_STA_TOP", top},
            {"EMUFLOW_STA_NET_MAP", net_map.string()},
            {"EMUFLOW_STA_CLOCKS", clocks_path.string()},
            {"EMUFLOW_STA_TIMING_IO", io_path.string()},
            {"EMUFLOW_STA_OUTPUT", raw.string()},
            {"EMUFLOW_STA_MAX_PATHS", std::to_string(maximum)},
            {"EMUFLOW_STA_THROUGH_NETS", ""},
            {"EMUFLOW_STA_THROUGH_COVERAGE", ""},
            {"EMUFLOW_STA_THROUGH_ENDPOINTS", ""}};
        if (owners.count("container"))
          environment.emplace_back("EMUFLOW_OPENSTA_CONTAINER_BINARY",
                                   owners.at("container").path());
        auto tool_refs = refs;
        for (const auto &path : {graph_path, route_liberty, liberty,
                                 clocks_path, io_path, verilog, net_map})
          tool_refs.push_back(
              ArtifactRef::Bind(path.string(), "physical-input-document-v1"));
        const auto execution = Execute({owners.at("sta").path(), "-exit",
                                        owners.at("export-script").path()},
                                       work.root, work.root / "opensta.log",
                                       tool_refs, flag, environment);
        const auto measured =
            ArtifactRef::Bind(raw.string(), "sta-source-document-v1");
        auto path_inputs = std::vector<ArtifactRef>{graph, measured};
        path_inputs.push_back(Request(work.root / "import.bin",
                                      0x31505453464d5545ULL, path_inputs, "{}",
                                      "sta-path-request-v1"));
        const auto payload = work.root / "paths.sqlite",
                   import_summary = work.root / "import.json";
        ImportStaPaths(path_inputs, payload.string(), import_summary.string());
        auto imported = Read(ArtifactRef::Bind(import_summary.string(),
                                               "physical-input-document-v1"));
        Doc stats(imported.Token("summary"));
        Check(imported.Text("status") == "pass" &&
                  imported.Text("design") == design,
              "imported path identity disagrees");
        Check(stats.Integer("paths") < maximum,
              "path limit was reached; global TNS is incomplete");
        const auto payload_hash = Hash(
            ArtifactRef::Bind(payload.string(), "timing-path-store-v1"), flag);
        const auto payload_path =
            output.parent_path() /
            (output.filename().string() + ".paths." + payload_hash + ".sqlite");
        PayloadOwner payload_owner{payload_path, !fs::exists(payload_path),
                                   false};
        Check(!fs::is_symlink(payload_path), "path owner is a symlink");
        if (fs::exists(payload_path))
          Check(Hash(ArtifactRef::Bind(payload_path.string(),
                                       "timing-path-store-v1"),
                     flag) == payload_hash,
                "content-addressed path owner disagrees");
        else {
          StagedArtifact pending(payload_path.string());
          fs::rename(payload, pending.path());
          PublishTemporaryFile(pending.path(), payload_path.string(), refs);
        }
        const auto final_payload =
            ArtifactRef::Bind(payload_path.string(), "timing-path-store-v1");
        refs.push_back(final_payload);
        const auto audit_config = Doc::Encode(
            {{"normalization", imported.Token("normalization")},
             {"summary", stats.text()},
             {"design", JsonQuote(design)},
             {"provider", JsonQuote("opensta-fpga-path-database-v1")}});
        auto audit_inputs = std::vector<ArtifactRef>{graph, final_payload};
        audit_inputs.push_back(Request(work.root / "audit.bin",
                                       0x31505453464d5545ULL, audit_inputs,
                                       audit_config, "sta-path-request-v1"));
        const auto audit_report = work.root / "audit.json";
        ValidateStaPaths(audit_inputs, audit_report.string());
        const auto origin = Doc::Encode(
            {{"provider", JsonQuote("opensta-fpga-path-database-v1")},
             {"input", JsonQuote("<opensta-path-tsv>")},
             {"input_sha256", JsonQuote(Hash(measured, flag))},
             {"timing_model", model_doc.Token("name")},
             {"timing_model_qualification",
              JsonQuote("analytical_uncharacterized")},
             {"engine", engine},
             {"architecture_timing_db", "null"},
             {"timing_io", Doc::Encode({{"sha256", "null"},
                                        {"input_ports", "0"},
                                        {"output_ports", "0"}})}});
        const auto selector_text = Doc::Encode(
            {{"schema", JsonQuote("emuflow.sta-path-database/v3")},
             {"design", JsonQuote(design)},
             {"source", origin},
             {"normalization", imported.Token("normalization")},
             {"summary", stats.text()},
             {"payloads",
              Doc::Encode(
                  {{"paths",
                    Doc::Encode(
                        {{"format", JsonQuote("sqlite-sta-path/v1")},
                         {"path", JsonQuote(payload_path.filename().string())},
                         {"sha256", JsonQuote(payload_hash)},
                         {"records", stats.Token("paths")}})}})}});
        StagedArtifact staged_paths(output.string()),
            staged_summary(summary.string()), staged_log(log.string());
        Write(staged_paths.path(), selector_text, refs);
        const auto pending_paths =
            ArtifactRef::Bind(staged_paths.path(), "sta-path-selector-v1");
        auto staging_fields =
            Fields{{"logical_route_endpoints",
                    staging.Token("inserted_route_delay_cells")},
                   {"inserted_route_delay_cells",
                    staging.Token("inserted_route_delay_cells")},
                   {"unique_route_delay_cells",
                    staging.Token("unique_route_delay_cells")},
                   {"hard_block_types_unqualified", Array(model.hard_blocks)},
                   {"falling_edge_ff_instances",
                    staging.Token("falling_edge_ff_instances")},
                   {"representation", JsonQuote("compact-emuir-sqlite")},
                   {"implementation_id", staging.Token("implementation_id")}};
        auto native_model_coverage = Doc::Encode(
            {{"schema", JsonQuote("emuflow.native-routed-model-coverage/v1")},
             {"count",
              std::to_string(
                  Doc::Array(staging.Token("original_cell_types")).size() +
                  staging.Integer("unique_route_delay_cells"))},
             {"base_cell_types", staging.Token("original_cell_types")},
             {"generated_cell_types",
              Doc::Encode(
                  {{"schema", JsonQuote("emuflow.routed-opensta-types/v1")},
                   {"records", staging.Token("unique_route_delay_cells")}})}});
        const auto opensta = Doc::Encode(
            {{"status", JsonQuote("pass")},
             {"design", JsonQuote(design)},
             {"provider", JsonQuote("opensta-fpga-path-database-v1")},
             {"timing_model", model_doc.Token("name")},
             {"timing_model_qualification",
              JsonQuote("analytical_uncharacterized")},
             {"engine", engine},
             {"clocks", clocks.text()},
             {"timing_io", Doc::Encode({{"sha256", "null"},
                                        {"input_ports", "0"},
                                        {"output_ports", "0"}})},
             {"paths", stats.Token("paths")},
             {"max_paths", std::to_string(maximum)},
             {"through_nets", "[]"},
             {"covered_through_nets", "[]"},
             {"through_net_coverage", "null"},
             {"path_limit_reached", "false"},
             {"unique_path_nets", stats.Token("unique_path_nets")},
             {"used_cell_type_summary", native_model_coverage},
             {"checker",
              Doc::Read(ArtifactRef::Bind(audit_report.string(),
                                          "physical-input-document-v1"))},
             {"output", JsonQuote(output.string())},
             {"log", JsonQuote(log.string())},
             {"input_export",
              Doc::Read(ArtifactRef::Bind(input_report.string(),
                                          "physical-input-document-v1"))},
             {"path_qor", stats.Token("qor")},
             {"path_import",
              Doc::Encode({{"implementation_id",
                            imported.Token("implementation_id")}})},
             {"engine_metrics",
              Doc(ToolResultJson(execution)).Token("engine_metrics")}});
        const auto summary_text = Doc::Encode(
            {{"schema", JsonQuote("emuflow.xilinx-routed-opensta-summary/v1")},
             {"status", JsonQuote("pass")},
             {"authority", JsonQuote("opensta")},
             {"qualification", model_doc.Token("source")},
             {"source",
              Doc::Encode({{"mapped_sha256", JsonQuote(mapped_hash)},
                           {"routed_timing_sha256", JsonQuote(timing_hash)},
                           {"timing_path_database_sha256",
                            JsonQuote(Hash(pending_paths, flag))}})},
             {"clocks", clocks.text()},
             {"staging", Doc::Encode(staging_fields)},
             {"qor", stats.Token("qor")},
             {"opensta", opensta}});
        Write(staged_summary.path(), summary_text, refs);
        const auto pending_summary = ArtifactRef::Bind(
            staged_summary.path(), "physical-input-document-v1");
        validation =
            ValidateSummary(pending_summary, pending_paths, owners, refs, flag);
        ExpectedHashes(config, {{"timing_path_database_sha256",
                                 Hash(pending_paths, flag)}});
        fs::copy_file(work.root / "opensta.log", staged_log.path(),
                      fs::copy_options::overwrite_existing);
        const auto result = Doc::Encode(
            {{"schema", JsonQuote("emuflow.routed-opensta-stage-report/v1")},
             {"status", JsonQuote("pass")},
             {"implementation_id", JsonQuote("native-routed-opensta-stage-v1")},
             {"mode", JsonQuote("run")},
             {"validation", validation},
             {"paths", JsonQuote(output.string())},
             {"summary", JsonQuote(summary.string())}});
        StagedArtifact staged_report(report_path.string());
        Write(staged_report.path(), result, refs);
        Stop(flag);
        PublishGroup(
            {{staged_paths.path(), output},
             {staged_summary.path(), summary},
             {staged_log.path(), log},
             {staged_report.path(), report_path}},
            work.root, refs, [&] {
              published = {
                  ArtifactRef::Bind(report_path.string(),
                                    "routed-opensta-stage-report-v1"),
                  ArtifactRef::Bind(output.string(), "sta-path-selector-v1"),
                  final_payload,
                  ArtifactRef::Bind(summary.string(),
                                    "physical-input-document-v1"),
                  ArtifactRef::Bind(log.string(),
                                    "physical-input-document-v1")};
            });
        payload_owner.accepted = true;
        work.accepted = true;
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
