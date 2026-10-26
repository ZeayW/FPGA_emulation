#pragma once
#include "emuflow_core/store.hpp"
#include <array>
#include <map>
#include <optional>

namespace emuflow::core {
// The physical assembler has already checked the proof owner and its seals.
// Carry those exact generations into the native consumer; a fresh snapshot
// cannot renew an older checked receipt. This is not an external-proof import.
struct CheckedOptimizationContext {
  std::string fpga,mapped_sha256,synthesized_checkpoint_sha256;
  std::array<FileGeneration,3> generations; // proof, mapped, lineage
};
struct EliminationSourceTag {};
enum class EliminationReason { kUnobservable, kConstantState };
struct EliminatedSourceFact {
  ArtifactId<EliminationSourceTag> source;
  EliminationReason reason;
  std::optional<int> constant_value;
  std::string initialization,source_cell;
};
// Read only requested canonical facts, plus bounded vendor confirmations.
// Source closure/induction/function checks remain owned by the checked producer;
// this consumer does not rehash/rebuild its graph or promote conditional rows.
class NativeOptimizationProofView {
 public:
  NativeOptimizationProofView(const ArtifactRef& proof,const ArtifactRef& mapped,
      const ArtifactRef& lineage,const CheckedOptimizationContext& context);
  EliminatedSourceFact Require(const std::string& reference,
      const std::vector<std::string>& endpoint_cells);
  void RequireOwner(const EliminatedSourceFact& fact) const;
  void RequireUnchanged() const;
  const ArtifactRef& artifact() const { return proof_.artifact(); }
  const std::string& fpga() const { return context_.fpga; }
  std::array<ArtifactRef,3> artifacts() const { return {proof_.artifact(),mapped_.artifact(),lineage_.artifact()}; }
 private:
  SqliteStore proof_,mapped_,lineage_;
  CheckedOptimizationContext context_;
  std::map<std::uint64_t,int> confirmed_;
  void ReadConfirmation();
};
// Logical physical-query spelling doubles literal backslashes. Top-level
// objects have no cell identity and must be excluded by the caller's kind.
std::string LogicalTimingCell(const std::string& pin);
}  // namespace emuflow::core
