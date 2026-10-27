#include "emuflow_core/transport.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/transport_schema.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <memory>
#include <set>
#include <tuple>
#include <unistd.h>

namespace emuflow::core {
namespace {
namespace fs=std::filesystem;
using Raw=std::map<std::string,std::string>;
using I=std::int64_t;
void Check(bool valid,const std::string& text){if(!valid)throw StageError(ErrorCode::kInvalidArtifact,"Phase 6 transport: "+text);}
std::string Object(const Raw& values){std::string out="{";for(const auto& value:values){if(out.size()>1)out+=',';out+=JsonQuote(value.first)+':'+value.second;}return out+'}';}
std::string Array(const std::vector<std::string>& values){std::string out="[";for(const auto& value:values){if(out.size()>1)out+=',';out+=value;}return out+']';}
Raw Fields(const std::string& text){Raw out;VisitJsonObjectTokens(text,[&](const auto& key,const auto& value){Check(out.size()<16384 && out.emplace(key,value).second,"duplicate or excessive control fields");});return out;}
std::vector<std::string> Values(const std::string& text){std::vector<std::string> out;VisitJsonArrayTokens(text,[&](const auto& value){Check(out.size()<16384,"control collection exceeds bound");out.push_back(value);});return out;}
std::string Get(const Raw& fields,const std::string& key){const auto found=fields.find(key);Check(found!=fields.end(),"missing field "+key);return found->second;}
std::string Text(const Raw& fields,const std::string& key){return DecodeJsonString(Get(fields,key));}
I Int(const std::string& token){I result=0;auto parsed=std::from_chars(token.data(),token.data()+token.size(),result);Check(parsed.ec==std::errc() && parsed.ptr==token.data()+token.size(),"invalid integer");return result;}
std::uint64_t UInt(const std::string& token){std::uint64_t result=0;auto parsed=std::from_chars(token.data(),token.data()+token.size(),result);Check(parsed.ec==std::errc() && parsed.ptr==token.data()+token.size(),"invalid unsigned integer");return result;}
std::string Meta(SqliteDatabase& db,const std::string& key){auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);Check(row.Step(),"missing metadata "+key);return row.TypedText(0);}
std::string Canonical(const std::string& text,unsigned depth=0){
  Check(depth<=64,"control record nesting exceeds bound");
  if(!text.empty() && text.front()=='{'){auto fields=Fields(text);for(auto& field:fields)field.second=Canonical(field.second,depth+1);return Object(fields);}
  if(!text.empty() && text.front()=='['){auto values=Values(text);for(auto& value:values)value=Canonical(value,depth+1);return Array(values);}
  if(!text.empty() && text.front()=='"')return JsonQuote(DecodeJsonString(text));
  if(!text.empty() && (text.front()=='-' || (text.front()>='0' && text.front()<='9')) && text.find_first_of(".eE")!=std::string::npos)return JsonRecord(ParseFiniteDecimal(text,"transport control number")).Encode();
  return text=="-0"?"0":text;
}
std::string Read(const fs::path& path){const auto size=fs::file_size(path);Check(size<=8*1024*1024,"control file exceeds bound");std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot read control");std::string out(size,'\0');in.read(out.data(),size);Check(bool(in),"truncated control");return out;}
void Add(Sha256& hash,const std::string& text){hash.update(reinterpret_cast<const unsigned char*>(text.data()),text.size());}
std::string Hex(Sha256& hash){std::string out;const char* digits="0123456789abcdef";for(auto c:hash.finish()){out+=digits[c>>4];out+=digits[c&15];}return out;}
std::string Digest(const std::string& text){Sha256 hash;Add(hash,text);return Hex(hash);}
std::string FileHash(const fs::path& path){std::ifstream in(path,std::ios::binary);Check(bool(in),"cannot seal transport store");Sha256 hash;std::array<char,65536> bytes;while(in){in.read(bytes.data(),bytes.size());hash.update(reinterpret_cast<const unsigned char*>(bytes.data()),in.gcount());}Check(in.eof(),"cannot read store seal");return Hex(hash);}
std::string Name(const std::string& text,bool prefix=false){(void)JsonQuote(text);std::string out;for(std::size_t i=0;i<text.size();){const auto c=static_cast<unsigned char>(text[i++]);if(c>=128){while(i<text.size() && (static_cast<unsigned char>(text[i])&0xc0)==0x80)++i;out+='_';}else out+=((c>='a' && c<='z') || (c>='A' && c<='Z') || (c>='0' && c<='9') || c=='_')?char(c):'_';}if(prefix && (out.empty() || (out[0]>='0' && out[0]<='9')))out="n_"+out;return out;}
std::string Port(const std::string& kind,const std::string& link,const std::string& peer){return kind+'_'+Name(link,true)+'_'+Name(peer,true);}
void Put(SqliteStatement& row,const std::vector<std::string>& strings){for(std::size_t i=0;i<strings.size();++i)row.BindText(int(i+1),strings[i]);row.Execute();}
struct Workspace{fs::path root;explicit Workspace(const fs::path& parent){auto name=(parent/".transport-stage-XXXXXX").string();Check(mkdtemp(name.data())!=nullptr,"cannot create workspace");root=name;}~Workspace(){if(root.empty())return;std::error_code error;fs::remove_all(root,error);}};
struct Link{std::string mode;I width=0,payload=0;};
struct PhysicalPort{Raw record;std::string role,logical,pin_p,pin_n,port_n;I width=0,count=0;Sha256 members;};
struct Section{I records=0;std::string sha;std::string Json()const{return Object({{"records",std::to_string(records)},{"sha256",JsonQuote(sha)}});}};
std::pair<Section,std::string> DocumentSection(SqliteDatabase& db,const Raw& header,const std::string& query){
  Raw fields=header;fields["entries"]="";Sha256 document,records;I count=0;Add(document,"{");bool first=true;
  for(const auto& field:fields){if(!first)Add(document,",");first=false;Add(document,JsonQuote(field.first)+':');
    if(field.first!="entries"){Add(document,field.second);continue;}Add(document,"[");auto rows=db.Prepare(query);bool record=true;
    while(rows.Step()){const auto encoded=Canonical(rows.TypedText(0));if(!record)Add(document,",");record=false;Add(document,encoded);Add(records,encoded);Add(records,"\n");++count;}Add(document,"]");}
  Add(document,"}");return {Section{count,Hex(records)},Hex(document)};
}
Section EndpointSection(SqliteDatabase& db,const std::string& fpga){
  // The generated RX prefix precedes TX. Within each prefix, schedule identity
  // order is endpoint identity order and uses the hop indexes directly. Never
  // sort a complete JSON endpoint population merely to produce its seal.
  Sha256 hash;I count=0;for(const auto* kind:{"rx","tx"}){auto rows=db.Prepare("SELECT record_json FROM endpoints WHERE fpga=? AND kind=? ORDER BY schedule_entry");rows.BindText(1,fpga);rows.BindText(2,kind);while(rows.Step()){Add(hash,Canonical(rows.TypedText(0)));Add(hash,"\n");++count;}}return {count,Hex(hash)};
}

void PublishText(const fs::path& path,const std::string& text,const std::vector<ArtifactRef>& refs){fs::create_directories(path.parent_path());PublishTextArtifact(path.string(),refs,[&](std::ostream& out){out<<text;});}
// Emitters consume normalized rows, never endpoint JSON or graph-sized lists.
void EmitTransportRtl(SqliteDatabase& db,const fs::path& path,const std::string& fpga,const std::map<std::string,Link>& links,I frame,I clocks,bool exact,const std::vector<ArtifactRef>& refs){
  fs::create_directories(path.parent_path());
  std::map<std::pair<std::string,std::string>,std::set<std::string>> groups;
  {auto q=db.Prepare("SELECT link,peer,kind FROM endpoint_values WHERE fpga=? GROUP BY link,peer,kind ORDER BY link,peer,kind");q.BindText(1,fpga);while(q.Step())groups[{q.TypedText(0),q.TypedText(1)}].insert(q.TypedText(2));}
  auto count=[&](const std::string& kind){auto q=db.Prepare("SELECT count(*) FROM signals WHERE fpga=? AND kind=?");q.BindText(1,fpga);q.BindText(2,kind);Check(q.Step(),"missing signal count");return std::max<I>(1,q.TypedInteger(0));};
  I bits=1;for(auto v=frame-1;v>1;v>>=1)++bits;
  PublishTextArtifact(path.string(),refs,[&](std::ostream& out){
    out<<"module emuflow_transport_"<<Name(fpga,true)<<" #(\n  parameter integer FRAME_SLOTS = "<<frame<<",\n  parameter integer SLOT_BITS = "<<bits<<",\n  parameter integer SOURCE_COUNT = "<<count("source")<<",\n  parameter integer SHADOW_COUNT = "<<count("shadow")<<"\n) (\n";
    std::vector<std::string> ports{"  input  logic fabric_clk","  input  logic reset","  input  logic links_ready","  input  logic [SOURCE_COUNT-1:0] source_values","  output logic [SHADOW_COUNT-1:0] shadow_values","  output logic ["+std::to_string(clocks-1)+":0] virtual_clock_enable","  output logic [SLOT_BITS-1:0] slot_debug"};
    for(const auto& group:groups)for(const auto* kind:{"rx","tx"})if(group.second.count(kind))ports.push_back("  "+std::string(kind==std::string("rx")?"input  ":"output ")+"logic ["+std::to_string(links.at(group.first.first).width-1)+":0] "+Port(kind,group.first.first,group.first.second));
    for(std::size_t n=0;n<ports.size();++n){if(n)out<<",\n";out<<ports[n];}out<<"\n);\n\n  logic [SLOT_BITS-1:0] slot;\n";
    if(exact)out<<"  (* KEEP = \"yes\", DONT_TOUCH = \"yes\" *)\n";
    out<<R"(  logic [SHADOW_COUNT-1:0] shadow_regs;
  emuflow_virtual_runtime_controller #(
    .FRAME_SLOTS(FRAME_SLOTS),
    .SLOT_BITS(SLOT_BITS)
  ) runtime_controller (
    .fabric_clk(fabric_clk),
    .reset(reset),
    .links_ready(links_ready),
    .virtual_clock_enable(virtual_clock_enable),
    .event_index_debug(),
    .slot(slot)
  );
  assign slot_debug = slot;
  assign shadow_values = shadow_regs;

)";
    auto rows=[&](const std::string& kind){const auto column=kind=="tx"?"slot":"arrival_slot";auto q=db.Prepare("SELECT e."+std::string(column)+",e.link,e.peer,e.lane,e.signal,s.seq FROM endpoint_values e JOIN signals s ON s.fpga=e.fpga AND s.signal=e.signal AND s.kind=CASE WHEN e.kind='tx' AND substr(e.signal,1,4)='net:' THEN 'source' ELSE 'shadow' END WHERE e.fpga=? AND e.kind=? ORDER BY e."+column+",e.link,e.lane,e.id");q.BindText(1,fpga);q.BindText(2,kind);return q;};
    bool tx=false;for(const auto& group:groups)if(group.second.count("tx"))tx=true;
    if(tx){out<<"  always @* begin\n";for(const auto& group:groups)if(group.second.count("tx"))out<<"    "<<Port("tx",group.first.first,group.first.second)<<" = '0;\n";out<<"    case (slot)\n";auto q=rows("tx");I active=-1;while(q.Step()){const I slot=q.TypedInteger(0);if(slot!=active){if(active>=0)out<<"      end\n";active=slot;out<<"      "<<slot<<": begin\n";}out<<"        "<<Port("tx",q.TypedText(1),q.TypedText(2))<<'['<<q.TypedInteger(3)<<"] = "<<(q.TypedText(4).rfind("net:",0)==0?"source_values[":"shadow_regs[")<<q.TypedInteger(5)<<"];\n";}if(active>=0)out<<"      end\n";out<<"      default: begin end\n    endcase\n  end\n\n";}
    out<<"  always_ff @(posedge fabric_clk) begin\n    if (reset) begin\n      shadow_regs <= '0;\n    end else begin\n      case (slot)\n";
    auto q=rows("rx");I active=-1;while(q.Step()){const I slot=q.TypedInteger(0);if(slot!=active){if(active>=0)out<<"        end\n";active=slot;out<<"        "<<slot<<": begin\n";}out<<"          shadow_regs["<<q.TypedInteger(5)<<"] <= "<<Port("rx",q.TypedText(1),q.TypedText(2))<<'['<<q.TypedInteger(3)<<"];\n";}if(active>=0)out<<"        end\n";out<<"        default: begin end\n      endcase\n    end\n  end\nendmodule\n";
  });
}
void EmitAnchors(SqliteDatabase& db,const fs::path& path,const std::string& fpga,const std::vector<ArtifactRef>& refs){
  PublishTextArtifact(path.string(),refs,[&](std::ostream& out){out<<"# EmuFlow virtual IO anchors.\n# This file is intentionally non-binding until a hardware BSP is selected.\n# Replace placeholders through the hardware pin planner; do not source this\n# template directly in Vivado.\n";auto q=db.Prepare("SELECT id,peer,lane,kind FROM anchors WHERE fpga=? ORDER BY id");q.BindText(1,fpga);while(q.Step()){const auto id=q.TypedText(0),port=Name(id,true);out<<"\n# anchor="<<id<<" peer="<<q.TypedText(1)<<" lane="<<q.TypedInteger(2)<<" direction="<<q.TypedText(3)<<"\n# set_property PACKAGE_PIN <PACKAGE_PIN> [get_ports {"<<port<<"}]\n# set_property IOSTANDARD <IOSTANDARD> [get_ports {"<<port<<"}]\n";}});
}
void EmitAdapter(SqliteDatabase& db,const fs::path& root,const std::string& fpga,const std::vector<ArtifactRef>& refs){
  struct Row{std::string p,n,role,logical,pin_p,pin_n,standard,bus;I width=0,bit=0;};
  std::vector<Row> records;std::map<std::string,std::pair<std::string,I>> buses;std::map<std::string,std::set<I>> used;
  auto q=db.Prepare("SELECT p_port,n_port,role,logical_port,logical_port_width,package_pin_p,package_pin_n,iostandard FROM binding_adapter_ports WHERE fpga=? ORDER BY p_port");q.BindText(1,fpga);
  while(q.Step()){Check(records.size()<32768,"physical adapter port inventory exceeds bound");Row row{q.TypedText(0),q.TypedText(1),q.TypedText(2),q.TypedText(3),q.TypedText(5),q.TypedText(6),q.TypedText(7),"",q.TypedInteger(4),0};const auto at=row.logical.find('[');Check(at!=std::string::npos && row.logical.back()==']',"invalid differential logical port");row.bus=row.logical.substr(0,at);row.bit=Int(row.logical.substr(at+1,row.logical.size()-at-2));Check(row.bit>=0 && row.bit<row.width && row.width<=UINT32_MAX && Name(row.bus,true)==row.bus && Name(row.p,true)==row.p && Name(row.n,true)==row.n,"invalid differential adapter bus or pin");const auto config=std::make_pair(row.role=="source"?std::string("input"):std::string("output"),row.width);auto prior=buses.emplace(row.bus,config);Check(prior.second || prior.first->second==config,"inconsistent differential adapter bus");if(row.role=="sink")used[row.bus].insert(row.bit);records.push_back(std::move(row));}
  PublishTextArtifact((root/"virtual_data_pins.xdc").string(),refs,[&](std::ostream& out){out<<"# Hypothetical LVDS board: vendor P/N pairs, assumed P-to-P and N-to-N cables.\n# Use with virtual_differential_io.sv exposed at the physical top level.\n# Data ports only; 1.8 V VCCO and internal 100-ohm receiver termination assumed.\n# Clock/reset/control constraints and physical backend integration remain required.\n";std::map<std::string,std::pair<std::string,std::string>> pins;for(const auto& row:records){pins[row.p]={row.pin_p,row.standard};pins[row.n]={row.pin_n,row.standard};}for(const auto& pin:pins){Check((pin.first+pin.second.first+pin.second.second).find_first_of("{}\n\r;")==std::string::npos,"unsafe virtual pin constraint");out<<"set_property PACKAGE_PIN "<<pin.second.first<<" [get_ports {"<<pin.first<<"}]\nset_property IOSTANDARD "<<pin.second.second<<" [get_ports {"<<pin.first<<"}]\n";}});
  PublishTextArtifact((root/"virtual_differential_io.sv").string(),refs,[&](std::ostream& out){out<<"// Standalone LVDS data adapter. Connect logical buses to emuflow_transport_<fpga>.\n// Expose P/N ports at the physical top level before applying virtual_data_pins.xdc.\n// Whole-bank directions are static for this build; internal receiver termination enabled.\nmodule emuflow_virtual_io_"<<Name(fpga)<<" (\n";bool first=true;const auto port=[&](const std::string& text){if(!first)out<<",\n";first=false;out<<text;};for(const auto& bus:buses)port("  "+bus.second.first+" wire ["+std::to_string(bus.second.second-1)+":0] "+bus.first);for(const auto& row:records){const auto direction=row.role=="source"?"output":"input";port("  "+std::string(direction)+" wire "+row.p);port("  "+std::string(direction)+" wire "+row.n);}out<<"\n);\n";I index=0;for(const auto& row:records){if(row.role=="source")out<<"  OBUFDS #(.IOSTANDARD(\"LVDS\")) tx_"<<index<<" (.I("<<row.logical<<"), .O("<<row.p<<"), .OB("<<row.n<<"));\n";else out<<"  IBUFDS #(.IOSTANDARD(\"LVDS\"), .DIFF_TERM(\"TRUE\")) rx_"<<index<<" (.I("<<row.p<<"), .IB("<<row.n<<"), .O("<<row.logical<<"));\n";++index;}for(const auto& bus:used)for(I bit=0;bit<buses.at(bus.first).second;++bit)if(!bus.second.count(bit))out<<"  assign "<<bus.first<<'['<<bit<<"] = 1'b0;\n";out<<"endmodule\n";});
}
void EmitRuntime(const fs::path& path,const Raw& runtime,const std::vector<ArtifactRef>& refs){
  const auto clocks=Fields(Get(runtime,"virtual_dut_clocks")),timebase=Fields(Get(runtime,"virtual_timebase"));const auto events=Values(Get(timebase,"events"));Check(!clocks.empty() && !events.empty() && events.size()<=4096,"invalid runtime calendar");std::map<std::string,std::size_t> ids;for(const auto& clock:clocks)ids.emplace(clock.first,ids.size());I bits=1;for(auto count=events.size()-1;count>1;count>>=1)++bits;
  PublishTextArtifact(path.string(),refs,[&](std::ostream& out){out<<"module emuflow_virtual_runtime_controller #(\n  parameter integer FRAME_SLOTS = 32,\n  parameter integer SLOT_BITS = 5\n) (\n  input  logic fabric_clk,\n  input  logic reset,\n  input  logic links_ready,\n  output logic ["<<clocks.size()-1<<":0] virtual_clock_enable,\n  output logic ["<<bits-1<<":0] event_index_debug,\n  output logic [SLOT_BITS-1:0] slot\n);\n  localparam integer CLOCK_COUNT = "<<clocks.size()<<";\n  localparam integer EVENT_COUNT = "<<events.size()<<";\n  logic started;\n  logic ["<<bits-1<<":0] event_index;\n\n";for(const auto& clock:ids)out<<"  // virtual_clock_enable["<<clock.second<<"] = "<<clock.first<<'\n';out<<"\n  always_comb begin\n    virtual_clock_enable = '0;\n    if (!reset && started && links_ready && (slot == FRAME_SLOTS - 1)) begin\n      case (event_index)\n";std::size_t index=0;for(const auto& event:events){std::string mask((clocks.size()+3)/4,'0');for(const auto& clock:Values(Get(Fields(event),"clocks"))){const auto name=DecodeJsonString(clock);Check(ids.count(name),"runtime event references unknown clock");const auto bit=ids.at(name),digit=mask.size()-bit/4-1;const auto old=std::string("0123456789abcdef").find(mask[digit]);mask[digit]="0123456789abcdef"[old | (1<<(bit%4))];}const auto first=mask.find_first_not_of('0');mask=first==std::string::npos?"0":mask.substr(first);out<<"      "<<index++<<": virtual_clock_enable = "<<clocks.size()<<"'h"<<mask<<";\n";}
    out<<R"(        default: virtual_clock_enable = '0;
      endcase
    end
    event_index_debug = event_index;
  end

  always_ff @(posedge fabric_clk) begin
    if (reset) begin
      slot <= '0;
      started <= 1'b0;
      event_index <= '0;
    end else if (!started) begin
      // No TDM work may execute before the distributed startup barrier.
      // The first complete frame begins only after global readiness.
      slot <= '0;
      if (links_ready) begin
        started <= 1'b1;
      end
    end else begin
      if (slot == FRAME_SLOTS - 1) begin
        if (links_ready) begin
          slot <= '0;
          if (event_index == EVENT_COUNT - 1)
            event_index <= '0;
          else
            event_index <= event_index + 1'b1;
        end
      end else begin
        slot <= slot + 1'b1;
      end
    end
  end
endmodule
)";
  });
}

