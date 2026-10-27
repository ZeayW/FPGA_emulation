#include "emuflow_core/mapped_normalization.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <regex>
#include <sstream>

namespace emuflow::core {
namespace {
void Require(bool ok,const std::string& message) {
  if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);
}
void None(SqliteDatabase& db,const std::string& sql,const char* message) {
  auto row=db.Prepare(sql+" LIMIT 1");
  if(row.Step())throw StageError(ErrorCode::kInvalidArtifact,std::string(message)+": "+row.Text(0));
}
std::int64_t Count(SqliteDatabase& db,const std::string& sql) {
  auto row=db.Prepare(sql);Require(row.Step(),"normalization count missing");return row.TypedInteger(0);
}
std::string Library(SqliteDatabase& db,const ArtifactRef& source) {
  Require(source.generation().bytes<=2*1024*1024,"primitive source exceeds normalization bound");
  std::ifstream stream(source.path());std::string text((std::istreambuf_iterator<char>(stream)),{});
  Require(!stream.bad() && text.size()==source.generation().bytes,"primitive source is incomplete");
  Sha256 sha;sha.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());
  std::string digest;const char* digits="0123456789abcdef";
  for(auto byte:sha.finish()){digest+=digits[byte>>4];digest+=digits[byte&15];}
  db.Execute("CREATE TEMP TABLE physical_ports(type TEXT,port TEXT,role INTEGER,width INTEGER,PRIMARY KEY(type,port)) WITHOUT ROWID");
  auto insert=db.Prepare("INSERT INTO physical_ports VALUES (?,?,?,?)");
  const std::regex declaration(R"(^\s*(input|output)\s+(?:\[([0-9]+):0\]\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*;\s*$)");
  const std::regex starts(R"(^\s*(input|output)\b)");
  for(const std::string name:{"RAMB18E2","RAMB36E2","URAM288"}) {
    const std::regex header("\\bmodule\\s+"+name+"\\s*\\(");std::smatch match;
    Require(std::regex_search(text,match,header),"primitive source lacks "+name);
    const auto begin=std::size_t(match.position()),end=text.find("endmodule",begin);
    Require(end!=std::string::npos,"primitive declaration is incomplete");
    std::istringstream lines(text.substr(begin,end-begin));std::string line;unsigned pins=0;
    while(std::getline(lines,line)) {
      if(!std::regex_search(line,starts))continue;
      Require(std::regex_match(line,match,declaration),"unsupported physical port declaration: "+line);
      const auto width=match[2].matched?std::stoul(match[2].str())+1:1;
      Require(width<=1024,"physical port width exceeds bound");
      insert.BindText(1,name);insert.BindText(2,match[3]);insert.BindInteger(3,match[1]=="input"?1:0);
      insert.BindInteger(4,width);insert.Execute();insert.Reset();++pins;
    }
    Require(pins>0,"primitive source has no physical pins");
  }
  return digest;
}
void Pins(SqliteDatabase& db,const std::string& name,const std::string& schema,bool normalized) {
  db.Execute("CREATE TEMP TABLE "+name+"(cell INTEGER,port TEXT,bit INTEGER,role INTEGER,net INTEGER,constant TEXT,"
      "local_net INTEGER,PRIMARY KEY(cell,port,bit,role)) WITHOUT ROWID");
  db.Execute("INSERT INTO "+name+" SELECT "+(normalized?"coalesce(m.old,-1)":"coalesce(e.instance_seq,-1)")+",p.name,e.bit,e.role,"
      +(normalized?"n.old":"e.net_seq")+",NULL,e.net_seq FROM "+schema+"endpoint_records e JOIN "+schema+"pin_names p ON p.seq=e.port_seq "
      +(normalized?"LEFT JOIN cell_match m ON m.fresh=e.instance_seq LEFT JOIN net_match n ON n.fresh=e.net_seq":""));
  db.Execute("INSERT INTO "+name+" SELECT "+(normalized?"m.old":"c.seq")+",json_extract(j.value,'$.port'),json_extract(j.value,'$.bit'),1,"
      "NULL,json_extract(j.value,'$.value'),NULL FROM "+schema+"cell_records c JOIN "+schema+"payloads k ON k.seq=c.constants_seq,"
      "json_each(k.value) j "+(normalized?"JOIN cell_match m ON m.fresh=c.seq":""));
  db.Execute("INSERT INTO "+name+" SELECT -1,json_extract(p.record_json,'$.id'),json_extract(j.value,'$.bit'),"
      "CASE json_extract(p.record_json,'$.direction') WHEN 'input' THEN 0 ELSE 1 END,NULL,"
      "json_extract(j.value,'$.value'),NULL FROM "+schema+"port_records p,json_each(p.record_json,'$.constant_connections') j");
}
}

