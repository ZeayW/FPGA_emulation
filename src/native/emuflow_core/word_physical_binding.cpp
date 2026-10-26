#include "emuflow_core/word_physical_binding.hpp"
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <set>
#include <sstream>
#include <tuple>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Attach(SqliteDatabase& db,const ArtifactRef& artifact,const char* name) {
  auto query=db.Prepare(std::string("ATTACH DATABASE ? AS ")+name);
  query.BindText(1,SqliteImmutableUri(artifact.path()));query.Execute();
}
std::string Header(SqliteDatabase& db,const std::string& field) {
  auto row=db.Prepare("SELECT json_extract(value,?1) FROM metadata WHERE key='header'");
  row.BindText(1,"$."+field);Check(row.Step(),"word physical header is missing");return row.TypedText(0);
}
std::uint32_t Index(std::int64_t value) {
  Check(value>=0 && std::uint64_t(value)<UINT32_MAX,"word physical coordinate exceeds native domain");
  return static_cast<std::uint32_t>(value);
}
std::uint32_t Part(const std::vector<std::string>& parts,const std::string& name) {
  const auto found=std::lower_bound(parts.begin(),parts.end(),name);
  Check(found!=parts.end() && *found==name,"word physical partition is unknown");return found-parts.begin();
}
std::uint32_t Group(const std::string& name) {
  Check(name.size()>=13 && name.rfind("segment",0)==0 &&
        name.find_first_not_of("0123456789",7)==std::string::npos,
        "word physical semantic group identity differs");
  const auto number=std::stoull(name.substr(7));Check(number<UINT32_MAX,"word physical semantic group is too large");
  std::ostringstream canonical;canonical<<"segment"<<std::setw(6)<<std::setfill('0')<<number;
  Check(canonical.str()==name,"word physical semantic group spelling differs");return number;
}
std::string ShadowQ(SqliteDatabase& db,const std::string& endpoint) {
  auto row=db.Prepare("SELECT instance,ordinal FROM boundary.boundary_registers WHERE endpoint_id=? ORDER BY ordinal");
  row.BindText(1,endpoint);Check(row.Step() && row.TypedInteger(1)==0,"word physical RX shadow is missing");
  std::string result;for(char c:row.TypedText(0)) {if(c=='\\')result+='\\';result+=c;}
  Check(!result.empty() && !row.Step(),"word physical RX shadow is ambiguous");return result+"/Q";
}
std::pair<double,double> Cost(SqliteStatement& row,int data,int sampling,int disposition,int proof) {
  Check(row.TypedText(disposition)!="optimized-away" && row.IsNull(proof),
        "word physical optimized-away row requires a checked disposition owner");
  Check(!row.IsNull(data) && !row.IsNull(sampling) && std::isfinite(row.Real(data)) && row.Real(data)>=0 &&
        std::isfinite(row.Real(sampling)) && std::isfinite(row.Real(sampling)-row.Real(data)),
        "word physical row lacks finite clock-aware costs");
  return {row.Real(data),row.Real(sampling)};
}
struct Entry {
  std::uint32_t cut,from,to;
  bool origin;
};
}

