#pragma once

#include <string>
#include <ostream>

namespace emuflow::core { struct ExactScheduleModel; struct ExactScheduleSolution; }

namespace emuflow::native {
core::ExactScheduleSolution ComputeStaticExactSchedule(const core::ExactScheduleModel& model);

void RefinePatron(const std::string& input_path, const std::string& output_path,
                  bool summary_output, bool typed_output);

void AnalyzeStaticExact(const std::string& input_path,
                        const std::string& output_path,
                        const std::string& indexed_path = "");
void FinalizeStaticExact(const std::string& topology_path,
                        const std::string& metadata_path,
                        const std::string& output_path);
void ResolveStaticExactCaptures(const std::string& source_path,
                               const std::string& request_path,
                               const std::string& topology_path,
                               const std::string& parts_path,
                               const std::string& output_path,
                               bool legacy_index = true);
void HashStaticExactSemantics(const std::string& nodes_path,
                              const std::string& captures_path,
                              const std::string& request_path,
                              const std::string& output_path);
void WriteStaticExactSections(const std::string& nodes_path,
                              const std::string& captures_path,
                              const std::string& request_path,
                              const std::string& output_path);
void RouteTlr(const std::string& input_path,
              const std::string& output_path);
void AssignChimewBankChannels(const std::string& input_path,
                              std::ostream& output);
void GroupChimewSignals(const std::string& input_path,
                        std::ostream& output,
                        const std::string& expected_sha256);
void RefineChimewPositions(const std::string& input_path,
                           std::ostream& output,
                           const std::string& expected_sha256);
void EvaluateChimewRudy(const std::string& input_path,
                        std::ostream& output);

}  // namespace emuflow::native
