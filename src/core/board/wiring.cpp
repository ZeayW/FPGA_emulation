#include "board/wiring.hpp"
#include "board/model.hpp"
#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/canonical_json.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <regex>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
namespace fs = std::filesystem;
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact, "fixed board wiring: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "fixed board wiring cancelled");
}
std::string Array(const std::vector<std::string> &rows) {
  std::string result = "[";
  for (const auto &r : rows) {
    if (result.size() > 1)
      result += ',';
    result += r;
  }
  return result + ']';
}
std::string Digest(const std::string &text) {
  Sha256 hash;
  hash.update(reinterpret_cast<const unsigned char *>(text.data()),
              text.size());
  std::string out;
  static const char *hex = "0123456789abcdef";
  for (const auto b : hash.finish()) {
    out += hex[b >> 4];
    out += hex[b & 15];
  }
  return out;
}
std::string Id(D &row) {
  const auto token = row.Token("id");
  return token[0] == '"' ? DecodeJsonString(token)
                         : std::to_string(D::IntegerToken(token));
}
struct Bank {
  std::string id;
  std::vector<std::string> pairs;
};
// RFC-style quoted fields, CRLF and embedded newlines. Malformed field counts,
// unfinished quoting and trailing data after a quote fail before publication.
std::vector<std::vector<std::string>> Csv(const ArtifactRef &source) {
  const auto text = D::Read(source);
  std::vector<std::vector<std::string>> rows;
  std::vector<std::string> row;
  std::string field;
  bool quoted = false, closed = false, started = false;
  auto finish_field = [&] {
    row.push_back(field);
    field.clear();
    closed = false;
    started = false;
  };
  auto finish_row = [&] {
    finish_field();
    Check(rows.size() < 65536, "CSV row count exceeds bound");
    rows.push_back(row);
    row.clear();
  };
  for (std::size_t i = 0; i < text.size(); ++i) {
    const char c = text[i];
    if (quoted) {
      if (c == '"') {
        if (i + 1 < text.size() && text[i + 1] == '"') {
          field += '"';
          ++i;
        } else {
          quoted = false;
          closed = true;
        }
      } else
        field += c;
      continue;
    }
    if (c == ',') {
      finish_field();
      continue;
    }
    if (c == '\n' || c == '\r') {
      if (c == '\r' && i + 1 < text.size() && text[i + 1] == '\n')
        ++i;
      finish_row();
      continue;
    }
    Check(!closed, "CSV has data after a quoted field");
    if (c == '"') {
      Check(!started, "CSV quote appears inside an unquoted field");
      quoted = true;
      started = true;
    } else {
      field += c;
      started = true;
    }
  }
  Check(!quoted, "CSV has unfinished quoting");
  if (started || closed || !row.empty())
    finish_row();
  return rows;
}
std::vector<std::string> ReadWires(const ArtifactRef &source) {
  std::vector<std::string> header = {"link",
                                     "wire",
                                     "cable",
                                     "bank_group",
                                     "bank_in_cable",
                                     "wire_in_bank",
                                     "connector_data_contact_p",
                                     "connector_data_contact_n"};
  for (const auto *tag : {"a", "b"}) {
    for (const auto *k : {"fpga", "bank", "pair_id"})
      header.push_back(std::string(k) + "_" + tag);
    for (const auto *p : {"p", "n"})
      for (const auto *k : {"package_pin", "site", "pin_function"})
        header.push_back(std::string(k) + "_" + tag + "_" + p);
  }
  const auto rows = Csv(source);
  Check(!rows.empty() && rows.front() == header,
        "fixed wiring CSV header is invalid");
  std::vector<std::string> wires;
  const std::regex cable("(.+):cable([0-9]+)");
  for (std::size_t i = 1; i < rows.size(); ++i) {
    Check(rows[i].size() == header.size(), "fixed wiring CSV row is malformed");
    O r;
    for (std::size_t j = 0; j < header.size(); ++j)
      r.emplace(header[j], rows[i][j]);
    auto number = [&](const std::string &k) {
      return std::to_string(D::IntegerToken(r.at(k)));
    };
    std::smatch match;
    Check(std::regex_match(r.at("cable"), match, cable) &&
              match[1].str() == r.at("link"),
          "fixed wiring CSV cable is invalid");
    std::vector<std::string> ends;
    for (const auto *tag : {"a", "b"}) {
      O end;
      for (const auto *k : {"fpga", "bank", "pair_id"})
        end[k] = JsonQuote(r.at(std::string(k) + "_" + tag));
      for (const auto *p : {"p", "n"}) {
        O pin;
        for (const auto *k : {"package_pin", "site", "pin_function"})
          pin[k] = JsonQuote(r.at(std::string(k) + "_" + tag + "_" + p));
        end[p] = D::Encode(pin);
      }
      ends.push_back(D::Encode(end));
    }
    wires.push_back(D::Encode(
        {{"id", JsonQuote(r.at("link") + ":wire" + number("wire"))},
         {"link", JsonQuote(r.at("link"))},
         {"wire", number("wire")},
         {"bank_group", number("bank_group")},
         {"cable", JsonQuote(r.at("cable"))},
         {"cable_index", std::to_string(D::IntegerToken(match[2].str()))},
         {"bank_in_cable", number("bank_in_cable")},
         {"wire_in_bank", number("wire_in_bank")},
         {"connector_data_contacts",
          D::Encode({{"p", number("connector_data_contact_p")},
                     {"n", number("connector_data_contact_n")}})},
         {"endpoints", Array(ends)}}));
  }
  return wires;
}
} // namespace

