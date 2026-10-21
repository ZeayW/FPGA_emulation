#include "emuflow_core/artifact_io.hpp"

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <stdexcept>

#if defined(_WIN32)
#include <process.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace emuflow::core {
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
  if (descriptor < 0) throw std::runtime_error("cannot open native output for sync");
  const int result = fsync(descriptor);
  const int saved_errno = errno;
  close(descriptor);
  if (result != 0) {
    throw std::runtime_error(std::string("cannot sync native output: ") +
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

BinaryReader::BinaryReader(const std::string& path) {
  std::ifstream stream(path, std::ios::binary | std::ios::ate);
  if (!stream) throw std::runtime_error("cannot open native input");
  const auto size = stream.tellg();
  if (size < 0) throw std::runtime_error("cannot size native input");
  bytes_.resize(static_cast<std::size_t>(size));
  stream.seekg(0);
  if (!bytes_.empty()) {
    stream.read(bytes_.data(), static_cast<std::streamsize>(bytes_.size()));
  }
  if (!stream) throw std::runtime_error("cannot read native input");
}

void BinaryReader::RequireEnd() const {
  if (offset_ != bytes_.size()) {
    throw std::runtime_error("native input has trailing bytes");
  }
}

AtomicBinaryWriter::AtomicBinaryWriter(const std::string& target_path)
    : target_path_(target_path) {
  const auto sequence = temporary_sequence.fetch_add(1);
  temporary_path_ = target_path_ + ".tmp." + std::to_string(ProcessId()) +
                    "." + std::to_string(sequence);
  stream_.open(temporary_path_, std::ios::binary | std::ios::trunc);
  if (!stream_) throw std::runtime_error("cannot open native output");
}

AtomicBinaryWriter::~AtomicBinaryWriter() {
  if (stream_.is_open()) stream_.close();
  if (!committed_) std::remove(temporary_path_.c_str());
}

void AtomicBinaryWriter::Commit() {
  if (committed_) throw std::runtime_error("native output is already committed");
  stream_.flush();
  if (!stream_) throw std::runtime_error("cannot flush native output");
  stream_.close();
  SyncFile(temporary_path_);
  std::error_code error;
  std::filesystem::rename(temporary_path_, target_path_, error);
  if (error) {
    throw std::runtime_error("cannot atomically publish native output: " +
                             error.message());
  }
  SyncDirectory(std::filesystem::path(target_path_).parent_path());
  committed_ = true;
}

}  // namespace emuflow::core
