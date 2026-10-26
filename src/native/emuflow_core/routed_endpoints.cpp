#include "emuflow_core/routed_endpoints.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/hash.hpp"
#include <charconv>
#include <algorithm>
#include <filesystem>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <sstream>

namespace emuflow::core {
namespace {
using Integer=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
void Check(bool value,const std::string& reason) {
  if(!value)throw StageError(ErrorCode::kInvalidArtifact,"routed endpoint binding "+reason);
}
std::string Text(SqliteStatement& row,int column) {
  auto text=row.TypedText(column);Check(!text.empty(),"has an empty identity");(void)JsonQuote(text);return text;
}
std::string Metadata(SqliteDatabase& db,const char* key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"mapped metadata is missing");auto value=row.TypedText(0);Check(!row.Step(),"mapped metadata repeats");return value;
}
std::string Digest(Sha256& hash) {
  std::ostringstream out;out<<std::hex<<std::setfill('0');for(auto c:hash.finish())out<<std::setw(2)<<unsigned(c);return out.str();
}
class Json {
 public:
  explicit Json(SqliteDatabase& db):field_(db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)")),type_(db.Prepare("SELECT json_type(?1,?2)")){}
  std::string String(const std::string& json,const std::string& path,const std::string* fallback=nullptr) {
    Read(json,path);if(field_.IsNull(0) && fallback)return *fallback;
    Check(field_.Text(0)=="text","text is invalid: "+path);return Text(field_,1);
  }
  Integer Int(const std::string& json,const std::string& path) {
    Read(json,path);Check(field_.Text(0)=="integer","integer is invalid: "+path);return field_.TypedInteger(1);
  }
  double Number(const std::string& json,const std::string& path) {
    Read(json,path);Check(field_.Text(0)=="integer" || field_.Text(0)=="real","number is invalid: "+path);
    const auto value=field_.TypedReal(1);Check(std::isfinite(value) && value>=0,"delay is invalid: "+path);return value;
  }
  bool Bool(const std::string& json,const std::string& path,bool fallback=false,bool optional=true) {
    Read(json,path);if(field_.IsNull(0) && optional)return fallback;
    Check(field_.Text(0)=="true" || field_.Text(0)=="false","boolean is invalid: "+path);return field_.Text(0)=="true";
  }
  void Expect(const std::string& json,const std::string& path,const std::string& kind,bool optional=false) {
    type_.Reset();type_.BindText(1,json);type_.BindText(2,path);Check(type_.Step(),"JSON type is absent");
    Check((type_.IsNull(0) && optional) || type_.Text(0)==kind,"collection is invalid: "+path);
  }
 private:
  void Read(const std::string& json,const std::string& path) {field_.Reset();field_.BindText(1,json);field_.BindText(2,path);Check(field_.Step(),"JSON field is missing");}
  SqliteStatement field_,type_;
};
Object ClockGate(Json& json,const std::string& value) {
  const auto site=json.String(value,"$.physical_site"),implementation=std::string("direct-bufgce");
  return {{"logical_cell",json.String(value,"$.logical_cell")},{"physical_site",site},
    {"physical_type",json.String(value,"$.physical_type")},{"input_site",json.String(value,"$.input_site",&site)},
    {"implementation",json.String(value,"$.implementation",&implementation)},
    {"cascaded_input",json.Bool(value,"$.cascaded_input")},{"qualification",json.String(value,"$.qualification")}};
}
Object VirtualIo(Json& json,const std::string& value) {
  return {{"id",json.String(value,"$.id")},{"direction",json.String(value,"$.direction")},
    {"logical_port",json.String(value,"$.logical_port")},{"logical_bit",json.Int(value,"$.logical_bit")},
    {"p_site",json.String(value,"$.p_site")},{"n_site",json.String(value,"$.n_site")},
    {"route_delay_ns",json.Number(value,"$.route_delay_ps")/1000.0},{"measurement","fixed-iob-routed-boundary"}};
}
bool ReadLine(std::istream& in,std::string& line) {
  line.clear();char c;
  while(in.get(c)) {
    Check(c!='\0' && line.size()<512ULL*1024*1024,"net record is invalid or oversized");
    line+=c;if(c=='\n')return true;
  }
  Check(!in.bad() && line.empty(),"net stream is truncated");return false;
}
}

