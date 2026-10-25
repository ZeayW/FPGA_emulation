#include "emuflow_core/schedule_timing.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/route_view.hpp"
#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/store.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <memory>
#include <numeric>
#include <optional>
#include <set>
#include <string_view>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool value,const char* message) {
  if(!value) throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Hash(Sha256& hash,std::string_view value) {
  hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());
}
std::string Hex(Sha256& hash) {
  const char* digits="0123456789abcdef";std::string result;
  for(auto c:hash.finish()){result+=digits[c>>4];result+=digits[c&15];}return result;
}
std::string Text(SqliteStatement& row,int column) {
  auto value=row.TypedText(column);Check(!value.empty(),"timing identity is empty");(void)JsonQuote(value);return value;
}
std::uint64_t Number(SqliteStatement& row,int column) {
  auto value=row.TypedInteger(column);Check(value>=0,"timing scalar is negative");return value;
}
double Time(SqliteStatement& row,int column,int type_column,bool positive) {
  const auto type=row.TypedText(type_column);
  Check(type=="integer" || type=="real","timing value is not a number");const auto value=row.Real(column);
  Check(std::isfinite(value) && (positive ? value>0:value>=0),"timing value is not finite or has invalid sign");return value;
}
void Generation(CheckedRequestReader& reader,const ArtifactRef& source) {
  FileGeneration value;
  value.device=reader.Read<std::uint64_t>();value.inode=reader.Read<std::uint64_t>();value.bytes=reader.Read<std::uint64_t>();
  value.modified_seconds=reader.Read<std::int64_t>();value.modified_nanoseconds=reader.Read<std::int64_t>();
  value.changed_seconds=reader.Read<std::int64_t>();value.changed_nanoseconds=reader.Read<std::int64_t>();
  if(!(value==source.generation())) throw StageError(ErrorCode::kMixedGeneration,"timing projection source generation changed");
}
std::string Digest(CheckedRequestReader& reader) {
  auto value=reader.ReadString32(false,64);
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,"timing source digest is invalid");return value;
}
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"timing source metadata is missing");auto result=row.TypedText(0);Check(!row.Step(),"timing source metadata repeats");return result;
}
struct Event {std::string id;std::uint64_t slot=0,ready=0;std::optional<std::uint64_t> ratio;const ExactScheduleArc* arc=nullptr;};
struct Model {
  RouteIdentity route;
  std::vector<Event> events;
  std::vector<std::int64_t> parents;
  std::string longest;
};
std::uint32_t Index(const std::vector<std::string>& names,const std::string& value) {
  auto found=std::lower_bound(names.begin(),names.end(),value);
  Check(found!=names.end() && *found==value,"timing route uses unknown board identity");return found-names.begin();
}