std::string BuildVirtualBoard(const std::string &normalized,
                              const ArtifactRef &inventory,
                              const std::optional<ArtifactRef> &wiring,
                              const volatile std::sig_atomic_t *cancelled) {
  Stop(cancelled);
  D board(normalized), platform(board.Token("platform"));
  Check(platform.Text("kind") == "virtual" &&
            platform.Text("virtual_board_model") == "xcvu19p-hp-lvds-cable-v1",
        "virtual board requires the declared xcvu19p-hp-lvds-cable-v1 model");
  const bool fixed = platform.Has("virtual_board_wiring") &&
                     platform.Token("virtual_board_wiring") != "null";
  Check(fixed == bool(wiring), "fixed table source coverage disagrees");
  Check(inventory.schema() == "hp-package-pin-inventory-v1",
        "inventory source schema disagrees");
  D pins(D::Read(inventory));
  Check(pins.Text("schema") == "emuflow.hp-package-pin-inventory/v1",
        "inventory version disagrees");
  std::map<std::string, std::string> fpga_parts;
  std::map<std::string, int> next;
  for (const auto &token : D::Array(board.Token("fpgas"))) {
    D f(token);
    Check(f.Text("part") == pins.Text("part"),
          "inventory does not match the FPGA part");
    fpga_parts.emplace(f.Text("id"), f.Text("part"));
    next[f.Text("id")] = 0;
  }
  std::vector<Bank> banks;
  std::set<std::string> packages, sites, bank_ids;
  const std::regex function("IO_L([0-9]+)([PN])_.*_([0-9]+)");
  for (const auto &token : D::Array(pins.Token("banks"))) {
    Stop(cancelled);
    D b(token);
    Bank bank{Id(b), {}};
    Check(bank_ids.insert(bank.id).second, "duplicate inventory HP bank");
    Check(b.Text("type") == "HP", "data wiring requires HP I/O banks");
    std::map<std::string, O> pairs;
    for (const auto &item : D::Array(b.Token("pins"))) {
      D pin(item);
      Check(!pin.Text("package_pin").empty() &&
                packages.insert(pin.Text("package_pin")).second &&
                !pin.Text("site").empty() &&
                sites.insert(pin.Text("site")).second,
            "invalid inventory package pin or I/O site");
      if (!pin.Has("differential_pair_id") ||
          pin.Token("differential_pair_id") == "null")
        continue;
      const auto pair = pin.Text("differential_pair_id"),
                 polarity = pin.Text("polarity");
      Check(pin.Boolean("eligible_data") &&
                (polarity == "p" || polarity == "n"),
            "invalid differential inventory polarity or eligibility");
      std::smatch match;
      const auto name =
          pin.Token("pin_function") == "null" ? "" : pin.Text("pin_function");
      Check(std::regex_match(name, match, function) &&
                "L" + match[1].str() == pair &&
                ((match[2] == "P" && polarity == "p") ||
                 (match[2] == "N" && polarity == "n")) &&
                match[3].str() == bank.id,
            "differential pair does not match vendor pin function");
      Check(
          pairs[pair]
              .emplace(polarity,
                       D::Encode({{"package_pin", pin.Token("package_pin")},
                                  {"site", pin.Token("site")},
                                  {"pin_function", pin.Token("pin_function")}}))
              .second,
          "duplicate differential inventory polarity");
    }
    std::vector<std::tuple<bool, int, std::string>> order;
    for (auto &p : pairs) {
      Check(p.second.size() == 2 && p.second.count("p") && p.second.count("n"),
            "incomplete differential pair in device inventory");
      D positive(p.second.at("p"));
      order.emplace_back(positive.Text("pin_function").find("_GC_") !=
                             std::string::npos,
                         D::IntegerToken(p.first.substr(1)), p.first);
    }
    std::sort(order.begin(), order.end());
    for (const auto &key : order) {
      const auto &id = std::get<2>(key);
      auto p = pairs.at(id);
      p["pair_id"] = JsonQuote(id);
      bank.pairs.push_back(D::Encode(p));
    }
    banks.push_back(std::move(bank));
  }
  std::sort(banks.begin(), banks.end(), [](const Bank &a, const Bank &b) {
    return D::IntegerToken(a.id) < D::IntegerToken(b.id);
  });
  std::vector<const Bank *> data;
  for (const auto &b : banks)
    if (b.id != "65")
      data.push_back(&b);
  std::map<std::string, std::string> links;
  for (const auto &token : D::Array(board.Token("links"))) {
    D l(token);
    links.emplace(l.Text("id"), token);
  }
  std::map<std::string, std::vector<std::string>> allocated;
  std::map<std::string, std::set<std::string>> used;
  std::vector<std::string> wires;
  int cables = 0;
  for (const auto &entry : links) {
    Stop(cancelled);
    D link(entry.second);
    Check(link.Text("capacity_sharing") == "shared_bidirectional" &&
              link.Text("direction") == "full_duplex" &&
              link.Text("mode") == "abstract" &&
              link.Integer("cable_count") == 6 &&
              link.Integer("hp_banks_per_cable") == 3 &&
              link.Integer("bank_count") == 18,
          "model requires 6 cables, 3 paired HP banks per cable and shared "
          "differential channels");
    const auto width = link.Integer("wires_per_bank");
    Check(width > 0 && width <= 24, "invalid differential width");
    cables += 6;
    std::vector<std::string> ends;
    for (const auto &e : D::Array(link.Token("endpoints"), 2))
      ends.push_back(DecodeJsonString(e));
    std::sort(ends.begin(), ends.end());
    Check(ends.size() == 2, "invalid link endpoints");
    std::map<std::string, int> begin;
    for (const auto &e : ends) {
      Check(next.count(e) && next.at(e) + 18 <= int(data.size()),
            e + " has insufficient disjoint HP banks for virtual cables");
      begin[e] = next[e];
      next[e] += 18;
    }
    for (int group = 0; group < 18; ++group) {
      const auto cable = entry.first + ":cable" + std::to_string(group / 3);
      for (const auto &e : ends) {
        const auto &bank = *data.at(begin.at(e) + group);
        Check(bank.pairs.size() >= std::size_t(width),
              "bank " + bank.id +
                  " has insufficient eligible differential pairs");
        used[e].insert(bank.id);
        allocated[e].push_back(
            D::Encode({{"link", JsonQuote(entry.first)},
                       {"bank_group", std::to_string(group)},
                       {"bank", JsonQuote(bank.id)},
                       {"cable", JsonQuote(cable)},
                       {"bank_in_cable", std::to_string(group % 3)},
                       {"differential_pairs_used", std::to_string(width)},
                       {"data_pins_used", std::to_string(2 * width)}}));
      }
      for (int index = 0; index < width; ++index) {
        std::vector<std::string> endpoints;
        for (const auto &e : ends) {
          const auto &bank = *data.at(begin.at(e) + group);
          auto pair = D(bank.pairs.at(index)).Fields();
          pair["fpga"] = JsonQuote(e);
          pair["bank"] = JsonQuote(bank.id);
          endpoints.push_back(D::Encode(pair));
        }
        const auto wire = group * width + index,
                   contact = 2 * ((group % 3) * width + index) + 1;
        wires.push_back(D::Encode(
            {{"id", JsonQuote(entry.first + ":wire" + std::to_string(wire))},
             {"link", JsonQuote(entry.first)},
             {"wire", std::to_string(wire)},
             {"bank_group", std::to_string(group)},
             {"cable", JsonQuote(cable)},
             {"cable_index", std::to_string(group / 3)},
             {"bank_in_cable", std::to_string(group % 3)},
             {"wire_in_bank", std::to_string(index)},
             {"connector_data_contacts",
              D::Encode({{"p", std::to_string(contact)},
                         {"n", std::to_string(contact + 1)}})},
             {"endpoints", Array(endpoints)}}));
      }
    }
  }
  O source{{"inventory_file",
            JsonQuote(fs::path(inventory.path()).filename().string())},
           {"inventory_sha256", JsonQuote(Digest(CanonicalJsonToken(pins.text())))},
           {"device_provenance", pins.Token("provenance")},
           {"pin_screening", pins.Token("screening")}};
  if (wiring) {
    Check(wiring->schema() == "fixed-wiring-csv-v1",
          "fixed table schema disagrees");
    Check(ReadWires(*wiring) == wires,
          "fixed virtual-board wiring table differs from the declared model or "
          "device pin inventory");
    source["wiring_table"] = platform.Token("virtual_board_wiring");
    source["wiring_table_sha256"] = JsonQuote(Sha256File(*wiring, cancelled));
    source["wiring_authority"] = JsonQuote("checked-in-fixed-table");
  }
  std::vector<std::string> fpgas;
  O bank_counts;
  for (const auto &fpga : fpga_parts) {
    std::vector<std::string> free;
    for (const auto &bank : banks)
      if (!used[fpga.first].count(bank.id))
        free.push_back(JsonQuote(bank.id));
    fpgas.push_back(
        D::Encode({{"id", JsonQuote(fpga.first)},
                   {"part", JsonQuote(fpga.second)},
                   {"bank_assignments", Array(allocated[fpga.first])},
                   {"unallocated_hp_banks", Array(free)}}));
    bank_counts[fpga.first] = std::to_string(next.at(fpga.first));
  }
  const auto assumptions =
      R"({"signaling":"differential","iostandard":"LVDS","vcco_v":1.8,"wire_unit":"one differential data channel: two conductors, one bit per fabric cycle","connectivity":"fixed P-to-P and N-to-N; direction chosen per bank by Phase 5","connector_contacts":"data-contact ordinals, not a connector product pinout","board_services":"clock, reset, power and ground connections are outside this data-wire table","electrical_qualification":"assumed 1.8V bank supply and internal 100-ohm receiver termination; no board SI or vendor DRC claim","receiver_termination":"internal_100_ohm_at_receiver","direction_configuration":"static per generated schedule/build; not switched during a TDM frame","pin_selection":"reserve bank 65 for configuration; prefer non-GC differential pairs within data banks","link_timing":"uses BoardDB TDM model; no extra invented cable delay"})";
  inventory.RequireUnchanged();
  if (wiring)
    wiring->RequireUnchanged();
  Stop(cancelled);
  return D::Encode(
      {{"schema", JsonQuote("emuflow.virtual-board-wiring/v1")},
       {"model", JsonQuote("xcvu19p-hp-lvds-cable-v1")},
       {"platform", platform.Token("name")},
       {"qualification", JsonQuote("virtual_board_real_device_pins")},
       {"source", CanonicalJsonToken(D::Encode(source))},
       {"assumptions", CanonicalJsonToken(assumptions)},
       {"fpgas", Array(fpgas)},
       {"wires", Array(wires)},
       {"metrics",
        D::Encode({{"links", std::to_string(links.size())},
                   {"cables", std::to_string(cables)},
                   {"differential_pairs", std::to_string(wires.size())},
                   {"physical_conductors", std::to_string(2 * wires.size())},
                   {"endpoint_data_pins", std::to_string(4 * wires.size())},
                   {"hp_banks_per_fpga", D::Encode(bank_counts)}})}});
}
StageResult BuildVirtualBoardArtifact(
    const ArtifactRef &board, const ArtifactRef &inventory,
    const std::optional<ArtifactRef> &wiring, const std::string &output,
    const volatile std::sig_atomic_t *cancelled) {
  return RunStage(
      "board.build-wiring",
      [&] {
        Check(board.schema() == "boarddb-v1", "board source schema disagrees");
        std::vector<ArtifactRef> refs{board, inventory};
        if (wiring)
          refs.push_back(*wiring);
        return refs;
      },
      [&](const auto &refs) {
        const auto text = BuildVirtualBoard(NormalizeBoard(D::Read(board)),
                                            inventory, wiring, cancelled);
        PublishTextArtifact(output, refs,
                            [&](std::ostream &out) { out << text << '\n'; });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(output, "virtual-board-wiring-v1")};
      });
}
} // namespace emuflow::core
