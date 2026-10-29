#include "physical/flow/context.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "physical/measurement_stage.hpp"
#include "physical/openparf/runtime.hpp"
#include "scheduling/phase5.hpp"
#include "timing/local_path_query.hpp"
#include "timing/logic_segment_query.hpp"
#include "transport/runtime_calendar.hpp"
#include "transport/split_overlay.hpp"
#include "transport/transport.hpp"
#include <algorithm>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
namespace emuflow::core::physical_flow {
using namespace openparf_input;
void Check(bool yes, const std::string &why) {
  if (!yes)
    throw StageError(ErrorCode::kInvalidArtifact, "physical flow: " + why);
}
R Generation(const ArtifactRef &r) {
  const auto &g = r.generation();
  return A{g.device,
           g.inode,
           g.bytes,
           g.modified_seconds,
           g.modified_nanoseconds,
           g.changed_seconds,
           g.changed_nanoseconds};
}
R Source(const std::string &role, const ArtifactRef &r) {
  return O{{"role", role},
           {"path", r.path()},
           {"schema", r.schema()},
           {"generation", Generation(r)}};
}
R Metadata(const ArtifactRef &r, const std::string &key) {
  SqliteStore store(r);
  const auto result = R::Parse(Meta(store.database(), key));
  store.RequireUnchanged();
  return result;
}
R Document(const fs::path &p) {
  return Read(ArtifactRef::Bind(p.string(), "physical-input-document-v1"));
}
void Keep(const StageResult &result) {
  if (!result.ok())
    throw StageError(result.error, result.stage + ": " + result.message);
  for (const auto &r : result.outputs)
    r.RequireUnchanged();
}
ArtifactRef Request(const fs::path &p, const R &value,
                    const std::string &schema,
                    const std::vector<ArtifactRef> &refs) {
  Json(p, value, refs);
  return ArtifactRef::Bind(p.string(), schema);
}
ArtifactRef Resolve(const ArtifactRef &selector, const char *field,
                    const std::string &schema) {
  const auto document = Read(selector);
  const fs::path member(document.at(field).text());
  Check(
      !member.empty() &&
          (member == member.filename() || (schema == "transport-store-v2" &&
                                           member == "../transport.sqlite")) &&
          member != "." && member != "..",
      "selector payload escapes its owner");
  auto ref = ArtifactRef::Bind(
      (fs::path(selector.path()).parent_path() / member).string(), schema);
  Check(document.at("store_bytes").integer() >= 0 &&
            std::uint64_t(document.at("store_bytes").integer()) ==
                ref.generation().bytes &&
            document.at("store_sha256").text() == FileHash(ref),
        "selector file seal disagrees");
  selector.RequireUnchanged();
  return ref;
}
Lease::Lease(const std::string &output, const std::vector<ArtifactRef> &refs,
             bool resume) {
  const fs::path p(output);
  Check(p.is_absolute() && !fs::is_symlink(p),
        "output must be an absolute directory");
  Check(!fs::exists(p) || fs::is_directory(p), "output is not a directory");
  Check(resume || !fs::exists(p) || fs::is_empty(p), "output is not empty");
  fs::create_directories(p);
  root = fs::canonical(p);
  for (const auto &ref : refs) {
    const auto relative = fs::path(ref.path()).lexically_relative(root);
    Check(relative.empty() || *relative.begin() == "..",
          "source lies in physical output");
  }
  fd = open((root / ".physical-owner.lock").c_str(),
            O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
  Check(fd >= 0, "cannot open physical flow lease");
  if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
    close(fd);
    fd = -1;
    Check(false, "physical flow already has an owner");
  }
  auto name = (root / "implementation.generation.XXXXXX").string();
  if (!mkdtemp(name.data())) {
    close(fd);
    fd = -1;
    Check(false, "cannot create physical generation");
  }
  generation = name;
}
Lease::~Lease() {
  if (fd >= 0)
    close(fd);
}
void Context::Stop() const {
  if (cancelled && *cancelled)
    throw StageError(ErrorCode::kExecutionFailed, "physical flow cancelled");
}
ArtifactRef Context::Relative(const std::string &path,
                              const std::string &schema) const {
  const fs::path p(path);
  Check(!p.empty() && !p.is_absolute(), "split member must be relative");
  for (const auto &component : p)
    Check(component != "..", "split member escapes its owner");
  return ArtifactRef::Bind(
      (fs::path(owners.at("split-manifest").path()).parent_path() / p).string(),
      schema);
}
void Context::Admit(const std::string &output) {
  Check(request.schema() == "physical-flow-request-v1",
        "request role disagrees");
  config = Read(request);
  BoundedJson(config.encode())
      .Keys({"schema", "sources", "backend", "backend_options", "workers",
             "seed", "resume", "selected_fpgas"});
  Check(config.at("schema").text() == "emuflow.physical-flow-request/v1",
        "request version disagrees");
  backend = config.at("backend").text();
  Check(backend == "rapidwright" || backend == "vivado",
        "unsupported physical backend");
  workers = config.at("workers").integer();
  seed = config.at("seed").integer();
  Check(workers > 0 && workers <= 256 && seed >= 0 && seed <= UINT32_MAX,
        "invalid workers or invalid physical seed");
  Check(std::holds_alternative<bool>(config.at("resume").value),
        "invalid resume flag");
  resume = std::get<bool>(config.at("resume").value);
  partial = !config.at("selected_fpgas").null();
  const std::map<std::string, std::string> schemas{
      {"split-manifest", "physical-input-document-v1"},
      {"compile-policy", "physical-flow-policy-v1"},
      {"board", "boarddb-v1"},
      {"schedule-selector", "tdm-schedule-selector-v4"},
      {"original", "emuir-compact-v2"},
      {"assignment-selector", "physical-input-document-v1"},
      {"route-selector", "system-route-selector-v1"},
      {"path-selector", "sta-path-selector-v1"},
      {"schedule-board", "exact-schedule-board-request-v1"},
      {"phase5-report", "phase5-report-v1"},
      {"logic-path-selector", "sta-path-selector-v1"},
      {"library", "physical-input-document-v1"},
      {"yosys", "external-tool-executable-v1"},
      {"yosys-container", "external-tool-executable-v1"}};
  sources.push_back(request);
  for (const auto &row : config.at("sources").array()) {
    BoundedJson(row.encode()).Keys({"role", "path", "schema", "generation"});
    const auto role = row.at("role").text(), schema = row.at("schema").text();
    Check((schemas.count(role) && schemas.at(role) == schema) ||
              role.rfind("backend.", 0) == 0,
          "unknown source role/schema");
    auto ref = ArtifactRef::Bind(row.at("path").text(), schema);
    Check(Generation(ref).encode() == row.at("generation").encode(),
          "original source generation changed");
    Check(owners.emplace(role, ref).second, "duplicate source role");
    sources.push_back(ref);
  }
  for (const auto *role :
       {"split-manifest", "board", "schedule-selector", "library", "yosys"})
    Check(owners.count(role), "missing required physical source");
  manifest = Read(owners.at("split-manifest"));
  board = Read(owners.at("board"));
  Check(manifest.at("schema").text() == "emuflow.split-manifest/v1",
        "split manifest schema disagrees");
  design = manifest.at("design").text();
  platform = board.at("platform").at("name").text();
  Check(manifest.at("platform").text() == platform,
        "split/board namespace disagrees");
  schedule_selector = Read(owners.at("schedule-selector"));
  Check(schedule_selector.at("schema").text() ==
            "emuflow.tdm-schedule-storage/v4",
        "schedule version disagrees");
  owners.emplace("schedule", Resolve(owners.at("schedule-selector"), "artifact",
                                     "tdm-schedule-store-v4"));
  sources.push_back(owners.at("schedule"));
  runtime =
      R::Parse(BuildVirtualRuntime(owners.at("schedule"), owners.at("board"),
                                   manifest.at("clock_periods").encode()));
  owners.emplace("split", Relative(manifest.at("netlist_storage").text(),
                                   "split-netlists-v3"));
  auto split = SqliteStore(owners.at("split"));
  auto originals = PrepareSplitProjection(split.database(), owners.at("split"));
  sources.insert(sources.end(), originals.begin(), originals.end());
  for (const auto &original : originals) {
    if (original.schema() == "tdm-schedule-store-v4")
      Check(original.path() == owners.at("schedule").path() &&
                original.generation() == owners.at("schedule").generation(),
            "split belongs to another schedule");
    if (original.schema() == "emuir-compact-v2" && owners.count("original"))
      Check(original.path() == owners.at("original").path() &&
                original.generation() == owners.at("original").generation(),
            "split belongs to another original IR");
    if (original.schema() == "assignment-index-v4")
      owners.emplace("split-assignment", original);
  }

  sources.push_back(owners.at("split"));
  (void)ValidateSplitOverlay(owners.at("split"));
  std::map<std::string, R> items;
  for (const auto &item : manifest.at("fpgas").array())
    Check(items.emplace(item.at("fpga").text(), item).second,
          "duplicate manifest FPGA");
  std::set<std::string> chosen;
  if (partial)
    for (const auto &item : config.at("selected_fpgas").array())
      Check(chosen.insert(item.text()).second, "duplicate selected FPGA");
  if (partial)
    Check(!chosen.empty(), "empty FPGA selection");
  std::set<std::string> names;
  for (const auto &fpga : board.at("fpgas").array()) {
    Partition part;
    part.id = fpga.at("id").text();
    part.part = fpga.at("part").text();
    part.budget = fpga;
    Check(!part.id.empty() && part.id == fs::path(part.id).filename() &&
              part.id != "." && part.id != ".." && names.insert(part.id).second,
          "unsafe or duplicate FPGA");
    Check(items.count(part.id), "manifest omits BoardDB FPGA");
    part.split_item = items.at(part.id);
    Check(part.split_item.at("netlist_selector").text() == part.id &&
              part.split_item.at("netlist").text() ==
                  manifest.at("netlist_storage").text(),
          "partition selector differs from split owner");
    auto row = split.database().Prepare(
        "SELECT p.part_index,(SELECT count(*) FROM instance_copies c WHERE "
        "c.part_index=p.part_index),(SELECT count(*) FROM net_segments n WHERE "
        "n.part_index=p.part_index),(SELECT count(*) FROM port_memberships q "
        "WHERE q.part_index=p.part_index) FROM parts p WHERE p.fpga=?");
    row.BindText(1, part.id);
    Check(row.Step(), "split omits BoardDB FPGA");
    part.index = row.TypedInteger(0);
    part.cells = row.TypedInteger(1);
    part.nets = row.TypedInteger(2);
    part.ports = row.TypedInteger(3);
    const auto transport_selector = Relative(
        part.split_item.at("transport").text(), "physical-input-document-v1");
    const auto transport_doc = Read(transport_selector);
    Check(transport_doc.at("schema").text() == "emuflow.transport-storage/v1" &&
              transport_doc.at("logical_schema").text() ==
                  "emuflow.transport-endpoints/v1" &&
              transport_doc.at("header").at("fpga").text() == part.id &&
              transport_doc.at("header").at("design").text() == design &&
              transport_doc.at("header").at("platform").text() == platform,
          "transport selector namespace/version disagrees");
    const auto transport =
        Resolve(transport_selector, "artifact", "transport-store-v2");
    if (!owners.count("transport")) {
      owners.emplace("transport", transport);
      sources.push_back(transport);
    }
    Check(owners.at("transport").path() == transport.path(),
          "partition transports have different owners");
    sources.push_back(transport_selector);
    SqliteStore hops(transport);
    auto count = hops.database().Prepare(
        "SELECT count(*) FROM endpoint_values WHERE fpga=?");
    count.BindText(1, part.id);
    Check(count.Step(), "missing transport census");
    part.endpoints = count.TypedInteger(0);
    if (!partial || chosen.count(part.id))
      selected.push_back(partitions.size());
    partitions.push_back(std::move(part));
  }
  Check(!names.empty() && names.size() <= 256 && names.size() == items.size() &&
            (!partial || std::includes(names.begin(), names.end(),
                                       chosen.begin(), chosen.end())),
        "FPGA coverage differs from BoardDB");
  std::size_t have = 0;
  for (const auto *role : {"original", "assignment-selector", "route-selector",
                           "path-selector", "schedule-board", "phase5-report"})
    have += owners.count(role);
  Check(have == 0 || have == 6, "incomplete original timing context");
  queries = have == 6;
  Check(runtime.at("design").text() == design &&
            runtime.at("platform").text() == platform,
        "runtime namespace disagrees");
  lease = std::make_unique<Lease>(output, sources, resume);
}

void Context::Prepare() {
  Stop();
  const auto root = lease->generation;
  const auto audit = AuditTransportArtifacts(
      owners.at("transport"), (root / "transport-audit.json").string());
  Keep(audit);
  for (const auto &ref : audit.inputs) {
    const auto role = ref.schema() == "assignment-index-v4" ? "split-assignment"
                      : ref.schema() == "tdm-schedule-store-v4" ? "schedule"
                                                                : "";
    if (*role)
      Check(ref.path() == owners.at(role).path() &&
                ref.generation() == owners.at(role).generation(),
            "transport belongs to another split assignment or schedule");
    sources.push_back(ref);
  }
  Check(
      !manifest.has("electrical_binding") || manifest.has("position_hints"),
      "physical Chimew anchors require position hints with electrical binding");
  for (const auto *field : {"position_hints", "electrical_binding"}) {
    if (!manifest.has(field))
      continue;
    const bool hints = std::string(field) == "position_hints";
    const auto selector = Relative(manifest.at(field).text(),
                                   hints ? "position-hints-selector-v1"
                                         : "chimew-binding-selector-v1");
    const auto doc = Read(selector);
    Check(doc.at("schema").text() ==
              (hints ? "emuflow.signal-position-hints-storage/v1"
                     : "emuflow.chimew-binding-storage/v1"),
          "Chimew physical source schema disagrees");
    const auto store =
        Resolve(selector, "artifact",
                hints ? "position-hints-store-v1" : "chimew-binding-store-v1");
    SqliteStore db(store);
    Check(R::Parse(Meta(db.database(), "header")).encode() ==
                  doc.at("header").encode() &&
              R::Parse(Meta(db.database(), "entries_sha256")).text() ==
                  doc.at("entries").at("sha256").text() &&
              R::Parse(Meta(db.database(), "logical_sha256")).text() ==
                  doc.at("logical_sha256").text(),
          "Chimew physical source metadata seal disagrees");
    const auto scalar = [&](const std::string &sql) {
      auto q = db.database().Prepare(sql);
      Check(q.Step(), "missing Chimew source census");
      return q.TypedInteger(0);
    };
    const auto table = hints ? "entries" : "groups";
    Check(scalar(std::string("SELECT count(*) FROM ") + table) ==
              doc.at("entries").at("records").integer(),
          "Chimew physical source row coverage disagrees");
    Check(scalar(std::string("SELECT count(*) FROM (SELECT count(*) AS "
                             "n,min(seq) AS lo,max(seq) AS hi FROM ") +
                 table + ") WHERE n>0 AND (lo!=0 OR hi!=n-1)") == 0,
          "Chimew source sequence is not contiguous");
    if (!hints) {
      Check(R::Parse(Meta(db.database(), "members_sha256")).text() ==
                    doc.at("members").at("sha256").text() &&
                R::Parse(Meta(db.database(), "canonical_sha256")).text() ==
                    doc.at("canonical_sha256").text() &&
                scalar("SELECT count(*) FROM members") ==
                    doc.at("members").at("records").integer() &&
                scalar("SELECT count(*) FROM members m LEFT JOIN groups g ON "
                       "g.seq=m.group_seq WHERE g.seq IS NULL") == 0 &&
                scalar("SELECT count(*) FROM groups g WHERE NOT EXISTS (SELECT "
                       "1 FROM members m WHERE m.group_seq=g.seq)") == 0 &&
                scalar("SELECT count(*) FROM (SELECT group_seq FROM members "
                       "GROUP BY group_seq HAVING min(member_seq)!=0 OR "
                       "max(member_seq)!=count(*)-1)") == 0,
            "Chimew physical binding member coverage disagrees");
    }
    sources.push_back(selector);
    sources.push_back(store);
  }
  descriptor = O{
      {"schema", "emuflow.physical-backend/v1"},
      {"id", backend},
      {"implementation_engine", backend == "vivado"
                                    ? "vivado"
                                    : "emuflow-pack-place+rapidwright-rwroute"},
      {"timing_engine",
       backend == "vivado" ? "vivado" : "rapidwright-lightweight+opensta"},
      {"architecture_class", "xilinx-commercial-device"},
      {"source_model", backend == "vivado" ? "external-proprietary-provider"
                                           : "external-mixed-license-provider"},
      {"qualification", backend == "vivado"
                            ? "vendor-device-implementation-not-board-signoff"
                            : "research-timing-not-vivado-signoff"},
      {"capabilities", O{{"packing", true},
                         {"placement", true},
                         {"routing", true},
                         {"timing", true},
                         {"bitstream", false}}}};
  const auto &settings = config.at("backend_options");
  if (backend == "rapidwright") {
    BoundedJson(settings.encode())
        .Keys({"openparf_runtime", "max_heap", "device_data_root"});
    Check(owners.count("backend.architecture"),
          "RapidWright architecture source is missing");
    Check(bool(owners.count("backend.native")) ==
              bool(owners.count("backend.provider")),
          "RapidWright native constraints/provider coverage disagrees");
    const auto &engine = settings.at("openparf_runtime");
    const auto request =
        Request(root / "runtime-preflight.json",
                O{{"schema", "emuflow.openparf-runtime-request/v1"},
                  {"mode", "preflight"},
                  {"sources", A{}},
                  {"python", engine.at("python")},
                  {"installation", engine.at("installation")},
                  {"environment", engine.at("environment")},
                  {"allow_detailed_legalization_handoff", false},
                  {"use_checkpoint", false}},
                "openparf-runtime-request-v1", sources);
    Keep(RunOpenparfStage(
        request, (root / "runtime-preflight-report.json").string(), cancelled));
    architecture =
        O{{"status", "pass"},
          {"mode", "provided-xilinx-architecture-db"},
          {"path", owners.at("backend.architecture").path()},
          {"sha256", FileHash(owners.at("backend.architecture"), cancelled)},
          {"provider", "rapidwright-xilinx-device-v1"}};
    if (owners.count("backend.provider")) {
      const auto provider = Read(owners.at("backend.provider"));
      Check(provider.at("schema").text() ==
                "emuflow.rapidwright-device-provider/v1",
            "RapidWright provider schema disagrees");
      for (const auto &part : partitions)
        Check(provider.at("device").at("part").text() == part.part,
              "RapidWright provider part differs from BoardDB");
      architecture["provider_revision"] = provider.at("release").at("revision");
      architecture["part"] = provider.at("device").at("part");
    }
  } else {
    BoundedJson(settings.encode())
        .Keys({"place_directive", "route_directive", "threads"});
    std::set<std::string> names;
    for (const auto &part : partitions)
      names.insert(part.part);
    A parts;
    for (const auto &name : names)
      parts.push_back(name);
    architecture = O{{"status", "pass"},
                     {"mode", "platform-parts"},
                     {"parts", parts},
                     {"provider", "vivado-device-database"}};
  }
  if (manifest.has("virtual_board_binding") &&
      !manifest.at("virtual_board_binding").null()) {
    const auto &io = manifest.at("virtual_board_binding");
    A rows{Source("transport", owners.at("transport")),
           Source("schedule", owners.at("schedule"))};
    for (const auto &r : std::vector<std::pair<std::string, std::string>>{
             {"binding", io.at("binding").text()},
             {"lane_map", manifest.at("lane_map").text()},
             {"wiring", io.at("wiring").text()}}) {
      auto ref = Relative(r.second, "physical-input-document-v1");
      sources.push_back(ref);
      rows.push_back(Source(r.first, ref));
    }
    for (const auto &part : partitions)
      for (const auto &r : std::vector<std::pair<std::string, std::string>>{
               {"xdc.", io.at("xdc").at(part.id).text()},
               {"adapter.", io.at("differential_io").at(part.id).text()}}) {
        auto ref = Relative(r.second, "exact-sha256-text-v1");
        sources.push_back(ref);
        rows.push_back(Source(r.first + part.id, ref));
      }
    const auto request = Request(
        root / "fixed-io-request.json",
        O{{"schema", "emuflow.physical-io-stage/v1"}, {"sources", rows}},
        "physical-io-stage-request-v1", sources);
    const auto result =
        PreparePhysicalIo(request, (root / "fixed-io").string(), cancelled);
    Keep(result);
    sources.insert(sources.end(), result.outputs.begin(), result.outputs.end());
    for (auto &part : partitions)
      part.io = Document(root / "fixed-io" / part.id /
                         "virtual-io-physical-contract.json");
  }
  const auto constraints =
      Metadata(owners.at("split-assignment"), "constraints");
  const R reserved = manifest.has("resource_reservations_by_fpga")
                         ? manifest.at("resource_reservations_by_fpga")
                         : R(O{});
  const R expected = constraints.has("resource_reservations_by_fpga")
                         ? constraints.at("resource_reservations_by_fpga")
                         : R(O{});
  SqliteDatabase comparison(":memory:", SqliteDatabase::Mode::kReadOnly);
  RequireEquivalentJson(
      comparison, reserved.encode(), expected.encode(),
      "split resource reservations differ from original assignment");
  if (!queries)
    return;
  (void)Phase5ReportControlReference(owners.at("phase5-report"));
  const auto phase5 = Read(owners.at("phase5-report"));
  bool have_board = false;
  for (const auto &member : phase5.at("generation").at("members").array())
    if (member.at("path").text() == "schedule-board.bin") {
      Check(fs::path(owners.at("schedule-board").path()) ==
                    fs::path(owners.at("phase5-report").path()).parent_path() /
                        "schedule-board.bin" &&
                member.at("sha256").text() ==
                    FileHash(owners.at("schedule-board")) &&
                member.at("bytes").integer() ==
                    I(owners.at("schedule-board").generation().bytes),
            "Phase 5 board policy seal disagrees");
      have_board = true;
    }
  Check(have_board, "Phase 5 omitted the physical board policy");
  const auto assignment_doc = Read(owners.at("assignment-selector"));
  Check(assignment_doc.at("schema").text() ==
            "emuflow.phase3-assignment-storage/v4",
        "assignment selector version disagrees");
  const auto assignment_name =
      assignment_doc.at("cut_nets_index").at("artifact").text();
  Check(fs::path(assignment_name) == fs::path(assignment_name).filename(),
        "assignment owner escapes selector");
  const auto assignment = ArtifactRef::Bind(
      (fs::path(owners.at("assignment-selector").path()).parent_path() /
       assignment_name)
          .string(),
      "assignment-index-v4");
  Check(assignment.path() == owners.at("split-assignment").path() &&
            assignment.generation() ==
                owners.at("split-assignment").generation(),
        "timing queries select another split assignment");
  owners.emplace("assignment", assignment);
  sources.push_back(assignment);
  Check(Metadata(assignment, "design").text() == design &&
            Metadata(assignment, "platform").text() == platform,
        "assignment namespace disagrees");
  const auto exact = Metadata(assignment, "semantic_contract_binary");
  Check(exact.at("schema").text() == "emuflow.phase3-static-exact-binary/v2",
        "physical flow requires current exact data");
  const auto exact_name = exact.at("artifact").text();
  Check(fs::path(exact_name) == fs::path(exact_name).filename(),
        "exact owner escapes assignment");
  owners.emplace(
      "exact",
      ArtifactRef::Bind(
          (fs::path(assignment.path()).parent_path() / exact_name).string(),
          "phase3-exact-data-v2"));
  owners.emplace("routes", Resolve(owners.at("route-selector"), "artifact",
                                   "system-route-store-v3"));
  const auto paths = [&](const ArtifactRef &selector) {
    const auto document = Read(selector);
    Check(document.at("schema").text() == "emuflow.sta-path-database/v3" &&
              document.at("design").text() == design,
          "path selector namespace/version disagrees");
    const auto &payload = document.at("payloads").at("paths");
    const auto name = payload.at("path").text();
    Check(payload.at("format").text() == "sqlite-sta-path/v1" &&
              fs::path(name) == fs::path(name).filename(),
          "invalid path payload owner");
    auto ref = ArtifactRef::Bind(
        (fs::path(selector.path()).parent_path() / name).string(),
        "timing-path-store-v1");
    Check(payload.at("sha256").text() == FileHash(ref),
          "path payload seal disagrees");
    return ref;
  };
  owners.emplace("paths", paths(owners.at("path-selector")));
  owners.emplace("logic-paths", owners.count("logic-path-selector")
                                    ? paths(owners.at("logic-path-selector"))
                                    : owners.at("paths"));
  for (const auto *role : {"exact", "routes", "paths", "logic-paths"})
    sources.push_back(owners.at(role));
  logic_sources = {owners.at("original"),      assignment,
                   owners.at("exact"),         owners.at("routes"),
                   owners.at("schedule"),      owners.at("logic-paths"),
                   owners.at("schedule-board")};
  local_sources = {owners.at("original"),
                   assignment,
                   owners.at("paths"),
                   owners.at("routes"),
                   owners.at("assignment-selector"),
                   owners.at("path-selector"),
                   owners.at("route-selector")};
  const auto request_path = root / "local-prepare.bin";
  CheckedRequestWriter writer(request_path.string(), sources);
  writer.Put<std::uint64_t>(0x31504C51464D5545ULL);
  writer.Put<std::uint32_t>(1);
  for (const auto &r : local_sources)
    writer.Generation(r.generation());
  writer.Finish();
  auto input = local_sources;
  input.push_back(ArtifactRef::Bind(request_path.string(),
                                    "local-path-prepare-request-v1"));
  auto prepared = PrepareLocalPathQueries(
      input, (root / "local-prepared").string(), cancelled);
  Keep(prepared);
  Check(prepared.outputs.size() == 2,
        "local preparation output coverage disagrees");
  local_sources.push_back(prepared.outputs[1]);
}
R Context::Query(const Partition &part, const ArtifactRef &merged,
                 const ArtifactRef &boundary, const fs::path &root, bool local,
                 std::vector<ArtifactRef> &proof) const {
  Stop();
  fs::create_directories(root);
  const auto output = root / "generation", request_path = root / "request.bin";
  const auto library = ArtifactRef::Bind(owners.at("library").path(),
                                         "xilinx-primitive-library-v1");
  std::vector<ArtifactRef> refs;
  if (local) {
    refs = local_sources;
    refs.push_back(merged);
    refs.push_back(library);
    CheckedRequestWriter writer(request_path.string(), refs);
    writer.Put<std::uint64_t>(0x31514C51464D5545ULL);
    writer.Put<std::uint32_t>(1);
    for (const auto &r : refs)
      writer.Generation(r.generation());
    writer.Text(backend == "vivado" ? "vivado" : "xilinx");
    writer.Text(part.id);
    writer.Finish();
    refs.push_back(ArtifactRef::Bind(request_path.string(),
                                     "local-path-query-request-v1"));
    const auto result = WriteLocalPathQueries(refs, output.string(), cancelled);
    Keep(result);
    proof.insert(proof.end(), result.outputs.begin(), result.outputs.end());
    return Document(output / "summary.json");
  }
  auto leases = sources;
  const auto identity = BindPhysicalIdentity(boundary, 2, leases);
  refs.assign(logic_sources.begin(), logic_sources.begin() + 6);
  refs.push_back(merged);
  refs.push_back(identity);
  refs.push_back(logic_sources[6]);
  refs.push_back(library);
  const auto route = Read(owners.at("route-selector"));
  const auto &path_selector = owners.count("logic-path-selector")
                                  ? owners.at("logic-path-selector")
                                  : owners.at("path-selector");
  const auto paths = Read(path_selector);
  R config = O{
      {"schema", "emuflow.logic-segment-query/v1"},
      {"design", design},
      {"platform", platform},
      {"route_sha256", route.at("logical_sha256")},
      {"route_file_sha256", route.at("store_sha256")},
      {"schedule_sha256", schedule_selector.at("logical_sha256")},
      {"schedule_pretty_sha256", schedule_selector.at("logical_pretty_sha256")},
      {"schedule_sections", schedule_selector.at("sections")},
      {"path_sha256", paths.at("payloads").at("paths").at("sha256")},
      {"fpga", part.id},
      {"provider", backend == "vivado" ? "vivado" : "xilinx"},
      {"query_name", "query.tsv"},
      {"identity_name", "identity.json"},
      {"report_name", "logic-query-report.json"}};
  CheckedRequestWriter writer(request_path.string(), refs);
  writer.Put<std::uint64_t>(0x31514C50464D5545ULL);
  writer.Put<std::uint32_t>(1);
  for (const auto &r : refs)
    writer.Generation(r.generation());
  writer.Text(config.encode());
  writer.Finish();
  refs.push_back(ArtifactRef::Bind(request_path.string(),
                                   "logic-segment-query-request-v1"));
  const auto result = RunLogicSegmentQuery(refs, output.string(), cancelled);
  Keep(result);
  proof.insert(proof.end(), result.outputs.begin(), result.outputs.end());
  return Document(output / "logic-query-report.json");
}
} // namespace emuflow::core::physical_flow
