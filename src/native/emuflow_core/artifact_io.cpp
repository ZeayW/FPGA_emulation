#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"

#include <atomic>
#include <cmath>
#include <cstdlib>
#include <locale.h>
#if defined(__APPLE__)
#include <xlocale.h>
#endif
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <stdexcept>
#include <utility>

#if defined(_WIN32)
#include <process.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace emuflow::core {
FileGeneration ReadFileGeneration(CheckedRequestReader& reader) {
  FileGeneration value;
  value.device=reader.Read<std::uint64_t>(); value.inode=reader.Read<std::uint64_t>();
  value.bytes=reader.Read<std::uint64_t>();
  value.modified_seconds=reader.Read<std::int64_t>(); value.modified_nanoseconds=reader.Read<std::int64_t>();
  value.changed_seconds=reader.Read<std::int64_t>(); value.changed_nanoseconds=reader.Read<std::int64_t>();
  return value;
}

double ParseFiniteDecimal(const std::string& text,const std::string& context) {
  const auto fail=[&](){throw StageError(ErrorCode::kInvalidArtifact,"invalid/nonfinite "+context);};
  if(text.empty())fail();
  for(char c:text)if(!((c>='0' && c<='9') || c=='e' || c=='E' || c=='+' || c=='-' || c=='.' || c==' ' || c=='\r' || c=='\v' || c=='\f'))fail();
  static const locale_t locale=newlocale(LC_NUMERIC_MASK,"C",nullptr);
  if(!locale)throw StageError(ErrorCode::kInvalidArtifact,"cannot create numeric locale");
  char* rest=nullptr;const auto value=strtod_l(text.c_str(),&rest,locale);
  if(rest==text.c_str())fail();while(*rest==' ' || *rest=='\r' || *rest=='\v' || *rest=='\f')++rest;
  if(*rest || !std::isfinite(value))fail();return value;
}
CheckedRequestReader::CheckedRequestReader(const std::string& path, std::size_t maximum_bytes)
    : reader_(path) {
  if (reader_.size() > maximum_bytes || reader_.remaining() < sizeof(std::uint32_t))
    throw StageError(ErrorCode::kInvalidArtifact,"native request exceeds bound or is truncated");
}
void CheckedRequestReader::ReadBytes(void* destination, std::size_t size) {
  if (reader_.remaining() < sizeof(std::uint32_t) || size > reader_.remaining() - sizeof(std::uint32_t))
    throw StageError(ErrorCode::kInvalidArtifact,"native request is truncated");
  reader_.ReadBytes(destination, size);
  UpdateCrc32(crc_, static_cast<const unsigned char*>(destination), size);
}
std::string CheckedRequestReader::ReadString32(bool allow_empty, std::size_t maximum_bytes) {
  const auto size = Read<std::uint32_t>();
  if ((!size && !allow_empty) || size > maximum_bytes || size > remaining())
    throw StageError(ErrorCode::kInvalidArtifact,"native request string exceeds bound or is empty");
  std::string value(size, '\0');
  ReadBytes(value.data(), size);
  return value;
}
void CheckedRequestReader::RequireEnd() {
  const auto expected = reader_.Read<std::uint32_t>();
  reader_.RequireEnd();
  if (expected != crc_) throw StageError(ErrorCode::kInvalidArtifact,"native request checksum disagrees");
}

void BinaryReader::Seek(std::size_t offset) {
  if (offset > size_) throw StageError(ErrorCode::kInvalidArtifact,"native input seek is out of bounds");
  stream_.clear();
  stream_.seekg(static_cast<std::streamoff>(offset));
  if (!stream_) throw StageError(ErrorCode::kInvalidArtifact,"cannot seek native input");
  offset_ = offset;
}

void BinaryReader::ReadBytes(void* destination, std::size_t size) {
  if (size > remaining()) throw StageError(ErrorCode::kInvalidArtifact,"native input is truncated");
  if (size != 0) {
    stream_.read(static_cast<char*>(destination), size);
    if (!stream_) throw StageError(ErrorCode::kInvalidArtifact,"cannot read native input");
  }
  offset_ += size;
}