struct Producer {
  const std::vector<ArtifactRef>& refs;Raw request,header;
  SqliteStore assignment,schedule;
  std::unique_ptr<SqliteStore> pin_plan;
  std::map<std::string,Link> links;
  std::map<std::string,std::string> parts;
  std::map<std::pair<std::string,I>,Raw> wires;
  std::map<std::string,std::pair<std::string,std::string>> directions;
  std::map<std::pair<std::string,std::string>,std::string> pin_ports,port_pins;
  std::map<std::pair<std::string,std::string>,PhysicalPort> physical;
  std::string design,platform,semantic,exact,clock_order,standard,board;
  I frame=0,entries=0;bool exact_mode=false,has_board=false;
  Producer(const std::vector<ArtifactRef>& inputs,const std::string& snapshot=""):refs(inputs),assignment(inputs[0]),schedule(inputs[1]) {
    const auto document=snapshot.empty()?Read(inputs[2].path()):snapshot;auto& json=schedule.database();auto valid=json.Prepare("SELECT json_valid(?1)");valid.BindText(1,document);Check(valid.Step() && valid.Integer(0),"invalid request JSON");
    auto duplicates=json.Prepare("SELECT 1 FROM json_tree(?1) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");duplicates.BindText(1,document);Check(!duplicates.Step(),"duplicate request key");request=Fields(Canonical(document));Check(Get(request,"emit_rtl")=="true" || Get(request,"emit_rtl")=="false","invalid emit_rtl flag");
    const std::set<std::string> keys{"schema","sources","design","platform","fpgas","links","clocks","virtual_board","emit_rtl","runtime"};Check(request.size()==keys.size(),"request field coverage disagrees");for(const auto& key:keys)Check(request.count(key),"missing request field");Check(Text(request,"schema")=="emuflow.transport-stage/v1","unsupported request");
    const auto sources=Values(Get(request,"sources"));Check(sources.size()+1==refs.size(),"original source binding coverage disagrees");
    for(std::size_t i=0;i<sources.size();++i){const auto fields=Fields(sources[i]);const auto& ref=refs[i<2?i:i+1];Check(fields.size()==3 && Text(fields,"path")==ref.path() && Text(fields,"schema")==ref.schema(),"original source namespace changed");const auto g=Values(Get(fields,"generation"));Check(g.size()==7,"source generation coverage disagrees");const FileGeneration original{UInt(g[0]),UInt(g[1]),UInt(g[2]),Int(g[3]),Int(g[4]),Int(g[5]),Int(g[6])};if(!(original==ref.generation()))throw StageError(ErrorCode::kMixedGeneration,"Phase 6 original source generation changed");}

    design=Text(Fields(Get(request,"design")),"name");platform=Text(request,"platform");header=Fields(Meta(schedule.database(),"header"));Check(Text(header,"schema")=="emuflow.tdm-schedule/v1" && Text(header,"design")==design && Text(header,"platform")==platform,"schedule namespace disagrees");
    Check(DecodeJsonString(Meta(assignment.database(),"design"))==design && DecodeJsonString(Meta(assignment.database(),"platform"))==platform,"assignment namespace disagrees");semantic=Canonical(Meta(assignment.database(),"semantic_contract_sha256"));exact=Canonical(Meta(assignment.database(),"semantic_contract_binary"));exact_mode=semantic!="null";
    if(exact_mode)Check(Get(header,"semantic_contract_sha256")==semantic && Text(header,"transport_semantics")=="sampled-virtual-wire" && Fields(Get(header,"schedule_dependency_certificate")).size()>0,"exact semantic contract disagrees");
    frame=Int(Get(Fields(Get(header,"metrics")),"frame_slots"));Check(frame>=2,"invalid frame size");
    if(Get(request,"runtime")!="null"){const auto runtime=Fields(Get(request,"runtime"));Check(Text(runtime,"design")==design && Text(runtime,"platform")==platform && Int(Get(Fields(Get(runtime,"frame")),"slots"))==frame,"runtime namespace/frame disagrees");}
    std::set<std::string> clocks;for(const auto& token:Values(Get(request,"clocks")))Check(clocks.insert(Text(Fields(token),"id")).second,"duplicate clock");if(clocks.empty())clocks.insert("clk");std::vector<std::string> clock_tokens;for(const auto& clock:clocks)clock_tokens.push_back(JsonQuote(clock));clock_order=Array(clock_tokens);
    for(const auto& token:Values(Get(request,"fpgas"))){auto f=Fields(token);const auto id=Text(f,"id");Check(!id.empty() && id!="." && id!=".." && fs::path(id).filename()==id && id.find('\0')==std::string::npos,"FPGA identity escapes stage directory");Check(parts.emplace(id,Text(f,"part")).second,"duplicate FPGA");}
    auto declared=assignment.database().Prepare("SELECT fpga FROM parts ORDER BY fpga");std::size_t count=0;while(declared.Step()){++count;Check(parts.count(declared.TypedText(0)),"unknown assigned FPGA");}Check(count==parts.size() && count>0,"FPGA inventory disagrees");
    for(const auto& token:Values(Get(request,"links"))){auto f=Fields(token);Link link{Text(f,"mode"),Int(Get(f,"width")),Int(Get(f,"payload"))};Check(link.width>0 && link.payload>0 && links.emplace(Text(f,"id"),link).second,"invalid link");}
    board=Get(request,"virtual_board");has_board=board!="null";if(has_board){const auto fields=Fields(board);Check(Text(fields,"schema")=="emuflow.virtual-board-wiring/v1" && Text(fields,"platform")==platform,"virtual board disagrees");standard=Text(Fields(Get(fields,"assumptions")),"iostandard");for(const auto& token:Values(Get(fields,"wires"))){auto wire=Fields(token);Check(wires.emplace(std::make_pair(Text(wire,"link"),Int(Get(wire,"wire"))),wire).second,"duplicate physical wire");}}
    if(refs.size()==4){pin_plan=std::make_unique<SqliteStore>(refs[3]);auto pin=Fields(Meta(pin_plan->database(),"header"));Check(Text(pin,"schema")=="emuflow.placement-aware-pin-plan/v1" && Text(pin,"provider")=="chimew-paper-plus-emuflow-electrical-slot-v1","unsupported pin plan");}
  }
  void Fill(SqliteDatabase& db) {
    db.Execute(kTransportDDL);
    auto attach=[&](const ArtifactRef& ref,const std::string& alias){auto row=db.Prepare("ATTACH DATABASE ? AS "+alias);row.BindText(1,SqliteImmutableUri(ref.path()));row.Execute();};attach(refs[0],"assignment");attach(refs[1],"schedule");if(pin_plan)attach(refs[3],"pin_plan");
    {auto row=db.Prepare("INSERT INTO metadata VALUES (?,?)");for(const auto& value:Raw{{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"frame_slots",std::to_string(frame)},{"virtual_clock_order",clock_order},{"semantic_contract_sha256",semantic},{"semantic_contract_storage",exact},{"iostandard",JsonQuote(standard)},{"configuration",Object(request)}})Put(row,{value.first,value.second});}
    db.Execute("INSERT INTO parts SELECT part_index,fpga FROM assignment.parts");
    const auto scalar=[&](const std::string& sql){auto q=db.Prepare(sql);Check(q.Step(),"missing coverage count");return q.TypedInteger(0);};
    Check(scalar("SELECT count(*) FROM schedule.schedule_entries e LEFT JOIN schedule.schedule_routes r ON r.demand=e.demand WHERE r.demand IS NULL OR e.net!=r.net")==0,"entry/route coverage disagrees");
    if(pin_plan){Check(scalar("SELECT count(*) FROM schedule.schedule_entries e LEFT JOIN pin_plan.entries p ON p.schedule_entry=e.entry_id WHERE p.schedule_entry IS NULL OR p.logical_lane!=e.lane")==0 && scalar("SELECT count(*) FROM pin_plan.entries p LEFT JOIN schedule.schedule_entries e ON e.entry_id=p.schedule_entry WHERE e.entry_id IS NULL")==0,"pin plan must cover schedule entries exactly");}
    auto hop=db.Prepare("INSERT INTO hops VALUES (?,?,?,?,?,?,?,?,?,?,?,?)");
    {auto wire=db.Prepare("INSERT INTO wire_bindings VALUES (?,?,?,?,?)");for(const auto& item:wires){const auto& value=item.second;wire.BindText(1,item.first.first);wire.BindInteger(2,item.first.second);wire.BindText(3,Text(value,"id"));wire.BindText(4,Get(value,"bank_group"));wire.BindText(5,Get(value,"cable"));wire.Execute();}}
    auto rows=db.Prepare("SELECT e.entry_id,e.demand,e.net,e.link_id,e.source_fpga,e.sink_fpga,e.slot,e.arrival_slot,e.lane,e.tdm_ratio,r.source_fpga,"+std::string(pin_plan?"p.physical_lane":"e.lane")+" FROM schedule.schedule_entries e JOIN schedule.schedule_routes r ON r.demand=e.demand "+(pin_plan?"LEFT JOIN pin_plan.entries p ON p.schedule_entry=e.entry_id ":"")+"ORDER BY e.entry_id");
    while(rows.Step()){
      const auto id=rows.TypedText(0),demand=rows.TypedText(1),net=rows.TypedText(2),link=rows.TypedText(3),from=rows.TypedText(4),to=rows.TypedText(5),route_source=rows.TypedText(10);const I slot=rows.TypedInteger(6),arrival=rows.TypedInteger(7),logical=rows.TypedInteger(8),wire=rows.TypedInteger(11);
      Check(!id.empty() && !demand.empty() && !net.empty() && parts.count(from) && parts.count(to) && links.count(link) && from!=to,"invalid scheduled endpoint");Check(slot>=0 && arrival>=slot && arrival<frame && logical>=0 && wire>=0 && wire<links.at(link).width,"invalid slot/lane");
      const std::array<std::string,6> identity{id,demand,net,link,from,to};for(unsigned n=0;n<identity.size();++n)hop.BindText(n+1,identity[n]);hop.BindInteger(7,slot);hop.BindInteger(8,arrival);hop.BindInteger(9,wire);hop.BindInteger(10,logical);hop.BindText(11,route_source);if(rows.IsNull(9))hop.BindNull(12);else hop.BindInteger(12,rows.TypedInteger(9));hop.Execute();
      if(has_board){
        const auto found=wires.find({link,wire});Check(found!=wires.end(),"scheduled wire has no virtual board connection");const auto& connection=found->second;const auto wire_id=Text(connection,"id");const auto direction=std::make_pair(from,to);const auto previous=directions.emplace(wire_id,direction);Check(previous.second || previous.first->second==direction,"one physical wire cannot change direction within the schedule");
        std::map<std::string,Raw> pins;for(const auto& token:Values(Get(connection,"endpoints"))){auto fields=Fields(token);Check(pins.emplace(Text(fields,"fpga"),fields).second,"duplicate wire endpoint");}
        for(bool transmit:{true,false}){
          const std::string role=transmit?"source":"sink",kind=transmit?"tx":"rx",fpga=transmit?from:to,peer=transmit?to:from;Check(pins.count(fpga),"wire omits scheduled FPGA");const auto& pair=pins.at(fpga);const auto bus=Port(kind,link,peer);const auto logical_port=bus+'['+std::to_string(wire)+']';
          Raw end{{"fpga",Get(pair,"fpga")},{"bank",Get(pair,"bank")},{"pair_id",Get(pair,"pair_id")},{"logical_port",JsonQuote(logical_port)},{"logical_port_width",std::to_string(links.at(link).width)}};
          std::vector<std::string> port_names,pin_names;
          for(const auto* polarity:{"p","n"}){auto pin=Fields(Get(pair,polarity));const auto port="emuflow_io_"+kind+'_'+Name(link)+"_wire"+std::to_string(wire)+'_'+polarity;const auto package=Text(pin,"package_pin");const auto pin_key=std::make_pair(fpga,package),port_key=std::make_pair(fpga,port);auto a=pin_ports.emplace(pin_key,port),b=port_pins.emplace(port_key,package);Check((a.second || a.first->second==port) && (b.second || b.first->second==package),"package pin or RTL port collision");pin["port"]=JsonQuote(port);end[polarity]=Object(pin);port_names.push_back(port);pin_names.push_back(package);}
          Raw contract{{"id",JsonQuote(kind+':'+port_names[0])},{"direction",JsonQuote(kind)},{"link",Get(connection,"link")},{"wire",Get(connection,"wire")},{"wire_id",JsonQuote(wire_id)},{"bank",Get(end,"bank")},{"pair_id",Get(end,"pair_id")},{"iostandard",JsonQuote(standard)},{"logical_port",JsonQuote(bus)},{"logical_bit",std::to_string(wire)},{"logical_port_width",Get(end,"logical_port_width")},{"p",Get(end,"p")},{"n",Get(end,"n")}};
          auto& owner=physical[{fpga,port_names[0]}];if(!owner.record.empty())Check(owner.record==contract,"one differential pair has conflicting bindings");else {owner.record=contract;owner.role=role;owner.logical=logical_port;owner.width=links.at(link).width;owner.pin_p=pin_names[0];owner.pin_n=pin_names[1];owner.port_n=port_names[1];}
          ++owner.count;Add(owner.members,JsonQuote(id));Add(owner.members,"\n");
        }
      }
      ++entries;
    }
    Check(entries==scalar("SELECT count(*) FROM schedule.schedule_entries"),"hop coverage disagrees");
    if(exact_mode)Check(scalar("SELECT count(*) FROM endpoints e LEFT JOIN assignment.cut_sinks c ON c.net_id=e.net WHERE c.net_id IS NULL")==0,"unknown exact cut node");
    {auto ports=db.Prepare("INSERT INTO binding_physical_ports VALUES (?,?,?,?,?,?,?,?)");for(auto& item:physical){const auto& key=item.first;auto& value=item.second;ports.BindText(1,key.first);ports.BindText(2,key.second);ports.BindText(3,Object(value.record));ports.BindInteger(4,value.count);ports.BindText(5,Hex(value.members));ports.BindText(6,Text(value.record,"link"));ports.BindInteger(7,Int(Get(value.record,"wire")));ports.BindText(8,Text(value.record,"direction"));ports.Execute();}}
    Check(!has_board || (scalar("SELECT count(*) FROM binding_entries")==entries && scalar("SELECT count(*) FROM (SELECT 1 FROM hops GROUP BY link_id,slot,lane HAVING count(*)>1)")==0),"physical binding coverage or slot collision");
    auto signal=db.Prepare("INSERT INTO signals VALUES (?,?,?,?)");for(const auto& part:parts)for(bool source:{true,false}){auto values=db.Prepare("SELECT DISTINCT signal FROM endpoints WHERE fpga=? AND kind=? "+std::string(source?"AND substr(signal,1,4)='net:' ":"")+"ORDER BY signal");values.BindText(1,part.first);values.BindText(2,source?"tx":"rx");I index=0;while(values.Step()){signal.BindText(1,part.first);signal.BindText(2,source?"source":"shadow");signal.BindInteger(3,index++);signal.BindText(4,values.TypedText(0));signal.Execute();}}
    auto anchors=db.Prepare("SELECT fpga,link,peer,kind,lane FROM endpoints GROUP BY fpga,link,peer,kind,lane ORDER BY fpga,link,peer,kind,lane"),insert=db.Prepare("INSERT INTO anchors VALUES (?,?,?,?,?,?,?)");while(anchors.Step()){const auto fpga=anchors.TypedText(0),link=anchors.TypedText(1),peer=anchors.TypedText(2),kind=anchors.TypedText(3);const I lane=anchors.TypedInteger(4);const auto& board_link=links.at(link);const bool serial=board_link.mode=="serial";const auto id=link+':'+fpga+':'+kind+':'+std::to_string(lane);const auto required=serial?"[\"package_pin_p\",\"package_pin_n\",\"connector\",\"transceiver_site\"]":"[\"package_pin\",\"bank\",\"iostandard\"]";
      Raw record{{"id",JsonQuote(id)},{"link",JsonQuote(link)},{"peer",JsonQuote(peer)},{"direction",JsonQuote(kind)},{"logical_lane",std::to_string(lane)},{"physical_lane",std::to_string(serial?lane/board_link.payload:lane)},{"bit_within_physical_lane",std::to_string(serial?lane%board_link.payload:0)},{"placement_class",JsonQuote("virtual_link_io_region")},{"binding_status",JsonQuote("unbound")},{"required_hardware_binding_fields",required}};insert.BindText(1,id);insert.BindText(2,fpga);insert.BindText(3,link);insert.BindText(4,peer);insert.BindText(5,kind);insert.BindInteger(6,lane);insert.BindText(7,Object(record));insert.Execute();}
  }
  void Describe(SqliteDatabase& db,const fs::path& root,std::vector<std::tuple<fs::path,std::string,std::string,Raw,Raw,std::string>>& selectors) {
    Raw lane_header{{"schema",JsonQuote("emuflow.logical-lane-map/v1")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"frame_slots",std::to_string(frame)},{"binding_status",JsonQuote("logical_only")}};if(exact_mode){lane_header["cut_mode"]=JsonQuote("static-exact-combinational");lane_header["semantic_contract_sha256"]=semantic;}
    const auto lane_query="SELECT record_json FROM lanes ORDER BY schedule_entry";const auto lane_section=DocumentSection(db,lane_header,lane_query);const auto lane_sha=lane_section.second;selectors.emplace_back(root/"lane_map.json","emuflow.logical-lane-map-storage/v1","emuflow.logical-lane-map/v1",lane_header,Raw{{"entries",lane_section.first.Json()}},lane_sha);
    if(has_board){Raw binding_header{{"schema",JsonQuote("emuflow.virtual-board-binding/v1")},{"platform",JsonQuote(platform)},{"design",JsonQuote(design)},{"qualification",JsonQuote("virtual_board_real_device_pins")},{"status",JsonQuote("pass")},{"integration_status",JsonQuote("differential_data_constraints_and_adapter_generated")},{"physical_backend_status",JsonQuote("phase7_consumption_required")},{"board_services_status",JsonQuote("clock_reset_and_control_pins_not_bound")},{"io_adapter_status",JsonQuote("phase7_top_level_integration_required")},{"board_sha256",JsonQuote(Digest(board))},{"schedule_sha256",Meta(schedule.database(),"logical_sha256")},{"lane_map_sha256",JsonQuote(lane_sha)},{"metrics",Object({{"scheduled_hops",std::to_string(entries)},{"used_differential_pairs",std::to_string(directions.size())},{"bound_data_ports",std::to_string(pin_ports.size())},{"total_differential_pairs",std::to_string(wires.size())}})}};
      const auto query="SELECT record_json FROM binding_entries ORDER BY schedule_entry";const auto section=DocumentSection(db,binding_header,query);selectors.emplace_back(root/"virtual_board_binding.json","emuflow.virtual-board-binding-storage/v1","emuflow.virtual-board-binding/v1",binding_header,Raw{{"entries",section.first.Json()}},section.second);}
    for(const auto& part:parts){const auto& fpga=part.first;Raw transport{{"schema",JsonQuote("emuflow.transport-endpoints/v1")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},{"fpga",JsonQuote(fpga)},{"frame_slots",std::to_string(frame)},{"virtual_clock_order",clock_order}};if(exact_mode){transport["cut_mode"]=JsonQuote("static-exact-combinational");transport["semantic_contract_sha256"]=semantic;if(exact!="null")transport["semantic_contract_storage"]=exact;transport["shadow_reset_value"]="0";transport["startup_policy"]=JsonQuote("first-post-reset-frame-current-arrivals");}
      Raw sections{{"endpoints",EndpointSection(db,fpga).Json()}};for(const auto* kind:{"source","shadow"}){auto rows=db.Prepare("SELECT seq,signal FROM signals WHERE fpga=? AND kind=? ORDER BY seq");rows.BindText(1,fpga);rows.BindText(2,kind);Sha256 hash;I count=0;while(rows.Step()){Add(hash,Object({{"index",std::to_string(rows.TypedInteger(0))},{"signal",JsonQuote(rows.TypedText(1))}}));Add(hash,"\n");++count;}sections[std::string(kind)+"_signals"]=Section{count,Hex(hash)}.Json();}
      selectors.emplace_back(root/fpga/"transport.json","emuflow.transport-storage/v1","emuflow.transport-endpoints/v1",transport,sections,"");
      auto anchors=db.Prepare("SELECT id,record_json FROM anchors WHERE fpga=? ORDER BY id");anchors.BindText(1,fpga);Sha256 hash;I count=0;std::set<std::string> required;
      while(anchors.Step()){const auto record=anchors.TypedText(1);Add(hash,std::string("anchor\0",7));Add(hash,Canonical(record));Add(hash,"\n");++count;for(const auto& field:Values(Get(Fields(record),"required_hardware_binding_fields")))required.insert(DecodeJsonString(field));auto members=db.Prepare("SELECT e.id,e.slot FROM endpoints e JOIN anchors a ON a.fpga=e.fpga AND a.link=e.link AND a.peer=e.peer AND a.kind=e.kind AND a.lane=e.lane WHERE a.id=? ORDER BY e.id");members.BindText(1,anchors.TypedText(0));while(members.Step()){Add(hash,std::string("member\0",7));Add(hash,members.TypedText(0));Add(hash,std::string(1,'\0'));Add(hash,std::to_string(members.TypedInteger(1)));Add(hash,"\n");}}
      std::vector<std::string> names;for(const auto& name:required)names.push_back(JsonQuote(name));Raw anchor{{"schema",JsonQuote("emuflow.virtual-io-anchors/v1")},{"platform",JsonQuote(platform)},{"fpga",JsonQuote(fpga)},{"part",JsonQuote(part.second)},{"required_hardware_binding_fields",Array(names)}};selectors.emplace_back(root/fpga/"virtual_anchors.json","emuflow.virtual-io-anchors-storage/v1","emuflow.virtual-io-anchors/v1",anchor,Raw{{"anchors",Section{count,Hex(hash)}.Json()}},"");
      if(Get(request,"emit_rtl")=="true"){EmitTransportRtl(db,root/fpga/"transport_schedule.sv",fpga,links,frame,I(Values(clock_order).size()),exact_mode,refs);EmitAnchors(db,root/fpga/"virtual_anchors.xdc.template",fpga,refs);if(has_board)EmitAdapter(db,root/fpga,fpga,refs);}
    }
    if(Get(request,"emit_rtl")=="true" && Get(request,"runtime")!="null")EmitRuntime(root/"virtual_runtime_controller.sv",Fields(Get(request,"runtime")),refs);
  }
};
std::string AuditRecords(const ArtifactRef& artifact){
  SqliteStore store(artifact);auto& db=store.database();const auto snapshot=Meta(db,"configuration");auto configuration=Fields(snapshot);
  const auto bindings=Values(Get(configuration,"sources"));Check(bindings.size()==2 || bindings.size()==3,"audit original-source coverage disagrees");
  std::vector<ArtifactRef> controls;for(std::size_t i=0;i<bindings.size();++i){auto fields=Fields(bindings[i]);const auto expected=i==0?"assignment-index-v4":i==1?"tdm-schedule-store-v4":"pin-plan-store-v1";Check(Text(fields,"schema")==expected,"audit source role disagrees");controls.push_back(ArtifactRef::Bind(Text(fields,"path"),expected));}
  controls.insert(controls.begin()+2,artifact);Producer expected(controls,snapshot);
  for(const auto& value:Raw{{"design",JsonQuote(expected.design)},{"platform",JsonQuote(expected.platform)},{"frame_slots",std::to_string(expected.frame)},{"virtual_clock_order",expected.clock_order},{"semantic_contract_sha256",expected.semantic}})Check(Meta(db,value.first)==value.second,"audit store namespace disagrees");
  auto attach=[&](const ArtifactRef& ref,const std::string& name){auto q=db.Prepare("ATTACH DATABASE ? AS "+name);q.BindText(1,SqliteImmutableUri(ref.path()));q.Execute();};attach(controls[1],"audit_schedule");if(controls.size()==4)attach(controls[3],"audit_pins");
  const auto scalar=[&](const std::string& sql){auto q=db.Prepare(sql);Check(q.Step(),"audit scalar missing");return q.TypedInteger(0);};
  const auto projected="SELECT e.entry_id,e.demand,e.net,e.link_id,e.source_fpga,e.sink_fpga,e.slot,e.arrival_slot,"+std::string(controls.size()==4?"p.physical_lane":"e.lane")+",e.lane,r.source_fpga,e.tdm_ratio FROM audit_schedule.schedule_entries e JOIN audit_schedule.schedule_routes r ON r.demand=e.demand "+(controls.size()==4?"LEFT JOIN audit_pins.entries p ON p.schedule_entry=e.entry_id ":"");
  const auto columns="entry_id,demand,net,link_id,source_fpga,sink_fpga,slot,arrival_slot,lane,logical_lane,route_source,tdm_ratio";
  using Key=std::tuple<std::string,std::string,I,std::string>;
  struct Membership{I count=0;Sha256 hash;};std::map<Key,Membership> members;
  auto actual=db.Prepare("SELECT "+std::string(columns)+" FROM hops ORDER BY entry_id");
  auto original=db.Prepare(projected+" ORDER BY e.entry_id");I count=0,maximum=0;
  while(true){const bool have_actual=actual.Step(),have_original=original.Step();Check(have_actual==have_original,"audit original hop coverage disagrees");if(!have_actual)break;
    for(unsigned column=0;column<12;++column){if(column==11){Check(actual.IsNull(column)==original.IsNull(column),"audit ratio presence disagrees");if(actual.IsNull(column))continue;}
      const bool string=column<6 || column==10;Check(string?actual.TypedText(column)==original.TypedText(column):actual.TypedInteger(column)==original.TypedInteger(column),"audit hop facts disagree with original schedule or pin plan");}
    ++count;maximum=std::max(maximum,original.TypedInteger(7));
    if(expected.has_board)for(bool tx:{true,false}){const std::string fpga=original.TypedText(tx?4:5),link=original.TypedText(3),kind=tx?"tx":"rx";const I lane=original.TypedInteger(8);auto& value=members[{fpga,link,lane,kind}];++value.count;Add(value.hash,JsonQuote(original.TypedText(0)));Add(value.hash,"\n");}
  }
  Check(count==Int(Meta(expected.schedule.database(),"entry_relation_records")) && count==scalar("SELECT count(*) FROM audit_schedule.schedule_entries"),"audit complete schedule coverage disagrees");
  Check(maximum==Int(Get(Fields(Get(expected.header,"metrics")),"completion_slot")),"audit runtime completion disagrees with original arrivals");
  std::vector<std::string> records;std::set<Key> visited;
  auto ports=db.Prepare("SELECT fpga,p_port,record_json,schedule_entry_count,schedule_entries_sha256,link,wire,kind FROM binding_physical_ports ORDER BY fpga,p_port");
  while(ports.Step()){
    const auto fpga=ports.TypedText(0),port=ports.TypedText(1),link=ports.TypedText(5),kind=ports.TypedText(7);const I wire=ports.TypedInteger(6);const Key key{fpga,link,wire,kind};Check(expected.has_board && members.count(key) && visited.insert(key).second && expected.wires.count({link,wire}) && expected.links.count(link),"audit physical-port ownership disagrees");
    const auto& board_wire=expected.wires.at({link,wire});std::map<std::string,Raw> ends;for(const auto& token:Values(Get(board_wire,"endpoints"))){auto end=Fields(token);Check(ends.emplace(Text(end,"fpga"),end).second,"duplicate original physical endpoint");}Check(ends.size()==2 && ends.count(fpga),"audit physical FPGA disagrees");const auto peer=ends.begin()->first==fpga?std::next(ends.begin())->first:ends.begin()->first;const auto& end=ends.at(fpga);
    Raw canonical{{"id",JsonQuote(kind+':'+port)},{"direction",JsonQuote(kind)},{"link",Get(board_wire,"link")},{"wire",Get(board_wire,"wire")},{"wire_id",Get(board_wire,"id")},{"bank",Get(end,"bank")},{"pair_id",Get(end,"pair_id")},{"iostandard",JsonQuote(expected.standard)},{"logical_port",JsonQuote(Port(kind,link,peer))},{"logical_bit",std::to_string(wire)},{"logical_port_width",std::to_string(expected.links.at(link).width)}};
    for(const auto* polarity:{"p","n"}){auto pin=Fields(Get(end,polarity));pin["port"]=JsonQuote("emuflow_io_"+kind+'_'+Name(link)+"_wire"+std::to_string(wire)+'_'+polarity);canonical[polarity]=Object(pin);}
    Check(port==Text(Fields(Get(canonical,"p")),"port") && Object(canonical)==Canonical(ports.TypedText(2)),"audit physical pin/bus/direction facts disagree with original fixed wiring");
    auto& membership=members.at(key);const auto digest=Hex(membership.hash);Check(ports.TypedInteger(3)==membership.count && ports.TypedText(4)==digest,"physical-port membership disagrees with binding entries");
    canonical["schedule_entry_count"]=std::to_string(membership.count);canonical["schedule_entries_sha256"]=JsonQuote(digest);records.push_back(Object({{"fpga",JsonQuote(fpga)},{"record",Object(canonical)}}));
  }
  Check(visited.size()==members.size(),"audit original physical-port coverage is incomplete");
  for(const auto& input:controls)input.RequireUnchanged();artifact.RequireUnchanged();
  return Object({{"status",JsonQuote("pass")},{"provider",JsonQuote("native-source-transport-audit-v1")},{"scheduled_hops",std::to_string(scalar("SELECT count(*) FROM hops"))},{"ports",Array(records)}});
}

}
StageResult BuildTransportArtifacts(const std::vector<ArtifactRef>& inputs,const std::string& directory){
  return RunStage("phase6.transport",[&]{Check((inputs.size()==3 || inputs.size()==4) && inputs[0].schema()=="assignment-index-v4" && inputs[1].schema()=="tdm-schedule-store-v4" && inputs[2].schema()=="transport-stage-request-v1" && (inputs.size()==3 || inputs[3].schema()=="pin-plan-store-v1"),"input roles disagree");return inputs;},[&](const auto& refs){
    const auto destination=fs::absolute(directory);Check(!fs::exists(destination) && !fs::is_symlink(destination),"output directory exists");fs::create_directories(destination.parent_path());Workspace workspace(destination.parent_path());Producer producer(refs);const auto root=workspace.root;std::vector<std::tuple<fs::path,std::string,std::string,Raw,Raw,std::string>> selectors;
    {SqliteArtifactWriter writer((root/"transport.sqlite").string(),"transport-store-v2",refs);auto& db=writer.database();db.Execute("PRAGMA temp_store=FILE;PRAGMA cache_size=-131072");{SqliteTransaction transaction(db);producer.Fill(db);transaction.Commit();}producer.Describe(db,root,selectors);writer.Commit();}
    (void)AuditRecords(ArtifactRef::Bind((root/"transport.sqlite").string(),"transport-store-v2"));
    const auto bytes=fs::file_size(root/"transport.sqlite");const auto sha=FileHash(root/"transport.sqlite");
    for(const auto& item:selectors){const auto& [path,schema,logical,header,sections,canonical]=item;Raw manifest{{"schema",JsonQuote(schema)},{"logical_schema",JsonQuote(logical)},{"artifact",JsonQuote(path.parent_path()==root?"transport.sqlite":"../transport.sqlite")},{"application_id",std::to_string(0x454d5558)},{"user_version","2"},{"store_bytes",std::to_string(bytes)},{"store_sha256",JsonQuote(sha)},{"header",Object(header)},{"sections",Object(sections)},{"logical_sha256",JsonQuote(Digest(Object({{"header",Object(header)},{"sections",Object(sections)}})))}};if(!canonical.empty())manifest["canonical_sha256"]=JsonQuote(canonical);PublishText(path,Object(manifest)+'\n',refs);}
    std::vector<std::string> order;auto selected=producer.assignment.database().Prepare("SELECT fpga FROM parts ORDER BY part_index");while(selected.Step())order.push_back(JsonQuote(selected.TypedText(0)));
    I anchor_count=0;{SqliteDatabase db((root/"transport.sqlite").string(),SqliteDatabase::Mode::kImmutableReadOnly);auto row=db.Prepare("SELECT count(*) FROM anchors");Check(row.Step(),"missing anchor count");anchor_count=row.TypedInteger(0);}
    PublishText(root/"transport-summary.json",Object({{"schema",JsonQuote("emuflow.transport-storage/v1")},{"status",JsonQuote("pass")},{"artifact",JsonQuote("transport.sqlite")},{"bytes",std::to_string(bytes)},{"sha256",JsonQuote(sha)},{"scheduled_hops",std::to_string(producer.entries)},{"transport_endpoints",std::to_string(2*producer.entries)},{"lane_entries",std::to_string(producer.entries)},{"virtual_binding_entries",std::to_string(producer.has_board?producer.entries:0)},{"dataflow",JsonQuote("indexed-lazy-sqlite-v1")},{"implementation_id",JsonQuote("native-transport-stage-v1")},{"anchors",std::to_string(anchor_count)},{"fpga_order",Array(order)}})+'\n',refs);
    PublishNewDirectory(root.string(),destination.string(),refs);workspace.root.clear();
  },[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind((fs::path(directory)/"transport.sqlite").string(),"transport-store-v2"),ArtifactRef::Bind((fs::path(directory)/"transport-summary.json").string(),"transport-stage-summary-v1")};});
}
StageResult AuditTransportArtifacts(const ArtifactRef& artifact,const std::string& summary){
  std::vector<ArtifactRef> sources{artifact};
  return RunStage("phase6.audit-transport",[&]{Check(artifact.schema()=="transport-store-v2","audit requires current hop owner");SqliteStore store(artifact);for(const auto& token:Values(Get(Fields(Meta(store.database(),"configuration")),"sources"))){const auto value=Fields(token);sources.push_back(ArtifactRef::Bind(Text(value,"path"),Text(value,"schema")));}return sources;},[&](const auto& refs){auto text=AuditRecords(artifact);PublishTextArtifact(summary,refs,[&](std::ostream& out){out<<text<<'\n';});},[&]{return std::vector<ArtifactRef>{ArtifactRef::Bind(summary,"transport-audit-summary-v1")};});
}
}
