#include "partition/assignment_cuts.hpp"
#include "storage/store.hpp"
#include "common/json_record.hpp"
#include "common/hash.hpp"

#include <algorithm>
#include <string_view>

namespace emuflow::core {
namespace {
void Check(bool value, const char* message) {
  if (!value) throw StageError(ErrorCode::kInvalidArtifact,message);
}
std::string Identity(const std::string& value) {
  Check(!value.empty() && value.find('\0')==std::string::npos,
        "assignment cut identity is invalid");
  try { return JsonQuote(value); }
  catch (const std::runtime_error& error) { throw StageError(ErrorCode::kInvalidArtifact,error.what()); }
}
std::string Hex(Sha256& digest) {
  const char* digits="0123456789abcdef";std::string result;
  for (auto byte:digest.finish()) { result+=digits[byte>>4];result+=digits[byte&15]; }
  return result;
}
}
AssignmentCutSummary AssignmentView::ValidateCuts() {
  Check(store_.artifact().schema()=="assignment-index-v4",
        "typed cut validation requires assignment v4");
  store_.RequireUnchanged();
  auto& db=store_.database();
  for(const auto& part:parts_) Identity(part);
  std::int64_t expected=0;std::string expected_digest;
  bool semantic=false;
  {
    auto row=db.Prepare(
        "SELECT json_type(c.value),json_extract(c.value,'$'),"
        "json_extract(s.value,'$'),json_extract(h.value,'$'),"
        "coalesce(json_type(e.value),'null') "
        "FROM metadata c JOIN metadata s ON s.key='cut_records_schema' "
        "JOIN metadata h ON h.key='cut_records_sha256' "
        "LEFT JOIN metadata e ON e.key='semantic_contract_sha256' "
        "WHERE c.key='cut_records'");
    Check(row.Step() && row.TypedText(0)=="integer" && row.TypedInteger(1)>=0 &&
          row.TypedText(2)=="emuflow.phase3-cut-records/v1",
          "assignment cut metadata is invalid");
    expected=row.TypedInteger(1);expected_digest=row.TypedText(3);
    Check(expected_digest.size()==64 && std::all_of(expected_digest.begin(),expected_digest.end(),
          [](char c){ return (c>='0' && c<='9') || (c>='a' && c<='f'); }),
          "assignment cut digest is invalid");
    semantic=row.TypedText(4)!="null";
    Check(!row.Step(),"assignment cut metadata repeats");
  }
  {
    // Existence of predecessor names needs the global net index. Parent
    // coordinates, sink ownership and orphan rows are checked by the merge
    // below, without rescanning those relations through additional joins.
    auto invalid=db.Prepare(
        "SELECT 1 FROM cut_predecessors q LEFT JOIN cuts p "
        "ON p.net_id=q.predecessor_net_id WHERE p.cut_index IS NULL LIMIT 1");
    Check(!invalid.Step(),"assignment cut relation contains missing or orphan identities");
  }
  auto cuts=db.Prepare(
      "SELECT cut_index,net_id,source_part_index,cut_class,sink_endpoints,"
      "transport_round,dependency_level,combinational_dependency_depth FROM cuts ORDER BY cut_index");
  // Cut indices already require canonical net order. Merge the child table's
  // native primary-key order directly; no full child join/sort or name map.
  auto sinks=db.Prepare(
      "SELECT net_id,source_part_index,sink_part_index FROM cut_sinks ORDER BY net_id,sink_part_index");
  auto predecessors=db.Prepare(
      "SELECT cut_index,ordinal,predecessor_net_id FROM cut_predecessors ORDER BY cut_index,ordinal");
  bool have_sink=sinks.Step(),have_predecessor=predecessors.Step();
  AssignmentCutSummary result;
  Sha256 digest;
  const auto emit=[&](std::string_view text) {
    digest.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());
  };
  std::string previous_net;
  while(cuts.Step()) {
    const auto index=cuts.TypedInteger(0),source=cuts.TypedInteger(2),endpoints=cuts.TypedInteger(4);
    const auto net=cuts.TypedText(1),kind=cuts.TypedText(3);
    Check(index>=0 && static_cast<std::uint64_t>(index)==result.records &&
          (!result.records || previous_net<net) && source>=0 &&
          static_cast<std::uint64_t>(source)<parts_.size() && endpoints>0,
          "assignment cut order, source or endpoint count is invalid");
    previous_net=net;++result.records;
    const auto net_json=Identity(net),kind_json=Identity(kind);
    Check(cuts.IsNull(6)==cuts.IsNull(7) && (!semantic || !cuts.IsNull(6)),
          "assignment cut dependency fields are incomplete");
    const bool dependency=!cuts.IsNull(6);
    const auto scalar=[&](int column) {
      const auto n=cuts.TypedInteger(column);Check(n>=0,"assignment cut scalar is negative");
      return std::to_string(n);
    };
    // Stream canonical keys independently of the producer's JsonRecord map.
    // Only the current text field is retained, even for a very large fanin.
    emit("{");
    if(dependency) {emit("\"combinational_dependency_depth\":");emit(scalar(7));emit(",");}
    emit("\"cut_class\":");emit(kind_json);
    if(dependency) {emit(",\"dependency_level\":");emit(scalar(6));}
    emit(",\"net\":");emit(net_json);
    if(dependency) {
      emit(",\"predecessor_cut_nets\":[");std::uint64_t ordinal=0;std::string previous;
      while(have_predecessor && predecessors.TypedInteger(0)==index) {
        const auto name=predecessors.TypedText(2);
        Check(predecessors.TypedInteger(1)==static_cast<std::int64_t>(ordinal) && name!=net &&
              (!ordinal || previous<name),"assignment cut predecessor order is invalid");
        if(ordinal++) emit(",");emit(Identity(name));previous=name;
        have_predecessor=predecessors.Step();
      }
      emit("]");
    } else {
      Check(!have_predecessor || predecessors.TypedInteger(0)!=index,
            "assignment cut predecessors lack dependency fields");
    }
    emit(",\"sink_endpoints\":");emit(std::to_string(endpoints));emit(",\"sink_fpgas\":[");
    std::uint64_t sink_count=0;std::int64_t previous_sink=-1;
    while(have_sink && sinks.TypedText(0)==net) {
      const auto part=sinks.TypedInteger(2);
      Check(sinks.TypedInteger(1)==source && part>=0 && static_cast<std::uint64_t>(part)<parts_.size() &&
            part!=source && previous_sink<part,"assignment cut sink order or ownership is invalid");
      if(sink_count++) emit(",");emit(Identity(parts_[part]));previous_sink=part;++result.sinks;
      have_sink=sinks.Step();
    }
    Check(sink_count,"assignment cut has no sinks");emit("]");
    emit(",\"source_fpgas\":[");emit(Identity(parts_[source]));emit("]");
    if(!cuts.IsNull(5)) {emit(",\"transport_round\":");emit(scalar(5));}
    emit("}\n");
  }
  Check(!have_sink && !have_predecessor,"assignment cut contains orphan child records");
  result.sha256=Hex(digest);
  Check(result.records==static_cast<std::uint64_t>(expected) && result.sha256==expected_digest,
        "assignment cut identity disagrees");
  store_.RequireUnchanged();
  return result;
}
void ValidateAssignmentCuts(const ArtifactRef& input,const std::string& output) {
  AssignmentView view(input);
  const auto result=view.ValidateCuts();
  PublishTextArtifact(output,{input},[&](std::ostream& stream) {
    stream<<"{\"schema\":\"emuflow.assignment-cut-validation/v1\",\"status\":\"pass\",\"records\":"
          <<result.records<<",\"sinks\":"<<result.sinks<<",\"sha256\":"<<JsonQuote(result.sha256)<<"}\n";
  });
}
}
