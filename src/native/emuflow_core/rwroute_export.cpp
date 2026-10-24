#include "emuflow_core/store.hpp"
#include "emuflow_core/rwroute_export.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/sqlite.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <map>
#include <optional>
#include <set>
#include <limits>
#include <unordered_map>
#include <vector>

namespace emuflow::core {
namespace {
using Integer = std::int64_t;
using Clock = std::chrono::steady_clock;
double Seconds(Clock::time_point start) {
  return std::chrono::duration<double>(Clock::now()-start).count();
}
void Check(bool valid, const std::string& message) {
  if (!valid) throw StageError(ErrorCode::kInvalidArtifact, message);
}
void Token(const std::string& value, bool allow_empty = false) {
  Check((allow_empty || !value.empty()) && value.find_first_of("\t\r\n\0",0,4)==std::string::npos,
        "RWRoute identity/property is not TSV-safe");
  // Validate UTF-8 without changing the output's original spelling.
  (void)JsonQuote(value);
}
void Digest(const std::string& value) {
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,
        "RWRoute source digest is invalid");
}
FileGeneration ReadGeneration(CheckedRequestReader& reader) {
  FileGeneration value;
  value.device=reader.Read<std::uint64_t>(); value.inode=reader.Read<std::uint64_t>();
  value.bytes=reader.Read<std::uint64_t>();
  value.modified_seconds=reader.Read<std::int64_t>(); value.modified_nanoseconds=reader.Read<std::int64_t>();
  value.changed_seconds=reader.Read<std::int64_t>(); value.changed_nanoseconds=reader.Read<std::int64_t>();
  return value;
}
std::string Metadata(SqliteDatabase& db, const std::string& key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?"); row.BindText(1,key);
  Check(row.Step(),"mapped index metadata is missing: "+key);
  const auto value=row.TypedText(0);
  Check(!row.Step(),"mapped index metadata repeats: "+key);
  return value;
}
std::string JsonString(SqliteDatabase& db, const std::string& json, const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");
  row.BindText(1,json); row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="text","RWRoute JSON identity is invalid: "+path);
  return row.TypedText(1);
}
// Binary Yosys parameter strings can exceed uint64. Preserve their exact
// decimal meaning without narrowing to machine integers or floating point.
std::string BinaryDecimal(const std::string& bits) {
  std::string digits="0";
  for (char bit:bits) {
    unsigned carry=bit-'0';
    for (auto i=digits.rbegin(); i!=digits.rend(); ++i) {
      const auto value=2*static_cast<unsigned>(*i-'0')+carry;
      *i=static_cast<char>('0'+value%10); carry=value/10;
    }
    if (carry) digits.insert(digits.begin(),static_cast<char>('0'+carry));
  }
  return digits;
}
// JSON1 is the parser/type authority. Locate only the already validated raw
// member span when an integer is too large for SQLite's signed scalar API;
// converting its coerced REAL back to text would lose parameter bits.
std::string IntegerToken(SqliteDatabase& db, const std::string& object, const std::string& name) {
  std::size_t position=1;
  const auto whitespace=[&]() {
    while (position<object.size() && (object[position]==' ' || object[position]=='\n' ||
           object[position]=='\r' || object[position]=='\t')) ++position;
  };
  const auto string_end=[&]() {
    Check(position<object.size() && object[position]=='"',"invalid parameter member token");
    ++position;
    while (position<object.size()) {
      const auto ch=object[position++];
      if (ch=='\\') { Check(position<object.size(),"truncated parameter string"); ++position; }
      else if (ch=='"') return;
    }
    Check(false,"truncated parameter member token");
  };
  while (position<object.size()) {
    whitespace(); if (object[position]=='}') break;
    const auto begin=position; string_end();
    const auto key=JsonString(db,object.substr(begin,position-begin),"$");
    whitespace(); Check(position<object.size() && object[position++]==':',"invalid parameter member");
    whitespace(); const auto start=position;
    unsigned depth=0;
    while (position<object.size()) {
      const auto ch=object[position];
      if (ch=='"') { string_end(); continue; }
      if (ch=='[' || ch=='{') ++depth;
      else if (ch==']' || ch=='}') { if (!depth) break; --depth; }
      else if (ch==',' && !depth) break;
      ++position;
    }
    if (key==name) {
      auto token=object.substr(start,position-start);
      while (!token.empty() && (token.back()==' ' || token.back()=='\r' || token.back()=='\n' || token.back()=='\t'))
        token.pop_back();
      Check(!token.empty() && token.find_first_not_of("-0123456789")==std::string::npos,
            "invalid primitive integer parameter token");
      return token=="-0" ? "0" : token;
    }
    if (position<object.size() && object[position]==',') ++position;
    else break;
  }
  throw StageError(ErrorCode::kInvalidArtifact,"primitive integer parameter token is missing");
}
std::optional<std::string> Parameter(SqliteDatabase& db, SqliteStatement& query, const std::string& record,
                                     const std::string& name) {
  query.Reset(); query.BindText(1,record); query.BindText(2,"$.parameters."+name);
  Check(query.Step(),"primitive parameter query failed");
  Check(query.Text(5)=="object","indexed mapped primitive record is invalid");
  const auto parameters=query.Text(0), kind=query.Text(1);
  Check(parameters.empty() || parameters=="object","primitive parameters have invalid type");
  if (kind.empty() || kind=="null") return std::nullopt;
  std::string value;
  if (kind=="true") value="1";
  else if (kind=="false") value="0";
  else if (kind=="integer") {
    value=query.Text(3)=="integer" ? query.Text(2) : IntegerToken(db,query.TypedText(4),name);
  } else {
    Check(kind=="text","primitive parameter '"+name+"' has invalid type");
    value=query.TypedText(2);
    if (!value.empty() && value.find_first_not_of("01")==std::string::npos)
      value=BinaryDecimal(value);
  }
  Token(value,true);
  return value;
}
constexpr std::uint64_t kNoOrdinal=std::numeric_limits<std::uint64_t>::max();
constexpr std::uint32_t kNoCell=std::numeric_limits<std::uint32_t>::max();
constexpr std::uint32_t kDual=1,kCarry=2,kFdre=4,kGate=8;
struct Cell {
  std::uint64_t ordinal=kNoOrdinal;
  std::uint32_t site=0, flags=0;
};
static_assert(sizeof(Cell)==16,"RWRoute source catalog must remain compact");
// Source seq belongs to this mapped artifact. Current producers use dense
// zero-based seq; older/custom v2 stores may use sparse nonnegative integers.
// Adapt those coordinates in compact sorted integers, never a name dictionary.
class CellCatalog {
 public:
  explicit CellCatalog(SqliteDatabase& db) {
    auto shape=db.Prepare("SELECT count(*),coalesce(min(seq),0),coalesce(max(seq),-1) FROM cells");
    Check(shape.Step(),"mapped cell catalog shape is missing");
    const auto count=shape.TypedInteger(0),minimum=shape.TypedInteger(1),maximum=shape.TypedInteger(2);
    Check(count>=0 && static_cast<std::uint64_t>(count)<=kNoCell && minimum>=0,
          "mapped cell sequence is invalid");
    entries_.resize(static_cast<std::size_t>(count));
    if (minimum!=0 || maximum!=count-1) {
      source_ids_.reserve(entries_.size());
      auto rows=db.Prepare("SELECT seq FROM cells ORDER BY seq");
      while (rows.Step()) {
        const auto value=rows.TypedInteger(0);
        Check(value>=0 && (source_ids_.empty() || source_ids_.back()<value),"mapped cell sequence repeats");
        source_ids_.push_back(value);
      }
      Check(source_ids_.size()==entries_.size(),"mapped cell source coverage disagrees");
    }
  }
  std::uint32_t Index(Integer value) const {
    if (source_ids_.empty()) {
      Check(value>=0 && static_cast<std::uint64_t>(value)<entries_.size(),"mapped cell sequence is invalid");
      return static_cast<std::uint32_t>(value);
    }
    const auto found=std::lower_bound(source_ids_.begin(),source_ids_.end(),value);
    Check(found!=source_ids_.end() && *found==value,"mapped cell sequence is invalid");
    return static_cast<std::uint32_t>(found-source_ids_.begin());
  }
  Integer Source(std::uint32_t value) const { return source_ids_.empty() ? value : source_ids_.at(value); }
  Cell& operator[](std::uint32_t value) { return entries_[value]; }
  const Cell& operator[](std::uint32_t value) const { return entries_[value]; }
  std::size_t size() const { return entries_.size(); }
  std::size_t bytes() const { return entries_.size()*sizeof(Cell)+source_ids_.size()*sizeof(Integer); }
 private:
  std::vector<Cell> entries_;
  std::vector<Integer> source_ids_;
};

