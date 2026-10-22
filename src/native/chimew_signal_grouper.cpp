// SPDX-License-Identifier: Apache-2.0
// Independent implementation of Chimew Algorithm 1 (FPGA 2026).

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <vector>

#include "emuflow_core/native_stages.hpp"

namespace {

struct Signal {
  int domain = -1;
  int guard_lane = -1;
  int ratio = 0;
  int slot = -1;
  std::uint64_t encoding = 0;
};

using BucketKey = std::tuple<int, bool, int, int>;

int popcount(std::uint64_t value) {
  return __builtin_popcountll(value);
}

std::vector<Signal> read_text_input(const std::string& path) {
  std::ifstream stream(path);
  std::string header;
  if (!(stream >> header) || header != "EMUFLOW_CHIMEW_GROUPER_INPUT_V2") {
    throw std::runtime_error("invalid Chimew grouper input header");
  }
  std::vector<Signal> signals;
  std::string record;
  while (stream >> record) {
    if (record != "SIGNAL") {
      throw std::runtime_error("invalid Chimew grouper record");
    }
    Signal signal;
    int declared_index = -1;
    int legacy_domain = -1;
    unsigned long long legacy_encoding = 0;
    if (!(stream >> declared_index >> legacy_domain >> signal.ratio >> signal.slot >>
          legacy_encoding)) {
      throw std::runtime_error("malformed Chimew signal record");
    }
    signal.domain = legacy_domain;
    signal.encoding = legacy_encoding;
    if (declared_index != static_cast<int>(signals.size()) ||
        signal.domain < 0 || signal.ratio <= 0 || signal.slot < 0) {
      throw std::runtime_error("invalid Chimew signal identity");
    }
    signals.push_back(signal);
  }
  if (signals.empty()) {
    throw std::runtime_error("Chimew grouper requires at least one signal");
  }
  return signals;
}

void read_exact(std::istream& stream, void* destination, std::size_t size,
                const char* label) {
  if (!stream.read(static_cast<char*>(destination),
                   static_cast<std::streamsize>(size))) {
    throw std::runtime_error(std::string("truncated Chimew typed input ") +
                             label);
  }
}

void hash_bytes(std::uint32_t& checksum, const unsigned char* data,
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

template <typename Unsigned>
Unsigned read_little_endian(std::istream& stream, std::uint32_t* checksum,
                            const char* label) {
  static_assert(std::is_unsigned_v<Unsigned>);
  std::array<unsigned char, sizeof(Unsigned)> bytes{};
  read_exact(stream, bytes.data(), bytes.size(), label);
  if (checksum != nullptr) {
    hash_bytes(*checksum, bytes.data(), bytes.size());
  }
  Unsigned result = 0;
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    result |= static_cast<Unsigned>(bytes[index]) << (8 * index);
  }
  return result;
}

template <typename Unsigned>
Unsigned decode_little_endian(const unsigned char* bytes) {
  static_assert(std::is_unsigned_v<Unsigned>);
  Unsigned result = 0;
  for (std::size_t index = 0; index < sizeof(Unsigned); ++index) {
    result |= static_cast<Unsigned>(bytes[index]) << (8 * index);
  }
  return result;
}

std::int32_t decode_i32(const unsigned char* bytes) {
  const std::uint32_t bits = decode_little_endian<std::uint32_t>(bytes);
  std::int32_t result = 0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

int hex_nibble(char value) {
  if (value >= '0' && value <= '9') {
    return value - '0';
  }
  if (value >= 'a' && value <= 'f') {
    return value - 'a' + 10;
  }
  throw std::runtime_error("invalid Chimew expected logical digest");
}

std::array<unsigned char, 32> parse_digest(const std::string& value) {
  if (value.size() != 64) {
    throw std::runtime_error("invalid Chimew expected logical digest");
  }
  std::array<unsigned char, 32> digest{};
  for (std::size_t index = 0; index < digest.size(); ++index) {
    digest[index] = static_cast<unsigned char>(
        (hex_nibble(value[index * 2]) << 4) |
        hex_nibble(value[index * 2 + 1]));
  }
  return digest;
}

int checked_count(std::uint64_t value, const char* label) {
  if (value == 0 ||
      value > static_cast<std::uint64_t>(std::numeric_limits<int>::max())) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
  return static_cast<int>(value);
}

std::vector<Signal> read_typed_input(
    const std::string& path,
    const std::array<unsigned char, 32>& expected_digest) {
  std::ifstream input_file;
  std::istream* stream = &std::cin;
  if (path != "-") {
    input_file.open(path, std::ios::binary);
    if (!input_file) {
      throw std::runtime_error("cannot open Chimew typed input");
    }
    stream = &input_file;
  }

  std::uint32_t checksum = 0;
  std::array<unsigned char, 8> magic{};
  read_exact(*stream, magic.data(), magic.size(), "header");
  hash_bytes(checksum, magic.data(), magic.size());
  const std::array<unsigned char, 8> expected_magic = {
      'E', 'M', 'C', 'H', 'G', 'R', '3', 0};
  if (magic != expected_magic ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "version") != 1 ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "endianness") !=
          0x01020304U) {
    throw std::runtime_error("invalid Chimew typed input header");
  }
  const int signal_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "signal count"),
      "signal count");
  std::array<unsigned char, 32> source_digest{};
  read_exact(*stream, source_digest.data(), source_digest.size(),
             "source digest");
  hash_bytes(checksum, source_digest.data(), source_digest.size());
  if (source_digest != expected_digest) {
    throw std::runtime_error("Chimew typed input logical digest disagrees");
  }

  std::vector<Signal> signals;
  signals.reserve(signal_count);
  Sha256 observed_digest;
  for (int index = 0; index < signal_count; ++index) {
    std::array<unsigned char, 25> record{};
    read_exact(*stream, record.data(), record.size(), "signal record");
    hash_bytes(checksum, record.data(), record.size());
    observed_digest.update(record.data(), record.size());
    if (record[0] != 'S') {
      throw std::runtime_error("invalid Chimew typed input signal tag");
    }
    Signal signal;
    signal.domain = decode_i32(record.data() + 1);
    signal.guard_lane = decode_i32(record.data() + 5);
    signal.ratio = decode_i32(record.data() + 9);
    signal.slot = decode_i32(record.data() + 13);
    signal.encoding = decode_little_endian<std::uint64_t>(record.data() + 17);
    if (signal.domain < 0 || signal.guard_lane < -1 || signal.ratio <= 0 ||
        signal.slot < 0) {
      throw std::runtime_error("invalid Chimew typed input signal");
    }
    signals.push_back(signal);
  }
  const std::array<unsigned char, 32> actual_digest =
      observed_digest.finish();

  std::array<unsigned char, 8> end_magic{};
  read_exact(*stream, end_magic.data(), end_magic.size(), "trailer");
  const std::array<unsigned char, 8> expected_end = {
      'E', 'M', 'C', 'H', 'G', 'R', 'E', 0};
  const std::uint64_t declared_checksum =
      read_little_endian<std::uint64_t>(*stream, nullptr, "checksum");
  std::array<unsigned char, 32> trailer_digest{};
  read_exact(*stream, trailer_digest.data(), trailer_digest.size(),
             "trailer digest");
  if (end_magic != expected_end || declared_checksum != checksum ||
      actual_digest != expected_digest || source_digest != expected_digest ||
      trailer_digest != expected_digest ||
      stream->peek() != std::char_traits<char>::eof()) {
    throw std::runtime_error("Chimew typed input seal disagrees");
  }
  return signals;
}

