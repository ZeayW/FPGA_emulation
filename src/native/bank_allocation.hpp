#pragma once

#include <algorithm>
#include <limits>
#include <tuple>
#include <utility>

// Same deterministic load prediction as emuflow.bank_allocation.  Ratios are
// deliberately not clipped: excess pressure is visible to the caller.
inline std::pair<int, int> shared_bank_ratios(
    long long forward, long long reverse, int banks, int wires,
    int quantum, int minimum, int infeasible_ratio) {
  auto ratio = [&](long long signals, int count) -> int {
    if (signals <= 0) return 1;
    if (count == 0) return -1;
    const long long lanes = static_cast<long long>(count) * wires;
    const long long raw = std::max<long long>(minimum, (signals + lanes - 1) / lanes);
    const long long value = raw == 1 ? 1 : ((raw + quantum - 1) / quantum) * quantum;
    return static_cast<int>(std::min<long long>(value, std::numeric_limits<int>::max()));
  };
  std::tuple<int, long long, double, double, int> best{
      std::numeric_limits<int>::max(), std::numeric_limits<long long>::max(),
      std::numeric_limits<double>::infinity(), std::numeric_limits<double>::infinity(), banks + 1};
  std::pair<int, int> result{infeasible_ratio, infeasible_ratio};
  for (int left = 0; left <= banks; ++left) {
    const int a = ratio(forward, left), b = ratio(reverse, banks - left);
    if (a < 0 || b < 0) continue;
    const double pressure_a = left ? static_cast<double>(forward) / (static_cast<long long>(left) * wires) : 0.0;
    const double pressure_b = banks - left ? static_cast<double>(reverse) / (static_cast<long long>(banks - left) * wires) : 0.0;
    const auto objective = std::make_tuple(std::max(a, b), static_cast<long long>(a) + b,
        std::max(pressure_a, pressure_b), pressure_a + pressure_b, left);
    if (objective < best) { best = objective; result = {a, b}; }
  }
  return result;
}
