#include "timing/physical_global_sta.hpp"

#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "storage/store.hpp"
#include "timing/global_events.hpp"

#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <sstream>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Object = std::map<std::string, JsonRecord>;
using Array = std::vector<JsonRecord>;

void Check(bool value, const std::string &message) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical Global STA: " + message);
}
std::string Metadata(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT value FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step(), "missing metadata " + key);
  const auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata " + key);
  return value;
}
std::string Hex(const std::array<unsigned char, 32> &value) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result;
  result.reserve(64);
  for (auto byte : value) {
    result += digits[byte >> 4];
    result += digits[byte & 15];
  }
  return result;
}
std::string FileHash(const ArtifactRef &source) {
  std::ifstream input(source.path(), std::ios::binary);
  Check(bool(input), "cannot read sealed source");
  Sha256 hash;
  std::array<char, 65536> block{};
  while (input) {
    input.read(block.data(), block.size());
    hash.update(reinterpret_cast<const unsigned char *>(block.data()),
                input.gcount());
  }
  Check(input.eof(), "cannot finish source seal");
  source.RequireUnchanged();
  return Hex(hash.finish());
}
void Generation(CheckedRequestWriter &request, const ArtifactRef &source) {
  request.Generation(source.generation());
}
std::vector<ArtifactRef>
BindingInputs(const ArtifactRef &routes, const ArtifactRef &schedule,
              const ArtifactRef &board,
              const PhysicalEvidenceCatalog &physical) {
  std::vector<ArtifactRef> result{routes, schedule, board};
  Check(physical.boundary_timing.size() ==
                physical.boundary_identities.size() &&
            physical.boundary_timing.size() ==
                physical.logic_segment_timing.size(),
        "boundary catalog dimensions disagree");
  for (std::size_t i = 0; i < physical.boundary_timing.size(); ++i) {
    Check(physical.boundary_timing[i].fpga ==
                  physical.boundary_identities[i].fpga &&
              physical.boundary_timing[i].fpga ==
                  physical.logic_segment_timing[i].fpga,
          "boundary catalog order disagrees");
    result.push_back(physical.boundary_timing[i].store);
    result.push_back(physical.boundary_identities[i].store);
    result.push_back(physical.logic_segment_timing[i].store);
  }
  for (const auto &item : physical.local_path_timing)
    result.push_back(item.store);
  return result;
}
std::string BindingConfiguration(const PhysicalEvidenceCatalog &physical,
                                 const PhysicalBoardTiming &board_timing,
                                 const std::string &runtime_json) {
  BoundedJson runtime(runtime_json);
  Check(runtime.Text("design") == physical.design &&
            runtime.Text("platform") == physical.platform,
        "runtime and physical namespace disagree");
  Array boundary, locals, clocks;
  for (const auto &item : physical.boundary_timing)
    boundary.emplace_back(item.fpga);
  for (const auto &item : physical.local_path_timing)
    locals.emplace_back(item.fpga);
  BoundedJson clock_map(runtime.Token("virtual_dut_clocks"));
  for (const auto &item : clock_map.Fields()) {
    BoundedJson clock(item.second);
    const auto period = clock.Number("nominal_period_ns");
    Check(period > 0, "runtime clock period is invalid");
    clocks.emplace_back(Array{item.first, period});
  }
  auto encoded =
      JsonRecord(Object{{"design", physical.design},
                        {"platform", physical.platform},
                        {"boundary_fpgas", std::move(boundary)},
                        {"local_fpgas", std::move(locals)},
                        {"uncertainty", physical.clock_uncertainty_ns},
                        {"clocks", std::move(clocks)},
                        {"links", JsonRecord()}})
          .Encode();
  auto fields = BoundedJson(encoded).Fields();
  fields["links"] = board_timing.event_links;
  return BoundedJson::Encode(fields);
}
std::string SchedulePretty(SqliteDatabase &schedule) {
  BoundedJson summary(Metadata(schedule, "native_publication_summary"));
  return summary.Text("logical_pretty_sha256");
}
std::vector<ArtifactRef> ModelSources(const ArtifactRef &manifest,
                                      const fs::path &directory) {
  BoundedJson doc(BoundedJson::Read(manifest));
  Check(doc.Text("schema") == "emuflow.global-event-export/v1" &&
            doc.Text("status") == "pass",
        "model export did not pass");
  Check(fs::path(doc.Text("directory")).lexically_normal() ==
            directory.lexically_normal(),
        "model directory identity disagrees");
  std::vector<ArtifactRef> result{manifest};
  for (const auto &token : BoundedJson::Array(doc.Token("files"))) {
    BoundedJson item(token);
    const auto name = item.Text("name");
    Check(fs::path(name).filename() == name && name != "." && name != "..",
          "model file name is unsafe");
    auto source = ArtifactRef::Bind((directory / name).string(),
                                    "global-event-model-text-v1");
    Check(item.Integer("bytes") >= 0 &&
              std::uint64_t(item.Integer("bytes")) ==
                  source.generation().bytes &&
              item.Text("sha256") == FileHash(source),
          "model file seal disagrees");
    result.push_back(std::move(source));
  }
  return result;
}
} // namespace

