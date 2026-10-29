#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
// Shared deterministic scalar Liberty renderer for pre-placement and routed
// timing models. Primitive pin roles and finite delays are checked here.
std::string RenderOpenstaLiberty(const std::string &model);
struct RoutedOpenstaModel {
  std::string model_json, liberty;
  std::vector<std::string> hard_blocks;
};
RoutedOpenstaModel BuildRoutedOpenstaModel(const ArtifactRef &base_model,
                                           const std::string &coefficients,
                                           const std::string &pin_sets);
} // namespace emuflow::core
