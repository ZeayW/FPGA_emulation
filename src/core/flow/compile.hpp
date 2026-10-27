#pragma once
#include "common/tool_runtime.hpp"

namespace emuflow::core {
struct CompileStepResult {
  std::string stage;
  bool passed = false;
  std::string implementation_id = "isolated-python-adapter-v1";
  double wall_seconds = 0, user_cpu_seconds = 0, system_cpu_seconds = 0;
  std::uint64_t maximum_process_rss_kib = 0;
};
struct CompileResult {
  StageResult operation;
  std::string output_directory;
  std::string run_id;
  bool owns_output = false;
  std::string request_configuration, effective_configuration;
  std::vector<CompileStepResult> steps;
};
// One-shot native orchestration, not a call to the Python compile function.
// Transitional phase adapters execute shared phase bodies in isolated
// processes until every remaining data-plane owner has a native replacement.
CompileResult Compile(const ArtifactRef& request,const ArtifactRef& native_executable,
                      const volatile std::sig_atomic_t* cancelled = nullptr);
std::string CompileResultJson(const CompileResult& result);
void PublishCompileResult(const CompileResult& result);
}  // namespace emuflow::core
