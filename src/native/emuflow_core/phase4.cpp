#include "emuflow_core/phase4.hpp"
#include "emuflow_core/phase4_route_schema.hpp"
#include "bank_allocation.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/assignment_cuts.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/route_seal.hpp"
#include "emuflow_core/store.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <numeric>
#include <queue>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using I=std::int64_t;
using Raw=std::map<std::string,std::string>;
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
constexpr const char* provider="timing-aware-global-candidate-v1";
constexpr const char* generators[]={"shortest-path-tree","delay-demand-balanced",
  "nearest-terminal-steiner","directed-metric-closure","shallow-light-tree",
  "adaptive-hop-tree","refined-final"};
void Check(bool value,const std::string& message) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"Phase 4: "+message);
}
void Stop(const volatile std::sig_atomic_t* cancelled) {
  if(cancelled && *cancelled)throw StageError(ErrorCode::kExecutionFailed,"Phase 4 cancelled");
}
Raw Fields(const std::string& text) {
  Raw value;VisitJsonObjectTokens(text,[&](const auto& key,const auto& token){
    Check(value.size()<16384 && value.emplace(key,token).second,"duplicate or excessive object fields");});
  return value;
}
std::string Get(const Raw& value,const std::string& name) {
  const auto found=value.find(name);Check(found!=value.end(),"missing "+name);return found->second;
}
std::string Text(const Raw& value,const std::string& name) {return DecodeJsonString(Get(value,name));}
I Integer(const std::string& token) {
  I value=0;const auto result=std::from_chars(token.data(),token.data()+token.size(),value);
  Check(result.ec==std::errc() && result.ptr==token.data()+token.size(),"invalid integer");return value;
}
I Int(const Raw& value,const std::string& name) {return Integer(Get(value,name));}
double Number(const std::string& token) {return ParseFiniteDecimal(token,"Phase 4 number");}
double Num(const Raw& value,const std::string& name) {return Number(Get(value,name));}
std::vector<std::string> Values(const std::string& text) {
  std::vector<std::string> out;VisitJsonArrayTokens(text,[&](const auto& token){out.push_back(token);});return out;
}
std::vector<std::string> Strings(const std::string& text) {
  auto values=Values(text);for(auto& value:values)value=DecodeJsonString(value);return values;
}
JsonRecord Record(const std::string& token,unsigned depth=0) {
  Check(depth<128 && !token.empty(),"invalid JSON record");
  if(token[0]=='{'){Object out;for(const auto& field:Fields(token))out.emplace(field.first,Record(field.second,depth+1));return out;}
  if(token[0]=='['){Array out;VisitJsonArrayTokens(token,[&](const auto& item){out.push_back(Record(item,depth+1));});return out;}
  if(token[0]=='"')return DecodeJsonString(token);
  if(token=="null")return JsonRecord();
  if(token=="true" || token=="false")return token=="true";
  if(token.find_first_of(".eE")!=std::string::npos)return Number(token);
  return Integer(token);
}
std::string Encode(const Raw& fields) {Object value;for(const auto& field:fields)value.emplace(field.first,Record(field.second));return JsonRecord(value).Encode();}
std::string Meta(SqliteDatabase& db,const std::string& key) {
  auto q=db.Prepare("SELECT value FROM metadata WHERE key=?");q.BindText(1,key);Check(q.Step(),"missing "+key);return q.TypedText(0);
}
std::string ReadSmall(const ArtifactRef& ref) {
  Check(ref.generation().bytes<=8*1024*1024,"control document exceeds bound");
  std::ifstream in(ref.path(),std::ios::binary);std::string value(ref.generation().bytes,'\0');in.read(value.data(),value.size());
  Check(bool(in),"truncated control document");(void)Record(value);return value;
}
std::string DigestFile(const fs::path& path) {
  std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot read artifact for sealing");Sha256 h;std::array<char,65536> block;
  while(in){in.read(block.data(),block.size());h.update(reinterpret_cast<const unsigned char*>(block.data()),in.gcount());}
  Check(in.eof(),"artifact read failed");std::string out;for(auto c:h.finish()){out+="0123456789abcdef"[c>>4];out+="0123456789abcdef"[c&15];}return out;
}
struct Workspace {
  fs::path root;
  explicit Workspace(const fs::path& parent){auto value=(parent/".phase4-stage-XXXXXX").string();Check(mkdtemp(value.data())!=nullptr,"cannot create stage workspace");root=value;}
  ~Workspace(){if(!root.empty()){std::error_code error;fs::remove_all(root,error);}}
};

// Only one path record is captured. The paths array is never decoded into a
// second document-sized JSON object, including for original-member evidence.
class PathStream {
 public:
  explicit PathStream(std::istream& input):input_(input){}
  template<class F> void Object(F visit){Expect('{');if(Peek()=='}'){input_.get();return;}
    for(;;){Check(Peek()=='"',"object key is not a string");const auto key=DecodeJsonString(Value());Expect(':');visit(key);
      const auto c=Peek();Check(c==',' || c=='}',"invalid object delimiter");input_.get();if(c=='}')break;}}
  template<class F> void Array(F visit){Expect('[');if(Peek()==']'){input_.get();return;}
    for(;;){visit(Value());const auto c=Peek();Check(c==',' || c==']',"invalid array delimiter");input_.get();if(c==']')break;}}
  std::string Value(){std::string out;Read(out,0);return out;}
  void Finish(){Check(Peek()==EOF && !input_.bad(),"trailing or unreadable path input");}
 private:
  int Peek(){while(input_ && std::string(" \t\r\n").find(char(input_.peek()))!=std::string::npos)input_.get();return input_.peek();}
  void Expect(char c){Check(Peek()==c,"unexpected JSON token");input_.get();}
  void Add(std::string& out,int c){Check(c!=EOF && out.size()<32*1024*1024,"truncated or excessive single path record");out+=char(c);}
  void Read(std::string& out,unsigned depth){Check(depth<128,"excessive path nesting");const auto first=Peek();Check(first!=EOF,"truncated path input");
    if(first=='"'){Add(out,input_.get());for(;;){const auto c=input_.get();Check(c>=32,"invalid JSON string");Add(out,c);if(c=='"')break;if(c=='\\')Add(out,input_.get());}return;}
    if(first=='{' || first=='['){Add(out,input_.get());const auto end=first=='{'?'}':']';if(Peek()==end){Add(out,input_.get());return;}
      for(;;){if(first=='{'){Check(Peek()=='"',"invalid JSON member");Read(out,depth+1);Expect(':');Add(out,':');}Read(out,depth+1);
        const auto c=Peek();Check(c==',' || c==end,"invalid JSON delimiter");Add(out,input_.get());if(c==end)break;}return;}
    std::string scalar;for(;;){const auto c=input_.peek();if(c==EOF || std::string(",]} \t\r\n").find(char(c))!=std::string::npos)break;Check(scalar.size()<128,"excessive JSON scalar");scalar+=char(input_.get());}
    Check(!scalar.empty(),"empty JSON scalar");(void)Record(scalar);out+=scalar;
  }
  std::istream& input_;
};

struct Arc {
  std::string link,from,to,capacity_key;
  int source=0,sink=0,link_index=0,domain=0,direction=-1,opposite=-1,banks=0,wires=0,latency=0,lanes=0;
  I capacity=0;double delay=0,beta=0;bool sll=false;
};
struct Demand {
  std::string id,net,source;std::vector<std::string> sinks,predecessors;
  int node=0,round=0;std::vector<int> terminals;Raw fields;
  double normalized_slack=1,predicted_delay=0;
};
struct Path {
  I source_seq=0;std::string id,clock;double period=0,slack=0,fixed=0,normalized=0;
  std::vector<int> demands;
};
struct Model {
  std::string design,platform,semantic_schema,semantic_hash;
  Raw constraints,normalization,compression;
  std::vector<std::string> nodes;
  std::vector<Arc> arcs;
  std::vector<Raw> capacities;
  std::vector<Demand> demands;
  std::vector<Path> paths;
  std::map<std::string,int> demand_index;
  std::map<int,Raw> default_direction_locks;
  int workers=1;bool timing_driven=true,exact=false;
};

