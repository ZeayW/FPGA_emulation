#pragma once
#include "emuflow_core/patron_model.hpp"
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
struct PatronBuildContext {
  std::vector<std::uint32_t> native_to_artifact;
  // Source-only communication export keeps numeric ownership/net coordinates;
  // no selected TimingPathDB is needed to find an actual crossing.
  std::vector<std::uint32_t> source_cell_cluster;
  std::vector<int> source_net_native;
};
std::vector<std::uint32_t> CanonicalPatronClusterOrder(std::uint32_t count);
patron::Model BuildPatronModel(const std::vector<ArtifactRef>& inputs,
                              PatronBuildContext& context, bool read_paths = true);
void RefinePatronFromPrimary(const std::vector<ArtifactRef>& inputs,
                           const std::string& output);
}  // namespace emuflow::core
