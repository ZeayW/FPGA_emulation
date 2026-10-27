#pragma once
#include "partition/patron_model.hpp"
#include "common/stage_runtime.hpp"

namespace emuflow::core {
struct PatronBuildContext {
  std::vector<std::uint32_t> native_to_artifact;
};
std::vector<std::uint32_t> CanonicalPatronClusterOrder(std::uint32_t count);
patron::Model BuildPatronModel(const std::vector<ArtifactRef>& inputs,
                              PatronBuildContext& context);
void RefinePatronFromPrimary(const std::vector<ArtifactRef>& inputs,
                           const std::string& output);
}  // namespace emuflow::core
