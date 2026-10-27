#pragma once

#include <cstdint>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#include <cmath>
#include <type_traits>
#include <sstream>
#include <iomanip>
#include <locale>
#include <cstdlib>
#include <locale.h>
#include <functional>
#if __has_include(<charconv>)
#include <charconv>
#define EMUFLOW_HAS_CHARCONV_HEADER 1
#endif
#include <string_view>
#if defined(__APPLE__)
#include <xlocale.h>
#endif

namespace emuflow::core {
// Whitespace formatting only: callers validate the canonical JSON with the
// owning SQLite/request contract. Preserve producer number/string spellings;
// never build another JSON tree merely to hash its pretty representation.
inline void VisitPrettyCanonicalJson(const std::string& value, unsigned level,
                                    const std::function<void(std::string_view)>& emit) {
  std::string buffer;
  buffer.reserve(4096);
  unsigned depth = 0;
  bool quoted = false, escaped = false;
  const auto flush = [&] { if (!buffer.empty()) { emit(buffer); buffer.clear(); } };
  for (std::size_t i = 0; i < value.size(); ++i) {
    const char c = value[i];
    if (quoted) {
      buffer += c;
      if (escaped) escaped = false;
      else if (c == '\\') escaped = true;
      else if (c == '"') quoted = false;
    } else if (c == '"') { quoted = true; buffer += c; }
    else if (c == '{' || c == '[') {
      buffer += c;
      if (++depth > 1000) throw std::runtime_error("JSON formatting depth exceeds bound");
      if (i+1 < value.size() && value[i+1] != '}' && value[i+1] != ']') {
        buffer += '\n'; buffer.append(2*(level+depth),' ');
      }
    } else if (c == '}' || c == ']') {
      if (!depth) throw std::runtime_error("JSON formatting depth is invalid");
      --depth;
      if (i && value[i-1] != '{' && value[i-1] != '[') {
        buffer += '\n'; buffer.append(2*(level+depth),' ');
      }
      buffer += c;
    } else if (c == ',') { buffer += ",\n"; buffer.append(2*(level+depth),' '); }
    else if (c == ':') buffer += ": ";
    else {
      if (c == ' ' || c == '\n' || c == '\t' || c == '\r')
        throw std::runtime_error("JSON formatting input is not compact");
      buffer += c;
    }
    if (buffer.size() >= 4096) flush();
  }
  if (quoted || depth) throw std::runtime_error("JSON formatting input is incomplete");
  flush();
}

// Small typed record serializer, never a whole-graph JSON representation.
// Matches the existing sorted-key, ensure_ascii Python contract exactly.
inline std::string QuoteJsonString(const std::string& text, bool ensure_ascii) {
  std::string out = "\"";
  const auto escaped = [&](std::uint32_t cp) {
    static const char* digits = "0123456789abcdef";
    out += "\\u";
    for (int shift : {12, 8, 4, 0}) out += digits[(cp >> shift) & 15];
  };
  for (std::size_t i = 0; i < text.size();) {
    const auto start = i;
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
        if (!ensure_ascii && value >= 127) out.append(text,start,i-start);
        else if (value < 32 || (value >= 127 && value <= 0xffff)) escaped(value);
        else if (value > 0xffff) { value -= 0x10000; escaped(0xd800 + (value >> 10)); escaped(0xdc00 + (value & 1023)); }
        else out += static_cast<char>(value);
    }
  }
  return out + '"';
}
inline std::string JsonQuote(const std::string& text) { return QuoteJsonString(text,true); }
inline std::string JsonQuoteUtf8(const std::string& text) { return QuoteJsonString(text,false); }

