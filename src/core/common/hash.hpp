#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>

namespace emuflow::core {

inline void UpdateCrc32(std::uint32_t& checksum, const unsigned char* data,
                        std::size_t size) {
  checksum ^= 0xffffffffU;
  for (std::size_t index = 0; index < size; ++index) {
    checksum ^= data[index];
    for (int bit = 0; bit < 8; ++bit) {
      checksum = (checksum >> 1) ^
                 (0xedb88320U & (0U - (checksum & 1U)));
    }
  }
  checksum ^= 0xffffffffU;
}

// RapidWright publishes MD5 device inventory checksums. Artifact integrity and
// provenance continue to use SHA-256; this implements that vendor inventory.
class Md5 {
public:
  void update(const unsigned char *data, std::size_t size) {
    bytes_ += size;
    while (size) {
      const auto n = std::min(size, buffer_.size() - used_);
      std::copy_n(data, n, buffer_.begin() + used_);
      data += n;
      size -= n;
      used_ += n;
      if (used_ == 64) {
        transform();
        used_ = 0;
      }
    }
  }
  std::array<unsigned char, 16> finish() {
    const auto bits = bytes_ * 8;
    const unsigned char one = 0x80, zero = 0;
    update(&one, 1);
    while (used_ != 56)
      update(&zero, 1);
    std::array<unsigned char, 8> length{};
    for (unsigned i = 0; i < 8; ++i)
      length[i] = bits >> (8 * i);
    update(length.data(), length.size());
    std::array<unsigned char, 16> result{};
    for (unsigned i = 0; i < 4; ++i)
      for (unsigned j = 0; j < 4; ++j)
        result[4 * i + j] = state_[i] >> (8 * j);
    return result;
  }

private:
  void transform() {
    static constexpr std::uint32_t k[] = {
        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a,
        0xa8304613, 0xfd469501, 0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be,
        0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340,
        0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,
        0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8,
        0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c,
        0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, 0x289b7ec6, 0xeaa127fa,
        0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,
        0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92,
        0xffeff47d, 0x85845dd1, 0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1,
        0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391};
    static constexpr unsigned shifts[] = {7, 12, 17, 22, 5, 9,  14, 20,
                                          4, 11, 16, 23, 6, 10, 15, 21};
    std::array<std::uint32_t, 16> m{};
    for (unsigned i = 0; i < 16; ++i)
      for (unsigned j = 0; j < 4; ++j)
        m[i] |= std::uint32_t(buffer_[4 * i + j]) << (8 * j);
    auto a = state_[0], b = state_[1], c = state_[2], d = state_[3];
    for (unsigned i = 0; i < 64; ++i) {
      std::uint32_t f, g;
      if (i < 16) {
        f = (b & c) | (~b & d);
        g = i;
      } else if (i < 32) {
        f = (d & b) | (~d & c);
        g = (5 * i + 1) % 16;
      } else if (i < 48) {
        f = b ^ c ^ d;
        g = (3 * i + 5) % 16;
      } else {
        f = c ^ (b | ~d);
        g = (7 * i) % 16;
      }
      const auto x = a + f + k[i] + m[g], shift = shifts[4 * (i / 16) + i % 4];
      a = d;
      d = c;
      c = b;
      b += (x << shift) | (x >> (32 - shift));
    }
    state_[0] += a;
    state_[1] += b;
    state_[2] += c;
    state_[3] += d;
  }
  std::array<std::uint32_t, 4> state_ = {0x67452301, 0xefcdab89, 0x98badcfe,
                                         0x10325476};
  std::array<unsigned char, 64> buffer_{};
  std::size_t used_ = 0;
  std::uint64_t bytes_ = 0;
};

class Sha256 {
public:
  void update(const unsigned char* data, std::size_t size) {
    total_bytes_ += size;
    while (size > 0) {
      const std::size_t copied =
          std::min(size, buffer_.size() - buffer_size_);
      std::copy_n(data, copied, buffer_.begin() + buffer_size_);
      buffer_size_ += copied;
      data += copied;
      size -= copied;
      if (buffer_size_ == buffer_.size()) {
        transform(buffer_.data());
        buffer_size_ = 0;
      }
    }
  }

