#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace emuflow::core {

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
    if (offset_ > bytes_.size() || bytes_.size() - offset_ < sizeof(T)) {
      throw std::runtime_error("native input is truncated");
    }
    T value{};
    std::memcpy(&value, bytes_.data() + offset_, sizeof(T));
    offset_ += sizeof(T);
    return value;
  }

  void RequireEnd() const;
  std::size_t size() const { return bytes_.size(); }
  std::size_t remaining() const { return bytes_.size() - offset_; }

 private:
  std::vector<char> bytes_;
  std::size_t offset_ = 0;
};

// Same-directory temporary publication prevents consumers from observing a
// partial native artifact. Commit synchronizes the file before atomic rename;
// an exception or early exit removes only the unpublished temporary.
class AtomicBinaryWriter {
 public:
  explicit AtomicBinaryWriter(const std::string& target_path);
  ~AtomicBinaryWriter();

  AtomicBinaryWriter(const AtomicBinaryWriter&) = delete;
  AtomicBinaryWriter& operator=(const AtomicBinaryWriter&) = delete;

  template <typename T>
  void Write(const T& value) {
    static_assert(std::is_trivially_copyable<T>::value,
                  "artifact fields must be trivially copyable");
    stream_.write(reinterpret_cast<const char*>(&value), sizeof(T));
    if (!stream_) throw std::runtime_error("cannot write native output");
  }

  void Commit();
  const std::string& temporary_path() const { return temporary_path_; }

 private:
  std::string target_path_;
  std::string temporary_path_;
  std::ofstream stream_;
  bool committed_ = false;
};

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
