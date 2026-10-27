#include "partition/phase3.hpp"
#include "common/artifact_io.hpp"
#include "partition/candidate_ids.hpp"
#include "ir/emuir_view.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "flow/native_stages.hpp"
#include "partition/partition_clusters.hpp"
#include "partition/partition_config.hpp"
#include "partition/partition_decision.hpp"
#include "partition/partition_hops.hpp"
#include "partition/partition_init.hpp"
#include "partition/partition_publish.hpp"
#include "partition/partition_semantics.hpp"
#include "partition/patron_ingress.hpp"
#include "partition/primary_cluster_view.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include "common/tool_runtime.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <numeric>
#include <optional>
#include <set>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using U=std::uint64_t;using Raw=std::map<std::string,std::string>;
constexpr U metadata_bound=8*1024*1024;
const std::vector<std::string> transported{"combinational","register_input","register_output"};
void Check(bool value,const std::string& text){if(!value)throw StageError(ErrorCode::kInvalidArtifact,"Phase 3 "+text);}
std::string Object(const Raw& fields){std::string out="{";for(const auto& item:fields){if(out.size()>1)out+=',';out+=JsonQuote(item.first)+':'+item.second;}return out+'}';}
std::string Array(const std::vector<std::string>& tokens){std::string out="[";for(const auto& item:tokens){if(out.size()>1)out+=',';out+=item;}return out+']';}
std::string Names(const std::vector<std::string>& names){std::vector<std::string> out;for(const auto& value:names)out.push_back(JsonQuote(value));return Array(out);}
std::string Number(double value){return JsonRecord(value).Encode();}
std::string Read(const fs::path& path,U limit=metadata_bound){const auto size=fs::file_size(path);Check(size<=limit,"control file exceeds bound");std::ifstream in(path,std::ios::binary);std::string out(size,'\0');in.read(out.data(),size);Check(bool(in),"cannot read control input");return out;}
std::string HashFile(const ArtifactRef& ref){ref.RequireUnchanged();std::ifstream in(ref.path(),std::ios::binary);Check(bool(in),"cannot seal payload");Sha256 hash;std::array<char,65536> block;
  while(in){in.read(block.data(),block.size());hash.update(reinterpret_cast<const unsigned char*>(block.data()),in.gcount());}Check(in.eof(),"payload read failed");ref.RequireUnchanged();const char* digits="0123456789abcdef";std::string text;for(auto byte:hash.finish()){text+=digits[byte>>4];text+=digits[byte&15];}return text;}
