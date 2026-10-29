#pragma once
#include "common/stage_runtime.hpp"
#include "storage/sqlite.hpp"
#include <set>
namespace emuflow::core {
// One finite primitive model shared by timing export and independent structural
// qualification. Cell populations remain in the borrowed compact IR owner.
struct TimingCellModel {
  std::string json;
  std::set<std::string> used_types;
  bool architecture = false;
};
TimingCellModel BuildTimingCellModel(SqliteDatabase &ir,
                                     const ArtifactRef &base,
                                     const ArtifactRef *architecture = nullptr,
                                     const ArtifactRef *contract = nullptr);
} // namespace emuflow::core
