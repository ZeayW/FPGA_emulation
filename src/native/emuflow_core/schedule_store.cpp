#include "emuflow_core/schedule_store.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/schedule_store_schema.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include "emuflow_core/route_view.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <functional>
#include <limits>
#include <numeric>
#include <set>
#include <sstream>
#include <tuple>

namespace emuflow::core {
namespace {
using Object = std::map<std::string, JsonRecord>;
using Array = std::vector<JsonRecord>;
constexpr std::uint64_t kInputMagic = 0x3149534d44544545ULL;
constexpr std::uint64_t kResultMagic = 0x314f534d44544545ULL;
constexpr std::uint64_t kRequestMagic = 0x3157534d44544545ULL;
void Require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
void Hash(Sha256& digest, const std::string& value) {
  digest.update(reinterpret_cast<const unsigned char*>(value.data()), value.size());
}
std::string Hex(Sha256& digest) {
  static const char* digits = "0123456789abcdef";
  std::string result;
  for (auto byte : digest.finish()) { result += digits[byte >> 4]; result += digits[byte & 15]; }
  return result;
}
std::string IndentLines(const std::string& value, unsigned spaces, bool first) {
  std::string result = first ? std::string(spaces, ' ') : "";
  for (char c : value) { result += c; if (c == '\n') result += std::string(spaces, ' '); }
  return result;
}
std::vector<std::string> Strings(BinaryReader& reader) {
  auto count = reader.Read<std::uint32_t>();
  Require(count <= reader.remaining() / 5, "schedule identity catalog is truncated");
  std::vector<std::string> result;
  for (std::uint32_t i = 0; i < count; ++i) {
    auto value = reader.ReadString32();
    Require(i == 0 || value > result.back(), "schedule identity catalog is not ordered and unique");
    (void)JsonQuote(value);
    result.push_back(std::move(value));
  }
  return result;
}
struct Encoded { std::string compact, pretty; };
struct Hop { std::uint32_t depth, capacity, link, from, to, offset, budget, assigned, ratio, latency; };
struct Route {
  std::string demand, net;
  std::uint32_t source = 0, rank = 0;
  std::vector<std::uint32_t> sinks, edge_order;
  std::vector<Hop> hops;
  std::map<std::tuple<std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t>,std::size_t> hop_index;
};
struct TemporaryStore {
  std::string path;
  explicit TemporaryStore(const std::string& output) {
    static std::atomic<std::uint64_t> sequence{0};
    path = output + ".publishing." + std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count()) + "." + std::to_string(sequence++);
  }
  ~TemporaryStore() { std::error_code error; std::filesystem::remove(path, error); }
};
Encoded ReadEncoded(BinaryReader& reader, SqliteDatabase& db) {
  Encoded result{reader.ReadString32(), reader.ReadString32()};
  auto valid = db.Prepare("SELECT json_valid(?),json_valid(?),json(?)=json(?)");
  valid.BindText(1, result.compact); valid.BindText(2, result.pretty);
  valid.BindText(3, result.compact); valid.BindText(4, result.pretty);
  Require(valid.Step() && valid.Integer(0) && valid.Integer(1) && valid.Integer(2),
          "schedule publication JSON encodings disagree");
  return result;
}
}  // namespace

