#include "emuflow_core/tool_runtime.hpp"

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <map>
#include <sstream>
#include <thread>
#include <fcntl.h>
#include <poll.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

extern char** environ;

namespace emuflow::core {
namespace {
constexpr std::uint64_t kMagic = 0x31514552544c4645ULL;
constexpr std::size_t kMaximumRequestBytes = 4 * 1024 * 1024;
using Clock = std::chrono::steady_clock;
void Invalid(const std::string& message) {
  throw StageError(ErrorCode::kInvalidRequest, message);
}
void CheckString(const std::string& text) {
  if (text.find('\0') != std::string::npos) Invalid("NUL in external tool request");
}
void Validate(const ToolRequest& request) {
  if (request.argv.empty() || request.argv.size() > 4096 || request.argv[0].empty() ||
      !std::filesystem::path(request.argv[0]).is_absolute())
    Invalid("external tool requires an absolute executable and bounded argv");
  if (!std::filesystem::path(request.working_directory).is_absolute() ||
      !std::filesystem::is_directory(request.working_directory))
    Invalid("external tool working directory is unavailable or not absolute");
  if (!request.tail_bytes || request.tail_bytes > 2 * 1024 * 1024 ||
      request.termination_grace_ms > 60000 || request.environment.size() > 4096 ||
      request.timeout_ms > 365ULL * 24 * 3600 * 1000)
    Invalid("external tool bounds are invalid");
  std::size_t total = request.working_directory.size();
  CheckString(request.working_directory);
  for (const auto& arg : request.argv) { CheckString(arg); total += arg.size(); }
  std::map<std::string, bool> names;
  for (const auto& item : request.environment) {
    CheckString(item.first); CheckString(item.second);
    if (item.first.empty() || item.first.find('=') != std::string::npos ||
        !names.emplace(item.first, true).second)
      Invalid("external tool environment key is invalid or duplicate");
    total += item.first.size() + item.second.size();
  }
  if (total > kMaximumRequestBytes) Invalid("external tool request exceeds bound");
  for (auto limit : {request.cpu_seconds, request.address_space_bytes, request.file_size_bytes})
    if (limit && (limit >= static_cast<std::uint64_t>(RLIM_INFINITY) ||
                  static_cast<std::uint64_t>(static_cast<rlim_t>(limit)) != limit))
      Invalid("external tool resource limit is out of range");
}
struct Pipe {
  int read = -1, write = -1;
  Pipe() {
    int fds[2];
    if (pipe(fds) != 0) throw std::runtime_error("cannot create external tool pipe");
    read = fds[0]; write = fds[1];
    if (fcntl(read, F_SETFD, FD_CLOEXEC) < 0 || fcntl(write, F_SETFD, FD_CLOEXEC) < 0) {
      close(read); close(write); read = write = -1;
      throw std::runtime_error("cannot protect external tool descriptors");
    }
  }
  void CloseRead() { if (read >= 0) close(read); read = -1; }
  void CloseWrite() { if (write >= 0) close(write); write = -1; }
  ~Pipe() { CloseRead(); CloseWrite(); }
};
// A child may not survive any exception after fork. Ownership is local to one
// tool invocation, so simultaneous FPGA workers do not share process groups.
struct Child {
  pid_t pid;
  bool reaped = false;
  ~Child() {
    if (!reaped) {
      kill(-pid, SIGKILL);
      kill(pid, SIGKILL);
      int status;
      while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }
  }
};
void AppendTail(ToolResult& result, std::vector<char>& ring, std::size_t& cursor,
                const char* bytes, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) {
    ring[cursor] = bytes[i]; cursor = (cursor + 1) % ring.size();
  }
  result.diagnostic_bytes += count;
}
void FinishTail(ToolResult& result, const std::vector<char>& ring, std::size_t cursor) {
  const auto count = std::min<std::uint64_t>(ring.size(), result.diagnostic_bytes);
  const auto start = result.diagnostic_bytes >= ring.size() ? cursor : 0;
  result.diagnostic_tail.reserve(count);
  for (std::size_t i = 0; i < count; ++i)
    result.diagnostic_tail += ring[(start + i) % ring.size()];
}
void ChildFailure(int fd, int error) {
  // Only async-signal-safe operations after fork. errno reaches the supervisor
  // through a CLOEXEC pipe; an engine's genuine exit 127 stays distinguishable.
  const auto ignored = write(fd, &error, sizeof(error));
  (void)ignored;
  _exit(127);
}
void Limit(int resource, std::uint64_t value, int errors) {
  if (value) {
    const struct rlimit limit{static_cast<rlim_t>(value), static_cast<rlim_t>(value)};
    if (setrlimit(resource, &limit) != 0) ChildFailure(errors, errno);
  }
}
void Execute(const ToolRequest& request, ToolResult& result,
             const volatile std::sig_atomic_t* cancelled) {
  std::map<std::string, std::string> environment;
  if (request.inherit_environment) {
    for (auto item = environ; item && *item; ++item) {
      const std::string text(*item); const auto split = text.find('=');
      if (split != std::string::npos) environment[text.substr(0, split)] = text.substr(split + 1);
    }
  }
  for (const auto& item : request.environment) environment[item.first] = item.second;
  std::vector<std::string> env;
  for (const auto& item : environment) env.push_back(item.first + "=" + item.second);
  std::vector<char*> argv, envp;
  for (const auto& arg : request.argv) argv.push_back(const_cast<char*>(arg.c_str()));
  for (auto& item : env) envp.push_back(item.data());
  argv.push_back(nullptr); envp.push_back(nullptr);
  // Prepare allocations and argument pointers before fork (the library may
  // eventually be called by a compiler with multiple worker threads).
  std::vector<char> ring(request.tail_bytes); std::size_t cursor = 0;
  Pipe diagnostics, errors;
  if (fcntl(diagnostics.read, F_SETFL, O_NONBLOCK) < 0 ||
      fcntl(errors.read, F_SETFL, O_NONBLOCK) < 0)
    throw std::runtime_error("cannot configure external tool pipe");
  if (cancelled && *cancelled) {
    result.cancelled_signal = *cancelled;
    throw StageError(ErrorCode::kExecutionFailed, "external tool cancelled before launch");
  }
  const auto start = Clock::now();
  const pid_t pid = fork();
  if (pid < 0) throw std::runtime_error("cannot fork external tool");
  if (pid == 0) {
    if (setpgid(0, 0) != 0) ChildFailure(errors.write, errno);
    struct sigaction action{}; action.sa_handler = SIG_DFL; sigemptyset(&action.sa_mask);
    for (int sig : {SIGTERM, SIGINT, SIGPIPE, SIGHUP})
      if (sigaction(sig, &action, nullptr) != 0) ChildFailure(errors.write, errno);
    sigset_t mask; sigemptyset(&mask);
    if (sigprocmask(SIG_SETMASK, &mask, nullptr) != 0) ChildFailure(errors.write, errno);
    if (chdir(request.working_directory.c_str()) != 0) ChildFailure(errors.write, errno);
    const int input = open("/dev/null", O_RDONLY);
    if (input < 0 || dup2(input, STDIN_FILENO) < 0 ||
        dup2(diagnostics.write, STDERR_FILENO) < 0 ||
        (!request.forward_stdout && dup2(diagnostics.write, STDOUT_FILENO) < 0))
      ChildFailure(errors.write, errno);
    if (input > STDERR_FILENO) close(input);
    close(diagnostics.read); close(diagnostics.write); close(errors.read);
    Limit(RLIMIT_CPU, request.cpu_seconds, errors.write);
    Limit(RLIMIT_AS, request.address_space_bytes, errors.write);
    Limit(RLIMIT_FSIZE, request.file_size_bytes, errors.write);
    execve(argv[0], argv.data(), envp.data());
    ChildFailure(errors.write, errno);
  }
  Child child{pid}; result.started = true;
  // Child also calls setpgid before exec. Either ordering is safe.
  if (setpgid(pid, pid) != 0 && errno != EACCES && errno != ESRCH)
    throw std::runtime_error("cannot isolate external tool process group");
  diagnostics.CloseWrite(); errors.CloseWrite();
  bool terminating = false; auto terminate_at = start, reaped_at = start;
  int status = 0; struct rusage usage{};
  std::array<char, 65536> buffer{};
  while (!child.reaped || diagnostics.read >= 0) {
    const auto now = Clock::now();
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - start).count();
    if (!terminating && ((cancelled && *cancelled) ||
        (request.timeout_ms && elapsed >= static_cast<std::int64_t>(request.timeout_ms)))) {
      result.cancelled_signal = cancelled ? *cancelled : 0;
      result.timed_out = !result.cancelled_signal;
      terminating = true; terminate_at = now; kill(-pid, SIGTERM);
    }
    if (terminating && now - terminate_at >= std::chrono::milliseconds(request.termination_grace_ms))
      kill(-pid, SIGKILL);
    if (!child.reaped) {
      const auto waited = wait4(pid, &status, WNOHANG, &usage);
      if (waited == pid) {
        child.reaped = true;
        reaped_at = Clock::now();
        // A tool invocation owns the whole process group. Do not leave ABC,
        // Java helpers, or a pipe-holding descendant alive after its leader.
        kill(-pid, SIGKILL);
      } else if (waited < 0 && errno != EINTR) throw std::runtime_error("cannot reap external tool");
    }
    if (child.reaped && diagnostics.read >= 0 &&
        now - reaped_at > std::chrono::milliseconds(1000))
      throw StageError(ErrorCode::kExecutionFailed,
                       "external tool left a detached diagnostic writer");
    // Drain only a bounded number of chunks per tick; a noisy engine must not
    // starve deadline/cancellation processing by keeping its pipe readable.
    for (unsigned chunk = 0; chunk < 16 && diagnostics.read >= 0; ++chunk) {
      const auto count = read(diagnostics.read, buffer.data(), buffer.size());
      if (count > 0) AppendTail(result, ring, cursor, buffer.data(), count);
      else if (!count) diagnostics.CloseRead();
      else if (errno == EINTR) continue;
      else if (errno == EAGAIN || errno == EWOULDBLOCK) break;
      else throw std::runtime_error("cannot read external tool diagnostics");
    }
    if (child.reaped && diagnostics.read < 0) break;
    struct pollfd descriptor{diagnostics.read, POLLIN | POLLHUP, 0};
    if (poll(&descriptor, 1, 20) < 0 && errno != EINTR)
      throw std::runtime_error("cannot observe external tool");
  }
  ssize_t count;
  do { count = read(errors.read, &result.spawn_errno, sizeof(result.spawn_errno)); }
  while (count < 0 && errno == EINTR);
  if (count != 0 && count != sizeof(result.spawn_errno))
    throw std::runtime_error("invalid external tool exec status");
  result.return_code = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
  result.wall_seconds = std::chrono::duration<double>(Clock::now() - start).count();
  result.user_cpu_seconds = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6;
  result.system_cpu_seconds = usage.ru_stime.tv_sec + usage.ru_stime.tv_usec / 1e6;
  result.maximum_process_rss_kib = usage.ru_maxrss;
#if defined(__APPLE__)
  result.maximum_process_rss_kib /= 1024;
#endif
  FinishTail(result, ring, cursor);
  if (result.timed_out || result.cancelled_signal || result.spawn_errno || result.return_code)
    throw StageError(ErrorCode::kExecutionFailed,
        result.timed_out ? "external tool exceeded wall-time limit" :
        result.cancelled_signal ? "external tool cancelled" :
        result.spawn_errno ? "external tool setup/exec failed: " + std::string(std::strerror(result.spawn_errno)) :
        "external tool exited with code " + std::to_string(result.return_code));
}
}  // namespace

