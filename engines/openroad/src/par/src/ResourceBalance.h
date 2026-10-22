// Componentwise resource bounds. std::vector relational operators are
// lexicographic and must never be used to check independent resources.
#pragma once

#include <cmath>
#include <cstddef>
#include <vector>

namespace par {

inline bool ResourceWithin(const std::vector<float> &load,
                           const std::vector<float> &upper) {
  if (load.size() != upper.size()) {
    return false;
  }
  for (std::size_t dim = 0; dim < load.size(); ++dim) {
    if (!std::isfinite(load[dim]) || !std::isfinite(upper[dim]) ||
        load[dim] > upper[dim]) {
      return false;
    }
  }
  return true;
}

inline bool ResourceWithin(const std::vector<std::vector<float>> &load,
                           const std::vector<std::vector<float>> &upper) {
  if (load.size() != upper.size()) {
    return false;
  }
  for (std::size_t part = 0; part < load.size(); ++part) {
    if (!ResourceWithin(load[part], upper[part])) {
      return false;
    }
  }
  return true;
}

inline bool ResourceBalanced(const std::vector<std::vector<float>> &load,
                             const std::vector<std::vector<float>> &lower,
                             const std::vector<std::vector<float>> &upper) {
  return ResourceWithin(load, upper) && ResourceWithin(lower, load);
}

} // namespace par
