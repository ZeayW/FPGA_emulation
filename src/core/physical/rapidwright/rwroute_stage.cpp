#include "physical/rapidwright/rwroute_stage.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "common/tool_runtime.hpp"
#include "physical/rapidwright/route_validation.hpp"
#include "physical/rapidwright/rwroute_export.hpp"
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <regex>
#include <sys/file.h>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Fields = std::map<std::string, std::string>;
using Owners = std::map<std::string, ArtifactRef>;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact, "RWRoute stage: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "RWRoute stage cancelled");
}
void Need(const StageResult &value) {
  if (!value.ok())
    throw StageError(value.error, value.message);
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return "[" + std::to_string(g.device) + "," + std::to_string(g.inode) + "," +
         std::to_string(g.bytes) + "," + std::to_string(g.modified_seconds) +
         "," + std::to_string(g.modified_nanoseconds) + "," +
         std::to_string(g.changed_seconds) + "," +
         std::to_string(g.changed_nanoseconds) + "]";
}
template <class Hash>
std::string Digest(const ArtifactRef &ref,
                   const volatile std::sig_atomic_t *flag) {
  std::ifstream in(ref.path(), std::ios::binary);
  Check(bool(in), "cannot read source");
  Hash h;
  std::array<char, 65536> buffer{};
  while (in) {
    Stop(flag);
    in.read(buffer.data(), buffer.size());
    h.update(reinterpret_cast<unsigned char *>(buffer.data()), in.gcount());
  }
  Check(in.eof(), "source read failed");
  ref.RequireUnchanged();
  std::string result;
  for (auto byte : h.finish()) {
    result += "0123456789abcdef"[byte >> 4];
    result += "0123456789abcdef"[byte & 15];
  }
  return result;
}
Doc Read(const std::string &path) {
  return Doc(Doc::Read(ArtifactRef::Bind(path, "physical-input-document-v1")));
}
void Write(const fs::path &path, const std::string &text,
           const std::vector<ArtifactRef> &sources) {
  PublishTextArtifact(path.string(), sources,
                      [&](auto &out) { out << text << '\n'; });
}
struct Lease {
  int fd = -1;
  explicit Lease(const fs::path &path) {
    fd = open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open output lease");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "route output already has an active owner");
    }
  }
  ~Lease() {
    if (fd >= 0)
      close(fd);
  }
};
fs::path Destination(const std::string &value,
                     const std::vector<ArtifactRef> &sources) {
  fs::path path(value);
  Check(path.is_absolute() && !fs::is_symlink(path),
        "output must be an absolute file destination");
  fs::create_directories(path.parent_path());
  path = fs::canonical(path.parent_path()) / path.filename();
  for (const auto &ref : sources) {
    std::error_code e;
    Check(path != ref.path() && !fs::equivalent(path, ref.path(), e),
          "output aliases input");
  }
  return path;
}
std::string Execute(
    const std::vector<std::string> &argv, const fs::path &directory,
    const fs::path &log, const std::vector<ArtifactRef> &sources,
    const volatile std::sig_atomic_t *flag, std::uint64_t timeout,
    const std::vector<std::pair<std::string, std::string>> &environment = {}) {
  ToolRequest tool;
  tool.argv = argv;
  tool.working_directory = directory.string();
  tool.environment = environment;
  tool.timeout_ms = timeout;
  tool.tail_bytes = 8000;
  const auto result = RunExternalToolLogged(tool, log.string(), flag, sources);
  if (!result.operation.ok())
    throw StageError(
        result.operation.error,
        "RWRoute tool failed: exit=" + std::to_string(result.return_code) +
            "; log=" + log.string() + "\n" + result.operation.message + "\n" +
            result.diagnostic_tail);
  Doc report(ToolResultJson(result));
  return Doc::Encode(
      {{"engine_metrics", report.Token("engine_metrics")},
       {"wall_seconds", JsonRecord(result.wall_seconds).Encode()},
       {"log", JsonQuote(log.string())},
       {"log_sha256",
        JsonQuote(Digest<Sha256>(
            ArtifactRef::Bind(log.string(), "physical-input-document-v1"),
            flag))}});
}
void Export(const Owners &owners, const Fields &hashes, Doc &config,
            const fs::path &input, const fs::path &work,
            std::vector<ArtifactRef> &sources) {
  const auto request = work / "export.bin", summary = work / "export.json";
  CheckedRequestWriter writer(request.string(), sources);
  writer.Put<std::uint64_t>(0x31515752554d5545ULL);
  writer.Put<std::uint32_t>(2);
  for (const auto &role : {"mapped", "packed"})
    writer.Generation(owners.at(role).generation());
  for (const auto &field :
       {"mapped_sha256", "packed_sha256", "placement_sha256"})
    writer.Text(hashes.at(field));
  const bool with_io = config.Token("virtual_io") != "null";
  writer.Text(with_io ? config.Text("virtual_io_sha256") : "");
  Doc limits(config.Token("resource_limits"));
  writer.Put<std::uint32_t>(limits.Fields().size());
  for (const auto &limit : limits.Fields()) {
    const auto count = Doc::IntegerToken(limit.second);
    Check(std::set<std::string>{"lut", "ff", "bram18k", "dsp48", "carry8",
                                "uram288"}
                  .count(limit.first) &&
              count >= 0,
          "invalid resource limit");
    writer.Text(limit.first);
    writer.Put<std::uint64_t>(count);
  }
  std::vector<std::string> entries;
  if (with_io) {
    Doc io(config.Token("virtual_io"));
    Check((io.Text("schema") == "emuflow.virtual-io-physical-contract/v1" ||
           io.Text("schema") == "emuflow.virtual-io-physical-contract/v2") &&
              io.Text("status") == "pass",
          "invalid fixed I/O contract");
    entries = Doc::Array(io.Token("entries"), 65536);
  }
  writer.Put<std::uint32_t>(entries.size());
  for (const auto &row : entries) {
    Doc item(row), p(item.Token("p")), n(item.Token("n"));
    const auto bit = item.Integer("logical_bit"),
               width = item.Integer("logical_port_width");
    Check(bit >= 0 && bit < width, "invalid logical I/O width/bit");
    for (const auto &v : {item.Text("id"), item.Text("direction"),
                          p.Text("port"), n.Text("port"), p.Text("package_pin"),
                          n.Text("package_pin"), p.Text("site"), n.Text("site"),
                          item.Text("logical_port"), item.Text("iostandard")})
      writer.Text(v);
    writer.Put<std::uint64_t>(bit);
    writer.Put<std::uint64_t>(width);
  }
  writer.Finish();
  ExportRwroute(
      ArtifactRef::Bind(owners.at("mapped").path(), "xilinx-mapped-index-v2"),
      ArtifactRef::Bind(owners.at("packed").path(),
                        "xilinx-physical-handoff-v1"),
      ArtifactRef::Bind(request.string(), "rwroute-export-request-v2"),
      input.string(), summary.string());
}
std::string Validate(const Owners &owners, const ArtifactRef &route,
                     const ArtifactRef &input, Doc &config,
                     const fs::path &work,
                     const volatile std::sig_atomic_t *flag) {
  std::vector<std::pair<std::string, ArtifactRef>> sources = {
      {"route", route}, {"timing", owners.at("contract")}, {"input", input}};
  for (const auto &role : {"mapped", "packed", "placement"})
    if (owners.count(role))
      sources.emplace_back(role, owners.at(role));
  const auto request = work / "validate.bin",
             summary = work / "validation.json";
  CheckedRequestWriter writer(request.string(), {});
  writer.Put<std::uint64_t>(0x31565250464d5545ULL);
  writer.Put<std::uint32_t>(1);
  writer.Put<std::uint32_t>(sources.size());
  for (const auto &source : sources) {
    writer.Text(source.first);
    writer.Text(source.second.path());
    writer.Text(source.first == "route" ? "xilinx-route-manifest-v2"
                                        : "physical-input-document-v1");
    writer.Generation(source.second.generation());
  }
  writer.Text(
      Doc::Encode({{"schema", JsonQuote("emuflow.route-validation-request/v1")},
                   {"virtual_io", config.Token("virtual_io")},
                   {"virtual_io_sha256", config.Token("virtual_io_sha256")}}));
  writer.Finish();
  Need(ValidateRouteCertificate(
      ArtifactRef::Bind(request.string(), "xilinx-route-validation-request-v1"),
      summary.string(), flag));
  return Read(summary.string()).text();
}
} // namespace
StageResult RunRwroute(const ArtifactRef &request, const std::string &summary,
                       const volatile std::sig_atomic_t *flag) {
  return RunStage(
      "physical.run-rwroute", [&] { return std::vector<ArtifactRef>{request}; },
      [&](const auto &initial) {
        Stop(flag);
        Doc config(Doc::Read(request));
        config.Keys({"schema", "sources", "output_path", "max_heap",
                     "device_data_root", "virtual_io", "virtual_io_sha256",
                     "resource_limits", "export_input", "resume",
                     "timeout_ms"});
        Check(config.Text("schema") == "emuflow.rwroute-stage-request/v1",
              "request version disagrees");
        const auto heap = config.Text("max_heap");
        Check(std::regex_match(heap, std::regex("[1-9][0-9]*[gGmM]")),
              "maximum heap must be a positive Java g/m size");
        const auto timeout = config.Integer("timeout_ms");
        Check(timeout >= 0, "negative timeout");
        const bool exporting = config.Boolean("export_input");
        Owners owners;
        auto sources = initial;
        Doc catalog(config.Token("sources"));
        const std::set<std::string> allowed = {
            "java",   "javac",  "jar",       "java_source", "contract",
            "parts",  "device", "intersite", "intrasite",   "input",
            "mapped", "packed", "placement"};
        for (const auto &row : catalog.Fields()) {
          Check(allowed.count(row.first), "unknown source role");
          Doc locator(row.second);
          locator.Keys({"path", "generation"});
          const auto ref = ArtifactRef::Bind(locator.Text("path"),
                                             "physical-input-document-v1");
          Check(locator.Token("generation") == Generation(ref),
                "source generation changed: " + row.first);
          owners.emplace(row.first, ref);
          sources.push_back(ref);
        }
        for (const auto &role :
             {"java", "javac", "jar", "java_source", "contract", "parts",
              "device", "intersite", "intrasite"})
          Check(owners.count(role), "missing source " + std::string(role));
        if (exporting) {
          Check(owners.count("mapped") && owners.count("packed") &&
                    owners.count("placement") && !owners.count("input"),
                "typed export sources incomplete");
          Check(owners.at("packed").path() == owners.at("placement").path(),
                "production requires one typed physical handoff");
        } else
          Check(owners.count("input"), "route input missing");
        const auto output = Destination(config.Text("output_path"), sources),
                   report =
                       Destination(fs::absolute(summary).string(), sources);
        Check(output != report, "route/report destinations alias");
        Lease lease(output.string() + ".owner.lock");
        auto pattern =
            (output.parent_path() / ".rwroute-generation.XXXXXX").string();
        Check(mkdtemp(pattern.data()), "cannot create route generation");
        const fs::path work(pattern);
        // Fresh classes/runtime per invocation avoid shared mutable Java
        // caches.
        const auto classes = work / "classes", home = work / "home",
                   runtime = home / "RapidWright";
        fs::create_directories(classes);
        fs::create_directories(runtime / "timing/ultrascaleplus");
        Doc contract(Doc::Read(owners.at("contract")));
        Check(contract.Text("schema") ==
                  "emuflow.rapidwright-timing-contract/v1",
              "timing contract version disagrees");
        Doc timing(contract.Token("timing")),
            md5(timing.Token("device_data_md5")),
            sha(timing.Token("source_data_sha256"));
        md5.Keys({"data/parts.db", "data/devices/virtexuplus/xcvu19p_db.dat"});
        sha.Keys({"intersite_delay_terms.txt", "intrasite_delay_terms.txt"});
        const auto revision = timing.Text("source_revision");
        Check(std::regex_match(revision, std::regex("[0-9a-f]{40}")),
              "invalid timing revision");
        for (const auto &row : std::vector<std::pair<std::string, std::string>>{
                 {"parts", "data/parts.db"},
                 {"device", "data/devices/virtexuplus/xcvu19p_db.dat"}}) {
          Check(Digest<Md5>(owners.at(row.first), flag) == md5.Text(row.second),
                "device data does not match pinned XCVU19P provider: " +
                    row.second);
          Check(fs::canonical(fs::path(config.Text("device_data_root")) /
                              row.second) == owners.at(row.first).path(),
                "provider data root disagrees with sealed device files");
        }
        fs::create_directory_symlink(
            fs::canonical(fs::path(config.Text("device_data_root")) / "data"),
            runtime / "data");
        for (const auto &row : std::vector<std::pair<std::string, std::string>>{
                 {"intersite", "intersite_delay_terms.txt"},
                 {"intrasite", "intrasite_delay_terms.txt"}}) {
          const auto &source = owners.at(row.first);
          Check(Digest<Sha256>(source, flag) == sha.Text(row.second),
                "timing data does not match pinned revision: " + row.second);
          const auto destination =
              runtime / "timing/ultrascaleplus" / row.second;
          fs::copy_file(source.path(), destination);
          auto copied = ArtifactRef::Bind(destination.string(),
                                          "physical-input-document-v1");
          Check(Digest<Sha256>(copied, flag) == sha.Text(row.second),
                "runtime timing copy changed");
          sources.push_back(copied);
        }
        Fields hashes;
        for (const auto &role : {"mapped", "packed", "placement"})
          if (owners.count(role))
            hashes[std::string(role) + "_sha256"] =
                Digest<Sha256>(owners.at(role), flag);
        const fs::path input = exporting ? work / "rwroute.tsv"
                                         : fs::path(owners.at("input").path());
        std::string export_report = "null";
        if (exporting) {
          Export(owners, hashes, config, input, work, sources);
          export_report = Read((work / "export.json").string()).text();
        }
        const auto input_ref =
            ArtifactRef::Bind(input.string(), "physical-input-document-v1");
        sources.push_back(input_ref);
        const bool reused =
            config.Boolean("resume") && fs::is_regular_file(output);
        std::string compilation = "null", routing = "null", validation;
        std::string log = "null";
        if (reused) {
          validation = Validate(
              owners,
              ArtifactRef::Bind(output.string(), "xilinx-route-manifest-v2"),
              input_ref, config, work, flag);
        } else {
          compilation = Execute(
              {owners.at("javac").path(), "-cp", owners.at("jar").path(), "-d",
               classes.string(), owners.at("java_source").path()},
              work, work / "javac.log", sources, flag, timeout);
          Check(fs::is_regular_file(classes / "EmuFlowRWRoute.class"),
                "Java compiler did not produce route adapter");
          unsigned class_count = 0;
          for (const auto &item : fs::recursive_directory_iterator(classes))
            if (item.is_regular_file()) {
              Check(++class_count <= 1024,
                    "Java class population exceeds bound");
              sources.push_back(ArtifactRef::Bind(
                  item.path().string(), "physical-input-document-v1"));
            }
          StagedArtifact pending(output.string());
          struct PayloadCleanup {
            std::string path;
            bool accepted = false;
            ~PayloadCleanup() {
              if (!accepted) {
                std::error_code e;
                for (const auto &suffix : {".nets.jsonl", ".pips.jsonl"})
                  fs::remove(path + suffix, e);
              }
            }
          } cleanup{pending.path()};
          routing = Execute(
              {owners.at("java").path(), "-Xmx" + heap,
               "-Duser.home=" + home.string(), "-cp",
               classes.string() + ":" + owners.at("jar").path(),
               "EmuFlowRWRoute", input.string(), pending.path(), revision,
               sha.Text("intersite_delay_terms.txt"),
               sha.Text("intrasite_delay_terms.txt"), md5.Text("data/parts.db"),
               md5.Text("data/devices/virtexuplus/xcvu19p_db.dat")},
              work, work / "rwroute.log", sources, flag, timeout,
              {{"RAPIDWRIGHT_PATH", runtime.string()}});
          auto candidate =
              ArtifactRef::Bind(pending.path(), "xilinx-route-manifest-v2");
          Doc candidate_manifest(Doc::Read(candidate));
          Check(candidate_manifest.Text("schema") ==
                    "emuflow.xilinx-route-db/v2",
                "production route manifest must use streaming schema");
          validation =
              Validate(owners, candidate, input_ref, config, work, flag);
          // The route manifest remains byte-identical and its unique sidecars
          // stay in the same directory. Only the accepted selector is
          // atomically moved.
          auto proof = Read((work / "validation.json").string());
          Doc stamps(proof.Token("source_generations"));
          auto final_sources = sources;
          final_sources.push_back(candidate);
          Doc manifest(Doc::Read(candidate));
          Doc payloads(manifest.Token("payloads"));
          for (const auto &field : {"nets", "pips"}) {
            Doc descriptor(payloads.Token(field));
            const auto payload = ArtifactRef::Bind(
                (output.parent_path() / descriptor.Text("path")).string(),
                "physical-input-document-v1");
            Check(stamps.Token(field) == Generation(payload),
                  "validated payload generation changed");
            final_sources.push_back(payload);
            sources.push_back(payload);
          }
          Stop(flag);
          PublishTemporaryFile(pending.path(), output.string(), final_sources);
          cleanup.accepted = true;
          auto fields = proof.Fields(), generations = stamps.Fields();
          generations["route"] = Generation(
              ArtifactRef::Bind(output.string(), "xilinx-route-manifest-v2"));
          fields["source_generations"] = Doc::Encode(generations);
          validation = Doc::Encode(fields);
          log = JsonQuote((work / "rwroute.log").string());
        }
        const auto route =
            ArtifactRef::Bind(output.string(), "xilinx-route-manifest-v2");
        Doc route_proof(validation);
        Check(Digest<Sha256>(route, nullptr) == route_proof.Text("route_sha256"),
              "accepted route manifest changed");
        Doc manifest(Doc::Read(route));
        Check(manifest.Text("schema") == "emuflow.xilinx-route-db/v2",
              "production route manifest must use streaming schema");
        auto result = route_proof.Fields();
        result["output"] = JsonQuote(output.string());
        result["log"] = log;
        if (reused)
          result["reused"] = "true";
        sources.push_back(route);
        Write(report,
              Doc::Encode(
                  {{"schema", JsonQuote("emuflow.rwroute-stage-report/v1")},
                   {"status", JsonQuote("pass")},
                   {"implementation_id", JsonQuote("native-rwroute-stage-v1")},
                   {"input", JsonQuote(input.string())},
                   {"export", export_report},
                   {"route", Doc::Encode(result)},
                   {"manifest", manifest.text()},
                   {"source_sha256", Doc::Encode([&] {
                      Fields out;
                      for (const auto &field : hashes)
                        out[field.first] = JsonQuote(field.second);
                      return out;
                    }())},
                   {"engine", Doc::Encode({{"compilation", compilation},
                                           {"routing", routing}})}}),
              sources);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "rwroute-stage-report-v1")};
      });
}
} // namespace emuflow::core
