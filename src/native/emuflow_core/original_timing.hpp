#pragma once
#include "emuflow_core/source_launch_clocks.hpp"
#include <optional>

namespace emuflow::core {
struct OriginalClockBudget {
  std::string source_clock,target_clock;
  // Target period is the path owner's displayed period. Normalization uses
  // the separate canonical engine clock catalog, never this rounded value.
  double source_period_ns,target_period_ns;
  double launch_edge_ns,capture_edge_ns,source_offset_ns;
  double target_cycle_time_ns,multicycle_adjustment_ns;
  double check_margin_ns,source_latency_ns,target_clock_delay_ns,clock_skew_ns;
  bool output_delay;
  double ReferenceWindow() const;
};
struct OriginalEndpoint {
  std::string engine_object,port;
  std::optional<std::string> instance;
  std::uint64_t bit;
};
struct OriginalTimingPath {
  PathId source;
  std::string identity;
  OriginalEndpoint start,end;
  OriginalClockBudget budget;
};
struct ScaledOriginalWindow {
  double launch_ns,window_ns,coefficient;
};
// Both clock roots must scale uniformly. Raw engine periods preserve exact
// edge ratios, including half cycles and engine cycle/multicycle offsets.
ScaledOriginalWindow ScaleOriginalWindow(const OriginalClockBudget& budget,
    const SourceClockReferences& clocks,const std::map<std::string,double>& targets);

class OriginalTimingView {
 public:
  // Inputs are the already validated immutable selector/payload pair. The
  // bounded selector is read and sealed once; no whole-store rehash, net
  // expansion or original-path dictionary is performed at this boundary.
  OriginalTimingView(const ArtifactRef& selector,const ArtifactRef& payload,
                     const std::string& selector_sha256);
  std::uint64_t count() const { return count_; }
  const std::string& selector_sha256() const { return selector_sha256_; }
  const ArtifactRef& artifact() const { return paths_.artifact(); }
  void Visit(const std::function<void(const OriginalTimingPath&)>& visitor);
  void RequireUnchanged() const;
  void RequireOwner(const PathId& path) const { paths_.RequireOwner(path); }
 private:
  ArtifactRef selector_;
  SqliteStore paths_;
  std::uint64_t count_=0;
  std::string selector_sha256_;
};
struct OriginalCaptureCheck {
  // The final event may be an original launch or a routed RX shadow event.
  double event_launch_ns,data_delay_ns,required_ns;
  double original_launch_ns,edge_window_ns,period_coefficient;
};
// Routed sampling E already owns setup/skew/uncertainty/I/O check costs.
// Keep D as the engine data arc and shift the capture deadline by E-D.
// Reference-library check_margin is retained above but never added again.
OriginalCaptureCheck BindOriginalCaptureCheck(const OriginalTimingPath& path,
    const SourceClockReferences& clocks,const std::map<std::string,double>& targets,
    double final_event_ns,double data_delay_ns,double sampling_cost_ns);
}  // namespace emuflow::core
