#pragma once
#include <cstdint>
#include <string>
namespace emuflow::core {
struct PhysicalReportFacts {
  std::string summary;
  std::string metrics;
};
std::string ValidatePhysicalPartitionReport(const std::string &document,
                                            const std::string &backend,
                                            const std::string &fpga,
                                            const std::string &part,
                                            std::int64_t original,
                                            std::int64_t transport);
// Bounded per-FPGA reports only. Circuit identities remain in typed owners.
// Shared numerical physical/runtime validation used by implementation and
// signoff.
std::string ValidatePhysicalSummaryReport(const std::string &summary,
                                          const std::string &board,
                                          const std::string &runtime);
PhysicalReportFacts
ValidatePhysicalFlowReport(const std::string &document,
                           const std::string &physical_summary = "");
} // namespace emuflow::core
