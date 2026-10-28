#include "physical/board_timing.hpp"
#include "common/bounded_json.hpp"
#include <cmath>
#include <map>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
using Key = std::tuple<std::string, std::string, std::string>;
using Array = std::vector<JsonRecord>;
using Object = std::map<std::string, JsonRecord>;
void Check(bool ok, const std::string &message) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "physical board timing: " + message);
}
bool Near(double a, double b) {
  return std::abs(a - b) <=
         std::max(1e-9, 1e-9 * std::max(std::abs(a), std::abs(b)));
}
struct Link {
  double delay = 0, frequency = 0, alpha = 0, beta = 0;
  std::int64_t latency = 0, lo = 0, hi = 0, quantum = 0;
  bool affine = false;
  std::string qualification = "model-only";
};
} // namespace
PhysicalBoardTiming
ResolvePhysicalBoardTiming(const ArtifactRef &board,
                           const std::optional<ArtifactRef> &measured) {
  BoundedJson platform(BoundedJson::Read(board));
  Check(platform.Text("schema") == "emuflow.boarddb/v1",
        "BoardDB schema disagrees");
  std::map<Key, Link> links;
  std::set<std::string> ids;
  for (const auto &token : BoundedJson::Array(platform.Token("links"))) {
    BoundedJson raw(token);
    Link value;
    const auto id = raw.Text("id");
    Check(ids.insert(id).second, "duplicate link");
    value.frequency = raw.Number("fabric_clock_mhz");
    value.latency = raw.Integer("latency_cycles");
    Check(value.frequency > 0 && value.latency >= 0,
          "invalid link frequency/latency");
    value.delay = value.latency * (1000.0 / value.frequency);
    if (raw.Has("tdm_delay_model") && raw.Token("tdm_delay_model") != "null") {
      BoundedJson model(raw.Token("tdm_delay_model"));
      Check(model.Text("model") == "linear-ratio-v1", "unknown ratio model");
      value.affine = true;
      value.alpha = model.Number("ratio_coefficient_ns");
      value.beta = model.Number("intercept_ns");
      value.lo = model.Integer("min_ratio");
      value.hi = model.Integer("max_ratio");
      value.quantum = model.Integer("ratio_quantum");
      Check(value.alpha >= 0 && value.beta >= 0 && value.lo > 0 &&
                value.hi >= value.lo && value.quantum > 0 &&
                value.lo % value.quantum == 0 && value.hi % value.quantum == 0,
            "invalid ratio model");
      value.delay = value.alpha * value.lo + value.beta;
    }
    const auto ends = BoundedJson::Array(raw.Token("endpoints"));
    Check(ends.size() == 2, "invalid endpoints");
    const auto from = DecodeJsonString(ends[0]), to = DecodeJsonString(ends[1]),
               direction = raw.Text("direction");
    Check(from != to, "self link");
    Check(direction == "unidirectional" || direction == "full_duplex" ||
              direction == "half_duplex",
          "invalid direction");
    links.emplace(Key{id, from, to}, value);
    if (direction != "unidirectional")
      links.emplace(Key{id, to, from}, value);
  }
  bool final = !links.empty();
  if (measured) {
    BoundedJson document(BoundedJson::Read(*measured));
    BoundedJson identity(platform.Token("platform"));
    Check(document.Text("schema") == "emuflow.board-link-timing/v1" &&
              document.Text("status") == "pass" &&
              document.Text("platform") == identity.Text("name") &&
              document.Text("measurement_scope") ==
                  "tx-transport-stage-to-rx-transport-stage",
          "measured library identity/scope disagrees");
    std::set<Key> seen;
    for (const auto &token : BoundedJson::Array(document.Token("links"))) {
      BoundedJson row(token);
      Key key{row.Text("link"), row.Text("from"), row.Text("to")};
      auto found = links.find(key);
      Check(found != links.end() && seen.insert(key).second,
            "unknown or repeated directed link");
      auto &link = found->second;
      const auto delay = row.Number("delay_bound_ns");
      Check(delay >= 0 &&
                Near(row.Number("fabric_clock_mhz"), link.frequency) &&
                row.Integer("latency_cycles") == link.latency,
            "measured link model disagrees");
      const auto qualification = row.Text("qualification");
      BoundedJson source(row.Token("source"));
      const auto kind = source.Text("kind");
      Check(!source.Text("reference").empty(), "empty timing source");
      Check((qualification == "model-only" && kind == "boarddb-model") ||
                (qualification == "characterized-upper-bound" &&
                 kind == "vendor-characterization") ||
                (qualification == "measured-upper-bound" &&
                 kind == "hardware-measurement"),
            "measurement qualification disagrees");
      if (kind == "hardware-measurement")
        Check(source.Integer("observations") > 0,
              "missing measurement observations");
      if (link.affine) {
        BoundedJson model(row.Token("tdm_delay_model"));
        Check(qualification == "model-only" &&
                  row.Integer("reference_tdm_ratio") == link.lo &&
                  Near(delay, link.delay) &&
                  model.Text("model") == "linear-ratio-v1" &&
                  model.Number("ratio_coefficient_ns") == link.alpha &&
                  model.Number("intercept_ns") == link.beta &&
                  model.Integer("min_ratio") == link.lo &&
                  model.Integer("max_ratio") == link.hi &&
                  model.Integer("ratio_quantum") == link.quantum,
              "ratio timing source disagrees");
      } else
        Check(!row.Has("tdm_delay_model"), "unexpected ratio model");
      link.delay = delay;
      link.qualification = qualification;
      final &= qualification == "measured-upper-bound";
    }
    Check(seen.size() == links.size() &&
              document.Boolean("final_link_timing_signoff") == final,
          "measured link coverage/signoff disagrees");
  } else
    final = false;
  Array events, coverage;
  for (const auto &entry : links) {
    const auto &link = entry.second;
    Array row{std::get<0>(entry.first), std::get<1>(entry.first),
              std::get<2>(entry.first), link.delay};
    if (link.affine) {
      row.emplace_back(link.alpha);
      row.emplace_back(link.beta);
      row.emplace_back(link.lo);
      row.emplace_back(link.hi);
      row.emplace_back(link.quantum);
    } else
      for (int i = 0; i < 5; ++i)
        row.emplace_back();
    events.emplace_back(std::move(row));
    coverage.emplace_back(Object{{"link", std::get<0>(entry.first)},
                                 {"from", std::get<1>(entry.first)},
                                 {"to", std::get<2>(entry.first)},
                                 {"delay_bound_ns", link.delay},
                                 {"qualification", link.qualification}});
  }
  board.RequireUnchanged();
  if (measured)
    measured->RequireUnchanged();
  return {JsonRecord(events).Encode(), JsonRecord(coverage).Encode(), final};
}
} // namespace emuflow::core