  std::array<unsigned char, 32> finish() {
    const std::uint64_t bit_count = total_bytes_ * 8U;
    const unsigned char marker = 0x80U;
    update(&marker, 1);
    const unsigned char zero = 0;
    while (buffer_size_ != 56) {
      update(&zero, 1);
    }
    std::array<unsigned char, 8> length{};
    for (std::size_t index = 0; index < length.size(); ++index) {
      length[length.size() - 1 - index] =
          static_cast<unsigned char>(bit_count >> (index * 8));
    }
    update(length.data(), length.size());
    std::array<unsigned char, 32> digest{};
    for (std::size_t word = 0; word < state_.size(); ++word) {
      for (int byte = 0; byte < 4; ++byte) {
        digest[word * 4 + byte] = static_cast<unsigned char>(
            state_[word] >> (24 - byte * 8));
      }
    }
    return digest;
  }

 private:
  static std::uint32_t rotate_right(std::uint32_t value, int count) {
    return (value >> count) | (value << (32 - count));
  }

  void transform(const unsigned char* block) {
    static constexpr std::array<std::uint32_t, 64> constants = {
        0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
        0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
        0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
        0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
        0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
        0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
        0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
        0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
        0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
        0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
        0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
        0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
        0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
        0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
        0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
        0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U};
    std::array<std::uint32_t, 64> words{};
    for (int index = 0; index < 16; ++index) {
      const int offset = index * 4;
      words[index] = (static_cast<std::uint32_t>(block[offset]) << 24) |
                     (static_cast<std::uint32_t>(block[offset + 1]) << 16) |
                     (static_cast<std::uint32_t>(block[offset + 2]) << 8) |
                     static_cast<std::uint32_t>(block[offset + 3]);
    }
    for (int index = 16; index < 64; ++index) {
      const std::uint32_t s0 =
          rotate_right(words[index - 15], 7) ^
          rotate_right(words[index - 15], 18) ^
          (words[index - 15] >> 3);
      const std::uint32_t s1 =
          rotate_right(words[index - 2], 17) ^
          rotate_right(words[index - 2], 19) ^
          (words[index - 2] >> 10);
      words[index] = words[index - 16] + s0 + words[index - 7] + s1;
    }
    std::uint32_t a = state_[0];
    std::uint32_t b = state_[1];
    std::uint32_t c = state_[2];
    std::uint32_t d = state_[3];
    std::uint32_t e = state_[4];
    std::uint32_t f = state_[5];
    std::uint32_t g = state_[6];
    std::uint32_t h = state_[7];
    for (int index = 0; index < 64; ++index) {
      const std::uint32_t sum1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^
                                 rotate_right(e, 25);
      const std::uint32_t choose = (e & f) ^ ((~e) & g);
      const std::uint32_t temporary1 =
          h + sum1 + choose + constants[index] + words[index];
      const std::uint32_t sum0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^
                                 rotate_right(a, 22);
      const std::uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
      const std::uint32_t temporary2 = sum0 + majority;
      h = g;
      g = f;
      f = e;
      e = d + temporary1;
      d = c;
      c = b;
      b = a;
      a = temporary1 + temporary2;
    }
    state_[0] += a;
    state_[1] += b;
    state_[2] += c;
    state_[3] += d;
    state_[4] += e;
    state_[5] += f;
    state_[6] += g;
    state_[7] += h;
  }

  std::array<std::uint32_t, 8> state_ = {
      0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
      0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U};
  std::array<unsigned char, 64> buffer_{};
  std::size_t buffer_size_ = 0;
  std::uint64_t total_bytes_ = 0;
};

}  // namespace emuflow::core