void BuildRoutedEndpoints(const std::vector<ArtifactRef>& inputs,const std::string& endpoint_path,
                          const std::string& manifest_path,const std::string& summary_path) {
  Check(inputs.size()==5,"source coverage is invalid");CheckedRequestReader request(inputs[4].path(),65536);
  Check(request.Read<std::uint64_t>()==0x31505452464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  for(unsigned i=0;i<4;++i)if(!(ReadFileGeneration(request)==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"routed endpoint source generation changed");
  const auto source=request.ReadString32(false,4096),payload_name=request.ReadString32(false,4096);request.RequireEnd();
  Check(std::filesystem::path(payload_name).filename().string()==payload_name && payload_name.find('\0')==std::string::npos && payload_name!="." && payload_name!="..","payload basename is invalid");
  SqliteStore mapped(inputs[0]);auto& db=mapped.database();Json json(db);
  {auto attach=db.Prepare("ATTACH DATABASE ? AS physical");attach.BindText(1,SqliteImmutableUri(inputs[1].path()));attach.Execute();}
  db.Execute("PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-8192");
  SqliteTransaction transaction(db,SqliteTransaction::Mode::kDeferred);
  std::ifstream header_stream(inputs[2].path(),std::ios::binary);Check(bool(header_stream),"route manifest is missing");
  const std::string route((std::istreambuf_iterator<char>(header_stream)),{});Check(!header_stream.bad(),"route manifest read failed");
  Check(json.String(route,"$.schema")=="emuflow.xilinx-route-db/v2","route schema is invalid");
  const auto status=json.String(route,"$.status");
  Check((status=="candidate" || status=="pass") && json.String(route,"$.provider")=="rapidwright-rwroute-2026.1.0","route producer/status is invalid");
  json.Expect(source,"$","object");json.Expect(route,"$.clock_gates","array",true);json.Expect(route,"$.virtual_io","array",true);
  auto source_shape=db.Prepare("SELECT count(*),count(DISTINCT key) FROM json_each(?)");source_shape.BindText(1,source);
  Check(source_shape.Step() && source_shape.TypedInteger(0)==4 && source_shape.TypedInteger(1)==4,"source seal fields are invalid");
  for(const char* key:{"mapped_sha256","packed_sha256","placement_sha256","route_sha256"}) {
    const auto path=std::string("$.")+key,sha=json.String(source,path);
    Check(sha.size()==64 && sha.find_first_not_of("0123456789abcdef")==std::string::npos,"source seal is invalid");
    if(std::string(key)!="route_sha256")Check(json.String(route,"$.source."+std::string(key))==sha,"route source seal disagrees");
  }
  Check(json.String(source,"$.packed_sha256")==json.String(source,"$.placement_sha256"),"physical handoff seals disagree");
  const auto expected_nets=json.Int(route,"$.payloads.nets.records");Check(expected_nets>=0,"net count is invalid");
  const auto net_sha=json.String(route,"$.payloads.nets.sha256");
  Check(json.String(route,"$.payloads.nets.format")=="jsonl-object/v1","net stream format is invalid");
  auto coverage=db.Prepare("SELECT 1 FROM cells c LEFT JOIN physical.assignment a ON a.instance=c.id WHERE a.instance IS NULL UNION ALL SELECT 1 FROM physical.assignment a LEFT JOIN cells c ON c.id=a.instance WHERE c.id IS NULL LIMIT 1");
  Check(!coverage.Step(),"placement does not cover exactly the mapped population");
  const auto top=json.String("{\"top\":"+Metadata(db,"top_json")+"}","$.top"),part=json.String(route,"$.part");
  db.Execute("CREATE TEMP TABLE clock_gates(cell TEXT PRIMARY KEY,site TEXT NOT NULL,input_site TEXT NOT NULL,cascaded INTEGER NOT NULL) WITHOUT ROWID; CREATE TEMP TABLE virtual_io(net TEXT PRIMARY KEY,id TEXT NOT NULL UNIQUE,direction TEXT NOT NULL,p_site TEXT NOT NULL,logical_port TEXT NOT NULL,logical_bit INTEGER NOT NULL,UNIQUE(logical_port,logical_bit,direction)) WITHOUT ROWID");
  auto gate_rows=db.Prepare("SELECT value FROM json_each(?,'$.clock_gates') ORDER BY key");gate_rows.BindText(1,route);
  auto gate_put=db.Prepare("INSERT INTO clock_gates VALUES(?,?,?,?)"),cell_exists=db.Prepare("SELECT 1 FROM cells WHERE id=?");
  Integer gates=0,ios=0;
  while(gate_rows.Step()) {
    const auto value=gate_rows.TypedText(0);(void)ClockGate(json,value);const auto cell=json.String(value,"$.logical_cell"),site=json.String(value,"$.physical_site");
    cell_exists.Reset();cell_exists.BindText(1,cell);Check(cell_exists.Step(),"clock gate has no logical cell");
    gate_put.BindText(1,cell);gate_put.BindText(2,site);gate_put.BindText(3,json.String(value,"$.input_site",&site));gate_put.BindInteger(4,json.Bool(value,"$.cascaded_input"));gate_put.Execute();++gates;
  }
  auto io_rows=db.Prepare("SELECT value FROM json_each(?,'$.virtual_io') ORDER BY key");io_rows.BindText(1,route);
  auto io_put=db.Prepare("INSERT INTO virtual_io VALUES(?,?,?,?,?,?)");
  while(io_rows.Step()) {
    const auto value=io_rows.TypedText(0);(void)VirtualIo(json,value);const auto direction=json.String(value,"$.direction"),psite=json.String(value,"$.p_site");
    Check((direction=="tx" || direction=="rx") && json.Int(value,"$.logical_bit")>=0 && psite!=json.String(value,"$.n_site"),"fixed I/O identity is invalid");
    io_put.BindText(1,json.String(value,"$.mapped_net"));io_put.BindText(2,json.String(value,"$.id"));io_put.BindText(3,direction);io_put.BindText(4,psite);io_put.BindText(5,json.String(value,"$.logical_port"));io_put.BindInteger(6,json.Int(value,"$.logical_bit"));io_put.Execute();++ios;
  }
  db.Execute("CREATE TEMP TABLE seen_bits(bit INTEGER PRIMARY KEY); CREATE TEMP TABLE logical_pins(instance TEXT NOT NULL,pin TEXT NOT NULL,driver INTEGER NOT NULL,site TEXT NOT NULL,PRIMARY KEY(instance,pin)) WITHOUT ROWID; CREATE INDEX temp.logical_sites ON logical_pins(site,driver); CREATE TEMP TABLE physical_sinks(site TEXT PRIMARY KEY,records INTEGER NOT NULL,delay REAL NOT NULL) WITHOUT ROWID");
  auto logical=db.Prepare("INSERT INTO logical_pins SELECT p.cell_id,CASE WHEN p.port_width=1 THEN p.port ELSE p.port||'['||p.port_bit||']' END,p.direction!=0,CASE WHEN g.cell IS NULL THEN a.physical_site WHEN p.port_width=1 AND p.port IN ('I0','I1') THEN g.input_site ELSE g.site END FROM cell_pins p JOIN physical.assignment a ON a.instance=p.cell_id LEFT JOIN clock_gates g ON g.cell=p.cell_id WHERE p.value_kind=0 AND p.net_bit=? AND NOT(p.direction=0 AND p.port_width=1 AND p.port='I0' AND COALESCE(g.cascaded,0)=1)");
  auto seen=db.Prepare("INSERT INTO seen_bits VALUES(?)");
  auto shape=db.Prepare("SELECT sum(driver),sum(1-driver) FROM logical_pins");
  auto driver_row=db.Prepare("SELECT instance,pin,site FROM logical_pins WHERE driver=1 ORDER BY instance,pin");
  auto io=db.Prepare("SELECT id,direction,p_site FROM virtual_io WHERE net=?");
  auto pins=db.Prepare("SELECT value FROM json_each(?,'$.pins') ORDER BY key");
  auto put_sink=db.Prepare("INSERT INTO physical_sinks VALUES(?,1,?) ON CONFLICT(site) DO UPDATE SET records=records+1,delay=max(delay,excluded.delay)");
  auto unbound=db.Prepare("SELECT 1 FROM physical_sinks p LEFT JOIN logical_pins l ON l.site=p.site AND l.driver=0 WHERE l.instance IS NULL LIMIT 1");
  auto sinks=db.Prepare("SELECT l.instance,l.pin,l.site,p.records,p.delay FROM logical_pins l LEFT JOIN physical_sinks p ON p.site=l.site WHERE l.driver=0 ORDER BY l.instance,l.pin");
  AtomicBinaryWriter endpoint_writer(endpoint_path,inputs);Sha256 endpoint_hash,net_hash;
  Integer nets=0,endpoints=0,physical_sinks=0,exact=0,shared=0,intra=0;double maximum=0;
  std::ifstream stream(inputs[3].path(),std::ios::binary);Check(bool(stream),"net stream is missing");std::string line;
  while(ReadLine(stream,line)) {
    net_hash.update(reinterpret_cast<const unsigned char*>(line.data()),line.size());++nets;Check(json.Int(line,"$.pip_count")>=0,"PIP count is invalid");
    const std::string empty;const auto kind=json.String(line,"$.kind",&empty),name=json.String(line,"$.net");
    if(kind=="static_vcc" || kind=="static_gnd") {Check(name==(kind=="static_vcc"?"GLOBAL_LOGIC1":"GLOBAL_LOGIC0") && json.String(line,"$.qualification")=="device-tied-static","static route identity is invalid");continue;}
    if(kind=="clock_internal") {Check(json.String(line,"$.qualification")=="lut2-prefilter-to-bufgce-v1","internal clock route is invalid");continue;}
    Integer bit=0;const auto parsed=std::from_chars(name.data()+std::min<std::size_t>(name.size(),1),name.data()+name.size(),bit);
    Check(name.size()>1 && name[0]=='n' && name[1]>='0' && name[1]<='9' && parsed.ec==std::errc{} && parsed.ptr==name.data()+name.size() && bit>=0,"route net does not encode a mapped bit");
    seen.BindInteger(1,bit);seen.Execute();db.Execute("DELETE FROM logical_pins; DELETE FROM physical_sinks");logical.Reset();logical.BindInteger(1,bit);logical.Execute();
    json.Expect(line,"$.pins","array");
    shape.Reset();Check(shape.Step(),"logical endpoints are absent");const auto drivers=shape.IsNull(0)?0:shape.TypedInteger(0),logical_sinks=shape.IsNull(1)?0:shape.TypedInteger(1);
    io.Reset();io.BindText(1,name);const bool virtual_io=io.Step();const auto direction=virtual_io?Text(io,1):"";
    std::string driver,driver_pin,driver_site,p_site;
    if(!virtual_io || direction=="tx") {
      Check(drivers==1 && (virtual_io?logical_sinks==0:logical_sinks>0),"routed bit lacks one mapped driver/sinks");driver_row.Reset();Check(driver_row.Step(),"mapped driver is missing");driver=Text(driver_row,0);driver_pin=Text(driver_row,1);driver_site=Text(driver_row,2);Check(!driver_row.Step(),"mapped driver repeats");
      if(virtual_io)p_site=Text(io,2);
    } else {Check(direction=="rx" && drivers==0 && logical_sinks>0,"RX endpoint set is invalid");driver="virtual_io:"+Text(io,0);driver_pin="O";driver_site=p_site=Text(io,2);}
    pins.Reset();pins.BindText(1,line);Integer sources=0;std::string physical_source;bool tx_sink=false;
    while(pins.Step()) {
      const auto value=pins.TypedText(0),site=json.String(value,"$.site");
      if(json.Bool(value,"$.is_output",false,false)){++sources;physical_source=site;}
      else {
        const auto delay=json.Number(value,"$.route_delay_ps");if(virtual_io && direction=="tx"){if(site==p_site)tx_sink=true;}
        else {put_sink.BindText(1,site);put_sink.BindReal(2,delay);put_sink.Execute();++physical_sinks;}
      }
    }
    Check(sources==1 && physical_source==driver_site,"physical driver site disagrees");
    if(virtual_io && direction=="tx"){Check(tx_sink,"TX fixed I/O sink is missing");continue;}
    unbound.Reset();Check(!unbound.Step(),"physical sink site has no logical binding");sinks.Reset();
    while(sinks.Step()) {
      const auto instance=Text(sinks,0),pin=Text(sinks,1),site=Text(sinks,2);double delay=0;std::string binding;
      if(!sinks.IsNull(3)) {delay=sinks.TypedReal(4)/1000.0;if(sinks.TypedInteger(3)==1){binding="exact-site-pin";++exact;}else{binding="shared-site-conservative-max";++shared;}}
      else {Check(site==driver_site,"logical sink has no physical binding");binding="intra-site-interconnect-zero";++intra;}
      const auto net="n"+std::to_string(bit);
      const auto encoded=JsonRecord(Object{{"id",net+":"+instance+"/"+pin},{"net",net},{"mapped_bit",bit},{"driver",Object{{"instance",driver},{"pin",driver_pin},{"site",driver_site}}},{"sink",Object{{"instance",instance},{"pin",pin},{"site",site}}},{"route_delay_ns",delay},{"binding",binding}}).Encode()+"\n";
      endpoint_writer.WriteBytes(encoded.data(),encoded.size());endpoint_hash.update(reinterpret_cast<const unsigned char*>(encoded.data()),encoded.size());++endpoints;maximum=std::max(maximum,delay);
    }
  }
  Check(nets==expected_nets && Digest(net_hash)==net_sha,"net stream count/digest disagrees");
  const auto endpoint_sha=Digest(endpoint_hash);
  Object counts{{"logical_endpoints",endpoints},{"physical_route_sinks",physical_sinks},{"exact_site_bindings",exact},{"shared_site_bindings",shared},{"intra_site_endpoints",intra},{"maximum_route_delay_ns",maximum}};
  Object coefficients;auto coefficient=db.Prepare("SELECT key,type,value FROM json_each(?,'$.timing.logic_coefficients_ps') ORDER BY key");coefficient.BindText(1,route);
  while(coefficient.Step()){const auto key=Text(coefficient,0),type=Text(coefficient,1);Check(type=="integer" || type=="real","logic coefficient is invalid");const auto value=coefficient.TypedReal(2);Check(std::isfinite(value) && value>=0,"logic coefficient is invalid");coefficients.emplace(key,type=="integer"?JsonRecord(coefficient.TypedInteger(2)):JsonRecord(value));}
  Check(coefficients.size()==8,"logic coefficient coverage is invalid");
  for(const char* name:{"ff_clock_to_q","carry_co","lut_a1","lut_a2","lut_a3","lut_a4","lut_a5","lut_a6"})Check(coefficients.count(name),"logic coefficient is missing");
  const auto qualification=JsonRecord(Object{{"provider","rapidwright-lightweight"},{"analysis","setup-route-only"},{"hold_analysis","unavailable"},{"hard_block_clock_timing","unqualified"},{"logic_coefficients_ps",coefficients}}).Encode();
  std::map<std::string,std::string> fields{{"part",JsonQuote(part)},{"top",JsonQuote(top)},{"qualification",qualification},{"summary",JsonRecord(counts).Encode()},
    {"schema",JsonQuote("emuflow.xilinx-routed-timing/v2")},{"status",JsonQuote("pass")},{"source",source},
    {"payloads",JsonRecord(Object{{"endpoints",Object{{"format","jsonl-object/v1"},{"path",payload_name},{"sha256",endpoint_sha},{"records",endpoints}}}}).Encode()}};
  const auto bounded=fields;fields.emplace("clock_gates","");fields.emplace("virtual_io","");
  AtomicBinaryWriter manifest_writer(manifest_path,inputs);Sha256 manifest_hash;
  const auto write=[&](const std::string& text){manifest_writer.WriteBytes(text.data(),text.size());manifest_hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());};
  write("{");bool first=true;
  for(const auto& field:fields) {
    if(!first)write(",");first=false;write(JsonQuote(field.first)+":");
    if(field.first=="clock_gates" || field.first=="virtual_io") {
      auto rows=db.Prepare("SELECT value FROM json_each(?1,?2) ORDER BY key");rows.BindText(1,route);rows.BindText(2,"$."+field.first);write("[");bool item_first=true;
      while(rows.Step()){if(!item_first)write(",");item_first=false;write(JsonRecord(field.first=="clock_gates"?ClockGate(json,rows.TypedText(0)):VirtualIo(json,rows.TypedText(0))).Encode());}write("]");
    } else write(field.second);
  }
  write("}\n");const auto manifest_sha=Digest(manifest_hash);transaction.Commit();
  endpoint_writer.Commit();manifest_writer.Commit();
  std::string header="{";first=true;for(const auto& field:bounded){if(!first)header+=',';first=false;header+=JsonQuote(field.first)+":"+field.second;}header+='}';
  AtomicBinaryWriter summary(summary_path,inputs);summary.stream()<<JsonRecord(Object{{"schema","emuflow.routed-endpoint-summary/v1"},{"status","pass"},{"implementation_id","native-routed-endpoints-v1"},{"header_json",header},{"clock_gates",gates},{"virtual_io",ios},{"timing_sha256",manifest_sha}}).Encode()<<'\n';summary.Commit();
}
}
