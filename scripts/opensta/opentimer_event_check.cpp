// Offline third-engine qualification, linked against upstream OpenTimer.
// Usage: event-check CHECK_COUNT (working directory: exported timing model).
// This reads the same raw Liberty/Verilog/SDC as OpenSTA; no composed Python
// arrival or slack is supplied to this engine.
#include <ot/timer/timer.hpp>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
  try {
    if (argc != 2) throw std::runtime_error("expected check count");
    const auto count = std::stoul(argv[1]);
    if (!count) throw std::runtime_error("empty check population");
    ot::Timer timer;
    timer.set_num_threads(1)
         .set_time_unit(ot::second_t(1e-9))
         .read_celllib("global_timing.lib")
         .read_verilog("global_timing.v")
         .read_sdc("global_timing.sdc");
    timer.update_timing();
    std::ofstream out("opentimer-measurements.tsv");
    out << "endpoint\tarrival_ns\trequired_ns\tslack_ns\n"
        << std::setprecision(17);
    for (size_t i = 0; i < count; ++i) {
      const auto pin = "o" + std::to_string(i);
      float worst = INFINITY, arrival = 0, required = 0;
      for (auto edge : {ot::RISE, ot::FALL}) {
        const auto at = timer.report_at(pin, ot::MAX, edge);
        const auto rat = timer.report_rat(pin, ot::MAX, edge);
        const auto slack = timer.report_slack(pin, ot::MAX, edge);
        if (!at || !rat || !slack || !std::isfinite(*at) ||
            !std::isfinite(*rat) || !std::isfinite(*slack))
          throw std::runtime_error("missing/nonfinite endpoint: " + pin);
        if (*slack < worst) {
          worst = *slack; arrival = *at; required = *rat;
        }
      }
      out << pin << '\t' << arrival << '\t' << required << '\t' << worst << '\n';
    }
    if (!out) throw std::runtime_error("cannot write measurements");
  } catch (const std::exception& e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
