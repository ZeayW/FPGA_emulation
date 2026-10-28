#pragma once

#include <filesystem>

namespace emuflow::core {

// Seal the terminal report from bounded stage summaries and typed artifacts.
// Circuit records stay in their owning stores and are never materialized here.
void FinalizeFlow(const std::filesystem::path& output);

}  // namespace emuflow::core
