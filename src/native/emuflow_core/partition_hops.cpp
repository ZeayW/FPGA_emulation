#include "emuflow_core/partition_hops.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/partition_semantics.hpp"
#include "emuflow_core/primary_cluster_view.hpp"
#include "emuflow_core/resource_view.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numeric>
#include <set>
#include <unistd.h>
#include <tuple>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using U=std::uint64_t;using Object=std::map<std::string,JsonRecord>;using Array=std::vector<JsonRecord>;
void Check(bool condition,const std::string& text) {if(!condition)throw StageError(ErrorCode::kInvalidArtifact,"partition hops "+text);}
void Add(U& value,U amount) {Check(value<=UINT64_MAX-amount,"resource/counter overflow");value+=amount;}
std::vector<std::string> Catalog(CheckedRequestReader& r,unsigned maximum=65536) {
  const auto count=r.Read<std::uint32_t>();Check(count && count<=maximum,"invalid catalog size");
  std::vector<std::string> out;std::set<std::string> unique;
  for(unsigned i=0;i<count;++i) {auto name=r.ReadString32(false,65536);JsonQuote(name);
    Check(name.find('\0')==std::string::npos && unique.insert(name).second,"invalid catalog identity");out.push_back(std::move(name));}
  return out;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent) {auto pattern=(parent/".partition-hops-XXXXXX").string();
    Check(mkdtemp(pattern.data())!=nullptr,"cannot create private workspace");root=pattern;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
struct RequestWriter {
  AtomicBinaryWriter out;std::uint32_t crc=0;
  RequestWriter(const fs::path& path,const std::vector<ArtifactRef>& sources):out(path.string(),sources){}
  template<class T>void Put(T value){out.Write(value);UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof value);}
  void Finish(){out.Write(crc);out.Commit();}
};
struct Config {
  unsigned kind=0;bool weights=false;std::vector<std::string> parts,initial_parts,classes,dimensions;
  std::vector<unsigned> dimension_indices;std::map<unsigned,unsigned> fixed;
  PartitionHopModel model;
};
Config Read(const std::vector<ArtifactRef>& inputs,bool audit_only) {
  Check(inputs.size()>=3 && inputs.size()<=7,"input coverage disagrees");
  Check(inputs[0].schema()=="emuir-compact-v2" && inputs[1].schema()=="partition-cluster-columns-v2" && inputs[2].schema()=="partition-hop-request-v1","source schemas disagree");
  CheckedRequestReader r(inputs[2].path(),8*1024*1024);Config c;
  Check(r.Read<U>()==0x31514850464d5545ULL && r.Read<std::uint32_t>()==1,"unsupported request");
  const auto flags=r.Read<std::uint32_t>();c.kind=flags&3;c.weights=flags&4;
  Check(c.kind<=2 && !(flags&~15U) && bool(flags&8)==audit_only && inputs.size()==3+c.kind+2*unsigned(c.weights),"owner/weight source coverage disagrees");
  if(c.kind)Check(inputs[3].schema()==(c.kind==1?"patron-initial-columns-v1":"patron-output-v1"),"owner schema disagrees");
  if(c.kind==2)Check(inputs[4].schema()=="patron-cluster-order-v1","owner coordinate schema disagrees");
  if(c.weights)Check(inputs[3+c.kind].schema()=="net-weight-selector-v2" && inputs[4+c.kind].schema()=="net-weight-store-v1","weight schema disagrees");
  for(unsigned i=0;i<inputs.size();++i)if(i!=2 && !(ReadFileGeneration(r)==inputs[i].generation()))
    throw StageError(ErrorCode::kMixedGeneration,"partition hop original source generation changed");
  c.parts=Catalog(r,255);c.initial_parts=Catalog(r,255);c.classes=Catalog(r,3);c.dimensions=Catalog(r,13);
  for(const auto& name:c.classes)Check(name=="register_output" || name=="register_input" || name=="combinational","invalid transported cut class");
  Check(c.dimensions.front()=="cells","missing cell dimension");
  for(const auto& name:c.dimensions) {
    if(name=="cells"){c.dimension_indices.push_back(0);continue;}
    const auto field=std::find(kResourceFields.begin(),kResourceFields.end(),name);
    Check(field!=kResourceFields.end(),"unknown resource dimension");c.dimension_indices.push_back(1+unsigned(field-kResourceFields.begin()));
  }
  auto& m=c.model;m.num_parts=c.parts.size();m.num_dimensions=c.dimensions.size();
  const auto limit=r.Read<U>(),minimum=r.Read<U>();Check(limit && limit<=INT32_MAX && minimum && minimum<=c.parts.size(),"invalid hop/occupancy limit");
  m.hop_limit=limit;m.min_used_parts=minimum;
  m.distance.resize(m.num_parts,std::vector<int>(m.num_parts));
  for(auto& row:m.distance)for(auto& distance:row){distance=r.Read<std::int32_t>();Check(distance>=-1,"invalid distance");}
  for(int part=0;part<m.num_parts;++part)Check(m.distance[part][part]==0,"distance diagonal must be zero");
  m.balance_bound.resize(m.num_parts,std::vector<double>(m.num_dimensions));
  m.hard_capacity.resize(m.num_parts,std::vector<long long>(m.num_dimensions));
  for(int p=0;p<m.num_parts;++p)for(int d=0;d<m.num_dimensions;++d) {
    const auto bound=r.Read<double>();const auto capacity=r.Read<U>();
    Check(std::isfinite(bound) && bound>=0 && capacity<=INT64_MAX,"invalid balance/capacity bound");
    m.balance_bound[p][d]=bound;m.hard_capacity[p][d]=capacity;
  }
  const auto fixed=r.Read<std::uint32_t>();Check(fixed<=65536,"fixed constraints exceed bound");
  for(unsigned i=0;i<fixed;++i){auto vertex=r.Read<std::uint32_t>(),part=r.Read<std::uint32_t>();Check(part<c.parts.size() && c.fixed.emplace(vertex,part).second,"invalid fixed identity");}
  r.RequireEnd();return c;
}
std::string HashFile(const std::string& path) {
  std::ifstream input(path,std::ios::binary);Check(bool(input),"cannot read weights");Sha256 hash;std::array<char,65536> block;
  while(input){input.read(block.data(),block.size());hash.update(reinterpret_cast<const unsigned char*>(block.data()),input.gcount());}
  Check(input.eof(),"cannot finish weight seal");std::string out;const char* digits="0123456789abcdef";
  for(auto byte:hash.finish()){out+=digits[byte>>4];out+=digits[byte&15];}return out;
}
U BindWeights(SqliteDatabase& db,const Config& c,const std::vector<ArtifactRef>& inputs) {
  if(!c.weights)return 0;
  const auto& selector=inputs[3+c.kind];const auto& payload=inputs[4+c.kind];
  Check(selector.generation().bytes<=512*1024,"weight selector exceeds bound");
  std::ifstream file(selector.path(),std::ios::binary);std::string text(selector.generation().bytes,'\0');file.read(text.data(),text.size());Check(bool(file),"cannot read weight selector");
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,text);Check(valid.Step() && valid.Integer(0)==1 && valid.Text(1)=="object","invalid weight selector");
  auto duplicate=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicate.BindText(1,text);Check(!duplicate.Step(),"duplicate weight selector member");
  const auto raw=[&](const std::string& object,const std::string& key){return JsonMemberToken(db,object,key);};
  Check(DecodeJsonString(raw(text,"schema"))=="emuflow.partition-net-weights/v2","unsupported weight selector");
  const auto manifest=raw(raw(text,"payloads"),"weights"),relative=DecodeJsonString(raw(manifest,"path"));
  Check(fs::path(relative).filename()==relative && fs::canonical(fs::path(selector.path()).parent_path()/relative)==payload.path(),"weight payload path disagrees");
  Check(DecodeJsonString(raw(manifest,"format"))=="sqlite-net-weights/v1" && DecodeJsonString(raw(manifest,"sha256"))==HashFile(payload.path()),"weight payload seal/format disagrees");
  SqliteStore owner(payload);auto attach=db.Prepare("ATTACH DATABASE ? AS hop_weights");attach.BindText(1,SqliteImmutableUri(payload.path()));attach.Execute();
  auto rows=db.Prepare("SELECT net_id,criticality,path_count,weight FROM hop_weights.net_weights ORDER BY net_id");U count=0,positive=0;
  while(rows.Step()) {Check(!rows.TypedText(0).empty() && std::isfinite(rows.TypedReal(1)) && rows.Real(1)>=0 && rows.Real(1)<=1 && rows.TypedInteger(2)>0 && std::isfinite(rows.TypedReal(3)) && rows.Real(3)>0,"invalid timing weight record");++count;if(rows.Real(1)>0)++positive;}
  auto declared=db.Prepare("SELECT json_type(?1),json_extract(?1,'$')");declared.BindText(1,raw(manifest,"records"));Check(declared.Step() && declared.Text(0)=="integer" && declared.TypedInteger(1)>=0 && U(declared.Integer(1))==count,"weight record count disagrees");
  auto unknown=db.Prepare("SELECT 1 FROM hop_weights.net_weights w LEFT JOIN net_records n ON n.id=w.net_id WHERE w.criticality>0 AND n.seq IS NULL LIMIT 1");Check(!unknown.Step(),"timing weight references an unknown net");
  return positive;
}
std::vector<unsigned> Translation(const std::vector<std::string>& input,const std::vector<std::string>& parts) {
  std::vector<unsigned> result;for(const auto& part:input){auto found=std::find(parts.begin(),parts.end(),part);Check(found!=parts.end(),"initial owner uses inactive FPGA");result.push_back(found-parts.begin());}return result;
}
void BuildModel(EmuirView& source,const PrimaryClusterView& primary,Config& c,const SelectedPartitionOwnership& ownership) {
  Check(primary.clusters() && primary.clusters()<=INT32_MAX,"cluster count exceeds kernel domain");
  auto& m=c.model;const auto translation=Translation(ownership.parts,c.parts);m.num_clusters=primary.clusters();m.clusters.resize(m.num_clusters);
  for(int vertex=0;vertex<m.num_clusters;++vertex) {
    auto& cluster=m.clusters[vertex];cluster.part=translation.at(ownership.cluster_parts.at(vertex));
    const auto fix=c.fixed.find(vertex);cluster.fixed_part=fix==c.fixed.end()?-1:int(fix->second);
    for(auto dimension:c.dimension_indices)cluster.weights.push_back(primary.Weight(vertex,dimension));
  }
  for(const auto& fixed:c.fixed)Check(fixed.first<primary.clusters(),"fixed cluster is unknown");
  auto& db=source.store().database();auto nets=db.Prepare(c.weights?
      "SELECT n.seq,n.cut_class,w.criticality,w.weight FROM net_records n LEFT JOIN hop_weights.net_weights w ON w.net_id=n.id ORDER BY n.seq":
      "SELECT seq,cut_class,NULL,NULL FROM net_records ORDER BY seq");
  bool have=nets.Step();std::set<int> drivers,sinks;U observed=0;
  source.VisitConnectivity([&](const EmuirEndpoint& endpoint) {
    Check(have && endpoint.net.sequence()==observed && nets.TypedInteger(0)>=0 && U(nets.Integer(0))==observed,"source net order disagrees");
    if(endpoint.cell)(endpoint.role==EndpointRole::kSink?sinks:drivers).insert(primary.Owner(endpoint.cell->sequence()));
  },[&](EmuirNetId net) {
    Check(have && net.sequence()==observed && nets.TypedInteger(0)>=0 && U(nets.Integer(0))==observed,"source net coverage disagrees");
    if(std::find(c.classes.begin(),c.classes.end(),nets.TypedText(1))!=c.classes.end() && !drivers.empty() && !sinks.empty()) {
      Check(drivers.size()==1,"refinement requires one driver cluster");const int driver=*drivers.begin();sinks.erase(driver);
      if(!sinks.empty()) {PartitionHopNet row;row.source=driver;row.sinks.assign(sinks.begin(),sinks.end());
        if(!nets.IsNull(2) && nets.TypedReal(2)>0)row.weight=nets.TypedReal(3);
        Check(m.nets.size()<INT32_MAX,"net count exceeds kernel domain");m.nets.push_back(std::move(row));}
    }
    drivers.clear();sinks.clear();++observed;have=nets.Step();
  });
  Check(!have && observed==source.counts().nets,"source net coverage is incomplete");m.num_nets=m.nets.size();
}
// This audit reduces original source endpoints independently of the projected
// model. It never accepts the kernel's objective or projected net population.
JsonRecord AuditHops(EmuirView& source,const PrimaryClusterView& primary,const Config& c,const std::vector<int>& parts) {
  auto& db=source.store().database();auto nets=db.Prepare("SELECT seq,id,cut_class FROM net_records ORDER BY seq");
  auto endpoints=db.Prepare("SELECT net_seq,role,instance_seq FROM endpoint_records ORDER BY net_seq,role,ordinal");
  bool have=endpoints.Step();U observed=0,routed=0,violating=0,unreachable=0,total=0,maximum=0;Array violations;
  std::vector<std::tuple<std::string,std::string,std::string,int>> examples;
  while(nets.Step()) {
    Check(nets.TypedInteger(0)>=0 && U(nets.Integer(0))==observed,"independent source net order disagrees");std::set<int> drivers,sinks;
    while(have && endpoints.TypedInteger(0)==static_cast<std::int64_t>(observed)) {
      const auto role=endpoints.TypedInteger(1);Check(role==0 || role==1,"invalid source endpoint role");
      if(!endpoints.IsNull(2)) {const auto cell=endpoints.TypedInteger(2);Check(cell>=0 && U(cell)<primary.cells(),"source endpoint cell is unknown");
        (role?sinks:drivers).insert(parts.at(primary.Owner(cell)));}
      have=endpoints.Step();
    }
    Check(!have || endpoints.TypedInteger(0)>static_cast<std::int64_t>(observed),"orphaned source endpoint");++observed;
    if(std::find(c.classes.begin(),c.classes.end(),nets.TypedText(2))==c.classes.end() || drivers.empty() || sinks.empty())continue;
    Check(drivers.size()==1,"independent audit requires one source FPGA");const int driver=*drivers.begin();sinks.erase(driver);
    for(auto sink:sinks) {++routed;const auto hops=c.model.distance.at(driver).at(sink);
      if(hops<0)++unreachable;else {Add(total,hops);maximum=std::max<U>(maximum,hops);}
      if(hops<0 || hops>c.model.hop_limit) {
        ++violating;auto example=std::make_tuple(nets.TypedText(1),c.parts.at(driver),c.parts.at(sink),hops);
        if(examples.size()<64){examples.push_back(std::move(example));std::push_heap(examples.begin(),examples.end());}
        else if(example<examples.front()){std::pop_heap(examples.begin(),examples.end());examples.back()=std::move(example);std::push_heap(examples.begin(),examples.end());}
      }
    }
  }
  Check(!have && observed==source.counts().nets,"independent source endpoint coverage disagrees");
  std::sort(examples.begin(),examples.end());
  for(const auto& example:examples){const auto hops=std::get<3>(example);
    violations.emplace_back(Object{{"net",std::get<0>(example)},{"source",std::get<1>(example)},{"sink",std::get<2>(example)},
      {"hops",hops<0?JsonRecord():JsonRecord(hops)},{"limit",c.model.hop_limit}});}
  return Object{{"status",violating?"violation":"pass"},{"max_route_hops",c.model.hop_limit},{"routed_net_sink_pairs",routed},
    {"violating_net_sink_pairs",violating},{"unreachable_net_sink_pairs",unreachable},{"maximum_observed_hops",maximum},
    {"total_shortest_path_hops",total},{"violations",violations},{"violations_truncated",violating-violations.size()}};
}
void AuditResources(EmuirView& source,const PrimaryClusterView& primary,const Config& c,const std::vector<int>& owners) {
  Check(owners.size()==primary.clusters(),"result ownership coverage disagrees");
  std::vector<std::array<U,13>> loads(c.parts.size()),projected(c.parts.size());std::vector<U> clusters(c.parts.size());
  for(U vertex=0;vertex<primary.clusters();++vertex) {
    const auto part=owners[vertex];Check(part>=0 && unsigned(part)<c.parts.size(),"invalid result part");++clusters[part];
    if(c.fixed.count(vertex))Check(unsigned(part)==c.fixed.at(vertex),"result changed a fixed source cluster");
    for(unsigned d=0;d<13;++d)Add(projected[part][d],primary.Weight(vertex,d));
  }
  ResourceView resources(source.store().database());auto rows=source.store().database().Prepare("SELECT seq,resources_seq FROM cell_records ORDER BY seq");U observed=0;
  while(rows.Step()) {
    Check(rows.TypedInteger(0)>=0 && U(rows.Integer(0))==observed && observed<primary.cells(),"source cell coverage disagrees");
    auto& load=loads.at(owners.at(primary.Owner(observed++)));Add(load[0],1);const auto& record=resources.Get(rows.TypedInteger(1));
    Check(!record.exceeds_uint32,"source resources exceed primary domain");for(unsigned d=0;d<12;++d)Add(load[d+1],record.amounts[d]);
  }
  Check(observed==primary.cells() && loads==projected,"source/primary resource census disagrees");
  Check(std::count_if(clusters.begin(),clusters.end(),[](U value){return value>0;})>=c.model.min_used_parts,"result uses too few FPGAs");
  for(unsigned p=0;p<c.parts.size();++p)for(unsigned d=0;d<c.dimension_indices.size();++d) {
    const auto amount=loads[p][c.dimension_indices[d]];
    Check(amount<=U(c.model.hard_capacity[p][d]) && double(amount)<=c.model.balance_bound[p][d]+1e-9,"result violates source resource capacity/balance");
  }
}
}
StageResult RefinePartitionHopsFromPrimary(const std::vector<ArtifactRef>& inputs,const std::string& directory,bool audit_only) {
  return RunStage(audit_only?"phase3.audit-hops-from-primary":"phase3.refine-hops-from-primary",[&]{for(const auto& input:inputs)input.RequireUnchanged();return inputs;},[&](const auto& refs) {
    auto config=Read(refs,audit_only);EmuirView source(refs[0]);PrimaryClusterView primary(refs[1],refs[0]);primary.ValidateMembers(source);
    for(const auto& fixed:config.fixed)Check(fixed.first<primary.clusters(),"fixed cluster is unknown");
    const auto weights=BindWeights(source.store().database(),config,refs);
    auto original=ReadSelectedPartitionOwnership(primary,{refs.begin()+3,refs.begin()+3+config.kind},config.initial_parts);
    if(!audit_only)BuildModel(source,primary,config,original);
    const auto translation=Translation(original.parts,config.parts);
    std::vector<int> initial;initial.reserve(original.cluster_parts.size());for(auto part:original.cluster_parts)initial.push_back(translation.at(part));
    original.cluster_parts.clear();original.cluster_parts.shrink_to_fit();
    AuditResources(source,primary,config,initial);const auto before=AuditHops(source,primary,config,initial);
    PartitionHopSolution solution;JsonRecord after;
    if(audit_only){solution.assignment=std::move(initial);solution.metrics["committed_moves"]=0;solution.pass=true;after=before;}
    else {solution=ComputePartitionHops(config.model);config.model.nets.clear();config.model.nets.shrink_to_fit();
      AuditResources(source,primary,config,solution.assignment);after=AuditHops(source,primary,config,solution.assignment);}
    auto& db=source.store().database();Check(solution.pass && DecodeJsonString(JsonMemberToken(db,after.Encode(),"status"))=="pass","refinement could not satisfy hard hop constraints");
    const fs::path output=fs::absolute(directory);Check(!fs::exists(output) && !fs::is_symlink(output),"output directory must be absent");fs::create_directories(output.parent_path());Workspace workspace(output.parent_path());const auto staged=workspace.root/"selected";fs::create_directory(staged);
    auto canonical=config.parts;std::sort(canonical.begin(),canonical.end());
    if(!audit_only){RequestWriter writer(staged/"ownership.bin",refs);writer.Put(U(0x315450494e415045ULL));writer.Put(std::uint32_t(1));writer.Put(U(primary.clusters()));writer.Put(U(primary.cells()));
      for(auto part:solution.assignment)writer.Put(std::uint32_t(part));writer.Put(U(config.fixed.size()));
      for(const auto& fixed:config.fixed){writer.Put(std::uint32_t(fixed.first));writer.Put(std::uint32_t(std::find(canonical.begin(),canonical.end(),config.parts[fixed.second])-canonical.begin()));}writer.Finish();}
    Array parts;for(const auto& part:config.parts)parts.emplace_back(part);Object metrics;for(const auto& metric:solution.metrics)metrics[metric.first]=metric.second;
    const auto summary=JsonRecord(Object{{"schema","emuflow.partition-hops/v1"},{"status","pass"},{"implementation_id","native-primary-hop-refinement-v1"},
      {"clusters",primary.clusters()},{"instances",primary.cells()},{"parts",parts},{"before",before},{"after",after},{"move_count",U(solution.metrics.at("committed_moves"))},
      {"native_metrics",metrics},{"timing_weighted_nets",weights},{"nets",U(config.model.num_nets)}}).Encode();
    PublishTextArtifact((staged/"summary.json").string(),refs,[&](std::ostream& out){out<<summary<<'\n';});
    // All source readers and statements remain within the operation. Sources
    // are immutable; only the completed new result directory is published.
    const auto ready=output.parent_path()/(workspace.root.filename().string()+"-publish");
    fs::rename(staged,ready);
    try{PublishNewDirectory(ready.string(),output.string(),refs);}
    catch(...){std::error_code error;fs::remove_all(ready,error);throw;}
  },[&]{std::vector<ArtifactRef> outputs;if(!audit_only)outputs.push_back(ArtifactRef::Bind((fs::path(directory)/"ownership.bin").string(),"patron-initial-columns-v1"));
    outputs.push_back(ArtifactRef::Bind((fs::path(directory)/"summary.json").string(),"partition-hop-summary-v1"));return outputs;});
}
}
