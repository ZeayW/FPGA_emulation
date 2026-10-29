#pragma once
#include "common/bounded_json.hpp"
#include <cctype>
#include <csignal>
#include <istream>

namespace emuflow::core {
// Structural streaming framing; only one bounded site/family record is held.
class JsonRecordStream {
public:
  JsonRecordStream(std::istream &input, SqliteDatabase &db,
                   const volatile std::sig_atomic_t *flag)
      : in(input), db(db), flag(flag) {}
  template <class F> void Object(F visit) {
    Expect('{');
    if (Peek() == '}') {
      in.get();
      return;
    }
    std::set<std::string> seen;
    for (;;) {
      Check(Peek() == '"', "JSON object key is not string");
      const auto key = DecodeJsonString(Value());
      Check(seen.size() < 65536 && seen.insert(key).second,
            "duplicate or excessive object keys");
      Expect(':');
      visit(key);
      const auto c = Peek();
      Check(c == ',' || c == '}', "invalid object delimiter");
      in.get();
      if (c == '}')
        break;
    }
  }
  template <class F> void Array(F visit) {
    Expect('[');
    if (Peek() == ']') {
      in.get();
      return;
    }
    for (;;) {
      visit(Value());
      const auto c = Peek();
      Check(c == ',' || c == ']', "invalid array delimiter");
      in.get();
      if (c == ']')
        break;
    }
  }
  std::string Value() {
    std::string out;
    Read(&out, 0);
    return out;
  }
  void Skip() { Read(nullptr, 0); }
  void Finish() {
    Check(Peek() == EOF && !in.bad(), "trailing or unreadable JSON");
  }

private:
  static void Check(bool valid, const std::string &why) {
    if (!valid)
      throw StageError(ErrorCode::kInvalidArtifact, "JSON stream: " + why);
  }
  static void Stop(const volatile std::sig_atomic_t *flag) {
    if (flag && *flag)
      throw StageError(ErrorCode::kExecutionFailed, "JSON stream cancelled");
  }
  int Peek() {
    Stop(flag);
    while (in && (in.peek() == ' ' || in.peek() == '\t' || in.peek() == '\n' ||
                  in.peek() == '\r'))
      in.get();
    return in.peek();
  }
  void Expect(char c) {
    Check(Peek() == c, "unexpected JSON token");
    in.get();
  }
  void Add(std::string *out, int c) {
    if (out) {
      Check(out->size() < 32 * 1024 * 1024, "JSON record exceeds bound");
      out->push_back(char(c));
    }
  }
  void Read(std::string *out, unsigned depth) {
    Check(depth < 64, "JSON depth exceeds bound");
    const auto first = Peek();
    Check(first != EOF, "truncated JSON");
    if (first == '"') {
      Add(out, in.get());
      for (;;) {
        const auto c = in.get();
        Check(c != EOF && c >= 32, "invalid JSON string");
        Add(out, c);
        if (c == '"')
          return;
        if (c == '\\') {
          const auto e = in.get();
          Check(e != EOF, "truncated JSON escape");
          Add(out, e);
          if (e == 'u') {
            for (unsigned i = 0; i < 4; ++i) {
              const auto d = in.get();
              Check(d != EOF && std::isxdigit(static_cast<unsigned char>(d)),
                    "invalid Unicode escape");
              Add(out, d);
            }
          } else
            Check(std::string("\"\\/bfnrt").find(char(e)) != std::string::npos,
                  "invalid JSON escape");
        }
      }
    }
    if (first == '{' || first == '[') {
      Add(out, in.get());
      const auto end = first == '{' ? '}' : ']';
      if (Peek() == end) {
        Add(out, in.get());
        return;
      }
      for (;;) {
        if (first == '{') {
          Check(Peek() == '"', "invalid object key");
          Read(out, depth + 1);
          Expect(':');
          Add(out, ':');
        }
        Read(out, depth + 1);
        const auto c = Peek();
        Check(c == ',' || c == end, "invalid nested delimiter");
        Add(out, in.get());
        if (c == end)
          return;
      }
    }
    std::string token;
    for (;;) {
      const auto c = in.peek();
      if (c == EOF || c == ',' || c == ']' || c == '}' ||
          std::isspace(static_cast<unsigned char>(c)))
        break;
      Check(token.size() < 128, "scalar exceeds bound");
      token += char(in.get());
    }
    auto valid = db.Prepare("SELECT json_valid(?)");
    valid.BindText(1, token);
    Check(!token.empty() && valid.Step() && valid.Integer(0),
          "invalid JSON scalar");
    if (out) {
      Check(out->size() + token.size() <= 32 * 1024 * 1024,
            "record exceeds bound");
      *out += token;
    }
  }
  std::istream &in;
  SqliteDatabase &db;
  const volatile std::sig_atomic_t *flag;
};
} // namespace emuflow::core
