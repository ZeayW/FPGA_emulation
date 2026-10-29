#pragma once
#include "common/tool_runtime.hpp"
#include <filesystem>
namespace emuflow::core {
struct FrontendReportConfiguration {
  std::string platform, synthesis, input_sha256;
  bool require_no_fabric_clock = false;
  std::vector<ArtifactRef> sources;
};
std::string
BuildFrontendReport(const std::filesystem::path &root,
                    const FrontendReportConfiguration &configuration,
                    const std::string &importer,
                    const ToolResult *tool = nullptr);
} // namespace emuflow::core
