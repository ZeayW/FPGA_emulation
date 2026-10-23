// Compatibility CLI; all storage, traversal and publication live in the core.
#include "emuflow_core/native_pipeline.hpp"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_xilinx_bookshelf_exporter MAPPED ATOMS NETS REPORT SPOOL\n";
    return 0;
  }
  if (argc != 6) return 2;
  emuflow::native::StageRequest request{
      emuflow::native::Stage::kExportBookshelf, {argv[1],argv[2]}, argv[3]};
  request.auxiliary_output_path = argv[4];
  request.scratch_path = argv[5];
  const auto result = emuflow::native::ExecuteStage(request);
  if (!result.ok()) std::cerr << "emuflow_xilinx_bookshelf_exporter: "
                              << emuflow::core::StageResultJson(result);
  return result.ok() ? 0 : result.error == emuflow::core::ErrorCode::kInvalidRequest ? 2 : 1;
}
