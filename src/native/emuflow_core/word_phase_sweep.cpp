#include "emuflow_core/word_phase_sweep.hpp"
#include "emuflow_core/stage_runtime.hpp"
#include <algorithm>
#include <deque>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <queue>
#include <tuple>

namespace emuflow::core {
namespace {
using Wide = __int128;
void Check(bool valid, const char* message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::int64_t Integer(Wide value) {
  Check(value >= std::numeric_limits<std::int64_t>::min() &&
        value <= std::numeric_limits<std::int64_t>::max(),
        "continuous word phase arithmetic exceeds its signed time domain");
  return static_cast<std::int64_t>(value);
}
void Increment(std::uint64_t& count) {
  Check(count < UINT64_MAX,"continuous word phase count exceeds its domain");
  ++count;
}
Wide Floor(Wide value, Wide divisor) {
  return value/divisor - (value%divisor < 0);
}
Wide Ceil(Wide value, Wide divisor) {
  return value/divisor + (value%divisor > 0);
}
std::int64_t RootFirst(std::int64_t cost, std::int64_t upper, std::int64_t period) {
  // At upper-epsilon, an equality at upper is not yet a feasible root load.
  return Integer(Floor(Wide(cost)-upper,period)+1);
}
struct Prepared {
  struct Edge { std::uint32_t source; std::int64_t minimum_words; };
  std::vector<std::uint32_t> order, rank;
  std::vector<std::vector<std::uint32_t>> successors;
  std::vector<std::vector<Edge>> edges;
  std::vector<bool> active;
  std::map<std::int64_t,std::vector<std::uint32_t>> root_changes;
  std::vector<std::int64_t> bounds;
  std::uint64_t repeat = 1;
};
Prepared Prepare(std::int64_t period,const std::vector<WordPhaseTransfer>& transfers,
                 std::uint64_t maximum_repeat) {
  Check(period > 0 && maximum_repeat > 0,"continuous word phase bounds are invalid");
  Check(transfers.size() <= UINT32_MAX,"continuous word phase population exceeds its domain");
  const auto count = transfers.size();
  Prepared result;
  result.successors.resize(count);result.edges.resize(count);
  result.rank.resize(count);result.active.resize(count);
  std::vector<std::size_t> pending(count,0);
  std::deque<std::uint32_t> ready;
  for (std::size_t i=0; i<count; ++i) {
    const auto& transfer = transfers[i];
    Check(transfer.ratio_serial_uis >= 8 && transfer.ratio_serial_uis <= 256 &&
          transfer.ratio_serial_uis%8 == 0 && transfer.phase_word < transfer.ratio_serial_uis/8 &&
          transfer.fixed_latency_words > 0,"continuous phase transfer has invalid eight-bit word units");
    const auto words = transfer.ratio_serial_uis/8;
    const auto quotient = result.repeat/std::gcd(result.repeat,std::uint64_t(words));
    Check(quotient <= UINT64_MAX/words,"continuous word phase recurrence exceeds its domain");
    result.repeat = quotient*words;
    Check(result.repeat <= maximum_repeat,"complete word phase recurrence exceeds its declared execution bound");
    if (transfer.root_sampling_cost_ps) {
      auto residue = *transfer.root_sampling_cost_ps%period;
      if (residue < 0) residue += period;
      if (residue) result.root_changes[residue].push_back(static_cast<std::uint32_t>(i));
    }
    for (const auto& fanin:transfer.fanins) {
      Check(fanin.source < count && fanin.source != i,"continuous word phase dependency is unknown or self-referential");
      result.edges[i].push_back({fanin.source,Integer(std::max(Wide(1),Ceil(fanin.sampling_cost_ps,period)))});
      result.successors[fanin.source].push_back(static_cast<std::uint32_t>(i));
      ++pending[i];
    }
    if (!pending[i]) ready.push_back(static_cast<std::uint32_t>(i));
  }
  while (!ready.empty()) {
    const auto node = ready.front();ready.pop_front();
    result.rank[node] = static_cast<std::uint32_t>(result.order.size());
    result.order.push_back(node);
    bool active = bool(transfers[node].root_sampling_cost_ps);
    for (const auto& edge:result.edges[node]) active = active || result.active[edge.source];
    result.active[node] = active;
    for (const auto child:result.successors[node]) if (!--pending[child]) ready.push_back(child);
  }
  Check(result.order.size() == count,"continuous word phase fanin graph contains a cycle");
  for (const auto& item:result.root_changes) result.bounds.push_back(item.first);
  result.bounds.push_back(period);
  Check(Wide(result.repeat)*result.bounds.size() <= UINT64_MAX,
        "continuous word phase cell population exceeds its domain");
  return result;
}
std::int64_t Compute(const Prepared& prepared,const std::vector<WordPhaseTransfer>& transfers,
    std::uint64_t phase,std::uint32_t node,const std::vector<std::int64_t>& roots,
    const std::vector<std::optional<std::int64_t>>& loads) {
  const auto& transfer = transfers[node];
  std::optional<std::int64_t> first;
  if (transfer.root_sampling_cost_ps) first = roots[node];
  for (const auto& edge:prepared.edges[node]) {
    if (!prepared.active[edge.source]) continue;
    Check(bool(loads[edge.source]),"continuous word phase predecessor is not evaluated");
    const auto capture = Integer(Wide(*loads[edge.source])+transfers[edge.source].fixed_latency_words);
    const auto required = Integer(Wide(capture)+edge.minimum_words);
    if (!first || required > *first) first = required;
  }
  Check(bool(first),"continuous word phase active node has no causal contribution");
  const auto words = transfer.ratio_serial_uis/8;
  const auto local = (transfer.phase_word+words-phase%words)%words;
  return Integer(Wide(local)+Wide(words)*Ceil(Wide(*first)-local,words));
}
std::int64_t CaptureCost(const WordPhaseTransfer& transfer,std::int64_t load,std::int64_t period) {
  const auto capture = Integer(Wide(load)+transfer.fixed_latency_words);
  const auto time = Integer(Wide(capture)*period);
  return Integer(Wide(time)+*transfer.capture_sampling_cost_ps);
}
std::vector<std::optional<std::int64_t>> Initial(const Prepared& prepared,
    const std::vector<WordPhaseTransfer>& transfers,std::uint64_t phase,
    std::int64_t upper,std::int64_t period,std::vector<std::int64_t>& roots,
    std::uint64_t& recomputations) {
  std::vector<std::optional<std::int64_t>> loads(transfers.size());
  for (const auto node:prepared.order) {
    if (!prepared.active[node]) continue;
    if (transfers[node].root_sampling_cost_ps)
      roots[node] = RootFirst(*transfers[node].root_sampling_cost_ps,upper,period);
    loads[node] = Compute(prepared,transfers,phase,node,roots,loads);
    Increment(recomputations);
  }
  return loads;
}
}  // namespace

WordPhaseSweepResult SweepContinuousWordPhases(std::int64_t period,
    const std::vector<WordPhaseTransfer>& transfers,std::uint64_t maximum_repeat) {
  const auto prepared = Prepare(period,transfers,maximum_repeat);
  WordPhaseSweepResult result;result.repeat_words = prepared.repeat;
  const auto count = transfers.size();
  result.maximum_arrival_ps.resize(count);
  std::vector<std::int64_t> roots(count);
  const auto arrival = [&](std::uint32_t node,std::int64_t load,std::int64_t upper) {
    const auto value = Integer((Wide(load)+transfers[node].fixed_latency_words)*period+upper);
    auto& maximum = result.maximum_arrival_ps[node];
    if (!maximum || value>*maximum) maximum=value;
  };
  for (std::uint64_t phase=0; phase<prepared.repeat; ++phase) {
    auto loads = Initial(prepared,transfers,phase,prepared.bounds.front(),period,roots,result.node_recomputations);
    using Capture = std::tuple<std::int64_t,std::uint32_t,std::int64_t>;
    std::priority_queue<Capture> captures;
    const auto add_capture = [&](std::uint32_t node) {
      if (loads[node] && transfers[node].capture_sampling_cost_ps)
        captures.emplace(CaptureCost(transfers[node],*loads[node],period),node,*loads[node]);
    };
    for (std::uint32_t node=0; node<count; ++node) add_capture(node);
    for (std::size_t cell=0; cell<prepared.bounds.size(); ++cell) {
      const auto upper = prepared.bounds[cell];
      const auto lower = cell ? prepared.bounds[cell-1] : 0;
      while (!captures.empty()) {
        const auto& entry = captures.top();
        if (loads[std::get<1>(entry)] && *loads[std::get<1>(entry)] == std::get<2>(entry)) break;
        captures.pop();
      }
      Increment(result.phase_cells);
      if (!captures.empty()) {
        const auto value = Integer(Wide(std::get<0>(captures.top()))+upper);
        if (!result.maximum_capture_cost_ps || value > *result.maximum_capture_cost_ps) {
          result.maximum_capture_cost_ps = value;
          result.witness = WordPhaseSweepWitness{phase,lower,upper,std::get<1>(captures.top()),{}};
        }
      }
      if (cell+1 == prepared.bounds.size()) {
        // A node's load is constant between its own updates. Its arrival
        // grows with the fractional offset there, so record only the last
        // left limit before each change and the final cell. Do not scan the
        // entire graph for every fractional root boundary.
        for (std::uint32_t node=0;node<count;++node)
          if (loads[node]) arrival(node,*loads[node],upper);
        continue;
      }
      const auto changed = prepared.root_changes.find(upper);
      Check(changed != prepared.root_changes.end(),"continuous word phase cell lacks its root boundary");
      std::priority_queue<std::pair<std::uint32_t,std::uint32_t>,
          std::vector<std::pair<std::uint32_t,std::uint32_t>>,std::greater<std::pair<std::uint32_t,std::uint32_t>>> updates;
      std::vector<bool> queued(count,false);
      const auto enqueue = [&](std::uint32_t node) {
        if (!queued[node]) {queued[node] = true;updates.emplace(prepared.rank[node],node);}
      };
      for (const auto node:changed->second) {
        roots[node] = RootFirst(*transfers[node].root_sampling_cost_ps,prepared.bounds[cell+1],period);
        enqueue(node);
      }
      while (!updates.empty()) {
        const auto node = updates.top().second;updates.pop();queued[node] = false;
        const auto previous = *loads[node];
        const auto next = Compute(prepared,transfers,phase,node,roots,loads);
        Increment(result.node_recomputations);
        Check(next <= previous,"continuous word phase update is not monotone within its cell sweep");
        if (next == previous) continue;
        arrival(node,previous,upper);
        loads[node] = next;add_capture(node);
        for (const auto child:prepared.successors[node]) enqueue(child);
      }
    }
  }
  if (result.witness) {
    result.witness->load_words = Initial(prepared,transfers,result.witness->phase_word,
        result.witness->upper_offset_ps,period,roots,result.node_recomputations);
  }
  return result;
}
}  // namespace emuflow::core
