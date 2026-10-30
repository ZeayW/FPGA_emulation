#include "transport/chimew/bank_store.hpp"
#include "common/bounded_json.hpp"
#include "common/canonical_json.hpp"
#include "common/exact_signed_sum.hpp"
#include "common/hash.hpp"
#include "storage/store.hpp"
#include "transport/chimew/qualification.hpp"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <filesystem>
#include <limits>
#include <map>
#include <memory>
#include <numeric>
#include <optional>
#include <set>
#include <sys/file.h>
#include <unistd.h>

namespace emuflow::core {
namespace {
using D = BoundedJson;
using O = std::map<std::string, std::string>;
using I = std::int64_t;
using namespace emuflow::native;
namespace fs = std::filesystem;
void Check(bool valid, const std::string &why) {
  if (!valid)
    throw StageError(ErrorCode::kInvalidArtifact,
                     "Chimew bank assignment: " + why);
}
void Stop(const volatile std::sig_atomic_t *flag) {
  if (flag && *flag)
    throw StageError(ErrorCode::kExecutionFailed,
                     "Chimew bank assignment cancelled");
}
struct Hash {
  Sha256 value;
  void Add(const std::string &text) {
    value.update(reinterpret_cast<const unsigned char *>(text.data()),
                 text.size());
  }
  std::string Finish() {
    std::string out;
    for (auto c : value.finish()) {
      out += "0123456789abcdef"[c >> 4];
      out += "0123456789abcdef"[c & 15];
    }
    return out;
  }
};
std::string Digest(const std::string &text) {
  Hash h;
  h.Add(text);
  return h.Finish();
}
bool IsDigest(const std::string &text) {
  return text.size() == 64 &&
         text.find_first_not_of("0123456789abcdef") == std::string::npos;
}
std::string Meta(SqliteDatabase &db, const std::string &key) {
  auto row = db.Prepare("SELECT CASE WHEN length(value)<=8388608 THEN value "
                        "END FROM metadata WHERE key=?");
  row.BindText(1, key);
  Check(row.Step() && !row.IsNull(0),
        "metadata missing or exceeds bound: " + key);
  const auto text = row.TypedText(0);
  Check(!row.Step(), "duplicate metadata");
  return text;
}
struct Record {
  O fields;
  explicit Record(const std::string &text) {
    VisitJsonObjectTokens(text, [&](const auto &key, const auto &token) {
      Check(fields.emplace(key, token).second, "duplicate record field");
    });
  }
  std::string Token(const std::string &key) const {
    const auto it = fields.find(key);
    Check(it != fields.end(), "missing record field " + key);
    return it->second;
  }
  bool Has(const std::string &key) const { return fields.count(key); }
  std::string Text(const std::string &key) const {
    auto text = DecodeJsonString(Token(key));
    Check(!text.empty(), "empty record identity " + key);
    return text;
  }
  I Integer(const std::string &key) const {
    return D::IntegerToken(Token(key));
  }
  double Number(const std::string &key) const {
    return ParseFiniteDecimal(Token(key), "Chimew numeric record");
  }
};
ChimewPoint Point(const std::string &token) {
  Record p(token);
  return {p.Number("x"), p.Number("y")};
}
I Count(I n) {
  Check(n >= 0 && n <= INT32_MAX, "record count exceeds kernel identity range");
  return n;
}
struct Lease {
  int fd = -1;
  Lease(const fs::path &selector, const char *category, bool exclusive) {
    const auto dir = fs::temp_directory_path() / category;
    fs::create_directories(dir);
    const auto path =
        dir / (Digest(fs::weakly_canonical(selector).string()) + ".lock");
    fd = open(path.c_str(), O_RDWR | O_CREAT | O_NOFOLLOW, 0666);
    Check(fd >= 0, "cannot open publication lease");
    if (flock(fd, exclusive ? LOCK_EX : LOCK_SH) != 0) {
      close(fd);
      fd = -1;
      Check(false, "cannot acquire publication lease");
    }
  }
  ~Lease() {
    if (fd >= 0) {
      flock(fd, LOCK_UN);
      close(fd);
    }
  }
};
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path &parent) {
    auto name = (parent / ".chimew-bank-XXXXXX").string();
    Check(mkdtemp(name.data()), "cannot create workspace");
    root = name;
  }
  ~Workspace() {
    std::error_code ignored;
    fs::remove_all(root, ignored);
  }
};
constexpr const char *input_ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE groups(seq INTEGER PRIMARY KEY,group_id TEXT NOT NULL UNIQUE,record_json TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE banks(seq INTEGER PRIMARY KEY,bank_id TEXT NOT NULL UNIQUE,first_channel INTEGER NOT NULL,record_json TEXT NOT NULL,CHECK(first_channel>=0)) WITHOUT ROWID;
)SQL";
constexpr const char *report_ddl = R"SQL(
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE assignments(seq INTEGER PRIMARY KEY,group_id TEXT NOT NULL UNIQUE,bank_pair TEXT NOT NULL,channel TEXT NOT NULL UNIQUE,bank_cost_rank INTEGER NOT NULL CHECK(bank_cost_rank>=0),channel_cost_rank INTEGER NOT NULL CHECK(channel_cost_rank>=0),bank_cost REAL NOT NULL CHECK(bank_cost>=0.0),channel_cost REAL NOT NULL CHECK(channel_cost>=0.0)) WITHOUT ROWID;
)SQL";
double RawCost(const ChimewBankGroup &group, const ChimewPoint &a,
               const ChimewPoint &b) {
  double total = 0;
  for (const auto &m : group.members) {
    Check(!m.fanins.empty(), "member has no fanins");
    const auto &output = m.direction == 0 ? a : b;
    const auto &input = m.direction == 0 ? b : a;
    double fanins = 0;
    for (const auto &p : m.fanins)
      fanins += std::abs(p.x - input.x) + std::abs(p.y - input.y);
    const double value = std::abs(m.fanout.x - output.x) +
                         std::abs(m.fanout.y - output.y) +
                         fanins / double(m.fanins.size());
    volatile double weighted = m.timing_weight * value;
    total += weighted;
  }
  Check(std::isfinite(total) && total >= 0, "physical cost is invalid");
  return total;
}
I Cost(const ChimewBankGroup &group, const ChimewPoint &a, const ChimewPoint &b,
       double scale) {
  const auto rounded = std::floor(RawCost(group, a, b) * scale + 0.5);
  Check(std::isfinite(rounded) && rounded >= 0 && rounded < std::ldexp(1.0, 63),
        "edge cost is out of range");
  return I(rounded);
}
void Sum(I &total, I value) {
  Check(value >= 0 && total <= std::numeric_limits<I>::max() - value,
        "cost total overflows its contract");
  total += value;
}
void Reduced(I cost, I from, I to, bool reverse) {
  ExactSignedSum sum;
  sum.add(cost, reverse);
  sum.add(from);
  sum.add(to, true);
  Check(!sum.negative(),
        "residual-dual certificate has a negative reduced cost");
}
struct BankInput {
  ChimewBankInput model;
  std::vector<std::string> bank_ids, channel_ids, group_ids;
  O header, provenance, metrics;
  std::string logical, design, platform, domains;
  std::vector<ArtifactRef> sources;
  std::optional<ArtifactRef> store;
  const volatile std::sig_atomic_t *cancelled;
  explicit BankInput(const volatile std::sig_atomic_t *flag)
      : cancelled(flag) {}
  void Read(const ArtifactRef &selector) {
    sources = {selector};
    Check(selector.schema() == "chimew-bank-input-selector-v1",
          "only current typed bank/channel input is supported");
    D manifest(D::Read(selector));
    manifest.Keys({"schema", "logical_schema", "artifact", "application_id",
                   "user_version", "store_bytes", "store_sha256", "header",
                   "problem_header", "bank_pairs", "groups", "logical_sha256"});
    Check(manifest.Text("schema") == "emuflow.chimew-bank-channel-storage/v1" &&
              manifest.Integer("application_id") == 0x454d5543 &&
              manifest.Integer("user_version") == 1,
          "selector version disagrees");
    const auto hash = manifest.Text("store_sha256");
    Check(IsDigest(hash), "invalid store content seal");
    const fs::path member = manifest.Text("artifact"),
                   payload = fs::path(selector.path()).parent_path() / member;
    const auto stem = fs::path(selector.path()).stem().string();
    Check(member == stem + "." + hash + ".sqlite" && !fs::is_symlink(payload),
          "input store escapes selector ownership");
    store = ArtifactRef::Bind(payload.string(), "chimew-bank-input-store-v1");
    sources.push_back(*store);
    Check(manifest.Integer("store_bytes") >= 0 &&
              std::uint64_t(manifest.Integer("store_bytes")) ==
                  store->generation().bytes &&
              Sha256File(*store, cancelled) == hash,
          "input content seal disagrees");
    SqliteStore owner(*store);
    auto &db = owner.database();
    RequireSqliteDDL(db, input_ddl);
    SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
    header = D(CanonicalJsonToken(manifest.Token("header"))).Fields();
    D h(manifest.Token("header"));
    const auto schema = h.Text("schema");
    Check((schema == "emuflow.chimew-bank-channel-input/v1" ||
           schema == "emuflow.chimew-bank-channel-input/v2") &&
              manifest.Text("logical_schema") == schema &&
              h.Text("provider") ==
                  "source-qualified-physical-bank-channel-v1" &&
              h.Text("coordinate_system") == "physical-site-xy",
          "logical input contract disagrees");
    design = h.Text("design");
    platform = h.Text("platform");
    Check(!design.empty() && !platform.empty(), "empty input namespace");
    const auto scale = h.Integer("cost_quantization_per_site");
    Check(scale > 0, "invalid cost quantization");
    model.cost_scale = double(scale);
    logical = manifest.Text("logical_sha256");
    Check(IsDigest(logical), "invalid logical input seal");
    D p(h.Token("provenance"));
    for (const auto *key : {"producer", "producer_version"}) {
      const auto v = p.Text(key);
      Check(!v.empty(), "empty provenance field");
      provenance[key] = JsonQuote(v);
    }
    for (const auto *key :
         {"grouping_sha256", "placement_sha256", "architecture_sha256"}) {
      auto value = p.Text(key);
      std::transform(
          value.begin(), value.end(), value.begin(), [](unsigned char c) {
            return c >= 'A' && c <= 'F' ? char(c - 'A' + 'a') : char(c);
          });
      Check(IsDigest(value), "invalid input provenance digest");
      provenance[key] = JsonQuote(value);
    }
    std::map<std::string, int> domain_ids;
    std::vector<std::string> normalized_domains;
    for (const auto &token : D::Array(h.Token("domains"), 16384)) {
      D domain(token);
      const auto id = domain.Text("id"), a = domain.Text("fpga_a"),
                 b = domain.Text("fpga_b");
      Check(!id.empty() && !a.empty() && !b.empty() && a != b &&
                domain_ids.emplace(id, int(domain_ids.size())).second,
            "invalid/duplicate assignment domain");
      normalized_domains.push_back(D::Encode({{"id", JsonQuote(id)},
                                              {"fpga_a", JsonQuote(a)},
                                              {"fpga_b", JsonQuote(b)}}));
    }
    Check(!domain_ids.empty(), "empty assignment domains");
    domains = "[";
    for (const auto &d : normalized_domains) {
      if (domains.size() > 1)
        domains += ',';
      domains += d;
    }
    domains += ']';
    Hash bank_hash, group_hash;
    std::set<std::string> bank_names, channel_names, group_names;
    I signals = 0, fanins = 0, bidirectional = 0;
    std::vector<I> demand(domain_ids.size()), capacity(domain_ids.size());
    auto banks = db.Prepare(
        "SELECT seq,bank_id,first_channel,record_json FROM banks ORDER BY seq");
    while (banks.Step()) {
      Stop(cancelled);
      Check(banks.TypedInteger(0) == I(model.banks.size()) &&
                banks.TypedInteger(2) == I(model.channels.size()),
            "bank sequence/channel offsets disagree");
      const auto raw = banks.TypedText(3);
      bank_hash.Add(raw + "\n");
      Record row(raw);
      const auto id = row.Text("id"), domain = row.Text("domain");
      Check(id == banks.TypedText(1) && bank_names.insert(id).second &&
                domain_ids.count(domain),
            "bank identity/domain disagrees");
      Record a(row.Token("bank_a")), b(row.Token("bank_b"));
      Check(!a.Text("id").empty() && !b.Text("id").empty(),
            "empty bank endpoint identity");
      ChimewBankPair bank;
      bank.index = int(Count(model.banks.size()));
      bank.domain = domain_ids.at(domain);
      bank.bank_a = Point(row.Token("bank_a"));
      bank.bank_b = Point(row.Token("bank_b"));
      VisitJsonArrayTokens(row.Token("channels"), [&](const auto &token) {
        Record channel(token);
        const auto name = channel.Text("id");
        const auto order = channel.Integer("order");
        Check(order == I(bank.channels.size()) &&
                  channel_names.insert(name).second,
              "channel identity/order is not canonical");
        ChimewBankChannel c;
        c.index = int(Count(model.channels.size()));
        c.bank = bank.index;
        c.order = int(order);
        c.pin_a = Point(channel.Token("pin_a"));
        c.pin_b = Point(channel.Token("pin_b"));
        bank.channels.push_back(c.index);
        model.channels.push_back(c);
        channel_ids.push_back(name);
      });
      Check(!bank.channels.empty(), "bank has no channels");
      capacity[bank.domain] += bank.channels.size();
      model.banks.push_back(std::move(bank));
      bank_ids.push_back(id);
    }
    auto groups =
        db.Prepare("SELECT seq,group_id,record_json FROM groups ORDER BY seq");
    while (groups.Step()) {
      Stop(cancelled);
      Check(groups.TypedInteger(0) == I(model.groups.size()),
            "group order is incomplete");
      const auto raw = groups.TypedText(2);
      group_hash.Add(raw + "\n");
      Record row(raw);
      const auto id = row.Text("id"), domain = row.Text("domain"),
                 kind = row.Text("kind"), direction = row.Text("direction");
      Check(id == groups.TypedText(1) && group_names.insert(id).second &&
                domain_ids.count(domain) &&
                (kind == "tdm_group" || kind == "common_signal") &&
                (direction == "a_to_b" || direction == "b_to_a" ||
                 (schema == "emuflow.chimew-bank-channel-input/v2" &&
                  direction == "bidirectional")),
            "group identity/kind/direction is invalid");
      ChimewBankGroup group;
      group.index = int(Count(model.groups.size()));
      group.domain = domain_ids.at(domain);
      group.kind = kind == "tdm_group" ? 0 : 1;
      group.direction = direction == "a_to_b"   ? 0
                        : direction == "b_to_a" ? 1
                                                : 2;
      Check(group.kind == 0 || group.direction != 2,
            "common signal cannot be bidirectional");
      std::set<std::string> member_names;
      bool seen[2] = {false, false};
      VisitJsonArrayTokens(row.Token("members"), [&](const auto &token) {
        Record member(token);
        Check(member_names.insert(member.Text("id")).second,
              "duplicate member identity");
        const auto dir =
            member.Has("direction") ? member.Text("direction") : direction;
        Check((dir == "a_to_b" || dir == "b_to_a") &&
                  (group.direction == 2 || dir == direction),
              "member direction disagrees");
        ChimewBankMember m;
        m.direction = dir == "a_to_b" ? 0 : 1;
        seen[m.direction] = true;
        m.timing_weight =
            member.Has("timing_weight") ? member.Number("timing_weight") : 1.0;
        Check(m.timing_weight > 0, "nonpositive timing weight");
        m.fanout = Point(member.Token("fanout"));
        VisitJsonArrayTokens(member.Token("fanins"), [&](const auto &point) {
          m.fanins.push_back(Point(point));
          ++fanins;
          Count(fanins);
        });
        Check(!m.fanins.empty(), "member has no fanins");
        group.members.push_back(std::move(m));
        ++signals;
        Count(signals);
      });
      Check(!group.members.empty() &&
                (group.kind == 0 || group.members.size() == 1) &&
                (group.direction != 2 || (seen[0] && seen[1])),
            "group membership is invalid");
      group.expected_members = int(Count(group.members.size()));
      ++demand[group.domain];
      bidirectional += group.direction == 2;
      model.groups.push_back(std::move(group));
      group_ids.push_back(id);
    }
    Check(!model.groups.empty() && !model.banks.empty(),
          "empty bank assignment input");
    for (std::size_t i = 0; i < demand.size(); ++i)
      Check(demand[i] <= capacity[i],
            "assignment domain has insufficient channel capacity");
    metrics = {{"groups", std::to_string(model.groups.size())},
               {"signals", std::to_string(signals)},
               {"fanins", std::to_string(fanins)},
               {"bank_pairs", std::to_string(model.banks.size())},
               {"channels", std::to_string(model.channels.size())},
               {"bidirectional_bundles", std::to_string(bidirectional)}};
    D expected_metrics(h.Token("metrics"));
    for (const auto &m : metrics) {
      if (m.first == "bidirectional_bundles" && !expected_metrics.Has(m.first))
        continue;
      Check(expected_metrics.Integer(m.first) == D::IntegerToken(m.second),
            "input record metrics disagree");
    }
    const auto normalized = D::Encode({{"design", JsonQuote(design)},
                                       {"platform", JsonQuote(platform)},
                                       {"cost_scale", std::to_string(scale)},
                                       {"provenance", D::Encode(provenance)},
                                       {"domains", domains},
                                       {"metrics", D::Encode(metrics)}});
    RequireEquivalentJson(
        compare, normalized, manifest.Token("problem_header"),
        "Chimew stored normalized header is not derived from logical facts");
    const auto banks_digest = bank_hash.Finish(),
               groups_digest = group_hash.Finish();
    D bank_seal(manifest.Token("bank_pairs")),
        group_seal(manifest.Token("groups"));
    Check(bank_seal.Integer("records") == I(model.banks.size()) &&
              group_seal.Integer("records") == I(model.groups.size()) &&
              bank_seal.Text("sha256") == banks_digest &&
              group_seal.Text("sha256") == groups_digest,
          "input relation seals disagree");
    O all = header;
    all["bank_pairs"] = "";
    all["groups"] = "";
    Hash logical_hash;
    logical_hash.Add("{");
    bool first = true;
    for (const auto &field : all) {
      if (!first)
        logical_hash.Add(",");
      first = false;
      logical_hash.Add(JsonQuote(field.first) + ":");
      if (field.first == "bank_pairs" || field.first == "groups") {
        logical_hash.Add("[");
        auto records = db.Prepare(
            std::string("SELECT record_json FROM ") +
            (field.first == "groups" ? "groups" : "banks") + " ORDER BY seq");
        bool first_row = true;
        while (records.Step()) {
          if (!first_row)
            logical_hash.Add(",");
          first_row = false;
          logical_hash.Add(records.TypedText(0));
        }
        logical_hash.Add("]");
      } else
        logical_hash.Add(field.second);
    }
    logical_hash.Add("}");
    Check(logical_hash.Finish() == logical,
          "logical bank input seal disagrees");
    const O metadata{{"header", manifest.Token("header")},
                     {"problem_header", manifest.Token("problem_header")},
                     {"banks_sha256", JsonQuote(banks_digest)},
                     {"groups_sha256", JsonQuote(groups_digest)},
                     {"logical_sha256", JsonQuote(logical)}};
    auto count = db.Prepare("SELECT count(*) FROM metadata");
    Check(count.Step() && count.TypedInteger(0) == I(metadata.size()),
          "input metadata coverage disagrees");
    for (const auto &m : metadata)
      RequireEquivalentJson(compare, m.second, Meta(db, m.first),
                            "Chimew bank input metadata seal differs");
  }
};
struct Verified {
  std::vector<int> channels;
  std::vector<I> channel_costs;
  I stage2_total = 0, active_banks = 0;
};
Verified Verify(const ChimewBankInput &input, const ChimewBankResult &result,
                const volatile std::sig_atomic_t *cancelled) {
  const auto &primal = result.stage1;
  const auto groups = input.groups.size(), banks = input.banks.size();
  Check(std::isfinite(input.cost_scale) && input.cost_scale > 0 && groups &&
            banks && input.channels.size() <= std::size_t(INT32_MAX),
        "invalid certificate input dimensions/scale");
  const auto finite = [](const ChimewPoint &p) {
    return std::isfinite(p.x) && std::isfinite(p.y);
  };
  std::vector<bool> owned(input.channels.size());
  for (std::size_t b = 0; b < banks; ++b) {
    const auto &bank = input.banks[b];
    Check(bank.index == int(b) && bank.domain >= 0 && !bank.channels.empty() &&
              finite(bank.bank_a) && finite(bank.bank_b),
          "invalid certificate bank");
    for (std::size_t order = 0; order < bank.channels.size(); ++order) {
      const int id = bank.channels[order];
      Check(id >= 0 && std::size_t(id) < owned.size() && !owned[id],
            "invalid certificate channel ownership");
      const auto &channel = input.channels[id];
      Check(channel.index == id && channel.bank == int(b) &&
                channel.order == int(order) && finite(channel.pin_a) &&
                finite(channel.pin_b),
            "invalid certificate channel");
      owned[id] = true;
    }
  }
  Check(std::find(owned.begin(), owned.end(), false) == owned.end(),
        "incomplete certificate channel ownership");
  for (std::size_t i = 0; i < groups; ++i) {
    const auto &g = input.groups[i];
    Check(g.index == int(i) && g.domain >= 0 && (g.kind == 0 || g.kind == 1) &&
              g.direction >= 0 && g.direction <= 2 && !g.members.empty() &&
              g.expected_members >= 0 &&
              std::size_t(g.expected_members) == g.members.size() &&
              (g.kind == 0 || (g.members.size() == 1 && g.direction != 2)),
          "invalid certificate group");
    bool directions[2] = {false, false};
    for (const auto &m : g.members) {
      Check(m.direction >= 0 && m.direction <= 1 &&
                (g.direction == 2 || m.direction == g.direction) &&
                std::isfinite(m.timing_weight) && m.timing_weight > 0 &&
                finite(m.fanout) && !m.fanins.empty(),
            "invalid certificate member");
      directions[m.direction] = true;
      for (const auto &p : m.fanins)
        Check(finite(p), "invalid certificate fanin");
    }
    Check(g.direction != 2 || (directions[0] && directions[1]),
          "incomplete certificate bidirectional group");
  }
  Check(groups <= std::size_t(INT32_MAX) && banks <= std::size_t(INT32_MAX) &&
            primal.right_for_left.size() == groups &&
            primal.cost_for_left.size() == groups &&
            primal.potentials.size() == 2 + groups + banks &&
            result.alternatives.size() == banks &&
            result.chosen_priority.size() == banks,
        "certificate dimensions are invalid");
  const std::size_t first_right = 1, first_left = 1 + banks,
                    sink = first_left + groups;
  std::vector<I> used(banks);
  std::vector<std::vector<int>> members(banks);
  I total = 0;
  Verified verified;
  verified.channels.assign(groups, -1);
  verified.channel_costs.resize(groups);
  for (std::size_t i = 0; i < groups; ++i) {
    Stop(cancelled);
    const int selected = primal.right_for_left[i];
    Check(selected >= 0 && std::size_t(selected) < banks &&
              input.groups[i].domain == input.banks[selected].domain,
          "selected bank is ineligible");
    ++used[selected];
    members[selected].push_back(int(i));
    Sum(total, primal.cost_for_left[i]);
    for (std::size_t j = 0; j < banks; ++j)
      if (input.banks[j].domain == input.groups[i].domain) {
        const auto cost = Cost(input.groups[i], input.banks[j].bank_a,
                               input.banks[j].bank_b, input.cost_scale);
        Check(int(j) != selected || cost == primal.cost_for_left[i],
              "selected bank edge has wrong cost");
        if (int(j) == selected)
          Reduced(cost, primal.potentials[first_left + i],
                  primal.potentials[first_right + j], true);
        else
          Reduced(cost, primal.potentials[first_right + j],
                  primal.potentials[first_left + i], false);
      }
    Check(primal.potentials[sink] >= primal.potentials[first_left + i],
          "negative bank sink reduced cost");
  }
  Check(total == primal.total_cost, "bank primal total is invalid");
  for (std::size_t b = 0; b < banks; ++b) {
    Check(used[b] <= I(input.banks[b].channels.size()),
          "bank capacity exceeded");
    if (used[b])
      Check(primal.potentials[1 + b] >= primal.potentials[0],
            "negative used bank source reduced cost");
    if (used[b] < I(input.banks[b].channels.size()))
      Check(primal.potentials[0] >= primal.potentials[1 + b],
            "negative residual bank source reduced cost");
    if (members[b].empty()) {
      Check(result.alternatives[b].first.groups.empty() &&
                result.alternatives[b].second.groups.empty(),
            "unused bank has assignment records");
      continue;
    }
    ++verified.active_banks;
    I alternatives[2] = {0, 0};
    for (int priority = 0; priority < 2; ++priority) {
      const auto &alternative = priority ? result.alternatives[b].second
                                         : result.alternatives[b].first;
      const auto &assignment = alternative.assignment;
      const auto &bank = input.banks[b];
      const auto count = bank.channels.size();
      const auto &ids = members[b];
      Check(alternative.priority == priority && alternative.groups == ids &&
                assignment.right_for_left.size() == ids.size() &&
                assignment.cost_for_left.size() == ids.size() &&
                assignment.potentials.size() == 2 + count + ids.size(),
            "package-pin certificate dimensions/coverage disagree");
      std::array<int, 3> directions{};
      int common = 0;
      for (int id : ids) {
        if (input.groups[id].kind == 0)
          ++directions[input.groups[id].direction];
        else
          ++common;
      }
      int dedicated = -1;
      if (!common)
        for (int direction = 0; direction < 3; ++direction)
          if (directions[direction] &&
              std::count_if(directions.begin(), directions.end(),
                            [](int n) { return n > 0; }) == 1)
            dedicated = direction;
      const auto eligible = [&](const ChimewBankGroup &g, std::size_t r) {
        if (dedicated >= 0)
          return g.kind == 0 && g.direction == dedicated;
        int required = -1;
        if (r < std::size_t(directions[priority]))
          required = priority;
        else if (r <
                 std::size_t(directions[priority] + directions[1 - priority]))
          required = 1 - priority;
        else if (r < std::size_t(directions[0] + directions[1] + directions[2]))
          required = 2;
        return required >= 0 ? g.kind == 0 && g.direction == required
                             : g.kind == 1;
      };
      std::vector<bool> occupied(count);
      I sum = 0;
      const auto left_start = 1 + count, last = left_start + ids.size();
      for (std::size_t left = 0; left < ids.size(); ++left) {
        Stop(cancelled);
        const auto right = assignment.right_for_left[left];
        Check(right >= 0 && std::size_t(right) < count && !occupied[right] &&
                  eligible(input.groups[ids[left]], right),
              "selected package-pin channel is ineligible or duplicated");
        occupied[right] = true;
        Sum(sum, assignment.cost_for_left[left]);
        for (std::size_t candidate = 0; candidate < count; ++candidate)
          if (eligible(input.groups[ids[left]], candidate)) {
            const auto &channel = input.channels.at(bank.channels[candidate]);
            const auto cost = Cost(input.groups[ids[left]], channel.pin_a,
                                   channel.pin_b, input.cost_scale);
            Check(int(candidate) != right ||
                      cost == assignment.cost_for_left[left],
                  "selected channel edge has wrong cost");
            if (int(candidate) == right)
              Reduced(cost, assignment.potentials[left_start + left],
                      assignment.potentials[1 + candidate], true);
            else
              Reduced(cost, assignment.potentials[1 + candidate],
                      assignment.potentials[left_start + left], false);
          }
        Check(assignment.potentials[last] >=
                  assignment.potentials[left_start + left],
              "negative package-pin sink reduced cost");
      }
      Check(sum == assignment.total_cost, "package-pin primal total disagrees");
      for (std::size_t r = 0; r < count; ++r) {
        if (occupied[r])
          Check(assignment.potentials[1 + r] >= assignment.potentials[0],
                "negative used channel source reduced cost");
        else
          Check(assignment.potentials[0] >= assignment.potentials[1 + r],
                "negative unused channel source reduced cost");
      }
      alternatives[priority] = sum;
    }
    const int chosen = alternatives[1] < alternatives[0] ? 1 : 0;
    Check(result.chosen_priority[b] == chosen,
          "package-pin priority is not the minimum-cost alternative");
    Sum(verified.stage2_total, alternatives[chosen]);
    const auto &winner =
        chosen ? result.alternatives[b].second : result.alternatives[b].first;
    for (std::size_t i = 0; i < members[b].size(); ++i) {
      const auto group = members[b][i];
      verified.channels[group] =
          input.banks[b].channels[winner.assignment.right_for_left[i]];
      verified.channel_costs[group] = winner.assignment.cost_for_left[i];
    }
  }
  Check(verified.stage2_total == result.channel_cost &&
            std::find(verified.channels.begin(), verified.channels.end(), -1) ==
                verified.channels.end(),
        "final channel assignment cost/coverage disagrees");
  return verified;
}
std::string AssignmentRow(const BankInput &input,
                          const ChimewBankResult &result,
                          const Verified &checked, std::size_t i) {
  const auto bank = result.stage1.right_for_left[i],
             channel = checked.channels[i];
  const auto &b = input.model.banks[bank];
  const auto &c = input.model.channels[channel];
  return D::Encode(
      {{"group", JsonQuote(input.group_ids[i])},
       {"bank_pair", JsonQuote(input.bank_ids[bank])},
       {"channel", JsonQuote(input.channel_ids[channel])},
       {"bank_cost_rank", std::to_string(result.stage1.cost_for_left[i])},
       {"channel_cost_rank", std::to_string(checked.channel_costs[i])},
       {"bank_cost",
        JsonRecord(RawCost(input.model.groups[i], b.bank_a, b.bank_b))
            .Encode()},
       {"channel_cost",
        JsonRecord(RawCost(input.model.groups[i], c.pin_a, c.pin_b))
            .Encode()}});
}
void AuditStoredReport(
    const ArtifactRef &artifact, const O &expected_header,
    const std::string &expected_digest, std::size_t expected_count,
    const volatile std::sig_atomic_t *cancelled,
    const std::function<void(std::size_t, SqliteStatement &)> &check_row = {}) {
  SqliteStore store(artifact);
  auto &db = store.database();
  RequireSqliteDDL(db, report_ddl);
  const auto header = Meta(db, "header");
  Check(CanonicalJsonToken(header) == D::Encode(expected_header),
        "stored report header disagrees");
  O fields = expected_header;
  fields["assignments"] = "";
  Hash logical, relation;
  logical.Add("{");
  bool first_field = true;
  double bank_physical = 0, channel_physical = 0;
  I bank_total = 0, channel_total = 0;
  for (const auto &field : fields) {
    if (!first_field)
      logical.Add(",");
    first_field = false;
    logical.Add(JsonQuote(field.first) + ":");
    if (field.first != "assignments") {
      logical.Add(field.second);
      continue;
    }
    logical.Add("[");
    auto rows =
        db.Prepare("SELECT seq,group_id,bank_pair,channel,bank_cost_rank,"
                   "channel_cost_rank,bank_cost,channel_cost FROM assignments "
                   "ORDER BY seq");
    std::size_t count = 0;
    while (rows.Step()) {
      Stop(cancelled);
      Check(count < expected_count && rows.TypedInteger(0) == I(count),
            "stored report sequence/coverage disagrees");
      const auto group = rows.TypedText(1), bank = rows.TypedText(2),
                 channel = rows.TypedText(3);
      const auto bank_rank = rows.TypedInteger(4),
                 channel_rank = rows.TypedInteger(5);
      const auto bank_cost = rows.TypedReal(6),
                 channel_cost = rows.TypedReal(7);
      Check(!group.empty() && !bank.empty() && !channel.empty() &&
                bank_rank >= 0 && channel_rank >= 0 &&
                std::isfinite(bank_cost) && bank_cost >= 0 &&
                std::isfinite(channel_cost) && channel_cost >= 0,
            "stored report assignment has invalid identity/cost");
      if (check_row)
        check_row(count, rows);
      Sum(bank_total, bank_rank);
      Sum(channel_total, channel_rank);
      bank_physical += bank_cost;
      channel_physical += channel_cost;
      const auto record =
          D::Encode({{"group", JsonQuote(group)},
                     {"bank_pair", JsonQuote(bank)},
                     {"channel", JsonQuote(channel)},
                     {"bank_cost_rank", std::to_string(bank_rank)},
                     {"channel_cost_rank", std::to_string(channel_rank)},
                     {"bank_cost", JsonRecord(bank_cost).Encode()},
                     {"channel_cost", JsonRecord(channel_cost).Encode()}});
      if (count++)
        logical.Add(",");
      logical.Add(record);
      relation.Add(record + "\n");
    }
    Check(count == expected_count, "stored report omits assignments");
    logical.Add("]");
  }
  logical.Add("}");
  D metrics(expected_header.at("metrics"));
  const auto physical_agrees = [](double a, double b) {
    return std::isfinite(a) && std::isfinite(b) &&
           std::abs(a - b) <=
               std::max(1e-9, 1e-10 * std::max(std::abs(a), std::abs(b)));
  };
  Check(metrics.Integer("groups") == I(expected_count) && expected_count > 0 &&
            bank_total == metrics.Integer("stage1_cost_rank") &&
            channel_total == metrics.Integer("stage2_cost_rank") &&
            physical_agrees(bank_physical,
                            metrics.Number("stage1_physical_site_cost")) &&
            physical_agrees(channel_physical,
                            metrics.Number("stage2_physical_site_cost")),
        "stored report aggregate costs disagree");
  Check(DecodeJsonString(Meta(db, "assignments_sha256")) == relation.Finish() &&
            DecodeJsonString(Meta(db, "logical_sha256")) == expected_digest &&
            logical.Finish() == expected_digest,
        "stored report record seals disagree");
  auto count = db.Prepare("SELECT count(*) FROM metadata");
  Check(count.Step() && count.TypedInteger(0) == 3,
        "stored report metadata coverage disagrees");
}
void AuditReport(const ArtifactRef &artifact, const BankInput &input,
                 const ChimewBankResult &result, const Verified &checked,
                 const O &header, const std::string &digest,
                 const volatile std::sig_atomic_t *cancelled) {
  AuditStoredReport(
      artifact, header, digest, input.model.groups.size(), cancelled,
      [&](std::size_t row, SqliteStatement &values) {
        const auto bank = result.stage1.right_for_left[row],
                   channel = checked.channels[row];
        const auto &b = input.model.banks[bank];
        const auto &c = input.model.channels[channel];
        Check(values.TypedText(1) == input.group_ids[row] &&
                  values.TypedText(2) == input.bank_ids[bank] &&
                  values.TypedText(3) == input.channel_ids[channel] &&
                  values.TypedInteger(4) == result.stage1.cost_for_left[row] &&
                  values.TypedInteger(5) == checked.channel_costs[row] &&
                  values.TypedReal(6) ==
                      RawCost(input.model.groups[row], b.bank_a, b.bank_b) &&
                  values.TypedReal(7) ==
                      RawCost(input.model.groups[row], c.pin_a, c.pin_b),
              "stored report assignment disagrees with certified solution");
      });
}
std::string ReportStem(const fs::path &selector) {
  auto stem = selector.stem().string();
  const auto last = stem.find_last_of('.');
  if (last != std::string::npos && IsDigest(stem.substr(last + 1)))
    stem.resize(last);
  return stem;
}
struct ReportSource {
  O manifest;
  std::vector<ArtifactRef> refs;
  void Read(const ArtifactRef &selector,
            const volatile std::sig_atomic_t *cancelled) {
    Check(selector.schema() == "chimew-bank-report-selector-v1",
          "report selector schema disagrees");
    D document(D::Read(selector));
    document.Keys({"schema", "logical_schema", "artifact", "application_id",
                   "user_version", "store_bytes", "store_sha256", "header",
                   "assignments", "logical_sha256"});
    Check(document.Text("schema") ==
                  "emuflow.chimew-bank-channel-report-storage/v1" &&
              document.Text("logical_schema") ==
                  "emuflow.chimew-bank-channel-report/v1" &&
              document.Integer("application_id") == 0x454d5552 &&
              document.Integer("user_version") == 1,
          "report selector version disagrees");
    const auto digest = document.Text("store_sha256"),
               logical = document.Text("logical_sha256");
    const fs::path name = document.Text("artifact");
    const auto payload = fs::path(selector.path()).parent_path() / name;
    Check(IsDigest(digest) && IsDigest(logical) &&
              name == ReportStem(selector.path()) + "." + digest + ".sqlite" &&
              !fs::is_symlink(payload),
          "report store escapes selector ownership");
    refs = {selector,
            ArtifactRef::Bind(payload.string(), "chimew-bank-report-store-v1")};
    Check(document.Integer("store_bytes") >= 0 &&
              std::uint64_t(document.Integer("store_bytes")) ==
                  refs[1].generation().bytes &&
              Sha256File(refs[1], cancelled) == digest,
          "report content seal disagrees");
    D header(document.Token("header"));
    header.Keys({"schema", "status", "integration_status", "design", "platform",
                 "provider", "paper_scope", "algorithm2_interpretation",
                 "numeric_policy", "provenance", "input_sha256", "metrics",
                 "direction_priority"});
    Check(header.Text("schema") == document.Text("logical_schema") &&
              IsDigest(header.Text("input_sha256")),
          "logical report namespace/seal disagrees");
    for (const char *key :
         {"status", "integration_status", "design", "platform", "provider",
          "paper_scope", "algorithm2_interpretation", "numeric_policy"})
      Check(!header.Text(key).empty(), "logical report identity is empty");
    D provenance(header.Token("provenance")), metrics(header.Token("metrics")),
        priorities(header.Token("direction_priority"));
    for (const auto &priority : priorities.Fields()) {
      const auto direction = DecodeJsonString(priority.second);
      Check(!priority.first.empty() &&
                (direction == "a_to_b" || direction == "b_to_a"),
            "invalid report direction priority");
    }
    D assignments(document.Token("assignments"));
    assignments.Keys({"records", "sha256"});
    Check(assignments.Integer("records") > 0 &&
              IsDigest(assignments.Text("sha256")),
          "invalid report relation seal");
    const auto canonical_header = D(CanonicalJsonToken(header.text())).Fields();
    AuditStoredReport(refs[1], canonical_header, logical,
                      std::size_t(assignments.Integer("records")), cancelled);
    SqliteStore store(refs[1]);
    Check(DecodeJsonString(Meta(store.database(), "assignments_sha256")) ==
              assignments.Text("sha256"),
          "report selector relation seal disagrees");
    manifest = document.Fields();
    for (const auto &ref : refs)
      ref.RequireUnchanged();
  }
};
fs::path PublishReport(const ArtifactRef &staged, O manifest,
                       const std::vector<ArtifactRef> &sources,
                       const fs::path &target, const fs::path &workspace,
                       const volatile std::sig_atomic_t *cancelled,
                       const std::string &expected_store_hash = "") {
  const auto hash = Sha256File(staged, cancelled), stem = ReportStem(target);
  Check(expected_store_hash.empty() || expected_store_hash == hash,
        "copied report content seal disagrees");
  const auto final_store =
      target.parent_path() / (stem + "." + hash + ".sqlite");
  manifest["artifact"] = JsonQuote(final_store.filename().string());
  manifest["store_bytes"] = std::to_string(staged.generation().bytes);
  manifest["store_sha256"] = JsonQuote(hash);
  std::optional<fs::path> obsolete;
  if (fs::is_regular_file(target)) {
    try {
      ReportSource old;
      old.Read(
          ArtifactRef::Bind(target.string(), "chimew-bank-report-selector-v1"),
          cancelled);
      obsolete = old.refs[1].path();
    } catch (const std::exception &) {
      // Invalid old selectors cannot authorize reclamation of any payload.
    }
  }
  Check(!fs::is_symlink(final_store), "report store owner is a symlink");
  StagedArtifact store_publication(final_store.string()),
      selector_publication(target.string());
  std::vector<std::pair<fs::path, fs::path>> files;
  if (fs::exists(final_store)) {
    const auto prior =
        ArtifactRef::Bind(final_store.string(), "chimew-bank-report-store-v1");
    Check(Sha256File(prior, cancelled) == hash,
          "content-addressed report collision");
  } else {
    fs::rename(staged.path(), store_publication.path());
    files.emplace_back(store_publication.path(), final_store);
  }
  const auto text = D::Encode(manifest);
  PublishTextArtifact(selector_publication.path(), sources,
                      [&](std::ostream &out) { out << text << '\n'; });
  files.emplace_back(selector_publication.path(), target);
  Stop(cancelled);
  PublishArtifactGroup(files, sources);
  if (obsolete && *obsolete != final_store && !fs::is_symlink(*obsolete) &&
      std::none_of(sources.begin(), sources.end(),
                   [&](const auto &ref) { return ref.path() == *obsolete; })) {
    std::error_code ignored;
    fs::remove(*obsolete, ignored);
  }
  return final_store;
}
} // namespace
void ValidateChimewBankSolution(const ChimewBankInput &input,
                                const ChimewBankResult &result,
                                const volatile std::sig_atomic_t *cancelled) {
  (void)Verify(input, result, cancelled);
}
StageResult AssignChimewBankStore(const ArtifactRef &selector,
                                  const std::string &output,
                                  const volatile std::sig_atomic_t *cancelled) {
  BankInput input(cancelled);
  const auto target = fs::absolute(output);
  std::optional<Lease> source_lease, result_lease;
  std::optional<Workspace> work;
  std::optional<fs::path> final_store;
  return RunStage(
      "phase6.assign-bank-store",
      [&] {
        Check(target.extension() == ".json" && !fs::is_symlink(target) &&
                  target != fs::path(selector.path()),
              "invalid output selector owner");
        source_lease.emplace(selector.path(), "emuflow-bank-channel-locks",
                             false);
        input.Read(selector);
        for (const auto &ref : input.sources)
          Check(!fs::exists(target) || !fs::equivalent(target, ref.path()),
                "output aliases input");
        fs::create_directories(target.parent_path());
        result_lease.emplace(target, "emuflow-bank-report-locks", true);
        work.emplace(target.parent_path());
        return input.sources;
      },
      [&](const auto &sources) {
        const auto result = AssignChimewBankRecords(input.model);
        const auto checked = Verify(input.model, result, cancelled);
        O header{{"schema", JsonQuote("emuflow.chimew-bank-channel-report/v1")},
                 {"status", JsonQuote("standalone_paper_kernel")},
                 {"integration_status", JsonQuote("not-a-phase6-pin-plan")},
                 {"design", JsonQuote(input.design)},
                 {"platform", JsonQuote(input.platform)},
                 {"provider",
                  JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
                 {"paper_scope",
                  JsonQuote("FPGA-2026-Sections-3.4.1-through-3.4.3")},
                 {"algorithm2_interpretation",
                  JsonQuote("per-signal-fanout-distance-plus-mean-fanin-"
                            "distance; optional source-bound timing weights "
                            "are an EmuFlow integration extension")},
                 {"numeric_policy",
                  JsonQuote("nearest-integer-cost-rank-with-explicit-scale")},
                 {"provenance", D::Encode(input.provenance)},
                 {"input_sha256", JsonQuote(input.logical)}};
        O metrics = input.metrics;
        metrics["stage1_cost_rank"] = std::to_string(result.stage1.total_cost);
        metrics["stage2_cost_rank"] = std::to_string(result.channel_cost);
        metrics["certified_matchings"] =
            std::to_string(1 + 2 * checked.active_banks);
        metrics["certificate_disagreements"] = "0";
        O priorities;
        for (std::size_t b = 0; b < input.model.banks.size(); ++b)
          if (!result.alternatives[b].first.groups.empty())
            priorities[input.bank_ids[b]] =
                JsonQuote(result.chosen_priority[b] ? "b_to_a" : "a_to_b");
        header["direction_priority"] = D::Encode(priorities);
        double raw_bank = 0, raw_channel = 0;
        Hash rows_hash;
        std::vector<fs::path> outputs;
        std::string encoded_header, logical_digest;
        ArtifactRef staged = [&] {
          SqliteArtifactWriter writer((work->root / "report.sqlite").string(),
                                      "chimew-bank-report-store-v1", sources);
          auto &db = writer.database();
          db.Execute("PRAGMA application_id=0x454d5552;PRAGMA user_version=1");
          db.Execute(report_ddl);
          db.Execute("BEGIN");
          {
            auto add =
                db.Prepare("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?)");
            for (std::size_t i = 0; i < input.model.groups.size(); ++i) {
              Stop(cancelled);
              const auto bank = result.stage1.right_for_left[i],
                         channel = checked.channels[i];
              const auto &b = input.model.banks[bank];
              const auto &c = input.model.channels[channel];
              const auto bc =
                             RawCost(input.model.groups[i], b.bank_a, b.bank_b),
                         cc = RawCost(input.model.groups[i], c.pin_a, c.pin_b);
              raw_bank += bc;
              raw_channel += cc;
              rows_hash.Add(AssignmentRow(input, result, checked, i) + "\n");
              add.BindInteger(1, i);
              add.BindText(2, input.group_ids[i]);
              add.BindText(3, input.bank_ids[bank]);
              add.BindText(4, input.channel_ids[channel]);
              add.BindInteger(5, result.stage1.cost_for_left[i]);
              add.BindInteger(6, checked.channel_costs[i]);
              add.BindReal(7, bc);
              add.BindReal(8, cc);
              add.Execute();
              add.Reset();
            }
            metrics["stage1_physical_site_cost"] =
                JsonRecord(raw_bank).Encode();
            metrics["stage2_physical_site_cost"] =
                JsonRecord(raw_channel).Encode();
            header["metrics"] = D::Encode(metrics);
            encoded_header = D::Encode(header);
            O logical = header;
            logical["assignments"] = "";
            Hash h;
            h.Add("{");
            bool first = true;
            for (const auto &field : logical) {
              if (!first)
                h.Add(",");
              first = false;
              h.Add(JsonQuote(field.first) + ":");
              if (field.first == "assignments") {
                h.Add("[");
                for (std::size_t i = 0; i < input.model.groups.size(); ++i) {
                  if (i)
                    h.Add(",");
                  h.Add(AssignmentRow(input, result, checked, i));
                }
                h.Add("]");
              } else
                h.Add(field.second);
            }
            h.Add("}");
            logical_digest = h.Finish();
            const auto relation = rows_hash.Finish();
            auto metadata = db.Prepare("INSERT INTO metadata VALUES(?,?)");
            for (const auto &m :
                 O{{"header", encoded_header},
                   {"assignments_sha256", JsonQuote(relation)},
                   {"logical_sha256", JsonQuote(logical_digest)}}) {
              metadata.BindText(1, m.first);
              metadata.BindText(2, m.second);
              metadata.Execute();
              metadata.Reset();
            }
            db.Execute("COMMIT");
          }
          return writer.Commit();
        }();
        AuditReport(staged, input, result, checked, header, logical_digest,
                    cancelled);
        const auto relation = [&] {
          SqliteStore store(staged);
          return DecodeJsonString(Meta(store.database(), "assignments_sha256"));
        }();
        O manifest{
            {"schema",
             JsonQuote("emuflow.chimew-bank-channel-report-storage/v1")},
            {"logical_schema", header.at("schema")},
            {"application_id", std::to_string(0x454d5552)},
            {"user_version", "1"},
            {"header", encoded_header},
            {"assignments",
             D::Encode({{"records", std::to_string(input.model.groups.size())},
                        {"sha256", JsonQuote(relation)}})},
            {"logical_sha256", JsonQuote(logical_digest)}};
        final_store = PublishReport(staged, std::move(manifest), sources,
                                    target, work->root, cancelled);
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(final_store->string(),
                              "chimew-bank-report-store-v1"),
            ArtifactRef::Bind(target.string(),
                              "chimew-bank-report-selector-v1")};
      });
}
StageResult CopyChimewBankReport(const ArtifactRef &selector,
                                 const std::string &output,
                                 const volatile std::sig_atomic_t *cancelled) {
  ReportSource source;
  const auto target = fs::absolute(output);
  std::optional<Lease> first_lease, second_lease;
  std::optional<Workspace> work;
  std::optional<fs::path> store_path;
  bool same = false;
  return RunStage(
      "phase6.copy-bank-report",
      [&] {
        Check(target.extension() == ".json" && !fs::is_symlink(target),
              "invalid report output owner");
        const auto from = fs::weakly_canonical(selector.path()),
                   to = fs::weakly_canonical(target);
        same = from == to;
        // Acquire selector locks in stable path order for opposing transfers.
        if (same || from < to) {
          first_lease.emplace(from, "emuflow-bank-report-locks", false);
          if (!same)
            second_lease.emplace(to, "emuflow-bank-report-locks", true);
        } else {
          first_lease.emplace(to, "emuflow-bank-report-locks", true);
          second_lease.emplace(from, "emuflow-bank-report-locks", false);
        }
        source.Read(selector, cancelled);
        for (const auto &ref : source.refs)
          Check(same || !fs::exists(target) ||
                    !fs::equivalent(target, ref.path()),
                "report output aliases input");
        if (!same) {
          fs::create_directories(target.parent_path());
          work.emplace(target.parent_path());
        }
        return source.refs;
      },
      [&](const auto &refs) {
        if (same) {
          store_path = refs[1].path();
          return;
        }
        const auto copy = work->root / "report.sqlite";
        fs::copy_file(refs[1].path(), copy);
        const auto staged =
            ArtifactRef::Bind(copy.string(), "chimew-bank-report-store-v1");
        store_path = PublishReport(
            staged, source.manifest, refs, target, work->root, cancelled,
            DecodeJsonString(source.manifest.at("store_sha256")));
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(store_path->string(),
                              "chimew-bank-report-store-v1"),
            ArtifactRef::Bind(target.string(),
                              "chimew-bank-report-selector-v1")};
      });
}

