#include "emuflow_core/native_word_costs.hpp"
#include "emuflow_core/json_record.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  try {
    using namespace emuflow::core;
    if (argc != 2 || (std::string(argv[1]) != "rows" && std::string(argv[1]) != "summary")) return 2;
    const bool rows = std::string(argv[1]) == "rows";
    const auto counts = VisitNativeWordCosts(std::cin,[&](const NativeWordCost& row) {
      if (!rows) return;
      JsonRecord evidence;
      if (row.evidence) {
        const auto& e = *row.evidence;
        evidence = JsonRecord(std::map<std::string,JsonRecord>{{"requirement_ns",e.requirement_ns},{"data_ns",e.data_ns},
            {"sampling_cost_ns",e.sampling_cost_ns},{"clock_adjustment_ns",e.clock_adjustment_ns},
            {"start_pin",e.start_pin},{"end_pin",e.end_pin},{"start_clock",e.start_clock},
            {"end_clock",e.end_clock},{"launch_ns",e.launch_edge_ns},{"capture_ns",e.capture_edge_ns}});
      }
      std::cout << JsonRecord(std::map<std::string,JsonRecord>{{"phase_word",row.phase_word},{"query",row.query},
          {"source",row.source},{"source_kind",static_cast<int>(row.source_kind)},
          {"launch_edge",static_cast<int>(row.launch_edge)},
          {"measurement",static_cast<int>(row.measurement)},{"evidence",evidence}}).Encode() << "\n";
    });
    std::cout << JsonRecord(std::map<std::string,JsonRecord>{{"rows",counts.rows},{"measured",counts.measured},
        {"no_timed_path",counts.no_timed_path},{"context_required",counts.context_required}}).Encode() << "\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\n";
    return 1;
  }
}
