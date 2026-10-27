#pragma once

#include "storage/store.hpp"
#include "storage/sqlite.hpp"
#include "common/stage_runtime.hpp"

#include <functional>
#include <memory>
#include <optional>

namespace emuflow::core {
class EmuirView;
using EmuirCellTag=CellTag;
using EmuirNetTag=NetTag;
struct EmuirPinTag {};

// A public record identity includes its artifact namespace. Algorithms can
// explicitly extract the local sequence for their own compact integer arrays.
template <typename Tag> using EmuirId = ArtifactId<Tag>;
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
  std::optional<EmuirPinId> pin;  // Compact v2 has an interned pin identity.
  std::string port;  // Present when pin validation was requested, including v1.
  std::uint64_t bit=0;
};

// Read-only EmuIR v1/v2 owner. Compact v2 reads bounded metadata only;
// the explicit older v1 adapter derives counts because its format has none.
// the ordered visitors validate records during the consumer's required scan.
// There is no cached graph, whole-store recount or generic JSON materializer.
class EmuirView {
 public:
  explicit EmuirView(const ArtifactRef& artifact);
  // Phase-specific ordered joins borrow this same owner, not a second source
  // connection/cache with an independent lifetime or schema interpretation.
  SqliteStore& store() { store_.RequireUnchanged();return store_; }
  const EmuirCounts& counts() const { return counts_; }
  EmuirNetId Net(std::uint64_t sequence) const;
  std::string NetName(const EmuirNetId& net);
  void VisitCellStates(const std::function<void(EmuirCellId, bool)>& visitor);
  void VisitConnectivity(const std::function<void(const EmuirEndpoint&)>& endpoint,
                         const std::function<void(EmuirNetId)>& net_end,
                         bool validate_pins = false);
 private:
  SqliteStore store_;
  SqliteDatabase& database_;
  EmuirCounts counts_{};
  bool compact_=true;
};
}  // namespace emuflow::core
