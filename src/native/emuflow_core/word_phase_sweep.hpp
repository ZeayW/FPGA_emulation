#pragma once
#include <cstdint>
#include <optional>
#include <vector>

namespace emuflow::core {
struct WordPhaseRelay {
  std::uint32_t source;
  std::int64_t sampling_cost_ps;
};
struct WordPhaseTransfer {
  std::uint32_t phase_word, ratio_serial_uis, fixed_latency_words;
  // Contributions from one declared launch phase, rebased to time zero.
  // No root contribution means absent from this phase's causal subgraph,
  // not constant/optimized-away logic or a numerical zero-delay arc.
  std::optional<std::int64_t> root_sampling_cost_ps;
  std::vector<WordPhaseRelay> fanins;
  std::optional<std::int64_t> capture_sampling_cost_ps;
};
struct WordPhaseSweepWitness {
  std::uint64_t phase_word;
  // The maximum is a left-limit supremum as offset approaches upper from
  // within this open phase cell. The cell is not a sampled point in time.
  std::int64_t lower_offset_ps, upper_offset_ps;
  std::uint32_t capture_transfer;
  std::vector<std::optional<std::int64_t>> load_words;
};
struct WordPhaseSweepResult {
  std::optional<std::int64_t> maximum_capture_cost_ps;
  std::optional<WordPhaseSweepWitness> witness;
  std::uint64_t repeat_words = 1, phase_cells = 0, node_recomputations = 0;
};
// Exact continuous-calendar kernel for an explicitly integral time quantum.
// Source/clock binding, provider precision and complete fanins are owned by
// the caller. Each transfer's costs and fanins must already be proved invariant
// over its recurring active serializer phases. This kernel neither rounds
// provider observations nor certifies those prerequisites or global STA.
// Sweep every joint serializer phase and every fractional word phase cell.
// The serializer recurrence is not a DUT period. maximum_repeat_words is an
// explicit execution bound; exceeding it fails rather than sampling phases.
WordPhaseSweepResult SweepContinuousWordPhases(std::int64_t word_period_ps,
    const std::vector<WordPhaseTransfer>& transfers,
    std::uint64_t maximum_repeat_words);
}  // namespace emuflow::core
