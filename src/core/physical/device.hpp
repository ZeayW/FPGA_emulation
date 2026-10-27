#pragma once
#include "storage/store.hpp"
#include <csignal>
#include <functional>
#include <memory>
#include <optional>
#include <set>
#include <variant>

namespace emuflow::core {
using PhysicalBit = std::variant<std::int64_t, std::string>;
struct PhysicalAssignment {
  std::string instance, type, bel;
};
struct SitePinUse {
  std::int64_t bit;
  std::string instance, bel;
};
struct SitePinConflict {
  std::string resource;
  std::vector<SitePinUse> uses;
};
struct PhysicalSite {
  std::string name, type, base, slr, clock_region, region_json;
  std::int64_t x = 0, y = 0;
};
struct PhysicalBel {
  std::string bel, mode, physical_site;
};
// A temporary SQLite projection of original device facts. No host-built site
// catalog or inferred BRAM coordinates participate in these lookups.
class PhysicalDevice {
public:
  PhysicalDevice(SqliteDatabase &scratch, const ArtifactRef &architecture,
                 const ArtifactRef *native_constraints = nullptr,
                 const ArtifactRef *provider = nullptr,
                 const volatile std::sig_atomic_t *cancelled = nullptr);
  ~PhysicalDevice();
  PhysicalSite Site(const std::string &name);
  PhysicalBel ResolveBel(const std::string &site, const std::string &bel,
                         const std::string &type,
                         const std::set<std::string> &declared_modes);
  SqliteDatabase &database();
  const std::string &part() const;
  const std::string &architecture_sha() const;
  std::string native_sha() const;
  std::string provider_sha() const;
  void RequireUnchanged() const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
// Projects optional graph-sized fixed constraints with bounded entry memory.
void ImportPhysicalPlacementConstraints(
    SqliteDatabase &scratch, const ArtifactRef *source,
    const volatile std::sig_atomic_t *cancelled = nullptr);
class MappedPhysicalSemantics {
public:
  explicit MappedPhysicalSemantics(const ArtifactRef &mapped);
  ~MappedPhysicalSemantics();
  std::string CellType(const std::string &instance);
  std::string Parameters(const std::string &instance);
  std::vector<PhysicalBit> Bits(const std::string &instance,
                                const std::string &port);
  std::optional<std::string> Driver(std::int64_t bit);
  std::vector<std::string>
  ControlSets(const std::vector<PhysicalAssignment> &assignments);
  std::vector<SitePinConflict>
  SitePinConflicts(const std::vector<PhysicalAssignment> &assignments);
  void ValidateSlice(const std::vector<PhysicalAssignment> &assignments);
  // Emits the canonical source-derived certificate in stable type/head order.
  void DeriveCascades(const std::function<void(const std::string &)> &emit,
                      const volatile std::sig_atomic_t *cancelled = nullptr);
  void RequireUnchanged() const;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
} // namespace emuflow::core
