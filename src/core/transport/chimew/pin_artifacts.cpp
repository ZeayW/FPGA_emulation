#include "transport/chimew/pin_artifacts.hpp"
#include "board/model.hpp"
#include "board/wiring.hpp"
#include "common/canonical_json.hpp"
#include "common/hash.hpp"
#include "scheduling/schedule_view.hpp"
#include "transport/chimew/bank_store.hpp"
#include "transport/chimew/qualification.hpp"
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <memory>
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
namespace fs = std::filesystem;
constexpr const char *provider = "chimew-paper-plus-emuflow-electrical-slot-v1";
constexpr const char *position_schema = "emuflow.signal-position-hints/v1";
constexpr const char *pin_schema = "emuflow.placement-aware-pin-plan/v1";
constexpr const char *position_storage =
    "emuflow.signal-position-hints-storage/v1";
constexpr const char *pin_storage = "emuflow.pin-plan-storage/v1";
constexpr const char *binding_storage = "emuflow.chimew-binding-storage/v1";
void Check(bool ok, const std::string &why) {
  if (!ok)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Chimew pin artifacts: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "Chimew pin artifacts cancelled");
}
O Fields(const std::string &text) {
  O result;
  VisitJsonObjectTokens(text, [&](const auto &key, const auto &value) {
    Check(result.emplace(key, value).second, "duplicate record field");
  });
  return result;
}
std::string Get(const O &o, const std::string &key,
                const std::string &fallback = "null") {
  auto i = o.find(key);
  return i == o.end() ? fallback : i->second;
}
std::string Text(const O &o, const std::string &key) {
  auto value = DecodeJsonString(Get(o, key));
  Check(!value.empty(), "empty identity " + key);
  return value;
}
I Int(const O &o, const std::string &key) {
  return D::IntegerToken(Get(o, key));
}
double Num(const O &o, const std::string &key) {
  auto token = Get(o, key);
  Check(token != "true" && token != "false" && token != "null",
        "invalid number " + key);
  return ParseFiniteDecimal(token, "pin artifact number");
}
std::string Number(double n) {
  Check(std::isfinite(n), "non-finite arithmetic");
  return JsonRecord(n).Encode();
}
void Hash(Sha256 &h, const std::string &s) {
  h.update(reinterpret_cast<const unsigned char *>(s.data()), s.size());
}
std::string Hex(Sha256 &h) {
  std::string s;
  for (auto c : h.finish()) {
    s += "0123456789abcdef"[c >> 4];
    s += "0123456789abcdef"[c & 15];
  }
  return s;
}
std::string Digest(const std::string &s) {
  Sha256 h;
  Hash(h, s);
  return Hex(h);
}
bool IsSha(const std::string &s) {
  return s.size() == 64 &&
         s.find_first_not_of("0123456789abcdef") == std::string::npos;
}
std::string Sha(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return char(std::tolower(c)); });
  Check(IsSha(s), "invalid SHA-256");
  return s;
}
bool Agree(double a, double b, double tolerance = 1e-8) {
  return std::isfinite(a) && std::isfinite(b) &&
         std::abs(a - b) <= tolerance * std::max(1.0, std::abs(b));
}
std::string Array(const std::vector<std::string> &values) {
  std::string out = "[";
  for (const auto &v : values) {
    if (out.size() > 1)
      out += ',';
    out += v;
  }
  return out + "]";
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto q = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value END "
                      "FROM metadata WHERE key=?");
  q.BindText(1, key);
  Check(q.Step() && !q.IsNull(0), "missing metadata " + key);
  auto out = q.TypedText(0);
  Check(!q.Step(), "duplicate metadata");
  return out;
}
void PutMeta(SqliteDatabase &db, const std::string &key,
             const std::string &token) {
  auto q = db.Prepare("INSERT INTO metadata VALUES(?,?)");
  q.BindText(1, key);
  q.BindText(2, token);
  q.Execute();
}
void Attach(SqliteDatabase &db, const ArtifactRef &ref, const char *alias) {
  auto q = db.Prepare(std::string("ATTACH DATABASE ? AS ") + alias);
  q.BindText(1, SqliteImmutableUri(ref.path()));
  q.Execute();
}
I Scalar(SqliteDatabase &db, const std::string &query) {
  auto q = db.Prepare(query);
  Check(q.Step(), "missing aggregate");
  return q.TypedInteger(0);
}
void NoRows(SqliteDatabase &db, const std::string &query,
            const std::string &message) {
  auto q = db.Prepare(query);
  Check(!q.Step(), message);
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".chimew-pins-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create workspace");
    root = name;
  }
  ~Workspace() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};

const char *pin_ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE entries(seq INTEGER NOT NULL UNIQUE,schedule_entry TEXT PRIMARY KEY,group_id INTEGER NOT NULL,physical_lane INTEGER NOT NULL,logical_lane INTEGER NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID;
)SQL";
const char *positions_ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE entries(seq INTEGER NOT NULL UNIQUE,schedule_entry TEXT PRIMARY KEY,source_y REAL NOT NULL,sink_y REAL NOT NULL,source_region INTEGER NOT NULL,sink_region INTEGER NOT NULL,source_fallback INTEGER NOT NULL,sink_fallback INTEGER NOT NULL,record_json TEXT NOT NULL) WITHOUT ROWID;
)SQL";
const char *binding_ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE groups(seq INTEGER NOT NULL UNIQUE,group_id TEXT PRIMARY KEY,link_id TEXT NOT NULL,fpga_a TEXT NOT NULL,fpga_b TEXT NOT NULL,physical_lane INTEGER NOT NULL,direction TEXT NOT NULL,package_pin_a TEXT NOT NULL,package_pin_b TEXT NOT NULL,group_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE members(member TEXT PRIMARY KEY,group_seq INTEGER NOT NULL,member_seq INTEGER NOT NULL,UNIQUE(group_seq,member_seq)) WITHOUT ROWID;
CREATE INDEX members_by_group ON members(group_seq,member_seq);
)SQL";

struct Link {
  std::string a, b, direction, sharing;
  I width = 0, banks = 0, bank_width = 0;
};
struct Board {
  O normalized, metadata;
  std::string name, virtual_board = "null", fixed_source = "null", standard;
  bool fixed = false;
  std::map<std::string, Link> links;
  std::set<std::string> fpgas;
  std::map<std::pair<std::string, I>, std::string> wires;
  std::map<std::pair<std::string, I>, std::pair<std::string, std::string>>
      bank_directions;
  void Read(const std::string &token,
            const std::optional<ArtifactRef> &inventory,
            const std::optional<ArtifactRef> &wiring,
            const volatile std::sig_atomic_t *cancelled) {
    normalized = Fields(NormalizeBoard(token));
    metadata = Fields(normalized.at("platform"));
    name = Text(metadata, "name");
    fixed = metadata.count("virtual_board_model");
    VisitJsonArrayTokens(normalized.at("fpgas"), [&](const auto &raw) {
      fpgas.insert(D(raw).Text("id"));
    });
    VisitJsonArrayTokens(normalized.at("links"), [&](const auto &raw) {
      D d(raw);
      auto e = D::Array(d.Token("endpoints"), 2);
      Check(e.size() == 2, "invalid link endpoints");
      Link l;
      l.a = DecodeJsonString(e[0]);
      l.b = DecodeJsonString(e[1]);
      l.direction = d.Text("direction");
      l.sharing = d.Text("capacity_sharing");
      I lanes = d.Integer("data_lanes_per_direction"),
        payload = d.Has("payload_bits_per_lane_per_cycle")
                      ? d.Integer("payload_bits_per_lane_per_cycle")
                      : 1;
      Check(lanes > 0 && payload > 0 && lanes <= INT64_MAX / payload,
            "invalid transport width");
      l.width = lanes * payload;
      if (d.Has("bank_count")) {
        l.banks = d.Integer("bank_count");
        l.bank_width = d.Integer("wires_per_bank");
      }
      Check(links.emplace(d.Text("id"), l).second, "duplicate link");
    });
    if (fixed) {
      Check(bool(inventory), "fixed board requires its pin inventory");
      virtual_board = BuildVirtualBoard(D::Encode(normalized), *inventory,
                                        wiring, cancelled);
      auto board = Fields(virtual_board);
      D source(board.at("source"));
      standard = D(board.at("assumptions")).Text("iostandard");
      fixed_source = D::Encode(
          {{"board_sha256", JsonQuote(Digest(virtual_board))},
           {"inventory_sha256", source.Token("inventory_sha256")},
           {"wiring_table_sha256", source.Has("wiring_table_sha256")
                                       ? source.Token("wiring_table_sha256")
                                       : "null"}});
      VisitJsonArrayTokens(board.at("wires"), [&](const auto &raw) {
        D w(raw);
        Check(
            wires
                .emplace(std::make_pair(w.Text("link"), w.Integer("wire")), raw)
                .second,
            "duplicate fixed wire");
      });
    }
  }
  const Link &GetLink(const std::string &id, const std::string &a,
                      const std::string &b) const {
    auto it = links.find(id);
    Check(it != links.end() && ((it->second.a == a && it->second.b == b) ||
                                (it->second.a == b && it->second.b == a)),
          "electrical channel link/domain mismatch");
    return it->second;
  }
  std::pair<O, O> Ends(const O &record, const std::string &a,
                       const std::string &b) const {
    auto it = wires.find({Text(record, "link"), Int(record, "physical_lane")});
    Check(it != wires.end(), "channel has no fixed differential wire");
    D wire(it->second);
    O left, right;
    VisitJsonArrayTokens(wire.Token("endpoints"), [&](const auto &raw) {
      auto end = Fields(raw);
      if (Get(end, "bank").front() == '"')
        end["bank"] =
            std::to_string(D::IntegerToken(DecodeJsonString(end.at("bank"))));
      if (Text(end, "fpga") == a)
        left = end;
      else if (Text(end, "fpga") == b)
        right = end;
      else
        Check(false, "fixed wire FPGA endpoints disagree");
    });
    Check(!left.empty() && !right.empty(),
          "fixed wire endpoint coverage differs");
    return {left, right};
  }
  void ValidateFixed(const O &r, const std::string &a, const std::string &b) {
    auto ends = Ends(r, a, b);
    auto wire = Fields(wires.at({Text(r, "link"), Int(r, "physical_lane")}));
    Check(!r.count("differential_pair_a") && !r.count("differential_pair_b"),
          "fixed wire references must not duplicate P/N inventory");
    O expected{
        {"fixed_wire", wire.at("id")},
        {"bank_a",
         JsonQuote(a + ":HP" + std::to_string(Int(ends.first, "bank")))},
        {"bank_b",
         JsonQuote(b + ":HP" + std::to_string(Int(ends.second, "bank")))},
        {"package_pin_a", Fields(ends.first.at("p")).at("package_pin")},
        {"package_pin_b", Fields(ends.second.at("p")).at("package_pin")}};
    for (const auto &p : expected)
      Check(Get(r, p.first) == p.second,
            "differential P/N, bank or fixed wire identity disagrees");
    Check(Text(r, "electrical_class") == "fixed-lvds-pair" &&
              Text(r, "iostandard") == standard &&
              CanonicalJsonToken(Get(r, "supported_iostandards")) ==
                  Array({JsonQuote(standard)}) &&
              Num(r, "bank_voltage") == 1.8 &&
              Get(r, "placement_anchor") == "true",
          "fixed LVDS electrical contract is invalid");
    auto direction = Text(r, "direction");
    Check(direction == "a_to_b" || direction == "b_to_a",
          "fixed LVDS wire requires one static direction");
    auto pair =
        direction == "a_to_b" ? std::make_pair(a, b) : std::make_pair(b, a);
    for (const auto &end : {ends.first, ends.second}) {
      auto key = std::make_pair(Text(end, "fpga"), Int(end, "bank"));
      auto inserted = bank_directions.emplace(key, pair);
      Check(inserted.second || inserted.first->second == pair,
            "cannot reverse a fixed HP bank within a build");
    }
  }
  std::vector<std::pair<std::string, std::string>> Pins(const O &r) const {
    auto a = Text(r, "fpga_a"), b = Text(r, "fpga_b");
    std::vector<std::pair<std::string, std::string>> out{
        {a, Text(r, "package_pin_a")}, {b, Text(r, "package_pin_b")}};
    if (Text(r, "electrical_class") == "fixed-lvds-pair") {
      Check(fixed, "differential pins require fixed wiring owner");
      auto ends = Ends(r, a, b);
      out.emplace_back(a, Text(Fields(ends.first.at("n")), "package_pin"));
      out.emplace_back(b, Text(Fields(ends.second.at("n")), "package_pin"));
    }
    return out;
  }
};
const std::map<std::string, double> voltages{{"LVCMOS12", 1.2},
                                             {"LVCMOS15", 1.5},
                                             {"LVCMOS18", 1.8},
                                             {"LVCMOS25", 2.5},
                                             {"LVCMOS33", 3.3}};

