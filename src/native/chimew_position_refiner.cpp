// SPDX-License-Identifier: Apache-2.0
// Deterministic position refinement bounded by Chimew Section 3.3.2.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <vector>

#include "emuflow_core/hash.hpp"
#include "emuflow_core/native_stages.hpp"

namespace {

struct Signal {
  int index = -1;
  int domain = -1;
  int ratio = 0;
  unsigned long long encoding = 0;
  int group = -1;
  double source_y = 0.0;
};

std::vector<Signal> read_input(const std::string& path) {
  std::ifstream stream(path);
  std::string header;
  if (!(stream >> header) || header != "EMUFLOW_CHIMEW_REFINER_INPUT_V1") {
    throw std::runtime_error("invalid Chimew refiner input header");
  }
  std::vector<Signal> signals;
  std::string record;
  while (stream >> record) {
    if (record != "SIGNAL") {
      throw std::runtime_error("invalid Chimew refiner record");
    }
    Signal signal;
    if (!(stream >> signal.index >> signal.domain >> signal.ratio >>
          signal.encoding >> signal.group >> signal.source_y)) {
      throw std::runtime_error("malformed Chimew refiner signal");
    }
    if (signal.index != static_cast<int>(signals.size()) ||
        signal.domain < 0 || signal.ratio <= 0 || signal.group < 0 ||
        !std::isfinite(signal.source_y)) {
      throw std::runtime_error("invalid Chimew refiner signal identity");
    }
    signals.push_back(signal);
  }
  if (signals.empty()) {
    throw std::runtime_error("Chimew refiner requires at least one signal");
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

template <typename Unsigned>
Unsigned decode_little_endian(const unsigned char* bytes) {
  static_assert(std::is_unsigned_v<Unsigned>);
  Unsigned result = 0;
  for (std::size_t index = 0; index < sizeof(Unsigned); ++index) {
    result |= static_cast<Unsigned>(bytes[index]) << (8 * index);
  }
  return result;
}

template <typename Unsigned>
Unsigned read_little_endian(std::istream& stream, std::uint32_t* checksum,
                            const char* label) {
  std::array<unsigned char, sizeof(Unsigned)> bytes{};
  read_exact(stream, bytes.data(), bytes.size(), label);
  if (checksum != nullptr) {
    emuflow::core::UpdateCrc32(*checksum, bytes.data(), bytes.size());
  }
  return decode_little_endian<Unsigned>(bytes.data());
}

std::int32_t decode_i32(const unsigned char* bytes) {
  const std::uint32_t bits = decode_little_endian<std::uint32_t>(bytes);
  std::int32_t result = 0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

double decode_double(const unsigned char* bytes) {
  const std::uint64_t bits = decode_little_endian<std::uint64_t>(bytes);
  double result = 0.0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

int hex_nibble(char value) {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
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

std::vector<Signal> read_typed_input(
    const std::string& path,
    const std::array<unsigned char, 32>& expected_digest) {
  std::ifstream input_file;
  std::istream* stream = &std::cin;
  if (path != "-") {
    input_file.open(path, std::ios::binary);
    if (!input_file) throw std::runtime_error("cannot open Chimew typed input");
    stream = &input_file;
  }
  std::uint32_t checksum = 0;
  std::array<unsigned char, 8> magic{};
  read_exact(*stream, magic.data(), magic.size(), "header");
  emuflow::core::UpdateCrc32(checksum, magic.data(), magic.size());
  const std::array<unsigned char, 8> expected_magic = {
      'E', 'M', 'C', 'H', 'R', 'F', '2', 0};
  if (magic != expected_magic ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "version") != 1 ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "endianness") !=
          0x01020304U) {
    throw std::runtime_error("invalid Chimew typed input header");
  }
  const std::uint64_t raw_count =
      read_little_endian<std::uint64_t>(*stream, &checksum, "signal count");
  if (raw_count == 0 ||
      raw_count > static_cast<std::uint64_t>(std::numeric_limits<int>::max())) {
    throw std::runtime_error("invalid Chimew typed input signal count");
  }
  const int signal_count = static_cast<int>(raw_count);
  std::array<unsigned char, 32> source_digest{};
  read_exact(*stream, source_digest.data(), source_digest.size(),
             "source digest");
  emuflow::core::UpdateCrc32(
      checksum, source_digest.data(), source_digest.size());
  if (source_digest != expected_digest) {
    throw std::runtime_error("Chimew typed input logical digest disagrees");
  }
  std::vector<Signal> signals;
  signals.reserve(signal_count);
  emuflow::core::Sha256 observed_digest;
  for (int index = 0; index < signal_count; ++index) {
    std::array<unsigned char, 33> record{};
    read_exact(*stream, record.data(), record.size(), "signal record");
    emuflow::core::UpdateCrc32(checksum, record.data(), record.size());
    observed_digest.update(record.data(), record.size());
    Signal signal;
    signal.index = decode_i32(record.data() + 1);
    signal.domain = decode_i32(record.data() + 5);
    signal.ratio = decode_i32(record.data() + 9);
    signal.group = decode_i32(record.data() + 13);
    signal.encoding = decode_little_endian<std::uint64_t>(record.data() + 17);
    signal.source_y = decode_double(record.data() + 25);
    if (record[0] != 'S' || signal.index != index || signal.domain < 0 ||
        signal.ratio <= 0 || signal.group < 0 ||
        !std::isfinite(signal.source_y)) {
      throw std::runtime_error("invalid Chimew typed input signal");
    }
    signals.push_back(signal);
  }
  const auto actual_digest = observed_digest.finish();
  std::array<unsigned char, 8> end_magic{};
  read_exact(*stream, end_magic.data(), end_magic.size(), "trailer");
  const std::array<unsigned char, 8> expected_end = {
      'E', 'M', 'C', 'H', 'R', 'F', 'E', 0};
  const std::uint64_t declared_checksum =
      read_little_endian<std::uint64_t>(*stream, nullptr, "checksum");
  std::array<unsigned char, 32> trailer_digest{};
  read_exact(*stream, trailer_digest.data(), trailer_digest.size(),
             "trailer digest");
  if (end_magic != expected_end || declared_checksum != checksum ||
      actual_digest != expected_digest || trailer_digest != expected_digest ||
      stream->peek() != std::char_traits<char>::eof()) {
    throw std::runtime_error("Chimew typed input seal disagrees");
  }
  return signals;
}

std::map<int, std::set<int>> group_members(
    const std::vector<Signal>& signals) {
  std::map<int, std::set<int>> members;
  for (int index = 0; index < static_cast<int>(signals.size()); ++index) {
    members[signals[index].group].insert(index);
  }
  return members;
}

double pairwise_objective(const std::vector<Signal>& signals,
                          const std::map<int, std::set<int>>& members,
                          const std::set<int>& groups) {
  double objective = 0.0;
  for (int group : groups) {
    const auto found = members.find(group);
    if (found == members.end()) {
      continue;
    }
    std::vector<double> values;
    values.reserve(found->second.size());
    for (int index : found->second) {
      values.push_back(signals[index].source_y);
    }
    std::sort(values.begin(), values.end());
    double prefix = 0.0;
    for (std::size_t index = 0; index < values.size(); ++index) {
      objective += values[index] * static_cast<double>(index) - prefix;
      prefix += values[index];
    }
  }
  return objective;
}

double median(std::vector<double> values) {
  if (values.empty()) {
    throw std::runtime_error("cannot calculate an empty median");
  }
  std::sort(values.begin(), values.end());
  const std::size_t middle = values.size() / 2;
  if (values.size() % 2 == 1) {
    return values[middle];
  }
  return (values[middle - 1] + values[middle]) / 2.0;
}

void refine_bucket(std::vector<Signal>& signals,
                   std::map<int, std::set<int>>& members,
                   const std::vector<int>& bucket,
                   unsigned long long encoding, int& accepted_buckets,
                   int& moved_signals) {
  std::map<int, int> slots;
  std::set<int> affected_groups;
  for (int index : bucket) {
    ++slots[signals[index].group];
    affected_groups.insert(signals[index].group);
  }
  if (bucket.size() < 2 || affected_groups.size() < 2) {
    return;
  }

  std::vector<std::pair<double, int>> ordered_groups;
  for (int group : affected_groups) {
    std::vector<double> anchor_values;
    for (int index : members.at(group)) {
      if (signals[index].encoding != encoding) {
        anchor_values.push_back(signals[index].source_y);
      }
    }
    if (anchor_values.empty()) {
      for (int index : members.at(group)) {
        if (signals[index].encoding == encoding) {
          anchor_values.push_back(signals[index].source_y);
        }
      }
    }
    ordered_groups.emplace_back(median(anchor_values), group);
  }
  std::sort(ordered_groups.begin(), ordered_groups.end());

  std::vector<int> ordered_signals = bucket;
  std::sort(ordered_signals.begin(), ordered_signals.end(),
            [&](int lhs, int rhs) {
              return std::make_tuple(signals[lhs].source_y,
                                     signals[lhs].index) <
                     std::make_tuple(signals[rhs].source_y,
                                     signals[rhs].index);
            });

  const double before = pairwise_objective(signals, members, affected_groups);
  std::vector<int> old_groups;
  old_groups.reserve(ordered_signals.size());
  for (int index : ordered_signals) {
    old_groups.push_back(signals[index].group);
  }
  int position = 0;
  for (const auto& [anchor, group] : ordered_groups) {
    (void)anchor;
    for (int slot = 0; slot < slots[group]; ++slot) {
      signals[ordered_signals[position++]].group = group;
    }
  }
  for (int offset = 0; offset < static_cast<int>(ordered_signals.size());
       ++offset) {
    members[old_groups[offset]].erase(ordered_signals[offset]);
  }
  for (int index : ordered_signals) {
    members[signals[index].group].insert(index);
  }
  const double after = pairwise_objective(signals, members, affected_groups);
  if (after > before + 1e-12) {
    for (int offset = 0; offset < static_cast<int>(ordered_signals.size());
         ++offset) {
      members[signals[ordered_signals[offset]].group].erase(
          ordered_signals[offset]);
      signals[ordered_signals[offset]].group = old_groups[offset];
      members[old_groups[offset]].insert(ordered_signals[offset]);
    }
    return;
  }
  int moved = 0;
  for (int offset = 0; offset < static_cast<int>(ordered_signals.size());
       ++offset) {
    moved += signals[ordered_signals[offset]].group != old_groups[offset];
  }
  if (moved > 0) {
    ++accepted_buckets;
    moved_signals += moved;
  }
}

void run(std::vector<Signal> signals, const std::string& output_path) {
  auto members = group_members(signals);
  std::set<int> all_groups;
  std::map<std::tuple<int, int, unsigned long long>, std::vector<int>> buckets;
  for (int index = 0; index < static_cast<int>(signals.size()); ++index) {
    all_groups.insert(signals[index].group);
    buckets[{signals[index].domain, signals[index].ratio,
             signals[index].encoding}]
        .push_back(index);
  }
  const double before = pairwise_objective(signals, members, all_groups);
  int accepted_buckets = 0;
  int moved_signals = 0;
  for (const auto& [key, bucket] : buckets) {
    refine_bucket(signals, members, bucket, std::get<2>(key), accepted_buckets,
                  moved_signals);
  }
  const double after = pairwise_objective(signals, members, all_groups);
  if (after > before + 1e-12) {
    throw std::runtime_error("position refinement increased its objective");
  }

  std::ofstream output_file;
  std::ostream* output = &std::cout;
  if (output_path != "-") {
    output_file.open(output_path);
    if (!output_file) {
      throw std::runtime_error("cannot open Chimew refiner output");
    }
    output = &output_file;
  }
  *output << "EMUFLOW_CHIMEW_REFINER_OUTPUT_V1\n";
  *output << std::setprecision(17);
  *output << "METRIC " << accepted_buckets << " " << moved_signals << " "
          << before << " " << after << "\n";
  std::sort(signals.begin(), signals.end(),
            [](const Signal& lhs, const Signal& rhs) {
              return lhs.index < rhs.index;
            });
  for (const auto& signal : signals) {
    *output << "ASSIGN " << signal.index << " " << signal.group << "\n";
  }
}

}  // namespace

void emuflow::native::RefineChimewPositions(
    const std::string& input_path, const std::string& output_path,
    bool typed_input, const std::string& expected_sha256) {
  run(typed_input ? read_typed_input(input_path, parse_digest(expected_sha256))
                  : read_input(input_path),
      output_path);
}

#if !defined(EMUFLOW_NATIVE_STAGE_LIBRARY)
int main(int argc, char** argv) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "usage: emuflow_chimew_position_refiner "
                 "--typed-input INPUT OUTPUT EXPECTED_SHA256 | "
                 "--legacy-text INPUT OUTPUT\n";
    return 0;
  }
  const bool typed_input =
      argc == 5 && std::string(argv[1]) == "--typed-input";
  const bool legacy_text =
      argc == 4 && std::string(argv[1]) == "--legacy-text";
  if (!typed_input && !legacy_text) {
    std::cerr << "usage: emuflow_chimew_position_refiner "
                 "--typed-input INPUT OUTPUT EXPECTED_SHA256 | "
                 "--legacy-text INPUT OUTPUT\n";
    return 2;
  }
  try {
    emuflow::native::RefineChimewPositions(
        argv[2], argv[3], typed_input, typed_input ? argv[4] : "");
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "emuflow_chimew_position_refiner: " << error.what() << "\n";
    return 1;
  }
}
#endif