std::string BinaryReader::ReadString32(bool allow_empty) {
  const auto size = Read<std::uint32_t>();
  if ((!allow_empty && size == 0) || size > remaining()) {
    throw StageError(ErrorCode::kInvalidArtifact,"native input string is invalid");
  }
  std::string value(size, '\0');
  ReadBytes(value.data(), size);
  return value;
}
namespace {

std::atomic<std::uint64_t> temporary_sequence{0};

std::uint64_t ProcessId() {
#if defined(_WIN32)
  return static_cast<std::uint64_t>(_getpid());
#else
  return static_cast<std::uint64_t>(getpid());
#endif
}

void SyncFile(const std::string& path) {
#if !defined(_WIN32)
  const int descriptor = open(path.c_str(), O_RDONLY);
  if (descriptor < 0) throw StageError(ErrorCode::kPublicationFailed,"cannot open native output for sync");
  const int result = fsync(descriptor);
  const int saved_errno = errno;
  close(descriptor);
  if (result != 0) {
    throw StageError(ErrorCode::kPublicationFailed,std::string("cannot sync native output: ") +
                             std::strerror(saved_errno));
  }
#else
  (void)path;
#endif
}

void SyncDirectory(const std::filesystem::path& path) {
#if !defined(_WIN32)
  const auto directory = path.empty() ? std::filesystem::path(".") : path;
  const int descriptor = open(directory.c_str(), O_RDONLY);
  if (descriptor < 0) return;
  fsync(descriptor);
  close(descriptor);
#else
  (void)path;
#endif
}

}  // namespace

BinaryReader::BinaryReader(const std::string& path)
    : stream_(path, std::ios::binary) {
  if (!stream_) throw StageError(ErrorCode::kInvalidArtifact,"cannot open native input");
  std::error_code error;
  const auto file_size = std::filesystem::file_size(path, error);
  if (error || file_size > std::numeric_limits<std::size_t>::max()) {
    throw StageError(ErrorCode::kInvalidArtifact,"cannot size native input");
  }
  size_ = static_cast<std::size_t>(file_size);
}

void BinaryReader::RequireEnd() const {
  if (offset_ != size_) {
    throw StageError(ErrorCode::kInvalidArtifact,"native input has trailing bytes");
  }
}

void ReadAndValidateHeader(BinaryReader& reader, const ArtifactSchema& schema) {
  if (reader.Read<std::uint64_t>() != schema.magic ||
      reader.Read<std::uint32_t>() != schema.version) {
    throw StageError(ErrorCode::kInvalidArtifact,std::string("unsupported ") + schema.name +
                             " header");
  }
}

StagedArtifact::StagedArtifact(const std::string& target,std::vector<std::string> suffixes)
    :suffixes_(std::move(suffixes)) {
  for (const auto& suffix:suffixes_)
    if (suffix.empty() || suffix[0]!='.' || suffix.find('/')!=std::string::npos || suffix.find('\\')!=std::string::npos)
      throw StageError(ErrorCode::kInvalidRequest,"invalid staged artifact sidecar");
#if !defined(_WIN32)
  auto pattern=target+".stage.XXXXXX";
  std::vector<char> name(pattern.begin(),pattern.end()); name.push_back('\0');
  int descriptor=mkstemp(name.data());
  if (descriptor<0) throw StageError(ErrorCode::kPublicationFailed,"cannot stage native artifact");
  close(descriptor); path_=name.data();
#else
  throw StageError(ErrorCode::kPublicationFailed,"exclusive native staging is unavailable");
#endif
}
StagedArtifact::~StagedArtifact() {
  std::error_code error;
  std::filesystem::remove(path_,error);
  for (const auto& suffix:suffixes_) std::filesystem::remove(path_+suffix,error);
}

AtomicBinaryWriter::AtomicBinaryWriter(const std::string& target_path,
                                      std::vector<ArtifactRef> inputs)
    : target_path_(target_path), inputs_(std::move(inputs)) {
  if (target_path_.empty() || target_path_ == "-")
    throw StageError(ErrorCode::kInvalidRequest, "binary artifact requires a file destination");
  for (const auto& input : inputs_) {
    std::error_code error;
    if (!input.is_pipe() &&
        (std::filesystem::equivalent(target_path_,input.path(),error) ||
         std::filesystem::weakly_canonical(target_path_) == std::filesystem::weakly_canonical(input.path())))
      throw StageError(ErrorCode::kInvalidRequest, "artifact destination aliases an input");
    input.RequireUnchanged();
  }
  const auto sequence = temporary_sequence.fetch_add(1);
  temporary_path_ = target_path_ + ".tmp." + std::to_string(ProcessId()) +
                    "." + std::to_string(sequence);
  stream_.open(temporary_path_, std::ios::binary | std::ios::trunc);
  if (!stream_) throw StageError(ErrorCode::kPublicationFailed,"cannot open native output");
}

