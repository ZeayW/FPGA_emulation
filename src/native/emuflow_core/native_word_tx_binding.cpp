#include "emuflow_core/native_word_tx_binding.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <set>
#include <tuple>

namespace emuflow::core {
namespace {
void Check(bool valid,const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
using ContextKey = std::pair<std::uint64_t,std::string>;
using SourceKey = std::tuple<NativeWordSourceKind,std::string,NativeWordLaunchEdge>;
using DataCosts = std::map<SourceKey,std::pair<NativeWordMeasurement,std::optional<std::int64_t>>>;
SourceKey Key(const NativeWordCost& row) {return {row.source_kind,row.source,row.launch_edge};}
bool DataSource(NativeWordSourceKind kind) {
  return kind == NativeWordSourceKind::kDutClock || kind == NativeWordSourceKind::kRx;
}
std::int64_t Picoseconds(double value) {
  const auto rounded = std::round(static_cast<long double>(value)*1000);
  Check(std::isfinite(value) && rounded >= std::numeric_limits<std::int64_t>::min() &&
      rounded <= std::numeric_limits<std::int64_t>::max(),"native TX cost exceeds its integral time domain");
  Check(std::abs(static_cast<long double>(value)-rounded/1000) <= 1e-8L,
      "native TX cost is finer than its declared 1 ps provider grid");
  return static_cast<std::int64_t>(rounded);
}
}  // namespace

NativeWordTxBinding BindNativeWordTxCosts(const NativeWordTxCatalog& catalog,
    const std::vector<NativeWordTxFaninCounts>& fanins,std::istream& costs) {
  NativeWordTxBinding result;
  Check(catalog.maximum_repeat_words > 0 && catalog.maximum_contexts > 0,
      "native TX catalog has no explicit execution bounds");
  std::set<std::string> ids,clocks;
  for (const auto& clock:catalog.dut_clocks)
    Check(!clock.empty() && clocks.insert(clock).second,"native TX DUT clock is empty or repeated");
  Check(!clocks.empty(),"native TX catalog has no DUT launch clocks");
  std::set<std::uint32_t> calendar;
  for (auto period:catalog.calendar_period_words) {
    Check(period>=1 && period<=32 && calendar.insert(period).second,
        "native TX counter calendar period is invalid or repeated");
    const auto quotient=result.repeat_words/std::gcd(result.repeat_words,static_cast<std::uint64_t>(period));
    Check(quotient<=catalog.maximum_repeat_words/period,"native TX serializer recurrence exceeds its explicit bound");
    result.repeat_words=quotient*period;
  }
  Check(!calendar.empty(),"native TX source-bound counter calendar is missing");
  for (const auto& item:catalog.rx_entries) {
    const auto& tag = item.first;
    Check(tag.size() > 1 && tag[0] == 's' && tag.find_first_not_of("0123456789",1) == std::string::npos &&
        !item.second.empty(),"native TX RX role has no canonical schedule entry");
  }
  for (const auto& entry:catalog.entries) {
    Check(!entry.id.empty() && !entry.query.empty() && ids.insert(entry.id).second,
        "native TX canonical entry is empty or repeated");
    Check(entry.ratio_serial_uis >= 8 && entry.ratio_serial_uis <= 256 && entry.ratio_serial_uis%8 == 0 &&
        entry.phase_word < entry.ratio_serial_uis/8,"native TX serializer phase or ratio is invalid");
    const auto period = entry.ratio_serial_uis/8;
    Check(calendar.count(period),"native TX serializer ratio is absent from its counter calendar");
    result.entries.push_back({entry,{},std::nullopt});
  }
  struct Context {
    std::size_t entry;
    const NativeWordTxFaninCounts* fanin = nullptr;
    std::vector<NativeWordBoundTxCost> observations;
    std::set<SourceKey> keys;
  };
  std::map<ContextKey,Context> contexts;
  for (std::size_t i=0;i<catalog.entries.size();++i) {
    const auto& entry = catalog.entries[i];
    for (std::uint64_t phase=entry.phase_word;phase<result.repeat_words;phase+=entry.ratio_serial_uis/8) {
      Check(contexts.size() < catalog.maximum_contexts,"native TX calendar contexts exceed their explicit bound");
      Check(contexts.emplace(ContextKey{phase,entry.query},Context{i,nullptr,{},{}}).second,
          "native TX calendar selects multiple canonical entries for one word bit");
    }
  }
  result.expected_contexts = contexts.size();
  for (const auto& fanin:fanins) {
    const auto found = contexts.find({fanin.phase_word,fanin.query});
    Check(found != contexts.end(),"native TX fanin ledger names an unknown calendar context");
    Check(!found->second.fanin,"native TX fanin ledger repeats a calendar context");
    found->second.fanin = &fanin;
  }
  result.rows = VisitNativeWordCosts(costs,[&](const NativeWordCost& row) {
    Check(row.phase_word >= 0,"native TX observation has no serializer phase");
    const auto found = contexts.find({static_cast<std::uint64_t>(row.phase_word),row.query});
    Check(found != contexts.end(),"native TX observation names an unknown calendar context");
    auto& context = found->second;
    Check(context.fanin,"native TX observation lacks its independent fanin ledger");
    Check(context.keys.insert(Key(row)).second,"native TX observation repeats a source in its calendar context");
    NativeWordBoundTxCost bound{row,{},std::nullopt};
    if (row.source_kind == NativeWordSourceKind::kDutClock) {
      Check(clocks.count(row.source),"native TX observation names an undeclared DUT clock");
      if (row.evidence) Check(row.evidence->start_clock == row.source,
          "native TX DUT timing witness belongs to another launch clock");
    } else if (row.source_kind == NativeWordSourceKind::kRx) {
      const auto incoming = catalog.rx_entries.find(row.source);
      Check(incoming != catalog.rx_entries.end(),"native TX observation names an unbound RX source role");
      bound.incoming_entry = incoming->second;
      if (row.evidence) Check(row.evidence->start_clock == "emuflow_tdm_clk",
          "native TX RX timing witness is outside its word-clock domain");
      else ++result.untimed_rx_rows;
    } else {
      Check(row.source_kind != NativeWordSourceKind::kDeclaredInput,
          "native TX declared input requires an explicit input environment adapter");
      ++result.unbound_source_rows;
    }
    if (row.evidence) bound.sampling_cost_ps = Picoseconds(row.evidence->sampling_cost_ns);
    context.observations.push_back(std::move(bound));
  });
  for (auto& item:contexts) {
    auto& context = item.second;
    if (context.observations.empty()) {
      Check(!context.fanin,"native TX completed fanin context has no timing dispositions");
      continue;
    }
    std::map<NativeWordSourceKind,std::uint64_t> counts;
    for (const auto& observation:context.observations) ++counts[observation.observation.source_kind];
    for (const auto& clock:catalog.dut_clocks) {
      for (auto edge:{NativeWordLaunchEdge::kRise,NativeWordLaunchEdge::kFall})
        Check(context.keys.count({NativeWordSourceKind::kDutClock,clock,edge}),
            "native TX context omits a declared DUT launch edge");
    }
    const auto& f = *context.fanin;
    Check(counts[NativeWordSourceKind::kRx] == f.rx &&
        counts[NativeWordSourceKind::kInfrastructure] == f.infrastructure &&
        counts[NativeWordSourceKind::kPort] == f.ports &&
        counts[NativeWordSourceKind::kUnclassified] == f.unclassified,
        "native TX timing sources disagree with independent enabled fanin counts");
    std::sort(context.observations.begin(),context.observations.end(),
        [](const auto& a,const auto& b){return Key(a.observation)<Key(b.observation);});
    result.entries[context.entry].contexts.push_back({item.first.first,std::move(context.observations)});
    ++result.observed_contexts;
  }
  for (auto& entry:result.entries) {
    std::optional<DataCosts> first;
    bool equal = true;
    for (const auto& context:entry.contexts) {
      DataCosts values;
      for (const auto& observation:context.costs) {
        const auto& row = observation.observation;
        if (DataSource(row.source_kind)) values.emplace(Key(row),std::make_pair(row.measurement,observation.sampling_cost_ps));
      }
      if (!first) first = std::move(values);
      else if (*first != values) equal = false;
    }
    if (!equal) entry.data_phase_invariant = false;
    else if (entry.contexts.size() == result.repeat_words/(entry.source.ratio_serial_uis/8))
      entry.data_phase_invariant = true;
  }
  return result;
}
}  // namespace emuflow::core
