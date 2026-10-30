#pragma once
#include "storage/store.hpp"
#include <csignal>

namespace emuflow::core {
struct BoundScheduleStore {
  ArtifactRef selector, store;
  std::string header, logical_sha256;
};
// Complete content/ordered-record seals, without rebuilding route/hop objects.
BoundScheduleStore
OpenScheduleStore(const ArtifactRef &selector,
                  const volatile std::sig_atomic_t *cancelled = nullptr);
} // namespace emuflow::core