void BuildBoard(Model& model,const Raw& board) {
  Check(Text(board,"schema")=="emuflow.boarddb/v1","unsupported board");
  model.platform=Text(Fields(Get(board,"platform")),"name");
  for(const auto& token:Values(Get(board,"fpgas")))model.nodes.push_back(Text(Fields(token),"id"));
  std::sort(model.nodes.begin(),model.nodes.end());
  Check(!model.nodes.empty() && model.nodes.size()<=4096 && std::adjacent_find(model.nodes.begin(),model.nodes.end())==model.nodes.end(),"invalid board node catalog");
  std::map<std::string,int> nodes;for(std::size_t i=0;i<model.nodes.size();++i)nodes.emplace(model.nodes[i],i);
  const auto& c=model.constraints;
  Check(Text(c,"schema")=="emuflow.system-route-constraints/v1","unsupported normalized constraints");
  const auto frame=Int(c,"frame_slots"),maximum=Int(c,"tdm_max_ratio"),minimum=Int(c,"tdm_min_ratio"),quantum=Int(c,"tdm_ratio_quantum");
  Check(frame>0 && maximum<=frame && minimum>0 && minimum<=maximum && quantum>0 && Int(c,"max_iterations")>0,"invalid route ratio/iteration domain");
  for(const auto* key:{"lambda_load","lambda_timing","lambda_history","lambda_tdm"})Check(Num(c,key)>=0,"negative route objective weight");
  Check(Int(c,"reroute_rounds")>=0,"negative reroute effort");
  for(const auto* flag:{"tree_edge_sum_tdm","hard_sll_capacity"})Check(Get(c,flag)=="true" || Get(c,flag)=="false","invalid route policy boolean");
  Check(Get(c,"max_route_hops")=="null" || Int(c,"max_route_hops")>0,"invalid route hop bound");
  const auto unavailable=Strings(Get(c,"unavailable_links")),sll=Strings(Get(c,"sll_links")),shared=Strings(Get(c,"shared_capacity_links"));
  const auto contains=[](const auto& values,const auto& name){return std::find(values.begin(),values.end(),name)!=values.end();};
  std::map<std::string,Raw> links;
  for(const auto& token:Values(Get(board,"links"))){const auto link=Fields(token);Check(links.emplace(Text(link,"id"),link).second,"duplicate board link");}
  for(const auto* list:{&unavailable,&sll,&shared})for(const auto& id:*list)Check(links.count(id),"constraint references unknown link");
  const auto overrides=Fields(Get(c,"link_delay_ns")),directed=Fields(Get(c,"directed_link_delay_ns"));
  std::map<std::string,Raw> capacities;int link_index=0,direction=0;
  for(const auto& entry:links){const auto& l=entry.second;const auto id=entry.first;const auto ends=Strings(Get(l,"endpoints"));
    Check(ends.size()==2 && ends[0]!=ends[1] && nodes.count(ends[0]) && nodes.count(ends[1]),"invalid link endpoints");
    const auto kind=Text(l,"direction");Check(kind=="full_duplex" || kind=="half_duplex" || kind=="unidirectional","invalid link direction");
    const int group=kind=="half_duplex"?direction++:-1;
    if(group>=0)model.default_direction_locks.emplace(group,Raw{{"group",std::to_string(group)},{"link",JsonQuote(id)},{"from",JsonQuote(ends[0])},{"to",JsonQuote(ends[1])}});
    if(contains(unavailable,id)){++link_index;continue;}
    const auto payload=l.count("payload_bits_per_lane_per_cycle")?Int(l,"payload_bits_per_lane_per_cycle"):1;
    const auto lanes=Int(l,"data_lanes_per_direction");
    Check(lanes>0 && payload>0 && lanes<=std::numeric_limits<int>::max()/payload,"invalid link payload width");
    const auto latency=Int(l,"latency_cycles");const auto frequency=Num(l,"fabric_clock_mhz");Check(latency>=0 && frequency>0,"invalid link clock/latency");
    const int banks=l.count("bank_count") && Get(l,"bank_count")!="null"?int(Int(l,"bank_count")):0;
    const int wires=banks?int(Int(l,"wires_per_bank")):0;Check(!banks || (banks>0 && wires>0 && banks*wires==lanes),"invalid shared-bank inventory");
    const bool is_sll=contains(sll,id),has_ratio=l.count("tdm_delay_model") && Get(l,"tdm_delay_model")!="null";
    Check(!(banks && is_sll),"banked link cannot be SLL");
    I slots=is_sll?1:has_ratio?maximum:frame;
    if(model.exact && !is_sll){const auto usable=frame-1-latency;Check(usable>0,"link cannot arrive before commit");slots=std::min(maximum,usable>=quantum?(usable/quantum)*quantum:minimum==1?I(1):I(0));Check(slots>=minimum,"no discrete ratio arrives before commit");}
    double base=latency*1000.0/frequency,beta=1000.0/frequency;
    if(has_ratio){const auto ratio=Fields(Get(l,"tdm_delay_model"));Check(Text(ratio,"model")=="linear-ratio-v1","unsupported delay model");
      Check(minimum>=Int(ratio,"min_ratio") && maximum<=Int(ratio,"max_ratio") && quantum==Int(ratio,"ratio_quantum"),"route domain contradicts board delay model");base=Num(ratio,"intercept_ns")+Num(ratio,"ratio_coefficient_ns");beta=Num(ratio,"ratio_coefficient_ns");}
    for(int d=0;d<(kind=="unidirectional"?1:2);++d){Arc a;a.link=id;a.from=ends[d];a.to=ends[1-d];a.source=nodes.at(a.from);a.sink=nodes.at(a.to);a.link_index=link_index;a.direction=group;
      a.latency=int(latency);a.lanes=int(lanes*payload);a.capacity=a.lanes*slots;a.banks=banks;a.wires=wires;a.sll=is_sll;a.delay=base;a.beta=beta;
      if(overrides.count(id)){Check(!has_ratio || std::abs(Number(overrides.at(id))-base)<=1e-9,"delay override contradicts board model");if(!has_ratio)a.delay=Number(overrides.at(id));}
      if(directed.count(id)){const auto sources=Fields(directed.at(id));if(sources.count(a.from)){const auto targets=Fields(sources.at(a.from));if(targets.count(a.to)){const auto value=Number(targets.at(a.to));Check(!has_ratio || std::abs(value-base)<=1e-9,"directed delay contradicts board model");if(!has_ratio)a.delay=value;}}}
      Check(a.delay>=0 && a.beta>=0,"negative link delay");
      const bool share=!banks && (kind=="half_duplex" || contains(shared,id));a.capacity_key=id+(share?":shared":":"+a.from+"->"+a.to);
      Raw capacity{{"key",JsonQuote(a.capacity_key)},{"link",JsonQuote(id)},{"direction",JsonQuote(share?"shared":a.from+"->"+a.to)},{"capacity_bits",std::to_string(a.capacity)}};
      if(banks){capacity["bank_group"]=JsonQuote(id);capacity["bank_count"]=std::to_string(banks);capacity["wires_per_bank"]=std::to_string(wires);capacity["opposite_capacity_key"]=JsonQuote(id+":"+a.to+"->"+a.from);}
      capacities.emplace(a.capacity_key,std::move(capacity));model.arcs.push_back(std::move(a));
    }++link_index;
  }
  std::map<std::string,int> domains;for(const auto& c:capacities){domains.emplace(c.first,int(model.capacities.size()));model.capacities.push_back(c.second);}
  std::sort(model.arcs.begin(),model.arcs.end(),[](const auto& a,const auto& b){return std::tie(a.link,a.from,a.to)<std::tie(b.link,b.from,b.to);});
  for(auto& a:model.arcs){a.domain=domains.at(a.capacity_key);for(std::size_t i=0;i<model.arcs.size();++i)if(model.arcs[i].link==a.link && model.arcs[i].from==a.to && model.arcs[i].to==a.from)a.opposite=int(i);}
}

void BuildDemands(Model& model,const ArtifactRef& artifact) {
  AssignmentView view(artifact);(void)view.ValidateCuts();SqliteStore store(artifact);auto& db=store.database();
  model.design=DecodeJsonString(Meta(db,"design"));Check(DecodeJsonString(Meta(db,"platform"))==model.platform,"assignment/board namespace disagrees");
  std::map<I,std::string> parts;auto part=db.Prepare("SELECT part_index,fpga FROM parts ORDER BY part_index");
  while(part.Step()){const auto name=part.TypedText(1);Check(std::find(model.nodes.begin(),model.nodes.end(),name)!=model.nodes.end(),"assignment has unknown FPGA");parts.emplace(part.TypedInteger(0),name);}
  auto rows=db.Prepare("SELECT cut_index,net_id,source_part_index,transport_round,cut_class,dependency_level,combinational_dependency_depth FROM cuts ORDER BY cut_index");
  auto sinks=db.Prepare("SELECT net_id,sink_part_index FROM cut_sinks ORDER BY net_id,sink_part_index");bool have_sink=sinks.Step();
  auto predecessors=db.Prepare("SELECT cut_index,predecessor_net_id FROM cut_predecessors ORDER BY cut_index,ordinal");bool have_predecessor=predecessors.Step();
  std::vector<std::vector<double>> distances(model.nodes.size(),std::vector<double>(model.nodes.size(),std::numeric_limits<double>::infinity()));
  std::vector<std::vector<int>> adjacency(model.nodes.size());
  for(std::size_t a=0;a<model.arcs.size();++a)adjacency[model.arcs[a].source].push_back(int(a));
  // Preserve the previous source-rooted shortest-distance computation and
  // its lexical node tie break, including floating-point addition order.
  for(std::size_t source=0;source<distances.size();++source){
    auto& distance=distances[source];distance[source]=0;
    using Entry=std::pair<double,int>;
    std::priority_queue<Entry,std::vector<Entry>,std::greater<Entry>> queue;
    queue.emplace(0,int(source));
    while(!queue.empty()){
      const auto [current,node]=queue.top();queue.pop();if(current!=distance[node])continue;
      for(const auto index:adjacency[node]){const auto& a=model.arcs[index];const auto candidate=current+a.delay;
        if(candidate<distance[a.sink]){distance[a.sink]=candidate;queue.emplace(candidate,a.sink);}}
    }
  }
  while(rows.Step()){Demand d;const auto index=rows.TypedInteger(0);Check(index==I(model.demands.size()) && parts.count(rows.TypedInteger(2)),"invalid cut ordinal/source");
    std::ostringstream id;id<<'d'<<std::setw(6)<<std::setfill('0')<<index;d.id=id.str();d.net=rows.TypedText(1);d.source=parts.at(rows.TypedInteger(2));d.node=int(std::find(model.nodes.begin(),model.nodes.end(),d.source)-model.nodes.begin());
    while(have_sink && sinks.TypedText(0)==d.net){Check(parts.count(sinks.TypedInteger(1)),"unknown cut sink");d.sinks.push_back(parts.at(sinks.TypedInteger(1)));have_sink=sinks.Step();}
    std::sort(d.sinks.begin(),d.sinks.end());Check(!d.sinks.empty(),"cut has no sink");
    for(const auto& sink:d.sinks){const auto n=int(std::find(model.nodes.begin(),model.nodes.end(),sink)-model.nodes.begin());Check(n!=d.node && n<int(model.nodes.size()) && std::isfinite(distances[d.node][n]),"unreachable/nonremote cut sink");d.terminals.push_back(n);d.predicted_delay=std::max(d.predicted_delay,distances[d.node][n]);}
    d.fields={{"id",JsonQuote(d.id)},{"net",JsonQuote(d.net)},{"source",JsonQuote(d.source)},{"width_bits","1"}};
    Array encoded_sinks;for(const auto& sink:d.sinks)encoded_sinks.emplace_back(sink);d.fields["sinks"]=JsonRecord(encoded_sinks).Encode();
    if(!rows.IsNull(3)){d.round=int(rows.TypedInteger(3));d.fields["transport_round"]=std::to_string(d.round);}
    while(have_predecessor && predecessors.TypedInteger(0)==index){d.predecessors.push_back(predecessors.TypedText(1));have_predecessor=predecessors.Step();}
    if(model.exact){Check(!rows.IsNull(5) && !rows.IsNull(6),"missing exact dependency coordinates");d.fields["cut_class"]=JsonQuote(rows.TypedText(4));d.fields["dependency_level"]=std::to_string(rows.TypedInteger(5));d.fields["combinational_dependency_depth"]=std::to_string(rows.TypedInteger(6));Array pred;for(const auto& name:d.predecessors)pred.emplace_back(name);d.fields["predecessor_cut_nets"]=JsonRecord(pred).Encode();}
    Check(model.demand_index.emplace(d.net,int(model.demands.size())).second,"duplicate cut net");model.demands.push_back(std::move(d));
  }
  Check(!have_sink && !have_predecessor,"orphan cut relation");
  std::set<int> rounds;for(const auto& d:model.demands)rounds.insert(d.round);
  Check(model.exact || rounds!=std::set<int>{0,1},"registered round-barrier routing is retired; use Static Exact");
}