ChimewBankValidation InspectChimewBankInput(const ArtifactRef& input,
    const volatile std::sig_atomic_t* cancelled) {
  Lease lease(input.path(), "emuflow-bank-channel-locks", false);
  BankInput bank(cancelled); bank.Read(input);
  ChimewBankValidation proof;
  proof.design=bank.design;proof.platform=bank.platform;
  proof.input_sha256=bank.logical;proof.provenance=D::Encode(bank.provenance);
  proof.metrics=D::Encode(bank.metrics);proof.inputs=bank.sources;
  for(const auto& ref:proof.inputs)ref.RequireUnchanged();
  return proof;
}

ChimewBankValidation
ValidateChimewBankAssignment(const ArtifactRef &input,
                             const ArtifactRef &report,
                             const ChimewBankGroupVisitor &visit_group,
                             const volatile std::sig_atomic_t *cancelled) {
  Lease input_lease(input.path(), "emuflow-bank-channel-locks", false),
      report_lease(report.path(), "emuflow-bank-report-locks", false);
  BankInput bank(cancelled);
  bank.Read(input);
  ReportSource solution;
  solution.Read(report, cancelled);
  D header(solution.manifest.at("header"));
  const O expected{
      {"schema", JsonQuote("emuflow.chimew-bank-channel-report/v1")},
      {"status", JsonQuote("standalone_paper_kernel")},
      {"integration_status", JsonQuote("not-a-phase6-pin-plan")},
      {"provider", JsonQuote("chimew-section3.4-two-stage-assignment-v1")},
      {"paper_scope", JsonQuote("FPGA-2026-Sections-3.4.1-through-3.4.3")},
      {"algorithm2_interpretation",
       JsonQuote(
           "per-signal-fanout-distance-plus-mean-fanin-distance; optional "
           "source-bound timing weights are an EmuFlow integration extension")},
      {"numeric_policy",
       JsonQuote("nearest-integer-cost-rank-with-explicit-scale")},
      {"design", JsonQuote(bank.design)},
      {"platform", JsonQuote(bank.platform)},
      {"input_sha256", JsonQuote(bank.logical)},
      {"provenance", D::Encode(bank.provenance)}};
  SqliteDatabase compare(":memory:", SqliteDatabase::Mode::kReadOnly);
  for (const auto &field : expected)
    RequireEquivalentJson(
        compare, header.Token(field.first), field.second,
        "Chimew bank/channel report provenance does not agree");
  D relation(solution.manifest.at("assignments"));
  Check(relation.Integer("records") == I(bank.model.groups.size()),
        "bank/channel assignment coverage is invalid");
  std::map<std::string, int> banks, channels;
  for (std::size_t i = 0; i < bank.bank_ids.size(); ++i)
    banks.emplace(bank.bank_ids[i], int(i));
  for (std::size_t i = 0; i < bank.channel_ids.size(); ++i)
    channels.emplace(bank.channel_ids[i], int(i));
  SqliteDatabase joined(":memory:", SqliteDatabase::Mode::kCreateExclusive);
  {
    auto attach = joined.Prepare("ATTACH DATABASE ? AS problem");
    attach.BindText(1, SqliteImmutableUri(bank.store->path()));
    attach.Execute();
  }
  {
    auto attach = joined.Prepare("ATTACH DATABASE ? AS solution");
    attach.BindText(1, SqliteImmutableUri(solution.refs[1].path()));
    attach.Execute();
  }
  std::vector<std::vector<std::pair<int, int>>> assigned(
      bank.model.banks.size());
  I stage1 = 0, stage2 = 0;
  double raw1 = 0, raw2 = 0;
  auto rows = joined.Prepare(
      "SELECT grouping.seq,grouping.group_id,grouping.record_json,"
      "assignment.bank_pair,assignment.channel,assignment.bank_cost_rank,"
      "assignment.channel_cost_rank,assignment.bank_cost,assignment.channel_"
      "cost "
      "FROM problem.groups AS grouping LEFT JOIN solution.assignments AS "
      "assignment "
      "ON assignment.group_id=grouping.group_id ORDER BY grouping.seq");
  std::size_t count = 0;
  const auto close = [](double a, double b) {
    return std::isfinite(a) && std::isfinite(b) &&
           std::abs(a - b) <=
               std::max(1e-9, 1e-10 * std::max(std::abs(a), std::abs(b)));
  };
  while (rows.Step()) {
    Stop(cancelled);
    Check(count < bank.model.groups.size() &&
              rows.TypedInteger(0) == I(count) &&
              rows.TypedText(1) == bank.group_ids[count] && !rows.IsNull(3) &&
              !rows.IsNull(4),
          "bank/channel assignment coverage is invalid");
    const auto b = banks.find(rows.TypedText(3)),
               c = channels.find(rows.TypedText(4));
    Check(b != banks.end() && c != channels.end(),
          "bank/channel assignment coverage is invalid");
    const auto &group = bank.model.groups[count];
    const auto &owner = bank.model.banks[b->second];
    const auto &channel = bank.model.channels[c->second];
    Check(owner.domain == group.domain && channel.bank == b->second,
          "bank/channel assignment coverage is invalid");
    const auto cost1 = Cost(group, owner.bank_a, owner.bank_b,
                            bank.model.cost_scale),
               cost2 = Cost(group, channel.pin_a, channel.pin_b,
                            bank.model.cost_scale);
    const auto physical1 = RawCost(group, owner.bank_a, owner.bank_b),
               physical2 = RawCost(group, channel.pin_a, channel.pin_b);
    Check(rows.TypedInteger(5) == cost1 && rows.TypedInteger(6) == cost2 &&
              close(rows.TypedReal(7), physical1) &&
              close(rows.TypedReal(8), physical2),
          "bank/channel assignment cost does not agree");
    Sum(stage1, cost1);
    Sum(stage2, cost2);
    raw1 += physical1;
    raw2 += physical2;
    assigned[b->second].emplace_back(int(count), channel.order);
    if (visit_group)
      visit_group(count, rows.TypedText(2));
    ++count;
  }
  Check(count == bank.model.groups.size(),
        "bank/channel assignment coverage is invalid");
  O priorities;
  for (std::size_t b = 0; b < assigned.size(); ++b) {
    if (assigned[b].empty())
      continue;
    std::array<I, 3> counts{};
    I common = 0;
    for (const auto &assignment : assigned[b]) {
      const auto &group = bank.model.groups[assignment.first];
      if (group.kind == 0)
        ++counts[group.direction];
      else
        ++common;
    }
    const auto compatible = [&](int first) {
      const auto second = 1 - first;
      const auto dedicated =
          common == 0 && std::count_if(counts.begin(), counts.end(),
                                       [](I n) { return n != 0; }) == 1;
      for (const auto &assignment : assigned[b]) {
        const auto &group = bank.model.groups[assignment.first];
        const auto position = assignment.second;
        int direction = -1;
        if (dedicated) {
          for (int i = 0; i < 3; ++i)
            if (counts[i])
              direction = i;
        } else if (position < counts[first])
          direction = first;
        else if (position < counts[first] + counts[second])
          direction = second;
        else if (position < counts[0] + counts[1] + counts[2])
          direction = 2;
        if (direction >= 0 ? group.kind != 0 || group.direction != direction
                           : group.kind != 1)
          return false;
      }
      return true;
    };
    const bool first = compatible(0), second = compatible(1);
    Check(first || second, "direction priority does not agree");
    priorities[bank.bank_ids[b]] = JsonQuote(first ? "a_to_b" : "b_to_a");
  }
  RequireEquivalentJson(compare, header.Token("direction_priority"),
                        D::Encode(priorities),
                        "Chimew direction priority does not agree");
  O metrics = bank.metrics;
  metrics["stage1_cost_rank"] = std::to_string(stage1);
  metrics["stage2_cost_rank"] = std::to_string(stage2);
  metrics["stage1_physical_site_cost"] = JsonRecord(raw1).Encode();
  metrics["stage2_physical_site_cost"] = JsonRecord(raw2).Encode();
  metrics["certified_matchings"] = std::to_string(1 + 2 * priorities.size());
  metrics["certificate_disagreements"] = "0";
  D supplied(header.Token("metrics"));
  Check(supplied.Fields().size() == metrics.size(),
        "bank/channel report metrics do not agree");
  for (const auto &field : metrics) {
    const bool physical = field.first == "stage1_physical_site_cost" ||
                          field.first == "stage2_physical_site_cost";
    Check(physical
              ? close(supplied.Number(field.first),
                      ParseFiniteDecimal(field.second, "physical cost"))
              : supplied.Integer(field.first) == D::IntegerToken(field.second),
          "bank/channel report metrics do not agree");
  }
  ChimewBankValidation proof;
  proof.design = bank.design;
  proof.platform = bank.platform;
  proof.input_sha256 = bank.logical;
  proof.report_sha256 =
      DecodeJsonString(solution.manifest.at("logical_sha256"));
  proof.provenance = D::Encode(bank.provenance);
  proof.metrics = D::Encode(metrics);
  proof.inputs = bank.sources;
  proof.inputs.insert(proof.inputs.end(), solution.refs.begin(),
                      solution.refs.end());
  for (const auto &ref : proof.inputs)
    ref.RequireUnchanged();
  return proof;
}
StageResult
AuditChimewBankAssignment(const ArtifactRef &input, const ArtifactRef &report,
                          const std::string &summary,
                          const volatile std::sig_atomic_t *cancelled) {
  std::optional<ChimewBankValidation> proof;
  return RunStage(
      "phase6.audit-bank-assignment",
      [&] {
        proof = ValidateChimewBankAssignment(input, report, {}, cancelled);
        return proof->inputs;
      },
      [&](const auto &refs) {
        PublishTextArtifact(summary, refs, [&](std::ostream &out) {
          out << D::Encode(
                     {{"schema",
                       JsonQuote(
                           "emuflow.chimew-bank-assignment-validation/v1")},
                      {"status", JsonQuote("pass")},
                      {"design", JsonQuote(proof->design)},
                      {"platform", JsonQuote(proof->platform)},
                      {"input_sha256", JsonQuote(proof->input_sha256)},
                      {"report_sha256", JsonQuote(proof->report_sha256)},
                      {"provenance", proof->provenance},
                      {"metrics", proof->metrics}})
              << '\n';
        });
      },
      [&] {
        return std::vector<ArtifactRef>{
            ArtifactRef::Bind(summary, "chimew-bank-validation-v1")};
      });
}
} // namespace emuflow::core
