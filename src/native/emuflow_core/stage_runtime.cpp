#include "emuflow_core/stage_runtime.hpp"

#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/sqlite.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <sys/stat.h>
#if !defined(_WIN32)
#include <sys/resource.h>
#endif

namespace emuflow::core {
namespace {
constexpr RegisteredSchema schemas[] = {
    {"native-compile-request-v1", ArtifactEncoding::kText, 0, 1},
    {"native-compile-adapter-v1", ArtifactEncoding::kText, 0, 1},
    {"native-compile-state-v1", ArtifactEncoding::kText, 0, 1},
    {"multi-fpga-flow-report-v2", ArtifactEncoding::kText, 0, 2},
    {"external-tool-request-v1", ArtifactEncoding::kBinary, 0x31514552544c4645ULL, 1},
    {"external-tool-executable-v1", ArtifactEncoding::kText, 0, 1},
    {"emuir-compact-v2", ArtifactEncoding::kSqlite, 0x454d5549, 2},
    {"exact-analysis-input-v3", ArtifactEncoding::kBinary, 0x31584553464d5545ULL, 3},
    {"exact-indexed-request-v4", ArtifactEncoding::kBinary, 0x31584553464d5545ULL, 4},
    {"exact-topology-v3", ArtifactEncoding::kBinary, 0x315845534f4d5545ULL, 3},
    {"exact-identities-v1", ArtifactEncoding::kBinary, 0x314d4553464d5545ULL, 1},
    {"exact-parts-v1", ArtifactEncoding::kBinary, 0x31504553464d5545ULL, 1},
    {"exact-nodes-v2", ArtifactEncoding::kBinary, 0x314e4553464d5545ULL, 2},
    {"exact-captures-v1", ArtifactEncoding::kBinary, 0x31434553464d5545ULL, 1},
    {"exact-hash-request-v1", ArtifactEncoding::kBinary, 0x31484553464d5545ULL, 1},
    {"exact-section-request-v1", ArtifactEncoding::kBinary, 0x31574553464d5545ULL, 1},
    {"exact-schedule-board-request-v1", ArtifactEncoding::kBinary, 0x3142534d44544545ULL, 1},
    {"exact-schedule-input-v1", ArtifactEncoding::kBinary, 0x3149534d44544545ULL, 1},
    {"patron-input-v1", ArtifactEncoding::kBinary, 0x314e525441504d45ULL, 1},
    {"chimew-bank-input-v1", ArtifactEncoding::kBinary, 0x0033434248434d45ULL, 1},
    {"chimew-group-input-v1", ArtifactEncoding::kBinary, 0x0033524748434d45ULL, 1},
    {"chimew-refine-input-v1", ArtifactEncoding::kBinary, 0x0032465248434d45ULL, 1},
    // Text compatibility/IPC schemas have stage-owned grammars. Binding does
    // not consume a pipe or duplicate their parser; the body checks the grammar.
    {"patron-legacy-text", ArtifactEncoding::kText, 0, 1},
    {"tlr-input-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-bank-legacy-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-group-legacy-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-refine-legacy-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-rudy-input-v1", ArtifactEncoding::kText, 0, 1},
    {"patron-output-v1", ArtifactEncoding::kBinary, 0x3154554f54415045ULL, 1},
    {"patron-output-text", ArtifactEncoding::kText, 0, 1},
    {"tlr-output-v1", ArtifactEncoding::kSqlite, 0x45545252, 1},
    {"tlr-output-text", ArtifactEncoding::kText, 0, 1},
    {"system-route-store-v3", ArtifactEncoding::kSqlite, 0x454d5552, 3},
    {"exact-schedule-publication-request-v1", ArtifactEncoding::kBinary, 0x3157534d44544545ULL, 1},
    {"exact-schedule-generation-request-v1", ArtifactEncoding::kBinary, 0x3147534d44544545ULL, 1},
    {"tdm-schedule-store-v4", ArtifactEncoding::kSqlite, 0x454d5554, 3},
    {"exact-schedule-output-v2", ArtifactEncoding::kBinary, 0x314f534d44544545ULL, 2},
    {"exact-capture-index-private-v1", ArtifactEncoding::kSqlite, 0x45434150, 1},
    {"exact-sha256-text-v1", ArtifactEncoding::kText, 0, 1},
    {"exact-section-summary-v2", ArtifactEncoding::kBinary, 0x314f5753464d5545ULL, 2},
    {"chimew-bank-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-group-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-refine-output-text", ArtifactEncoding::kText, 0, 1},
    {"chimew-rudy-output-text", ArtifactEncoding::kText, 0, 1},
};

FileGeneration Generation(const std::string& path) {
#if defined(_WIN32)
  struct _stat64 status{};
  const auto code = _stat64(path.c_str(), &status);
#else
  struct stat status{};
  const auto code = stat(path.c_str(), &status);
#endif
  if (code != 0 || !std::filesystem::is_regular_file(path)) {
    throw StageError(ErrorCode::kMissingArtifact, "native input is not a regular file: " + path);
  }
  FileGeneration result;
  result.device = status.st_dev; result.inode = status.st_ino;
  result.bytes = status.st_size;
#if defined(__APPLE__)
  result.modified_seconds = status.st_mtimespec.tv_sec;
  result.modified_nanoseconds = status.st_mtimespec.tv_nsec;
  result.changed_seconds = status.st_ctimespec.tv_sec;
  result.changed_nanoseconds = status.st_ctimespec.tv_nsec;
#elif !defined(_WIN32)
  result.modified_seconds = status.st_mtim.tv_sec;
  result.modified_nanoseconds = status.st_mtim.tv_nsec;
  result.changed_seconds = status.st_ctim.tv_sec;
  result.changed_nanoseconds = status.st_ctim.tv_nsec;
#else
  result.modified_seconds = status.st_mtime;
  result.changed_seconds = status.st_ctime;
#endif
  return result;
}

struct Usage {
  double user = 0, system = 0;
  std::uint64_t peak_rss_kib = 0;
  bool has_io = false;
  std::uint64_t read_bytes = 0, write_bytes = 0;
};
void RequireFinalizedSqlite(const std::string& path) {
  for (const char* suffix : {"-wal", "-journal"}) {
    std::error_code error;
    const auto size = std::filesystem::file_size(path + suffix, error);
    if (!error && size != 0)
      throw StageError(ErrorCode::kInvalidArtifact, "native SQLite input has an unfinalized sidecar: " + path);
  }
}
Usage CurrentUsage() {
  Usage result;
#if !defined(_WIN32)
  struct rusage usage{};
  if (getrusage(RUSAGE_SELF, &usage) == 0) {
    result.user = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec / 1e6;
    result.system = usage.ru_stime.tv_sec + usage.ru_stime.tv_usec / 1e6;
    result.peak_rss_kib = usage.ru_maxrss;
#if defined(__APPLE__)
    result.peak_rss_kib /= 1024;
#endif
  }
#endif
#if defined(__linux__)
  std::ifstream io("/proc/self/io");
  std::string name; std::uint64_t value = 0; unsigned fields = 0;
  while (io >> name >> value) {
    if (name == "read_bytes:") { result.read_bytes = value; fields |= 1; }
    if (name == "write_bytes:") { result.write_bytes = value; fields |= 2; }
  }
  result.has_io = fields == 3;
#endif
  return result;
}
std::string Quote(const std::string& value) {
  std::ostringstream out; out << '"';
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') out << '\\' << c;
    else if (c < 32) out << "\\u00" << "0123456789abcdef"[c >> 4]
                        << "0123456789abcdef"[c & 15];
    else out << c;
  }
  out << '"'; return out.str();
}
}  // namespace