void PublishExactScheduleStore(const std::string& input_path,
    const std::string& result_path, const std::string& route_store_path,
    const std::string& request_path, const std::string& output_path) {
  BinaryReader input(input_path), result(result_path), request(request_path);
  ReadAndValidateHeader(input, {kInputMagic, 1, "exact schedule input"});
  ReadAndValidateHeader(result, {kResultMagic, 2, "exact schedule result"});
  ReadAndValidateHeader(request, {kRequestMagic, 1, "exact schedule publication request"});
  std::vector<ArtifactRef> sources;
  for (const auto& item : std::vector<std::pair<std::string,std::string>>{
      {input_path,"exact-schedule-input-v1"},{result_path,"exact-schedule-output-v2"},
      {route_store_path,"system-route-store-v3"}}) {
    FileGeneration expected;
    expected.device = request.Read<std::uint64_t>(); expected.inode = request.Read<std::uint64_t>();
    expected.bytes = request.Read<std::uint64_t>(); expected.modified_seconds = request.Read<std::int64_t>();
    expected.modified_nanoseconds = request.Read<std::int64_t>(); expected.changed_seconds = request.Read<std::int64_t>();
    expected.changed_nanoseconds = request.Read<std::int64_t>();
    auto source = ArtifactRef::Bind(item.first,item.second);
    if (!(source.generation() == expected))
      throw StageError(ErrorCode::kMixedGeneration,"native ScheduleStore source generation changed");
    sources.push_back(std::move(source));
  }
  const auto frame = input.Read<std::uint32_t>();
  const auto barrier = input.Read<std::uint32_t>();
  input.Read<std::uint32_t>();  // settle; already independently qualified
  const auto fpga_count = input.Read<std::uint32_t>();
  const auto capacity_count = input.Read<std::uint32_t>();
  const auto route_count = input.Read<std::uint32_t>();
  const auto fpga = Strings(request), links = Strings(request), capacities = Strings(request);
  Require(fpga_count && frame > barrier && fpga.size() == fpga_count && capacities.size() == capacity_count &&
          route_count <= input.remaining() / 28,
          "schedule publication identity dimensions disagree");
  for (std::uint32_t i = 0; i < capacity_count; ++i)
    Require(input.Read<std::uint32_t>() > 0, "schedule publication capacity is invalid");
  std::vector<Route> routes(route_count);
  std::vector<std::uint32_t> demand_order(route_count);
  std::vector<bool> ranks(route_count, false);
  std::uint64_t expected_entries = 0;
  for (std::uint32_t i = 0; i < route_count; ++i) {
    auto& route = routes[i];
    route.source = input.Read<std::uint32_t>();
    const auto sinks = input.Read<std::uint32_t>(), predecessors = input.Read<std::uint32_t>(), hops = input.Read<std::uint32_t>();
    input.Read<std::uint32_t>(); input.Read<std::uint32_t>();
    route.rank = input.Read<std::uint32_t>();
    Require(route.source < fpga_count && route.rank < route_count && !ranks[route.rank] && sinks &&
            std::uint64_t(sinks) * 4 + std::uint64_t(predecessors) * 4 + std::uint64_t(hops) * 40 <= input.remaining(),
            "schedule publication route dimensions are invalid");
    ranks[route.rank] = true; demand_order[route.rank] = i;
    for (std::uint32_t j = 0; j < sinks; ++j) {
      auto sink = input.Read<std::uint32_t>();
      Require(sink < fpga_count, "schedule publication sink is invalid"); route.sinks.push_back(sink);
    }
    for (std::uint32_t j = 0; j < predecessors; ++j)
      Require(input.Read<std::uint32_t>() < route_count, "schedule publication predecessor is invalid");
    for (std::uint32_t j = 0; j < hops; ++j) {
      Hop hop{input.Read<std::uint32_t>(), input.Read<std::uint32_t>(), input.Read<std::uint32_t>(),
              input.Read<std::uint32_t>(), input.Read<std::uint32_t>(), input.Read<std::uint32_t>(),
              input.Read<std::uint32_t>(), input.Read<std::uint32_t>(), input.Read<std::uint32_t>(), input.Read<std::uint32_t>()};
      Require(hop.capacity < capacity_count && hop.link < links.size() && hop.from < fpga_count && hop.to < fpga_count &&
              hop.budget && route.hop_index.emplace(std::make_tuple(hop.depth,hop.capacity,hop.link,hop.from,hop.to),route.hops.size()).second,
              "schedule publication hop identity is invalid");
      route.hops.push_back(hop);
    }
    expected_entries += hops;
  }
  input.RequireEnd();
  RouteView route_store(sources[2]);
  Require(route_store.count() == route_count,"schedule publication RouteStore count disagrees");
  const auto catalog_index = [](const auto& catalog, const std::string& name) {
    auto found = std::lower_bound(catalog.begin(),catalog.end(),name);
    Require(found != catalog.end() && *found == name,"schedule publication RouteStore catalog identity is unknown");
    return static_cast<std::uint32_t>(found - catalog.begin());
  };
  route_store.VisitNetOrder([&](std::uint64_t index, const RouteIdentity& identity) {
    auto& route = routes[index];
    route.demand = identity.demand; route.net = identity.net;
    (void)JsonQuote(route.demand); (void)JsonQuote(route.net);
    Require(identity.source == fpga[route.source] && identity.sinks.size() == route.sinks.size() &&
            identity.edges.size() == route.hops.size(),"schedule publication RouteStore topology disagrees");
    for (std::size_t i = 0; i < identity.sinks.size(); ++i)
      Require(identity.sinks[i] == fpga[route.sinks[i]],"schedule publication RouteStore sink order disagrees");
    std::map<std::tuple<std::uint32_t,std::uint32_t,std::uint32_t>,std::size_t> hops;
    for (std::size_t i = 0; i < route.hops.size(); ++i) {
      const auto& hop = route.hops[i];
      Require(hops.emplace(std::make_tuple(hop.link,hop.from,hop.to),i).second,
              "schedule publication route has a repeated edge");
    }
    std::vector<bool> seen(route.hops.size(),false);
    for (const auto& edge : identity.edges) {
      const auto key = std::make_tuple(catalog_index(links,edge.link),catalog_index(fpga,edge.from),catalog_index(fpga,edge.to));
      auto found = hops.find(key);
      Require(found != hops.end() && !seen[found->second],"schedule publication RouteStore edge disagrees");
      seen[found->second] = true; route.edge_order.push_back(found->second);
    }
  });
  for (std::uint32_t i = 1; i < route_count; ++i)
    Require(routes[demand_order[i-1]].demand < routes[demand_order[i]].demand,
            "schedule publication demand ordering disagrees");
  const auto entry_count = result.Read<std::uint64_t>();
  Require(entry_count == expected_entries && result.Read<std::uint32_t>() == route_count,
          "schedule publication result coverage disagrees");
  std::vector<bool> order_seen(route_count, false);
  for (std::uint32_t i = 0; i < route_count; ++i) {
    auto route = result.Read<std::uint32_t>();
    Require(route < route_count && !order_seen[route], "schedule publication execution order is invalid"); order_seen[route] = true;
  }
  const auto entry_offset = result.size() - result.remaining();
  Require(entry_count <= result.remaining() / 48, "schedule publication entries are truncated");
  result.Seek(entry_offset + static_cast<std::size_t>(entry_count * 48));
  std::vector<std::size_t> completion_offsets(route_count);
  std::vector<bool> completed(route_count, false);
  for (std::uint32_t i = 0; i < route_count; ++i) {
    const auto offset = result.size() - result.remaining();
    const auto route = result.Read<std::uint32_t>();
    result.Read<std::uint32_t>(); result.Read<std::uint32_t>();
    const auto sinks = result.Read<std::uint32_t>();
    Require(route < route_count && !completed[route] && sinks == routes[route].sinks.size() && sinks <= result.remaining() / 8,
            "schedule publication completion coverage is invalid");
    completed[route] = true; completion_offsets[route] = offset;
    result.Seek(result.size() - result.remaining() + static_cast<std::size_t>(sinks) * 8);
  }
  result.RequireEnd();

  TemporaryStore temporary(output_path);
  std::string summary;
  {
    SqliteDatabase db(temporary.path, SqliteDatabase::Mode::kCreateExclusive);
    db.Execute("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE;"
               "PRAGMA cache_size=-65536; PRAGMA application_id=0x454d5554; PRAGMA user_version=3;");
    db.Execute(kScheduleStoreDDL);
    SqliteTransaction transaction(db);
    const auto header_count = request.Read<std::uint32_t>();
    Require(header_count <= 4096, "schedule publication header is too large");
    std::map<std::string, Encoded> header;
    for (std::uint32_t i = 0; i < header_count; ++i) {
      auto key = request.ReadString32(); auto value = ReadEncoded(request, db);
      Require(header.emplace(key, std::move(value)).second, "schedule publication header key repeats");
    }
    Require(header.count("schema") && header.at("schema").compact == "\"emuflow.tdm-schedule/v1\"" &&
            header.count("provider") && header.at("provider").compact == "\"deterministic-dependency-earliest-slot-v1\"" &&
            header.count("qualification") && header.at("qualification").compact == "\"dependency-schedule-readiness-pass\"",
            "schedule publication requires the qualified exact contract");
    const auto domains = request.Read<std::uint32_t>();
    Require(domains <= request.remaining() / 15, "schedule publication domain records are truncated");
    std::vector<std::pair<std::string, Encoded>> domain_records;
    for (std::uint32_t i = 0; i < domains; ++i) {
      auto identity = request.ReadString32(); auto encoded = ReadEncoded(request, db);
      domain_records.emplace_back(std::move(identity), std::move(encoded));
    }
    request.RequireEnd();
    auto metadata = db.Prepare("INSERT INTO metadata VALUES(?,?)");
    const auto put_metadata = [&](const std::string& key, const std::string& value) {
      metadata.BindText(1, key); metadata.BindText(2, value); metadata.Execute();
    };
    auto insert_entry = db.Prepare("INSERT INTO schedule_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
    auto insert_route = db.Prepare("INSERT INTO schedule_routes VALUES(?,?,?,?)");
    auto insert_sink = db.Prepare("INSERT INTO schedule_route_sinks VALUES(?,?,?)");
    auto insert_edge = db.Prepare("INSERT INTO schedule_route_edges VALUES(?,?,?,?,?)");
    auto insert_completion = db.Prepare("INSERT INTO schedule_completions VALUES(?,?,?,?,?)");
    auto insert_arrival = db.Prepare("INSERT INTO schedule_completion_arrivals VALUES(?,?,?,?)");
    auto insert_record = db.Prepare("INSERT INTO records VALUES(?,?,?,?)");
    Sha256 logical, pretty;
    std::map<std::string, JsonRecord> sections;
    std::map<std::string,std::uint64_t> relation_counts{{"route_sinks",0},{"route_edges",0},{"completion_arrivals",0}};
    std::set<std::string> keys;
    for (const auto& item : header) keys.insert(item.first);
    for (const auto* section : {"routes","entries","demand_completions","domain_schedules"})
      Require(keys.insert(section).second, "schedule publication header contains a record section");
    Hash(logical, "{"); Hash(pretty, "{\n");
    std::size_t key_index = 0;
    for (const auto& key : keys) {
      if (key_index++) Hash(logical, ",");
      Hash(logical, JsonQuote(key) + ":"); Hash(pretty, "  " + JsonQuote(key) + ": ");
      if (header.count(key)) {
        const auto& value = header.at(key); Hash(logical, value.compact); Hash(pretty, IndentLines(value.pretty,2,false));
      } else {
        const auto count = key == "entries" ? entry_count : key == "domain_schedules" ? domains : route_count;
        Sha256 section_hash;
        Hash(logical, "["); Hash(pretty, count ? "[\n" : "[]");
        const auto emit_encoded = [&](std::uint64_t seq, const Encoded& record) {
          if (seq) Hash(logical, ",");
          Hash(logical, record.compact); Hash(section_hash, record.compact + "\n");
          Hash(pretty, IndentLines(record.pretty,4,true));
          if (seq + 1 < count) Hash(pretty, ",\n");
        };
        const auto emit = [&](std::uint64_t seq, const JsonRecord& record) {
          emit_encoded(seq, {record.Encode(), record.Encode(true)});
        };
        if (key == "entries") {
          result.Seek(entry_offset);
          std::vector<bool> sequences(entry_count, false);
          std::vector<std::vector<bool>> seen_hops;
          for (const auto& route : routes) seen_hops.emplace_back(route.hops.size(), false);
          for (std::uint64_t seq = 0; seq < entry_count; ++seq) {
            std::array<std::uint32_t,12> values;
            for (auto& value : values) value = result.Read<std::uint32_t>();
            const auto route_index = values[0], sequence = values[1];
            Require(route_index < route_count && sequence < entry_count && !sequences[sequence],
                    "schedule publication entry identity is invalid");
            sequences[sequence] = true;
            const auto& route = routes[route_index];
            const auto found = route.hop_index.find(std::make_tuple(values[2],values[3],values[4],values[5],values[6]));
            const auto selected = found == route.hop_index.end() ? route.hops.size() : found->second;
            Require(selected < route.hops.size() && !seen_hops[route_index][selected],
                    "schedule publication entry disagrees with its route");
            seen_hops[route_index][selected] = true;
            const auto& hop = route.hops[selected];
            const auto slot = values[7], lane = values[8], ready = values[9], arrival = values[10], ratio = values[11];
            Require(slot >= ready && slot < frame - barrier && std::uint64_t(slot) + hop.latency == arrival &&
                    lane >= hop.offset && std::uint64_t(lane) < std::uint64_t(hop.offset) + hop.budget &&
                    (hop.assigned == UINT32_MAX ? ratio == 0 : ratio == hop.ratio && lane == hop.assigned),
                    "schedule publication entry timing/lane disagrees");
            std::ostringstream identity; identity << 's'; identity.width(6); identity.fill('0'); identity << sequence;
            const auto id = identity.str();
            insert_entry.BindInteger(1,seq); insert_entry.BindText(2,id); insert_entry.BindText(3,route.demand);
            insert_entry.BindText(4,route.net); insert_entry.BindInteger(5,hop.depth); insert_entry.BindText(6,links[hop.link]);
            insert_entry.BindText(7,fpga[hop.from]); insert_entry.BindText(8,fpga[hop.to]); insert_entry.BindText(9,capacities[hop.capacity]);
            insert_entry.BindInteger(10,slot); insert_entry.BindInteger(11,lane); insert_entry.BindInteger(12,ready);
            insert_entry.BindInteger(13,arrival); if (ratio) insert_entry.BindInteger(14,ratio); else insert_entry.BindNull(14); insert_entry.Execute();
            Object record{{"id",id},{"demand",route.demand},{"net",route.net},{"hop",std::uint64_t(hop.depth)},
              {"link",links[hop.link]},{"from",fpga[hop.from]},{"to",fpga[hop.to]}, {"capacity_key",capacities[hop.capacity]},
              {"slot",std::uint64_t(slot)},{"lane",std::uint64_t(lane)},{"ready_slot",std::uint64_t(ready)},{"arrival_slot",std::uint64_t(arrival)}};
            if (ratio) record.emplace("tdm_ratio",std::uint64_t(ratio));
            emit(seq,JsonRecord(std::move(record)));
          }
        } else if (key == "routes") {
          for (std::uint32_t seq = 0; seq < route_count; ++seq) {
            const auto& route = routes[demand_order[seq]];
            insert_route.BindInteger(1,seq); insert_route.BindText(2,route.demand); insert_route.BindText(3,route.net);
            insert_route.BindText(4,fpga[route.source]); insert_route.Execute();
            Array sinks, edges;
            for (std::size_t j = 0; j < route.sinks.size(); ++j) {
              const auto& sink = fpga[route.sinks[j]]; sinks.emplace_back(sink);
              insert_sink.BindInteger(1,seq); insert_sink.BindInteger(2,j); insert_sink.BindText(3,sink); insert_sink.Execute();
            }
            for (std::size_t j = 0; j < route.edge_order.size(); ++j) {
              const auto& hop = route.hops[route.edge_order[j]];
              insert_edge.BindInteger(1,seq); insert_edge.BindInteger(2,j); insert_edge.BindText(3,links[hop.link]);
              insert_edge.BindText(4,fpga[hop.from]); insert_edge.BindText(5,fpga[hop.to]); insert_edge.Execute();
              edges.emplace_back(Object{{"link",links[hop.link]},{"from",fpga[hop.from]},{"to",fpga[hop.to]}});
            }
            relation_counts["route_sinks"] += sinks.size(); relation_counts["route_edges"] += edges.size();
            emit(seq,JsonRecord(Object{{"id",route.demand},{"net",route.net},{"source",fpga[route.source]},
                                     {"sinks",JsonRecord(std::move(sinks))},{"tree_edges",JsonRecord(std::move(edges))}}));
          }
        } else if (key == "demand_completions") {
          for (std::uint32_t seq = 0; seq < route_count; ++seq) {
            const auto route_index = demand_order[seq]; const auto& route = routes[route_index];
            result.Seek(completion_offsets[route_index]);
            Require(result.Read<std::uint32_t>() == route_index,"schedule publication completion changed");
            auto ready = result.Read<std::uint32_t>(), completion = result.Read<std::uint32_t>(), sinks = result.Read<std::uint32_t>();
            Object arrivals; std::map<std::string,std::uint32_t> values;
            std::set<std::uint32_t> seen;
            const std::set<std::uint32_t> wanted(route.sinks.begin(),route.sinks.end());
            Require(wanted.size() == sinks, "schedule publication route sinks repeat");
            std::uint32_t maximum = 0;
            for (std::uint32_t j = 0; j < sinks; ++j) {
              auto sink = result.Read<std::uint32_t>(), arrival = result.Read<std::uint32_t>();
              Require(sink < fpga_count && seen.insert(sink).second && wanted.count(sink) != 0,
                      "schedule publication completion sink is invalid");
              maximum = std::max(maximum,arrival); values.emplace(fpga[sink],arrival); arrivals.emplace(fpga[sink],std::uint64_t(arrival));
            }
            Require(maximum == completion, "schedule publication completion maximum disagrees");
            insert_completion.BindInteger(1,seq); insert_completion.BindText(2,route.demand); insert_completion.BindText(3,route.net);
            insert_completion.BindInteger(4,ready); insert_completion.BindInteger(5,completion); insert_completion.Execute();
            std::size_t j = 0;
            for (const auto& arrival : values) {
              insert_arrival.BindInteger(1,seq); insert_arrival.BindInteger(2,j++); insert_arrival.BindText(3,arrival.first);
              insert_arrival.BindInteger(4,arrival.second); insert_arrival.Execute();
            }
            relation_counts["completion_arrivals"] += values.size();
            emit(seq,JsonRecord(Object{{"demand",route.demand},{"net",route.net},{"source_ready_slot",std::uint64_t(ready)},
                                     {"completion_slot",std::uint64_t(completion)},{"sink_arrival_slots",JsonRecord(std::move(arrivals))}}));
          }
        } else {
          for (std::uint32_t seq = 0; seq < domains; ++seq) {
            const auto& record = domain_records[seq];
            insert_record.BindText(1,key); insert_record.BindInteger(2,seq); insert_record.BindText(3,record.first);
            insert_record.BindText(4,record.second.compact); insert_record.Execute(); emit_encoded(seq,record.second);
          }
        }
        Hash(logical,"]"); if (count) Hash(pretty,"\n  ]");
        sections.emplace(key, Object{{"records",std::uint64_t(count)},{"sha256",Hex(section_hash)}});
      }
      Hash(pretty, key_index < keys.size() ? ",\n" : "\n}\n");
    }
    Hash(logical,"}"); const auto logical_sha = Hex(logical), pretty_sha = Hex(pretty);
    std::string encoded_header = "{";
    for (const auto& item : header) { if (encoded_header.size() > 1) encoded_header += ','; encoded_header += JsonQuote(item.first) + ':' + item.second.compact; }
    encoded_header += '}';
    put_metadata("logical_schema","\"emuflow.tdm-schedule/v1\""); put_metadata("header",encoded_header);
    put_metadata("logical_sha256",JsonQuote(logical_sha));
    put_metadata("entry_relation_schema","\"emuflow.tdm-schedule-entry-relation/v1\"");
    put_metadata("entry_relation_records",std::to_string(entry_count));
    put_metadata("route_relation_schema","\"emuflow.tdm-schedule-route-relation/v1\"");
    put_metadata("route_relation_records",std::to_string(route_count));
    put_metadata("completion_relation_schema","\"emuflow.tdm-schedule-completion-relation/v1\"");
    put_metadata("completion_relation_records",std::to_string(route_count));
    put_metadata("route_relation_sink_records",std::to_string(relation_counts["route_sinks"]));
    put_metadata("route_relation_edge_records",std::to_string(relation_counts["route_edges"]));
    put_metadata("completion_relation_arrival_records",std::to_string(relation_counts["completion_arrivals"]));
    Object counts; for (const auto& item : relation_counts) counts.emplace(item.first,item.second);
    summary = JsonRecord(Object{{"schema","emuflow.native-schedule-publication/v1"},
       {"implementation_id","native-exact-schedule-store-v1"},{"logical_sha256",logical_sha},
       {"logical_pretty_sha256",pretty_sha},{"sections",JsonRecord(std::move(sections))},{"relation_counts",JsonRecord(std::move(counts))}}).Encode();
    put_metadata("native_publication_summary",summary);
    // SQLite owns uniqueness and lookup indexes; no whole-store integrity pass.
    db.Execute(kScheduleStoreIndexDDL);
    transaction.Commit();
  }
  for (const auto& source : sources) source.RequireUnchanged();
  PublishTemporaryFile(temporary.path,output_path);
}
}  // namespace emuflow::core
