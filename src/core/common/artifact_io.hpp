#pragma once

#include "common/stage_runtime.hpp"

#include <cstddef>
#include <cstdint>
#include <fstream>
#include <functional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace emuflow::core {

// Locale-independent finite decimal numbers for engine text protocols.
double ParseFiniteDecimal(const std::string& text,const std::string& context);

struct ArtifactSchema {
  std::uint64_t magic;
  std::uint32_t version;
  const char* name;
};

// Checked little-endian host artifact reader used by versioned, same-build
// native boundaries. Stable schemas still declare their own magic/version;
// this class owns only bounds checking and complete-consumption enforcement.
class BinaryReader {
 public:
  explicit BinaryReader(const std::string& path);

  template <typename T>
  T Read() {
    static_assert(std::is_trivially_copyable<T>::value,
                  "artifact fields must be trivially copyable");
    if (remaining() < sizeof(T)) {
      throw StageError(ErrorCode::kInvalidArtifact,"native input is truncated");
    }
    T value{};
    stream_.read(reinterpret_cast<char*>(&value), sizeof(T));
    if (!stream_) throw StageError(ErrorCode::kInvalidArtifact,"cannot read native input");
    offset_ += sizeof(T);
    return value;
  }

  void RequireEnd() const;
  void Seek(std::size_t offset);
  void ReadBytes(void* destination, std::size_t size);
  // Artifact identities are nonempty by default. Protocol fields such as argv
  // elements and environment values explicitly permit an empty string.
  std::string ReadString32(bool allow_empty = false);
  std::size_t size() const { return size_; }
  std::size_t remaining() const { return size_ - offset_; }

 private:
  std::ifstream stream_;
  std::size_t size_ = 0;
  std::size_t offset_ = 0;
};

// Stable native schemas keep their own magic and version. Centralizing the
// envelope check gives every producer the same truncation and version failure
// behavior without teaching the shared core any stage-specific payload.
void ReadAndValidateHeader(BinaryReader& reader, const ArtifactSchema& schema);

// Small versioned requests share the checked reader and trailing CRC protocol.
// Circuit payloads remain in their owning stores rather than these requests.
class CheckedRequestReader {
 public:
  explicit CheckedRequestReader(const std::string& path, std::size_t maximum_bytes);
  template <typename T> T Read() {
    T value{};
    ReadBytes(&value, sizeof value);
    return value;
  }
  void ReadBytes(void* destination, std::size_t size);
  std::string ReadString32(bool allow_empty = false, std::size_t maximum_bytes = 1024*1024);
  std::size_t remaining() const { return reader_.remaining()>=4 ? reader_.remaining()-4 : 0; }
  void RequireEnd();
 private:
  BinaryReader reader_;
  std::uint32_t crc_ = 0;
};

FileGeneration ReadFileGeneration(CheckedRequestReader& reader);

// Publish an already closed same-directory file (including a SQLite store)
// through the same synchronization/rename boundary as binary artifacts.
void PublishTemporaryFile(const std::string& temporary, const std::string& target,
                          const std::vector<ArtifactRef>& inputs = {});

// Publish a new complete stage directory with one rename. Every member is a
// closed regular file or directory; existing destinations, source aliases and
// symlink members are rejected at every depth.
// Sources remain bound through directory synchronization and rollback.
void PublishNewDirectory(const std::string& temporary,const std::string& target,
                         const std::vector<ArtifactRef>& inputs);

// Same-directory exclusive staging with invocation-owned sidecars. Destruction
// happens after reader/writer scopes unwind; it never removes published inputs.
class StagedArtifact {
 public:
  explicit StagedArtifact(const std::string& target,
                          std::vector<std::string> suffixes = {});
  ~StagedArtifact();
  StagedArtifact(const StagedArtifact&) = delete;
  StagedArtifact& operator=(const StagedArtifact&) = delete;
  const std::string& path() const { return path_; }
 private:
  std::string path_;
  std::vector<std::string> suffixes_;
};

// Same-directory temporary publication prevents consumers from observing a
// partial native artifact. Commit synchronizes the file before atomic rename;
// an exception or early exit removes only the unpublished temporary.
class AtomicBinaryWriter {
 public:
  explicit AtomicBinaryWriter(const std::string& target_path,
                              std::vector<ArtifactRef> inputs = {});
  ~AtomicBinaryWriter();

  AtomicBinaryWriter(const AtomicBinaryWriter&) = delete;
  AtomicBinaryWriter& operator=(const AtomicBinaryWriter&) = delete;

  template <typename T>
  void Write(const T& value) {
    static_assert(std::is_trivially_copyable<T>::value,
                  "artifact fields must be trivially copyable");
    stream_.write(reinterpret_cast<const char*>(&value), sizeof(T));
    if (!stream_) throw StageError(ErrorCode::kPublicationFailed,"cannot write native output");
  }

  void WriteBytes(const void* data, std::size_t size);

  void Commit();
  // Streaming text exporters share the same flush/sync/rename owner.
  std::ostream& stream() { return stream_; }
  const std::string& temporary_path() const { return temporary_path_; }

 private:
  std::string target_path_;
  std::string temporary_path_;
  std::vector<ArtifactRef> inputs_;
  std::ofstream stream_;
  bool committed_ = false;
};

// Producer counterpart of CheckedRequestReader. Stage composition uses the
// same source lease, CRC and atomic-publication contract as external callers.
class CheckedRequestWriter {
 public:
  CheckedRequestWriter(const std::string& path,std::vector<ArtifactRef> sources={})
      :writer_(path,std::move(sources)){}
  template<class T>void Put(T value){
    static_assert(std::is_trivially_copyable<T>::value,"request scalar must be trivial");
    Bytes(&value,sizeof value);
  }
  void Text(const std::string& value);
  void Generation(const FileGeneration& value);
  void Finish();
 private:
  void Bytes(const void* value,std::size_t size);
  AtomicBinaryWriter writer_;
  std::uint32_t crc_=0;
};

// Stream a file artifact into the common writer and recheck source generations
// before publication. Failure leaves a prior destination intact. Pipe output
// remains the caller's explicitly uncommitted streaming protocol.
void PublishTextArtifact(const std::string& target,
                         const std::vector<ArtifactRef>& inputs,
                         const std::function<void(std::ostream&)>& produce);

template <typename Tag, typename Representation = std::uint32_t>
class TypedId {
 public:
  using representation_type = Representation;

  explicit constexpr TypedId(Representation value) : value_(value) {}
  constexpr Representation value() const { return value_; }

  friend constexpr bool operator==(TypedId lhs, TypedId rhs) {
    return lhs.value_ == rhs.value_;
  }
  friend constexpr bool operator<(TypedId lhs, TypedId rhs) {
    return lhs.value_ < rhs.value_;
  }

 private:
  Representation value_;
};

}  // namespace emuflow::core