struct Schedule {
  SqliteDatabase &db;
  O header;
  std::string logical, design, platform;
  I count = 0;
  std::vector<ArtifactRef> refs;
  explicit Schedule(SqliteDatabase &db) : db(db) {}
  void Read(const ArtifactRef &ref,
            const volatile std::sig_atomic_t *cancelled) {
    refs = {ref};
    db.Execute(
        "CREATE TABLE schedule(entry_id TEXT PRIMARY KEY,link_id TEXT NOT "
        "NULL,source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL,slot INTEGER "
        "NOT NULL,lane INTEGER NOT NULL,ratio INTEGER NOT NULL) WITHOUT ROWID");
    auto add = db.Prepare("INSERT INTO schedule VALUES(?,?,?,?,?,?,?)");
    const auto insert = [&](const std::string &id, const std::string &link,
                            const std::string &source, const std::string &sink,
                            I slot, I lane, I ratio) {
      Check(!id.empty() && !link.empty() && !source.empty() && !sink.empty() &&
                slot >= 0 && lane >= 0 && ratio > 0,
            "schedule entry is malformed");
      add.BindText(1, id);
      add.BindText(2, link);
      add.BindText(3, source);
      add.BindText(4, sink);
      add.BindInteger(5, slot);
      add.BindInteger(6, lane);
      add.BindInteger(7, ratio);
      add.Execute();
      add.Reset();
      ++count;
    };
    auto bound = OpenScheduleStore(
        ArtifactRef::Bind(ref.path(), "tdm-schedule-selector-v4"), cancelled);
    refs.push_back(bound.selector);
    refs.push_back(bound.store);
    header = Fields(bound.header);
    logical = bound.logical_sha256;
    SqliteStore store(bound.store);
    auto q = store.database().Prepare(
        "SELECT entry_id,link_id,source_fpga,sink_fpga,slot,lane,tdm_ratio "
        "FROM schedule_entries ORDER BY seq");
    while (q.Step()) {
      Stop(cancelled);
      insert(q.TypedText(0), q.TypedText(1), q.TypedText(2), q.TypedText(3),
             q.TypedInteger(4), q.TypedInteger(5),
             q.IsNull(6) ? 1 : q.TypedInteger(6));
    }
    design = Text(header, "design");
    platform = Text(header, "platform");
    for (const auto &source : refs)
      source.RequireUnchanged();
  }
  void BoardRules(const Board &board) {
    Check(platform == board.name, "schedule platform differs");
    db.Execute(
        "CREATE TABLE links(link_id TEXT PRIMARY KEY,capacity INTEGER,shared "
        "INTEGER,bank_width INTEGER) WITHOUT ROWID; CREATE TABLE "
        "budgets(link_id TEXT,source_fpga TEXT,sink_fpga TEXT,low INTEGER,high "
        "INTEGER,PRIMARY KEY(link_id,source_fpga,sink_fpga)) WITHOUT ROWID");
    auto add = db.Prepare("INSERT INTO links VALUES(?,?,?,?)");
    std::set<Domain> expected;
    for (const auto &p : board.links) {
      add.BindText(1, p.first);
      add.BindInteger(2, p.second.width);
      add.BindInteger(3, p.second.sharing == "shared_bidirectional");
      add.BindInteger(4, p.second.bank_width);
      add.Execute();
      add.Reset();
      if (p.second.banks) {
        expected.insert({p.first, p.second.a, p.second.b});
        expected.insert({p.first, p.second.b, p.second.a});
      }
    }
    if (!expected.empty()) {
      Check(header.count("bank_allocations"),
            "bank pin assignment requires Phase 5 bank_allocations");
      auto put = db.Prepare("INSERT INTO budgets VALUES(?,?,?,?,?)");
      std::set<std::pair<std::string, I>> owners;
      std::set<Domain> actual;
      VisitJsonArrayTokens(
          header.at("bank_allocations"), [&](const auto &token) {
            D row(token);
            Domain key{row.Text("link"), row.Text("from"), row.Text("to")};
            Check(expected.count(key) && actual.insert(key).second,
                  "bank pin allocation direction coverage is invalid");
            const auto &link = board.links.at(std::get<0>(key));
            I count = row.Integer("bank_count"),
              offset = row.Integer("lane_offset"), lanes = row.Integer("lanes");
            Check(count >= 0 && offset >= 0 && lanes >= 0 &&
                      link.bank_width > 0 &&
                      count <= INT64_MAX / link.bank_width &&
                      row.Integer("wires_per_bank") == link.bank_width &&
                      offset % link.bank_width == 0 &&
                      lanes == count * link.bank_width &&
                      offset <= link.width && lanes <= link.width - offset,
                  "bank pin allocation disagrees with BoardDB capacity");
            for (I b = offset / link.bank_width;
                 b < (offset + lanes) / link.bank_width; ++b)
              Check(owners.insert({std::get<0>(key), b}).second,
                    "bank pin allocation assigns one bank to both directions");
            put.BindText(1, std::get<0>(key));
            put.BindText(2, std::get<1>(key));
            put.BindText(3, std::get<2>(key));
            put.BindInteger(4, offset);
            put.BindInteger(5, offset + lanes);
            put.Execute();
            put.Reset();
          });
      Check(actual == expected,
            "bank pin allocation direction coverage is incomplete");
    }
  }
};

struct Electrical {
  O provenance, header;
  std::string map_hash;
  std::map<std::string, std::pair<double, double>> bounds;
  std::map<std::string, O> channels;
  void Read(const ArtifactRef &source, const ArtifactRef &bank_store,
            const O &bank_header, Board &board,
            const volatile std::sig_atomic_t *cancelled) {
    D doc(D::Read(source));
    header = doc.Fields();
    const auto schema = doc.Text("schema");
    Check(schema == "emuflow.chimew-electrical-channel-map/v1" ||
              schema == "emuflow.chimew-electrical-channel-map/v2" ||
              schema == "emuflow.chimew-electrical-channel-map/v3",
          "electrical channel-map schema is invalid");
    Check(!board.fixed || schema == "emuflow.chimew-electrical-channel-map/v3",
          "fixed LVDS BoardDB requires a differential Chimew electrical map");
    Check((schema == "emuflow.chimew-electrical-channel-map/v3") == board.fixed,
          "fixed differential map requires fixed board");
    Check(doc.Text("provider") ==
                  "source-qualified-boarddb-electrical-channel-map-v1" &&
              doc.Text("design") == Text(bank_header, "design") &&
              doc.Text("platform") == board.name &&
              Text(bank_header, "platform") == board.name,
          "electrical channel-map identity does not agree");
    if (board.fixed)
      Check(CanonicalJsonToken(doc.Token("fixed_wiring")) == board.fixed_source,
            "fixed wiring source differs from BoardDB");
    D p(doc.Token("provenance"));
    provenance = {{"producer", JsonQuote(p.Text("producer"))},
                  {"producer_version", JsonQuote(p.Text("producer_version"))},
                  {"boarddb_sha256", JsonQuote(Sha(p.Text("boarddb_sha256")))},
                  {"package_pin_inventory_sha256",
                   JsonQuote(Sha(p.Text("package_pin_inventory_sha256")))}};
    Check(!p.Text("producer").empty() && !p.Text("producer_version").empty(),
          "electrical provenance is empty");
    std::map<std::string, std::pair<std::string, std::string>> domains;
    std::set<std::string> used_fpgas;
    VisitJsonArrayTokens(bank_header.at("domains"), [&](const auto &raw) {
      D domain(raw);
      auto a = domain.Text("fpga_a"), b = domain.Text("fpga_b");
      domains.emplace(domain.Text("id"), std::make_pair(a, b));
      used_fpgas.insert(a);
      used_fpgas.insert(b);
    });
    VisitJsonArrayTokens(doc.Token("fpga_y_bounds"), [&](const auto &raw) {
      D row(raw);
      auto id = row.Text("fpga");
      double low = row.Number("y_min"), high = row.Number("y_max");
      Check(high > low && bounds.emplace(id, std::make_pair(low, high)).second,
            "electrical FPGA bounds are invalid");
    });
    std::set<std::string> found;
    for (const auto &p : bounds)
      found.insert(p.first);
    Check(found == used_fpgas &&
              std::includes(board.fpgas.begin(), board.fpgas.end(),
                            used_fpgas.begin(), used_fpgas.end()),
          "electrical FPGA bounds do not cover domains");
    struct PhysicalChannel {
      std::string bank_a, bank_b, a, b;
      O pin_a, pin_b;
    };
    std::map<std::string, PhysicalChannel> physical;
    SqliteStore owner(bank_store);
    auto banks =
        owner.database().Prepare("SELECT record_json FROM banks ORDER BY seq");
    while (banks.Step()) {
      D bank(banks.TypedText(0));
      auto domain = domains.at(bank.Text("domain"));
      auto ba = D(bank.Token("bank_a")).Text("id"),
           bb = D(bank.Token("bank_b")).Text("id");
      VisitJsonArrayTokens(bank.Token("channels"), [&](const auto &raw) {
        D row(raw);
        O a = Fields(row.Token("pin_a")), b = Fields(row.Token("pin_b"));
        physical.emplace(
            row.Text("id"),
            PhysicalChannel{
                ba,
                bb,
                domain.first,
                domain.second,
                {{"x", Number(Num(a, "x"))}, {"y", Number(Num(a, "y"))}},
                {{"x", Number(Num(b, "x"))}, {"y", Number(Num(b, "y"))}}});
      });
    }
    std::map<std::pair<std::string, I>, std::vector<std::string>> uses;
    std::set<std::pair<std::string, std::string>> pins;
    VisitJsonArrayTokens(doc.Token("channels"), [&](const auto &raw) {
      Stop(cancelled);
      auto row = Fields(raw);
      auto id = Text(row, "chimew_channel");
      Check(physical.count(id) && !channels.count(id),
            "electrical channel identity is invalid");
      const auto &channel = physical.at(id);
      const auto &link = board.GetLink(Text(row, "link"), channel.a, channel.b);
      I lane = Int(row, "physical_lane");
      Check(lane >= 0 && lane < link.width,
            "electrical concrete lane is invalid");
      std::string direction = "either";
      if (schema != "emuflow.chimew-electrical-channel-map/v1") {
        direction = Text(row, "direction");
        Check(direction == "a_to_b" || direction == "b_to_a" ||
                  direction == "either",
              "electrical channel direction is invalid");
      } else
        Check(Get(row, "direction") == "null",
              "legacy electrical maps cannot declare a direction");
      auto &previous = uses[{Text(row, "link"), lane}];
      bool reuse = schema != "emuflow.chimew-electrical-channel-map/v1" &&
                   link.direction == "full_duplex" &&
                   link.sharing == "per_direction";
      if (direction == "either")
        Check(std::all_of(previous.begin(), previous.end(),
                          [](const auto &d) { return d == "either"; }) &&
                  previous.size() < std::size_t(reuse ? 2 : 1),
              "electrical concrete lane is invalid");
      else
        Check(std::find(previous.begin(), previous.end(), "either") ==
                      previous.end() &&
                  std::find(previous.begin(), previous.end(), direction) ==
                      previous.end() &&
                  (previous.empty() || reuse),
              "electrical concrete lane is invalid");
      previous.push_back(direction);
      Check(Text(row, "bank_a") == channel.bank_a &&
                Text(row, "bank_b") == channel.bank_b,
            "electrical bank identity does not agree");
      row["fpga_a"] = JsonQuote(channel.a);
      row["fpga_b"] = JsonQuote(channel.b);
      Text(row, "package_pin_a");
      Text(row, "package_pin_b");
      if (board.fixed)
        board.ValidateFixed(row, channel.a, channel.b);
      for (const auto &pin : board.Pins(row))
        Check(pins.insert(pin).second, "electrical package-pin collision");
      auto standard = Text(row, "iostandard");
      std::set<std::string> supported;
      I standards = 0;
      VisitJsonArrayTokens(
          Get(row, "supported_iostandards"), [&](const auto &raw) {
            auto value = DecodeJsonString(raw);
            Check(!value.empty() && supported.insert(value).second,
                  "electrical IOSTANDARD support is invalid");
            ++standards;
          });
      Check(standards > 0 && supported.count(standard),
            "electrical IOSTANDARD support is invalid");
      Check(Get(row, "reserved") == "false", "electrical channel is reserved");
      auto anchor = Get(row, "placement_anchor", "true");
      Check(anchor == "true" || anchor == "false",
            "electrical placement anchor is invalid");
      double voltage = Num(row, "bank_voltage");
      Check((board.fixed || voltages.count(standard)) &&
                std::abs(voltage -
                         (board.fixed ? 1.8 : voltages.at(standard))) <= 1e-9,
            "electrical bank voltage/IOSTANDARD mismatch");
      Check(board.fixed ||
                Text(row, "electrical_class") == "single_ended_parallel",
            "electrical maps require single-ended parallel channels");
      O normalized;
      for (auto key :
           {"link", "physical_lane", "fpga_a", "fpga_b", "bank_a", "bank_b",
            "package_pin_a", "package_pin_b", "iostandard", "electrical_class"})
        normalized[key] = row.at(key);
      normalized["direction"] = JsonQuote(direction);
      normalized["placement_anchor"] = anchor;
      normalized["bank_voltage"] = Number(voltage);
      std::vector<std::string> values;
      for (const auto &v : supported)
        values.push_back(JsonQuote(v));
      normalized["supported_iostandards"] = Array(values);
      normalized["pin_a_point"] = D::Encode(channel.pin_a);
      normalized["pin_b_point"] = D::Encode(channel.pin_b);
      if (board.fixed)
        normalized["fixed_wire"] = row.at("fixed_wire");
      channels.emplace(id, std::move(normalized));
    });
    D metrics(doc.Token("metrics"));
    Check(channels.size() == physical.size() &&
              metrics.Integer("channels") == I(channels.size()) &&
              metrics.Integer("package_pins") == I(pins.size()) &&
              metrics.Integer("concrete_lanes") == I(channels.size()),
          "electrical channel-map metrics or coverage does not agree");
    map_hash = Digest(CanonicalJsonToken(doc.text()));
    source.RequireUnchanged();
    bank_store.RequireUnchanged();
  }
};