BucketKey bucket_key(const Signal& signal) {
  return {signal.domain, signal.guard_lane >= 0, signal.guard_lane,
          signal.ratio};
}

void group_bucket(std::vector<Signal>& signals,
                  const std::vector<int>& ordered, std::size_t begin,
                  std::size_t end, int ratio, int& group_id,
                  long long& crossing_bits, std::size_t& assigned_count) {
  std::map<std::uint64_t, std::deque<int>> remaining_by_encoding;
  for (std::size_t position = begin; position < end; ++position) {
    const int index = ordered[position];
    remaining_by_encoding[signals[index].encoding].push_back(index);
  }

  int remaining_count = static_cast<int>(end - begin);
  while (remaining_count > 0) {
    std::set<int> used_slots;
    bool have_seed = false;
    std::uint64_t target = 0;
    std::tuple<int, std::uint64_t, int> seed_key;
    for (const auto& [encoding, indices] : remaining_by_encoding) {
      if (indices.empty()) {
        continue;
      }
      const auto key =
          std::make_tuple(-popcount(encoding), ~encoding, indices.front());
      if (!have_seed || key < seed_key) {
        have_seed = true;
        seed_key = key;
        target = encoding;
      }
    }
    int group_size = 0;
    while (remaining_count > 0 && group_size < ratio) {
      bool have_best = false;
      std::uint64_t best_encoding = 0;
      std::tuple<int, int, int, int, std::uint64_t, int> best_key{
          4, 0, 0, 0, 0, 0};
      int best_signal = -1;
      for (const auto& [encoding, indices] : remaining_by_encoding) {
        if (indices.empty()) {
          continue;
        }
        const auto compatible = std::find_if(
            indices.begin(), indices.end(), [&](int signal) {
              return used_slots.count(signals[signal].slot) == 0;
            });
        if (compatible == indices.end()) {
          continue;
        }
        int category = 2;
        if (encoding == target) {
          category = 0;
        } else if ((encoding | target) == target) {
          category = 1;
        }
        const int different = popcount(encoding ^ target);
        const auto key = std::make_tuple(
            category, category == 2 ? different : 0, -popcount(encoding),
            static_cast<int>(indices.size()), ~encoding, *compatible);
        if (!have_best || key < best_key) {
          have_best = true;
          best_encoding = encoding;
          best_signal = *compatible;
          best_key = key;
        }
      }
      if (!have_best) {
        break;
      }
      auto& selected_indices = remaining_by_encoding[best_encoding];
      const auto selected_position =
          std::find(selected_indices.begin(), selected_indices.end(), best_signal);
      selected_indices.erase(selected_position);
      target |= best_encoding;
      // The complete bucket boundary was discovered before grouping starts,
      // and the kernel no longer reads a processed signal's domain.  Reuse
      // that fixed-width field for the final group ID instead of retaining a
      // second signal-sized assignment vector.
      signals[best_signal].domain = group_id;
      used_slots.insert(signals[best_signal].slot);
      ++group_size;
      --remaining_count;
      ++assigned_count;
    }
    if (group_size == 0) {
      throw std::runtime_error("Chimew grouping made no progress");
    }
    crossing_bits += popcount(target);
    ++group_id;
  }
}

