#include "emuflow_core/routed_segments.hpp"
#include "emuflow_core/physical_evidence.hpp"
#include "emuflow_core/global_event_schema.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"

#include <algorithm>
#include <cmath>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <unordered_map>

namespace emuflow::core {
namespace {
using Integer=std::int64_t;
using Node=std::size_t;
using Object=std::map<std::string,JsonRecord>;
void Check(bool value,const std::string& reason) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"RapidWright routed segments "+reason);
}
std::string Text(SqliteStatement& row,int field) {
  auto value=row.TypedText(field);Check(!value.empty(),"has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"metadata is missing");auto value=row.TypedText(0);Check(!row.Step(),"metadata repeats");return value;
}
std::string Digest(Sha256& hash) {
  std::ostringstream text;text<<std::hex<<std::setfill('0');for(auto c:hash.finish())text<<std::setw(2)<<unsigned(c);return text.str();
}
std::string Pin(const std::string& cell,const std::string& port,Integer bit,Integer width) {
  Check(bit>=0 && width>0 && bit<width,"pin ordinal is invalid");
  return "cell:"+cell+"/"+port+(width==1?"":"["+std::to_string(bit)+"]");
}
std::string Top(const std::string& port,Integer bit,Integer width) {
  Check(bit>=0 && width>0 && bit<width,"top port ordinal is invalid");
  return "top:"+port+(width==1?"":"["+std::to_string(bit)+"]");
}
bool Line(std::istream& input,std::string& line) {
  line.clear();char c;
  while(input.get(c)) {
    Check(c!='\0' && line.size()<16*1024*1024,"endpoint record is invalid or oversized");
    line+=c;if(c=='\n')return true;
  }
  Check(!input.bad() && line.empty(),"endpoint stream is truncated");return false;
}

// Integer nodes are private to this leased mapped artifact. Keep only forward
// adjacency. Query work arrays are reused; each solve clears/visits only its
// reachable cone, rather than initializing graph-sized maps for every path.
class Graph {
 public:
  struct Edge {Node target;double delay;};
  Node Intern(const std::string& name) {
    const auto found=ids.find(name);if(found!=ids.end())return found->second;
    const Node id=names.size();ids.emplace(name,id);names.push_back(name);
    if(frozen)row_offsets.push_back(flat.size());else edges.emplace_back();connected.push_back(false);
    offsets.push_back(std::numeric_limits<double>::quiet_NaN());return id;
  }
  void EdgeTo(Node from,Node to,double delay) {
    Check(!frozen,"graph is already sealed");
    Check(std::isfinite(delay) && delay>=0,"edge delay is invalid");
    edges.at(from).push_back({to,delay});connected.at(from)=connected.at(to)=true;++edge_count;
  }
  bool Connected(const std::string& name) const {
    auto found=ids.find(name);return found!=ids.end() && connected[found->second];
  }
  void Source(Node id,double delay) {offsets.at(id)=delay;}
  void Freeze() {
    flat.reserve(edge_count);row_offsets.reserve(names.size()+1);row_offsets.push_back(0);
    for(auto& row:edges){flat.insert(flat.end(),row.begin(),row.end());row_offsets.push_back(flat.size());}
    std::vector<std::vector<Edge>>{}.swap(edges);frozen=true;
  }
  struct Range {
    std::vector<Edge>::const_iterator first,last;
    auto begin() const {return first;}auto end() const {return last;}
  };
  Range Successors(Node n) const {return {flat.begin()+row_offsets[n],flat.begin()+row_offsets[n+1]};}
  void Solve(const std::string& start) {
    // An empty start is the shared architectural-source TX solve.
    std::vector<Node> starts;
    if(start.empty()) {
      for(Node n=0;n<names.size();++n)if(std::isfinite(offsets[n]))starts.push_back(n);
      std::sort(starts.begin(),starts.end(),[&](Node a,Node b){return names[a]<names[b];});
    } else starts.push_back(Intern(start));
    for(Node n:touched){seen[n]=false;arrival[n]=-std::numeric_limits<double>::infinity();degree[n]=0;}
    seen.resize(names.size(),false);arrival.resize(names.size(),-std::numeric_limits<double>::infinity());
    degree.resize(names.size(),0);origin.resize(names.size(),0);touched.clear();
    std::vector<Node> pending;
    for(Node n:starts)if(!seen[n]){seen[n]=true;touched.push_back(n);pending.push_back(n);arrival[n]=std::isfinite(offsets[n])?offsets[n]:0;origin[n]=n;}
    while(!pending.empty()) {
      Node n=pending.back();pending.pop_back();
      for(const auto& e:Successors(n))if(!seen[e.target]){seen[e.target]=true;touched.push_back(e.target);pending.push_back(e.target);}
    }
    for(Node n:touched)for(const auto& e:Successors(n))++degree[e.target];
    std::vector<Node> ready;for(Node n:touched)if(!degree[n])ready.push_back(n);
    std::sort(ready.begin(),ready.end(),[&](Node a,Node b){return names[a]<names[b];});
    std::deque<Node> queue(ready.begin(),ready.end());std::size_t processed=0;
    while(!queue.empty()) {
      Node n=queue.front();queue.pop_front();++processed;
      for(const auto& e:Successors(n)) {
        if(std::isfinite(arrival[n])) {
          const double candidate=arrival[n]+e.delay;
          if(candidate>arrival[e.target]){arrival[e.target]=candidate;origin[e.target]=origin[n];}
        }
        if(!--degree[e.target])queue.push_back(e.target);
      }
    }
    cyclic=processed!=touched.size();++solves;visited+=touched.size();
  }
  std::pair<double,std::string> Result(const std::string& target) const {
    const auto it=ids.find(target);Check(it!=ids.end() && it->second<seen.size() && seen[it->second],"timing graph has no path to "+target);
    Check(!cyclic,"timing graph contains a combinational cycle");
    return {arrival[it->second],names[origin[it->second]]};
  }
  std::unordered_map<std::string,Node> ids;
  std::vector<std::string> names;
  std::vector<std::vector<Edge>> edges;
  std::vector<bool> connected;
  std::vector<double> offsets;
  std::map<std::tuple<std::string,Integer,std::string>,double> io;
  std::uint64_t undriven_bits=0;
  std::uint64_t edge_count=0,solves=0,visited=0;
 private:
  std::vector<bool> seen;
  std::vector<double> arrival;
  std::vector<Node> degree,origin,touched;
  std::vector<Node> row_offsets;
  std::vector<Edge> flat;
  bool frozen=false;
  bool cyclic=false;
};

Graph BuildGraph(const std::vector<ArtifactRef>& inputs) {
  SqliteStore mapped(inputs[0]);auto& db=mapped.database();
  db.Execute("PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-8192");
  {auto attach=db.Prepare("ATTACH DATABASE ? AS original");attach.BindText(1,SqliteImmutableUri(inputs[1].path()));attach.Execute();}
  SqliteTransaction staging(db,SqliteTransaction::Mode::kDeferred);
  std::ifstream metadata_file(inputs[2].path(),std::ios::binary);Check(bool(metadata_file),"timing header is missing");
  // The existing timing manifest owns clock-gate/fixed-I/O metadata. Do not
  // copy that population into a request or reconstruct it in the host.
  std::string timing((std::istreambuf_iterator<char>(metadata_file)),{});Check(!metadata_file.bad(),"timing header read failed");
  auto header=db.Prepare("SELECT json_extract(?1,'$.schema'),json_extract(?1,'$.top'),json_extract(?1,'$.payloads.endpoints.records'),json_extract(?1,'$.payloads.endpoints.sha256')");header.BindText(1,timing);
  Check(header.Step() && header.Text(0)=="emuflow.xilinx-routed-timing/v2","timing schema is invalid");
  const auto top_json=Metadata(db,"top_json");auto top=db.Prepare("SELECT json_extract(?,'$')");top.BindText(1,top_json);
  Check(top.Step() && Text(top,0)==Text(header,1),"mapped top disagrees");
  const auto expected=header.TypedInteger(2);const auto expected_sha=Text(header,3);Check(expected>=0,"endpoint count is invalid");
  auto coefficients=db.Prepare("SELECT key,value FROM json_each(?,'$.qualification.logic_coefficients_ps') ORDER BY key");coefficients.BindText(1,timing);
  double lut=-1,ff=-1,carry=-1;
  while(coefficients.Step()) {
    const auto key=Text(coefficients,0);const auto delay=coefficients.TypedReal(1);Check(std::isfinite(delay) && delay>=0,"coefficient is invalid");
    if(key.rfind("lut_a",0)==0)lut=std::max(lut,delay/1000);else if(key=="ff_clock_to_q")ff=delay/1000;else if(key=="carry_co")carry=delay/1000;
  }
  Check(lut>=0 && ff>=0 && carry>=0,"coefficients are incomplete");
  Graph graph;std::set<std::string> gates;
  auto gate=db.Prepare("SELECT json_extract(value,'$.logical_cell') FROM json_each(?,'$.clock_gates')");gate.BindText(1,timing);
  while(gate.Step())gates.insert(Text(gate,0));
  auto io=db.Prepare("SELECT json_extract(value,'$.logical_port'),json_extract(value,'$.logical_bit'),json_extract(value,'$.direction'),json_extract(value,'$.route_delay_ns') FROM json_each(?,'$.virtual_io')");io.BindText(1,timing);
  while(io.Step()) {
    const auto port=Text(io,0),direction=Text(io,2);const auto bit=io.TypedInteger(1);const auto delay=io.TypedReal(3);
    Check(bit>=0 && (direction=="tx" || direction=="rx") && std::isfinite(delay) && delay>=0 && graph.io.emplace(std::make_tuple(port,bit,direction),delay).second,"fixed I/O timing is invalid");
  }
  db.Execute("CREATE TEMP TABLE routed_delays(cell TEXT NOT NULL,pin TEXT NOT NULL,delay REAL NOT NULL,PRIMARY KEY(cell,pin)) WITHOUT ROWID");
  auto endpoint=db.Prepare("SELECT json_type(?1),json_extract(?1,'$.sink.instance'),json_extract(?1,'$.sink.pin'),json_extract(?1,'$.route_delay_ns')");
  auto put_delay=db.Prepare("INSERT INTO routed_delays VALUES(?,?,?)");
  std::ifstream stream(inputs[3].path(),std::ios::binary);Check(bool(stream),"endpoint stream is missing");
  std::string line;Sha256 digest;Integer count=0;
  while(Line(stream,line)) {
    digest.update(reinterpret_cast<const unsigned char*>(line.data()),line.size());endpoint.Reset();endpoint.BindText(1,line);
    Check(endpoint.Step() && endpoint.Text(0)=="object","endpoint record is invalid");const auto delay=endpoint.TypedReal(3);Check(std::isfinite(delay) && delay>=0,"route delay is invalid");
    put_delay.BindText(1,Text(endpoint,1));put_delay.BindText(2,Text(endpoint,2));put_delay.BindReal(3,delay);put_delay.Execute();++count;
  }
  Check(count==expected && Digest(digest)==expected_sha,"endpoint count/digest disagrees");
  std::set<Integer> declared;
  auto floating=db.Prepare("SELECT n.id,m.record_json,EXISTS(SELECT 1 FROM original.endpoint_records e WHERE e.net_seq=n.seq AND e.role=0) FROM original.net_records n LEFT JOIN main.netnames m ON m.id=n.id WHERE n.cut_class='undriven' ORDER BY n.id");
  auto bits=db.Prepare("SELECT type,value FROM json_each(?,'$.bits')");
  while(floating.Step()) {
    const auto name=Text(floating,0);Check(floating.TypedInteger(2)==0,"declared undriven source has a driver");Check(!floating.IsNull(1),"declared undriven source is absent from mapping");
    bits.Reset();bits.BindText(1,floating.TypedText(1));bool any=false;
    while(bits.Step()){Check(bits.Text(0)=="integer","declared undriven source maps to a constant");declared.insert(bits.TypedInteger(1));any=true;}Check(any,"declared undriven source has no mapped bits");
  }
  db.Execute("CREATE TEMP TABLE endpoints(bit INTEGER NOT NULL,node INTEGER NOT NULL,driver INTEGER NOT NULL,major INTEGER NOT NULL,owner TEXT NOT NULL,port TEXT NOT NULL,ordinal INTEGER NOT NULL,delay REAL NOT NULL)");
  auto put=db.Prepare("INSERT INTO endpoints VALUES(?,?,?,?,?,?,?,?)");
  auto add=[&](Integer bit,Node node,bool driver,int major,const std::string& owner,const std::string& port,Integer ordinal,double delay) {
    Check(bit>=0,"mapped bit is invalid");put.BindInteger(1,bit);put.BindInteger(2,node);put.BindInteger(3,driver);put.BindInteger(4,major);put.BindText(5,owner);put.BindText(6,port);put.BindInteger(7,ordinal);put.BindReal(8,delay);put.Execute();
  };
  const auto ports=Metadata(db,"ports_json");
  auto top_pins=db.Prepare("SELECT p.key,json_extract(p.value,'$.direction'),json_array_length(p.value,'$.bits'),b.key,b.type,b.value FROM json_each(?1) p,json_each(p.value,'$.bits') b ORDER BY p.key,CAST(b.key AS INTEGER)");top_pins.BindText(1,ports);
  while(top_pins.Step()) {
    const auto name=Text(top_pins,0),direction=Text(top_pins,1);const auto width=top_pins.TypedInteger(2),bit=top_pins.TypedInteger(3);
    Check(direction=="input" || direction=="output" || direction=="inout","top direction is invalid");if(top_pins.Text(4)!="integer")continue;
    const auto node=graph.Intern(Top(name,bit,width));const bool driver=direction!="output";add(top_pins.TypedInteger(5),node,driver,0,name,"",bit,0);if(driver)graph.Source(node,0);
  }
  auto cells=db.Prepare("SELECT id,type FROM cells ORDER BY id");
  auto pins=db.Prepare("SELECT p.cell_id,p.port,p.port_bit,p.port_width,p.direction,p.value_kind,p.net_bit,COALESCE(d.delay,0.0) FROM cell_pins p LEFT JOIN routed_delays d ON d.cell=p.cell_id AND d.pin=CASE WHEN p.port_width=1 THEN p.port ELSE p.port||'['||p.port_bit||']' END ORDER BY p.cell_id,p.port,p.port_bit");bool pin=pins.Step();
  const std::set<std::string> sequential{"FDCE","FDPE","FDRE","FDSE","RAMB18E2","RAMB36E2","URAM288"};
  while(cells.Step()) {
    const auto name=Text(cells,0),type=Text(cells,1);std::vector<Node> in,out;
    while(pin && pins.Text(0)<=name) {
      Check(pins.Text(0)==name,"pin owner is absent");const auto port=Text(pins,1);const auto ordinal=pins.TypedInteger(2),width=pins.TypedInteger(3),direction=pins.TypedInteger(4),kind=pins.TypedInteger(5);
      Check(direction>=0 && direction<=2 && (kind==0 || kind==1),"mapped pin is invalid");auto n=graph.Intern(Pin(name,port,ordinal,width));
      if(direction!=1)in.push_back(n);if(direction!=0)out.push_back(n);
      if(kind==0)add(pins.TypedInteger(6),n,direction!=0,1,name,port,ordinal,pins.TypedReal(7));pin=pins.Step();
    }
    if(sequential.count(type)){for(Node n:out)graph.Source(n,type.rfind("FD",0)==0?ff:0);}
    else if(type=="VCC" || type=="GND"){for(Node n:out)graph.Source(n,0);}
    else if(!gates.count(name)) {
      double delay=0;
      if(type=="CARRY8")delay=carry;else if(type.rfind("LUT",0)==0 || type.rfind("MUXF",0)==0)delay=lut;else if(type=="DSP48E2")delay=5;
      else Check(false,"timing does not cover "+type);
      for(Node from:in)for(Node to:out)graph.EdgeTo(from,to,delay);
    }
  }
  Check(!pin,"orphan mapped pin");
  db.Execute("CREATE INDEX temp.endpoints_bit ON endpoints(bit,driver,major,owner,port,ordinal)");
  auto counts=db.Prepare("SELECT bit,sum(driver),sum(1-driver) FROM endpoints GROUP BY bit ORDER BY bit");
  while(counts.Step()) {
    auto bit=counts.TypedInteger(0),drivers=counts.TypedInteger(1),sinks=counts.TypedInteger(2);
    if(drivers!=1) {
      if(!drivers && declared.count(bit))++graph.undriven_bits;
      else Check(!sinks,"timing bit lacks one driver: "+std::to_string(bit));
    }
  }
  auto wires=db.Prepare("SELECT d.node,s.node,s.delay FROM endpoints d JOIN endpoints s USING(bit) WHERE d.driver=1 AND s.driver=0 ORDER BY d.bit,s.major,s.owner,s.port,s.ordinal");
  while(wires.Step())graph.EdgeTo(wires.TypedInteger(0),wires.TypedInteger(1),wires.TypedReal(2));
  graph.Freeze();staging.Commit();return graph;
}

JsonRecord Measure(Graph& graph,const ArtifactRef& identity,unsigned kind,const std::string& header,
                   const std::vector<ArtifactRef>& inputs,const std::string& output) {
  SqliteStore source(identity);auto& id=source.database();RequireSqliteDDL(id,kind==2?kBoundaryIdentityDDL:kind==3?kLogicSegmentIdentityDDL:kLocalPathTimingDDL);
  auto ownership=id.Prepare("SELECT json_extract(?1,'$.design')=json_extract(?2,'$.design'),json_extract(?1,'$.fpga')=json_extract(?2,'$.fpga')");
  ownership.BindText(1,Metadata(id,"header"));ownership.BindText(2,header);
  Check(ownership.Step() && ownership.TypedInteger(0)==1 && ownership.TypedInteger(1)==1,"projection identity disagrees");
  SqliteArtifactWriter writer(output,kind==2?"boundary-timing-store-v1":kind==3?"logic-segment-timing-store-v1":"local-path-timing-store-v1",inputs);auto& db=writer.database();
  db.Execute(kind==2?kBoundaryTimingDDL:kind==3?kLogicSegmentTimingDDL:kLocalPathTimingDDL);
  db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-8192; PRAGMA temp.cache_size=-8192");
  {auto attach=db.Prepare("ATTACH DATABASE ? AS identity");attach.BindText(1,SqliteImmutableUri(identity.path()));attach.Execute();}
  SqliteTransaction transaction(db);db.Execute("CREATE TEMP TABLE queries(id TEXT PRIMARY KEY,start TEXT NOT NULL,target TEXT NOT NULL,extra REAL NOT NULL) WITHOUT ROWID");
  auto query=db.Prepare("INSERT INTO queries VALUES(?,?,?,?)");
  if(kind==2) {
    db.Execute("INSERT INTO timings SELECT id,kind,schedule_entry,0.0,'pending','pending',CASE WHEN kind='tx' THEN 'logical-source-to-tx-port' ELSE 'rx-port-to-shadow-capture' END FROM identity.endpoints ORDER BY id");
    auto endpoints=id.Prepare("SELECT id,kind,external_port,external_port_bit FROM endpoints ORDER BY id");auto regs=id.Prepare("SELECT instance FROM boundary_registers WHERE endpoint_id=? ORDER BY ordinal");
    while(endpoints.Step()) {
      const auto name=Text(endpoints,0),type=Text(endpoints,1),port=Text(endpoints,2);const auto bit=endpoints.TypedInteger(3);
      std::string node;for(const auto& candidate:std::vector<std::string>{"top:"+port,"top:"+port+"["+std::to_string(bit)+"]"})if(graph.Connected(candidate)){Check(node.empty(),"boundary top port is ambiguous");node=candidate;}
      Check(!node.empty(),"boundary top port is absent");
      auto io=graph.io.find(std::make_tuple(port,bit,type));Check(graph.io.empty() || io!=graph.io.end(),"boundary lacks fixed I/O timing");
      std::string start,target;double extra=0;
      if(type=="tx"){target=node;if(io!=graph.io.end())extra=io->second;}
      else {
        Check(type=="rx","boundary kind is invalid");regs.Reset();regs.BindText(1,name);Check(regs.Step(),"RX boundary register is absent");target="cell:"+Text(regs,0)+"/D";Check(!regs.Step(),"RX boundary register is ambiguous");start=node;
      }
      query.BindText(1,name);query.BindText(2,start);query.BindText(3,target);query.BindReal(4,extra);query.Execute();
    }
  } else if(kind==3) {
    db.Execute("INSERT INTO segments SELECT seq,segment_id,kind,system_path,member_path,cut_index,fpga,replace_tx_endpoint,start_pin,end_pin,0.0,static_exact_segment_id,start_object_kind,end_object_kind,cone_anchor_object_kind,cone_anchor_pin,'endpoint-exact',start_pin,end_pin FROM identity.segments ORDER BY seq");
    db.Execute("INSERT INTO unsupported_members SELECT * FROM identity.unsupported_members ORDER BY seq");
    db.Execute("INSERT INTO queries SELECT segment_id,start_pin,end_pin,0.0 FROM identity.segments ORDER BY seq");
  } else {
    auto checks=id.Prepare("SELECT json_extract(?,'$.qualification'),(SELECT count(*) FROM timings WHERE delay_ns!=0.0)");checks.BindText(1,Metadata(id,"header"));
    Check(checks.Step() && checks.Text(0)=="typed-local-path-query-identity-owner-v1" && checks.TypedInteger(1)==0,"local identity is not an unmeasured typed owner");
    db.Execute("INSERT INTO timings SELECT * FROM identity.timings ORDER BY seq; INSERT INTO path_pins SELECT * FROM identity.path_pins ORDER BY path_id,ordinal");
    db.Execute("INSERT INTO queries SELECT id,start_pin,end_pin,0.0 FROM identity.timings ORDER BY seq");
  }
  db.Execute("CREATE INDEX temp.queries_start ON queries(start,id)");
  auto rows=db.Prepare("SELECT id,start,target,extra FROM queries ORDER BY start,id");
  auto update=db.Prepare(kind==2?"UPDATE timings SET delay_ns=?,start_object=?,end_object=? WHERE id=?":kind==3?"UPDATE segments SET delay_ns=?,actual_start_object=?,actual_end_object=? WHERE segment_id=?":"UPDATE timings SET delay_ns=? WHERE id=?");
  std::string previous;bool first=true;
  while(rows.Step()) {
    const auto name=Text(rows,0),start=rows.TypedText(1),target=Text(rows,2);
    if(first || start!=previous){graph.Solve(start);previous=start;first=false;}
    const auto result=graph.Result(target);const auto delay=result.first+rows.TypedReal(3);Check(std::isfinite(delay) && delay>=0,"measured delay is invalid");update.BindReal(1,delay);
    if(kind==4)update.BindText(2,name);else {update.BindText(2,result.second);update.BindText(3,target);update.BindText(4,name);}update.Execute();
  }
  const auto owner=kind==3?Metadata(id,"source"):"null";
  const auto seal=SealPhysicalEvidence(db,kind,header,owner);transaction.Commit();rows=SqliteStatement{};update=SqliteStatement{};query=SqliteStatement{};writer.Commit();
  return Object{{"kind",kind},{"header_json",seal.header},{"counts_json",seal.counts},{"source_json",seal.source},{"logical_sha256",seal.logical_sha256},{"maximum_delay_ns",seal.maximum_delay_ns},{"measurement_counts_json",seal.measurement_counts},{"missing_segments",0}};
}
}

