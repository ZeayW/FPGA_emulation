#pragma once
#include "emuflow_core/sqlite.hpp"
#include <array>
#include <cstdint>
#include <map>

namespace emuflow::core {
inline constexpr std::array<const char*,12> kResourceFields={
  "lut","ff","bram","dsp","carry","bram18k","uram288","dsp48","carry8","io","clock","other"};
struct ResourceRecord {
  std::uint64_t present=0;
  bool exceeds_uint32=false;
  std::array<std::uint32_t,12> amounts{};
};
// Shared compact resource decoder, with a design-independent cache bound.
// Classification can inspect arbitrary nonnegative JSON integers. Consumers of
// uint32 resource columns must also reject exceeds_uint32. References remain
// valid only until the next Get call.
class ResourceView {
 public:
  explicit ResourceView(SqliteDatabase& source);
  const ResourceRecord& Get(std::int64_t sequence);
 private:
  SqliteStatement payload_, fields_;
  std::map<std::int64_t,ResourceRecord> cache_;
};
}
