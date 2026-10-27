#pragma once
#include "common/stage_runtime.hpp"
#include <vector>
#include <csignal>

namespace emuflow::core {
// Consume current assignment/schedule owners plus bounded board configuration.
// Pin-plan entries, when selected, remain in their typed source store.
StageResult BuildTransportArtifacts(const std::vector<ArtifactRef>& inputs,
                                   const std::string& output_directory);
StageResult AuditTransportArtifacts(const ArtifactRef& artifact,const std::string& summary);
// One baseline Phase 6 generation: source-bound split, transport, independent
// certificates, engine inputs, manifest and report. No intermediate is exposed.
StageResult RunPhase6(const std::vector<ArtifactRef>& inputs,
                     const std::string& output_directory,
                     const std::vector<ArtifactRef>& enclosing_sources={},
                     const volatile std::sig_atomic_t* cancelled=nullptr);
StageResult CompilePhase6(const std::vector<ArtifactRef>& inputs,
                         const std::string& output_directory,
                         const volatile std::sig_atomic_t* cancelled=nullptr);
}