// Decode one JSON1-validated string token without SQLite's scalar text
// conversion: older JSON1 releases truncate escaped NUL in extracted strings.
inline std::string DecodeJsonString(const std::string& token) {
  const auto first=token.find_first_not_of(" \t\r\n"),last=token.find_last_not_of(" \t\r\n");
  if(first==std::string::npos || first==last || token[first]!='"' || token[last]!='"')
    throw std::runtime_error("invalid JSON string token");
  std::size_t position=first+1;std::string result;
  const auto hex=[&]() {
    if(last-position<4)throw std::runtime_error("truncated JSON Unicode escape");
    std::uint32_t value=0;
    for(unsigned i=0;i<4;++i) {
      const char c=token[position++];unsigned digit;
      if(c>='0' && c<='9')digit=c-'0';else if(c>='a' && c<='f')digit=c-'a'+10;
      else if(c>='A' && c<='F')digit=c-'A'+10;else throw std::runtime_error("invalid JSON Unicode escape");
      value=(value<<4)|digit;
    }
    return value;
  };
  while(position<last) {
    const auto c=static_cast<unsigned char>(token[position++]);
    if(c<32 || c=='"')throw std::runtime_error("invalid JSON string character");
    if(c!='\\'){result+=static_cast<char>(c);continue;}
    if(position==last)throw std::runtime_error("truncated JSON escape");
    switch(token[position++]) {
      case '"':result+='"';break;case '\\':result+='\\';break;case '/':result+='/';break;
      case 'b':result+='\b';break;case 'f':result+='\f';break;case 'n':result+='\n';break;
      case 'r':result+='\r';break;case 't':result+='\t';break;
      case 'u': {
        auto value=hex();
        if(value>=0xd800 && value<=0xdbff) {
          if(last-position<6 || token[position++]!='\\' || token[position++]!='u')
            throw std::runtime_error("missing JSON low surrogate");
          const auto low=hex();if(low<0xdc00 || low>0xdfff)throw std::runtime_error("invalid JSON low surrogate");
          value=0x10000+((value-0xd800)<<10)+(low-0xdc00);
        } else if(value>=0xdc00 && value<=0xdfff)throw std::runtime_error("unpaired JSON low surrogate");
        if(value<0x80)result+=static_cast<char>(value);
        else if(value<0x800){result+=static_cast<char>(0xc0|(value>>6));result+=static_cast<char>(0x80|(value&63));}
        else if(value<0x10000){result+=static_cast<char>(0xe0|(value>>12));result+=static_cast<char>(0x80|((value>>6)&63));result+=static_cast<char>(0x80|(value&63));}
        else {result+=static_cast<char>(0xf0|(value>>18));result+=static_cast<char>(0x80|((value>>12)&63));result+=static_cast<char>(0x80|((value>>6)&63));result+=static_cast<char>(0x80|(value&63));}
        break;
      }
      default:throw std::runtime_error("invalid JSON escape");
    }
  }
  (void)JsonQuoteUtf8(result);return result;
}
#if defined(EMUFLOW_HAS_CHARCONV_HEADER)
template<class T,class=void> struct HasFloatingToChars : std::false_type {};
template<class T> struct HasFloatingToChars<T,std::void_t<decltype(std::to_chars(
    static_cast<char*>(nullptr),static_cast<char*>(nullptr),std::declval<T>(),std::chars_format::scientific))>> : std::true_type {};