void BoundPhysicalWordModel::RequireUnchanged() const {
  for(const auto& input:inputs)input.RequireUnchanged();
}
void VisitPhysicalWordSupportChecks(const BoundPhysicalWordModel& binding,
    const std::vector<std::uint32_t>& selected,const std::function<void(const WordSupportCheck&)>& visitor) {
  const auto& model=binding.model;
  Check(selected.size()==model.transfers.size(),"word physical support occurrence coverage differs");
  for(std::uint32_t i=0;i<model.transfers.size();++i) {
    const auto& transfer=model.transfers[i];
    Check(transfer.ratio>=8 && transfer.ratio<=256 && transfer.ratio%8==0 && selected[i]>0 &&
          selected[i]%(transfer.ratio/8)==transfer.phase_word &&
          std::uint64_t(selected[i])+transfer.fixed_latency_words<model.frame_slots-1,
          "word physical support occurrence is outside its calendar");
    for(std::uint32_t j=0;j<transfer.fanins.size();++j) {
      const auto& fanin=transfer.fanins[j];double launch;
      if(fanin.from_event) {
        Check(fanin.source<model.transfers.size(),"word physical support has an unknown RX event");
        const auto rx=std::uint64_t(selected[fanin.source])+model.transfers[fanin.source].fixed_latency_words;
        Check(rx<selected[i],"word physical support TX does not follow its RX capture");
        launch=WordEventTime(model,rx);
      } else {
        Check(fanin.source<model.root_launch_ns.size(),"word physical support has an unknown root");
        launch=model.root_launch_ns[fanin.source];
      }
      const auto required=WordEventTime(model,selected[i])-(fanin.sampling_cost_ns-fanin.data_delay_ns);
      Check(std::isfinite(launch) && std::isfinite(required) && std::isfinite(fanin.data_delay_ns) && fanin.data_delay_ns>=0,
            "word physical support check is nonfinite or negative");
      visitor({i,j,launch,fanin.data_delay_ns,required});
    }
  }
}
BoundPhysicalWordModel BindPhysicalWordModel(const ArtifactRef& assignment,const ArtifactRef& semantic,
    const ArtifactRef& schedule,SourceLaunchClockView& launches,const WordPhysicalBindingContext& context,
    const std::vector<WordPhysicalPartition>& partitions) {
  Check(schedule.schema()=="tdm-schedule-store-v4","word physical binding requires current ScheduleStore");
  Check(!context.design.empty() && !context.platform.empty() && std::isfinite(context.word_period_ns) &&
        context.word_period_ns>0 && std::isfinite(context.epoch_ns) && context.frame_slots>=3 &&
        context.fixed_latency_words>0 && context.fixed_latency_words<context.frame_slots-2 &&
        !context.implementation_clock_periods_ns.empty(),"word physical clock or PHY context is invalid");
  for(const auto& item:context.implementation_clock_periods_ns)
    Check(!item.first.empty() && std::isfinite(item.second) && item.second>0,"word physical DUT clock is invalid");
  auto shared_semantics=std::make_shared<ExactSemanticView>(assignment,semantic);
  auto& exact=*shared_semantics;
  Check(exact.semantic_sha256()==context.source.semantic_sha256,"word physical semantic identity differs");
  launches.RequireSourceIdentity(context.source);
  SqliteStore calendar(schedule);auto& db=calendar.database();Attach(db,assignment,"assignment");
  Check(Header(db,"design")==context.design && Header(db,"platform")==context.platform &&
        Header(db,"semantic_contract_sha256")==context.source.semantic_sha256 &&
        Header(db,"word_event_policy")=="periodic-lvds-8bit-word-load-to-rx-visible-v2" &&
        Header(db,"transport_semantics")=="sampled-virtual-wire","word physical schedule context differs");
  auto metadata=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='logical_sha256'");
  Check(metadata.Step() && metadata.TypedText(0)==context.source.schedule_sha256,"word physical schedule seal differs");
  auto frame=db.Prepare("SELECT json_extract(value,'$.route_constraints.frame_slots') FROM metadata WHERE key='header'");
  Check(frame.Step() && frame.TypedInteger(0)==context.frame_slots,"word physical frame boundary differs");
  // The original route is already in ScheduleStore. No net-name catalog or
  // per-transfer lookup over the original graph is reconstructed in Python.
  auto rows=db.Prepare("SELECT e.seq,c.cut_index,e.source_fpga,e.sink_fpga,r.source_fpga,e.slot,e.arrival_slot,"
      "e.tdm_ratio,e.net,r.net,c.source_part_index,e.hop FROM schedule_entries e "
      "LEFT JOIN schedule_routes r ON r.demand=e.demand LEFT JOIN assignment.cuts c ON c.net_id=e.net ORDER BY e.seq");
  BoundPhysicalWordModel result;result.context=context;result.semantics=std::move(shared_semantics);
  result.inputs={assignment,semantic,schedule,launches.artifact(),launches.original_artifact()};
  result.model.period_ns=context.word_period_ns;result.model.epoch_ns=context.epoch_ns;
  result.model.frame_slots=context.frame_slots;
  std::vector<Entry> entries;
  while(rows.Step()) {
    const auto seq=Index(rows.TypedInteger(0));Check(seq==entries.size(),"word physical schedule sequence is not dense");
    const auto cut=Index(rows.TypedInteger(1)),from=Part(exact.parts(),rows.TypedText(2)),to=Part(exact.parts(),rows.TypedText(3));
    const auto source=Part(exact.parts(),rows.TypedText(4));
    const auto slot=Index(rows.TypedInteger(5)),arrival=Index(rows.TypedInteger(6)),ratio=Index(rows.TypedInteger(7));
    Check(cut<exact.cut_count() && from!=to && source==exact.SourcePart(cut) && source==rows.TypedInteger(10) &&
          rows.TypedText(8)==rows.TypedText(9) && ratio>=8 && ratio<=256 && ratio%8==0 &&
          slot>0 && std::uint64_t(slot)+context.fixed_latency_words==arrival && arrival<context.frame_slots-1 &&
          ((from==source)==(rows.TypedInteger(11)==0)),"word physical transfer disagrees with its cut, route or PHY");
    entries.push_back({cut,from,to,from==source});
    result.model.transfers.push_back({slot%(ratio/8),ratio,context.fixed_latency_words,false,{}});
    result.transfers.push_back({calendar.Id<WordScheduleEntryTag>(seq),cut,from,to});
    result.fanins.emplace_back();
  }
  auto count=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key='entry_relation_records'");
  Check(count.Step() && count.TypedInteger(0)==static_cast<std::int64_t>(entries.size()),"word physical schedule coverage differs");
  // Dense native incoming coordinates are sufficient for fanin DAG edges.
  // Route trees have one arrival per (cut, FPGA), including relay-only nodes.
  std::map<std::pair<std::uint32_t,std::uint32_t>,std::uint32_t> incoming;
  std::vector<std::uint32_t> origin_count(exact.cut_count(),0);
  for(std::uint32_t i=0;i<entries.size();++i) {
    const auto& entry=entries[i];
    Check(incoming.emplace(std::make_pair(entry.cut,entry.to),i).second,"word physical route has duplicate arrivals");
    if(entry.origin)++origin_count[entry.cut];
  }
  Check(std::all_of(origin_count.begin(),origin_count.end(),[](auto n){return n>0;}),"word physical schedule omits an assigned cut");
  for(const auto& entry:entries)if(!entry.origin)
    Check(incoming.count({entry.cut,entry.from}),"word physical relay has no preceding RX");
  std::map<std::tuple<std::uint64_t,std::uint64_t,double>,std::uint32_t> roots;
  std::set<std::uint32_t> seen_parts;
  std::set<std::pair<std::uint32_t,std::uint32_t>> groups;
  std::vector<bool> tx_seen(entries.size(),false),rx_seen(entries.size(),false);
  for(const auto& partition:partitions) {
    Check(partition.logic_costs.schema()=="logic-segment-timing-store-v3" &&
          partition.boundary_identity.schema()=="boundary-identity-store-v1" && partition.boundary_costs.schema()=="boundary-timing-store-v3",
          "word physical binding requires current identity and cost owners");
    SqliteStore logic(partition.logic_costs),boundary(partition.boundary_identity),costs(partition.boundary_costs);
    auto& local=logic.database();const auto part=Part(exact.parts(),Header(local,"fpga"));
    Check(seen_parts.insert(part).second,"word physical partition is repeated");
    for(auto* store:{&logic,&boundary,&costs}) {
      auto& physical=store->database();
      Check(Header(physical,"fpga")==exact.parts()[part] && Header(physical,"design")==context.design &&
            Header(physical,"platform")==context.platform && Header(physical,"status")=="pass",
            "word physical partition belongs to another design or platform");
    }
    for(auto* store:{&logic,&costs})
      Check(Header(store->database(),"sampling_cost_policy")=="vivado-clock-aware-reference-v1","word physical sampling policy differs");
    Check(Header(costs.database(),"measurement_scope")=="native-word-registered-interface" &&
          Header(local,"system_routes_sha256")==context.source.routes_sha256 &&
          Header(local,"tdm_schedule_sha256")==context.source.schedule_sha256 &&
          Header(local,"semantic_contract_sha256")==context.source.semantic_sha256,"word physical cost context differs");
    for(const auto* owner:{&partition.logic_costs,&partition.boundary_identity,&partition.boundary_costs})result.inputs.push_back(*owner);
    // At most three attachments per connection, independent of FPGA count.
    Attach(local,partition.boundary_identity,"boundary");Attach(local,partition.boundary_costs,"costs");Attach(local,schedule,"calendar");
    auto endpoints=local.Prepare("SELECT b.id,b.kind,b.source_class,b.logical_net,b.demand,b.link,b.peer,b.lane,"
        "e.seq,e.net,e.demand,e.link_id,e.source_fpga,e.sink_fpga,e.lane,"
        "t.kind,t.schedule_entry,CASE WHEN typeof(t.delay_ns) IN ('real','integer') THEN t.delay_ns END,"
        "CASE WHEN typeof(t.sampling_cost_ns) IN ('real','integer') THEN t.sampling_cost_ns END,"
        "t.measurement,t.optimization_source,e.entry_id "
        "FROM boundary.endpoints b LEFT JOIN calendar.schedule_entries e ON e.entry_id=b.schedule_entry "
        "LEFT JOIN costs.timings t ON t.id=b.id ORDER BY b.id");
    while(endpoints.Step()) {
      const auto i=Index(endpoints.TypedInteger(8));Check(i<entries.size(),"word physical endpoint has no schedule entry");
      const auto& entry=entries[i];const auto kind=endpoints.TypedText(1);const bool tx=kind=="tx";
      Check((tx || kind=="rx") && (tx?entry.from:entry.to)==part &&
            endpoints.TypedText(3)==endpoints.TypedText(9) && endpoints.TypedText(4)==endpoints.TypedText(10) &&
            endpoints.TypedText(5)==endpoints.TypedText(11) &&
            endpoints.TypedText(6)==endpoints.TypedText(tx?13:12) && endpoints.TypedInteger(7)==endpoints.TypedInteger(14) &&
            endpoints.TypedText(15)==kind && !endpoints.IsNull(16),"word physical endpoint identity differs from its schedule");
      Check(endpoints.TypedText(21)==endpoints.TypedText(16),"word physical timing endpoint belongs to another transfer");
      const auto cost=Cost(endpoints,17,18,19,20);
      if(tx) {
        Check(!tx_seen[i] && endpoints.TypedText(2)==(entry.origin?"dut-net":"forwarded-shadow"),"word physical TX source class differs");
        tx_seen[i]=true;
        if(!entry.origin) {
          const auto parent=incoming.at({entry.cut,entry.from});
          auto rx=local.Prepare("SELECT b.id FROM boundary.endpoints b JOIN calendar.schedule_entries e "
              "ON e.entry_id=b.schedule_entry WHERE b.kind='rx' AND e.seq=?");rx.BindInteger(1,parent);
          Check(rx.Step(),"word physical relay source boundary is missing");const auto source=rx.TypedText(0);
          Check(!rx.Step() && ShadowQ(local,source)==ShadowQ(local,endpoints.TypedText(0)),"word physical relay forwards another RX shadow");
          result.model.transfers[i].fanins.push_back({true,parent,cost.first,cost.second});
          result.fanins[i].push_back({part,UINT32_MAX,{},endpoints.TypedText(0)});
        }
      } else {
        Check(!rx_seen[i] && endpoints.TypedText(2)=="captured-shadow" && cost.second<=context.word_period_ns,
              "word physical RX shadow identity or word-stage timing differs");
        rx_seen[i]=true;ShadowQ(local,endpoints.TypedText(0));
        // The RX word -> shadow stage is already inside fixed_latency_words.
      }
    }
    auto extras=local.Prepare("SELECT 1 FROM costs.timings t LEFT JOIN boundary.endpoints b ON b.id=t.id WHERE b.id IS NULL LIMIT 1");
    Check(!extras.Step(),"word physical timing contains an unknown boundary");
    auto support=local.Prepare("SELECT s.seq,s.kind,s.static_exact_segment_id,s.system_path,s.start_pin,s.start_object_kind,"
        "CASE WHEN typeof(s.delay_ns) IN ('real','integer') THEN s.delay_ns END,"
        "CASE WHEN typeof(s.sampling_cost_ns) IN ('real','integer') THEN s.sampling_cost_ns END,"
        "s.measurement,s.optimization_source,s.fpga,b.kind,e.seq "
        "FROM segments s LEFT JOIN boundary.endpoints b ON b.id=s.replace_tx_endpoint "
        "LEFT JOIN calendar.schedule_entries e ON e.entry_id=b.schedule_entry "
        "WHERE s.kind IN ('launch','transition') AND s.system_path GLOB 'static-exact-contract:*' ORDER BY s.seq");
    std::set<std::tuple<std::uint32_t,std::uint32_t,std::uint64_t,std::uint64_t,double>> launch_seen;
    while(support.Step()) {
      const auto i=Index(support.TypedInteger(12)),group=Group(support.TypedText(2));
      Check(i<entries.size() && entries[i].origin && entries[i].from==part && group<exact.segments().size(),
            "word physical complete fanin has no originating transfer");
      const auto& semantic_segment=exact.segments()[group];const auto kind=support.TypedText(1);
      Check(semantic_segment.kind<2 && !semantic_segment.configuration_stable && semantic_segment.part==part &&
            semantic_segment.target==entries[i].cut && kind==(semantic_segment.kind?"transition":"launch") &&
            support.TypedText(3)=="static-exact-contract:"+support.TypedText(2) &&
            support.TypedText(10)==exact.parts()[part] && support.TypedText(11)=="tx",
            "word physical complete fanin disagrees with the semantic contract");
      const auto cost=Cost(support,6,7,8,9);const auto origin=logic.Id<WordLogicCostTag>(Index(support.TypedInteger(0)));
      if(semantic_segment.kind==0) {
        for(const auto& fact:launches.Facts(support.TypedText(4),support.TypedText(5),context.implementation_clock_periods_ns)) {
          launches.RequireOwner(fact);
          const auto key=std::make_tuple(fact.source.sequence(),fact.clock.sequence(),fact.engine_phase_ns);
          Check(launch_seen.emplace(i,group,fact.source.sequence(),fact.clock.sequence(),fact.engine_phase_ns).second,
                "word physical architectural fanin is repeated");
          auto found=roots.find(key);std::uint32_t root;
          if(found==roots.end()) {
            Check(roots.size()<UINT32_MAX,"word physical root population exceeds native domain");root=roots.size();roots.emplace(key,root);
            result.roots.push_back(fact);result.model.root_launch_ns.push_back(fact.launch_ns);
          } else root=found->second;
          result.model.transfers[i].fanins.push_back({false,root,cost.first,cost.second});
          result.fanins[i].push_back({part,group,origin,{}});
        }
        groups.emplace(i,group);
      } else {
        const auto found=incoming.find({semantic_segment.source,part});
        Check(found!=incoming.end() && groups.emplace(i,group).second,"word physical transition is missing or repeated");
        auto rx=local.Prepare("SELECT b.id FROM boundary.endpoints b JOIN calendar.schedule_entries e ON e.entry_id=b.schedule_entry "
            "WHERE b.kind='rx' AND e.seq=?");rx.BindInteger(1,found->second);
        Check(rx.Step(),"word physical transition RX boundary is missing");const auto source=rx.TypedText(0);
        Check(!rx.Step() && support.TypedText(5)=="pin" && support.TypedText(4)==ShadowQ(local,source),
              "word physical transition starts at another RX shadow");
        result.model.transfers[i].fanins.push_back({true,found->second,cost.first,cost.second});
        result.fanins[i].push_back({part,group,origin,{}});
      }
    }
  }
  Check(seen_parts.size()==exact.parts().size(),"word physical partition coverage is incomplete");
  // Root coordinates are canonical within their single launch-fact owner;
  // parallel physical-result enumeration must not affect their model indices.
  std::vector<SourceLaunchFact> ordered_roots;std::vector<double> ordered_times;
  std::vector<std::uint32_t> root_order(result.roots.size());
  for(const auto& item:roots) {
    root_order[item.second]=ordered_roots.size();ordered_roots.push_back(result.roots[item.second]);
    ordered_times.push_back(result.roots[item.second].launch_ns);
  }
  result.roots=std::move(ordered_roots);result.model.root_launch_ns=std::move(ordered_times);
  for(std::uint32_t i=0;i<entries.size();++i) {
    Check(tx_seen[i] && rx_seen[i],"word physical transfer lacks its TX or RX boundary");
    if(entries[i].origin)for(auto group:exact.Sources(entries[i].cut)) {
      if(exact.segments()[group].configuration_stable) {
        Check(result.model.transfers[i].fanins.empty(),"word physical constant cut has dynamic fanins");
        result.model.transfers[i].configuration_stable=true;
      } else Check(groups.count({i,group}),"word physical complete source group is missing");
    }
    for(auto& fanin:result.model.transfers[i].fanins)if(!fanin.from_event)fanin.source=root_order[fanin.source];
    Check(result.fanins[i].size()==result.model.transfers[i].fanins.size(),"word physical cost origins are incomplete");
  }
  // The projector performs the complete DAG/cycle and frame feasibility check.
  // Its result is intentionally not saved as a second occurrence owner here.
  exact.RequireUnchanged();launches.RequireUnchanged();result.RequireUnchanged();return result;
}
}  // namespace emuflow::core