AtomicBinaryWriter::~AtomicBinaryWriter() {
  if (stream_.is_open()) stream_.close();
  if (!committed_) std::remove(temporary_path_.c_str());
}

void AtomicBinaryWriter::WriteBytes(const void* data, std::size_t size) {
  if (size == 0) return;
  if (size > static_cast<std::size_t>(
                 std::numeric_limits<std::streamsize>::max())) {
    throw StageError(ErrorCode::kPublicationFailed,"native output block is too large");
  }
  stream_.write(static_cast<const char*>(data),
                static_cast<std::streamsize>(size));
  if (!stream_) throw StageError(ErrorCode::kPublicationFailed,"cannot write native output");
}

void AtomicBinaryWriter::Commit() {
  if (committed_) throw StageError(ErrorCode::kPublicationFailed,"native output is already committed");
  stream_.flush();
  if (!stream_) throw StageError(ErrorCode::kPublicationFailed,"cannot flush native output");
  stream_.close();
  PublishTemporaryFile(temporary_path_, target_path_, inputs_);
  committed_ = true;
}

void PublishTextArtifact(const std::string& target,
                         const std::vector<ArtifactRef>& inputs,
                         const std::function<void(std::ostream&)>& produce) {
  if (target.empty() || target == "-")
    throw StageError(ErrorCode::kInvalidRequest, "text artifact requires a file destination");
  AtomicBinaryWriter writer(target,inputs);
  produce(writer.stream());
  writer.Commit();
}

void PublishNewDirectory(const std::string& temporary,const std::string& target,
                         const std::vector<ArtifactRef>& inputs) {
  namespace fs=std::filesystem;
  const fs::path source=temporary,destination=target;
  if(fs::is_symlink(source) || !fs::is_directory(source) || fs::exists(destination) || fs::is_symlink(destination) ||
      fs::weakly_canonical(source.parent_path())!=fs::weakly_canonical(destination.parent_path()))
    throw StageError(ErrorCode::kPublicationFailed,"invalid new stage directory publication");
  for(const auto& input:inputs)input.RequireUnchanged();
  for(const auto& member:fs::directory_iterator(source)) {
    if(member.is_symlink() || !member.is_regular_file())throw StageError(ErrorCode::kPublicationFailed,"stage directory contains a non-file member");
    for(const auto& input:inputs)if(fs::equivalent(member.path(),input.path()))throw StageError(ErrorCode::kPublicationFailed,"stage directory member aliases source");
    SyncFile(member.path().string());
  }
  SyncDirectory(source);
  for(const auto& input:inputs)input.RequireUnchanged();
  fs::rename(source,destination);
  try {SyncDirectory(destination.parent_path());for(const auto& input:inputs)input.RequireUnchanged();}
  catch(...) {
    try{fs::rename(destination,source);SyncDirectory(source.parent_path());}
    catch(...){throw StageError(ErrorCode::kPublicationFailed,"stage directory rollback failed; published generation retained at "+target);}
    throw;
  }
}

void PublishTemporaryFile(const std::string& temporary_path_, const std::string& target_path_,
                          const std::vector<ArtifactRef>& inputs) {
  if (std::filesystem::weakly_canonical(std::filesystem::absolute(temporary_path_).parent_path()) !=
      std::filesystem::weakly_canonical(std::filesystem::absolute(target_path_).parent_path()))
    throw StageError(ErrorCode::kPublicationFailed,"native publication requires a same-directory temporary");
  SyncFile(temporary_path_);
  // Check after synchronization, immediately before replacing the public name.
  // These are generation metadata checks, never another payload/hash scan.
  for (const auto& input : inputs) {
    std::error_code error;
    if (!input.is_pipe() &&
        (std::filesystem::equivalent(target_path_,input.path(),error) ||
         std::filesystem::weakly_canonical(target_path_) == std::filesystem::weakly_canonical(input.path())))
      throw StageError(ErrorCode::kInvalidRequest,"artifact destination aliases an input");
    input.RequireUnchanged();
  }
  std::error_code error;
  std::filesystem::rename(temporary_path_, target_path_, error);
  if (error) {
    throw StageError(ErrorCode::kPublicationFailed,"cannot atomically publish native output: " +
                             error.message());
  }
  SyncDirectory(std::filesystem::path(target_path_).parent_path());
}

}  // namespace emuflow::core
