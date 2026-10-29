#include "transport/chimew/lookahead.hpp"
#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"
#include "storage/store.hpp"
#include "transport/chimew/kernels.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
using Domain = std::tuple<std::string, std::string, std::string>;
using Lane = std::tuple<std::string, std::string, std::string, int>;
using Group = std::tuple<std::string, std::string, int>;
namespace fs = std::filesystem;
constexpr const char *producer =
    "indexed-xilinx-placement+fixed-lvds-lookahead-v1";
void Check(bool valid, const std::string &why) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact, "Chimew lookahead: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed, "Chimew lookahead cancelled");
}
struct Hash {
  Sha256 hash;
  void Add(const std::string &text) {
    hash.update(reinterpret_cast<const unsigned char *>(text.data()),
                text.size());
  }
  std::string Finish() {
    std::string result;
    for (auto c : hash.finish()) {
      result += "0123456789abcdef"[c >> 4];
      result += "0123456789abcdef"[c & 15];
    }
    return result;
  }
};
std::string Digest(const std::string &text) {
  Hash h;
  h.Add(text);
  return h.Finish();
}
std::string Array(const std::vector<std::string> &rows) {
  std::string result = "[";
  for (const auto &row : rows) {
    if (result.size() > 1)
      result += ',';
    result += row;
  }
  return result + ']';
}
std::string Point(double x, double y) {
  return D::Encode(
      {{"x", JsonRecord(x).Encode()}, {"y", JsonRecord(y).Encode()}});
}
std::string Generation(const ArtifactRef &ref) {
  const auto &g = ref.generation();
  return JsonRecord(
             std::vector<JsonRecord>{g.device, g.inode, g.bytes,
                                     g.modified_seconds, g.modified_nanoseconds,
                                     g.changed_seconds, g.changed_nanoseconds})
      .Encode();
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value "
                        "END FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step() && !row.IsNull(0), "missing or excessive metadata " + key);
  const auto value = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return value;
}
O Fields(const std::string &text) {
  O out;
  VisitJsonObjectTokens(text, [&](const auto &key, const auto &value) {
    Check(out.emplace(key, value).second, "duplicate record key");
  });
  return out;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto text = (parent / ".chimew-lookahead-XXXXXX").string();
    Check(mkdtemp(text.data()), "cannot create workspace");
    root = text;
  }
  ~Workspace() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
