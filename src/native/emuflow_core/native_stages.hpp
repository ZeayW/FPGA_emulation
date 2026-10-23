#pragma once

#include <string>

namespace emuflow::native {

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
                               const std::string& output_path);
void HashStaticExactSemantics(const std::string& nodes_path,
                              const std::string& captures_path,
                              const std::string& request_path,
                              const std::string& output_path);
void WriteStaticExactSections(const std::string& nodes_path,
                              const std::string& captures_path,
                              const std::string& request_path,
                              const std::string& output_path);
void RouteTlr(const std::string& input_path,
              const std::string& output_path,
              bool typed_output = false);
void ScheduleStaticExact(const std::string& input_path,
                         const std::string& output_path);
void AssignChimewBankChannels(const std::string& input_path,
                              const std::string& output_path,
                              bool typed_input);
void GroupChimewSignals(const std::string& input_path,
                        const std::string& output_path, bool typed_input,
                        const std::string& expected_sha256);
void RefineChimewPositions(const std::string& input_path,
                           const std::string& output_path, bool typed_input,
                           const std::string& expected_sha256);
void EvaluateChimewRudy(const std::string& input_path,
                        const std::string& output_path);

}  // namespace emuflow::native
