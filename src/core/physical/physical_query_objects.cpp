#include "physical/physical_query_objects.hpp"
#include "common/bounded_json.hpp"
#include "ir/emuir_view.hpp"
#include "common/json_record.hpp"
#include "ir/resource_view.hpp"
#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <utility>

namespace emuflow::core {
namespace {
using I=std::int64_t;
constexpr std::size_t kRecordBytes=8*1024*1024;
constexpr std::size_t kCellPins=65536;
void Check(bool good,const std::string& why) {
  if(!good)throw StageError(ErrorCode::kInvalidArtifact,"physical query object: "+why);
}
void Identity(const std::string& value) {
  Check(!value.empty() && value.size()<=kRecordBytes && value.find_first_of(std::string("\0\t\r\n",4))==std::string::npos,
        "empty or control-delimited identity");
  (void)JsonQuoteUtf8(value);
}
const ArtifactRef& Compact(const ArtifactRef& source) {
  Check(source.schema()=="emuir-compact-v2" && !source.is_pipe(),"requires compact merged EmuIR owner");
  source.RequireUnchanged();return source;
}
bool Prefix(const std::string& value,const std::string& prefix) {
  return value.compare(0,prefix.size(),prefix)==0;
}
bool NativeFf(const std::string& type) {
  return type=="FDCE" || type=="FDPE" || type=="FDRE" || type=="FDSE";
}
bool LutInput(const std::string& port) {
  return port.size()>1 && port[0]=='I' && std::all_of(port.begin()+1,port.end(),
      [](unsigned char value){return value>='0' && value<='9';});
}
bool FfControl(const std::string& type,const std::string& port) {
  return (NativeFf(type) && port=="CE") ||
      (type=="FDRE" && port=="R") || (type=="FDSE" && port=="S") ||
      (type=="FDCE" && port=="CLR") || (type=="FDPE" && port=="PRE");
}
// Match the original generic-LUT binary parameter interpretation without
// narrowing a truth table to uint64. Only WIDTH needs a bounded integer value.
I BinaryParameter(std::string text,bool width) {
  const auto first=text.find_first_not_of(" \t\r\n\v\f");
  Check(first!=std::string::npos,"generic LUT has an empty binary parameter");
  text=text.substr(first,text.find_last_not_of(" \t\r\n\v\f")-first+1);
  bool negative=false;
  if(text[0]=='+' || text[0]=='-'){negative=text[0]=='-';text.erase(0,1);}
  if(text.size()>=2 && text[0]=='0' && (text[1]=='b' || text[1]=='B')) {
    text.erase(0,2);if(!text.empty() && text[0]=='_')text.erase(0,1);
  }
  Check(!text.empty(),"generic LUT has an empty binary parameter");
  I value=0;bool digit=false,underscore=false;
  for(const char c:text) {
    if(c=='_'){Check(digit && !underscore,"invalid generic LUT binary separator");underscore=true;continue;}
    Check(c=='0' || c=='1',"generic LUT parameter is not binary");
    digit=true;underscore=false;
    if(width){Check(value<=I(kCellPins)/2,"generic LUT width exceeds per-cell bound");value=value*2+(c-'0');Check(value<=I(kCellPins),"generic LUT width exceeds per-cell bound");}
  }
  Check(digit && !underscore && (!width || !negative),"invalid generic LUT width or binary parameter");
  return value;
}
}  // namespace

std::string VivadoMappedName(const std::string& value) {
  Identity(value);std::string result;
  for(const char c:value){result+=c;if(c=='\\')result+='\\';}
  return result;
}

struct PhysicalQueryObjects::Impl {
  struct Cell {
    I sequence=-1;
    std::string id,type,parameters;
    bool state=false;
    std::size_t pin_bytes=0;
    std::set<std::pair<std::string,I>> pins;
    std::map<std::string,I> widths;
  };
  ArtifactRef source;
  EmuirView owner;
  SqliteDatabase& db;
  std::string provider;
  ResourceView resources;
  std::optional<ArtifactRef> library;
  std::set<std::string> native_types;
  std::string cached_id;
  std::optional<Cell> cached;
  SqliteStatement cell_rows,pin_rows,constant_rows,port_rows,parameter_rows;

