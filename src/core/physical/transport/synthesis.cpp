#include "physical/transport/synthesis.hpp"
#include "common/tool_runtime.hpp"
#include "frontend/yosys_hierarchy.hpp"
#include "ir/emuir_view.hpp"
#include "ir/resource_view.hpp"
#include "physical/openparf/input_records.hpp"
#include "physical/transport/fanout.hpp"
#include <algorithm>
#include <climits>
#include <optional>
#include <regex>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using namespace openparf_input;
using Doc = BoundedJson;
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
std::string Top(const ArtifactRef &rtl) {
  std::ifstream in(rtl.path());
  Check(bool(in), "cannot read transport RTL");
  std::string line;
  std::smatch found;
  const std::regex pattern(R"(^module\s+([A-Za-z_][A-Za-z0-9_$]*))");
  while (std::getline(in, line)) {
    Check(line.size() <= 1024 * 1024, "transport RTL line exceeds bound");
    if (std::regex_search(line, found, pattern)) {
      rtl.RequireUnchanged();
      return found[1];
    }
  }
  Check(false, "cannot find transport top module");
  return {};
}
std::string Script(const ArtifactRef &controller, const ArtifactRef &rtl,
                   const std::string &top) {
  return "read_verilog -sv " + JsonQuote(controller.path()) + " " +
         JsonQuote(rtl.path()) + "; hierarchy -check -top " + top +
         "; proc; flatten; opt; memory_dff; memory_map; techmap; opt; "
         "dffunmap; abc -lut 6; dffunmap; delete t:$scopeinfo; clean; check; "
         "write_json";
}
struct Workspace {
  fs::path root;
  bool accepted = false;
  explicit Workspace(const fs::path &parent) {
    auto p = (parent / ".transport-synthesis.XXXXXX").string();
    Check(mkdtemp(p.data()), "cannot create transport synthesis generation");
    root = p;
  }
  ~Workspace() {
    std::error_code e;
    for (const auto &item : fs::directory_iterator(root, e))
      if (accepted || item.path().extension() != ".log")
        fs::remove_all(item.path(), e);
    if (accepted)
      fs::remove(root, e);
  }
};
} // namespace
StageResult SynthesizeTransport(const ArtifactRef &request,
                                const std::string &output,
                                const volatile std::sig_atomic_t *flag) {
  std::optional<R> config;
  std::map<std::string, ArtifactRef> owners;
  std::vector<ArtifactRef> refs;
  fs::path destination;
  return RunStage(
      "physical.synthesize-transport",
      [&] {
        Check(request.schema() == "transport-synthesis-request-v1",
              "transport request schema disagrees");
        config = Read(request);
        Doc shape(config->encode());
        shape.Keys({"schema", "sources", "maximum_fanout", "timeout_ms"});
        Check(config->at("schema").text() ==
                  "emuflow.transport-synthesis-request/v1",
              "transport request version disagrees");
        refs.push_back(request);
        const auto maximum = config->at("maximum_fanout").integer(),
                   timeout = config->at("timeout_ms").integer();
        Check(maximum >= 2 && maximum <= INT_MAX && timeout >= 0,
              "invalid transport synthesis policy");
        const std::map<std::string, std::string> schemas{
            {"runtime", "yosys-source-document-v1"},
            {"rtl", "yosys-source-document-v1"},
            {"yosys", "external-tool-executable-v1"},
            {"container", "external-tool-executable-v1"}};
        for (const auto &row : config->at("sources").array()) {
          Doc fields(row.encode());
          fields.Keys({"role", "path", "schema", "generation"});
          const auto role = row.at("role").text();
          Check(schemas.count(role) &&
                    schemas.at(role) == row.at("schema").text(),
                "invalid transport source role/schema");
          auto ref = ArtifactRef::Bind(row.at("path").text(), schemas.at(role));
          Check(Generation(ref).encode() == row.at("generation").encode(),
                "transport source generation changed: " + role);
          Check(owners.emplace(role, ref).second, "duplicate transport source");
          refs.push_back(ref);
        }
        for (const auto *key : {"runtime", "rtl", "yosys"})
          Check(owners.count(key), "missing transport synthesis source");
        const fs::path path(output);
        Check(path.is_absolute() && !fs::exists(path) && !fs::is_symlink(path),
              "transport synthesis output must be a fresh absolute directory");
        fs::create_directories(path.parent_path());
        destination = fs::canonical(path.parent_path()) / path.filename();
        for (const auto &ref : refs) {
          const auto relative =
              fs::path(ref.path()).lexically_relative(destination);
          Check(relative.empty() || *relative.begin() == "..",
                "transport source lies in its output directory");
        }
        return refs;
      },
      [&](const auto &) {
        Stop(flag);
        Workspace work(destination.parent_path());
        const auto stage = work.root / "stage";
        fs::create_directory(stage);
        const auto top = Top(owners.at("rtl"));
        const auto policy = config->at("maximum_fanout").integer();
        const auto importer_request = work.root / "import.bin",
                   importer_summary = work.root / "import.json";
        CheckedRequestWriter writer(importer_request.string(), refs);
        writer.Put<std::uint64_t>(0x31524959464d5545ULL);
        writer.Put<std::uint32_t>(1);
        writer.Put<std::uint32_t>(0);
        writer.Text(top);
        writer.Text("<generic-yosys-stdout>");
        writer.Put<std::uint32_t>(1);
        writer.Text("fabric_clk");
        writer.Finish();
        const auto bound = ArtifactRef::Bind(importer_request.string(),
                                             "yosys-emuir-import-request-v1");
        ToolRequest tool;
        tool.argv = {owners.at("yosys").path(), "-q", "-p",
                     Script(owners.at("runtime"), owners.at("rtl"), top)};
        tool.working_directory = work.root.string();
        tool.timeout_ms = config->at("timeout_ms").integer();
        tool.tail_bytes = 65536;
        tool.termination_grace_ms = 1000;
        if (owners.count("container"))
          tool.environment.emplace_back("EMUFLOW_YOSYS_CONTAINER_BINARY",
                                        owners.at("container").path());
        std::string fanout;
        StageResult imported;
        const auto log = work.root / "transport-yosys.log";
        const auto result = RunExternalToolStream(
            tool,
            [&](std::istream &stream) {
              imported = ImportFlatYosysEmuir(
                  bound, stream, (stage / "transport.emuir.sqlite").string(),
                  importer_summary.string(), [&](SqliteDatabase &db) {
                    fanout = BufferTransportFanout(db, policy, flag);
                  });
              if (!imported.ok())
                throw StageError(imported.error, imported.message);
            },
            flag, refs, log.string());
        if (!result.operation.ok())
          throw StageError(
              result.operation.error,
              "transport Yosys/import failed; log=" + log.string() + "\n" +
                  result.operation.message + "\n" + result.diagnostic_tail);
        Check(imported.ok() && !fanout.empty(),
              "transport import did not finalize");
        for (const auto &ref : imported.outputs)
          ref.RequireUnchanged();
        Stop(flag);
        const auto ir = ArtifactRef::Bind(
            (stage / "transport.emuir.sqlite").string(), "emuir-compact-v2");
        const auto transform = R::Parse(fanout);
        Json(stage / "transport-fanout-buffering.json", transform, refs);
        SqliteStore owner(ir);
        auto &db = owner.database();
        const auto counts = R::Parse(Meta(db, "record_counts_json"));
        O resources;
        auto amounts =
            db.Prepare("SELECT r.key,SUM(r.value*c.amount) FROM (SELECT "
                       "resources_seq,count(*) AS amount FROM cell_records "
                       "GROUP BY resources_seq) c JOIN payloads p ON "
                       "p.seq=c.resources_seq,json_each(p.value) r GROUP BY "
                       "r.key ORDER BY r.key");
        while (amounts.Step()) {
          const auto name = amounts.TypedText(0);
          const auto amount = amounts.TypedInteger(1);
          Check(std::find(kResourceFields.begin(), kResourceFields.end(),
                          name) != kResourceFields.end() &&
                    amount >= 0,
                "invalid transport resource count");
          if (amount)
            resources[name] = amount;
        }
        const auto transform_ref = ArtifactRef::Bind(
            (stage / "transport-fanout-buffering.json").string(),
            "physical-input-document-v1");
        const auto ir_final = (destination / "transport.emuir.sqlite").string(),
                   log_final = (destination / "transport-yosys.log").string();
        const auto report =
            O{{"schema", "emuflow.transport-synthesis-report/v1"},
              {"status", "pass"},
              {"provider", "yosys-generic-lut6-ff"},
              {"implementation_id", "native-transport-synthesis-v1"},
              {"top", top},
              {"instances", counts.at("cell_records")},
              {"nets", counts.at("net_records")},
              {"resources", resources},
              {"output", ir_final},
              {"sha256", FileHash(ir, flag)},
              {"fanout_buffering",
               O{{"output",
                  (destination / "transport-fanout-buffering.json").string()},
                 {"sha256", FileHash(transform_ref, flag)},
                 {"summary", transform.at("summary")},
                 {"policy", transform.at("policy")}}},
              {"engine", R::Parse(ToolResultJson(result))},
              {"log", log_final},
              {"log_sha256",
               FileHash(ArtifactRef::Bind(log.string(), "exact-sha256-text-v1"),
                        flag)},
              {"executable_sha256",
               FileHash(
                   owners.at(owners.count("container") ? "container" : "yosys"),
                   flag)},
              {"import",
               Read(ArtifactRef::Bind(importer_summary.string(),
                                      "yosys-emuir-import-summary-v1"))}};
        Json(stage / "transport-synthesis-report.json", report, refs);
        owner.RequireUnchanged();
        for (const auto &ref : refs)
          ref.RequireUnchanged();
        fs::rename(log, stage / "transport-yosys.log");
        // New-directory publication requires staging directly beside the
        // target.
        const auto sibling = destination.parent_path() /
                             (work.root.filename().string() + ".publish");
        fs::rename(stage, sibling);
        try {
          PublishNewDirectory(sibling.string(), destination.string(), refs);
        } catch (...) {
          std::error_code e;
          fs::remove_all(sibling, e);
          throw;
        }
        work.accepted = true;
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(
                (destination / "transport-synthesis-report.json").string(),
                "transport-synthesis-report-v1"),
            ArtifactRef::Bind((destination / "transport.emuir.sqlite").string(),
                              "emuir-compact-v2"),
            ArtifactRef::Bind(
                (destination / "transport-fanout-buffering.json").string(),
                "physical-input-document-v1"),
            ArtifactRef::Bind((destination / "transport-yosys.log").string(),
                              "exact-sha256-text-v1")};
      });
}
} // namespace emuflow::core
