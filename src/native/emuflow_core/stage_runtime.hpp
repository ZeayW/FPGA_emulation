#pragma once

#include <cstdint>
#include <functional>
#include <stdexcept>
#include <string>
#include <vector>

namespace emuflow::core {

enum class ErrorCode { kNone, kInvalidRequest, kMissingArtifact,
                       kInvalidArtifact, kMixedGeneration, kExecutionFailed,
                       kPublicationFailed };
const char* ErrorName(ErrorCode code);

class StageError : public std::runtime_error {
 public:
  StageError(ErrorCode code, const std::string& message)
      : std::runtime_error(message), code_(code) {}
  ErrorCode code() const { return code_; }
 private:
  ErrorCode code_;
};

enum class ArtifactEncoding { kBinary, kSqlite, kText };
struct RegisteredSchema {
  const char* name;
  ArtifactEncoding encoding;
  std::uint64_t magic;
  std::uint32_t version;
};
// Only explicitly registered versions are accepted. This registry describes
// envelopes; circuit/record semantics remain independently checked by stages.
const RegisteredSchema& FindSchema(const std::string& name);

struct FileGeneration {
  std::uint64_t device = 0, inode = 0, bytes = 0;
  std::int64_t modified_seconds = 0, modified_nanoseconds = 0;
  std::int64_t changed_seconds = 0, changed_nanoseconds = 0;
  bool operator==(const FileGeneration& other) const;
};

// A reference binds a schema and a particular immutable file generation.
// It never hashes or loads its payload. An explicitly allowed pipe is instead
// validated by the stage's streaming parser, including its EOF/checksum seal.
class ArtifactRef {
 public:
  static ArtifactRef Bind(const std::string& path, const std::string& schema,
                          bool allow_pipe = false);
  const std::string& path() const { return path_; }
  const std::string& schema() const { return schema_; }
  const FileGeneration& generation() const { return generation_; }
  bool is_pipe() const { return pipe_; }
  void RequireUnchanged() const;
 private:
  std::string path_, schema_;
  FileGeneration generation_;
  bool pipe_ = false;
};

struct StageMetrics {
  double wall_seconds = 0, user_cpu_seconds = 0, system_cpu_seconds = 0;
  std::uint64_t process_peak_rss_kib = 0;
  bool has_io_counters = false;
  std::uint64_t read_bytes = 0, write_bytes = 0;
};
struct StageResult {
  std::string stage;
  ErrorCode error = ErrorCode::kNone;
  std::string message;
  StageMetrics metrics;
  std::vector<ArtifactRef> inputs;
  std::vector<ArtifactRef> outputs;
  bool ok() const { return error == ErrorCode::kNone; }
};

// The native compiler and CLI share this lifecycle. The body runs in-process;
// this layer never dispatches Python, materializes graphs, or repeats hashes.
StageResult RunStage(const std::string& stage,
                     const std::function<std::vector<ArtifactRef>()>& bind,
                     const std::function<void(const std::vector<ArtifactRef>&)>& body,
                     const std::function<std::vector<ArtifactRef>()>& outputs = {});
std::string StageResultJson(const StageResult& result);
void PublishStageResult(const std::string& path, const StageResult& result);

}  // namespace emuflow::core
