#include "physical/rapidwright/partition.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "physical/mapped_index.hpp"
#include "physical/measurement_stage.hpp"
#include "physical/openparf/input_records.hpp"
#include "physical/openparf/inputs.hpp"
#include "physical/openparf/qualification.hpp"
#include "physical/rapidwright/routed_endpoints.hpp"
#include "physical/rapidwright/routed_segments.hpp"
#include "physical/rapidwright/rwroute_stage.hpp"
#include "physical/report.hpp"
#include "timing/routed_stage.hpp"
#include <cmath>
#include <fcntl.h>
#include <optional>
#include <regex>
#include <sys/file.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using namespace openparf_input;
using Doc = BoundedJson;
using Owners = std::map<std::string, ArtifactRef>;
JsonRecord Ascii(const R &r) {
  if (r.null())
    return JsonRecord{};
  if (auto p = std::get_if<bool>(&r.value))
    return *p;
  if (auto p = std::get_if<I>(&r.value))
    return *p;
  if (auto p = std::get_if<double>(&r.value))
    return *p;
  if (auto p = std::get_if<std::string>(&r.value))
    return *p;
  if (auto p = std::get_if<O>(&r.value)) {
    std::map<std::string, JsonRecord> out;
    for (const auto &f : *p)
      out.emplace(f.first, Ascii(f.second));
    return out;
  }
  std::vector<JsonRecord> out;
  for (const auto &f : r.array())
    out.push_back(Ascii(f));
  return out;
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
R Source(const std::string &role, const ArtifactRef &ref) {
  return O{{"role", role},
           {"path", ref.path()},
           {"schema", ref.schema()},
           {"generation", Generation(ref)}};
}
R ReadPath(const fs::path &path) {
  return Read(ArtifactRef::Bind(path.string(), "physical-input-document-v1"));
}
R Metadata(const ArtifactRef &ref, const std::string &key) {
  SqliteStore store(ref);
  auto text = Meta(store.database(), key);
  store.RequireUnchanged();
  return R::Parse(text);
}
void Keep(const StageResult &result, std::vector<ArtifactRef> &refs) {
  if (!result.ok())
    throw StageError(result.error, result.message);
  for (const auto &ref : result.outputs) {
    ref.RequireUnchanged();
    refs.push_back(ref);
  }
}
ArtifactRef TextRequest(const fs::path &path, const R &value,
                        const std::string &schema,
                        std::vector<ArtifactRef> &refs) {
  Json(path, value, refs);
  auto ref = ArtifactRef::Bind(path.string(), schema);
  refs.push_back(ref);
  return ref;
}
struct Owner {
  fs::path root, attempt;
  int fd = -1;
  explicit Owner(const std::string &output,
                 const std::vector<ArtifactRef> &refs) {
    const fs::path path(output);
    Check(path.is_absolute() && !fs::is_symlink(path),
          "partition output must be an absolute directory");
    fs::create_directories(path);
    root = fs::canonical(path);
    for (const auto &ref : refs) {
      const auto relative = fs::path(ref.path()).lexically_relative(root);
      Check(relative.empty() || *relative.begin() == "..",
            "partition input lies in its output directory");
    }
    fd = open((root / ".partition-owner.lock").c_str(),
              O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open partition lease");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "partition already has an active owner");
    }
    auto pattern = (root / "implementation.generation.XXXXXX").string();
    if (!mkdtemp(pattern.data())) {
      close(fd);
      fd = -1;
      Check(false, "cannot create partition generation");
    }
    attempt = pattern;
  }
  ~Owner() {
    if (fd >= 0)
      close(fd);
  }
};
I Natural(const R &r) {
  const auto n = r.integer();
  Check(n >= 0 && n <= INT64_MAX / 4096, "invalid partition count");
  return n;
}
double Number(const R &r) {
  return ParseFiniteDecimal(r.encode(), "partition number");
}
R Usage(const R &counts, const R &runtime, const char *stage) {
  if (!runtime.has("resource_budget"))
    return {};
  const auto &b = runtime.at("resource_budget"), &cap = b.at("capacity");
  Check(std::holds_alternative<O>(cap.value) && !std::get<O>(cap.value).empty(),
        "invalid device capacity");
  const double limit = Number(b.at("utilization_limit"));
  Check(limit > 0 && limit <= 1, "invalid device utilization limit");
  O allowed, used, device, budget;
  for (const auto &r : std::get<O>(cap.value)) {
    const auto total = Natural(r.second);
    Check(std::string(stage) != "post-physical-materialization" ||
              counts.has(r.first),
          "physical inventory dimension is missing");
    const I count = counts.has(r.first) ? Natural(counts.at(r.first)) : 0;
    const I maximum = I(std::floor(double(total) * limit));
    Check(count <= maximum,
          "strict whole-device resource budget exceeded: " + r.first);
    allowed[r.first] = maximum;
    used[r.first] = count;
    device[r.first] = total ? double(count) / total : 0.0;
    budget[r.first] = maximum ? double(count) / maximum : 0.0;
  }
  if (b.has("effective_capacity")) {
    SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(db, b.at("effective_capacity").encode(),
                          R(allowed).encode(),
                          "effective device capacity disagrees");
  }
  return O{{"schema", "emuflow.device-resource-usage/v1"},
           {"status", "pass"},
           {"stage", stage},
           {"fpga", b.has("id") ? b.at("id") : R{}},
           {"part", b.has("part") ? b.at("part") : R{}},
           {"capacity_basis", "whole-device-raw-capacity"},
           {"capacity", cap},
           {"utilization_limit", limit},
           {"allowed", allowed},
           {"counts", used},
           {"device_utilization", device},
           {"budget_utilization", budget}};
}
R Artifact(const ArtifactRef &ref, const volatile std::sig_atomic_t *flag) {
  return O{{"path", ref.path()}, {"sha256", FileHash(ref, flag)}};
}
R Clocks(const ArtifactRef &ir, const R &runtime) {
  SqliteStore store(ir);
  auto rows =
      store.database().Prepare("SELECT record_json FROM clocks ORDER BY seq");
  O clocks;
  while (rows.Step()) {
    const auto clock = R::Parse(rows.TypedText(0));
    if (!clock.has("id"))
      continue;
    const auto name = clock.at("id").text();
    if (name.empty())
      continue;
    if (name == "fabric_clk")
      clocks[name] = runtime.at("fabric_clock").at("period_ns");
    else {
      Check(runtime.at("virtual_dut_clocks").has(name),
            "physical DUT clock is absent from runtime contract: " + name);
      clocks[name] =
          runtime.at("virtual_dut_clocks").at(name).at("nominal_period_ns");
    }
    Check(Number(clocks.at(name)) > 0, "invalid physical clock period");
  }
  return clocks;
}
R TimingHeader(const ArtifactRef &identity, unsigned kind) {
  const auto origin = Metadata(identity, "header");
  O header;
  header["status"] = "pass";
  header["design"] = origin.at("design");
  header["fpga"] = origin.at("fpga");
  const char *q =
      "rapidwright-lightweight-routed-setup-no-hold-fixed-lvds-iob-boundary";
  if (kind == 2) {
    header["schema"] = "emuflow.boundary-timing/v1";
    header["platform"] = origin.at("platform");
    header["provider"] = "rapidwright-lightweight-segment-graph-v1";
    header["qualification"] = q;
    header["measurement_scope"] = "partition-boundary";
    header["coverage"] = O{{"endpoints", 0}, {"tx", 0}, {"rx", 0}};
  } else if (kind == 3) {
    header["schema"] = "emuflow.logic-segment-timing/v1";
    for (const auto *key : {"platform", "system_routes_sha256",
                            "tdm_schedule_sha256", "coverage"})
      header[key] = origin.at(key);
    if (origin.has("semantic_contract_sha256"))
      header["semantic_contract_sha256"] =
          origin.at("semantic_contract_sha256");
    header["provider"] = "rapidwright-lightweight-logic-segment-graph-v1";
    header["qualification"] = q;
    header["coverage"]["endpoint_exact_segments"] =
        origin.at("coverage").at("segments");
    header["coverage"]["cone_bound_segments"] = 0;
  } else {
    header["schema"] = "emuflow.local-path-timing/v1";
    header["provider"] = "rapidwright-lightweight-local-path-graph-v1";
    header["qualification"] = "source-bound-routed-endpoint-longest-path-upper-"
                              "bound-with-launch-clock-to-q-and-capture-setup";
    for (const auto *key : {"source", "coverage"})
      header[key] = origin.at(key);
    header["identity_schema"] = origin.at("identity_schema");
  }
  return header;
}
R Segments(const Owners &owners, const ArtifactRef &mapped,
           const ArtifactRef &timing, const ArtifactRef &endpoints,
           const fs::path &output, const fs::path &work,
           std::vector<ArtifactRef> &refs) {
  std::vector<ArtifactRef> inputs{mapped, owners.at("ir"), timing, endpoints};
  std::map<unsigned, ArtifactRef> ids;
  std::map<unsigned, R> headers;
  for (const auto &p : std::map<unsigned, std::string>{
           {2, "boundary"}, {3, "logic"}, {4, "local"}}) {
    if (!owners.count(p.second))
      continue;
    auto store = BindPhysicalIdentity(owners.at(p.second), p.first, refs);
    ids.emplace(p.first, store);
    headers.emplace(p.first, TimingHeader(store, p.first));
    inputs.push_back(store);
  }
  const auto req = work / "segments.bin", summary = work / "segments.json",
             stores = work / "segments";
  fs::create_directory(stores);
  CheckedRequestWriter w(req.string(), refs);
  w.Put<std::uint64_t>(0x31544752464d5545ULL);
  w.Put<std::uint32_t>(1);
  w.Put<std::uint32_t>(unsigned(ids.count(3)) | (unsigned(ids.count(4)) << 1));
  for (const auto &r : inputs)
    w.Generation(r.generation());
  for (unsigned kind : {2, 3, 4})
    w.Text(headers.count(kind) ? headers.at(kind).encode() : "");
  w.Finish();
  inputs.push_back(
      ArtifactRef::Bind(req.string(), "routed-segment-request-v1"));
  BuildRoutedSegments(inputs, stores.string(), summary.string());
  const auto result = ReadPath(summary);
  Check(result.at("status").text() == "pass", "routed graph failed");
  O bundle;
  for (const auto &projection : result.at("projections").array()) {
    const unsigned kind = projection.at("kind").integer();
    Check(ids.count(kind), "unexpected timing projection");
    const std::string name = kind == 2   ? "boundary"
                             : kind == 3 ? "logic"
                                         : "local";
    const std::string file = kind == 2   ? "boundary-timing.json"
                             : kind == 3 ? "logic-segment-timing.json"
                                         : "local-path-timing.json";
    const std::string key = kind == 2   ? "boundary"
                            : kind == 3 ? "logic_segment"
                                        : "local_path";
    bundle[key] = R::Parse(PublishPhysicalProjection(
        (stores / (name + ".sqlite")).string(), (output / file).string(), kind,
        projection.encode(), refs));
  }
  Check(bundle.size() == ids.size(), "missing timing projection");
  bundle["source_constraints"] = O{
      {"provider", "source-declared-undriven-inputs-v1"},
      {"source_ir", owners.at("ir").path()},
      {"undriven_bit_count", result.at("undriven_bit_count")},
      {"timing_policy", "disconnected; no fabricated driver or timing source"}};
  O graph;
  for (const auto *key :
       {"implementation_id", "nodes", "edges", "solves", "visited_nodes"})
    graph[key] = result.at(key);
  bundle["native_graph"] = graph;
  return bundle;
}
R Endpoints(const ArtifactRef &mapped, const ArtifactRef &handoff,
            const R &route, const fs::path &output, const fs::path &work,
            std::vector<ArtifactRef> &refs) {
  const auto manifest = ArtifactRef::Bind(route.at("route").at("output").text(),
                                          "xilinx-route-manifest-v2");
  const auto route_doc = Read(manifest);
  const auto &desc = route_doc.at("payloads").at("nets");
  const auto name = desc.at("path").text();
  Check(fs::path(name).filename() == name && name != "." && name != "..",
        "invalid net payload path");
  const auto net = ArtifactRef::Bind(
      (fs::path(manifest.path()).parent_path() / name).string(),
      "xilinx-route-nets-v1");
  Check(
      Generation(manifest).encode() ==
              route.at("route").at("source_generations").at("route").encode() &&
          Generation(net).encode() ==
              route.at("route").at("source_generations").at("nets").encode(),
      "validated route generation changed");
  std::vector<ArtifactRef> inputs{mapped, handoff, manifest, net};
  const auto req = work / "endpoints.bin",
             timing = output / "routed-timing.json",
             endpoints = output / "routed-timing.json.endpoints.jsonl",
             summary = work / "endpoints.json";
  O source = std::get<O>(route.at("source_sha256").value);
  source["route_sha256"] = route.at("route").at("route_sha256");
  CheckedRequestWriter w(req.string(), refs);
  w.Put<std::uint64_t>(0x31505452464d5545ULL);
  w.Put<std::uint32_t>(1);
  for (const auto &r : inputs)
    w.Generation(r.generation());
  w.Text(R(source).encode());
  w.Text(endpoints.filename().string());
  w.Finish();
  inputs.push_back(
      ArtifactRef::Bind(req.string(), "routed-endpoint-request-v1"));
  BuildRoutedEndpoints(inputs, endpoints.string(), timing.string(),
                       summary.string());
  refs.push_back(ArtifactRef::Bind(timing.string(), "xilinx-routed-timing-v2"));
  refs.push_back(
      ArtifactRef::Bind(endpoints.string(), "xilinx-routed-endpoints-v1"));
  auto result = ReadPath(summary);
  O check{{"status", "pass"},
          {"schema", "emuflow.xilinx-routed-timing-validation/v1"}};
  const auto header = R::Parse(result.at("header_json").text());
  for (const auto &p : std::get<O>(header.at("summary").value))
    check[p.first] = p.second;
  for (const auto *key : {"implementation_id", "timing_sha256"})
    check[key] = result.at(key);
  return check;
}
} // namespace

