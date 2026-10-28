#pragma once

#include "common/stage_runtime.hpp"
#include "common/tool_runtime.hpp"
#include "physical/board_timing.hpp"
#include "physical/evidence_catalog.hpp"

#include <csignal>
#include <filesystem>
#include <string>

namespace emuflow::core {

struct PhysicalGlobalStaResult {
  ArtifactRef events;
  ArtifactRef index;
  ArtifactRef measurements;
  std::string event_binding;
  std::string index_header;
  std::string measurement_header;
  std::filesystem::path model_directory;
  std::filesystem::path opensta_log;
  ToolResult engine;
  std::string executable_sha256, launcher_sha256;
  std::vector<ArtifactRef> engine_sources;
};

PhysicalGlobalStaResult RunPhysicalGlobalSta(
    const ArtifactRef &routes, const ArtifactRef &schedule,
    const ArtifactRef &board, const PhysicalEvidenceCatalog &physical,
    const PhysicalBoardTiming &board_timing, const std::string &runtime_json,
    const std::string &opensta_executable,
    const std::filesystem::path &workspace,
    const volatile std::sig_atomic_t *cancelled = nullptr,
    const std::string &launcher_reference = "");

} // namespace emuflow::core