const char* ErrorName(ErrorCode code) {
  switch (code) {
    case ErrorCode::kNone: return "none";
    case ErrorCode::kInvalidRequest: return "invalid-request";
    case ErrorCode::kMissingArtifact: return "missing-artifact";
    case ErrorCode::kInvalidArtifact: return "invalid-artifact";
    case ErrorCode::kMixedGeneration: return "mixed-generation";
    case ErrorCode::kExecutionFailed: return "execution-failed";
    case ErrorCode::kPublicationFailed: return "publication-failed";
  }
  return "execution-failed";
}
const RegisteredSchema& FindSchema(const std::string& name) {
  for (const auto& schema : schemas) if (schema.name == name) return schema;
  throw StageError(ErrorCode::kInvalidRequest, "unknown native artifact schema: " + name);
}
bool FileGeneration::operator==(const FileGeneration& other) const {
  return device == other.device && inode == other.inode && bytes == other.bytes &&
      modified_seconds == other.modified_seconds && modified_nanoseconds == other.modified_nanoseconds &&
      changed_seconds == other.changed_seconds && changed_nanoseconds == other.changed_nanoseconds;
}
ArtifactRef ArtifactRef::Bind(const std::string& path, const std::string& schema_name,
                              bool allow_pipe) {
  const auto& schema = FindSchema(schema_name);
  ArtifactRef result; result.schema_ = schema_name;
  if (path == "-" && allow_pipe) {
    result.path_ = path; result.pipe_ = true; return result;
  }
  std::error_code error;
  result.path_ = std::filesystem::canonical(path, error).string();
  if (error) throw StageError(ErrorCode::kMissingArtifact, "cannot open native input: " + path);
  result.generation_ = Generation(result.path_);
  try {
    if (schema.encoding == ArtifactEncoding::kBinary) {
      BinaryReader reader(result.path_);
      ReadAndValidateHeader(reader, {schema.magic, schema.version, schema.name});
    } else if (schema.encoding == ArtifactEncoding::kSqlite) {
      RequireFinalizedSqlite(result.path_);
      SqliteDatabase database(result.path_, SqliteDatabase::Mode::kReadOnly);
      auto application = database.Prepare("PRAGMA application_id");
      auto version = database.Prepare("PRAGMA user_version");
      if (!application.Step() || !version.Step() ||
          application.Integer(0) != static_cast<std::int64_t>(schema.magic) ||
          version.Integer(0) != schema.version) {
        throw StageError(ErrorCode::kInvalidArtifact, "unsupported " + schema_name + " storage envelope");
      }
    }
  } catch (const StageError&) { throw; }
  catch (const std::exception& e) { throw StageError(ErrorCode::kInvalidArtifact, e.what()); }
  result.RequireUnchanged();
  return result;
}
void ArtifactRef::RequireUnchanged() const {
  if (pipe_) return;
  try {
    if (FindSchema(schema_).encoding == ArtifactEncoding::kSqlite)
      RequireFinalizedSqlite(path_);
    if (!(Generation(path_) == generation_))
      throw StageError(ErrorCode::kMixedGeneration, "native input generation changed: " + path_);
  } catch (const StageError& e) {
    if (e.code() == ErrorCode::kMixedGeneration) throw;
    throw StageError(ErrorCode::kMixedGeneration, "native input generation disappeared: " + path_);
  }
}
StageResult RunStage(const std::string& stage,
                     const std::function<std::vector<ArtifactRef>()>& bind,
                     const std::function<void(const std::vector<ArtifactRef>&)>& body,
                     const std::function<std::vector<ArtifactRef>()>& outputs) {
  StageResult result; result.stage = stage;
  const auto start = std::chrono::steady_clock::now();
  const auto before = CurrentUsage();
  try {
    if (stage.empty()) throw StageError(ErrorCode::kInvalidRequest, "native stage is missing");
    result.inputs = bind();
    for (const auto& input : result.inputs) input.RequireUnchanged();
    body(result.inputs);
    for (const auto& input : result.inputs) input.RequireUnchanged();
    if (outputs) result.outputs = outputs();
  } catch (const StageError& e) { result.error = e.code(); result.message = e.what(); }
  catch (const std::exception& e) { result.error = ErrorCode::kExecutionFailed; result.message = e.what(); }
  const auto after = CurrentUsage();
  result.metrics.wall_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
  result.metrics.user_cpu_seconds = after.user - before.user;
  result.metrics.system_cpu_seconds = after.system - before.system;
  result.metrics.process_peak_rss_kib = after.peak_rss_kib;
  if (before.has_io && after.has_io && after.read_bytes >= before.read_bytes && after.write_bytes >= before.write_bytes) {
    result.metrics.has_io_counters = true;
    result.metrics.read_bytes = after.read_bytes - before.read_bytes;
    result.metrics.write_bytes = after.write_bytes - before.write_bytes;
  }
  if (result.message.size() > 16384) {
    std::size_t end = 16384;
    while (end && (static_cast<unsigned char>(result.message[end]) & 0xc0) == 0x80) --end;
    result.message.resize(end);
    result.message += " [truncated]";
  }
  return result;
}
StageResult CheckNativeRuntime() {
  return RunStage("native.runtime-check", [] { return std::vector<ArtifactRef>{}; }, [](const auto&) {
    SqliteDatabase database(":memory:",SqliteDatabase::Mode::kReadOnly);
    auto probe = database.Prepare("SELECT sqlite_version(),json_extract('{\"probe\":true}','$.probe'),json_valid('{\"probe\":true}')");
    if (!probe.Step() || probe.Integer(1) != 1 || probe.Integer(2) != 1)
      throw StageError(ErrorCode::kExecutionFailed,"native SQLite JSON feature probe failed");
    std::istringstream version(probe.Text(0));
    int major = 0, minor = 0; char dot = 0;
    if (!(version >> major >> dot >> minor) || dot != '.' || major < 3 || (major == 3 && minor < 22))
      throw StageError(ErrorCode::kExecutionFailed,"native runtime requires SQLite 3.22 or newer");
  });
}
std::string StageResultJson(const StageResult& result) {
  std::ostringstream out; out << std::setprecision(17);
  out << "{\"schema\":\"emuflow.native-stage-result/v1\",\"stage\":" << Quote(result.stage)
      << ",\"status\":\"" << (result.ok() ? "pass" : "fail") << "\",\"error\":{\"code\":"
      << Quote(ErrorName(result.error)) << ",\"message\":" << Quote(result.message)
      << "},\"metrics\":{\"wall_seconds\":" << result.metrics.wall_seconds
      << ",\"user_cpu_seconds\":" << result.metrics.user_cpu_seconds
      << ",\"system_cpu_seconds\":" << result.metrics.system_cpu_seconds
      << ",\"cpu_scope\":\"self-process-interval\""
      << ",\"process_lifetime_peak_rss_kib\":" << result.metrics.process_peak_rss_kib
      << ",\"rss_scope\":\"self-lifetime-high-water\",\"io_scope\":\"self-proc-io-delta\",\"read_bytes\":";
  if (result.metrics.has_io_counters) out << result.metrics.read_bytes; else out << "null";
  out << ",\"write_bytes\":";
  if (result.metrics.has_io_counters) out << result.metrics.write_bytes; else out << "null";
  out << "},\"inputs\":";
  const auto write_refs = [&](const std::vector<ArtifactRef>& refs) {
    out << '[';
    bool first = true;
    for (const auto& input : refs) {
    if (!first) out << ',';
    first = false;
    const auto& g = input.generation();
    out << "{\"path\":" << Quote(input.path()) << ",\"schema\":" << Quote(input.schema())
        << ",\"pipe\":" << (input.is_pipe() ? "true" : "false")
        << ",\"generation\":{\"device\":" << g.device << ",\"inode\":" << g.inode
        << ",\"bytes\":" << g.bytes << ",\"mtime_seconds\":" << g.modified_seconds
        << ",\"mtime_nanoseconds\":" << g.modified_nanoseconds
        << ",\"ctime_seconds\":" << g.changed_seconds << ",\"ctime_nanoseconds\":" << g.changed_nanoseconds << "}}";
    }
    out << ']';
  };
  write_refs(result.inputs); out << ",\"outputs\":"; write_refs(result.outputs);
  out << "}\n"; return out.str();
}
void PublishStageResult(const std::string& path, const StageResult& result) {
  try {
    auto refs = result.inputs;
    refs.insert(refs.end(), result.outputs.begin(), result.outputs.end());
    for (const auto& input : refs) {
      std::error_code error;
      if (!input.is_pipe() && std::filesystem::equivalent(path, input.path(), error))
        throw StageError(ErrorCode::kInvalidRequest, "native report aliases a stage artifact");
    }
    const auto text = StageResultJson(result);
    AtomicBinaryWriter output(path); output.WriteBytes(text.data(), text.size()); output.Commit();
  } catch (const StageError&) { throw; }
  catch (const std::exception& e) { throw StageError(ErrorCode::kPublicationFailed, e.what()); }
}
}  // namespace emuflow::core