struct PathKey {
  std::string clock;double period=0,required=0;
  std::vector<std::string> signature,nets;
  std::vector<std::tuple<std::string,std::string,std::string>> transitions;
  bool operator<(const PathKey& b)const{return std::tie(clock,period,required,signature,nets,transitions)<std::tie(b.clock,b.period,b.required,b.signature,b.nets,b.transitions);}
};
struct PathGroup {I representative=0;double fixed=0,normalized=0;std::string id;std::vector<std::string> members;};
double Normalized(const Model& m,double slack,double period) {
  return slack>=0?slack*period/(Num(m.normalization,"positive_slack_scale_ns")*Num(m.normalization,"max_clock_period_ns")):
      slack/(Num(m.normalization,"negative_slack_scale_ns")*period);
}
Raw PathFields(const std::string& raw,const Model& model,bool& explicit_signature) {
  (void)Record(raw);const auto source=Fields(raw);Raw out;
  explicit_signature=source.count("cut_signature") && source.at("cut_signature")!="null";
  for(const auto* key:{"id","clock_domain"}){const auto value=Text(source,key);Check(!value.empty(),"empty timing identity/domain");out[key]=JsonQuote(value);}
  for(const auto* key:{"clock_period_ns","slack_ns","fixed_delay_ns"})out[key]=JsonRecord(Num(source,key)).Encode();
  Check(Num(out,"clock_period_ns")>0 && Num(out,"fixed_delay_ns")>=0,"invalid path period/fixed delay");
  const auto nets=Strings(Get(source,"cut_nets"));Check(!nets.empty() && std::set<std::string>(nets.begin(),nets.end()).size()==nets.size(),"empty or repeated path cuts");
  Array signature;
  for(const auto& net:nets){const auto found=model.demand_index.find(net);Check(found!=model.demand_index.end(),"path references unknown cut");const auto& d=model.demands[found->second];
    std::string name=d.source+"->";for(const auto& sink:d.sinks){if(name.back()!='>')name+=',';name+=sink;}signature.emplace_back(name);}
  out["cut_nets"]=JsonRecord([&]{Array a;for(const auto& net:nets)a.emplace_back(net);return a;}()).Encode();
  out["cut_signature"]=explicit_signature?JsonRecord([&]{Array a;for(const auto& text:Strings(source.at("cut_signature"))){Check(!text.empty(),"empty cut signature");a.emplace_back(text);}Check(!a.empty(),"empty cut signature");return a;}()).Encode():JsonRecord(signature).Encode();
  if(source.count("cut_transitions") && source.at("cut_transitions")!="null"){
    const auto values=Values(source.at("cut_transitions"));Check(values.size()==nets.size(),"cut transition coverage disagrees");Array transitions;std::string last;
    for(std::size_t i=0;i<values.size();++i){const auto t=Fields(values[i]);const auto& d=model.demands[model.demand_index.at(nets[i])];const auto from=Text(t,"from"),to=Text(t,"to");
      Check(t.size()==3 && Text(t,"net")==nets[i] && from==d.source && std::find(d.sinks.begin(),d.sinks.end(),to)!=d.sinks.end() && (!i || last==from),"invalid/discontinuous cut transition");last=to;transitions.push_back(Record(Encode(t)));}
    out["cut_transitions"]=JsonRecord(transitions).Encode();
  }
  if(source.count("normalized_slack"))out["normalized_slack"]=JsonRecord(Num(source,"normalized_slack")).Encode();
  if(source.count("compressed_path_ids")){Array members;for(const auto& id:Strings(source.at("compressed_path_ids")))members.emplace_back(id);Check(!members.empty(),"empty path members");out["compressed_path_ids"]=JsonRecord(members).Encode();}
  return out;
}
void LoadPaths(Model& model,const ArtifactRef* input,SqliteDatabase& db,const volatile std::sig_atomic_t* cancelled) {
  db.Execute("CREATE TABLE normalized_paths(seq INTEGER PRIMARY KEY,record_json TEXT NOT NULL)");
  model.normalization={{"positive_slack_scale_ns","1.0"},{"negative_slack_scale_ns","1.0"},{"max_clock_period_ns","1.0"}};
  if(!input){Check(!model.timing_driven || model.demands.empty(),"timing-driven routing requires timing paths");return;}
  db.Execute("CREATE TABLE raw_paths(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,record_json TEXT NOT NULL,explicit_signature INTEGER NOT NULL)");
  auto insert=db.Prepare("INSERT INTO raw_paths VALUES(?,?,?,?)");I count=0;Raw header;bool paths_seen=false;
  std::ifstream stream(input->path(),std::ios::binary);Check(bool(stream),"cannot open timing paths");PathStream parser(stream);
  parser.Object([&](const auto& key){Stop(cancelled);if(key=="paths"){Check(!paths_seen,"duplicate paths array");paths_seen=true;parser.Array([&](const auto& raw){Stop(cancelled);bool explicit_signature=false;const auto row=PathFields(raw,model,explicit_signature);insert.BindInteger(1,count++);insert.BindText(2,Text(row,"id"));insert.BindText(3,Encode(row));insert.BindInteger(4,explicit_signature);insert.Execute();insert.Reset();});}
    else{const auto token=parser.Value();Check(token.size()<=8*1024*1024 && header.emplace(key,token).second,"duplicate/excessive timing metadata");}});
  parser.Finish();Check(paths_seen && count>0 && Text(header,"schema")=="emuflow.sta-paths/v1","invalid timing document");
  if(header.count("design") && header.at("design")!="null")Check(Text(header,"design")==model.design,"timing/assignment namespace disagrees");
  const bool normalized=header.count("normalization") || header.count("compression");
  if(normalized){model.normalization=Fields(Get(header,"normalization"));model.compression=Fields(Get(header,"compression"));
    Check(model.normalization.size()==3 && Num(model.normalization,"positive_slack_scale_ns")>0 && Num(model.normalization,"negative_slack_scale_ns")>0 && Num(model.normalization,"max_clock_period_ns")>0,"invalid timing normalization");
    Check(Int(model.compression,"compressed_paths")==count && Int(model.compression,"original_paths")>=count,"invalid timing compression counts");
  }else{
    auto scales=db.Prepare("SELECT max(CASE WHEN json_extract(record_json,'$.slack_ns')>=0 THEN json_extract(record_json,'$.slack_ns') END),min(CASE WHEN json_extract(record_json,'$.slack_ns')<0 THEN json_extract(record_json,'$.slack_ns') END),max(json_extract(record_json,'$.clock_period_ns')) FROM raw_paths");
    Check(scales.Step(),"missing path normalization population");const auto positive=scales.IsNull(0)||scales.Real(0)==0?1.0:scales.Real(0),negative=scales.IsNull(1)?1.0:-scales.Real(1);
    model.normalization={{"positive_slack_scale_ns",JsonRecord(positive).Encode()},{"negative_slack_scale_ns",JsonRecord(negative).Encode()},{"max_clock_period_ns",JsonRecord(scales.Real(2)).Encode()}};
  }
  std::map<PathKey,PathGroup> groups;
  auto rows=db.Prepare("SELECT seq,record_json,explicit_signature FROM raw_paths ORDER BY seq");
  auto output=db.Prepare("INSERT INTO normalized_paths VALUES(?,?)");
  const auto append=[&](I source,Raw row){Path path;path.source_seq=source;path.id=Text(row,"id");path.clock=Text(row,"clock_domain");path.period=Num(row,"clock_period_ns");path.slack=Num(row,"slack_ns");path.fixed=Num(row,"fixed_delay_ns");path.normalized=Normalized(model,path.slack,path.period);
    if(normalized)Check(row.count("compressed_path_ids") && std::abs(Num(row,"normalized_slack")-path.normalized)<=1e-12,"normalized path evidence disagrees");
    row["normalized_slack"]=JsonRecord(path.normalized).Encode();
    for(const auto& net:Strings(Get(row,"cut_nets"))){const auto demand=model.demand_index.at(net);path.demands.push_back(demand);if(model.timing_driven)model.demands[demand].normalized_slack=std::min(model.demands[demand].normalized_slack,path.normalized);}
    output.BindInteger(1,model.paths.size());output.BindText(2,Encode(row));output.Execute();output.Reset();model.paths.push_back(std::move(path));};
  // Criticality starts at the minimum related path, including positive values
  // above one. The default one applies only to an unrelated demand.
  if(model.timing_driven)for(auto& demand:model.demands)demand.normalized_slack=std::numeric_limits<double>::infinity();
  while(rows.Step()){Stop(cancelled);auto row=Fields(rows.TypedText(1));if(normalized){Check(rows.TypedInteger(2)==1,"normalized path signature must be explicit");append(rows.TypedInteger(0),std::move(row));continue;}
    const auto slack=Num(row,"slack_ns"),period=Num(row,"clock_period_ns"),fixed=Num(row,"fixed_delay_ns"),ns=Normalized(model,slack,period);const auto id=Text(row,"id");
    PathKey key;key.clock=Text(row,"clock_domain");key.period=period;key.required=fixed+slack;key.signature=Strings(Get(row,"cut_signature"));key.nets=Strings(Get(row,"cut_nets"));
    if(row.count("cut_transitions"))for(const auto& token:Values(row.at("cut_transitions"))){const auto t=Fields(token);key.transitions.emplace_back(Text(t,"net"),Text(t,"from"),Text(t,"to"));}
    auto [found,created]=groups.emplace(key,PathGroup{rows.TypedInteger(0),fixed,ns,id,{}});auto& group=found->second;group.members.push_back(id);
    if(!created && std::make_tuple(fixed,-ns,id)>std::make_tuple(group.fixed,-group.normalized,group.id)){group.representative=rows.TypedInteger(0);group.fixed=fixed;group.normalized=ns;group.id=id;}
  }
  if(!normalized){auto representative=db.Prepare("SELECT record_json FROM raw_paths WHERE seq=?");
    for(auto& entry:groups){auto& g=entry.second;std::sort(g.members.begin(),g.members.end());representative.BindInteger(1,g.representative);Check(representative.Step(),"missing representative");auto row=Fields(representative.TypedText(0));representative.Reset();Array members;for(const auto& member:g.members)members.emplace_back(member);row["compressed_path_ids"]=JsonRecord(members).Encode();append(g.representative,std::move(row));}
    model.compression={{"original_paths",std::to_string(count)},{"compressed_paths",std::to_string(model.paths.size())},{"lossless_by",JsonQuote("clock-domain+period+required-time+ordered-cut-signature+cut-net-sequence+member-sink-transitions/max-fixed-delay")}};
  }
  for(auto& d:model.demands)if(!std::isfinite(d.normalized_slack))d.normalized_slack=1;
  input->RequireUnchanged();
}

