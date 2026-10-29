#pragma once
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core {
// Index, generation-bound request, then four immutable streams per observed
// partition: TX costs, enabled-fanin ledger, capture costs, missing RX roles.
// Missing partitions remain explicit pending observations. Successful parsing
// or a setup-model projection is not physical/global-STA acceptance.
void AnalyzeNativeWordNetwork(const std::vector<ArtifactRef>& inputs,
                              const std::string& output,bool verification_events = false,
                              bool arrival_bounds = false);
}  // namespace emuflow::core