std::string KindSelector(const std::string &kind) {
  return kind == "pin-plan-store-v1"         ? "pin-plan-selector-v1"
         : kind == "position-hints-store-v1" ? "position-hints-selector-v1"
                                             : "chimew-binding-selector-v1";
}
const char *KindDDL(const std::string &kind) {
  return kind == "pin-plan-store-v1"         ? pin_ddl
         : kind == "position-hints-store-v1" ? positions_ddl
                                             : binding_ddl;
}
const char *KindStorage(const std::string &kind) {
  return kind == "pin-plan-store-v1"         ? pin_storage
         : kind == "position-hints-store-v1" ? position_storage
                                             : binding_storage;
}
I KindId(const std::string &kind) {
  return kind == "pin-plan-store-v1"         ? 0x454d5550
         : kind == "position-hints-store-v1" ? 0x454d5548
                                             : 0x454d5542;
}
struct StoredArtifact {
  O manifest, header;
  std::vector<ArtifactRef> refs;
  std::optional<ArtifactRef> store;
  I count = 0;
  std::string logical, canonical;
  void Open(const ArtifactRef &selector, const std::string &kind,
            const volatile std::sig_atomic_t *cancelled) {
    Check(kind == "pin-plan-store-v1" || kind == "position-hints-store-v1" ||
              kind == "chimew-binding-store-v1",
          "unknown pin artifact kind");
    Check(selector.schema() == KindSelector(kind) &&
              !fs::is_symlink(selector.path()),
          "current pin artifact selector required");
    refs = {selector};
    D doc(D::Read(selector));
    manifest = doc.Fields();
    Check(doc.Text("schema") == KindStorage(kind) &&
              doc.Integer("application_id") == KindId(kind) &&
              doc.Integer("user_version") == 1,
          "pin artifact selector envelope differs");
    auto member = doc.Text("artifact"), hash = doc.Text("store_sha256");
    auto path = fs::path(selector.path()).parent_path() / member;
    Check(IsSha(hash) && fs::path(member).filename() == member &&
              fs::path(member).extension() == ".sqlite" &&
              !fs::is_symlink(path),
          "pin artifact payload path is invalid");
    store = ArtifactRef::Bind(path.string(), kind);
    refs.push_back(*store);
    Check(doc.Integer("store_bytes") >= 0 &&
              std::uint64_t(doc.Integer("store_bytes")) ==
                  store->generation().bytes &&
              Sha256File(*store, cancelled) == hash,
          "pin artifact store seal differs");
    header = Fields(CanonicalJsonToken(doc.Token("header")));
    auto schema = Text(header, "schema");
    Check(doc.Text("logical_schema") == schema,
          "pin artifact logical schema differs");
    if (kind == "pin-plan-store-v1")
      Check(schema == pin_schema, "pin artifact logical schema differs");
    else if (kind == "position-hints-store-v1")
      Check(schema == position_schema,
            "position artifact logical schema differs");
    else
      Check(schema == "emuflow.chimew-phase6-electrical-binding/v1" ||
                schema == "emuflow.chimew-phase6-electrical-binding/v2" ||
                schema == "emuflow.chimew-phase6-electrical-binding/v3",
            "binding logical schema differs");
    SqliteStore owner(*store);
    auto &db = owner.database();
    RequireSqliteDDL(db, KindDDL(kind));
    RequireEquivalentJson(db, Meta(db, "header"), D::Encode(header),
                          "pin artifact header differs");
    Sha256 records, members;
    I member_count = 0;
    std::string records_hash, members_hash;
    const bool binding = kind == "chimew-binding-store-v1";
    if (!binding) {
      auto q =
          db.Prepare(kind == "pin-plan-store-v1"
                         ? "SELECT "
                           "seq,schedule_entry,group_id,physical_lane,logical_"
                           "lane,record_json FROM entries ORDER BY seq"
                         : "SELECT "
                           "seq,schedule_entry,source_y,sink_y,source_region,"
                           "sink_region,source_fallback,sink_fallback,record_"
                           "json FROM entries ORDER BY seq");
      while (q.Step()) {
        Stop(cancelled);
        Check(q.TypedInteger(0) == count, "pin record sequence differs");
        auto raw = q.TypedText(binding                       ? 0
                               : kind == "pin-plan-store-v1" ? 5
                                                             : 8);
        D record(raw);
        Check(!q.TypedText(1).empty() &&
                  record.Text("schedule_entry") == q.TypedText(1),
              "pin artifact row identity differs");
        if (kind == "pin-plan-store-v1") {
          Check(record.Integer("group") == q.TypedInteger(2) &&
                    record.Integer("physical_lane") == q.TypedInteger(3) &&
                    record.Integer("logical_lane") == q.TypedInteger(4),
                "pin artifact typed columns differ");
        } else {
          I regions = Int(header, "region_count");
          Check(regions >= 1 && regions <= 31,
                "position hints have invalid region count");
          for (int c = 2; c <= 3; ++c) {
            const char *field = c == 2 ? "source_y" : "sink_y";
            double value = q.TypedReal(c);
            Check(value >= 0 && value <= 1 && record.Number(field) == value,
                  "position typed coordinates differ");
          }
          for (int c = 4; c <= 5; ++c) {
            I value = q.TypedInteger(c);
            Check(value >= 0 && value < regions &&
                      record.Integer(c == 4 ? "source_region"
                                            : "sink_region") == value,
                  "position typed region differs");
          }
          for (int c = 6; c <= 7; ++c) {
            I value = q.TypedInteger(c);
            Check((value == 0 || value == 1) &&
                      record.Boolean(c == 6 ? "source_fallback"
                                            : "sink_fallback") == bool(value),
                  "position typed flags differ");
          }
        }
        Hash(records, raw + "\n");
        ++count;
      }
    } else {
      auto groups = db.Prepare(
          "SELECT "
          "seq,group_id,link_id,fpga_a,fpga_b,physical_lane,direction,package_"
          "pin_a,package_pin_b,group_json FROM groups ORDER BY seq");
      auto child = db.Prepare("SELECT group_seq,member_seq,member FROM members "
                              "ORDER BY group_seq,member_seq");
      bool more = child.Step();
      O full_header = header;
      full_header["entries"] = "[]";
      Sha256 all;
      Hash(all, "{");
      bool comma = false;
      for (const auto &field : full_header) {
        if (comma)
          Hash(all, ",");
        comma = true;
        Hash(all, JsonQuote(field.first) + ":");
        if (field.first != "entries") {
          Hash(all, field.second);
          continue;
        }
        Hash(all, "[");
        while (groups.Step()) {
          Stop(cancelled);
          Check(groups.TypedInteger(0) == count,
                "binding group sequence differs");
          auto raw = groups.TypedText(9);
          D record(raw);
          Check(!record.Has("schedule_entries"),
                "binding membership is duplicated in group JSON");
          for (auto p :
               std::vector<std::pair<const char *, int>>{{"group", 1},
                                                         {"link", 2},
                                                         {"fpga_a", 3},
                                                         {"fpga_b", 4},
                                                         {"direction", 6},
                                                         {"package_pin_a", 7},
                                                         {"package_pin_b", 8}})
            Check(record.Text(p.first) == groups.TypedText(p.second) &&
                      !groups.TypedText(p.second).empty(),
                  "binding typed group identity differs");
          Check(record.Integer("physical_lane") == groups.TypedInteger(5),
                "binding typed lane differs");
          auto full = Fields(raw);
          std::vector<std::string> ids;
          I index = 0;
          while (more && child.TypedInteger(0) == count) {
            Check(child.TypedInteger(1) == index && !child.TypedText(2).empty(),
                  "binding member sequence differs");
            ids.push_back(JsonQuote(child.TypedText(2)));
            Hash(members, D::Encode({{"group_seq", std::to_string(count)},
                                     {"member_seq", std::to_string(index++)},
                                     {"member", ids.back()}}) +
                              "\n");
            ++member_count;
            more = child.Step();
          }
          Check(index > 0, "binding group is empty");
          full["schedule_entries"] = Array(ids);
          const auto encoded = D::Encode(full);
          Hash(records, encoded + "\n");
          if (count)
            Hash(all, ",");
          Hash(all, encoded);
          ++count;
        }
        Check(!more, "orphan binding member");
        Hash(all, "]");
      }
      Hash(all, "}");
      canonical = Hex(all);
    }
    records_hash = Hex(records);
    O entries{{"records", std::to_string(count)},
              {"sha256", JsonQuote(records_hash)}};
    RequireEquivalentJson(db, doc.Token("entries"), D::Encode(entries),
                          "pin artifact record seal differs");
    RequireEquivalentJson(db, Meta(db, "entries_sha256"),
                          JsonQuote(records_hash),
                          "pin artifact stored record seal differs");
    O envelope{{"header", D::Encode(header)}, {"entries", D::Encode(entries)}};
    if (binding) {
      members_hash = Hex(members);
      auto descriptor = D::Encode({{"records", std::to_string(member_count)},
                                   {"sha256", JsonQuote(members_hash)}});
      envelope["members"] = descriptor;
      RequireEquivalentJson(db, doc.Token("members"), descriptor,
                            "binding member logical seal differs");
      RequireEquivalentJson(db, Meta(db, "members_sha256"),
                            JsonQuote(members_hash),
                            "binding stored member seal differs");
      Check(doc.Text("canonical_sha256") == canonical &&
                DecodeJsonString(Meta(db, "canonical_sha256")) == canonical,
            "binding canonical seal differs");
    }
    logical = Digest(D::Encode(envelope));
    Check(doc.Text("logical_sha256") == logical &&
              DecodeJsonString(Meta(db, "logical_sha256")) == logical,
          "pin artifact logical seal differs");
    Check(Scalar(db, "SELECT count(*) FROM metadata") == (binding ? 5 : 3),
          "unknown pin artifact metadata");
    for (const auto &ref : refs)
      ref.RequireUnchanged();
  }
};

