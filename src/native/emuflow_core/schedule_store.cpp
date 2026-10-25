#include "emuflow_core/store.hpp"
#include "emuflow_core/schedule_store.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/schedule_store_schema.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include "emuflow_core/schedule_model.hpp"
#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/native_stages.hpp"

#include <algorithm>
#include <array>
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
struct Encoded { std::string compact, pretty; };
struct Route : ExactScheduleRoute {
  std::string demand, net;
  std::uint32_t rank = 0, dependency_depth = 0;
  std::vector<std::uint32_t> edge_order;
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
Encoded Encode(const JsonRecord& value) { return {value.Encode(),value.Encode(true)}; }
void DeriveScheduleHeader(SqliteDatabase& db, std::map<std::string,Encoded>& header,
    std::vector<std::pair<std::string,Encoded>>& domains, const std::vector<Route>& routes,
    const ExactScheduleSolution& solution, const std::vector<std::string>& fpga,
    const std::vector<std::string>& capacities, std::uint32_t frame, std::uint32_t barrier,
    std::uint32_t settle, const std::vector<std::size_t>& completion_indices) {
  Require(!header.count("metrics") && !header.count("schedule_dependency_certificate"),
          "native generation header contains producer-owned results");
  const auto policy = header.find("timing_constraints");
  Require(policy != header.end(),"native generation timing policy is missing");
  auto timing = db.Prepare("SELECT json_extract(?,'$.settle_slots'),json_extract(?,'$.commit_slot')");
  timing.BindText(1,policy->second.compact); timing.BindText(2,policy->second.compact);
  Require(timing.Step() && timing.Integer(0) == settle && timing.Integer(1) == frame - barrier,
          "native generation timing policy disagrees");
  std::vector<std::uint64_t> domain_counts(capacities.size(),0);
  for (const auto& entry : solution.entries) {
    Require(entry.route < routes.size() && entry.hop < routes[entry.route].hops.size(),
            "native generation entry is invalid");
    const auto capacity = routes[entry.route].hops[entry.hop].capacity;
    Require(capacity < capacities.size(),"native generation capacity is invalid");
    ++domain_counts[capacity];
  }
  Require(domains.size() == capacities.size(),"native generation domain coverage disagrees");
  double maximum_utilization = 0.0;
  for (std::size_t i = 0; i < domains.size(); ++i) {
    const auto& value = domains[i].second.compact;
    auto fields = db.Prepare("SELECT json_extract(?1,'$.key'),json_extract(?1,'$.link'),"
        "json_extract(?1,'$.direction'),json_extract(?1,'$.lanes'),json_extract(?1,'$.frame_slots'),"
        "json_extract(?1,'$.capacity_bit_hops'),json_type(?1,'$.lanes'),(SELECT count(*) FROM json_each(?1))");
    fields.BindText(1,value);
    Require(fields.Step() && domains[i].first == capacities[i] && fields.Text(0) == capacities[i] &&
            fields.Text(6) == "integer" && fields.Integer(3) > 0 && fields.Integer(3) <= UINT32_MAX &&
            std::uint64_t(fields.Integer(3)) <= std::uint64_t(INT64_MAX) / frame &&
            fields.Integer(4) == frame && fields.Integer(5) == fields.Integer(3) * std::int64_t(frame) &&
            fields.Integer(7) == 6,"native generation domain template disagrees");
    const auto lanes = static_cast<std::uint64_t>(fields.Integer(3)), capacity = lanes * frame;
    const double utilization = static_cast<double>(domain_counts[i]) / static_cast<double>(capacity);
    maximum_utilization = std::max(maximum_utilization,utilization);
    domains[i].second = Encode(JsonRecord(Object{{"key",capacities[i]},{"link",fields.Text(1)},
       {"direction",fields.Text(2)},{"lanes",lanes},{"frame_slots",frame},
       {"scheduled_bit_hops",domain_counts[i]},{"capacity_bit_hops",capacity},{"utilization",utilization}}));
  }
  Sha256 order_hash, readiness_hash, sink_hash;
  std::uint64_t dependencies = 0, sink_count = 0, maximum_depth = 0, maximum_completion = 0;
  std::optional<std::uint64_t> minimum_slack;
  const auto digest = [&](Sha256& hash, const JsonRecord& record) { Hash(hash,record.Encode() + "\n"); };
  Require(solution.order.size() == routes.size(),"native generation order coverage disagrees");
  std::vector<bool> seen(routes.size(),false);
  for (const auto index : solution.order) {
    Require(index < routes.size() && !seen[index],"native generation order repeats or is invalid");
    const auto& route = routes[index]; const auto& completion = solution.completions[completion_indices[index]];
    digest(order_hash,JsonRecord(route.net));
    Array evidence;
    std::vector<std::uint32_t> predecessors = route.predecessors;
    std::sort(predecessors.begin(),predecessors.end(),[&](auto a, auto b) { return routes.at(a).net < routes.at(b).net; });
    std::uint64_t source_ready = settle;
    for (const auto predecessor : predecessors) {
      Require(predecessor < routes.size() && seen[predecessor],"native generation predecessor order is invalid");
      const auto& previous = solution.completions[completion_indices[predecessor]];
      const auto arrival = std::find_if(previous.sink_arrivals.begin(),previous.sink_arrivals.end(),
          [&](const auto& item) { return item.first == route.source; });
      Require(arrival != previous.sink_arrivals.end(),"native generation predecessor arrival is missing");
      source_ready = std::max(source_ready,std::uint64_t(arrival->second) + settle);
      evidence.emplace_back(Array{JsonRecord(routes[predecessor].net),JsonRecord(arrival->second)});
    }
    Require(source_ready == completion.ready,"native generation source readiness disagrees");
    dependencies += evidence.size();
    digest(readiness_hash,JsonRecord(Object{{"net",route.net},{"source",fpga[route.source]},
        {"source_ready_slot",source_ready},{"predecessor_arrivals",JsonRecord(std::move(evidence))}}));
    auto sinks = route.sinks;
    std::sort(sinks.begin(),sinks.end(),[&](auto a, auto b) { return fpga.at(a) < fpga.at(b); });
    for (const auto sink : sinks) {
      const auto arrival = std::find_if(completion.sink_arrivals.begin(),completion.sink_arrivals.end(),
          [&](const auto& item) { return item.first == sink; });
      Require(arrival != completion.sink_arrivals.end(),"native generation sink arrival is missing");
      const std::uint64_t ready = std::uint64_t(arrival->second) + settle, commit = frame - barrier;
      Require(ready <= commit,"native generation sink misses commit deadline");
      const auto slack = commit - ready;
      minimum_slack = minimum_slack ? std::min(*minimum_slack,slack) : slack;
      digest(sink_hash,JsonRecord(Object{{"net",route.net},{"fpga",fpga[sink]},
         {"arrival_slot",arrival->second},{"budget_slots",settle},{"ready_slot",ready},
         {"commit_slot",commit},{"slack_slots",slack}}));
      ++sink_count;
    }
    maximum_depth = std::max(maximum_depth,std::uint64_t(route.dependency_depth));
    maximum_completion = std::max(maximum_completion,std::uint64_t(completion.completion));
    seen[index] = true;
  }
  const JsonRecord slack = minimum_slack ? JsonRecord(*minimum_slack) : JsonRecord();
  header.emplace("schedule_dependency_certificate",Encode(JsonRecord(Object{
    {"schema","emuflow.sampled-virtual-wire-schedule-certificate/v2"},
    {"provider","indexed-conservative-readiness-certificate-v2"},
    {"launch_readiness_policy","one-settle-slot-conservative-v1"},
    {"capture_coverage","all-routed-sinks-conservative-v1"},
    {"topological_cut_count",routes.size()},{"topological_cut_order_sha256",Hex(order_hash)},
    {"dependency_edges",dependencies},{"readiness_records",routes.size()},
    {"readiness_sha256",Hex(readiness_hash)},{"sink_readiness_checks",sink_count},
    {"sink_readiness_sha256",Hex(sink_hash)},{"minimum_sink_slack_slots",slack}})));
  header.emplace("metrics",Encode(JsonRecord(Object{{"demands",routes.size()},
    {"scheduled_bit_hops",solution.entries.size()},{"frame_slots",frame},{"completion_slot",maximum_completion},
    {"max_domain_utilization",maximum_utilization},{"collisions",0},{"transport_ordering_policy","path-local-dependency-dag-v1"},
    {"commit_slot",frame - barrier},{"dependency_edges",dependencies},{"maximum_combinational_dependency_depth",maximum_depth},
    {"conservative_sink_readiness_checks",sink_count},{"minimum_capture_slack_slots",slack}})));
}
void PublishScheduleData(const std::vector<Route>& routes, const ExactScheduleSolution& solution,
    const std::vector<std::string>& fpga, const std::vector<std::string>& links,
    const std::vector<std::string>& capacities, BinaryReader& request, const std::string& output_path,
    const std::vector<ArtifactRef>& sources, std::uint32_t frame, std::uint32_t barrier,
    std::uint32_t settle, bool uses_word_links) {
  const auto route_count = routes.size(), fpga_count = fpga.size();
  const auto entry_count = solution.entries.size();
  Require(solution.completions.size() == route_count,"schedule publication completion coverage disagrees");
  std::vector<std::uint32_t> demand_order(route_count,UINT32_MAX);
  std::uint64_t hops = 0;
  for (std::uint32_t i = 0; i < route_count; ++i) {
    Require(routes[i].rank < route_count && demand_order[routes[i].rank] == UINT32_MAX,
            "schedule publication route rank is invalid");
    demand_order[routes[i].rank] = i; hops += routes[i].hops.size();
  }
  Require(hops == entry_count,"schedule publication entry coverage disagrees");
  std::vector<std::size_t> completion_indices(route_count,SIZE_MAX);
  for (std::size_t i = 0; i < solution.completions.size(); ++i) {
    const auto& completion = solution.completions[i];
    Require(completion.route < route_count && completion_indices[completion.route] == SIZE_MAX &&
            completion.sink_arrivals.size() == routes[completion.route].sinks.size(),
            "schedule publication completion identity is invalid");
    completion_indices[completion.route] = i;
  }
  SqliteArtifactWriter writer(output_path,"tdm-schedule-store-v4",sources);
  std::string summary;
  {
    auto& db=writer.database();
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
    Require(!header.count("word_event_policy"), "schedule word policy is producer owned");
    if (uses_word_links)
      header.emplace("word_event_policy", Encode(JsonRecord("periodic-lvds-8bit-word-load-to-rx-visible-v2")));
    const auto domains = request.Read<std::uint32_t>();
    Require(domains <= request.remaining() / 15, "schedule publication domain records are truncated");
    std::vector<std::pair<std::string, Encoded>> domain_records;
    for (std::uint32_t i = 0; i < domains; ++i) {
      auto identity = request.ReadString32(); auto encoded = ReadEncoded(request, db);
      domain_records.emplace_back(std::move(identity), std::move(encoded));
    }
    request.RequireEnd();
    DeriveScheduleHeader(db,header,domain_records,routes,solution,fpga,capacities,
                                     frame,barrier,settle,completion_indices);
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
          std::vector<bool> sequences(entry_count, false);
          std::vector<std::vector<bool>> seen_hops;
          for (const auto& route : routes) seen_hops.emplace_back(route.hops.size(), false);
          for (std::uint64_t seq = 0; seq < entry_count; ++seq) {
            const auto& native = solution.entries[seq];
            Require(native.route < route_count && native.hop < routes[native.route].hops.size(),
                    "schedule publication typed entry identity is invalid");
            const auto& model_hop = routes[native.route].hops[native.hop];
            const std::array<std::uint32_t,12> values{native.route,native.sequence,model_hop.depth,
                model_hop.capacity,model_hop.link,model_hop.from,model_hop.to,native.slot,native.lane,
                native.ready,native.arrival,model_hop.assigned_lane == UINT32_MAX ? 0u : model_hop.ratio};
            const auto route_index = values[0], sequence = values[1];
            Require(route_index < route_count && sequence < entry_count && !sequences[sequence],
                    "schedule publication entry identity is invalid");
            sequences[sequence] = true;
            const auto& route = routes[route_index];
            const auto selected = native.hop;
            Require(selected < route.hops.size() && !seen_hops[route_index][selected],
                    "schedule publication entry disagrees with its route");
            seen_hops[route_index][selected] = true;
            const auto& hop = route.hops[selected];
            const auto slot = values[7], lane = values[8], ready = values[9], arrival = values[10], ratio = values[11];
            Require(slot >= ready && slot < frame - barrier && std::uint64_t(slot) + hop.latency == arrival &&
                    lane >= hop.lane_offset && std::uint64_t(lane) < std::uint64_t(hop.lane_offset) + hop.lane_budget &&
                    (hop.assigned_lane == UINT32_MAX ? ratio == 0 : ratio == hop.ratio && lane == hop.assigned_lane),
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
            const auto& native = solution.completions[completion_indices[route_index]];
            const auto ready = native.ready, completion = native.completion;
            const auto sinks = native.sink_arrivals.size();
            Object arrivals; std::map<std::string,std::uint32_t> values;
            std::set<std::uint32_t> seen;
            const std::set<std::uint32_t> wanted(route.sinks.begin(),route.sinks.end());
            Require(wanted.size() == sinks, "schedule publication route sinks repeat");
            std::uint32_t maximum = 0;
            for (std::uint32_t j = 0; j < sinks; ++j) {
              const auto sink = native.sink_arrivals[j].first, arrival = native.sink_arrivals[j].second;
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
  (void)writer.Commit();
}
}  // namespace

void BuildExactScheduleStore(const std::vector<ArtifactRef>& sources, const std::string& output_path) {
  const auto& route_path=sources.at(0).path();
  const auto& board_path=sources.at(1).path();
  const auto& generation_path=sources.at(2).path();
  BinaryReader request(generation_path);
  Require(request.size() <= 8 * 1024 * 1024,"schedule generation request exceeds bound");
  ReadAndValidateHeader(request,{0x3147534d44544545ULL,1,"exact schedule generation request"});
  FileGeneration expected;
  expected.device = request.Read<std::uint64_t>(); expected.inode = request.Read<std::uint64_t>();
  expected.bytes = request.Read<std::uint64_t>(); expected.modified_seconds = request.Read<std::int64_t>();
  expected.modified_nanoseconds = request.Read<std::int64_t>(); expected.changed_seconds = request.Read<std::int64_t>();
  expected.changed_nanoseconds = request.Read<std::int64_t>();
  if (!(expected == sources[0].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"native ScheduleStore generation source changed");
  ExactScheduleBuildContext context;
  auto model = BuildExactScheduleModel(route_path,board_path,context);
  const auto& catalogs = context.catalogs;
  const auto solution = native::ComputeStaticExactSchedule(model);
  std::vector<Route> routes(model.routes.size());
  std::vector<std::uint32_t> demand_order(routes.size(),UINT32_MAX);
  for (std::uint32_t i = 0; i < routes.size(); ++i) {
    static_cast<ExactScheduleRoute&>(routes[i]) = std::move(model.routes[i]);
    routes[i].demand = std::move(context.identities[i].demand);
    routes[i].net = std::move(context.identities[i].net);
    routes[i].dependency_depth = context.identities[i].dependency_depth;
    routes[i].edge_order = std::move(context.identities[i].original_hop_order);
    routes[i].rank = routes[i].demand_rank;
    Require(routes[i].rank < routes.size() && demand_order[routes[i].rank] == UINT32_MAX,
            "native schedule demand rank is invalid");
    demand_order[routes[i].rank] = i;
  }
  model.routes.clear(); model.routes.shrink_to_fit();
  context.identities.clear(); context.identities.shrink_to_fit();
  PublishScheduleData(routes,solution,catalogs.fpgas,catalogs.links,catalogs.capacities,request,
                      output_path,sources,model.frame_slots,model.barrier_slots,model.settle_slots,context.uses_word_links);
}
}  // namespace emuflow::core
