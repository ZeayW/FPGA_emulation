// Small explicit qualifier for the native kernel. This is not a flow adapter.
#include "emuflow_core/word_events.hpp"
#include <iostream>
#include <stdexcept>
#include <string>

int main() {
  try {
    using namespace emuflow::core;
    std::string mode;
    if (!(std::cin >> mode) || (mode != "project" && mode != "check")) throw std::runtime_error("invalid test mode");
    const auto number = [] { std::string text; if (!(std::cin >> text)) throw std::runtime_error("truncated test input"); return std::stod(text); };
    PhysicalWordModel model;
    model.period_ns = number(); model.epoch_ns = number();
    unsigned roots, events;
    if (!(std::cin >> model.frame_slots >> roots >> events) || roots > 100000 || events > 100000) throw std::runtime_error("invalid test population");
    for (unsigned i=0; i<roots; ++i) model.root_launch_ns.push_back(number());
    for (unsigned i=0; i<events; ++i) {
      PhysicalWordTransfer transfer{}; unsigned stable, count;
      if (!(std::cin >> transfer.phase_word >> transfer.ratio >> transfer.fixed_latency_words >> stable >> count) || stable > 1 || count > 100000)
        throw std::runtime_error("invalid test transfer");
      transfer.configuration_stable = stable;
      for (unsigned j=0; j<count; ++j) {
        unsigned event, index;
        if (!(std::cin >> event >> index) || event > 1) throw std::runtime_error("invalid test fanin");
        const auto data = number(), cost = number();
        transfer.fanins.push_back({event != 0,index,data,cost});
      }
      model.transfers.push_back(std::move(transfer));
    }
    if (mode == "check") {
      std::vector<std::uint32_t> selected;std::vector<double> slacks;
      unsigned count;
      if (!(std::cin >> count) || count > 100000) throw std::runtime_error("invalid test selection");
      for (unsigned i=0; i<count; ++i) { unsigned value;if (!(std::cin >> value)) throw std::runtime_error("truncated selection");selected.push_back(value); }
      if (!(std::cin >> count) || count > 100000) throw std::runtime_error("invalid test measurements");
      for (unsigned i=0; i<count; ++i) slacks.push_back(number());
      ValidatePhysicalWordEvents(model, selected, slacks, 1e-5);
      std::cout << "true\n";return 0;
    }
    const auto selected = ProjectPhysicalWordEvents(model);
    std::cout << "[";
    for (std::size_t i=0; i<selected.size(); ++i) {
      if (i) std::cout << ",";
      std::cout << selected[i];
    }
    std::cout << "]\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\n";
    return 1;
  }
}
