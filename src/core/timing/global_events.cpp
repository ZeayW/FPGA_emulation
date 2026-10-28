#include "timing/global_events.hpp"
#include "common/exact_sum.hpp"
#include "storage/schema/global_event_schema.hpp"
#include "storage/store.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include "scheduling/schedule_timing.hpp"
#include "physical/physical_evidence.hpp"
#include <deque>
#include <tuple>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <locale>
#include <limits>
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
  SqliteStore primary(input);auto& source=primary.database();RequireSqliteDDL(source,kGlobalEventsDDL);
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
      const auto scope=Number(path_rows,3),cone=Number(path_rows,6);const auto period=path_rows.TypedReal(5);
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
  RequireSqliteDDL(index,kGlobalEventIndexDDL);const auto header=Metadata(index,"header");
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

namespace emuflow::core {
namespace {
using Scalars=std::array<double,3>;
void AttachFacts(SqliteDatabase& db,const std::vector<ArtifactRef>& inputs) {
  for(const auto& entry:std::array<std::pair<int,const char*>,2>{{{0,"facts"},{1,"idx"}}}) {
    auto attach=db.Prepare("ATTACH DATABASE ? AS "+std::string(entry.second));
    attach.BindText(1,SqliteImmutableUri(inputs[entry.first].path()));attach.Execute();
  }
  db.Execute("PRAGMA cache_size=-8192; PRAGMA facts.cache_size=-8192; PRAGMA idx.cache_size=-8192; PRAGMA temp_store=FILE");
}
std::string IndexHeader(const std::vector<ArtifactRef>& inputs) {
  SqliteStore index(inputs[1]);RequireIndexSource(inputs[0],index.database());return Metadata(index.database(),"header");
}
Scalars ReadScalars(const std::string& bytes) {
  Check(bytes.size()==24,"measurement payload is malformed");Scalars result;std::memcpy(result.data(),bytes.data(),24);
  for(auto value:result)Check(std::isfinite(value),"measurement is nonfinite");return result;
}
std::string ScalarBytes(const Scalars& numbers) {return std::string(reinterpret_cast<const char*>(numbers.data()),24);}

void VerifyScalars(const Numbers& source,const Scalars& actual) {
  ExactSum sum;for(std::size_t i=2;i<source.size();++i)sum.Add(source.At(i));
  const double arrival=sum.Finish(),required=source.At(1)-source.At(0);
  const Scalars expected{arrival,required,required-arrival};
  for(std::size_t i=0;i<3;++i)Check(std::isfinite(expected[i]) &&
      std::abs(actual[i]-expected[i])<=std::max(1e-3,4*std::ldexp(1.0,-23)*std::abs(expected[i])),
      "event binding disagrees with engine measurement");
}
class TsvInput {
 public:
  explicit TsvInput(const std::string& path):stream_(path,std::ios::binary) {
    Check(bool(stream_),"cannot read engine measurements");std::string header;Check(Line(header) && header=="endpoint\tarrival_ns\trequired_ns\tslack_ns","invalid measurements header");
  }
  Scalars Take(Integer index) {
    std::string line;Check(Line(line),"missing/unconstrained engine endpoints");
    const auto split=line.find('\t');Check(split!=std::string::npos && line.substr(0,split)=="o"+std::to_string(index),"duplicate/malformed engine endpoint");
    Scalars values;std::size_t begin=split+1;
    for(int column=0;column<3;++column) {
      const auto end=line.find('\t',begin);Check((end==std::string::npos)==(column==2),"malformed engine measurement columns");
      const auto text=line.substr(begin,end==std::string::npos?end:end-begin);Check(!text.empty(),"empty engine measurement");
      values[column]=ParseFiniteDecimal(text,"engine measurement");begin=end+1;
    }
    return values;
  }
  std::string Finish() {std::string extra;Check(!Line(extra),"extra engine endpoints");return Hex(hash_);}
 private:
  bool Line(std::string& line) {
    char buffer[4097];stream_.getline(buffer,sizeof(buffer));const auto size=stream_.gcount();
    if(!size && stream_.eof())return false;
    Check(!stream_.bad() && (stream_.good() || stream_.eof()),"oversized/unreadable engine measurement line");
    line.assign(buffer,size-(stream_.eof()?0:1));Check(line.find('\0')==std::string::npos,"NUL in engine measurements");
    Hash(hash_,line);if(!stream_.eof())Hash(hash_,"\n");if(!line.empty() && line.back()=='\r')line.pop_back();return true;
  }
  std::ifstream stream_;Sha256 hash_;
};
struct Metric {
  double wns=std::numeric_limits<double>::infinity();std::string worst;Integer negative=0;ExactSum sum;
  void Add(const std::string& path,double slack) {
    if(slack<wns || (slack==wns && path<worst)){wns=slack;worst=path;}
    if(slack<0){++negative;sum.Add(slack);}
  }
  JsonRecord Record() const {return Object{{"wns_ns",wns},{"worst_path",worst},{"negative_paths",negative},{"original_path_tns_ns",sum.Finish()}};}
};
struct Reduction {
  Integer paths=0,checks=0,local=0,bounds=0,failures=0;double maximum=0;Metric target,runtime;std::string measurement_sha;
  JsonRecord Record() const {return Object{{"paths",paths},{"checks",checks},{"local_paths",local},{"bounds_paths",bounds},{"event_failures",failures},
      {"maximum_delay_ns",maximum},{"target",target.Record()},{"runtime",runtime.Record()},{"measurements_sha256",measurement_sha}};}
};
using PathConsumer=std::function<void(Integer,SqliteStatement&,const Scalars&,const Scalars&)>;
Reduction ReduceMeasurements(SqliteDatabase& db,SqliteDatabase& index,const std::string& header,bool verify,const PathConsumer& consume={}) {
  const auto expected_checks=JsonNumber(index,header,"$.checks"),expected_paths=JsonNumber(index,header,"$.paths"),expected_reps=JsonNumber(index,header,"$.representative_checks");
  Sha256 digest;Hash(digest,"emuflow.global-measurements-logical-binary/v1\n");Hash(digest,JsonText(index,header,"$.logical_sha256"));
  auto values=db.Prepare("SELECT seq,typeof(numbers),numbers FROM measurements ORDER BY seq");Integer seen=0;
  while(values.Step()) {Check(Number(values,0)==seen++ && values.Text(1)=="blob","measurement sequence/type is invalid");const auto bytes=values.Blob(2);(void)ReadScalars(bytes);HashInteger(digest,seen-1);Hash(digest,bytes);}
  Check(seen==expected_reps,"representative measurement coverage disagrees");
  Reduction result;result.measurement_sha=Hex(digest);
  auto paths=db.Prepare("SELECT p.seq,p.path,p.representative_path,p.path_scope,p.clock_domain,p.target_period_ns,p.cone_bound,p.interconnect_delay_model,i.first_check,i.check_count,i.representative_first_check,i.engine_first_check FROM facts.paths p JOIN idx.path_index i ON i.path_seq=p.seq ORDER BY p.path");
  auto representative=db.Prepare("SELECT i.check_count,i.engine_first_check FROM facts.checks c JOIN idx.path_index i ON i.path_seq=c.path_seq WHERE c.seq=? AND i.first_check=c.seq AND i.representative_first_check=c.seq");
  auto checks=db.Prepare("SELECT c.seq,c.role,c.numbers,r.role,r.numbers,m.numbers FROM facts.checks c JOIN facts.checks r ON r.seq=?3+c.seq-?1 JOIN measurements m ON m.seq=?4+c.seq-?1 WHERE c.seq>=?1 AND c.seq<?2 ORDER BY c.seq");
  while(paths.Step()) {
    const auto path=Text(paths,1);const auto first=Number(paths,8),count=Number(paths,9),rep=Number(paths,10),engine=Number(paths,11);
    Check(count>0,"empty original-path measurement bundle");representative.BindInteger(1,rep);
    Check(representative.Step() && Number(representative,0)==count && Number(representative,1)==engine && !representative.Step(),"representative whole-bundle span disagrees");representative.Reset();
    checks.BindInteger(1,first);checks.BindInteger(2,first+count);checks.BindInteger(3,rep);checks.BindInteger(4,engine);
    std::optional<Scalars> target,runtime;Integer ordinal=0;
    while(checks.Step()) {
      Check(Number(checks,0)==first+ordinal++ && Number(checks,1)==Number(checks,3),"measurement mapping role/order disagrees");
      const auto role=Number(checks,1);auto source=ReadNumbers(checks,2),other=ReadNumbers(checks,4);
      Check(NumericalEqual(source,other),"measurement expansion mapping disagrees");
      auto measured=ReadScalars(checks.Blob(5));if(verify)VerifyScalars(source,measured);
      measured[0]+=source.At(0);measured[1]+=source.At(0);
      for(auto value:measured)Check(std::isfinite(value),"absolute measurement overflows");
      if(role<2) {auto& observation=role==0?target:runtime;Check(!observation,"duplicate original-path observation");observation=measured;}
      else {Check(role<=3,"invalid measurement role");if(measured[2]<-1e-3)++result.failures;}
      ++result.checks;
    }
    Check(ordinal==count && target && runtime,"missing original-path measurements");checks.Reset();
    Check((*target)[0]>=0,"negative measured system delay");
    result.maximum=std::max(result.maximum,(*target)[0]);result.target.Add(path,(*target)[2]);result.runtime.Add(path,(*runtime)[2]);
    if(!paths.IsNull(3))result.local+=Number(paths,3)==1;if(!paths.IsNull(6))result.bounds+=Number(paths,6)==1;
    if(consume)consume(result.paths,paths,*target,*runtime);++result.paths;
  }
  Check(result.checks==expected_checks && result.paths==expected_paths,"original measurement coverage disagrees");return result;
}
std::string CheckMeasurementOwner(SqliteDatabase& db,SqliteDatabase& index,const std::string& header) {
  RequireSqliteDDL(db,kGlobalMeasurementsDDL);const auto meta=Metadata(db,"header");Check(meta.size()<65536,"measurement header exceeds bound");
  Check(JsonText(db,meta,"$.source_logical_sha256")==JsonText(index,header,"$.logical_sha256") &&
        JsonNumber(db,meta,"$.checks")==JsonNumber(index,header,"$.checks") &&
        JsonNumber(db,meta,"$.representative_checks")==JsonNumber(index,header,"$.representative_checks"),"measurement identity namespace disagrees");
  Check(JsonText(db,meta,"$.implementation_id")=="native-global-measurements-v1","measurement implementation is unsupported");
  auto fields=db.Prepare("SELECT count(*),count(DISTINCT key),sum(key IN ('schema','implementation_id','source_logical_sha256','source_tsv_sha256','checks','representative_checks','arcs_verified','reduction')) FROM json_each(?)");fields.BindText(1,meta);
  Check(fields.Step() && fields.Integer(0)==8 && fields.Integer(1)==8 && fields.Integer(2)==8,"measurement metadata fields disagree");
  return meta;
}
void CheckReduction(SqliteDatabase& db,const std::string& meta,const Reduction& reduction) {
  auto summary=db.Prepare("SELECT json_extract(?,'$.reduction')");summary.BindText(1,meta);
  Check(summary.Step() && summary.Text(0)==reduction.Record().Encode(),"measurement reduction/digest disagrees");
}
std::string BoundedHeader(CheckedRequestReader& reader,SqliteDatabase& db) {
  const auto header=reader.ReadString32(false,8*1024*1024);
  auto valid=db.Prepare("SELECT json_valid(?1),json_type(?1)");valid.BindText(1,header);
  Check(valid.Step() && valid.Integer(0) && valid.Text(1)=="object","QoR header is not JSON object");
  auto repeated=db.Prepare("SELECT 1 FROM json_tree(?) WHERE parent IS NOT NULL GROUP BY parent,key HAVING count(*)>1 LIMIT 1");repeated.BindText(1,header);Check(!repeated.Step(),"QoR header has duplicate keys");
  return header;
}
JsonRecord PathRecord(SqliteStatement& path,const Scalars& target,const Scalars& runtime) {
  const auto normalized=[](double n){return n==0?0.0:n;};
  Object value{{"path",Text(path,1)},{"representative_path",Text(path,2)},
    {"path_scope",Number(path,3)?"same-fpga-local":"cross-fpga"},{"clock_domain",path.IsNull(4)?JsonRecord():JsonRecord(Text(path,4))},
    {"target_period_ns",path.TypedReal(5)},{"physical_logic_segments_cone_bound",bool(Number(path,6))},
    {"system_delay_bound_ns",normalized(target[0])},{"target_required_time_ns",normalized(target[1])},
    {"target_clock_slack_bound_ns",normalized(target[2])},{"runtime_required_time_ns",normalized(runtime[1])},{"runtime_clock_slack_bound_ns",normalized(runtime[2])}};
  if(!path.IsNull(7))value.emplace("interconnect_delay_model",Text(path,7));return value;
}
void HeaderCoverage(SqliteDatabase& db,const std::string& header,Integer count) {
  Check(JsonText(db,header,"$.schema")=="emuflow.qor-report/v4" && JsonText(db,header,"$.timing.schema")=="emuflow.system-timing/v2" &&
      JsonText(db,header,"$.timing.global_opensta.implementation_id")=="native-global-measurements-v1" && JsonNumber(db,header,"$.timing.summary.timing_paths")==count,"QoR header schema/coverage disagrees");
  auto paths=db.Prepare("SELECT json_type(?,'$.timing.paths')");paths.BindText(1,header);Check(paths.Step() && paths.IsNull(0),"QoR header contains path payload");
}
double JsonReal(SqliteDatabase& db,const std::string& object,const std::string& path) {
  auto row=db.Prepare("SELECT json_type(?1,?2),json_extract(?1,?2)");row.BindText(1,object);row.BindText(2,path);
  Check(row.Step() && (row.Text(0)=="real" || row.Text(0)=="integer"),"QoR header numerical field is invalid");
  const auto value=row.TypedReal(1);Check(std::isfinite(value),"QoR header has nonfinite number");return value;
}
void HeaderReduction(SqliteDatabase& db,const std::string& header,const Reduction& result) {
  Check(JsonNumber(db,header,"$.timing.summary.original_local_paths")==result.local &&
      JsonNumber(db,header,"$.timing.summary.original_cross_fpga_paths")==result.paths-result.local &&
      JsonNumber(db,header,"$.timing.path_exactness.cone_bound_logic_paths")==result.bounds &&
      JsonNumber(db,header,"$.timing.path_exactness.fixed_slot_event_failures")==result.failures &&
      JsonNumber(db,header,"$.timing.global_opensta.checks")==result.checks &&
      JsonNumber(db,header,"$.timing.global_opensta.original_paths")==result.paths &&
      JsonNumber(db,header,"$.timing.global_opensta.event_failures")==result.failures &&
      JsonNumber(db,header,"$.timing.path_exactness.endpoint_exact_logic_paths")==result.paths-result.bounds &&
      JsonReal(db,header,"$.timing.summary.maximum_system_delay_bound_ns")==result.maximum &&
      JsonText(db,header,"$.timing.global_opensta.measurement_logical_sha256")==result.measurement_sha,
      "QoR header reduction/counts disagree");
  const auto status=(result.failures || result.runtime.wns<0)?"fail":"pass";
  Check(JsonText(db,header,"$.timing.status")==status && JsonText(db,header,"$.timing.global_opensta.status")==status,"QoR header status disagrees");
  for(const auto& entry:std::array<std::pair<const char*,const Metric*>,2>{{{"target",&result.target},{"runtime",&result.runtime}}}) {
    const auto role=std::string(entry.first),prefix="$.timing."+role+"_clock.";const auto& value=*entry.second;
    Check(JsonText(db,header,(prefix+"worst_path").c_str())==value.worst &&
      JsonReal(db,header,prefix+"worst_slack_bound_ns")==value.wns &&
      JsonReal(db,header,prefix+"tns_bound_ns")==value.sum.Finish() &&
      JsonReal(db,header,prefix+"total_negative_slack_bound_ns")==value.sum.Finish() &&
      JsonNumber(db,header,(prefix+"negative_slack_paths").c_str())==value.negative &&
      JsonReal(db,header,"$.timing.global_opensta.metrics."+role+".wns_ns")==value.wns &&
      JsonReal(db,header,"$.timing.global_opensta.metrics."+role+".original_path_tns_ns")==value.sum.Finish() &&
      JsonNumber(db,header,("$.timing.global_opensta.metrics."+role+".negative_paths").c_str())==value.negative,"QoR clock metrics disagree");
  }
}
std::string LogicalQor(const std::string& header,Integer count,const std::string& paths_sha) {
  Sha256 hash;Hash(hash,"{\"counts\":{\"timing_paths\":"+std::to_string(count)+"},\"header\":"+header+",\"paths_sha256\":"+JsonQuote(paths_sha)+"}");return Hex(hash);
}
}  // namespace

void ImportGlobalMeasurements(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  const auto header=IndexHeader(inputs);SqliteStore index(inputs[1]);
  Check(JsonNumber(index.database(),header,"$.mode")&1,"measurement import requires timing checks");
  CheckedRequestReader request(inputs[3].path(),4096);Check(request.Read<std::uint64_t>()==0x314d4947464d5545ULL && request.Read<std::uint32_t>()==1,"measurement import request version is invalid");
  const auto representative=request.Read<std::uint32_t>(),verify=request.Read<std::uint32_t>();Check(representative<=1 && verify<=1,"measurement import flags are invalid");
  for(int i=0;i<3;++i)ExpectedGeneration(request,inputs[i]);request.RequireEnd();
  SqliteArtifactWriter writer(output,"global-measurements-store-v1",inputs);auto& db=writer.database();db.Execute(kGlobalMeasurementsDDL);AttachFacts(db,inputs);SqliteTransaction transaction(db);
  TsvInput data(inputs[2].path());SqliteStore primary(inputs[0]);Selection rows(primary.database(),index.database(),representative);
  const auto count=JsonNumber(index.database(),header,representative?"$.representative_checks":"$.checks");
  auto mapping=db.Prepare("SELECT i.engine_first_check+c.seq-i.first_check FROM facts.checks c JOIN idx.path_index i ON i.path_seq=c.path_seq WHERE c.seq=?");
  auto existing=db.Prepare("SELECT numbers FROM measurements WHERE seq=?"),append=db.Prepare("INSERT INTO measurements VALUES(?,?)");
  rows.Visit(count,[&](Integer ordinal,const Numbers&) {
    const auto value=data.Take(ordinal);Integer seq=ordinal;
    if(!representative){mapping.BindInteger(1,ordinal);Check(mapping.Step(),"measurement mapping is missing");seq=Number(mapping,0);Check(!mapping.Step(),"measurement mapping repeats");mapping.Reset();}
    existing.BindInteger(1,seq);
    if(existing.Step()){Check(ReadScalars(existing.Blob(0))==value,"repeated representative engine scalars disagree");Check(!existing.Step(),"repeated representative index");existing.Reset();}
    else {existing.Reset();append.BindInteger(1,seq);append.BindBlob(2,value.data(),24);append.Execute();}
  });
  const auto source_sha=data.Finish();const auto reduction=ReduceMeasurements(db,index.database(),header,verify);
  PutMetadata(db,"header",Object{{"schema","emuflow.global-measurements/v1"},{"implementation_id","native-global-measurements-v1"},
    {"source_logical_sha256",JsonText(index.database(),header,"$.logical_sha256")},{"source_tsv_sha256",source_sha},
    {"checks",JsonNumber(index.database(),header,"$.checks")},{"representative_checks",JsonNumber(index.database(),header,"$.representative_checks")},
    {"arcs_verified",bool(verify)},{"reduction",reduction.Record()}});
  mapping=SqliteStatement{};existing=SqliteStatement{};append=SqliteStatement{};transaction.Commit();writer.Commit();
}
void AuditGlobalMeasurements(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  const auto header=IndexHeader(inputs);SqliteStore index(inputs[1]),measurements(inputs[2]);auto& db=measurements.database();const auto meta=CheckMeasurementOwner(db,index.database(),header);
  CheckedRequestReader request(inputs[3].path(),4096);Check(request.Read<std::uint64_t>()==0x31415647464d5545ULL && request.Read<std::uint32_t>()==1,"measurement audit request version is invalid");
  const auto verify=request.Read<std::uint32_t>();Check(verify<=1,"measurement audit flag is invalid");for(int i=0;i<3;++i)ExpectedGeneration(request,inputs[i]);request.RequireEnd();
  AttachFacts(db,inputs);const auto reduction=ReduceMeasurements(db,index.database(),header,verify);CheckReduction(db,meta,reduction);
  PublishTextArtifact(output,inputs,[&](std::ostream& out){out<<reduction.Record().Encode()<<'\n';});
}
void PublishGlobalQor(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  const auto index_header=IndexHeader(inputs);SqliteStore index(inputs[1]),measurements(inputs[2]);auto& db=measurements.database();const auto meta=CheckMeasurementOwner(db,index.database(),index_header);
  CheckedRequestReader request(inputs[3].path(),8*1024*1024+4096);Check(request.Read<std::uint64_t>()==0x31515047464d5545ULL && request.Read<std::uint32_t>()==1,"QoR request version is invalid");
  for(int i=0;i<3;++i)ExpectedGeneration(request,inputs[i]);const auto header=BoundedHeader(request,db);request.RequireEnd();
  Check(JsonNumber(index.database(),index_header,"$.mode")==3,"QoR requires bound path metadata");HeaderCoverage(db,header,JsonNumber(index.database(),index_header,"$.paths"));AttachFacts(db,inputs);
  SqliteArtifactWriter writer(output,"qor-report-store-v1",inputs);auto& final=writer.database();final.Execute(kGlobalQorDDL);SqliteTransaction transaction(final);Sha256 digest;
  auto append=final.Prepare("INSERT INTO timing_paths VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)");
  const auto reduction=ReduceMeasurements(db,index.database(),index_header,false,[&](Integer seq,SqliteStatement& path,const Scalars& target,const Scalars& runtime) {
    Hash(digest,PathRecord(path,target,runtime).EncodeUtf8()+"\n");append.BindInteger(1,seq);append.BindText(2,Text(path,1));append.BindInteger(3,path.IsNull(7)?31:63);
    append.BindText(4,Text(path,2));append.BindText(5,Number(path,3)?"same-fpga-local":"cross-fpga");if(path.IsNull(4))append.BindNull(6);else append.BindText(6,Text(path,4));
    append.BindReal(7,path.TypedReal(5));append.BindInteger(8,Number(path,6));if(path.IsNull(7))append.BindNull(9);else append.BindText(9,Text(path,7));
    const std::array<double,5> values{target[0],target[1],target[2],runtime[1],runtime[2]};for(int i=0;i<5;++i)append.BindReal(10+i,values[i]);append.BindText(15,"{}");append.Execute();
  });
  CheckReduction(db,meta,reduction);HeaderReduction(db,header,reduction);
  const auto logical=LogicalQor(header,reduction.paths,Hex(digest));PutMetadata(final,"counts",Object{{"timing_paths",reduction.paths}});
  auto put=final.Prepare("INSERT INTO metadata VALUES('header',?)");put.BindText(1,header);put.Execute();put=SqliteStatement{};PutMetadata(final,"logical_sha256",logical);
  append=SqliteStatement{};transaction.Commit();writer.Commit();
}
void ValidateGlobalQor(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  SqliteStore store(inputs[0]);auto& db=store.database();RequireSqliteDDL(db,kGlobalQorDDL);
  CheckedRequestReader request(inputs[1].path(),8*1024*1024+4096);Check(request.Read<std::uint64_t>()==0x31565147464d5545ULL && request.Read<std::uint32_t>()==1,"QoR validation request version is invalid");ExpectedGeneration(request,inputs[0]);const auto header=BoundedHeader(request,db);const auto expected=request.ReadString32();request.RequireEnd();
  auto metadata_count=db.Prepare("SELECT count(*) FROM metadata");Check(metadata_count.Step() && metadata_count.Integer(0)==3,"QoR metadata population disagrees");
  Check(header==Metadata(db,"header"),"QoR header/selector disagree");auto count=db.Prepare("SELECT json_extract(value,'$.timing_paths') FROM metadata WHERE key='counts'");Check(count.Step(),"QoR count is missing");const auto total=Number(count,0);HeaderCoverage(db,header,total);
  auto rows=db.Prepare("SELECT seq,path,representative_path,path_scope,clock_domain,target_period_ns,cone_bound,interconnect_delay_model,system_delay_bound_ns,target_required_time_ns,target_clock_slack_bound_ns,runtime_required_time_ns,runtime_clock_slack_bound_ns,typed_presence,extra_json FROM timing_paths ORDER BY seq");
  Sha256 digest;Integer seen=0;std::string previous;
  while(rows.Step()) {
    Check(Number(rows,0)==seen++ && (seen==1 || previous<Text(rows,1)),"QoR path order/identity disagrees");previous=Text(rows,1);
    // Adapt the existing typed QoR columns to the canonical event path record.
    const auto scope=Text(rows,3);Check(scope=="same-fpga-local" || scope=="cross-fpga","QoR scope is invalid");
    Object record{{"path",previous},{"representative_path",Text(rows,2)},{"path_scope",scope},
      {"clock_domain",rows.IsNull(4)?JsonRecord():JsonRecord(Text(rows,4))},{"target_period_ns",rows.TypedReal(5)},
      {"physical_logic_segments_cone_bound",bool(Number(rows,6))}};
    Check(Number(rows,6)<=1 && rows.TypedReal(5)>0 && std::isfinite(rows.TypedReal(5)) && rows.TypedReal(8)>=0 && rows.TypedText(14)=="{}" && Number(rows,13)==(rows.IsNull(7)?31:63),"QoR typed fields disagree");
    if(!rows.IsNull(7))record.emplace("interconnect_delay_model",Text(rows,7));
    const char* names[]={"system_delay_bound_ns","target_required_time_ns","target_clock_slack_bound_ns","runtime_required_time_ns","runtime_clock_slack_bound_ns"};
    for(int i=0;i<5;++i){const auto value=rows.TypedReal(8+i);Check(std::isfinite(value) && !rows.IsNull(8+i),"QoR number is invalid");record.emplace(names[i],value);}
    Hash(digest,JsonRecord(std::move(record)).EncodeUtf8()+"\n");
  }
  const auto logical=LogicalQor(header,seen,Hex(digest));Check(seen==total && logical==expected && JsonText(db,Metadata(db,"logical_sha256"),"$")==logical,"QoR logical records disagree");
  PublishTextArtifact(output,inputs,[&](std::ostream& out){out<<JsonRecord(Object{{"schema","emuflow.native-qor-validation/v1"},{"status","pass"},{"paths",seen},{"logical_sha256",logical}}).Encode()<<'\n';});
}
}  // namespace emuflow::core

