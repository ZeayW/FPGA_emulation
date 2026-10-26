#pragma once
#include <cstdint>
#include <vector>

namespace emuflow::core {
// Shared integer calendar operation. The first/last bounds are inclusive.
std::uint32_t FirstPeriodicWord(std::uint32_t phase, std::uint32_t words,
                               std::uint32_t first, std::uint32_t last);

struct WordFanIn {
  // A root index names an explicit architectural launch phase; an event index
  // names that transfer's RX shadow capture. No implicit zero-phase root.
  bool from_event;
  std::uint32_t source;
  double data_delay_ns;
  double sampling_cost_ns;
};
struct PhysicalWordTransfer {
  std::uint32_t phase_word, ratio, fixed_latency_words;
  bool configuration_stable = false;
  std::vector<WordFanIn> fanins;
};
struct PhysicalWordModel {
  double period_ns = 5.0, epoch_ns = 0.0;
  std::uint32_t frame_slots = 0;
  std::vector<double> root_launch_ns;
  std::vector<PhysicalWordTransfer> transfers;
};

// Native data-plane kernel, not an STA engine or a source-binding certificate.
// The caller must bind every canonical fanin/clock/disposition before calling.
// Costs select nominal TX edges. Data delay remains a separate nonnegative STA
// arc; (sampling_cost - data_delay) shifts that arc's deadline, never its value.
// A relay begins at the earlier RX shadow capture. The fixed link arc already
// includes registered RX -> shadow, so that boundary must not be added again.
// Result indices alone own the chosen occurrences; RX indices are derivable.
std::vector<std::uint32_t> ProjectPhysicalWordEvents(const PhysicalWordModel& model);
double WordEventTime(const PhysicalWordModel& model, std::uint32_t index);
// Independent acceptance over actual OpenSTA TX-check slacks in canonical
// transfer/fanin order. Never calls the projector or composes data arrivals.
// The caller binds the engine checks to these same canonical input records.
void ValidatePhysicalWordEvents(const PhysicalWordModel& model,
    const std::vector<std::uint32_t>& selected,
    const std::vector<double>& support_slack_ns, double slack_tolerance_ns);
}  // namespace emuflow::core
