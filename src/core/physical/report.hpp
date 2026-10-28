#pragma once
#include <string>
namespace emuflow::core {
struct PhysicalReportFacts {
  std::string summary;
  std::string metrics;
};
// Bounded per-FPGA reports only. Circuit identities remain in typed owners.
PhysicalReportFacts ValidatePhysicalFlowReport(const std::string &document);
} // namespace emuflow::core
