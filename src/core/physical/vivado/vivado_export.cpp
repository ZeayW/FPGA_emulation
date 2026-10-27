#include "physical/vivado/vivado_export.hpp"
#include "storage/store.hpp"
#include "common/json_record.hpp"
#include "common/hash.hpp"
#include <algorithm>
#include <cctype>
#include <iomanip>
#include <limits>
#include <map>
#include <sstream>

namespace emuflow::core {
namespace {
using Integer=std::int64_t;
void Check(bool valid,const std::string& message) {
  if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"Vivado export "+message);
}
std::string Hex(Sha256& hash) {
  std::ostringstream out;out<<std::hex<<std::setfill('0');
  for(auto byte:hash.finish())out<<std::setw(2)<<unsigned(byte);
  return out.str();
}
std::string Digest(const std::string& text) {
  Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());return Hex(hash);
}
std::string Identifier(const std::string& name) {
  Check(!name.empty(),"has an empty identifier");(void)JsonQuote(name);return "\\"+name+" ";
}
std::string Metadata(SqliteDatabase& db,const char* name) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,name);
  Check(row.Step(),"metadata is missing");auto value=row.TypedText(0);Check(!row.Step() && value.size()<=8*1024*1024,"metadata is invalid");return value;
}
std::string Bit(Integer number,const std::string& constant) {
  if(constant.empty()){Check(number>=2,"net bit is invalid");return "__emuflow_net_"+std::to_string(number-2);}
  Check(constant.size()==1 && std::string("01xXzZ").find(constant[0])!=std::string::npos,"constant bit is invalid");
  return std::string("1'b")+char(std::tolower(static_cast<unsigned char>(constant[0])));
}
struct Parameter {std::string canonical,rtl;bool omit=false;};
std::map<std::string,Parameter> Parameters(SqliteDatabase& db,SqliteStatement& rows,const std::string& value) {
  std::map<std::string,Parameter> result;
  rows.Reset();rows.BindText(1,value);
  while(rows.Step()) {
    const auto name=rows.TypedText(0),kind=rows.TypedText(1);Parameter p;
    if(kind=="text") {
      const auto text=rows.TypedText(2);p.canonical=JsonQuote(text);
      const bool binary=!text.empty() && text.find_first_not_of("01xXzZ")==std::string::npos;
      p.rtl=binary?std::to_string(text.size())+"'b"+text:JsonQuote(text);
      p.omit=name=="INIT" && binary && text.find_first_of("xXzZ")!=std::string::npos;
    } else if(kind=="integer") {
      p.canonical=rows.Text(3)=="integer"?std::to_string(rows.TypedInteger(2)):JsonIntegerMemberToken(db,value,name);p.rtl=p.canonical;
    } else if(kind=="real") {p.canonical=JsonRecord(rows.TypedReal(2)).Encode();p.rtl=p.canonical;}
    else if(kind=="true" || kind=="false") {p.canonical=kind;p.rtl=kind=="true"?"1'b1":"1'b0";}
    else if(kind=="null") {p.canonical="null";p.rtl="\"None\"";}
    else Check(false,"primitive parameter is not scalar");
    Check(result.emplace(name,std::move(p)).second,"parameter name repeats");
  }
  return result;
}
struct Port {Integer direction=0,width=0;std::vector<std::string> rtl,canonical;};
}

