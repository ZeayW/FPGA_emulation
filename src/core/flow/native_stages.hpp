#pragma once

#include <csignal>
#include <string>
#include <ostream>
#include "common/stage_runtime.hpp"

namespace emuflow::core { struct ExactScheduleModel; struct ExactScheduleSolution; }

namespace emuflow::native {
core::ExactScheduleSolution ComputeStaticExactSchedule(const core::ExactScheduleModel& model);

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
              const std::string& output_path,
              const volatile std::sig_atomic_t* cancelled = nullptr);
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