class Json {
 public:
  SqliteDatabase db{":memory:",SqliteDatabase::Mode::kReadOnly};
  void Validate(const std::string& text){(void)JsonQuote(text);auto r=db.Prepare("SELECT json_valid(?1)");r.BindText(1,text);Check(r.Step() && r.Integer(0)==1,"invalid control JSON");auto duplicates=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicates.BindText(1,text);Check(!duplicates.Step(),"duplicate control member");}
  std::string Kind(const std::string& value){auto r=db.Prepare("SELECT json_type(?1)");r.BindText(1,value);Check(r.Step(),"missing JSON type");return r.TypedText(0);}
  Raw Fields(const std::string& value){Check(Kind(value)=="object","expected control object");Raw out;VisitJsonObjectTokens(value,[&](const auto& key,const auto& token){Check(out.emplace(key,token).second,"duplicate raw control key");});return out;}
  std::string Get(const std::string& value,const std::string& key,const std::string& fallback="null"){auto fields=Fields(value);const auto found=fields.find(key);return found==fields.end()?fallback:found->second;}
  std::vector<std::string> Values(const std::string& value){Check(Kind(value)=="array","expected control array");std::vector<std::string> out;VisitJsonArrayTokens(value,[&](const auto& token){out.push_back(token);});return out;}
  std::vector<std::string> Strings(const std::string& value){std::vector<std::string> out;for(const auto& token:Values(value))out.push_back(Text(token));return out;}
  std::string Text(const std::string& value){Check(Kind(value)=="text","expected string");return DecodeJsonString(value);}
  U UInt(const std::string& token){Check(Kind(token)=="integer","expected integer");U out=0;auto result=std::from_chars(token.data(),token.data()+token.size(),out);Check(result.ec==std::errc() && result.ptr==token.data()+token.size(),"integer exceeds unsigned domain");return out;}
  std::int64_t Int(const std::string& token){Check(Kind(token)=="integer","expected signed integer");std::int64_t out=0;auto result=std::from_chars(token.data(),token.data()+token.size(),out);Check(result.ec==std::errc() && result.ptr==token.data()+token.size(),"integer exceeds signed domain");return out;}
  double Real(const std::string& token){const auto kind=Kind(token);Check(kind=="integer" || kind=="real","expected finite numeric value");return ParseFiniteDecimal(token,"Phase 3 number");}
  bool Bool(const std::string& token){Check(token=="true" || token=="false","expected boolean");return token=="true";}
};
struct Writer {
  AtomicBinaryWriter out;std::uint32_t crc=0;bool checked;
  Writer(const fs::path& path,const std::vector<ArtifactRef>& sources={},bool checksum=true):out(path.string(),sources),checked(checksum){}
  void Bytes(const void* bytes,std::size_t count){out.WriteBytes(bytes,count);if(checked)UpdateCrc32(crc,static_cast<const unsigned char*>(bytes),count);}
  template<class T>void Put(T value){Bytes(&value,sizeof value);}
  void Generation(const ArtifactRef& ref){const auto& g=ref.generation();Put(g.device);Put(g.inode);Put(g.bytes);Put(g.modified_seconds);Put(g.modified_nanoseconds);Put(g.changed_seconds);Put(g.changed_nanoseconds);}
  void Text(const std::string& text){Check(text.size()<=UINT32_MAX,"text field exceeds protocol domain");Put(std::uint32_t(text.size()));Bytes(text.data(),text.size());}
  void Catalog(const std::vector<std::string>& names){Check(names.size()<=UINT32_MAX,"catalog exceeds protocol domain");Put(std::uint32_t(names.size()));for(const auto& value:names)Text(value);}
  void Finish(){if(checked)out.Write(crc);out.Commit();}
};
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent){auto name=(parent/".phase3-stage-XXXXXX").string();Check(mkdtemp(name.data())!=nullptr,"cannot create workspace");root=name;}
  ~Workspace(){std::error_code error;fs::remove_all(root,error);}
};
void Require(const StageResult& result){if(!result.ok())throw StageError(result.error,result.stage+": "+result.message);}
struct Fpga {std::string id,capacity_json;std::array<U,13> physical{},planning{};};
struct Owner {std::vector<ArtifactRef> sources;std::vector<std::string> parts;};
struct Stage {
  Json json;std::string config,constraints,design,platform,policy;
  std::map<std::string,ArtifactRef> refs;std::vector<ArtifactRef> inputs;
  std::vector<std::string> steps,tool_runs;std::vector<Fpga> fpgas;std::vector<unsigned> active,sorted_active,dimensions;
  std::vector<std::string> parts,active_parts,sorted_parts;
  std::array<bool,13> balanced{};std::array<double,13> tolerance{};
  std::map<unsigned,std::string> fixed;std::map<unsigned,std::set<std::string>> groups;
  U minimum=1,seed=0,depth=0;std::string provider;std::optional<ArtifactRef> primary,candidates;
  std::unique_ptr<EmuirView> source;std::unique_ptr<PrimaryClusterView> clusters;
  const volatile std::sig_atomic_t* cancelled;
  Stage(const ArtifactRef& request,const volatile std::sig_atomic_t* cancel):cancelled(cancel){
    inputs.push_back(request);config=Read(request.path());json.Validate(config);
    Check(json.Text(json.Get(config,"schema"))=="emuflow.phase3-stage/v1","unsupported stage request");
    const std::set<std::string> keys{"schema","inputs","platform","platform_sha256","partition_configuration","cluster_policy","candidate_selection_policy","provider","seed","openroad","timeout_seconds","seed_attempts","num_initial_solutions","num_best_initial_solutions","repair_min_used","unweighted_baseline","max_moves","route_constraints","domains","shortest_routes","hop_distances"};
    const auto fields=json.Fields(config);Check(fields.size()==keys.size(),"stage request field coverage disagrees");for(const auto& field:fields)Check(keys.count(field.first),"unknown stage request member");
    const std::map<std::string,std::string> schemas{{"native","external-tool-executable-v1"},{"source","emuir-compact-v2"},{"board","boarddb-v1"},{"candidate_policy","candidate-policy-request-v1"},
      {"constraints","partition-constraints-input-v1"},{"timing_selector","sta-path-selector-v1"},{"timing","timing-path-store-v1"},
      {"weight_selector","net-weight-selector-v2"},{"weights","net-weight-store-v1"},{"solution","tritonpart-solution-v1"}};
    for(const auto& field:json.Fields(json.Get(config,"inputs"))) {
      const auto expected=schemas.find(field.first);Check(expected!=schemas.end(),"unknown input role");
      const auto record=json.Fields(field.second);Check(record.size()==3 && record.count("path") && record.count("schema") && record.count("generation"),"unknown source binding member");
      const auto path=json.Text(json.Get(field.second,"path")),schema=json.Text(json.Get(field.second,"schema"));Check(schema==expected->second && fs::path(path).is_absolute(),"input schema/path disagrees");
      auto ref=ArtifactRef::Bind(path,schema);const auto g=json.Values(json.Get(field.second,"generation"));Check(g.size()==7,"input generation coverage disagrees");
      const FileGeneration generation{json.UInt(g[0]),json.UInt(g[1]),json.UInt(g[2]),json.Int(g[3]),json.Int(g[4]),json.Int(g[5]),json.Int(g[6])};
      if(!(generation==ref.generation()))throw StageError(ErrorCode::kMixedGeneration,"Phase 3 original input generation changed");
      refs.emplace(field.first,ref);inputs.push_back(ref);
    }
    for(const auto* role:{"native","source","board","candidate_policy"})Check(refs.count(role),"missing required input role");
    Check(refs.count("weights")==refs.count("weight_selector") && refs.count("timing")==refs.count("timing_selector"),"unpaired selector/payload");
    source=std::make_unique<EmuirView>(refs.at("source"));auto row=source->store().database().Prepare("SELECT value FROM metadata WHERE key='design_json'");Check(row.Step(),"source design is missing");design=json.Text(json.Get(row.TypedText(0),"name"));
    platform=json.Text(json.Get(config,"platform"));provider=json.Text(json.Get(config,"provider"));Check(provider=="patron" || provider=="tritonpart","unsupported provider");
    seed=json.UInt(json.Get(config,"seed"));Check(seed<=INT32_MAX,"partition seed exceeds kernel domain");
    const auto cluster_policy=json.Get(config,"cluster_policy");
    Check(json.Text(json.Get(cluster_policy,"cut_mode"))=="static-exact-combinational" && json.Text(json.Get(cluster_policy,"qualification"))=="structural-partition-legality" && json.Strings(json.Get(cluster_policy,"transported_cut_classes"))==transported,"cluster policy disagrees with native stage");
    depth=json.UInt(json.Get(cluster_policy,"max_cross_fpga_dependency_depth"));
    policy=json.Text(json.Get(config,"candidate_selection_policy"));Check(policy=="assignment-derived-acyclic-v2","unsupported candidate policy");
  }
  void Unchanged(){for(const auto& input:inputs)input.RequireUnchanged();if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"Phase 3 cancelled");}
  void AddResult(const StageResult& result){Require(result);steps.push_back(StageResultJson(result));Unchanged();}
  template<class F>void Action(const std::string& name,F produce){auto result=RunStage(name,[&]{Unchanged();return inputs;},[&](const auto&){produce();});AddResult(result);}
  std::vector<Fpga> Targets(bool sorted=false){std::vector<Fpga> out;for(auto p:sorted?sorted_active:active)out.push_back(fpgas[p]);return out;}
  std::string Field(unsigned d)const{return d?kResourceFields[d-1]:"cells";}
  void Balance(Writer& w){for(unsigned d=0;d<13;++d){w.Put(std::uint8_t(balanced[d]));w.Put(tolerance[d]);}}
  void Capacities(Writer& w,bool sorted=false){for(const auto& fpga:Targets(sorted))for(unsigned d=0;d<13;++d){w.Put(fpga.physical[d]);w.Put(fpga.planning[d]);}}
  void Normalize(){
    const auto settings=json.Get(config,"partition_configuration");
    const auto raw=refs.count("constraints")?Read(refs.at("constraints").path(),UINT64_MAX):"null";(void)JsonQuote(raw);
    constraints=NormalizePartitionConfiguration(refs.at("source"),settings,raw);
    const auto normalized=json.Get(settings,"board"),raw_board=Read(refs.at("board").path());json.Validate(raw_board);
    Check(json.Text(json.Get(json.Get(raw_board,"platform"),"name"))==platform,"board name disagrees");
    const auto original=json.Values(json.Get(raw_board,"fpgas")),configured=json.Values(json.Get(normalized,"fpgas"));Check(original.size()==configured.size() && !original.empty() && original.size()<=255,"board inventory size disagrees");
    const auto reservations=json.Get(constraints,"resource_reservations_by_fpga","{}");
    for(unsigned p=0;p<configured.size();++p){Fpga fpga;fpga.id=json.Text(json.Get(configured[p],"id"));fpga.capacity_json=json.Get(configured[p],"effective_capacity");
      Check(json.Text(json.Get(original[p],"id"))==fpga.id && std::find(parts.begin(),parts.end(),fpga.id)==parts.end(),"board FPGA identity/order disagrees");
      const auto raw_capacity=json.Fields(json.Get(original[p],"capacity")),capacity=json.Fields(fpga.capacity_json);const double util=json.Real(json.Get(original[p],"utilization_limit"));Check(util>0 && util<=1,"invalid board utilization limit");
      Check(raw_capacity.size()==capacity.size(),"effective capacity field coverage disagrees");
      for(const auto& field:capacity){const auto found=std::find(kResourceFields.begin(),kResourceFields.end(),field.first);Check(found!=kResourceFields.end() && raw_capacity.count(field.first),"unknown board resource");const unsigned d=1+unsigned(found-kResourceFields.begin());const auto amount=json.UInt(field.second);const double expected=std::floor(double(json.UInt(raw_capacity.at(field.first)))*util);Check(expected>=0 && expected<double(UINT64_MAX) && amount==U(expected) && amount<=INT64_MAX,"effective capacity disagrees with BoardDB");fpga.physical[d]=fpga.planning[d]=amount;}
      const auto reserved=json.Get(reservations,fpga.id,"{}");for(const auto& field:json.Fields(reserved)){const auto resource=std::find(kResourceFields.begin(),kResourceFields.end(),field.first);Check(resource!=kResourceFields.end(),"unknown reservation dimension");const unsigned d=1+unsigned(resource-kResourceFields.begin());auto value=json.UInt(field.second);Check(value<=fpga.planning[d],"reservation exceeds capacity");fpga.planning[d]-=value;}
      parts.push_back(fpga.id);fpgas.push_back(std::move(fpga));
    }
    active_parts=json.Strings(json.Get(constraints,"active_fpgas"));for(unsigned p=0;p<parts.size();++p)if(std::find(active_parts.begin(),active_parts.end(),parts[p])!=active_parts.end())active.push_back(p);
    Check(active.size()==active_parts.size() && !active.empty(),"invalid active targets");active_parts.clear();for(auto p:active)active_parts.push_back(parts[p]);
    sorted_active=active;std::sort(sorted_active.begin(),sorted_active.end(),[&](unsigned a,unsigned b){return parts[a]<parts[b];});for(auto p:sorted_active)sorted_parts.push_back(parts[p]);
    minimum=json.UInt(json.Get(constraints,"min_used_fpgas"));
    const auto selected=json.Get(constraints,"balance_dimensions"),overrides=json.Get(constraints,"balance_tolerance_by_dimension");std::set<std::string> selected_fields;
    if(selected!="null"){const auto names=json.Strings(selected);selected_fields.insert(names.begin(),names.end());}
    for(unsigned d=0;d<13;++d){balanced[d]=selected=="null" || selected_fields.count(Field(d));tolerance[d]=json.Real(json.Get(overrides,Field(d),json.Get(constraints,"balance_tolerance")));}
  }
  U CellSequence(const std::string& name){auto query=source->store().database().Prepare("SELECT seq FROM cell_records WHERE id=?");query.BindText(1,name);Check(query.Step() && query.TypedInteger(0)>=0,"constraint references unknown source cell");return query.Integer(0);}
  void Cluster(const fs::path& root){
    const auto candidate_path=root/"candidates.bin";Action("phase3.candidates",[&]{BuildCandidateIds({refs.at("source"),refs.at("candidate_policy")},candidate_path.string());});candidates=ArtifactRef::Bind(candidate_path.string(),"candidate-id-columns-v1");
    const auto request=root/"clusters.bin.request";const auto primary_path=root/"clusters.bin";
    {Writer w(request,inputs,false);w.Put(U(0x31515243464d5545ULL));w.Put(std::uint32_t(1));w.Generation(refs.at("source"));w.Put(U(source->counts().cells));w.Put(U(source->counts().nets));
      BinaryReader candidate(candidate_path.string());ReadAndValidateHeader(candidate,{0x31544343464d5545ULL,1,"candidate columns"});Check(candidate.Read<U>()==source->counts().nets,"candidate net count disagrees");(void)candidate.Read<U>();
      const U bytes=(source->counts().nets+7)/8;w.Put(bytes);std::array<char,65536> block;for(U offset=0;offset<bytes;){const auto count=std::min<U>(block.size(),bytes-offset);candidate.ReadBytes(block.data(),count);w.Bytes(block.data(),count);offset+=count;}
      const auto groups=json.Values(json.Get(constraints,"groups"));w.Put(U(groups.size()));for(const auto& group:groups){auto members=json.Strings(json.Get(group,"instances"));w.Put(U(members.size()));for(const auto& name:members){const auto seq=CellSequence(name);Check(seq<=UINT32_MAX,"group cell exceeds integer domain");w.Put(std::uint32_t(seq));}}w.Finish();}
    Action("phase3.clusters",[&]{BuildPartitionClusters({refs.at("source"),ArtifactRef::Bind(request.string(),"partition-cluster-request-v1")},primary_path.string());});
    primary=ArtifactRef::Bind(primary_path.string(),"partition-cluster-columns-v2");clusters=std::make_unique<PrimaryClusterView>(*primary,refs.at("source"));
    for(const auto& item:json.Values(json.Get(constraints,"fixed"))){const auto vertex=clusters->Owner(CellSequence(json.Text(json.Get(item,"instance"))));const auto part=json.Text(json.Get(item,"fpga"));const auto found=fixed.emplace(vertex,part);Check(found.second || found.first->second==part,"cluster has conflicting fixed targets");}
    for(const auto& group:json.Values(json.Get(constraints,"groups"))){const auto name=json.Text(json.Get(group,"id"));for(const auto& member:json.Strings(json.Get(group,"instances")))groups[clusters->Owner(CellSequence(member))].insert(name);}
    dimensions={0};for(unsigned d=1;d<13;++d)if(clusters->totals()[d] && std::all_of(active.begin(),active.end(),[&](unsigned p){return fpgas[p].physical[d]>0;}))dimensions.push_back(d);
  }
  U Limit(unsigned part,unsigned d){U allowed=d?fpgas[part].planning[d]:clusters->totals()[0];if(balanced[d]){U total=0;for(auto p:active){Check(total<=UINT64_MAX-fpgas[p].physical[d],"capacity sum overflow");total+=fpgas[p].physical[d];}const double share=d?(total?double(fpgas[part].physical[d])/total:0):1.0/active.size();volatile double scaled=double(clusters->totals()[d])*share;volatile double relaxed=scaled*(1.0+tolerance[d]);const double value=std::floor(relaxed+1e-9);Check(std::isfinite(value) && value>=0 && value<double(UINT64_MAX),"balance bound overflow");allowed=d?std::min<U>(allowed,U(value)):U(value);}return allowed;}
  Owner Initialize(const fs::path& root,std::string& summary){
    if(active.size()==1){summary="{}";return {{},active_parts};}
    const bool weights=refs.count("weights"),imported=refs.count("solution");std::vector<ArtifactRef> sources{refs.at("source"),*primary};
    if(weights){sources.push_back(refs.at("weight_selector"));sources.push_back(refs.at("weights"));}if(imported)sources.push_back(refs.at("solution"));
    const auto request=root/"initialize.request";{Writer w(request,inputs);w.Put(U(0x31544950464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(weights+2*imported));for(const auto& ref:sources)w.Generation(ref);
      w.Catalog(active_parts);w.Catalog(transported);w.Put(seed);w.Put(json.UInt(json.Get(config,"timeout_seconds")));for(const auto* name:{"seed_attempts","num_initial_solutions","num_best_initial_solutions"}){const auto value=json.UInt(json.Get(config,name));Check(value<=UINT32_MAX,"initializer effort exceeds integer domain");w.Put(std::uint32_t(value));}
      w.Put(std::uint32_t(minimum));w.Put(std::uint32_t(json.Bool(json.Get(config,"repair_min_used"))));w.Put(std::uint32_t(json.Bool(json.Get(config,"unweighted_baseline"))));w.Put(json.Real(json.Get(constraints,"balance_tolerance")));w.Text(json.Text(json.Get(config,"openroad")));Balance(w);Capacities(w);
      w.Put(std::uint32_t(fixed.size()));for(const auto& pair:fixed){auto found=std::find(active_parts.begin(),active_parts.end(),pair.second);Check(found!=active_parts.end(),"fixed target is inactive");w.Put(std::uint32_t(pair.first));w.Put(std::uint32_t(found-active_parts.begin()));}w.Finish();}
    std::vector<ArtifactRef> arguments{sources[0],sources[1],ArtifactRef::Bind(request.string(),"partition-initialization-request-v1")};arguments.insert(arguments.end(),sources.begin()+2,sources.end());
    auto result=InitializePartition(arguments,(root/"initial").string(),cancelled);AddResult(result);Check(result.outputs.size()==2,"initializer output coverage disagrees");summary=Read(result.outputs[1].path());seed=json.UInt(json.Get(summary,"selected_seed"));return {{result.outputs[0]},active_parts};
  }
  Owner Hops(const fs::path& root,const Owner& original,bool audit,std::string& summary){
    const auto routes=json.Get(config,"route_constraints"),limit=json.Get(routes,"max_route_hops");if(limit=="null"){summary=Object({{"schema",JsonQuote("emuflow.hop-partition-refinement/v1")},{"status",JsonQuote("not-requested")},{"enabled","false"},{"reason",JsonQuote("route constraints do not define max_route_hops")}});return original;}
    std::vector<ArtifactRef> sources{refs.at("source"),*primary};sources.insert(sources.end(),original.sources.begin(),original.sources.end());
    const bool weights=!audit && refs.count("weights");if(weights){sources.push_back(refs.at("weight_selector"));sources.push_back(refs.at("weights"));}
    const auto request=root/(audit?"audit-hop.request":"repair-hop.request");{Writer w(request,inputs);w.Put(U(0x31514850464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(original.sources.size()+4*weights+8*audit));for(const auto& ref:sources)w.Generation(ref);w.Catalog(active_parts);w.Catalog(original.parts);w.Catalog(transported);std::vector<std::string> fields;for(auto d:dimensions)fields.push_back(Field(d));w.Catalog(fields);w.Put(json.UInt(limit));w.Put(minimum);
      const auto distances=json.Get(config,"hop_distances");for(const auto& a:active_parts)for(const auto& b:active_parts){const auto value=json.Get(json.Get(distances,a),b);const auto distance=value=="null"?-1:json.Int(value);Check(distance>=-1 && distance<=INT32_MAX,"hop distance exceeds kernel domain");w.Put(std::int32_t(distance));}
      for(auto p:active)for(auto d:dimensions){const auto allowed=Limit(p,d);w.Put(double(allowed));w.Put(d?fpgas[p].planning[d]:allowed);}w.Put(std::uint32_t(fixed.size()));for(const auto& item:fixed){w.Put(std::uint32_t(item.first));w.Put(std::uint32_t(std::find(active_parts.begin(),active_parts.end(),item.second)-active_parts.begin()));}w.Finish();}
    std::vector<ArtifactRef> arguments{sources[0],sources[1],ArtifactRef::Bind(request.string(),"partition-hop-request-v1")};arguments.insert(arguments.end(),sources.begin()+2,sources.end());
    const auto result=RefinePartitionHopsFromPrimary(arguments,(root/(audit?"audited-hops":"repaired-hops")).string(),audit);AddResult(result);summary=Read(result.outputs.back().path());
    return audit || json.UInt(json.Get(summary,"move_count"))==0?original:Owner{{result.outputs.front()},active_parts};
  }
  Owner Patron(const fs::path& root,const Owner& initial,std::string& model_summary){
    Check(refs.count("timing"),"PATRON requires complete indexed timing paths");
    const auto manifest=Read(refs.at("timing_selector").path());json.Validate(manifest);
    Check(json.Text(json.Get(manifest,"schema"))=="emuflow.sta-path-database/v3","unsupported timing selector schema");
    Check(json.Text(json.Get(manifest,"design"))==design,"timing design disagrees");const auto payload=json.Get(json.Get(manifest,"payloads"),"paths");
    Check(json.Text(json.Get(payload,"format"))=="sqlite-sta-path/v1","unsupported timing payload format");
    const auto relative=json.Text(json.Get(payload,"path"));Check(fs::path(relative).filename()==relative && fs::canonical(fs::path(refs.at("timing_selector").path()).parent_path()/relative)==refs.at("timing").path(),"timing selector/payload path disagrees");
    Check(json.Text(json.Get(payload,"sha256"))==HashFile(refs.at("timing")),"timing payload seal disagrees");
    const auto normalization=json.Get(manifest,"normalization"),routes=json.Get(config,"route_constraints");
    const auto request=root/"patron.request";std::vector<ArtifactRef> sources{refs.at("source"),refs.at("timing"),*primary,initial.sources.at(0)};
    {Writer w(request,inputs);w.Put(U(0x3151455254415045ULL));w.Put(std::uint32_t(2));for(const auto& source:sources)w.Generation(source);w.Catalog(sorted_parts);w.Catalog(initial.parts);w.Catalog(transported);w.Put(json.UInt(json.Get(payload,"records")));
      const auto max_hops=json.Get(routes,"max_route_hops"),moves=json.Get(config,"max_moves");const U budget=moves=="null"?clusters->clusters():json.UInt(moves);Check(budget<=INT32_MAX,"PATRON effort exceeds kernel domain");
      const std::array<std::int64_t,10> integers{{max_hops=="null"?-1:json.Int(max_hops),json.Int(json.Get(routes,"tdm_max_ratio")),json.Int(json.Get(routes,"tdm_ratio_quantum")),std::int64_t(minimum),std::int64_t(budget),4,2048,32,64,json.Int(json.Get(routes,"tdm_min_ratio","1"))}};
      for(auto value:integers){Check(value>=-1 && value<=INT32_MAX,"PATRON integer policy exceeds domain");w.Put(std::int32_t(value));}
      for(const auto* name:{"positive_slack_scale_ns","negative_slack_scale_ns","max_clock_period_ns"})w.Put(json.Real(json.Get(normalization,name)));w.Put(0.0);w.Put(0.0);
      for(int value:{4,0,8,512,256,256})w.Put(std::int32_t(value));Balance(w);Capacities(w,true);
      const auto domains=json.Values(json.Get(config,"domains"));Check(domains.size()<=4096,"capacity domain count exceeds bound");w.Put(std::uint32_t(domains.size()));
      for(const auto& domain:domains){w.Put(std::int32_t(json.Int(json.Get(domain,"lanes"))));w.Put(std::uint8_t(json.Bool(json.Get(domain,"sll"))));w.Put(json.Real(json.Get(domain,"slope")));for(const auto* key:{"opposite","bank_count","wires_per_bank"})w.Put(std::int32_t(json.Int(json.Get(domain,key))));w.Put(json.Int(json.Get(domain,"capacity_bits")));}
      const auto matrix=json.Get(config,"shortest_routes");for(const auto& a:sorted_parts)for(const auto& b:sorted_parts){const auto value=json.Get(json.Get(matrix,a),b);if(value=="null"){w.Put(std::int32_t(-1));continue;}const auto path=json.Values(value);Check(path.size()<=65536,"route length exceeds bound");w.Put(std::int32_t(path.size()));for(const auto& arc:path){const auto fields=json.Values(arc);Check(fields.size()==2,"invalid capacity route arc");w.Put(std::uint32_t(json.UInt(fields[0])));w.Put(json.Real(fields[1]));}}w.Finish();}
    sources.push_back(ArtifactRef::Bind(request.string(),"patron-board-request-v2"));const auto output=root/"patron.bin";
    ToolRequest tool;tool.argv={refs.at("native").path(),"--report",(root/"patron-operation.json").string(),"phase3","refine-patron-from-primary"};
    for(const auto& ref:sources)tool.argv.push_back(ref.path());tool.argv.push_back(output.string());tool.working_directory=root.string();
    const U timeout=json.UInt(json.Get(config,"timeout_seconds"));Check(timeout && timeout<=365ULL*24*3600,"invalid search deadline");tool.timeout_ms=timeout*1000;tool.tail_bytes=65536;
    Action("phase3.patron",[&]{auto bindings=inputs;bindings.insert(bindings.end(),sources.begin(),sources.end());const auto result=RunExternalTool(tool,cancelled,bindings);
      if(!result.operation.ok())throw StageError(result.operation.error,"native PATRON: "+result.operation.message+"\n"+result.diagnostic_tail);
      tool_runs.push_back(ToolResultJson(result));
      const auto proof=Read(root/"patron-operation.json");json.Validate(proof);
      Check(json.Text(json.Get(proof,"stage"))=="phase3.refine-patron-from-primary" && json.Text(json.Get(proof,"status"))=="pass","PATRON producer identity disagrees");
      const auto records=json.Values(json.Get(proof,"outputs"));Check(records.size()==3,"PATRON producer output coverage disagrees");
      const std::string paths[]={output.string(),output.string()+".order",output.string()+".summary.json"},schemas[]={"patron-output-v1","patron-cluster-order-v1","patron-model-summary-v1"};
      for(unsigned i=0;i<3;++i){const auto original=ArtifactRef::Bind(paths[i],schemas[i]);const auto& g=original.generation();
        Check(json.Text(json.Get(records[i],"path"))==original.path() && json.Text(json.Get(records[i],"schema"))==schemas[i],"PATRON output path/schema disagrees");
        const auto generation=json.Get(records[i],"generation");const FileGeneration expected{json.UInt(json.Get(generation,"device")),json.UInt(json.Get(generation,"inode")),json.UInt(json.Get(generation,"bytes")),json.Int(json.Get(generation,"mtime_seconds")),json.Int(json.Get(generation,"mtime_nanoseconds")),json.Int(json.Get(generation,"ctime_seconds")),json.Int(json.Get(generation,"ctime_nanoseconds"))};
        if(!(expected==g))throw StageError(ErrorCode::kMixedGeneration,"native PATRON output generation changed");}
    });model_summary=Read(output.string()+".summary.json");
    return {{ArtifactRef::Bind(output.string(),"patron-output-v1"),ArtifactRef::Bind(output.string()+".order","patron-cluster-order-v1")},sorted_parts};
  }
  std::string Audit(const fs::path& root,const Owner& initial,const std::optional<Owner>& candidate){
    std::vector<ArtifactRef> sources{refs.at("source"),*primary};sources.insert(sources.end(),initial.sources.begin(),initial.sources.end());if(candidate)sources.insert(sources.end(),candidate->sources.begin(),candidate->sources.end());
    const unsigned kind=candidate?3:initial.sources.size();const auto request=root/"decision.request";
    {Writer w(request,inputs);w.Put(U(0x31514450464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(kind));for(const auto& source:sources)w.Generation(source);w.Catalog(initial.parts);if(candidate)w.Catalog(candidate->parts);w.Catalog(parts);w.Catalog(active_parts);w.Put(minimum);Balance(w);
      for(const auto& fpga:fpgas)for(unsigned d=1;d<13;++d){w.Put(fpga.physical[d]);w.Put(fpga.planning[d]);}w.Text(Object({{"fixed",json.Get(constraints,"fixed")},{"groups",json.Get(constraints,"groups")}}));
      if(candidate){w.Text("tritonpart-openroad-hypergraph-v1");w.Text("patron-predictive-timing-flow-native-v14");}w.Finish();}
    std::vector<ArtifactRef> args{sources[0],sources[1],ArtifactRef::Bind(request.string(),"partition-decision-request-v1")};args.insert(args.end(),sources.begin()+2,sources.end());const auto output=root/"decision.json";
    Action("phase3.decision",[&]{AuditPartitionDecision(args,output.string());});return Read(output);
  }
  Raw BalanceSummary(const std::string& audit){
    const auto all_loads=json.Values(json.Get(audit,"loads")),totals=json.Values(json.Get(audit,"totals"));Check(all_loads.size()==parts.size() && totals.size()==13,"audit population disagrees");
    std::vector<std::array<U,13>> loads(parts.size());for(unsigned p=0;p<parts.size();++p){auto row=json.Values(all_loads[p]);Check(row.size()==13,"audit resource width disagrees");for(unsigned d=0;d<13;++d)loads[p][d]=json.UInt(row[d]);}
    Raw percentages,modes,enforced,load_fields,bounds,planning,physical,reservations,shares,max_mean;std::vector<std::string> fields,strict,capacity_only;double maximum_percent=0,maximum_ratio=0;
    for(auto d:dimensions){const auto name=Field(d);fields.push_back(name);percentages[name]=Number(tolerance[d]*100);maximum_percent=std::max(maximum_percent,tolerance[d]*100);modes[name]=JsonQuote(balanced[d]?"balance":"capacity");(balanced[d]?strict:capacity_only).push_back(name);if(balanced[d])enforced[name]=percentages[name];U maximum=0;for(auto p:active)maximum=std::max(maximum,loads[p][d]);max_mean[name]=Number(clusters->totals()[d]?double(active.size()*maximum)/clusters->totals()[d]:0);}
    std::sort(strict.begin(),strict.end());std::sort(capacity_only.begin(),capacity_only.end());const auto reserved=json.Get(constraints,"resource_reservations_by_fpga","{}");
    for(auto p:active){Raw row,allowed,device,capacity,share;for(auto d:dimensions){const auto name=Field(d);const U limit=Limit(p,d);row[name]=std::to_string(loads[p][d]);allowed[name]=std::to_string(limit);device[name]=std::to_string(d?fpgas[p].physical[d]:clusters->totals()[d]);capacity[name]=std::to_string(d?fpgas[p].planning[d]:clusters->totals()[d]);share[name]=Number(clusters->totals()[d]?double(loads[p][d])/clusters->totals()[d]:0);if(limit)maximum_ratio=std::max(maximum_ratio,double(loads[p][d])/limit);else Check(!loads[p][d],"zero bound carries resource load");}
      load_fields[parts[p]]=Object(row);bounds[parts[p]]=Object(allowed);planning[parts[p]]=Object(capacity);physical[parts[p]]=Object(device);shares[parts[p]]=Object(share);reservations[parts[p]]=json.Get(reserved,parts[p],"{}");}
    return {{"requested_balance_percent",Number(json.Real(json.Get(constraints,"balance_tolerance"))*100)},{"effective_balance_percent",Number(maximum_percent)},
      {"requested_balance_percent_by_dimension",Object(percentages)},{"effective_balance_percent_by_dimension",Object(percentages)},
      {"balance_auto_relaxed","false"},{"balance_dimensions",Names(fields)},{"strict_balance_dimensions",Names(strict)},{"capacity_only_dimensions",Names(capacity_only)},
      {"constraint_mode_by_dimension",Object(modes)},{"enforced_balance_percent_by_dimension",Object(enforced)},
      {"capacity_limits",Object(planning)},{"physical_capacity_limits",Object(physical)},{"resource_reservations_by_fpga",Object(reservations)},
      {"balance_observation",Object({{"diagnostic_only",strict.empty()?"true":"false"},{"hard_constraint_dimensions",Names(strict)},{"design_share_by_fpga",Object(shares)},{"max_to_mean_by_dimension",Object(max_mean)}})},
      {"balance_loads",Object(load_fields)},{"balance_allowed_loads",Object(bounds)},{"max_balance_limit_ratio",Number(maximum_ratio)},
      {"balance_violations","0"},{"strict_balance_violations","0"},{"capacity_violations","0"}};
  }
  std::pair<std::string,std::string> Semantics(const fs::path& root,const Owner& selected){
    std::vector<ArtifactRef> sources{refs.at("source"),*primary,refs.at("candidate_policy")};sources.insert(sources.end(),selected.sources.begin(),selected.sources.end());const auto request=root/"semantics.request";
    {Writer w(request,inputs);w.Put(U(0x31514546464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(selected.sources.size()));for(const auto& ref:sources)w.Generation(ref);w.Catalog(selected.parts);w.Put(depth);w.Finish();}
    std::vector<ArtifactRef> args{sources[0],sources[1],sources[2],ArtifactRef::Bind(request.string(),"partition-finalization-request-v1")};args.insert(args.end(),selected.sources.begin(),selected.sources.end());
    AddResult(FinalizePartitionSemantics(args,(root/"semantics").string()));const auto summary=Read(root/"semantics/summary.json");
    const auto header=Object({{"schema",JsonQuote("emuflow.static-exact-combinational-cut/v3")},{"mode",JsonQuote("static-exact-combinational")},{"qualification",JsonQuote("structural-partition-legality")},
      {"max_cross_fpga_dependency_depth",std::to_string(depth)},{"candidate_selection_policy",JsonQuote(policy)},{"metrics",json.Get(summary,"metrics")},
      {"source_identity",Object({{"emuir",Object({{"schema",JsonQuote("emuflow.emuir/v1")},{"design",JsonQuote(design)},{"instances",std::to_string(source->counts().cells)},{"nets",std::to_string(source->counts().nets)},{"qualification",JsonQuote("phase1-index-plus-assignment-relation-v1")}})},
        {"canonical_platform_sha256",json.Get(config,"platform_sha256")},{"instance_assignment_sha256",json.Get(summary,"instance_assignment_sha256")},{"timing_source_sha256","null"}})}});
    auto fields=json.Fields(header);for(const auto* key:{"capture_requirements","cut_nodes","dependency_edges","logic_segments"})fields[key]="null";
    const std::map<std::string,unsigned char> tags{{"capture_requirements",1},{"cut_nodes",2},{"dependency_edges",3},{"logic_segments",4}};
    const auto hash_request=root/"semantic-hash.request",digest_path=root/"semantic.sha256";
    {Writer w(hash_request,inputs,false);w.Put(U(0x31484553464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(fields.size()));for(const auto& field:fields){w.Text(field.first);const auto tag=tags.find(field.first);w.Put(tag==tags.end()?std::uint8_t(0):tag->second);if(tag==tags.end()){std::string formatted;VisitPrettyCanonicalJson(field.second,1,[&](std::string_view chunk){formatted.append(chunk);});w.Text(formatted);}}w.Finish();}
    Action("phase3.semantic-hash",[&]{emuflow::native::HashStaticExactSemantics({ArtifactRef::Bind((root/"semantics/nodes.bin").string(),"exact-nodes-v2"),ArtifactRef::Bind((root/"semantics/captures.bin").string(),"exact-captures-v1"),ArtifactRef::Bind(hash_request.string(),"exact-hash-request-v1")},digest_path.string());});
    auto digest=Read(digest_path,65);Check(digest.size()==65 && digest.back()=='\n',"invalid semantic digest");digest.pop_back();return {header,digest};
  }
  void Publish(const fs::path& root,const Owner& selected,const std::string& audit,const std::string& decision,const std::string& initial_summary,const std::string& model_summary,const std::string& hops){
    const auto semantic=Semantics(root,selected);const auto semantic_summary=Read(root/"semantics/summary.json");
    const auto audit_loads=json.Values(json.Get(audit,"loads")),counts=json.Values(json.Get(audit,"cluster_counts"));std::vector<std::string> partitions;Raw resource_by_fpga;U used=0;
    const auto reserved=json.Get(constraints,"resource_reservations_by_fpga","{}");
    for(unsigned p=0;p<parts.size();++p){const auto loads=json.Values(audit_loads.at(p));Raw resources,utilization,planning;for(unsigned d=1;d<13;++d){const auto amount=json.UInt(loads.at(d));if(amount)resources[Field(d)]=std::to_string(amount);if(fpgas[p].physical[d])utilization[Field(d)]=Number(double(amount)/fpgas[p].physical[d]);}for(const auto& field:json.Fields(fpgas[p].capacity_json)){const auto d=1+unsigned(std::find(kResourceFields.begin(),kResourceFields.end(),field.first)-kResourceFields.begin());planning[field.first]=std::to_string(fpgas[p].planning[d]);}
      Raw row{{"fpga",JsonQuote(parts[p])},{"cluster_count",counts.at(p)},{"instance_count",loads.at(0)},{"resources",Object(resources)},{"effective_capacity",fpgas[p].capacity_json},{"utilization",Object(utilization)}};
      if(!json.Fields(reserved).empty()){row["planning_capacity"]=Object(planning);row["resource_reservation"]=json.Get(reserved,parts[p],"{}");}if(json.UInt(loads[0]))++used;partitions.push_back(Object(row));resource_by_fpga[parts[p]]=Object(resources);}
    Raw metrics=json.Fields(json.Get(semantic_summary,"cut_metrics"));metrics["instances"]=std::to_string(source->counts().cells);metrics["clusters"]=std::to_string(clusters->clusters());metrics["used_fpgas"]=std::to_string(used);
    auto validation=BalanceSummary(audit);for(const auto& field:metrics)validation[field.first]=field.second;validation["status"]=JsonQuote("pass");validation["illegal_cuts"]="0";validation["resources_by_fpga"]=Object(resource_by_fpga);validation["cut_mode"]=JsonQuote("static-exact-combinational");validation["qualification"]=JsonQuote("structural-partition-legality");auto semantic_validation=json.Fields(json.Get(semantic_summary,"metrics"));semantic_validation["status"]=JsonQuote("pass");validation["semantic_contract"]=Object(semantic_validation);
    std::string chosen_provider=active.size()==1?"single-active-fpga-capacity-selection-v1":provider=="patron" && selected.sources.size()==2?"patron-predictive-timing-flow-native-v14":"tritonpart-openroad-hypergraph-v1";
    Raw document{{"schema",JsonQuote("emuflow.partition-assignment/v1")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"provider",JsonQuote(chosen_provider)},{"seed",std::to_string(seed)},{"constraints",constraints},{"metrics",Object(metrics)},{"semantic_contract_sha256",JsonQuote(semantic.second)}};
    if(active.size()>1)document["provider_metadata"]=selected.sources.size()==1?json.Get(initial_summary,"provider_metadata"):Object({{"algorithm_version","14"},{"model_implementation_id",JsonQuote("native-primary-store-model-v2")},{"initial_provider",JsonQuote("tritonpart-openroad-hypergraph-v1")},{"initial_provider_metadata",json.Get(initial_summary,"provider_metadata")}});
    Raw report{{"schema",JsonQuote("emuflow.phase3-report/v1")},{"phase","3"},{"status",JsonQuote("pass")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"provider",JsonQuote(chosen_provider)},{"requested_provider",JsonQuote(provider)},{"seed",std::to_string(seed)},{"validation",Object(validation)},{"hop_refinement",hops},{"patron_initial_clusters_reused","false"},{"partitions",Array(partitions)},
      {"cut_mode",JsonQuote("static-exact-combinational")},{"static_exact_candidate_policy",JsonQuote(policy)},{"qualification",JsonQuote("structural-partition-legality")},
      {"stage_implementation_id",JsonQuote("native-phase3-v1")},{"configuration_implementation_id",JsonQuote("native-partition-configuration-v1")},{"configuration_pattern_policy",json.Get(json.Get(config,"partition_configuration"),"pattern_range_policy")},
      {"cluster_implementation_id",JsonQuote("native-primary-clusters-v1")},{"candidate_implementation_id",JsonQuote("native-candidate-ids-v1")},{"semantic_implementation_id",JsonQuote("native-selected-partition-semantics-v1")},
      {"artifacts",Object({{"clusters",JsonQuote("clusters.json")},{"constraints",JsonQuote("constraints.normalized.json")},{"assignment",JsonQuote("assignment.json")},{"assignment_index",JsonQuote("assignment.sqlite")},{"report",JsonQuote("phase3_report.json")},{"runtime_breakdown",JsonQuote("phase3-runtime.json")},{"semantic_contract",JsonQuote("assignment.json#/semantic_contract")},{"semantic_contract_data",JsonQuote("assignment.exact.bin")}})},
      {"artifact_storage",Object({{"clusters",JsonQuote("emuflow.phase3-clusters-storage/v3")},{"assignment",JsonQuote("emuflow.phase3-assignment-storage/v4")},{"assignment_index",JsonQuote("emuflow.phase3-assignment-sqlite/v4")},{"static_exact_data",JsonQuote("emuflow.phase3-exact-data/v2")},{"logical_schema",JsonQuote("unchanged-transparent-expansion")}})}};
    if(provider=="patron" && active.size()>1){report["patron_algorithm_version"]="14";report["patron_initialization"]=JsonQuote(refs.count("solution")?"caller-supplied-generalized-tritonpart-solution-v1":"native-generalized-tritonpart-net-cut-v5");report["algorithm_validation"]=Object({{"status",JsonQuote("pass")},{"scope",JsonQuote("production-source-native-independent-audit")},{"native_input_transport",JsonQuote("native-primary-store-model-v2")},{"decision_implementation_id",JsonQuote("native-partition-decision-v1")},{"candidate_selection",json.Get(decision,"selection")},{"model",model_summary}});report["patron_diagnostics"]=Object({{"storage",JsonQuote("not-persisted")},{"reason",JsonQuote("production-hot-path")},{"offline_qualification",JsonQuote("independent-current-model-validator")}});}
    if(active.size()==1)report["partition_decision"]=Object({{"executed","false"},{"reason",JsonQuote("one-active-assignment-target")},{"requested_provider",JsonQuote(provider)}});
    const auto catalog_request=root/"catalog.request";
    auto cluster_policy=json.Fields(json.Get(config,"cluster_policy"));if(policy=="assignment-derived-acyclic-v2")cluster_policy["candidate_selection_policy"]=JsonQuote(policy);
    const auto cluster_document=Object({{"schema",JsonQuote("emuflow.clusters/v1")},{"design",JsonQuote(design)},{"instances",std::to_string(source->counts().cells)},{"policy",Object(cluster_policy)}});
    {Writer w(catalog_request,inputs);w.Put(U(0x31514343464d5545ULL));w.Put(std::uint32_t(1));w.Generation(refs.at("source"));w.Generation(*primary);w.Text(cluster_document);w.Put(std::uint32_t(1));w.Put(std::uint32_t(0));w.Generation(*candidates);w.Put(U(fixed.size()));for(const auto& field:fixed){w.Put(std::uint32_t(field.first));w.Text(field.second);}w.Put(U(groups.size()));for(const auto& group:groups){w.Put(std::uint32_t(group.first));w.Put(std::uint32_t(group.second.size()));for(const auto& name:group.second)w.Text(name);}w.Finish();}
    const auto metadata=Object({{"schema",JsonQuote("emuflow.phase3-assignment-sqlite/v4")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"provider",JsonQuote(chosen_provider)},{"seed",std::to_string(seed)},{"source_instance_count",std::to_string(source->counts().cells)},{"constraints",constraints},{"semantic_contract_sha256",JsonQuote(semantic.second)},{"semantic_contract_schema",JsonQuote("emuflow.static-exact-combinational-cut/v3")},{"semantic_contract_header",semantic.first},{"semantic_contract_binary","null"}});
    const auto header=Object({{"index_metadata",metadata},{"document",Object(document)},{"partitions",Array(partitions)},{"report",Object(report)}});
    std::vector<ArtifactRef> sources{refs.at("source"),*primary,ArtifactRef::Bind((root/"semantics/nodes.bin").string(),"exact-nodes-v2"),ArtifactRef::Bind((root/"semantics/captures.bin").string(),"exact-captures-v1"),ArtifactRef::Bind((root/"semantics/counts.bin").string(),"partition-cut-counts-v1"),ArtifactRef::Bind(catalog_request.string(),"cluster-catalog-request-v1"),*candidates};sources.insert(sources.end(),selected.sources.begin(),selected.sources.end());
    const auto request=root/"publication.request";{Writer w(request,inputs);w.Put(U(0x31555050464d5545ULL));w.Put(std::uint32_t(1));w.Put(std::uint32_t(selected.sources.size()));for(const auto& source:sources)w.Generation(source);auto canonical=parts;std::sort(canonical.begin(),canonical.end());w.Catalog(canonical);w.Catalog(selected.parts);w.Text(header);w.Finish();}sources.push_back(ArtifactRef::Bind(request.string(),"partition-publication-request-v1"));
    AddResult(PublishPartition(sources,(root/"published").string()));
  }
};
}
StageResult RunPhase3(const ArtifactRef& request,const std::string& directory,const volatile std::sig_atomic_t* cancelled){
  // Stage owns mappings into workspace. Unwind those borrowers before removing
  // the files on every exit, including failed initialization and cancellation.
  std::unique_ptr<Workspace> workspace;
  std::unique_ptr<Stage> stage;const auto started=std::chrono::steady_clock::now();
  return RunStage("phase3.run",[&]{stage=std::make_unique<Stage>(request,cancelled);return stage->inputs;},[&](const auto& refs){
    const fs::path destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"stage destination must be absent");fs::create_directories(destination.parent_path());workspace=std::make_unique<Workspace>(destination.parent_path());const auto root=workspace->root;
    stage->Action("phase3.configuration",[&]{stage->Normalize();});stage->Cluster(root);
    std::string initial_summary,initial_hops,model_summary="{}";auto initial=stage->Initialize(root,initial_summary);
    initial=stage->Hops(root,initial,false,initial_hops);std::optional<Owner> candidate;
    if(stage->provider=="patron" && stage->active.size()>1)candidate=stage->Patron(root,initial,model_summary);
    const auto decision=stage->Audit(root,initial,candidate);unsigned selected=0;
    if(candidate && stage->json.Text(stage->json.Get(stage->json.Get(decision,"selection"),"selected"))=="candidate")selected=1;
    const auto owner=selected?*candidate:initial;const auto audits=stage->json.Values(stage->json.Get(decision,"audits"));Check(selected<audits.size(),"selected audit missing");
    std::string final_hops;stage->Hops(root,owner,true,final_hops);
    if(stage->json.Get(stage->json.Get(stage->config,"route_constraints"),"max_route_hops")!="null"){
      const auto audited=stage->json.Get(final_hops,"after");final_hops=Object({{"schema",JsonQuote("emuflow.hop-partition-refinement/v1")},{"status",JsonQuote("pass")},{"enabled","false"},{"algorithm",JsonQuote("patron-native-hop-contract-v1")},{"before",audited},{"after",audited},{"moves","[]"},{"move_count","0"}});
    }
    if(stage->provider=="tritonpart" && stage->json.Get(stage->json.Get(stage->config,"route_constraints"),"max_route_hops")!="null") {
      final_hops=Object({{"schema",JsonQuote("emuflow.hop-partition-refinement/v1")},{"status",JsonQuote("pass")},{"enabled","true"},{"algorithm",JsonQuote("topology-constrained-fm-v1")},{"before",stage->json.Get(initial_hops,"before")},{"after",stage->json.Get(initial_hops,"after")},{"move_count",stage->json.Get(initial_hops,"move_count")},{"native_metrics",stage->json.Get(initial_hops,"native_metrics")}});
    }
    stage->Publish(root,owner,audits[selected],decision,initial_summary,model_summary,final_hops);
    const auto runtime=Object({{"schema",JsonQuote("emuflow.phase3-runtime/v1")},{"status",JsonQuote("pass")},{"implementation_id",JsonQuote("native-phase3-v1")},{"steps",Array(stage->steps)},{"tools",Array(stage->tool_runs)},{"wall_seconds",Number(std::chrono::duration<double>(std::chrono::steady_clock::now()-started).count())},{"wall_scope",JsonQuote("stage through closed private publication; final directory commit measured by enclosing result")},{"rss_measurement",JsonQuote("per-operation-self-lifetime-high-water; external sampled process-tree measurement required")}});
    PublishTextArtifact((root/"published/phase3-runtime.json").string(),refs,[&](std::ostream& out){out<<runtime<<'\n';});stage->Unchanged();
    stage->clusters.reset();stage->source.reset();const auto ready=destination.parent_path()/(root.filename().string()+"-publish");fs::rename(root/"published",ready);
    try{PublishNewDirectory(ready.string(),destination.string(),refs);}catch(...){std::error_code error;fs::remove_all(ready,error);throw;}
  },[&]{std::vector<ArtifactRef> out;for(const auto& pair:std::vector<std::pair<std::string,std::string>>{{"phase3_report.json","phase3-report-v1"},{"assignment.sqlite","assignment-index-v4"},{"assignment.exact.bin","phase3-exact-data-v2"},{"assignment.json","assignment-selector-v4"},{"phase3-runtime.json","phase3-runtime-v1"}})out.push_back(ArtifactRef::Bind((fs::path(directory)/pair.first).string(),pair.second));return out;});
}
}
