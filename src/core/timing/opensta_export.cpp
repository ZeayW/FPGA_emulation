#include "timing/opensta_export.hpp"
#include "ir/emuir_view.hpp"
#include "common/json_record.hpp"
#include <algorithm>
#include <cctype>
#include <map>
#include <limits>
#include <memory>
#include <set>
#include <sstream>

namespace emuflow::core {
bool TimingParameterIsOne(const std::string& kind,std::int64_t integer,const std::string& raw) {
  if(kind=="integer")return integer==1;
  if(kind!="text" || raw.empty())return false;
  auto value=raw;const int base=value.find_first_not_of("01")==std::string::npos?2:10;
  if(base==10 && value.find('_')!=std::string::npos) {
    std::string digits;
    for(std::size_t i=0;i<value.size();++i) {
      if(value[i]=='_') {
        if(i==0 || i+1==value.size() || value[i-1]<'0' || value[i-1]>'9' || value[i+1]<'0' || value[i+1]>'9')return false;
      } else digits+=value[i];
    }
    value=std::move(digits);
  }
  try {
    std::size_t end=0;const auto number=std::stoll(value,&end,base);
    while(end<value.size() && std::isspace(static_cast<unsigned char>(value[end])))++end;
    return end==value.size() && number==1;
  } catch(const std::exception&) {return false;}
}
namespace {
using I=std::int64_t;
using Pin=std::pair<std::string,I>;
using Pins=std::map<Pin,std::string>;
void Check(bool valid,const char* reason) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,std::string("OpenSTA export ")+reason);
}
std::string Text(SqliteStatement& s,int col) {
  auto value=s.TypedText(col);Check(!value.empty(),"has an empty identity");(void)JsonQuoteUtf8(value);return value;
}
std::string Identifier(const std::string& value) {return "\\"+value+" ";}
std::string Wire(I net) {return "__emuflow_net_"+std::to_string(net);}
std::string Hex(const std::string& value) {
  static constexpr char digits[]="0123456789abcdef";std::string result;result.reserve(value.size()*2);
  for(unsigned char c:value){result+=digits[c>>4];result+=digits[c&15];}return result;
}
std::string Escaped(std::string value) {
  std::string result;for(char c:value){result+=c;if(c=='\\')result+='\\';}return result;
}
std::string PinName(const Pins& pins,const Pin& pin) {
  auto next=pins.upper_bound({pin.first,std::numeric_limits<I>::max()});
  Check(next!=pins.begin(),"pin width is missing");--next;
  return next->first.second==0?pin.first:pin.first+"__"+std::to_string(pin.second);
}
struct Port {I seq,width;std::string name,direction;std::vector<std::pair<I,std::string>> constants;};
class PinsReader {
 public:
  explicit PinsReader(SqliteDatabase& db):db_(db),constants_(db.Prepare("SELECT json_type(?1),json_type(value,'$.port'),json_extract(value,'$.port'),json_type(value,'$.bit'),json_extract(value,'$.bit'),json_type(value,'$.value'),json_extract(value,'$.value') FROM json_each(?1)")){}
  void Constants(const std::string& json,Pins& pins) {
    auto kind=db_.Prepare("SELECT json_type(?)");kind.BindText(1,json);Check(kind.Step() && kind.Text(0)=="array","constant collection is invalid");
    constants_.Reset();constants_.BindText(1,json);
    while(constants_.Step()) {
      Check(constants_.Text(1)=="text" && constants_.Text(3)=="integer" && constants_.Text(5)=="text","constant pin is invalid");
      auto name=Text(constants_,2),value=Text(constants_,6);const auto bit=constants_.TypedInteger(4);
      Check(bit>=0 && (value=="0" || value=="1" || value=="x" || value=="z"),"constant bit/value is invalid");
      pins[{name,bit}]="1'b"+value;
    }
  }
  Pins For(I owner) {
    Pins pins;
    auto c=db_.Prepare("SELECT p.value FROM cell_records c LEFT JOIN payloads p ON p.seq=c.constants_seq WHERE c.seq=?");c.BindInteger(1,owner);Check(c.Step(),"pin owner is missing");Constants(c.TypedText(0),pins);
    auto e=db_.Prepare("SELECT p.name,e.bit FROM endpoint_records e LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=?");e.BindInteger(1,owner);
    while(e.Step()){const auto bit=e.TypedInteger(1);Check(bit>=0,"pin bit is negative");pins[{Text(e,0),bit}]="";}
    return pins;
  }
 private:SqliteDatabase& db_;SqliteStatement constants_;
};
// These are sparse escaped-name collision candidates, not an all-pin table.
void ValidateAliases(SqliteDatabase& db,const std::vector<Port>& ports,PinsReader& reader) {
  auto pairs=db.Prepare("SELECT a.seq,a.id,b.seq,b.id FROM cell_records a JOIN cell_records b ON b.id=replace(a.id,char(92),char(92)||char(92)) WHERE instr(a.id,char(92))>0 AND a.id<>b.id");
  while(pairs.Step()) {
    auto a=reader.For(pairs.TypedInteger(0)),b=reader.For(pairs.TypedInteger(2));std::set<std::string> names;
    for(const auto& pin:a)names.insert(Escaped(pairs.TypedText(1)+"/"+PinName(a,pin.first)));
    for(const auto& pin:b)Check(!names.count(pairs.TypedText(3)+"/"+PinName(b,pin.first)),"provider object alias is ambiguous");
  }
  auto owner=db.Prepare("SELECT seq FROM cell_records WHERE id=?");
  auto canonical=[&](const std::string& name) {
    for(const auto& port:ports)for(I bit=0;bit<port.width;++bit)
      if(name==(port.width==1?port.name:port.name+"["+std::to_string(bit)+"]") || (port.width>1 && name==port.name+"__"+std::to_string(bit)))return "P"+std::to_string(port.seq)+":"+std::to_string(bit);
    auto slash=name.rfind('/');if(slash==std::string::npos)return std::string();
    owner.Reset();owner.BindText(1,name.substr(0,slash));if(!owner.Step())return std::string();const auto seq=owner.TypedInteger(0);auto pins=reader.For(seq);
    for(const auto& pin:pins)if(name.substr(slash+1)==PinName(pins,pin.first))return "C"+std::to_string(seq)+":"+pin.first.first+":"+std::to_string(pin.first.second);
    return std::string();
  };
  for(const auto& port:ports)if(port.name.find('\\')!=std::string::npos)
    for(I bit=0;bit<port.width;++bit) {
      std::vector<std::string> names{port.width==1?port.name:port.name+"["+std::to_string(bit)+"]"};if(port.width>1)names.push_back(port.name+"__"+std::to_string(bit));
      for(const auto& name:names) {
        const auto alias=Escaped(name);if(alias==name)continue;
        const auto a=canonical(name),b=canonical(alias);Check(a.empty() || b.empty() || a==b,"provider object alias is ambiguous");
      }
    }
}
}
void ExportOpenstaInputs(const std::vector<ArtifactRef>& inputs,const std::string& summary,
                        const std::string& verilog,const std::string& net_map) {
  Check(inputs.size()==2,"input coverage is invalid");
  CheckedRequestReader request(inputs[1].path(),4096);
  Check(request.Read<std::uint64_t>()==0x31455853464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  Check(ReadFileGeneration(request)==inputs[0].generation(),"source generation changed");
  const auto mode=request.Read<std::uint32_t>(),mask=request.Read<std::uint32_t>();request.RequireEnd();
  Check(mode<=1 && mask>0 && mask<=3 && bool(mask&1)==!verilog.empty() && bool(mask&2)==!net_map.empty(),"output/mode contract is invalid");
  EmuirView source(inputs[0]);auto& db=source.store().database();db.Execute("PRAGMA cache_size=-32768");
  const auto counts=source.counts();
  auto meta=db.Prepare("SELECT json_extract(value,'$.top'),json_type(value,'$.top') FROM metadata WHERE key='design_json'");
  Check(meta.Step() && meta.Text(1)=="text","design identity is missing");const auto top=Text(meta,0);
  AtomicBinaryWriter report(summary,inputs);
  std::unique_ptr<AtomicBinaryWriter> rtl,map;
  if(mask&1)rtl=std::make_unique<AtomicBinaryWriter>(verilog,inputs);
  if(mask&2){map=std::make_unique<AtomicBinaryWriter>(net_map,inputs);map->stream()<<"mapped_net_hex\temuir_net_hex\n";}
  std::vector<Port> ports;PinsReader reader(db);
  if(rtl) {
    auto rows=db.Prepare("SELECT seq,json_extract(record_json,'$.id'),json_extract(record_json,'$.direction'),json_extract(record_json,'$.width'),coalesce(json_extract(record_json,'$.constant_connections'),'[]'),json_type(record_json,'$.width') FROM port_records ORDER BY seq");
    std::set<std::string> seen;
    while(rows.Step()) {
      const auto width=rows.TypedInteger(3);auto name=Text(rows,1),direction=Text(rows,2);
      Check(rows.TypedInteger(0)==I(ports.size()) && seen.insert(name).second && rows.Text(5)=="integer" && width>0 && (direction=="input" || direction=="output" || direction=="inout"),"top port is invalid");
      Port p{I(ports.size()),width,name,direction,{}};
      auto constants=db.Prepare("SELECT json_type(?1),json_type(value,'$.bit'),json_extract(value,'$.bit'),json_type(value,'$.value'),json_extract(value,'$.value') FROM json_each(?1)");constants.BindText(1,rows.TypedText(4));
      while(constants.Step()) {
        Check(constants.Text(0)=="array" && constants.Text(1)=="integer" && constants.Text(3)=="text","top constant is invalid");const auto bit=constants.TypedInteger(2);const auto value=Text(constants,4);
        Check(bit>=0 && bit<width && (value=="0" || value=="1" || value=="x" || value=="z"),"top constant is invalid");p.constants.emplace_back(bit,"1'b"+value);
      }
      ports.push_back(std::move(p));
    }
    ValidateAliases(db,ports,reader);
    auto& out=rtl->stream();out<<"module "<<Identifier(top)<<"(\n  ";
    for(std::size_t i=0;i<ports.size();++i){if(i)out<<",\n  ";out<<Identifier(ports[i].name);}out<<"\n);\n";
    for(const auto& p:ports){out<<"  "<<p.direction<<' ';if(p.width!=1)out<<'['<<p.width-1<<":0] ";out<<Identifier(p.name)<<";\n";}out<<'\n';
  }
  auto nets=db.Prepare("SELECT seq,id FROM net_records ORDER BY seq");I observed_nets=0;
  while(nets.Step()) {
    Check(nets.TypedInteger(0)==observed_nets && std::uint64_t(observed_nets)<counts.nets,"net ordinals disagree");const auto name=Text(nets,1),wire=Wire(observed_nets++);
    if(rtl)rtl->stream()<<"  wire "<<wire<<";\n";
    if(map)map->stream()<<Hex(wire)<<'\t'<<Hex(name)<<'\n';
  }
  Check(std::uint64_t(observed_nets)==counts.nets,"net count disagrees");
  I observed_cells=0,observed_endpoints=0,max_pins=0;
  if(rtl) {
    auto& out=rtl->stream();out<<'\n';
    auto top_rows=db.Prepare("SELECT e.net_seq,e.role,p.name,e.bit FROM endpoint_records e LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq IS NULL ORDER BY e.net_seq,e.role,e.ordinal");
    std::map<std::string,const Port*> by_name;for(const auto& p:ports)by_name.emplace(p.name,&p);
    while(top_rows.Step()) {
      const auto net=top_rows.TypedInteger(0),role=top_rows.TypedInteger(1),bit=top_rows.TypedInteger(3);auto name=Text(top_rows,2);auto found=by_name.find(name);
      Check(net>=0 && std::uint64_t(net)<counts.nets && (role==0 || role==1) && found!=by_name.end() && bit>=0 && bit<found->second->width,"top endpoint is invalid");
      const auto port=Identifier(name)+(found->second->width==1?"":"["+std::to_string(bit)+"]"),wire=Wire(net);
      out<<"  assign "<<(role==0?wire:port)<<" = "<<(role==0?port:wire)<<";\n";++observed_endpoints;
    }
    for(const auto& p:ports)for(const auto& constant:p.constants)out<<"  assign "<<Identifier(p.name)<<(p.width==1?"":"["+std::to_string(constant.first)+"]")<<" = "<<constant.second<<";\n";
    out<<'\n';
    auto cells=db.Prepare("SELECT c.seq,c.id,c.type,k.value,CASE WHEN c.type IN ('FDCE','FDPE','FDRE','FDSE') THEN p.value END FROM cell_records c LEFT JOIN payloads k ON k.seq=c.constants_seq LEFT JOIN payloads p ON p.seq=c.parameters_seq ORDER BY c.seq");
    auto endpoints=db.Prepare("SELECT e.instance_seq,e.net_seq,p.name,e.bit,e.role FROM endpoint_records e INDEXED BY endpoint_owner LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq IS NOT NULL ORDER BY e.instance_seq,e.port_seq,e.bit");bool next=endpoints.Step();
    auto inverted=db.Prepare("SELECT json_type(?1,'$.IS_C_INVERTED'),json_extract(?1,'$.IS_C_INVERTED')");
    while(cells.Step()) {
      const auto seq=cells.TypedInteger(0);Check(seq==observed_cells && std::uint64_t(seq)<counts.cells,"cell ordinals disagree");auto owner=Text(cells,1),type=Text(cells,2);Pins pins;reader.Constants(cells.TypedText(3),pins);
      Check(!next || endpoints.TypedInteger(0)>=seq,"orphaned endpoint owner");
      std::map<Pin,I> wires;
      while(next && endpoints.TypedInteger(0)==seq) {
        const auto net=endpoints.TypedInteger(1),bit=endpoints.TypedInteger(3),role=endpoints.TypedInteger(4);const auto port=Text(endpoints,2);
        Check(net>=0 && std::uint64_t(net)<counts.nets && bit>=0 && (role==0 || role==1),"cell endpoint is invalid");const Pin pin{port,bit};auto found=wires.find(pin);if(found==wires.end() || found->second<net)wires[pin]=net;
        ++observed_endpoints;next=endpoints.Step();
      }
      for(const auto& wire:wires)pins[wire.first]=Wire(wire.second);
      max_pins=std::max(max_pins,I(pins.size()));
      std::set<std::string> names;
      for(const auto& pin:pins)names.insert(owner+"/"+PinName(pins,pin.first));
      for(const auto& name:names){const auto alias=Escaped(name);Check(alias==name || !names.count(alias),"provider object alias is ambiguous");}
      if(mode && !cells.IsNull(4)) {
        inverted.Reset();inverted.BindText(1,cells.TypedText(4));Check(inverted.Step(),"clock parameter is invalid");const auto kind=inverted.Text(0);
        if(TimingParameterIsOne(kind,kind=="integer"?inverted.TypedInteger(1):0,kind=="text"?inverted.TypedText(1):""))type+="__NEG";
      }
      out<<"  "<<Identifier(type)<<' '<<Identifier(owner)<<"(\n    ";bool first=true;
      for(const auto& pin:pins){if(!first)out<<",\n    ";first=false;out<<'.'<<Identifier(PinName(pins,pin.first))<<'('<<pin.second<<')';}out<<"\n  );\n";++observed_cells;
    }
    Check(!next,"orphaned endpoint owner");Check(std::uint64_t(observed_cells)==counts.cells && std::uint64_t(observed_endpoints)==counts.endpoints,"cell/endpoint count disagrees");out<<"endmodule\n";
  }
  report.stream()<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.opensta-input-export/v1"},{"status","pass"},{"implementation_id","native-opensta-export-v1"},{"nets",observed_nets},{"cells",observed_cells},{"endpoints",observed_endpoints},{"maximum_instance_pins",max_pins},{"mode",I(mode)},{"outputs",I(mask)}}).Encode()<<'\n';
  for(const auto& input:inputs)input.RequireUnchanged();
  if(rtl)rtl->Commit();if(map)map->Commit();report.Commit();
}
}
