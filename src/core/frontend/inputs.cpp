#include "frontend/inputs.hpp"
#include "common/bounded_json.hpp"
#include "frontend/report.hpp"
#include "frontend/yosys_hierarchy.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <fstream>
#include <memory>
#include <set>
#include <sys/file.h>
#include <unistd.h>
#include <zlib.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using D = BoundedJson;
using O = std::map<std::string, std::string>;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "frontend input: " + why);
}
std::string Text(const std::string &value) { return JsonQuoteUtf8(value); }
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
void Same(const std::string &left, const std::string &right,
          const std::string &why) {
  SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
  RequireEquivalentJson(db, left, right, "frontend input: " + why);
}
std::string Source(const std::string &role, const ArtifactRef &ref) {
  return D::Encode({{"role", Text(role)},
                    {"path", Text(ref.path())},
                    {"schema", Text(ref.schema())},
                    {"generation", Generation(ref)}});
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto q = db.Prepare("SELECT CASE WHEN length(value)<=1048576 THEN value END "
                      "FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step() && !q.IsNull(0), "missing/oversized compact metadata " + key);
  return q.TypedText(0);
}
void Json(const fs::path &path, const std::string &value,
          const std::vector<ArtifactRef> &sources) {
  PublishTextArtifact(path.string(), sources,
                      [&](std::ostream &out) { out << value << '\n'; });
}
struct Workspace {
  fs::path path;
  Workspace(const fs::path &parent, const std::string &prefix) {
    fs::create_directories(parent);
    auto name = (parent / (prefix + "XXXXXX")).string();
    Check(mkdtemp(name.data()), "cannot create import generation");
    path = name;
  }
  ~Workspace() {
    std::error_code e;
    fs::remove_all(path, e);
  }
};
struct Lease {
  int fd = -1;
  explicit Lease(const fs::path &root) {
    Check(root.is_absolute() && !fs::is_symlink(root),
          "output must be an absolute directory");
    fs::create_directories(root);
    fd = open((root / ".frontend-input-owner.lock").c_str(),
              O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open input owner lock");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "frontend inputs already have a writer");
    }
  }
  ~Lease() {
    if (fd >= 0)
      close(fd);
  }
};
class GzipBuffer : public std::streambuf {
public:
  explicit GzipBuffer(const fs::path &path) {
    file_ = gzopen(path.c_str(), "rb");
    Check(file_, "cannot read compressed source");
    setg(buffer_, buffer_, buffer_);
  }
  ~GzipBuffer() {
    if (file_)
      gzclose(file_);
  }

protected:
  int_type underflow() override {
    const int count = gzread(file_, buffer_, sizeof(buffer_));
    if (count < 0)
      throw StageError(ErrorCode::kInvalidArtifact,
                       "compressed Yosys source is corrupt");
    if (!count)
      return traits_type::eof();
    setg(buffer_, buffer_, buffer_ + count);
    return traits_type::to_int_type(*gptr());
  }

private:
  gzFile file_ = nullptr;
  char buffer_[65536];
};
struct Input {
  ArtifactRef request;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> owners;
  std::string mode, platform, periods, top, profile, design, phase1_report,
      timing_report, frontend_summary, timing_summary;
  std::vector<std::string> clocks;
  DirectoryReferences timing_links;
  const volatile std::sig_atomic_t *cancelled;
  Input(const ArtifactRef &r, const volatile std::sig_atomic_t *flag)
      : request(r), cancelled(flag) {}
  void Stop() const {
    if (cancelled && *cancelled)
      throw StageError(ErrorCode::kExecutionFailed, "frontend input cancelled");
  }
  void Read() {
    Check(request.schema() == "frontend-input-request-v1",
          "request schema disagrees");
    D config(D::Read(request));
    config.Keys({"schema", "mode", "sources", "top", "clocks", "clock_periods",
                 "mapping_profile", "platform"});
    Check(config.Text("schema") == "emuflow.frontend-input-request/v1",
          "request version disagrees");
    mode = config.Text("mode");
    Check(mode == "prepared" || mode == "yosys-json", "unknown input mode");
    top = config.Text("top");
    profile = config.Text("mapping_profile");
    Check(profile == "xilinx-ultrascaleplus-open-v1",
          "unsupported mapping profile");
    platform = config.Token("platform");
    periods = config.Token("clock_periods");
    D expected_periods(periods);
    for (const auto &clock : D::Array(config.Token("clocks")))
      clocks.push_back(DecodeJsonString(clock));
    Check(std::is_sorted(clocks.begin(), clocks.end()) &&
              std::adjacent_find(clocks.begin(), clocks.end()) == clocks.end(),
          "clock catalog is not unique/canonical");
    const std::map<std::string, std::string> types{
        {"board", "boarddb-v1"},
        {"json", "yosys-source-document-v1"},
        {"library", "xilinx-primitive-library-v1"},
        {"emuir", "emuir-compact-v2"},
        {"phase1-report", "frontend-input-report-v1"},
        {"phase1-board", "boarddb-v1"},
        {"path-selector", "sta-path-selector-v1"},
        {"weight-selector", "net-weight-selector-v2"},
        {"path-store", "timing-path-store-v1"},
        {"weight-store", "net-weight-store-v1"},
        {"timing-report", "frontend-input-report-v1"}};
    sources.push_back(request);
    for (const auto &token : D::Array(config.Token("sources"), 16)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      const auto role = row.Text("role");
      Check(types.count(role) && row.Text("schema") == types.at(role),
            "unknown input role/schema");
      auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
      Same(Generation(ref), row.Token("generation"),
           "original source generation changed");
      Check(owners.emplace(role, ref).second, "duplicate input role");
      sources.push_back(ref);
    }
    const std::set<std::string> required =
        mode == "prepared"
            ? std::set<std::string>{"board",         "emuir",
                                    "phase1-report", "phase1-board",
                                    "path-selector", "weight-selector",
                                    "path-store",    "weight-store"}
            : std::set<std::string>{"board", "json", "library"};
    for (const auto &role : required)
      Check(owners.count(role), "missing input role " + role);
    Check(owners.size() == required.size() ||
              (mode == "prepared" && owners.size() == required.size() + 1 &&
               owners.count("timing-report")),
          "input role coverage disagrees");
    ValidateBoard();
  }
  void ValidateBoard() {
    D raw(D::Read(owners.at("board"))), normalized(platform);
    Check(raw.Text("schema") == "emuflow.boarddb/v1" &&
              normalized.Text("schema") == "emuflow.boarddb/v1",
          "BoardDB version disagrees");
    D raw_name(raw.Token("platform")), name(normalized.Token("platform"));
    Check(raw_name.Text("name") == name.Text("name"),
          "normalized BoardDB namespace disagrees");
    std::map<std::string, std::string> raw_parts;
    for (const auto &token : D::Array(raw.Token("fpgas"), 256)) {
      D fpga(token);
      Check(raw_parts.emplace(fpga.Text("id"), token).second, "duplicate FPGA");
    }
    Check(!raw_parts.empty(), "empty BoardDB");
    std::set<std::string> seen;
    for (const auto &token : D::Array(normalized.Token("fpgas"), 256)) {
      D fpga(token);
      const auto id = fpga.Text("id");
      Check(raw_parts.count(id) && seen.insert(id).second,
            "normalized FPGA coverage disagrees");
      D original(raw_parts.at(id));
      Same(original.Token("part"), fpga.Token("part"),
           "normalized FPGA part disagrees");
      Same(original.Token("capacity"), fpga.Token("capacity"),
           "normalized raw capacity disagrees");
      Same(original.Token("utilization_limit"), fpga.Token("utilization_limit"),
           "normalized utilization limit disagrees");
      const auto limit = fpga.Number("utilization_limit");
      Check(limit > 0 && limit <= 1, "invalid utilization limit");
      O expected;
      D capacities(fpga.Token("capacity"));
      for (const auto &entry : capacities.Fields()) {
        const auto count = D::IntegerToken(entry.second);
        Check(count >= 0 &&
                  std::find(kResourceFields.begin(), kResourceFields.end(),
                            entry.first) != kResourceFields.end(),
              "invalid resource capacity");
        expected[entry.first] =
            std::to_string(std::int64_t(std::floor(double(count) * limit)));
      }
      Same(D::Encode(expected), fpga.Token("effective_capacity"),
           "normalized effective capacity disagrees");
    }
    Check(seen.size() == raw_parts.size(), "normalized BoardDB omits FPGA");
  }
  std::string Payload(const char *selector_role, const char *store_role,
                      const char *kind, const char *format) {
    const auto &selector = owners.at(selector_role),
               &store = owners.at(store_role);
    D manifest(D::Read(selector));
    D payloads(manifest.Token("payloads"));
    D item(payloads.Token(kind));
    const fs::path member(item.Text("path"));
    Check(member == member.filename() && member != "." && member != ".." &&
              !member.empty(),
          "prepared payload must be a sibling of its manifest");
    Check(fs::canonical(fs::path(selector.path()).parent_path() / member) ==
              fs::path(store.path()),
          "prepared payload does not select its bound owner");
    Check(item.Text("format") == format && item.Integer("records") > 0,
          "prepared payload descriptor is invalid");
    Check(item.Text("sha256") == Sha256File(store, cancelled),
          "prepared payload content seal disagrees");
    return member.string();
  }
  void Prepared() {
    Stop();
    SqliteStore ir(owners.at("emuir"));
    D metadata(Meta(ir.database(), "design_json"));
    design = metadata.Text("name");
    std::set<std::string> declared;
    auto clock =
        ir.database().Prepare("SELECT record_json FROM clocks ORDER BY seq");
    while (clock.Step()) {
      Check(declared.size() < 16384, "clock catalog exceeds bound");
      D row(clock.TypedText(0));
      Check(declared.insert(row.Text("id")).second, "duplicate prepared clock");
    }
    D periods_doc(periods);
    std::set<std::string> selected;
    for (const auto &row : periods_doc.Fields()) {
      selected.insert(row.first);
      const auto value =
          ParseFiniteDecimal(row.second, "prepared clock period");
      Check(row.second != "true" && row.second != "false" && value > 0,
            "invalid prepared clock period");
    }
    Check(declared == selected,
          "prepared continuation must preserve every DUT clock");
    phase1_report = D::Read(owners.at("phase1-report"));
    D front(phase1_report);
    Check(front.Text("schema") == "emuflow.phase1-report/v1" &&
              front.Text("status") == "pass" && front.Text("design") == design,
          "prepared Phase 1 publication identity/status disagrees");
    D transform(front.Token("indexed_staging_transform"));
    D audit(transform.Token("primitive_audit"));
    Check(audit.Text("status") == "pass" &&
              audit.Text("mapping_profile") == profile,
          "prepared Phase 1 mapping profile/publication did not pass");
    D old_board(D::Read(owners.at("phase1-board"))), new_board(platform);
    Check(old_board.Text("schema") == "emuflow.boarddb/v1",
          "prepared board snapshot version disagrees");
    const auto parts = [](D &board) {
      std::set<std::string> parts;
      for (const auto &token : D::Array(board.Token("fpgas"), 256)) {
        D fpga(token);
        Check(fpga.Token("part") != "null",
              "prepared device identity is invalid");
        const auto part = fpga.Text("part");
        Check(!part.empty(), "prepared device identity is invalid");
        parts.insert(part);
      }
      Check(!parts.empty(), "prepared device inventory is empty");
      return parts;
    };
    Check(parts(old_board) == parts(new_board),
          "prepared Phase 1 device part differs from selected platform");
    D paths(D::Read(owners.at("path-selector"))),
        weights(D::Read(owners.at("weight-selector")));
    Check(paths.Text("schema") == "emuflow.sta-path-database/v3" &&
              paths.Text("design") == design &&
              weights.Text("schema") == "emuflow.partition-net-weights/v2" &&
              weights.Text("design") == design,
          "prepared indexed timing/weight identity mismatch");
    const auto path_member =
        Payload("path-selector", "path-store", "paths", "sqlite-sta-path/v1");
    const auto weight_member = Payload("weight-selector", "weight-store",
                                       "weights", "sqlite-net-weights/v1");
    Check(path_member != weight_member,
          "prepared timing owners alias one filename");
    timing_links = {{path_member, owners.at("path-store")},
                    {weight_member, owners.at("weight-store")}};
    SqliteStore path_store(owners.at("path-store")),
        weight_store(owners.at("weight-store"));
    O observed;
    std::int64_t path_count = 0;
    auto rows = path_store.database().Prepare(
        "SELECT clock_domain,clock_period_ns,COUNT(*) FROM paths GROUP BY "
        "clock_domain,clock_period_ns");
    while (rows.Step()) {
      Check(observed.size() < 16384, "timing clock catalog exceeds bound");
      const auto period = rows.TypedReal(1);
      Check(std::isfinite(period) && period > 0,
            "invalid published clock period");
      Check(observed.emplace(rows.TypedText(0), JsonRecord(period).Encode())
                .second,
            "duplicate timing clock period");
      const auto count = rows.TypedInteger(2);
      Check(count >= 0 && path_count <= INT64_MAX - count,
            "prepared timing census overflows");
      path_count += count;
    }
    // An admitted DUT clock may have no timing paths. Every measured domain
    // must still use its declared period; all DUT clocks were checked above.
    for (const auto &row : observed) {
      Check(periods_doc.Has(row.first),
            "prepared Phase 2 clock constraints mismatch");
      Same(row.second, periods_doc.Token(row.first),
           "prepared Phase 2 clock constraints mismatch");
    }
    D path_items(paths.Token("payloads"));
    D path_descriptor(path_items.Token("paths"));
    D path_summary(paths.Token("summary"));
    Check(path_count == path_descriptor.Integer("records") &&
              path_count == path_summary.Integer("paths"),
          "prepared timing path publication count mismatch");
    auto weights_count =
        weight_store.database().Prepare("SELECT COUNT(*) FROM net_weights");
    Check(weights_count.Step(), "missing weight census");
    const auto count = weights_count.TypedInteger(0);
    D weight_items(weights.Token("payloads"));
    D weight_descriptor(weight_items.Token("weights"));
    D weight_summary(weights.Token("summary"));
    Check(count == weight_descriptor.Integer("records") &&
              count == weight_summary.Integer("weighted_nets"),
          "prepared net-weight publication count mismatch");
    O derived{
        {"status", Text("pass")},
        {"clocks", periods},
        {"weights", D::Encode({{"weighted_nets", std::to_string(count)}})},
        {"sta",
         D::Encode(
             {{"engine", paths.Has("source")
                             ? D(paths.Token("source")).Fields().count("engine")
                                   ? D(paths.Token("source")).Token("engine")
                                   : "null"
                             : "null"},
              {"paths", std::to_string(path_count)}})},
        {"publication_validation",
         Text("native-typed-version-clock-count-and-content-seals-v1")}};
    timing_report = D::Encode(derived);
    if (owners.count("timing-report")) {
      timing_report = D::Read(owners.at("timing-report"));
      D original(timing_report);
      Check(original.Text("status") == "pass",
            "prepared Phase 2 publication did not pass");
      Same(original.Token("clocks"), periods,
           "prepared Phase 2 clock constraints mismatch");
    }
    D raw_name(new_board.Token("platform"));
    auto fields = front.Fields();
    fields["platform"] = Text(raw_name.Text("name"));
    fields["continuation"] =
        D::Encode({{"mode", Text("read-only-indexed-boundary")},
                   {"source", Text(owners.at("emuir").path())}});
    phase1_report = D::Encode(fields);
    frontend_summary = D::Encode(
        {{"schema", Text("emuflow.phase1-report/v1")},
         {"phase", "1"},
         {"status", Text("pass")},
         {"design", Text(design)},
         {"platform", Text(raw_name.Text("name"))},
         {"synthesis", D::Encode({{"provider", Text("prepared-indexed-input")},
                                  {"mode", Text("validated-existing-phase1")},
                                  {"mapping_profile", Text(profile)}})}});
    D original(timing_report);
    auto prior = original.Fields();
    const auto summary =
        D::Encode({{"status", Text("pass")},
                   {"mode", Text("validated-existing-phase2")},
                   {"backend", Text("opensta")},
                   {"sta", prior.count("sta") ? prior.at("sta") : "{}"},
                   {"optimization_enabled", "true"},
                   {"partition_weights",
                    prior.count("weights") ? prior.at("weights") : "{}"}});
    std::vector<std::string> bound;
    for (const auto &owner : owners)
      bound.push_back(Source(owner.first, owner.second));
    timing_summary =
        D::Encode({{"schema", Text("emuflow.prepared-timing-summary/v1")},
                   {"status", Text("pass")},
                   {"implementation_id", Text("native-frontend-input-v1")},
                   {"summary", summary},
                   {"validation", D::Encode(derived)},
                   {"sources", Array(bound)}});
    ir.RequireUnchanged();
    path_store.RequireUnchanged();
    weight_store.RequireUnchanged();
    Stop();
  }
};
void RequirePass(const StageResult &result) {
  if (!result.ok())
    throw StageError(result.error, result.message);
}
} // namespace
StageResult RunFrontendInputs(const ArtifactRef &request,
                              const std::string &output,
                              const volatile std::sig_atomic_t *cancelled) {
  Input input(request, cancelled);
  std::optional<Lease> lease;
  const fs::path root = output, front = root / "frontend/phase1",
                 timing = root / "timing";
  return RunStage(
      "phase1.import-inputs",
      [&] {
        input.Read();
        return input.sources;
      },
      [&](const auto &) {
        lease.emplace(root);
        input.Stop();
        Check(!fs::exists(front) && !fs::is_symlink(front),
              "frontend output is not fresh");
        if (input.mode == "prepared")
          Check(!fs::exists(timing) && !fs::is_symlink(timing),
                "timing output is not fresh");
        Workspace work(root, ".frontend-input-work-"),
            staged_front(root / "frontend", ".frontend-input-generation-");
        std::unique_ptr<Workspace> staged_timing;
        DirectoryReferences references;
        if (input.mode == "prepared") {
          input.Prepared();
          staged_timing =
              std::make_unique<Workspace>(root, ".prepared-timing-generation-");
          fs::create_symlink(input.owners.at("emuir").path(),
                             staged_front.path / "design.emuir.sqlite");
          references.emplace_back("design.emuir.sqlite",
                                  input.owners.at("emuir"));
          Json(staged_front.path / "phase1_report.json", input.phase1_report,
               input.sources);
          Json(staged_front.path / "frontend-summary.json",
               input.frontend_summary, input.sources);
          Json(staged_front.path / "platform.normalized.json", input.platform,
               input.sources);
          for (const auto &reference : input.timing_links)
            fs::create_symlink(reference.second.path(),
                               staged_timing->path / reference.first);
          for (const auto &pair :
               std::vector<std::pair<std::string, std::string>>{
                   {"path-selector", "path-database.json"},
                   {"weight-selector", "partition-net-weights.json"}})
            Json(staged_timing->path / pair.second,
                 D::Read(input.owners.at(pair.first)), input.sources);
          Json(staged_timing->path / "experiment-timing-report.json",
               input.timing_report, input.sources);
          Json(staged_timing->path / "prepared-timing-summary.json",
               input.timing_summary, input.sources);
        } else {
          const auto &source = input.owners.at("json"),
                     &library = input.owners.at("library");
          const auto path = work.path / "import.bin";
          CheckedRequestWriter writer(path.string(), input.sources);
          writer.Put<std::uint64_t>(0x31524959464d5545ULL);
          writer.Put<std::uint32_t>(1);
          writer.Put<std::uint32_t>(1);
          writer.Text(input.top);
          writer.Text(source.path());
          writer.Put<std::uint32_t>(input.clocks.size());
          for (const auto &clock : input.clocks)
            writer.Text(clock);
          writer.Generation(library.generation());
          writer.Finish();
          const auto control =
              ArtifactRef::Bind(path.string(), "yosys-emuir-import-request-v1");
          const auto imported = work.path / "import.json";
          StageResult result;
          if (fs::path(source.path()).extension() == ".gz") {
            GzipBuffer buffer(source.path());
            std::istream stream(&buffer);
            stream.exceptions(std::ios::badbit);
            result = ImportYosysEmuir(
                control, stream,
                (staged_front.path / "design.emuir.sqlite").string(),
                imported.string(), &library);
          } else {
            std::ifstream stream(source.path(), std::ios::binary);
            Check(bool(stream), "cannot read Yosys source");
            result = ImportYosysEmuir(
                control, stream,
                (staged_front.path / "design.emuir.sqlite").string(),
                imported.string(), &library);
          }
          RequirePass(result);
          for (const auto &ref : result.outputs)
            ref.RequireUnchanged();
          input.Stop();
          FrontendReportConfiguration config;
          config.platform = input.platform;
          config.sources = input.sources;
          config.input_sha256 = Sha256File(source, cancelled);
          config.synthesis =
              D::Encode({{"provider", Text("yosys")},
                         {"mode", Text("provided-yosys-json")},
                         {"mapping_profile", Text("provided-yosys-json")},
                         {"sources", "[]"},
                         {"include_dirs", "[]"},
                         {"defines", "[]"},
                         {"yosys_json_sha256", Text(config.input_sha256)},
                         {"interchange_retention",
                          Text("source-owned-stream; no graph staging copy")}});
          BuildFrontendReport(
              staged_front.path, config,
              D::Read(ArtifactRef::Bind(imported.string(),
                                        "frontend-input-report-v1")));
        }
        std::vector<NewDirectoryPublication> directories{
            {staged_front.path, front, references}};
        if (staged_timing)
          directories.push_back(
              {staged_timing->path, timing, input.timing_links});
        PublishNewDirectoryGroup(directories, input.sources,
                                 [&] { input.Stop(); });
      },
      [&] {
        std::vector<ArtifactRef> refs{
            ArtifactRef::Bind((front / "frontend-summary.json").string(),
                              "phase1-summary-v1"),
            ArtifactRef::Bind((front / "design.emuir.sqlite").string(),
                              "emuir-compact-v2")};
        if (input.mode == "prepared")
          refs.push_back(ArtifactRef::Bind(
              (timing / "prepared-timing-summary.json").string(),
              "prepared-timing-summary-v1"));
        return refs;
      });
}
} // namespace emuflow::core