struct Context {
  const volatile std::sig_atomic_t *cancelled;
  std::vector<ArtifactRef> refs;
  std::map<std::string, ArtifactRef> roles;
  Board board;
  O request;
  explicit Context(const volatile std::sig_atomic_t *flag) : cancelled(flag) {}
  void Read(const ArtifactRef &source, const char *schema) {
    refs = {source};
    D input(D::Read(source));
    Check(input.Text("schema") == schema, "request schema differs");
    request = input.Fields();
    for (const auto &r : Fields(input.Token("sources"))) {
      D ref(r.second);
      ref.Keys({"path", "schema"});
      Check(!fs::is_symlink(ref.Text("path")), "input source is a symlink");
      auto bound = ArtifactRef::Bind(ref.Text("path"), ref.Text("schema"));
      roles.emplace(r.first, bound);
      refs.push_back(bound);
    }
    Check(roles.count("board"), "missing board input");
    Check(roles.at("board").schema() == "boarddb-v1",
          "board input role differs");
    std::optional<ArtifactRef> inventory, wiring;
    if (roles.count("inventory")) {
      inventory = roles.at("inventory");
      Check(inventory->schema() == "hp-package-pin-inventory-v1",
            "pin inventory input role differs");
    }
    if (roles.count("wiring")) {
      wiring = roles.at("wiring");
      Check(wiring->schema() == "fixed-wiring-csv-v1",
            "fixed wiring input role differs");
    }
    board.Read(D::Read(roles.at("board")), inventory, wiring, cancelled);
  }
  ArtifactRef Role(const std::string &role, const std::string &schema) {
    Check(roles.count(role) && roles.at(role).schema() == schema,
          "missing or wrong input role " + role);
    return roles.at(role);
  }
  void Add(const std::vector<ArtifactRef> &sources) {
    refs.insert(refs.end(), sources.begin(), sources.end());
  }
};

std::string QualifySeal(const O &certificate, const Schedule &schedule,
                        const std::string &bank_input_hash = "",
                        const std::string &bank_report_hash = "",
                        const O &bank_provenance = {},
                        const O &electrical_provenance = {}) {
  bool bound = Get(certificate, "schema") ==
               JsonQuote("emuflow.chimew-phase6-qualification/v2");
  Check(
      (bound || Get(certificate, "schema") ==
                    JsonQuote("emuflow.chimew-phase6-qualification/v1")) &&
          Text(certificate, "provider") ==
              (bound
                   ? "chimew-paper-kernel-chain-plus-emuflow-byte-provenance-v2"
                   : "chimew-paper-kernel-chain-plus-emuflow-provenance-v1") &&
          Text(certificate, "status") == "pass" &&
          Text(certificate, "design") == schedule.design &&
          Text(certificate, "platform") == schedule.platform,
      "qualification certificate is invalid");
  O unsigned_certificate = certificate;
  auto hash = Text(certificate, "qualification_sha256");
  unsigned_certificate.erase("qualification_sha256");
  Check(hash == Digest(D::Encode(unsigned_certificate)),
        "qualification self-seal is invalid");
  auto artifacts = Fields(Get(certificate, "artifacts")),
       provenance = Fields(Get(certificate, "provenance")),
       metrics = Fields(Get(certificate, "metrics"));
  Check(Text(artifacts, "schedule") == schedule.logical &&
            Int(metrics, "artifact_chain_disagreements") == 0 &&
            Int(metrics, "rudy_overloaded_bins") == 0,
        "qualification schedule binding or gates differ");
  if (!bank_input_hash.empty())
    Check(Text(artifacts, "bank_channel_input") == bank_input_hash &&
              Text(artifacts, "refined_grouping") ==
                  Text(bank_provenance, "grouping_sha256") &&
              Text(provenance, "placement_sha256") ==
                  Text(bank_provenance, "placement_sha256") &&
              Text(provenance, "architecture_sha256") ==
                  Text(bank_provenance, "architecture_sha256"),
          "qualification binding is broken");
  if (!bank_report_hash.empty())
    Check(Text(artifacts, "bank_channel_report") == bank_report_hash,
          "assignment report does not match qualification certificate");
  if (bound) {
    D source(Get(certificate, "source_binding"));
    source.Keys({"scope", "digests"});
    Check(source.Text("scope") == "byte-bound-source-artifacts",
          "invalid source binding scope");
    auto digests = Fields(source.Token("digests"));
    std::set<std::string> required{"routing", "placement", "netlist",
                                   "architecture", "package_pins"};
    for (const auto &p : digests) {
      Check(required.count(p.first) || p.first == "timing_paths",
            "unknown source binding digest");
      auto value = DecodeJsonString(p.second);
      Check(IsSha(value), "invalid source binding digest");
      required.erase(p.first);
      if (provenance.count(p.first + "_sha256"))
        Check(p.second == provenance.at(p.first + "_sha256"),
              "source binding provenance differs");
    }
    Check(required.empty() && Text(certificate, "source_binding_sha256") ==
                                  Digest(CanonicalJsonToken(source.text())),
          "source binding seal or inventory differs");
    if (!electrical_provenance.empty())
      Check(Text(digests, "package_pins") ==
                Text(electrical_provenance, "package_pin_inventory_sha256"),
            "byte-bound package-pin provenance does not agree");
  } else
    Check(!certificate.count("source_binding") &&
              !certificate.count("source_binding_sha256"),
          "legacy qualification contains source binding");
  return bound ? "byte-bound-source-artifacts"
               : "declared-digest-artifact-chain";
}

struct GroupStats {
  std::uint64_t mask = 0;
  I n = 0;
  double mean = 0, sse = 0;
  void Add(double source, double sink, I sr, I tr) {
    mask |= (std::uint64_t(1) << sr) | (std::uint64_t(1) << (32 + tr));
    for (double value : {source, sink}) {
      ++n;
      double delta = value - mean;
      mean += delta / n;
      sse += delta * (value - mean);
    }
  }
};
O Metrics(SqliteDatabase &db, const std::string &table, bool baseline,
          double crossing = 1, double position = 1) {
  auto q = db.Prepare(
      "SELECT "
      "entry_id,group_index,physical_lane,source_y,sink_y,source_region,sink_"
      "region,link_id,source_fpga,sink_fpga,logical_lane,capacity FROM " +
      table + " ORDER BY entry_id");
  std::map<std::string, GroupStats> stats;
  std::vector<std::string> order;
  double distance = 0;
  while (q.Step()) {
    std::string group =
        baseline ? D::Encode({{"link", JsonQuote(q.TypedText(7))},
                              {"from", JsonQuote(q.TypedText(8))},
                              {"to", JsonQuote(q.TypedText(9))},
                              {"lane", std::to_string(q.TypedInteger(10))}})
                 : std::to_string(q.TypedInteger(1));
    auto inserted = stats.emplace(group, GroupStats{});
    if (inserted.second)
      order.push_back(group);
    double sy = q.TypedReal(3), ty = q.TypedReal(4);
    inserted.first->second.Add(sy, ty, q.TypedInteger(5), q.TypedInteger(6));
    I capacity = q.TypedInteger(11), pin = q.TypedInteger(baseline ? 10 : 2);
    double y = capacity == 1 ? 0.5 : double(pin) / double(capacity - 1);
    distance += std::abs(sy - y);
    distance += std::abs(ty - y);
  }
  I bits = 0;
  double sse = 0;
  for (const auto &key : order) {
    bits += __builtin_popcountll(stats.at(key).mask);
    sse += stats.at(key).sse;
  }
  return {{"objective", Number(crossing * double(bits) + position * sse)},
          {"crossing_bits", Number(double(bits))},
          {"position_sse", Number(sse)},
          {"pin_distance", Number(distance)}};
}
O PinSummary(I count, I groups, const O &metrics, const O &baseline) {
  O result{{"status", JsonQuote("pass")},
           {"signals", std::to_string(count)},
           {"groups", std::to_string(groups)},
           {"physical_lane_slot_collisions", "0"}};
  for (const auto &p : metrics)
    result[p.first] = p.second;
  for (const auto &p : baseline)
    result["baseline_logical_lane_" + p.first] = p.second;
  double objective = Num(baseline, "objective"),
         distance = Num(baseline, "pin_distance");
  result["objective_improvement_percent"] = Number(
      objective ? 100.0 * (objective - Num(metrics, "objective")) / objective
                : 0.0);
  result["crossing_bits_reduction"] =
      Number(Num(baseline, "crossing_bits") - Num(metrics, "crossing_bits"));
  result["pin_distance_improvement_percent"] = Number(
      distance ? 100.0 * (distance - Num(metrics, "pin_distance")) / distance
               : 0.0);
  return result;
}