void WriteRouterInput(const Model& m,const fs::path& path,const std::vector<ArtifactRef>& sources) {
  PublishTextArtifact(path.string(),sources,[&](std::ostream& out){out.imbue(std::locale::classic());out<<std::setprecision(17)<<"EMUFLOW_GLOBAL_ROUTE_INPUT_V1\nPARAM "<<m.nodes.size()<<' '<<Int(m.constraints,"max_iterations")<<' '<<Int(m.constraints,"reroute_rounds");
    for(const auto* key:{"lambda_load","lambda_timing","lambda_history","lambda_tdm"})out<<' '<<Num(m.constraints,key);
    out<<' '<<Int(m.constraints,"tdm_ratio_quantum")<<' '<<Int(m.constraints,"tdm_max_ratio");
    for(const auto* key:{"positive_slack_scale_ns","negative_slack_scale_ns","max_clock_period_ns"})out<<' '<<(m.timing_driven?Num(m.normalization,key):1.0);
    out<<' '<<(Get(m.constraints,"tree_edge_sum_tdm")=="true")<<' '<<Int(m.constraints,"tdm_min_ratio")<<' '<<(Get(m.constraints,"hard_sll_capacity")=="true")<<' '<<(Get(m.constraints,"max_route_hops")=="null"?0:Int(m.constraints,"max_route_hops"))<<' '<<m.workers<<'\n';
    for(std::size_t i=0;i<m.arcs.size();++i){const auto& a=m.arcs[i];out<<"ARC "<<i<<' '<<a.link_index<<' '<<a.source<<' '<<a.sink<<' '<<a.domain<<' '<<a.direction<<' '<<a.opposite<<' '<<a.capacity<<' '<<a.lanes<<' '<<a.delay<<' '<<a.beta<<' '<<a.sll<<' '<<a.banks<<' '<<a.wires<<'\n';}
    for(std::size_t i=0;i<m.demands.size();++i){const auto& d=m.demands[i];out<<"DEMAND "<<i<<' '<<d.node<<' ';for(std::size_t j=0;j<d.terminals.size();++j)out<<(j?",":"")<<d.terminals[j];out<<" 1 "<<(m.timing_driven?d.normalized_slack:1.0)<<' '<<d.predicted_delay<<'\n';}
    if(m.timing_driven)for(std::size_t i=0;i<m.paths.size();++i){const auto& p=m.paths[i];out<<"PATH "<<i<<' '<<p.period<<' '<<p.slack<<' '<<p.fixed<<' ';for(std::size_t j=0;j<p.demands.size();++j)out<<(j?",":"")<<p.demands[j];out<<'\n';}
  });
}

