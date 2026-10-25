#include "emuflow_core/global_events.hpp"
#include "emuflow_core/global_event_schema.hpp"
#include "emuflow_core/store.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/json_record.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <locale>
#include <optional>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
using Object=std::map<std::string,JsonRecord>;
using Array=std::vector<JsonRecord>;
using Integer=std::int64_t;
void Check(bool value,const std::string& message) {
  if(!value) throw StageError(ErrorCode::kInvalidArtifact,"global STA "+message);
}
std::string Text(SqliteStatement& row,int column,bool empty=false) {
  auto value=row.TypedText(column);Check(empty || !value.empty(),"has an empty identity");
  (void)JsonQuote(value);return value;
}
Integer Number(SqliteStatement& row,int column) {
  const auto value=row.TypedInteger(column);Check(value>=0,"has a negative identity/count");return value;
}
void Hash(Sha256& hash,std::string_view value) {
  hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());
}
void HashInteger(Sha256& hash,std::uint64_t value) {
  unsigned char bytes[8];for(unsigned i=0;i<8;++i){bytes[i]=value&255;value>>=8;}
  hash.update(bytes,8);
}
std::string Hex(Sha256& hash) {
  std::string value;const char* digits="0123456789abcdef";
  for(auto byte:hash.finish()){value+=digits[byte>>4];value+=digits[byte&15];}return value;
}
std::string Metadata(SqliteDatabase& db,const std::string& key) {
  auto row=db.Prepare("SELECT value FROM metadata WHERE key=?");row.BindText(1,key);
  Check(row.Step(),"metadata is missing: "+key);auto value=row.TypedText(0);
  Check(!row.Step(),"metadata repeats: "+key);return value;
}
Array Generation(const FileGeneration& g) {
  Array value;
  for(auto n:{g.device,g.inode,g.bytes})value.emplace_back(std::to_string(n));
  for(auto n:{g.modified_seconds,g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds})value.emplace_back(std::to_string(n));
  return value;
}
void PutMetadata(SqliteDatabase& db,const char* key,const JsonRecord& value) {
  auto row=db.Prepare("INSERT INTO metadata VALUES(?,?)");row.BindText(1,key);row.BindText(2,value.Encode());row.Execute();
}
void ExactDDL(SqliteDatabase& db,const char* ddl) {
  SqliteDatabase expected(":memory:",SqliteDatabase::Mode::kCreateExclusive);expected.Execute(ddl);
  const char* sql="SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name";
  auto observed=db.Prepare(sql),reference=expected.Prepare(sql);
  const auto normalize=[](const std::string& source) {
    std::istringstream input(source);std::string result,token;
    while(input>>token){if(!result.empty())result+=' ';result+=token;}return result;
  };
  while(reference.Step()) {
    Check(observed.Step(),"store has missing schema objects");
    for(int i=0;i<4;++i)Check(normalize(observed.TypedText(i))==normalize(reference.TypedText(i)),"store schema objects disagree");
  }
  Check(!observed.Step(),"store has unexpected schema objects");
}
struct Numbers {
  std::string bytes;
  double At(std::size_t index) const {double value;std::memcpy(&value,bytes.data()+index*8,8);return value;}
  std::size_t size() const {return bytes.size()/8;}
};
Numbers ReadNumbers(SqliteStatement& row,int column) {
  Numbers result{row.Blob(column)};
  Check(result.bytes.size()>=16 && result.bytes.size()%8==0,"check numeric payload is malformed");
  for(std::size_t i=0;i<result.size();++i)
    Check(std::isfinite(result.At(i)) && (i==1 || result.At(i)>=0),"launch/arc delay must be nonnegative and all times finite");
  return result;
}
bool PhysicalEqual(const Numbers& a,const Numbers& b) {
  if(a.size()!=b.size() || a.At(0)!=b.At(0))return false;
  for(std::size_t i=2;i<a.size();++i)if(a.At(i)!=b.At(i))return false;
  return true;
}
bool NumericalEqual(const Numbers& a,const Numbers& b) {
  return a.size()==b.size() && a.At(1)==b.At(1) && PhysicalEqual(a,b);
}
void NumericalHash(Sha256& hash,Integer role,const Numbers& values) {
  Hash(hash,std::to_string(role)+":"+std::to_string(values.size())+":");
  for(std::size_t i=0;i<values.size();++i) {
    // Numerical bundle equality follows IEEE equality, including signed zero.
    const double value=values.At(i)==0 ? 0.0:values.At(i);
    hash.update(reinterpret_cast<const unsigned char*>(&value),8);
  }
}
bool BundlesEqual(SqliteDatabase& primary,Integer left,Integer right,Integer count) {
  auto a=primary.Prepare("SELECT role,numbers FROM checks WHERE seq>=? AND seq<? ORDER BY seq");
  auto b=primary.Prepare("SELECT role,numbers FROM checks WHERE seq>=? AND seq<? ORDER BY seq");
  a.BindInteger(1,left);a.BindInteger(2,left+count);b.BindInteger(1,right);b.BindInteger(2,right+count);
  for(Integer i=0;i<count;++i) {
    Check(a.Step() && b.Step(),"representative bundle is incomplete");
    if(a.TypedInteger(0)!=b.TypedInteger(0) || !NumericalEqual(ReadNumbers(a,1),ReadNumbers(b,1)))return false;
  }
  Check(!a.Step() && !b.Step(),"representative bundle has extra rows");return true;
}
}  // namespace

