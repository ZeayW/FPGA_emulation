#pragma once

#include <string>

namespace emuflow::native {

void AnalyzeStaticExact(const std::string& input_path,
                        const std::string& output_path);
void ScheduleStaticExact(const std::string& input_path,
                         const std::string& output_path);
void AssignChimewBankChannels(const std::string& input_path,
                              const std::string& output_path,
                              bool typed_input);

}  // namespace emuflow::native