struct Validation {
  Context &context;
  SqliteDatabase db;
  Schedule schedule;
  StoredArtifact plan, positions, binding;
  O pin_result, binding_result;
  bool have_positions = false, have_binding = false;
  Validation(Context &c, const fs::path &work)
      : context(c), db((work / "validation.sqlite").string(),
                       SqliteDatabase::Mode::kCreateExclusive),
        schedule(db) {
    db.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA "
               "temp_store=FILE; BEGIN");
    db.Execute(
        "CREATE TABLE records(document INTEGER,field TEXT,seq INTEGER,token "
        "TEXT,PRIMARY KEY(document,field,seq)) WITHOUT ROWID");
  }
  void Run() {
    schedule.Read(context.Role("schedule", "physical-input-document-v1"),
                  context.cancelled);
    context.Add(schedule.refs);
    schedule.BoardRules(context.board);
    plan.Open(context.Role("plan", "pin-plan-selector-v1"), "pin-plan-store-v1",
              context.cancelled);
    context.Add(plan.refs);
    Attach(db, *plan.store, "planned");
    Check(Text(plan.header, "schema") == pin_schema &&
              Text(plan.header, "provider") == provider,
          "pin plan provider is not source-complete");
    Check(Text(plan.header, "design") == schedule.design &&
              Text(plan.header, "platform") == context.board.name,
          "pin plan does not match schedule design/platform");
    NoRows(
        db,
        "SELECT s.entry_id FROM schedule s LEFT JOIN planned.entries p ON "
        "p.schedule_entry=s.entry_id WHERE p.schedule_entry IS NULL UNION ALL "
        "SELECT p.schedule_entry FROM planned.entries p LEFT JOIN schedule s "
        "ON s.entry_id=p.schedule_entry WHERE s.entry_id IS NULL LIMIT 1",
        "pin plan must cover schedule entries exactly");
    NoRows(db,
           "SELECT s.entry_id FROM schedule s LEFT JOIN links l ON "
           "l.link_id=s.link_id WHERE l.link_id IS NULL LIMIT 1",
           "pin assignment references an unknown link");
    NoRows(db,
           "SELECT s.entry_id FROM schedule s JOIN planned.entries p ON "
           "p.schedule_entry=s.entry_id JOIN links l ON l.link_id=s.link_id "
           "WHERE p.group_id<0 OR p.physical_lane<0 OR "
           "p.physical_lane>=l.capacity OR p.logical_lane!=s.lane LIMIT 1",
           "pin assignment is invalid or changed logical lane");
    NoRows(
        db,
        "SELECT s.entry_id FROM schedule s JOIN planned.entries p ON "
        "p.schedule_entry=s.entry_id JOIN links l ON l.link_id=s.link_id LEFT "
        "JOIN budgets b ON b.link_id=s.link_id AND b.source_fpga=s.source_fpga "
        "AND b.sink_fpga=s.sink_fpga WHERE l.bank_width>0 AND (b.low IS NULL "
        "OR s.lane<b.low OR s.lane>=b.high OR p.physical_lane<b.low OR "
        "p.physical_lane>=b.high) LIMIT 1",
        "pin assignment leaves the schedule's allocated direction banks");
    have_positions = context.roles.count("positions");
    have_binding = context.roles.count("binding");
    Check(have_positions || have_binding,
          "pin validation requires positions or electrical binding");
    if (have_positions) {
      positions.Open(context.Role("positions", "position-hints-selector-v1"),
                     "position-hints-store-v1", context.cancelled);
      context.Add(positions.refs);
      Attach(db, *positions.store, "positioned");
      Check(Text(positions.header, "design") == schedule.design &&
                Text(positions.header, "platform") == schedule.platform,
            "position hints do not match schedule design/platform");
      auto source = Text(positions.header, "provider");
      Check(
          source == "openparf-lookahead-centroid-v1" ||
              source == "chimew-physical-site-projection-v1",
          "position hints are not from a supported source-qualified provider");
      if (source == "chimew-physical-site-projection-v1") {
        Check(Text(positions.header, "source_coordinate_system") ==
                  "physical-site-xy",
              "Chimew position projection is not physical-site based");
        auto p = Fields(Get(positions.header, "provenance"));
        Text(p, "producer");
        Text(p, "producer_version");
        for (auto key : {"assignment_input_sha256", "placement_sha256",
                         "architecture_sha256"})
          Check(IsSha(Text(p, key)), "invalid position projection provenance");
      }
      NoRows(db,
             "SELECT s.entry_id FROM schedule s LEFT JOIN positioned.entries p "
             "ON p.schedule_entry=s.entry_id WHERE p.schedule_entry IS NULL "
             "UNION ALL SELECT p.schedule_entry FROM positioned.entries p LEFT "
             "JOIN schedule s ON s.entry_id=p.schedule_entry WHERE s.entry_id "
             "IS NULL LIMIT 1",
             "position hints must cover schedule entries exactly");
      auto metric = Fields(Get(positions.header, "metrics"));
      Check(Int(metric, "signals") == schedule.count &&
                Int(metric, "endpoint_centroid_fallbacks") ==
                    Scalar(
                        db,
                        "SELECT coalesce(sum(source_fallback+sink_fallback),0) "
                        "FROM positioned.entries"),
            "position hint metrics do not agree");
    }
    db.Execute(
        "CREATE TEMP VIEW validation AS SELECT s.entry_id,p.group_id AS "
        "group_index,p.physical_lane,s.link_id,s.source_fpga,s.sink_fpga,s."
        "lane AS logical_lane,s.slot,s.ratio,l.capacity,l.shared" +
        std::string(have_positions
                        ? ",t.source_y,t.sink_y,t.source_region,t.sink_region"
                        : "") +
        " FROM schedule s JOIN planned.entries p ON "
        "p.schedule_entry=s.entry_id JOIN links l ON l.link_id=s.link_id" +
        (have_positions
             ? " JOIN positioned.entries t ON t.schedule_entry=s.entry_id"
             : ""));
    if (have_positions)
      PinCheck();
    if (have_binding) {
      binding.Open(context.Role("binding", "chimew-binding-selector-v1"),
                   "chimew-binding-store-v1", context.cancelled);
      context.Add(binding.refs);
      Attach(db, *binding.store, "bound");
      BindingCheck();
    }
    db.Execute("COMMIT");
    for (const auto &ref : context.refs)
      ref.RequireUnchanged();
  }
  void PinCheck() {
    O configuration = Fields(Get(plan.header, "configuration")),
      weights = Fields(Get(plan.header, "weights")),
      metric = Fields(Get(plan.header, "metrics"));
    Check(Int(configuration, "refinement_iterations") >= 0,
          "pin plan refinement iteration count is invalid");
    double crossing = Num(weights, "crossing"),
           position = Num(weights, "position");
    Check(crossing >= 0 && position >= 0, "pin plan weights are invalid");
    NoRows(db,
           "SELECT link_id,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
           "source_fpga END,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
           "sink_fpga END,physical_lane,slot FROM validation GROUP BY "
           "1,2,3,4,5 HAVING count(*)>1 LIMIT 1",
           "physical lane/slot collision");
    NoRows(db,
           "SELECT link_id,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
           "source_fpga END,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
           "sink_fpga END,physical_lane FROM validation GROUP BY 1,2,3,4 "
           "HAVING min(group_index)!=max(group_index) LIMIT 1",
           "physical pin is assigned to multiple groups");
    auto groups = db.Prepare(
        "SELECT group_index,count(DISTINCT "
        "json_array(link_id,source_fpga,sink_fpga)),count(DISTINCT "
        "physical_lane),count(DISTINCT link_id),count(DISTINCT "
        "json_array(link_id,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
        "source_fpga END,CASE WHEN shared THEN 'shared_bidirectional' ELSE "
        "sink_fpga END)),min(link_id) FROM validation GROUP BY group_index "
        "ORDER BY group_index");
    I count = 0;
    while (groups.Step()) {
      ++count;
      const auto &link = context.board.links.at(groups.TypedText(5));
      bool bundle = !link.banks && groups.TypedInteger(1) == 2 &&
                    groups.TypedInteger(3) == 1 &&
                    groups.TypedInteger(4) == 1 &&
                    link.direction == "full_duplex" &&
                    link.sharing == "shared_bidirectional";
      Check((groups.TypedInteger(1) == 1 || bundle) &&
                groups.TypedInteger(2) == 1,
            "group is not homogeneous");
    }
    NoRows(db,
           "SELECT group_index FROM validation GROUP BY "
           "group_index,link_id,source_fpga,sink_fpga HAVING "
           "min(ratio)!=max(ratio) OR count(*)>min(ratio) LIMIT 1",
           "group violates TDM capacity");
    NoRows(db,
           "SELECT group_index FROM validation GROUP BY group_index,slot "
           "HAVING count(*)>1 LIMIT 1",
           "group violates TDM capacity");
    auto actual = Metrics(db, "validation", false, crossing, position),
         baseline = Metrics(db, "validation", true, crossing, position);
    for (const auto &p : actual)
      Check(Agree(Num(metric, p.first), ParseFiniteDecimal(p.second, "metric")),
            "pin plan metric " + p.first + " does not independently agree");
    for (const auto &p : baseline)
      Check(Agree(Num(metric, "baseline_logical_lane_" + p.first),
                  ParseFiniteDecimal(p.second, "metric")),
            "pin plan baseline metric does not independently agree");
    Check(Int(metric, "signals") == schedule.count &&
              Int(metric, "groups") == count,
          "pin plan count metrics do not agree");
    pin_result = PinSummary(schedule.count, count, actual, baseline);
  }
  void BindingCheck() {
    O h = binding.header, provenance = Fields(Get(h, "provenance")),
      configuration = Fields(Get(plan.header, "configuration"));
    auto schema = Text(h, "schema");
    bool fixed = schema == "emuflow.chimew-phase6-electrical-binding/v3";
    Check(fixed == context.board.fixed,
          "fixed LVDS BoardDB requires differential Chimew binding");
    Check(Text(h, "status") == "pass" &&
              Text(h, "integration_status") == "phase6-pin-plan" &&
              Text(h, "provider") == provider &&
              Text(h, "design") == schedule.design &&
              Text(h, "platform") == context.board.name,
          "electrical binding provider or identity differs");
    Text(h, "paper_provider");
    Text(provenance, "producer");
    Text(provenance, "producer_version");
    for (auto key : {"boarddb_sha256", "package_pin_inventory_sha256",
                     "electrical_map_sha256", "assignment_input_sha256",
                     "schedule_sha256"})
      Sha(Text(provenance, key));
    Check(Text(provenance, "schedule_sha256") == schedule.logical &&
              Text(configuration, "schedule_sha256") == schedule.logical &&
              Get(provenance, "electrical_map_sha256") ==
                  Get(configuration, "electrical_map_sha256"),
          "electrical provenance does not agree");
    auto status = DecodeJsonString(Get(configuration, "lookahead_qualification",
                                       JsonQuote("bank-electrical-only")));
    if (status == "complete-artifact-chain") {
      auto certificate = Fields(Get(h, "lookahead_qualification"));
      auto scope = QualifySeal(certificate, schedule, "", "", {}, provenance);
      Check(Text(configuration, "qualification_sha256") ==
                    Text(certificate, "qualification_sha256") &&
                Get(provenance, "qualification_sha256") ==
                    Get(configuration, "qualification_sha256"),
            "qualification binding does not agree");
      bool omitted = scope == "declared-digest-artifact-chain" &&
                     !configuration.count("qualification_scope") &&
                     !provenance.count("qualification_scope");
      Check(
          omitted ||
              (Get(configuration, "qualification_scope") == JsonQuote(scope) &&
               Get(provenance, "qualification_scope") == JsonQuote(scope)),
          "embedded qualification scope differs");
    } else {
      Check(status == "bank-electrical-only",
            "lookahead qualification status is invalid");
      for (const auto &p : {configuration, provenance})
        Check(!p.count("qualification_sha256") &&
                  !p.count("qualification_scope"),
              "partial binding contains qualification seal");
      Check(!h.count("lookahead_qualification"),
            "partial binding contains qualification seal");
    }
    std::map<std::string, std::pair<double, double>> bounds;
    if (schema != "emuflow.chimew-phase6-electrical-binding/v1")
      VisitJsonArrayTokens(Get(h, "fpga_y_bounds"), [&](const auto &raw) {
        D row(raw);
        double low = row.Number("y_min"), high = row.Number("y_max");
        Check(high > low &&
                  bounds.emplace(row.Text("fpga"), std::make_pair(low, high))
                      .second,
              "binding FPGA bounds are invalid");
      });
    if (fixed)
      Check(CanonicalJsonToken(Get(h, "fixed_wiring")) ==
                context.board.fixed_source,
            "fixed wiring source differs from BoardDB");
    auto groups =
        db.Prepare("SELECT group_json FROM bound.groups ORDER BY seq");
    std::set<std::pair<std::string, std::string>> pins;
    std::map<std::pair<std::string, I>, std::set<std::string>> lane_uses;
    I count = 0;
    while (groups.Step()) {
      Stop(context.cancelled);
      auto row = Fields(groups.TypedText(0));
      auto a = Text(row, "fpga_a"), b = Text(row, "fpga_b"),
           linkid = Text(row, "link"), direction = Text(row, "direction");
      const auto &link = context.board.GetLink(linkid, a, b);
      I lane = Int(row, "physical_lane");
      Check(lane >= 0 && lane < link.width, "binding physical lane is invalid");
      Check(direction == "a_to_b" || direction == "b_to_a" ||
                direction == "bidirectional",
            "binding direction is invalid");
      Check(direction != "bidirectional" ||
                (!link.banks && link.direction == "full_duplex" &&
                 link.sharing == "shared_bidirectional"),
            "bidirectional bundle requires shared full-duplex capacity without "
            "HP-bank directions");
      auto &used = lane_uses[{linkid, lane}];
      Check(!used.count(direction) &&
                (used.empty() || (direction != "bidirectional" &&
                                  !used.count("bidirectional") &&
                                  link.direction == "full_duplex" &&
                                  link.sharing == "per_direction")),
            "binding reuses a concrete lane");
      used.insert(direction);
      if (fixed)
        context.board.ValidateFixed(row, a, b);
      for (const auto &key : context.board.Pins(row))
        Check(pins.insert(key).second, "binding reuses a package pin");
      Text(row, "bank_pair");
      Text(row, "channel");
      Text(row, "bank_a");
      Text(row, "bank_b");
      auto standard = Text(row, "iostandard");
      std::set<std::string> supported;
      VisitJsonArrayTokens(
          Get(row, "supported_iostandards"), [&](const auto &value) {
            Check(supported.insert(DecodeJsonString(value)).second,
                  "binding IOSTANDARD is invalid");
          });
      Check(supported.count(standard) && (fixed || voltages.count(standard)),
            "binding IOSTANDARD is invalid");
      Check(
          (fixed || Text(row, "electrical_class") == "single_ended_parallel") &&
              std::abs(Num(row, "bank_voltage") -
                       (fixed ? 1.8 : voltages.at(standard))) <= 1e-9,
          "binding voltage is invalid");
      if (schema != "emuflow.chimew-phase6-electrical-binding/v1")
        for (const auto &endpoint :
             std::vector<std::pair<std::string, std::string>>{{"a", a},
                                                              {"b", b}}) {
          Check(bounds.count(endpoint.second),
                "binding physical pin point is missing");
          Check(row.count("pin_" + endpoint.first + "_point"),
                "binding physical pin point is missing");
          auto point = Fields(Get(row, "pin_" + endpoint.first + "_point"));
          Num(point, "x");
          double y = Num(point, "y");
          Check(y >= bounds.at(endpoint.second).first - 1e-9 &&
                    y <= bounds.at(endpoint.second).second + 1e-9,
                "binding pin lies outside FPGA bounds");
        }
      auto anchor = Get(row, "placement_anchor", "true");
      Check(anchor == "true" || anchor == "false",
            "binding placement anchor is invalid");
      ++count;
    }
    NoRows(db,
           "SELECT m.member FROM bound.members m LEFT JOIN schedule s ON "
           "s.entry_id=m.member LEFT JOIN planned.entries p ON "
           "p.schedule_entry=m.member WHERE s.entry_id IS NULL OR "
           "p.schedule_entry IS NULL LIMIT 1",
           "electrical binding conflicts with pin plan");
    Check(Scalar(db, "SELECT count(*) FROM bound.members") == schedule.count,
          "electrical binding coverage is incomplete");
    NoRows(db,
           "SELECT m.member FROM bound.members m JOIN bound.groups g ON "
           "g.seq=m.group_seq JOIN schedule s ON s.entry_id=m.member JOIN "
           "planned.entries p ON p.schedule_entry=m.member WHERE "
           "s.link_id!=g.link_id OR p.physical_lane!=g.physical_lane OR "
           "NOT((s.source_fpga=g.fpga_a AND s.sink_fpga=g.fpga_b) "
           "OR(s.source_fpga=g.fpga_b AND s.sink_fpga=g.fpga_a)) LIMIT 1",
           "electrical binding conflicts with pin plan or direction");
    NoRows(db,
           "SELECT m.group_seq FROM bound.members m JOIN schedule s ON "
           "s.entry_id=m.member GROUP BY m.group_seq,s.slot HAVING count(*)>1 "
           "LIMIT 1",
           "electrical binding conflicts with pin plan");
    NoRows(db,
           "SELECT m.group_seq FROM bound.members m JOIN bound.groups g ON "
           "g.seq=m.group_seq JOIN schedule s ON s.entry_id=m.member GROUP BY "
           "m.group_seq,CASE WHEN s.source_fpga=g.fpga_a AND "
           "s.sink_fpga=g.fpga_b THEN 'a_to_b' ELSE 'b_to_a' END HAVING "
           "min(s.ratio)!=max(s.ratio) OR count(*)>min(s.ratio) LIMIT 1",
           "electrical binding TDM bundle is invalid");
    NoRows(db,
           "SELECT g.seq FROM bound.groups g JOIN bound.members m ON "
           "m.group_seq=g.seq JOIN schedule s ON s.entry_id=m.member GROUP BY "
           "g.seq,g.direction HAVING (g.direction='bidirectional' AND "
           "count(DISTINCT CASE WHEN s.source_fpga=g.fpga_a AND "
           "s.sink_fpga=g.fpga_b THEN 'a_to_b' ELSE 'b_to_a' END)!=2) OR "
           "(g.direction!='bidirectional' AND (count(DISTINCT CASE WHEN "
           "s.source_fpga=g.fpga_a AND s.sink_fpga=g.fpga_b THEN 'a_to_b' ELSE "
           "'b_to_a' END)!=1 OR min(CASE WHEN s.source_fpga=g.fpga_a AND "
           "s.sink_fpga=g.fpga_b THEN 'a_to_b' ELSE 'b_to_a' "
           "END)!=g.direction)) LIMIT 1",
           "electrical binding TDM bundle is invalid");
    auto metrics = Fields(Get(h, "metrics"));
    O expected{{"signals", std::to_string(schedule.count)},
               {"groups", std::to_string(count)},
               {"channels", std::to_string(count)},
               {"package_pins", std::to_string(pins.size())},
               {"lane_slot_collisions", "0"},
               {"package_pin_collisions", "0"}};
    for (const auto &p : expected)
      Check(Int(metrics, p.first) == D::IntegerToken(p.second),
            "electrical metrics do not agree");
    I lanes = 0;
    for (const auto &p : lane_uses)
      lanes += I(p.second.size());
    binding_result = {{"status", JsonQuote("pass")},
                      {"signals", std::to_string(schedule.count)},
                      {"groups", std::to_string(count)},
                      {"concrete_lanes", std::to_string(lanes)},
                      {"package_pins", std::to_string(pins.size())},
                      {"binding_sha256", JsonQuote(binding.canonical)}};
  }
};