void IndexGlobalEvents(const ArtifactRef& input,const std::string& output) {
  SqliteStore primary(input);auto& source=primary.database();ExactDDL(source,kGlobalEventsDDL);
  const auto header_text=Metadata(source,"header");
  Check(header_text.size()<8192,"primary header exceeds bound");
  auto fields=source.Prepare("SELECT count(*),count(DISTINCT key),sum(key IN ('schema','mode','paths','checks','events')) FROM json_each(?)");fields.BindText(1,header_text);
  Check(fields.Step() && fields.TypedInteger(0)==5 && fields.TypedInteger(1)==5 && fields.TypedInteger(2)==5,"primary header fields disagree");
  auto header=source.Prepare("SELECT json_type(?1,'$.mode'),json_extract(?1,'$.mode'),json_type(?1,'$.paths'),"
      "json_extract(?1,'$.paths'),json_type(?1,'$.checks'),json_extract(?1,'$.checks'),"
      "json_type(?1,'$.events'),json_extract(?1,'$.events')");header.BindText(1,header_text);
  Check(header.Step() && header.Text(0)=="integer" && header.Text(2)=="integer" &&
        header.Text(4)=="integer" && header.Text(6)=="integer","primary header counts are malformed");
  const auto mode=Number(header,1),paths=Number(header,3),checks=Number(header,5),events=Number(header,7);
  Check(mode>=1 && mode<=3 && static_cast<std::uint64_t>(paths)<=input.generation().bytes &&
      static_cast<std::uint64_t>(checks)<=input.generation().bytes/16 &&
      static_cast<std::uint64_t>(events)<=input.generation().bytes,"primary coverage exceeds source bounds");
  Check((mode&1) ? checks>0 && events>0:checks==0 && events==0,"has no timing checks or wrong metadata-only mode");
  Sha256 logical,path_ids;Hash(logical,"emuflow.global-event-checks-logical-binary/v1\n");
  for(auto value:{mode,paths,checks,events})HashInteger(logical,value);
  SqliteArtifactWriter writer(output,"global-event-index-v1",{input});auto& index=writer.database();index.Execute(kGlobalEventIndexDDL);
  index.Execute("PRAGMA cache_size=-8192; CREATE TEMP TABLE bundles(digest TEXT NOT NULL,count INTEGER NOT NULL,first_check INTEGER NOT NULL,engine_first INTEGER NOT NULL,PRIMARY KEY(digest,first_check)) WITHOUT ROWID");
  SqliteTransaction transaction(index);
  auto order_append=index.Prepare("INSERT INTO path_order VALUES(?,?)");
  auto path_rows=source.Prepare("SELECT seq,path,representative_path,path_scope,clock_domain,target_period_ns,cone_bound,interconnect_delay_model,typeof(target_period_ns) FROM paths ORDER BY path");
  Integer observed=0;
  Hash(path_ids,"[");
  while(path_rows.Step()) {
    const auto seq=Number(path_rows,0);Check(seq<paths,"path identity exceeds declared scope");
    order_append.BindInteger(1,observed);order_append.BindInteger(2,seq);order_append.Execute();
    if(observed++)Hash(path_ids,",");Hash(path_ids,JsonQuoteUtf8(Text(path_rows,1)));
    Object record{{"seq",seq},{"path",Text(path_rows,1)}};
    if(mode&2) {
      const auto scope=Number(path_rows,3),cone=Number(path_rows,6);const auto period=path_rows.Real(5);
      Check(scope<=1 && cone<=1 && (path_rows.Text(8)=="real" || path_rows.Text(8)=="integer") &&
          std::isfinite(period) && period>0,"path metadata period/scope is invalid");
      record.emplace("representative_path",Text(path_rows,2));record.emplace("path_scope",scope);
      record.emplace("target_period_ns",period);record.emplace("cone_bound",cone);
      for(auto field:std::array<std::pair<int,const char*>,2>{{{4,"clock_domain"},{7,"interconnect_delay_model"}}})
        record.emplace(field.second,path_rows.IsNull(field.first)?JsonRecord():JsonRecord(Text(path_rows,field.first)));
    } else for(int i=2;i<=7;++i)Check(path_rows.IsNull(i),"check-only store contains undeclared path metadata");
    Hash(logical,JsonRecord(std::move(record)).Encode()+"\n");
  }
  Check(observed==paths,"path coverage disagrees");Hash(path_ids,"]");
  auto event_rows=source.Prepare("SELECT seq,event FROM events ORDER BY seq");observed=0;
  while(event_rows.Step()){Check(Number(event_rows,0)==observed++,"event identities are not contiguous");Hash(logical,JsonRecord(Array{observed-1,Text(event_rows,1)}).Encode()+"\n");}
  Check(observed==events,"event identity coverage disagrees");
  auto append=index.Prepare("INSERT INTO path_index VALUES(?,?,?,?,?)");
  Integer current=-1,begin=0,count=0,engine_checks=0,bundle_count=0;std::uint64_t numeric_bytes=0;
  Sha256 bundle;std::optional<Numbers> target,runtime;
  const auto finish=[&]() {
    if(current<0)return;
    Check(target.has_value() && runtime.has_value(),"target/runtime population disagrees");
    Check(PhysicalEqual(*target,*runtime),"target/runtime physical chains disagree");
    const auto digest=Hex(bundle);Integer representative=begin,engine=engine_checks;bool found=false;
    auto candidates=index.Prepare("SELECT first_check,engine_first FROM bundles WHERE digest=? AND count=? ORDER BY engine_first");
    candidates.BindText(1,digest);candidates.BindInteger(2,count);
    while(candidates.Step()) if(BundlesEqual(source,candidates.TypedInteger(0),begin,count)) {
      representative=candidates.TypedInteger(0);engine=candidates.TypedInteger(1);found=true;break;
    }
    if(!found) {
      auto record=index.Prepare("INSERT INTO bundles VALUES(?,?,?,?)");record.BindText(1,digest);record.BindInteger(2,count);
      record.BindInteger(3,begin);record.BindInteger(4,engine);record.Execute();engine_checks+=count;++bundle_count;
    }
    auto repeated=index.Prepare("SELECT 1 FROM path_index WHERE path_seq=?");repeated.BindInteger(1,current);
    Check(!repeated.Step(),"requires contiguous original-path bundles");
    append.BindInteger(1,current);append.BindInteger(2,begin);append.BindInteger(3,count);append.BindInteger(4,representative);append.BindInteger(5,engine);append.Execute();
    bundle=Sha256();target.reset();runtime.reset();
  };
  auto rows=source.Prepare("SELECT c.seq,c.path_seq,c.role,c.event_seq,typeof(c.numbers),c.numbers,p.seq,e.seq FROM checks c LEFT JOIN paths p ON p.seq=c.path_seq LEFT JOIN events e ON e.seq=c.event_seq ORDER BY c.seq");
  observed=0;std::set<std::pair<Integer,Integer>> identities;
  while(rows.Step()) {
    Check(Number(rows,0)==observed && !rows.IsNull(6) && !rows.IsNull(7),"check identity/population is invalid");
    const auto path=Number(rows,1),role=Number(rows,2),event=Number(rows,3);Check(role<=3,"check has an unsupported role");
    if(path!=current){finish();current=path;begin=observed;count=0;identities.clear();}
    Check(identities.emplace(role,event).second,"has duplicate check identities");
    Check(rows.TypedText(4)=="blob","check numeric payload is not a blob");auto numbers=ReadNumbers(rows,5);numeric_bytes+=numbers.bytes.size();
    if(role<2) {
      auto& destination=role==0?target:runtime;Check(!destination.has_value(),"must observe each original path once");destination=numbers;
    }
    NumericalHash(bundle,role,numbers);
    for(auto value:{observed,path,role,event})HashInteger(logical,value);
    HashInteger(logical,numbers.bytes.size());Hash(logical,numbers.bytes);
    ++observed;++count;
  }
  finish();Check(observed==checks,"check row coverage disagrees");
  if(mode&1) {auto total=index.Prepare("SELECT count(*) FROM path_index");Check(total.Step() && total.TypedInteger(0)==paths,"requires contiguous original-path bundles with complete path coverage");}
  else for(Integer i=0;i<paths;++i){append.BindInteger(1,i);for(int col=2;col<=5;++col)append.BindInteger(col,0);append.Execute();}
  const auto output_header=JsonRecord(Object{{"schema","emuflow.global-event-index/v1"},{"implementation_id","native-global-event-index-v1"},
      {"source_path",input.path()},{"source_generation",Generation(input.generation())},{"mode",mode},{"paths",paths},{"checks",checks},
      {"events",events},{"representative_checks",engine_checks},{"representative_bundles",bundle_count},
      {"numeric_bytes",numeric_bytes},{"logical_sha256",Hex(logical)},{"path_ids_sha256",Hex(path_ids)}});
  PutMetadata(index,"header",output_header);transaction.Commit();
  append=SqliteStatement{};order_append=SqliteStatement{};writer.Commit();
}
}  // namespace emuflow::core

