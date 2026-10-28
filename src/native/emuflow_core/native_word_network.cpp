#include "emuflow_core/native_word_network.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <tuple>

namespace emuflow::core {
bool NativeWordClockPhase::operator<(const NativeWordClockPhase& other) const {
  return std::tie(clock,edge)<std::tie(other.clock,other.edge);
}
namespace {
using Phase = NativeWordClockPhase;
using Pair = std::pair<Phase,Phase>;
void Check(bool condition,const char* message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::int64_t Ps(double cost) {
  const auto rounded = std::round(static_cast<long double>(cost)*1000);
  Check(std::isfinite(cost) && rounded >= std::numeric_limits<std::int64_t>::min() &&
      rounded <= std::numeric_limits<std::int64_t>::max(),"native network time exceeds its integral domain");
  Check(std::abs(static_cast<long double>(cost)-rounded/1000) <= 1e-8L,
      "native network cost is finer than its declared 1 ps provider grid");
  return static_cast<std::int64_t>(rounded);
}
void Maximum(std::optional<std::int64_t>& target,std::int64_t value) {
  if (!target || value>*target) target=value;
}
std::int64_t Required(std::int64_t cost,std::int64_t reference,std::int64_t window) {
  if (cost<=0) return 0;
  const __int128 numerator=static_cast<__int128>(cost)*reference;
  const auto result=numerator/window+(numerator%window!=0);
  Check(result<=std::numeric_limits<std::int64_t>::max(),"native network normalized period exceeds its domain");
  return static_cast<std::int64_t>(result);
}
Phase Logical(const std::map<std::string,std::string>& clocks,const std::string& name,NativeWordLaunchEdge edge) {
  const auto found=clocks.find(name);
  Check(found!=clocks.end() && edge!=NativeWordLaunchEdge::kNone,"native network clock/edge has no logical owner");
  return {found->second,edge};
}
struct TransferCosts {
  std::map<Phase,std::int64_t> roots, captures;
  std::map<std::uint32_t,std::int64_t> fanins;
};
void Merge(std::map<Phase,std::int64_t>& target,const Phase& phase,std::int64_t cost) {
  const auto found=target.find(phase);
  if (found==target.end()) target.emplace(phase,cost);
  else found->second=std::max(found->second,cost);
}
}  // namespace
NativeWordNetworkProjection ProjectNativeWordNetwork(
    const std::vector<NativeWordTransferOwner>& transfers,
    const std::vector<NativeWordPartitionCosts>& partitions,
    const std::vector<NativeWordClockWindow>& windows,std::int64_t word_period,
    std::int64_t reference_period,std::uint64_t maximum_repeat,
    const std::function<void(const NativeWordWindowProjection&,
        const std::vector<WordPhaseTransfer>&)>& visit_window) {
  Check(word_period>0 && reference_period>0 && maximum_repeat>0,
      "native network needs explicit word/reference periods and recurrence bounds");
  Check(transfers.size()<=UINT32_MAX,"native network transfer population exceeds its domain");
  NativeWordNetworkProjection result;result.transfers=transfers.size();
  std::map<std::string,std::uint32_t> ids;
  std::set<std::string> parts;
  for (const auto& part:partitions)
    Check(!part.partition.empty() && parts.insert(part.partition).second && part.tx && part.captures,
        "native network partition is empty, repeated or missing its cost streams");
  for (std::uint32_t i=0;i<transfers.size();++i) {
    const auto& t=transfers[i];
    Check(!t.id.empty() && ids.emplace(t.id,i).second && parts.count(t.source_partition) &&
        parts.count(t.sink_partition) && t.source_partition!=t.sink_partition,
        "native network transfer has duplicate or invalid partition ownership");
  }
  std::vector<TransferCosts> costs(transfers.size());
  std::vector<bool> tx_seen(transfers.size(),false),rx_seen(transfers.size(),false);
  std::map<Pair,std::int64_t> local;
  std::set<Phase> phases;
  for (const auto& part:partitions) {
    std::map<std::string,std::string> clocks;
    for (const auto& c:part.clocks) {
      Check(!c.native_clock.empty() && !c.logical_clock.empty() && clocks.emplace(c.native_clock,c.logical_clock).second,
          "native network clock alias is empty or repeated");
      phases.insert({c.logical_clock,NativeWordLaunchEdge::kRise});
      phases.insert({c.logical_clock,NativeWordLaunchEdge::kFall});
    }
    // Empty local clock families are valid for a pure RX -> TX relay.
    const auto incoming=[&](const std::string& origin) {
      const auto role=part.rx_entries.find(origin);
      Check(role!=part.rx_entries.end(),"native network RX source has no canonical entry");
      const auto entry=ids.find(role->second);
      Check(entry!=ids.end() && transfers[entry->second].sink_partition==part.partition,
          "native network RX source belongs to another receiver");
      return entry->second;
    };
    for (const auto& role:part.rx_entries) {
      const auto i=incoming(role.first);
      Check(!rx_seen[i],"native network canonical arrival has multiple RX source owners");rx_seen[i]=true;
    }
    for (const auto& missing:part.missing_rx_origins) {
      (void)incoming(missing);++result.missing_rx_origins;
    }
    Check(part.tx->observed_contexts==part.tx->expected_contexts,
        "native network cannot project an incomplete TX calendar");
    Check(!part.tx->untimed_rx_rows,"native network RX fanin has no numerical timing disposition");
    for (const auto& entry:part.tx->entries) {
      const auto found=ids.find(entry.source.id);
      Check(found!=ids.end(),"native network TX source has no canonical entry");const auto i=found->second;
      const auto& t=transfers[i];
      Check(!tx_seen[i] && t.source_partition==part.partition && t.phase_word==entry.source.phase_word &&
          t.ratio_serial_uis==entry.source.ratio_serial_uis,"native network TX source differs from its canonical owner");
      tx_seen[i]=true;
      Check(entry.data_phase_invariant && *entry.data_phase_invariant && !entry.contexts.empty(),
          "native network needs complete recurring-phase data cost/fanin invariance");
      for (const auto& context:entry.contexts) {
        for (const auto& bound:context.costs) {
          const auto& row=bound.observation;
          if (row.source_kind!=NativeWordSourceKind::kDutClock && row.source_kind!=NativeWordSourceKind::kRx)
            result.external_sources.emplace(part.partition,row.source_kind,row.source);
        }
      }
      for (const auto& bound:entry.contexts.front().costs) {
        const auto& row=bound.observation;
        if (row.source_kind==NativeWordSourceKind::kDutClock) {
          const auto phase=Logical(clocks,row.source,row.launch_edge);
          if (bound.sampling_cost_ps) Merge(costs[i].roots,phase,*bound.sampling_cost_ps);
        } else if (row.source_kind==NativeWordSourceKind::kRx) {
          const auto source=incoming(row.source);
          Check(!part.missing_rx_origins.count(row.source) && bound.incoming_entry==transfers[source].id && bound.sampling_cost_ps,
              "native network relay has a missing or different RX projection");
          const auto old=costs[i].fanins.find(source);
          if (old==costs[i].fanins.end()) costs[i].fanins.emplace(source,*bound.sampling_cost_ps);
          else old->second=std::max(old->second,*bound.sampling_cost_ps);
        }
      }
    }
    std::map<std::string,Phase> capture_queries;
    std::map<std::string,std::string> capture_clocks;
    for (const auto& c:clocks) {
      capture_queries.emplace("dut-capture:"+c.first+":rise",Logical(clocks,c.first,NativeWordLaunchEdge::kRise));
      capture_queries.emplace("dut-capture:"+c.first+":fall",Logical(clocks,c.first,NativeWordLaunchEdge::kFall));
      capture_clocks.emplace("dut-capture:"+c.first+":rise",c.first);
      capture_clocks.emplace("dut-capture:"+c.first+":fall",c.first);
    }
    using CaptureKey=std::tuple<std::string,NativeWordSourceKind,std::string,NativeWordLaunchEdge>;
    std::set<CaptureKey> seen;
    VisitNativeWordCosts(*part.captures,[&](const auto& row) {
      Check(row.phase_word==-1,"native network capture is conditioned on a TX phase");
      const auto target=capture_queries.find(row.query);
      Check(target!=capture_queries.end(),"native network capture query has no clock/edge owner");
      Check(seen.emplace(row.query,row.source_kind,row.source,row.launch_edge).second,
          "native network capture source disposition repeats");
      ++result.capture_rows;
      if (row.evidence)Check(row.evidence->end_clock==capture_clocks.at(row.query),
          "native network capture witness belongs to another clock family");
      if (row.source_kind==NativeWordSourceKind::kDutClock) {
        const auto launch=Logical(clocks,row.source,row.launch_edge);
        if (row.evidence) {
          Check(row.evidence->start_clock==row.source,"native network capture starts in another clock family");
          const Pair key{launch,target->second};const auto value=Ps(row.evidence->sampling_cost_ns);
          const auto old=local.find(key);
          if (old==local.end()) local.emplace(key,value);else old->second=std::max(old->second,value);
        }
      } else if (row.source_kind==NativeWordSourceKind::kRx) {
        const auto source=incoming(row.source);
        Check(!part.missing_rx_origins.count(row.source),"native network capture uses a missing RX source");
        if (row.evidence) {
          Check(row.evidence->start_clock=="emuflow_tdm_clk","native network capture RX clock differs");
          Merge(costs[source].captures,target->second,Ps(row.evidence->sampling_cost_ns));
        }
      } else {
        Check(row.source_kind==NativeWordSourceKind::kDeclaredInput && part.separately_queried_inputs.count(row.source),
            "native network capture source has an undeclared external context");
      }
    });
    for (const auto& q:capture_queries) {
      for (const auto& c:clocks)for (auto edge:{NativeWordLaunchEdge::kRise,NativeWordLaunchEdge::kFall})
        Check(seen.count({q.first,NativeWordSourceKind::kDutClock,c.first,edge}),
            "native network capture matrix omits a DUT clock edge");
      for (const auto& rx:part.rx_entries)if (!part.missing_rx_origins.count(rx.first))
        Check(seen.count({q.first,NativeWordSourceKind::kRx,rx.first,NativeWordLaunchEdge::kNone}),
            "native network capture matrix omits an active RX source");
      for (const auto& port:part.separately_queried_inputs)
        Check(seen.count({q.first,NativeWordSourceKind::kDeclaredInput,port,NativeWordLaunchEdge::kNone}),
            "native network capture matrix omits a declared input");
    }
  }
  Check(std::all_of(tx_seen.begin(),tx_seen.end(),[](bool x){return x;}) &&
        std::all_of(rx_seen.begin(),rx_seen.end(),[](bool x){return x;}),
        "native network canonical transfer lacks its complete TX/RX owner pair");
  std::map<Pair,NativeWordClockWindow> budget;
  for (const auto& window:windows) {
    Check(phases.count(window.launch) && phases.count(window.capture) && window.reference_window_ps>0 &&
        budget.emplace(Pair{window.launch,window.capture},window).second,
        "native network clock window is absent, repeated or invalid");
  }
  for (const auto& launch:phases)for (const auto& capture:phases) {
    std::vector<WordPhaseTransfer> graph;
    for (std::size_t i=0;i<transfers.size();++i) {
      const auto& t=transfers[i];WordPhaseTransfer node{t.phase_word,t.ratio_serial_uis,t.fixed_latency_words,{}, {}, {}};
      const auto root=costs[i].roots.find(launch),cap=costs[i].captures.find(capture);
      if (root!=costs[i].roots.end()) node.root_sampling_cost_ps=root->second;
      if (cap!=costs[i].captures.end()) node.capture_sampling_cost_ps=cap->second;
      for (const auto& fanin:costs[i].fanins)node.fanins.push_back({fanin.first,fanin.second});
      graph.push_back(std::move(node));
    }
    auto projection=SweepContinuousWordPhases(word_period,graph,maximum_repeat);
    const Pair pair{launch,capture};const auto direct=local.find(pair);
    if (!projection.maximum_capture_cost_ps && direct==local.end())continue;
    const auto window=budget.find(pair);
    Check(window!=budget.end(),"native timed clock pair lacks an original edge-window budget");
    NativeWordWindowProjection result_window{window->second,{},std::move(projection),{}, {}};
    if (direct!=local.end())result_window.local_sampling_cost_ps=direct->second;
    result_window.maximum_sampling_cost_ps=result_window.local_sampling_cost_ps;
    if (result_window.transport.maximum_capture_cost_ps)
      Maximum(result_window.maximum_sampling_cost_ps,*result_window.transport.maximum_capture_cost_ps);
    result_window.required_reference_period_ps=Required(*result_window.maximum_sampling_cost_ps,reference_period,window->second.reference_window_ps);
    Maximum(result.maximum_sampling_cost_ps,*result_window.maximum_sampling_cost_ps);
    Maximum(result.required_reference_period_ps,*result_window.required_reference_period_ps);
    if(visit_window)visit_window(result_window,graph);
    result.windows.push_back(std::move(result_window));
  }
  return result;
}
}  // namespace emuflow::core