void ValidateMappedNormalization(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Require(inputs.size()==4,"normalization parent count differs");
  for(const auto& input:inputs)input.RequireUnchanged();
  CheckedRequestReader request(inputs[3].path(),4096);
  Require(request.Read<std::uint64_t>()==0x31514e4d464d5545ULL && request.Read<std::uint32_t>()==1,
          "unsupported mapped normalization request");
  for(unsigned i=0;i<3;++i) {
    const FileGeneration generation{request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),request.Read<std::uint64_t>(),
      request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>(),request.Read<std::int64_t>()};
    if(!(generation==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"normalization parent generation changed");
  }
  request.RequireEnd();EmuirView original(inputs[0]),normalized(inputs[1]);
  Require(original.counts().cells==normalized.counts().cells,"normalization changed cell count");
  auto& db=original.store().database();db.Execute("PRAGMA temp_store=FILE; PRAGMA cache_size=-32768; PRAGMA temp.cache_size=-32768");
  auto attach=db.Prepare("ATTACH DATABASE ? AS fresh");attach.BindText(1,SqliteImmutableUri(inputs[1].path()));attach.Execute();
  const auto library=Library(db,inputs[2]);
  Require(Count(db,"SELECT count(*) FROM cell_records")==std::int64_t(original.counts().cells) &&
      Count(db,"SELECT count(*) FROM fresh.cell_records")==std::int64_t(normalized.counts().cells),"normalization cell metadata count differs");
  None(db,"SELECT 'top' FROM metadata a JOIN fresh.metadata b ON b.key=a.key WHERE a.key='design_json' "
      "AND json_extract(a.value,'$.top') IS NOT json_extract(b.value,'$.top')","normalization changed top");
  None(db,"SELECT a.id FROM cell_records a LEFT JOIN fresh.cell_records b ON b.id=a.id LEFT JOIN payloads p ON p.seq=a.parameters_seq "
      "LEFT JOIN fresh.payloads q ON q.seq=b.parameters_seq WHERE b.id IS NULL OR a.type IS NOT b.type OR "
      "coalesce(p.value,'{}') IS NOT coalesce(q.value,'{}')","normalization changed cell identity/type/parameters");
  db.Execute("CREATE TEMP TABLE cell_match AS SELECT a.seq AS old,b.seq AS fresh,a.type FROM cell_records a JOIN fresh.cell_records b ON b.id=a.id;"
      "CREATE UNIQUE INDEX cell_match_old ON cell_match(old);CREATE UNIQUE INDEX cell_match_fresh ON cell_match(fresh);"
      "CREATE TEMP TABLE net_match(old INTEGER UNIQUE,fresh INTEGER UNIQUE);"
      "INSERT INTO net_match SELECT a.seq,b.seq FROM net_records a JOIN fresh.net_records b ON b.id=a.id;"
      "INSERT OR IGNORE INTO net_match SELECT a.seq,b.net_seq FROM fresh.alias_records b JOIN net_records a ON a.id=b.alias;");
  None(db,"SELECT a.id FROM fresh.alias_records b JOIN net_records a ON a.id=b.alias JOIN net_match m ON m.old=a.seq "
      "WHERE m.fresh IS NOT b.net_seq","normalization net alias is ambiguous");
  // Top-level shape is checked in both directions; constants/connectivity
  // below are checked separately, including unused input/output bits.
  for(const std::string side:{"main","fresh"}) {
    const auto other=side=="main"?"fresh":"main";
    None(db,"SELECT json_extract(a.record_json,'$.id') FROM "+side+".port_records a LEFT JOIN "+other+".port_records b "
        "ON json_extract(a.record_json,'$.id')=json_extract(b.record_json,'$.id') WHERE b.seq IS NULL OR "
        "json_extract(a.record_json,'$.direction') IS NOT json_extract(b.record_json,'$.direction') OR "
        "json_extract(a.record_json,'$.width') IS NOT json_extract(b.record_json,'$.width') OR "
        "json_extract(a.record_json,'$.direction') NOT IN ('input','output')","normalization changed top port shape");
  }
  Pins(db,"old_pins","",false);Pins(db,"new_pins","fresh.",true);
  db.Execute("CREATE TEMP TABLE old_widths AS SELECT cell,port,max(bit)+1 AS width FROM old_pins GROUP BY cell,port;"
      "CREATE UNIQUE INDEX old_width_key ON old_widths(cell,port);");
  for(const std::string name:{"old_pins","new_pins"}) {
    None(db,"SELECT c.id||'.'||p.port FROM "+name+" p JOIN cell_records c ON c.seq=p.cell LEFT JOIN physical_ports s "
        "ON s.type=c.type AND s.port=p.port WHERE c.type IN ('RAMB18E2','RAMB36E2','URAM288') AND "
        "(s.port IS NULL OR p.role IS NOT s.role"+(name=="new_pins"?" OR p.bit>=s.width":"")+")",
        "normalization macro pin is not physical");
  }
  None(db,"SELECT c.id||'.'||p.port FROM old_pins p JOIN cell_records c ON c.seq=p.cell JOIN physical_ports s "
      "ON s.type=c.type AND s.port=p.port WHERE p.bit>=s.width AND ((p.role=0 AND EXISTS "
      "(SELECT 1 FROM endpoint_records e WHERE e.net_seq=p.net AND e.role=1)) OR (p.role=1 AND p.constant IS NULL AND "
      "(p.port!='WEA' OR NOT EXISTS(SELECT 1 FROM old_pins low WHERE low.cell=p.cell AND low.port=p.port "
      "AND low.bit=p.bit%s.width AND low.role=p.role AND low.net=p.net))))","normalization discarded a live nonphysical connection");
  const auto removed=Count(db,"SELECT count(*) FROM old_pins p JOIN cell_records c ON c.seq=p.cell JOIN physical_ports s "
      "ON s.type=c.type AND s.port=p.port WHERE p.bit>=s.width");
  db.Execute("DELETE FROM old_pins WHERE (cell,port,bit,role) IN (SELECT p.cell,p.port,p.bit,p.role FROM old_pins p "
      "JOIN cell_records c ON c.seq=p.cell JOIN physical_ports s ON s.type=c.type AND s.port=p.port WHERE p.bit>=s.width)");
  None(db,"SELECT coalesce(c.id,'<top>')||'.'||a.port||'['||a.bit||']' FROM old_pins a LEFT JOIN new_pins b "
      "ON b.cell=a.cell AND b.port=a.port AND b.bit=a.bit AND b.role=a.role LEFT JOIN cell_records c ON c.seq=a.cell "
      "WHERE b.cell IS NULL OR a.net IS NOT b.net OR a.constant IS NOT b.constant",
      "normalization changed a retained pin");
  // Width extension is permitted only for existing macro ports: zero input
  // extension, or fresh sinkless outputs. Arbitrary added logic is rejected.
  None(db,"SELECT coalesce(c.id,'<top>')||'.'||b.port||'['||b.bit||']' FROM new_pins b LEFT JOIN old_pins a "
      "ON b.cell=a.cell AND b.port=a.port AND b.bit=a.bit AND b.role=a.role LEFT JOIN cell_records c ON c.seq=b.cell "
      "LEFT JOIN physical_ports s ON s.type=c.type AND s.port=b.port LEFT JOIN old_widths w ON w.cell=b.cell AND w.port=b.port "
      "WHERE a.cell IS NULL AND NOT coalesce(((b.cell>=0 AND s.port IS NOT NULL AND w.width IS NOT NULL AND b.bit>=w.width AND b.bit<s.width AND "
      "((b.role=1 AND b.constant='0') OR (b.role=0 AND b.constant IS NULL AND NOT EXISTS "
      "(SELECT 1 FROM fresh.endpoint_records e WHERE e.net_seq=b.local_net AND e.role=1)))) OR "
      "(b.cell=-1 AND ((b.role=0 AND b.constant IS NULL AND NOT EXISTS (SELECT 1 FROM fresh.endpoint_records e "
      "WHERE e.net_seq=b.local_net AND e.role=1)) OR (b.role=1 AND b.constant='x')))),0)",
      "normalization added an unproved pin");
  const JsonRecord result(std::map<std::string,JsonRecord>{{"schema","emuflow.mapped-normalization-proof/v1"},
      {"status","pass"},{"cells",original.counts().cells},{"retained_pins",Count(db,"SELECT count(*) FROM old_pins")},
      {"removed_nonphysical_pins",removed},{"primitive_source_sha256",library}});
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<result.Encode()<<'\n';});
}
}