struct Io {
  std::string key, id, direction, p_port, n_port, p_pin, n_pin, p_site, n_site, port, standard;
  Integer bit=0,width=0,net=0,ordinal=0;
  std::uint32_t site=0;
};
struct Endpoint {
  Integer net=0,bit=0,width=0;
  std::string port;
  std::uint32_t source=0;
  bool driver=false,io=false;
};
bool IsClock(const std::string& pin) {
  return pin=="C" || pin=="CLK" || pin=="CLKARDCLK" || pin=="CLKBWRCLK";
}
std::string LogicalPin(const Endpoint& e) {
  return e.width==1 ? e.port : e.port+"["+std::to_string(e.bit)+"]";
}
// Cursor columns: net, source seq, port, bit, width, direction,
// independently joined source-label agreement, I/O namespace tag.
std::optional<Endpoint> ReadEndpoint(SqliteStatement& row,const CellCatalog& cells,
                                     const std::vector<Io>& ios) {
  Endpoint e;e.net=row.TypedInteger(0);e.io=row.TypedInteger(7)!=0;
  const auto source=row.TypedInteger(1);
  Check(!row.IsNull(6) && row.TypedInteger(6)==1,"mapped pin cell sequence/identity disagree");
  if (e.io) {
    Check(source>=0 && static_cast<std::uint64_t>(source)<ios.size(),"RWRoute I/O ordinal is invalid");
    e.source=source;
  } else {
    e.source=cells.Index(source);
    if (cells[e.source].ordinal==kNoOrdinal) return std::nullopt;
  }
  e.port=row.TypedText(2);e.bit=row.TypedInteger(3);e.width=row.TypedInteger(4);
  const auto direction=row.TypedInteger(5);
  Check(e.width>0 && e.bit>=0 && e.bit<e.width && direction>=0 && direction<=2,
        "indexed mapped pin row is invalid");
  Token(e.port);e.driver=direction!=0;
  return e;
}
struct Driver {
  std::uint64_t count=0;
  std::uint32_t source=0;
  bool io=false;
  // Only exact scalar Q/O matter for the existing qualification policy.
  char pin=0;
};
struct ClockNet {
  Driver driver;
  std::uint64_t sinks=0;
  bool invalid_sink=false;
  std::vector<std::uint32_t> candidate_sinks;
};
using ClockNets=std::unordered_map<Integer,ClockNet>;
using CandidateIds=std::unordered_map<std::uint32_t,std::uint32_t>;
struct ClockMetrics {
  std::uint64_t candidates=0,enabled=0,pin_rows=0,selected_nets=0,dependency_edges=0;
};
// Each selected net is reduced once per pass, including its fixed I/O. This
// replaces candidate-by-candidate correlated rescans of the same fanout.
void ReduceClockNets(SqliteDatabase& db,ClockNets& nets,const CellCatalog& cells,
                     const std::vector<Io>& ios,const CandidateIds* candidates,
                     ClockMetrics& metrics) {
  if (nets.empty()) return;
  db.Execute("DELETE FROM rwroute_clock_nets");
  {
    SqliteTransaction transaction(db);
    auto insert=db.Prepare("INSERT INTO rwroute_clock_nets VALUES (?)");
    for (const auto& item:nets) { insert.BindInteger(1,item.first);insert.Execute(); }
    transaction.Commit();
  }
  metrics.selected_nets+=nets.size();
  const auto add=[&](const Endpoint& e) {
    auto& net=nets.at(e.net);
    if (e.driver) {
      ++net.driver.count;net.driver.source=e.source;net.driver.io=e.io;
      net.driver.pin=e.width==1 && e.port=="Q" ? 'Q' : e.width==1 && e.port=="O" ? 'O' : 0;
    } else {
      ++net.sinks;
      if (!candidates || (e.width==1 && IsClock(e.port))) return;
      if (!e.io && e.width==1 && e.port=="I0") {
        const auto found=candidates->find(e.source);
        if (found!=candidates->end()) { net.candidate_sinks.push_back(found->second);return; }
      }
      net.invalid_sink=true;
    }
  };
  {
    auto rows=db.Prepare(R"SQL(
      SELECT p.net_bit,p.cell_seq,p.port,p.port_bit,p.port_width,p.direction,
             c.id=p.cell_id,0
        FROM rwroute_clock_nets n
       CROSS JOIN cell_pins p ON p.net_bit=n.net_bit
        LEFT JOIN cells c ON c.seq=p.cell_seq
       WHERE p.value_kind=0
    )SQL");
    while (rows.Step()) {
      ++metrics.pin_rows;
      const auto endpoint=ReadEndpoint(rows,cells,ios);
      if (endpoint) add(*endpoint);
    }
  }
  for (const auto& io:ios) if (nets.find(io.net)!=nets.end()) {
    ++metrics.pin_rows;
    Endpoint e;e.net=io.net;e.io=true;e.source=io.ordinal;e.width=1;
    e.port=io.direction=="rx" ? "O" : "I";e.driver=io.direction=="rx";add(e);
  }
}
struct ClockCandidate {
  std::uint32_t source;
  Integer root,enable,output;
};
Integer QualifyClocks(SqliteDatabase& db,SqliteStatement& parameters,CellCatalog& cells,
                       const std::vector<Io>& ios,std::ostream& out,ClockMetrics& metrics) {
  std::vector<ClockCandidate> candidates;
  {
    auto rows=db.Prepare(R"SQL(
      SELECT c.seq,c.record_json,
             sum(CASE WHEN p.port='I0' THEN 1 ELSE 0 END),
             sum(CASE WHEN p.port='I1' THEN 1 ELSE 0 END),
             sum(CASE WHEN p.port='O' THEN 1 ELSE 0 END),
             max(CASE WHEN p.port='I0' AND p.port_width=1 AND p.port_bit=0 AND p.value_kind=0 THEN p.net_bit END),
             max(CASE WHEN p.port='I1' AND p.port_width=1 AND p.port_bit=0 AND p.value_kind=0 THEN p.net_bit END),
             max(CASE WHEN p.port='O' AND p.port_width=1 AND p.port_bit=0 AND p.value_kind=0 THEN p.net_bit END)
        FROM cells c LEFT JOIN cell_pins p ON p.cell_id=c.id
       WHERE c.type='LUT2' GROUP BY c.id ORDER BY c.id
    )SQL");
    while (rows.Step()) {
      const auto value=Parameter(db,parameters,rows.TypedText(1),"INIT");
      const auto source=cells.Index(rows.TypedInteger(0));
      if (!value || *value!="8" || cells[source].ordinal==kNoOrdinal ||
          rows.TypedInteger(2)!=1 || rows.TypedInteger(3)!=1 || rows.TypedInteger(4)!=1 ||
          rows.IsNull(5) || rows.IsNull(6) || rows.IsNull(7)) continue;
      candidates.push_back({source,rows.TypedInteger(5),rows.TypedInteger(6),rows.TypedInteger(7)});
    }
  }
  metrics.candidates=candidates.size();
  db.Execute("CREATE TEMP TABLE rwroute_clock_nets(net_bit INTEGER PRIMARY KEY) WITHOUT ROWID");
  {
    ClockNets enable;
    for (const auto& c:candidates) enable.try_emplace(c.enable);
    ReduceClockNets(db,enable,cells,ios,nullptr,metrics);
    candidates.erase(std::remove_if(candidates.begin(),candidates.end(),[&](const auto& c) {
      const auto& driver=enable.at(c.enable).driver;
      return driver.count!=1 || driver.io || driver.pin!='Q' || !(cells[driver.source].flags&kFdre);
    }),candidates.end());
  }
  metrics.enabled=candidates.size();
  CandidateIds identities;
  for (std::uint32_t i=0;i<candidates.size();++i)
    Check(identities.emplace(candidates[i].source,i).second,"clock candidate identity repeats");
  ClockNets nets;
  for (const auto& c:candidates) { nets.try_emplace(c.root);nets.try_emplace(c.output); }
  ReduceClockNets(db,nets,cells,ios,&identities,metrics);
  std::vector<unsigned char> alive(candidates.size());
  std::vector<std::pair<std::uint32_t,std::uint32_t>> dependencies;
  for (std::uint32_t i=0;i<candidates.size();++i) {
    const auto& c=candidates[i];const auto& root=nets.at(c.root);const auto& output=nets.at(c.output);
    auto source=identities.end();
    if (root.driver.count==1 && !root.driver.io && root.driver.pin=='O')
      source=identities.find(root.driver.source);
    alive[i]=(!root.driver.count || source!=identities.end()) && output.sinks && !output.invalid_sink;
    if (alive[i]) {
      if (source!=identities.end()) dependencies.emplace_back(source->second,i);
      for (auto sink:output.candidate_sinks) dependencies.emplace_back(sink,i);
    }
  }
  metrics.dependency_edges=dependencies.size();
  // Reverse dependency CSR computes the same greatest closed subset as the
  // historical repeated SQL deletes, including cycles and one-way constraints.
  std::vector<std::size_t> offsets(candidates.size()+1);
  for (const auto& e:dependencies) ++offsets[e.first+1];
  for (std::size_t i=1;i<offsets.size();++i) offsets[i]+=offsets[i-1];
  auto cursor=offsets;
  std::vector<std::uint32_t> dependents(dependencies.size());
  for (const auto& e:dependencies) dependents[cursor[e.first]++]=e.second;
  std::vector<std::uint32_t> queue;
  for (std::uint32_t i=0;i<alive.size();++i) if (!alive[i]) queue.push_back(i);
  for (std::size_t head=0;head<queue.size();++head) {
    const auto source=queue[head];
    for (std::size_t at=offsets[source];at<offsets[source+1];++at) {
      const auto dependent=dependents[at];
      if (alive[dependent]) { alive[dependent]=false;queue.push_back(dependent); }
    }
  }
  Integer count=0;
  // Candidate order is the source's lexical order, independent of map/hash and
  // work-queue iteration order. No source names need to be retained or resorted.
  for (std::uint32_t i=0;i<alive.size();++i) if (alive[i]) {
    auto& cell=cells[candidates[i].source];cell.flags|=kGate;++count;
    out<<"CLOCK_GATE\tc"<<cell.ordinal<<"\tI0\tI1\tO\tlut2-init-1000-fdre-latched-enable-v1\n";
  }
  db.Execute("DROP TABLE rwroute_clock_nets");
  return count;
}
}  // namespace