void ExportVivado(const std::vector<ArtifactRef>& inputs,const std::string& rtl,
                  const std::string& functions,const std::string& summary) {
  Check(inputs.size()==2,"source coverage disagrees");
  CheckedRequestReader request(inputs[1].path(),4096);
  Check(request.Read<std::uint64_t>()==0x31585656464d5545ULL && request.Read<std::uint32_t>()==1,"request version is invalid");
  if(!(ReadFileGeneration(request)==inputs[0].generation()))throw StageError(ErrorCode::kMixedGeneration,"Vivado mapped generation changed");
  const auto mapped_sha=request.ReadString32(false,64);const auto expected=request.Read<std::uint64_t>();request.RequireEnd();
  Check(mapped_sha.size()==64 && mapped_sha.find_first_not_of("0123456789abcdef")==std::string::npos,"mapped seal is invalid");
  SqliteStore source(inputs[0]);auto& db=source.database();
  auto top_row=db.Prepare("SELECT json_extract(?1,'$'),json_type(?1)");top_row.BindText(1,Metadata(db,"top_json"));
  Check(top_row.Step() && top_row.Text(1)=="text","top identity is invalid");const auto top=top_row.TypedText(0);
  const auto ports=Metadata(db,"ports_json");
  auto port_kind=db.Prepare("SELECT json_type(?)");port_kind.BindText(1,ports);
  Check(port_kind.Step() && port_kind.Text(0)=="object","top ports are not an object");
  AtomicBinaryWriter writer(rtl,inputs),summary_writer(summary,inputs);Sha256 rtl_hash;
  const auto put=[&](const std::string& text){writer.WriteBytes(text.data(),text.size());rtl_hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());};
  const auto line=[&](const std::string& text){put(text);put("\n");};
  SqliteArtifactWriter signatures(functions,"vivado-source-functions-v1",inputs);auto& output=signatures.database();
  output.Execute("CREATE TABLE signatures(seq INTEGER PRIMARY KEY,signature TEXT NOT NULL); CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)");
  SqliteTransaction transaction(output);
  auto meta=output.Prepare("INSERT INTO metadata VALUES(?,?)");
  for(const auto& field:std::map<std::string,std::string>{{"schema","emuflow.vivado-source-functions/v1"},{"mapped_index_sha256",mapped_sha}}){meta.Reset();meta.BindText(1,field.first);meta.BindText(2,field.second);meta.Execute();}
  line("`default_nettype none");line("module "+Identifier(top)+"(");
  auto names=db.Prepare("SELECT key FROM json_each(?) ORDER BY key");names.BindText(1,ports);bool first=true;
  std::string previous_name;
  while(names.Step()){const auto name=names.TypedText(0);Check(first || name>previous_name,"top port repeats");previous_name=name;if(!first)put(",\n");first=false;put("  "+Identifier(name));}line("");line(");");
  auto top_ports=db.Prepare("SELECT key,json_extract(value,'$.direction'),json_array_length(value,'$.bits'),json_type(value,'$.bits') FROM json_each(?) ORDER BY key");top_ports.BindText(1,ports);
  while(top_ports.Step()) {
    const auto name=top_ports.TypedText(0),direction=top_ports.TypedText(1);const auto width=top_ports.TypedInteger(2);
    Check(width>0 && top_ports.Text(3)=="array" && (direction=="input" || direction=="output" || direction=="inout"),"top port is invalid");
    line("  "+direction+" wire "+(width==1?"":"["+std::to_string(width-1)+":0] ")+Identifier(name)+";");
  }
  Integer largest=1;
  auto nets=db.Prepare("SELECT seq FROM netnames ORDER BY seq");
  while(nets.Step()){const auto seq=nets.TypedInteger(0);Check(seq>=0 && seq<=std::numeric_limits<Integer>::max()-2,"net ordinal is invalid");largest=seq+2;line("  wire __emuflow_net_"+std::to_string(seq)+";");}
  auto top_bits=db.Prepare("SELECT p.key,json_extract(p.value,'$.direction'),json_array_length(p.value,'$.bits'),b.key,b.type,b.value FROM json_each(?1) p,json_each(p.value,'$.bits') b ORDER BY p.key,CAST(b.key AS INTEGER)");top_bits.BindText(1,ports);
  while(top_bits.Step()) {
    const auto name=top_bits.TypedText(0),direction=top_bits.TypedText(1),kind=top_bits.TypedText(4);
    const auto width=top_bits.TypedInteger(2),ordinal=top_bits.TypedInteger(3);
    Check(kind=="integer" || kind=="text","top bit is invalid");
    const auto number=kind=="integer"?top_bits.TypedInteger(5):0;const auto constant=kind=="text"?top_bits.TypedText(5):"";
    auto value=Bit(number,constant);if(kind=="integer" && number>largest)line("  wire "+value+";");
    auto target=Identifier(name)+(width==1?"":"["+std::to_string(ordinal)+"]");
    if(direction=="input"){if(kind=="integer")line("  assign "+value+" = "+target+";");}
    else {Check(direction=="output","top-level inout is unsupported");line("  assign "+target+" = "+value+";");}
  }
  auto append=output.Prepare("INSERT INTO signatures VALUES(?,?)");
  auto cells=db.Prepare("SELECT seq,id,type,record_json FROM cells ORDER BY seq");
  auto properties=db.Prepare("SELECT json_type(?1),json_extract(?1,'$.type'),json_type(?1,'$.parameters'),json_extract(?1,'$.parameters')");
  auto parameter_rows=db.Prepare("SELECT key,type,value,typeof(value) FROM json_each(?) ORDER BY key");
  auto pins=db.Prepare("SELECT cell_seq,cell_id,port,port_bit,port_width,direction,value_kind,net_bit,constant FROM cell_pins ORDER BY cell_seq,port,port_bit");
  bool pin=pins.Step();std::uint64_t count=0;Integer previous=-1;
  while(cells.Step()) {
    const auto seq=cells.TypedInteger(0);const auto name=cells.TypedText(1),type=cells.TypedText(2),record=cells.TypedText(3);
    Check(seq>previous && record.size()<=8*1024*1024,"cell ordinal/record is invalid");previous=seq;
    properties.Reset();properties.BindText(1,record);
    Check(properties.Step() && properties.Text(0)=="object" && properties.Text(1)==type,"cell type disagrees");
    Check(properties.IsNull(2) || properties.Text(2)=="object","parameters are invalid");
    const auto parameters=Parameters(db,parameter_rows,properties.IsNull(2)?"{}":properties.TypedText(3));
    std::map<std::string,Port> ports_by_name;
    while(pin && pins.TypedInteger(0)<=seq) {
      Check(pins.TypedInteger(0)==seq && pins.TypedText(1)==name,"pin owner disagrees");
      const auto port_name=pins.TypedText(2);auto& port=ports_by_name[port_name];const auto bit=pins.TypedInteger(3),width=pins.TypedInteger(4),direction=pins.TypedInteger(5),kind=pins.TypedInteger(6);
      Check(width>0 && direction>=0 && direction<=2 && (kind==0 || kind==1),"pin declaration is invalid");
      if(port.rtl.empty()){port.width=width;port.direction=direction;}
      Check(port.width==width && port.direction==direction && bit==Integer(port.rtl.size()) && bit<width,"pin order/coverage disagrees");
      const auto number=kind==0?pins.TypedInteger(7):0;const auto constant=kind==1?pins.TypedText(8):"";
      Check(kind==0?pins.IsNull(8):pins.IsNull(7),"pin value kind disagrees");
      port.rtl.push_back(Bit(number,constant));port.canonical.push_back(kind==0?std::to_string(number):JsonQuote(constant));pin=pins.Step();
    }
    std::string signature="{\"parameters\":{";first=true;
    for(const auto& item:parameters){if(!first)signature+=',';first=false;signature+=JsonQuote(item.first)+":"+item.second.canonical;}
    signature+="},\"pins\":{";first=true;
    static const char* directions[]={"input","output","inout"};
    for(const auto& item:ports_by_name) {
      const auto& p=item.second;Check(Integer(p.rtl.size())==p.width,"port is incomplete");if(!first)signature+=',';first=false;
      signature+=JsonQuote(item.first)+":{\"bits\":";
      if(p.direction==1)signature+=std::to_string(p.width);
      else {signature+='[';for(std::size_t i=0;i<p.canonical.size();++i){if(i)signature+=',';signature+=p.canonical[i];}signature+=']';}
      signature+=",\"direction\":"+JsonQuote(directions[p.direction])+"}";
    }
    signature+="},\"type\":"+JsonQuote(type)+"}";
    append.Reset();append.BindInteger(1,seq);append.BindText(2,Digest(signature));append.Execute();
    line("  (* EMUFLOW_MAPPED = \"yes\", EMUFLOW_SOURCE = \"s"+std::to_string(seq)+"\" *)");
    put("  "+Identifier(type));bool parameter_first=true;
    for(const auto& item:parameters)if(!item.second.omit){put(parameter_first?" #(\n":",\n");parameter_first=false;put("    ."+Identifier(item.first)+"("+item.second.rtl+")");}
    if(!parameter_first)put("\n  )");line(" "+Identifier(name)+"(");first=true;
    for(const auto& item:ports_by_name) {
      if(!first)put(",\n");first=false;put("    ."+Identifier(item.first)+"(");const auto& bits=item.second.rtl;
      if(bits.size()==1)put(bits.front());else{put("{");for(std::size_t i=bits.size();i>0;--i){if(i<bits.size())put(", ");put(bits[i-1]);}put("}");}put(")");
    }
    line("");line("  );");++count;
  }
  Check(!pin && count==expected,"cell/pin population disagrees");line("endmodule");line("`default_nettype wire");
  output.Execute("CREATE INDEX signatures_function ON signatures(signature,seq)");transaction.Commit();append=SqliteStatement{};meta=SqliteStatement{};
  for(const auto& input:inputs)input.RequireUnchanged();
  const auto digest=Hex(rtl_hash);writer.Commit();signatures.Commit();
  summary_writer.stream()<<JsonRecord(std::map<std::string,JsonRecord>{{"schema","emuflow.vivado-export-summary/v1"},{"status","pass"},{"implementation_id","native-vivado-export-v1"},{"top",top},{"cells",count},{"mapped_index_sha256",mapped_sha},{"rtl_sha256",digest}}).Encode()<<'\n';summary_writer.Commit();
}
}