ToolRequest ReadToolRequest(const std::string& path) {
  BinaryReader reader(path);
  if (reader.size() > kMaximumRequestBytes) Invalid("external tool request exceeds bound");
  ReadAndValidateHeader(reader, {kMagic, 1, "external-tool-request-v1"});
  ToolRequest request;
  const auto mode = reader.Read<std::uint32_t>(), inherit = reader.Read<std::uint32_t>();
  if (mode > 1 || inherit > 1) Invalid("unknown external tool mode");
  request.forward_stdout = mode; request.inherit_environment = inherit;
  request.tail_bytes = reader.Read<std::uint32_t>();
  request.timeout_ms = reader.Read<std::uint64_t>();
  request.termination_grace_ms = reader.Read<std::uint32_t>();
  request.cpu_seconds = reader.Read<std::uint64_t>();
  request.address_space_bytes = reader.Read<std::uint64_t>();
  request.file_size_bytes = reader.Read<std::uint64_t>();
  request.working_directory = reader.ReadString32();
  const auto args = reader.Read<std::uint32_t>();
  if (!args || args > 4096) Invalid("external tool argv exceeds bound");
  for (std::uint32_t i = 0; i < args; ++i) request.argv.push_back(reader.ReadString32(i != 0));
  const auto env = reader.Read<std::uint32_t>();
  if (env > 4096) Invalid("external tool environment exceeds bound");
  for (std::uint32_t i = 0; i < env; ++i) {
    const auto key = reader.ReadString32();
    request.environment.emplace_back(key, reader.ReadString32(true));
  }
  reader.RequireEnd(); Validate(request); return request;
}
ToolResult RunExternalTool(const ToolRequest& request,
                          const volatile std::sig_atomic_t* cancelled,
                          const std::vector<ArtifactRef>& inputs) {
  ToolResult result;
  result.operation = RunStage("native.external-tool", [&] {
    Validate(request);
    auto refs = inputs;
    auto executable = ArtifactRef::Bind(request.argv[0], "external-tool-executable-v1");
    if (access(executable.path().c_str(), X_OK) != 0) Invalid("external tool is not executable");
    refs.push_back(std::move(executable)); return refs;
  }, [&](const auto&) { Execute(request, result, cancelled); });
  return result;
}
std::string ToolResultJson(const ToolResult& result) {
  static const char digits[] = "0123456789abcdef";
  std::string hex; hex.reserve(result.diagnostic_tail.size() * 2);
  for (unsigned char c : result.diagnostic_tail) { hex += digits[c >> 4]; hex += digits[c & 15]; }
  std::ostringstream out; out << std::setprecision(17);
  out << "{\"schema\":\"emuflow.external-tool-result/v1\",\"operation\":" << StageResultJson(result.operation)
      << ",\"started\":" << (result.started ? "true" : "false")
      << ",\"returncode\":" << result.return_code << ",\"spawn_errno\":" << result.spawn_errno
      << ",\"timed_out\":" << (result.timed_out ? "true" : "false")
      << ",\"cancelled_signal\":" << result.cancelled_signal
      << ",\"diagnostic_tail_hex\":" << JsonQuote(hex)
      << ",\"diagnostic_bytes\":" << result.diagnostic_bytes
      << ",\"engine_metrics\":{\"wall_seconds\":" << result.wall_seconds
      << ",\"user_cpu_seconds\":" << result.user_cpu_seconds
      << ",\"system_cpu_seconds\":" << result.system_cpu_seconds
      << ",\"maximum_process_rss_kib\":" << result.maximum_process_rss_kib
      << ",\"cpu_scope\":\"wait4-child-and-reaped-descendants\""
      << ",\"rss_scope\":\"wait4-maximum-process-high-water\"}}\n";
  return out.str();
}
void PublishToolResult(const std::string& path, const ToolResult& result) {
  for (const auto& input : result.operation.inputs) {
    std::error_code error;
    if (std::filesystem::equivalent(path, input.path(), error) ||
        std::filesystem::absolute(path).lexically_normal() == input.path())
      Invalid("external tool result aliases an input");
  }
  const auto text = ToolResultJson(result);
  AtomicBinaryWriter writer(path); writer.WriteBytes(text.data(), text.size()); writer.Commit();
}
}  // namespace emuflow::core