void ExportRwroute(const ArtifactRef& mapped_ref, const ArtifactRef& physical_ref,
                   const ArtifactRef& request_ref, const std::string& output,
                   const std::string& summary_path) {
  const auto started=Clock::now();
  for (const auto* ref:{&mapped_ref,&physical_ref,&request_ref}) ref->RequireUnchanged();
  CheckedRequestReader request(request_ref.path(),16*1024*1024);
  Check(request.Read<std::uint64_t>()==0x31515752554d5545ULL && request.Read<std::uint32_t>()==1,
        "unsupported RWRoute export request");
  if (!(ReadGeneration(request)==mapped_ref.generation()) ||
      !(ReadGeneration(request)==physical_ref.generation()))
    throw StageError(ErrorCode::kMixedGeneration,"RWRoute source generation changed");
  const auto top=request.ReadString32(), part=request.ReadString32();
  std::array<std::string,3> digests;
  for (auto& digest:digests) { digest=request.ReadString32(); Digest(digest); }
  const auto io_digest=request.ReadString32(true);
  if (!io_digest.empty()) Digest(io_digest);
  const auto limit_count=request.Read<std::uint32_t>();
  Check(limit_count<=6,"RWRoute resource budget is invalid");
  std::map<std::string,Integer> limits;
  for (unsigned i=0;i<limit_count;++i) {
    const auto field=request.ReadString32(); const auto count=request.Read<std::uint64_t>();
    Check((field=="lut" || field=="ff" || field=="bram18k" || field=="dsp48" ||
           field=="carry8" || field=="uram288") && count<=INT64_MAX &&
           limits.emplace(field,static_cast<Integer>(count)).second,"RWRoute resource budget is invalid");
  }
  const auto io_count=request.Read<std::uint32_t>();
  Check(io_count<=65536 && (!io_digest.empty() || !io_count),"RWRoute I/O population is invalid");
  std::vector<Io> ios; ios.reserve(io_count);
  for (unsigned i=0;i<io_count;++i) {
    Io io; io.key=std::string(1,'\0')+"IO"+std::to_string(i);
    for (auto* value:{&io.id,&io.direction,&io.p_port,&io.n_port,&io.p_pin,&io.n_pin,
                      &io.p_site,&io.n_site,&io.port,&io.standard}) {
      *value=request.ReadString32(); Token(*value);
    }
    const auto bit=request.Read<std::uint64_t>(), width=request.Read<std::uint64_t>();
    Check(bit<width && width<=INT64_MAX && (io.direction=="tx" || io.direction=="rx"),
          "RWRoute virtual I/O logical top-port binding is invalid");
    io.bit=bit; io.width=width; ios.push_back(std::move(io));
  }
  request.RequireEnd(); Token(top); Token(part);
  SqliteStore mapped_store(mapped_ref);
  auto& db=mapped_store.database();
  db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-16384; PRAGMA temp.cache_size=-16384");
  auto attach=db.Prepare("ATTACH DATABASE ? AS physical");
  attach.BindText(1,SqliteImmutableUri(physical_ref.path())); attach.Execute();
  Check(Metadata(db,"schema")=="emuflow.xilinx-mapped-index/v2","RWRoute requires mapped index v2");
  Check(JsonString(db,Metadata(db,"top_json"),"$")==top,"RWRoute mapped top disagrees");
  {
    auto row=db.Prepare("SELECT schema,assignments FROM physical.manifest WHERE singleton=1");
    Check(row.Step() && row.TypedText(0)=="emuflow.xilinx-physical-handoff-sqlite/v1",
          "RWRoute physical handoff schema is invalid");
    const auto expected=row.TypedInteger(1);
    auto counts=db.Prepare("SELECT count(*),coalesce(sum(c.id IS NULL OR c.type!=a.cell_type),0) "
      "FROM physical.assignment a LEFT JOIN cells c ON c.id=a.instance");
    Check(counts.Step() && counts.TypedInteger(0)==expected && counts.TypedInteger(1)==0,
          "mapped index and physical handoff cell ownership disagree");
    auto documents=db.Prepare("SELECT name,value_json FROM physical.document ORDER BY name");
    Check(documents.Step() && documents.Text(0)=="packed" &&
          JsonString(db,documents.Text(1),"$.schema")=="emuflow.packed-site-netlist/v1" &&
          JsonString(db,documents.Text(1),"$.top")==top,"RWRoute packed header disagrees");
    auto source=db.Prepare("SELECT json_type(?1,'$.source.mapped_json_sha256'),"
                           "json_extract(?1,'$.source.mapped_json_sha256')");
    source.BindText(1,documents.Text(1));
    Check(source.Step(),"RWRoute physical source identity is invalid");
    Check(source.IsNull(0) || (source.Text(0)=="text" && source.Text(1)==digests[0]),
          "RWRoute physical source mapped identity disagrees");
    Check(documents.Step() && documents.Text(0)=="placement" &&
          JsonString(db,documents.Text(1),"$.schema")=="emuflow.xilinx-placement/v1" &&
          JsonString(db,documents.Text(1),"$.part")==part && !documents.Step(),
          "RWRoute placement header disagrees");
  }
  db.Execute("CREATE TEMP TABLE rwroute_io_endpoints(net_bit INTEGER PRIMARY KEY,"
             "seq INTEGER NOT NULL,cell_id TEXT NOT NULL,pin TEXT NOT NULL,"
             "role INTEGER NOT NULL,site TEXT NOT NULL,route_ordinal INTEGER NOT NULL)");
  const auto ports=Metadata(db,"ports_json");
  auto port_query=db.Prepare("SELECT json_type(value,'$.direction'),json_extract(value,'$.direction'),"
      "json_type(value,'$.bits'),json_array_length(value,'$.bits'),"
      "json_type(value,?3),json_extract(value,?3) FROM json_each(?1) WHERE key=?2");
  auto add_io=db.Prepare("INSERT INTO rwroute_io_endpoints VALUES (?,?,?,?,?,?,?)");
  std::sort(ios.begin(),ios.end(),[](const auto& a,const auto& b){return a.key<b.key;});
  for (std::size_t i=0;i<ios.size();++i) {
    auto& io=ios[i]; io.ordinal=i;
    port_query.Reset(); port_query.BindText(1,ports); port_query.BindText(2,io.port);
    port_query.BindText(3,"$.bits["+std::to_string(io.bit)+"]");
    Check(port_query.Step() && port_query.Text(0)=="text" &&
          port_query.Text(1)==(io.direction=="tx" ? "output" : "input") &&
          port_query.Text(2)=="array" && port_query.TypedInteger(3)==io.width &&
          port_query.Text(4)=="integer","RWRoute virtual I/O logical top-port binding is invalid");
    io.net=port_query.TypedInteger(5);
    Check(!port_query.Step(),"RWRoute virtual I/O top-port identity repeats");
    add_io.Reset(); add_io.BindInteger(1,io.net); add_io.BindInteger(2,i); add_io.BindText(3,io.key);
    add_io.BindText(4,io.direction=="tx" ? "I" : "O");
    add_io.BindInteger(5,io.direction=="tx" ? 1 : 0);
    add_io.BindText(6,io.p_site); add_io.BindInteger(7,i); add_io.Execute();
  }
  const auto binding_seconds=Seconds(started);
  const std::vector<ArtifactRef> inputs{mapped_ref,physical_ref,request_ref};
  AtomicBinaryWriter writer(output,inputs), summary_writer(summary_path,inputs);
  auto& out=writer.stream();
  for (const auto& limit:limits) out<<"META\tresource_limit_"<<limit.first<<'\t'<<limit.second<<'\n';
  out<<"META\tpart\t"<<part<<"\nMETA\tmapped_sha256\t"<<digests[0]
     <<"\nMETA\tpacked_sha256\t"<<digests[1]<<"\nMETA\tplacement_sha256\t"<<digests[2]<<'\n';
  if (!io_digest.empty()) out<<"META\tvirtual_io_contract_sha256\t"<<io_digest<<'\n';
  const auto cell_started=Clock::now();
  Integer ordinal=ios.size(), logical=0, expanded=0, dsps=0;
  CellCatalog catalog(db);
  std::unordered_map<std::string,std::uint32_t> sites;
  const auto site_id=[&](const std::string& name) {
    Check(sites.size()<kNoCell,"physical site catalog exceeds native identity range");
    return sites.emplace(name,static_cast<std::uint32_t>(sites.size())).first->second;
  };

  auto parameters=db.Prepare("SELECT json_type(?1,'$.parameters'),json_type(?1,?2),"
      "json_extract(?1,?2),typeof(json_extract(?1,?2)),json_extract(?1,'$.parameters'),json_type(?1)");
  {
    auto cells=db.Prepare("SELECT a.instance,c.type,a.physical_site,a.bel,"
      "CASE WHEN c.type IN ('RAMB18E2','RAMB36E2') THEN c.record_json END,c.seq "
      "FROM physical.assignment a JOIN cells c ON c.id=a.instance ORDER BY a.instance");
    while (cells.Step()) {
      const auto name=cells.TypedText(0), type=cells.TypedText(1),site=cells.TypedText(2),bel=cells.TypedText(3);
      Token(name); Token(type); Token(site); Token(bel); ++logical;
      auto& cell=catalog[catalog.Index(cells.TypedInteger(5))];
      Check(cell.ordinal==kNoOrdinal,"physical source cell sequence repeats");
      cell.ordinal=ordinal;cell.site=site_id(site);
      cell.flags=type=="LUT6_2" ? kDual : type=="CARRY8" ? kCarry : type=="FDRE" ? kFdre : 0;

      if (type=="LUT6_2") {
        Check(bel.size()==5 && bel[0]>='A' && bel[0]<='H' && bel.substr(1)=="6LUT",
              "LUT6_2 cell is not assigned to a 6LUT BEL");
        out<<"CELL\tc"<<ordinal<<'\t'<<name<<"$physical_o5\tLUT5\t"<<site<<'\t'<<bel[0]<<"5LUT\n"
           <<"CELL\tc"<<ordinal+1<<'\t'<<name<<"$physical_o6\tLUT6\t"<<site<<'\t'<<bel<<'\n';
        ordinal+=2; ++expanded;
      } else {
        out<<"CELL\tc"<<ordinal<<'\t'<<name<<'\t'<<type<<'\t'<<site<<'\t'<<bel<<'\n';
        if (type=="DSP48E2") ++dsps;
        if (type=="RAMB18E2" || type=="RAMB36E2") {
          Check(JsonString(db,cells.TypedText(4),"$.type")==type,
                "indexed mapped primitive type projection disagrees");
          for (const auto* field:{"READ_WIDTH_A","READ_WIDTH_B","WRITE_WIDTH_A","WRITE_WIDTH_B",
                                 "DOA_REG","DOB_REG","WRITE_MODE_A","WRITE_MODE_B","RAM_MODE"}) {
            const auto value=Parameter(db,parameters,cells.TypedText(4),field);
            if (value) out<<"PARAM\tc"<<ordinal<<'\t'<<field<<'\t'<<*value<<'\n';
          }
        }
        ++ordinal;
      }
    }
  }
  for (auto& io:ios) {
    io.site=site_id(io.p_site);
    out<<"IOB\tc"<<io.ordinal<<'\t'<<io.id<<'\t'<<io.direction<<'\t'<<io.p_port<<'\t'<<io.n_port
       <<'\t'<<io.p_pin<<'\t'<<io.n_pin<<'\t'<<io.p_site<<'\t'<<io.n_site<<'\t'<<io.port
       <<'\t'<<io.bit<<'\t'<<io.standard<<'\n';
  }
  sites.clear();sites.rehash(0);
  const auto cell_seconds=Seconds(cell_started);
  const auto clock_started=Clock::now();
  ClockMetrics clock_metrics;
  const auto gate_count=QualifyClocks(db,parameters,catalog,ios,out,clock_metrics);
  const auto clock_seconds=Seconds(clock_started);
  const auto endpoint_started=Clock::now();
  auto endpoints=db.Prepare(R"SQL(
    SELECT p.net_bit,p.cell_seq,p.port,p.port_bit,p.port_width,p.direction,c.id=p.cell_id,0
      FROM cell_pins p LEFT JOIN cells c ON c.seq=p.cell_seq WHERE p.value_kind=0
    UNION ALL
    SELECT net_bit,route_ordinal,pin,0,1,CASE WHEN role=0 THEN 1 ELSE 0 END,1,1
      FROM rwroute_io_endpoints
    ORDER BY 1
  )SQL");
  bool more=endpoints.Step();
  const auto endpoint_seconds=Seconds(endpoint_started);
  const auto net_started=Clock::now();
  Integer included=0,excluded=0,max_fanout=0;
  std::uint64_t pin_rows=0;
  std::vector<Endpoint> group;
  const auto route_ordinal=[&](const Endpoint& e) {
    return e.io ? static_cast<std::uint64_t>(ios[e.source].ordinal) : catalog[e.source].ordinal;
  };
  const auto site=[&](const Endpoint& e) { return e.io ? ios[e.source].site : catalog[e.source].site; };
  const auto gate=[&](const Endpoint& e) { return !e.io && (catalog[e.source].flags&kGate); };
  auto carry_name=db.Prepare("SELECT id FROM cells WHERE seq=?");
  const auto emit=[&]() {
    if (group.empty()) return;
    max_fanout=std::max(max_fanout,static_cast<Integer>(group.size()));
    const auto bit=group.front().net;
    std::size_t driver_count=0,sink_count=0;
    const Endpoint* driver=nullptr;
    bool boundary_clock=false,touches_gate=false,same_site=true;
    for (const auto& e:group) {
      if (e.driver) { ++driver_count; driver=&e; }
      else { ++sink_count; boundary_clock=boundary_clock || (e.width==1 && IsClock(e.port)); }
      touches_gate=touches_gate || gate(e);
      same_site=same_site && site(e)==site(group.front());
    }
    if (boundary_clock && driver_count)
      Check(driver_count==1 && gate(*driver) && driver->width==1 && driver->port=="O",
            "routed clock bit "+std::to_string(bit)+" is not driven by a qualified "
            "LUT2 INIT=1000 gate with an FDRE-latched enable");
    if (driver_count!=1 || !sink_count) {
      out<<"EXCLUDED\tn"<<bit<<'\t';
      if (!driver_count && sink_count && boundary_clock) out<<"boundary_clock\tideal-boundary-clock";
      else out<<(driver_count ? "multiple_driver_or_sinkless" : "boundary_or_driverless");
      out<<'\n'; ++excluded; return;
    }
    if (same_site && !touches_gate) {
      out<<"EXCLUDED\tn"<<bit<<"\tintra_site\n"; ++excluded; return;
    }
    if (!driver->io && (catalog[driver->source].flags&kCarry)) {
      const auto pin=LogicalPin(*driver);const auto bracket=pin.find('[');
      if (bracket!=std::string::npos && pin.back()==']') {
        const auto kind=pin.substr(0,bracket);
        auto digits=pin.substr(bracket+1,pin.size()-bracket-2);
        if ((kind=="CO" || kind=="O") && !digits.empty() &&
            digits.find_first_not_of("0123456789")==std::string::npos) {
          const auto first=digits.find_first_not_of('0');
          digits=first==std::string::npos ? "0" : digits.substr(first);
          if (digits.size()==1 && digits[0]<'7') {
            const auto index=digits[0]-'0';
            const auto own=1U<<((kind=="CO" ? 8 : 15)+index);
            const auto other=1U<<((kind=="CO" ? 15 : 8)+index);
            auto& flags=catalog[driver->source].flags;
            if (flags&other) {
              carry_name.Reset();carry_name.BindInteger(1,catalog.Source(driver->source));
              Check(carry_name.Step(),"CARRY8 source identity is missing");
              throw StageError(ErrorCode::kInvalidArtifact,"CARRY8 shared site exit is multiply driven: "+carry_name.TypedText(0));
            }
            flags|=own;
          }
        }
      }
    }
    out<<"NET\tn"<<bit<<'\t'<<(boundary_clock ? "clock" : "signal")<<'\n';
    // The source net index may group direction codes differently. Restore the
    // canonical lexical-cell/port/numeric-bit order only for emitted nets.
    // Base route ordinal is the canonical cell rank, including LUT6_2's pair.
    std::sort(group.begin(),group.end(),[&](const auto& a,const auto& b) {
      if (a.io!=b.io) return !a.io;
      const auto x=route_ordinal(a),y=route_ordinal(b);
      if (x!=y) return x<y;
      if (a.port!=b.port) return a.port<b.port;
      return a.bit<b.bit;
    });
    for (bool drivers:{true,false}) for (const auto& e:group) if (e.driver==drivers) {
      auto ordinal=route_ordinal(e);auto pin=LogicalPin(e);
      if (!e.io && (catalog[e.source].flags&kDual)) {
        if (e.port=="I0") pin="I0";
        else if (e.port=="O5") pin="O";
        else if (e.port=="I1") { ++ordinal;pin="I1"; }
        else if (e.port=="O6") { ++ordinal;pin="O"; }
        else throw StageError(ErrorCode::kInvalidArtifact,"LUT6_2 pin has no RWRoute physical binding: "+pin);
      }
      out<<"PIN\tn"<<bit<<"\tc"<<ordinal<<'\t'<<pin<<'\t'<<(drivers ? "driver" : "sink")<<'\n';
    }
    ++included;
  };
  while (more) {
    ++pin_rows;
    const auto endpoint=ReadEndpoint(endpoints,catalog,ios);
    if (endpoint) {
      if (!group.empty() && group.front().net!=endpoint->net) { emit();group.clear(); }
      group.push_back(*endpoint);
    }
    more=endpoints.Step();
    Check(static_cast<bool>(out),"cannot write RWRoute input");
  }
  emit();
  const auto net_seconds=Seconds(net_started);
  for (const auto* ref:{&mapped_ref,&physical_ref,&request_ref}) ref->RequireUnchanged();
  const auto commit_started=Clock::now(); writer.Commit();
  const auto commit_seconds=Seconds(commit_started);
  const JsonRecord value(std::map<std::string,JsonRecord>{
    {"schema","emuflow.rwroute-export-summary/v1"},
    {"status","pass"},{"implementation_id","native-rwroute-export-v2"},
    {"cells",ordinal},{"logical_cells",logical},{"virtual_io_cells",ios.size()},
    {"expanded_lut6_2_cells",expanded},{"transformed_dsp48e2_cells",dsps},
    {"qualified_clock_gates",gate_count},{"physical_cells",ordinal+7*dsps},
    {"routable_nets",included},{"excluded_nets",excluded},{"output",output},
    {"runtime_breakdown",std::map<std::string,JsonRecord>{
      {"schema","emuflow.rwroute-export-runtime/v1"},{"binding_seconds",binding_seconds},
      {"cell_and_io_rows_seconds",cell_seconds},{"endpoint_index_seconds",endpoint_seconds},
      {"clock_validation_seconds",clock_seconds},{"net_rows_seconds",net_seconds},
      {"atomic_commit_seconds",commit_seconds},{"total_seconds",Seconds(started)},
      {"maximum_live_net_endpoints",max_fanout},
      {"source_cell_catalog_bytes",catalog.bytes()},
      {"source_pin_rows_visited",pin_rows},{"clock_candidates",clock_metrics.candidates},
      {"enabled_clock_candidates",clock_metrics.enabled},{"clock_pin_rows_visited",clock_metrics.pin_rows},
      {"clock_selected_nets",clock_metrics.selected_nets},{"clock_dependency_edges",clock_metrics.dependency_edges},
      {"dataflow",std::map<std::string,JsonRecord>{
        {"route_cells","native-mapped-physical-cursor"},{"physical_bindings","native-mapped-physical-join"},
        {"safe_names","native-ordinal-cursor"},{"endpoints","native-integer-cell-catalog"},
        {"clock_gates","native-sparse-net-reduction"},{"tsv","streamed-atomic-spool"}}}}}
  });
  const auto encoded=value.Encode();
  summary_writer.WriteBytes(encoded.data(),encoded.size()); summary_writer.Commit();
}
}  // namespace emuflow::core
