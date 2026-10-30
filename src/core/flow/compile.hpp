#pragma once
#include "common/tool_runtime.hpp"

namespace emuflow::core {
struct CompileStepResult {
  std::string stage;
  bool passed = false;
  std::string implementation_id = "native-stage-v1";
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
// One-shot native orchestration for the maintained upstream, baseline/Chimew
// transport, physical backends and full system timing. External tools remain
// supervised processes with source-bound stage ownership.
CompileResult Compile(const ArtifactRef& request,const ArtifactRef& native_executable,
                      const volatile std::sig_atomic_t* cancelled = nullptr);
std::string CompileResultJson(const CompileResult& result);
void PublishCompileResult(const CompileResult& result);
}  // namespace emuflow::core
