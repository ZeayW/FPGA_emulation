// SPDX-License-Identifier: Apache-2.0
// Test-only prior wire adapter for the current Chimew matching kernel.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "common/hash.hpp"
#include "transport/chimew/kernels.hpp"

namespace {

using Point = emuflow::native::ChimewPoint;
using Member = emuflow::native::ChimewBankMember;
using Group = emuflow::native::ChimewBankGroup;
using Channel = emuflow::native::ChimewBankChannel;
using BankPair = emuflow::native::ChimewBankPair;
using Input = emuflow::native::ChimewBankInput;
using AssignmentResult = emuflow::native::ChimewAssignment;
using Stage2Result = emuflow::native::ChimewBankAlternative;

void read_exact(std::istream &stream, void *destination, std::size_t size,
                const char *label) {
  if (!stream.read(static_cast<char *>(destination),
                   static_cast<std::streamsize>(size))) {
    throw std::runtime_error(std::string("truncated Chimew typed input ") +
                             label);
  }
}

template <typename Unsigned>
Unsigned read_little_endian(std::istream &stream, std::uint32_t *checksum,
                            const char *label) {
  static_assert(std::is_unsigned_v<Unsigned>);
  std::array<unsigned char, sizeof(Unsigned)> bytes{};
  read_exact(stream, bytes.data(), bytes.size(), label);
  if (checksum != nullptr) {
    emuflow::core::UpdateCrc32(*checksum, bytes.data(), bytes.size());
  }
  Unsigned result = 0;
  for (std::size_t index = 0; index < bytes.size(); ++index) {
    result |= static_cast<Unsigned>(bytes[index]) << (8 * index);
  }
  return result;
}

std::int32_t read_i32(std::istream &stream, std::uint32_t *checksum,
                      const char *label) {
  const std::uint32_t bits =
      read_little_endian<std::uint32_t>(stream, checksum, label);
  std::int32_t result = 0;
  std::memcpy(&result, &bits, sizeof(result));
  return result;
}

double read_double(std::istream &stream, std::uint32_t *checksum,
                   const char *label) {
  const std::uint64_t bits =
      read_little_endian<std::uint64_t>(stream, checksum, label);
  double result = 0.0;
  static_assert(sizeof(result) == sizeof(bits));
  std::memcpy(&result, &bits, sizeof(result));
  if (!std::isfinite(result)) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
  return result;
}

void expect_tag(std::istream &stream, std::uint32_t &checksum,
                unsigned char expected, const char *label) {
  unsigned char tag = 0;
  read_exact(stream, &tag, 1, label);
  emuflow::core::UpdateCrc32(checksum, &tag, 1);
  if (tag != expected) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
}

int checked_count(std::uint64_t value, const char *label, bool positive) {
  if (value > static_cast<std::uint64_t>(std::numeric_limits<int>::max()) ||
      (positive && value == 0)) {
    throw std::runtime_error(std::string("invalid Chimew typed input ") +
                             label);
  }
  return static_cast<int>(value);
}

Input read_typed_input(const std::string &path) {
  std::ifstream input_file;
  std::istream *stream = &std::cin;
  if (path != "-") {
    input_file.open(path, std::ios::binary);
    if (!input_file) {
      throw std::runtime_error("cannot open Chimew typed input");
    }
    stream = &input_file;
  }
  // Seal scope: the complete fixed-width header and every record byte.  The
  // trailer carrying the declared checksum is outside the checksum itself.
  std::uint32_t checksum = 0;
  std::array<unsigned char, 8> magic{};
  read_exact(*stream, magic.data(), magic.size(), "header");
  emuflow::core::UpdateCrc32(checksum, magic.data(), magic.size());
  const std::array<unsigned char, 8> expected_magic = {'E', 'M', 'C', 'H',
                                                       'B', 'C', '3', 0};
  if (magic != expected_magic ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "version") != 1 ||
      read_little_endian<std::uint32_t>(*stream, &checksum, "endianness") !=
          0x01020304U) {
    throw std::runtime_error("invalid Chimew typed input header");
  }
  const int bank_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "bank count"),
      "bank count", true);
  const int channel_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "channel count"),
      "channel count", true);
  const int group_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "group count"),
      "group count", true);
  const int member_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "member count"),
      "member count", true);
  const int fanin_count = checked_count(
      read_little_endian<std::uint64_t>(*stream, &checksum, "fanin count"),
      "fanin count", true);
  const double cost_scale = read_double(*stream, &checksum, "cost scale");
  if (!(cost_scale > 0.0)) {
    throw std::runtime_error("invalid Chimew typed input cost scale");
  }
  std::array<unsigned char, 32> source_digest{};
  read_exact(*stream, source_digest.data(), source_digest.size(),
             "source digest");
  emuflow::core::UpdateCrc32(checksum, source_digest.data(),
                             source_digest.size());

  Input input;
  input.cost_scale = cost_scale;
  input.banks.reserve(bank_count);
  input.channels.reserve(channel_count);
  input.groups.reserve(group_count);
  int observed_members = 0;
  int observed_fanins = 0;
  for (int bank_index = 0; bank_index < bank_count; ++bank_index) {
    expect_tag(*stream, checksum, 'B', "bank tag");
    BankPair bank;
    bank.index = read_i32(*stream, &checksum, "bank index");
    bank.domain = read_i32(*stream, &checksum, "bank domain");
    bank.bank_a.x = read_double(*stream, &checksum, "bank A x");
    bank.bank_a.y = read_double(*stream, &checksum, "bank A y");
    bank.bank_b.x = read_double(*stream, &checksum, "bank B x");
    bank.bank_b.y = read_double(*stream, &checksum, "bank B y");
    const int local_channels =
        checked_count(read_little_endian<std::uint64_t>(*stream, &checksum,
                                                        "bank channel count"),
                      "bank channel count", true);
    if (bank.index != bank_index || bank.domain < 0 ||
        local_channels >
            channel_count - static_cast<int>(input.channels.size())) {
      throw std::runtime_error("invalid Chimew typed input bank");
    }
    input.banks.push_back(std::move(bank));
    for (int order = 0; order < local_channels; ++order) {
      expect_tag(*stream, checksum, 'C', "channel tag");
      Channel channel;
      channel.index = read_i32(*stream, &checksum, "channel index");
      channel.bank = read_i32(*stream, &checksum, "channel bank");
      channel.order = read_i32(*stream, &checksum, "channel order");
      channel.pin_a.x = read_double(*stream, &checksum, "channel A x");
      channel.pin_a.y = read_double(*stream, &checksum, "channel A y");
      channel.pin_b.x = read_double(*stream, &checksum, "channel B x");
      channel.pin_b.y = read_double(*stream, &checksum, "channel B y");
      if (channel.index != static_cast<int>(input.channels.size()) ||
          channel.bank != bank_index || channel.order != order) {
        throw std::runtime_error("invalid Chimew typed input channel");
      }
      input.banks.back().channels.push_back(channel.index);
      input.channels.push_back(std::move(channel));
    }
  }
  if (static_cast<int>(input.channels.size()) != channel_count) {
    throw std::runtime_error("Chimew typed input channel count disagrees");
  }
  for (int group_index = 0; group_index < group_count; ++group_index) {
    expect_tag(*stream, checksum, 'G', "group tag");
    Group group;
    group.index = read_i32(*stream, &checksum, "group index");
    group.domain = read_i32(*stream, &checksum, "group domain");
    group.kind = read_i32(*stream, &checksum, "group kind");
    group.direction = read_i32(*stream, &checksum, "group direction");
    group.expected_members =
        checked_count(read_little_endian<std::uint64_t>(*stream, &checksum,
                                                        "group member count"),
                      "group member count", true);
    if (group.index != group_index || group.domain < 0 ||
        (group.kind != 0 && group.kind != 1) ||
        (group.direction != 0 && group.direction != 1 &&
         group.direction != 2) ||
        (group.kind == 1 && group.expected_members != 1) ||
        group.expected_members > member_count - observed_members) {
      throw std::runtime_error("invalid Chimew typed input group");
    }
    group.members.reserve(group.expected_members);
    for (int member_index = 0; member_index < group.expected_members;
         ++member_index) {
      expect_tag(*stream, checksum, 'M', "member tag");
      const int declared_index = read_i32(*stream, &checksum, "member index");
      Member member;
      member.direction = read_i32(*stream, &checksum, "member direction");
      member.timing_weight =
          read_double(*stream, &checksum, "member timing weight");
      member.fanout.x = read_double(*stream, &checksum, "member fanout x");
      member.fanout.y = read_double(*stream, &checksum, "member fanout y");
      const int local_fanins =
          checked_count(read_little_endian<std::uint64_t>(*stream, &checksum,
                                                          "member fanin count"),
                        "member fanin count", true);
      if (declared_index != member_index ||
          (member.direction != 0 && member.direction != 1) ||
          (group.direction != 2 && member.direction != group.direction) ||
          !(member.timing_weight > 0.0) ||
          local_fanins > fanin_count - observed_fanins) {
        throw std::runtime_error("invalid Chimew typed input member");
      }
      member.fanins.reserve(local_fanins);
      for (int fanin = 0; fanin < local_fanins; ++fanin) {
        expect_tag(*stream, checksum, 'F', "fanin tag");
        Point point;
        point.x = read_double(*stream, &checksum, "fanin x");
        point.y = read_double(*stream, &checksum, "fanin y");
        member.fanins.push_back(point);
        ++observed_fanins;
      }
      group.members.push_back(std::move(member));
      ++observed_members;
    }
    if (group.direction == 2) {
      bool seen_direction[2] = {false, false};
      for (const Member &member : group.members) {
        seen_direction[member.direction] = true;
      }
      if (!seen_direction[0] || !seen_direction[1]) {
        throw std::runtime_error(
            "bidirectional Chimew typed bundle lacks one direction");
      }
    }
    input.groups.push_back(std::move(group));
  }
  if (observed_members != member_count || observed_fanins != fanin_count) {
    throw std::runtime_error("Chimew typed input relation counts disagree");
  }
  std::array<unsigned char, 8> end_magic{};
  read_exact(*stream, end_magic.data(), end_magic.size(), "trailer");
  const std::array<unsigned char, 8> expected_end = {'E', 'M', 'C', 'H',
                                                     'E', 'N', 'D', 0};
  const std::uint64_t declared_checksum =
      read_little_endian<std::uint64_t>(*stream, nullptr, "checksum");
  std::array<unsigned char, 32> trailer_digest{};
  read_exact(*stream, trailer_digest.data(), trailer_digest.size(),
             "trailer digest");
  if (end_magic != expected_end || declared_checksum != checksum ||
      trailer_digest != source_digest ||
      stream->peek() != std::char_traits<char>::eof()) {
    throw std::runtime_error("Chimew typed input seal disagrees");
  }
  return input;
}