namespace emuflow::core {
namespace {
double BindingReal(SqliteStatement& row,int column,bool positive=false) {
  const auto value=row.TypedReal(column);
  Check(std::isfinite(value) && (positive?value>0:value>=0),"physical timing has an invalid delay/period");return value;
}
std::string OptionalText(SqliteStatement& row,int column) {return row.IsNull(column)?"":Text(row,column);}
std::string JsonOptional(SqliteDatabase& db,const std::string& json,const char* path) {
  auto r=db.Prepare("SELECT json_extract(?1,?2)");r.BindText(1,json);r.BindText(2,path);Check(r.Step(),"binding metadata is missing");return OptionalText(r,0);
}
struct EventPrimaryWriter {
  SqliteDatabase& db;SqliteStatement path,event_find,event_put,check;
  Integer paths=0,events=0,checks=0;
  std::map<std::string,Integer> cache;std::deque<std::string> fifo;
  explicit EventPrimaryWriter(SqliteDatabase& d):db(d) {
    db.Execute(kGlobalEventsDDL);
    db.Execute("CREATE TEMP TABLE binding_segment_ids(id TEXT PRIMARY KEY NOT NULL) WITHOUT ROWID");
    path=db.Prepare("INSERT INTO paths VALUES(?,?,?,?,?,?,?,?)");
    event_find=db.Prepare("SELECT seq FROM events WHERE event=?");event_put=db.Prepare("INSERT INTO events VALUES(?,?)");
    check=db.Prepare("INSERT INTO checks VALUES(?,?,?,?,?)");
  }
  void Path(const std::string& id,const std::string& representative,bool local,const std::string& clock,double period,bool cone,bool ratio) {
    Check(!id.empty() && !representative.empty() && std::isfinite(period) && period>0,"path metadata is invalid");
    path.BindInteger(1,paths++);path.BindText(2,id);path.BindText(3,representative);path.BindInteger(4,local);
    if(clock.empty())path.BindNull(5);else path.BindText(5,clock);path.BindReal(6,period);path.BindInteger(7,cone);
    if(ratio)path.BindText(8,"linear-ratio-v1");else path.BindNull(8);path.Execute();
  }
  Integer Event(const std::string& id) {
    auto found=cache.find(id);if(found!=cache.end())return found->second;
    event_find.BindText(1,id);Integer seq;
    if(event_find.Step()){seq=Number(event_find,0);Check(!event_find.Step(),"event identity repeats");event_find.Reset();}
    else {event_find.Reset();seq=events++;event_put.BindInteger(1,seq);event_put.BindText(2,id);event_put.Execute();}
    cache.emplace(id,seq);fifo.push_back(id);if(fifo.size()>8192){cache.erase(fifo.front());fifo.pop_front();}return seq;
  }
  void CheckRow(Integer role,const std::string& id,double launch,const std::vector<double>& arcs,double required) {
    Check(std::isfinite(launch) && launch>=0 && std::isfinite(required),"bound check has invalid times");
    std::vector<double> payload{launch,required};for(auto arc:arcs){Check(std::isfinite(arc) && arc>=0,"bound check has invalid arc");payload.push_back(arc);}
    check.BindInteger(1,checks++);check.BindInteger(2,paths-1);check.BindInteger(3,role);check.BindInteger(4,Event(id));
    check.BindBlob(5,payload.data(),payload.size()*sizeof(double));check.Execute();
  }
};
struct BindingSegment {
  std::string system,member,id,kind,replace,fpga,measurement;
  Integer cut=0;double delay=0;
};
struct BindingPhysical {
  std::string fpga;std::unique_ptr<SqliteStore> timing,identity,logic;
  SqliteStatement endpoint,segments;bool have=false;BindingSegment current;
  void Next() {
    have=segments.Step();if(!have)return;
    current={Text(segments,0),Text(segments,1),Text(segments,2),Text(segments,3),OptionalText(segments,5),Text(segments,7),Text(segments,8),Number(segments,4),BindingReal(segments,6)};
    Check(current.fpga==fpga && (current.kind=="launch" || current.kind=="transition" || current.kind=="capture") &&
       (current.measurement=="endpoint-exact" || current.measurement=="cut-net-cone-upper-bound" || current.measurement=="optimized-endpoint-collection-upper-bound"),"logic segment classification disagrees");
  }
  double Endpoint(const std::string& id,const std::string& entry,const char* kind) {
    endpoint.BindText(1,id);Check(endpoint.Step() && Text(endpoint,0)==kind && Text(endpoint,1)==entry,"missing physical endpoint or schedule binding");
    const auto delay=BindingReal(endpoint,2);Check(!endpoint.Step(),"physical endpoint repeats");endpoint.Reset();return delay;
  }
};
void BindingHeader(SqliteDatabase& db,const std::string& header,const std::string& fpga,const std::string& design,const std::string& platform,bool local=false) {
  Check(header.size()<1024*1024 && JsonText(db,header,"$.status")=="pass" && JsonText(db,header,"$.fpga")==fpga &&
      JsonText(db,header,"$.design")==design,"physical source identity disagrees");
  if(!local)Check(JsonText(db,header,"$.platform")==platform,"physical platform identity disagrees");
  (void)JsonText(db,header,"$.provider");
}
void ValidateBindingEndpoints(BindingPhysical& p,SqliteDatabase& schedule,const std::string& design,const std::string& platform) {
  auto& t=p.timing->database();auto& i=p.identity->database();RequireSqliteDDL(t,kBoundaryTimingDDL);RequireSqliteDDL(i,kBoundaryIdentityDDL);
  const auto th=Metadata(t,"header"),ih=Metadata(i,"header");
  BindingHeader(t,th,p.fpga,design,platform);BindingHeader(i,ih,p.fpga,design,platform);
  (void)JsonText(t,th,"$.qualification");const auto scope=JsonOptional(t,th,"$.measurement_scope");
  Check(scope.empty() || scope=="partition-boundary" || scope=="board-integrated-interface","boundary measurement scope is invalid");
  auto a=t.Prepare("SELECT id,kind,schedule_entry,delay_ns,start_object,end_object,measurement FROM timings ORDER BY id");
  auto b=i.Prepare("SELECT id,kind,schedule_entry,demand,logical_net,link,peer,logical_lane FROM endpoints ORDER BY id");
  auto entry=schedule.Prepare("SELECT source_fpga,sink_fpga,demand,net,link_id,lane FROM schedule_entries WHERE entry_id=?");
  Integer count=0,tx=0,rx=0;
  while(a.Step()) {
    Check(b.Step(),"boundary endpoint coverage disagrees");
    const auto id=Text(a,0),kind=Text(a,1),eid=Text(a,2);
    Check((kind=="tx" || kind=="rx") && id==Text(b,0) && kind==Text(b,1) && eid==Text(b,2),"boundary identity/timing disagrees");
    Check(id=="__emuflow_"+kind+"_"+eid,"boundary endpoint is outside its schedule namespace");
    (void)BindingReal(a,3);(void)Text(a,4);(void)Text(a,5);
    const auto measurement=scope=="board-integrated-interface" ? (kind=="tx"?"routed-launch-through-tx-boundary-to-interface-endpoint":"interface-startpoint-through-rx-boundary-to-shadow-capture"):
        (kind=="tx"?"logical-source-to-tx-port":"rx-port-to-shadow-capture");
    Check(Text(a,6)==measurement,"boundary measurement trace scope disagrees");
    entry.BindText(1,eid);Check(entry.Step(),"boundary references an absent schedule entry");
    Check(Text(entry,kind=="tx"?0:1)==p.fpga && Text(entry,kind=="tx"?1:0)==Text(b,6) && Text(entry,2)==Text(b,3) &&
        Text(entry,3)==Text(b,4) && Text(entry,4)==Text(b,5) && Number(entry,5)==Number(b,7),"boundary/schedule ownership disagrees");
    Check(!entry.Step(),"schedule entry identity repeats");entry.Reset();++count;if(kind=="tx")++tx;else ++rx;
  }
  Check(!b.Step() && JsonNumber(t,th,"$.coverage.endpoints")==count && JsonNumber(t,th,"$.coverage.tx")==tx && JsonNumber(t,th,"$.coverage.rx")==rx &&
      JsonNumber(i,ih,"$.coverage.endpoints")==count && JsonNumber(i,ih,"$.coverage.tx")==tx && JsonNumber(i,ih,"$.coverage.rx")==rx,"boundary endpoint coverage disagrees");
  p.endpoint=t.Prepare("SELECT kind,schedule_entry,delay_ns FROM timings WHERE id=?");
}
} // namespace

void BindGlobalEvents(const std::vector<ArtifactRef>& inputs,const std::string& output) {
  Check(inputs.size()>=4,"binding input coverage is invalid");
  CheckedRequestReader request(inputs.back().path(),8*1024*1024);
  Check(request.Read<std::uint64_t>()==0x31424747464d5545ULL && request.Read<std::uint32_t>()==1,"binding request version is invalid");
  const auto boundary_count=request.Read<std::uint32_t>(),local_count=request.Read<std::uint32_t>();
  Check(boundary_count<=256 && local_count<=256 && inputs.size()==4+3*boundary_count+local_count,"binding source coverage is invalid");
  for(std::size_t i=0;i+1<inputs.size();++i)ExpectedGeneration(request,inputs[i]);
  const auto route_sha=request.ReadString32(),route_pretty=request.ReadString32(),schedule_sha=request.ReadString32(),schedule_pretty=request.ReadString32();
  const auto config=request.ReadString32(false,4*1024*1024);request.RequireEnd();
  SqliteStore routes(inputs[0]),schedule(inputs[1]);auto& r=routes.database();auto& s=schedule.database();
  const auto rh=Metadata(r,"header"),sh=Metadata(s,"header");
  Check(Metadata(r,"logical_sha256")==JsonQuote(route_sha) && Metadata(s,"logical_sha256")==JsonQuote(schedule_sha),"binding logical source seals disagree");
  const auto design=JsonText(r,rh,"$.design"),platform=JsonText(r,rh,"$.platform");
  Check(JsonText(r,config,"$.design")==design && JsonText(r,config,"$.platform")==platform && JsonText(s,sh,"$.design")==design && JsonText(s,sh,"$.platform")==platform,"binding design/platform disagree");
  const auto semantics=JsonText(s,sh,"$.transport_semantics");
  Check(semantics=="sampled-virtual-wire" || semantics=="registered-boundary","binding requires fixed-slot transport");
  const auto semantic=JsonOptional(r,rh,"$.semantic_contract_sha256");
  Check(JsonOptional(s,sh,"$.semantic_contract_sha256")==semantic,"binding semantic source seals disagree");
  const auto board=ReadExactScheduleBoard(inputs[0],inputs[2].path());
  Check(JsonNumber(s,sh,"$.route_constraints.frame_slots")==board.frame,"binding frame identity disagrees");
  auto uncertainty_row=r.Prepare("SELECT json_extract(?,'$.uncertainty')");uncertainty_row.BindText(1,config);Check(uncertainty_row.Step(),"uncertainty is missing");const auto uncertainty=BindingReal(uncertainty_row,0);
  std::map<std::string,double> clocks;
  auto clock_rows=r.Prepare("SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?,'$.clocks')");clock_rows.BindText(1,config);
  while(clock_rows.Step())Check(clocks.emplace(Text(clock_rows,0),BindingReal(clock_rows,1,true)).second,"runtime clock repeats");
  Check(!clocks.empty(),"runtime clocks are missing");
  const auto virtual_period=[&](const std::string& clock){auto found=clocks.find(clock);if(found!=clocks.end())return found->second;Check(clocks.size()==1,"unknown runtime clock domain");return clocks.begin()->second;};
  struct Link {double bound=0,alpha=0,beta=0;Integer minimum=0,maximum=0,quantum=0;bool affine=false;};
  std::map<std::tuple<std::string,std::string,std::string>,Link> links;
  auto link_rows=r.Prepare("SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]'),json_extract(value,'$[3]'),json_extract(value,'$[4]'),json_extract(value,'$[5]'),json_extract(value,'$[6]'),json_extract(value,'$[7]'),json_extract(value,'$[8]') FROM json_each(?,'$.links')");link_rows.BindText(1,config);
  while(link_rows.Step()) {
    Link link;link.bound=BindingReal(link_rows,3);link.affine=!link_rows.IsNull(4);
    if(link.affine){link.alpha=BindingReal(link_rows,4);link.beta=BindingReal(link_rows,5);link.minimum=Number(link_rows,6);link.maximum=Number(link_rows,7);link.quantum=Number(link_rows,8);
      Check(link.minimum>0 && link.maximum>=link.minimum && link.quantum>0 && link.minimum%link.quantum==0 && link.maximum%link.quantum==0,"physical ratio domain is invalid");}
    Check(links.emplace(std::make_tuple(Text(link_rows,0),Text(link_rows,1),Text(link_rows,2)),link).second,"directed board link repeats");
  }
  SqliteArtifactWriter writer(output,"global-event-primary-v1",inputs);auto& db=writer.database();
  db.Execute("PRAGMA cache_size=-8192; PRAGMA temp_store=FILE; PRAGMA temp.cache_size=-8192");
  SqliteTransaction transaction(db);EventPrimaryWriter events(db);
  std::vector<std::unique_ptr<BindingPhysical>> physical;std::map<std::string,BindingPhysical*> by_fpga;
  auto fpgas=r.Prepare("SELECT value FROM json_each(?,'$.boundary_fpgas') ORDER BY key");fpgas.BindText(1,config);
  for(unsigned i=0;i<boundary_count;++i) {
    Check(fpgas.Step(),"boundary FPGA catalog is incomplete");auto p=std::make_unique<BindingPhysical>();p->fpga=Text(fpgas,0);
    Check(by_fpga.emplace(p->fpga,p.get()).second,"boundary FPGA repeats");
    p->timing=std::make_unique<SqliteStore>(inputs[3+3*i]);p->identity=std::make_unique<SqliteStore>(inputs[4+3*i]);p->logic=std::make_unique<SqliteStore>(inputs[5+3*i]);
    ValidateBindingEndpoints(*p,s,design,platform);
    auto& l=p->logic->database();RequireSqliteDDL(l,kLogicSegmentTimingDDL);const auto lh=Metadata(l,"header");BindingHeader(l,lh,p->fpga,design,platform);
    Check(JsonText(l,lh,"$.system_routes_sha256")==route_pretty && JsonText(l,lh,"$.tdm_schedule_sha256")==schedule_pretty && JsonOptional(l,lh,"$.semantic_contract_sha256")==semantic,"physical logic source seals disagree");
    auto missing=l.Prepare("SELECT 1 FROM unsupported_members UNION ALL SELECT 1 FROM unmeasured_segments LIMIT 1");Check(!missing.Step(),"physical logic segments are incomplete");
    p->segments=l.Prepare("SELECT system_path,member_path,segment_id,kind,cut_index,replace_tx_endpoint,delay_ns,fpga,measurement FROM segments ORDER BY system_path,member_path,cut_index,segment_id");p->Next();physical.push_back(std::move(p));
  }
  Check(!fpgas.Step(),"boundary FPGA catalog has extra records");
  auto segment_id=db.Prepare("INSERT INTO binding_segment_ids VALUES(?)");
  const auto take=[&](const std::string& path,const std::string& member){
    std::vector<BindingSegment> result;
    for(;;) {
      BindingPhysical* next=nullptr;
      for(auto& p:physical)if(p->have && (!next || std::tie(p->current.system,p->current.member,p->current.cut,p->current.id)<std::tie(next->current.system,next->current.member,next->current.cut,next->current.id)))next=p.get();
      if(!next)break;
      const auto key=std::make_pair(next->current.system,next->current.member),wanted=std::make_pair(path,member);
      Check(key>=wanted,"unexpected/unconsumed physical logic member");if(key>wanted)break;
      Check(next->current.cut==static_cast<Integer>(result.size()),"missing or duplicate original-member logic segment");
      segment_id.BindText(1,next->current.id);segment_id.Execute();result.push_back(next->current);next->Next();
    }
    return result;
  };
  Integer representatives=0;
  const auto totals=VisitScheduledTimingPaths(inputs[0],inputs[1],board,[&](const ScheduledTimingPath& path) {
    ++representatives;const auto runtime=virtual_period(path.clock);
    auto member_rows=r.Prepare("SELECT type,value FROM json_each((SELECT record_json FROM timing_paths WHERE seq=?),'$.compressed_path_ids') ORDER BY value");member_rows.BindInteger(1,path.source_seq);
    Integer members=0;const auto emit=[&](const std::string& member) {
      const auto chain=take(path.path,member);Check(chain.size()==path.cuts.size()+1,"missing original-member logic segments");
      bool ratio=false,cone=false;std::set<double> periods;
      for(const auto& hop:path.hops){auto link=links.find({hop.link,hop.from,hop.to});Check(link!=links.end(),"physical board link is missing");ratio|=link->second.affine;}
      for(const auto& seg:chain)cone|=seg.measurement!="endpoint-exact";
      for(const auto& hop:path.hops)Check(links.at({hop.link,hop.from,hop.to}).affine==ratio,"one path cannot mix total net-ratio and fixed-slot delay models");
      events.Path(member,path.path,false,path.clock,path.period,cone,ratio);
      double origin=0;std::vector<double> trailing,model;std::size_t used=0;
      for(std::size_t k=0;k<path.cuts.size();++k) {
        const auto& cut=path.cuts[k];Check(cut.hop_count>0 && cut.first_hop==used && chain[k].replace=="__emuflow_tx_"+path.hops[used].entry,"cut/segment binding disagrees");
        Check(chain[k].fpga==cut.from && chain[k].kind==(k==0?"launch":"transition"),"logic segment launch/transition ownership disagrees");
        for(std::size_t j=0;j<cut.hop_count;++j) {
          const auto& hop=path.hops[used++];periods.insert(hop.period);
          auto tx=by_fpga.find(hop.from),rx=by_fpga.find(hop.to);Check(tx!=by_fpga.end() && rx!=by_fpga.end(),"physical FPGA endpoint owner is missing");
          const auto transmit=j?tx->second->Endpoint("__emuflow_tx_"+hop.entry,hop.entry,"tx"):chain[k].delay;
          const auto receive=rx->second->Endpoint("__emuflow_rx_"+hop.entry,hop.entry,"rx");
          const auto& link=links.at({hop.link,hop.from,hop.to});double delay=link.bound;
          if(ratio){Check(hop.ratio && *hop.ratio>=static_cast<std::uint64_t>(link.minimum) && *hop.ratio<=static_cast<std::uint64_t>(link.maximum) && *hop.ratio%link.quantum==0,"ratio model has an unsupported ratio");volatile double product=link.alpha*static_cast<double>(*hop.ratio);delay=product+link.beta;}
          std::vector<double> arcs=trailing;arcs.push_back(transmit);
          if(ratio){model.push_back(transmit);model.push_back(delay);model.push_back(receive);}
          if(semantics!="registered-boundary" || j)events.CheckRow(2,hop.entry,origin,arcs,hop.tx_time-uncertainty);
          origin=hop.tx_time;trailing={delay,receive};
        }
      }
      Check(periods.size()==1 && used==path.hops.size() && chain.back().kind=="capture" && chain.back().replace.empty() && chain.back().fpga==path.cuts.back().to,"incomplete event coverage or invalid capture");
      trailing.push_back(chain.back().delay);trailing.push_back(uncertainty);model.push_back(chain.back().delay);model.push_back(uncertainty);
      const double commit=(board.frame-1)*(*periods.begin());
      events.CheckRow(0,"capture",ratio?0:origin,ratio?model:trailing,path.required);
      events.CheckRow(1,"capture",ratio?0:origin,ratio?model:trailing,runtime-(path.period-path.required));
      events.CheckRow(3,"capture",origin,trailing,commit);++members;
    };
    if(!path.explicit_members)emit(path.path);
    else while(member_rows.Step()){Check(Text(member_rows,0)=="text","path member identity is invalid");emit(Text(member_rows,1));}
    Check(members==static_cast<Integer>(path.explicit_members?path.member_count:1),"path member coverage disagrees");
  });
  (void)totals;for(const auto& p:physical)Check(!p->have,"unconsumed physical logic member");
  JsonRecord source;std::string source_identity;auto locals=r.Prepare("SELECT value FROM json_each(?,'$.local_fpgas') ORDER BY key");locals.BindText(1,config);
  for(unsigned i=0;i<local_count;++i) {
    Check(locals.Step(),"local FPGA catalog is incomplete");const auto fpga=Text(locals,0);
    SqliteStore local(inputs[3+3*boundary_count+i]);auto& l=local.database();RequireSqliteDDL(l,kLocalPathTimingDDL);
    const auto lh=Metadata(l,"header");BindingHeader(l,lh,fpga,design,platform,true);(void)JsonText(l,lh,"$.qualification");
    (void)AuditPhysicalEvidenceStore(l,4);
    Object binding;for(const char* key:{"path_database_sha256","original_ir_sha256","assignment_sha256","routes_sha256","original_path_ids_sha256"}) {
      const auto value=JsonText(l,lh,(std::string("$.source.")+key).c_str());Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,"local source digest is invalid");binding.emplace(key,value);
    }
    const auto original_paths=JsonNumber(l,lh,"$.source.original_paths");Check(original_paths>0,"original path source is empty");
    binding.emplace("original_paths",original_paths);const auto encoded=JsonRecord(binding).Encode();
    Check(source_identity.empty() || source_identity==encoded,"local source seals disagree");source_identity=encoded;source=JsonRecord(std::move(binding));
    const auto identity=JsonText(l,lh,"$.identity_schema");Check(identity=="emuflow.local-path-identity/v1" || identity=="emuflow.local-path-identity/v2","local identity schema is invalid");
    auto rows=l.Prepare("SELECT id,kind,fpga,clock_domain,clock_period_ns,required_time_ns,measurement,delay_ns FROM timings ORDER BY id");Integer count=0;
    while(rows.Step()) {
      const auto id=Text(rows,0),clock=Text(rows,3);Check(Text(rows,1)=="local" && Text(rows,2)==fpga,"local timing identity disagrees");
      const auto period=BindingReal(rows,4,true),delay=BindingReal(rows,7);double required=period;
      if(!rows.IsNull(5)){required=BindingReal(rows,5,true);Check(std::isfinite(required),"local required time is invalid");}
      Check(identity=="emuflow.local-path-identity/v1" ? rows.IsNull(5):!rows.IsNull(5),"local required-time identity disagrees");
      const auto measurement=OptionalText(rows,6);Check(measurement.empty() || measurement=="explicit-routed-path-chain" || measurement=="endpoint-longest-path-fallback","local measurement is invalid");
      events.Path(id,id,true,clock,period,measurement!="explicit-routed-path-chain",false);
      events.CheckRow(0,"capture",0,{delay},required);events.CheckRow(1,"capture",0,{delay},virtual_period(clock)-(period-required));++count;
    }
    Check(JsonNumber(l,lh,"$.coverage.local_paths")==count,"local path coverage disagrees");
  }
  Check(!locals.Step(),"local FPGA catalog has extra records");
  PutMetadata(db,"header",Object{{"schema","emuflow.global-event-checks/v1"},{"mode",3},{"paths",events.paths},{"checks",events.checks},{"events",events.events}});
  PutMetadata(db,"binding",Object{{"implementation_id","native-physical-event-binding-v1"},{"compressed_representative_paths",representatives},{"source_binding",source}});
  transaction.Commit();events.path=SqliteStatement{};events.event_find=SqliteStatement{};events.event_put=SqliteStatement{};events.check=SqliteStatement{};segment_id=SqliteStatement{};
  writer.Commit();
}
} // namespace emuflow::core
