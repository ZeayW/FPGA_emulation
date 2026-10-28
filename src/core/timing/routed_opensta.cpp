#include "timing/routed_opensta.hpp"
#include "timing/opensta_export.hpp"
#include "storage/store.hpp"
#include "storage/schema/emuir_compact_schema.hpp"
#include "ir/resource_view.hpp"
#include "common/json_record.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <charconv>
#include <cctype>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <regex>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
using I=std::int64_t;
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
void Check(bool valid,const std::string& reason) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"routed OpenSTA staging "+reason);
}
std::string Text(SqliteStatement& s,int col) {
  auto value=s.TypedText(col);Check(!value.empty(),"has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
std::string Meta(SqliteDatabase& db,const char* name) {
  auto s=db.Prepare("SELECT value FROM source.metadata WHERE key=?");s.BindText(1,name);
  Check(s.Step(),"source metadata is missing");auto result=s.TypedText(0);Check(!s.Step(),"source metadata repeats");return result;
}
std::string Digest(Sha256& hash) {
  std::ostringstream s;s.imbue(std::locale::classic());s<<std::hex<<std::setfill('0');for(auto c:hash.finish())s<<std::setw(2)<<unsigned(c);return s.str();
}
std::string Padded(I value,unsigned width) {std::ostringstream s;s.imbue(std::locale::classic());s<<std::setfill('0')<<std::setw(width)<<value;return s.str();}
std::string DelayName(I seq) {return "__emuflow_rw_delay__"+Padded(seq,8);}
std::string DelayType(I seq) {return "EMUFLOW_RW_ROUTE_DELAY_"+Padded(seq,6);}
std::string DelayNet(I seq) {return "__emuflow_rw_delay_net__"+Padded(seq,8);}
class Json {
 public:
  explicit Json(SqliteDatabase& db):s_(db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)")){}
  void Read(const std::string& json,const std::string& path) {s_.Reset();s_.BindText(1,json);s_.BindText(2,path);Check(s_.Step(),"JSON field is absent");}
  std::string String(const std::string& json,const std::string& path) {Read(json,path);Check(s_.Text(0)=="text","text is invalid: "+path);return Text(s_,1);}
  I Int(const std::string& json,const std::string& path) {Read(json,path);Check(s_.Text(0)=="integer","integer is invalid: "+path);return s_.TypedInteger(1);}
  double Number(const std::string& json,const std::string& path) {Read(json,path);Check(s_.Text(0)=="real" || s_.Text(0)=="integer","delay is invalid");auto n=s_.TypedReal(1);Check(std::isfinite(n) && n>=0,"delay is invalid");return n;}
  bool Inverted(const std::string& json) {
    Read(json,"$.IS_C_INVERTED");const auto kind=s_.Text(0);
    return TimingParameterIsOne(kind,kind=="integer"?s_.Integer(1):0,kind=="text"?s_.Text(1):"");
  }
 private:SqliteStatement s_;
};
class Intern {
 public:
  Intern(SqliteDatabase& db,const char* table,const char* column):get_(db.Prepare(std::string("SELECT seq FROM ")+table+" WHERE "+column+"=?")),put_(db.Prepare(std::string("INSERT INTO ")+table+"("+column+") VALUES (?)")){}
  I operator()(const std::string& value) {
    auto found=cache_.find(value);if(found!=cache_.end())return found->second;
    get_.Reset();get_.BindText(1,value);
    if(!get_.Step()){put_.BindText(1,value);put_.Execute();get_.Reset();get_.BindText(1,value);Check(get_.Step(),"interned record is missing");}
    const auto seq=get_.TypedInteger(0);if(cache_.size()>=8192 || bytes_+value.size()>16*1024*1024){cache_.clear();bytes_=0;}
    if(value.size()<=1024*1024){cache_.emplace(value,seq);bytes_+=value.size();}return seq;
  }
 private:SqliteStatement get_,put_;std::map<std::string,I> cache_;std::size_t bytes_=0;
};
std::string Resources(const std::string& type,bool delay) {
  Object resources;for(auto key:kResourceFields)resources.emplace(key,I(0));auto t=type;
  std::transform(t.begin(),t.end(),t.begin(),[](unsigned char c){return std::toupper(c);});
  const auto starts=[&](const char* p){return t.rfind(p,0)==0;};
  if(!delay) {
    if(starts("RAMB36"))resources["bram18k"]=I(2);
    else if(starts("RAMB18"))resources["bram18k"]=I(1);
    else if(starts("URAM288"))resources["uram288"]=I(1);
    else if(starts("DSP48"))resources["dsp48"]=I(1);
    else if(starts("CARRY8"))resources["carry8"]=I(1);
    else if(starts("LUT"))resources["lut"]=I(1);
    else if(starts("FD"))resources["ff"]=I(1);
    else if(starts("IBUF") || starts("OBUF") || starts("IOBUF"))resources["io"]=I(1);
    else if(starts("BUFG") || starts("BUFH") || starts("BUFMR") || starts("MMCM") || starts("PLLE") || starts("STARTUPE"))resources["clock"]=I(1);
    else if(t!="GND" && t!="VCC" && t!="MUXF7" && t!="MUXF8" && t!="MUXF9")resources["other"]=I(1);
  }
  return JsonRecord(resources).Encode();
}
bool SafeInput(const std::string& type,const std::string& port) {
  if(type=="FDCE" || type=="FDPE")return port=="D" || port=="CE";
  if(type=="FDRE")return port=="D" || port=="CE" || port=="R";
  if(type=="FDSE")return port=="D" || port=="CE" || port=="S";
  return false;
}
bool Sequential(const std::string& type) {return type=="FDCE" || type=="FDPE" || type=="FDRE" || type=="FDSE" || type=="FDCE__NEG" || type=="FDPE__NEG" || type=="FDRE__NEG" || type=="FDSE__NEG" || type=="RAMB18E2" || type=="RAMB36E2" || type=="URAM288";}
bool Line(std::istream& stream,std::string& line) {
  line.clear();char ch;while(stream.get(ch)){Check(ch!='\0' && line.size()<16*1024*1024,"endpoint record is oversized");line+=ch;if(ch=='\n')return true;}
  Check(!stream.bad() && line.empty(),"endpoint stream is truncated");return false;
}
std::pair<std::string,I> Pin(const std::string& value) {
  auto port=value;I bit=0;
  const auto opening=value.rfind('[');
  if(opening!=std::string::npos && value.back()==']' && opening+2<value.size() && value.find_first_not_of("0123456789",opening+1)==value.size()-1) {
    auto result=std::from_chars(value.data()+opening+1,value.data()+value.size()-1,bit);
    Check(result.ec==std::errc{} && opening>0,"sink pin ordinal is invalid");port=value.substr(0,opening);
  }
  return {port,bit};
}
void LibertyCell(std::ostream& out,I seq,double delay) {
  out<<"  cell ("<<DelayType(seq)<<") {\n    area : 1.0;\n    pin (A) {\n      direction : input;\n      capacitance : 0.001;\n    }\n    pin (Y) {\n      direction : output;\n      timing () {\n        related_pin : \"A\";\n        timing_sense : non_unate;\n";
  for(const auto& item:std::vector<std::pair<const char*,double>>{{"cell_rise",delay},{"cell_fall",delay},{"rise_transition",.01},{"fall_transition",.01}})
    out<<"        "<<item.first<<" (scalar) {\n          values (\""<<std::setprecision(12)<<item.second<<"\");\n        }\n";
  out<<"      }\n    }\n  }\n\n";
}
}

void ValidateRoutedOpenstaGraph(SqliteDatabase& db,I endpoints,I types) {
  auto count=db.Prepare("SELECT (SELECT count(*) FROM cell_records WHERE substr(type,1,23)='EMUFLOW_RW_ROUTE_DELAY_'),(SELECT count(*) FROM routed_delay_types)");
  Check(count.Step() && count.TypedInteger(0)==endpoints && count.TypedInteger(1)==types,"delay cell/type coverage disagrees");
  auto invalid=db.Prepare("SELECT c.id FROM cell_records c LEFT JOIN endpoint_records e INDEXED BY endpoint_owner ON e.instance_seq=c.seq LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE substr(c.type,1,23)='EMUFLOW_RW_ROUTE_DELAY_' GROUP BY c.seq HAVING count(e.net_seq)!=2 OR sum(e.role=1 AND p.name='A' AND e.bit=0)!=1 OR sum(e.role=0 AND p.name='Y' AND e.bit=0)!=1 OR count(DISTINCT e.net_seq)!=2 LIMIT 1");
  Check(!invalid.Step(),"delay cell A/Y connectivity is invalid");
  auto models=db.Prepare("SELECT c.id FROM cell_records c LEFT JOIN routed_delay_types t ON t.seq=CAST(substr(c.type,24) AS INTEGER) WHERE substr(c.type,1,23)='EMUFLOW_RW_ROUTE_DELAY_' AND (t.seq IS NULL OR c.type!=printf('EMUFLOW_RW_ROUTE_DELAY_%06d',t.seq) OR c.attributes_seq!=t.attributes_seq) LIMIT 1");
  Check(!models.Step(),"delay type is not bound to its emitted cell");
  auto payloads=db.Prepare("SELECT t.seq FROM routed_delay_types t LEFT JOIN payloads p ON p.seq=t.attributes_seq WHERE p.seq IS NULL OR coalesce(json_type(p.value,'$.emuflow_route_delay_ns'),'null') NOT IN ('integer','real') OR json_extract(p.value,'$.emuflow_route_delay_ns')<0 OR json_extract(p.value,'$.emuflow_route_delay_ns')>1.7976931348623157e308 LIMIT 1");
  Check(!payloads.Step(),"delay type payload is absent or invalid");
}

void StageRoutedOpensta(const std::vector<ArtifactRef>& inputs,const std::string& emuir,
                       const std::string& liberty,const std::string& summary,const std::string* preloaded_manifest) {
  Check(inputs.size()==4,"source coverage is invalid");CheckedRequestReader request(inputs[3].path(),65536);
  Check(request.Read<std::uint64_t>()==0x314f5352464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  for(unsigned i=0;i<3;++i)if(!(ReadFileGeneration(request)==inputs[i].generation()))throw StageError(ErrorCode::kMixedGeneration,"routed OpenSTA input generation changed");
  const auto config=request.ReadString32(false,60000);request.RequireEnd();
  SqliteArtifactWriter writer(emuir,"emuir-compact-v2",inputs);
  AtomicBinaryWriter library(liberty,inputs),report(summary,inputs);Object result;
  library.stream().imbue(std::locale::classic());
  {
    auto& db=writer.database();db.Execute(kEmuirCompactDDL);db.Execute("PRAGMA cache_size=-32768; PRAGMA temp.cache_size=-8192");
    {auto attach=db.Prepare("ATTACH DATABASE ? AS source");attach.BindText(1,SqliteImmutableUri(inputs[0].path()));attach.Execute();}
    SqliteTransaction transaction(db);Json json(db);
    std::string loaded;Sha256 manifest_hash;
    if(preloaded_manifest)manifest_hash.update(reinterpret_cast<const unsigned char*>(preloaded_manifest->data()),preloaded_manifest->size());
    else {
      std::ifstream manifest_stream(inputs[1].path(),std::ios::binary);Check(bool(manifest_stream),"timing manifest is missing");char chunk[65536];
      while(manifest_stream.read(chunk,sizeof(chunk))||manifest_stream.gcount()) {
        const auto size=std::size_t(manifest_stream.gcount());loaded.append(chunk,size);manifest_hash.update(reinterpret_cast<const unsigned char*>(chunk),size);
      }
      Check(!manifest_stream.bad(),"timing manifest read failed");
    }
    const auto &timing=preloaded_manifest?*preloaded_manifest:loaded;
    Check(Digest(manifest_hash)==json.String(config,"$.timing_sha256"),"timing manifest read/seal failed");
    Check(json.String(timing,"$.schema")=="emuflow.xilinx-routed-timing/v2" && json.String(timing,"$.status")=="pass","timing manifest schema/status is invalid");
    Check(json.String(timing,"$.source.mapped_sha256")==json.String(config,"$.mapped_sha256"),"mapped source seal disagrees");
    for(const char* key:{"mapped_sha256","packed_sha256","placement_sha256","route_sha256"}) {const auto value=json.String(timing,std::string("$.source.")+key);Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,"source digest is invalid");}
    Check(json.String(timing,"$.payloads.endpoints.format")=="jsonl-object/v1","endpoint payload format is invalid");
    const auto payload_name=json.String(timing,"$.payloads.endpoints.path");
    Check(!payload_name.empty()&&std::filesystem::path(payload_name).filename()==payload_name&&payload_name!="."&&payload_name!=".."&&
      std::filesystem::weakly_canonical(std::filesystem::path(inputs[1].path()).parent_path()/payload_name)==inputs[2].path()&&
      std::filesystem::path(inputs[2].path()).parent_path()==std::filesystem::path(inputs[1].path()).parent_path(),"endpoint payload belongs to another timing owner");
    Object coefficients;std::set<std::string> expected_coefficients{"ff_clock_to_q","carry_co","lut_a1","lut_a2","lut_a3","lut_a4","lut_a5","lut_a6"};
    auto coefficient=db.Prepare("SELECT key,type,value FROM json_each(?,'$.qualification.logic_coefficients_ps')");coefficient.BindText(1,timing);
    while(coefficient.Step()){const auto key=Text(coefficient,0),kind=coefficient.TypedText(1);Check(expected_coefficients.erase(key)==1 && (kind=="integer" || kind=="real"),"logic coefficient identity/type is invalid");const auto value=coefficient.TypedReal(2);Check(std::isfinite(value) && value>=0,"logic coefficient is invalid");coefficients.emplace(key,value);}
    Check(expected_coefficients.empty(),"logic coefficient coverage disagrees");
    db.Execute("CREATE TEMP TABLE work_io(id TEXT PRIMARY KEY,port TEXT NOT NULL,bit INTEGER NOT NULL,direction TEXT NOT NULL,UNIQUE(port,bit,direction)) WITHOUT ROWID");
    auto io_kind=db.Prepare("SELECT json_type(?,'$.virtual_io')");io_kind.BindText(1,timing);Check(io_kind.Step() && (io_kind.IsNull(0) || io_kind.TypedText(0)=="array"),"virtual I/O collection is invalid");
    auto io_rows=db.Prepare("SELECT value FROM json_each(?,'$.virtual_io')");io_rows.BindText(1,timing);auto io_put=db.Prepare("INSERT INTO work_io VALUES(?,?,?,?)");
    while(io_rows.Step()){const auto value=io_rows.TypedText(0),id=json.String(value,"$.id"),port=json.String(value,"$.logical_port"),direction=json.String(value,"$.direction"),p_site=json.String(value,"$.p_site"),n_site=json.String(value,"$.n_site");const auto bit=json.Int(value,"$.logical_bit");(void)json.Number(value,"$.route_delay_ns");Check(bit>=0 && (direction=="tx" || direction=="rx") && p_site!=n_site && json.String(value,"$.measurement")=="fixed-iob-routed-boundary","virtual I/O binding is invalid");io_put.BindText(1,id);io_put.BindText(2,port);io_put.BindInteger(3,bit);io_put.BindText(4,direction);io_put.Execute();}
    const auto top=json.String("{\"top\":"+Meta(db,"top_json")+"}","$.top"),ports=Meta(db,"ports_json");
    Check(top==json.String(timing,"$.top"),"top identity disagrees");
    std::set<std::string> supported,clocks,resets;
    auto keys=db.Prepare("SELECT value FROM json_each(?1,?2)");keys.BindText(1,config);keys.BindText(2,"$.base_types");while(keys.Step())supported.insert(Text(keys,0));
    Check(!supported.empty() && supported.size()<=256,"base primitive inventory is invalid");
    auto clock_rows=db.Prepare("SELECT key FROM json_each(?,'$.clocks') ORDER BY key");clock_rows.BindText(1,config);while(clock_rows.Step())clocks.insert(Text(clock_rows,0));
    db.Execute("CREATE TEMP TABLE work_routes(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,cell_seq INTEGER NOT NULL,port TEXT NOT NULL,bit INTEGER NOT NULL,old_bit INTEGER NOT NULL,new_bit INTEGER,delay_seq INTEGER NOT NULL,UNIQUE(cell_seq,port,bit)); CREATE TEMP TABLE work_delays(seq INTEGER PRIMARY KEY,delay REAL NOT NULL UNIQUE); CREATE TABLE routed_delay_types(seq INTEGER PRIMARY KEY,attributes_seq INTEGER NOT NULL UNIQUE)");
    auto lookup=db.Prepare("SELECT p.cell_seq,p.direction,p.value_kind,p.net_bit FROM source.cell_pins p WHERE p.cell_id=? AND p.port=? AND p.port_bit=?");
    auto type_get=db.Prepare("SELECT seq FROM work_delays WHERE delay=?"),type_put=db.Prepare("INSERT INTO work_delays VALUES(?,?)"),append_route=db.Prepare("INSERT INTO work_routes(seq,id,cell_seq,port,bit,old_bit,delay_seq) VALUES(?,?,?,?,?,?,?)");
    auto payload=Intern(db,"payloads","value");auto pins=Intern(db,"pin_names","name");const auto empty=payload("{}"),no_constants=payload("[]");
    Sha256 endpoint_hash;std::ifstream endpoints(inputs[2].path(),std::ios::binary);Check(bool(endpoints),"endpoint stream is missing");std::string line;I routed=0,unique_delays=0;double maximum_delay=0;
    while(Line(endpoints,line)) {
      endpoint_hash.update(reinterpret_cast<const unsigned char*>(line.data()),line.size());
      const auto cell=json.String(line,"$.sink.instance"),pin=json.String(line,"$.sink.pin"),id=json.String(line,"$.id");const auto identity=Pin(pin);const auto delay=json.Number(line,"$.route_delay_ns");
      maximum_delay=std::max(maximum_delay,delay);
      lookup.Reset();lookup.BindText(1,cell);lookup.BindText(2,identity.first);lookup.BindInteger(3,identity.second);
      Check(lookup.Step(),"endpoint sink is absent");const auto source_seq=lookup.TypedInteger(0);
      Check((lookup.TypedInteger(1)==0 || lookup.TypedInteger(1)==2) && lookup.TypedInteger(2)==0 && lookup.TypedInteger(3)==json.Int(line,"$.mapped_bit"),"endpoint is not bound to its mapped input bit");const auto old=lookup.TypedInteger(3);Check(!lookup.Step(),"endpoint pin repeats");
      type_get.Reset();type_get.BindReal(1,delay);I type;
      if(type_get.Step())type=type_get.TypedInteger(0);else {type=unique_delays++;type_put.BindInteger(1,type);type_put.BindReal(2,delay);type_put.Execute();}
      append_route.BindInteger(1,routed++);append_route.BindText(2,id);append_route.BindInteger(3,source_seq);append_route.BindText(4,identity.first);append_route.BindInteger(5,identity.second);append_route.BindInteger(6,old);append_route.BindInteger(7,type);append_route.Execute();
    }
    Check(routed==json.Int(timing,"$.payloads.endpoints.records") && routed==json.Int(timing,"$.summary.logical_endpoints") && Digest(endpoint_hash)==json.String(timing,"$.payloads.endpoints.sha256"),"endpoint count/digest disagrees");
    const auto declared_maximum=json.Number(timing,"$.summary.maximum_route_delay_ns");Check(std::abs(maximum_delay-declared_maximum)<=std::max(1e-12,1e-9*std::max(maximum_delay,declared_maximum)),"maximum endpoint delay disagrees");
    auto max_row=db.Prepare("SELECT max(value) FROM (SELECT coalesce(max(seq)+2,1) AS value FROM source.netnames UNION ALL SELECT b.value FROM json_each(?1) p,json_each(p.value,'$.bits') b WHERE b.type='integer')");max_row.BindText(1,ports);Check(max_row.Step(),"source bit inventory is absent");const auto maximum=max_row.TypedInteger(0);Check(maximum<std::numeric_limits<I>::max()-routed,"routed bit inventory overflows");
    auto allocation=db.Prepare("SELECT r.seq FROM work_routes r JOIN source.cells c ON c.seq=r.cell_seq ORDER BY c.id,r.port,r.bit");
    auto allocate=db.Prepare("UPDATE work_routes SET new_bit=? WHERE seq=?");I allocated=0;
    while(allocation.Step()){allocate.BindInteger(1,maximum+1+allocated++);allocate.BindInteger(2,allocation.TypedInteger(0));allocate.Execute();}
    Check(allocated==routed,"routed allocation coverage disagrees");
    db.Execute("CREATE UNIQUE INDEX temp.routed_bit_lookup ON work_routes(new_bit)");
    auto cell_put=db.Prepare("INSERT INTO cell_records VALUES(?,?,NULL,1,?,?,?,?,?,?)");
    auto constants=db.Prepare("SELECT port,port_bit,constant FROM source.cell_pins WHERE cell_seq=? AND value_kind=1 ORDER BY port,port_bit");
    auto originals=db.Prepare("SELECT id,type,record_json,seq FROM source.cells ORDER BY id");auto delay_cells=db.Prepare("SELECT seq,delay_seq FROM work_routes ORDER BY printf('__emuflow_rw_delay__%08d',seq)");
    auto parameter=db.Prepare("SELECT json_type(?1),json_extract(?1,'$.type'),json_type(?1,'$.parameters'),json_extract(?1,'$.parameters'),json_type(?1,'$.attributes'),json_extract(?1,'$.attributes')");
    auto pin_rows=db.Prepare("SELECT cell_id,port,port_bit,direction,cell_seq,port_width,value_kind,net_bit,constant FROM source.cell_pins ORDER BY cell_id,port,port_bit");bool have_pin=pin_rows.Step();
    bool original=originals.Step(),added=delay_cells.Step();I cell_count=0,falling=0;
    std::map<std::string,std::map<std::string,std::set<std::pair<std::string,I>>>> pin_sets;
    std::set<std::string> original_types;
    auto delays=db.Prepare("SELECT delay FROM work_delays WHERE seq=?");
    while(original || added) {
      const auto original_id=original?Text(originals,0):"",added_id=added?DelayName(delay_cells.TypedInteger(0)):"";
      Check(!original || !added || original_id!=added_id,"delay identity collides with a source cell");
      const bool take_original=original && (!added || original_id<added_id);std::string id,type,parameters="{}",attributes="{}",constant_json="[]";
      if(take_original) {
        id=original_id;type=Text(originals,1);Check(supported.count(type) || type=="LUT6_2" || type=="CARRY8" || type=="MUXF9" || type=="DSP48E2" || type=="RAMB18E2" || type=="RAMB36E2" || type=="URAM288","primitive is not supported: "+type);
        parameter.Reset();parameter.BindText(1,originals.TypedText(2));Check(parameter.Step() && parameter.Text(0)=="object" && parameter.Text(1)==type,"cell payload/type disagrees");
        Check((parameter.IsNull(2) || parameter.Text(2)=="object") && (parameter.IsNull(4) || parameter.Text(4)=="object"),"cell parameter/attribute shape is invalid");
        if(parameter.Text(2)=="object")parameters=parameter.TypedText(3);if(parameter.Text(4)=="object")attributes=parameter.TypedText(5);
        if((type=="FDCE" || type=="FDPE" || type=="FDRE" || type=="FDSE") && json.Inverted(parameters)){type+="__NEG";++falling;}
        original_types.insert(type);
        Array values;constants.Reset();constants.BindInteger(1,originals.TypedInteger(3));while(constants.Step()){auto value=Text(constants,2);std::transform(value.begin(),value.end(),value.begin(),[](unsigned char c){return std::tolower(c);});values.emplace_back(Object{{"port",Text(constants,0)},{"bit",constants.TypedInteger(1)},{"value",value}});}constant_json=JsonRecord(values).Encode();
        std::string previous_port;I port_width=0,port_direction=0,next_pin=0;
        while(have_pin && Text(pin_rows,0)==id) {const auto port=Text(pin_rows,1);const auto bit=pin_rows.TypedInteger(2),direction=pin_rows.TypedInteger(3),width=pin_rows.TypedInteger(5),kind=pin_rows.TypedInteger(6);Check(direction>=0 && direction<=2 && bit>=0 && width>0 && bit<width && pin_rows.TypedInteger(4)==originals.TypedInteger(3),"source pin owner/direction/ordinal is invalid");
          if(port!=previous_port){Check(previous_port.empty() || next_pin==port_width,"source port coverage is incomplete");previous_port=port;next_pin=0;port_width=width;port_direction=direction;}
          Check(bit==next_pin++ && width==port_width && direction==port_direction,"source port pin order/shape disagrees");
          Check(kind==0 || kind==1,"source pin value kind is invalid");
          if(kind==0)Check(pin_rows.IsNull(8) && pin_rows.TypedInteger(7)>=2,"source net pin is invalid");
          else {const auto constant=Text(pin_rows,8);Check(pin_rows.IsNull(7) && constant.size()==1 && std::string("01xXzZ").find(constant[0])!=std::string::npos,"source constant pin is invalid");}
          if(direction!=1)pin_sets[type]["inputs"].emplace(port,bit);if(direction!=0)pin_sets[type]["outputs"].emplace(port,bit);pins(port);have_pin=pin_rows.Step();}
        Check(previous_port.empty() || next_pin==port_width,"source port coverage is incomplete");
        if(have_pin)Check(Text(pin_rows,0)>id,"source pin owner is orphaned");original=originals.Step();
      } else {
        id=added_id;type=DelayType(delay_cells.TypedInteger(1));delays.Reset();delays.BindInteger(1,delay_cells.TypedInteger(1));Check(delays.Step(),"delay type is missing");attributes=JsonRecord(Object{{"emuflow_route_delay_ns",delays.TypedReal(0)}}).Encode();added=delay_cells.Step();
      }
      cell_put.BindInteger(1,cell_count++);cell_put.BindText(2,id);cell_put.BindText(3,type);cell_put.BindInteger(4,payload(Resources(type,!take_original)));cell_put.BindInteger(5,payload(parameters));cell_put.BindInteger(6,payload(attributes));cell_put.BindInteger(7,payload(constant_json));cell_put.BindInteger(8,empty);cell_put.Execute();
    }
    Check(!have_pin,"source pin owner is orphaned");pins("A");pins("Y");
    auto delay_attrs=db.Prepare("INSERT INTO routed_delay_types VALUES(?,?)");auto delay_values=db.Prepare("SELECT seq,delay FROM work_delays ORDER BY seq");
    library.stream()<<"library (rapidwright_ultrascaleplus_route_delays_v1) {\n  delay_model : \"table_lookup\";\n  time_unit : \"1ns\";\n  voltage_unit : \"1V\";\n  capacitive_load_unit (1,pF);\n  input_threshold_pct_rise : 50;\n  input_threshold_pct_fall : 50;\n  output_threshold_pct_rise : 50;\n  output_threshold_pct_fall : 50;\n  slew_lower_threshold_pct_rise : 20;\n  slew_lower_threshold_pct_fall : 20;\n  slew_upper_threshold_pct_rise : 80;\n  slew_upper_threshold_pct_fall : 80;\n\n";
    while(delay_values.Step()){auto seq=delay_values.TypedInteger(0);auto value=delay_values.TypedReal(1);delay_attrs.BindInteger(1,seq);delay_attrs.BindInteger(2,payload(JsonRecord(Object{{"emuflow_route_delay_ns",value}}).Encode()));delay_attrs.Execute();LibertyCell(library.stream(),seq,value);}library.stream()<<"}\n";
    db.Execute("CREATE TEMP TABLE work_endpoints(net_bit INTEGER NOT NULL,role INTEGER NOT NULL,seq INTEGER NOT NULL,instance_seq INTEGER,port_seq INTEGER NOT NULL,bit INTEGER NOT NULL,PRIMARY KEY(net_bit,role,seq)) WITHOUT ROWID");
    auto add_endpoint=db.Prepare("INSERT INTO work_endpoints VALUES(?,?,?,NULL,?,?)");auto port_put=db.Prepare("INSERT INTO port_records VALUES(?,?)");I endpoint_seq=0,port_seq=0;
    const std::regex clock_pattern("(^|[_/])(clk|clock)([_/]|$)",std::regex::icase),reset_pattern("(^|[_/])(rst|reset|aresetn?)([_/]|$)",std::regex::icase);
    auto port_rows=db.Prepare("SELECT key,value FROM json_each(?) ORDER BY key");port_rows.BindText(1,ports);std::set<std::string> port_names;
    while(port_rows.Step()) {
      const auto name=Text(port_rows,0),record=port_rows.TypedText(1),direction=json.String(record,"$.direction");port_names.insert(name);
      if(json.Int(config,"$.clock_count")==0 && std::regex_search(name,clock_pattern))clocks.insert(name);
      if(std::regex_search(name,reset_pattern))resets.insert(name);
      auto bits=db.Prepare("SELECT key,type,value FROM json_each(?,'$.bits') ORDER BY key");bits.BindText(1,record);Array constant_bits;I width=0;
      Check(direction=="input" || direction=="output" || direction=="inout","top port direction is invalid");
      while(bits.Step()){const auto ordinal=bits.TypedInteger(0);Check(ordinal==width++,"top bit order is invalid");
        if(bits.Text(1)=="text"){Check(direction!="input","input top port is constant");auto value=Text(bits,2);std::transform(value.begin(),value.end(),value.begin(),[](unsigned char c){return std::tolower(c);});constant_bits.emplace_back(Object{{"bit",ordinal},{"value",value}});continue;}
        Check(bits.Text(1)=="integer","top bit is invalid");for(I role=0;role<2;++role)if(direction=="inout" || (role==0 && direction=="input") || (role==1 && direction=="output")){add_endpoint.BindInteger(1,bits.TypedInteger(2));add_endpoint.BindInteger(2,role);add_endpoint.BindInteger(3,endpoint_seq++);add_endpoint.BindInteger(4,pins(name));add_endpoint.BindInteger(5,ordinal);add_endpoint.Execute();}}
      Check(width>0,"top port is empty");port_put.BindInteger(1,port_seq++);port_put.BindText(2,JsonRecord(Object{{"id",name},{"name",name},{"direction",direction},{"width",width},{"clock",bool(clocks.count(name))},{"reset",bool(resets.count(name))},{"constant_connections",constant_bits}}).Encode());port_put.Execute();
    }
    for(const auto& name:clocks)Check(port_names.count(name),"explicit clock is absent");
    auto add_cells=db.Prepare("WITH roles(role) AS(SELECT 0 UNION ALL SELECT 1),records AS(SELECT coalesce(r.new_bit,p.net_bit) AS net_bit,roles.role,c.seq AS owner,c.id,p.port,p.port_bit AS bit FROM source.cell_pins p JOIN cell_records c ON c.id=p.cell_id LEFT JOIN work_routes r ON r.cell_seq=p.cell_seq AND r.port=p.port AND r.bit=p.port_bit CROSS JOIN roles WHERE p.value_kind=0 AND (p.direction=2 OR (p.direction=1 AND roles.role=0) OR (p.direction=0 AND roles.role=1)) UNION ALL SELECT r.old_bit,1,c.seq,c.id,'A',0 FROM work_routes r JOIN cell_records c ON c.id=printf('__emuflow_rw_delay__%08d',r.seq) UNION ALL SELECT r.new_bit,0,c.seq,c.id,'Y',0 FROM work_routes r JOIN cell_records c ON c.id=printf('__emuflow_rw_delay__%08d',r.seq)) INSERT INTO work_endpoints SELECT r.net_bit,r.role,?+row_number() OVER(ORDER BY r.id,r.port,r.bit,r.role)-1,r.owner,p.seq,r.bit FROM records r JOIN pin_names p ON p.name=r.port");add_cells.BindInteger(1,endpoint_seq);add_cells.Execute();
    auto work=db.Prepare("SELECT e.net_bit,e.role,e.instance_seq,e.port_seq,e.bit,c.type,p.name FROM work_endpoints e LEFT JOIN cell_records c ON c.seq=e.instance_seq JOIN pin_names p ON p.seq=e.port_seq ORDER BY e.net_bit,e.role,e.seq");
    auto source_name=db.Prepare("SELECT id FROM source.netnames WHERE seq=?"),new_name=db.Prepare("SELECT seq FROM work_routes WHERE new_bit=?");
    auto net_put=db.Prepare("INSERT INTO net_records VALUES(?,?,?, ?,?,?,0,'',?)"),net_finish=db.Prepare("UPDATE net_records SET fanout=?,cut_class=? WHERE seq=?"),endpoint_put=db.Prepare("INSERT INTO endpoint_records VALUES(?,?,?,?,?,?,?)"),alias=db.Prepare("INSERT INTO alias_records VALUES(?,0,?)");
    db.Execute("CREATE TEMP TABLE work_warnings(value TEXT PRIMARY KEY) WITHOUT ROWID");auto warning=db.Prepare("INSERT OR IGNORE INTO work_warnings VALUES(?)");warning.BindText(1,"RapidWright route delays are exact per-sink setup arcs; hold timing and hard-block/clock intrinsic timing remain unqualified");warning.Execute();
    I net_count=0,endpoint_count=0;bool next=work.Step();
    while(next) {
      const auto bit=work.TypedInteger(0);source_name.Reset();source_name.BindInteger(1,bit-2);const bool named=source_name.Step();new_name.Reset();new_name.BindInteger(1,bit);const bool generated=new_name.Step();
      const auto canonical=named?Text(source_name,0):(generated?DelayNet(new_name.TypedInteger(0)):"$bit_"+std::to_string(bit));auto id=canonical;
      auto exists=db.Prepare("SELECT 1 FROM net_records WHERE id=?");exists.BindText(1,id);if(exists.Step())id+="#"+std::to_string(bit);
      net_put.BindInteger(1,net_count);net_put.BindText(2,id);if(id==canonical)net_put.BindNull(3);else net_put.BindText(3,canonical);net_put.BindInteger(4,id==canonical?1:0);if(generated)net_put.BindNull(5);else net_put.BindInteger(5,bit);net_put.BindInteger(6,0);net_put.BindInteger(7,empty);net_put.Execute();
      if(named && !generated){alias.BindInteger(1,net_count);alias.BindText(2,canonical);alias.Execute();}
      I counts[2]={0,0};bool first_top=false,first_sequential=false,all_safe=true;
      while(next && work.TypedInteger(0)==bit) {
        const auto role=work.TypedInteger(1);if(role==0 && !counts[0]){first_top=work.IsNull(2);first_sequential=!first_top && Sequential(work.TypedText(5));}
        if(role==1)all_safe=all_safe && !work.IsNull(2) && SafeInput(work.IsNull(5)?"":work.TypedText(5),work.TypedText(6));
        endpoint_put.BindInteger(1,net_count);endpoint_put.BindInteger(2,role);endpoint_put.BindInteger(3,counts[role]++);if(work.IsNull(2))endpoint_put.BindNull(4);else endpoint_put.BindInteger(4,work.TypedInteger(2));endpoint_put.BindInteger(5,work.TypedInteger(3));endpoint_put.BindInteger(6,work.TypedInteger(4));endpoint_put.BindInteger(7,empty);endpoint_put.Execute();++endpoint_count;next=work.Step();
      }
      std::string cut=clocks.count(canonical)?"clock":resets.count(canonical)?"reset":counts[0]>1?"multi_driver":!counts[0]?"undriven":first_top?"primary_input":first_sequential?"register_output":counts[1] && all_safe?"register_input":"combinational";
      if(counts[0]>1 && cut=="multi_driver"){warning.BindText(1,"net '"+id+"' has "+std::to_string(counts[0])+" drivers");warning.Execute();}
      net_finish.BindInteger(1,counts[1]);net_finish.BindText(2,cut);net_finish.BindInteger(3,net_count++);net_finish.Execute();
    }
    auto clock_put=db.Prepare("INSERT INTO clocks VALUES(?,?)");I clock_seq=0;for(const auto& name:clocks){clock_put.BindInteger(1,clock_seq++);clock_put.BindText(2,JsonRecord(Object{{"id",name},{"name",name},{"source_port",name},{"period_ns",JsonRecord()}}).Encode());clock_put.Execute();}
    Array warnings;auto warning_rows=db.Prepare("SELECT value FROM work_warnings ORDER BY value");while(warning_rows.Step())warnings.emplace_back(warning_rows.TypedText(0));
    auto metadata=db.Prepare("INSERT INTO metadata VALUES(?,?)");
    const Object counts{{"cell_records",cell_count},{"net_records",net_count},{"endpoint_records",endpoint_count}};
    for(const auto& item:std::map<std::string,std::string>{{"storage_schema","emuflow.emuir-sqlite/v2"},{"logical_schema","emuflow.emuir/v1"},{"design_json",JsonRecord(Object{{"name",top},{"top",top},{"source_format","xilinx-mapped-sqlite-index"},{"source",json.String(config,"$.source_path")},{"creator",json.String("{\"creator\":"+Meta(db,"creator_json")+"}","$.creator")}}).Encode()},{"warnings_json",JsonRecord(warnings).Encode()},{"record_counts_json",JsonRecord(counts).Encode()},{"routed_opensta_feature_schema","emuflow.routed-opensta-types/v1"}}){metadata.BindText(1,item.first);metadata.BindText(2,item.second);metadata.Execute();}
    db.Execute("CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit)");
    // Independent final-record coverage: never trust insertion counters alone.
    ValidateRoutedOpenstaGraph(db,routed,unique_delays);
    Object sets;for(const auto& cell:pin_sets){Object roles;for(const auto& role:cell.second){Array values;for(const auto& pin:role.second)values.emplace_back(Array{pin.first,pin.second});roles.emplace(role.first,values);}if(!roles.count("inputs"))roles.emplace("inputs",Array{});if(!roles.count("outputs"))roles.emplace("outputs",Array{});sets.emplace(cell.first,roles);}
    Array inventory;for(const auto& type:original_types)inventory.emplace_back(type);
    result={{"schema","emuflow.routed-opensta-summary/v1"},{"status","pass"},{"implementation_id","native-routed-opensta-v1"},{"inserted_route_delay_cells",routed},{"unique_route_delay_cells",unique_delays},{"falling_edge_ff_instances",falling},{"pin_sets",sets},{"counts",counts},{"top",top},{"original_cell_types",inventory}};
    result.emplace("logic_coefficients_ps",coefficients);
    const auto encoded=JsonRecord(result).Encode();Check(encoded.size()<=65536,"bounded summary is oversized");report.stream()<<encoded<<'\n';transaction.Commit();
  }
  writer.Commit();library.Commit();report.Commit();
}
}
