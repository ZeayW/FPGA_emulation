#include "emuflow_core/partition_decision.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/partition_semantics.hpp"
#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/resource_view.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cctype>
#include <iomanip>
#include <locale>
#include <numeric>
#include <set>

namespace emuflow::core {
namespace {
using U=std::uint64_t;using Load=std::array<U,13>;
using Object=std::map<std::string,JsonRecord>;using Array=std::vector<JsonRecord>;
void Check(bool value,const std::string& text) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"partition decision "+text);
}
void Add(U& target,U amount) {Check(target<=UINT64_MAX-amount,"resource sum overflow");target+=amount;}
std::vector<std::string> Catalog(CheckedRequestReader& request) {
  const auto count=request.Read<std::uint32_t>();Check(count && count<=65536,"invalid part catalog size");
  std::vector<std::string> result;std::set<std::string> unique;
  for(unsigned i=0;i<count;++i) {auto part=request.ReadString32(false,65536);JsonQuote(part);
    Check(part.find('\0')==std::string::npos && unique.insert(part).second,"invalid part catalog");result.push_back(std::move(part));}
  return result;
}
struct Facts {std::vector<Load> loads;std::vector<U> clusters;Load totals{};};
// Each source cell is read once for all candidates. The source resource reader
// is independent of the primary producer and the optimizer. Cluster resource
// columns are checked against this reduction, never used as the source facts.
std::vector<Facts> Reduce(EmuirView& source,const PrimaryClusterView& primary,
    const std::vector<SelectedPartitionOwnership>& owners,const std::vector<std::string>& parts,bool independent=true) {
  if(independent)primary.ValidateMembers(source);
  std::vector<Facts> facts(owners.size());std::vector<std::vector<unsigned>> translations;
  for(unsigned a=0;a<owners.size();++a) {
    facts[a].loads.resize(parts.size());facts[a].clusters.resize(parts.size());
    std::vector<unsigned> translation;
    for(const auto& part:owners[a].parts) {auto found=std::find(parts.begin(),parts.end(),part);
      Check(found!=parts.end(),"ownership references an unknown FPGA");translation.push_back(found-parts.begin());}
    translations.push_back(std::move(translation));
    for(auto part:owners[a].cluster_parts)++facts[a].clusters.at(translations.back().at(part));
  }
  if(independent) {
  ResourceView resources(source.store().database());
  auto rows=source.store().database().Prepare("SELECT seq,resources_seq FROM cell_records ORDER BY seq");U observed=0;
  while(rows.Step()) {
    Check(rows.TypedInteger(0)>=0 && U(rows.Integer(0))==observed && observed<primary.cells(),"source cell coverage disagrees");
    const auto cluster=primary.Owner(observed++);const auto& resource=resources.Get(rows.TypedInteger(1));
    Check(!resource.exceeds_uint32,"source resource exceeds primary integer domain");
    for(unsigned a=0;a<owners.size();++a) {
      auto& load=facts[a].loads.at(translations[a].at(owners[a].cluster_parts.at(cluster)));
      Add(load[0],1);Add(facts[a].totals[0],1);
      for(unsigned d=1;d<13;++d) {Add(load[d],resource.amounts[d-1]);Add(facts[a].totals[d],resource.amounts[d-1]);}
    }
  }
  Check(observed==primary.cells(),"source cell coverage is incomplete");
  }
  for(unsigned a=0;a<owners.size();++a) {
    std::vector<Load> primary_loads(parts.size());
    for(U cluster=0;cluster<primary.clusters();++cluster) {
      auto& load=primary_loads.at(translations[a].at(owners[a].cluster_parts.at(cluster)));
      for(unsigned d=0;d<13;++d)Add(load[d],primary.Weight(cluster,d));
    }
    if(independent)Check(primary_loads==facts[a].loads && facts[a].totals==primary.totals(),"primary/source resource loads disagree");
    else {facts[a].loads=std::move(primary_loads);facts[a].totals=primary.totals();}
  }
  return facts;
}
JsonRecord Summary(const Facts& facts,U count,const std::vector<std::string>& parts) {
  Array loads,clusters,totals,catalog;
  for(const auto& load:facts.loads) {Array row;for(auto value:load)row.emplace_back(value);loads.emplace_back(std::move(row));}
  for(auto value:facts.clusters)clusters.emplace_back(value);
  for(auto value:facts.totals)totals.emplace_back(value);
  for(const auto& part:parts)catalog.emplace_back(part);
  return Object{{"schema","emuflow.partition-load-summary/v1"},{"clusters",count},{"instances",facts.totals[0]},
    {"parts",catalog},{"loads",loads},{"cluster_counts",clusters},{"totals",totals}};
}
struct Policy {
  std::vector<std::string> parts,active;std::vector<Load> physical,planning;
  std::array<bool,13> balanced{};std::array<double,13> tolerance{};
  U minimum=1;std::string constraints;
};
void ValidatePolicy(const Facts& facts,const Policy& policy) {
  unsigned used=0;std::vector<unsigned> active;
  for(const auto& name:policy.active) {auto found=std::find(policy.parts.begin(),policy.parts.end(),name);
    Check(found!=policy.parts.end(),"active FPGA is unknown");active.push_back(found-policy.parts.begin());}
  for(unsigned p=0;p<policy.parts.size();++p) {
    if(facts.clusters[p]) {++used;Check(std::find(active.begin(),active.end(),p)!=active.end(),"assignment uses an inactive FPGA");}
    for(unsigned d=1;d<13;++d)Check(facts.loads[p][d]<=policy.planning[p][d],"assignment exceeds reserved planning capacity");
  }
  Check(used>=policy.minimum,"assignment uses too few FPGAs");
  for(unsigned d=0;d<13;++d) {
    if(d && (!facts.totals[d] || std::any_of(active.begin(),active.end(),[&](unsigned p){return !policy.physical[p][d];})))continue;
    U total_capacity=0;for(auto p:active)Add(total_capacity,policy.physical[p][d]);
    for(auto p:active) {
      U allowed=d?policy.planning[p][d]:facts.totals[0];
      if(policy.balanced[d]) {
        const double share=d?(total_capacity?double(policy.physical[p][d])/total_capacity:0):1.0/active.size();
        // Preserve the reference's separately rounded Python operations rather
        // than permit a fused multiply-add to change an integer boundary.
        volatile double scaled=double(facts.totals[d])*share;
        volatile double relaxed=scaled*(1.0+policy.tolerance[d]);
        const double upper=std::floor(relaxed+1e-9);
        Check(std::isfinite(upper) && upper>=0,"balance bound is invalid");
        // A finite bound above the integer resource domain cannot constrain
        // these loads. Avoid undefined float-to-uint conversion at that edge.
        if(upper<double(UINT64_MAX))allowed=d?std::min(allowed,U(upper)):U(upper);
      }
      Check(facts.loads[p][d]<=allowed,"assignment violates effective multi-resource balance/capacity");
    }
  }
}
void ValidateConstraints(EmuirView& source,const PrimaryClusterView& primary,
    const std::vector<SelectedPartitionOwnership>& owners,const std::string& text) {
  auto& db=source.store().database();
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1,'$.fixed'),json_type(?1,'$.groups')");valid.BindText(1,text);
  Check(valid.Step() && valid.Integer(0)==1 && valid.Text(1)=="array" && valid.Text(2)=="array","invalid fixed/group constraints");
  auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,text);
  Check(!duplicate.Step(),"duplicate constraint member");
  const auto owner_for=[&](unsigned a,const std::string& identity) {
    auto row=db.Prepare("SELECT seq FROM cell_records WHERE id=?1");row.BindText(1,identity);
    Check(row.Step() && row.TypedInteger(0)>=0 && U(row.Integer(0))<primary.cells(),"constraint references unknown source cell");
    return owners[a].parts.at(owners[a].cluster_parts.at(primary.Owner(row.Integer(0))));
  };
  auto fixed=db.Prepare("SELECT value FROM json_each(?1,'$.fixed')");fixed.BindText(1,text);
  while(fixed.Step()) {
    const auto item=fixed.TypedText(0),identity=DecodeJsonString(JsonMemberToken(db,item,"instance")),part=DecodeJsonString(JsonMemberToken(db,item,"fpga"));
    Check(!identity.empty() && !part.empty(),"empty fixed constraint identity");
    for(unsigned a=0;a<owners.size();++a)Check(owner_for(a,identity)==part,"assignment violates a fixed instance");
  }
  auto groups=db.Prepare("SELECT value FROM json_each(?1,'$.groups')");groups.BindText(1,text);
  while(groups.Step()) {
    const auto group=groups.TypedText(0),members=JsonMemberToken(db,group,"instances");
    auto valid_members=db.Prepare("SELECT json_type(?1)");valid_members.BindText(1,members);
    Check(valid_members.Step() && valid_members.Text(0)=="array","invalid group member array");
    std::vector<std::string> expected(owners.size());U count=0;std::size_t at=0;
    const auto space=[&]{while(at<members.size() && std::isspace(static_cast<unsigned char>(members[at])))++at;};
    space();Check(at<members.size() && members[at++]=='[',"invalid group array");space();
    while(at<members.size() && members[at]!=']') {
      const auto begin=at;Check(members[at++]=='"',"invalid group source identity");bool escaped=false,closed=false;
      while(at<members.size()) {const char c=members[at++];if(escaped)escaped=false;else if(c=='\\')escaped=true;else if(c=='"'){closed=true;break;}}
      Check(closed,"incomplete group source identity");const auto identity=DecodeJsonString(members.substr(begin,at-begin));
      for(unsigned a=0;a<owners.size();++a) {auto part=owner_for(a,identity);if(!count)expected[a]=part;else Check(part==expected[a],"assignment splits a fixed group");}++count;
      space();if(at<members.size() && members[at]==','){++at;space();}else break;
    }
    Check(count && at<members.size() && members[at++]==']',"empty or invalid fixed group");space();Check(at==members.size(),"extra group content");
  }
}
double RoundEven(double value) {
  Check(std::isfinite(value),"timing objective exceeds the finite rank domain");
  // Python round(float) is ties-to-even. nearbyint would depend on the caller's
  // floating-point rounding mode; the explicit tie rule does not.
  const auto lower=std::floor(value),fraction=value-lower;
  return fraction<0.5?lower:fraction>0.5?lower+1:std::fmod(lower,2.0)==0?lower:lower+1;
}
std::string IntegerText(double value) {
  if(value==0)return "0";std::ostringstream out;out.imbue(std::locale::classic());out<<std::fixed<<std::setprecision(0)<<value;return out.str();
}
std::string Selection(const ArtifactRef& candidate,const SelectedPartitionOwnership& initial,
    const SelectedPartitionOwnership& final,const std::string& initial_provider,const std::string& candidate_provider) {
  BinaryReader input(candidate.path());
  Check(input.Read<U>()==0x3154554f54415045ULL && input.Read<std::uint32_t>()==1 && input.Read<std::uint32_t>()==14,"invalid objective envelope");
  const auto length=input.Read<std::uint32_t>();Check(length==32 && input.remaining()>=length,"invalid objective mode");
  std::string mode(length,'\0');input.ReadBytes(mode.data(),length);Check(mode=="endpoint-exact-critical-flow-v14","invalid objective mode");
  std::array<std::array<double,8>,2> objectives{};for(auto& objective:objectives)for(auto& value:objective)value=input.Read<double>();
  for(unsigned i=0;i<4;++i)Check(input.Read<std::int64_t>()>=0,"invalid capacity counter");
  std::array<std::array<std::int64_t,2>,2> path{};for(auto& rank:path)for(auto& value:rank) {value=input.Read<std::int64_t>();Check(value>=0,"invalid timing path rank");}
  for(auto& objective:objectives)for(unsigned i=0;i<8;++i)objective[i]=RoundEven(i<2?objective[i]/1.0e-9:objective[i]);
  bool changed=initial.cluster_parts.size()!=final.cluster_parts.size();
  if(!changed)for(U c=0;c<initial.cluster_parts.size();++c)if(initial.parts[initial.cluster_parts[c]]!=final.parts[final.cluster_parts[c]]) {changed=true;break;}
  const bool improved=path[1]<path[0] || (path[1]==path[0] && objectives[1]<objectives[0]);
  const auto rank=[&](unsigned index) {std::string result="["+std::to_string(path[index][0])+','+std::to_string(path[index][1]);for(auto value:objectives[index])result+=','+IntegerText(value);return result+']';};
  const std::vector<std::string> fields{"maximum_path_hops","maximum_path_tdm_ratio_sum","negative_worst_normalized_slack","negative_total_negative_normalized_slack","negative_paths","maximum_predicted_tdm_ratio","maximum_capacity_domain_load","total_path_partition_transitions","total_bit_hops","cut_bits"};
  Array names;for(const auto& field:fields)names.emplace_back(field);
  auto header=JsonRecord(Object{{"status","pass"},{"provider","patron-path-objective-selection-v2"},
    {"policy","path-hop-then-maximum-path-ratio-sum-v1"},{"objective_fields",names},{"assignment_changed",changed},
    {"timing_improved",improved},{"selected",changed && improved?"candidate":"initial"},
    {"initial_provider",initial_provider},{"candidate_provider",candidate_provider}}).Encode();
  header.pop_back();return header+",\"initial_timing_rank\":"+rank(0)+",\"candidate_timing_rank\":"+rank(1)+'}';
}
}
void AuditPartitionDecision(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()>=3 && inputs.size()<=6,"input coverage disagrees");
  for(const auto& input:inputs)input.RequireUnchanged();
  CheckedRequestReader request(inputs[2].path(),8*1024*1024);
  Check(request.Read<U>()==0x31514450464d5545ULL && request.Read<std::uint32_t>()==1,"invalid request");
  const auto kind=request.Read<std::uint32_t>();Check(kind<=3 && inputs.size()==3+kind,"owner coverage disagrees");
  for(unsigned i=0;i<inputs.size();++i)if(i!=2 && !(ReadFileGeneration(request)==inputs[i].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"partition decision original source generation changed");
  const auto first_parts=Catalog(request);std::vector<std::string> second_parts;if(kind==3)second_parts=Catalog(request);
  Policy policy;policy.parts=Catalog(request);policy.active=Catalog(request);policy.minimum=request.Read<U>();
  Check(policy.minimum && policy.minimum<=policy.active.size(),"invalid minimum occupancy");
  for(unsigned d=0;d<13;++d) {const auto flag=request.Read<std::uint8_t>();Check(flag<=1,"invalid balance flag");policy.balanced[d]=flag;
    policy.tolerance[d]=request.Read<double>();Check(std::isfinite(policy.tolerance[d]) && policy.tolerance[d]>=0,"invalid tolerance");}
  policy.physical.resize(policy.parts.size());policy.planning.resize(policy.parts.size());
  for(unsigned p=0;p<policy.parts.size();++p)for(unsigned d=1;d<13;++d) {
    policy.physical[p][d]=request.Read<U>();policy.planning[p][d]=request.Read<U>();
    Check(policy.physical[p][d]<=INT64_MAX && policy.planning[p][d]<=policy.physical[p][d],"invalid capacity");}
  policy.constraints=request.ReadString32(false,8*1024*1024);
  std::string initial_provider,candidate_provider;
  if(kind==3) {initial_provider=request.ReadString32();candidate_provider=request.ReadString32();}request.RequireEnd();
  EmuirView source(inputs[0]);PrimaryClusterView primary(inputs[1],inputs[0]);
  std::vector<SelectedPartitionOwnership> owners;
  if(kind==3) {owners.push_back(ReadSelectedPartitionOwnership(primary,{inputs[3]},first_parts));
    owners.push_back(ReadSelectedPartitionOwnership(primary,{inputs[4],inputs[5]},second_parts));}
  else owners.push_back(ReadSelectedPartitionOwnership(primary,std::vector<ArtifactRef>(inputs.begin()+3,inputs.end()),first_parts));
  const auto facts=Reduce(source,primary,owners,policy.parts);
  for(const auto& fact:facts)ValidatePolicy(fact,policy);
  ValidateConstraints(source,primary,owners,policy.constraints);
  std::string text="{\"schema\":\"emuflow.partition-decision/v1\",\"status\":\"pass\",\"implementation_id\":\"native-partition-decision-v1\",\"audits\":[";
  for(unsigned i=0;i<facts.size();++i) {if(i)text+=',';text+=Summary(facts[i],primary.clusters(),policy.parts).Encode();}
  text+=']';if(kind==3)text+=",\"selection\":"+Selection(inputs[4],owners[0],owners[1],initial_provider,candidate_provider);text+='}';
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<text<<'\n';});
}
void ReduceOwnedPartitionLoads(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()>=3 && inputs.size()<=5,"load owner coverage disagrees");
  for(const auto& input:inputs)input.RequireUnchanged();
  CheckedRequestReader request(inputs[2].path(),4*1024*1024);
  Check(request.Read<U>()==0x3251454c504d5545ULL && request.Read<std::uint32_t>()==2,"invalid owned load request");
  for(unsigned i=0;i<inputs.size();++i)if(i!=2 && !(ReadFileGeneration(request)==inputs[i].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"partition load original source generation changed");
  const auto parts=Catalog(request);request.RequireEnd();
  EmuirView source(inputs[0]);PrimaryClusterView primary(inputs[1],inputs[0]);
  const auto owner=ReadSelectedPartitionOwnership(primary,std::vector<ArtifactRef>(inputs.begin()+3,inputs.end()),parts);
  const auto facts=Reduce(source,primary,{owner},parts,false);
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<Summary(facts[0],primary.clusters(),parts).Encode()<<'\n';});
}
}
