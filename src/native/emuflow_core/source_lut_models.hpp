#pragma once
#include "emuflow_core/store.hpp"

namespace emuflow::core {
// Reduce original LUT occurrences to their producer-owned parameter catalog.
// Consumers join by original type/parameter identity, never by an instance
// dictionary reconstructed in Python. INIT remains exact, including bit order.
void BuildSourceLutModels(const std::vector<ArtifactRef>& inputs,
                          const std::string& output);
}
