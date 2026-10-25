#include "emuflow_core/json_record.hpp"
#include <cstring>
#include <iostream>
#include <limits>

int main(int argc, char**) {
  using emuflow::core::JsonRecord;
  if (JsonRecord().Encode() != "null" || JsonRecord(true).Encode() != "true" ||
      JsonRecord(false).Encode() != "false" || JsonRecord(-1).Encode() != "-1" ||
      JsonRecord(1.0).Encode() != "1.0" || JsonRecord(-0.0).Encode() != "-0.0" ||
      JsonRecord(1e-5).Encode() != "1e-05" || JsonRecord(1e16).Encode() != "1e+16") return 1;
  bool rejected = false;
  try { (void)JsonRecord(std::numeric_limits<double>::infinity()); }
  catch (const std::runtime_error&) { rejected = true; }
  if (!rejected) return 2;
  if (argc == 1) return 0;
  std::uint64_t bits;
  while (std::cin >> std::hex >> bits) {
    double value; std::memcpy(&value,&bits,sizeof(value));
    std::cout << JsonRecord(value).Encode() << '\n';
  }
}
