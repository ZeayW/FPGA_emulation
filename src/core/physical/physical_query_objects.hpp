#pragma once

#include "common/stage_runtime.hpp"
#include <cstdint>
#include <memory>
#include <optional>
#include <string>

namespace emuflow::core {
// Logical endpoint identity belongs to the caller's source artifact. An empty
// instance is a top-level port, never a synthetic cell-name convention.
struct PhysicalEndpoint {
  std::optional<std::string> instance;
  std::string port;
  std::int64_t bit=0;
  std::optional<std::string> object;
};
struct PhysicalObject {
  std::string kind;  // "port" or "pin"
  std::string name;
};

// Query only requested compact EmuIR objects. The resolver owns its source
// lease and at most one cell's pins/parameters; it never constructs a global
// name/parameter map or requires a later mapped/placed artifact.
class PhysicalQueryObjects {
 public:
  PhysicalQueryObjects(const ArtifactRef& merged, const std::string& provider,
                       const ArtifactRef* primitive_library=nullptr);
  ~PhysicalQueryObjects();
  PhysicalQueryObjects(const PhysicalQueryObjects&)=delete;
  PhysicalQueryObjects& operator=(const PhysicalQueryObjects&)=delete;

  // A cross-partition endpoint need not exist in this merged owner yet.
  // Normalize only applies the conditional RAMB18 physical-pin alias.
  PhysicalEndpoint Normalize(const PhysicalEndpoint& endpoint);
  // Resolve, unlike Normalize, requires the exact local port/cell/pin.
  PhysicalObject Resolve(const PhysicalEndpoint& endpoint);
  bool IsArchitecturalState(const std::string& instance);
  std::string CellType(const std::string& instance);
  void RequireUnchanged() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

std::string VivadoMappedName(const std::string& logical_name);
}  // namespace emuflow::core
