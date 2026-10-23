#pragma once

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/stage_runtime.hpp"

#include <functional>
#include <memory>
#include <optional>

namespace emuflow::core {
class EmuirView;
struct EmuirCellTag {};
struct EmuirNetTag {};
struct EmuirPinTag {};

// A public record identity includes its artifact namespace. Algorithms can
// explicitly extract the local sequence for their own compact integer arrays.
template <typename Tag> class EmuirId {
 public:
  std::uint64_t sequence() const { return sequence_; }
 private:
  friend class EmuirView;
  EmuirId(std::shared_ptr<const ArtifactRef> owner, std::uint64_t sequence)
      : owner_(std::move(owner)), sequence_(sequence) {}
  std::shared_ptr<const ArtifactRef> owner_;
  std::uint64_t sequence_;
};
using EmuirCellId = EmuirId<EmuirCellTag>;
using EmuirNetId = EmuirId<EmuirNetTag>;
using EmuirPinId = EmuirId<EmuirPinTag>;
enum class EndpointRole { kDriver, kSink };
struct EmuirCounts { std::uint64_t cells, nets, endpoints; };
struct EmuirEndpoint {
  EmuirNetId net;
  EndpointRole role;
  std::uint64_t ordinal;
  std::optional<EmuirCellId> cell;
  std::optional<EmuirPinId> pin;  // Present when pin validation was requested.
};

// Read-only compact EmuIR v2 owner. Construction reads bounded metadata only;
// the ordered visitors validate records during the consumer's required scan.
// There is no cached graph, whole-store recount or generic JSON materializer.
class EmuirView {
 public:
  explicit EmuirView(const ArtifactRef& artifact);
  const EmuirCounts& counts() const { return counts_; }
  EmuirNetId Net(std::uint64_t sequence) const;
  std::string NetName(const EmuirNetId& net);
  void VisitCellStates(const std::function<void(EmuirCellId, bool)>& visitor);
  void VisitConnectivity(const std::function<void(const EmuirEndpoint&)>& endpoint,
                         const std::function<void(EmuirNetId)>& net_end,
                         bool validate_pins = false);
 private:
  std::shared_ptr<const ArtifactRef> artifact_;
  SqliteDatabase database_;
  EmuirCounts counts_{};
};
}  // namespace emuflow::core
