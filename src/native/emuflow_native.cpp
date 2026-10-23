#include <exception>
#include <iostream>
#include <string>

#include "emuflow_core/native_stages.hpp"

namespace {

constexpr const char* kUsage =
    "usage:\n"
    "  emuflow-native phase3 analyze INPUT OUTPUT\n"
    "  emuflow-native phase3 resolve-captures EMUIR REQUEST TOPOLOGY PARTS OUTPUT\n"
    "  emuflow-native phase3 finalize-semantics TOPOLOGY METADATA OUTPUT\n"
    "  emuflow-native phase3 analyze --indexed-input EMUIR INPUT OUTPUT\n"
    "  emuflow-native phase4 route --typed-output INPUT OUTPUT\n"
    "  emuflow-native phase5 schedule INPUT OUTPUT\n"
    "  emuflow-native phase6 assign-banks "
    "(--typed-input|--legacy-text) INPUT OUTPUT\n"
    "  emuflow-native phase6 group-signals --typed-input "
    "INPUT OUTPUT EXPECTED_SHA256\n"
    "  emuflow-native phase6 group-signals --legacy-text INPUT OUTPUT\n"
    "  emuflow-native phase6 refine-groups --typed-input "
    "INPUT OUTPUT EXPECTED_SHA256\n"
    "  emuflow-native phase6 refine-groups --legacy-text INPUT OUTPUT\n"
    "  emuflow-native phase6 rudy INPUT OUTPUT\n";

int Dispatch(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << kUsage;
    return 0;
  }
  const std::string phase = argc > 1 ? argv[1] : "";
  const std::string operation = argc > 2 ? argv[2] : "";
  if (argc == 8 && phase == "phase3" && operation == "resolve-captures") {
    emuflow::native::ResolveStaticExactCaptures(argv[3], argv[4], argv[5], argv[6], argv[7]);
    return 0;
  }
  if (argc == 6 && phase == "phase3" && operation == "finalize-semantics") {
    emuflow::native::FinalizeStaticExact(argv[3], argv[4], argv[5]);
    return 0;
  }
  if (argc == 5 && phase == "phase3" && operation == "analyze") {
    emuflow::native::AnalyzeStaticExact(argv[3], argv[4]);
    return 0;
  }
  if (argc == 7 && phase == "phase3" && operation == "analyze" &&
      std::string(argv[3]) == "--indexed-input") {
    emuflow::native::AnalyzeStaticExact(argv[5], argv[6], argv[4]);
    return 0;
  }
  if (argc == 6 && phase == "phase4" && operation == "route" &&
      std::string(argv[3]) == "--typed-output") {
    emuflow::native::RouteTlr(argv[4], argv[5], true);
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
  if (phase == "phase6" && operation == "group-signals") {
    const std::string mode = argc > 3 ? argv[3] : "";
    if (argc == 7 && mode == "--typed-input") {
      emuflow::native::GroupChimewSignals(
          argv[4], argv[5], true, argv[6]);
      return 0;
    }
    if (argc == 6 && mode == "--legacy-text") {
      emuflow::native::GroupChimewSignals(argv[4], argv[5], false, "");
      return 0;
    }
  }
  if (phase == "phase6" && operation == "refine-groups") {
    const std::string mode = argc > 3 ? argv[3] : "";
    if (argc == 7 && mode == "--typed-input") {
      emuflow::native::RefineChimewPositions(
          argv[4], argv[5], true, argv[6]);
      return 0;
    }
    if (argc == 6 && mode == "--legacy-text") {
      emuflow::native::RefineChimewPositions(argv[4], argv[5], false, "");
      return 0;
    }
  }
  if (argc == 5 && phase == "phase6" && operation == "rudy") {
    emuflow::native::EvaluateChimewRudy(argv[3], argv[4]);
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