namespace emuflow::core {
namespace {
std::string JsonText(SqliteDatabase& db,const std::string& object,const char* path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="text","index text metadata is invalid");return Text(row,1);
}
Integer JsonNumber(SqliteDatabase& db,const std::string& object,const char* path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && row.Text(0)=="integer","index count metadata is invalid");return Number(row,1);
}
void RequireIndexSource(const ArtifactRef& primary,SqliteDatabase& index) {
  ExactDDL(index,kGlobalEventIndexDDL);const auto header=Metadata(index,"header");
  Check(header.size()<8192 && JsonText(index,header,"$.source_path")==primary.path(),"index belongs to another event owner");
  auto generation=index.Prepare("SELECT json_extract(?1,'$.source_generation')");generation.BindText(1,header);
  Check(generation.Step() && generation.TypedText(0)==JsonRecord(Generation(primary.generation())).Encode(),"event owner generation changed");
}
void ExpectedGeneration(CheckedRequestReader& reader,const ArtifactRef& input) {
  FileGeneration expected;
  expected.device=reader.Read<std::uint64_t>();expected.inode=reader.Read<std::uint64_t>();expected.bytes=reader.Read<std::uint64_t>();
  expected.modified_seconds=reader.Read<std::int64_t>();expected.modified_nanoseconds=reader.Read<std::int64_t>();
  expected.changed_seconds=reader.Read<std::int64_t>();expected.changed_nanoseconds=reader.Read<std::int64_t>();
  if(!(expected==input.generation()))throw StageError(ErrorCode::kMixedGeneration,"global STA original owner changed");
}
class Selection {
 public:
  Selection(SqliteDatabase& primary,SqliteDatabase& index,bool representatives):primary_(primary),index_(index),representatives_(representatives) {}
  void Visit(Integer expected,const std::function<void(Integer,const Numbers&)>& visit) {
    Integer count=0;
    if(representatives_) {
      auto spans=index_.Prepare("SELECT first_check,check_count,engine_first_check FROM path_index WHERE first_check=representative_first_check ORDER BY engine_first_check");
      auto rows=primary_.Prepare("SELECT seq,numbers FROM checks WHERE seq>=? AND seq<? ORDER BY seq");
      while(spans.Step()) {
        const auto first=Number(spans,0),size=Number(spans,1);Check(Number(spans,2)==count && size>0,"representative span coverage is invalid");
        rows.BindInteger(1,first);rows.BindInteger(2,first+size);Integer offset=0;
        while(rows.Step()){Check(Number(rows,0)==first+offset++,"representative row order is invalid");visit(count++,ReadNumbers(rows,1));}
        Check(offset==size,"representative span is incomplete");rows.Reset();
      }
    } else {
      auto rows=primary_.Prepare("SELECT seq,numbers FROM checks ORDER BY seq");
      while(rows.Step()){Check(Number(rows,0)==count,"original check order is invalid");visit(count++,ReadNumbers(rows,1));}
    }
    Check(count==expected,"selected check coverage disagrees");
  }
 private:
  SqliteDatabase& primary_;SqliteDatabase& index_;bool representatives_;
};
class TextOutput {
 public:
  TextOutput(const std::filesystem::path& path,const std::vector<ArtifactRef>& inputs):writer_(path.string(),inputs),path_(path.string()),name_(path.filename().string()) {}
  void Put(std::string_view value) {writer_.WriteBytes(value.data(),value.size());Hash(hash_,value);bytes_+=value.size();}
  JsonRecord Finish() {
    writer_.Commit();return Object{{"name",name_},{"bytes",bytes_},{"sha256",Hex(hash_)},
        {"generation",Generation(ArtifactRef::Bind(path_,"global-event-model-text-v1").generation())}};
  }
 private:
  AtomicBinaryWriter writer_;std::string path_,name_;Sha256 hash_;std::uint64_t bytes_=0;
};
std::string Float(double value,unsigned precision) {
  Check(std::isfinite(value),"exported model time overflows");std::ostringstream stream;stream.imbue(std::locale::classic());
  stream<<std::setprecision(precision)<<std::defaultfloat<<value;return stream.str();
}
void LibertyCell(TextOutput& output,std::size_t id,double delay) {
  output.Put("  cell (D"+std::to_string(id)+") {\n    area : 1.0;\n    pin (A) {\n      direction : input;\n      capacitance : 0.001;\n    }\n    pin (Y) {\n      direction : output;\n      timing () {\n        related_pin : \"A\";\n        timing_sense : non_unate;\n");
  for(const auto& table:std::array<std::pair<const char*,double>,4>{{{"cell_rise",delay},{"cell_fall",delay},{"rise_transition",0.01},{"fall_transition",0.01}}})
    output.Put("        "+std::string(table.first)+" (scalar) {\n          values (\""+Float(table.second,12)+"\");\n        }\n");
  output.Put("      }\n    }\n  }\n\n");
}
void LexicalCells(TextOutput& output,const std::vector<double>& delays,std::size_t id) {
  if(id>=delays.size())return;LibertyCell(output,id,delays[id]);
  // Decimal-trie traversal matches sorted D0,D1,D10,... without allocating
  // another population of names or sort keys.
  if(id && id<=(delays.size()-1)/10)
    for(std::size_t digit=0;digit<10;++digit)LexicalCells(output,delays,id*10+digit);
}
}  // namespace
void ExportGlobalEvents(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()==3,"export input coverage disagrees");
  CheckedRequestReader request(inputs[2].path(),4096);
  Check(request.Read<std::uint64_t>()==0x31584547464d5545ULL && request.Read<std::uint32_t>()==1,"export request version is invalid");
  const auto mode=request.Read<std::uint32_t>();Check(mode<=1,"export selection mode is invalid");
  ExpectedGeneration(request,inputs[0]);ExpectedGeneration(request,inputs[1]);request.RequireEnd();
  SqliteStore primary(inputs[0]),index(inputs[1]);RequireIndexSource(inputs[0],index.database());
  const auto header=Metadata(index.database(),"header");
  Check(JsonNumber(index.database(),header,"$.mode")&1,"metadata-only owner has no timing checks");
  const auto count=JsonNumber(index.database(),header,mode?"$.representative_checks":"$.checks");
  Check(count>0 && static_cast<std::uint64_t>(count)<=inputs[0].generation().bytes/16,"export check count exceeds source bounds");
  Selection rows(primary.database(),index.database(),mode!=0);
  std::set<double> unique{0.0};
  rows.Visit(count,[&](Integer,const Numbers& row){for(std::size_t i=2;i<row.size();++i)unique.insert(row.At(i));});
  std::vector<double> delays(unique.begin(),unique.end());unique.clear();
  namespace fs=std::filesystem;StagedArtifact stage(output);
  const auto temporary=stage.path()+".model";
  const auto final=(fs::path(output).parent_path()/("model-"+fs::path(stage.path()).filename().string())).string();
  struct Directory {
    std::string path;bool committed=false;
    ~Directory(){if(!committed){std::error_code error;fs::remove_all(path,error);}}
  } directory{temporary};
  Check(fs::create_directory(temporary),"cannot create model generation");Array files;
  {
    TextOutput liberty(fs::path(temporary)/"global_timing.lib",inputs);
    liberty.Put("library (emuflow_event_arcs) {\n  delay_model : \"table_lookup\";\n  time_unit : \"1ns\";\n  voltage_unit : \"1V\";\n  current_unit : \"1mA\";\n  leakage_power_unit : \"1nW\";\n  pulling_resistance_unit : \"1kohm\";\n  capacitive_load_unit (1,pF);\n  input_threshold_pct_rise : 50;\n  input_threshold_pct_fall : 50;\n  output_threshold_pct_rise : 50;\n  output_threshold_pct_fall : 50;\n  slew_lower_threshold_pct_rise : 20;\n  slew_lower_threshold_pct_fall : 20;\n  slew_upper_threshold_pct_rise : 80;\n  slew_upper_threshold_pct_fall : 80;\n\n");
    LexicalCells(liberty,delays,0);for(std::size_t digit=1;digit<10;++digit)LexicalCells(liberty,delays,digit);
    liberty.Put("}\n");files.push_back(liberty.Finish());
  }
  {
    TextOutput verilog(fs::path(temporary)/"global_timing.v",inputs),sdc(fs::path(temporary)/"global_timing.sdc",inputs);
    verilog.Put("module global_timing(\n");
    for(Integer i=0;i<count;++i){if(i)verilog.Put(",\n");verilog.Put("i"+std::to_string(i)+", o"+std::to_string(i));}
    verilog.Put(");\n");
    for(Integer i=0;i<count;++i)verilog.Put("input i"+std::to_string(i)+";\noutput o"+std::to_string(i)+";\n");
    sdc.Put("create_clock -name epoch -period 1\n");
    rows.Visit(count,[&](Integer i,const Numbers& row){
      const auto ordinal=std::to_string(i);auto previous="i"+ordinal;const auto length=std::max<std::size_t>(1,row.size()-2);
      for(std::size_t j=0;j<length;++j) {
        const double value=row.size()==2?0.0:row.At(j+2);
        const auto found=std::lower_bound(delays.begin(),delays.end(),value);Check(found!=delays.end() && *found==value,"model delay dictionary is incomplete");
        const auto net=j+1==length?"o"+ordinal:"n"+ordinal+"_"+std::to_string(j);
        if(j+1!=length)verilog.Put("wire "+net+";\n");
        verilog.Put("D"+std::to_string(found-delays.begin())+" a"+ordinal+"_"+std::to_string(j)+" (.A("+previous+"), .Y("+net+"));\n");previous=net;
      }
      const double deadline=1.0-(row.At(1)-row.At(0));
      sdc.Put("set_input_delay -clock epoch -max 0 [get_ports i"+ordinal+"]\nset_input_transition 0 [get_ports i"+ordinal+"]\nset_output_delay -clock epoch -max "+Float(deadline,17)+" [get_ports o"+ordinal+"]\n");
    });
    verilog.Put("endmodule\n");files.push_back(verilog.Finish());files.push_back(sdc.Finish());
  }
  {
    std::string program=kGlobalEventAnalyzeTcl;const auto token=program.find("@CHECK_COUNT@");Check(token!=std::string::npos,"model query template is malformed");
    program.replace(token,13,std::to_string(count));TextOutput tcl(fs::path(temporary)/"analyze.tcl",inputs);tcl.Put(program);files.push_back(tcl.Finish());
  }
  for(const auto& input:inputs)input.RequireUnchanged();
  fs::rename(temporary,final);directory.path=final;
  const auto manifest=JsonRecord(Object{{"schema","emuflow.global-event-export/v1"},{"status","pass"},
      {"implementation_id","native-global-event-export-v1"},{"directory",fs::absolute(final).string()},
      {"checks",count},{"delay_cells",delays.size()},{"logical_sha256",JsonText(index.database(),header,"$.logical_sha256")},{"files",std::move(files)}}).Encode();
  PublishTextArtifact(output,inputs,[&](std::ostream& stream){stream<<manifest<<'\n';});directory.committed=true;
}
}  // namespace emuflow::core
