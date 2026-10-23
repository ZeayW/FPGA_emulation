#pragma once

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace emuflow::core {
// Small typed record serializer, never a whole-graph JSON representation.
// Matches the existing sorted-key, ensure_ascii Python contract exactly.
inline std::string JsonQuote(const std::string& text) {
  std::string out = "\"";
  const auto escaped = [&](std::uint32_t cp) {
    static const char* digits = "0123456789abcdef";
    out += "\\u";
    for (int shift : {12, 8, 4, 0}) out += digits[(cp >> shift) & 15];
  };
  for (std::size_t i = 0; i < text.size();) {
    auto cp = static_cast<unsigned char>(text[i++]);
    std::uint32_t value = cp;
    if (cp >= 128) {
      unsigned count;
      std::uint32_t minimum;
      if (cp >= 0xc2 && cp <= 0xdf) { count = 1; value = cp & 31; minimum = 0x80; }
      else if (cp >= 0xe0 && cp <= 0xef) { count = 2; value = cp & 15; minimum = 0x800; }
      else if (cp >= 0xf0 && cp <= 0xf4) { count = 3; value = cp & 7; minimum = 0x10000; }
      else throw std::runtime_error("invalid UTF-8 in native JSON identity");
      for (unsigned j = 0; j < count; ++j) {
        if (i == text.size() || (static_cast<unsigned char>(text[i]) & 0xc0) != 0x80)
          throw std::runtime_error("invalid UTF-8 in native JSON identity");
        value = (value << 6) | (static_cast<unsigned char>(text[i++]) & 63);
      }
      if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff))
        throw std::runtime_error("invalid UTF-8 in native JSON identity");
    }
    switch (value) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (value < 32 || (value >= 127 && value <= 0xffff)) escaped(value);
        else if (value > 0xffff) { value -= 0x10000; escaped(0xd800 + (value >> 10)); escaped(0xdc00 + (value & 1023)); }
        else out += static_cast<char>(value);
    }
  }
  return out + '"';
}
class JsonRecord {
 public:
  enum class Kind { kString, kInteger, kObject, kArray };
  JsonRecord(const std::string& text) : kind_(Kind::kString), scalar_(JsonQuote(text)) {}
  JsonRecord(const char* text) : JsonRecord(std::string(text)) {}
  JsonRecord(std::uint64_t integer) : kind_(Kind::kInteger), scalar_(std::to_string(integer)) {}
  JsonRecord(std::map<std::string, JsonRecord> object) : kind_(Kind::kObject), object_(std::move(object)) {}
  JsonRecord(std::vector<JsonRecord> array) : kind_(Kind::kArray), array_(std::move(array)) {}
  std::string Encode(bool pretty = false, unsigned level = 0) const {
    if (kind_ == Kind::kString || kind_ == Kind::kInteger) return scalar_;
    const bool object = kind_ == Kind::kObject;
    std::string out = object ? "{" : "[";
    const auto count = object ? object_.size() : array_.size();
    std::size_t position = 0;
    const auto item = [&](const std::string& key, const JsonRecord& value) {
      if (position++) out += pretty ? ",\n" : ",";
      else if (pretty) out += '\n';
      if (pretty) out += std::string((level + 1) * 2, ' ');
      if (object) out += JsonQuote(key) + (pretty ? ": " : ":");
      out += value.Encode(pretty, level + 1);
    };
    if (object) for (const auto& entry : object_) item(entry.first, entry.second);
    else for (const auto& value : array_) item("", value);
    if (pretty && count) out += "\n" + std::string(level * 2, ' ');
    return out + (object ? '}' : ']');
  }
 private:
  Kind kind_;
  std::string scalar_;
  std::map<std::string, JsonRecord> object_;
  std::vector<JsonRecord> array_;
};
}  // namespace emuflow::core
