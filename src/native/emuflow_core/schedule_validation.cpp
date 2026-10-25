#include "emuflow_core/schedule_validation.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/route_view.hpp"
#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/store.hpp"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <optional>
#include <queue>
#include <set>
#include <string_view>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool valid,const std::string& message) {
  if(!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Hash(Sha256& hash,std::string_view value) {
  hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());
}
std::string Hex(Sha256& hash) {
  const char* digits="0123456789abcdef";std::string result;
  for(auto c:hash.finish()) {result+=digits[c>>4];result+=digits[c&15];}return result;
}
void Digest(Sha256& hash,const JsonRecord& record) {Hash(hash,record.Encode());Hash(hash,"\n");}
FileGeneration Generation(CheckedRequestReader& reader) {
  FileGeneration result;
  result.device=reader.Read<std::uint64_t>();result.inode=reader.Read<std::uint64_t>();
  result.bytes=reader.Read<std::uint64_t>();result.modified_seconds=reader.Read<std::int64_t>();
  result.modified_nanoseconds=reader.Read<std::int64_t>();result.changed_seconds=reader.Read<std::int64_t>();
  result.changed_nanoseconds=reader.Read<std::int64_t>();return result;
}
std::string Text(SqliteStatement& row,int column) {
  auto value=row.TypedText(column);Check(!value.empty(),"schedule identity is empty");
  (void)JsonQuote(value);return value;
}
std::uint64_t Number(SqliteStatement& row,int column) {
  auto value=row.TypedInteger(column);Check(value>=0,"schedule scalar is negative");return value;
}
std::uint32_t Index(const std::vector<std::string>& catalog,const std::string& value) {
  auto found=std::lower_bound(catalog.begin(),catalog.end(),value);
  Check(found!=catalog.end() && *found==value,"schedule identity is outside the board catalog");
  return static_cast<std::uint32_t>(found-catalog.begin());
}
struct Seal {std::uint64_t count;std::string sha;};
struct Hop {
  const ExactScheduleArc* arc;
  std::uint32_t depth;
  std::uint64_t slot=0,lane=0,ready=0,arrival=0;
  std::optional<std::uint64_t> ratio;
};
struct Route {
  RouteIdentity identity;
  std::uint32_t source=0,max_hop=0;
  std::vector<std::uint32_t> predecessors,successors;
  std::vector<Hop> hops;
  std::vector<std::int64_t> arrivals;
  std::uint64_t ready=0,completion=0;
};

// Hash each canonical typed record only while its ordered cursor is live.
// Header and domain configuration are bounded by the board, never by routes.
class LogicalSeals {
 public:
  LogicalSeals(const std::map<std::string,std::string>& header,
               const std::map<std::string,Seal>& sections)
      : header_(header),sections_(sections) {Hash(compact_,"{");Hash(pretty_,"{\n");}
  void Begin(const std::string& key) {
    Check(!open_,"schedule logical section overlaps");
    while(position_!=header_.end() && position_->first<key) {
      Key(position_->first);Hash(compact_,position_->second);
      VisitPrettyCanonicalJson(position_->second,1,[&](auto v){Hash(pretty_,v);});++position_;
    }
    Check(position_==header_.end() || position_->first!=key,"schedule header duplicates a typed section");
    Key(key);key_=key;count_=0;section_=Sha256{};open_=true;
    Hash(compact_,"[");Hash(pretty_,sections_.at(key).count ? "[\n":"[]");
  }
  void Record(const std::string& value) {
    Check(open_,"schedule logical record is outside a section");
    if(count_) {Hash(compact_,",");Hash(pretty_,",\n");}
    Hash(compact_,value);Hash(section_,value);Hash(section_,"\n");
    Hash(pretty_,"    ");VisitPrettyCanonicalJson(value,2,[&](auto v){Hash(pretty_,v);});++count_;
  }
  void End() {
    Check(open_ && count_==sections_.at(key_).count && Hex(section_)==sections_.at(key_).sha,
          "schedule "+key_+" logical seal disagrees");
    Hash(compact_,"]");if(count_) Hash(pretty_,"\n  ]");open_=false;
  }
  void Finish(const std::string& compact,const std::string& pretty) {
    Check(!open_,"schedule logical section is incomplete");
    while(position_!=header_.end()) {
      Key(position_->first);Hash(compact_,position_->second);
      VisitPrettyCanonicalJson(position_->second,1,[&](auto v){Hash(pretty_,v);});++position_;
    }
    Hash(compact_,"}");Hash(pretty_,"\n}\n");
    Check(Hex(compact_)==compact,"schedule compact logical seal disagrees");
    Check(Hex(pretty_)==pretty,"schedule pretty logical seal disagrees");
  }
 private:
  void Key(const std::string& key) {
    if(keys_++) {Hash(compact_,",");Hash(pretty_,",\n");}
    Hash(compact_,JsonQuote(key)+":");Hash(pretty_,"  "+JsonQuote(key)+": ");
  }
  const std::map<std::string,std::string>& header_;
  const std::map<std::string,Seal>& sections_;
  std::map<std::string,std::string>::const_iterator position_=header_.begin();
  Sha256 compact_,pretty_,section_;
  std::string key_;
  std::uint64_t keys_=0,count_=0;
  bool open_=false;
};