struct TreeResult {std::vector<int> arcs;double delay=0,tdm_delay=0;int latency=0,hops=0;};
TreeResult ValidateTree(const Model& m,std::size_t demand,const std::vector<int>& edges,
                        const std::map<int,int>& locks,const double* supplied) {
  Check(demand<m.demands.size(),"unknown routed demand");const auto& d=m.demands[demand];
  std::vector<std::vector<int>> adjacency(m.nodes.size());std::set<int> seen_edges;
  for(const auto edge:edges){Check(edge>=0 && std::size_t(edge)<m.arcs.size() && seen_edges.insert(edge).second,"unknown/repeated route edge");const auto& a=m.arcs[edge];
    Check(a.direction<0 || (locks.count(a.direction) && locks.at(a.direction)==edge),"route violates half-duplex direction lock");adjacency[a.source].push_back(edge);}
  std::vector<bool> visited(m.nodes.size());std::vector<double> delay(m.nodes.size());std::vector<int> latency(m.nodes.size()),hops(m.nodes.size());std::queue<int> queue;queue.push(d.node);visited[d.node]=true;
  while(!queue.empty()){const auto node=queue.front();queue.pop();for(const auto edge:adjacency[node]){const auto& a=m.arcs[edge];Check(!visited[a.sink],"route contains cycle or multiple source-to-node paths");visited[a.sink]=true;delay[a.sink]=delay[node]+a.delay;latency[a.sink]=latency[node]+a.latency;hops[a.sink]=hops[node]+1;queue.push(a.sink);}}
  TreeResult result;result.arcs=edges;
  for(const auto edge:edges)Check(visited[m.arcs[edge].source] && visited[m.arcs[edge].sink],"disconnected route edge");
  for(const auto sink:d.terminals){Check(visited[sink],"route misses required sink");result.delay=std::max(result.delay,delay[sink]);result.latency=std::max(result.latency,latency[sink]);result.hops=std::max(result.hops,hops[sink]);}
  if(Get(m.constraints,"max_route_hops")!="null")Check(result.hops<=Int(m.constraints,"max_route_hops"),"route exceeds hop limit");
  if(supplied)Check(std::isfinite(*supplied) && *supplied>=0 && std::abs(*supplied-result.delay)<=1e-9,"router delay disagrees with independent edge-delay sum");
  // Preserve the producer's verified floating-point scalar in the public row.
  if(supplied)result.delay=*supplied;
  return result;
}
struct Routed {
  std::vector<TreeResult> routes;
  std::vector<std::set<int>> domains;
  std::vector<std::vector<int>> batches;
  std::vector<I> usage;
  std::vector<int> selection,ratios;
  std::map<int,int> locks;
  Raw native_metrics,metrics,validation;
};
bool Intersects(const std::set<int>& a,const std::set<int>& b) {
  auto x=a.begin(),y=b.begin();while(x!=a.end() && y!=b.end()){if(*x==*y)return true;if(*x<*y)++x;else ++y;}return false;
}
std::vector<std::vector<int>> ReconstructBatches(const Model& m,const std::vector<std::set<int>>& domains) {
  if(!m.timing_driven)return {};
  std::vector<std::set<int>> paths_by_demand(m.demands.size());for(std::size_t i=0;i<m.paths.size();++i)for(const auto d:m.paths[i].demands)paths_by_demand[d].insert(int(i));
  std::vector<int> order(m.paths.size());std::iota(order.begin(),order.end(),0);
  std::stable_sort(order.begin(),order.end(),[&](int a,int b){const auto delta=m.paths[a].normalized-m.paths[b].normalized;return std::abs(delta)>1e-12?delta<0:a<b;});
  std::vector<std::vector<int>> batches;std::vector<std::vector<std::pair<std::set<int>,std::set<int>>>> footprints;
  std::vector<int> last_domain(m.capacities.size(),-1),last_path(m.paths.size(),-1);
  for(const auto i:order){std::set<int> capacity,affected;for(const auto demand:m.paths[i].demands){capacity.insert(domains[demand].begin(),domains[demand].end());affected.insert(paths_by_demand[demand].begin(),paths_by_demand[demand].end());}
    if(m.paths.size()<=4096){std::size_t b=0;for(;b<footprints.size();++b){bool conflict=false;for(const auto& other:footprints[b])if(Intersects(capacity,other.first) || Intersects(affected,other.second)){conflict=true;break;}if(!conflict)break;}
      if(b==batches.size()){batches.emplace_back();footprints.emplace_back();}batches[b].push_back(i);footprints[b].emplace_back(std::move(capacity),std::move(affected));
    }else{int b=0;for(const auto d:capacity)b=std::max(b,last_domain[d]+1);for(const auto p:affected)b=std::max(b,last_path[p]+1);while(int(batches.size())<=b)batches.emplace_back();batches[b].push_back(i);for(const auto d:capacity)last_domain[d]=b;for(const auto p:affected)last_path[p]=b;}
  }
  return batches;
}
std::uint32_t U32(const std::string& bytes,std::size_t offset) {Check(bytes.size()-offset>=4,"truncated candidate ordinal");std::uint32_t out=0;for(unsigned i=0;i<4;++i)out|=std::uint32_t(static_cast<unsigned char>(bytes[offset+i]))<<(8*i);return out;}
Routed ReadRouter(const Model& m,const ArtifactRef* artifact) {
  Routed result;result.usage.resize(m.capacities.size());result.domains.resize(m.demands.size());result.selection.resize(m.demands.size(),-1);
  if(artifact){SqliteStore store(*artifact);auto& db=store.database();
    const std::set<std::string> integer_metrics={"accepted_reroutes","adaptive_hop_candidate_feasible","balanced_candidate_feasible","baseline_candidate_feasible","candidate_workers","estimated_max_tdm_ratio","iterations","master_exact","master_rounds","master_switches","maximum_parallel_batch","metric_closure_candidate_feasible","parallel_candidate_tasks","parallel_reroute_tasks","reroute_conflict_batches","rolled_back_reroutes","selected_delay_demand_balanced","shallow_light_candidate_feasible","steiner_candidate_feasible","total_link_bit_hops"};
    const std::set<std::string> real_metrics={"estimated_worst_tdm_normalized_slack","estimated_worst_tdm_slack_ns","max_utilization","worst_normalized_slack","worst_slack_ns"};
    auto metrics=db.Prepare("SELECT name,kind,integer_value,real_value FROM metrics ORDER BY name");
    while(metrics.Step()){
      const auto name=metrics.TypedText(0);const auto kind=metrics.TypedInteger(1);
      Check((integer_metrics.count(name) && kind==0 && metrics.IsNull(3)) ||
            (real_metrics.count(name) && kind==1 && metrics.IsNull(2)),"invalid router metric name/type");
      Check(result.native_metrics.emplace(name,kind==0?std::to_string(metrics.TypedInteger(2)):JsonRecord(metrics.TypedReal(3)).Encode()).second,"duplicate router metric");
    }
    Check(result.native_metrics.size()==integer_metrics.size()+real_metrics.size(),"incomplete router metric coverage");
    auto locks=db.Prepare("SELECT group_index,arc_index FROM direction_locks ORDER BY group_index");while(locks.Step()){const auto group=int(locks.TypedInteger(0)),arc=int(locks.TypedInteger(1));Check(arc>=0 && std::size_t(arc)<m.arcs.size() && m.arcs[arc].direction==group && result.locks.emplace(group,arc).second,"invalid router direction lock");}
    std::set<int> expected_locks;for(const auto& a:m.arcs)if(a.direction>=0)expected_locks.insert(a.direction);Check(expected_locks.size()==result.locks.size(),"incomplete direction lock coverage");
    auto routes=db.Prepare("SELECT demand_index,max_delay_ns FROM routes ORDER BY demand_index");auto arcs=db.Prepare("SELECT demand_index,seq,arc_index FROM route_arcs ORDER BY demand_index,seq");bool have=arcs.Step();
    while(routes.Step()){const auto index=routes.TypedInteger(0);Check(index==I(result.routes.size()),"route order/coverage disagrees");std::vector<int> edges;
      while(have && arcs.TypedInteger(0)==index){Check(arcs.TypedInteger(1)==I(edges.size()),"noncontiguous route edge ordinal");edges.push_back(int(arcs.TypedInteger(2)));have=arcs.Step();}
      const auto delay=routes.Real(1);result.routes.push_back(ValidateTree(m,index,edges,result.locks,&delay));for(const auto edge:edges)++result.usage[m.arcs[edge].domain];}
    Check(!have && result.routes.size()==m.demands.size(),"orphan route arc or incomplete demand population");
    std::vector<unsigned> candidate_masks(m.demands.size());
    auto candidates=db.Prepare("SELECT demand_index,record_count,records FROM candidate_bundles ORDER BY demand_index");I ordinal=0;
    while(candidates.Step()){const auto index=candidates.TypedInteger(0);Check(index==ordinal++ && index<I(m.demands.size()),"candidate demand coverage disagrees");const auto count=candidates.TypedInteger(1);Check(count>=1 && count<=7,"invalid candidate bundle count");const auto blob=candidates.Blob(2);const std::string bytes(reinterpret_cast<const char*>(blob.data()),blob.size());std::size_t offset=0;int previous=-1;unsigned mask=0;
      for(I i=0;i<count;++i){Check(bytes.size()-offset>=13,"truncated candidate bundle");const auto generator=int(static_cast<unsigned char>(bytes[offset]));Check(generator>previous && generator<=6,"candidate generator order disagrees");previous=generator;mask|=1U<<generator;
        std::uint64_t bits=0;for(unsigned j=0;j<8;++j)bits|=std::uint64_t(static_cast<unsigned char>(bytes[offset+1+j]))<<(8*j);double delay=0;std::memcpy(&delay,&bits,sizeof(delay));const auto size=U32(bytes,offset+9);offset+=13;Check(size<=(bytes.size()-offset)/4,"truncated candidate edges");std::vector<int> edges;for(std::uint32_t j=0;j<size;++j)edges.push_back(int(U32(bytes,offset+4*j)));offset+=4*size;
        const auto tree=ValidateTree(m,index,edges,result.locks,&delay);
        if(generator==6)Check(tree.arcs==result.routes[index].arcs && delay==result.routes[index].delay,"refined candidate differs from final route");
        else for(const auto edge:edges){const auto& a=m.arcs[edge];result.domains[index].insert(a.domain);if(a.banks){Check(a.opposite>=0,"banked arc lacks reverse");result.domains[index].insert(m.arcs[a.opposite].domain);}}
      }Check(offset==bytes.size() && (mask&(1U<<6)),"missing final candidate or trailing bundle bytes");candidate_masks[index]=mask;}
    Check(ordinal==I(m.demands.size()),"missing candidate population");
    auto selection=db.Prepare("SELECT demand_index,generator FROM selections ORDER BY demand_index");I selected=0;while(selection.Step()){const auto index=selection.TypedInteger(0),generator=selection.TypedInteger(1);Check(index==selected++ && index<I(m.demands.size()) && generator>=0 && generator<6,"invalid master selection");Check(candidate_masks[index]&(1U<<generator),"master selection names an unavailable candidate");result.selection[index]=int(generator);}Check(selected==I(m.demands.size()),"incomplete master selection");
    result.batches=ReconstructBatches(m,result.domains);I largest=0;for(const auto& b:result.batches)largest=std::max(largest,I(b.size()));
    Check(Int(result.native_metrics,"reroute_conflict_batches")==I(result.batches.size()) && Int(result.native_metrics,"maximum_parallel_batch")==largest,"independent conflict batch reconstruction disagrees");
    artifact->RequireUnchanged();
  }else Check(m.demands.empty(),"missing router result");
  const auto minimum=Int(m.constraints,"tdm_min_ratio"),maximum=Int(m.constraints,"tdm_max_ratio"),quantum=Int(m.constraints,"tdm_ratio_quantum");result.ratios.resize(m.capacities.size(),1);
  std::set<std::string> banked;
  for(const auto& a:m.arcs){Check(result.usage[a.domain]<=a.capacity,"route exceeds capacity domain");const auto amount=result.usage[a.domain];I ratio=1;
    if(!a.sll && amount>0){const auto raw=std::max(minimum,(amount+a.lanes-1)/a.lanes);ratio=raw==1?1:((raw+quantum-1)/quantum)*quantum;if(minimum<=1)ratio=std::min(maximum,ratio);}result.ratios[a.domain]=int(ratio);
    if(a.banks)banked.insert(a.link);}
  for(const auto& link:banked){const auto first=std::find_if(m.arcs.begin(),m.arcs.end(),[&](const auto& a){return a.link==link;});Check(first!=m.arcs.end() && first->opposite>=0,"invalid bank topology");const auto& reverse=m.arcs[first->opposite];
    const auto per_bank=I(first->wires)*maximum;Check((result.usage[first->domain]+per_bank-1)/per_bank+(result.usage[reverse.domain]+per_bank-1)/per_bank<=first->banks,"route exceeds shared bank inventory");
    const auto ratios=shared_bank_ratios(result.usage[first->domain],result.usage[reverse.domain],first->banks,first->wires,int(quantum),int(minimum),int(maximum+quantum));result.ratios[first->domain]=ratios.first;result.ratios[reverse.domain]=ratios.second;}
  I sinks=0,edges=0,max_hops=0;double max_utilization=0;
  for(std::size_t i=0;i<result.routes.size();++i){auto& route=result.routes[i];sinks+=m.demands[i].sinks.size();edges+=route.arcs.size();max_hops=std::max(max_hops,I(route.hops));
    std::vector<std::vector<int>> adjacency(m.nodes.size());for(const auto edge:route.arcs)adjacency[m.arcs[edge].source].push_back(edge);std::vector<double> delay(m.nodes.size());std::queue<int> queue;queue.push(m.demands[i].node);
    while(!queue.empty()){const auto node=queue.front();queue.pop();for(const auto edge:adjacency[node]){const auto& a=m.arcs[edge];delay[a.sink]=delay[node]+a.delay+a.beta*(result.ratios[a.domain]-1);queue.push(a.sink);}}
    if(Get(m.constraints,"tree_edge_sum_tdm")=="true"){for(const auto edge:route.arcs){const auto& a=m.arcs[edge];route.tdm_delay+=a.delay+(a.sll?0:a.beta)*(result.ratios[a.domain]-1);}}
    else for(const auto sink:m.demands[i].terminals)route.tdm_delay=std::max(route.tdm_delay,delay[sink]);
  }
  Array utilization;I bit_hops=0;for(std::size_t i=0;i<m.capacities.size();++i){auto capacity=m.capacities[i];const auto used=result.usage[i];const auto ratio=double(used)/Int(capacity,"capacity_bits");capacity["used_bits"]=std::to_string(used);capacity["utilization"]=JsonRecord(ratio).Encode();max_utilization=std::max(max_utilization,ratio);bit_hops+=used;utilization.push_back(Record(Encode(capacity)));}
  result.metrics={{"demands",std::to_string(m.demands.size())},{"routed_sinks",std::to_string(sinks)},{"tree_edges",std::to_string(edges)},{"max_link_utilization",JsonRecord(max_utilization).Encode()},{"total_link_bit_hops",std::to_string(bit_hops)}};
  for(const auto* name:{"iterations","accepted_reroutes","rolled_back_reroutes","master_rounds","master_switches","reroute_conflict_batches","maximum_parallel_batch"})result.metrics[name]=artifact?Get(result.native_metrics,name):"0";
  result.metrics["master_exact"]=artifact && Int(result.native_metrics,"master_exact")?"true":"false";
  if(m.demands.empty())for(const auto* key:{"master_rounds","master_switches","master_exact","reroute_conflict_batches","maximum_parallel_batch"})result.metrics.erase(key);
  result.metrics["estimated_max_tdm_ratio"]=std::to_string(m.demands.empty()?0:*std::max_element(result.ratios.begin(),result.ratios.end()));
  if(Get(m.constraints,"max_route_hops")!="null")result.metrics["max_route_hops_observed"]=std::to_string(max_hops);
  result.validation=result.metrics;for(const auto* key:{"master_rounds","master_switches","master_exact","reroute_conflict_batches","maximum_parallel_batch","estimated_max_tdm_ratio","accepted_reroutes","rolled_back_reroutes"})result.validation.erase(key);
  result.validation["status"]=JsonQuote("pass");result.validation["provider"]=JsonQuote(provider);result.validation["overloaded_links"]="0";result.validation["link_utilization"]=JsonRecord(utilization).Encode();result.validation["direction_locks"]=std::to_string(m.demands.empty()?m.default_direction_locks.size():result.locks.size());result.validation["timing_optimization_enabled"]=m.timing_driven && !m.demands.empty()?"true":"false";result.validation["predicted_delay_check"]=JsonQuote("pass");
  return result;
}

