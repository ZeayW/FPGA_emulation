#include "emuflow_core/native_word_tx_binding.hpp"
#include "emuflow_core/json_record.hpp"
#include <iomanip>
#include <iostream>
#include <limits>

int main() {
  try {
    using namespace emuflow::core;
    NativeWordTxCatalog catalog;
    std::uint64_t entries,clocks,rx,contexts,periods;
    if (!(std::cin >> catalog.maximum_repeat_words >> catalog.maximum_contexts >> entries >> clocks >> rx >> contexts >> periods) ||
        entries>100000 || clocks>1000 || rx>100000 || contexts>1000000 || periods>32) throw std::runtime_error("invalid binding fixture");
    for (std::uint64_t i=0;i<periods;++i) {
      std::uint32_t period;if (!(std::cin>>period))throw std::runtime_error("invalid counter calendar fixture");
      catalog.calendar_period_words.push_back(period);
    }
    for (std::uint64_t i=0;i<entries;++i) {
      NativeWordTxEntry entry;
      if (!(std::cin >> std::quoted(entry.id) >> std::quoted(entry.query) >> entry.phase_word >> entry.ratio_serial_uis))
        throw std::runtime_error("invalid entry fixture");
      catalog.entries.push_back(std::move(entry));
    }
    for (std::uint64_t i=0;i<clocks;++i) {
      std::string name;if (!(std::cin >> std::quoted(name))) throw std::runtime_error("invalid clock fixture");
      catalog.dut_clocks.push_back(name);
    }
    for (std::uint64_t i=0;i<rx;++i) {
      std::string origin,entry;
      if (!(std::cin >> std::quoted(origin) >> std::quoted(entry)) || !catalog.rx_entries.emplace(origin,entry).second)
        throw std::runtime_error("invalid RX fixture");
    }
    std::vector<NativeWordTxFaninCounts> fanins;
    for (std::uint64_t i=0;i<contexts;++i) {
      NativeWordTxFaninCounts f;
      if (!(std::cin >> f.phase_word >> std::quoted(f.query) >> f.rx >> f.infrastructure >> f.ports >> f.unclassified))
        throw std::runtime_error("invalid fanin fixture");
      fanins.push_back(std::move(f));
    }
    std::cin.ignore(std::numeric_limits<std::streamsize>::max(),'\n');
    const auto result = BindNativeWordTxCosts(catalog,fanins,std::cin);
    std::vector<JsonRecord> bound;
    for (const auto& entry:result.entries) {
      std::vector<JsonRecord> rows;
      for (const auto& context:entry.contexts) {
        for (const auto& row:context.costs) rows.emplace_back(std::map<std::string,JsonRecord>{
            {"phase",context.phase_word},{"source",row.observation.source},{"incoming_entry",row.incoming_entry},
            {"cost_ps",row.sampling_cost_ps ? JsonRecord(*row.sampling_cost_ps) : JsonRecord()}});
      }
      bound.emplace_back(std::map<std::string,JsonRecord>{{"id",entry.source.id},{"contexts",entry.contexts.size()},
          {"data_phase_invariant",entry.data_phase_invariant ? JsonRecord(*entry.data_phase_invariant) : JsonRecord()},
          {"rows",JsonRecord(std::move(rows))}});
    }
    std::cout << JsonRecord(std::map<std::string,JsonRecord>{{"repeat_words",result.repeat_words},
        {"expected_contexts",result.expected_contexts},{"observed_contexts",result.observed_contexts},
        {"unbound_source_rows",result.unbound_source_rows},{"untimed_rx_rows",result.untimed_rx_rows},
        {"rows",result.rows.rows},{"entries",JsonRecord(std::move(bound))}}).Encode()<<"\n";
    return 0;
  } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
