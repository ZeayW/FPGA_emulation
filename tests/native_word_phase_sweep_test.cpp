#include "emuflow_core/word_phase_sweep.hpp"
#include "emuflow_core/json_record.hpp"
#include <iostream>
#include <string>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;
    if (argc != 2 || (std::string(argv[1]) != "details" && std::string(argv[1]) != "summary")) return 2;
    std::int64_t period;
    std::uint64_t bound,count;
    if (!(std::cin >> period >> bound >> count) || count > 100000) throw std::runtime_error("invalid sweep fixture");
    std::vector<WordPhaseTransfer> nodes;
    for (std::uint64_t i=0; i<count; ++i) {
      WordPhaseTransfer node{};
      unsigned root,capture;std::int64_t root_cost,capture_cost;std::uint64_t fanins;
      if (!(std::cin >> node.phase_word >> node.ratio_serial_uis >> node.fixed_latency_words >>
            root >> root_cost >> capture >> capture_cost >> fanins) || root > 1 || capture > 1 || fanins > 100000)
        throw std::runtime_error("invalid sweep transfer");
      if (root) node.root_sampling_cost_ps = root_cost;
      if (capture) node.capture_sampling_cost_ps = capture_cost;
      for (std::uint64_t j=0; j<fanins; ++j) {
        WordPhaseRelay edge{};
        if (!(std::cin >> edge.source >> edge.sampling_cost_ps)) throw std::runtime_error("invalid sweep dependency");
        node.fanins.push_back(edge);
      }
      nodes.push_back(std::move(node));
    }
    const auto result = SweepContinuousWordPhases(period,nodes,bound);
    JsonRecord cost,witness;
    std::vector<JsonRecord> arrivals;
    if (std::string(argv[1]) == "details")
      for (const auto& value:result.maximum_arrival_ps)
        arrivals.push_back(value ? JsonRecord(*value) : JsonRecord());
    if (result.maximum_capture_cost_ps) cost = JsonRecord(*result.maximum_capture_cost_ps);
    if (result.witness && std::string(argv[1]) == "details") {
      const auto& w = *result.witness;
      std::vector<JsonRecord> words;
      for (const auto& load:w.load_words) words.push_back(load ? JsonRecord(*load) : JsonRecord());
      witness = JsonRecord(std::map<std::string,JsonRecord>{{"phase_word",w.phase_word},
          {"lower_offset_ps",w.lower_offset_ps},{"upper_offset_ps",w.upper_offset_ps},
          {"capture_transfer",w.capture_transfer},{"load_words",JsonRecord(std::move(words))}});
    }
    std::cout << JsonRecord(std::map<std::string,JsonRecord>{{"maximum_capture_cost_ps",cost},
        {"repeat_words",result.repeat_words},{"phase_cells",result.phase_cells},
        {"maximum_arrival_ps",JsonRecord(std::move(arrivals))},
        {"node_recomputations",result.node_recomputations},{"witness",witness}}).Encode() << "\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\n";return 1;
  }
}