std::vector<Route> ReadRoutes(const ArtifactRef& source,const ExactScheduleBoard& board) {
  RouteView view(source);Check(view.count()<UINT32_MAX && view.count()<=source.generation().bytes,"schedule route population exceeds source bounds");
  const auto& fpgas=board.catalogs.fpgas;const auto& links=board.catalogs.links;
  std::vector<Route> routes;routes.reserve(view.count());
  view.VisitNetOrder([&](const RouteCoordinate&,const RouteIdentity& value) {
    Route route;route.identity=value;route.source=Index(fpgas,value.source);
    Check(!value.sinks.empty(),"schedule route has no sinks");
    std::set<std::string> sinks;
    for(const auto& sink:value.sinks) {
      (void)Index(fpgas,sink);Check(sink!=value.source && sinks.insert(sink).second,"schedule route sinks repeat or include source");
    }
    std::vector<std::vector<const ExactScheduleArc*>> children(fpgas.size());
    for(const auto& edge:value.edges) {
      const auto key=ExactScheduleArcKey{Index(links,edge.link),Index(fpgas,edge.from),Index(fpgas,edge.to)};
      const auto found=board.arcs.find(key);Check(found!=board.arcs.end(),"schedule route uses an unknown directed arc");
      children[found->second.from].push_back(&found->second);
    }
    std::vector<std::int64_t> depth(fpgas.size(),-1);depth[route.source]=0;
    std::vector<std::uint32_t> queue{route.source};
    for(std::size_t q=0;q<queue.size();++q) {
      const auto from=queue[q];
      for(const auto* arc:children[from]) {
        Check(depth[arc->to]<0,"schedule route is not a directed tree");
        depth[arc->to]=depth[from]+1;queue.push_back(arc->to);
        const auto hop=static_cast<std::uint32_t>(depth[from]);
        route.hops.push_back({arc,hop});route.max_hop=std::max(route.max_hop,hop);
      }
    }
    Check(route.hops.size()==value.edges.size(),"schedule route has disconnected edges");
    for(const auto& sink:value.sinks) Check(depth[Index(fpgas,sink)]>=0,"schedule route misses a sink");
    std::sort(route.hops.begin(),route.hops.end(),[](const auto& a,const auto& b) {
      return std::tie(a.depth,a.arc->link,a.arc->from,a.arc->to)<std::tie(b.depth,b.arc->link,b.arc->from,b.arc->to);
    });
    route.arrivals.assign(fpgas.size(),-1);routes.push_back(std::move(route));
  },true);
  for(std::uint32_t child=0;child<routes.size();++child) {
    auto& route=routes[child];std::set<std::string> unique;
    for(const auto& parent:route.identity.predecessors) {
      const auto found=std::lower_bound(routes.begin(),routes.end(),parent,[](const auto& route,const auto& net){return route.identity.net<net;});
      Check(found!=routes.end() && found->identity.net==parent && parent!=route.identity.net && unique.insert(parent).second,
            "schedule predecessor is missing, self-referential or repeated");
      const auto index=static_cast<std::uint32_t>(found-routes.begin());
      route.predecessors.push_back(index);found->successors.push_back(child);
    }
    std::sort(route.predecessors.begin(),route.predecessors.end());
  }
  return routes;
}

