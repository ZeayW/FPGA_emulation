#include "transport/chimew/pipeline.hpp"
#include "common/canonical_json.hpp"
#include "common/hash.hpp"
#include "scheduling/schedule_view.hpp"
#include "storage/store.hpp"
#include "transport/chimew/bank_store.hpp"
#include "transport/chimew/pin_artifacts.hpp"
#include "transport/chimew/qualification.hpp"
#include <algorithm>
#include <cerrno>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <set>
#include <sys/file.h>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using Refs = std::map<std::string, ArtifactRef>;
constexpr auto pipeline_schema = "emuflow.chimew-phase6-pipeline-report/v1";
constexpr auto generation_schema = "emuflow.chimew-pipeline-generation/v2";
constexpr auto adapter_schema = "emuflow.chimew-phase6-adapter-report/v3";
constexpr auto provider = "source-qualified-chimew-phase6-pipeline-v1";
constexpr auto bound_provider = "byte-bound-chimew-phase6-pipeline-v2";
constexpr auto pin_provider = "chimew-paper-plus-emuflow-electrical-slot-v1";
const std::set<std::string> source_labels{"routing", "placement", "netlist",
                                          "architecture", "package_pins"};
const std::set<std::string> base_labels{"schedule",
                                        "schedule_store",
                                        "platform",
                                        "crossings",
                                        "positions",
                                        "rudy_input",
                                        "bank_channel_input",
                                        "bank_channel_input_store",
                                        "electrical_map",
                                        "initial_grouping",
                                        "refined_grouping",
                                        "rudy_report",
                                        "bank_channel_report",
                                        "bank_channel_report_store",
                                        "qualification",
                                        "adapter_report",
                                        "adapter_bank_channel_report",
                                        "adapter_bank_channel_report_store",
                                        "adapter_electrical_binding",
                                        "adapter_electrical_binding_store",
                                        "adapter_pin_plan",
                                        "adapter_pin_plan_store",
                                        "adapter_position_hints",
                                        "adapter_position_hints_store",
                                        "adapter_qualification_certificate"};
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "Chimew pipeline: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "Chimew pipeline cancelled");
}
O Fields(const std::string &token) {
  O out;
  VisitJsonObjectTokens(token, [&](const auto &k, const auto &v) {
    Check(out.emplace(k, v).second, "duplicate control member");
  });
  return out;
}
D Read(const fs::path &path) {
  return D(
      D::Read(ArtifactRef::Bind(path.string(), "physical-input-document-v1")));
}
std::string Hash(const fs::path &path, const volatile std::sig_atomic_t *flag) {
  return Sha256File(
      ArtifactRef::Bind(path.string(), "physical-input-document-v1"), flag);
}
std::string Pretty(const O &fields) {
  std::string out;
  VisitPrettyCanonicalJson(
      CanonicalJsonToken(D::Encode(fields)), 0,
      [&](auto part) { out.append(part.data(), part.size()); });
  return out + '\n';
}
void Write(const fs::path &path, const O &fields,
           const std::vector<ArtifactRef> &inputs = {}) {
  PublishTextArtifact(path.string(), inputs,
                      [&](auto &out) { out << Pretty(fields); });
}
bool Within(const fs::path &path, const fs::path &root) {
  auto rel =
      fs::weakly_canonical(path).lexically_relative(fs::weakly_canonical(root));
  return !rel.empty() && *rel.begin() != "..";
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto pattern = (parent / ".chimew-pipeline-XXXXXX").string();
    Check(mkdtemp(pattern.data()), "cannot create private workspace");
    root = pattern;
  }
  ~Work() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