template<class T> inline std::string ShortestScientific(T value) {
  if constexpr(HasFloatingToChars<T>::value) {
    char text[64];const auto result=std::to_chars(text,text+sizeof(text),value,std::chars_format::scientific);
    if(result.ec==std::errc())return std::string(text,result.ptr);
  }
  return {};
}
#else
inline std::string ShortestScientific(double) { return {}; }
#endif
class JsonRecord {
 public:
  enum class Kind { kString, kInteger, kScalar, kObject, kArray };
  JsonRecord() : kind_(Kind::kScalar), scalar_("null") {}
  // Validate/quote once at encoding; storing a row must not encode its strings twice.
  JsonRecord(const std::string& text) : kind_(Kind::kString), scalar_(text) {}
  JsonRecord(const char* text) : JsonRecord(std::string(text)) {}
  template <typename T, std::enable_if_t<std::is_integral<T>::value && !std::is_same<T,bool>::value,int> = 0>
  JsonRecord(T integer) : kind_(Kind::kInteger), scalar_(std::to_string(integer)) {}
  JsonRecord(bool value) : kind_(Kind::kScalar), scalar_(value ? "true" : "false") {}
  JsonRecord(double value) : kind_(Kind::kScalar) {
    if (!std::isfinite(value)) throw std::runtime_error("non-finite native JSON number");
    // Shortest round-trip digits, with Python's fixed/scientific thresholds,
    // exponent padding and explicit .0 for integral floating-point values.
    // Some supported C++17 standard libraries lack floating-point to_chars.
    // Bulk path records use the standard shortest converter when available.
    // Older C++17 libraries retain the locale-independent round-trip fallback.
    static const locale_t c_locale = newlocale(LC_NUMERIC_MASK,"C",nullptr);
    if (!c_locale) throw std::runtime_error("cannot create native JSON numeric locale");
    std::string text=ShortestScientific(value);
    if(text.empty()) for (unsigned precision = 1; precision <= 17; ++precision) {
      std::ostringstream encoded; encoded.imbue(std::locale::classic());
      encoded << std::scientific << std::setprecision(precision - 1) << value;
      text = encoded.str(); char* end = nullptr;
      if (strtod_l(text.c_str(),&end,c_locale) == value && end == text.c_str() + text.size()) break;
      // At binary powers of two the rounding interval is asymmetric. The
      // nearest p-digit decimal may be outside its narrow side while the next
      // decimal on the wider side still round-trips (a shorter valid result).
      const auto e = text.find('e');
      const auto negative = text[0] == '-';
      auto digits = text.substr(negative ? 1 : 0,e - (negative ? 1 : 0));
      const auto dot = digits.find('.');
      if (dot != std::string::npos) digits.erase(dot,1);
      const auto integer = std::stoull(digits);
      const auto exponent = std::stoi(text.substr(e + 1));
      bool found = false;
      for (int delta : {-1,1}) {
        if (delta < 0 && integer == 0) continue;
        auto adjacent = std::to_string(delta < 0 ? integer - 1 : integer + 1);
        const auto power = exponent - static_cast<int>(precision) + 1;
        const std::string candidate = (negative ? "-" : "") + adjacent + "e" + std::to_string(power);
        if (strtod_l(candidate.c_str(),&end,c_locale) != value || end != candidate.c_str() + candidate.size()) continue;
        const auto adjusted = power + static_cast<int>(adjacent.size()) - 1;
        while (adjacent.size() > 1 && adjacent.back() == '0') adjacent.pop_back();
        text = (negative ? "-" : "") + adjacent.substr(0,1);
        if (adjacent.size() > 1) text += "." + adjacent.substr(1);
        text += "e" + std::to_string(adjusted);
        found = true; break;
      }
      if (found) break;
    }
    const auto e = text.find('e');
    const int exponent = std::stoi(text.substr(e + 1));
    const bool negative = text[0] == '-';
    std::string digits = text.substr(negative ? 1 : 0,e - (negative ? 1 : 0));
    const auto dot = digits.find('.');
    if (dot != std::string::npos) digits.erase(dot,1);
    scalar_ = negative ? "-" : "";
    if (exponent < -4 || exponent >= 16) {
      scalar_ += digits.substr(0,1);
      if (digits.size() > 1) scalar_ += "." + digits.substr(1);
      scalar_ += exponent < 0 ? "e-" : "e+";
      const auto power = std::to_string(std::abs(exponent));
      if (power.size() < 2) scalar_ += '0';
      scalar_ += power;
    } else if (exponent < 0) {
      scalar_ += "0." + std::string(-exponent - 1,'0') + digits;
    } else if (static_cast<std::size_t>(exponent + 1) >= digits.size()) {
      scalar_ += digits + std::string(exponent + 1 - digits.size(),'0') + ".0";
    } else {
      scalar_ += digits.substr(0,exponent + 1) + "." + digits.substr(exponent + 1);
    }
  }
  JsonRecord(std::map<std::string, JsonRecord> object) : kind_(Kind::kObject), object_(std::move(object)) {}
  JsonRecord(std::vector<JsonRecord> array) : kind_(Kind::kArray), array_(std::move(array)) {}
  std::string EncodeUtf8() const { return Encode(false,0,false); }
  std::string Encode(bool pretty = false, unsigned level = 0, bool ensure_ascii = true) const {
    if (kind_ == Kind::kString) return QuoteJsonString(scalar_,ensure_ascii);
    if (kind_ != Kind::kObject && kind_ != Kind::kArray) return scalar_;
    const bool object = kind_ == Kind::kObject;
    std::string out = object ? "{" : "[";
    const auto count = object ? object_.size() : array_.size();
    std::size_t position = 0;
    const auto item = [&](const std::string& key, const JsonRecord& value) {
      if (position++) out += pretty ? ",\n" : ",";
      else if (pretty) out += '\n';
      if (pretty) out += std::string((level + 1) * 2, ' ');
      if (object) out += QuoteJsonString(key,ensure_ascii) + (pretty ? ": " : ":");
      out += value.Encode(pretty, level + 1,ensure_ascii);
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