std::vector<std::uint32_t> DependencyOrder(const std::vector<Route>& routes) {
  std::vector<std::uint32_t> reverse(routes.size()),remaining(routes.size()),pending(routes.size());
  std::iota(reverse.begin(),reverse.end(),0);
  std::sort(reverse.begin(),reverse.end(),[&](auto a,auto b) {
    return std::tie(routes[a].identity.dependency_level,a)>std::tie(routes[b].identity.dependency_level,b);
  });
  for(auto child:reverse) for(auto parent:routes[child].predecessors) {
    Check(remaining[child]<UINT32_MAX,"schedule dependency depth exceeds native ranks");
    remaining[parent]=std::max(remaining[parent],remaining[child]+1);
  }
  using Priority=std::tuple<std::int64_t,std::int64_t,std::uint32_t>;
  std::priority_queue<Priority,std::vector<Priority>,std::greater<Priority>> ready;
  const auto push=[&](auto route) {ready.emplace(-std::int64_t(remaining[route]),-std::int64_t(routes[route].max_hop),route);};
  for(std::uint32_t i=0;i<routes.size();++i) {
    pending[i]=routes[i].predecessors.size();if(!pending[i]) push(i);
  }
  std::vector<std::uint32_t> result;
  while(!ready.empty()) {
    auto parent=std::get<2>(ready.top());ready.pop();result.push_back(parent);
    for(auto child:routes[parent].successors) {Check(pending[child]>0,"schedule indegree is inconsistent");if(!--pending[child]) push(child);}
  }
  Check(result.size()==routes.size(),"cross-layer timing dependency graph is cyclic");return result;
}
}  // namespace

