#pragma once
#include "common/stage_runtime.hpp"
#include <vector>
namespace emuflow::core {
void ExportOpenstaInputs(const std::vector<ArtifactRef>& inputs,
                        const std::string& summary,
                        const std::string& verilog,
                        const std::string& net_map);
// JSON integer/string semantics shared by Xilinx timing producers. Booleans
// and reals are not integer parameters; binary strings have priority.
bool TimingParameterIsOne(const std::string& kind, std::int64_t integer,
                          const std::string& text);
}
