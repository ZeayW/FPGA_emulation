#include "physical/evidence_catalog.hpp"

#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "storage/store.hpp"

#include <array>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;

void Check(bool value, const std::string &message) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical evidence catalog: " + message);
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
std::string HashFile(const ArtifactRef &source) {
  std::ifstream input(source.path(), std::ios::binary);
  Check(bool(input), "cannot read sealed artifact");
  Sha256 hash;
  std::array<char, 65536> bytes{};
  while (input) {
    input.read(bytes.data(), bytes.size());
    hash.update(reinterpret_cast<const unsigned char *>(bytes.data()),
                input.gcount());
  }
  Check(input.eof(), "cannot finish artifact seal");
  source.RequireUnchanged();
  return Hex(hash.finish());
}
bool Digest(const std::string &value) {
  return value.size() == 64 &&
         value.find_first_not_of("0123456789abcdef") == std::string::npos;
}
struct Kind {
  const char *field;
  const char *logical;
  const char *selector;
  const char *store;
  bool platform;
};
constexpr Kind kinds[] = {
    {"boundary_timing", "emuflow.boundary-timing/v1",
     "emuflow.boundary-timing-storage/v1", "boundary-timing-store-v1", true},
    {"boundary_identities", "emuflow.boundary-identity/v1",
     "emuflow.boundary-identity-storage/v1", "boundary-identity-store-v1",
     true},
    {"logic_segment_timing", "emuflow.logic-segment-timing/v1",
     "emuflow.logic-segment-timing-storage/v1", "logic-segment-timing-store-v1",
     true},
    {"local_path_timing", "emuflow.local-path-timing/v1",
     "emuflow.local-path-timing-storage/v1", "local-path-timing-store-v1",
     false},
};
fs::path SafeChild(const fs::path &root, const std::string &relative,
                   const char *owner) {
  const fs::path path(relative);
  Check(!path.empty() && !path.is_absolute(),
        std::string(owner) + " path is not relative");
  for (const auto &item : path)
    Check(item != "..", std::string(owner) + " path escapes its owner");
  const auto normalized = (root / path).lexically_normal(),
             base = root.lexically_normal();
  const auto rel = normalized.lexically_relative(base);
  Check(!rel.empty() && *rel.begin() != "..",
        std::string(owner) + " path escapes its owner");
  const auto canonical =
      fs::weakly_canonical(normalized).lexically_relative(fs::canonical(base));
  Check(!canonical.empty() && *canonical.begin() != "..",
        std::string(owner) + " resolved path escapes its owner");
  Check(!fs::is_symlink(normalized) && fs::is_regular_file(normalized),
        std::string(owner) + " is unavailable");
  return normalized;
}
std::vector<PhysicalEvidenceOwner> ResolveGroup(BoundedJson &summary,
                                                const ArtifactRef &summary_ref,
                                                const Kind &kind,
                                                const std::string &design,
                                                const std::string &platform) {
  Check(summary.Has(kind.field), std::string("missing ") + kind.field);
  const auto group =
      BoundedJson(summary.Token(kind.field), 4 * 1024 * 1024).Fields();
  Check(!group.empty() && group.size() <= 256,
        std::string(kind.field) + " population is invalid");
  std::vector<PhysicalEvidenceOwner> result;
  result.reserve(group.size());
  const auto root = fs::path(summary_ref.path()).parent_path();
  for (const auto &item : group) {
    Check(!item.first.empty(),
          std::string(kind.field) + " has empty FPGA identity");
    BoundedJson reference(item.second, 64 * 1024);
    reference.Keys({"schema", "logical_schema", "path", "bytes", "sha256"});
    Check(reference.Text("schema") ==
                  "emuflow.physical-evidence-reference/v2" &&
              reference.Text("logical_schema") == kind.logical,
          std::string(kind.field) + " reference schema disagrees");
    const auto selector_path =
        SafeChild(root, reference.Text("path"), "physical selector");
    auto selector_ref = ArtifactRef::Bind(
        selector_path.string(),
        std::string(kind.store).substr(0, std::string(kind.store).size() - 8) +
            "selector-v1");
    Check(reference.Integer("bytes") >= 0 &&
              std::uint64_t(reference.Integer("bytes")) ==
                  selector_ref.generation().bytes &&
              Digest(reference.Text("sha256")) &&
              HashFile(selector_ref) == reference.Text("sha256"),
          "physical selector seal disagrees");
    BoundedJson selector(BoundedJson::Read(selector_ref));
    std::set<std::string> keys = {
        "schema",      "logical_schema", "application_id", "user_version",
        "header",      "counts",         "logical_sha256", "artifact",
        "store_bytes", "store_sha256"};
    if (std::string(kind.store) == "logic-segment-timing-store-v1")
      keys.insert("source");
    selector.Keys(keys);
    const auto &schema = FindSchema(kind.store);
    Check(selector.Integer("application_id") == std::int64_t(schema.magic) &&
              selector.Integer("user_version") == schema.version,
          std::string(kind.field) + " selector storage version disagrees");
    Check(selector.Text("schema") == kind.selector &&
              selector.Text("logical_schema") == kind.logical,
          std::string(kind.field) + " selector schema disagrees");
    BoundedJson header(selector.Token("header"));
    Check(header.Text("status") == "pass" && header.Text("design") == design &&
              header.Text("fpga") == item.first,
          std::string(kind.field) + " selector namespace disagrees");
    if (kind.platform)
      Check(header.Text("platform") == platform,
            std::string(kind.field) + " platform disagrees");
    if (std::string(kind.store) == "logic-segment-timing-store-v1") {
      Check(summary.Text("logic_segment_system_routes_sha256") ==
                    header.Text("system_routes_sha256") &&
                summary.Text("logic_segment_tdm_schedule_sha256") ==
                    header.Text("tdm_schedule_sha256"),
            "summary and logic source seals disagree");
      const auto semantic = header.Has("semantic_contract_sha256")
                                ? header.Token("semantic_contract_sha256")
                                : "null";
      const auto expected =
          summary.Has("logic_segment_semantic_contract_sha256")
              ? summary.Token("logic_segment_semantic_contract_sha256")
              : "null";
      Check(semantic == expected, "summary and logic semantic seals disagree");
    }
    const auto artifact = selector.Text("artifact");
    Check(fs::path(artifact).filename() == artifact && artifact != "." &&
              artifact != "..",
          std::string(kind.field) + " payload name is unsafe");
    const auto store_path =
        SafeChild(selector_path.parent_path(), artifact, "physical store");
    auto store_ref = ArtifactRef::Bind(store_path.string(), kind.store);
    Check(selector.Integer("store_bytes") >= 0 &&
              std::uint64_t(selector.Integer("store_bytes")) ==
                  store_ref.generation().bytes &&
              Digest(selector.Text("store_sha256")) &&
              HashFile(store_ref) == selector.Text("store_sha256"),
          std::string(kind.field) + " store seal disagrees");
    SqliteStore store(store_ref);
    auto metadata = store.database().Prepare(
        "SELECT value FROM metadata WHERE key='header'");
    Check(metadata.Step(),
          std::string(kind.field) + " store header is missing");
    RequireEquivalentJson(
        store.database(), selector.Token("header"), metadata.TypedText(0),
        std::string(kind.field) + " selector/store header disagrees");
    auto counts = store.database().Prepare(
        "SELECT value FROM metadata WHERE key='counts'");
    Check(counts.Step(), std::string(kind.field) + " store counts are missing");
    RequireEquivalentJson(
        store.database(), selector.Token("counts"), counts.TypedText(0),
        std::string(kind.field) + " selector/store counts disagree");
    if (selector.Has("source")) {
      auto source = store.database().Prepare(
          "SELECT value FROM metadata WHERE key='source'");
      Check(source.Step(),
            std::string(kind.field) + " source metadata is missing");
      RequireEquivalentJson(
          store.database(), selector.Token("source"), source.TypedText(0),
          std::string(kind.field) + " selector/store source disagrees");
    }
    auto logical = store.database().Prepare(
        "SELECT value FROM metadata WHERE key='logical_sha256'");
    Check(logical.Step() && DecodeJsonString(logical.TypedText(0)) ==
                                selector.Text("logical_sha256"),
          std::string(kind.field) + " logical seal disagrees");
    result.push_back(
        {item.first, std::move(selector_ref), std::move(store_ref)});
  }
  return result;
}
std::set<std::string> Names(const std::vector<PhysicalEvidenceOwner> &values) {
  std::set<std::string> result;
  for (const auto &item : values)
    Check(result.insert(item.fpga).second, "FPGA evidence repeats");
  return result;
}
} // namespace

