#include "board/model.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "ir/emuir_view.hpp"
#include "ir/resource_view.hpp"
#include "partition/phase3.hpp"
#include "routing/policy.hpp"
#include <algorithm>
#include <filesystem>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
namespace fs = std::filesystem;
void Check(bool value, const std::string &why) {
  if (!value)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Phase 3 configuration: " + why);
}
std::string Quote(const std::string &v) {
  Check(v.find('\0') == std::string::npos, "NUL in identity");
  return JsonQuote(v);
}
std::string Number(double v) { return JsonRecord(v).Encode(); }
std::string Array(const std::vector<std::string> &tokens) {
  std::string out = "[";
  for (const auto &v : tokens) {
    if (out.size() > 1)
      out += ',';
    out += v;
  }
  return out + "]";
}
std::vector<std::string> Names(const std::string &token) {
  std::vector<std::string> out;
  for (const auto &t : D::Array(token, 256))
    out.push_back(DecodeJsonString(t));
  return out;
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Binding(const ArtifactRef &ref, const fs::path &path = {}) {
  return D::Encode({{"path", Quote(path.empty() ? ref.path() : path.string())},
                    {"schema", Quote(ref.schema())},
                    {"generation", Generation(ref)}});
}
std::string Canonical(const std::string &token) {
  Check(!token.empty(), "empty JSON token");
  if (token[0] == '{') {
    O fields;
    for (const auto &f : D(token).Fields())
      fields[f.first] = Canonical(f.second);
    return D::Encode(fields);
  }
  if (token[0] == '[') {
    std::vector<std::string> rows;
    for (const auto &v : D::Array(token, 65536))
      rows.push_back(Canonical(v));
    return Array(rows);
  }
  if (token[0] == '"')
    return Quote(DecodeJsonString(token));
  return token;
}
std::string HashBoard(const std::string &token) {
  Sha256 hash;
  const auto feed = [&](std::string_view s) {
    hash.update(reinterpret_cast<const unsigned char *>(s.data()), s.size());
  };
  VisitPrettyCanonicalJson(Canonical(token), 0, feed);
  feed("\n");
  std::string out;
  for (auto c : hash.finish()) {
    out += "0123456789abcdef"[c >> 4];
    out += "0123456789abcdef"[c & 15];
  }
  return out;
}
struct Work {
  fs::path root;
  explicit Work(const fs::path &parent) {
    auto name = (parent / ".partition-configuration-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create configuration workspace");
    root = name;
  }
  ~Work() {
    std::error_code e;
    fs::remove_all(root, e);
  }
};
struct Inputs {
  ArtifactRef request;
  std::vector<ArtifactRef> sources;
  std::map<std::string, ArtifactRef> roles;
  std::string board;
  O options;
  explicit Inputs(const ArtifactRef &r) : request(r) {}
  void Read(const std::map<std::string, ArtifactRef> &compiled) {
    Check(request.schema() == "phase3-configuration-request-v1",
          "request schema disagrees");
    D doc(D::Read(request));
    doc.Keys({"schema", "sources", "board", "options"});
    Check(doc.Text("schema") == "emuflow.phase3-configuration-request/v1",
          "request version disagrees");
    board = doc.Token("board");
    D b(board);
    Check(b.Text("schema") == "emuflow.boarddb/v1",
          "normalized board version disagrees");
    D config(doc.Token("options"));
    config.Keys({"pattern_range_policy", "min_used_fpgas", "balance_tolerance",
                 "provider", "seed", "openroad", "timeout_seconds",
                 "seed_attempts", "num_initial_solutions",
                 "num_best_initial_solutions", "repair_min_used",
                 "unweighted_baseline", "max_moves",
                 "max_cross_fpga_dependency_depth",
                 "candidate_selection_policy", "timing_driven"});
    options = config.Fields();
    Check(config.Text("provider") == "patron" ||
              config.Text("provider") == "tritonpart",
          "unsupported provider");
    Check(config.Text("candidate_selection_policy") ==
              "assignment-derived-acyclic-v2",
          "unsupported candidate policy");
    Check(config.Text("pattern_range_policy") == "empty-ranges-v1" ||
              config.Text("pattern_range_policy") == "strict-ranges-v1",
          "unknown pattern grammar");
    for (const auto *key :
         {"seed", "timeout_seconds", "seed_attempts", "num_initial_solutions",
          "num_best_initial_solutions", "max_cross_fpga_dependency_depth"})
      Check(config.Integer(key) >= 0, "negative partition effort");
    for (const auto *key : {"seed_attempts", "num_initial_solutions",
                            "num_best_initial_solutions", "timeout_seconds"})
      Check(config.Integer(key) > 0, "empty initializer effort");
    for (const auto *key :
         {"repair_min_used", "unweighted_baseline", "timing_driven"})
      (void)config.Boolean(key);
    (void)config.Text("openroad");
    if (config.Token("min_used_fpgas") != "null")
      Check(config.Integer("min_used_fpgas") > 0,
            "invalid minimum target count");
    if (config.Token("balance_tolerance") != "null")
      Check(config.Number("balance_tolerance") >= 0,
            "invalid balance tolerance");
    if (config.Token("max_moves") != "null")
      Check(config.Integer("max_moves") >= 0, "invalid move bound");
    sources = {request};
    const std::map<std::string, std::string> schemas = {
        {"source", "emuir-compact-v2"},
        {"board", "boarddb-v1"},
        {"facts", "phase3-configuration-source-v1"},
        {"native", "external-tool-executable-v1"},
        {"constraints", "partition-constraints-input-v1"},
        {"route_constraints", "phase3-configuration-source-v1"},
        {"board_link_timing", "phase3-configuration-source-v1"},
        {"timing_selector", "sta-path-selector-v1"},
        {"timing", "timing-path-store-v1"},
        {"weight_selector", "net-weight-selector-v2"},
        {"weights", "net-weight-store-v1"},
        {"solution", "tritonpart-solution-v1"}};
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    for (const auto &token : D::Array(doc.Token("sources"), 16)) {
      D row(token);
      row.Keys({"role", "path", "schema", "generation"});
      const auto role = row.Text("role");
      Check(schemas.count(role) && row.Text("schema") == schemas.at(role) &&
                fs::path(row.Text("path")).is_absolute(),
            "unknown/relative source binding");
      auto ref = ArtifactRef::Bind(row.Text("path"), row.Text("schema"));
      RequireEquivalentJson(compare, row.Token("generation"), Generation(ref),
                            "Phase 3 configuration source generation changed");
      Check(roles.emplace(role, ref).second, "duplicate source role");
      sources.push_back(ref);
    }
    for (const auto &item : compiled) {
      Check((item.first == "source" || item.first == "timing_selector" ||
             item.first == "timing" || item.first == "weight_selector" ||
             item.first == "weights") &&
                item.second.schema() == schemas.at(item.first),
            "unknown compiled source role");
      Check(roles.emplace(item).second, "compiled source was supplied by host");
      sources.push_back(item.second);
    }
    for (const auto *role : {"source", "board", "native", "facts"})
      Check(roles.count(role), std::string("missing source ") + role);
    Check(roles.count("timing") == roles.count("timing_selector") &&
              roles.count("weights") == roles.count("weight_selector"),
          "unpaired timing source");
    const auto normalized = NormalizeBoard(D::Read(roles.at("board")));
    RequireEquivalentJson(compare, board, normalized,
                          "normalized board contract differs from its source");
    board = normalized;
  }
};
void Candidate(const fs::path &output, Inputs &in, D &facts) {
  EmuirView ir(in.roles.at("source"));
  auto fields = Names(facts.Token("resource_fields"));
  Check(fields.size() == kResourceFields.size(),
        "resource catalog size disagrees");
  for (std::size_t i = 0; i < fields.size(); ++i)
    Check(fields[i] == kResourceFields[i], "resource catalog order disagrees");
  AtomicBinaryWriter w(output.string(), in.sources);
  const auto put = [&](const auto &value) { w.Write(value); };
  const auto text = [&](const std::string &value) {
    Check(!value.empty() && value.size() <= UINT32_MAX,
          "invalid policy identity");
    put(std::uint32_t(value.size()));
    w.WriteBytes(value.data(), value.size());
  };
  const auto catalog = [&](const std::vector<std::string> &values) {
    Check(!values.empty() && values.size() <= 256 &&
              std::set<std::string>(values.begin(), values.end()).size() ==
                  values.size(),
          "invalid policy catalog");
    put(std::uint32_t(values.size()));
    for (const auto &value : values)
      text(value);
  };
  const auto &g = in.roles.at("source").generation();
  put(std::uint64_t(0x31514543464d5545ULL));
  put(std::uint32_t(1));
  put(g.device);
  put(g.inode);
  put(g.bytes);
  put(g.modified_seconds);
  put(g.modified_nanoseconds);
  put(g.changed_seconds);
  put(g.changed_nanoseconds);
  put(ir.counts().cells);
  put(ir.counts().nets);
  catalog(fields);
  const auto mask = [&](const std::string &token) {
    std::uint64_t value = 0;
    for (const auto &name : Names(token)) {
      auto p = std::find(fields.begin(), fields.end(), name);
      Check(p != fields.end(), "unknown policy resource");
      value |= std::uint64_t(1) << (p - fields.begin());
    }
    return value;
  };
  put(mask(facts.Token("state_resources")));
  put(mask(facts.Token("hard_combinational_resources")));
  put(std::uint32_t(std::find(fields.begin(), fields.end(), "lut") -
                    fields.begin()));
  text(facts.Text("generic_ff_prefix"));
  catalog(Names(facts.Token("generic_ff_inputs")));
  const auto sequential = D(facts.Token("sequential_inputs")).Fields();
  put(std::uint32_t(sequential.size()));
  for (const auto &f : sequential) {
    text(f.first);
    auto ports = Names(f.second);
    std::sort(ports.begin(), ports.end());
    catalog(ports);
  }
  auto cuts = Names(facts.Token("valid_cut_classes"));
  std::sort(cuts.begin(), cuts.end());
  catalog(cuts);
  w.Commit();
}
struct LinkTiming {
  std::string database = "null", report = "null", constraints = "null";
};
LinkTiming ProjectTiming(Inputs &in) {
  LinkTiming out;
  out.constraints = in.roles.count("route_constraints")
                        ? D::Read(in.roles.at("route_constraints"))
                        : "null";
  if (!in.roles.count("board_link_timing"))
    return out;
  out.database = D::Read(in.roles.at("board_link_timing"));
  D db(out.database), board(in.board);
  const auto name = D(board.Token("platform")).Text("name");
  Check(db.Text("schema") == "emuflow.board-link-timing/v1" &&
            db.Text("status") == "pass" && db.Text("platform") == name &&
            db.Text("measurement_scope") ==
                "tx-transport-stage-to-rx-transport-stage",
        "board link timing identity disagrees");
  using Key = std::tuple<std::string, std::string, std::string>;
  std::map<Key, std::string> expected;
  for (const auto &token : D::Array(board.Token("links"))) {
    D link(token);
    auto ends = Names(link.Token("endpoints"));
    Check(ends.size() == 2, "invalid board link endpoints");
    expected.emplace(Key{link.Text("id"), ends[0], ends[1]}, token);
    if (link.Text("direction") != "unidirectional")
      expected.emplace(Key{link.Text("id"), ends[1], ends[0]}, token);
  }
  std::map<Key, double> actual;
  I model_only = 0, characterized = 0, measured = 0;
  double maximum = 0;
  SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
  for (const auto &token : D::Array(db.Token("links"))) {
    D row(token);
    const Key key{row.Text("link"), row.Text("from"), row.Text("to")};
    Check(expected.count(key), "unknown directed timing arc");
    D link(expected.at(key));
    const auto delay = row.Number("delay_bound_ns");
    Check(delay >= 0 &&
              std::abs(row.Number("fabric_clock_mhz") -
                       link.Number("fabric_clock_mhz")) <= 1e-9 &&
              row.Integer("latency_cycles") == link.Integer("latency_cycles"),
          "link timing frequency/latency/bound disagrees");
    const auto qualification = row.Text("qualification");
    D source(row.Token("source"));
    const auto kind = source.Text("kind");
    Check(!source.Text("reference").empty(), "empty timing reference");
    if (qualification == "model-only") {
      ++model_only;
      Check(kind == "boarddb-model", "model timing source disagrees");
    } else if (qualification == "characterized-upper-bound") {
      ++characterized;
      Check(kind == "vendor-characterization",
            "characterized timing source disagrees");
    } else {
      Check(qualification == "measured-upper-bound" &&
                kind == "hardware-measurement" &&
                source.Integer("observations") > 0,
            "measured timing source disagrees");
      ++measured;
    }
    if (link.Has("tdm_delay_model")) {
      D ratio(link.Token("tdm_delay_model"));
      RequireEquivalentJson(compare, row.Token("tdm_delay_model"), ratio.text(),
                            "board link TDM delay model changed");
      const auto reference =
          ratio.Number("intercept_ns") +
          ratio.Number("ratio_coefficient_ns") * ratio.Integer("min_ratio");
      Check(row.Integer("reference_tdm_ratio") == ratio.Integer("min_ratio") &&
                std::abs(delay - reference) <=
                    std::max(1e-9, 1e-9 * std::max(std::abs(delay),
                                                   std::abs(reference))) &&
                qualification == "model-only",
            "TDM timing reference disagrees");
    } else
      Check(!row.Has("tdm_delay_model"), "unexpected TDM delay model");
    Check(actual.emplace(key, delay).second, "duplicate directed timing arc");
    maximum = std::max(maximum, delay);
  }
  Check(actual.size() == expected.size(),
        "board link timing coverage disagrees");
  const bool final = !actual.empty() && measured == I(actual.size());
  Check(db.Boolean("final_link_timing_signoff") == final,
        "board link timing signoff claim disagrees");
  std::map<std::string, std::map<std::string, O>> projections;
  for (const auto &a : actual)
    projections[std::get<0>(a.first)][std::get<1>(a.first)]
               [std::get<2>(a.first)] = Number(a.second);
  O directed;
  for (const auto &l : projections) {
    O sources;
    for (const auto &s : l.second)
      sources[s.first] = D::Encode(s.second);
    directed[l.first] = D::Encode(sources);
  }
  O constraints =
      out.constraints == "null"
          ? O{{"schema", Quote("emuflow.system-route-constraints/v1")}}
          : D(out.constraints).Fields();
  constraints["directed_link_delay_ns"] = D::Encode(directed);
  out.constraints = D::Encode(constraints);
  const auto validation =
      D::Encode({{"status", Quote("pass")},
                 {"platform", Quote(name)},
                 {"directed_links", std::to_string(actual.size())},
                 {"maximum_delay_bound_ns", Number(maximum)},
                 {"model_only_links", std::to_string(model_only)},
                 {"characterized_links", std::to_string(characterized)},
                 {"measured_links", std::to_string(measured)},
                 {"final_link_timing_signoff", final ? "true" : "false"}});
  const auto projection =
      D::Encode({{"status", Quote("pass")},
                 {"projection", Quote("direction-exact-link-upper-bounds")},
                 {"directed_links", std::to_string(actual.size())},
                 {"maximum_route_link_delay_ns", Number(maximum)}});
  const auto applied =
      in.options.at("timing_driven") == "true"
          ? std::vector<
                std::string>{Quote("phase4-system-routing"),
                             Quote("phase5-tdm-ratio-and-schedule-timing"),
                             Quote("phase7c-system-timing-when-physical")}
          : std::vector<std::string>{
                Quote("phase4-post-route-timing-evaluation"),
                Quote("phase5-baseline-schedule-timing-evaluation"),
                Quote("phase7c-system-timing-when-physical")};
  out.report =
      D::Encode({{"status", Quote("pass")},
                 {"validation", validation},
                 {"routing_projection", projection},
                 {"optimization_enabled", in.options.at("timing_driven")},
                 {"applied_to", Array(applied)}});
  return out;
}
} // namespace
StageResult PreparePhase3Policy(
    const ArtifactRef &request, const std::string &directory,
    const volatile std::sig_atomic_t *cancelled,
    const std::map<std::string, ArtifactRef> &compiled_sources) {
  Inputs in(request);
  const auto destination = fs::absolute(directory);
  return RunStage(
      "phase3.prepare-policy",
      [&] {
        in.Read(compiled_sources);
        return in.sources;
      },
      [&](const auto &sources) {
        Check(!fs::exists(destination) && !fs::is_symlink(destination),
              "configuration output already exists");
        fs::create_directories(destination.parent_path());
        Work work(destination.parent_path());
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 3 configuration cancelled");
        D facts(D::Read(in.roles.at("facts")));
        facts.Keys({"schema", "candidate_selection_policy", "cut_mode",
                    "generic_ff_inputs", "generic_ff_prefix",
                    "hard_combinational_resources", "hard_macro_resources",
                    "legal_cut_classes", "replicated_net_classes",
                    "resource_fields", "sequential_inputs", "state_resources",
                    "transported_cut_classes", "valid_cut_classes"});
        Check(facts.Text("schema") ==
                      "emuflow.partition-structural-policy/v1" &&
                  facts.Text("candidate_selection_policy") ==
                      DecodeJsonString(
                          in.options.at("candidate_selection_policy")),
              "structural policy identity disagrees");
        Candidate(work.root / "candidate-policy.bin", in, facts);
        const auto candidate =
            ArtifactRef::Bind((work.root / "candidate-policy.bin").string(),
                              "candidate-policy-request-v1");
        const auto timing = ProjectTiming(in);
        const auto normalized_board = work.root / "board.json";
        PublishTextArtifact(
            normalized_board.string(), sources,
            [&](std::ostream &stream) { stream << in.board << '\n'; });
        const auto routing = BuildPartitionRoutingPolicy(
            ArtifactRef::Bind(normalized_board.string(), "boarddb-v1"),
            timing.constraints);
        fs::remove(normalized_board);
        D board(in.board);
        std::vector<std::string> fpgas, links;
        for (const auto &token : D::Array(board.Token("fpgas"), 255)) {
          D fpga(token);
          fpgas.push_back(D::Encode(
              {{"id", fpga.Token("id")},
               {"effective_capacity", fpga.Token("effective_capacity")}}));
        }
        Check(!fpgas.empty(), "empty board");
        for (const auto &token : D::Array(board.Token("links"))) {
          D link(token);
          auto ends = D::Array(link.Token("endpoints"), 2);
          Check(ends.size() == 2, "invalid board link");
          const auto payload =
              link.Has("payload_bits_per_lane_per_cycle")
                  ? link.Integer("payload_bits_per_lane_per_cycle")
                  : 1;
          const auto lanes = link.Integer("data_lanes_per_direction");
          Check(lanes > 0 && payload > 0 && lanes <= INT64_MAX / payload,
                "link payload overflows");
          links.push_back(D::Encode(
              {{"from", ends[0]},
               {"to", ends[1]},
               {"capacity", std::to_string(lanes * payload)},
               {"latency", link.Token("latency_cycles")},
               {"bidirectional", link.Text("direction") == "unidirectional"
                                     ? "false"
                                     : "true"}}));
        }
        O refs;
        for (const auto *role :
             {"source", "board", "native", "constraints", "timing_selector",
              "timing", "weight_selector", "weights", "solution"})
          if (in.roles.count(role))
            refs[role] = Binding(in.roles.at(role));
        refs["candidate_policy"] =
            Binding(candidate, destination / "candidate-policy.bin");
        O config;
        for (const auto *name :
             {"provider", "seed", "openroad", "timeout_seconds",
              "seed_attempts", "num_initial_solutions",
              "num_best_initial_solutions", "repair_min_used",
              "unweighted_baseline", "max_moves", "candidate_selection_policy"})
          config[name] = in.options.at(name);
        config["schema"] = Quote("emuflow.phase3-stage/v1");
        config["inputs"] = D::Encode(refs);
        config["platform"] = D(board.Token("platform")).Token("name");
        config["platform_sha256"] = Quote(HashBoard(in.board));
        config["partition_configuration"] = D::Encode(
            {{"pattern_range_policy", in.options.at("pattern_range_policy")},
             {"min_used_fpgas", in.options.at("min_used_fpgas")},
             {"balance_tolerance", in.options.at("balance_tolerance")},
             {"board",
              D::Encode({{"fpgas", Array(fpgas)}, {"links", Array(links)}})}});
        O cluster;
        for (const auto *name :
             {"legal_cut_classes", "replicated_net_classes",
              "hard_macro_resources", "cut_mode", "transported_cut_classes"})
          cluster[name] = facts.Token(name);
        cluster["hard_macro_granularity"] = Quote("instance");
        cluster["qualification"] = Quote("structural-partition-legality");
        cluster["max_cross_fpga_dependency_depth"] =
            in.options.at("max_cross_fpga_dependency_depth");
        config["cluster_policy"] = D::Encode(cluster);
        config["route_constraints"] = routing.constraints;
        config["domains"] = routing.domains;
        config["shortest_routes"] = routing.shortest_routes;
        config["hop_distances"] = routing.hop_distances;
        PublishTextArtifact((work.root / "phase3-request.json").string(),
                            sources, [&](std::ostream &stream) {
                              const auto value = D::Encode(config);
                              D checked(value);
                              stream << value << '\n';
                            });
        const auto summary = D::Encode(
            {{"schema", Quote("emuflow.phase3-configuration-summary/v1")},
             {"effective_route_constraints", timing.constraints},
             {"link_timing_report", timing.report},
             {"board_link_timing", timing.database}});
        PublishTextArtifact(
            (work.root / "configuration-summary.json").string(), sources,
            [&](std::ostream &stream) { stream << summary << '\n'; });
        if (cancelled && *cancelled)
          throw StageError(ErrorCode::kExecutionFailed,
                           "Phase 3 configuration cancelled");
        PublishNewDirectory(work.root.string(), destination.string(), sources);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind((destination / "phase3-request.json").string(),
                              "phase3-stage-request-v1"),
            ArtifactRef::Bind((destination / "candidate-policy.bin").string(),
                              "candidate-policy-request-v1"),
            ArtifactRef::Bind(
                (destination / "configuration-summary.json").string(),
                "phase3-configuration-summary-v1")};
      });
}
} // namespace emuflow::core
