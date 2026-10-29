#pragma once

#include "common/stage_runtime.hpp"

#include <csignal>
#include <utility>
#include <istream>

namespace emuflow::core {
// Canonical current binary identity for isolated native phase processes.
std::string CurrentExecutablePath();
// Bounded control-plane request. The engine owns its data-plane artifacts;
// forwarding stdout preserves the existing Yosys -> indexed importer pipe.
struct ToolRequest {
  std::vector<std::string> argv;
  std::string working_directory;
  bool inherit_environment = true;
  std::vector<std::pair<std::string, std::string>> environment;
  bool forward_stdout = false;
  bool forward_stdin = false;
  std::uint32_t tail_bytes = 2 * 1024 * 1024;
  std::uint64_t timeout_ms = 0;
  std::uint32_t termination_grace_ms = 1000;
  std::uint64_t cpu_seconds = 0, address_space_bytes = 0, file_size_bytes = 0;
};
struct ToolResult {
  StageResult operation;
  int return_code = -1, spawn_errno = 0, cancelled_signal = 0;
  bool started = false, timed_out = false;
  std::string diagnostic_tail;
  std::uint64_t diagnostic_bytes = 0;
  double wall_seconds = 0, user_cpu_seconds = 0, system_cpu_seconds = 0;
  std::uint64_t maximum_process_rss_kib = 0;
};

// The library never installs process-global signal handlers. A native flow
// supplies its cancellation token; only the separate CLI owns signal delivery.
ToolResult RunExternalTool(const ToolRequest& request,
                          const volatile std::sig_atomic_t* cancelled = nullptr,
                          const std::vector<ArtifactRef>& inputs = {});
// Consume engine stdout in-process with bounded pipe backpressure. The engine
// is forked before the consumer thread starts; stderr/deadlines remain serviced
// by the common supervisor. Consumer failure cancels and reaps the whole group.
// An optional diagnostic log persists stderr, never the graph on stdout.
ToolResult RunExternalToolStream(const ToolRequest& request,
    const std::function<void(std::istream&)>& consume,
    const volatile std::sig_atomic_t* cancelled = nullptr,
    const std::vector<ArtifactRef>& inputs = {},
    const std::string& diagnostic_log = {});
// Persist the complete combined diagnostic stream while retaining the same
// process-group supervision, cancellation and bounded diagnostic tail.
// The log survives engine failure; it is diagnostic evidence, not a pass seal.
ToolResult RunExternalToolLogged(const ToolRequest& request,const std::string& log,
    const volatile std::sig_atomic_t* cancelled=nullptr,
    const std::vector<ArtifactRef>& inputs={});
ToolRequest ReadToolRequest(const std::string& path);
std::string ToolResultJson(const ToolResult& result);
void PublishToolResult(const std::string& path, const ToolResult& result);
}  // namespace emuflow::core
