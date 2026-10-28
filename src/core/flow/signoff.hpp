#pragma once

#include <filesystem>
#include <csignal>
#include <string>
#include <map>
#include <vector>

namespace emuflow::core {

// Publish the model-only Phase 7C contract directly from the typed schedule
// and bounded Phase 3--6 summaries. Physical evidence remains owned by the
// selected physical backend and is handled by the physical signoff path.
void SignoffNonphysical(const std::filesystem::path& output);
void SignoffPhysical(const std::filesystem::path& output,
                     const std::string& opensta_executable,
                     const volatile std::sig_atomic_t* cancelled=nullptr,
                     const std::string& launcher_reference="");
std::string RuntimeSignoffXdc(const std::map<std::string,std::string>& runtime);
std::string RuntimeSignoffTestbench(const std::map<std::string,std::string>& runtime,
                                  const std::vector<std::string>& fpgas);

}  // namespace emuflow::core
