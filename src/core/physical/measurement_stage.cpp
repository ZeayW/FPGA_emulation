#include "physical/measurement_stage.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "physical/physical_evidence.hpp"
#include "storage/store.hpp"
#include <filesystem>
#include <fstream>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Doc = BoundedJson;
using Raw = std::map<std::string, std::string>;
void Check(bool yes, const std::string &why) {
  if (!yes)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical measurement stage: " + why);
}
std::string Hash(const ArtifactRef &source) {
  std::ifstream in(source.path(), std::ios::binary);
  Check(bool(in), "cannot read source");
  Sha256 sha;
  std::array<char, 65536> block{};
  while (in) {
    in.read(block.data(), block.size());
    sha.update(reinterpret_cast<unsigned char *>(block.data()), in.gcount());
  }
  Check(in.eof(), "cannot finish source seal");
  source.RequireUnchanged();
  static constexpr char digits[] = "0123456789abcdef";
  std::string out;
  for (auto n : sha.finish()) {
    out += digits[n >> 4];
    out += digits[n & 15];
  }
  return out;
}
std::string Meta(SqliteDatabase &db, const char *key) {
  auto q = db.Prepare("SELECT value FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step(), "missing store metadata");
  auto value = q.TypedText(0);
  Check(value.size() <= 8 * 1024 * 1024 && !q.Step(), "invalid store metadata");
  return value;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto pattern = (parent / ".measurement-stage-XXXXXX").string();
    Check(mkdtemp(pattern.data()), "cannot create scratch");
    root = pattern;
  }
  ~Workspace() {
    std::error_code e;
    fs::remove_all(root, e);
  }
};
const char *IdentityStore(unsigned kind) {
  Check(kind >= 2 && kind <= 4, "invalid evidence kind");
  return kind == 2   ? "boundary-identity-store-v1"
         : kind == 3 ? "logic-segment-identity-store-v1"
                     : "local-path-timing-store-v1";
}
} // namespace
ArtifactRef BindPhysicalIdentity(const ArtifactRef &selector, unsigned kind,
                                 std::vector<ArtifactRef> &sources) {
  Doc doc(Doc::Read(selector));
  std::set<std::string> keys = {
      "schema",      "logical_schema", "application_id", "user_version",
      "header",      "counts",         "logical_sha256", "artifact",
      "store_bytes", "store_sha256"};
  if (kind == 3)
    keys.insert("source");
  doc.Keys(keys);
  Check(doc.Integer("user_version") == 1 &&
            doc.Integer("application_id") == (kind == 2   ? 0x454d5542
                                              : kind == 3 ? 0x454d5549
                                                          : 0x454d554c),
        "identity selector version/application disagrees");
  Check(doc.Text("logical_schema") ==
            (kind == 2   ? "emuflow.boundary-identity/v1"
             : kind == 3 ? "emuflow.logic-segment-identity/v1"
                         : "emuflow.local-path-timing/v1"),
        "identity logical schema disagrees");
  const auto name = doc.Text("artifact");
  Check(fs::path(name).filename() == name && name != "." && name != "..",
        "invalid selector payload path");
  const auto path = fs::path(selector.path()).parent_path() / name;
  Check(!fs::is_symlink(path), "identity payload is a symbolic link");
  auto store = ArtifactRef::Bind(path.string(), IdentityStore(kind));
  const char *schema = kind == 2   ? "emuflow.boundary-identity-storage/v1"
                       : kind == 3 ? "emuflow.logic-segment-identity-storage/v1"
                                   : "emuflow.local-path-timing-storage/v1";
  for (const auto &bound : sources)
    if (bound.path() == store.path() && bound.schema() == store.schema()) {
      selector.RequireUnchanged();
      bound.RequireUnchanged();
      return bound;
    }
  Check(doc.Text("schema") == schema && doc.Integer("store_bytes") >= 0 &&
            std::uint64_t(doc.Integer("store_bytes")) ==
                store.generation().bytes &&
            doc.Text("store_sha256") == Hash(store),
        "identity payload seal disagrees");
  SqliteStore owner(store);
  auto &db = owner.database();
  (void)AuditPhysicalEvidenceStore(db, kind == 2 ? 1 : kind == 3 ? 5 : 4);
  for (const auto *field : {"header", "counts"})
    RequireEquivalentJson(db, doc.Token(field), Meta(db, field),
                          "identity selector metadata disagrees");
  Check(doc.Text("logical_sha256") ==
            DecodeJsonString(Meta(db, "logical_sha256")),
        "identity logical seal disagrees");
  if (kind == 3)
    RequireEquivalentJson(db, doc.Token("source"), Meta(db, "source"),
                          "identity source metadata disagrees");
  selector.RequireUnchanged();
  store.RequireUnchanged();
  sources.push_back(selector);
  sources.push_back(store);
  return store;
}
std::string ImportVivadoMeasurement(const ArtifactRef &identity_selector,
                                    const ArtifactRef &measurements,
                                    unsigned kind, const std::string &output,
                                    bool board_interface,
                                    std::vector<ArtifactRef> &sources) {
  const fs::path target(output);
  Check(target.is_absolute() && !fs::exists(target) && !fs::is_symlink(target),
        "output must be fresh");
  auto identity = BindPhysicalIdentity(identity_selector, kind, sources);
  sources.push_back(measurements);
  SqliteStore store(identity);
  auto &db = store.database();
  Doc origin(Meta(db, "header"));
  Raw header;
  const auto copy = [&](const char *key) { header[key] = origin.Token(key); };
  for (const auto *key : {"design", "fpga"})
    copy(key);
  if (kind != 4)
    copy("platform");
  const auto name = kind == 2   ? "boundary"
                    : kind == 3 ? "logic-segment"
                                : "local-path";
  header["schema"] = JsonQuote("emuflow." + std::string(name) + "-timing/v1");
  header["status"] = JsonQuote("pass");
  std::string source = "null";
  if (kind == 2) {
    header["provider"] = JsonQuote("vivado-indexed-routed-boundary-v1");
    header["qualification"] = JsonQuote("routed-vendor-device-boundary");
    header["measurement_scope"] = JsonQuote(
        board_interface ? "board-integrated-interface" : "partition-boundary");
    header["coverage"] = "{\"endpoints\":0,\"tx\":0,\"rx\":0}";
  } else if (kind == 3) {
    for (const auto *key :
         {"coverage", "system_routes_sha256", "tdm_schedule_sha256"})
      copy(key);
    if (origin.Has("semantic_contract_sha256"))
      copy("semantic_contract_sha256");
    header["provider"] = JsonQuote("vivado-routed-logic-segment-datapath-v1");
    header["qualification"] = JsonQuote("routed-vendor-device-endpoint-chain");
    source = Meta(db, "source");
  } else {
    for (const auto *key : {"source", "coverage", "identity_schema"})
      copy(key);
    header["provider"] =
        JsonQuote("vivado-source-bound-local-endpoint-delay-v1");
    header["qualification"] =
        JsonQuote("routed-vendor-device-local-endpoint-longest-path");
  }
  Workspace work(target.parent_path());
  const auto request = work.root / "request.bin",
             payload = work.root / "timing.sqlite",
             summary = work.root / "summary.json";
  CheckedRequestWriter writer(request.string(), sources);
  writer.Put<std::uint64_t>(0x314d4950464d5545ULL);
  writer.Put<std::uint32_t>(1);
  writer.Put<std::uint32_t>(kind);
  writer.Put<std::uint32_t>(0);
  writer.Generation(identity.generation());
  writer.Generation(measurements.generation());
  writer.Text(Doc::Encode(header));
  writer.Text(source);
  writer.Finish();
  auto request_ref = ArtifactRef::Bind(
      request.string(), "physical-measurement-import-request-v1");
  ImportPhysicalMeasurements({identity, measurements, request_ref}, kind,
                             payload.string(), summary.string());
  Doc result(
      Doc::Read(ArtifactRef::Bind(summary.string(), "exact-sha256-text-v1")));
  Check(result.Text("status") == "pass" && result.Integer("kind") == kind &&
            result.Integer("missing_segments") == 0,
        "import summary disagrees");
  return PublishPhysicalProjection(payload.string(), output, kind,
                                   result.text(), sources);
}
std::string PublishPhysicalProjection(const std::string &payload,
                                      const std::string &output, unsigned kind,
                                      const std::string &summary,
                                      std::vector<ArtifactRef> &sources) {
  Check(kind >= 2 && kind <= 4, "invalid projection kind");
  const fs::path target(output);
  Check(target.is_absolute() && !fs::exists(target) && !fs::is_symlink(target),
        "projection output must be fresh");
  Doc result(summary);
  Check(result.Integer("kind") == kind &&
            result.Integer("missing_segments") == 0,
        "projection summary disagrees");
  Doc header_doc(DecodeJsonString(result.Token("header_json")));
  const auto header = header_doc.Fields();
  const auto name = kind == 2   ? "boundary"
                    : kind == 3 ? "logic-segment"
                                : "local-path";
  auto staged =
      ArtifactRef::Bind(payload, kind == 2   ? "boundary-timing-store-v1"
                                 : kind == 3 ? "logic-segment-timing-store-v1"
                                             : "local-path-timing-store-v1");
  const auto hash = Hash(staged);
  const auto published = target.parent_path() / (target.filename().string() +
                                                 ".store." + hash + ".sqlite");
  Check(!fs::exists(published) && !fs::is_symlink(published),
        "payload destination already exists");
  Raw selector{{"schema", JsonQuote("emuflow." + std::string(name) +
                                    "-timing-storage/v1")},
               {"logical_schema",
                JsonQuote("emuflow." + std::string(name) + "-timing/v1")},
               {"application_id", std::to_string(kind == 2   ? 0x454d5554
                                                 : kind == 3 ? 0x454d5547
                                                             : 0x454d554c)},
               {"user_version", "1"},
               {"header", DecodeJsonString(result.Token("header_json"))},
               {"counts", DecodeJsonString(result.Token("counts_json"))},
               {"logical_sha256", result.Token("logical_sha256")},
               {"artifact", JsonQuote(published.filename().string())},
               {"store_bytes", std::to_string(staged.generation().bytes)},
               {"store_sha256", JsonQuote(hash)}};
  if (kind == 3)
    selector["source"] = DecodeJsonString(result.Token("source_json"));
  StagedArtifact publication(published.string(), {".sqlite"});
  const auto sibling = publication.path() + ".sqlite";
  fs::rename(payload, sibling);
  PublishTemporaryFile(sibling, published.string(), sources);
  try {
    PublishTextArtifact(target.string(), sources, [&](std::ostream &out) {
      out << Doc::Encode(selector) << '\n';
    });
  } catch (...) {
    std::error_code e;
    fs::remove(published, e);
    throw;
  }
  sources.push_back(ArtifactRef::Bind(
      published.string(), kind == 2   ? "boundary-timing-store-v1"
                          : kind == 3 ? "logic-segment-timing-store-v1"
                                      : "local-path-timing-store-v1"));
  sources.push_back(ArtifactRef::Bind(
      target.string(), kind == 2   ? "boundary-timing-selector-v1"
                       : kind == 3 ? "logic-segment-timing-selector-v1"
                                   : "local-path-timing-selector-v1"));
  Raw validation{{"status", JsonQuote("pass")},
                 {"fpga", header.at("fpga")},
                 {"maximum_delay_ns", result.Token("maximum_delay_ns")},
                 {"output", JsonQuote(target.string())}};
  Doc counts(selector.at("counts"));
  if (kind == 2)
    for (const auto &item : counts.Fields())
      validation[item.first] = item.second;
  else if (kind == 3) {
    for (const auto *key :
         {"segments", "endpoint_exact_segments", "cone_bound_segments",
          "endpoint_collection_bound_segments"})
      validation[key] = counts.Has(key) ? counts.Token(key) : "0";
    validation["missing_segments"] = "0";
  } else {
    validation["local_paths"] = counts.Token("local_paths");
    validation["measurement_counts"] =
        DecodeJsonString(result.Token("measurement_counts_json"));
  }
  return Doc::Encode(validation);
}
} // namespace emuflow::core
