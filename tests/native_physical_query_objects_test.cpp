#include "emuflow_core/physical_query_objects.hpp"
#include "emuflow_core/emuir_compact_schema.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/sqlite.hpp"
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <vector>
#include <unistd.h>

namespace {
namespace fs=std::filesystem;
using namespace emuflow::core;
using I=std::int64_t;
void Check(bool value,const char* reason){if(!value)throw std::runtime_error(reason);}
void Reject(const std::function<void()>& run) {
  bool rejected=false;try{run();}catch(const StageError&){rejected=true;}
  Check(rejected,"invalid object or changed source was accepted");
}
void Object(PhysicalQueryObjects& resolver,const PhysicalEndpoint& endpoint,
            const std::string& kind,const std::string& name) {
  const auto actual=resolver.Resolve(endpoint);
  Check(actual.kind==kind && actual.name==name,"physical object name differs from golden fact");
}
struct Scratch {
  fs::path root;
  Scratch(){auto pattern=(fs::temp_directory_path()/"physical-objects-XXXXXX").string();Check(mkdtemp(pattern.data())!=nullptr,"scratch failed");root=pattern;}
  ~Scratch(){std::error_code error;fs::remove_all(root,error);}
};
struct Fixture {
  SqliteDatabase db;
  I payloads=0,cells=0,nets=0,endpoints=0,pins=0;
  explicit Fixture(const fs::path& path):db(path.string(),SqliteDatabase::Mode::kCreateExclusive) {
    db.Execute("PRAGMA application_id=0x454d5549;PRAGMA user_version=2;");db.Execute(kEmuirCompactDDL);
    db.Execute("CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit)");
  }
  I Payload(const std::string& value) {
    auto existing=db.Prepare("SELECT seq FROM payloads WHERE value=?");existing.BindText(1,value);
    if(existing.Step())return existing.TypedInteger(0);
    auto insert=db.Prepare("INSERT INTO payloads VALUES(?,?)");insert.BindInteger(1,payloads);insert.BindText(2,value);insert.Execute();return payloads++;
  }
  I Cell(const std::string& id,const std::string& type,const std::string& resources="{}",
         const std::string& parameters="{}",const std::string& constants="[]") {
    const auto resource=Payload(resources),parameter=Payload(parameters),constant=Payload(constants),extra=Payload("{}");
    auto row=db.Prepare("INSERT INTO cell_records VALUES(?,?,NULL,1,?,?,?,NULL,?,?)");
    row.BindInteger(1,cells);row.BindText(2,id);row.BindText(3,type);row.BindInteger(4,resource);
    row.BindInteger(5,parameter);row.BindInteger(6,constant);row.BindInteger(7,extra);row.Execute();return cells++;
  }
  void Pin(I cell,const std::string& name,I bit=0,bool driver=false) {
    I pin;
    auto lookup=db.Prepare("SELECT seq FROM pin_names WHERE name=?");lookup.BindText(1,name);
    if(lookup.Step())pin=lookup.TypedInteger(0);
    else{pin=pins++;auto row=db.Prepare("INSERT INTO pin_names VALUES(?,?)");row.BindInteger(1,pin);row.BindText(2,name);row.Execute();}
    const auto empty=Payload("{}");
    auto net=db.Prepare("INSERT INTO net_records VALUES(?,?,NULL,1,NULL,NULL,0,'combinational',?)");
    net.BindInteger(1,nets);net.BindText(2,"n"+std::to_string(nets));net.BindInteger(3,empty);net.Execute();
    auto endpoint=db.Prepare("INSERT INTO endpoint_records VALUES(?,?,0,?,?,?,?)");
    endpoint.BindInteger(1,nets++);endpoint.BindInteger(2,driver?0:1);endpoint.BindInteger(3,cell);
    endpoint.BindInteger(4,pin);endpoint.BindInteger(5,bit);endpoint.BindInteger(6,empty);endpoint.Execute();++endpoints;
  }
  void SparseCell(I previous,I sparse) {
    auto endpoints=db.Prepare("UPDATE endpoint_records SET instance_seq=? WHERE instance_seq=?");
    endpoints.BindInteger(1,sparse);endpoints.BindInteger(2,previous);endpoints.Execute();
    auto cell=db.Prepare("UPDATE cell_records SET seq=? WHERE seq=?");
    cell.BindInteger(1,sparse);cell.BindInteger(2,previous);cell.Execute();
  }
  void Finish() {
    db.Execute("INSERT INTO metadata VALUES('storage_schema','emuflow.emuir-sqlite/v2')");
    {auto counts=db.Prepare("INSERT INTO metadata VALUES('record_counts_json',?)");
    counts.BindText(1,JsonRecord(std::map<std::string,JsonRecord>{{"cell_records",cells},{"net_records",nets},{"endpoint_records",endpoints}}).Encode());counts.Execute();}
    db.Execute(R"SQL(INSERT INTO port_records VALUES(0,'{"id":"in_bus","width":4,"direction":"input"}'),(1,'{"id":"out","width":1,"direction":"output"}'))SQL");
    db.Close();
  }
};
}  // namespace