void write_certificate(std::ostream &output, const std::string &label,
                       const AssignmentResult &result) {
  output << "CERT " << label << " " << result.potentials.size() << "\n";
  for (int node = 0; node < static_cast<int>(result.potentials.size());
       ++node) {
    output << "POT " << label << " " << node << " " << result.potentials[node]
           << "\n";
  }
}

} // namespace
void WriteReference(const std::string &input_path, std::ostream &output) {
  const auto input = read_typed_input(input_path);
  const auto solution = emuflow::native::AssignChimewBankRecords(input);
  output << "EMUFLOW_CHIMEW_BANK_CHANNEL_OUTPUT_V1\n";
  output << "METRIC " << input.groups.size() << " "
         << solution.stage1.total_cost << " " << solution.channel_cost << "\n";
  for (int group = 0; group < static_cast<int>(input.groups.size()); ++group) {
    output << "BANK_ASSIGN " << group << " "
           << solution.stage1.right_for_left[group] << " "
           << solution.stage1.cost_for_left[group] << "\n";
  }
  write_certificate(output, "STAGE1", solution.stage1);
  for (int bank = 0; bank < static_cast<int>(input.banks.size()); ++bank) {
    if (solution.alternatives[bank].first.groups.empty()) {
      continue;
    }
    output << "CHOSEN " << bank << " " << solution.chosen_priority[bank]
           << "\n";
    for (int priority = 0; priority < 2; ++priority) {
      const Stage2Result &result = priority == 0
                                       ? solution.alternatives[bank].first
                                       : solution.alternatives[bank].second;
      const std::string label =
          "BANK" + std::to_string(bank) + "P" + std::to_string(priority);
      output << "ALTERNATIVE " << bank << " " << priority << " "
             << result.assignment.total_cost << "\n";
      for (int left = 0; left < static_cast<int>(result.groups.size());
           ++left) {
        const int local_channel = result.assignment.right_for_left[left];
        output << "CHANNEL_ASSIGN " << bank << " " << priority << " "
               << result.groups[left] << " "
               << input.banks[bank].channels[local_channel] << " "
               << result.assignment.cost_for_left[left] << "\n";
      }
      write_certificate(output, label, result.assignment);
    }
  }
}

int main(int argc, char **argv) {
  try {
    if (argc != 4 || std::string(argv[1]) != "--typed-input")
      throw std::runtime_error(
          "test oracle expects --typed-input INPUT OUTPUT");
    if (std::string(argv[3]) == "-") {
      WriteReference(argv[2], std::cout);
    } else {
      std::ofstream output(argv[3]);
      if (!output)
        throw std::runtime_error("cannot open test oracle output");
      WriteReference(argv[2], output);
    }
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