  Impl(const ArtifactRef& merged,const std::string& selected,const ArtifactRef* primitive)
      :source(Compact(merged)),owner(source),db(owner.store().database()),provider(selected),resources(db),
       cell_rows(db.Prepare("SELECT c.seq,c.type,c.resources_seq,p.value,k.value FROM cell_records c "
           "LEFT JOIN payloads p ON p.seq=c.parameters_seq "
           "JOIN payloads k ON k.seq=c.constants_seq WHERE c.id=?")),
       pin_rows(db.Prepare("SELECT p.name,e.bit FROM endpoint_records e INDEXED BY endpoint_owner "
           "LEFT JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=?")),
       constant_rows(db.Prepare("SELECT type,json_type(value,'$.port'),json_extract(value,'$.port'),"
           "json_type(value,'$.bit'),json_extract(value,'$.bit') FROM json_each(?)")),
       port_rows(db.Prepare("SELECT json_type(record_json,'$.width'),json_extract(record_json,'$.width') "
           "FROM port_records WHERE json_extract(record_json,'$.id')=?")),
       parameter_rows(db.Prepare("SELECT type,value FROM json_each(?1) WHERE key=?2")) {
    Check(merged.schema()=="emuir-compact-v2","requires compact merged EmuIR owner");
    Check(provider=="xilinx" || provider=="vivado","unsupported object provider");
    Check(provider!="vivado" || primitive,"Vivado requires a source-bound primitive library");
    if(primitive) {
      Check(primitive->schema()=="xilinx-primitive-library-v1","primitive library source schema disagrees");
      library=*primitive;BoundedJson document(BoundedJson::Read(*primitive));
      Check(document.Text("schema")=="emuflow.xilinx-primitive-library-db/v1" &&
          document.Text("mapping_profile")=="xilinx-ultrascaleplus-open-v1" &&
          document.Text("family")=="UltraScale+" && document.Text("unknown_cell_policy")=="fail",
          "primitive library identity disagrees");
      BoundedJson cells(document.Token("cells"));const auto records=cells.Fields();
      Check(!records.empty() && records.size()<=4096,"primitive catalog exceeds bound");
      for(const auto& record:records) {
        Identity(record.first);BoundedJson contract(record.second);
        Identity(contract.Text("category"));
        (void)contract.Boolean("hard_block");(void)contract.Boolean("placement_required");
        BoundedJson amounts(contract.Token("resources"));
        for(const auto& amount:amounts.Fields()) {
          Check(std::find_if(kResourceFields.begin(),kResourceFields.end(),[&](const char* key){return amount.first==key;})!=kResourceFields.end(),"unknown primitive resource field");
          Check(amounts.Integer(amount.first)>=0,"negative primitive resource amount");
        }
        native_types.insert(record.first);
      }
    }
    RequireUnchanged();
  }
  void RequireUnchanged() const {source.RequireUnchanged();if(library)library->RequireUnchanged();}
  void JsonType(const std::string& value,const char* type) {
    Check(value.size()<=kRecordBytes,"per-cell payload exceeds bound");
    auto row=db.Prepare("SELECT json_valid(?1),json_type(?1)");row.BindText(1,value);
    Check(row.Step() && row.TypedInteger(0) && row.TypedText(1)==type,"invalid per-cell payload type");
  }
  const Cell* Find(const std::string& identity) {
    Identity(identity);
    if(identity==cached_id)return cached?&*cached:nullptr;
    cached.reset();cached_id.clear();
    cell_rows.Reset();cell_rows.BindText(1,identity);
    if(!cell_rows.Step()){cached_id=identity;return nullptr;}
    Cell value;value.id=identity;value.sequence=cell_rows.TypedInteger(0);value.type=cell_rows.TypedText(1);
    // Compact lowering may retain sparse or one-based SQLite keys. A row
    // population is not the upper bound of an artifact-owned primary key.
    Check(value.sequence>=0,"cell ordinal is negative");Identity(value.type);
    const auto resource_sequence=cell_rows.TypedInteger(2);const auto constants=cell_rows.TypedText(4);
    value.parameters=cell_rows.IsNull(3)?"{}":cell_rows.TypedText(3);
    Check(!cell_rows.Step(),"duplicate cell identity");
    JsonType(value.parameters,"object");JsonType(constants,"array");
    const auto& resource=resources.Get(resource_sequence);
    for(unsigned index=0;index<kResourceFields.size();++index) {
      const std::string field=kResourceFields[index];
      if(field=="ff" || field=="bram" || field=="bram18k" || field=="uram288")value.state|=bool(resource.present&(UINT64_C(1)<<index));
    }
    const auto add=[&](std::string port,I bit){Identity(port);Check(bit>=0 && bit<std::numeric_limits<I>::max(),"invalid cell pin bit");
      if(value.pins.emplace(port,bit).second) {
        Check(port.size()<=kRecordBytes-value.pin_bytes,"per-cell pin names exceed bound");value.pin_bytes+=port.size();
      }
      Check(value.pins.size()<=kCellPins,"per-cell pin population exceeds bound");
      auto& width=value.widths[port];width=std::max(width,bit+1);};
    pin_rows.Reset();pin_rows.BindInteger(1,value.sequence);
    while(pin_rows.Step())add(pin_rows.TypedText(0),pin_rows.TypedInteger(1));
    constant_rows.Reset();constant_rows.BindText(1,constants);
    while(constant_rows.Step()) {
      Check(constant_rows.TypedText(0)=="object" && constant_rows.TypedText(1)=="text" &&
          constant_rows.TypedText(3)=="integer","invalid constant pin identity");
      add(constant_rows.TypedText(2),constant_rows.TypedInteger(4));
    }
    cached=std::move(value);cached_id=identity;return &*cached;
  }
  const Cell& Require(const std::string& identity) {
    const auto* cell=Find(identity);Check(cell,"physical endpoint cell is absent: "+identity);return *cell;
  }
  I Width(const Cell& cell,const std::string& port) const {
    const auto found=cell.widths.find(port);return found==cell.widths.end()?0:found->second;
  }
  std::optional<std::string> Parameter(const Cell& cell,const std::string& key) {
    parameter_rows.Reset();parameter_rows.BindText(1,cell.parameters);parameter_rows.BindText(2,key);
    if(!parameter_rows.Step())return std::nullopt;
    const auto kind=parameter_rows.TypedText(0);
    Check(kind=="text" || kind=="integer","generic LUT binary parameter has an unsupported scalar type");
    const auto value=kind=="text"?parameter_rows.TypedText(1):JsonMemberToken(db,cell.parameters,key);
    Check(!parameter_rows.Step(),"duplicate cell parameter "+key);return value;
  }
  std::string RequiredParameter(const Cell& cell,const std::string& key) {
    const auto value=Parameter(cell,key);Check(value && !value->empty(),"generic LUT lacks "+key+" parameter");return *value;
  }
  PhysicalEndpoint Normalize(const PhysicalEndpoint& endpoint) {
    Identity(endpoint.port);Check(endpoint.bit>=0,"negative endpoint bit");
    if(!endpoint.instance)return endpoint;
    const auto* cell=Find(*endpoint.instance);
    if(!cell || cell->type!="RAMB18E2" || endpoint.port!="WEA" || (endpoint.bit!=2 && endpoint.bit!=3))return endpoint;
    if(cell->pins.count({"WEA",endpoint.bit}) || !cell->pins.count({"WEA",endpoint.bit%2}))return endpoint;
    auto value=endpoint;value.bit%=2;return value;
  }
  PhysicalObject Resolve(const PhysicalEndpoint& original) {
    const auto endpoint=Normalize(original);Identity(endpoint.port);
    if(!endpoint.instance) {
      port_rows.Reset();port_rows.BindText(1,endpoint.port);
      Check(port_rows.Step() && port_rows.TypedText(0)=="integer","physical top endpoint is absent");
      const auto width=port_rows.TypedInteger(1);Check(width>0 && endpoint.bit<width && !port_rows.Step(),"physical top endpoint bit is absent or ambiguous");
      const auto name=endpoint.port+(width==1?"":"["+std::to_string(endpoint.bit)+"]");
      return {"port",provider=="vivado"?name:"top:"+name};
    }
    const auto& cell=Require(*endpoint.instance);
    Check(cell.pins.count({endpoint.port,endpoint.bit}),"physical endpoint pin is absent: "+cell.id+"."+endpoint.port);
    const auto& type=cell.type;std::string port=endpoint.port;I bit=endpoint.bit,width=Width(cell,port);
    if(provider=="vivado") {
      const auto prefix=VivadoMappedName(cell.id)+"/";
      if(endpoint.object && Prefix(*endpoint.object,prefix) && endpoint.object->find("/memory_reg_bram_")!=std::string::npos) {
        Identity(*endpoint.object);return {"pin",*endpoint.object};
      }
      if(Prefix(type,"LUT")) {
        if(port=="O" || (type=="LUT6_2" && (port=="O5" || port=="O6")))bit=0;
        else Check(LutInput(port) && bit==0,"unsupported Vivado LUT pin");
      } else if(type=="$lut" || type=="$_LUT_") {
        if(port=="Y" || port=="O"){port="O";bit=0;}
        else if(port=="A" || port=="I"){port="I"+std::to_string(bit);bit=0;}
        else Check(false,"unsupported Vivado generic LUT pin");
      } else if(NativeFf(type) || Prefix(type,"$_DFF_")) {
        Check((port=="C" || port=="D" || port=="Q" || FfControl(type,port)) && bit==0,"unsupported Vivado FF pin");
      } else Check(native_types.count(type),"unsupported Vivado physical logic cell "+type);
      const bool lut=Prefix(type,"LUT") || type=="$lut" || type=="$_LUT_";
      const auto suffix=lut || (bit==0 && width<=1)?port:port+"["+std::to_string(bit)+"]";
      return {"pin",prefix+suffix};
    }
    if(type=="$lut" || type=="$_LUT_") {
      const auto inputs=BinaryParameter(RequiredParameter(cell,"WIDTH"),true);
      auto truth=Parameter(cell,"LUT");
      if(!truth && type=="$_LUT_")truth=Parameter(cell,"INIT");
      Check(truth && !truth->empty(),"generic LUT lacks LUT/INIT parameter");
      (void)BinaryParameter(*truth,false);
      if(port=="A"){port="I"+std::to_string(bit);bit=0;width=endpoint.bit<inputs?1:0;}
      else if(port=="Y"){port="O";bit=0;width=1;}
      else Check(false,"Xilinx generic LUT endpoint is invalid");
    }
    return {"pin","cell:"+cell.id+"/"+port+(width==1?"":"["+std::to_string(bit)+"]")};
  }
};

PhysicalQueryObjects::PhysicalQueryObjects(const ArtifactRef& merged,const std::string& provider,
                                           const ArtifactRef* primitive_library)
    :impl_(std::make_unique<Impl>(merged,provider,primitive_library)) {}
PhysicalQueryObjects::~PhysicalQueryObjects()=default;
PhysicalEndpoint PhysicalQueryObjects::Normalize(const PhysicalEndpoint& endpoint){return impl_->Normalize(endpoint);}
PhysicalObject PhysicalQueryObjects::Resolve(const PhysicalEndpoint& endpoint){return impl_->Resolve(endpoint);}
bool PhysicalQueryObjects::IsArchitecturalState(const std::string& instance){return impl_->Require(instance).state;}
std::string PhysicalQueryObjects::CellType(const std::string& instance){return impl_->Require(instance).type;}
void PhysicalQueryObjects::RequireUnchanged() const {impl_->RequireUnchanged();}
}  // namespace emuflow::core
