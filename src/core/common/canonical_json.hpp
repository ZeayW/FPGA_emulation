#pragma once
#include "common/bounded_json.hpp"

namespace emuflow::core {
// Canonicalize one already validated record or bounded header, never a circuit.
inline std::string CanonicalJsonToken(const std::string &token) {
  if (token.empty())
    throw StageError(ErrorCode::kInvalidArtifact, "empty JSON token");
  if (token[0] == '{') {
    std::map<std::string, std::string> fields;
    VisitJsonObjectTokens(token, [&](const auto &k, const auto &v) {
      fields.emplace(k, CanonicalJsonToken(v));
    });
    return BoundedJson::Encode(fields);
  }
  if (token[0] == '[') {
    std::vector<std::string> rows;
    VisitJsonArrayTokens(
        token, [&](const auto &v) { rows.push_back(CanonicalJsonToken(v)); });
    std::string out = "[";
    for (const auto &row : rows) {
      if (out.size() > 1)
        out += ',';
      out += row;
    }
    return out + ']';
  }
  if (token[0] == '"')
    return JsonQuote(DecodeJsonString(token));
  if (token == "true" || token == "false" || token == "null")
    return token;
  if (token.find_first_of(".eE") != std::string::npos)
    return JsonRecord(ParseFiniteDecimal(token, "inventory number")).Encode();
  return std::to_string(BoundedJson::IntegerToken(token));
}
} // namespace emuflow::core
