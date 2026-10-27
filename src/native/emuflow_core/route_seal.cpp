#include "emuflow_core/route_seal.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/hash.hpp"
#include <array>
#include <memory>
#include <algorithm>
#include <string_view>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool condition,const char* message) {
  if (!condition) throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Hash(Sha256& hash,std::string_view value) {
  hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());
}
std::string Hex(Sha256& hash) {
  static const char* digits="0123456789abcdef";
  std::string value;value.reserve(64);
  for (auto byte:hash.finish()) {value+=digits[byte>>4];value+=digits[byte&15];}
  return value;
}
const std::array<std::string,5> names{"demands","routes","timing_paths","master_selections","refinement_batch_paths"};
const std::array<std::vector<std::string>,5> paths{{
  {"demands"},{"routes"},{"timing","paths"},
  {"joint_optimization","candidate_generation","master_selection"},
  {"joint_optimization","refinement_batches","batches"}}};
struct Tree {
  std::uint8_t tag=0,relation=0;
  std::string literal;
  std::uint64_t count=0;
  std::vector<std::pair<std::string,std::unique_ptr<Tree>>> children;
};
std::unique_ptr<Tree> ReadTree(CheckedRequestReader& request,SqliteDatabase& db,
    std::vector<std::string>& path,std::array<bool,5>& seen,std::array<std::uint64_t,5>& counts,unsigned& nodes) {
  Check(++nodes<=65536 && path.size()<=64,"route seal request tree exceeds bound");
  auto tree=std::make_unique<Tree>();tree->tag=request.Read<std::uint8_t>();
  if (tree->tag==0) {
    tree->literal=request.ReadString32(false,8*1024*1024);
    auto valid=db.Prepare("SELECT json_valid(?)");valid.BindText(1,tree->literal);
    Check(valid.Step() && valid.Integer(0),"route seal literal is invalid JSON");
  } else if (tree->tag==1) {
    const auto count=request.Read<std::uint32_t>();
    Check(count<=65536 && count<=request.remaining()/6,"route seal object is truncated");
    std::string last;
    for (std::uint32_t i=0;i<count;++i) {
      auto key=request.ReadString32(true);
      Check(!i || last<key,"route seal object keys repeat or are unordered");last=key;
      path.push_back(key);auto child=ReadTree(request,db,path,seen,counts,nodes);path.pop_back();
      tree->children.emplace_back(std::move(key),std::move(child));
    }
  } else {
    Check(tree->tag==2,"route seal request node tag is invalid");
    tree->relation=request.Read<std::uint8_t>();
    Check(tree->relation<5 && !seen[tree->relation] && path==paths[tree->relation],
          "route seal relation location repeats or disagrees");
    seen[tree->relation]=true;tree->count=request.Read<std::uint64_t>();counts[tree->relation]=tree->count;
  }
  return tree;
}
std::string Projection(const Tree& tree) {
  if (tree.tag==0) return tree.literal;
  Check(tree.tag==1,"route seal header projection is invalid");
  std::string value="{";bool first=true;
  for (const auto& item:tree.children) {
    if (item.second->tag==2) continue;
    if (!first) value+=',';
    first=false;
    value+=JsonQuote(item.first)+":"+Projection(*item.second);
  }
  return value+'}';
}
struct Section {
  std::uint64_t count=0,batches=0,observed=0;
  std::string expected;
  Sha256 hash;
};
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"route seal metadata is missing");auto value=row.TypedText(0);
  Check(!row.Step(),"route seal metadata repeats");return value;
}
std::array<Section,5> Sections(SqliteDatabase& db) {
  const auto encoded=Metadata(db,"sections");std::array<Section,5> result;
  for (std::size_t i=0;i<5;++i) {
    auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2),json_extract(?1,?3)");
    row.BindText(1,encoded);row.BindText(2,"$."+names[i]+".records");row.BindText(3,"$."+names[i]+".sha256");
    Check(row.Step() && row.Text(0)=="integer" && row.Integer(1)>=0,"route seal record count is invalid");
    result[i].count=static_cast<std::uint64_t>(row.Integer(1));
    if (!row.IsNull(2)) {
      result[i].expected=row.TypedText(2);
      Check(result[i].expected.size()==64 && std::all_of(result[i].expected.begin(),result[i].expected.end(),
           [](char c){return (c>='0' && c<='9') || (c>='a' && c<='f');}),"route seal source digest is invalid");
    }
    Check((i==1 || i==3) || !result[i].expected.empty(),"route seal producer section certificate is missing");
    if (i==4) {
      auto batches=db.Prepare("SELECT json_type(?1,'$.refinement_batch_paths.batches'),json_extract(?1,'$.refinement_batch_paths.batches')");
      batches.BindText(1,encoded);Check(batches.Step() && batches.Text(0)=="integer" && batches.Integer(1)>=0,"route seal batch count is invalid");
      result[i].batches=static_cast<std::uint64_t>(batches.Integer(1));
    }
  }
  Check(result[0].count==result[1].count,"route seal demand/route coverage disagrees");
  Check(!result[3].count || result[3].count==result[1].count,"route seal selection coverage disagrees");
  return result;
}
// One route plus its board-sized children at a time. All joins are ordered
// cursors; no per-route SQL lookup or circuit-sized ID/container is constructed.
class RouteCursor {
 public:
  RouteCursor(SqliteDatabase& db,bool demand):demand_(demand),
    routes_(db.Prepare(std::string("SELECT r.seq,r.demand_id,r.net_id,r.source_fpga,r.width_bits,r.transport_round,r.cut_class,r.dependency_level,r.combinational_dependency_depth,r.max_latency_cycles,r.predicted_max_delay_ns,d.seq,d.demand_id,d.route_seq,typeof(r.predicted_max_delay_ns) ")
       +std::string(demand ? "FROM demands d LEFT JOIN routes r ON d.route_seq=r.seq ORDER BY d.seq" : "FROM routes r LEFT JOIN demands d ON d.route_seq=r.seq ORDER BY r.seq"))),
    sinks_(db.Prepare("SELECT route_seq,seq,sink_fpga FROM route_sinks ORDER BY route_seq,seq")),
    edges_(demand?SqliteStatement{}:db.Prepare("SELECT route_seq,seq,link_id,source_fpga,sink_fpga FROM route_edges ORDER BY route_seq,seq")),
    predecessors_(db.Prepare("SELECT route_seq,seq,predecessor_net FROM route_predecessors ORDER BY route_seq,seq")) {
    have_sink_=sinks_.Step();have_edge_=!demand_ && edges_.Step();have_predecessor_=predecessors_.Step();
  }
  bool Next(Object& value) {
    if (!routes_.Step()) {
      Check(!have_sink_ && !have_edge_ && !have_predecessor_,"route seal has orphan child records");return false;
    }
    const auto seq=routes_.TypedInteger(0);
    Check(seq==static_cast<std::int64_t>(position_++) && routes_.TypedInteger(11)==seq && routes_.TypedInteger(13)==seq && routes_.TypedText(12)==routes_.TypedText(1),"route seal demand/route sequence disagrees");
    value.clear();
    for (const auto& item:std::array<std::pair<const char*,int>,3>{{{"id",1},{"net",2},{"source",3}}}) {
      const auto text=routes_.TypedText(item.second);Check(!text.empty(),"route seal identity is empty");value.emplace(item.first,text);
    }
    const auto width=routes_.TypedInteger(4);Check(width>0,"route seal width is invalid");value.emplace("width_bits",width);
    const auto integer=[&](const char* key,int column) {
      if (!routes_.IsNull(column)) {auto n=routes_.TypedInteger(column);Check(n>=0,"route seal scalar is negative");value.emplace(key,n);}
    };
    integer("transport_round",5);
    if (!routes_.IsNull(6)) {
      auto kind=routes_.TypedText(6);Check(kind=="combinational" || kind=="register_input" || kind=="register_output","route seal cut class is invalid");value.emplace("cut_class",kind);
    }
    Array sinks,edges,parents;
    std::uint64_t ordinal=0;
    while (have_sink_ && sinks_.TypedInteger(0)==seq) {
      Check(sinks_.TypedInteger(1)==static_cast<std::int64_t>(ordinal++),"route seal sink order disagrees");
      auto text=sinks_.TypedText(2);Check(!text.empty(),"route seal sink identity is empty");sinks.emplace_back(text);have_sink_=sinks_.Step();
    }
    ordinal=0;
    while (have_edge_ && edges_.TypedInteger(0)==seq) {
      Check(edges_.TypedInteger(1)==static_cast<std::int64_t>(ordinal++),"route seal edge order disagrees");
      auto link=edges_.TypedText(2),from=edges_.TypedText(3),to=edges_.TypedText(4);
      Check(!link.empty() && !from.empty() && !to.empty(),"route seal edge identity is empty");
      edges.emplace_back(Object{{"link",link},{"from",from},{"to",to}});have_edge_=edges_.Step();
    }
    ordinal=0;
    while (have_predecessor_ && predecessors_.TypedInteger(0)==seq) {
      Check(predecessors_.TypedInteger(1)==static_cast<std::int64_t>(ordinal++),"route seal predecessor order disagrees");
      auto text=predecessors_.TypedText(2);Check(!text.empty(),"route seal predecessor identity is empty");parents.emplace_back(text);have_predecessor_=predecessors_.Step();
    }
    value.emplace("sinks",std::move(sinks));
    Check(routes_.IsNull(7)==routes_.IsNull(8),"route seal dependency scalars are incomplete");
    if (!routes_.IsNull(7)) {
      integer("dependency_level",7);integer("combinational_dependency_depth",8);value.emplace("predecessor_cut_nets",std::move(parents));
    } else Check(parents.empty(),"route seal non-exact route has predecessors");
    if (!demand_) {
      value.emplace("tree_edges",std::move(edges));integer("max_latency_cycles",9);
      if (!routes_.IsNull(10)) {
        Check(routes_.Text(14)=="integer" || routes_.Text(14)=="real","route seal delay type is invalid");
        const auto delay=routes_.Real(10);Check(std::isfinite(delay) && delay>=0,"route seal delay is invalid");value.emplace("predicted_max_delay_ns",delay);
      }
    }
    return true;
  }
 private:
  bool demand_,have_sink_,have_edge_,have_predecessor_;
  std::uint64_t position_=0;
  SqliteStatement routes_,sinks_,edges_,predecessors_;
};
class Sealer {
 public:
  Sealer(SqliteDatabase& db,std::array<Section,5>& sections):db_(db),sections_(sections) {}
  void TreeValue(const Tree& node,unsigned level=0) {
    if (node.tag==0) {Pretty(node.literal,level);return;}
    if (node.tag==2) {Relation(node.relation,level);return;}
    Out("{");
    for (std::size_t i=0;i<node.children.size();++i) {
      Out(i?",\n":"\n");Indent(level+1);Out(JsonQuote(node.children[i].first));Out(": ");TreeValue(*node.children[i].second,level+1);
    }
    if (!node.children.empty()) {Out("\n");Indent(level);}Out("}");
  }
  std::string Finish() {Out("\n");return Hex(logical_);}
  void EmptyAbsent(unsigned kind) {
    Check(!sections_[kind].count && !sections_[kind].batches,"route seal logical relation is missing");
    // Still inspect the physical relation: a false zero count must not hide rows.
    auto row=db_.Prepare("SELECT 1 FROM "+std::string(kind==0?"demands":kind==1?"routes":kind==2?"timing_paths":kind==3?"route_master_selections":"refinement_batch_paths")+" LIMIT 1");
    Check(!row.Step(),"route seal absent relation has records");
  }
 private:
  void Out(std::string_view text) {Hash(logical_,text);}
  void Indent(unsigned level) {Out(std::string(2*level,' '));}
  void Pretty(const std::string& value,unsigned level) {VisitPrettyCanonicalJson(value,level,[&](auto text){Out(text);});}
  void Record(unsigned kind,const std::string& compact,unsigned level) {
    auto& s=sections_[kind];Check(s.observed<s.count,"route seal has extra records");
    Hash(s.hash,compact);Hash(s.hash,"\n");
    Out(s.observed++?",\n":"\n");Indent(level+1);Pretty(compact,level+1);
  }
  void Relation(unsigned kind,unsigned level) {
    auto& s=sections_[kind];Out("[");
    if (kind<2) {
      RouteCursor cursor(db_,kind==0);Object record;
      while(cursor.Next(record)) Record(kind,JsonRecord(std::move(record)).Encode(),level);
    } else if (kind==2) {
      auto rows=db_.Prepare("SELECT seq,path_id,record_json,json_valid(record_json),json_extract(record_json,'$.path'),json_extract(record_json,'$.id') FROM timing_paths ORDER BY seq");
      while (rows.Step()) {
        Check(rows.TypedInteger(0)==static_cast<std::int64_t>(s.observed) && rows.Integer(3),"route seal timing record order/JSON is invalid");
        const auto identity=rows.IsNull(4)?rows.Text(5):rows.Text(4);Check(rows.TypedText(1)==identity && !identity.empty(),"route seal timing identity disagrees");
        Record(kind,rows.TypedText(2),level);
      }
    } else if (kind==3) {
      auto rows=db_.Prepare("SELECT s.seq,s.demand_id,s.generator,r.demand_id FROM route_master_selections s LEFT JOIN routes r ON s.seq=r.seq ORDER BY s.seq");
      while(rows.Step()) {
        Check(rows.TypedInteger(0)==static_cast<std::int64_t>(s.observed) && rows.TypedText(1)==rows.TypedText(3) && !rows.TypedText(2).empty(),"route seal master selection disagrees");
        Record(kind,JsonRecord(Object{{"demand",rows.TypedText(1)},{"generator",rows.TypedText(2)}}).Encode(),level);
      }
    } else {
      auto rows=db_.Prepare("SELECT seq,batch_index,position,path_index FROM refinement_batch_paths ORDER BY seq");bool have=rows.Step();
      for (std::uint64_t batch=0;batch<s.batches;++batch) {
        Out(batch?",\n":"\n");Indent(level+1);Out("[");std::uint64_t position=0;
        while(have && rows.TypedInteger(1)==static_cast<std::int64_t>(batch)) {
          const auto path=rows.TypedInteger(3);
          Check(rows.TypedInteger(0)==static_cast<std::int64_t>(s.observed) && rows.TypedInteger(2)==static_cast<std::int64_t>(position) && path>=0 && static_cast<std::uint64_t>(path)<sections_[2].count && s.observed<s.count,"route seal refinement order/coverage disagrees");
          const auto record=JsonRecord(Object{{"batch",batch},{"position",position},{"path",path}}).Encode();Hash(s.hash,record);Hash(s.hash,"\n");++s.observed;
          Out(position?",\n":"\n");Indent(level+2);Out(std::to_string(path));++position;have=rows.Step();
        }
        if(position) {Out("\n");Indent(level+1);}Out("]");
      }
      Check(!have,"route seal refinement has extra records");
    }
    Check(s.observed==s.count,"route seal record coverage disagrees");
    if ((kind==4?s.batches:s.count)) {Out("\n");Indent(level);}Out("]");
  }
  SqliteDatabase& db_;std::array<Section,5>& sections_;Sha256 logical_;
};
}
std::string SealSystemRouteDatabase(SqliteDatabase& db,const std::string& request_path) {
  CheckedRequestReader request(request_path,8*1024*1024);
  Check(request.Read<std::uint64_t>()==UINT64_C(0x31515352464d5545) && request.Read<std::uint32_t>()==1,"route seal request version disagrees");
  std::array<bool,5> seen{};std::array<std::uint64_t,5> counts{};std::vector<std::string> path;unsigned nodes=0;
  auto tree=ReadTree(request,db,path,seen,counts,nodes);request.RequireEnd();
  Check(tree->tag==1 && seen[0] && seen[1] && Projection(*tree)==Metadata(db,"header"),"route seal logical header disagrees with its owner");
  for (const auto* sql : {
      "SELECT 1 FROM route_predecessors p LEFT JOIN routes r ON p.predecessor_net=r.net_id WHERE r.seq IS NULL LIMIT 1",
      "SELECT 1 FROM refinement_batch_paths GROUP BY path_index HAVING count(*)>1 LIMIT 1"}) {
    auto invalid=db.Prepare(sql);Check(!invalid.Step(),"route seal has orphan or repeated path references");
  }
  auto sections=Sections(db);
  for (unsigned kind=0;kind<5;++kind)
    if (seen[kind]) Check(counts[kind]==(kind==4?sections[kind].batches:sections[kind].count),
                         "route seal requested population count disagrees");
  Sealer sealer(db,sections);sealer.TreeValue(*tree);const auto logical=sealer.Finish();Object result;
  for (unsigned kind=0;kind<5;++kind) {
    if (!seen[kind]) sealer.EmptyAbsent(kind);
    auto& s=sections[kind];const auto digest=Hex(s.hash);
    if (!s.expected.empty() && s.expected!=digest)
      throw StageError(ErrorCode::kInvalidArtifact,"route seal source section digest disagrees: "+names[kind]);
    Object item{{"records",s.count},{"sha256",digest}};if(kind==4)item.emplace("batches",s.batches);result.emplace(names[kind],std::move(item));
  }
  const auto summary=JsonRecord(Object{{"schema","emuflow.system-route-seal/v1"},{"implementation_id","native-route-store-seal-v1"},{"logical_sha256",logical},{"sections",std::move(result)}}).Encode()+"\n";
  return summary;
}
void SealSystemRoutes(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==2,"route seal source coverage disagrees");
  SqliteStore store(inputs[0]);auto& db=store.database();db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-2048");
  const auto summary=SealSystemRouteDatabase(db,inputs[1].path());
  AtomicBinaryWriter writer(output,inputs);writer.WriteBytes(summary.data(),summary.size());writer.Commit();
}
}
