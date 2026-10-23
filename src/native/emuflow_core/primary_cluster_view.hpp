#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <array>
#include <memory>

namespace emuflow::core {
// Immutable, source-bound primary columns shared by native phase consumers.
// Local ordinals belong to this view; they are never IDs in another artifact.
class PrimaryClusterView {
 public:
  PrimaryClusterView(const ArtifactRef& primary, const ArtifactRef& source);
  ~PrimaryClusterView();
  PrimaryClusterView(const PrimaryClusterView&) = delete;
  PrimaryClusterView& operator=(const PrimaryClusterView&) = delete;
  std::uint64_t cells() const;
  std::uint64_t clusters() const;
  std::uint32_t Owner(std::uint64_t cell) const;
  std::uint32_t Weight(std::uint64_t cluster, unsigned dimension) const;
  const std::array<std::uint64_t,13>& totals() const;
  // Retire pages from a completed projection before constructing another one.
  void ReleasePages() const;
 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}