struct Published {
  fs::path selector, store;
  O manifest;
};
Published SealStore(const fs::path &selector, const ArtifactRef &store,
                    const std::string &kind, const O &header, I count,
                    const std::string &records_hash, I members = 0,
                    const std::string &member_hash = "",
                    const std::string &canonical = "") {
  O descriptor{{"records", std::to_string(count)},
               {"sha256", JsonQuote(records_hash)}};
  O envelope{{"header", D::Encode(header)}, {"entries", D::Encode(descriptor)}};
  if (kind == "chimew-binding-store-v1")
    envelope["members"] = D::Encode({{"records", std::to_string(members)},
                                     {"sha256", JsonQuote(member_hash)}});
  auto logical = Digest(D::Encode(envelope));
  const auto hash = Sha256File(store);
  auto name = selector.stem().string() + "." + hash + ".sqlite";
  auto payload = selector.parent_path() / name;
  fs::rename(store.path(), payload);
  O manifest{{"schema", JsonQuote(KindStorage(kind))},
             {"logical_schema", header.at("schema")},
             {"artifact", JsonQuote(name)},
             {"application_id", std::to_string(KindId(kind))},
             {"user_version", "1"},
             {"store_bytes", std::to_string(store.generation().bytes)},
             {"store_sha256", JsonQuote(hash)},
             {"header", D::Encode(header)},
             {"entries", D::Encode(descriptor)},
             {"logical_sha256", JsonQuote(logical)}};
  if (kind == "chimew-binding-store-v1") {
    manifest["members"] = envelope.at("members");
    manifest["canonical_sha256"] = JsonQuote(canonical);
  }
  PublishTextArtifact(selector.string(), {}, [&](std::ostream &out) {
    out << D::Encode(manifest) << '\n';
  });
  return {selector, payload, manifest};
}
void StoreMetadata(SqliteDatabase &db, const O &header, I count,
                   const std::string &hash, I members = 0,
                   const std::string &member_hash = "",
                   const std::string &canonical = "") {
  O envelope{{"header", D::Encode(header)},
             {"entries", D::Encode({{"records", std::to_string(count)},
                                    {"sha256", JsonQuote(hash)}})}};
  if (!member_hash.empty())
    envelope["members"] = D::Encode({{"records", std::to_string(members)},
                                     {"sha256", JsonQuote(member_hash)}});
  PutMeta(db, "header", D::Encode(header));
  PutMeta(db, "entries_sha256", JsonQuote(hash));
  PutMeta(db, "logical_sha256", JsonQuote(Digest(D::Encode(envelope))));
  if (!member_hash.empty()) {
    PutMeta(db, "members_sha256", JsonQuote(member_hash));
    PutMeta(db, "canonical_sha256", JsonQuote(canonical));
  }
}
ArtifactRef SourceOf(const std::vector<ArtifactRef> &refs,
                     const std::string &schema) {
  std::optional<ArtifactRef> found;
  for (const auto &ref : refs)
    if (ref.schema() == schema) {
      Check(!found || found->path() == ref.path(), "ambiguous source owner");
      found = ref;
    }
  Check(bool(found), "missing source owner " + schema);
  return *found;
}
double NormalizeY(double value, const std::pair<double, double> &box,
                  const std::string &label) {
  double tolerance =
      1e-9 * std::max({1.0, std::abs(box.first), std::abs(box.second)});
  Check(value >= box.first - tolerance && value <= box.second + tolerance,
        label + ": physical y lies outside FPGA bounds");
  return std::min(
      1.0, std::max(0.0, (value - box.first) / (box.second - box.first)));
}

