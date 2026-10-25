#pragma once

#include <string>
#include <ostream>
#include "emuflow_core/stage_runtime.hpp"

namespace emuflow::core { struct ExactScheduleModel; struct ExactScheduleSolution; }

namespace emuflow::native {
void RefinePartitionHops(const std::vector<core::ArtifactRef>& inputs,
                         const std::string& output_path, bool retain_trace);
core::ExactScheduleSolution ComputeStaticExactSchedule(const core::ExactScheduleModel& model);

void RefinePatron(const std::vector<core::ArtifactRef>& inputs, const std::string& output_path,
                  bool summary_output, bool typed_output);

void AnalyzeStaticExact(const std::vector<core::ArtifactRef>& inputs,
                        const std::string& output_path);
void FinalizeStaticExact(const std::vector<core::ArtifactRef>& inputs,
                        const std::string& output_path);
void BuildStaticExactCaptureRecords(const std::vector<core::ArtifactRef>& inputs,
                               const std::string& output_path);
void HashStaticExactSemantics(const std::vector<core::ArtifactRef>& inputs,
                              const std::string& output_path);
void WriteStaticExactSections(const std::vector<core::ArtifactRef>& inputs,
                              const std::string& output_path);
void RouteTlr(const std::vector<core::ArtifactRef>& inputs,
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
