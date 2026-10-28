#pragma once
#include <cstdint>
#include <functional>
#include <istream>
#include <optional>
#include <string>

namespace emuflow::core {
enum class NativeWordSourceKind {
  kDutClock, kRx, kInfrastructure, kPort, kUnclassified, kDeclaredInput
};
enum class NativeWordLaunchEdge { kNone, kRise, kFall };
enum class NativeWordMeasurement { kReferenceCost, kNoTimedPath, kContextRequired };
struct NativeWordCostEvidence {
  double requirement_ns, data_ns, sampling_cost_ns, clock_adjustment_ns;
  std::string start_pin, end_pin, start_clock, end_clock;
  double launch_edge_ns, capture_edge_ns;
};
struct NativeWordCost {
  std::int64_t phase_word;
  std::string query, source;
  NativeWordSourceKind source_kind;
  NativeWordLaunchEdge launch_edge;
  NativeWordMeasurement measurement;
  std::optional<NativeWordCostEvidence> evidence;
};
struct NativeWordCostCounts {
  std::uint64_t rows = 0, measured = 0, no_timed_path = 0, context_required = 0;
};
// Consume the provider's 16-column stream with bounded per-record memory.
// A missing native path has no numerical evidence and is not an optimization
// certificate. Root clocks and RX roles are identities; old DUT FF names or
// primitive types are not required. The caller still binds immutable inputs,
// source/clock roles, canonical query coverage and duplicate keys before using
// these observations in an event model. Counts alone prove no such coverage.
NativeWordCostCounts VisitNativeWordCosts(std::istream& input,
    const std::function<void(const NativeWordCost&)>& visitor);
}  // namespace emuflow::core