struct Lock {
  int fd = -1;
  Lock(const fs::path &root, bool exclusive,
       const volatile std::sig_atomic_t *flag) {
    fs::create_directories(root.parent_path());
    const auto path = root.parent_path() /
                      ("." + root.filename().string() + ".pipeline.lock");
    fd = open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open publication lock");
    while (flock(fd, exclusive ? LOCK_EX : LOCK_SH) != 0) {
      if (errno != EINTR) {
        close(fd);
        fd = -1;
        Check(false, "cannot acquire publication lock");
      }
      try {
        Stop(flag);
      } catch (...) {
        close(fd);
        fd = -1;
        throw;
      }
    }
  }
  ~Lock() {
    if (fd >= 0)
      close(fd);
  }
};
void Accepted(const StageResult &result) {
  if (!result.ok())
    throw StageError(result.error, result.message);
}
std::string Source(const ArtifactRef &ref) {
  return D::Encode(
      {{"path", JsonQuote(ref.path())}, {"schema", JsonQuote(ref.schema())}});
}
O SourceFields(const Refs &refs) {
  O out;
  for (const auto &p : refs)
    out[p.first] = Source(p.second);
  return out;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto q = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value END "
                      "FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step() && !q.IsNull(0), "missing or excessive metadata " + key);
  auto value = q.TypedText(0);
  Check(!q.Step(), "duplicate metadata");
  return value;
}
ArtifactRef Payload(const fs::path &selector, const std::string &schema) {
  auto doc = Read(selector);
  auto name = doc.Text("artifact");
  Check(!name.empty() && fs::path(name).filename() == name &&
            !fs::is_symlink(selector.parent_path() / name),
        "selector escapes owner");
  return ArtifactRef::Bind((selector.parent_path() / name).string(), schema);
}
O Header(const fs::path &selector, const std::string &schema) {
  auto ref = Payload(selector, schema);
  SqliteStore store(ref);
  return Fields(Meta(store.database(), "header"));
}
void CopyFile(const ArtifactRef &ref, const fs::path &target,
              const volatile std::sig_atomic_t *flag) {
  Check(!fs::exists(target) && !fs::is_symlink(target),
        "copy target already exists");
  ref.RequireUnchanged();
  fs::create_directories(target.parent_path());
  std::ifstream input(ref.path(), std::ios::binary);
  std::ofstream output(target, std::ios::binary);
  Check(bool(input) && bool(output), "cannot open pipeline copy");
  std::vector<char> buffer(1024 * 1024);
  while (input) {
    Stop(flag);
    input.read(buffer.data(), buffer.size());
    const auto count = input.gcount();
    if (count)
      output.write(buffer.data(), count);
    Check(bool(output), "cannot write pipeline copy");
  }
  Check(input.eof() && !input.bad(), "cannot read pipeline copy");
  output.close();
  Check(bool(output), "cannot close pipeline copy");
  ref.RequireUnchanged();
  Stop(flag);
}

