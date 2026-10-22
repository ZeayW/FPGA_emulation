#include <exception>
#include <iostream>
#include <string>

#include "emuflow_core/native_stages.hpp"

namespace {

constexpr const char* kUsage =
    "usage:\n"
    "  emuflow-native phase3 analyze INPUT OUTPUT\n"
    "  emuflow-native phase5 schedule INPUT OUTPUT\n";

int Dispatch(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << kUsage;
    return 0;
  }
  if (argc != 5) {
    std::cerr << kUsage;
    return 2;
  }
  const std::string phase = argv[1];
  const std::string operation = argv[2];
  if (phase == "phase3" && operation == "analyze") {
    emuflow::native::AnalyzeStaticExact(argv[3], argv[4]);
    return 0;
  }
  if (phase == "phase5" && operation == "schedule") {
    emuflow::native::ScheduleStaticExact(argv[3], argv[4]);
    return 0;
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
