#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
struct RoutedOpenstaModel {
  std::string model_json, liberty;
  std::vector<std::string> hard_blocks;
};
RoutedOpenstaModel BuildRoutedOpenstaModel(const ArtifactRef &base_model,
                                           const std::string &coefficients,
                                           const std::string &pin_sets);
} // namespace emuflow::core
