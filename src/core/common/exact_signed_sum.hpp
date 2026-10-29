#pragma once
#include <cstdint>

namespace emuflow::core {
// Accumulate the sign of a short linear expression without relying on the
// non-standard __int128 extension or overflowing int64_t.  The certificate
// checks below contain at most three signed int64_t terms, so a two-limb
// unsigned magnitude is more than sufficient while remaining strict C++17.
class ExactSignedSum {
public:
  void add(std::int64_t value, bool negate = false) {
    const bool term_negative = (value < 0) != negate;
    const std::uint64_t magnitude =
        value < 0 ? static_cast<std::uint64_t>(-(value + 1)) + 1U
                  : static_cast<std::uint64_t>(value);
    if (magnitude == 0) {
      return;
    }
    if (high_ == 0 && low_ == 0) {
      negative_ = term_negative;
      low_ = magnitude;
      return;
    }
    if (negative_ == term_negative) {
      const std::uint64_t previous = low_;
      low_ += magnitude;
      if (low_ < previous) {
        ++high_;
      }
      return;
    }
    if (high_ != 0 || low_ >= magnitude) {
      if (low_ < magnitude) {
        --high_;
      }
      low_ -= magnitude;
      if (high_ == 0 && low_ == 0) {
        negative_ = false;
      }
      return;
    }
    low_ = magnitude - low_;
    negative_ = term_negative;
  }

  bool negative() const { return negative_; }

private:
  bool negative_ = false;
  std::uint64_t high_ = 0;
  std::uint64_t low_ = 0;
};

} // namespace emuflow::core
