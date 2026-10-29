#include "board/model.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "ir/resource_view.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <regex>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "BoardDB: " + why);
}
std::string Get(const O &doc, const std::string &key,
                const std::string &fallback = "null") {
  const auto p = doc.find(key);
  return p == doc.end() ? fallback : p->second;
}
bool Present(const O &doc, const std::string &key) {
  return Get(doc, key) != "null";
}
std::string Array(const std::vector<std::string> &rows) {
  std::string text = "[";
  for (const auto &row : rows) {
    if (text.size() > 1)
      text += ',';
    text += row;
  }
  return text + ']';
}
// Python's str.strip whitespace domain, preserving the original identity.
bool Blank(const std::string &text) {
  if (text.empty())
    return true;
  for (std::size_t i = 0; i < text.size();) {
    const auto b = static_cast<unsigned char>(text[i++]);
    std::uint32_t cp = b;
    unsigned n = b < 128 ? 0 : b < 224 ? 1 : b < 240 ? 2 : 3;
    if (n)
      cp = b & ((1u << (6 - n)) - 1);
    for (unsigned k = 0; k < n; ++k)
      cp = (cp << 6) | (static_cast<unsigned char>(text[i++]) & 63);
    if (!((cp >= 9 && cp <= 13) || (cp >= 28 && cp <= 32) || cp == 0x85 ||
          cp == 0xa0 || cp == 0x1680 || (cp >= 0x2000 && cp <= 0x200a) ||
          cp == 0x2028 || cp == 0x2029 || cp == 0x202f || cp == 0x205f ||
          cp == 0x3000))
      return false;
  }
  return true;
}
std::string Name(const O &doc, const std::string &key,
                 const std::string &context) {
  const auto text = DecodeJsonString(Get(doc, key));
  Check(!Blank(text), context + "." + key + ": expected a non-empty string");
  return text;
}
I Integer(const O &doc, const std::string &key, I minimum,
          const std::string &context, const std::string &fallback = "null") {
  const auto context_message = context + "." + key + ": expected " +
                               (minimum ? "a positive" : "a non-negative") +
                               " integer";
  I value = 0;
  try {
    value = D::IntegerToken(Get(doc, key, fallback));
  } catch (const StageError &) {
    Check(false, context_message);
  }
  Check(value >= minimum, context_message);
  return value;
}
double Number(const O &doc, const std::string &key, const std::string &context,
              bool positive = true) {
  const auto value = D("{\"n\":" + Get(doc, key) + "}").Number("n");
  Check(std::isfinite(value) && (positive ? value > 0 : value >= 0),
        context + "." + key + ": expected a finite " +
            (positive ? "positive" : "non-negative") + " number");
  return value;
}
std::string Real(double value) {
  Check(std::isfinite(value),
        "derived board rate exceeds finite number domain");
  return JsonRecord(value).Encode();
}
I Product(I a, I b, const std::string &context) {
  Check(a >= 0 && b >= 0 && (!b || a <= INT64_MAX / b),
        context + ": capacity exceeds integer domain");
  return a * b;
}
void Add(I &a, I b, const std::string &context) {
  Check(b >= 0 && a <= INT64_MAX - b,
        context + ": inventory exceeds integer domain");
  a += b;
}
std::string Ratio(const std::string &text) {
  D doc(text);
  try {
    doc.Keys({"model", "intercept_ns", "ratio_coefficient_ns", "min_ratio",
              "max_ratio", "ratio_quantum"});
  } catch (const StageError &) {
    Check(false, "tdm_delay_model: unexpected or missing fields");
  }
  auto f = doc.Fields();
  Check(doc.Text("model") == "linear-ratio-v1",
        "tdm_delay_model: expected linear-ratio-v1");
  const auto lo = Integer(f, "min_ratio", 1, "tdm_delay_model"),
             hi = Integer(f, "max_ratio", 1, "tdm_delay_model"),
             q = Integer(f, "ratio_quantum", 1, "tdm_delay_model");
  Check(lo <= hi && lo % q == 0 && hi % q == 0,
        "tdm_delay_model: ratio bounds must be ordered multiples of "
        "ratio_quantum");
  f["intercept_ns"] = Real(Number(f, "intercept_ns", "tdm_delay_model", false));
  f["ratio_coefficient_ns"] =
      Real(Number(f, "ratio_coefficient_ns", "tdm_delay_model", false));
  return D::Encode(f);
}
struct Builder {
  std::set<std::string> fpga_ids, link_ids, service_ids;
  std::set<std::pair<std::string, std::string>> connectors, pins;
  std::map<std::string, I> inventory, used_banks;
  std::string model;
  std::vector<std::string> Fpgas(const std::string &token) {
    std::vector<std::string> result;
    for (const auto &row : D::Array(token, 256)) {
      const auto f = D(row).Fields();
      const auto context = "fpgas[" + std::to_string(result.size()) + "]";
      const auto id = Name(f, "id", context), part = Name(f, "part", context);
      Check(fpga_ids.insert(id).second, context + ".id: duplicate FPGA");
      const auto limit = Number(f, "utilization_limit", context);
      Check(limit <= 1,
            context + ".utilization_limit: expected 0 < value <= 1");
      const auto resources = D(Get(f, "capacity")).Fields();
      Check(!resources.empty(),
            context + ".capacity: expected at least one resource");
      O capacity, effective;
      for (const auto &entry : resources) {
        Check(std::find(kResourceFields.begin(), kResourceFields.end(),
                        entry.first) != kResourceFields.end(),
              context + ".capacity: unknown resource " + entry.first);
        const auto count = D::IntegerToken(entry.second);
        Check(count >= 0,
              context + ".capacity: expected a non-negative integer");
        const double amount = std::floor(double(count) * limit);
        Check(amount >= 0 && static_cast<long double>(amount) <= INT64_MAX,
              context + ".effective_capacity exceeds integer domain");
        capacity[entry.first] = std::to_string(count);
        effective[entry.first] = std::to_string(I(amount));
      }
      O out{{"id", JsonQuote(id)},
            {"part", JsonQuote(part)},
            {"utilization_limit", Real(limit)},
            {"capacity", D::Encode(capacity)},
            {"effective_capacity", D::Encode(effective)}};
      for (const auto *key : {"hp_io_bank_count", "hd_io_bank_count"})
        if (Present(f, key)) {
          const auto count = Integer(f, key, 0, context);
          out[key] = std::to_string(count);
          if (std::string(key) == "hp_io_bank_count")
            inventory[id] = count;
        }
      result.push_back(D::Encode(out));
    }
    Check(!result.empty(), "platform.fpgas: expected a non-empty array");
    return result;
  }
  std::string Bindings(const std::string &token,
                       const std::set<std::string> &ends, I lanes,
                       const std::string &mode, const std::string &context) {
    const auto rows = D::Array(token);
    Check(rows.empty() || mode == "serial",
          context + ".endpoint_bindings: require serial mode");
    std::map<std::string, std::string> sorted;
    for (const auto &row : rows) {
      const auto f = D(row).Fields();
      const auto fpga = Name(f, "fpga", context),
                 connector = Name(f, "connector", context),
                 mgt = Name(f, "mgt", context);
      Check(ends.count(fpga) && !sorted.count(fpga),
            context + ": FPGA must cover one link endpoint exactly once");
      Check(connectors.insert({fpga, connector}).second,
            context + ": connector is already used by another link");
      std::map<I, std::string> ordered;
      for (const auto &lane : D::Array(Get(f, "lanes"))) {
        const auto l = D(lane).Fields();
        const auto id = Integer(l, "lane", 0, context);
        Check(!ordered.count(id),
              context + ".lane: expected a unique non-negative integer");
        O out{{"lane", std::to_string(id)}};
        std::set<std::string> local;
        for (const auto *direction : {"tx_package_pins", "rx_package_pins"}) {
          const auto pair = D(Get(l, direction)).Fields();
          O normalized;
          for (const auto *polarity : {"p", "n"}) {
            const auto pin = Name(pair, polarity, context);
            Check(local.insert(pin).second,
                  context + ": differential package pins must be distinct");
            Check(pins.insert({fpga, pin}).second,
                  context + ": package pin is already used");
            normalized[polarity] = JsonQuote(pin);
          }
          out[direction] = D::Encode(normalized);
        }
        ordered.emplace(id, D::Encode(out));
      }
      Check(ordered.size() == std::uint64_t(lanes),
            context + ".lanes: must cover every physical lane exactly once");
      I next = 0;
      std::vector<std::string> emitted;
      for (const auto &lane : ordered) {
        Check(lane.first == next++,
              context + ".lanes: must cover every physical lane exactly once");
        emitted.push_back(lane.second);
      }
      sorted.emplace(fpga, D::Encode({{"fpga", JsonQuote(fpga)},
                                      {"connector", JsonQuote(connector)},
                                      {"mgt", JsonQuote(mgt)},
                                      {"lanes", Array(emitted)}}));
    }
    Check(sorted.empty() || sorted.size() == ends.size(),
          context +
              ".endpoint_bindings: must cover both link endpoints exactly");
    std::vector<std::string> result;
    for (const auto &row : sorted)
      result.push_back(row.second);
    return Array(result);
  }
  std::vector<std::string> Links(const std::string &token) {
    std::vector<std::string> result;
    for (const auto &row : D::Array(token)) {
      const auto f = D(row).Fields();
      const auto context = "links[" + std::to_string(result.size()) + "]";
      const auto id = Name(f, "id", context),
                 direction = Name(f, "direction", context),
                 mode = Name(f, "mode", context);
      Check(link_ids.insert(id).second, context + ".id: duplicate link");
      const auto ends = D::Array(Get(f, "endpoints"), 2);
      Check(ends.size() == 2, context + ".endpoints: expected two FPGA IDs");
      const auto a = DecodeJsonString(ends[0]), b = DecodeJsonString(ends[1]);
      Check(a != b, context + ".endpoints: self-links are not allowed");
      Check(fpga_ids.count(a) && fpga_ids.count(b),
            context + ".endpoints: unknown FPGA IDs");
      Check(
          std::set<std::string>{"full_duplex", "half_duplex", "unidirectional"}
              .count(direction),
          context + ".direction: invalid direction");
      Check(std::set<std::string>{"source_synchronous", "parallel", "serial",
                                  "abstract"}
                .count(mode),
            context + ".mode: invalid mode");
      const auto sharing =
          DecodeJsonString(Get(f, "capacity_sharing", "\"per_direction\""));
      Check(sharing == "per_direction" || sharing == "shared_bidirectional",
            context + ".capacity_sharing: invalid sharing");
      Check(sharing != "shared_bidirectional" || direction == "full_duplex",
            context + ".capacity_sharing: shared_bidirectional requires "
                      "full_duplex direction");
      const auto lanes = Integer(f, "data_lanes_per_direction", 1, context),
                 latency = Integer(f, "latency_cycles", 0, context),
                 width = Integer(f, "payload_bits_per_lane_per_cycle", 1,
                                 context, "1");
      const auto frequency = Number(f, "fabric_clock_mhz", context);
      Check(mode == "serial" || width == 1,
            context + ".payload_bits_per_lane_per_cycle: values greater than "
                      "one require serial mode");
      const auto bits = Product(lanes, width, context);
      O out{{"id", JsonQuote(id)},
            {"endpoints", Array({JsonQuote(a), JsonQuote(b)})},
            {"direction", JsonQuote(direction)},
            {"mode", JsonQuote(mode)},
            {"capacity_sharing", JsonQuote(sharing)},
            {"data_lanes_per_direction", std::to_string(lanes)},
            {"fabric_clock_mhz", Real(frequency)},
            {"latency_cycles", std::to_string(latency)},
            {"raw_bits_per_second_per_direction",
             Real(double(bits) * frequency * 1000000.0)}};
      if (width != 1) {
        out["payload_bits_per_lane_per_cycle"] = std::to_string(width);
        out["transport_bits_per_cycle_per_direction"] = std::to_string(bits);
      }
      I banks = 0;
      if (Present(f, "bank_count") || Present(f, "wires_per_bank")) {
        banks = Integer(f, "bank_count", 1, context);
        const auto wires = Integer(f, "wires_per_bank", 1, context);
        Check(model != "xcvu19p-hp-lvds-cable-v1" || wires <= 24,
              context + ".wires_per_bank: the XCVU19P HP bank has at most 24 "
                        "differential pairs");
        Check(sharing == "shared_bidirectional" && mode != "serial" &&
                  width == 1 && lanes == Product(banks, wires, context),
              context + ": bank capacity requires shared_bidirectional, one "
                        "bit per wire, and data_lanes_per_direction = "
                        "bank_count * wires_per_bank");
        out["bank_count"] = std::to_string(banks);
        out["wires_per_bank"] = std::to_string(wires);
      }
      if (Present(f, "tdm_delay_model"))
        out["tdm_delay_model"] = Ratio(Get(f, "tdm_delay_model"));
      if (Present(f, "cable_count") || Present(f, "hp_banks_per_cable")) {
        const auto cables = Integer(f, "cable_count", 1, context),
                   per = Integer(f, "hp_banks_per_cable", 1, context);
        Check(banks == Product(cables, per, context),
              context + ": bank_count must equal cable_count * "
                        "hp_banks_per_cable (corresponding bank groups, or HP "
                        "banks used at each endpoint)");
        out["cable_count"] = std::to_string(cables);
        out["hp_banks_per_cable"] = std::to_string(per);
        Add(used_banks[a], banks, context);
        Add(used_banks[b], banks, context);
      }
      if (Present(f, "max_line_rate_gbps_per_lane")) {
        const auto rate = Number(f, "max_line_rate_gbps_per_lane", context);
        Check(mode == "serial",
              context + ".max_line_rate_gbps_per_lane: expected a positive "
                        "number for a serial link");
        Check(frequency * double(width) / 1000.0 <= rate * (1.0 + 1e-9),
              context +
                  ": configured user-side rate exceeds maximum line rate");
        out["max_line_rate_gbps_per_lane"] = Real(rate);
      }
      const auto bindings = Bindings(Get(f, "endpoint_bindings", "[]"), {a, b},
                                     lanes, mode, context);
      if (bindings != "[]")
        out["endpoint_bindings"] = bindings;
      result.push_back(D::Encode(out));
    }
    for (const auto &used : used_banks) {
      Check(inventory.count(used.first),
            "FPGA " + used.first +
                ": cable model requires hp_io_bank_count inventory");
      Check(used.second <= inventory.at(used.first),
            "FPGA " + used.first + ": cables use " +
                std::to_string(used.second) +
                " HP banks, exceeding inventory " +
                std::to_string(inventory.at(used.first)));
    }
    return result;
  }
  std::string Services(const std::string &token) {
    const auto f = D(token).Fields();
    std::map<std::string, std::string> clocks, resets;
    for (const auto &row : D::Array(Get(f, "clocks", "[]"))) {
      const auto c = D(row).Fields();
      const std::string context = "board_services.clocks";
      const auto id = Name(c, "id", context);
      Check(service_ids.insert(id).second, context + ".id: duplicate service");
      O out;
      for (const auto *key :
           {"id", "signal", "kind", "destination", "qualification"})
        out[key] = JsonQuote(Name(c, key, context));
      const auto fq = Name(c, "frequency_qualification", context),
                 binding = Name(c, "binding_status", context);
      Check(std::set<std::string>{"fixed", "documented_default", "configured"}
                    .count(fq) &&
                std::set<std::string>{"logical_source_without_package_pins",
                                      "package_bound"}
                    .count(binding),
            context + ": invalid clock contract");
      out["frequency_mhz"] = Real(Number(c, "frequency_mhz", context));
      out["count"] = std::to_string(Integer(c, "count", 1, context));
      out["frequency_qualification"] = JsonQuote(fq);
      out["binding_status"] = JsonQuote(binding);
      clocks.emplace(id, D::Encode(out));
    }
    for (const auto &row : D::Array(Get(f, "resets", "[]"))) {
      const auto r = D(row).Fields();
      const std::string context = "board_services.resets";
      const auto id = Name(r, "id", context);
      Check(service_ids.insert(id).second, context + ".id: duplicate service");
      O out;
      for (const auto *key : {"id", "signal", "purpose", "qualification"})
        out[key] = JsonQuote(Name(r, key, context));
      const auto polarity = Name(r, "polarity", context),
                 binding = Name(r, "binding_status", context);
      Check(
          std::set<std::string>{"active_low", "active_high"}.count(polarity) &&
              std::set<std::string>{"logical_source_without_package_pins",
                                    "package_bound"}
                  .count(binding),
          context + ": invalid reset contract");
      out["polarity"] = JsonQuote(polarity);
      out["binding_status"] = JsonQuote(binding);
      resets.emplace(id, D::Encode(out));
    }
    if (clocks.empty() && resets.empty())
      return "null";
    std::vector<std::string> a, b;
    for (const auto &c : clocks)
      a.push_back(c.second);
    for (const auto &r : resets)
      b.push_back(r.second);
    return D::Encode({{"clocks", Array(a)}, {"resets", Array(b)}});
  }
  std::string Run(const std::string &text) {
    D doc(text);
    Check(doc.Text("schema") == "emuflow.boarddb/v1",
          "platform.schema: expected emuflow.boarddb/v1");
    const auto meta = D(doc.Token("platform")).Fields();
    const auto name = Name(meta, "name", "platform"),
               kind = Name(meta, "kind", "platform");
    Check(kind == "virtual" || kind == "hardware",
          "platform.kind: expected virtual or hardware");
    O normalized{{"name", JsonQuote(name)},
                 {"kind", JsonQuote(kind)},
                 {"description", JsonQuote(DecodeJsonString(
                                     Get(meta, "description", "\"\"")))}};
    if (Present(meta, "virtual_board_model")) {
      model = Name(meta, "virtual_board_model", "platform");
      Check(model == "xcvu19p-hp-lvds-cable-v1" && kind == "virtual",
            "platform.virtual_board_model: unsupported virtual board model");
      normalized["virtual_board_model"] = JsonQuote(model);
    }
    if (Present(meta, "virtual_board_wiring")) {
      const auto wiring = Name(meta, "virtual_board_wiring", "platform");
      Check(model == "xcvu19p-hp-lvds-cable-v1" &&
                std::regex_match(wiring,
                                 std::regex("rapidwright_xcvu19p_(2fpga_p2p|"
                                            "4fpga_mesh)\\.wires\\.csv")),
            "platform.virtual_board_wiring: unsupported fixed wiring table");
      normalized["virtual_board_wiring"] = JsonQuote(wiring);
    }
    const auto fpgas = Fpgas(doc.Token("fpgas")),
               links = Links(doc.Token("links"));
    const auto services = Services(
        doc.Has("board_services") ? doc.Token("board_services") : "{}");
    O result{{"schema", JsonQuote("emuflow.boarddb/v1")},
             {"platform", D::Encode(normalized)},
             {"fpgas", Array(fpgas)},
             {"links", Array(links)}};
    if (services != "null")
      result["board_services"] = services;
    return D::Encode(result);
  }
};
} // namespace
std::string NormalizeBoard(const std::string &source) {
  return Builder{}.Run(source);
}
StageResult NormalizeBoardArtifact(const ArtifactRef &source,
                                   const std::string &output) {
  return RunStage(
      "board.normalize",
      [&] {
        Check(source.schema() == "boarddb-v1", "source schema disagrees");
        return std::vector<ArtifactRef>{source};
      },
      [&](const auto &refs) {
        const auto normalized = NormalizeBoard(D::Read(source));
        PublishTextArtifact(output, refs, [&](std::ostream &stream) {
          stream << normalized << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "boarddb-v1")};
      });
}

} // namespace emuflow::core