struct Section {
  Sha256 hash;I records=0;
  void Add(const std::string& record){hash.update(reinterpret_cast<const unsigned char*>(record.data()),record.size());const unsigned char newline='\n';hash.update(&newline,1);++records;}
  std::string Seal(){std::string out;for(const auto byte:hash.finish()){out+="0123456789abcdef"[byte>>4];out+="0123456789abcdef"[byte&15];}return out;}
};
class SealRequest {
 public:
  SealRequest(const fs::path& path,const std::vector<ArtifactRef>& sources):writer_(path.string(),sources){}
  template<class T>void Put(T value){writer_.Write(value);UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(&value),sizeof(value));}
  void Text(const std::string& text){Check(text.size()<=8*1024*1024,"seal header exceeds bound");Put<std::uint32_t>(text.size());writer_.WriteBytes(text.data(),text.size());UpdateCrc32(crc_,reinterpret_cast<const unsigned char*>(text.data()),text.size());}
  void Finish(){writer_.Write(crc_);writer_.Commit();}
 private:AtomicBinaryWriter writer_;std::uint32_t crc_=0;
};
void WriteSealTree(SealRequest& writer,const std::string& raw,const std::array<I,5>& counts,
                   std::vector<std::string>& path,unsigned depth=0) {
  Check(depth<64,"seal header nesting exceeds bound");
  const std::array<std::vector<std::string>,5> locations{{{"demands"},{"routes"},{"timing","paths"},{"joint_optimization","candidate_generation","master_selection"},{"joint_optimization","refinement_batches","batches"}}};
  for(unsigned i=0;i<locations.size();++i)if(path==locations[i]){writer.Put<std::uint8_t>(2);writer.Put<std::uint8_t>(i);writer.Put<std::uint64_t>(counts[i]);return;}
  bool parent=false;for(const auto& location:locations)if(path.size()<location.size() && std::equal(path.begin(),path.end(),location.begin()))parent=true;
  if(parent && !raw.empty() && raw[0]=='{'){const auto fields=Fields(raw);writer.Put<std::uint8_t>(1);writer.Put<std::uint32_t>(fields.size());for(const auto& field:fields){writer.Text(field.first);path.push_back(field.first);WriteSealTree(writer,field.second,counts,path,depth+1);path.pop_back();}}
  else{writer.Put<std::uint8_t>(0);writer.Text(raw);}
}
struct Publication {Raw report,header,selector;};
Publication PublishRoutes(Model& m,Routed& routed,SqliteDatabase& staging,
                          const ArtifactRef* native_result,const fs::path& directory,
                          const fs::path& scratch,const std::vector<ArtifactRef>& sources,
                          const volatile std::sig_atomic_t* cancelled) {
  const auto temporary=directory/"route-store.sqlite";Raw header;
  header={{"schema",JsonQuote("emuflow.system-routes/v1")},{"design",JsonQuote(m.design)},{"platform",JsonQuote(m.platform)},
    {"provider",JsonQuote(provider)},{"constraints",Encode(m.constraints)},{"metrics",Encode(routed.metrics)}};
  Array utilization,locks;
  for(std::size_t i=0;i<m.capacities.size();++i){auto c=m.capacities[i];c["used_bits"]=std::to_string(routed.usage[i]);c["utilization"]=JsonRecord(double(routed.usage[i])/Int(c,"capacity_bits")).Encode();utilization.push_back(Record(Encode(c)));}
  for(const auto& lock:routed.locks){const auto& a=m.arcs[lock.second];locks.emplace_back(Object{{"group",lock.first},{"link",a.link},{"from",a.from},{"to",a.to}});}
  if(m.demands.empty())for(const auto& lock:m.default_direction_locks)locks.push_back(Record(Encode(lock.second)));
  header["link_utilization"]=JsonRecord(utilization).Encode();header["direction_locks"]=JsonRecord(locks).Encode();
  if(m.exact){header["semantic_contract_schema"]=JsonQuote(m.semantic_schema);header["semantic_contract_sha256"]=JsonQuote(m.semantic_hash);routed.validation["transport_round_policy"]=JsonQuote("dependency-diagnostic-only");}
  if(!m.demands.empty() && (!m.timing_driven || m.paths.empty()))header["timing_evaluation"]=JsonRecord(Object{{"mode","post-route-evaluation-only"},{"optimization_enabled",false}}).Encode();
  Raw joint,candidate,refinement;I largest=0;for(const auto& b:routed.batches)largest=std::max(largest,I(b.size()));
  if(!m.demands.empty()){
    candidate={{"shortest_path_tree",Int(routed.native_metrics,"baseline_candidate_feasible")?"true":"false"},
      {"delay_demand_balanced_tree",Int(routed.native_metrics,"balanced_candidate_feasible")?"true":"false"},
      {"selected",JsonQuote(Int(routed.native_metrics,"selected_delay_demand_balanced")?"delay-demand-balanced":"shortest-path-tree")}};
    refinement={{"batch_count",std::to_string(routed.batches.size())},{"maximum_parallel_batch",std::to_string(largest)}};
    joint={{"method",JsonQuote("checked-candidate-pool+deterministic-batch-conflict-lns-v2")},
      {"objective",JsonQuote("lexicographic estimated TDM normalized slack, route normalized slack, utilization, bit-hops")},
      {"ratio_model",JsonQuote("quantized-domain-load-over-lanes")},
      {"reference",JsonQuote("DAC 2025 delay-demand-balanced die-level routing and ASP-DAC 2026 timing-aware load-balanced routing/rip-up-reroute")},
      {"candidate_generation",Encode(candidate)},{"refinement_batches",Encode(refinement)}};
    header["joint_optimization"]=Encode(joint);
  }
  std::string logical;Raw sections;
  {
    SqliteArtifactWriter writer(temporary.string(),"system-route-store-v3",sources);auto& db=writer.database();db.Execute(kSystemRouteDDL);
    std::array<Section,5> section;
    {
      SqliteTransaction transaction(db);
      auto demand=db.Prepare("INSERT INTO demands VALUES(?,?,?)");auto route=db.Prepare("INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,?,?,?)");
      auto sink=db.Prepare("INSERT INTO route_sinks VALUES(?,?,?)");auto edge=db.Prepare("INSERT INTO route_edges VALUES(?,?,?,?,?)");auto predecessor=db.Prepare("INSERT INTO route_predecessors VALUES(?,?,?)");auto selection=db.Prepare("INSERT INTO route_master_selections VALUES(?,?,?)");
      for(std::size_t i=0;i<m.demands.size();++i){Stop(cancelled);const auto& d=m.demands[i];const auto& tree=routed.routes[i];demand.BindInteger(1,i);demand.BindText(2,d.id);demand.BindInteger(3,i);demand.Execute();demand.Reset();
        route.BindInteger(1,i);route.BindText(2,d.id);route.BindText(3,d.net);route.BindText(4,d.source);route.BindInteger(5,1);
        for(const auto& field:std::vector<std::pair<const char*,int>>{{"transport_round",6},{"cut_class",7},{"dependency_level",8},{"combinational_dependency_depth",9}}){if(!d.fields.count(field.first))route.BindNull(field.second);else if(field.second==7)route.BindText(field.second,Text(d.fields,field.first));else route.BindInteger(field.second,Int(d.fields,field.first));}
        route.BindInteger(10,tree.latency);route.BindReal(11,tree.delay);route.Execute();route.Reset();
        for(std::size_t j=0;j<d.sinks.size();++j){sink.BindInteger(1,i);sink.BindInteger(2,j);sink.BindText(3,d.sinks[j]);sink.Execute();sink.Reset();}
        Array edges;for(std::size_t j=0;j<tree.arcs.size();++j){const auto& a=m.arcs[tree.arcs[j]];edge.BindInteger(1,i);edge.BindInteger(2,j);edge.BindText(3,a.link);edge.BindText(4,a.from);edge.BindText(5,a.to);edge.Execute();edge.Reset();edges.emplace_back(Object{{"link",a.link},{"from",a.from},{"to",a.to}});}
        for(std::size_t j=0;j<d.predecessors.size();++j){predecessor.BindInteger(1,i);predecessor.BindInteger(2,j);predecessor.BindText(3,d.predecessors[j]);predecessor.Execute();predecessor.Reset();}
        auto record=d.fields;record["tree_edges"]=JsonRecord(edges).Encode();record["max_latency_cycles"]=std::to_string(tree.latency);record["predicted_max_delay_ns"]=JsonRecord(tree.delay).Encode();section[0].Add(Encode(d.fields));section[1].Add(Encode(record));
        selection.BindInteger(1,i);selection.BindText(2,d.id);selection.BindText(3,generators[routed.selection[i]]);selection.Execute();selection.Reset();section[3].Add(JsonRecord(Object{{"demand",d.id},{"generator",generators[routed.selection[i]]}}).Encode());
      }
      std::optional<SqliteStore> native;if(native_result)native.emplace(*native_result);
      auto native_paths=native?native->database().Prepare("SELECT path_index,delay_ns,slack_ns,normalized_slack FROM timing_paths ORDER BY path_index"):SqliteStatement{};
      bool have_native=native && native_paths.Step();
      auto input=staging.Prepare("SELECT seq,record_json FROM normalized_paths ORDER BY seq");auto output=db.Prepare("INSERT INTO timing_paths VALUES(?,?,?)");std::size_t count=0;
      std::array<std::pair<double,std::string>,4> worst;for(auto& item:worst)item={std::numeric_limits<double>::infinity(),""};
      while(input.Step()){Stop(cancelled);Check(input.TypedInteger(0)==I(count) && count<m.paths.size(),"normalized path cursor disagrees");const auto fields=Fields(input.TypedText(1));const auto& p=m.paths[count];
        double transport=0,tdm_transport=0;std::string signature;
        for(const auto d:p.demands){transport+=routed.routes[d].delay;tdm_transport+=routed.routes[d].tdm_delay;auto arcs=routed.routes[d].arcs;std::sort(arcs.begin(),arcs.end());for(const auto arc:arcs){if(!signature.empty())signature+=',';signature+=std::to_string(arc);}}
        const double required=p.fixed+p.slack,delay=p.fixed+transport,slack=required-delay,normalized=Normalized(m,slack,p.period),tdm_delay=p.fixed+tdm_transport,tdm_slack=required-tdm_delay,tdm_normalized=Normalized(m,tdm_slack,p.period);
        Raw record;for(const auto* field:{"clock_domain","clock_period_ns","fixed_delay_ns","cut_nets","cut_signature","compressed_path_ids"})record[field]=Get(fields,field);
        if(fields.count("cut_transitions"))record["cut_transitions"]=fields.at("cut_transitions");
        record["path"]=JsonQuote(p.id);record["required_time_ns"]=JsonRecord(required).Encode();record["route_signature"]=JsonQuote(signature.empty()?"-":signature);
        if(m.timing_driven){Check(have_native && native_paths.TypedInteger(0)==I(count),"missing native timing result");for(const auto& expected:std::vector<std::pair<int,double>>{{1,delay},{2,slack},{3,normalized}})Check(std::isfinite(native_paths.Real(expected.first)) && std::abs(native_paths.Real(expected.first)-expected.second)<=1e-9,"native timing disagrees with independent route reconstruction");
          record["delay_ns"]=JsonRecord(native_paths.Real(1)).Encode();record["slack_ns"]=JsonRecord(native_paths.Real(2)).Encode();record["normalized_slack"]=JsonRecord(native_paths.Real(3)).Encode();have_native=native_paths.Step();
        }else{record["delay_ns"]=JsonRecord(delay).Encode();record["slack_ns"]=JsonRecord(slack).Encode();record["normalized_slack"]=JsonRecord(normalized).Encode();record["estimated_tdm_slack_ns"]=JsonRecord(tdm_slack).Encode();record["estimated_tdm_normalized_slack"]=JsonRecord(tdm_normalized).Encode();}
        const auto encoded=Encode(record);output.BindInteger(1,count);output.BindText(2,p.id);output.BindText(3,encoded);output.Execute();output.Reset();section[2].Add(encoded);
        const std::array<double,4> values{slack,normalized,tdm_slack,tdm_normalized};for(std::size_t i=0;i<4;++i)worst[i]=std::min(worst[i],std::make_pair(values[i],p.id));++count;
      }
      Check(count==m.paths.size() && !have_native,"timing result coverage disagrees");
      if(count){header["timing"]=JsonRecord(Object{{"schema","emuflow.sta-paths/v1"},{"normalization",Record(Encode(m.normalization))},{"compression",Record(Encode(m.compression))}}).Encode();
        routed.validation.erase("predicted_delay_check");routed.validation["timing_paths_original"]=Get(m.compression,"original_paths");routed.validation["timing_paths_compressed"]=std::to_string(count);
        const char* metric_names[]={"worst_slack_ns","worst_normalized_slack","estimated_worst_tdm_slack_ns","estimated_worst_tdm_normalized_slack"};
        const char* path_names[]={"worst_slack_path","worst_normalized_path","estimated_worst_tdm_slack_path","estimated_worst_tdm_normalized_path"};
        for(unsigned i=0;i<4;++i){routed.validation[metric_names[i]]=JsonRecord(worst[i].first).Encode();routed.validation[path_names[i]]=JsonQuote(worst[i].second);
          if(m.timing_driven){Check(std::abs(Num(routed.native_metrics,metric_names[i])-worst[i].first)<=1e-9,"native objective metric disagrees with independent timing reduction");routed.metrics[metric_names[i]]=Get(routed.native_metrics,metric_names[i]);}
          else routed.metrics[metric_names[i]]=JsonRecord(worst[i].first).Encode();}
        routed.validation["estimated_max_tdm_ratio"]=Get(routed.metrics,"estimated_max_tdm_ratio");routed.validation["accepted_reroutes"]=Get(routed.metrics,"accepted_reroutes");routed.validation["rolled_back_reroutes"]=Get(routed.metrics,"rolled_back_reroutes");
      }
      header["metrics"]=Encode(routed.metrics);
      auto batch=db.Prepare("INSERT INTO refinement_batch_paths VALUES(?,?,?,?)");I seq=0;for(std::size_t b=0;b<routed.batches.size();++b)for(std::size_t position=0;position<routed.batches[b].size();++position){const auto p=routed.batches[b][position];batch.BindInteger(1,seq++);batch.BindInteger(2,b);batch.BindInteger(3,position);batch.BindInteger(4,p);batch.Execute();batch.Reset();section[4].Add(JsonRecord(Object{{"batch",b},{"position",position},{"path",p}}).Encode());}
      const char* section_names[]={"demands","routes","timing_paths","master_selections","refinement_batch_paths"};
      for(unsigned i=0;i<5;++i){Object seal{{"records",section[i].records},{"sha256",section[i].Seal()}};if(i==4)seal.emplace("batches",routed.batches.size());sections[section_names[i]]=JsonRecord(seal).Encode();}
      auto metadata=db.Prepare("INSERT INTO metadata VALUES(?,?)");for(const auto& field:Raw{{"header",Encode(header)},{"sections",Encode(sections)}}){metadata.BindText(1,field.first);metadata.BindText(2,field.second);metadata.Execute();metadata.Reset();}
      transaction.Commit();
    }
    auto logical_header=header;logical_header["demands"]="[]";logical_header["routes"]="[]";
    if(logical_header.count("timing")){auto timing=Fields(logical_header["timing"]);timing["paths"]="[]";logical_header["timing"]=Encode(timing);}
    if(!m.demands.empty()){candidate["master_selection"]="[]";refinement["batches"]="[]";joint["candidate_generation"]=Encode(candidate);joint["refinement_batches"]=Encode(refinement);logical_header["joint_optimization"]=Encode(joint);}
    const auto request_path=scratch/"seal.bin";SealRequest request(request_path,sources);request.Put<std::uint64_t>(0x31515352464d5545ULL);request.Put<std::uint32_t>(1);
    std::vector<std::string> path;WriteSealTree(request,Encode(logical_header),{I(m.demands.size()),I(m.demands.size()),I(m.paths.size()),I(m.demands.size()),I(routed.batches.size())},path);request.Finish();
    const auto seal=Fields(SealSystemRouteDatabase(db,request_path.string()));logical=Text(seal,"logical_sha256");Check(Get(seal,"sections")==Encode(sections),"route seal differs from producer relation hashes");
    {auto q=db.Prepare("INSERT INTO metadata VALUES('logical_sha256',?)");q.BindText(1,JsonQuote(logical));q.Execute();}
    writer.Commit();
  }
  const auto hash=DigestFile(temporary),filename="routes."+hash+".sqlite";fs::rename(temporary,directory/filename);
  Raw selector{{"schema",JsonQuote("emuflow.system-route-storage/v1")},{"logical_schema",JsonQuote("emuflow.system-routes/v1")},
    {"application_id",std::to_string(0x454d5552)},{"user_version","3"},{"header",Encode(header)},{"sections",Encode(sections)},
    {"logical_sha256",JsonQuote(logical)},{"artifact",JsonQuote(filename)},{"store_bytes",std::to_string(fs::file_size(directory/filename))},{"store_sha256",JsonQuote(hash)}};
  Raw artifacts{{"constraints",JsonQuote("route_constraints.normalized.json")},{"routes",JsonQuote("routes.json")},{"route_store",JsonQuote(filename)},{"report",JsonQuote("phase4_report.json")}};
  Raw records;for(const auto& section:sections)records[section.first]=Get(Fields(section.second),"records");
  Raw report{{"schema",JsonQuote("emuflow.phase4-report/v1")},{"phase","4"},{"status",JsonQuote("pass")},{"design",JsonQuote(m.design)},{"platform",JsonQuote(m.platform)},{"provider",JsonQuote(provider)},
    {"stage_implementation_id",JsonQuote("native-phase4-v1")},{"validation",Encode(routed.validation)},{"artifacts",Encode(artifacts)},
    {"candidate_generation",JsonRecord(Object{{"requested_workers",m.workers},{"ordering","demand-index-then-generator-index"},{"route_artifact_deterministic",true},{"validation_dataflow","streamed-candidate-domain-footprint-v1"}}).Encode()},
    {"route_storage",JsonRecord(Object{{"schema","emuflow.system-route-storage/v1"},{"user_version",3},{"dataflow","native-sql-route-arc-delay-selection-publication-v1"},{"bytes",fs::file_size(directory/filename)},{"logical_sha256",logical},{"records",Record(Encode(records))}}).Encode()}};
  if(m.exact){report["cut_mode"]=JsonQuote("static-exact-combinational");report["qualification"]=JsonQuote("route-contract-propagation-pass");}
  for(const auto& file:std::vector<std::pair<std::string,std::string>>{{"routes.json",Encode(selector)},{"route_constraints.normalized.json",Encode(m.constraints)},{"phase4_report.json",Encode(report)}})
    PublishTextArtifact((directory/file.first).string(),sources,[&](std::ostream& out){out<<file.second<<'\n';});
  return {report,header,selector};
}
} // namespace