PhysicalEvidenceCatalog
ResolvePhysicalEvidenceCatalog(const ArtifactRef &summary_ref) {
  Check(summary_ref.schema() == "physical-summary-v1",
        "summary artifact schema disagrees");
  BoundedJson summary(BoundedJson::Read(summary_ref));
  Check(summary.Text("schema") == "emuflow.phase7b-physical-summary/v1" &&
            summary.Text("status") == "pass",
        "physical summary did not pass");
  PhysicalEvidenceCatalog result;
  result.design = summary.Text("design");
  result.platform = summary.Text("platform");
  result.provider = summary.Text("provider");
  result.qualification = summary.Text("qualification");
  if (summary.Has("static_exact_clock_uncertainty_ns")) {
    result.clock_uncertainty_ns =
        summary.Number("static_exact_clock_uncertainty_ns");
    Check(result.clock_uncertainty_ns >= 0, "clock uncertainty is negative");
  }
  result.boundary_timing = ResolveGroup(summary, summary_ref, kinds[0],
                                        result.design, result.platform);
  result.boundary_identities = ResolveGroup(summary, summary_ref, kinds[1],
                                            result.design, result.platform);
  result.logic_segment_timing = ResolveGroup(summary, summary_ref, kinds[2],
                                             result.design, result.platform);
  result.local_path_timing = ResolveGroup(summary, summary_ref, kinds[3],
                                          result.design, result.platform);
  const auto boundary = Names(result.boundary_timing);
  Check(boundary == Names(result.boundary_identities) &&
            boundary == Names(result.logic_segment_timing),
        "boundary evidence FPGA coverage disagrees");
  Check(boundary == Names(result.local_path_timing),
        "local and boundary FPGA coverage disagrees");
  summary_ref.RequireUnchanged();
  return result;
}

} // namespace emuflow::core
