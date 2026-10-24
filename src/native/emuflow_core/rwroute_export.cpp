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
struct Io {
  std::string key, id, direction, p_port, n_port, p_pin, n_pin, p_site, n_site, port, standard;
  Integer bit=0,width=0,net=0,ordinal=0;
};
struct Endpoint {
  Integer net=0, ordinal=-1;
  std::string cell,pin,site,route_pin,type;
  bool driver=false,gate=false,io=false;
};
bool IsClock(const std::string& pin) {
  return pin=="C" || pin=="CLK" || pin=="CLKARDCLK" || pin=="CLKBWRCLK";
}
const char* kPhysicalCells=R"SQL(
 WITH physical_cells AS (
   SELECT a.instance,c.type,a.physical_site,
     ? + sum(CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END) OVER (
       ORDER BY a.instance ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
       - CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END AS route_ordinal
   FROM physical.assignment AS a JOIN cells AS c ON c.id=a.instance
 )
)SQL";
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
  SqliteDatabase db(mapped_ref.path(),SqliteDatabase::Mode::kImmutableReadOnly);
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
  AtomicBinaryWriter writer(output), summary_writer(summary_path);
  auto& out=writer.stream();
  for (const auto& limit:limits) out<<"META\tresource_limit_"<<limit.first<<'\t'<<limit.second<<'\n';
  out<<"META\tpart\t"<<part<<"\nMETA\tmapped_sha256\t"<<digests[0]
     <<"\nMETA\tpacked_sha256\t"<<digests[1]<<"\nMETA\tplacement_sha256\t"<<digests[2]<<'\n';
  if (!io_digest.empty()) out<<"META\tvirtual_io_contract_sha256\t"<<io_digest<<'\n';
  const auto cell_started=Clock::now();
  Integer ordinal=ios.size(), logical=0, expanded=0, dsps=0;
  auto parameters=db.Prepare("SELECT json_type(?1,'$.parameters'),json_type(?1,?2),"
      "json_extract(?1,?2),typeof(json_extract(?1,?2)),json_extract(?1,'$.parameters'),json_type(?1)");
  {
    auto cells=db.Prepare("SELECT a.instance,c.type,a.physical_site,a.bel,"
      "CASE WHEN c.type IN ('RAMB18E2','RAMB36E2') THEN c.record_json END "
      "FROM physical.assignment a JOIN cells c ON c.id=a.instance ORDER BY a.instance");
    while (cells.Step()) {
      const auto name=cells.TypedText(0), type=cells.TypedText(1),site=cells.TypedText(2),bel=cells.TypedText(3);
      Token(name); Token(type); Token(site); Token(bel); ++logical;
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
  for (const auto& io:ios)
    out<<"IOB\tc"<<io.ordinal<<'\t'<<io.id<<'\t'<<io.direction<<'\t'<<io.p_port<<'\t'<<io.n_port
       <<'\t'<<io.p_pin<<'\t'<<io.n_pin<<'\t'<<io.p_site<<'\t'<<io.n_site<<'\t'<<io.port
       <<'\t'<<io.bit<<'\t'<<io.standard<<'\n';
  const auto cell_seconds=Seconds(cell_started);
  const auto clock_started=Clock::now();
  // The clock qualification relations and endpoint cursor below share the
  // authoritative source stores. No gate, safe-name or endpoint population
  // crosses back into Python.
  db.Execute("CREATE TEMP TABLE rwroute_clock_candidates(name TEXT PRIMARY KEY,root_bit INTEGER NOT NULL,"
             "enable_bit INTEGER NOT NULL,output_bit INTEGER NOT NULL,route_ordinal INTEGER NOT NULL) WITHOUT ROWID");
  {
    auto candidates=db.Prepare(R"SQL(
                WITH physical_cells AS (
                  SELECT a.instance,c.type,
                         ? + sum(CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END)
                           OVER (
                             ORDER BY a.instance
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                           )
                           - CASE WHEN c.type='LUT6_2' THEN 2 ELSE 1 END
                           AS route_ordinal
                    FROM physical.assignment AS a
                    JOIN cells AS c ON c.id=a.instance
                )
                SELECT c.id,c.record_json,r.route_ordinal,
                       sum(CASE WHEN p.port='I0' THEN 1 ELSE 0 END) AS i0_rows,
                       sum(CASE WHEN p.port='I1' THEN 1 ELSE 0 END) AS i1_rows,
                       sum(CASE WHEN p.port='O' THEN 1 ELSE 0 END) AS o_rows,
                       max(CASE WHEN p.port='I0' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS root_bit,
                       max(CASE WHEN p.port='I1' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS enable_bit,
                       max(CASE WHEN p.port='O' AND p.port_width=1
                                     AND p.port_bit=0 AND p.value_kind=0
                                THEN p.net_bit END) AS output_bit
                  FROM cells AS c
             LEFT JOIN physical_cells AS r ON r.instance=c.id
             LEFT JOIN cell_pins AS p ON p.cell_id=c.id
                 WHERE c.type='LUT2'
                 GROUP BY c.id,c.record_json,r.route_ordinal
                 ORDER BY c.id
                )SQL");
    candidates.BindInteger(1,ios.size());
    auto add=db.Prepare("INSERT INTO rwroute_clock_candidates VALUES (?,?,?,?,?)");
    while (candidates.Step()) {
      const auto value=Parameter(db,parameters,candidates.TypedText(1),"INIT");
      if (!value || *value!="8" || candidates.IsNull(2) ||
          candidates.TypedInteger(3)!=1 || candidates.TypedInteger(4)!=1 || candidates.TypedInteger(5)!=1 ||
          candidates.IsNull(6) || candidates.IsNull(7) || candidates.IsNull(8)) continue;
      add.Reset(); add.BindText(1,candidates.TypedText(0));
      for (int i=0;i<3;++i) add.BindInteger(i+2,candidates.TypedInteger(i+6));
      add.BindInteger(5,candidates.TypedInteger(2)); add.Execute();
    }
  }
  db.Execute(R"SQL(
                CREATE TEMP VIEW rwroute_clock_endpoints AS
                SELECT p.net_bit,p.cell_id,
                       CASE WHEN p.port_width=1 THEN p.port
                            ELSE p.port || '[' || p.port_bit || ']' END AS pin,
                       CASE WHEN p.direction IN (1,2) THEN 0 ELSE 1 END AS role
                  FROM cell_pins AS p
                  JOIN physical.assignment AS a ON a.instance=p.cell_id
                 WHERE p.value_kind=0
                UNION ALL
                SELECT net_bit,cell_id,pin,role
                  FROM rwroute_io_endpoints;

                DELETE FROM rwroute_clock_candidates AS c
                 WHERE (SELECT count(*) FROM rwroute_clock_endpoints AS e
                         WHERE e.net_bit=c.enable_bit AND e.role=0) != 1
                    OR NOT EXISTS (
                         SELECT 1
                           FROM rwroute_clock_endpoints AS e
                           JOIN cells AS d ON d.id=e.cell_id
                          WHERE e.net_bit=c.enable_bit AND e.role=0
                            AND d.type='FDRE' AND e.pin='Q'
                       );

                CREATE TEMP TABLE rwroute_qualified_clock_gates(
                  name TEXT PRIMARY KEY,
                  root_bit INTEGER NOT NULL,
                  output_bit INTEGER NOT NULL,
                  route_ordinal INTEGER NOT NULL
                ) WITHOUT ROWID;
                INSERT INTO rwroute_qualified_clock_gates
                SELECT c.name,c.root_bit,c.output_bit,c.route_ordinal
                  FROM rwroute_clock_candidates AS c
                 WHERE (
                       NOT EXISTS (
                         SELECT 1 FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.root_bit AND e.role=0
                       )
                       OR (
                         (SELECT count(*) FROM rwroute_clock_endpoints AS e
                           WHERE e.net_bit=c.root_bit AND e.role=0) = 1
                         AND EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=c.root_bit AND e.role=0
                              AND e.pin='O'
                         )
                       )
                     )
                   AND EXISTS (
                         SELECT 1 FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.output_bit AND e.role=1
                       )
                   AND NOT EXISTS (
                         SELECT 1
                           FROM rwroute_clock_endpoints AS e
                          WHERE e.net_bit=c.output_bit AND e.role=1
                            AND e.pin NOT IN ('C','CLK','CLKARDCLK','CLKBWRCLK')
                            AND NOT EXISTS (
                              SELECT 1
                                FROM rwroute_clock_candidates AS n
                               WHERE n.name=e.cell_id AND e.pin='I0'
                            )
                       );
                )SQL");
  while (true) {
    db.Execute(R"SQL(
                    DELETE FROM rwroute_qualified_clock_gates AS q
                     WHERE EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=q.root_bit AND e.role=0
                              AND e.pin='O'
                              AND NOT EXISTS (
                                SELECT 1
                                  FROM rwroute_qualified_clock_gates AS keep
                                 WHERE keep.name=e.cell_id
                              )
                         )
                        OR EXISTS (
                           SELECT 1
                             FROM rwroute_clock_endpoints AS e
                             JOIN rwroute_clock_candidates AS n
                               ON n.name=e.cell_id
                            WHERE e.net_bit=q.output_bit AND e.role=1
                              AND e.pin='I0'
                              AND NOT EXISTS (
                                SELECT 1
                                  FROM rwroute_qualified_clock_gates AS keep
                                 WHERE keep.name=e.cell_id
                              )
                         )
                    )SQL");
    auto changes=db.Prepare("SELECT changes()");
    Check(changes.Step(),"RWRoute clock closure query failed");
    if (!changes.TypedInteger(0)) break;
  }
  Integer gate_count=0;
  {
    auto gates=db.Prepare("SELECT name,route_ordinal FROM rwroute_qualified_clock_gates ORDER BY name");
    while (gates.Step()) {
      out<<"CLOCK_GATE\tc"<<gates.TypedInteger(1)<<"\tI0\tI1\tO\tlut2-init-1000-fdre-latched-enable-v1\n";
      ++gate_count;
    }
  }
  const auto clock_seconds=Seconds(clock_started);
  const auto endpoint_started=Clock::now();
  db.Execute("CREATE TEMP TABLE rwroute_carry_exits(cell TEXT NOT NULL,bit INTEGER NOT NULL,"
             "kind TEXT NOT NULL,PRIMARY KEY(cell,bit)) WITHOUT ROWID");
  auto carry_get=db.Prepare("SELECT kind FROM rwroute_carry_exits WHERE cell=? AND bit=?");
  auto carry_add=db.Prepare("INSERT OR IGNORE INTO rwroute_carry_exits VALUES (?,?,?)");
  auto endpoints=db.Prepare(std::string(kPhysicalCells)+R"SQL(
    SELECT p.net_bit,p.cell_id,r.type,p.port,p.port_bit,p.port_width,p.direction,
           r.physical_site,r.route_ordinal,q.name IS NOT NULL,0 AS io
      FROM cell_pins p JOIN physical_cells r ON r.instance=p.cell_id
      LEFT JOIN rwroute_qualified_clock_gates q ON q.name=p.cell_id
     WHERE p.value_kind=0
    UNION ALL
    SELECT net_bit,cell_id,'',pin,0,1,CASE WHEN role=0 THEN 1 ELSE 0 END,
           site,route_ordinal,0,1 FROM rwroute_io_endpoints
    ORDER BY 1,11,2,4,5
  )SQL");
  endpoints.BindInteger(1,ios.size());
  bool more=endpoints.Step();
  const auto endpoint_seconds=Seconds(endpoint_started);
  const auto net_started=Clock::now();
  Integer included=0,excluded=0,max_fanout=0;
  std::vector<Endpoint> group;
  const auto emit=[&]() {
    if (group.empty()) return;
    max_fanout=std::max(max_fanout,static_cast<Integer>(group.size()));
    const auto bit=group.front().net;
    std::size_t driver_count=0,sink_count=0;
    const Endpoint* driver=nullptr;
    bool boundary_clock=false,touches_gate=false,same_site=true;
    for (const auto& e:group) {
      if (e.driver) { ++driver_count; driver=&e; }
      else { ++sink_count; boundary_clock=boundary_clock || IsClock(e.pin); }
      touches_gate=touches_gate || e.gate;
      same_site=same_site && e.site==group.front().site;
    }
    if (boundary_clock && driver_count)
      Check(driver_count==1 && driver->gate && driver->pin=="O",
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
    if (driver->type=="CARRY8") {
      const auto bracket=driver->pin.find('[');
      if (bracket!=std::string::npos && driver->pin.back()==']') {
        const auto kind=driver->pin.substr(0,bracket);
        auto digits=driver->pin.substr(bracket+1,driver->pin.size()-bracket-2);
        if ((kind=="CO" || kind=="O") && !digits.empty() &&
            digits.find_first_not_of("0123456789")==std::string::npos) {
          const auto first=digits.find_first_not_of('0');
          digits=first==std::string::npos ? "0" : digits.substr(first);
          if (digits.size()==1 && digits[0]<'7') {
            const auto index=digits[0]-'0';
            carry_get.Reset(); carry_get.BindText(1,driver->cell); carry_get.BindInteger(2,index);
            Check(!carry_get.Step() || carry_get.Text(0)==kind,
                  "CARRY8 shared site exit is multiply driven: "+driver->cell);
            carry_get.Reset();
            carry_add.Reset(); carry_add.BindText(1,driver->cell); carry_add.BindInteger(2,index);
            carry_add.BindText(3,kind); carry_add.Execute();
          }
        }
      }
    }
    out<<"NET\tn"<<bit<<'\t'<<(boundary_clock ? "clock" : "signal")<<'\n';
    // Stable driver-first order matches the legacy contract. Only one live net
    // is retained; gate/ordinal/cell populations stay in the source relations.
    for (bool drivers:{true,false}) for (const auto& e:group) if (e.driver==drivers) {
      Check(e.ordinal>=0,"cell "+e.cell+" pin "+e.pin+" has no RWRoute physical binding");
      out<<"PIN\tn"<<bit<<"\tc"<<e.ordinal<<'\t'<<e.route_pin<<'\t'<<(drivers ? "driver" : "sink")<<'\n';
    }
    ++included;
  };
  while (more) {
    Endpoint e; e.net=endpoints.TypedInteger(0); e.cell=endpoints.TypedText(1); e.type=endpoints.TypedText(2);
    const auto port=endpoints.TypedText(3);
    const auto pin_bit=endpoints.TypedInteger(4),width=endpoints.TypedInteger(5),direction=endpoints.TypedInteger(6);
    Check(width>0 && pin_bit>=0 && pin_bit<width && direction>=0 && direction<=2,
          "indexed mapped pin row is invalid");
    e.io=endpoints.TypedInteger(10)!=0;
    if (!e.io) Token(e.cell);
    e.pin=width==1 ? port : port+"["+std::to_string(pin_bit)+"]";
    e.route_pin=e.pin; Token(e.pin);
    e.site=endpoints.TypedText(7); Token(e.site);
    e.ordinal=endpoints.TypedInteger(8); e.gate=endpoints.TypedInteger(9)!=0;
    Check(e.ordinal>=0 && e.ordinal<ordinal,"RWRoute route ordinal is invalid");
    e.driver=direction!=0;
    if (e.type=="LUT6_2") {
      if (port=="I0") e.route_pin="I0";
      else if (port=="O5") e.route_pin="O";
      else if (port=="I1") { ++e.ordinal; e.route_pin="I1"; }
      else if (port=="O6") { ++e.ordinal; e.route_pin="O"; }
      else e.ordinal=-1;
    }
    if (!group.empty() && group.front().net!=e.net) { emit(); group.clear(); }
    group.push_back(std::move(e));
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
    {"status","pass"},{"implementation_id","native-rwroute-export-v1"},
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
      {"dataflow",std::map<std::string,JsonRecord>{
        {"route_cells","native-mapped-physical-cursor"},{"physical_bindings","native-mapped-physical-join"},
        {"safe_names","native-ordinal-cursor"},{"endpoints","native-mapped-physical-join"},
        {"clock_gates","native-qualified-relation"},{"tsv","streamed-atomic-spool"}}}}}
  });
  const auto encoded=value.Encode();
  summary_writer.WriteBytes(encoded.data(),encoded.size()); summary_writer.Commit();
}
}  // namespace emuflow::core
