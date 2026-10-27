#pragma once
#include "emuflow_core/stage_runtime.hpp"
#include <csignal>
namespace emuflow::core {
// merged compact EmuIR, sealed boundary store, selector, CRC request.
StageResult WriteVivadoBoundaryQuery(const std::vector<ArtifactRef>& inputs,
    const std::string& output,const volatile std::sig_atomic_t* cancelled=nullptr);
// mapped index, source-function signatures, routed lineage, source TSV, CRC
// request. All descendants are retained in deterministic NAME order.
StageResult ResolveVivadoTimingQuery(const std::vector<ArtifactRef>& inputs,
    const std::string& output,const volatile std::sig_atomic_t* cancelled=nullptr);
}
