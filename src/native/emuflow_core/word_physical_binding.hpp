#pragma once
#include "emuflow_core/exact_semantic_view.hpp"
#include "emuflow_core/source_launch_clocks.hpp"
#include "emuflow_core/word_events.hpp"
#include <optional>

namespace emuflow::core {
struct WordPhysicalPartition {
  // Already validated physical-result owners from the same Phase7 invocation.
  ArtifactRef logic_costs, boundary_identity, boundary_costs;
};
struct WordPhysicalBindingContext {
  SourceLaunchIdentity source;
  std::string design, platform;
  // The caller binds these to its validated Phase6 word PHY plan. They are
  // independent of the uniform DUT implementation-clock periods below.
  double word_period_ns, epoch_ns;
  std::uint32_t frame_slots, fixed_latency_words;
  std::map<std::string,double> implementation_clock_periods_ns;
};
struct WordScheduleEntryTag {};
struct WordLogicCostTag {};
struct WordTransferOrigin {
  ArtifactId<WordScheduleEntryTag> entry;
  std::uint32_t cut, from, to;
};
struct WordFaninOrigin {
  std::uint32_t partition, semantic_group;
  // Exactly one owner key: logic row seq, or boundary endpoint id for a relay.
  std::optional<ArtifactId<WordLogicCostTag>> logic;
  std::string boundary_endpoint;
};
struct BoundPhysicalWordModel {
  PhysicalWordModel model;
  std::vector<SourceLaunchFact> roots;
  std::vector<WordTransferOrigin> transfers;
  // Same transfer/fanin order used by the independent engine-slack validator.
  std::vector<std::vector<WordFaninOrigin>> fanins;
  std::vector<ArtifactRef> inputs;
  void RequireUnchanged() const;
};
struct WordSupportCheck {
  std::uint32_t transfer, fanin;
  double launch_ns, data_delay_ns, required_ns;
};
// Derived engine interchange only. Emit these alongside original capture
// checks in the single selected OpenSTA execution, then pass its slacks in
// this order to ValidatePhysicalWordEvents. No second timing composer runs.
void VisitPhysicalWordSupportChecks(const BoundPhysicalWordModel& binding,
    const std::vector<std::uint32_t>& selected,
    const std::function<void(const WordSupportCheck&)>& visitor);

// Indexed production join, not an STA result or a coverage certificate. Reads
// the current assignment, ScheduleStore, complete-contract physical rows and
// original launch facts, retaining only native coordinates and numerical arcs.
// Selected original timing-path subsets cannot supply complete fanins.
// This entry rejects required optimized-away rows until their checked native
// disposition owner is bound; it never interprets missing facts as constants.
// Callers must still validate OpenSTA support slacks, all original captures,
// and target/runtime clock semantics before releasing the global result.
BoundPhysicalWordModel BindPhysicalWordModel(const ArtifactRef& assignment,
    const ArtifactRef& semantic, const ArtifactRef& schedule,
    SourceLaunchClockView& launches, const WordPhysicalBindingContext& context,
    const std::vector<WordPhysicalPartition>& partitions);
}  // namespace emuflow::core
