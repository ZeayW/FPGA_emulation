#pragma once
#include "physical/openparf/input_records.hpp"
namespace emuflow::core::openparf_placement {
using namespace openparf_input;
inline JsonRecord Record(const R &r) {
  return std::visit(
      [](const auto &v) -> JsonRecord {
        using T = std::decay_t<decltype(v)>;
        if constexpr (std::is_same_v<T, std::nullptr_t>)
          return {};
        else if constexpr (std::is_same_v<T, O>) {
          std::map<std::string, JsonRecord> out;
          for (const auto &f : v)
            out.emplace(f.first, Record(f.second));
          return out;
        } else if constexpr (std::is_same_v<T, A>) {
          std::vector<JsonRecord> out;
          for (const auto &f : v)
            out.push_back(Record(f));
          return out;
        } else
          return JsonRecord(v);
      },
      r.value);
}
inline void Attach(SqliteDatabase &db, const ArtifactRef &source,
                   const std::string &schema) {
  auto query = db.Prepare("ATTACH DATABASE ? AS " + schema);
  query.BindText(1, SqliteImmutableUri(source.path()));
  query.Execute();
}
inline void None(SqliteDatabase &db, const std::string &sql,
                 const std::string &why) {
  Check(!db.Prepare(sql).Step(), why);
}
// Both the raw importer and legalizer use this indexed relation. They never
// construct a complete name -> placement Python view.
void CheckWindows(SqliteDatabase &db, BoundRequest &input, const R &name_header,
                  const volatile std::sig_atomic_t *flag);
R LegalizeFf(SqliteDatabase &db, MappedPhysicalSemantics &mapped,
             const volatile std::sig_atomic_t *flag);
} // namespace emuflow::core::openparf_placement
