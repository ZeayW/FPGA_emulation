#include "partition/partition_init.hpp"
#include "common/artifact_io.hpp"
#include "ir/emuir_view.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "partition/primary_cluster_view.hpp"
#include "ir/resource_view.hpp"
#include "storage/store.hpp"
#include "common/tool_runtime.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <locale>
#include <numeric>
#include <optional>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using U=std::uint64_t;using Object=std::map<std::string,JsonRecord>;using Array=std::vector<JsonRecord>;
using Load=std::array<U,13>;
void Check(bool valid,const std::string& text){if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"partition initializer "+text);}
std::string Identity(U n){auto text=std::to_string(n);return "c"+std::string(text.size()<6?6-text.size():0,'0')+text;}
std::string Number(double value,unsigned digits){Check(std::isfinite(value),"nonfinite text number");std::ostringstream out;out.imbue(std::locale::classic());out<<std::setprecision(digits)<<value;return out.str();}
std::string HashFile(const fs::path& path){std::ifstream input(path,std::ios::binary);Check(bool(input),"cannot open source seal");Sha256 hash;std::array<char,65536> bytes;while(input){input.read(bytes.data(),bytes.size());hash.update(reinterpret_cast<const unsigned char*>(bytes.data()),input.gcount());}Check(input.eof(),"cannot read source seal");const char* digits="0123456789abcdef";std::string result;for(auto byte:hash.finish()){result+=digits[byte>>4];result+=digits[byte&15];}return result;}
std::string ReadSmall(const fs::path& path){auto size=fs::file_size(path);Check(size<=512*1024,"selector exceeds bound");std::ifstream in(path,std::ios::binary);std::string result(size,'\0');in.read(result.data(),size);Check(bool(in),"cannot read selector");return result;}
std::string Field(SqliteDatabase& db,const std::string& doc,const std::string& path,const std::string& kind){auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,doc);row.BindText(2,path);Check(row.Step() && row.Text(0)==kind,"invalid selector field "+path);return row.Text(1);}
struct Workspace{fs::path root;explicit Workspace(const fs::path& parent){std::string text=(parent/".partition-initializer-XXXXXX").string();Check(mkdtemp(text.data())!=nullptr,"cannot create workspace");root=text;}~Workspace(){std::error_code error;fs::remove_all(root,error);}};
struct Config {
 std::vector<std::string> parts,classes;std::array<bool,13> balanced{};std::array<double,13> tolerance{};
 std::vector<Load> capacity,planning;std::map<std::uint32_t,std::uint32_t> fixed;
 double base_tolerance=0;std::string executable;U seed=0,timeout=0;unsigned attempts=1,initial=50,best=10,min_used=1;bool repair=true,baseline=false,weights=false,import=false;
};
std::vector<std::string> Catalog(CheckedRequestReader& r){auto count=r.Read<std::uint32_t>();Check(count>0 && count<=65536,"invalid catalog size");std::vector<std::string> out;std::set<std::string> unique;for(unsigned i=0;i<count;++i){auto value=r.ReadString32(false,65536);Check(value.find('\0')==std::string::npos && unique.insert(value).second,"invalid catalog identity");out.push_back(std::move(value));}return out;}
Config Read(const std::vector<ArtifactRef>& inputs){
 Check(inputs.size()>=3 && inputs.size()<=6,"input coverage disagrees");CheckedRequestReader r(inputs[2].path(),4*1024*1024);Config c;
 Check(r.Read<U>()==0x31544950464d5545ULL && r.Read<std::uint32_t>()==1,"unsupported request");auto flags=r.Read<std::uint32_t>();Check(flags<=3,"invalid request flags");c.weights=flags&1;c.import=flags&2;
 Check(inputs.size()==3+2*unsigned(c.weights)+unsigned(c.import),"optional source coverage disagrees");
 Check(inputs[0].schema()=="emuir-compact-v2" && inputs[1].schema()=="partition-cluster-columns-v2" && inputs[2].schema()=="partition-initialization-request-v1","source schemas disagree");
 if(c.weights)Check(inputs[3].schema()=="net-weight-selector-v2" && inputs[4].schema()=="net-weight-store-v1","weight source schemas disagree");
 if(c.import)Check(inputs.back().schema()=="tritonpart-solution-v1","solution schema disagrees");
 for(unsigned i=0;i<inputs.size();++i)if(i!=2)Check(ReadFileGeneration(r)==inputs[i].generation(),"original source generation changed");
 c.parts=Catalog(r);c.classes=Catalog(r);c.seed=r.Read<U>();c.timeout=r.Read<U>();c.attempts=r.Read<std::uint32_t>();c.initial=r.Read<std::uint32_t>();c.best=r.Read<std::uint32_t>();c.min_used=r.Read<std::uint32_t>();
 auto repair=r.Read<std::uint32_t>(),baseline=r.Read<std::uint32_t>();Check(repair<=1 && baseline<=1,"invalid policy flags");c.repair=repair;c.baseline=baseline;c.base_tolerance=r.Read<double>();Check(std::isfinite(c.base_tolerance) && c.base_tolerance>=0,"invalid base tolerance");c.executable=r.ReadString32(true,65536);
 Check(c.parts.size()>=2 && c.parts.size()<=65536 && c.attempts && c.attempts<=4096 && c.initial && c.best && c.best<=c.initial && c.min_used && c.min_used<=c.parts.size() && c.timeout && c.timeout<=365ULL*24*3600,"invalid effort/occupancy bounds");
 Check(c.seed<=INT32_MAX && c.attempts<=U(INT32_MAX)-c.seed+1 && (!c.import || c.attempts==1),"invalid seed sweep/import configuration");
 for(const auto& name:c.classes)Check(name=="register_output" || name=="register_input" || name=="combinational","invalid cut class");
 for(unsigned d=0;d<13;++d){auto flag=r.Read<std::uint8_t>();Check(flag<=1,"invalid balance flag");c.balanced[d]=flag;c.tolerance[d]=r.Read<double>();Check(std::isfinite(c.tolerance[d]) && c.tolerance[d]>=0,"invalid tolerance");}
 c.capacity.resize(c.parts.size());c.planning.resize(c.parts.size());
 for(unsigned p=0;p<c.parts.size();++p)for(unsigned d=0;d<13;++d){c.capacity[p][d]=r.Read<U>();c.planning[p][d]=r.Read<U>();Check(c.capacity[p][d]<=INT64_MAX && c.planning[p][d]<=c.capacity[p][d],"invalid planning capacity");}
 const auto count=r.Read<std::uint32_t>();Check(count<=65536,"fixed constraints exceed request bound");for(unsigned i=0;i<count;++i){auto vertex=r.Read<std::uint32_t>(),part=r.Read<std::uint32_t>();Check(part<c.parts.size() && c.fixed.emplace(vertex,part).second,"invalid fixed constraint");}
 r.RequireEnd();return c;
}
std::vector<Load> Bounds(const PrimaryClusterView& primary,const Config& c){
 const auto totals=primary.totals();std::vector<Load> result(c.parts.size());
 for(unsigned d=0;d<13;++d){U cap_total=0;for(const auto& row:c.capacity){Check(cap_total<=UINT64_MAX-row[d],"capacity sum overflow");cap_total+=row[d];}for(unsigned p=0;p<c.parts.size();++p){
  U allowed=d?c.planning[p][d]:totals[0];
  if(c.balanced[d]){double share=d?(cap_total?double(c.capacity[p][d])/cap_total:0):1.0/c.parts.size();double value=std::floor(double(totals[d])*share*(1.0+c.tolerance[d])+1e-9);Check(value>=0 && value<double(UINT64_MAX),"balance bound overflow");allowed=d?std::min<U>(U(value),allowed):U(value);}
  result[p][d]=allowed;
 }}return result;
}
Object LimitsJson(const Config& c,const std::vector<Load>& limits,const std::vector<unsigned>& dimensions){Object result;for(unsigned p=0;p<c.parts.size();++p){Object row;for(auto d:dimensions)row[d?kResourceFields[d-1]:"cells"]=limits[p][d];result[c.parts[p]]=std::move(row);}return result;}
void Necessary(const PrimaryClusterView& primary,const Config& c,const std::vector<Load>& limits){
 std::vector<Load> fixed(c.parts.size());
 for(const auto& item:c.fixed)Check(item.first<primary.clusters(),"fixed cluster is out of range");
 for(U vertex=0;vertex<primary.clusters();++vertex){bool legal=false;for(unsigned p=0;p<c.parts.size();++p){if(c.fixed.count(vertex) && c.fixed.at(vertex)!=p)continue;bool fit=true;for(unsigned d=0;d<13;++d)fit=fit && primary.Weight(vertex,d)<=limits[p][d];legal=legal||fit;}Check(legal,"strict balance/capacity infeasible: indivisible cluster "+Identity(vertex)+"; automatic relaxation is forbidden");
  if(c.fixed.count(vertex))for(unsigned d=0;d<13;++d)fixed[c.fixed.at(vertex)][d]+=primary.Weight(vertex,d);
 }
 for(unsigned d=0;d<13;++d){U total=0;for(unsigned p=0;p<c.parts.size();++p){Check(total<=UINT64_MAX-limits[p][d],"bound sum overflow");total+=limits[p][d];Check(fixed[p][d]<=limits[p][d],"strict balance/capacity infeasible: fixed load");}Check(primary.totals()[d]<=total,"strict balance/capacity infeasible: aggregate load");}
}
struct Hypergraph {std::vector<U> offsets{0};std::vector<std::uint32_t> vertices;std::vector<double> weights;std::vector<std::uint8_t> timed;U specified=0,timed_edges=0;double minimum=1,maximum=1;};
Hypergraph BuildGraph(EmuirView& source,PrimaryClusterView& primary,const Config& c,const std::vector<ArtifactRef>& inputs){
 auto& db=source.store().database();Hypergraph graph;
 if(c.weights){const auto selector=ReadSmall(inputs[3].path());auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,selector);Check(valid.Step() && valid.Integer(0) && valid.Text(1)=="object","invalid weight selector");
  auto duplicates=db.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicates.BindText(1,selector);Check(!duplicates.Step(),"duplicate weight selector key");
  Check(Field(db,selector,"$.schema","text")=="emuflow.partition-net-weights/v2","unsupported weight selector");
  auto relative=Field(db,selector,"$.payloads.weights.path","text");Check(fs::path(relative).filename()==relative && fs::canonical(fs::path(inputs[3].path()).parent_path()/relative)==inputs[4].path(),"weight payload path disagrees");
  auto digest=Field(db,selector,"$.payloads.weights.sha256","text");Check(digest.size()==64 && HashFile(inputs[4].path())==digest,"weight payload seal disagrees");
  SqliteStore weight_owner(inputs[4]);
  auto attach=db.Prepare("ATTACH DATABASE ? AS timing_weights");attach.BindText(1,SqliteImmutableUri(inputs[4].path()));attach.Execute();
  auto invalid=db.Prepare("SELECT 1 FROM timing_weights.net_weights WHERE net_id='' OR criticality IS NULL OR criticality<0 OR criticality>1 OR path_count<=0 OR weight IS NULL OR weight<=0 OR typeof(path_count)<>'integer' LIMIT 1");Check(!invalid.Step(),"invalid timing-weight record");
  auto records=db.Prepare("SELECT net_id,criticality,path_count,weight FROM timing_weights.net_weights ORDER BY net_id");U observed=0;
  while(records.Step()){Check(!records.TypedText(0).empty() && std::isfinite(records.TypedReal(1)) && records.Real(1)>=0 && records.Real(1)<=1 && records.TypedInteger(2)>0 && std::isfinite(records.TypedReal(3)) && records.Real(3)>0,"invalid timing-weight record");++observed;}
  auto declared=db.Prepare("SELECT json_type(?1,'$.payloads.weights.records'),json_extract(?1,'$.payloads.weights.records')");declared.BindText(1,selector);
  Check(declared.Step() && declared.Text(0)=="integer" && declared.TypedInteger(1)>=0 && U(declared.Integer(1))==observed && Field(db,selector,"$.payloads.weights.format","text")=="sqlite-net-weights/v1","weight payload count/format disagrees");
  auto unknown=db.Prepare("SELECT w.net_id FROM timing_weights.net_weights w LEFT JOIN net_records n ON n.id=w.net_id WHERE w.criticality>0 AND n.seq IS NULL LIMIT 1");Check(!unknown.Step(),"net weights reference unknown nets");
  auto count=db.Prepare("SELECT COUNT(*) FROM timing_weights.net_weights WHERE criticality>0");Check(count.Step(),"cannot read weight count");graph.specified=count.TypedInteger(0);
 }
 std::set<std::uint32_t> vertices;std::set<std::string> classes(c.classes.begin(),c.classes.end());
 const auto query=c.weights?"SELECT n.seq,n.cut_class,w.criticality,w.weight FROM net_records n LEFT JOIN timing_weights.net_weights w ON w.net_id=n.id ORDER BY n.seq":"SELECT seq,cut_class,NULL,NULL FROM net_records ORDER BY seq";
 auto nets=db.Prepare(query);bool have=nets.Step();Check(!have || nets.TypedInteger(0)==0,"invalid net sequence");
 source.VisitConnectivity([&](const EmuirEndpoint& endpoint){if(endpoint.cell)vertices.insert(primary.Owner(endpoint.cell->sequence()));},[&](EmuirNetId net){
  Check(have && nets.TypedInteger(0)>=0 && U(nets.Integer(0))==net.sequence(),"net/endpoint coverage disagrees");
  if(classes.count(nets.TypedText(1)) && vertices.size()>1){double weight=1;bool timed=!nets.IsNull(2) && nets.Real(2)>0;if(timed)weight=nets.Real(3);Check(std::isfinite(weight) && weight>0,"invalid timing weight");
   graph.weights.push_back(weight);graph.timed.push_back(timed);graph.vertices.insert(graph.vertices.end(),vertices.begin(),vertices.end());graph.offsets.push_back(graph.vertices.size());
   if(timed){if(!graph.timed_edges)graph.minimum=graph.maximum=weight;else{graph.minimum=std::min(graph.minimum,weight);graph.maximum=std::max(graph.maximum,weight);}++graph.timed_edges;}
  }vertices.clear();have=nets.Step();});
 Check(!have && !graph.weights.empty(),"hypergraph has no legal sequential-boundary hyperedges");return graph;
}
void Emit(const fs::path& path,const std::function<void(std::ostream&)>& produce,const std::vector<ArtifactRef>& inputs){PublishTextArtifact(path.string(),inputs,produce);}
void Export(const fs::path& root,const PrimaryClusterView& primary,const Config& c,const Hypergraph& graph,const std::vector<unsigned>& dimensions,const std::vector<Load>& limits,const std::vector<ArtifactRef>& inputs){
 const auto hgr=[&](const fs::path& path,bool weighted){Emit(path,[&](std::ostream& out){out<<graph.weights.size()<<' '<<primary.clusters()<<" 11\n";for(U e=0;e<graph.weights.size();++e){out<<(weighted?Number(graph.weights[e],9):"1");for(U i=graph.offsets[e];i<graph.offsets[e+1];++i)out<<' '<<U(graph.vertices[i])+1;out<<'\n';}for(U v=0;v<primary.clusters();++v){bool first=true;for(auto d:dimensions){if(!first)out<<' ';first=false;out<<primary.Weight(v,d);}out<<'\n';}},inputs);};
 hgr(root/"partition.hgr",true);if(c.baseline && graph.specified && !c.import)hgr(root/"partition.unweighted.hgr",false);
 Emit(root/"partition.fix",[&](std::ostream& out){for(U v=0;v<primary.clusters();++v){auto found=c.fixed.find(v);if(found==c.fixed.end())out<<-1;else out<<found->second;out<<'\n';}},inputs);
 Emit(root/"partition.resource-bounds",[&](std::ostream& out){out<<c.parts.size()<<' '<<dimensions.size()<<'\n';for(unsigned p=0;p<c.parts.size();++p){bool first=true;for(auto d:dimensions){if(!first)out<<' ';first=false;out<<"0 "<<limits[p][d];}out<<'\n';}},inputs);
}
void AuditExport(const fs::path& path,EmuirView& source,const PrimaryClusterView& primary,const Config& c,const std::vector<unsigned>& dimensions,bool weighted){
 std::ifstream input(path);input.imbue(std::locale::classic());U declared=0,vertices=0,format=0;Check(bool(input>>declared>>vertices>>format) && vertices==primary.clusters() && format==11,"invalid exported hypergraph header");std::string line;std::getline(input,line);Check(line.find_first_not_of(" \t\r")==std::string::npos,"extra hypergraph header fields");
 auto& db=source.store().database();auto endpoints=db.Prepare("SELECT net_seq,role,instance_seq FROM endpoint_records ORDER BY net_seq,role,ordinal");bool have=endpoints.Step();
 auto nets=db.Prepare(c.weights?"SELECT n.seq,n.cut_class,w.criticality,w.weight FROM net_records n LEFT JOIN timing_weights.net_weights w ON w.net_id=n.id ORDER BY n.seq":"SELECT seq,cut_class,NULL,NULL FROM net_records ORDER BY seq");
 U net_count=0,edges=0;std::set<std::string> classes(c.classes.begin(),c.classes.end());std::vector<std::uint32_t> owners;
 while(nets.Step()){Check(nets.TypedInteger(0)>=0 && U(nets.Integer(0))==net_count,"audit net order disagrees");owners.clear();
  while(have && endpoints.TypedInteger(0)==nets.Integer(0)){Check(endpoints.Integer(1)==0 || endpoints.Integer(1)==1,"audit endpoint role invalid");if(!endpoints.IsNull(2)){auto cell=endpoints.TypedInteger(2);Check(cell>=0 && U(cell)<primary.cells(),"audit endpoint owner invalid");owners.push_back(primary.Owner(cell));}have=endpoints.Step();}
  Check(!have || endpoints.TypedInteger(0)>nets.Integer(0),"audit has orphaned endpoints");++net_count;
  if(!classes.count(nets.TypedText(1)))continue;std::sort(owners.begin(),owners.end());owners.erase(std::unique(owners.begin(),owners.end()),owners.end());if(owners.size()<2)continue;
  Check(bool(std::getline(input,line)),"missing exported hyperedge");std::istringstream row(line);row.imbue(std::locale::classic());std::string coefficient;Check(bool(row>>coefficient),"missing exported weight");double weight=weighted && !nets.IsNull(2) && nets.Real(2)>0?nets.Real(3):1;Check(coefficient==Number(weight,9),"exported timing weight disagrees");
  for(auto owner:owners){U vertex=0;Check(bool(row>>vertex) && vertex==U(owner)+1,"exported incidence disagrees with source");}std::string extra;Check(!(row>>extra),"extra exported vertex");++edges;
 }
 Check(!have && net_count==source.counts().nets && edges==declared,"exported hyperedge coverage disagrees");
 for(U vertex=0;vertex<primary.clusters();++vertex){Check(bool(std::getline(input,line)),"missing exported vertex weights");std::istringstream row(line);for(auto d:dimensions){U value=0;Check(bool(row>>value) && value==primary.Weight(vertex,d),"exported resource weights disagree");}std::string extra;Check(!(row>>extra),"extra exported resource dimension");}
 Check(!std::getline(input,line),"extra exported hypergraph rows");
}
double Envelope(const PrimaryClusterView& primary,const Config& c,const std::vector<unsigned>& dimensions,const std::vector<Load>& limits){double margin=0,target=1.0/c.parts.size();for(auto d:dimensions){auto total=primary.totals()[d];if(!total)continue;std::vector<double> upper;for(const auto& row:limits)upper.push_back(double(std::min(total,row[d]))/total);for(unsigned p=0;p<c.parts.size();++p){double left=0,right=0;for(unsigned i=0;i<p;++i)left+=upper[i];for(unsigned i=p+1;i<c.parts.size();++i)right+=upper[i];double lower=std::max(0.0,1.0-left-right);margin=std::max({margin,upper[p]-target,target-lower});}}return std::ceil(std::max(0.0,margin)*100*1000000)/1000000;}
std::string Tcl(const fs::path& root,const fs::path& graph,const Config& c,U seed,unsigned dimensions,double envelope){
 const auto path=[](const fs::path& value){auto text=fs::absolute(value).string();Check(text.find_first_of("{}\n\r\\")==std::string::npos,"Tcl path contains unsupported delimiters");return '{'+text+'}';};
 const auto list=[](unsigned count,const std::string& value){std::string text="{ ";for(unsigned i=0;i<count;++i){if(i)text+=' ';text+=value;}return text+" }";};
 std::ostringstream out;out.imbue(std::locale::classic());out<<"triton_part_hypergraph \\\n  -hypergraph_file "<<path(graph)<<" \\\n  -fixed_file "<<path(root/"partition.fix")<<" \\\n  -resource_bounds_file "<<path(root/"partition.resource-bounds")<<" \\\n  -num_parts "<<c.parts.size()<<" \\\n  -balance_constraint "<<Number(envelope,9)<<" \\\n  -base_balance "<<list(c.parts.size(),Number(1.0/c.parts.size(),12))<<" \\\n  -scale_factor "<<list(c.parts.size(),"1.0")<<" \\\n  -seed "<<seed<<" \\\n  -num_initial_solutions "<<c.initial<<" \\\n  -num_vertices_threshold_ilp 1000 \\\n  -num_best_initial_solutions "<<c.best<<" \\\n  -vertex_dimension "<<dimensions<<" \\\n  -hyperedge_dimension 1 \\\n  -v_wt_factors "<<list(dimensions,"1.0")<<" \\\n  -e_wt_factors { 1.0 } \\\n  -min_num_vertices_each_part 1\nexit\n";return out.str();
}
std::vector<std::uint32_t> Parse(const fs::path& path,U vertices,unsigned parts){
 std::ifstream input(path);Check(bool(input),"TritonPart reported success without a solution file");input.imbue(std::locale::classic());std::string token;std::vector<std::uint32_t> result;result.reserve(vertices);
 while(input>>token){Check(result.size()<vertices,"solution vertex count mismatch");std::size_t at=0;bool negative=false;if(token[0]=='+' || token[0]=='-'){negative=token[0]=='-';++at;}Check(at<token.size(),"solution requires an integer");U value=0;bool previous=false;for(;at<token.size();++at){char ch=token[at];if(ch=='_'){Check(previous && at+1<token.size() && token[at+1]>='0' && token[at+1]<='9',"invalid integer separator");previous=false;continue;}Check(ch>='0' && ch<='9',"solution requires an integer");Check(value<=(UINT64_MAX-(ch-'0'))/10,"solution integer overflow");value=value*10+(ch-'0');previous=true;}Check((!negative || !value) && value<parts,"solution part is invalid");result.push_back(value);}
 Check(input.eof() && result.size()==vertices,"solution vertex count mismatch");return result;
}
std::vector<Load> Loads(const PrimaryClusterView& primary,const std::vector<std::uint32_t>& parts,unsigned count){std::vector<Load> result(count);for(U v=0;v<parts.size();++v){Check(parts[v]<count,"owner part out of range");for(unsigned d=0;d<13;++d)result[parts[v]][d]+=primary.Weight(v,d);}return result;}
void Validate(EmuirView& source,PrimaryClusterView& primary,const Config& c,const std::vector<Load>& limits,const std::vector<std::uint32_t>& parts){
 Check(parts.size()==primary.clusters(),"solution primary coverage disagrees");for(const auto& item:c.fixed)Check(parts[item.first]==item.second,"fixed cluster ownership disagrees");
 auto computed=Loads(primary,parts,c.parts.size());std::vector<Load> independent(c.parts.size());auto& db=source.store().database();ResourceView resources(db);auto cells=db.Prepare("SELECT seq,resources_seq FROM cell_records ORDER BY seq");U ordinal=0;
 while(cells.Step()){Check(cells.TypedInteger(0)>=0 && U(cells.Integer(0))==ordinal,"source cell order disagrees");auto part=parts.at(primary.Owner(ordinal++));const auto& resource=resources.Get(cells.TypedInteger(1));Check(!resource.exceeds_uint32,"source resource exceeds primary columns");++independent[part][0];for(unsigned d=1;d<13;++d)independent[part][d]+=resource.amounts[d-1];}
 Check(ordinal==primary.cells() && computed==independent,"independent source resource loads disagree");
 for(unsigned p=0;p<c.parts.size();++p)for(unsigned d=0;d<13;++d)Check(independent[p][d]<=limits[p][d],"multi_resource_balance: strict capacity/balance bound exceeded");
}
struct Score{U cuts=0,timed_cuts=0;double weight=0,timed_weight=0;};
Score Evaluate(const Hypergraph& graph,const std::vector<std::uint32_t>& parts){Score result;for(U e=0;e<graph.weights.size();++e){auto first=parts[graph.vertices[graph.offsets[e]]];bool cut=false;for(U i=graph.offsets[e]+1;i<graph.offsets[e+1];++i)cut=cut || parts[graph.vertices[i]]!=first;if(cut){++result.cuts;result.weight+=graph.weights[e];if(graph.timed[e]){++result.timed_cuts;result.timed_weight+=graph.weights[e];}}}return result;}
unsigned Used(const std::vector<std::uint32_t>& parts,unsigned count){std::vector<bool> used(count);for(auto p:parts)used[p]=true;return std::count(used.begin(),used.end(),true);}
Array Repair(const PrimaryClusterView& primary,const Config& c,const Hypergraph& graph,std::vector<std::uint32_t>& parts){
 Array moves;std::vector<U> counts(c.parts.size());for(auto p:parts)++counts[p];auto loads=Loads(primary,parts,c.parts.size());
 // Incidence is constructed only for an actual occupancy repair. No repair
 // can rescue a candidate that failed independent resource validation.
 std::vector<U> offsets(primary.clusters()+1);for(auto vertex:graph.vertices)++offsets[vertex+1];std::partial_sum(offsets.begin(),offsets.end(),offsets.begin());auto cursor=offsets;std::vector<U> incident(graph.vertices.size());for(U e=0;e<graph.weights.size();++e)for(U i=graph.offsets[e];i<graph.offsets[e+1];++i)incident[cursor[graph.vertices[i]]++]=e;
 std::vector<std::map<unsigned,U>> edge_counts(graph.weights.size());for(U e=0;e<graph.weights.size();++e)for(U i=graph.offsets[e];i<graph.offsets[e+1];++i)++edge_counts[e][parts[graph.vertices[i]]];
 while(Used(parts,c.parts.size())<c.min_used){auto target=std::find(counts.begin(),counts.end(),0)-counts.begin();std::optional<std::tuple<U,U,double,std::string,std::string>> best;U selected=0;double delta=0;
  for(U v=0;v<parts.size();++v){auto from=parts[v];if(counts[from]<=1 || c.fixed.count(v))continue;bool fit=true;U resource_sum=0;for(unsigned d=1;d<13;++d){resource_sum+=primary.Weight(v,d);fit=fit && loads[target][d]+primary.Weight(v,d)<=c.planning[target][d];}if(!fit)continue;double change=0;for(U i=offsets[v];i<offsets[v+1];++i){auto e=incident[i];const auto& row=edge_counts[e];auto before=row.size(),after=before-(row.at(from)==1)+(!row.count(target));change+=(int(after>1)-int(before>1))*graph.weights[e];}auto key=std::make_tuple(U(primary.Weight(v,0)),resource_sum,change,Identity(v),c.parts[from]);if(!best || key<*best){best=key;selected=v;delta=change;}}
  Check(bool(best),"min-used-FPGA repair found no legal movable cluster");auto from=parts[selected];parts[selected]=target;--counts[from];++counts[target];Object resources;
  for(unsigned d=0;d<13;++d){auto amount=primary.Weight(selected,d);loads[from][d]-=amount;loads[target][d]+=amount;if(d && amount)resources[kResourceFields[d-1]]=amount;}
  for(U i=offsets[selected];i<offsets[selected+1];++i){auto& row=edge_counts[incident[i]];if(!--row[from])row.erase(from);++row[target];}
  moves.emplace_back(Object{{"cluster",Identity(selected)},{"source",c.parts[from]},{"target",c.parts[target]},{"instances",primary.Weight(selected,0)},{"resources",std::move(resources)},{"estimated_cut_delta",delta}});
 }return moves;
}
}
StageResult InitializePartition(const std::vector<ArtifactRef>& inputs,const std::string& directory,const volatile std::sig_atomic_t* cancelled){
 std::optional<Config> configuration;
 return RunStage("phase3.initialize-tritonpart",[&]{for(const auto& input:inputs)input.RequireUnchanged();configuration=Read(inputs);auto sources=inputs;if(!configuration->import){auto engine=ArtifactRef::Bind(configuration->executable,"external-tool-executable-v1");configuration->executable=engine.path();sources.push_back(std::move(engine));}return sources;},[&](const auto& sources){
  auto& c=*configuration;const fs::path destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"output directory must be absent");fs::create_directories(destination.parent_path());Workspace workspace(destination.parent_path());const auto solver=workspace.root/"solver",selected_root=workspace.root/"selected";fs::create_directory(solver);fs::create_directory(selected_root);
  EmuirView source(sources[0]);PrimaryClusterView primary(sources[1],sources[0]);primary.ValidateMembers(source);Check(primary.clusters()>=c.parts.size(),"needs at least one atomic cluster per FPGA");
  const auto limits=Bounds(primary,c);Necessary(primary,c,limits);std::vector<unsigned> dimensions{0};for(unsigned d=1;d<13;++d)if(primary.totals()[d])dimensions.push_back(d);
  auto graph=BuildGraph(source,primary,c,sources);if(!c.import){Export(solver,primary,c,graph,dimensions,limits,sources);AuditExport(solver/"partition.hgr",source,primary,c,dimensions,true);if(c.baseline && graph.specified)AuditExport(solver/"partition.unweighted.hgr",source,primary,c,dimensions,false);}double envelope=Envelope(primary,c,dimensions,limits);
  Array attempts;std::vector<std::uint32_t> selected;Array selected_moves;std::optional<std::tuple<double,U,U>> objective;U selected_seed=c.seed;std::string selected_mode;Score selected_score;const auto total=c.attempts+unsigned(c.baseline && graph.specified && !c.import);
  for(unsigned attempt=0;attempt<total;++attempt){if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"partition initializer cancelled");const bool baseline=attempt>=c.attempts;const U seed=baseline?c.seed:c.seed+attempt;const std::string mode=baseline?"unweighted_baseline":"timing_weighted";std::optional<ToolResult> engine;auto graph_path=solver/(baseline?"partition.unweighted.hgr":"partition.hgr");auto solution=fs::path(graph_path.string()+".part."+std::to_string(c.parts.size()));
   if(c.import)solution=sources.back().path();else{Check(!c.executable.empty(),"OpenROAD executable is missing");Emit(solver/"run_tritonpart.tcl",[&](std::ostream& out){out<<Tcl(solver,graph_path,c,seed,dimensions.size(),envelope);},sources);fs::remove(solution);ToolRequest tool;tool.argv={c.executable,"-exit",(solver/"run_tritonpart.tcl").string()};tool.working_directory=solver.string();tool.timeout_ms=c.timeout*1000;tool.tail_bytes=65536;auto tool_sources=sources;for(const auto& path:{graph_path,solver/"partition.fix",solver/"partition.resource-bounds",solver/"run_tritonpart.tcl"})tool_sources.push_back(ArtifactRef::Bind(path.string(),"tritonpart-input-text-v1"));auto result=RunExternalTool(tool,cancelled,tool_sources);Check(result.operation.ok(),"OpenROAD/TritonPart failed: "+result.operation.message+"\n"+result.diagnostic_tail);engine=std::move(result);}
   auto candidate=Parse(solution,primary.clusters(),c.parts.size());const auto raw_used=Used(candidate,c.parts.size());Object record{{"mode",mode},{"seed",seed},{"raw_used_fpgas",raw_used},{"used_fpgas",raw_used},{"repair_moves",Array{}}};Array moves;
   if(engine)record["engine"]=Object{{"wall_seconds",engine->wall_seconds},{"user_cpu_seconds",engine->user_cpu_seconds},{"system_cpu_seconds",engine->system_cpu_seconds},{"maximum_process_rss_kib",engine->maximum_process_rss_kib},{"rss_scope","wait4-maximum-process-high-water"}};
   try{Validate(source,primary,c,limits,candidate);if(c.repair && raw_used<c.min_used){moves=Repair(primary,c,graph,candidate);Validate(source,primary,c,limits,candidate);}}
   catch(const StageError& error){record["accepted"]=false;record["rejection"]="multi_resource_balance";record["error"]=error.what();attempts.emplace_back(std::move(record));continue;}
   auto used=Used(candidate,c.parts.size());record["used_fpgas"]=used;record["repair_moves"]=moves;if(used<c.min_used){record["accepted"]=false;record["rejection"]="min_used_fpgas";attempts.emplace_back(std::move(record));continue;}
   auto score=Evaluate(graph,candidate);record["accepted"]=true;record["cut_hyperedges"]=score.cuts;record["cut_weight"]=score.weight;attempts.emplace_back(std::move(record));auto key=std::make_tuple(score.weight,score.cuts,seed);if(!objective || key<*objective){objective=key;selected=std::move(candidate);selected_seed=seed;selected_mode=mode;selected_moves=std::move(moves);selected_score=score;}
  }
  Check(bool(objective),"seed sweep produced no independently acceptable assignment");
  // The selected immutable column already passed the independent source
  // reduction in its attempt. Do not rescan the full source merely to publish.
  const U edge_count=graph.weights.size();
  std::vector<U>().swap(graph.offsets);std::vector<std::uint32_t>().swap(graph.vertices);
  std::vector<double>().swap(graph.weights);std::vector<std::uint8_t>().swap(graph.timed);
  // Same current PATRON initial-column contract: canonical cluster order and
  // sorted fixed-part coordinates. Later pressure modeling borrows this file.
  AtomicBinaryWriter output((selected_root/"ownership.bin").string(),sources);std::uint32_t crc=0;
  const auto put=[&](auto value){output.Write(value);UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
  put(U(0x315450494e415045ULL));put(std::uint32_t(1));put(primary.clusters());put(primary.cells());for(auto p:selected)put(p);put(U(c.fixed.size()));auto canonical=c.parts;std::sort(canonical.begin(),canonical.end());for(const auto& item:c.fixed){put(item.first);put(std::uint32_t(std::find(canonical.begin(),canonical.end(),c.parts[item.second])-canonical.begin()));}output.Write(crc);output.Commit();
  Array parts,fields;for(const auto& part:c.parts)parts.emplace_back(part);for(auto d:dimensions)fields.emplace_back(d?kResourceFields[d-1]:"cells");
  Object timing{{"specified_nets",graph.specified},{"legal_hyperedges",edge_count},{"timed_legal_hyperedges",graph.timed_edges},{"timed_legal_hyperedge_fraction",double(graph.timed_edges)/edge_count},{"minimum_timed_weight",graph.minimum},{"maximum_timed_weight",graph.maximum},{"timed_cut_hyperedges",selected_score.timed_cuts},{"timed_cut_weight",selected_score.timed_weight}};
  double effective_tolerance=0;for(auto d:dimensions)effective_tolerance=std::max(effective_tolerance,c.tolerance[d]);
  Object metadata{{"requested_balance_percent",c.base_tolerance*100},{"effective_balance_percent",effective_tolerance*100},{"mode",c.import?"import":"execute"},{"selected_attempt_mode",selected_mode},{"executable",c.import?JsonRecord():JsonRecord(c.executable)},{"input_schema","emuflow.tritonpart-input/v1"},{"vertex_dimensions",fields},{"hyperedges",edge_count},{"timing_weights",timing},{"tritonpart_ubfactor_percent_points",envelope},{"balance_auto_relaxed",false},{"seed_attempts",attempts},{"search_effort",Object{{"num_initial_solutions",c.initial},{"num_best_initial_solutions",c.best},{"num_vertices_threshold_ilp",1000}}},{"objective_encoding","net-cut-v1"},{"objective_components",Object{{"net_cut_hyperedges",edge_count}}},{"min_used_fpgas_repair",Object{{"enabled",c.repair},{"moves",selected_moves}}},{"resource_constraints","native-exact-bounds-v1"},{"artifacts",Object{{"retained",false}}},{"initializer_implementation_id","native-tritonpart-initialization-v1"}};
  Object summary{{"schema","emuflow.partition-initialization/v1"},{"status","pass"},{"implementation_id","native-tritonpart-initialization-v1"},{"clusters",primary.clusters()},{"instances",primary.cells()},{"parts",parts},{"selected_seed",selected_seed},{"provider_metadata",metadata},{"balance_allowed_loads",LimitsJson(c,limits,dimensions)},{"validation",Object{{"status","pass"},{"scope","complete-source-resource-loads-fixed-ownership-and-export-incidence"}}}};
  auto summary_text=JsonRecord(summary).EncodeUtf8();Check(summary_text.size()<=4*1024*1024,"summary exceeds bound");
  Emit(selected_root/"summary.json",[&](std::ostream& out){out<<summary_text<<'\n';},sources);
  const auto ready=destination.parent_path()/(workspace.root.filename().string()+"-publish");fs::rename(selected_root,ready);try{PublishNewDirectory(ready.string(),destination.string(),sources);}catch(...){std::error_code error;fs::remove_all(ready,error);throw;}
 },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"ownership.bin").string(),"patron-initial-columns-v1"),ArtifactRef::Bind((fs::path(directory)/"summary.json").string(),"partition-initialization-summary-v1")};});
}
}