struct Builder {
  Context &context;
  fs::path root;
  SqliteDatabase db;
  Schedule schedule;
  Electrical electrical;
  ChimewBankValidation proof;
  O bank_header, bank_provenance, certificate, qualification_validation;
  std::map<std::string, std::pair<std::string, std::string>> domains;
  I regions = 31, groups = 0, pin_count = 0;
  std::vector<Published> outputs;
  O pin_result, binding_result;
  Builder(Context &c, const fs::path &root)
      : context(c), root(root), db((root / "build-index.sqlite").string(),
                                   SqliteDatabase::Mode::kCreateExclusive),
        schedule(db) {
    db.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA "
               "temp_store=FILE; BEGIN");
    db.Execute(
        "CREATE TABLE records(document INTEGER,field TEXT,seq INTEGER,token "
        "TEXT,PRIMARY KEY(document,field,seq)) WITHOUT ROWID; CREATE TABLE "
        "raw_groups(seq INTEGER PRIMARY KEY,group_id TEXT UNIQUE,record_json "
        "TEXT); CREATE TABLE build_members(entry_id TEXT PRIMARY "
        "KEY,group_index INTEGER,member_seq INTEGER,member_direction "
        "TEXT,expected_link TEXT,expected_source TEXT,expected_sink "
        "TEXT,physical_lane INTEGER,source_y REAL,sink_y REAL,source_region "
        "INTEGER,sink_region INTEGER,UNIQUE(group_index,member_seq)) WITHOUT "
        "ROWID; CREATE INDEX build_members_group ON "
        "build_members(group_index,member_seq); CREATE TABLE "
        "build_groups(group_index INTEGER PRIMARY KEY,group_id TEXT NOT NULL "
        "UNIQUE,record_json TEXT NOT NULL)");
  }
  void Read() {
    regions = D::IntegerToken(Get(context.request, "region_count", "31"));
    Check(regions >= 1 && regions <= 31, "region_count must be in [1,31]");
    schedule.Read(context.Role("schedule", "physical-input-document-v1"),
                  context.cancelled);
    context.Add(schedule.refs);
    schedule.BoardRules(context.board);
    auto input = context.Role("bank_input", "chimew-bank-input-selector-v1");
    auto manifest = Fields(D::Read(input));
    bank_header = Fields(Get(manifest, "header"));
    bool supplied = context.roles.count("bank_report");
    ArtifactRef report =
        supplied ? context.Role("bank_report", "chimew-bank-report-selector-v1")
                 : GenerateReport(input);
    Check(!supplied || context.roles.count("qualification"),
          "a precomputed assignment report requires qualification");
    auto add = db.Prepare("INSERT INTO raw_groups VALUES(?,?,?)");
    proof = ValidateChimewBankAssignment(
        input, report,
        [&](std::size_t index, const auto &raw) {
          D group(raw);
          add.BindInteger(1, I(index));
          add.BindText(2, group.Text("id"));
          add.BindText(3, raw);
          add.Execute();
          add.Reset();
        },
        context.cancelled);
    bank_provenance = Fields(proof.provenance);
    for (const auto &ref : proof.inputs)
      if (supplied || ref.schema() == "chimew-bank-input-selector-v1" ||
          ref.schema() == "chimew-bank-input-store-v1")
        context.refs.push_back(ref);
    Check(proof.design == schedule.design &&
              proof.platform == context.board.name &&
              schedule.platform == context.board.name,
          "assignment does not match schedule identity");
    auto store = SourceOf(proof.inputs, "chimew-bank-input-store-v1");
    electrical.Read(context.Role("electrical", "physical-input-document-v1"),
                    store, bank_header, context.board, context.cancelled);
    if (Get(context.request, "verify_board_bytes", "false") == "true")
      Check(Text(electrical.provenance, "boarddb_sha256") ==
                Sha256File(context.roles.at("board"), context.cancelled),
            "electrical map BoardDB SHA-256 does not match platform");
    if (context.roles.count("qualification")) {
      certificate = Fields(CanonicalJsonToken(
          D::Read(context.Role("qualification", "chimew-qualification-v1"))));
      auto scope = QualifySeal(certificate, schedule, proof.input_sha256,
                               proof.report_sha256, bank_provenance,
                               electrical.provenance);
      auto metrics = Fields(Get(certificate, "metrics"));
      qualification_validation = {
          {"status", JsonQuote("pass")},
          {"qualification_sha256", certificate.at("qualification_sha256")},
          {"qualification_scope", JsonQuote(scope)},
          {"signals", Get(metrics, "signals")},
          {"groups", Get(metrics, "groups")}};
    }
    VisitJsonArrayTokens(bank_header.at("domains"), [&](const auto &token) {
      D domain(token);
      domains.emplace(domain.Text("id"), std::make_pair(domain.Text("fpga_a"),
                                                        domain.Text("fpga_b")));
    });
    Attach(db, SourceOf(proof.inputs, "chimew-bank-report-store-v1"),
           "solution");
    auto copy = CopyChimewBankReport(
        report, (root / "bank_channel_report.json").string(),
        context.cancelled);
    Check(copy.ok(), copy.message);
    auto rows = db.Prepare(
        "SELECT g.seq,g.record_json,a.bank_pair,a.channel FROM raw_groups g "
        "JOIN solution.assignments a ON a.group_id=g.group_id ORDER BY g.seq");
    auto put =
        db.Prepare("INSERT INTO build_members VALUES(?,?,?,?,?,?,?,?,?,?,?,?)");
    auto putgroup = db.Prepare("INSERT INTO build_groups VALUES(?,?,?)");
    std::set<std::pair<std::string, std::string>> selected_pins;
    while (rows.Step()) {
      Stop(context.cancelled);
      I index = rows.TypedInteger(0);
      Check(index == groups, "assignment group coverage is invalid");
      D group(rows.TypedText(1));
      auto id = group.Text("id"), direction = group.Text("direction");
      Check(electrical.channels.count(rows.TypedText(3)),
            "assignment has no electrical channel");
      auto channel = electrical.channels.at(rows.TypedText(3));
      auto domain = domains.at(group.Text("domain"));
      auto allowed = Text(channel, "direction");
      Check((direction == "bidirectional" && allowed == "either") ||
                (direction != "bidirectional" &&
                 (allowed == "either" || allowed == direction)),
            "assignment electrical direction mismatch");
      Check(Text(channel, "fpga_a") == domain.first &&
                Text(channel, "fpga_b") == domain.second,
            "assignment electrical domain mismatch");
      std::set<std::string> directions;
      I member_seq = 0;
      VisitJsonArrayTokens(group.Token("members"), [&](const auto &token) {
        D member(token);
        auto entry = member.Text("id"), dir = member.Has("direction")
                                                  ? member.Text("direction")
                                                  : direction;
        directions.insert(dir);
        auto source = dir == "a_to_b" ? domain.first : domain.second,
             sink = dir == "a_to_b" ? domain.second : domain.first;
        D fanout(member.Token("fanout"));
        double sy = NormalizeY(fanout.Number("y"), electrical.bounds.at(source),
                               entry + ".source_y"),
               sum = 0;
        I count = 0;
        VisitJsonArrayTokens(member.Token("fanins"), [&](const auto &raw) {
          sum += D(raw).Number("y");
          ++count;
        });
        Check(count > 0 && std::isfinite(sum), "member fanins are invalid");
        double ty = NormalizeY(sum / count, electrical.bounds.at(sink),
                               entry + ".sink_y");
        put.BindText(1, entry);
        put.BindInteger(2, index);
        put.BindInteger(3, member_seq++);
        put.BindText(4, dir);
        put.BindText(5, Text(channel, "link"));
        put.BindText(6, source);
        put.BindText(7, sink);
        put.BindInteger(8, Int(channel, "physical_lane"));
        put.BindReal(9, sy);
        put.BindReal(10, ty);
        put.BindInteger(11, std::min<I>(regions - 1, I(sy * regions)));
        put.BindInteger(12, std::min<I>(regions - 1, I(ty * regions)));
        put.Execute();
        put.Reset();
      });
      Check((direction == "bidirectional" &&
             directions == std::set<std::string>{"a_to_b", "b_to_a"}) ||
                (direction != "bidirectional" &&
                 directions == std::set<std::string>{direction}),
            "group violates schedule TDM capacity");
      for (const auto &pin : context.board.Pins(channel))
        Check(selected_pins.insert(pin).second,
              "selected groups collide on package pins");
      channel["group"] = JsonQuote(id);
      channel["bank_pair"] = JsonQuote(rows.TypedText(2));
      channel["channel"] = JsonQuote(rows.TypedText(3));
      channel["direction"] = JsonQuote(direction);
      putgroup.BindInteger(1, index);
      putgroup.BindText(2, id);
      putgroup.BindText(3, D::Encode(channel));
      putgroup.Execute();
      putgroup.Reset();
      ++groups;
    }
    pin_count = I(selected_pins.size());
    Check(Scalar(db, "SELECT count(*) FROM build_members") == schedule.count &&
              Scalar(db, "SELECT count(*) FROM build_members m JOIN schedule s "
                         "ON s.entry_id=m.entry_id") == schedule.count,
          "groups do not cover schedule exactly");
    NoRows(db,
           "SELECT m.entry_id FROM build_members m JOIN schedule s ON "
           "s.entry_id=m.entry_id WHERE m.expected_link!=s.link_id OR "
           "m.expected_source!=s.source_fpga OR m.expected_sink!=s.sink_fpga "
           "LIMIT 1",
           "member schedule domain does not agree");
    NoRows(db,
           "SELECT m.group_index FROM build_members m JOIN schedule s ON "
           "s.entry_id=m.entry_id GROUP BY m.group_index,s.slot HAVING "
           "count(*)>1 LIMIT 1",
           "group has a TDM slot collision");
    NoRows(db,
           "SELECT m.group_index FROM build_members m JOIN schedule s ON "
           "s.entry_id=m.entry_id GROUP BY m.group_index,m.member_direction "
           "HAVING min(s.ratio)!=max(s.ratio) OR count(*)>min(s.ratio) LIMIT 1",
           "group violates schedule TDM capacity");
    db.Execute(
        "CREATE VIEW build_rows AS SELECT "
        "m.entry_id,m.group_index,m.physical_lane,m.source_y,m.sink_y,m.source_"
        "region,m.sink_region,s.link_id,s.source_fpga,s.sink_fpga,s.lane AS "
        "logical_lane,l.capacity FROM build_members m JOIN schedule s ON "
        "s.entry_id=m.entry_id JOIN links l ON l.link_id=s.link_id");
    Check(Scalar(db, "SELECT count(*) FROM build_rows") == schedule.count,
          "assignment link inventory differs");
  }
  ArtifactRef GenerateReport(const ArtifactRef &input) {
    auto path = root / "generated-bank-report.json";
    auto result =
        AssignChimewBankStore(input, path.string(), context.cancelled);
    Check(result.ok(), result.message);
    return ArtifactRef::Bind(path.string(), "chimew-bank-report-selector-v1");
  }
  void Publish() {
    if (db.InTransaction())
      db.Execute("COMMIT");
    O position_header{
        {"schema", JsonQuote(position_schema)},
        {"design", JsonQuote(schedule.design)},
        {"platform", JsonQuote(context.board.name)},
        {"provider", JsonQuote("chimew-physical-site-projection-v1")},
        {"source_coordinate_system", JsonQuote("physical-site-xy")},
        {"region_count", std::to_string(regions)},
        {"provenance",
         D::Encode(
             {{"producer", bank_provenance.at("producer")},
              {"producer_version", bank_provenance.at("producer_version")},
              {"assignment_input_sha256", JsonQuote(proof.input_sha256)},
              {"placement_sha256", bank_provenance.at("placement_sha256")},
              {"architecture_sha256",
               bank_provenance.at("architecture_sha256")}})},
        {"metrics", D::Encode({{"signals", std::to_string(schedule.count)},
                               {"endpoint_centroid_fallbacks", "0"}})}};
    {
      SqliteArtifactWriter writer((root / "positions.sqlite").string(),
                                  "position-hints-store-v1", context.refs);
      auto &target = writer.database();
      target.Execute(positions_ddl);
      target.Execute("BEGIN");
      auto add =
          target.Prepare("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?)");
      auto rows = db.Prepare(
          "SELECT entry_id,source_y,sink_y,source_region,sink_region FROM "
          "build_members ORDER BY entry_id");
      Sha256 hash;
      I seq = 0;
      while (rows.Step()) {
        Stop(context.cancelled);
        O record{{"schedule_entry", JsonQuote(rows.TypedText(0))},
                 {"source_y", Number(rows.TypedReal(1))},
                 {"sink_y", Number(rows.TypedReal(2))},
                 {"source_region", std::to_string(rows.TypedInteger(3))},
                 {"sink_region", std::to_string(rows.TypedInteger(4))},
                 {"source_fallback", "false"},
                 {"sink_fallback", "false"}};
        auto encoded = D::Encode(record);
        Hash(hash, encoded + "\n");
        add.BindInteger(1, seq++);
        add.BindText(2, rows.TypedText(0));
        add.BindReal(3, rows.TypedReal(1));
        add.BindReal(4, rows.TypedReal(2));
        add.BindInteger(5, rows.TypedInteger(3));
        add.BindInteger(6, rows.TypedInteger(4));
        add.BindInteger(7, 0);
        add.BindInteger(8, 0);
        add.BindText(9, encoded);
        add.Execute();
        add.Reset();
      }
      auto digest = Hex(hash);
      StoreMetadata(target, position_header, seq, digest);
      add = SqliteStatement{};
      target.Execute("COMMIT");
      auto store = writer.Commit();
      outputs.push_back(SealStore(root / "position_hints.json", store,
                                  "position-hints-store-v1", position_header,
                                  seq, digest));
    }
    auto metrics = Metrics(db, "build_rows", false),
         baseline = Metrics(db, "build_rows", true);
    O plan_metrics{{"signals", std::to_string(schedule.count)},
                   {"groups", std::to_string(groups)}};
    for (const auto &p : metrics)
      plan_metrics[p.first] = p.second;
    for (const auto &p : baseline)
      plan_metrics["baseline_logical_lane_" + p.first] = p.second;
    O config{{"refinement_iterations", "0"},
             {"paper_assignment_provider",
              JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
             {"electrical_map_sha256", JsonQuote(electrical.map_hash)},
             {"schedule_sha256", JsonQuote(schedule.logical)},
             {"lookahead_qualification",
              JsonQuote(certificate.empty() ? "bank-electrical-only"
                                            : "complete-artifact-chain")}};
    if (!certificate.empty()) {
      config["qualification_sha256"] =
          qualification_validation.at("qualification_sha256");
      config["qualification_scope"] =
          qualification_validation.at("qualification_scope");
    }
    O plan_header{
        {"schema", JsonQuote(pin_schema)},
        {"design", JsonQuote(schedule.design)},
        {"platform", JsonQuote(context.board.name)},
        {"provider", JsonQuote(provider)},
        {"configuration", D::Encode(config)},
        {"weights", D::Encode({{"crossing", "1.0"}, {"position", "1.0"}})},
        {"metrics", D::Encode(plan_metrics)}};
    {
      SqliteArtifactWriter writer((root / "plan.sqlite").string(),
                                  "pin-plan-store-v1", context.refs);
      auto &target = writer.database();
      target.Execute(pin_ddl);
      target.Execute("BEGIN");
      auto add = target.Prepare("INSERT INTO entries VALUES(?,?,?,?,?,?)");
      auto rows =
          db.Prepare("SELECT entry_id,group_index,physical_lane,logical_lane "
                     "FROM build_rows ORDER BY entry_id");
      Sha256 hash;
      I seq = 0;
      while (rows.Step()) {
        Stop(context.cancelled);
        auto encoded =
            D::Encode({{"schedule_entry", JsonQuote(rows.TypedText(0))},
                       {"group", std::to_string(rows.TypedInteger(1))},
                       {"physical_lane", std::to_string(rows.TypedInteger(2))},
                       {"logical_lane", std::to_string(rows.TypedInteger(3))}});
        Hash(hash, encoded + "\n");
        add.BindInteger(1, seq++);
        add.BindText(2, rows.TypedText(0));
        for (int i = 1; i <= 3; ++i)
          add.BindInteger(i + 2, rows.TypedInteger(i));
        add.BindText(6, encoded);
        add.Execute();
        add.Reset();
      }
      auto digest = Hex(hash);
      StoreMetadata(target, plan_header, seq, digest);
      add = SqliteStatement{};
      target.Execute("COMMIT");
      auto store = writer.Commit();
      outputs.push_back(SealStore(root / "pin_plan.json", store,
                                  "pin-plan-store-v1", plan_header, seq,
                                  digest));
    }
    O provenance = electrical.provenance;
    provenance["electrical_map_sha256"] = JsonQuote(electrical.map_hash);
    provenance["assignment_input_sha256"] = JsonQuote(proof.input_sha256);
    provenance["schedule_sha256"] = JsonQuote(schedule.logical);
    if (!certificate.empty()) {
      provenance["qualification_sha256"] =
          qualification_validation.at("qualification_sha256");
      provenance["qualification_scope"] =
          qualification_validation.at("qualification_scope");
    }
    std::vector<std::string> boxes;
    for (const auto &p : electrical.bounds)
      boxes.push_back(D::Encode({{"fpga", JsonQuote(p.first)},
                                 {"y_min", Number(p.second.first)},
                                 {"y_max", Number(p.second.second)}}));
    O binding_header{
        {"schema",
         JsonQuote(context.board.fixed
                       ? "emuflow.chimew-phase6-electrical-binding/v3"
                       : "emuflow.chimew-phase6-electrical-binding/v2")},
        {"status", JsonQuote("pass")},
        {"integration_status", JsonQuote("phase6-pin-plan")},
        {"design", JsonQuote(schedule.design)},
        {"platform", JsonQuote(context.board.name)},
        {"provider", JsonQuote(provider)},
        {"paper_provider",
         JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
        {"extension_scope", JsonQuote("EmuFlow concrete lane, bank voltage, "
                                      "IOSTANDARD, and package-pin legality")},
        {"fpga_y_bounds", Array(boxes)},
        {"provenance", D::Encode(provenance)},
        {"metrics", D::Encode({{"signals", std::to_string(schedule.count)},
                               {"groups", std::to_string(groups)},
                               {"channels", std::to_string(groups)},
                               {"package_pins", std::to_string(pin_count)},
                               {"lane_slot_collisions", "0"},
                               {"package_pin_collisions", "0"}})}};
    if (context.board.fixed)
      binding_header["fixed_wiring"] = context.board.fixed_source;
    if (!certificate.empty())
      binding_header["lookahead_qualification"] = D::Encode(certificate);
    {
      SqliteArtifactWriter writer((root / "binding.sqlite").string(),
                                  "chimew-binding-store-v1", context.refs);
      auto &target = writer.database();
      target.Execute(binding_ddl);
      target.Execute("BEGIN");
      auto add =
               target.Prepare("INSERT INTO groups VALUES(?,?,?,?,?,?,?,?,?,?)"),
           child = target.Prepare("INSERT INTO members VALUES(?,?,?)");
      auto rows = db.Prepare("SELECT group_index,group_id,record_json FROM "
                             "build_groups ORDER BY group_id");
      Sha256 hash, member_hash, canonical;
      O fields = binding_header;
      fields["entries"] = "[]";
      I seq = 0, total = 0;
      Hash(canonical, "{");
      bool comma = false;
      for (const auto &field : fields) {
        if (comma)
          Hash(canonical, ",");
        comma = true;
        Hash(canonical, JsonQuote(field.first) + ":");
        if (field.first != "entries") {
          Hash(canonical, field.second);
          continue;
        }
        Hash(canonical, "[");
        while (rows.Step()) {
          Stop(context.cancelled);
          auto record = Fields(rows.TypedText(2));
          std::vector<std::string> ids;
          auto members = db.Prepare("SELECT entry_id FROM build_members WHERE "
                                    "group_index=? ORDER BY entry_id");
          members.BindInteger(1, rows.TypedInteger(0));
          I index = 0;
          while (members.Step()) {
            auto id = members.TypedText(0);
            ids.push_back(JsonQuote(id));
            child.BindText(1, id);
            child.BindInteger(2, seq);
            child.BindInteger(3, index);
            child.Execute();
            child.Reset();
            Hash(member_hash,
                 D::Encode({{"group_seq", std::to_string(seq)},
                            {"member_seq", std::to_string(index++)},
                            {"member", JsonQuote(id)}}) +
                     "\n");
            ++total;
          }
          O full = record;
          full["schedule_entries"] = Array(ids);
          auto encoded = D::Encode(full);
          Hash(hash, encoded + "\n");
          if (seq)
            Hash(canonical, ",");
          Hash(canonical, encoded);
          add.BindInteger(1, seq++);
          add.BindText(2, Text(record, "group"));
          add.BindText(3, Text(record, "link"));
          add.BindText(4, Text(record, "fpga_a"));
          add.BindText(5, Text(record, "fpga_b"));
          add.BindInteger(6, Int(record, "physical_lane"));
          add.BindText(7, Text(record, "direction"));
          add.BindText(8, Text(record, "package_pin_a"));
          add.BindText(9, Text(record, "package_pin_b"));
          add.BindText(10, D::Encode(record));
          add.Execute();
          add.Reset();
        }
        Hash(canonical, "]");
      }
      Hash(canonical, "}");
      auto digest = Hex(hash), members = Hex(member_hash),
           complete = Hex(canonical);
      StoreMetadata(target, binding_header, seq, digest, total, members,
                    complete);
      add = SqliteStatement{};
      child = SqliteStatement{};
      target.Execute("COMMIT");
      auto store = writer.Commit();
      outputs.push_back(SealStore(root / "electrical_binding.json", store,
                                  "chimew-binding-store-v1", binding_header,
                                  seq, digest, total, members, complete));
    }
    Context checked = context;
    checked.roles.insert_or_assign(
        "positions", ArtifactRef::Bind((root / "position_hints.json").string(),
                                       "position-hints-selector-v1"));
    checked.roles.insert_or_assign(
        "plan", ArtifactRef::Bind((root / "pin_plan.json").string(),
                                  "pin-plan-selector-v1"));
    checked.roles.insert_or_assign(
        "binding",
        ArtifactRef::Bind((root / "electrical_binding.json").string(),
                          "chimew-binding-selector-v1"));
    Validation validator(checked, root);
    validator.Run();
    pin_result = validator.pin_result;
    binding_result = validator.binding_result;
    O report{{"schema", JsonQuote("emuflow.chimew-pin-artifacts/v1")},
             {"status", JsonQuote("pass")},
             {"provider", JsonQuote(provider)},
             {"design", JsonQuote(schedule.design)},
             {"platform", JsonQuote(context.board.name)},
             {"validation", D::Encode(pin_result)},
             {"electrical_validation", D::Encode(binding_result)},
             {"qualification_validation",
              qualification_validation.empty()
                  ? "null"
                  : D::Encode(qualification_validation)}};
    PublishTextArtifact(
        (root / "pin_artifacts.json").string(), {},
        [&](std::ostream &out) { out << D::Encode(report) << '\n'; });
  }
};
} // namespace

