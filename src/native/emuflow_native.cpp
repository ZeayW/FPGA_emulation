#include <exception>
#include <iostream>
#include <string>

#include "emuflow_core/native_stages.hpp"

namespace {

constexpr const char* kUsage =
    "usage:\n"
    "  emuflow-native phase3 analyze INPUT OUTPUT\n"
    "  emuflow-native phase5 schedule INPUT OUTPUT\n"
    "  emuflow-native phase6 assign-banks "
    "(--typed-input|--legacy-text) INPUT OUTPUT\n";

int Dispatch(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << kUsage;
    return 0;
  }
  const std::string phase = argc > 1 ? argv[1] : "";
  const std::string operation = argc > 2 ? argv[2] : "";
  if (argc == 5 && phase == "phase3" && operation == "analyze") {
    emuflow::native::AnalyzeStaticExact(argv[3], argv[4]);
    return 0;
  }
  if (argc == 5 && phase == "phase5" && operation == "schedule") {
    emuflow::native::ScheduleStaticExact(argv[3], argv[4]);
    return 0;
  }
  if (argc == 6 && phase == "phase6" && operation == "assign-banks") {
    const std::string mode = argv[3];
    if (mode == "--typed-input" || mode == "--legacy-text") {
      emuflow::native::AssignChimewBankChannels(
          argv[4], argv[5], mode == "--typed-input");
      return 0;
    }
  }
  std::cerr << kUsage;
  return 2;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    return Dispatch(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "emuflow-native: " << error.what() << '\n';
    return 1;
  }
}
