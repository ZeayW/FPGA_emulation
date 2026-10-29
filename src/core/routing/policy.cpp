#include "routing/policy.hpp"
#include "common/bounded_json.hpp"
#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <tuple>
namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "route policy: " + why);
}
std::string Get(const O &values, const std::string &key,
                const std::string &fallback) {
  auto p = values.find(key);
  return p == values.end() ? fallback : p->second;
}
std::string Array(const std::set<std::string> &values) {
  std::vector<JsonRecord> out;
  for (const auto &v : values)
    out.emplace_back(v);
  return JsonRecord(out).Encode();
}
std::set<std::string> Names(const std::string &value) {
  std::set<std::string> out;
  for (const auto &v : D::Array(value))
    out.insert(DecodeJsonString(v));
  return out;
}
I Integer(const O &values, const std::string &key, I fallback) {
  return D::IntegerToken(Get(values, key, std::to_string(fallback)));
}
double Number(const std::string &raw) {
  return D("{\"v\":" + raw + "}").Number("v");
}
} // namespace
std::string NormalizeRoutePolicy(const std::string &board_value,
                                 const std::string &input,
                                 std::optional<I> frame,
                                 std::optional<I> iterations) {
  D board(board_value);
  const auto raw = input == "null" ? O{} : D(input).Fields();
  Check(raw.empty() || DecodeJsonString(Get(raw, "schema", "null")) ==
                           "emuflow.system-route-constraints/v1",
        "unsupported constraint schema");
  const I slots = frame ? *frame : Integer(raw, "frame_slots", 32),
          rounds =
              iterations ? *iterations : Integer(raw, "max_iterations", 20);
  Check(slots > 0 && rounds > 0,
        "frame_slots/max_iterations must be positive integers");
  const auto hops = Get(raw, "max_route_hops", "null");
  Check(hops == "null" || D::IntegerToken(hops) > 0,
        "max_route_hops must be positive");
  std::map<std::string, O> links;
  std::set<std::tuple<std::string, std::string, std::string>> arcs;
  std::set<std::tuple<I, I, I>> domains;
  for (const auto &token : D::Array(board.Token("links"), 65536)) {
    const auto link = D(token).Fields();
    const auto id = DecodeJsonString(link.at("id"));
    Check(links.emplace(id, link).second, "duplicate link identity");
    const auto ends = D::Array(link.at("endpoints"), 2);
    Check(ends.size() == 2, "invalid link endpoints");
    const auto a = DecodeJsonString(ends[0]), b = DecodeJsonString(ends[1]);
    arcs.emplace(id, a, b);
    const auto direction = DecodeJsonString(link.at("direction"));
    if (direction == "full_duplex" || direction == "half_duplex")
      arcs.emplace(id, b, a);
    if (Get(link, "tdm_delay_model", "null") != "null") {
      D model(link.at("tdm_delay_model"));
      Check(model.Text("model") == "linear-ratio-v1", "unknown delay model");
      const auto lo = model.Integer("min_ratio"),
                 hi = model.Integer("max_ratio"),
                 q = model.Integer("ratio_quantum");
      Check(q > 0 && lo > 0 && hi >= lo && lo % q == 0 && hi % q == 0 &&
                model.Number("intercept_ns") >= 0 &&
                model.Number("ratio_coefficient_ns") > 0,
            "invalid board delay model");
      domains.emplace(lo, hi, q);
    }
  }
  Check(domains.size() <= 1, "one common BoardDB TDM ratio domain is required");
  auto unavailable = Names(Get(raw, "unavailable_links", "[]")),
       sll = Names(Get(raw, "sll_links", "[]")),
       shared = Names(Get(raw, "shared_capacity_links", "[]"));
  for (const auto *names : {&unavailable, &sll, &shared})
    for (const auto &id : *names)
      Check(links.count(id), "unknown link in constraint catalog");
  O delays;
  for (const auto &entry : D(Get(raw, "link_delay_ns", "{}")).Fields()) {
    const auto value = Number(entry.second);
    Check(links.count(entry.first) && value >= 0, "invalid/unknown link delay");
    delays[entry.first] = JsonRecord(value).Encode();
  }
  std::map<std::string, std::map<std::string, O>> directed;
  for (const auto &link : D(Get(raw, "directed_link_delay_ns", "{}")).Fields())
    for (const auto &source : D(link.second).Fields())
      for (const auto &sink : D(source.second).Fields()) {
        const auto value = Number(sink.second);
        Check(arcs.count({link.first, source.first, sink.first}) && value >= 0,
              "invalid/unknown directed link delay");
        directed[link.first][source.first][sink.first] =
            JsonRecord(value).Encode();
      }
  const auto base =
      domains.empty() ? std::make_tuple(I(1), slots, I(8)) : *domains.begin();
  O result{{"schema", JsonQuote("emuflow.system-route-constraints/v1")},
           {"frame_slots", std::to_string(slots)},
           {"max_iterations", std::to_string(rounds)},
           {"max_route_hops", hops}};
  for (const auto &entry : std::map<std::string, I>{
           {"reroute_rounds", 8},
           {"tdm_ratio_quantum", std::get<2>(base)},
           {"tdm_min_ratio", std::get<0>(base)},
           {"tdm_max_ratio", std::min(slots, std::get<1>(base))}}) {
    const auto value = Integer(raw, entry.first, entry.second);
    Check(value >= 0, "negative route objective/ratio parameter");
    result[entry.first] = std::to_string(value);
  }
  for (const auto &entry : std::map<std::string, double>{{"lambda_load", 2.},
                                                         {"lambda_timing", 4.},
                                                         {"lambda_history", 1.},
                                                         {"lambda_tdm", .1}}) {
    const auto value =
        Number(Get(raw, entry.first, JsonRecord(entry.second).Encode()));
    Check(value >= 0, "negative route objective weight");
    result[entry.first] = JsonRecord(value).Encode();
  }
  const auto q = D::IntegerToken(result.at("tdm_ratio_quantum")),
             lo = D::IntegerToken(result.at("tdm_min_ratio")),
             hi = D::IntegerToken(result.at("tdm_max_ratio"));
  Check(q > 0 && lo > 0 && lo <= hi && hi <= slots, "invalid TDM ratio bounds");
  Check(lo % q == 0 || (domains.empty() && lo == 1),
        "minimum ratio is outside its quantum");
  Check(domains.empty() || (hi % q == 0 && lo >= std::get<0>(base) &&
                            hi <= std::get<1>(base) && q == std::get<2>(base)),
        "route ratio domain contradicts BoardDB");
  for (const auto *flag : {"tree_edge_sum_tdm", "hard_sll_capacity"}) {
    const auto value = Get(raw, flag, "false");
    Check(value == "true" || value == "false",
          "route policy boolean is invalid");
    result[flag] = value;
  }
  for (const auto &entry : links) {
    const auto &link = entry.second;
    if (Get(link, "capacity_sharing", JsonQuote("per_direction")) ==
        JsonQuote("shared_bidirectional"))
      shared.insert(entry.first);
    Check(Get(link, "bank_count", "null") == "null" || !sll.count(entry.first),
          "shared-bank FPGA link cannot be SLL");
    if (Get(link, "tdm_delay_model", "null") == "null")
      continue;
    D model(link.at("tdm_delay_model"));
    const auto reference =
        model.Number("intercept_ns") +
        model.Number("ratio_coefficient_ns") * model.Integer("min_ratio");
    const auto agrees = [&](double value) {
      return std::abs(value - reference) <=
             std::max(1e-9,
                      1e-9 * std::max(std::abs(value), std::abs(reference)));
    };
    if (delays.count(entry.first))
      Check(agrees(Number(delays.at(entry.first))),
            "fixed delay override conflicts with TDM ratio model");
    for (const auto &source : directed[entry.first])
      for (const auto &sink : source.second)
        Check(agrees(Number(sink.second)),
              "directed delay override conflicts with TDM ratio model");
  }
  O directed_json;
  for (const auto &link : directed)
    if (!link.second.empty()) {
      O sources;
      for (const auto &source : link.second)
        sources[source.first] = D::Encode(source.second);
      directed_json[link.first] = D::Encode(sources);
    }
  result["unavailable_links"] = Array(unavailable);
  result["sll_links"] = Array(sll);
  result["shared_capacity_links"] = Array(shared);
  result["link_delay_ns"] = D::Encode(delays);
  result["directed_link_delay_ns"] = D::Encode(directed_json);
  return D::Encode(result);
}
} // namespace emuflow::core
