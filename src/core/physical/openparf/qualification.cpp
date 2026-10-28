#include "physical/openparf/qualification.hpp"
#include "physical/openparf/input_records.hpp"
#include "physical/openparf/inputs.hpp"
#include "physical/openparf/placement_import.hpp"
#include "physical/openparf/runtime.hpp"
#include <algorithm>
#include <cctype>
#include <fcntl.h>
#include <sys/file.h>
namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using namespace openparf_input;
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
void Need(const StageResult &r, std::vector<ArtifactRef> &refs) {
  if (!r.ok())
    throw StageError(r.error, r.message);
  for (const auto &s : r.outputs) {
    s.RequireUnchanged();
    refs.push_back(s);
  }
}
struct Lease {
  int fd = -1;
  explicit Lease(const fs::path &file) {
    fd = open(file.c_str(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    Check(fd >= 0, "cannot open placement stage lease");
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      fd = -1;
      Check(false, "placement stage already has an active owner");
    }
  }
  ~Lease() {
    if (fd >= 0)
      close(fd);
  }
};
R PlanRegion(const ArtifactRef &mapped, const ArtifactRef &architecture,
             const std::string &part, const fs::path &path,
             const std::vector<ArtifactRef> &refs,
             const volatile std::sig_atomic_t *flag) {
  Scratch scratch(path.parent_path());
  SqliteDatabase db((scratch.root / "device.sqlite").string(),
                    SqliteDatabase::Mode::kCreateExclusive);
  PhysicalDevice device(db, architecture, nullptr, nullptr, flag);
  Check(!part.empty() && device.part() == part,
        "architecture part does not match BoardDB part");
  SqliteStore owner(mapped);
  auto &m = owner.database();
  std::map<std::string, I> atoms;
  auto kinds =
      m.Prepare("SELECT type,count(*) FROM cells GROUP BY type");
  I luts = 0, ffs = 0;
  while (kinds.Step()) {
    const auto kind = kinds.TypedText(0);
    const auto count = kinds.TypedInteger(1);
    atoms[kind] = count;
    if (Lut(kind) || kind == "LUT6_2")
      luts += count;
    if (Ff(kind))
      ffs += count;
  }
  std::map<std::string, I> demand{
      {"slice", std::max({(luts + 7) / 8, (ffs + 15) / 16, atoms["CARRY8"],
                          (atoms["MUXF7"] + 3) / 4, (atoms["MUXF8"] + 1) / 2,
                          atoms["MUXF9"]})},
      {"dsp", atoms["DSP48E2"]},
      {"bram", atoms["RAMB18E2"] + atoms["RAMB36E2"]},
      {"uram", atoms["URAM288"]}};
  std::map<std::string, I> capacity;
  std::set<std::string> with_capacity, with_rows;
  auto sites = db.Prepare("SELECT s.slr,s.type,t.y FROM pd_sites s JOIN "
                          "pd_site_tiles t ON t.name=s.name ORDER BY s.name");
  struct Rank {
    long double sum = 0;
    I count = 0;
  };
  std::map<std::string, Rank> ranks;
  while (sites.Step()) {
    Stop(flag);
    const auto slr = sites.TypedText(0);
    if (slr == "@unspecified-slr")
      continue;
    auto type = sites.TypedText(1);
    for (auto &c : type)
      c = std::toupper(static_cast<unsigned char>(c));
    std::string resource;
    if (type.rfind("SLICE", 0) == 0)
      resource = "slice";
    else if (type.rfind("DSP", 0) == 0)
      resource = "dsp";
    else if (type.rfind("RAMB", 0) == 0)
      resource = "bram";
    else if (type.rfind("URAM", 0) == 0)
      resource = "uram";
    if (!resource.empty()) {
      ++capacity[resource];
      with_capacity.insert(slr);
    }
    auto &rank = ranks[slr];
    rank.sum += sites.TypedInteger(2);
    ++rank.count;
    with_rows.insert(slr);
  }
  Check(!with_capacity.empty() && with_capacity == with_rows,
        "ArchitectureDB has no complete physical SLR inventory");
  for (const auto &r : demand)
    Check(r.second + (r.second + 1) / 2 <= capacity[r.first],
          "packed partition exceeds the complete Xilinx device capacity with "
          "1.5x placement headroom");
  std::vector<std::string> ordered(with_capacity.begin(), with_capacity.end());
  std::sort(ordered.begin(), ordered.end(), [&](const auto &a, const auto &b) {
    const auto x = ranks.at(a).sum / ranks.at(a).count,
               y = ranks.at(b).sum / ranks.at(b).count;
    return x == y ? a < b : x < y;
  });
  A slrs;
  for (const auto &s : ordered)
    slrs.push_back(s);
  R result = O{{"schema", "emuflow.xilinx-placement-constraints/v1"},
               {"policy", O{{"provider", "full-device-routability-v1"},
                            {"resource_headroom", 1.5}}},
               {"global", O{{"allowed_slrs", slrs}}},
               {"clusters", A{}}};
  device.RequireUnchanged();
  owner.RequireUnchanged();
  Json(path, result, refs);
  return slrs;
}
} // namespace
StageResult QualifyOpenparf(const ArtifactRef &request,
                            const std::string &output,
                            const volatile std::sig_atomic_t *flag) {
  std::optional<R> config;
  std::map<std::string, ArtifactRef> owners;
  std::vector<ArtifactRef> refs, published;
  return RunStage(
      "physical.qualify-openparf",
      [&] {
        Check(request.schema() == "openparf-qualification-request-v1",
              "placement stage request schema disagrees");
        config = Read(request);
        BoundedJson shape(config->encode());
        shape.Keys({"schema", "sources", "top", "allowed_slrs", "runtime",
                    "expected_part", "plan_region"});
        Check(config->at("schema").text() ==
                  "emuflow.openparf-qualification-request/v1",
              "placement stage request version disagrees");
        refs.push_back(request);
        const std::map<std::string, std::string> schemas{
            {"mapped", "xilinx-mapped-index-v2"},
            {"mapped_identity", "xilinx-mapped-index-v2"},
            {"packed", "physical-input-document-v1"},
            {"architecture", "physical-input-document-v1"},
            {"policy", "physical-input-document-v1"},
            {"native", "physical-input-document-v1"},
            {"provider", "physical-input-document-v1"},
            {"constraints", "physical-input-document-v1"},
            {"driver", "physical-input-document-v1"}};
        for (const auto &s : config->at("sources").array()) {
          const auto role = s.at("role").text();
          Check(schemas.count(role) &&
                    schemas.at(role) == s.at("schema").text(),
                "invalid placement stage source role/schema");
          auto ref =
              ArtifactRef::Bind(s.at("path").text(), s.at("schema").text());
          Check(Generation(ref).encode() == s.at("generation").encode(),
                "placement stage source generation changed");
          Check(owners.emplace(role, ref).second,
                "duplicate placement stage source role");
          refs.push_back(ref);
        }
        for (const auto *key : {"mapped", "mapped_identity", "packed",
                                "architecture", "policy", "driver"})
          Check(owners.count(key), "missing placement stage source");
        Check(owners.count("native") == owners.count("provider"),
              "placement requires both native constraints and provider");
        Check(owners.at("mapped").path() == owners.at("mapped_identity").path(),
              "placement requires a single mapped-index identity");
        return refs;
      },
      [&](const auto &) {
        auto &c = *config;
        const auto target = fs::weakly_canonical(fs::absolute(output));
        Check(!fs::is_symlink(fs::path(output)),
              "placement stage output is a symlink");
        fs::create_directories(target.parent_path());
        for (const auto &source : refs) {
          const auto relative =
              fs::path(source.path()).lexically_relative(target);
          Check(relative.empty() || *relative.begin() == "..",
                "placement source lies in its output directory");
          for (const auto &destination :
               {target / "qualification-report.json",
                target / "placement-import-report.json",
                target / "openparf.runtime.json",
                target / "openparf-result-checkpoint.json",
                target.parent_path() / "placement-region.json"}) {
            std::error_code error;
            Check(destination != source.path() &&
                      !fs::equivalent(destination, source.path(), error),
                  "placement output aliases source");
          }
        }
        Lease lease(target.string() + ".qualification.lock");
        Scratch control(target.parent_path());
        const auto plan = std::get_if<bool>(&c.at("plan_region").value);
        Check(plan, "invalid region planning flag");
        R allowed = c.at("allowed_slrs");
        std::optional<ArtifactRef> constraints;
        fs::path region;
        if (*plan) {
          Check(
              allowed.null() && !owners.count("constraints"),
              "automatic region planning cannot override explicit constraints");
          const auto draft = control.root / "placement-region.json";
          allowed = PlanRegion(owners.at("mapped"), owners.at("architecture"),
                               c.at("expected_part").text(), draft, refs, flag);
          const auto temporary =
              ArtifactRef::Bind(draft.string(), "physical-input-document-v1");
          const auto digest = FileHash(temporary, flag);
          fs::create_directories(target);
          region = target / ("placement-region." + digest + ".json");
          Check(!fs::is_symlink(region), "region output is a symlink");
          if (fs::exists(region))
            Check(FileHash(ArtifactRef::Bind(region.string(),
                                             "physical-input-document-v1"),
                           flag) == digest,
                  "region owner content changed");
          else
            Write(region, BoundedJson::Read(temporary), refs);
          constraints =
              ArtifactRef::Bind(region.string(), "physical-input-document-v1");
          refs.push_back(*constraints);
        } else {
          if (!c.at("expected_part").text().empty()) {
            SqliteDatabase db(":memory:", SqliteDatabase::Mode::kReadOnly);
            PhysicalDevice device(db, owners.at("architecture"), nullptr,
                                  nullptr, flag);
            Check(device.part() == c.at("expected_part").text(),
                  "architecture part does not match BoardDB part");
          }
          if (owners.count("constraints"))
            constraints = owners.at("constraints");
        }
        A sources;
        for (const auto &entry : owners)
          if (entry.first != "driver" && entry.first != "constraints")
            sources.push_back(Source(entry.first, entry.second));
        const auto preparation = control.root / "input.json";
        Json(preparation,
             O{{"schema", "emuflow.openparf-input-request/v1"},
               {"top", c.at("top")},
               {"allowed_slrs", allowed},
               {"sources", sources}},
             refs);
        Need(
            PrepareOpenparfInput(ArtifactRef::Bind(preparation.string(),
                                                   "openparf-input-request-v1"),
                                 target.string(), flag),
            refs);
        auto runtime = c.at("runtime");
        BoundedJson(runtime.encode())
            .Keys({"python", "installation", "environment"});
        A solver_sources;
        for (const auto &entry :
             std::vector<std::pair<std::string, ArtifactRef>>{
                 {"config",
                  ArtifactRef::Bind((target / "openparf.json").string(),
                                    "physical-input-document-v1")},
                 {"manifest",
                  ArtifactRef::Bind((target / "manifest.json").string(),
                                    "physical-input-document-v1")},
                 {"driver", owners.at("driver")}})
          solver_sources.push_back(Source(entry.first, entry.second));
        runtime["sources"] = solver_sources;
        runtime["schema"] = "emuflow.openparf-runtime-request/v1";
        runtime["mode"] = "run";
        runtime["allow_detailed_legalization_handoff"] = true;
        runtime["use_checkpoint"] = true;
        const auto solver_request = control.root / "runtime.json",
                   solver_report = target / "openparf.runtime.json";
        Json(solver_request, runtime, refs);
        Need(RunOpenparfStage(ArtifactRef::Bind(solver_request.string(),
                                                "openparf-runtime-request-v1"),
                              solver_report.string(), flag),
             refs);
        auto solver = Read(ArtifactRef::Bind(solver_report.string(),
                                             "openparf-runtime-report-v1"));
        const auto placement = ArtifactRef::Bind(solver.at("placement").text(),
                                                 "physical-input-document-v1");
        A importer;
        importer.push_back(Source("placement", placement));
        importer.push_back(
            Source("name_selector",
                   ArtifactRef::Bind((target / "name_map.json").string(),
                                     "openparf-name-map-selector-v1")));
        for (const auto &role :
             {"mapped", "architecture", "packed", "native", "provider"})
          if (owners.count(role)) {
            const auto &r = owners.at(role);
            importer.push_back(Source(
                role, ArtifactRef::Bind(
                          r.path(), std::string(role) == "packed"
                                        ? "openparf-atomic-source-selector-v1"
                                        : r.schema())));
          }
        if (constraints)
          importer.push_back(Source("constraints", *constraints));
        R metadata = O{{"runtime_validation", "native-openparf"},
                       {"native_convergence",
                        O{{"artifact", solver.at("metrics_artifact")},
                          {"metrics", solver.at("metrics")}}}};
        const auto import_request = control.root / "import.json",
                   import_report = target / "placement-import-report.json";
        Json(import_request,
             O{{"schema", "emuflow.openparf-placement-import-request/v1"},
               {"sources", importer},
               {"metadata", metadata},
               {"legalize_ff", true},
               {"top", c.at("top")}},
             refs);
        const auto imported = ImportOpenparfPlacement(
            ArtifactRef::Bind(import_request.string(),
                              "openparf-placement-import-request-v1"),
            import_report.string(), flag);
        Need(imported, refs);
        auto result = Read(ArtifactRef::Bind(
            import_report.string(), "openparf-placement-import-report-v1"));
        result["schema"] = "emuflow.openparf-qualification-report/v1";
        result["implementation_id"] = "native-openparf-qualification-v1";
        result["runtime"] = solver.at("runtime");
        result["manifest"] = Read(ArtifactRef::Bind(
            (target / "manifest.json").string(), "openparf-input-manifest-v1"));
        result["openparf_result"] = O{{"reused", solver.at("reused")},
                                      {"checkpoint", solver.at("checkpoint")}};
        result["native_import"] =
            O{{"implementation_id", "native-openparf-placement-import-v1"},
              {"metrics",
               R::Parse(
                   BoundedJson(StageResultJson(imported)).Token("metrics"))}};
        if (*plan) {
          result["region_path"] = region.string();
          result["allowed_slrs"] = allowed;
        }
        const auto report = target / "qualification-report.json";
        Json(report, result, refs);
        published.push_back(ArtifactRef::Bind(
            report.string(), "openparf-qualification-report-v1"));
        for (const auto &r : refs)
          if (r.path() != request.path() &&
              fs::path(r.path()).lexically_relative(target).string().rfind(
                  "..", 0) != 0)
            published.push_back(r);
      },
      [&] {
        for (const auto &r : refs)
          r.RequireUnchanged();
        for (const auto &r : published)
          r.RequireUnchanged();
        return published;
      });
}
} // namespace emuflow::core