void BuildRoutedSegments(const std::vector<ArtifactRef>& inputs,const std::string& directory,const std::string& summary) {
  Check(inputs.size()>=6 && inputs.size()<=8,"input coverage is invalid");CheckedRequestReader request(inputs.back().path(),16*1024*1024);
  Check(request.Read<std::uint64_t>()==0x31544752464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  const auto flags=request.Read<std::uint32_t>();Check(flags<=3 && inputs.size()==6+unsigned(bool(flags&1))+unsigned(bool(flags&2)),"projection coverage is invalid");
  for(std::size_t i=0;i+1<inputs.size();++i)if(!(ReadFileGeneration(request)==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"routed timing source generation changed");
  std::vector<std::string> headers;for(int i=0;i<3;++i)headers.push_back(request.ReadString32(true,4*1024*1024));request.RequireEnd();
  {
    SqliteStore identity(inputs[4]);auto compare=identity.database().Prepare("SELECT json_extract(?1,'$.design')=json_extract(?2,'$.design'),json_extract(?1,'$.fpga')=json_extract(?2,'$.fpga')");
    for(unsigned kind=2;kind<=4;++kind) {
      const bool active=kind==2 || (kind==3?flags&1:flags&2);
      Check(active!=headers[kind-2].empty(),"header projection coverage disagrees");if(!active)continue;
      compare.Reset();compare.BindText(1,headers[0]);compare.BindText(2,headers[kind-2]);
      Check(compare.Step() && compare.TypedInteger(0)==1 && compare.TypedInteger(1)==1,"projection design/FPGA namespaces disagree");
    }
  }
  auto graph=BuildGraph(inputs);std::vector<JsonRecord> results;std::size_t next=4;
  for(unsigned kind=2;kind<=4;++kind) {
    if(kind==3 && !(flags&1))continue;if(kind==4 && !(flags&2))continue;
    const auto filename=kind==2?"boundary.sqlite":kind==3?"logic.sqlite":"local.sqlite";
    results.push_back(Measure(graph,inputs[next++],kind,headers[kind-2],inputs,(std::filesystem::path(directory)/filename).string()));
  }
  AtomicBinaryWriter result(summary,inputs);result.stream()<<JsonRecord(Object{{"schema","emuflow.routed-segment-summary/v1"},{"status","pass"},{"implementation_id","native-routed-segments-v1"},{"projections",std::move(results)},{"nodes",graph.names.size()},{"edges",graph.edge_count},{"solves",graph.solves},{"visited_nodes",graph.visited},{"undriven_bit_count",graph.undriven_bits}}).EncodeUtf8()<<'\n';result.Commit();
}
}
