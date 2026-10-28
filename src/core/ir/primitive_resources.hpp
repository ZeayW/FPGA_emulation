#pragma once
#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <string>
namespace emuflow::core {
inline std::array<std::int64_t, 12> ClassifyPrimitive(std::string kind) {
  std::transform(kind.begin(), kind.end(), kind.begin(),
                 [](unsigned char c) { return std::toupper(c); });
  std::array<std::int64_t, 12> r{};
  const auto starts = [&](const std::string &prefix) {
    return kind.rfind(prefix, 0) == 0;
  };
  if (starts("RAMB36"))
    r[5] = 2;
  else if (starts("RAMB18"))
    r[5] = 1;
  else if (kind == "VTR_SP_RAM" || kind == "VTR_DP_RAM")
    r[2] = 1;
  else if (kind == "VTR_MULTIPLY" || kind == "MULTIPLY")
    r[3] = 1;
  else if (starts("URAM288"))
    r[6] = 1;
  else if (starts("DSP48"))
    r[7] = 1;
  else if (starts("CARRY8"))
    r[8] = 1;
  else if (starts("LUT") || kind == "$LUT" || starts("$_LUT"))
    r[0] = 1;
  else if (starts("FD") || starts("$DFF") || starts("$SDFF") ||
           starts("$ADFF") || starts("$_DFF") || starts("$_SDFF") ||
           starts("$_ADFF"))
    r[1] = 1;
  else if (starts("IBUF") || starts("OBUF") || starts("IOBUF"))
    r[9] = 1;
  else if (starts("BUFG") || starts("BUFH") || starts("BUFMR") ||
           starts("MMCM") || starts("PLLE") || starts("STARTUPE"))
    r[10] = 1;
  else if (kind != "GND" && kind != "VCC" && kind != "MUXF7" &&
           kind != "MUXF8" && kind != "MUXF9")
    r[11] = 1;
  return r;
}
} // namespace emuflow::core