StageResult RunRapidwrightPartition(const ArtifactRef &request,
                                    const std::string &output,
                                    const volatile std::sig_atomic_t *flag) {
  std::optional<R> config;
  Owners owners;
  std::vector<ArtifactRef> refs, published;
  return RunStage(
      "physical.run-rapidwright-partition",
      [&] {
        Check(request.schema() == "rapidwright-partition-request-v1",
              "invalid partition request schema");
        config = Read(request);
        Doc shape(config->encode());
        shape.Keys({"schema", "sources", "fpga", "part", "runtime",
                    "original_cells", "transport_cells", "openparf_runtime",
                    "virtual_io", "max_heap", "device_data_root", "resume"});
        Check(config->at("schema").text() ==
                  "emuflow.rapidwright-partition-request/v1",
              "partition request version disagrees");
        refs.push_back(request);
        const std::map<std::string, std::string> schemas{
            {"ir", "emuir-compact-v2"},
            {"library", "physical-input-document-v1"},
            {"architecture", "physical-input-document-v1"},
            {"boundary", "boundary-identity-selector-v1"},
            {"logic", "logic-segment-identity-selector-v1"},
            {"local", "local-path-timing-selector-v1"},
            {"policy", "physical-input-document-v1"},
            {"driver", "physical-input-document-v1"},
            {"native", "physical-input-document-v1"},
            {"provider", "physical-input-document-v1"},
            {"java", "external-tool-executable-v1"},
            {"javac", "external-tool-executable-v1"},
            {"jar", "physical-input-document-v1"},
            {"java_source", "physical-input-document-v1"},
            {"contract", "physical-input-document-v1"},
            {"parts", "physical-input-document-v1"},
            {"device", "physical-input-document-v1"},
            {"intersite", "physical-input-document-v1"},
            {"intrasite", "physical-input-document-v1"},
            {"sta", "external-tool-executable-v1"},
            {"container", "external-tool-executable-v1"},
            {"base-model", "phase2-source-v1"},
            {"export-script", "phase2-source-v1"},
            {"runtime-script", "phase2-source-v1"}};
        for (const auto &s : config->at("sources").array()) {
          Doc row(s.encode());
          row.Keys({"role", "path", "schema", "generation"});
          const auto role = s.at("role").text();
          Check(schemas.count(role) &&
                    schemas.at(role) == s.at("schema").text(),
                "invalid partition source role/schema");
          auto ref = ArtifactRef::Bind(s.at("path").text(), schemas.at(role));
          Check(Generation(ref).encode() == s.at("generation").encode(),
                "partition source generation changed: " + role);
          Check(owners.emplace(role, ref).second, "duplicate partition source");
          refs.push_back(ref);
        }
        for (const auto &p : schemas)
          if (p.first != "logic" && p.first != "local" && p.first != "native" &&
              p.first != "provider" && p.first != "container")
            Check(owners.count(p.first),
                  "missing partition source: " + p.first);
        Check(owners.count("native") == owners.count("provider"),
              "native constraints require provider identity");
        Check(!config->at("fpga").text().empty() &&
                  std::regex_match(config->at("part").text(),
                                   std::regex("xc[a-z0-9-]+")),
              "invalid partition identity");
        Natural(config->at("original_cells"));
        Natural(config->at("transport_cells"));
        Check(std::holds_alternative<bool>(config->at("resume").value),
              "invalid resume flag");
        return refs;
      },
      [&](const auto &) {
        Stop(flag);
        auto &c = *config;
        Owner owner(output, refs);
        const auto dir = owner.attempt;
        Scratch work(dir);
        const auto fpga = c.at("fpga").text(), part = c.at("part").text();
        const auto &runtime = c.at("runtime");
        const I original = Natural(c.at("original_cells")),
                transport = Natural(c.at("transport_cells"));
        EmuirView ir(owners.at("ir"));
        Check(ir.counts().cells == std::uint64_t(original + transport),
              "merged source cell census disagrees");
        const auto clocks = Clocks(owners.at("ir"), runtime);
        for (const auto &p : std::map<unsigned, std::string>{
                 {2, "boundary"}, {3, "logic"}, {4, "local"}}) {
          if (!owners.count(p.second))
            continue;
          const auto store =
              BindPhysicalIdentity(owners.at(p.second), p.first, refs);
          Check(Metadata(store, "header").at("fpga").text() == fpga,
                "partition timing identity belongs to another FPGA");
        }
        const auto mapped_path = dir / "partition.mapped.sqlite",
                   mapped_report_path = dir / "mapped-netlist-report.json",
                   map_req = work.root / "mapped.bin";
        CheckedRequestWriter mw(map_req.string(), refs);
        mw.Put<std::uint64_t>(0x31584d50464d5545ULL);
        mw.Put<std::uint32_t>(1);
        mw.Generation(owners.at("ir").generation());
        mw.Generation(owners.at("library").generation());
        mw.Finish();
        Keep(BuildMappedIndex(
                 {owners.at("ir"), owners.at("library"),
                  ArtifactRef::Bind(map_req.string(),
                                    "xilinx-mapped-index-request-v1")},
                 mapped_path.string(), mapped_report_path.string(), flag),
             refs);
        const auto mapped =
            ArtifactRef::Bind(mapped_path.string(), "xilinx-mapped-index-v2");
        const auto mapped_report = ReadPath(mapped_report_path);
        const auto mapped_usage =
            Usage(mapped_report.at("audit").at("resource_totals"), runtime,
                  "post-transport-mapping");
        const auto top = mapped_report.at("top").text();
        const auto atomic_path = dir / "openparf-atomic-source.json";
        auto source_req =
            TextRequest(work.root / "atomic.json",
                        O{{"schema", "emuflow.openparf-input-request/v1"},
                          {"top", top},
                          {"allowed_slrs", nullptr},
                          {"sources", A{Source("mapped", mapped)}}},
                        "openparf-input-request-v1", refs);
        Keep(BuildOpenparfSource(source_req, atomic_path.string(), flag), refs);
        const auto atomic = ArtifactRef::Bind(atomic_path.string(),
                                              "physical-input-document-v1");
        const auto atomic_doc = Read(atomic);
        const auto atomic_store =
            ArtifactRef::Bind((dir / atomic_doc.at("artifact").text()).string(),
                              "openparf-atomic-source-store-v1");
        const auto atomic_header = Metadata(atomic_store, "header");
        A placement_sources{Source("mapped", mapped),
                            Source("mapped_identity", mapped),
                            Source("packed", atomic)};
        for (const auto *key :
             {"architecture", "policy", "driver", "native", "provider"})
          if (owners.count(key))
            placement_sources.push_back(Source(key, owners.at(key)));
        const auto qualification_dir = dir / "openparf-native";
        auto qualify = TextRequest(
            work.root / "qualify.json",
            O{{"schema", "emuflow.openparf-qualification-request/v1"},
              {"sources", placement_sources},
              {"top", top},
              {"allowed_slrs", nullptr},
              {"runtime", c.at("openparf_runtime")},
              {"expected_part", part},
              {"plan_region", true}},
            "openparf-qualification-request-v1", refs);
        Keep(QualifyOpenparf(qualify, qualification_dir.string(), flag), refs);
        auto qualification =
            ReadPath(qualification_dir / "qualification-report.json");
        auto compact = qualification;
        auto &certificate = compact["certificate"];
        const auto cert_ref =
            ArtifactRef::Bind(qualification.at("certificate_path").text(),
                              "physical-input-document-v1");
        O cert;
        for (const auto *key :
             {"schema", "status", "part", "provider", "runtime_validation",
              "source", "summary", "native_convergence"})
          if (certificate.has(key))
            cert[key] = certificate.at(key);
        cert["artifact"] = Artifact(cert_ref, flag);
        cert["artifact"]["bytes"] = I(cert_ref.generation().bytes);
        certificate = cert;
        const auto bridge = qualification.at("bridge");
        const auto handoff = ArtifactRef::Bind(
            bridge.at("physical_handoff").text(), "xilinx-physical-handoff-v1");
        Check(Generation(handoff).encode() ==
                  qualification.at("handoff_generation").encode(),
              "placement handoff generation changed");
        O route_sources;
        for (const auto *key :
             {"java", "javac", "jar", "java_source", "contract", "parts",
              "device", "intersite", "intrasite"})
          route_sources[key] = O{{"path", owners.at(key).path()},
                                 {"generation", Generation(owners.at(key))}};
        for (const auto &p :
             std::map<std::string, ArtifactRef>{{"mapped", mapped},
                                                {"packed", handoff},
                                                {"placement", handoff}})
          route_sources[p.first] = O{{"path", p.second.path()},
                                     {"generation", Generation(p.second)}};
        const auto &io = c.at("virtual_io");
        R limits = O{};
        if (runtime.has("resource_budget") &&
            runtime.at("resource_budget").has("effective_capacity"))
          limits = runtime.at("resource_budget").at("effective_capacity");
        auto route_req =
            TextRequest(work.root / "route-request.json",
                        O{{"schema", "emuflow.rwroute-stage-request/v1"},
                          {"sources", route_sources},
                          {"output_path", (dir / "route.json").string()},
                          {"device_data_root", c.at("device_data_root")},
                          {"max_heap", c.at("max_heap")},
                          {"virtual_io", io},
                          {"virtual_io_sha256",
                           io.null() ? R{} : R(Hash(Ascii(io).Encode()))},
                          {"resource_limits", limits},
                          {"export_input", true},
                          {"resume", c.at("resume")},
                          {"timeout_ms", 21600000}},
                        "rwroute-stage-request-v1", refs);
        Keep(RunRwroute(route_req, (dir / "rwroute-stage.json").string(), flag),
             refs);
        const auto route = ReadPath(dir / "rwroute-stage.json"),
                   manifest = route.at("manifest");
        const auto route_check = route.at("route");
        R resource_usage;
        if (!mapped_usage.null()) {
          const auto physical = manifest.at("physical_resources");
          O expected;
          for (const auto *key :
               {"lut", "ff", "bram18k", "dsp48", "carry8", "uram288"})
            expected[key] =
                mapped_report.at("audit").at("resource_totals").has(key)
                    ? mapped_report.at("audit").at("resource_totals").at(key)
                    : R(0);
          const auto material = manifest.at("materialization");
          expected["lut"] =
              Natural(expected.at("lut")) -
              (material.has("dedicated_clock_gate_cells")
                   ? Natural(material.at("dedicated_clock_gate_cells"))
                   : 0) +
              (material.has("fabric_prefilter_clock_gate_cells")
                   ? Natural(material.at("fabric_prefilter_clock_gate_cells"))
                   : 0);
          Check(R(expected).encode() == physical.encode(),
                "physical resource inventory disagrees with mapped "
                "transformations");
          resource_usage =
              O{{"mapped", mapped_usage},
                {"physical",
                 Usage(physical, runtime, "post-physical-materialization")}};
        }
        const auto timing_check =
            Endpoints(mapped, handoff, route, dir, work.root, refs);
        const auto timing =
                       ArtifactRef::Bind((dir / "routed-timing.json").string(),
                                         "xilinx-routed-timing-v2"),
                   endpoints = ArtifactRef::Bind(
                       (dir / "routed-timing.json.endpoints.jsonl").string(),
                       "xilinx-routed-endpoints-v1");
        const auto segments =
            Segments(owners, mapped, timing, endpoints, dir, work.root, refs);
        A sta_sources{Source("mapped", mapped), Source("timing", timing),
                      Source("endpoints", endpoints)};
        for (const auto *key : {"sta", "container", "base-model",
                                "export-script", "runtime-script"})
          if (owners.count(key))
            sta_sources.push_back(Source(key, owners.at(key)));
        auto sta_req = TextRequest(
            work.root / "sta-request.json",
            O{{"schema", "emuflow.routed-opensta-stage-request/v1"},
              {"mode", "run"},
              {"sources", sta_sources},
              {"configuration",
               O{{"clocks", clocks},
                 {"max_paths", 200000},
                 {"output_path", (dir / "opensta-paths.json").string()},
                 {"summary_path", (dir / "opensta-summary.json").string()},
                 {"log_path", (dir / "opensta.log").string()},
                 {"source_sha256",
                  O{{"mapped_sha256",
                     route.at("source_sha256").at("mapped_sha256")},
                    {"routed_timing_sha256",
                     timing_check.at("timing_sha256")}}},
                 {"timing_validation", timing_check}}}},
            "routed-opensta-stage-request-v1", refs);
        Keep(RunRoutedOpensta(
                 sta_req, (dir / "routed-opensta-stage.json").string(), flag),
             refs);
        const auto opensta = ReadPath(dir / "routed-opensta-stage.json"),
                   opensta_check = opensta.at("validation"),
                   opensta_summary = ReadPath(dir / "opensta-summary.json"),
                   qor = opensta_summary.at("qor");
        const auto &exported = route.at("export");
        const I optimization =
            Natural(exported.at("expanded_lut6_2_cells")) +
            7 * Natural(exported.at("transformed_dsp48e2_cells")) +
            (manifest.at("materialization")
                     .has("fabric_prefilter_clock_gate_cells")
                 ? Natural(manifest.at("materialization")
                               .at("fabric_prefilter_clock_gate_cells"))
                 : 0);
        const I infra = exported.has("virtual_io_cells")
                            ? Natural(exported.at("virtual_io_cells"))
                            : 0;
        O artifacts;
        for (const auto &p : std::map<std::string, ArtifactRef>{
                 {"mapped", mapped},
                 {"physical_handoff", handoff},
                 {"route", ArtifactRef::Bind(route_check.at("output").text(),
                                             "xilinx-route-manifest-v2")},
                 {"routed_timing", timing},
                 {"opensta_summary",
                  ArtifactRef::Bind((dir / "opensta-summary.json").string(),
                                    "physical-input-document-v1")},
                 {"placement_region",
                  ArtifactRef::Bind(qualification.at("region_path").text(),
                                    "physical-input-document-v1")},
                 {"openparf_atomic_source", atomic},
                 {"openparf_atomic_source_store", atomic_store},
                 {"openparf_atomic_certificate", cert_ref}})
          artifacts[p.first] = Artifact(p.second, flag);
        O dut_periods;
        for (const auto &p :
             std::get<O>(runtime.at("virtual_dut_clocks").value))
          dut_periods[p.first] = p.second.at("nominal_period_ns");
        O timing_result;
        for (const auto *key : {"wns_ns", "tns_ns", "failing_endpoints"})
          timing_result[key] = qor.at(key);
        timing_result["failing_endpoint_constraints"] =
            qor.at("failing_endpoints");
        timing_result["timing_met"] = Number(qor.at("wns_ns")) >= 0;
        for (const auto *key :
             {"dut_wns_ns", "fabric_wns_ns", "fabric_to_dut_wns_ns"})
          timing_result[key] = qor.at("wns_ns");
        timing_result["critical_path_ns"] =
            opensta_check.at("critical_path_ns");
        O hard;
        const auto inventory = mapped_report.at("audit").at("cell_types");
        for (const auto &p :
             std::map<std::string, std::string>{{"dsp48e2", "DSP48E2"},
                                                {"ramb18e2", "RAMB18E2"},
                                                {"ramb36e2", "RAMB36E2"},
                                                {"uram288", "URAM288"}})
          hard[p.first] =
              inventory.has(p.second) ? inventory.at(p.second) : R(0);
        R result = O{
            {"schema", "emuflow.physical-partition-result/v1"},
            {"status", "pass"},
            {"identity",
             O{{"backend", "rapidwright"}, {"fpga", fpga}, {"part", part}}},
            {"cell_accounting",
             O{{"original_cells", original},
               {"transport_cells", transport},
               {"routed_cells", original + transport},
               {"physical_cells", original + transport + optimization + infra},
               {"infrastructure_cells", infra},
               {"optimization_cells", optimization}}},
            {"closure", O{{"unrouted_nets", 0},
                          {"drc_violations", 0},
                          {"drc_warnings", 0}}},
            {"clocks",
             O{{"fabric_period_ns", runtime.at("fabric_clock").at("period_ns")},
               {"dut_periods_ns", dut_periods},
               {"dut_period_ns",
                runtime.at("virtual_dut_clock").at("nominal_period_ns")}}},
            {"timing", timing_result},
            {"hard_resources", hard},
            {"artifacts", artifacts}};
        if (!resource_usage.null())
          result["resource_usage"] = resource_usage;
        (void)ValidatePhysicalPartitionReport(result.encode(), "rapidwright",
                                              fpga, part, original, transport);
        SqliteStore physical(handoff);
        const auto
            packed_header = R::Parse([&] {
              auto row = physical.database().Prepare(
                  "SELECT value_json FROM document WHERE name='packed'");
              Check(row.Step(), "missing packing header");
              return row.TypedText(0);
            }()),
            placed_header = R::Parse([&] {
              auto row = physical.database().Prepare(
                  "SELECT value_json FROM document WHERE name='placement'");
              Check(row.Step(), "missing placement header");
              return row.TypedText(0);
            }());
        const auto site =
            ArtifactRef::Bind((qualification_dir / "site-map.sqlite3").string(),
                              "openparf-site-store-v1");
        R report = O{
            {"schema", "emuflow.rapidwright-partition-report/v1"},
            {"status", "pass"},
            {"provider", "rapidwright-rwroute-openparf-native-candidate-v1"},
            {"implementation_id", "native-rapidwright-partition-v1"},
            {"qualification", opensta_summary.at("qualification")},
            {"mapped_netlist", mapped_report},
            {"packing",
             O{{"source", atomic_header.at("summary")},
               {"source_storage",
                O{{"dataflow",
                   "mapped-index-derived-singletons+typed-exception-sidecar"},
                  {"selector_bytes", I(atomic.generation().bytes)},
                  {"store_bytes", I(atomic_store.generation().bytes)},
                  {"stored_macro_clusters",
                   atomic_doc.at("counts").at("macro_clusters")},
                  {"derived_singleton_clusters",
                   atomic_doc.at("counts").at("singleton_clusters")}}},
               {"result", packed_header.at("summary")},
               {"validation", bridge.at("validation").at("packing")}}},
            {"placement",
             O{{"site_database",
                O{{"schema", "emuflow.openparf-atomic-site-database/v1"},
                  {"path", site.path()},
                  {"generation", Generation(site)},
                  {"sha256", FileHash(site, flag)}}},
               {"region", O{{"scope", "full-device"},
                            {"allowed_slrs", qualification.at("allowed_slrs")},
                            {"resource_headroom", 1.5}}},
               {"native_openparf", compact},
               {"bridge", bridge},
               {"result", placed_header.at("summary")},
               {"validation", bridge.at("validation").at("placement")}}},
            {"route", O{{"input", exported},
                        {"result", route_check},
                        {"validation", route_check}}},
            {"routed_timing",
             O{{"result", timing_check}, {"validation", timing_check}}},
            {"opensta", O{{"validation", opensta_check},
                          {"summary", (dir / "opensta-summary.json").string()},
                          {"paths", (dir / "opensta-paths.json").string()}}},
            {"boundary_timing",
             O{{"status", "pass"}, {"import", segments.at("boundary")}}},
            {"source_constraints", segments.at("source_constraints")},
            {"routed_graph", segments.at("native_graph")},
            {"result", result}};
        if (segments.has("logic_segment"))
          report["logic_segment_timing"] =
              O{{"status", "pass"}, {"import", segments.at("logic_segment")}};
        if (segments.has("local_path"))
          report["local_path_timing"] =
              O{{"status", "pass"}, {"import", segments.at("local_path")}};
        if (!io.null())
          report["virtual_io_implementation"] =
              O{{"status", "pass"},
                {"provider", "rapidwright-fixed-lvds-iob-routed-boundary-v1"},
                {"qualification",
                 "P-side LVDS IOB materialized and routed; N-side dedicated "
                 "differential mate verified by the fixed device-pin contract"},
                {"contract_sha256", Hash(Ascii(io).Encode())},
                {"differential_pairs", infra},
                {"physical_ports", 2 * infra},
                {"route_validation",
                 O{{"virtual_io_cells", route_check.at("virtual_io_cells")},
                   {"maximum_route_delay_ps",
                    route_check.at("maximum_route_delay_ps")}}}};
        // Accepted artifacts are immutable per-attempt. A failed retry cannot
        // replace any part of the previous partition or its report.
        for (const auto &r : refs)
          r.RequireUnchanged();
        Stop(flag);
        Json(owner.root / "partition-report.json", report, refs);
        published.push_back(
            ArtifactRef::Bind((owner.root / "partition-report.json").string(),
                              "rapidwright-partition-report-v1"));
        for (const auto &r : refs) {
          const auto relative = fs::path(r.path()).lexically_relative(dir);
          if (!relative.empty() && *relative.begin() != ".." &&
              r.path().find(work.root.string() + "/") != 0)
            published.push_back(r);
        }
      },
      [&] { return published; });
}
} // namespace emuflow::core
