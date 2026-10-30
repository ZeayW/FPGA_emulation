#include "scheduling/schedule_view.hpp"
#include "common/bounded_json.hpp"
#include "scheduling/schedule_validation.hpp"
#include <filesystem>
namespace emuflow::core {
namespace {
void Check(bool valid, const std::string &message) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "schedule source: " + message);
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto q = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value END "
                      "FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step() && !q.IsNull(0), "metadata missing: " + key);
  auto s = q.TypedText(0);
  Check(!q.Step(), "duplicate metadata");
  return s;
}
} // namespace
BoundScheduleStore
OpenScheduleStore(const ArtifactRef &selector,
                  const volatile std::sig_atomic_t *cancelled) {
  using D = BoundedJson;
  namespace fs = std::filesystem;
  Check(selector.schema() == "tdm-schedule-selector-v4" &&
            !fs::is_symlink(selector.path()),
        "current selector required");
  D manifest(D::Read(selector));
  Check(manifest.Text("schema") == "emuflow.tdm-schedule-storage/v4" &&
            manifest.Text("logical_schema") == "emuflow.tdm-schedule/v1" &&
            manifest.Integer("application_id") == 0x454d5554 &&
            manifest.Integer("user_version") == 3,
        "selector envelope differs");
  auto hash = manifest.Text("store_sha256");
  Check(hash.size() == 64 &&
            hash.find_first_not_of("0123456789abcdef") == std::string::npos,
        "invalid seal");
  auto member = manifest.Text("artifact");
  const auto path = fs::path(selector.path()).parent_path() / member;
  Check(member == "schedule." + hash + ".sqlite" && !fs::is_symlink(path),
        "store escapes selector");
  auto store = ArtifactRef::Bind(path.string(), "tdm-schedule-store-v4");
  Check(manifest.Integer("store_bytes") >= 0 &&
            std::uint64_t(manifest.Integer("store_bytes")) ==
                store.generation().bytes &&
            Sha256File(store, cancelled) == hash,
        "content seal differs");
  SqliteStore data(store);
  auto logical = DecodeJsonString(Meta(data.database(), "logical_sha256"));
  Check(manifest.Text("logical_sha256") == logical, "logical source differs");
  AuditScheduleStoreSeal(data.database(), manifest.Token("sections"), logical,
                         manifest.Text("logical_pretty_sha256"));
  auto header = Meta(data.database(), "header");
  selector.RequireUnchanged();
  store.RequireUnchanged();
  return {selector, store, header, logical};
}
} // namespace emuflow::core