struct Signal {
  I seq = 0;
  std::string id, net, link, source, sink, crossed[2];
  int lane = 0, slot = 0, ratio = 0;
  std::uint64_t encoding = 0;
  double sx = 0, sy = 0, tx = 0, ty = 0, weight = 1;
  bool critical = false, guard = false;
  int initial = -1, refined = -1, materialized = -1;
  Domain domain() const { return {link, source, sink}; }
  Lane lane_key() const { return {link, source, sink, lane}; }
};
class Lookahead {
public:
  const volatile std::sig_atomic_t *cancelled;
  std::vector<ArtifactRef> sources;
  std::optional<ArtifactRef> signal_selector, signal_store, geometry_selector,
      geometry_store, board_ref, schedule_ref, schedule_store, route_ref,
      inventory_ref, architecture_ref, timing_ref, wiring_ref, original_ref;
  std::string design, platform, architecture_hash, board_normalized,
      virtual_board, geometry_header, signal_header, placement_hash,
      expected_grouping_hash;
  bool weighted = false;
  int regions = 0;
  I exact_hops = 0, fallbacks = 0, total_crossings = 0, forwarded = 0,
    guarded_entries = 0, guarded_lanes = 0, channels = 0;
  double weight_scale = 0, coordinate_scale = 0;
  std::string path_scope;
  std::map<std::string, std::array<I, 4>> bounds;
  std::map<std::pair<std::string, std::string>, std::array<I, 2>> pins;
  std::map<std::string, int> fpga_order;
  std::vector<std::string> fpga_ids, links;
  std::map<std::string, std::array<std::string, 2>> endpoints;
  std::map<std::string, std::pair<int, int>> bank_width;
  std::map<std::pair<std::string, int>, std::string> wires;
  std::map<Domain, std::pair<int, int>> allocations;
  std::vector<Signal> rows;
  std::vector<std::size_t> schedule_order;
  std::map<Group, std::vector<std::size_t>> grouped;
  std::map<Group, int> fixed_lanes;
  std::map<Group, std::string> group_domains;
  std::vector<std::string> domains, banks, electrical;
  emuflow::native::ChimewGroupingResult initial;
  emuflow::native::ChimewRefinementResult refined;
  explicit Lookahead(const volatile std::sig_atomic_t *flag)
      : cancelled(flag) {}
  ArtifactRef Bind(const std::string &path, const std::string &schema) {
    auto ref = ArtifactRef::Bind(path, schema);
    sources.push_back(ref);
    return ref;
  }
  ArtifactRef Lease(const std::string &token) {
    D doc(token);
    doc.Keys({"path", "schema", "generation"});
    auto ref = Bind(doc.Text("path"), doc.Text("schema"));
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(compare, doc.Token("generation"), Generation(ref),
                          "Chimew lookahead source generation changed");
    return ref;
  }
  void Read(const ArtifactRef &request) {
    sources = {request};
    D doc(D::Read(request));
    doc.Keys({"schema", "signals"});
    Check(doc.Text("schema") == "emuflow.chimew-lookahead-input/v1",
          "request schema disagrees");
    signal_selector = Lease(doc.Token("signals"));
    Check(signal_selector->schema() == "chimew-signal-selector-v1",
          "signal input role disagrees");
    D manifest(D::Read(*signal_selector));
    Check(manifest.Text("schema") == "emuflow.chimew-signal-storage/v1" &&
              manifest.Text("artifact") == "signals.sqlite",
          "signal selector namespace disagrees");
    signal_store = Bind(
        (fs::path(signal_selector->path()).parent_path() / "signals.sqlite")
            .string(),
        "chimew-signal-store-v1");
    Check(manifest.Integer("store_bytes") >= 0 &&
              std::uint64_t(manifest.Integer("store_bytes")) ==
                  signal_store->generation().bytes &&
              manifest.Text("store_sha256") ==
                  Sha256File(*signal_store, cancelled),
          "signal content seal disagrees");
    SqliteStore owner(*signal_store);
    signal_header = Meta(owner.database(), "header");
    D header(signal_header);
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    RequireEquivalentJson(compare, header.text(), manifest.Token("header"),
                          "Chimew signal selector header differs");
    Check(header.Text("schema") == "emuflow.chimew-signals/v1" &&
              header.Text("implementation_id") ==
                  "native-chimew-signal-preparation-v1",
          "signal producer identity disagrees");
    design = header.Text("design");
    platform = header.Text("platform");
    weighted = header.Boolean("weighted");
    regions = int(header.Integer("region_count"));
    weight_scale = header.Number("weight_scale");
    path_scope = header.Text("path_scope");
    exact_hops = header.Integer("exact_path_hops");
    fallbacks = header.Integer("whole_net_fallbacks");
    Check(regions >= 2 && regions <= 31 && weight_scale >= 0 &&
              (path_scope == "exact-hop" || path_scope == "whole-net"),
          "signal policy is invalid");
    std::map<std::string, ArtifactRef> unique;
    for (const auto &token : D::Array(header.Token("input_artifacts"), 16384)) {
      auto ref = Lease(token);
      auto it = unique.find(ref.schema());
      if (it == unique.end())
        unique.emplace(ref.schema(), ref);
      else
        Check(it->second.path() == ref.path() &&
                      it->second.generation() == ref.generation() ||
                  ref.schema() == "boundary-identity-store-v1" ||
                  ref.schema() == "boundary-identity-selector-v1" ||
                  ref.schema() == "xilinx-mapped-index-v2" ||
                  ref.schema() == "vivado-lineage-store-v2" ||
                  ref.schema() == "xilinx-physical-handoff-v1" ||
                  ref.schema() == "openparf-site-store-v1",
              "ambiguous primary signal source");
    }
    const auto take = [&](const char *schema) -> ArtifactRef {
      Check(unique.count(schema),
            std::string("missing signal source ") + schema);
      return unique.at(schema);
    };
    geometry_selector = take("chimew-geometry-selector-v1");
    geometry_store = take("chimew-geometry-store-v1");
    board_ref = take("boarddb-v1");
    schedule_ref = take("tdm-schedule-selector-v4");
    schedule_store = take("tdm-schedule-store-v4");
    route_ref = take("system-route-selector-v1");
    inventory_ref = take("hp-package-pin-inventory-v1");
    architecture_ref = take("physical-input-document-v1");
    original_ref = take("emuir-compact-v2");
    if (unique.count("fixed-wiring-csv-v1"))
      wiring_ref = take("fixed-wiring-csv-v1");
    if (weighted)
      timing_ref = take("sta-path-selector-v1");
    Check(weighted == bool(timing_ref), "timing source presence disagrees");
    SqliteStore geom(*geometry_store);
    geometry_header = Meta(geom.database(), "header");
    D gh(geometry_header);
    D architecture(gh.Token("architecture"));
    architecture_hash = architecture.Text("sha256");
    Check(architecture.Text("path") == architecture_ref->path(),
          "geometry architecture source differs");
    D gm(D::Read(*geometry_selector));
    RequireEquivalentJson(compare, gm.Token("header"), geometry_header,
                          "Chimew geometry header differs");
    D metric(gh.Token("metrics"));
    forwarded = metric.Integer("forwarded");
    for (const auto &box : Fields(gh.Token("bounds"))) {
      const auto v = D::Array(box.second, 4);
      Check(v.size() == 4, "invalid physical bounds");
      std::array<I, 4> b{};
      for (int i = 0; i < 4; ++i)
        b[i] = D::IntegerToken(v[i]);
      Check(b[0] >= 0 && b[1] >= b[0] && b[2] >= 0 && b[3] >= b[2],
            "invalid native site bounds");
      bounds.emplace(box.first, b);
      coordinate_scale = std::max(coordinate_scale, double(b[1]) + 1.0);
    }
    for (const auto &token : D::Array(gh.Token("pins"), 65536)) {
      const auto v = D::Array(token, 4);
      Check(v.size() == 4 &&
                pins.emplace(std::make_pair(DecodeJsonString(v[0]),
                                            DecodeJsonString(v[1])),
                             std::array<I, 2>{D::IntegerToken(v[2]),
                                              D::IntegerToken(v[3])})
                    .second,
            "invalid or duplicate pin coordinate");
    }
    board_normalized = NormalizeBoard(D::Read(*board_ref));
    D board(board_normalized);
    D name(board.Token("platform"));
    Check(name.Text("name") == platform, "board and schedule platform differ");
    for (const auto &token : D::Array(board.Token("fpgas"), 4096)) {
      D f(token);
      const auto id = f.Text("id");
      Check(bounds.count(id) &&
                fpga_order.emplace(id, int(fpga_ids.size())).second,
            "board FPGA bounds differ");
      fpga_ids.push_back(id);
    }
    Check(bounds.size() == fpga_ids.size(), "geometry FPGA coverage differs");
    for (const auto &token : D::Array(board.Token("links"), 16384)) {
      D l(token);
      const auto id = l.Text("id");
      Check(l.Has("bank_count") && l.Has("wires_per_bank") &&
                l.Text("capacity_sharing") == "shared_bidirectional",
            "requires fixed shared HP-bank wiring");
      const auto e = D::Array(l.Token("endpoints"), 2);
      Check(e.size() == 2, "invalid link endpoints");
      endpoints.emplace(id, std::array<std::string, 2>{DecodeJsonString(e[0]),
                                                       DecodeJsonString(e[1])});
      const auto count = l.Integer("bank_count"),
                 width = l.Integer("wires_per_bank");
      Check(count > 0 && width > 0 && count <= INT32_MAX / width,
            "invalid fixed wire inventory");
      bank_width.emplace(id, std::make_pair(int(count), int(width)));
      links.push_back(id);
    }
    virtual_board = BuildVirtualBoard(board_normalized, *inventory_ref,
                                      wiring_ref, cancelled);
    D wiring(virtual_board);
    VisitJsonArrayTokens(wiring.Token("wires"), [&](const auto &token) {
      D w(token);
      Check(wires
                .emplace(std::make_pair(w.Text("link"), int(w.Integer("wire"))),
                         token)
                .second,
            "duplicate wire");
    });
    SqliteStore schedule(*schedule_store);
    D sh(Meta(schedule.database(), "header"));
    Check(sh.Text("design") == design && sh.Text("platform") == platform,
          "schedule namespace differs");
    for (const auto &token : D::Array(sh.Token("bank_allocations"), 32768)) {
      D a(token);
      const auto offset = a.Integer("lane_offset"), lanes = a.Integer("lanes"),
                 count = a.Integer("bank_count"),
                 width = a.Integer("wires_per_bank");
      const auto link = a.Text("link");
      Check(bank_width.count(link) && offset >= 0 && lanes >= 0 && count >= 0 &&
                width > 0 && width == bank_width.at(link).second &&
                count <= INT32_MAX / width && lanes == count * width &&
                offset % width == 0 &&
                offset + lanes <= bank_width.at(link).first * width,
            "Phase5 whole-bank wire range disagrees");
      Check(allocations
                .emplace(Domain{link, a.Text("from"), a.Text("to")},
                         std::make_pair(int(offset), int(lanes)))
                .second,
            "duplicate Phase5 bank allocation");
    }
    Check(allocations.size() == 2 * links.size(),
          "Phase5 bank directions do not cover board links");
    for (const auto &id : links) {
      auto e = endpoints.at(id);
      std::sort(e.begin(), e.end());
      const auto f = allocations.find({id, e[0], e[1]}),
                 r = allocations.find({id, e[1], e[0]});
      Check(f != allocations.end() && r != allocations.end() &&
                f->second.first == 0 && r->second.first == f->second.second &&
                f->second.second + r->second.second ==
                    bank_width.at(id).first * bank_width.at(id).second,
            "Phase5 direction ranges overlap or omit fixed wires");
    }
    {
      auto attach = owner.database().Prepare("ATTACH DATABASE ? AS scheduled");
      attach.BindText(1, SqliteImmutableUri(schedule_store->path()));
      attach.Execute();
      auto mismatch = owner.database().Prepare(
          "SELECT 1 FROM signals s LEFT JOIN scheduled.schedule_entries e ON "
          "e.seq=s.seq "
          "WHERE s.entry_id IS NOT e.entry_id OR s.net IS NOT e.net OR "
          "s.link_id IS NOT e.link_id "
          "OR s.source_fpga IS NOT e.source_fpga OR s.sink_fpga IS NOT "
          "e.sink_fpga "
          "OR s.lane IS NOT e.lane OR s.slot IS NOT e.slot OR s.tdm_ratio IS "
          "NOT e.tdm_ratio LIMIT 1");
      Check(!mismatch.Step(),
            "signal identity disagrees with its schedule owner");
      auto coverage = owner.database().Prepare(
          "SELECT (SELECT count(*) FROM signals)=(SELECT count(*) FROM "
          "scheduled.schedule_entries)");
      Check(coverage.Step() && coverage.TypedInteger(0) == 1,
            "signal/schedule coverage differs");
    }
    auto entries = owner.database().Prepare(
        "SELECT "
        "seq,entry_id,net,link_id,source_fpga,sink_fpga,lane,slot,tdm_ratio,"
        "source_x,source_y,sink_x,sink_y,source_slls,sink_slls,encoding,timing_"
        "weight,protected FROM signals ORDER BY entry_id");
    while (entries.Step()) {
      Stop(cancelled);
      Signal s;
      s.seq = entries.TypedInteger(0);
      s.id = entries.TypedText(1);
      s.net = entries.TypedText(2);
      s.link = entries.TypedText(3);
      s.source = entries.TypedText(4);
      s.sink = entries.TypedText(5);
      const auto lane = entries.TypedInteger(6), slot = entries.TypedInteger(7),
                 ratio = entries.TypedInteger(8);
      Check(lane >= 0 && lane <= INT32_MAX && slot >= 0 && slot <= INT32_MAX &&
                ratio > 0 && ratio <= INT32_MAX && endpoints.count(s.link) &&
                fpga_order.count(s.source) && fpga_order.count(s.sink),
            "signal identity/domain is invalid");
      s.lane = int(lane);
      s.slot = int(slot);
      s.ratio = int(ratio);
      const auto allocation = allocations.find(s.domain());
      Check(allocation != allocations.end() &&
                lane >= allocation->second.first &&
                lane < allocation->second.first + allocation->second.second,
            "signal lane escapes its Phase5 direction allocation");
      s.sx = entries.TypedReal(9);
      s.sy = entries.TypedReal(10);
      s.tx = entries.TypedReal(11);
      s.ty = entries.TypedReal(12);
      s.crossed[0] = entries.TypedText(13);
      s.crossed[1] = entries.TypedText(14);
      const auto mask = entries.TypedInteger(15);
      Check(mask >= 0 &&
                std::uint64_t(mask) < (std::uint64_t(1) << (2 * (regions - 1))),
            "crossing mask exceeds policy");
      s.encoding = std::uint64_t(mask);
      std::uint64_t recomputed = 0;
      for (int side = 0; side < 2; ++side) {
        I previous = -1;
        for (const auto &token : D::Array(s.crossed[side], 31)) {
          const auto boundary = D::IntegerToken(token);
          Check(boundary > previous && boundary < regions - 1,
                "crossing boundary order/range disagrees");
          recomputed |= std::uint64_t(1) << (side * (regions - 1) + boundary);
          previous = boundary;
        }
      }
      Check(recomputed == s.encoding,
            "crossing mask differs from its source/sink boundaries");
      s.weight = entries.TypedReal(16);
      Check(s.weight >= 1 && std::isfinite(s.weight), "invalid timing weight");
      Check(entries.TypedInteger(17) == 0 || entries.TypedInteger(17) == 1,
            "invalid guard flag");
      s.critical = entries.TypedInteger(17) == 1;
      Check((s.weight > 1 &&
             std::abs(s.weight - 1) > std::max(1e-12, 1e-9 * s.weight)) ==
                s.critical,
            "critical-entry guard differs from source weight");
      Check(weighted || s.weight == 1,
            "unweighted source contains timing weights");
      total_crossings += __builtin_popcountll(s.encoding);
      rows.push_back(std::move(s));
    }
    Check(!rows.empty() && rows.size() <= std::size_t(INT32_MAX) &&
              I(rows.size()) == header.Integer("signals"),
          "signal coverage differs");
    schedule_order.resize(rows.size());
    std::iota(schedule_order.begin(), schedule_order.end(), 0);
    std::sort(schedule_order.begin(), schedule_order.end(),
              [&](auto a, auto b) { return rows[a].seq < rows[b].seq; });
    for (std::size_t i = 0; i < schedule_order.size(); ++i)
      Check(rows[schedule_order[i]].seq == I(i),
            "signal schedule order is incomplete");
  }
  void ValidateGroups(bool after) {
    std::map<int, std::uint64_t> initial_masks;
    for (const auto &signal : rows)
      initial_masks[signal.initial] |= signal.encoding;
    std::map<int, std::vector<std::size_t>> groups;
    for (std::size_t i = 0; i < rows.size(); ++i)
      groups[after ? rows[i].refined : rows[i].initial].push_back(i);
    Check(groups.size() == std::size_t(initial.groups),
          "group coverage differs");
    I crossings = 0;
    for (const auto &pair : groups) {
      Check(pair.first >= 0 && pair.first < initial.groups &&
                !pair.second.empty(),
            "invalid group identity");
      const auto &anchor = rows[pair.second.front()];
      std::set<int> slots;
      std::uint64_t encoding = 0;
      for (auto index : pair.second) {
        const auto &s = rows[index];
        Check(s.domain() == anchor.domain() && s.ratio == anchor.ratio &&
                  s.guard == anchor.guard &&
                  (!s.guard || s.lane == anchor.lane) &&
                  slots.insert(s.slot).second,
              "group crosses a domain, guard lane, ratio or frozen slot");
        encoding |= s.encoding;
      }
      Check(pair.second.size() <= std::size_t(anchor.ratio),
            "group exceeds TDM capacity");
      crossings += __builtin_popcountll(encoding);
      if (after) {
        Check(encoding == initial_masks.at(pair.first),
              "refinement changed group encoding");
      }
    }
    Check(crossings == initial.crossing_bits, "group crossing count differs");
  }
  void GroupSignals() {
    std::set<Lane> guards;
    std::map<Domain, int> rank;
    for (const auto &s : rows) {
      rank.emplace(s.domain(), 0);
      if (s.critical)
        guards.insert(s.lane_key());
    }
    int next = 0;
    for (auto &pair : rank)
      pair.second = next++;
    std::vector<emuflow::native::ChimewGroupingSignal> input;
    input.reserve(rows.size());
    for (auto &s : rows) {
      s.guard = guards.count(s.lane_key());
      guarded_entries += s.guard;
      input.push_back({rank.at(s.domain()), s.guard ? s.lane : -1, s.ratio,
                       s.slot, s.encoding});
    }
    guarded_lanes = guards.size();
    initial = emuflow::native::GroupChimewSignalRecords(std::move(input));
    Check(initial.assignment.size() == rows.size(),
          "grouping result coverage differs");
    for (std::size_t i = 0; i < rows.size(); ++i)
      rows[i].initial = initial.assignment[i];
    ValidateGroups(false);
    using RefinementDomain = std::tuple<Domain, bool, int, int>;
    std::map<RefinementDomain, int> refinement_rank;
    for (const auto &s : rows)
      refinement_rank.emplace(
          RefinementDomain{s.domain(), s.guard, s.guard ? s.lane : -1, s.slot},
          0);
    next = 0;
    for (auto &pair : refinement_rank)
      pair.second = next++;
    std::vector<emuflow::native::ChimewRefinementSignal> refinable;
    refinable.reserve(rows.size());
    for (std::size_t i = 0; i < rows.size(); ++i) {
      const auto &s = rows[i];
      refinable.push_back({int(i),
                           refinement_rank.at({s.domain(), s.guard,
                                               s.guard ? s.lane : -1, s.slot}),
                           s.ratio, s.encoding, s.initial, s.sy});
    }
    refined = emuflow::native::RefineChimewSignalRecords(std::move(refinable));
    Check(refined.entries.size() == rows.size() &&
              refined.after <= refined.before + 1e-9,
          "invalid refinement result");
    for (std::size_t i = 0; i < rows.size(); ++i) {
      Check(refined.entries[i].index == int(i),
            "refinement index namespace differs");
      rows[i].refined = rows[i].materialized = refined.entries[i].group;
    }
    ValidateGroups(true);
    next = initial.groups;
    std::map<Lane, int> materialized;
    for (const auto &lane : guards) {
      Check(next < INT32_MAX, "materialized group index overflow");
      materialized.emplace(lane, next++);
    }
    for (auto &signal : rows) {
      const auto found = materialized.find(signal.lane_key());
      if (found != materialized.end())
        signal.materialized = found->second;
    }
    for (auto index : schedule_order) {
      const auto &s = rows[index];
      const auto e = endpoints.at(s.link);
      Check((s.source == e[0] && s.sink == e[1]) ||
                (s.source == e[1] && s.sink == e[0]),
            "signal endpoints differ from board link");
      const std::string direction = s.source == e[0] ? "a_to_b" : "b_to_a";
      const Group key{s.link, direction, s.materialized};
      grouped[key].push_back(index);
      if (s.guard) {
        const auto inserted = fixed_lanes.emplace(key, s.lane);
        Check(inserted.second || inserted.first->second == s.lane,
              "timing guard does not preserve one exact lane");
      }
    }
  }
  std::string Wire(const std::string &link, int lane) {
    const auto found = wires.find({link, lane});
    Check(found != wires.end(), "channel has no fixed differential wire");
    return found->second;
  }
  std::array<std::string, 2> WireEnds(const std::string &wire,
                                      const std::string &a,
                                      const std::string &b) {
    D w(wire);
    std::array<std::string, 2> out;
    for (const auto &token : D::Array(w.Token("endpoints"), 2)) {
      D e(token);
      if (e.Text("fpga") == a)
        out[0] = token;
      else if (e.Text("fpga") == b)
        out[1] = token;
      else
        Check(false, "fixed wire endpoint differs");
    }
    Check(!out[0].empty() && !out[1].empty(),
          "fixed wire endpoint coverage differs");
    return out;
  }
  void FixedChannels() {
    D wiring(virtual_board), assumptions(wiring.Token("assumptions"));
    const auto iostandard = assumptions.Token("iostandard"),
               voltage = assumptions.Token("vcco_v");
    std::set<std::pair<std::string, std::string>> used_pins;
    std::map<std::pair<std::string, std::string>,
             std::pair<std::string, std::string>>
        bank_directions;
    for (const auto &link : links) {
      const auto ep = endpoints.at(link);
      const auto &a = ep[0];
      const auto &b = ep[1];
      for (int dir = 0; dir < 2; ++dir) {
        Stop(cancelled);
        const std::string direction = dir ? "b_to_a" : "a_to_b",
                          source = dir ? b : a, sink = dir ? a : b;
        std::vector<Group> keys;
        for (const auto &g : grouped)
          if (std::get<0>(g.first) == link && std::get<1>(g.first) == direction)
            keys.push_back(g.first);
        if (keys.empty())
          continue;
        const auto budget = allocations.at({link, source, sink});
        std::map<int, Group> guarded;
        std::vector<Group> free_groups;
        for (const auto &key : keys) {
          auto f = fixed_lanes.find(key);
          if (f == fixed_lanes.end())
            free_groups.push_back(key);
          else
            Check(f->second >= budget.first &&
                      f->second < budget.first + budget.second &&
                      guarded.emplace(f->second, key).second,
                  "protected lane conflicts with fixed direction");
        }
        std::vector<int> free_lanes;
        for (int lane = budget.first; lane < budget.first + budget.second;
             ++lane)
          if (!guarded.count(lane))
            free_lanes.push_back(lane);
        Check(free_groups.size() <= free_lanes.size(),
              "groups exceed eligible fixed-direction wires");
        struct Partition {
          std::string id;
          std::vector<Group> groups;
          std::vector<int> lanes;
        };
        std::vector<Partition> partitions;
        if (!free_groups.empty())
          partitions.push_back({link + ":" + direction, std::move(free_groups),
                                std::move(free_lanes)});
        for (const auto &g : guarded)
          partitions.push_back(
              {link + ":" + direction + ":lane" + std::to_string(g.first),
               {g.second},
               {g.first}});
        for (const auto &partition : partitions) {
          domains.push_back(D::Encode({{"id", JsonQuote(partition.id)},
                                       {"fpga_a", JsonQuote(a)},
                                       {"fpga_b", JsonQuote(b)}}));
          for (const auto &key : partition.groups)
            Check(group_domains.emplace(key, partition.id).second,
                  "duplicate assignment domain");
          using PhysicalBank = std::pair<std::string, std::string>;
          std::map<PhysicalBank,
                   std::vector<std::pair<int, std::array<std::string, 2>>>>
              bank_members;
          for (int lane : partition.lanes) {
            const auto ends = WireEnds(Wire(link, lane), a, b);
            D ea(ends[0]), eb(ends[1]);
            bank_members[{a + ":HP" + ea.Text("bank"),
                          b + ":HP" + eb.Text("bank")}]
                .push_back({lane, ends});
          }
          for (const auto &bucket : bank_members) {
            std::vector<std::string> records;
            double ax = 0, ay = 0, bx = 0, by = 0;
            int order = 0;
            for (const auto &item : bucket.second) {
              const auto lane = item.first;
              D ea(item.second[0]), eb(item.second[1]), pa(ea.Token("p")),
                  pb(eb.Token("p"));
              const auto pina = pa.Text("package_pin"),
                         pinb = pb.Text("package_pin");
              Check(pins.count({a, pina}) && pins.count({b, pinb}),
                    "fixed channel lacks geometry");
              const auto ga = pins.at({a, pina}), gb = pins.at({b, pinb});
              const double x1 = ga[0] + fpga_order.at(a) * coordinate_scale,
                           x2 = gb[0] + fpga_order.at(b) * coordinate_scale;
              const auto id =
                  link + ":" + direction + ":wire" + std::to_string(lane);
              records.push_back(D::Encode(
                  {{"id", JsonQuote(id)},
                   {"order", std::to_string(order++)},
                   {"pin_a", D::Encode({{"x", JsonRecord(x1).Encode()},
                                        {"y", std::to_string(ga[1])}})},
                   {"pin_b", D::Encode({{"x", JsonRecord(x2).Encode()},
                                        {"y", std::to_string(gb[1])}})}}));
              ax += x1;
              ay += ga[1];
              bx += x2;
              by += gb[1];
              for (int side = 0; side < 2; ++side) {
                D end(item.second[side]);
                const auto fpga = side ? b : a, bank = end.Text("bank");
                const auto assigned = bank_directions.emplace(
                    std::make_pair(fpga, bank), std::make_pair(source, sink));
                Check(assigned.second || assigned.first->second ==
                                             std::make_pair(source, sink),
                      "cannot reverse a fixed HP bank within a build");
                for (const auto *polarity : {"p", "n"}) {
                  D pin(end.Token(polarity));
                  Check(used_pins.emplace(fpga, pin.Text("package_pin")).second,
                        "differential channel conductors overlap");
                }
              }
              D wire(Wire(link, lane));
              electrical.push_back(
                  D::Encode({{"chimew_channel", JsonQuote(id)},
                             {"link", JsonQuote(link)},
                             {"physical_lane", std::to_string(lane)},
                             {"direction", JsonQuote(direction)},
                             {"fixed_wire", wire.Token("id")},
                             {"bank_a", JsonQuote(bucket.first.first)},
                             {"bank_b", JsonQuote(bucket.first.second)},
                             {"package_pin_a", JsonQuote(pina)},
                             {"package_pin_b", JsonQuote(pinb)},
                             {"iostandard", iostandard},
                             {"supported_iostandards", "[" + iostandard + "]"},
                             {"bank_voltage", voltage},
                             {"electrical_class", JsonQuote("fixed-lvds-pair")},
                             {"reserved", "false"},
                             {"placement_anchor", "true"}}));
              ++channels;
            }
            const auto count = double(records.size());
            banks.push_back(D::Encode(
                {{"id", JsonQuote(partition.id + ":" + bucket.first.first +
                                  ":" + bucket.first.second)},
                 {"domain", JsonQuote(partition.id)},
                 {"bank_a",
                  D::Encode({{"id", JsonQuote(bucket.first.first)},
                             {"x", JsonRecord(ax / count).Encode()},
                             {"y", JsonRecord(ay / count).Encode()}})},
                 {"bank_b",
                  D::Encode({{"id", JsonQuote(bucket.first.second)},
                             {"x", JsonRecord(bx / count).Encode()},
                             {"y", JsonRecord(by / count).Encode()}})},
                 {"channels", Array(records)}}));
          }
        }
      }
    }
    Check(group_domains.size() == grouped.size() && !banks.empty() &&
              channels > 0,
          "fixed bank domains do not cover every group");
  }
  O Identity(const char *schema, const char *provider) const {
    return {{"schema", JsonQuote(schema)},
            {"provider", JsonQuote(provider)},
            {"design", JsonQuote(design)},
            {"platform", JsonQuote(platform)}};
  }
  O Provenance() const {
    return {{"producer", JsonQuote(producer)},
            {"producer_version", JsonQuote("1")},
            {"placement_sha256", JsonQuote(placement_hash)}};
  }
  std::string GroupRecord(const Group &key,
                          const std::vector<std::size_t> &members) {
    std::vector<std::string> records;
    for (auto index : members) {
      const auto &s = rows[index];
      O member{{"id", JsonQuote(s.id)},
               {"direction", JsonQuote(std::get<1>(key))},
               {"fanout", Point(s.sx, s.sy)},
               {"fanins", "[" + Point(s.tx, s.ty) + "]"}};
      if (weighted)
        member["timing_weight"] = JsonRecord(s.weight).Encode();
      records.push_back(D::Encode(member));
    }
    return D::Encode({{"id", JsonQuote("chimew-" + std::get<0>(key) + "-" +
                                       std::get<1>(key) + "-group-" +
                                       std::to_string(std::get<2>(key)))},
                      {"domain", JsonQuote(group_domains.at(key))},
                      {"kind", JsonQuote("tdm_group")},
                      {"direction", JsonQuote(std::get<1>(key))},
                      {"members", Array(records)}});
  }
  // Bind the exact consumer replay record expected by the current qualification
  // contract. This digest publishes no claim that a Python replay has run:
  // the consumer independently derives that report and rejects any mismatch.
  std::string ExpectedRefinementDigest() {
    auto header = Identity("emuflow.chimew-refined-signal-groups/v1",
                           "chimew-section3.3.2-bounded-inference-v1");
    header["status"] = JsonQuote("standalone_paper_bounded_inference");
    header["paper_scope"] =
        JsonQuote("FPGA-2026-Section-3.3.2-position-refinement");
    header["claim_boundary"] =
        JsonQuote("paper specifies equal-encoding source-y sorting/swaps and "
                  "SLL non-increase; group-anchor ordering and pairwise-y "
                  "acceptance are deterministic first-party inference");
    header["integration_status"] = JsonQuote("not-a-phase6-pin-plan");
    header["placement_provenance"] = D::Encode(Provenance());
    header["metrics"] = D::Encode(
        {{"signals", std::to_string(rows.size())},
         {"groups", std::to_string(initial.groups)},
         {"accepted_encoding_buckets",
          std::to_string(refined.accepted_buckets)},
         {"moved_signals", std::to_string(refined.moved_signals)},
         {"pairwise_source_y_before", JsonRecord(refined.before).Encode()},
         {"pairwise_source_y_after", JsonRecord(refined.after).Encode()},
         {"group_physical_sll_crossings",
          std::to_string(initial.crossing_bits)},
         {"oracle_disagreements", "0"}});
    header["entries"] = "";
    Hash hash;
    hash.Add("{");
    bool first = true;
    for (const auto &field : header) {
      if (!first)
        hash.Add(",");
      first = false;
      hash.Add(JsonQuote(field.first) + ":");
      if (field.first != "entries")
        hash.Add(field.second);
      else {
        hash.Add("[");
        for (std::size_t i = 0; i < rows.size(); ++i) {
          if (i)
            hash.Add(",");
          const auto &s = rows[i];
          hash.Add(D::Encode({{"schedule_entry", JsonQuote(s.id)},
                              {"group", std::to_string(s.refined)},
                              {"encoding", std::to_string(s.encoding)},
                              {"source_y", JsonRecord(s.sy).Encode()}}));
        }
        hash.Add("]");
      }
    }
    hash.Add("}");
    return hash.Finish();
  }
  template <class Emit>
  void Write(const fs::path &path, O header, const std::string &key,
             Emit emit) {
    header[key] = "";
    PublishTextArtifact(path.string(), sources, [&](std::ostream &out) {
      out << '{';
      bool first = true;
      for (const auto &field : header) {
        if (!first)
          out << ',';
        first = false;
        out << JsonQuote(field.first) << ':';
        if (field.first == key)
          emit(out);
        else
          out << field.second;
      }
      out << "}\n";
    });
  }
  void WriteSmall(const fs::path &path, const O &header) {
    PublishTextArtifact(path.string(), sources, [&](std::ostream &out) {
      out << D::Encode(header) << '\n';
    });
  }
  O Record(const fs::path &actual, const fs::path &final,
           const std::string &schema = "physical-input-document-v1") {
    const auto ref = ArtifactRef::Bind(actual.string(), schema);
    return {{"path", JsonQuote(final.string())},
            {"sha256", JsonQuote(Sha256File(ref, cancelled))}};
  }
  void Publish(const fs::path &work, const fs::path &target) {
    fs::create_directory(work / "sources");
    fs::create_directory(work / "inputs");
    D gh(geometry_header);
    const auto placement = work / "sources/physical-inputs.json",
               final_placement = target / "sources/physical-inputs.json";
    auto gr = Record(geometry_selector->path(), geometry_selector->path(),
                     "chimew-geometry-selector-v1");
    auto sr = Record(signal_selector->path(), signal_selector->path(),
                     "chimew-signal-selector-v1");
    gr["schema"] = JsonQuote("emuflow.chimew-geometry-storage/v1");
    sr["schema"] = JsonQuote("emuflow.chimew-signal-storage/v1");
    WriteSmall(
        placement,
        {{"schema", JsonQuote("emuflow.xilinx-chimew-physical-inputs/v1")},
         {"coordinate_system", JsonQuote("native-device-tile-column-row-v1")},
         {"fpgas", gh.Token("sources")},
         {"geometry", D::Encode(gr)},
         {"signals", D::Encode(sr)}});
    placement_hash = Sha256File(
        ArtifactRef::Bind(placement.string(), "physical-input-document-v1"),
        cancelled);
    expected_grouping_hash = ExpectedRefinementDigest();
    auto crossing = Identity("emuflow.chimew-crossing-encodings/v1",
                             "academic-virtual-region-routing-lookahead-v1");
    crossing["qualification"] = JsonQuote("academic-virtual-region-lookahead");
    crossing["coordinate_system"] = JsonQuote("normalized-placement-y");
    crossing["slls_per_fpga"] = std::to_string(regions - 1);
    crossing["provenance"] = D::Encode(
        {{"producer", JsonQuote(producer)},
         {"producer_version", JsonQuote("1")},
         {"routing_sha256", JsonQuote(Sha256File(*route_ref, cancelled))},
         {"claim_boundary",
          JsonQuote("virtual regions derived from normalized Xilinx-placement "
                    "coordinates; not device SLR/SLL closure")}});
    crossing["metrics"] = D::Encode(
        {{"signals", std::to_string(rows.size())},
         {"physical_sll_crossings", std::to_string(total_crossings)}});
    double maximum_weight = 1,
           minimum_guard = std::numeric_limits<double>::infinity();
    std::vector<std::string> protected_ids;
    for (const auto &s : rows) {
      maximum_weight = std::max(maximum_weight, s.weight);
      if (s.critical) {
        protected_ids.push_back(JsonQuote(s.id));
        minimum_guard = std::min(minimum_guard, s.weight);
      }
    }
    if (!protected_ids.empty())
      crossing["timing_guard"] = D::Encode(
          {{"provider", JsonQuote("emuflow-chimew-critical-lane-guard-v1")},
           {"scope", JsonQuote("EmuFlow extension, not a Chimew paper claim")},
           {"source_sha256", JsonQuote(Sha256File(*timing_ref, cancelled))},
           {"maximum_weight", JsonRecord(maximum_weight).Encode()},
           {"minimum_protected_weight", JsonRecord(minimum_guard).Encode()},
           {"protected_entries", Array(protected_ids)}});
    Write(work / "inputs/crossings.json", crossing, "entries",
          [&](std::ostream &out) {
            out << '[';
            bool first = true;
            for (auto index : schedule_order) {
              Stop(cancelled);
              const auto &s = rows[index];
              if (!first)
                out << ',';
              first = false;
              out << D::Encode({{"schedule_entry", JsonQuote(s.id)},
                                {"source_slls", s.crossed[0]},
                                {"sink_slls", s.crossed[1]},
                                {"encoding", std::to_string(s.encoding)}});
            }
            out << ']';
          });
    auto positions = Identity("emuflow.chimew-lookahead-positions/v1",
                              "source-qualified-physical-site-lookahead-v1");
    positions["qualification"] = JsonQuote("chimew-Xilinx-placement-lookahead");
    positions["coordinate_system"] = JsonQuote("physical-site-y");
    positions["provenance"] = D::Encode(Provenance());
    positions["metrics"] =
        D::Encode({{"signals", std::to_string(rows.size())}});
    Write(work / "inputs/positions.json", positions, "entries",
          [&](std::ostream &out) {
            out << '[';
            bool first = true;
            for (auto index : schedule_order) {
              Stop(cancelled);
              const auto &s = rows[index];
              if (!first)
                out << ',';
              first = false;
              out << D::Encode({{"schedule_entry", JsonQuote(s.id)},
                                {"source_y", JsonRecord(s.sy).Encode()}});
            }
            out << ']';
          });
    double xmax = 0, ymax = 0;
    I guard_points = 0;
    for (const auto &s : rows) {
      xmax = std::max(xmax, std::max(s.sx, s.tx));
      ymax = std::max(ymax, std::max(s.sy, s.ty));
      if (s.sy == s.ty) {
        ++guard_points;
        ymax = std::max(ymax, s.sy + 0.5);
      }
    }
    xmax += 1;
    ymax += 1;
    const double capacity =
        std::max(1.0, double(rows.size()) * (xmax + ymax) * 4.0);
    auto rudy = Identity("emuflow.chimew-rudy-input/v1",
                         "source-qualified-lookahead-placement-rudy-v1");
    rudy["coordinate_system"] = JsonQuote("physical-site-xy");
    rudy["degenerate_bbox_policy"] = JsonQuote("reject");
    rudy["wire_pitch_per_layer"] = "1.0";
    rudy["max_utilization"] = "1.0";
    auto provenance = Provenance();
    provenance["netlist_sha256"] =
        JsonQuote(Sha256File(*original_ref, cancelled));
    provenance["architecture_sha256"] = JsonQuote(architecture_hash);
    rudy["provenance"] = D::Encode(provenance);
    rudy["grid"] =
        D::Encode({{"origin_x", "0.0"},
                   {"origin_y", "0.0"},
                   {"bin_width", JsonRecord(xmax).Encode()},
                   {"bin_height", JsonRecord(ymax).Encode()},
                   {"columns", "1"},
                   {"rows", "1"},
                   {"capacities", "[" + JsonRecord(capacity).Encode() + "]"}});
    rudy["academic_bbox_guard_points"] = std::to_string(guard_points);
    rudy["metrics"] =
        D::Encode({{"nets", std::to_string(rows.size())},
                   {"pins", std::to_string(2 * rows.size() + guard_points)}});
    Write(work / "inputs/rudy_input.json", rudy, "nets",
          [&](std::ostream &out) {
            out << '[';
            bool first = true;
            for (auto index : schedule_order) {
              Stop(cancelled);
              const auto &s = rows[index];
              if (!first)
                out << ',';
              first = false;
              std::vector<std::string> p{Point(s.sx, s.sy), Point(s.tx, s.ty)};
              if (s.sy == s.ty)
                p.push_back(Point((s.sx + s.tx) / 2.0, s.sy + 0.5));
              out << D::Encode(
                  {{"id", JsonQuote("chimew-" + s.id)}, {"pins", Array(p)}});
            }
            out << ']';
          });
    auto bank_header = Identity("emuflow.chimew-bank-channel-input/v2",
                                "source-qualified-physical-bank-channel-v1");
    bank_header["coordinate_system"] = JsonQuote("physical-site-xy");
    bank_header["cost_quantization_per_site"] = "1000";
    auto bank_provenance = Provenance();
    bank_provenance["architecture_sha256"] = JsonQuote(architecture_hash);
    bank_provenance["grouping_sha256"] = JsonQuote(expected_grouping_hash);
    bank_header["provenance"] = D::Encode(bank_provenance);
    bank_header["domains"] = Array(domains);
    O bank_metrics{{"groups", std::to_string(grouped.size())},
                   {"signals", std::to_string(rows.size())},
                   {"fanins", std::to_string(rows.size())},
                   {"bank_pairs", std::to_string(banks.size())},
                   {"channels", std::to_string(channels)}};
    auto header_metrics = bank_metrics;
    header_metrics["timing_guard_fixed_lanes"] =
        std::to_string(fixed_lanes.size());
    bank_header["metrics"] = D::Encode(header_metrics);
    bank_metrics["bidirectional_bundles"] = "0";
    const auto normalized =
        D::Encode({{"design", JsonQuote(design)},
                   {"platform", JsonQuote(platform)},
                   {"cost_scale", "1000"},
                   {"provenance", D::Encode(bank_provenance)},
                   {"domains", Array(domains)},
                   {"metrics", D::Encode(bank_metrics)}});
    Hash bank_hash, group_hash, logical_hash;
    O bank_logical = bank_header;
    bank_logical["bank_pairs"] = "";
    bank_logical["groups"] = "";
    logical_hash.Add("{");
    bool first_field = true;
    for (const auto &field : bank_logical) {
      if (!first_field)
        logical_hash.Add(",");
      first_field = false;
      logical_hash.Add(JsonQuote(field.first) + ":");
      if (field.first == "bank_pairs") {
        logical_hash.Add("[");
        for (std::size_t i = 0; i < banks.size(); ++i) {
          if (i)
            logical_hash.Add(",");
          logical_hash.Add(banks[i]);
          bank_hash.Add(banks[i] + "\n");
        }
        logical_hash.Add("]");
      } else if (field.first == "groups") {
        logical_hash.Add("[");
        bool first = true;
        for (const auto &group : grouped) {
          Stop(cancelled);
          if (!first)
            logical_hash.Add(",");
          first = false;
          const auto text = GroupRecord(group.first, group.second);
          logical_hash.Add(text);
          group_hash.Add(text + "\n");
        }
        logical_hash.Add("]");
      } else
        logical_hash.Add(field.second);
    }
    logical_hash.Add("}");
    const auto bank_digest = bank_hash.Finish(),
               group_digest = group_hash.Finish(),
               logical_digest = logical_hash.Finish();
    auto store = [&] {
      SqliteArtifactWriter writer(
          (work / "inputs/bank_channel_input.sqlite").string(),
          "chimew-bank-input-store-v1", sources);
      auto &db = writer.database();
      db.Execute(
          "PRAGMA application_id=0x454d5543;PRAGMA user_version=1;CREATE TABLE "
          "metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT "
          "ROWID;CREATE TABLE groups(seq INTEGER PRIMARY KEY,group_id TEXT NOT "
          "NULL UNIQUE,record_json TEXT NOT NULL) WITHOUT ROWID;CREATE TABLE "
          "banks(seq INTEGER PRIMARY KEY,bank_id TEXT NOT NULL "
          "UNIQUE,first_channel INTEGER NOT NULL,record_json TEXT NOT "
          "NULL,CHECK(first_channel>=0)) WITHOUT ROWID;BEGIN");
      {
        auto add_bank = db.Prepare("INSERT INTO banks VALUES(?,?,?,?)");
        I channel = 0;
        for (std::size_t i = 0; i < banks.size(); ++i) {
          D bank(banks[i]);
          add_bank.BindInteger(1, i);
          add_bank.BindText(2, bank.Text("id"));
          add_bank.BindInteger(3, channel);
          add_bank.BindText(4, banks[i]);
          add_bank.Execute();
          add_bank.Reset();
          channel += D::Array(bank.Token("channels"), 65536).size();
        }
        Check(channel == channels, "bank channel coverage differs");
        auto add_group = db.Prepare("INSERT INTO groups VALUES(?,?,?)");
        I ordinal = 0;
        for (const auto &group : grouped) {
          Stop(cancelled);
          const auto record = GroupRecord(group.first, group.second);
          const auto id = "chimew-" + std::get<0>(group.first) + "-" +
                          std::get<1>(group.first) + "-group-" +
                          std::to_string(std::get<2>(group.first));
          add_group.BindInteger(1, ordinal++);
          add_group.BindText(2, id);
          add_group.BindText(3, record);
          add_group.Execute();
          add_group.Reset();
        }
        auto add_meta = db.Prepare("INSERT INTO metadata VALUES(?,?)");
        for (const auto &m : O{{"header", D::Encode(bank_header)},
                               {"problem_header", normalized},
                               {"banks_sha256", JsonQuote(bank_digest)},
                               {"groups_sha256", JsonQuote(group_digest)},
                               {"logical_sha256", JsonQuote(logical_digest)}}) {
          add_meta.BindText(1, m.first);
          add_meta.BindText(2, m.second);
          add_meta.Execute();
          add_meta.Reset();
        }
        db.Execute("COMMIT");
      }
      return writer.Commit();
    }();
    const auto store_hash = Sha256File(store, cancelled),
               store_name = "bank_channel_input." + store_hash + ".sqlite";
    fs::rename(store.path(), work / "inputs" / store_name);
    WriteSmall(
        work / "inputs/bank_channel_input.json",
        {{"schema", JsonQuote("emuflow.chimew-bank-channel-storage/v1")},
         {"logical_schema", bank_header.at("schema")},
         {"artifact", JsonQuote(store_name)},
         {"application_id", std::to_string(0x454d5543)},
         {"user_version", "1"},
         {"store_bytes", std::to_string(store.generation().bytes)},
         {"store_sha256", JsonQuote(store_hash)},
         {"header", D::Encode(bank_header)},
         {"problem_header", normalized},
         {"bank_pairs", D::Encode({{"records", std::to_string(banks.size())},
                                   {"sha256", JsonQuote(bank_digest)}})},
         {"groups", D::Encode({{"records", std::to_string(grouped.size())},
                               {"sha256", JsonQuote(group_digest)}})},
         {"logical_sha256", JsonQuote(logical_digest)}});
    D vb(virtual_board), wiring_source(vb.Token("source"));
    std::vector<std::string> boxes;
    for (const auto &fpga : fpga_ids) {
      auto box = bounds.at(fpga);
      boxes.push_back(
          D::Encode({{"fpga", JsonQuote(fpga)},
                     {"y_min", std::to_string(box[2])},
                     {"y_max", std::to_string(std::max(box[2] + 1, box[3]))}}));
    }
    auto electrical_map =
        Identity("emuflow.chimew-electrical-channel-map/v3",
                 "source-qualified-boarddb-electrical-channel-map-v1");
    electrical_map["fixed_wiring"] = D::Encode(
        {{"board_sha256", JsonQuote(Digest(virtual_board))},
         {"inventory_sha256", wiring_source.Token("inventory_sha256")},
         {"wiring_table_sha256",
          wiring_source.Has("wiring_table_sha256")
              ? wiring_source.Token("wiring_table_sha256")
              : "null"}});
    electrical_map["provenance"] = D::Encode(
        {{"producer", JsonQuote("fixed-hp-bank-lvds-wiring-v1")},
         {"producer_version", JsonQuote("1")},
         {"boarddb_sha256", JsonQuote(Sha256File(*board_ref, cancelled))},
         {"package_pin_inventory_sha256",
          JsonQuote(Sha256File(*inventory_ref, cancelled))}});
    electrical_map["fpga_y_bounds"] = Array(boxes);
    electrical_map["channels"] = Array(electrical);
    electrical_map["metrics"] =
        D::Encode({{"channels", std::to_string(channels)},
                   {"package_pins", std::to_string(4 * channels)},
                   {"concrete_lanes", std::to_string(channels)}});
    WriteSmall(work / "inputs/electrical_map.json", electrical_map);
    O artifacts;
    for (const auto *label : {"crossings", "positions", "rudy_input",
                              "bank_channel_input", "electrical_map"})
      artifacts[label] =
          D::Encode(Record(work / "inputs" / (std::string(label) + ".json"),
                           target / "inputs" / (std::string(label) + ".json")));
    artifacts["schedule"] =
        D::Encode(Record(schedule_ref->path(), schedule_ref->path(),
                         "tdm-schedule-selector-v4"));
    artifacts["bank_channel_input_store"] = D::Encode(
        Record(work / "inputs" / store_name, target / "inputs" / store_name,
               "chimew-bank-input-store-v1"));
    O source_paths{{"routing", JsonQuote(route_ref->path())},
                   {"placement", JsonQuote(final_placement.string())},
                   {"netlist", JsonQuote(original_ref->path())},
                   {"architecture", JsonQuote(architecture_ref->path())},
                   {"package_pins", JsonQuote(inventory_ref->path())}};
    if (timing_ref)
      source_paths["timing_paths"] = JsonQuote(timing_ref->path());
    std::set<Lane> before_lanes;
    for (const auto &s : rows)
      before_lanes.insert(s.lane_key());
    auto report = Identity("emuflow.xilinx-chimew-lookahead/v1", producer);
    report["status"] = JsonQuote("pass");
    report["implementation_id"] =
        JsonQuote("native-chimew-lookahead-preparation-v1");
    report["qualification"] =
        JsonQuote("xilinx-placed-lookahead+fixed-lvds-wiring");
    report["timing_guard"] = D::Encode(
        {{"scope", JsonQuote("EmuFlow extension, not a Chimew paper claim")},
         {"protected_entries", std::to_string(guarded_entries)},
         {"protected_lane_groups", std::to_string(guarded_lanes)}});
    report["metrics"] = D::Encode(
        {{"signals", std::to_string(rows.size())},
         {"placed_boundary_endpoints", std::to_string(forwarded)},
         {"predicted_sll_crossings", std::to_string(total_crossings)},
         {"groups", std::to_string(grouped.size())},
         {"fixed_package_pins", std::to_string(4 * channels)},
         {"tdm_groups_before_chimew", std::to_string(before_lanes.size())}});
    report["artifacts"] = D::Encode(artifacts);
    report["sources"] = D::Encode(source_paths);
    if (weighted) {
      I weighted_signals = 0;
      for (const auto &s : rows)
        weighted_signals += s.weight > 1;
      report["timing_weighting"] = D::Encode(
          {{"provider", JsonQuote("partition-projected-sta-criticality-v1")},
           {"source_sha256", JsonQuote(Sha256File(*timing_ref, cancelled))},
           {"weighted_signals", std::to_string(weighted_signals)},
           {"maximum_weight", JsonRecord(maximum_weight).Encode()},
           {"weight_scale", JsonRecord(weight_scale).Encode()},
           {"path_scope", JsonQuote(path_scope)},
           {"exact_path_hops", std::to_string(exact_hops)},
           {"whole_net_fallbacks", std::to_string(fallbacks)}});
    }
    WriteSmall(work / "chimew-lookahead-report.json", report);
  }
};
} // namespace
StageResult
PrepareChimewLookahead(const ArtifactRef &request, const std::string &directory,
                       const volatile std::sig_atomic_t *cancelled) {
  Lookahead lookahead(cancelled);
  std::optional<Workspace> work;
  const auto output = fs::absolute(directory);
  return RunStage(
      "phase6.prepare-chimew-lookahead",
      [&] {
        Check(request.schema() == "chimew-lookahead-input-v1",
              "request role disagrees");
        lookahead.Read(request);
        Check(!fs::exists(output) && !fs::is_symlink(output),
              "output directory already exists");
        fs::create_directories(output.parent_path());
        work.emplace(output.parent_path());
        return lookahead.sources;
      },
      [&](const auto &sources) {
        lookahead.GroupSignals();
        lookahead.FixedChannels();
        lookahead.Publish(work->root, output);
        Stop(cancelled);
        PublishNewDirectory(work->root.string(), output.string(), sources);
      },
      [&] {
        return std::vector<ArtifactRef>{ArtifactRef::Bind(
            (output / "chimew-lookahead-report.json").string(),
            "chimew-lookahead-report-v1")};
      });
}
} // namespace emuflow::core
