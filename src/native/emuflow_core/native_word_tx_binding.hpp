#pragma once
#include "emuflow_core/native_word_costs.hpp"
#include <map>
#include <vector>

namespace emuflow::core {
struct NativeWordTxEntry {
  std::string id, query;
  std::uint32_t phase_word, ratio_serial_uis;
};
struct NativeWordTxFaninCounts {
  std::uint64_t phase_word;
  std::string query;
  // Independent case-enabled all_fanin observations, not inferred from costs.
  std::uint64_t rx, infrastructure, ports, unclassified;
};
struct NativeWordTxCatalog {
  std::vector<NativeWordTxEntry> entries;
  std::vector<std::string> dut_clocks;
  // Native RX source tags bind to incoming canonical schedule entries.
  std::map<std::string,std::string> rx_entries;
  std::uint64_t maximum_repeat_words, maximum_contexts;
  // Complete source-bound counter catalog, including receive-only periods.
  // TX ratios alone need not describe the partition's joint calendar.
  std::vector<std::uint32_t> calendar_period_words;
};
struct NativeWordBoundTxCost {
  NativeWordCost observation;
  std::string incoming_entry;  // nonempty exactly for RX observations
  std::optional<std::int64_t> sampling_cost_ps;
};
struct NativeWordBoundTxContext {
  std::uint64_t phase_word;
  std::vector<NativeWordBoundTxCost> costs;
};
struct NativeWordBoundTxEntry {
  NativeWordTxEntry source;
  std::vector<NativeWordBoundTxContext> contexts;
  // False as soon as differing data costs/fanins are observed; true only with
  // every recurring context present. No value means evidence is incomplete.
  std::optional<bool> data_phase_invariant;
};
struct NativeWordTxBinding {
  std::uint64_t repeat_words = 1, expected_contexts = 0, observed_contexts = 0;
  std::uint64_t unbound_source_rows = 0, untimed_rx_rows = 0;
  NativeWordCostCounts rows;
  std::vector<NativeWordBoundTxEntry> entries;
};
// Bind native observations to canonical serializer entries and RX source roles,
// checking every observed context against an independent fanin count ledger.
// The 1 ps grid is an explicit provider contract: only floating arithmetic
// roundoff <= 1e-8 ns is tolerated. Finer physical observations are rejected.
// Partial snapshots retain missing calendar contexts, never zero-cost arcs.
// Caller seals input files/checkpoints and qualifies source roles, clock/I/O
// ownership, startup controls, missing RX roles and native query completeness.
// Even complete/invariant TX data does not constitute a global STA result.
NativeWordTxBinding BindNativeWordTxCosts(const NativeWordTxCatalog& catalog,
    const std::vector<NativeWordTxFaninCounts>& fanins, std::istream& costs);
}  // namespace emuflow::core