StageResult
BuildChimewPinArtifacts(const ArtifactRef &request,
                        const std::string &directory,
                        const volatile std::sig_atomic_t *cancelled) {
  std::optional<Workspace> work;
  std::unique_ptr<Context> context;
  std::unique_ptr<Builder> builder;
  const auto output = fs::absolute(directory);
  return RunStage(
      "phase6.build-pin-artifacts",
      [&] {
        Check(!fs::exists(output) && !fs::is_symlink(output),
              "output directory already exists");
        fs::create_directories(output.parent_path());
        work.emplace(output.parent_path());
        context = std::make_unique<Context>(cancelled);
        context->Read(request, "emuflow.chimew-pin-artifacts-input/v1");
        builder = std::make_unique<Builder>(*context, work->root);
        builder->Read();
        return context->refs;
      },
      [&](const auto &) {
        builder->Publish();
        Stop(cancelled);
        builder.reset();
        for (const auto &ref : context->refs)
          ref.RequireUnchanged();
        fs::remove(work->root / "build-index.sqlite");
        fs::remove(work->root / "validation.sqlite");
        fs::remove(work->root / "generated-bank-report.json");
        for (auto it = fs::directory_iterator(work->root);
             it != fs::directory_iterator(); ++it)
          if (it->path().filename().string().rfind("generated-bank-report.",
                                                   0) == 0)
            fs::remove(it->path());
        PublishNewDirectory(work->root.string(), output.string(),
                            context->refs);
      },
      [&] {
        std::vector<ArtifactRef> outputs;
        for (const auto &kind :
             std::vector<std::pair<std::string, std::string>>{
                 {"position_hints", "position-hints-store-v1"},
                 {"pin_plan", "pin-plan-store-v1"},
                 {"electrical_binding", "chimew-binding-store-v1"}}) {
          const auto selector =
              ArtifactRef::Bind((output / (kind.first + ".json")).string(),
                                KindSelector(kind.second));
          D manifest(D::Read(selector));
          outputs.push_back(ArtifactRef::Bind(
              (output / manifest.Text("artifact")).string(), kind.second));
          outputs.push_back(selector);
        }
        auto report =
            ArtifactRef::Bind((output / "bank_channel_report.json").string(),
                              "chimew-bank-report-selector-v1");
        D manifest(D::Read(report));
        outputs.push_back(
            ArtifactRef::Bind((output / manifest.Text("artifact")).string(),
                              "chimew-bank-report-store-v1"));
        outputs.push_back(report);
        outputs.push_back(
            ArtifactRef::Bind((output / "pin_artifacts.json").string(),
                              "chimew-pin-artifacts-summary-v1"));
        return outputs;
      });
}
StageResult
ValidateChimewPinArtifacts(const ArtifactRef &request,
                           const std::string &summary,
                           const volatile std::sig_atomic_t *cancelled) {
  std::optional<Workspace> work;
  std::unique_ptr<Context> context;
  std::unique_ptr<Validation> validation;
  return RunStage(
      "phase6.validate-pin-artifacts",
      [&] {
        auto parent = fs::absolute(summary).parent_path();
        fs::create_directories(parent);
        work.emplace(parent);
        context = std::make_unique<Context>(cancelled);
        context->Read(request, "emuflow.chimew-pin-validation-input/v1");
        validation = std::make_unique<Validation>(*context, work->root);
        validation->Run();
        return context->refs;
      },
      [&](const auto &refs) {
        PublishTextArtifact(summary, refs, [&](std::ostream &out) {
          O result{{"schema", JsonQuote("emuflow.chimew-pin-validation/v1")},
                   {"status", JsonQuote("pass")}};
          if (!validation->pin_result.empty())
            result["pin_validation"] = D::Encode(validation->pin_result);
          if (!validation->binding_result.empty())
            result["binding_validation"] =
                D::Encode(validation->binding_result);
          out << D::Encode(result) << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "chimew-pin-validation-summary-v1")};
      });
}
StageResult CopyPinArtifact(const ArtifactRef &selector,
                            const std::string &schema,
                            const std::string &target,
                            const volatile std::sig_atomic_t *cancelled) {
  StoredArtifact source;
  std::optional<Workspace> work;
  std::optional<fs::path> payload;
  auto output = fs::absolute(target);
  bool same = false;
  return RunStage(
      "phase6.copy-pin-artifact",
      [&] {
        source.Open(selector, schema, cancelled);
        Check(!fs::is_symlink(output), "output selector is a symlink");
        same = fs::weakly_canonical(output) ==
               fs::weakly_canonical(selector.path());
        if (!same) {
          fs::create_directories(output.parent_path());
          work.emplace(output.parent_path());
          for (const auto &ref : source.refs)
            Check(!fs::exists(output) || !fs::equivalent(output, ref.path()),
                  "pin artifact output aliases input");
        }
        return source.refs;
      },
      [&](const auto &refs) {
        if (same) {
          payload = source.store->path();
          return;
        }
        auto name = output.stem().string();
        auto dot = name.rfind('.');
        if (dot != std::string::npos && IsSha(name.substr(dot + 1)))
          name.resize(dot);
        name += '.' + Text(source.manifest, "store_sha256") + ".sqlite";
        auto destination = output.parent_path() / name;
        StagedArtifact payload_staged(destination.string());
        StagedArtifact selector_staged(output.string());
        std::vector<std::pair<fs::path, fs::path>> files;
        if (fs::exists(destination)) {
          auto existing = ArtifactRef::Bind(destination.string(), schema);
          Check(Sha256File(existing, cancelled) ==
                    Text(source.manifest, "store_sha256"),
                "pin artifact payload collision");
        } else {
          fs::copy_file(source.store->path(), payload_staged.path(),
                        fs::copy_options::overwrite_existing);
          files.emplace_back(payload_staged.path(), destination);
        }
        source.manifest["artifact"] = JsonQuote(name);
        PublishTextArtifact(selector_staged.path(), refs,
                            [&](std::ostream &out) {
                              out << D::Encode(source.manifest) << '\n';
                            });
        files.emplace_back(selector_staged.path(), output);
        PublishArtifactGroup(files, refs);
        payload = destination;
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(payload->string(), schema),
            ArtifactRef::Bind(output.string(), KindSelector(schema))};
      });
}
StageResult
AuditChimewElectricalMap(const ArtifactRef &request, const std::string &summary,
                         const volatile std::sig_atomic_t *cancelled) {
  std::unique_ptr<Context> context;
  Electrical electrical;
  return RunStage(
      "phase6.audit-electrical-map",
      [&] {
        context = std::make_unique<Context>(cancelled);
        context->Read(request, "emuflow.chimew-electrical-validation-input/v1");
        auto input =
            context->Role("bank_input", "chimew-bank-input-selector-v1");
        auto proof = InspectChimewBankInput(input, cancelled);
        context->Add(proof.inputs);
        auto manifest = Fields(D::Read(input));
        electrical.Read(
            context->Role("electrical", "physical-input-document-v1"),
            SourceOf(proof.inputs, "chimew-bank-input-store-v1"),
            Fields(Get(manifest, "header")), context->board, cancelled);
        return context->refs;
      },
      [&](const auto &refs) {
        PublishTextArtifact(summary, refs, [&](std::ostream &out) {
          out << D::Encode(
                     {{"schema",
                       JsonQuote("emuflow.chimew-electrical-validation/v1")},
                      {"status", JsonQuote("pass")},
                      {"provenance", D::Encode(electrical.provenance)},
                      {"map_sha256", JsonQuote(electrical.map_hash)},
                      {"metrics", Get(electrical.header, "metrics")}})
              << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "chimew-pin-validation-summary-v1")};
      });
}
} // namespace emuflow::core