// Numeric order statistics spill into invocation-owned SQLite TEMP storage.
// Only source-scoped integer IDs/ranks and numbers are spooled; path names
// remain in the RouteStore. Binary values preserve signed zero and precision through
// SQLite sorting. Accumulate explicitly in the reference's sorted path order.
class Statistics {
 public:
  explicit Statistics(SqliteDatabase& db):db_(db) {
    db_.Execute("CREATE TEMP TABLE path_statistics(rank INTEGER PRIMARY KEY,source_seq INTEGER NOT NULL,normalized REAL NOT NULL,"
        "normalized_bits BLOB NOT NULL,slack_bits BLOB NOT NULL,delay_bits BLOB NOT NULL) WITHOUT ROWID");
    insert_=db_.Prepare("INSERT INTO path_statistics VALUES(?,?,?,?,?,?)");
    transaction_=std::make_unique<SqliteTransaction>(db_);
  }
  void Add(std::uint64_t source_seq,double normalized,double slack,double delay) {
    Check(std::isfinite(normalized) && std::isfinite(slack) && std::isfinite(delay),"reconstructed timing is not finite");
    insert_.BindInteger(1,count_);insert_.BindInteger(2,source_seq);insert_.BindReal(3,normalized);insert_.BindBlob(4,&normalized,8);
    insert_.BindBlob(5,&slack,8);insert_.BindBlob(6,&delay,8);insert_.Execute();++count_;
  }
  JsonRecord Finish(const std::string& model) {
    Check(count_>0,"schedule timing reconstruction has no timing paths");
    auto rows=db_.Prepare("SELECT source_seq,normalized_bits,slack_bits,delay_bits FROM path_statistics ORDER BY normalized,rank");
    Object result{{"status","pass"},{"timing_model",model},{"timing_paths",count_}};
    double tns=0,tns_normalized=0,p01=0,median=0;std::uint64_t negative=0,index=0;
    const auto number=[&](int column) {
      const auto bits=rows.Blob(column);Check(bits.size()==8,"timing statistic binary value is invalid");
      double value;std::memcpy(&value,bits.data(),8);return value;
    };
    while(rows.Step()) {
      const auto normalized=number(1),slack=number(2),delay=number(3);
      if(!index) {
        auto identity=db_.Prepare("SELECT path_id FROM timing_paths WHERE seq=?");identity.BindInteger(1,rows.TypedInteger(0));
        Check(identity.Step(),"timing worst-path source identity is missing");
        result.emplace("worst_path",Text(identity,0));result.emplace("worst_delay_ns",delay);
        result.emplace("worst_slack_ns",slack);result.emplace("worst_normalized_slack",normalized);}
      if(index==count_/100)p01=normalized;if(index==count_/2)median=normalized;
      if(slack<0)++negative;tns+=std::min(0.0,slack);tns_normalized+=std::min(0.0,normalized);++index;
    }
    Check(index==count_,"timing statistic coverage disagrees");
    result.emplace("negative_slack_paths",negative);result.emplace("total_negative_slack_ns",tns);
    result.emplace("total_negative_normalized_slack",tns_normalized);result.emplace("p01_normalized_slack",p01);
    result.emplace("median_normalized_slack",median);transaction_->Commit();return JsonRecord(std::move(result));
  }
 private:
  SqliteDatabase& db_;SqliteStatement insert_;std::unique_ptr<SqliteTransaction> transaction_;std::uint64_t count_=0;
};

void InstallEvents(std::vector<Model>& models,SqliteDatabase& db,const ExactScheduleBoard* board) {
  std::vector<std::uint32_t> order(models.size());std::iota(order.begin(),order.end(),0);
  std::sort(order.begin(),order.end(),[&](auto a,auto b){return models[a].route.demand<models[b].route.demand;});
  for(std::size_t i=1;i<order.size();++i) Check(models[order[i-1]].route.demand<models[order[i]].route.demand,"timing demand identities repeat");
  auto rows=db.Prepare("SELECT entry_id,demand,net,link_id,source_fpga,sink_fpga,slot,ready_slot,tdm_ratio FROM schedule_entries ORDER BY demand,link_id,source_fpga,sink_fpga");
  for(auto rank:order) {
    auto& model=models[rank];const auto& route=model.route;
    model.events.resize(route.edges.size());std::vector<std::uint32_t> edges(route.edges.size());std::iota(edges.begin(),edges.end(),0);
    const auto key=[&](auto i){const auto& e=route.edges[i];return std::tie(e.link,e.from,e.to);};
    std::sort(edges.begin(),edges.end(),[&](auto a,auto b){return key(a)<key(b);});
    for(std::size_t i=0;i<edges.size();++i) {
      auto index=edges[i];const auto& edge=route.edges[index];
      Check(!i || key(edges[i-1])<key(index),"cross-layer route repeats a hop");
      Check(rows.Step() && Text(rows,1)==route.demand && Text(rows,2)==route.net && Text(rows,3)==edge.link &&
            Text(rows,4)==edge.from && Text(rows,5)==edge.to,"cross-layer schedule does not exactly cover routed hops");
      auto& event=model.events[index];event.id=Text(rows,0);event.slot=Number(rows,6);event.ready=Number(rows,7);
      if(!rows.IsNull(8))event.ratio=Number(rows,8);
      if(board) {
        const auto arc=board->arcs.find({Index(board->catalogs.links,edge.link),Index(board->catalogs.fpgas,edge.from),Index(board->catalogs.fpgas,edge.to)});
        Check(arc!=board->arcs.end(),"timing route uses an illegal directed arc");event.arc=&arc->second;
        Check(event.slot>=event.ready,"schedule timing reconstruction found a negative wait");
        Check(!event.arc->affine || (event.ratio && *event.ratio>0),"ratio-delay schedule entry requires a positive tdm_ratio");
      }
    }
    if(!board)continue;
    const auto count=board->catalogs.fpgas.size();const auto source=Index(board->catalogs.fpgas,route.source);
    model.parents.assign(count,-1);std::vector<double> delays(count,0);std::vector<bool> seen(count,false);seen[source]=true;
    std::vector<std::uint32_t> queue{source};std::size_t visited_edges=0;
    for(std::size_t q=0;q<queue.size();++q) for(std::size_t e=0;e<route.edges.size();++e) {
      const auto& event=model.events[e];const auto& arc=*event.arc;if(arc.from!=queue[q])continue;
      Check(!seen[arc.to],"timing route is not a rooted tree");seen[arc.to]=true;queue.push_back(arc.to);++visited_edges;model.parents[arc.to]=e;
      volatile double scaled=arc.affine ? arc.slope*(*event.ratio-1):0;
      volatile double delay=arc.base+scaled;delays[arc.to]=delays[arc.from]+delay;
      Check(std::isfinite(delays[arc.to]),"timing route delay is not finite");
    }
    Check(visited_edges==route.edges.size() && !route.sinks.empty(),"timing route has disconnected edges or no sinks");
    double longest=-1;
    for(const auto& sink:route.sinks) {
      const auto fpga=Index(board->catalogs.fpgas,sink);Check(seen[fpga],"timing route has an unreachable sink");
      if(delays[fpga]>longest || (delays[fpga]==longest && sink<model.longest)){longest=delays[fpga];model.longest=sink;}
    }
  }
  Check(!rows.Step(),"cross-layer schedule contains an unexpected routed hop");
}
}  // namespace

