#pragma once
#include "common/stage_runtime.hpp"
namespace emuflow::core {
// Import one complete Vivado measurement population and publish the existing
// immutable store plus selector. The enclosing backend owns a fresh output
// root.
std::string ImportVivadoMeasurement(const ArtifactRef &identity_selector,
                                    const ArtifactRef &measurements,
                                    unsigned kind, const std::string &output,
                                    bool board_interface,
                                    std::vector<ArtifactRef> &sources);
ArtifactRef BindPhysicalIdentity(const ArtifactRef &selector, unsigned kind,
                                 std::vector<ArtifactRef> &sources);
} // namespace emuflow::core
