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
  if (emuflow::core::JsonQuoteUtf8(u8"路径😀\x7f\n\\\"") != u8"\"路径😀\x7f\\n\\\\\\\"\"") return 3;
  if (emuflow::core::JsonQuote(u8"路径😀") != "\"\\u8def\\u5f84\\ud83d\\ude00\"") return 4;
  for (const auto& invalid : {std::string("\xc0\x80"), std::string("\xed\xa0\x80"), std::string("\xf4\x90\x80\x80")}) {
    bool failed = false;
    try { (void)emuflow::core::JsonQuoteUtf8(invalid); }
    catch (const std::runtime_error&) { failed = true; }
    if (!failed) return 5;
    failed=false;
    try { (void)JsonRecord(invalid).EncodeUtf8(); }
    catch (const std::runtime_error&) { failed=true; }
    if (!failed) return 6;
  }
  if (JsonRecord(std::map<std::string,JsonRecord>{{u8"路径",JsonRecord(std::vector<JsonRecord>{u8"😀",1.25})}}).EncodeUtf8() != u8"{\"路径\":[\"😀\",1.25]}") return 7;
  if (argc == 1) return 0;
  std::uint64_t bits;
  while (std::cin >> std::hex >> bits) {
    double value; std::memcpy(&value,&bits,sizeof(value));
    std::cout << JsonRecord(value).Encode() << '\n';
  }
}