void ProjectScheduleTiming(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()>=2 && inputs.size()<=4,"timing projection input coverage disagrees");
  for(const auto& input:inputs)input.RequireUnchanged();
  const bool scheduled=inputs.size()>=3,board_input=inputs.size()==4;
  CheckedRequestReader request(inputs.back().path(),4096);
  Check(request.Read<std::uint64_t>()==0x31544354464d5545ULL && request.Read<std::uint32_t>()==1,"unsupported timing projection request");
  const auto flags=request.Read<std::uint32_t>();Check(flags>0 && flags<=3,"invalid timing projection mode");
  const bool bind=flags&1,analyze=flags&2;
  Check(board_input==analyze && (!analyze || scheduled),"timing analysis board/schedule coverage disagrees");
  Generation(request,inputs[0]);if(scheduled)Generation(request,inputs[1]);
  const auto route_sha=Digest(request),schedule_sha=scheduled ? Digest(request):"",schedule_pretty=scheduled ? Digest(request):"";
  const auto affine=request.Read<std::uint8_t>();Check(affine<=1,"invalid timing model flag");request.RequireEnd();
  SqliteStore source(inputs[0]);auto& db=source.database();db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-32768; PRAGMA temp.cache_size=-16384");
  Check(Metadata(db,"logical_sha256")==JsonQuote(route_sha),"cross-layer route source identity disagrees");
  auto header=db.Prepare("SELECT json_extract(value,'$.design'),json_extract(value,'$.platform'),json_extract(value,'$.provider'),"
      "json_extract(value,'$.semantic_contract_schema'),json_extract(value,'$.semantic_contract_sha256'),"
      "json_type(value,'$.semantic_contract'),json_extract(value,'$.timing.normalization.positive_slack_scale_ns'),"
      "json_extract(value,'$.timing.normalization.negative_slack_scale_ns'),json_extract(value,'$.timing.normalization.max_clock_period_ns'),"
      "json_type(value,'$.timing.normalization.positive_slack_scale_ns'),json_type(value,'$.timing.normalization.negative_slack_scale_ns'),"
      "json_type(value,'$.timing.normalization.max_clock_period_ns') FROM metadata WHERE key='header'");
  Check(header.Step(),"timing RouteStore header is missing");
  const auto design=Text(header,0),platform=Text(header,1),provider=Text(header,2);
  std::string semantic_schema,semantic_sha;
  if(bind){Check(header.IsNull(5),"cross-layer routes may not duplicate the Phase 3 semantic contract");semantic_schema=Text(header,3);semantic_sha=Text(header,4);
    Check(semantic_sha.size()==64 && semantic_sha.find_first_not_of("0123456789abcdef")==std::string::npos,"cross-layer semantic digest is invalid");}
  double positive=1,negative=1,maximum_period=1;
  if(analyze){positive=Time(header,6,9,true);negative=Time(header,7,10,true);maximum_period=Time(header,8,11,true);}
  std::unique_ptr<SqliteStore> schedule;
  std::string schedule_provider;
  if(scheduled) {
    schedule=std::make_unique<SqliteStore>(inputs[1]);auto& s=schedule->database();
    Check(Metadata(s,"logical_sha256")==JsonQuote(schedule_sha),"cross-layer schedule source identity disagrees");
    auto h=s.Prepare("SELECT json_extract(value,'$.design'),json_extract(value,'$.platform'),json_extract(value,'$.provider'),"
        "json_extract(value,'$.transport_semantics'),json_extract(value,'$.semantic_contract_schema'),json_extract(value,'$.semantic_contract_sha256') FROM metadata WHERE key='header'");
    Check(h.Step() && Text(h,0)==design && Text(h,1)==platform,"cross-layer schedule identity is invalid");schedule_provider=Text(h,2);
    if(bind)Check(Text(h,3)=="sampled-virtual-wire" && Text(h,4)==semantic_schema && Text(h,5)==semantic_sha,"cross-layer schedule transport semantics disagree");
  }
  std::optional<ExactScheduleBoard> board;if(analyze)board=ReadExactScheduleBoard(inputs[0],inputs[2].path());
  RouteView route_view(inputs[0]);Check(route_view.count()<UINT32_MAX && route_view.count()<=inputs[0].generation().bytes,"timing route count exceeds source/rank bounds");
  std::vector<Model> models;models.reserve(route_view.count());std::uint64_t hops=0;
  route_view.VisitNetOrder([&](const RouteCoordinate&,const RouteIdentity& route){hops+=route.edges.size();models.push_back({route});});
  if(scheduled)InstallEvents(models,schedule->database(),board ? &*board:nullptr);
  const auto lookup=[&](const std::string& net)->const Model& {
    auto found=std::lower_bound(models.begin(),models.end(),net,[](const auto& model,const auto& name){return model.route.net<name;});
    Check(found!=models.end() && found->route.net==net,"cross-layer timing path references unknown cut net");return *found;
  };
  if(bind) {
    auto duplicates=db.Prepare("SELECT 1 FROM timing_paths p,json_each(p.record_json,'$.compressed_path_ids') m "
        "GROUP BY p.path_id,m.value HAVING count(*)>1 LIMIT 1");
    Check(!duplicates.Step(),"cross-layer timing path member identities repeat");
  }
  auto paths=db.Prepare("SELECT seq,path_id,json_type(record_json),json_extract(record_json,'$.path'),"
      "json_extract(record_json,'$.clock_domain'),json_type(record_json,'$.cut_nets'),json_array_length(record_json,'$.cut_nets'),"
      "json_type(record_json,'$.compressed_path_ids'),json_array_length(record_json,'$.compressed_path_ids'),"
      "json_type(record_json,'$.cut_transitions'),json_array_length(record_json,'$.cut_transitions'),"
      "json_extract(record_json,'$.clock_period_ns'),json_type(record_json,'$.clock_period_ns'),"
      "CASE WHEN json_type(record_json,'$.required_time_ns') IS NULL THEN json_extract(record_json,'$.clock_period_ns') ELSE json_extract(record_json,'$.required_time_ns') END,"
      "coalesce(json_type(record_json,'$.required_time_ns'),json_type(record_json,'$.clock_period_ns')),"
      "json_extract(record_json,'$.fixed_delay_ns'),json_type(record_json,'$.fixed_delay_ns') FROM timing_paths ORDER BY path_id");
  auto cuts=db.Prepare("SELECT p.path_id,c.key,c.type,c.value,json_type(p.record_json,'$.cut_transitions['||c.key||']'),"
      "json_extract(p.record_json,'$.cut_transitions['||c.key||'].net'),json_extract(p.record_json,'$.cut_transitions['||c.key||'].from'),"
      "json_extract(p.record_json,'$.cut_transitions['||c.key||'].to') FROM timing_paths p,json_each(p.record_json,'$.cut_nets') c ORDER BY p.path_id,c.key");
  auto members=bind ? db.Prepare("SELECT p.path_id,m.key,m.type,m.value FROM timing_paths p,json_each(p.record_json,'$.compressed_path_ids') m ORDER BY p.path_id,m.key"):SqliteStatement{};
  bool have_cut=cuts.Step(),have_member=bind && members.Step();
  std::unique_ptr<Statistics> statistics;if(analyze)statistics=std::make_unique<Statistics>(db);
  Sha256 projection;Hash(projection,"[");std::uint64_t path_count=0,cut_count=0;std::string previous;
  while(paths.Step()) {
    const auto path=Text(paths,1),clock=Text(paths,4);Number(paths,0);
    Check(paths.TypedText(2)=="object" && Text(paths,3)==path && (!path_count || previous<path),"timing path identity or order is invalid");previous=path;
    Check(paths.TypedText(5)=="array","cross-layer timing cut nets must be an array");const auto count=Number(paths,6);
    const bool explicit_transitions=!paths.IsNull(9) && paths.TypedText(9)!="null";
    if(explicit_transitions)Check(paths.TypedText(9)=="array" && Number(paths,10)==count,"cross-layer timing transition coverage is invalid");
    const auto required=Time(paths,13,14,true),fixed=Time(paths,15,16,false);double period=1;
    if(analyze){Check(count>0,"schedule timing path has no cut nets");period=Time(paths,11,12,true);}
    if(bind) {
      if(path_count)Hash(projection,",");Hash(projection,"{\"clock_domain\":");Hash(projection,JsonQuote(clock));Hash(projection,",\"cuts\":[");
    }
    double transport=0;std::set<std::pair<std::uint64_t,std::uint32_t>> seen_hops;std::string prior_sink;
    for(std::uint64_t index=0;index<count;++index) {
      Check(have_cut && Text(cuts,0)==path && Number(cuts,1)==index && Text(cuts,2)=="text","timing cut sequence is malformed");
      const auto net=Text(cuts,3);const auto& model=lookup(net);const auto& route=model.route;std::string sink;
      if(explicit_transitions) {
        Check(Text(cuts,4)=="object" && Text(cuts,5)==net && Text(cuts,6)==route.source,"cross-layer timing transition does not match route");sink=Text(cuts,7);
        Check(std::find(route.sinks.begin(),route.sinks.end(),sink)!=route.sinks.end(),"cross-layer timing transition does not match route");
        if(analyze && index)Check(prior_sink==route.source,"timing transition member partition chain is discontinuous");
      } else {
        if(bind)Check(route.sinks.size()==1,"multicast timing paths require explicit transitions");
        sink=analyze ? model.longest:route.sinks.front();
      }
      prior_sink=sink;
      if(bind) {
        if(index)Hash(projection,",");Hash(projection,JsonRecord(Object{{"cut",path+":cut:"+std::to_string(index)},
            {"index",index},{"logical_net",net},{"demand",route.demand},{"from",route.source},{"to",sink}}).Encode());
      }
      if(analyze) {
        const auto& fpgas=board->catalogs.fpgas;auto node=Index(fpgas,sink);const auto source_fpga=Index(fpgas,route.source);
        std::vector<std::uint32_t> edges;
        while(node!=source_fpga) {
          Check(node<model.parents.size() && model.parents[node]>=0,"timing transition has unreachable sink");
          auto edge=static_cast<std::uint32_t>(model.parents[node]);edges.push_back(edge);node=model.events[edge].arc->from;
        }
        std::reverse(edges.begin(),edges.end());const auto rank=static_cast<std::uint64_t>(&model-models.data());
        for(auto edge:edges) {
          Check(seen_hops.emplace(rank,edge).second,"schedule timing path repeats a routed hop");
          const auto& event=model.events[edge];const auto& arc=*event.arc;
          if(arc.affine) {volatile double scaled=arc.slope*(*event.ratio-1);volatile double delay=arc.base+scaled;transport=transport+delay;}
          else {volatile double tx=static_cast<double>(event.slot)*arc.period;transport=std::max(transport,static_cast<double>(tx))+arc.base;}
        }
      }
      ++cut_count;have_cut=cuts.Step();
    }
    if(bind) {
      Hash(projection,"],\"estimated_logic_delay_ns\":");Hash(projection,JsonRecord(fixed).Encode());Hash(projection,",\"members\":[");
      if(paths.IsNull(7))Hash(projection,JsonQuote(path));
      else {
        Check(paths.TypedText(7)=="array" && Number(paths,8)>0,"cross-layer timing path member identities are invalid");
        for(std::uint64_t index=0;index<Number(paths,8);++index) {
          Check(have_member && Text(members,0)==path && Number(members,1)==index && Text(members,2)=="text","timing member sequence is malformed");
          if(index)Hash(projection,",");Hash(projection,JsonQuote(Text(members,3)));have_member=members.Step();
        }
      }
      Hash(projection,"],\"path\":");Hash(projection,JsonQuote(path));Hash(projection,",\"required_time_ns\":");Hash(projection,JsonRecord(required).Encode());Hash(projection,"}");
    }
    if(analyze) {
      volatile double delay=fixed+transport;const double slack=required-delay;double normalized;
      if(slack>=0){volatile double numerator=slack*period;volatile double denominator=positive*maximum_period;normalized=numerator/denominator;}
      else {volatile double denominator=negative*period;normalized=slack/denominator;}
      statistics->Add(Number(paths,0),normalized,slack,delay);
    }
    ++path_count;
  }
  Check(!have_cut && !have_member,"timing child record coverage is incomplete");Hash(projection,"]");
  auto expected=db.Prepare("SELECT json_extract(value,'$.timing_paths.records') FROM metadata WHERE key='sections'");
  Check(expected.Step() && Number(expected,0)==path_count,"timing path coverage disagrees with source");
  JsonRecord contract;
  if(bind) {
    Object value{{"schema","emuflow.cross-layer-timing/v3"},{"provider","provider-neutral-route-schedule-binding-v3"},
        {"design",design},{"platform",platform},{"transport_semantics","sampled-virtual-wire"},
        {"route_binding",Object{{"provider",provider},{"sha256",route_sha}}},
        {"path_binding",Object{{"mode","canonical-route-store-path-projection-v1"},{"source_user_version",3},{"records",path_count},{"sha256",Hex(projection)}}},
        {"logic_segment_bindings",Array{}},{"logic_segment_binding",Object{{"mode","phase3-assignment-index"},{"semantic_contract_sha256",semantic_sha}}},
        {"semantic_binding",Object{{"schema",semantic_schema},{"sha256",semantic_sha},{"storage","phase3-assignment-index"}}},
        {"metrics",Object{{"paths",path_count},{"path_cut_bindings",cut_count},{"logic_segments",0},{"scheduled_hops",scheduled ? hops:0}}}};
    if(scheduled){value.emplace("schedule_binding",Object{{"provider",schedule_provider},{"sha256",schedule_pretty}});
      value.emplace("hop_binding",Object{{"mode","canonical-route-schedule-merge-v1"},{"scheduled_hops",hops}});}
    contract=JsonRecord(std::move(value));
  }
  JsonRecord timing;if(analyze)timing=statistics->Finish(affine ? "per-hop-ratio-delay-v1":"projected-fixed-slot-event-v1");
  const auto result=JsonRecord(Object{{"schema","emuflow.native-phase5-timing/v1"},{"status","pass"},
      {"implementation_id","native-route-schedule-timing-v1"},{"contract",std::move(contract)},{"timing_validation",std::move(timing)}}).Encode();
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<result<<'\n';});
}
}  // namespace emuflow::core
