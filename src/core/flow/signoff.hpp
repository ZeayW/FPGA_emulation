#pragma once

#include <filesystem>

namespace emuflow::core {

// Publish the model-only Phase 7C contract directly from the typed schedule
// and bounded Phase 3--6 summaries. Physical evidence remains owned by the
// selected physical backend and is handled by the physical signoff path.
void SignoffNonphysical(const std::filesystem::path& output);

}  // namespace emuflow::core