void run(std::vector<Signal> signals, const std::string& output_path) {
  // Algorithm 1 still requires fixed-width signal and ordering arrays.  The
  // former graph-sized vector<vector<int>> is gone, and completed group IDs
  // overwrite the no-longer-needed domain column so a separate assignment
  // array is not retained either.
  std::vector<int> ordered(signals.size());
  std::iota(ordered.begin(), ordered.end(), 0);
  std::sort(ordered.begin(), ordered.end(), [&](int lhs, int rhs) {
    return std::make_tuple(bucket_key(signals[lhs]), lhs) <
           std::make_tuple(bucket_key(signals[rhs]), rhs);
  });

  int group_id = 0;
  long long crossing_bits = 0;
  std::size_t assigned_count = 0;
  std::size_t begin = 0;
  while (begin < ordered.size()) {
    const BucketKey key = bucket_key(signals[ordered[begin]]);
    std::size_t end = begin + 1;
    while (end < ordered.size() && bucket_key(signals[ordered[end]]) == key) {
      ++end;
    }
    group_bucket(signals, ordered, begin, end,
                 signals[ordered[begin]].ratio, group_id, crossing_bits,
                 assigned_count);
    begin = end;
  }
  if (assigned_count != signals.size()) {
    throw std::runtime_error("incomplete Chimew grouping assignment");
  }

  std::ofstream output_file;
  std::ostream* output = &std::cout;
  if (output_path != "-") {
    output_file.open(output_path);
    if (!output_file) {
      throw std::runtime_error("cannot open Chimew grouper output");
    }
    output = &output_file;
  }
  *output << "EMUFLOW_CHIMEW_GROUPER_OUTPUT_V2\n";
  *output << "METRIC " << group_id << " " << crossing_bits << "\n";
  for (int index = 0; index < static_cast<int>(signals.size()); ++index) {
    *output << "ASSIGN " << index << " " << signals[index].domain << "\n";
  }
}

}  // namespace

void emuflow::native::GroupChimewSignals(
    const std::string& input_path, const std::string& output_path,
    bool typed_input, const std::string& expected_sha256) {
  run(typed_input ? read_typed_input(input_path, parse_digest(expected_sha256))
                  : read_text_input(input_path),
      output_path);
}

#if !defined(EMUFLOW_NATIVE_STAGE_LIBRARY)
int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_chimew_signal_grouper "
                 "--typed-input INPUT OUTPUT EXPECTED_SHA256 | "
                 "--legacy-text INPUT OUTPUT\n";
    return 0;
  }
  const bool typed_input =
      argc == 5 && std::string(argv[1]) == "--typed-input";
  const bool legacy_text =
      argc == 4 && std::string(argv[1]) == "--legacy-text";
  if (!typed_input && !legacy_text) {
    std::cerr << "usage: emuflow_chimew_signal_grouper "
                 "--typed-input INPUT OUTPUT EXPECTED_SHA256 | "
                 "--legacy-text INPUT OUTPUT\n";
    return 2;
  }
  try {
    emuflow::native::GroupChimewSignals(
        argv[2], argv[3], typed_input, typed_input ? argv[4] : "");
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_chimew_signal_grouper: " << error.what() << "\n";
    return 1;
  }
}
#endif