StageResult RunPhase4(const std::vector<ArtifactRef>& inputs,const std::string& directory,
                      const volatile std::sig_atomic_t* cancelled) {
  return RunStage("phase4.run",[&]{Check((inputs.size()==3 || inputs.size()==4) && inputs[0].schema()=="assignment-index-v4" && inputs[1].schema()=="boarddb-v1" && inputs[2].schema()=="phase4-policy-v1" && (inputs.size()==3 || inputs[3].schema()=="system-routing-paths-v1"),"input source roles disagree");return inputs;},[&](const auto& sources){
    Stop(cancelled);const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"output directory already exists");fs::create_directories(destination.parent_path());Workspace work(destination.parent_path()),generation(destination.parent_path());
    Model model;const auto policy=Fields(ReadSmall(inputs[2]));Check(policy.size()==5 && Text(policy,"schema")=="emuflow.phase4-policy/v1","unsupported stage policy");
    model.constraints=Fields(Get(policy,"constraints"));model.workers=int(Int(policy,"candidate_workers"));Check(model.workers>0,"invalid worker count");Check(Get(policy,"timing_driven")=="true" || Get(policy,"timing_driven")=="false","invalid timing policy");model.timing_driven=Get(policy,"timing_driven")=="true";
    const auto refs=Values(Get(policy,"sources"));Check(refs.size()==inputs.size()-1,"policy source coverage disagrees");
    for(std::size_t i=0;i<refs.size();++i){const auto& source=inputs[i<2?i:i+1];const auto ref=Fields(refs[i]);Check(ref.size()==3 && Text(ref,"path")==source.path() && Text(ref,"schema")==source.schema(),"policy source identity disagrees");const auto& g=source.generation();
      Check(Record(Get(ref,"generation")).Encode()==JsonRecord(Array{g.device,g.inode,g.bytes,g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds}).Encode(),"policy source generation changed");}
    {SqliteStore assignment(inputs[0]);const auto semantic=Meta(assignment.database(),"semantic_contract_sha256");const auto schema=Meta(assignment.database(),"semantic_contract_schema");
      model.exact=semantic!="null";Check(model.exact==(schema!="null"),"incomplete semantic contract identity");
      if(model.exact){model.semantic_hash=DecodeJsonString(semantic);model.semantic_schema=DecodeJsonString(schema);
        Check(model.semantic_schema=="emuflow.static-exact-combinational-cut/v3" && model.semantic_hash.size()==64 &&
          std::all_of(model.semantic_hash.begin(),model.semantic_hash.end(),[](char c){return (c>='0' && c<='9') || (c>='a' && c<='f');}),"invalid semantic contract schema/digest");}}

    BuildBoard(model,Fields(ReadSmall(inputs[1])));BuildDemands(model,inputs[0]);
    SqliteDatabase staging((work.root/"paths.sqlite").string(),SqliteDatabase::Mode::kCreateExclusive);staging.Execute("PRAGMA journal_mode=OFF;PRAGMA synchronous=OFF;PRAGMA temp_store=FILE");LoadPaths(model,inputs.size()==4?&inputs[3]:nullptr,staging,cancelled);
    std::optional<ArtifactRef> native_result;
    if(!model.demands.empty()){const auto request=work.root/"router.in",output=work.root/"router.sqlite";WriteRouterInput(model,request,sources);Stop(cancelled);
      auto kernel_sources=sources;kernel_sources.insert(kernel_sources.begin(),ArtifactRef::Bind(request.string(),"global-route-input-v1"));emuflow::native::RouteTlr(kernel_sources,output.string(),cancelled);Stop(cancelled);native_result=ArtifactRef::Bind(output.string(),"tlr-output-v1");}
    auto routed=ReadRouter(model,native_result?&*native_result:nullptr);(void)PublishRoutes(model,routed,staging,native_result?&*native_result:nullptr,generation.root,work.root,sources,cancelled);
    staging.Close();Stop(cancelled);PublishNewDirectory(generation.root.string(),destination.string(),sources);generation.root.clear();
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"phase4_report.json").string(),"phase4-report-v1"),ArtifactRef::Bind((fs::path(directory)/"routes.json").string(),"system-route-selector-v1")};});
}
} // namespace emuflow::core
