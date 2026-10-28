#pragma once
#include "common/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// Raw native coordinates -> FF legalization -> typed certificate ->
// independently audited physical handoff. The host exchanges locators and
// bounded metadata.
StageResult
ImportOpenparfPlacement(const ArtifactRef &request, const std::string &report,
                        const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