void ValidateExactSchedule(const std::vector<ArtifactRef>& inputs,const std::string& summary) {
  Check(inputs.size()==4,"schedule validation input coverage disagrees");
  for(const auto& source:inputs) source.RequireUnchanged();
  const auto board=ReadExactScheduleBoard(inputs[0],inputs[1].path());
  const auto& fpgas=board.catalogs.fpgas;const auto& links=board.catalogs.links;const auto& capacities=board.catalogs.capacities;
  CheckedRequestReader request(inputs[3].path(),8*1024*1024);
  Check(request.Read<std::uint64_t>()==0x3156534d44544545ULL && request.Read<std::uint32_t>()==1,"unsupported schedule validation request");
  if(!(Generation(request)==inputs[2].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"schedule validation source generation changed");
  SqliteStore store(inputs[2]);auto& db=store.database();
  db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-32768");
  const auto headers=request.Read<std::uint32_t>();Check(headers<=4096,"schedule header exceeds bound");
  std::map<std::string,std::string> header;
  for(std::uint32_t i=0;i<headers;++i) {
    auto key=request.ReadString32(),value=request.ReadString32();
    auto json=db.Prepare("SELECT json_valid(?1),json(?1)=?1");json.BindText(1,value);
    Check(json.Step() && json.Integer(0) && json.Integer(1) && header.emplace(key,value).second,"schedule header is invalid");
  }
  std::string encoded_header="{";
  for(const auto& item:header) {if(encoded_header.size()>1) encoded_header+=',';encoded_header+=JsonQuote(item.first)+':'+item.second;}encoded_header+='}';
  {
    auto row=db.Prepare("SELECT json(value)=? FROM metadata WHERE key='header'");row.BindText(1,encoded_header);
    Check(row.Step() && row.Integer(0) && !row.Step(),"schedule metadata header disagrees with its bound view");
  }
  const auto domain_count=request.Read<std::uint32_t>();
  Check(domain_count==capacities.size(),"schedule domain catalog coverage disagrees");
  struct Domain {std::string link,direction;std::uint64_t lanes;};std::vector<Domain> domains;
  for(std::uint32_t i=0;i<domain_count;++i) {
    auto value=request.ReadString32();
    auto fields=db.Prepare("SELECT json_extract(?1,'$.key'),json_extract(?1,'$.link'),json_extract(?1,'$.direction'),"
        "json_extract(?1,'$.lanes'),json_extract(?1,'$.frame_slots'),json_extract(?1,'$.capacity_bit_hops'),"
        "(SELECT count(*) FROM json_each(?1))");fields.BindText(1,value);
    Check(fields.Step() && Text(fields,0)==capacities[i] && Number(fields,4)==board.frame &&
          Number(fields,3)>0 && Number(fields,3)<=UINT32_MAX && Number(fields,5)==Number(fields,3)*board.frame &&
          Number(fields,6)==6,"schedule domain template is invalid");
    domains.push_back({Text(fields,1),Text(fields,2),Number(fields,3)});
  }
  std::map<std::string,Seal> sections;
  for(const auto* key:{"demand_completions","domain_schedules","entries","routes"}) {
    auto count=request.Read<std::uint64_t>();auto hash=request.ReadString32(false,64);
    Check(hash.size()==64 && hash.find_first_not_of("0123456789abcdef")==std::string::npos,"schedule section digest is invalid");
    sections.emplace(key,Seal{count,hash});
  }
  auto logical=request.ReadString32(false,64),pretty=request.ReadString32(false,64);request.RequireEnd();
  auto logical_metadata=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='logical_sha256'");
  Check(logical_metadata.Step() && logical_metadata.TypedText(0)==logical,"schedule logical metadata disagrees");
  Check(header.count("metrics") && header.count("schedule_dependency_certificate"),"schedule lacks independent validation targets");
  for(const auto& field:std::map<std::string,std::string>{{"schema","emuflow.tdm-schedule/v1"},
      {"provider","deterministic-dependency-earliest-slot-v1"},{"qualification","dependency-schedule-readiness-pass"},
      {"transport_semantics","sampled-virtual-wire"}})
    Check(header.count(field.first) && header.at(field.first)==JsonQuote(field.second),"schedule header policy is invalid");
  {
    SqliteStore route_store(inputs[0]);
    auto source=route_store.database().Prepare("SELECT json_extract(value,'$.semantic_contract_schema'),"
        "json_extract(value,'$.semantic_contract_sha256'),json_extract(value,'$.constraints') FROM metadata WHERE key='header'");
    Check(source.Step() && header.count("semantic_contract_schema") && header.count("semantic_contract_sha256") &&
          header.at("semantic_contract_schema")==JsonQuote(Text(source,0)) &&
          header.at("semantic_contract_sha256")==JsonQuote(Text(source,1)) && header.count("route_constraints") &&
          header.at("route_constraints")==Text(source,2),"cross-layer timing schedule binding is invalid");
  }
  Check(header.count("timing_constraints"),"schedule timing constraints are missing");
  {
    auto timing=db.Prepare("SELECT json_extract(?1,'$.settle_slots'),json_extract(?1,'$.commit_slot')");
    timing.BindText(1,header.at("timing_constraints"));
    Check(timing.Step() && Number(timing,0)==board.settle && Number(timing,1)==board.frame-board.barrier,
          "schedule timing constraints disagree with board policy");
  }
  std::map<ExactScheduleArcKey,std::uint64_t> bank_widths;
  if(header.count("bank_allocations")) {
    auto banks=db.Prepare("SELECT json_extract(value,'$.link'),json_extract(value,'$.from'),json_extract(value,'$.to'),"
        "json_extract(value,'$.wires_per_bank'),json_extract(value,'$.lane_offset'),json_extract(value,'$.lanes') FROM json_each(?)");
    banks.BindText(1,header.at("bank_allocations"));
    while(banks.Step()) {
      const auto key=ExactScheduleArcKey{Index(links,Text(banks,0)),Index(fpgas,Text(banks,1)),Index(fpgas,Text(banks,2))};
      const auto found=board.arcs.find(key);
      Check(found!=board.arcs.end() && Number(banks,3)>0 && Number(banks,4)==found->second.offset &&
            Number(banks,5)==found->second.budget && bank_widths.emplace(key,Number(banks,3)).second,
            "schedule bank allocation disagrees with the board request");
    }
  }
  auto routes=ReadRoutes(inputs[0],board);
  Check(sections.at("routes").count==routes.size() && sections.at("demand_completions").count==routes.size() &&
        sections.at("domain_schedules").count==capacities.size(),"schedule route/domain coverage disagrees");
  std::vector<std::uint32_t> demand_order(routes.size());std::iota(demand_order.begin(),demand_order.end(),0);
  std::sort(demand_order.begin(),demand_order.end(),[&](auto a,auto b){return routes[a].identity.demand<routes[b].identity.demand;});
  for(std::size_t i=1;i<demand_order.size();++i) Check(routes[demand_order[i-1]].identity.demand<routes[demand_order[i]].identity.demand,"schedule route demands repeat");
  std::uint64_t hop_count=0;for(const auto& route:routes) hop_count+=route.hops.size();
  {
    auto collision=db.Prepare("SELECT 1 FROM schedule_entries GROUP BY capacity_key,slot,lane HAVING count(*)>1 LIMIT 1");
    Check(!collision.Step(),"schedule collision in capacity/slot/lane");
    auto duplicate=db.Prepare("SELECT 1 FROM schedule_entries GROUP BY entry_id HAVING count(*)>1 LIMIT 1");
    Check(!duplicate.Step(),"schedule entry identities repeat");
  }
  Check(hop_count==sections.at("entries").count,"schedule route-hop coverage disagrees");
  std::vector<std::uint64_t> counts(capacities.size());
  std::map<std::pair<std::uint32_t,std::uint64_t>,std::pair<std::uint32_t,std::uint32_t>> bank_directions;
  // Read each typed hop once in route order; retain only native numeric events.
  {
    auto rows=db.Prepare("SELECT demand,net,hop,link_id,source_fpga,sink_fpga,capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio "
        "FROM schedule_entries ORDER BY demand,hop,link_id,source_fpga,sink_fpga");
    for(auto rank:demand_order) for(auto& hop:routes[rank].hops) {
      const auto& route=routes[rank];const auto& arc=*hop.arc;
      Check(rows.Step() && Text(rows,0)==route.identity.demand && Text(rows,1)==route.identity.net &&
            Number(rows,2)==hop.depth && Text(rows,3)==links[arc.link] && Text(rows,4)==fpgas[arc.from] &&
            Text(rows,5)==fpgas[arc.to] && Text(rows,6)==capacities[arc.capacity],"schedule entry has unexpected route hop or net");
      hop.slot=Number(rows,7);hop.lane=Number(rows,8);hop.ready=Number(rows,9);hop.arrival=Number(rows,10);
      if(!rows.IsNull(11)) hop.ratio=Number(rows,11);
      Check(hop.slot<board.frame,"schedule entry slot is out of range");
      Check(hop.lane>=arc.offset && hop.lane<std::uint64_t(arc.offset)+arc.budget,
            "schedule entry wire lies outside its allocated direction banks");
      const auto width=bank_widths.find({arc.link,arc.from,arc.to});
      if(width!=bank_widths.end()) {
        const auto direction=std::make_pair(arc.from,arc.to);
        const auto inserted=bank_directions.emplace(std::make_pair(arc.link,hop.lane/width->second),direction);
        Check(inserted.first->second==direction,"schedule bank is assigned to both directions");
      }
      if(arc.affine) Check(hop.ratio && *hop.ratio>=board.minimum && *hop.ratio<=board.maximum && *hop.ratio%board.quantum==0,
                           "schedule entry tdm_ratio is invalid for the net delay model");
      ++counts[arc.capacity];
    }
    Check(!rows.Step(),"schedule contains unexpected route-hop entries");
  }
  // Baseline wire groups are independently recomputed in capacity identity
  // order, rather than reading the producer's assigned-lane decisions.
  {
    auto rows=db.Prepare("SELECT capacity_key,demand,link_id,source_fpga,sink_fpga,lane,tdm_ratio,slot,ready_slot "
        "FROM schedule_entries ORDER BY capacity_key,demand,link_id,source_fpga,sink_fpga");
    std::string previous;std::uint64_t index=0;
    while(rows.Step()) {
      const auto capacity=Text(rows,0);if(previous!=capacity){previous=capacity;index=0;}
      const auto found=board.arcs.find({Index(links,Text(rows,2)),Index(fpgas,Text(rows,3)),Index(fpgas,Text(rows,4))});
      Check(found!=board.arcs.end(),"schedule contains unknown direction");const auto& arc=found->second;
      if(!arc.affine) continue;
      Check(arc.budget>0,"active schedule direction has no allocated bank");
      const auto lane=index%arc.budget,group=counts[arc.capacity];
      const auto raw=std::max(std::uint64_t(board.minimum),(group-1-lane)/arc.budget+1);
      const auto ratio=raw==1 ? 1:((raw+board.quantum-1)/board.quantum)*board.quantum;
      Check(ratio<=board.maximum && Number(rows,5)==arc.offset+lane && Number(rows,6)==ratio,
            "baseline schedule wire/ratio differs from fixed group");
      const auto slot=Number(rows,7),ready=Number(rows,8);
      Check(slot>=ready && slot-ready<ratio,"baseline schedule wait exceeds its ratio window");++index;
    }
  }
  Sha256 order_hash,readiness_hash,sink_hash;
  std::uint64_t dependencies=0,sink_count=0,maximum_depth=0,maximum_completion=0;
  std::optional<std::uint64_t> minimum_slack;
  for(auto rank:DependencyOrder(routes)) {
    auto& route=routes[rank];route.ready=board.settle;Array evidence;
    for(auto parent:route.predecessors) {
      const auto arrival=routes[parent].arrivals[route.source];
      Check(arrival>=0,"indexed exact predecessor has not arrived at source");
      route.ready=std::max(route.ready,std::uint64_t(arrival)+board.settle);
      evidence.emplace_back(Array{routes[parent].identity.net,JsonRecord(arrival)});
    }
    dependencies+=route.predecessors.size();Digest(order_hash,JsonRecord(route.identity.net));
    Digest(readiness_hash,JsonRecord(Object{{"net",route.identity.net},{"source",route.identity.source},
        {"source_ready_slot",route.ready},{"predecessor_arrivals",JsonRecord(std::move(evidence))}}));
    route.arrivals[route.source]=static_cast<std::int64_t>(route.ready)-1;
    for(const auto& hop:route.hops) {
      const auto& arc=*hop.arc;
      Check(route.arrivals[arc.from]>=0,"schedule tree predecessor has not arrived");
      const auto ready=arc.from==route.source ? route.ready:std::uint64_t(route.arrivals[arc.from])+1;
      std::uint64_t latency=arc.fixed_latency;
      if(arc.affine) {
        volatile double scaled=arc.slope*(*hop.ratio-1);volatile double delay=arc.base+scaled;
        const auto cycles=std::ceil(delay/arc.period);
        Check(std::isfinite(cycles) && cycles>=0 && cycles<=UINT32_MAX,"schedule affine latency is invalid");latency=static_cast<std::uint64_t>(cycles);
      }
      Check(hop.ready==ready && hop.slot>=ready,"schedule ready-slot mismatch or precedence violation");
      Check(hop.arrival==hop.slot+latency,"schedule entry arrival_slot does not match link delay");
      Check(hop.arrival<board.frame-board.barrier,"schedule entry arrival reaches reserved runtime barrier slot");
      route.arrivals[arc.to]=hop.arrival;
    }
    auto sinks=route.identity.sinks;std::sort(sinks.begin(),sinks.end());
    for(const auto& sink:sinks) {
      const auto arrival=route.arrivals[Index(fpgas,sink)];Check(arrival>=0,"schedule misses routed sink arrival");
      const auto ready=std::uint64_t(arrival)+board.settle,commit=std::uint64_t(board.frame-board.barrier);
      Check(ready<=commit,"indexed exact sink misses the commit deadline");const auto slack=commit-ready;
      minimum_slack=minimum_slack ? std::min(*minimum_slack,slack):slack;++sink_count;
      Digest(sink_hash,JsonRecord(Object{{"net",route.identity.net},{"fpga",sink},{"arrival_slot",arrival},
          {"budget_slots",board.settle},{"ready_slot",ready},{"commit_slot",commit},{"slack_slots",slack}}));
      route.completion=std::max(route.completion,std::uint64_t(arrival));
    }
    // Only routed sinks may supply predecessor readiness to another route.
    for(std::size_t i=0;i<fpgas.size();++i) if(!std::binary_search(sinks.begin(),sinks.end(),fpgas[i])) route.arrivals[i]=-1;
    maximum_completion=std::max(maximum_completion,route.completion);
    maximum_depth=std::max(maximum_depth,std::uint64_t(*route.identity.dependency_depth));
  }
  const JsonRecord slack=minimum_slack ? JsonRecord(*minimum_slack):JsonRecord();
  const auto certificate=JsonRecord(Object{{"schema","emuflow.sampled-virtual-wire-schedule-certificate/v2"},
      {"provider","indexed-conservative-readiness-certificate-v2"},{"launch_readiness_policy","one-settle-slot-conservative-v1"},
      {"capture_coverage","all-routed-sinks-conservative-v1"},{"topological_cut_count",routes.size()},
      {"topological_cut_order_sha256",Hex(order_hash)},{"dependency_edges",dependencies},{"readiness_records",routes.size()},
      {"readiness_sha256",Hex(readiness_hash)},{"sink_readiness_checks",sink_count},{"sink_readiness_sha256",Hex(sink_hash)},
      {"minimum_sink_slack_slots",slack}}).Encode();
  Check(header.at("schedule_dependency_certificate")==certificate,"cross-layer timing readiness certificate does not match independent reconstruction");
  double maximum_utilization=0;
  std::vector<std::string> domain_records;
  for(std::size_t i=0;i<domains.size();++i) {
    const auto capacity=domains[i].lanes*board.frame;const auto utilization=static_cast<double>(counts[i])/static_cast<double>(capacity);
    maximum_utilization=std::max(maximum_utilization,utilization);
    domain_records.push_back(JsonRecord(Object{{"key",capacities[i]},{"link",domains[i].link},{"direction",domains[i].direction},
        {"lanes",domains[i].lanes},{"frame_slots",board.frame},{"scheduled_bit_hops",counts[i]},
        {"capacity_bit_hops",capacity},{"utilization",utilization}}).Encode());
  }
  Object metrics{{"demands",routes.size()},{"scheduled_bit_hops",hop_count},{"frame_slots",board.frame},
      {"completion_slot",maximum_completion},{"max_domain_utilization",maximum_utilization},{"collisions",0},
      {"transport_ordering_policy","path-local-dependency-dag-v1"},{"commit_slot",board.frame-board.barrier},
      {"dependency_edges",dependencies},{"maximum_combinational_dependency_depth",maximum_depth},
      {"conservative_sink_readiness_checks",sink_count},{"minimum_capture_slack_slots",slack}};
  Check(header.at("metrics")==JsonRecord(metrics).Encode(),"schedule.metrics does not match independently recomputed metrics");
  LogicalSeals seals(header,sections);
  // Completion and route children are merged in primary-key order; neither
  // per-route queries nor a second validation database are constructed.
  seals.Begin("demand_completions");
  {
    auto rows=db.Prepare("SELECT seq,demand,net,source_ready_slot,completion_slot FROM schedule_completions ORDER BY seq");
    auto children=db.Prepare("SELECT completion_seq,seq,sink_fpga,arrival_slot FROM schedule_completion_arrivals ORDER BY completion_seq,seq");
    bool child=children.Step();std::uint64_t seq=0;
    for(auto rank:demand_order) {
      const auto& route=routes[rank];
      Check(rows.Step() && Number(rows,0)==seq && Text(rows,1)==route.identity.demand && Text(rows,2)==route.identity.net &&
            Number(rows,3)==route.ready && Number(rows,4)==route.completion,"schedule.demand_completions record does not match recomputed value");
      auto sinks=route.identity.sinks;std::sort(sinks.begin(),sinks.end());Object arrivals;
      for(std::size_t j=0;j<sinks.size();++j) {
        Check(child && Number(children,0)==seq && Number(children,1)==j && Text(children,2)==sinks[j] &&
              Number(children,3)==std::uint64_t(route.arrivals[Index(fpgas,sinks[j])]),"schedule completion sink arrival does not match recomputed value");
        arrivals.emplace(sinks[j],Number(children,3));child=children.Step();
      }
      seals.Record(JsonRecord(Object{{"demand",route.identity.demand},{"net",route.identity.net},{"source_ready_slot",route.ready},
          {"completion_slot",route.completion},{"sink_arrival_slots",JsonRecord(std::move(arrivals))}}).Encode());++seq;
    }
    Check(!rows.Step() && !child,"schedule completion/arrival coverage disagrees");
  }seals.End();
  seals.Begin("domain_schedules");
  {
    auto rows=db.Prepare("SELECT seq,identity,record_json FROM records WHERE section='domain_schedules' ORDER BY seq");
    for(std::size_t i=0;i<domain_records.size();++i) {
      Check(rows.Step() && Number(rows,0)==i && Text(rows,1)==capacities[i] && Text(rows,2)==domain_records[i],"schedule.domain_schedules does not match recomputed value");
      seals.Record(domain_records[i]);
    }Check(!rows.Step(),"schedule domain coverage disagrees");
    auto unknown=db.Prepare("SELECT 1 FROM records WHERE section!='domain_schedules' LIMIT 1");
    Check(!unknown.Step(),"schedule duplicates typed records or contains an unknown section");
  }seals.End();
  seals.Begin("entries");
  {
    auto rows=db.Prepare("SELECT seq,entry_id,demand,net,hop,link_id,source_fpga,sink_fpga,capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio FROM schedule_entries ORDER BY seq");
    std::uint64_t seq=0;
    while(rows.Step()) {
      Check(Number(rows,0)==seq++,"schedule entries typed sequence is not contiguous");
      Object record{{"id",Text(rows,1)},{"demand",Text(rows,2)},{"net",Text(rows,3)},{"hop",Number(rows,4)},
          {"link",Text(rows,5)},{"from",Text(rows,6)},{"to",Text(rows,7)},{"capacity_key",Text(rows,8)},
          {"slot",Number(rows,9)},{"lane",Number(rows,10)},{"ready_slot",Number(rows,11)},{"arrival_slot",Number(rows,12)}};
      if(!rows.IsNull(13)) record.emplace("tdm_ratio",Number(rows,13));seals.Record(JsonRecord(std::move(record)).Encode());
    }
  }seals.End();
  seals.Begin("routes");
  {
    auto rows=db.Prepare("SELECT seq,demand,net,source_fpga FROM schedule_routes ORDER BY seq");
    auto sinks=db.Prepare("SELECT route_seq,seq,sink_fpga FROM schedule_route_sinks ORDER BY route_seq,seq");
    auto edges=db.Prepare("SELECT route_seq,seq,link_id,source_fpga,sink_fpga FROM schedule_route_edges ORDER BY route_seq,seq");
    bool have_sink=sinks.Step(),have_edge=edges.Step();std::uint64_t seq=0;
    for(auto rank:demand_order) {
      const auto& route=routes[rank].identity;
      Check(rows.Step() && Number(rows,0)==seq && Text(rows,1)==route.demand && Text(rows,2)==route.net && Text(rows,3)==route.source,
            "schedule.routes identity does not match upstream route");Array sink_records,edge_records;
      for(std::size_t j=0;j<route.sinks.size();++j) {
        Check(have_sink && Number(sinks,0)==seq && Number(sinks,1)==j && Text(sinks,2)==route.sinks[j],"schedule.routes sink does not match upstream route");
        sink_records.emplace_back(route.sinks[j]);have_sink=sinks.Step();
      }
      for(std::size_t j=0;j<route.edges.size();++j) {
        const auto& edge=route.edges[j];
        Check(have_edge && Number(edges,0)==seq && Number(edges,1)==j && Text(edges,2)==edge.link && Text(edges,3)==edge.from && Text(edges,4)==edge.to,
              "schedule.routes tree edge does not match upstream route");
        edge_records.emplace_back(Object{{"link",edge.link},{"from",edge.from},{"to",edge.to}});have_edge=edges.Step();
      }
      seals.Record(JsonRecord(Object{{"id",route.demand},{"net",route.net},{"source",route.source},
          {"sinks",JsonRecord(std::move(sink_records))},{"tree_edges",JsonRecord(std::move(edge_records))}}).Encode());++seq;
    }
    Check(!rows.Step() && !have_sink && !have_edge,"schedule route/child coverage disagrees");
  }seals.End();seals.Finish(logical,pretty);
  metrics.emplace("schema","emuflow.exact-schedule-validation/v1");metrics.emplace("status","pass");
  metrics.emplace("qualification","dependency-schedule-readiness-pass");
  metrics.emplace("implementation_id","native-independent-exact-schedule-validation-v1");metrics.emplace("routed_sinks",sink_count);
  PublishTextArtifact(summary,inputs,[&](std::ostream& out){out<<JsonRecord(metrics).Encode()<<'\n';});
}
}  // namespace emuflow::core
