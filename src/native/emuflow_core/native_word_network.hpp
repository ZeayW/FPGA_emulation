#pragma once
#include "emuflow_core/native_word_tx_binding.hpp"
#include "emuflow_core/word_phase_sweep.hpp"
#include <set>
#include <tuple>
#include <functional>

namespace emuflow::core {
struct NativeWordTransferOwner {
  std::string id, source_partition, sink_partition;
  std::uint32_t phase_word, ratio_serial_uis, fixed_latency_words;
};
struct NativeWordClockAlias {
  std::string native_clock, logical_clock;
};
struct NativeWordPartitionCosts {
  std::string partition;
  const NativeWordTxBinding* tx;
  std::vector<NativeWordClockAlias> clocks;
  // The complete source-role catalog includes missing native projections.
  std::map<std::string,std::string> rx_entries;
  std::set<std::string> missing_rx_origins;
  // Optional separately queried inputs are already represented by their
  // declared clock families; these rows are coverage evidence, not new roots.
  std::set<std::string> separately_queried_inputs;
  std::istream* captures;
};
struct NativeWordClockPhase {
  std::string clock;
  NativeWordLaunchEdge edge;
  bool operator<(const NativeWordClockPhase& other) const;
};
struct NativeWordClockWindow {
  NativeWordClockPhase launch, capture;
  // Exact source-owned edge window at the common reference period. A half
  // cycle has half that window. No serializer period is used as a DUT period.
  std::int64_t reference_window_ps;
};
struct NativeWordWindowProjection {
  NativeWordClockWindow window;
  std::optional<std::int64_t> local_sampling_cost_ps;
  WordPhaseSweepResult transport;
  std::optional<std::int64_t> maximum_sampling_cost_ps, required_reference_period_ps;
};
struct NativeWordNetworkProjection {
  std::vector<NativeWordWindowProjection> windows;
  std::optional<std::int64_t> maximum_sampling_cost_ps, required_reference_period_ps;
  std::uint64_t transfers = 0, capture_rows = 0, missing_rx_origins = 0;
  // Preserve every startup/control/unclassified TX source for the owning
  // physical-mode validator. This data-path projection does not resolve them.
  std::set<std::tuple<std::string,NativeWordSourceKind,std::string>> external_sources;
};
// Assemble every partition's complete canonical TX calendars and native
// capture-clock matrix, then sweep continuous TDM phases per source/capture
// edge window. Source-role owners, original clock-window coverage, explicit
// I/O constraints, startup-mode proofs and independent engine checks remain
// caller-owned prerequisites. Missing RX projections are retained, not removal
// certificates. This result is effective setup cost, not maximum raw datapath
// delay, endpoint TNS, hold/CDC signoff or a complete original-path STA ledger.
NativeWordNetworkProjection ProjectNativeWordNetwork(
    const std::vector<NativeWordTransferOwner>& transfers,
    const std::vector<NativeWordPartitionCosts>& partitions,
    const std::vector<NativeWordClockWindow>& windows,
    std::int64_t word_period_ps, std::int64_t reference_period_ps,
    std::uint64_t maximum_repeat_words,
    const std::function<void(const NativeWordWindowProjection&,
        const std::vector<WordPhaseTransfer>&)>& visit_window = {});
}  // namespace emuflow::core
