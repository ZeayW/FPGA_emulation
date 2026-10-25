#pragma once
#include "emuflow_core/store.hpp"
#include <array>
#include <memory>

namespace emuflow::core {
class EmuirView;
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
  // Ownership is indexed by the paired EmuIR source sequence; membership/name
  // columns use a separate lexical ordinal in the primary artifact namespace.
  std::uint32_t Owner(std::uint64_t source_sequence) const;
  CellId Cell(std::uint64_t primary_ordinal) const;
  std::string Name(const CellId& primary_cell) const;
  CellId Member(std::uint64_t cluster, std::uint64_t member) const;
  void ValidateMembers(EmuirView& source) const;
  std::uint32_t Weight(std::uint64_t cluster, unsigned dimension) const;
  const std::array<std::uint64_t,13>& totals() const;
  // Retire pages from a completed projection before constructing another one.
  void ReleasePages() const;
 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}
