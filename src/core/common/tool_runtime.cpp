#include "common/tool_runtime.hpp"

#include "common/artifact_io.hpp"
#include "common/json_record.hpp"

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
#include <atomic>
#include <exception>
#include <memory>
#include <streambuf>
#include <fcntl.h>
#include <poll.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif

extern char** environ;

namespace emuflow::core {
std::string CurrentExecutablePath() {
#if defined(__linux__)
  return std::filesystem::canonical("/proc/self/exe").string();
#elif defined(__APPLE__)
  char probe[1];std::uint32_t size=sizeof(probe);
  if(_NSGetExecutablePath(probe,&size)==0)return std::filesystem::canonical(probe).string();
  std::string buffer(size,'\0');
  if(_NSGetExecutablePath(buffer.data(),&size)!=0)throw StageError(ErrorCode::kExecutionFailed,"cannot locate native compiler executable");
  return std::filesystem::canonical(buffer.c_str()).string();
#else
  throw StageError(ErrorCode::kExecutionFailed,"unsupported native compiler executable lookup");
#endif
}

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
class PipeBuffer : public std::streambuf {
 public:
  PipeBuffer(int fd,std::atomic<bool>& stopping,std::atomic<bool>& waiting)
      :fd_(fd),stopping_(stopping),waiting_(waiting){setg(buffer_.data(),buffer_.data(),buffer_.data());}
 protected:
  int_type underflow() override {
    if(gptr()<egptr())return traits_type::to_int_type(*gptr());
    while(true) {
      if(stopping_)throw std::runtime_error("external tool stream cancelled");
      const auto n=read(fd_,buffer_.data(),buffer_.size());
      if(n<0 && errno==EINTR)continue;
      if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) {
        waiting_=true;struct pollfd descriptor{fd_,POLLIN|POLLHUP,0};
        if(poll(&descriptor,1,20)<0 && errno!=EINTR)throw std::runtime_error("cannot observe external tool stream");
        continue;
      }
      waiting_=false;
      if(n<0)throw std::runtime_error("cannot read external tool stream");
      if(!n)return traits_type::eof();
      setg(buffer_.data(),buffer_.data(),buffer_.data()+n);return traits_type::to_int_type(*gptr());
    }
  }
 private:int fd_;std::atomic<bool>& stopping_;std::atomic<bool>& waiting_;std::array<char,65536> buffer_{};
};
struct StreamWorker {
  Pipe& pipe;pid_t pid;std::atomic<bool> done{false},failed{false},stopping{false},waiting{false};
  std::exception_ptr error;std::thread thread;
  StreamWorker(Pipe& value,pid_t child,const std::function<void(std::istream&)>& consume):pipe(value),pid(child),thread([this,consume]{
    try {PipeBuffer buffer(pipe.read,stopping,waiting);std::istream input(&buffer);input.exceptions(std::ios::badbit);consume(input);
      if(input.peek()!=std::char_traits<char>::eof())throw std::runtime_error("external tool consumer left unread output");
    }catch(...){error=std::current_exception();failed=true;}
    pipe.CloseRead();done=true;
  }){}
  ~StreamWorker(){if(thread.joinable()){stopping=true;if(!done)kill(-pid,SIGKILL);thread.join();}}
  void Finish(){thread.join();if(error)std::rethrow_exception(error);}
};
void Execute(const ToolRequest& request, ToolResult& result,
             const volatile std::sig_atomic_t* cancelled,
             const std::function<void(std::istream&)>* consume=nullptr,
             AtomicBinaryWriter* log=nullptr) {
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
  std::unique_ptr<Pipe> output;if(consume)output=std::make_unique<Pipe>();
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
    const int input = request.forward_stdin ? STDIN_FILENO : open("/dev/null", O_RDONLY);
    if (input < 0 || dup2(input, STDIN_FILENO) < 0 ||
        dup2(diagnostics.write, STDERR_FILENO) < 0 ||
        (output ? dup2(output->write,STDOUT_FILENO)<0 : (!request.forward_stdout && dup2(diagnostics.write, STDOUT_FILENO) < 0)))
      ChildFailure(errors.write, errno);
    if (input > STDERR_FILENO) close(input);
    close(diagnostics.read); close(diagnostics.write); close(errors.read);
    if(output){close(output->read);close(output->write);}
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
  std::unique_ptr<StreamWorker> worker;
  if(output){output->CloseWrite();if(fcntl(output->read,F_SETFL,O_NONBLOCK)<0)throw std::runtime_error("cannot configure external stream");worker=std::make_unique<StreamWorker>(*output,pid,*consume);}
  bool terminating = false; auto terminate_at = start, reaped_at = start;
  int status = 0; struct rusage usage{};
  std::array<char, 65536> buffer{};
  while (!child.reaped || diagnostics.read >= 0 || (worker && !worker->done)) {
    const auto now = Clock::now();
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - start).count();
    if (!terminating && ((worker && worker->failed) || (cancelled && *cancelled) ||
        (request.timeout_ms && elapsed >= static_cast<std::int64_t>(request.timeout_ms)))) {
      result.cancelled_signal = cancelled ? *cancelled : 0;
      result.timed_out = !result.cancelled_signal && !(worker && worker->failed);
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
    if(child.reaped && worker && worker->waiting && now-reaped_at>std::chrono::milliseconds(1000))
      throw StageError(ErrorCode::kExecutionFailed,"external tool left a detached stdout writer");
    // Drain only a bounded number of chunks per tick; a noisy engine must not
    // starve deadline/cancellation processing by keeping its pipe readable.
    for (unsigned chunk = 0; chunk < 16 && diagnostics.read >= 0; ++chunk) {
      const auto count = read(diagnostics.read, buffer.data(), buffer.size());
      if (count > 0) {
        if(log)log->WriteBytes(buffer.data(),count);
        AppendTail(result, ring, cursor, buffer.data(), count);
      }
      else if (!count) diagnostics.CloseRead();
      else if (errno == EINTR) continue;
      else if (errno == EAGAIN || errno == EWOULDBLOCK) break;
      else throw std::runtime_error("cannot read external tool diagnostics");
    }
    if (child.reaped && diagnostics.read < 0 && (!worker || worker->done)) break;
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
  std::exception_ptr consumer_error;
  if(worker){try{worker->Finish();}catch(...){consumer_error=std::current_exception();}}
  // Cancellation closes the producer pipe. A parser's consequent EOF error
  // must not replace the authoritative timeout/cancellation diagnosis.
  if(!result.timed_out && !result.cancelled_signal && !result.spawn_errno && consumer_error)
    std::rethrow_exception(consumer_error);
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
  if (reader.Read<std::uint64_t>() != kMagic) Invalid("external tool request magic is invalid");
  const auto version = reader.Read<std::uint32_t>();
  if (version != 1 && version != 2) Invalid("external tool request version is invalid");
  ToolRequest request;
  const auto mode = reader.Read<std::uint32_t>(), inherit = reader.Read<std::uint32_t>();
  if (mode > (version == 1 ? 1U : 3U) || inherit > 1) Invalid("unknown external tool mode");
  request.forward_stdout = (mode & 1); request.forward_stdin = (mode & 2); request.inherit_environment = inherit;
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
ToolResult RunExternalToolStream(const ToolRequest& request,
    const std::function<void(std::istream&)>& consume,
    const volatile std::sig_atomic_t* cancelled,const std::vector<ArtifactRef>& inputs,const std::string& diagnostic_log) {
  ToolResult result;
  result.operation=RunStage("native.external-tool-stream",[&]{
    Validate(request);if(!consume || request.forward_stdout)Invalid("invalid external stream consumer/mode");
    auto refs=inputs;auto executable=ArtifactRef::Bind(request.argv[0],"external-tool-executable-v1");
    if(access(executable.path().c_str(),X_OK)!=0)Invalid("external tool is not executable");
    refs.push_back(std::move(executable));
    if(!diagnostic_log.empty()&&!std::filesystem::path(diagnostic_log).is_absolute())Invalid("stream log must be absolute");
    if(!diagnostic_log.empty())for(const auto& ref:refs){
      std::error_code error;
      if(std::filesystem::absolute(diagnostic_log).lexically_normal()==ref.path() || std::filesystem::equivalent(diagnostic_log,ref.path(),error))Invalid("stream log aliases an input");
    }
    return refs;
  },[&](const auto&){if(diagnostic_log.empty())Execute(request,result,cancelled,&consume);
    else {
      AtomicBinaryWriter writer(diagnostic_log);
      try{Execute(request,result,cancelled,&consume,&writer);}catch(...){writer.Commit();throw;}
      writer.Commit();
    }});
  return result;
}
ToolResult RunExternalToolLogged(const ToolRequest& request,const std::string& log,
    const volatile std::sig_atomic_t* cancelled,const std::vector<ArtifactRef>& inputs) {
  ToolResult result;
  result.operation=RunStage("native.external-tool-logged",[&]{
    Validate(request);if(request.forward_stdout)Invalid("logged tool must capture combined output");
    if(!std::filesystem::path(log).is_absolute())Invalid("tool log must be absolute");
    auto refs=inputs;auto executable=ArtifactRef::Bind(request.argv[0],"external-tool-executable-v1");
    if(access(executable.path().c_str(),X_OK)!=0)Invalid("external tool is not executable");
    refs.push_back(std::move(executable));
    for(const auto& ref:refs){std::error_code error;if(std::filesystem::absolute(log).lexically_normal()==ref.path()||std::filesystem::equivalent(log,ref.path(),error))Invalid("tool log aliases an input");}
    return refs;
  },[&](const auto&){
    AtomicBinaryWriter writer(log);
    try{Execute(request,result,cancelled,nullptr,&writer);}catch(...){writer.Commit();throw;}
    writer.Commit();
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(log,"exact-sha256-text-v1")};});
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