int main(int argc,char** argv) {
  try {
    Scratch scratch;
    const auto path=scratch.root/"merged.sqlite",library_path=scratch.root/"primitives.json";
    const auto authoritative=argc==2?fs::path(argv[1]):fs::path(__FILE__).parent_path().parent_path()/"src/emuflow/data/rapidwright/xilinx-ultrascaleplus-open-v1.primitives.json";
    fs::copy_file(authoritative,library_path);
    Fixture fixture(path);
    const auto lut=fixture.Cell("lut","$lut","{\"lut\":1}","{\"WIDTH\":\"10\",\"LUT\":\"1000\"}");
    fixture.Pin(lut,"A",0);fixture.Pin(lut,"A",1);fixture.Pin(lut,"Y",0,true);
    const auto ff=fixture.Cell("ff","FDRE","{\"ff\":1}","{}",R"([{"port":"CE","bit":0,"value":"1"},{"port":"R","bit":0,"value":"0"}])");
    fixture.Pin(ff,"C");fixture.Pin(ff,"D");fixture.Pin(ff,"Q",0,true);
    const auto clear=fixture.Cell("clear","FDCE","{\"ff\":1}","{}",R"([{"port":"CE","bit":0,"value":"1"},{"port":"CLR","bit":0,"value":"0"}])");fixture.Pin(clear,"Q",0,true);
    const auto ram=fixture.Cell("ram","RAMB18E2","{\"bram18k\":1}");fixture.Pin(ram,"WEA",0);fixture.Pin(ram,"WEA",1);
    const auto distinct=fixture.Cell("distinct","RAMB18E2","{\"bram18k\":1}");fixture.Pin(distinct,"WEA",0);fixture.Pin(distinct,"WEA",2);
    const auto hard=fixture.Cell("hard","RAMB36E2","{\"bram18k\":2}");for(I i=0;i<4;++i)fixture.Pin(hard,"ADDRARDADDR",i);fixture.Pin(hard,"DOUTBDOUT",3,true);
    const auto dual=fixture.Cell("dual","LUT6_2","{\"lut\":1}");fixture.Pin(dual,"I0");fixture.Pin(dual,"O5",0,true);fixture.Pin(dual,"O6",0,true);
    const auto buffer=fixture.Cell("buffer","BUF");fixture.Pin(buffer,"I");fixture.Pin(buffer,"O",0,true);
    const std::string escaped=u8"block\\子";
    const auto escaped_ff=fixture.Cell(escaped,"FDRE","{\"ff\":1}");fixture.Pin(escaped_ff,"D");fixture.Pin(escaped_ff,"Q",0,true);
    const auto generic_ff=fixture.Cell("neg","$_DFF_N_","{\"ff\":1}");fixture.Pin(generic_ff,"C");fixture.Pin(generic_ff,"D");fixture.Pin(generic_ff,"Q",0,true);
    const auto zero=fixture.Cell("zero","GND");fixture.Pin(zero,"G",0,true);
    const auto tied=fixture.Cell("tied","LUT1","{\"lut\":1}","{}",R"([{"port":"I0","bit":0,"value":"0"}])");fixture.Pin(tied,"O",0,true);
    // A legal typed owner's key need not be less than its row population.
    // Existing FF pin/control assertions below resolve this exact sparse key.
    fixture.SparseCell(ff,1000001);
    fixture.Finish();
    auto source=ArtifactRef::Bind(path.string(),"emuir-compact-v2");
    auto library=ArtifactRef::Bind(library_path.string(),"xilinx-primitive-library-v1");
    PhysicalQueryObjects xilinx(source,"xilinx",&library),vivado(source,"vivado",&library);
    Object(xilinx,{std::nullopt,"in_bus",3,std::nullopt},"port","top:in_bus[3]");
    Object(vivado,{std::nullopt,"out",0,std::nullopt},"port","out");
    Object(xilinx,{"lut","A",1,std::nullopt},"pin","cell:lut/I1");
    Object(xilinx,{"lut","Y",0,std::nullopt},"pin","cell:lut/O");
    Object(vivado,{"lut","A",1,std::nullopt},"pin","lut/I1");
    Object(vivado,{"ff","CE",0,std::nullopt},"pin","ff/CE");
    Object(vivado,{"ff","R",0,std::nullopt},"pin","ff/R");
    Object(vivado,{"clear","CLR",0,std::nullopt},"pin","clear/CLR");
    Object(xilinx,{"ram","WEA",2,std::nullopt},"pin","cell:ram/WEA[0]");
    Object(vivado,{"ram","WEA",3,std::nullopt},"pin","ram/WEA[1]");
    Object(xilinx,{"distinct","WEA",2,std::nullopt},"pin","cell:distinct/WEA[2]");
    Object(vivado,{"hard","ADDRARDADDR",3,std::nullopt},"pin","hard/ADDRARDADDR[3]");
    Object(vivado,{"hard","DOUTBDOUT",3,"hard/memory_reg_bram_0/CLKBWRCLK"},"pin","hard/memory_reg_bram_0/CLKBWRCLK");
    Object(vivado,{"dual","O5",0,std::nullopt},"pin","dual/O5");
    Object(xilinx,{"dual","O6",0,std::nullopt},"pin","cell:dual/O6");
    Object(xilinx,{"buffer","O",0,std::nullopt},"pin","cell:buffer/O");
    Reject([&]{vivado.Resolve({"buffer","O",0,std::nullopt});});
    Object(vivado,{escaped,"D",0,std::nullopt},"pin",u8"block\\\\子/D");
    Object(vivado,{"neg","C",0,std::nullopt},"pin","neg/C");
    Object(xilinx,{"zero","G",0,std::nullopt},"pin","cell:zero/G");
    Object(vivado,{"tied","I0",0,std::nullopt},"pin","tied/I0");
    Check(xilinx.IsArchitecturalState("ff") && xilinx.IsArchitecturalState("ram") && !xilinx.IsArchitecturalState("zero"),"state/constant resource semantics changed");
    Check(xilinx.CellType("dual")=="LUT6_2","cell type projection changed");
    Check(xilinx.Normalize({"remote","WEA",2,std::nullopt}).bit==2,"Normalize requires a remote endpoint to be local");
    Reject([&]{xilinx.Resolve({"remote","WEA",2,std::nullopt});});
    Reject([&]{xilinx.Resolve({std::nullopt,"in_bus",4,std::nullopt});});
    Reject([&]{xilinx.Resolve({"ff","D",-1,std::nullopt});});
    Reject([&]{vivado.Resolve({"ff","CE",1,std::nullopt});});
    Reject([&]{PhysicalQueryObjects missing(source,"vivado");});
    Reject([&]{PhysicalQueryObjects bad(source,"other",&library);});
    // Sparse positive keys are legal; negative keys remain invalid.
    const auto invalid_path=scratch.root/"negative-key.sqlite";
    Fixture invalid(invalid_path);const auto invalid_cell=invalid.Cell("negative","LUT1");
    invalid.Pin(invalid_cell,"O",0,true);invalid.SparseCell(invalid_cell,-1);invalid.Finish();
    auto invalid_source=ArtifactRef::Bind(invalid_path.string(),"emuir-compact-v2");
    PhysicalQueryObjects invalid_objects(invalid_source,"xilinx");
    Reject([&]{invalid_objects.Resolve({"negative","O",0,std::nullopt});});
    xilinx.RequireUnchanged();vivado.RequireUnchanged();
    // Same catalog contents at a different inode must fail an original lease.
    const auto replacement=scratch.root/"replacement.json";fs::copy_file(library_path,replacement);fs::rename(replacement,library_path);
    Reject([&]{vivado.RequireUnchanged();});
    PhysicalQueryObjects source_only(source,"xilinx");
    const auto replacement_ir=scratch.root/"replacement.sqlite";fs::copy_file(path,replacement_ir);fs::rename(replacement_ir,path);
    Reject([&]{source_only.RequireUnchanged();});
    std::cout<<"physical query objects: golden naming, constants, state, control, aliases, missing pins and source bindings pass\n";
    return 0;
  } catch(const std::exception& error) {
    std::cerr<<error.what()<<'\n';return 1;
  }
}