PhysicalGlobalStaResult RunPhysicalGlobalSta(
    const ArtifactRef &routes, const ArtifactRef &schedule,
    const ArtifactRef &board, const PhysicalEvidenceCatalog &physical,
    const PhysicalBoardTiming &board_timing, const std::string &runtime_json,
    const std::string &opensta_executable, const fs::path &workspace,
    const volatile std::sig_atomic_t *cancelled,
    const std::string &launcher_reference) {
  Check(workspace.is_absolute() && fs::is_directory(workspace),
        "workspace is unavailable");
  auto inputs = BindingInputs(routes, schedule, board, physical);
  const auto request_path = workspace / "event-binding.bin";
  std::string route_logical, schedule_logical, schedule_pretty;
  {
    SqliteStore route(routes), scheduled(schedule);
    route_logical =
        DecodeJsonString(Metadata(route.database(), "logical_sha256"));
    schedule_logical =
        DecodeJsonString(Metadata(scheduled.database(), "logical_sha256"));
    schedule_pretty = SchedulePretty(scheduled.database());
  }
  {
    CheckedRequestWriter request(request_path.string());
    request.Put<std::uint64_t>(0x31424747464d5545ULL);
    request.Put<std::uint32_t>(1);
    request.Put<std::uint32_t>(physical.boundary_timing.size());
    request.Put<std::uint32_t>(physical.local_path_timing.size());
    for (const auto &source : inputs)
      Generation(request, source);
    request.Text(route_logical);
    request.Text(route_logical);
    request.Text(schedule_logical);
    request.Text(schedule_pretty);
    request.Text(BindingConfiguration(physical, board_timing, runtime_json));
    request.Finish();
  }
  auto request_ref = ArtifactRef::Bind(request_path.string(),
                                       "global-event-binding-request-v1");
  inputs.push_back(request_ref);
  const auto events_path = workspace / "events.sqlite";
  BindGlobalEvents(inputs, events_path.string());
  auto events =
      ArtifactRef::Bind(events_path.string(), "global-event-primary-v1");
  const auto index_path = workspace / "event-index.sqlite";
  IndexGlobalEvents(events, index_path.string());
  auto index = ArtifactRef::Bind(index_path.string(), "global-event-index-v1");
  const auto export_request_path = workspace / "event-export.bin";
  {
    CheckedRequestWriter request(export_request_path.string());
    request.Put<std::uint64_t>(0x31584547464d5545ULL);
    request.Put<std::uint32_t>(1);
    request.Put<std::uint32_t>(1);
    Generation(request, events);
    Generation(request, index);
    request.Finish();
  }
  auto export_request = ArtifactRef::Bind(export_request_path.string(),
                                          "global-event-export-request-v1");
  const auto manifest_path = workspace / "event-model.json";
  ExportGlobalEvents({events, index, export_request}, manifest_path.string());
  auto manifest = ArtifactRef::Bind(manifest_path.string(),
                                    "global-event-export-summary-v1");
  BoundedJson exported(BoundedJson::Read(manifest));
  const fs::path model = exported.Text("directory");
  Check(model.is_absolute() && fs::is_directory(model),
        "model generation is unavailable");
  auto model_sources = ModelSources(manifest, model);
  const auto log_path = workspace / "opensta.log";
  std::ofstream log(log_path, std::ios::binary | std::ios::trunc);
  Check(bool(log), "cannot create OpenSTA log");
  auto executable =
      ArtifactRef::Bind(opensta_executable, "external-tool-executable-v1");
  std::string executable_sha = FileHash(executable), launcher_sha;
  std::vector<ArtifactRef> engine_sources{executable};
  if (!launcher_reference.empty() && fs::is_regular_file(launcher_reference)) {
    auto launcher =
        ArtifactRef::Bind(launcher_reference, "external-tool-executable-v1");
    engine_sources.push_back(launcher);
    if (FileHash(launcher) == executable_sha) {
      const auto *name = std::getenv("EMUFLOW_OPENSTA_CONTAINER_BINARY");
      Check(name && *name,
            "container engine identity lacks an explicit binary");
      auto binary = ArtifactRef::Bind(fs::absolute(name).string(),
                                      "external-tool-executable-v1");
      Check(access(binary.path().c_str(), X_OK) == 0,
            "container engine binary is not executable");
      launcher_sha = executable_sha;
      executable_sha = FileHash(binary);
      engine_sources.push_back(binary);
    }
  }
  log << "EmuFlow OpenSTA executable SHA256 " << executable_sha << '\n';
  if (!launcher_sha.empty())
    log << "EmuFlow OpenSTA launcher SHA256 " << launcher_sha << '\n';
  model_sources.insert(model_sources.end(), engine_sources.begin(),
                       engine_sources.end());
  ToolRequest tool;
  tool.argv = {opensta_executable, "-exit", "analyze.tcl"};
  tool.working_directory = model.string();
  tool.tail_bytes = 64 * 1024;
  tool.termination_grace_ms = 10000;
  auto engine = RunExternalToolStream(
      tool,
      [&](std::istream &output) {
        std::array<char, 65536> bytes{};
        while (output) {
          output.read(bytes.data(), bytes.size());
          log.write(bytes.data(), output.gcount());
        }
        Check(output.eof(), "cannot consume OpenSTA output");
      },
      cancelled, model_sources);
  if (!engine.diagnostic_tail.empty())
    log << engine.diagnostic_tail;
  log.flush();
  Check(bool(log), "cannot finish OpenSTA log");
  log.close();
  const auto tsv_path = model / "measurements.tsv";
  Check(engine.operation.ok() && fs::is_regular_file(tsv_path),
        "OpenSTA failed: " + engine.operation.message + "\n" +
            engine.diagnostic_tail);
  auto tsv = ArtifactRef::Bind(tsv_path.string(), "global-measurement-tsv-v1");
  const auto import_request_path = workspace / "measurement-import.bin";
  {
    CheckedRequestWriter request(import_request_path.string());
    request.Put<std::uint64_t>(0x314d4947464d5545ULL);
    request.Put<std::uint32_t>(1);
    request.Put<std::uint32_t>(1);
    request.Put<std::uint32_t>(0);
    Generation(request, events);
    Generation(request, index);
    Generation(request, tsv);
    request.Finish();
  }
  auto import_request = ArtifactRef::Bind(
      import_request_path.string(), "global-measurement-import-request-v1");
  const auto measurements_path = workspace / "measurements.sqlite";
  ImportGlobalMeasurements({events, index, tsv, import_request},
                           measurements_path.string());
  fs::remove(tsv_path);
  auto measurements = ArtifactRef::Bind(measurements_path.string(),
                                        "global-measurements-store-v1");
  std::string binding, index_header, measurement_header;
  {
    SqliteStore source(events);
    binding = Metadata(source.database(), "binding");
    SqliteStore derived(index);
    index_header = Metadata(derived.database(), "header");
    SqliteStore values(measurements);
    measurement_header = Metadata(values.database(), "header");
  }
  for (const auto &source : inputs)
    source.RequireUnchanged();
  return {std::move(events),
          std::move(index),
          std::move(measurements),
          std::move(binding),
          std::move(index_header),
          std::move(measurement_header),
          model,
          log_path,
          std::move(engine),
          executable_sha,
          launcher_sha,
          std::move(engine_sources)};
}

} // namespace emuflow::core