std::set<std::string> Expected(bool bound, bool timing) {
  auto out = base_labels;
  if (bound) {
    for (const auto &s : source_labels)
      out.insert("source_" + s);
    if (timing)
      out.insert("source_timing_paths");
  }
  return out;
}
struct Facts {
  std::string design, platform, qualification, validation;
  O artifacts;
  std::map<std::string, fs::path> paths;
  std::set<fs::path> files;
  bool bound = false;
};
struct Context {
  const volatile std::sig_atomic_t *flag;
  std::vector<ArtifactRef> sources, original_sources;
  bool producer = false;
  Refs roles, assets;
  int regions = 31;
  bool source_bound = false;
  fs::path work;
  explicit Context(const volatile std::sig_atomic_t *flag) : flag(flag) {}
  void Copy(const ArtifactRef &ref, const fs::path &path) {
    CopyFile(ref, path, flag);
  }
  ArtifactRef Bind(const fs::path &path, const std::string &schema) {
    Check(!fs::is_symlink(path), "source is a symlink");
    auto ref = ArtifactRef::Bind(path.string(), schema);
    sources.push_back(ref);
    return ref;
  }
  void ReadRequest(const ArtifactRef &request, bool producer) {
    this->producer = producer;
    sources = {request};
    D doc(D::Read(request));
    doc.Keys(producer
                 ? std::set<std::string>{"schema", "sources", "region_count"}
                 : std::set<std::string>{"schema", "sources"});
    Check(request.schema() == (producer
                                   ? "chimew-pipeline-input-v1"
                                   : "chimew-pipeline-validation-input-v1"),
          "request source role differs");
    Check(doc.Text("schema") ==
              std::string(producer
                              ? "emuflow.chimew-pipeline-input/v1"
                              : "emuflow.chimew-pipeline-validation-input/v1"),
          "request schema differs");
    if (producer) {
      auto value = doc.Integer("region_count");
      Check(value >= 2 && value <= 31, "region count is outside [2,31]");
      regions = int(value);
    }
    const std::map<std::string, std::string> expected{
        {"schedule", "physical-input-document-v1"},
        {"board", "boarddb-v1"},
        {"crossings", "physical-input-document-v1"},
        {"positions", "physical-input-document-v1"},
        {"rudy_input", "physical-input-document-v1"},
        {"bank_input", "chimew-bank-input-selector-v1"},
        {"electrical", "physical-input-document-v1"},
        {"inventory", "hp-package-pin-inventory-v1"},
        {"wiring", "fixed-wiring-csv-v1"}};
    for (const auto &p : Fields(doc.Token("sources"))) {
      D row(p.second);
      row.Keys({"path", "schema"});
      bool external = p.first.rfind("source_", 0) == 0 &&
                      (source_labels.count(p.first.substr(7)) ||
                       p.first == "source_timing_paths");
      Check(producer ? (expected.count(p.first) || external)
                     : (p.first == "inventory" || p.first == "wiring"),
            "unknown input role");
      const auto schema =
          external ? "physical-input-document-v1" : expected.at(p.first);
      Check(row.Text("schema") == schema, "input role schema differs");
      roles.emplace(p.first, Bind(row.Text("path"), schema));
      if (p.first == "inventory" || p.first == "wiring")
        assets.emplace(p.first, roles.at(p.first));
    }
    if (producer) {
      for (const auto *name : {"schedule", "board", "crossings", "positions",
                               "rudy_input", "bank_input", "electrical"})
        Check(roles.count(name), "missing input role " + std::string(name));
      std::size_t count = 0;
      for (const auto &p : roles)
        count += p.first.rfind("source_", 0) == 0;
      source_bound = count != 0;
      if (source_bound) {
        for (const auto &label : source_labels)
          Check(roles.count("source_" + label), "source bundle is incomplete");
      }
      auto schedule = OpenScheduleStore(
          Bind(roles.at("schedule").path(), "tdm-schedule-selector-v4"), flag);
      sources.push_back(schedule.store);
      auto bank = InspectChimewBankInput(roles.at("bank_input"), flag);
      sources.insert(sources.end(), bank.inputs.begin(), bank.inputs.end());
    }
    original_sources = sources;
  }
  fs::path Request(const std::string &name, const std::string &schema,
                   const Refs &refs, const O &extra = {}) {
    O value{{"schema", JsonQuote(schema)},
            {"sources", D::Encode(SourceFields(refs))}};
    value.insert(extra.begin(), extra.end());
    auto path = work / (name + ".json");
    Write(path, value);
    return path;
  }
  O Binding(const std::map<std::string, fs::path> &paths) {
    O digests;
    for (const auto &p : paths)
      if (p.first.rfind("source_", 0) == 0)
        digests[p.first.substr(7)] = JsonQuote(Hash(p.second, flag));
    return {{"scope", JsonQuote("byte-bound-source-artifacts")},
            {"digests", D::Encode(digests)}};
  }
  fs::path Certify(const std::map<std::string, fs::path> &paths, bool bound,
                   const std::string &tag) {
    O refs;
    for (const auto *name :
         {"schedule", "crossings", "positions", "rudy_input",
          "initial_grouping", "refined_grouping", "rudy_report",
          "bank_channel_input", "bank_channel_report"}) {
      auto ref = ArtifactRef::Bind(paths.at(name).string(),
                                   std::string(name) == "bank_channel_input"
                                       ? "chimew-bank-input-selector-v1"
                                   : std::string(name) == "bank_channel_report"
                                       ? "chimew-bank-report-selector-v1"
                                       : "physical-input-document-v1");
      refs[name] = Source(ref);
    }
    O request{{"schema", JsonQuote("emuflow.chimew-qualification-input/v1")},
              {"artifacts", D::Encode(refs)}};
    if (bound)
      request["source_binding"] = D::Encode(Binding(paths));
    const auto path = work / (tag + "-request.json"),
               out = work / (tag + "-certificate.json");
    Write(path, request);
    Accepted(QualifyChimewChain(
        ArtifactRef::Bind(path.string(), "chimew-qualification-input-v1"),
        out.string(), flag));
    return out;
  }
  O PinValidation(const std::map<std::string, fs::path> &paths,
                  const std::string &tag) {
    Refs refs = assets;
    for (const auto &p :
         std::vector<std::tuple<std::string, std::string, std::string>>{
             {"board", "platform", "boarddb-v1"},
             {"schedule", "schedule", "physical-input-document-v1"},
             {"plan", "adapter_pin_plan", "pin-plan-selector-v1"},
             {"positions", "adapter_position_hints",
              "position-hints-selector-v1"},
             {"binding", "adapter_electrical_binding",
              "chimew-binding-selector-v1"}})
      refs.emplace(
          std::get<0>(p),
          ArtifactRef::Bind(paths.at(std::get<1>(p)).string(), std::get<2>(p)));
    auto request = Request(tag + "-pin-request",
                           "emuflow.chimew-pin-validation-input/v1", refs),
         out = work / (tag + "-pins.json");
    Accepted(ValidateChimewPinArtifacts(
        ArtifactRef::Bind(request.string(), "chimew-pin-validation-input-v1"),
        out.string(), flag));
    return Read(out).Fields();
  }
  Facts ValidateGeneration(const fs::path &root, const std::string &tag) {
    Stop(flag);
    Check(!fs::is_symlink(root) && fs::is_directory(root) &&
              root.filename().string().size() == 64,
          "generation name is invalid");
    const auto manifest_ref =
        Bind(root / "generation_manifest.json", "physical-input-document-v1");
    Check(Hash(manifest_ref.path(), flag) == root.filename(),
          "generation seal differs");
    D manifest(D::Read(manifest_ref));
    Check(manifest.Text("schema") == generation_schema,
          "generation manifest schema differs");
    Facts facts;
    facts.design = manifest.Text("design");
    facts.platform = manifest.Text("platform");
    auto chosen = manifest.Text("provider");
    Check(chosen == provider || chosen == bound_provider,
          "generation provider differs");
    facts.bound = chosen == bound_provider;
    facts.artifacts = Fields(manifest.Token("artifacts"));
    std::set<std::string> keys;
    std::set<fs::path> members;
    facts.files.insert(fs::canonical(root / "generation_manifest.json"));
    for (const auto &p : facts.artifacts) {
      keys.insert(p.first);
      D record(p.second);
      record.Keys({"path", "sha256"});
      fs::path relative(record.Text("path"));
      Check(!relative.empty() && !relative.is_absolute() && relative != "." &&
                std::find(relative.begin(), relative.end(), fs::path("..")) ==
                    relative.end(),
            "generation artifact path is unsafe");
      auto path = root / relative;
      Check(Within(path, root) &&
                members.insert(fs::weakly_canonical(path)).second,
            "generation artifacts alias");
      auto cursor = path;
      while (cursor != root) {
        Check(!fs::is_symlink(cursor),
              "generation artifact path contains a symlink");
        cursor = cursor.parent_path();
      }
      auto ref = Bind(path, "physical-input-document-v1");
      Check(Sha256File(ref, flag) == record.Text("sha256"),
            "generation artifact seal differs");
      facts.paths.emplace(p.first, path);
      facts.files.insert(fs::canonical(path));
    }
    Check(keys == Expected(facts.bound, keys.count("source_timing_paths")),
          "generation artifact inventory differs");
    std::set<fs::path> actual;
    for (const auto &p : fs::recursive_directory_iterator(root)) {
      Check(!p.is_symlink(), "generation contains symlinks");
      if (p.is_regular_file())
        actual.insert(fs::canonical(p.path()));
      else
        Check(p.is_directory(), "generation has a non-file member");
    }
    Check(actual == facts.files, "generation coverage is not exact");
    auto cert = Certify(facts.paths, facts.bound, tag);
    auto recomputed = Read(cert),
         published = Read(facts.paths.at("qualification"));
    Check(CanonicalJsonToken(D::Encode(recomputed.Fields())) ==
              CanonicalJsonToken(D::Encode(published.Fields())),
          "qualification certificate differs from independent reconstruction");
    facts.qualification = D::Encode(published.Fields());
    auto check = PinValidation(facts.paths, tag);
    auto bank = ValidateChimewBankAssignment(
        ArtifactRef::Bind(facts.paths.at("bank_channel_input").string(),
                          "chimew-bank-input-selector-v1"),
        ArtifactRef::Bind(
            facts.paths.at("adapter_bank_channel_report").string(),
            "chimew-bank-report-selector-v1"),
        {}, flag);
    Check(published.Text("design") == facts.design &&
              published.Text("platform") == facts.platform &&
              bank.design == facts.design && bank.platform == facts.platform,
          "generation namespace differs");
    auto expected_bank = ValidateChimewBankAssignment(
        ArtifactRef::Bind(facts.paths.at("bank_channel_input").string(),
                          "chimew-bank-input-selector-v1"),
        ArtifactRef::Bind(facts.paths.at("bank_channel_report").string(),
                          "chimew-bank-report-selector-v1"),
        {}, flag);
    Check(bank.report_sha256 == expected_bank.report_sha256,
          "adapter assignment differs from qualified assignment");
    Check(CanonicalJsonToken(D::Encode(
              Read(facts.paths.at("adapter_qualification_certificate"))
                  .Fields())) == CanonicalJsonToken(facts.qualification),
          "adapter qualification certificate differs");
    Refs electrical = assets;
    for (const auto &p :
         std::vector<std::tuple<std::string, std::string, std::string>>{
             {"board", "platform", "boarddb-v1"},
             {"bank_input", "bank_channel_input",
              "chimew-bank-input-selector-v1"},
             {"electrical", "electrical_map", "physical-input-document-v1"}})
      electrical.emplace(
          std::get<0>(p),
          ArtifactRef::Bind(facts.paths.at(std::get<1>(p)).string(),
                            std::get<2>(p)));
    auto ereq =
        Request(tag + "-electrical-request",
                "emuflow.chimew-electrical-validation-input/v1", electrical);
    Accepted(AuditChimewElectricalMap(
        ArtifactRef::Bind(ereq.string(),
                          "chimew-electrical-validation-input-v1"),
        (work / (tag + "-electrical.json")).string(), flag));
    if (facts.bound) {
      auto electrical_doc = Read(facts.paths.at("electrical_map"));
      D provenance(electrical_doc.Token("provenance"));
      Check(provenance.Text("package_pin_inventory_sha256") ==
                Hash(facts.paths.at("source_package_pins"), flag),
            "package_pins source provenance does not agree");
    }
    D adapter = Read(facts.paths.at("adapter_report"));
    Check(adapter.Text("schema") == adapter_schema,
          "adapter report schema differs");
    O names;
    std::set<fs::path> adapter_files{
        fs::canonical(facts.paths.at("adapter_report"))};
    for (const auto &p : facts.paths)
      if (p.first.rfind("adapter_", 0) == 0 && p.first != "adapter_report") {
        const auto name = p.second.filename().string();
        Check(name.find(Hash(p.second, flag)) != std::string::npos,
              "adapter artifact is not content-addressed");
        names[p.first.substr(8)] = JsonQuote(name);
        adapter_files.insert(fs::canonical(p.second));
      }
    auto adapter_name = facts.paths.at("adapter_report").filename().string();
    Check(adapter_name == "adapter_report." +
                              Hash(facts.paths.at("adapter_report"), flag) +
                              ".json",
          "adapter report generation differs");
    std::set<fs::path> actual_adapter;
    for (const auto &p : fs::directory_iterator(
             facts.paths.at("adapter_report").parent_path())) {
      Check(!p.is_symlink() && p.is_regular_file(),
            "invalid adapter generation member");
      actual_adapter.insert(fs::canonical(p.path()));
    }
    Check(actual_adapter == adapter_files,
          "adapter generation inventory differs");
    for (const auto &p :
         std::vector<std::tuple<std::string, std::string, std::string>>{
             {"adapter_pin_plan", "adapter_pin_plan_store",
              "pin-plan-store-v1"},
             {"adapter_position_hints", "adapter_position_hints_store",
              "position-hints-store-v1"},
             {"adapter_electrical_binding", "adapter_electrical_binding_store",
              "chimew-binding-store-v1"},
             {"adapter_bank_channel_report",
              "adapter_bank_channel_report_store",
              "chimew-bank-report-store-v1"},
             {"schedule", "schedule_store", "tdm-schedule-store-v4"},
             {"bank_channel_input", "bank_channel_input_store",
              "chimew-bank-input-store-v1"},
             {"bank_channel_report", "bank_channel_report_store",
              "chimew-bank-report-store-v1"}})
      Check(Payload(facts.paths.at(std::get<0>(p)), std::get<2>(p)).path() ==
                fs::canonical(facts.paths.at(std::get<1>(p))),
            "selector/store pairing differs");
    auto binding = Header(facts.paths.at("adapter_electrical_binding"),
                          "chimew-binding-store-v1");
    D q(facts.qualification);
    D metrics(q.Token("metrics"));
    O qualification_validation{
        {"status", JsonQuote("pass")},
        {"signals", metrics.Token("signals")},
        {"groups", metrics.Token("groups")},
        {"qualification_sha256", q.Token("qualification_sha256")},
        {"qualification_scope",
         JsonQuote(facts.bound ? "byte-bound-source-artifacts"
                               : "declared-digest-artifact-chain")}};
    O expected_adapter{
        {"schema", JsonQuote(adapter_schema)},
        {"status", JsonQuote("pass")},
        {"provider", JsonQuote(pin_provider)},
        {"design", JsonQuote(facts.design)},
        {"platform", JsonQuote(facts.platform)},
        {"paper_provider",
         JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
        {"validation", check.at("pin_validation")},
        {"electrical_metrics", binding.at("metrics")},
        {"lookahead_qualification", JsonQuote("complete-artifact-chain")},
        {"qualification_validation", D::Encode(qualification_validation)},
        {"artifacts", D::Encode(names)}};
    Check(CanonicalJsonToken(D::Encode(adapter.Fields())) ==
              CanonicalJsonToken(D::Encode(expected_adapter)),
          "pipeline adapter report does not agree");
    facts.validation = D::Encode(
        {{"status", JsonQuote("pass")},
         {"provider", JsonQuote(chosen)},
         {"qualification_scope",
          JsonQuote(facts.bound ? "byte-bound-source-artifacts"
                                : "declared-digest-artifact-chain")},
         {"signals", metrics.Token("signals")},
         {"groups", metrics.Token("groups")},
         {"qualification_sha256", q.Token("qualification_sha256")},
         {"pin_plan", check.at("pin_validation")},
         {"electrical_binding", check.at("binding_validation")},
         {"artifact_hashes_verified", std::to_string(facts.paths.size() + 1)}});
    return facts;
  }
  O OuterReport(const Facts &facts, const fs::path &generation) {
    O artifacts = facts.artifacts;
    for (auto &p : artifacts) {
      auto record = Fields(p.second);
      record["path"] =
          JsonQuote((fs::path("generations") / generation.filename() /
                     DecodeJsonString(record.at("path")))
                        .generic_string());
      p.second = D::Encode(record);
    }
    artifacts["generation_manifest"] = D::Encode(
        {{"path", JsonQuote((fs::path("generations") / generation.filename() /
                             "generation_manifest.json")
                                .generic_string())},
         {"sha256",
          JsonQuote(Hash(generation / "generation_manifest.json", flag))}});
    D cert(facts.qualification), metrics(cert.Token("metrics"));
    return {{"schema", JsonQuote(pipeline_schema)},
            {"status", JsonQuote("pass")},
            {"implementation_id", JsonQuote("native-chimew-pipeline-v1")},
            {"design", JsonQuote(facts.design)},
            {"platform", JsonQuote(facts.platform)},
            {"provider", JsonQuote(facts.bound ? bound_provider : provider)},
            {"qualification_scope",
             JsonQuote(facts.bound ? "byte-bound-source-artifacts"
                                   : "declared-digest-artifact-chain")},
            {"qualification_sha256", cert.Token("qualification_sha256")},
            {"metrics", D::Encode({{"signals", metrics.Token("signals")},
                                   {"groups", metrics.Token("groups")},
                                   {"rudy_peak_utilization",
                                    metrics.Token("rudy_peak_utilization")},
                                   {"rudy_overloaded_bins", "0"},
                                   {"artifact_chain_disagreements", "0"}})},
            {"artifacts", D::Encode(artifacts)}};
  }
  Facts ValidateReport(const fs::path &root, const std::string &tag) {
    const auto ref =
        Bind(root / "pipeline_report.json", "chimew-pipeline-report-v1");
    D report(D::Read(ref));
    Check(report.Text("schema") == pipeline_schema &&
              report.Text("status") == "pass",
          "pipeline report identity differs");
    auto records = Fields(report.Token("artifacts"));
    Check(records.count("generation_manifest"),
          "pipeline report has no generation manifest");
    D record(records.at("generation_manifest"));
    fs::path relative(record.Text("path"));
    Check(relative.parent_path().parent_path() == "generations" &&
              relative.filename() == "generation_manifest.json",
          "generation manifest path differs");
    auto generation = root / relative.parent_path();
    auto facts = ValidateGeneration(generation, tag);
    O expected = OuterReport(facts, generation), actual = report.Fields();
    expected.erase("implementation_id");
    actual.erase("implementation_id");
    Check(CanonicalJsonToken(D::Encode(expected)) ==
              CanonicalJsonToken(D::Encode(actual)),
          "pipeline summary or committed artifact inventory does not agree");
    std::set<fs::path> allowed = facts.files;
    allowed.insert(fs::canonical(ref.path()));
    if (fs::is_directory(root / "generations"))
      for (const auto &p : fs::directory_iterator(root / "generations"))
        if (p.path() != generation && p.is_directory() && !p.is_symlink()) {
          try {
            auto orphan = ValidateGeneration(p.path(), tag + "-orphan");
            allowed.insert(orphan.files.begin(), orphan.files.end());
          } catch (const StageError &) {
          }
        }
    std::set<fs::path> actual_files;
    for (const auto &p : fs::recursive_directory_iterator(root)) {
      Check(!p.is_symlink(), "pipeline contains a symlink");
      if (p.is_regular_file())
        actual_files.insert(fs::canonical(p.path()));
      else
        Check(p.is_directory(), "pipeline contains a non-file member");
    }
    Check(actual_files == allowed, "pipeline artifact coverage is not exact");
    return facts;
  }
  void Build(const fs::path &root) {
    Refs kernel;
    for (const auto *name :
         {"schedule", "crossings", "positions", "rudy_input"})
      kernel.emplace(name, roles.at(name));
    auto req =
        Request("kernels", "emuflow.chimew-kernel-reports-input/v1", kernel);
    Accepted(BuildChimewKernelReports(
        ArtifactRef::Bind(req.string(), "chimew-kernel-reports-input-v1"),
        (work / "kernels").string(), flag));
    auto kernel_summary = Read(work / "kernels/kernel_reports.json");
    const auto payload = work / "payload";
    fs::create_directories(payload / "inputs");
    fs::create_directory(payload / "kernels");
    std::map<std::string, fs::path> paths;
    for (const auto &p : std::vector<std::pair<std::string, std::string>>{
             {"schedule", "schedule"},
             {"platform", "board"},
             {"crossings", "crossings"},
             {"positions", "positions"},
             {"rudy_input", "rudy_input"},
             {"bank_channel_input", "bank_input"},
             {"electrical_map", "electrical"}}) {
      auto destination = payload / "inputs" / (p.first + ".json");
      Copy(roles.at(p.second), destination);
      paths[p.first] = destination;
      if (p.first == "schedule" || p.first == "bank_channel_input") {
        auto data =
            Payload(roles.at(p.second).path(),
                    p.first == "schedule" ? "tdm-schedule-store-v4"
                                          : "chimew-bank-input-store-v1");
        auto data_name = fs::path(data.path()).filename().string();
        if (p.first == "bank_channel_input") {
          auto manifest = Read(roles.at(p.second).path());
          data_name = destination.stem().string() + "." +
                      manifest.Text("store_sha256") + ".sqlite";
          auto fields = manifest.Fields();
          fields["artifact"] = JsonQuote(data_name);
          Write(destination, fields);
        }
        Copy(data, destination.parent_path() / data_name);
        paths[p.first == "schedule" ? "schedule_store"
                                    : "bank_channel_input_store"] =
            destination.parent_path() / data_name;
      }
    }
    if (source_bound) {
      O digests = Fields(kernel_summary.Token("source_digests"));
      auto electrical = Read(paths.at("electrical_map"));
      digests["package_pins"] = D(electrical.Token("provenance"))
                                    .Token("package_pin_inventory_sha256");
      for (const auto &p : roles)
        if (p.first.rfind("source_", 0) == 0) {
          auto label = p.first.substr(7);
          auto digest = Sha256File(p.second, flag);
          Check(label == "timing_paths" ||
                    DecodeJsonString(digests.at(label)) == digest,
                label + " source does not match declared provenance");
          auto destination = payload / "sources" / (label + ".source");
          Copy(p.second, destination);
          paths[p.first] = destination;
        }
    }
    for (const auto *name :
         {"initial_grouping", "refined_grouping", "rudy_report"}) {
      auto destination = payload / "kernels" / (std::string(name) + ".json");
      Copy(ArtifactRef::Bind(
               (work / "kernels" / (std::string(name) + ".json")).string(),
               "physical-input-document-v1"),
           destination);
      paths[name] = destination;
    }
    auto bank_out = payload / "kernels/bank_channel_report.json";
    Accepted(
        AssignChimewBankStore(roles.at("bank_input"), bank_out.string(), flag));
    paths["bank_channel_report"] = bank_out;
    paths["bank_channel_report_store"] =
        Payload(bank_out, "chimew-bank-report-store-v1").path();
    auto certificate = Certify(paths, source_bound, "producer");
    Copy(ArtifactRef::Bind(certificate.string(), "chimew-qualification-v1"),
         payload / "kernels/qualification.json");
    paths["qualification"] = payload / "kernels/qualification.json";
    Refs pins = assets;
    for (const auto &p :
         std::vector<std::tuple<std::string, std::string, std::string>>{
             {"board", "platform", "boarddb-v1"},
             {"schedule", "schedule", "physical-input-document-v1"},
             {"bank_input", "bank_channel_input",
              "chimew-bank-input-selector-v1"},
             {"electrical", "electrical_map", "physical-input-document-v1"},
             {"qualification", "qualification", "chimew-qualification-v1"},
             {"bank_report", "bank_channel_report",
              "chimew-bank-report-selector-v1"}})
      pins.emplace(
          std::get<0>(p),
          ArtifactRef::Bind(paths.at(std::get<1>(p)).string(), std::get<2>(p)));
    auto pin_req =
        Request("pin-input", "emuflow.chimew-pin-artifacts-input/v1", pins,
                {{"region_count", std::to_string(regions)},
                 {"verify_board_bytes", "true"}});
    const auto pin_root = work / "pin-artifacts";
    Accepted(BuildChimewPinArtifacts(
        ArtifactRef::Bind(pin_req.string(), "chimew-pin-artifacts-input-v1"),
        pin_root.string(), flag));
    const auto adapter = payload / "phase6-adapter";
    fs::create_directory(adapter);
    O names;
    for (const auto *name : {"bank_channel_report", "position_hints",
                             "pin_plan", "electrical_binding"}) {
      const auto input = pin_root / (std::string(name) + ".json");
      auto header = Read(input);
      auto data = pin_root / header.Text("artifact");
      Copy(ArtifactRef::Bind(data.string(), "physical-input-document-v1"),
           adapter / data.filename());
      names[std::string(name) + "_store"] = JsonQuote(data.filename().string());
      paths["adapter_" + std::string(name) + "_store"] =
          adapter / data.filename();
      const auto filename =
          std::string(name) + "." + Hash(input, flag) + ".json";
      Copy(ArtifactRef::Bind(input.string(), "physical-input-document-v1"),
           adapter / filename);
      names[name] = JsonQuote(filename);
      paths["adapter_" + std::string(name)] = adapter / filename;
    }
    const auto certificate_name =
        "qualification_certificate." + Hash(certificate, flag) + ".json";
    Copy(ArtifactRef::Bind(certificate.string(), "chimew-qualification-v1"),
         adapter / certificate_name);
    names["qualification_certificate"] = JsonQuote(certificate_name);
    paths["adapter_qualification_certificate"] = adapter / certificate_name;
    auto summary = Read(pin_root / "pin_artifacts.json");
    auto binding = Header(paths.at("adapter_electrical_binding"),
                          "chimew-binding-store-v1");
    O adapter_report{
        {"schema", JsonQuote(adapter_schema)},
        {"status", JsonQuote("pass")},
        {"provider", JsonQuote(pin_provider)},
        {"design", summary.Token("design")},
        {"platform", summary.Token("platform")},
        {"paper_provider",
         JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
        {"validation", summary.Token("validation")},
        {"electrical_metrics", binding.at("metrics")},
        {"lookahead_qualification", JsonQuote("complete-artifact-chain")},
        {"qualification_validation", summary.Token("qualification_validation")},
        {"artifacts", D::Encode(names)}};
    auto report_path = work / "adapter_report.json";
    Write(report_path, adapter_report);
    const auto adapter_name =
        "adapter_report." + Hash(report_path, flag) + ".json";
    Copy(ArtifactRef::Bind(report_path.string(), "physical-input-document-v1"),
         adapter / adapter_name);
    paths["adapter_report"] = adapter / adapter_name;
    O artifacts;
    for (const auto &p : paths)
      artifacts[p.first] = D::Encode(
          {{"path",
            JsonQuote(p.second.lexically_relative(payload).generic_string())},
           {"sha256", JsonQuote(Hash(p.second, flag))}});
    Write(payload / "generation_manifest.json",
          {{"schema", JsonQuote(generation_schema)},
           {"design", summary.Token("design")},
           {"platform", summary.Token("platform")},
           {"provider", JsonQuote(source_bound ? bound_provider : provider)},
           {"artifacts", D::Encode(artifacts)}});
    const auto identity = Hash(payload / "generation_manifest.json", flag);
    const auto staged = work / identity;
    fs::rename(payload, staged);
    auto facts = ValidateGeneration(staged, "precommit");
    for (const auto &r : sources)
      r.RequireUnchanged();
    fs::create_directories(root / "generations");
    const auto final = root / "generations" / identity;
    if (fs::exists(final)) {
      auto prior = ValidateGeneration(final, "collision");
      Check(Hash(final / "generation_manifest.json", flag) == identity,
            "generation collision");
    } else
      PublishNewDirectory(staged.string(), final.string(), OriginalSources());
    auto report = OuterReport(facts, final);
    // Only original inputs survive the private candidate relocation. Snapshot
    // validation sources have already been checked before its atomic rename.
    for (const auto &r : roles)
      r.second.RequireUnchanged();
    StagedArtifact staged_report((root / "pipeline_report.json").string());
    Write(staged_report.path(), report);
    PublishArtifactGroup(
        {{staged_report.path(), root / "pipeline_report.json"}},
        OriginalSources(), [&] {
          auto published = Read(root / "pipeline_report.json");
          Check(CanonicalJsonToken(D::Encode(published.Fields())) ==
                    CanonicalJsonToken(D::Encode(report)),
                "published report differs");
          ValidateGeneration(final, "accepted");
        });
    try {
      Work retired(root.parent_path());
      for (const auto &p : fs::directory_iterator(root / "generations"))
        if (p.path() != final && p.is_directory() && !p.is_symlink()) {
          try {
            ValidateGeneration(p.path(), "retire");
          } catch (const StageError &) {
            continue;
          }
          std::error_code ignored;
          fs::rename(p.path(), retired.root / p.path().filename(), ignored);
        }
    } catch (const StageError &) {
    } catch (const fs::filesystem_error &) {
    }
  }
  std::vector<ArtifactRef> OriginalSources() const {
    if (producer)
      return original_sources;
    std::vector<ArtifactRef> out;
    for (const auto &ref : sources)
      if (!Within(ref.path(), work))
        out.push_back(ref);
    return out;
  }
};
} // namespace
StageResult RunChimewPipeline(const ArtifactRef &request,
                              const std::string &directory,
                              const volatile std::sig_atomic_t *cancelled) {
  std::optional<Work> work;
  std::unique_ptr<Lock> lock;
  std::unique_ptr<Context> context;
  const auto root = fs::absolute(directory).lexically_normal();
  return RunStage(
      "phase6.run-chimew-pipeline",
      [&] {
        Check(!fs::is_symlink(root), "output directory is a symlink");
        fs::create_directories(root.parent_path());
        lock = std::make_unique<Lock>(root, true, cancelled);
        work.emplace(root.parent_path());
        context = std::make_unique<Context>(cancelled);
        context->work = work->root;
        context->ReadRequest(request, true);
        for (const auto &ref : context->sources)
          Check(!Within(ref.path(), root),
                "producer input belongs to the output namespace");
        if (fs::exists(root / "pipeline_report.json"))
          context->ValidateReport(root, "previous");
        return context->OriginalSources();
      },
      [&](const auto &) { context->Build(root); },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind((root / "pipeline_report.json").string(),
                              "chimew-pipeline-report-v1")};
      });
}
StageResult AuditChimewPipeline(const ArtifactRef &request,
                                const std::string &directory,
                                const std::string &summary,
                                const volatile std::sig_atomic_t *cancelled) {
  std::optional<Work> work;
  std::unique_ptr<Lock> lock;
  std::unique_ptr<Context> context;
  std::optional<Facts> facts;
  const auto root = fs::absolute(directory).lexically_normal();
  return RunStage(
      "phase6.audit-chimew-pipeline",
      [&] {
        Check(!Within(summary, root),
              "audit summary overlaps the pipeline namespace");
        Check(fs::weakly_canonical(summary) !=
                  fs::weakly_canonical(
                      root.parent_path() /
                      ("." + root.filename().string() + ".pipeline.lock")),
              "audit summary aliases the publication lock");
        lock = std::make_unique<Lock>(root, false, cancelled);
        work.emplace(root.parent_path());
        context = std::make_unique<Context>(cancelled);
        context->work = work->root;
        context->ReadRequest(request, false);
        facts = context->ValidateReport(root, "audit");
        return context->OriginalSources();
      },
      [&](const auto &refs) {
        auto fields = Fields(facts->validation);
        fields["schema"] = JsonQuote("emuflow.chimew-pipeline-validation/v1");
        Write(summary, fields, refs);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "chimew-pipeline-validation-v1")};
      });
}
} // namespace emuflow::core
