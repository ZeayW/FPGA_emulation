#include "emuflow_core/optimization_proofs.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>
#include <charconv>
#include <set>

namespace emuflow::core {
namespace {
void Check(bool value,const char* message) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Digest(const std::string& value) {
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,
        "optimization parent digest is invalid");
}
std::string Hash(const std::string& raw) {
  Sha256 h;h.update(reinterpret_cast<const unsigned char*>(raw.data()),raw.size());
  std::string result;for(auto byte:h.finish()){result+="0123456789abcdef"[byte>>4];result+="0123456789abcdef"[byte&15];}return result;
}
std::string Scalar(SqliteDatabase& db,const std::string& key,bool json=true) {
  auto row=db.Prepare(json?"SELECT json_extract(value,'$') FROM metadata WHERE key=?":"SELECT value FROM metadata WHERE key=?");
  row.BindText(1,key);Check(row.Step(),"optimization metadata is missing");auto result=row.TypedText(0);
  Check(!result.empty() && result!="NULL" && result.size()<=1024*1024 && result.find('\0')==std::string::npos && !row.Step(),
        "optimization metadata is invalid or repeated");return result;
}
std::optional<std::string> OptionalScalar(SqliteDatabase& db,const std::string& key) {
  auto row=db.Prepare("SELECT json_extract(value,'$') FROM metadata WHERE key=?");row.BindText(1,key);
  if(!row.Step())return {};const auto result=row.TypedText(0);Check(!row.Step(),"optimization metadata repeats");return result;
}
std::string JsonText(SqliteDatabase& db,const std::string& raw,const std::string& path) {
  auto row=db.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,raw);row.BindText(2,path);
  Check(row.Step(),"optimization confirmation field is missing");return row.TypedText(0);
}
std::uint64_t Origin(const std::string& source) {
  Check(source.size()>=2 && source.size()<=20 && source[0]=='s' &&
        (source.size()==2 || source[1]!='0'),"optimization source spelling is not canonical");
  std::uint64_t value=0;const auto parsed=std::from_chars(source.data()+1,source.data()+source.size(),value);
  Check(parsed.ec==std::errc{} && parsed.ptr==source.data()+source.size() && value<=INT64_MAX,
        "optimization source coordinate is invalid");return value;
}
std::string LocalReference(const std::string& reference,const std::string& fpga) {
  if(reference.size()>1 && reference.front()=='s')return reference;
  const auto colon=reference.find(':');
  Check(reference.size()<=1024 && reference.front()=='p' && colon!=std::string::npos && colon>1 && (colon-1)%2==0,
        "optimization source reference is invalid");
  const auto encoded=reference.substr(1,colon-1);
  Check(encoded.find_first_not_of("0123456789abcdef")==std::string::npos,"optimization partition encoding is invalid");
  std::string name;for(std::size_t i=0;i<encoded.size();i+=2)name+=char(std::stoul(encoded.substr(i,2),nullptr,16));
  Check(name==fpga,"optimization reference belongs to another partition");return "s"+reference.substr(colon+1);
}
std::string Inventory(SqliteDatabase& db,const std::string& stage) {
  Check(stage=="routed" || stage=="optimized" || stage=="synthesized","optimization source stage is invalid");
  const auto stages=Scalar(db,"stages_json",false);
  auto value=JsonText(db,stages,"$."+stage+".inventory_sha256");Digest(value);return value;
}
}
std::string LogicalTimingCell(const std::string& pin) {
  Check(!pin.empty() && pin.size()<=1024*1024 && pin.find('\0')==std::string::npos,"optimization endpoint name is invalid");
  const auto slash=pin.rfind('/');Check(slash!=std::string::npos && slash>0 && slash+1<pin.size(),"optimization endpoint has no cell");
  std::string result;
  for(std::size_t i=0;i<slash;++i) {
    result+=pin[i];
    if(pin[i]=='\\')Check(++i<slash && pin[i]=='\\',"optimization endpoint cell escape is not canonical");
  }
  JsonQuote(result);return result;
}
NativeOptimizationProofView::NativeOptimizationProofView(const ArtifactRef& proof,const ArtifactRef& mapped,
    const ArtifactRef& lineage,const CheckedOptimizationContext& context)
    :proof_(proof),mapped_(mapped),lineage_(lineage),context_(context) {
  Check(proof.schema()=="vivado-optimization-proofs-v1" && mapped.schema()=="xilinx-mapped-index-v2" &&
        lineage.schema()=="vivado-lineage-store-v2","optimization requires current checked physical owners");
  unsigned index=0;for(const auto* input:{&proof,&mapped,&lineage})
    Check(input->generation()==context.generations[index++],"checked optimization input generation changed");
  Check(!context.fpga.empty() && context.fpga.size()<=4096 && context.fpga.find('\0')==std::string::npos,
        "optimization requires an explicit partition");Digest(context.mapped_sha256);
  auto& db=proof_.database();auto& observed=lineage_.database();
  Check(Scalar(db,"fpga")==context.fpga && Scalar(db,"stage")=="routed" &&
        Scalar(db,"mapped_index_sha256")==context.mapped_sha256 &&
        Scalar(observed,"mapped_index_sha256",false)==context.mapped_sha256 &&
        Scalar(observed,"retained_stage",false)=="routed" &&
        Scalar(db,"stage_inventory_sha256")==Inventory(observed,"routed"),
        "optimization proof belongs to another source, partition or routed inventory");
  // The receipt already validated these row seals; do not scan the complete
  // proof/name relations a second time at every native consumer boundary.
  Digest(Scalar(db,"proof_rows_sha256"));Digest(Scalar(db,"source_names_sha256"));
  ReadConfirmation();RequireUnchanged();
}
void NativeOptimizationProofView::ReadConfirmation() {
  auto& db=proof_.database();const auto digest=OptionalScalar(db,"vendor_confirmation_document_sha256");
  if(!digest)return;Digest(*digest);Digest(context_.synthesized_checkpoint_sha256);
  auto document=db.Prepare("SELECT id,length(CAST(document AS BLOB)) FROM vendor_confirmation_document ORDER BY id");
  Check(document.Step() && document.TypedInteger(0)==0 && document.TypedInteger(1)>0 && document.Integer(1)<=16*1024*1024 &&
        !document.Step(),"optimization vendor confirmation exceeds its bound or repeats");
  auto row=db.Prepare("SELECT document FROM vendor_confirmation_document WHERE id=0");Check(row.Step(),"optimization confirmation is missing");
  const auto raw=row.TypedText(0);Check(Hash(raw)==*digest,"optimization confirmation seal differs");
  const std::string init="joint-inductive-source-model-with-explicit-vendor-selected-initialization";
  const auto schema=JsonText(db,raw,"$.schema");
  const bool legacy=schema=="emuflow.vivado-selected-constant-confirmations/v3";
  const std::string input_policy=legacy ? "same-primitive-and-pin-bindings-at-preplacement-snapshot-v1" :
      "same-primitive-pin-bindings-and-direct-output-constants-at-preplacement-v2";
  Check((legacy || schema=="emuflow.vivado-selected-constant-confirmations/v4") &&
        JsonText(db,raw,"$.status")=="pass" && JsonText(db,raw,"$.fpga")==context_.fpga &&
        JsonText(db,raw,"$.mapped_index_sha256")==context_.mapped_sha256 &&
        JsonText(db,raw,"$.initialization_policy")==init && Scalar(db,"vendor_initialization_policy")==init &&
        JsonText(db,raw,"$.source_model_scope")=="selected-states-inductive-without-unconfirmed-state-assumptions" &&
        JsonText(db,raw,"$.native_input_policy")==input_policy &&
        JsonText(db,raw,"$.synthesized_checkpoint_sha256")==context_.synthesized_checkpoint_sha256 &&
        Scalar(db,"synthesized_checkpoint_sha256")==context_.synthesized_checkpoint_sha256,
        "optimization vendor confirmation belongs to another policy or implementation");
  const auto stage=JsonText(db,raw,"$.source_stage");
  Check((stage=="optimized" || stage=="synthesized") &&
        JsonText(db,raw,"$.source_inventory_sha256")==Inventory(lineage_.database(),stage),
        "optimization confirmation belongs to another preplacement inventory");
  Digest(JsonText(db,raw,"$.source_candidate_proofs_sha256"));Digest(JsonText(db,raw,"$.observations_sha256"));
  auto shape=db.Prepare("SELECT json_type(?1,'$.sources'),json_array_length(?1,'$.sources'),"
      "json_type(?1,'$.frontiers'),json_array_length(?1,'$.frontiers')");shape.BindText(1,raw);
  Check(shape.Step() && shape.TypedText(0)=="array" && shape.TypedText(2)=="array" &&
        shape.TypedInteger(1)>0 && shape.Integer(1)<=4096 && shape.TypedInteger(3)>0 && shape.Integer(3)<=4096,
        "optimization confirmation population is invalid");
  std::set<std::string> required_frontiers;
  auto sources=db.Prepare("SELECT json_extract(value,'$.source'),json_extract(value,'$.constant_value'),"
      "json_type(value,'$.constant_value'),json_extract(value,'$.source_initialization'),"
      "json_type(value,'$.frontier_sources'),json_array_length(value,'$.frontier_sources'),value FROM json_each(?,'$.sources')");
  sources.BindText(1,raw);
  while(sources.Step()) {
    const auto origin=Origin(sources.TypedText(0));const auto value=sources.TypedInteger(1);const auto policy=sources.TypedText(3);
    Check(sources.TypedText(2)=="integer" && (value==0 || value==1) &&
          (policy=="specified" || policy=="vendor-dont-care-selection") && sources.TypedText(4)=="array" &&
          sources.TypedInteger(5)>0 && sources.Integer(5)<=4096 && confirmed_.emplace(origin,value).second,
          "optimization confirmation source is invalid or repeated");
    auto frontier=db.Prepare("SELECT value FROM json_each(?,'$.frontier_sources')");frontier.BindText(1,sources.TypedText(6));
    std::set<std::string> distinct;
    while(frontier.Step()) {const auto item=frontier.TypedText(0);Origin(item);
      Check(distinct.insert(item).second,"optimization source frontier repeats");required_frontiers.insert(item);
      Check(required_frontiers.size()<=4096,"optimization confirmation frontier exceeds its bound");}
  }
  auto frontiers=db.Prepare("SELECT json_extract(value,'$.source_origin'),json_extract(value,'$.native_cell'),"
      "json_extract(value,'$.variables'),json_extract(value,'$.assignments'),json_type(value,'$.variables'),"
      "json_type(value,'$.assignments'),json_extract(value,'$.object_kind') FROM json_each(?,'$.frontiers')");frontiers.BindText(1,raw);
  std::set<std::pair<std::string,std::string>> objects;std::set<std::string> observed_frontiers;
  while(frontiers.Step()) {
    const auto origin=frontiers.TypedText(0),cell=frontiers.TypedText(1);const auto variables=frontiers.TypedInteger(2);
    const auto object_kind=legacy ? std::string("cell") : frontiers.TypedText(6);
    Check(required_frontiers.count(origin) && !cell.empty() && cell.find('\0')==std::string::npos &&
          (!legacy || (frontiers.IsNull(6) && !confirmed_.count(Origin(origin)))) &&
          (object_kind=="cell" || (object_kind=="output-port" && variables==0)) &&
          variables>=0 && variables<=12 && frontiers.TypedText(4)=="integer" && frontiers.TypedText(5)=="integer" &&
          frontiers.TypedInteger(3)==(1LL<<variables) && objects.emplace(origin,cell).second,
          "optimization confirmation functional frontier is invalid or repeated");observed_frontiers.insert(origin);
  }
  Check(observed_frontiers==required_frontiers,"optimization confirmation frontier coverage differs");
}
EliminatedSourceFact NativeOptimizationProofView::Require(const std::string& reference,const std::vector<std::string>& endpoints) {
  Check(!reference.empty() && !endpoints.empty() && endpoints.size()<=2,"optimization fact requires explicit endpoint cells");
  const auto origin=Origin(LocalReference(reference,context_.fpga));
  auto row=proof_.database().Prepare("SELECT p.reason,p.constant_value,p.initialization,n.name FROM eliminated_sources p "
      "JOIN source_names n USING(origin) WHERE p.origin=?");row.BindInteger(1,origin);
  Check(row.Step(),"optimization source is unproved or conditional");
  const auto reason=row.TypedText(0),name=row.TypedText(3);
  Check(!name.empty() && std::find(endpoints.begin(),endpoints.end(),name)!=endpoints.end(),
        "optimization proof belongs to another logical endpoint");
  auto mapped=mapped_.database().Prepare("SELECT id FROM cells WHERE seq=?");mapped.BindInteger(1,origin);
  Check(mapped.Step() && mapped.TypedText(0)==name && !mapped.Step(),"optimization source name differs from its mapped owner");
  auto retained=lineage_.database().Prepare("SELECT 1 FROM objects WHERE stage='routed' AND origin=? LIMIT 1");retained.BindInteger(1,origin);
  Check(!retained.Step(),"optimization source is still present in the routed inventory");
  std::optional<int> constant;std::string initialization;
  EliminationReason kind;
  if(reason=="unobservable-source-closure") {
    Check(row.IsNull(1) && row.IsNull(2),"unobservable optimization fact must not invent a constant");kind=EliminationReason::kUnobservable;
  } else {
    Check(reason=="constant-state" && !row.IsNull(1) && !row.IsNull(2),"optimization proof classification is invalid");
    const auto value=row.TypedInteger(1);initialization=row.TypedText(2);
    Check((value==0 || value==1) && (initialization=="specified" || initialization=="vendor-confirmed"),
          "optimization constant fact is conditional or invalid");
    if(initialization=="vendor-confirmed") {
      const auto found=confirmed_.find(origin);Check(found!=confirmed_.end() && found->second==value,
          "optimization constant differs from its checked vendor confirmation");
    }
    kind=EliminationReason::kConstantState;constant=value;
  }
  Check(!row.Step(),"optimization source proof repeats");
  return {proof_.Id<EliminationSourceTag>(origin),kind,constant,initialization,name};
}
void NativeOptimizationProofView::RequireOwner(const EliminatedSourceFact& fact) const {proof_.RequireOwner(fact.source);}
void NativeOptimizationProofView::RequireUnchanged() const {proof_.RequireUnchanged();mapped_.RequireUnchanged();lineage_.RequireUnchanged();}
}  // namespace emuflow::core
