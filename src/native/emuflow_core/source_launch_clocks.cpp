#include "emuflow_core/source_launch_clocks.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <filesystem>
#include <iomanip>
#include <regex>
#include <set>
#include <sstream>

namespace emuflow::core {
namespace {
void Check(bool value, const char* message) {
  if (!value) throw StageError(ErrorCode::kInvalidArtifact,message);
}
void Digest(const std::string& value) {
  Check(value.size()==64 && value.find_first_not_of("0123456789abcdef")==std::string::npos,
        "source launch parent digest is invalid");
}
std::string Text(SqliteDatabase& db, const std::string& sql) {
  auto row=db.Prepare(sql);Check(row.Step(),"source launch metadata is missing");
  const auto value=row.TypedText(0);Check(!row.Step(),"source launch metadata repeats");return value;
}
std::string Header(SqliteDatabase& db, const char* field) {
  return Text(db,"SELECT json_extract(value,'$."+std::string(field)+"') FROM metadata WHERE key='header'");
}
std::string Hex(const std::string& value) {
  const char* digits="0123456789abcdef";std::string result;result.reserve(value.size()*2);
  for (unsigned char c:value) {result+=digits[c>>4];result+=digits[c&15];}return result;
}
std::string Unhex(const std::string& value) {
  Check(!value.empty() && value.size()<=2*1024*1024 && value.size()%2==0 &&
        value.find_first_not_of("0123456789abcdef")==std::string::npos,"source launch text encoding is invalid");
  std::string result;
  for(std::size_t i=0;i<value.size();i+=2)result+=char(std::stoul(value.substr(i,2),nullptr,16));
  Check(result.find('\0')==std::string::npos,"source launch text contains NUL");JsonQuote(result);return result;
}
bool Line(std::istream& input, std::string& line) {
  line.clear();char c;
  while(input.get(c)) {
    if(c=='\n')return true;
    Check(line.size()<2*1024*1024 && c!='\0',"source launch row exceeds bound or contains NUL");line+=c;
  }
  Check(input.eof() && line.empty(),"source launch row is truncated");return false;
}
std::vector<std::string> Fields(const std::string& line, unsigned expected) {
  std::vector<std::string> result;std::size_t begin=0;
  for (;;) {
    const auto end=line.find('\t',begin);result.push_back(line.substr(begin,end-begin));
    Check(!result.back().empty() && result.size()<=expected,"source launch TSV width differs");
    if(end==std::string::npos)break;begin=end+1;
  }
  Check(result.size()==expected,"source launch TSV width differs");return result;
}
double Number(const std::string& value) {
  std::istringstream stream(value);stream.imbue(std::locale::classic());double number;
  stream>>std::noskipws>>number;
  Check(stream && stream.peek()==EOF && std::isfinite(number),"source launch numeric fact is invalid");return number;
}
std::uint64_t Index(const std::string& value) {
  Check(!value.empty() && value.find_first_not_of("0123456789")==std::string::npos &&
        (value.size()==1 || value.front()!='0'),"source launch coordinate is invalid");
  return std::stoull(value);
}
std::optional<std::string> PairedBackslashes(const std::string& name) {
  std::string result;
  for(std::size_t i=0;i<name.size();++i) {
    result+=name[i];
    if(name[i]=='\\' && (++i==name.size() || name[i]!='\\'))return {};
  }
  return result;
}
std::string SdcName(const std::string& name) {
  // OpenSTA's SdcNetwork keeps escaped literal backslashes in SDC names;
  // its flat-cell divider handling removes only the divider escapes.
  std::string result;
  for(char c:name) {if(c=='\\')result+='\\';result+=c;}return result;
}
void BindPoint(SqliteStatement& statement, const SourceLaunchPoint& point, unsigned first=1) {
  statement.BindInteger(first,point.cell);statement.BindInteger(first+1,point.port);statement.BindInteger(first+2,point.bit);
}
}  // namespace

SourceLaunchResolver::SourceLaunchResolver(const ArtifactRef& source):source_(source) {
  Check(source.schema()=="emuir-compact-v2","source launch resolution requires compact original EmuIR");
  source_.database().Execute("CREATE TEMP TABLE launch_ports AS SELECT p.seq,p.name,"
      "json_extract(r.record_json,'$.width') AS width FROM port_records r JOIN pin_names p "
      "ON p.name=json_extract(r.record_json,'$.id') WHERE json_extract(r.record_json,'$.direction')='input';"
      "CREATE UNIQUE INDEX temp.launch_ports_name ON launch_ports(name)");
}
void SourceLaunchResolver::RequireUnchanged() const {source_.RequireUnchanged();}
SourceLaunchPoint SourceLaunchResolver::Resolve(const std::string& name,const std::string& kind) {
  Check(!name.empty() && name.size()<=1024*1024 && name.find('\0')==std::string::npos &&
        (kind.empty() || kind=="pin" || kind=="port"),"source launch physical object is invalid");
  JsonQuote(name);auto& db=source_.database();std::vector<SourceLaunchPoint> matches;
  if(kind!="pin") {
    // Index the original top-level inventory once; never expand wide buses
    // or rescan all connected top bits for every local register start.
    std::string base=name;std::optional<std::uint64_t> selected_bit;
    const auto bracket=name.rfind('[');
    if(bracket!=std::string::npos && name.back()==']') {
      const auto bit=name.substr(bracket+1,name.size()-bracket-2);
      if(!bit.empty() && bit.find_first_not_of("0123456789")==std::string::npos && (bit=="0" || bit.front()!='0')) {
        selected_bit=Index(bit);base=name.substr(0,bracket);
      }
    }
    auto ports=db.Prepare("SELECT seq,name,width FROM temp.launch_ports WHERE name=? OR name=?");
    ports.BindText(1,name);ports.BindText(2,base);
    while(ports.Step()) {
      const auto width=ports.TypedInteger(2),port_seq=ports.TypedInteger(0);const auto port=ports.TypedText(1);
      Check(width>0 && port_seq>=0,"source launch top-level coordinate is invalid");
      const auto bit=width==1?0ULL:selected_bit.value_or(UINT64_MAX);
      if(bit>=static_cast<std::uint64_t>(width) || name!=(width==1?port:port+"["+std::to_string(bit)+"]"))continue;
      auto connected=db.Prepare("SELECT 1 FROM endpoint_records WHERE instance_seq IS NULL AND port_seq=? AND bit=? AND role=0 LIMIT 1");
      connected.BindInteger(1,port_seq);connected.BindInteger(2,bit);
      if(connected.Step())matches.push_back({-1,static_cast<std::uint64_t>(port_seq),bit,SdcName(name)});
    }
  }
  if(kind!="port" && name.rfind('/')!=std::string::npos) {
    const auto slash=name.rfind('/');const auto cell_name=PairedBackslashes(name.substr(0,slash));
    Check(kind.empty() || cell_name.has_value(),"source launch cell escape is not canonical");
    auto cell=db.Prepare("SELECT seq FROM cell_records WHERE id=?");cell.BindText(1,cell_name.value_or(""));
    if(cell_name && cell.Step()) {
      const auto seq=cell.TypedInteger(0);Check(seq>=0 && !cell.Step(),"source launch cell identity is invalid");
      auto pins=db.Prepare("SELECT e.port_seq,p.name,e.bit,(SELECT max(x.bit)+1 FROM endpoint_records x "
          "WHERE x.instance_seq=e.instance_seq AND x.port_seq=e.port_seq) FROM endpoint_records e "
          "JOIN pin_names p ON p.seq=e.port_seq WHERE e.instance_seq=? AND e.role=0 "
          "GROUP BY e.port_seq,e.bit ORDER BY e.port_seq,e.bit");pins.BindInteger(1,seq);
      while(pins.Step()) {
        const auto bit=pins.TypedInteger(2),width=pins.TypedInteger(3);const auto port=pins.TypedText(1);
        Check(bit>=0 && width>bit,"source launch driver coordinate is invalid");
        const auto scalar=width==1?port:port+"["+std::to_string(bit)+"]";
        if(name.substr(slash+1)==scalar)matches.push_back({seq,static_cast<std::uint64_t>(pins.TypedInteger(0)),
            static_cast<std::uint64_t>(bit),SdcName(*cell_name)+"/"+SdcName(width==1?port:port+"__"+std::to_string(bit))});
      }
    }
  }
  if(matches.size()!=1)throw StageError(ErrorCode::kInvalidArtifact,
      "source launch physical object has no unique original driver: "+name.substr(0,4096));
  return matches.front();
}

ArtifactRef BuildSourceLaunchClocks(const ArtifactRef& source,
    const std::vector<ArtifactRef>& logic,const std::vector<ArtifactRef>& local,
    const SourceLaunchContext& context,const std::string& output) {
  for(const auto* digest:{&context.original_ir_sha256,&context.path_database_sha256,&context.routes_sha256,
      &context.schedule_sha256,&context.semantic_sha256,&context.timing_io_sha256})Digest(*digest);
  Check(logic.size()<=256 && local.size()<=256 && !logic.empty(),"source launch physical owner coverage is invalid");
  Check(context.timing_inputs.size()>=8 && context.timing_inputs.size()<=16 &&
        context.timing_input_sha256.size()==context.timing_inputs.size(),"source launch timing input coverage is invalid");
  std::set<std::string> timing_paths;
  for(const auto& ref:context.timing_inputs) {
    const auto seal=context.timing_input_sha256.find(ref.path());
    Check(timing_paths.insert(ref.path()).second && seal!=context.timing_input_sha256.end(),
          "source launch timing input seal is absent or repeats");Digest(seal->second);
  }
  auto inputs=context.timing_inputs;inputs.push_back(source);inputs.insert(inputs.end(),logic.begin(),logic.end());
  inputs.insert(inputs.end(),local.begin(),local.end());
  inputs.insert(inputs.end(),context.control_inputs.begin(),context.control_inputs.end());
  SourceLaunchResolver resolver(source);SqliteStore original(source);
  const auto design=Text(original.database(),"SELECT json_extract(value,'$.name') FROM metadata WHERE key='design_json'");
  const auto top=Text(original.database(),"SELECT json_extract(value,'$.top') FROM metadata WHERE key='design_json'");
  SqliteArtifactWriter writer(output,"source-launch-clock-store-v1",inputs);auto& db=writer.database();
  db.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;"
      "CREATE TABLE sources(source_seq INTEGER PRIMARY KEY,cell_seq INTEGER NOT NULL CHECK(cell_seq>=-1),"
      "port_seq INTEGER NOT NULL CHECK(port_seq>=0),bit INTEGER NOT NULL CHECK(bit>=0),UNIQUE(cell_seq,port_seq,bit));"
      "CREATE TABLE clocks(clock_seq INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE,declared_period_ns REAL NOT NULL CHECK(declared_period_ns>0),engine_period_ns REAL);"
      "CREATE TABLE facts(source_seq INTEGER NOT NULL REFERENCES sources,clock_seq INTEGER NOT NULL REFERENCES clocks,"
      "phase_ns REAL NOT NULL,PRIMARY KEY(source_seq,clock_seq,phase_ns)) WITHOUT ROWID;"
      "CREATE TEMP TABLE requests(kind TEXT NOT NULL,pin TEXT NOT NULL,PRIMARY KEY(kind,pin)) WITHOUT ROWID;");
  std::uint64_t sources=0,facts=0;std::string line;
  StagedArtifact scratch(output,{".requests.tsv",".facts.tsv"});
  const auto requests=scratch.path()+".requests.tsv",raw=scratch.path()+".facts.tsv";
  ToolRequest tool=context.tool;
  Check(tool.argv.size()==4 && std::filesystem::path(tool.argv[0]).is_absolute() &&
        std::filesystem::path(tool.argv[3]).is_absolute() && tool.argv[1]=="-no_init" && tool.argv[2]=="-exit" &&
        std::filesystem::path(tool.argv[3]).filename()=="export_launch_clock_edges.tcl",
        "source launch requires the qualified engine export entry");
  tool.argv[0]=std::filesystem::weakly_canonical(tool.argv[0]).string();
  tool.argv[3]=std::filesystem::weakly_canonical(tool.argv[3]).string();
  std::map<std::string,JsonRecord> timing_seals;
  const auto bind_timing=[&](const std::string& role,const std::string& path) {
    const auto canonical=std::filesystem::weakly_canonical(path).string();
    const auto seal=context.timing_input_sha256.find(canonical);
    Check(seal!=context.timing_input_sha256.end() && timing_paths.count(canonical),"source launch timing dependency has no bound seal");
    timing_seals.emplace(role,seal->second);
  };
  bind_timing("engine",tool.argv[0]);bind_timing("export_script",tool.argv[3]);
  for(const auto* helper:{"design_context.tcl","launch_clock_edges.tcl"})
    bind_timing(helper,(std::filesystem::path(tool.argv[3]).parent_path()/helper).string());
  std::map<std::string,std::string> environment(tool.environment.begin(),tool.environment.end());
  Check(environment.size()==tool.environment.size(),"source launch tool environment repeats");
  for(const auto* key:{"EMUFLOW_STA_LIBERTY","EMUFLOW_STA_VERILOG","EMUFLOW_STA_CLOCKS","EMUFLOW_STA_TIMING_IO"}) {
    const auto found=environment.find(key);Check(found!=environment.end(),"source launch timing context is missing");
    bind_timing(key,found->second);
    found->second=std::filesystem::weakly_canonical(found->second).string();
  }
  environment["EMUFLOW_STA_TOP"]=top;environment["EMUFLOW_LAUNCH_REQUESTS"]=requests;environment["EMUFLOW_LAUNCH_OUTPUT"]=raw;
  tool.environment.assign(environment.begin(),environment.end());
  {
    SqliteTransaction transaction(db);
    auto append=db.Prepare("INSERT OR IGNORE INTO temp.requests VALUES(?,?)");
    std::set<std::string> logic_parts,local_parts;
    for(unsigned group=0;group<2;++group)for(const auto& ref:group?local:logic) {
      Check(ref.schema()==(group?"local-path-timing-store-v3":"logic-segment-timing-store-v3"),"source launch requires current clock-aware physical costs");
      SqliteStore store(ref);auto& costs=store.database();const auto part=Header(costs,"fpga");
      Check((group?local_parts:logic_parts).insert(part).second && Header(costs,"status")=="pass" && Header(costs,"design")==design,
            "source launch physical partition identity differs");
      Check(Header(costs,"sampling_cost_policy")=="vivado-clock-aware-reference-v1","source launch physical cost policy differs");
      if(group)Check(Header(costs,"source.original_ir_sha256")==context.original_ir_sha256 &&
          Header(costs,"source.path_database_sha256")==context.path_database_sha256 &&
          Header(costs,"source.routes_sha256")==context.routes_sha256,"source launch local timing belongs to another source");
      else Check(Header(costs,"system_routes_sha256")==context.routes_sha256 && Header(costs,"tdm_schedule_sha256")==context.schedule_sha256 &&
          Header(costs,"semantic_contract_sha256")==context.semantic_sha256,"source launch complete fanins belong to another run");
      auto rows=costs.Prepare(group?"SELECT '',start_pin,delay_ns,sampling_cost_ns,typeof(delay_ns),typeof(sampling_cost_ns) FROM timings WHERE COALESCE(measurement,'')!='optimized-away' ORDER BY seq":
          "SELECT start_object_kind,start_pin,delay_ns,sampling_cost_ns,typeof(delay_ns),typeof(sampling_cost_ns) FROM segments WHERE kind='launch' "
          "AND system_path GLOB 'static-exact-contract:*' AND measurement!='optimized-away' ORDER BY seq");
      while(rows.Step()) {
        const auto kind=rows.TypedText(0),pin=rows.TypedText(1);
        Check((group || kind=="pin" || kind=="port") && !rows.IsNull(2) && !rows.IsNull(3) &&
            (rows.TypedText(4)=="real" || rows.TypedText(4)=="integer") &&
            (rows.TypedText(5)=="real" || rows.TypedText(5)=="integer") &&
            std::isfinite(rows.Real(2)) && rows.Real(2)>=0 && std::isfinite(rows.Real(3)),"source launch physical query is incomplete");
        append.BindText(1,kind);append.BindText(2,pin);append.Execute();append.Reset();
      }
    }
    Check(logic_parts==local_parts,"source launch physical local/complete-fanin partitions differ");
    auto clock=db.Prepare("INSERT INTO clocks(clock_seq,name,declared_period_ns) VALUES(?,?,?)");
    std::ifstream clock_file(environment.at("EMUFLOW_STA_CLOCKS"));
    Check(Line(clock_file,line) && line=="clock_hex\tperiod_ns","source launch clock header differs");
    unsigned clock_count=0;
    while(Line(clock_file,line)) {
      const auto fields=Fields(line,2);const auto name=Unhex(fields[0]);const auto period=Number(fields[1]);
      Check(period>0 && clock_count<4096,"source launch declared clock is invalid");
      clock.BindInteger(1,clock_count++);clock.BindText(2,name);clock.BindReal(3,period);clock.Execute();clock.Reset();
    }
    Check(clock_count,"source launch declared clocks are missing");
    auto requested=db.Prepare("SELECT kind,pin FROM temp.requests ORDER BY kind,pin");
    auto find=db.Prepare("SELECT source_seq FROM sources WHERE cell_seq=? AND port_seq=? AND bit=?");
    auto insert=db.Prepare("INSERT INTO sources VALUES(?,?,?,?)");
    std::ofstream request_file(requests);request_file<<"source_seq\tpin_hex\n";
    while(requested.Step()) {
      const auto point=resolver.Resolve(requested.TypedText(1),requested.TypedText(0));
      BindPoint(find,point);const bool found=find.Step();find.Reset();if(found)continue;
      Check(sources<UINT32_MAX,"source launch population exceeds coordinate bound");
      insert.BindInteger(1,sources);BindPoint(insert,point,2);insert.Execute();insert.Reset();
      request_file<<sources++<<'\t'<<Hex(point.engine_pin)<<'\n';
    }
    request_file.close();Check(bool(request_file),"cannot close source launch requests");transaction.Commit();
  }
  const auto result=RunExternalTool(tool,context.cancelled,inputs);
  if(!result.operation.ok() || !result.started || result.return_code!=0 || result.timed_out || result.cancelled_signal)
    throw StageError(ErrorCode::kExecutionFailed,"source launch engine export failed: "+
        result.diagnostic_tail.substr(result.diagnostic_tail.size()>4096?result.diagnostic_tail.size()-4096:0));
  {
    SqliteTransaction transaction(db);std::ifstream input(raw);
    Check(Line(input,line) && line=="source_seq\tclock_hex\tperiod_ns\tphase_ns","source launch fact header differs");
    auto lookup=db.Prepare("SELECT clock_seq,declared_period_ns,engine_period_ns FROM clocks WHERE name=?");
    auto clock_update=db.Prepare("UPDATE clocks SET engine_period_ns=? WHERE clock_seq=?");
    auto insert=db.Prepare("INSERT INTO facts VALUES(?,?,?)");
    std::uint64_t covered=0;
    while(Line(input,line)) {
      const auto fields=Fields(line,4);const auto source_index=Index(fields[0]);
      Check(source_index<sources && (source_index==covered || (covered && source_index==covered-1)),
            "source launch fact coverage is missing or unordered");
      if(source_index==covered)++covered;
      const auto period=Number(fields[2]),phase=Number(fields[3]);lookup.BindText(1,Unhex(fields[1]));
      Check(lookup.Step(),"source launch fact uses an undeclared clock");const auto clock=lookup.TypedInteger(0);const auto declared=lookup.Real(1);
      Check(period>0 && std::abs(period-declared)<=1e-6*declared &&
          (lookup.IsNull(2) || lookup.Real(2)==period),"source launch engine period differs");lookup.Reset();
      clock_update.BindReal(1,period);clock_update.BindInteger(2,clock);clock_update.Execute();clock_update.Reset();
      insert.BindInteger(1,source_index);insert.BindInteger(2,clock);insert.BindReal(3,phase);insert.Execute();insert.Reset();++facts;
    }
    Check(covered==sources,"source launch engine omitted requested sources");
    auto clocks=db.Prepare("SELECT count(*) FROM clocks");Check(clocks.Step(),"source launch clocks are absent");
    const auto marker="EMUFLOW_LAUNCH_CLOCK_EXPORT status=pass sources="+std::to_string(sources)+" facts="+std::to_string(facts)+
        " clocks="+std::to_string(clocks.TypedInteger(0));
    std::istringstream diagnostics(result.diagnostic_tail);unsigned completions=0;
    while(std::getline(diagnostics,line))if(line.rfind("EMUFLOW_LAUNCH_CLOCK_EXPORT ",0)==0) {
      Check(line==marker,"source launch terminal counts differ");++completions;
    }
    Check(completions==1,"source launch engine has no unique terminal completion");
    transaction.Commit();
  }
  {
    const JsonRecord identity(std::map<std::string,JsonRecord>{{"original_ir_sha256",context.original_ir_sha256},
        {"path_database_sha256",context.path_database_sha256},{"routes_sha256",context.routes_sha256},
        {"schedule_sha256",context.schedule_sha256},{"semantic_sha256",context.semantic_sha256},{"timing_io_sha256",context.timing_io_sha256}});
    auto put=db.Prepare("INSERT INTO metadata VALUES(?,?)");
    for(const auto& item:std::map<std::string,std::string>{{"schema","emuflow.source-launch-clocks/v1"},{"source",identity.Encode()},
        {"timing_inputs",JsonRecord(timing_seals).Encode()},
        {"sources",std::to_string(sources)},{"facts",std::to_string(facts)}}) {
      put.BindText(1,item.first);put.BindText(2,item.second);put.Execute();put.Reset();
    }
  }
  resolver.RequireUnchanged();return writer.Commit();
}

namespace {
struct LaunchRequest {
  std::uint32_t logic,local,timing;
  SourceLaunchContext context;
  std::vector<FileGeneration> generations;
  std::vector<std::string> timing_seals;
};
LaunchRequest ReadLaunchRequest(const std::string& path) {
  CheckedRequestReader input(path,1024*1024);
  Check(input.Read<std::uint64_t>()==0x3151424c534d5545ULL && input.Read<std::uint32_t>()==1,
        "source launch request version differs");
  LaunchRequest request;request.logic=input.Read<std::uint32_t>();request.local=input.Read<std::uint32_t>();
  request.timing=input.Read<std::uint32_t>();
  Check(request.logic && request.logic<=256 && request.local==request.logic && request.timing>=8 && request.timing<=16,
        "source launch request owner counts differ");
  for(auto* field:{&request.context.original_ir_sha256,&request.context.path_database_sha256,&request.context.routes_sha256,
      &request.context.schedule_sha256,&request.context.semantic_sha256,&request.context.timing_io_sha256}) {
    *field=input.ReadString32(false,64);Digest(*field);
  }
  for(std::size_t i=0;i<2+request.logic+request.local+request.timing;++i) {
    FileGeneration generation;
    generation.device=input.Read<std::uint64_t>();generation.inode=input.Read<std::uint64_t>();generation.bytes=input.Read<std::uint64_t>();
    generation.modified_seconds=input.Read<std::int64_t>();generation.modified_nanoseconds=input.Read<std::int64_t>();
    generation.changed_seconds=input.Read<std::int64_t>();generation.changed_nanoseconds=input.Read<std::int64_t>();
    request.generations.push_back(generation);
  }
  for(unsigned i=0;i<request.timing;++i) {
    auto seal=input.ReadString32(false,64);Digest(seal);request.timing_seals.push_back(std::move(seal));
  }
  input.RequireEnd();return request;
}
}  // namespace
std::vector<std::string> SourceLaunchInputSchemas(const std::string& path) {
  const auto request=ReadLaunchRequest(path);
  std::vector<std::string> schemas{"emuir-compact-v2","source-launch-build-request-v1","external-tool-request-v1"};
  schemas.insert(schemas.end(),request.logic,"logic-segment-timing-store-v3");
  schemas.insert(schemas.end(),request.local,"local-path-timing-store-v3");
  schemas.insert(schemas.end(),request.timing,"opensta-timing-input-v1");return schemas;
}
void BuildSourceLaunchClocksFromRequest(const std::vector<ArtifactRef>& inputs,const std::string& output,
    const volatile std::sig_atomic_t* cancelled) {
  Check(inputs.size()>=3,"source launch request inputs are missing");auto request=ReadLaunchRequest(inputs[1].path());
  Check(inputs.size()==3+request.logic+request.local+request.timing,"source launch request input coverage differs");
  for(std::size_t i=0;i<request.generations.size();++i)
    if(!(request.generations[i]==inputs[i?i+1:0].generation()))
      throw StageError(ErrorCode::kMixedGeneration,"source launch prepared input generation changed");
  auto& context=request.context;context.tool=ReadToolRequest(inputs[2].path());context.control_inputs={inputs[1],inputs[2]};
  context.cancelled=cancelled;
  const auto first=inputs.begin()+3;
  const std::vector<ArtifactRef> logic(first,first+request.logic),local(first+request.logic,first+request.logic+request.local);
  context.timing_inputs.assign(first+request.logic+request.local,inputs.end());
  for(std::size_t i=0;i<context.timing_inputs.size();++i)
    context.timing_input_sha256.emplace(context.timing_inputs[i].path(),request.timing_seals[i]);
  BuildSourceLaunchClocks(inputs[0],logic,local,context,output);
}

SourceLaunchClockView::SourceLaunchClockView(const ArtifactRef& clocks,const ArtifactRef& source,const std::string& seal)
    :clocks_(clocks),source_(source) {
  Check(clocks.schema()=="source-launch-clock-store-v1","source launch clock owner schema differs");Digest(seal);
  Check(Text(clocks_.database(),"SELECT json_extract(value,'$.original_ir_sha256') FROM metadata WHERE key='source'")==seal,
        "source launch clock owner belongs to another original design");
}
void SourceLaunchClockView::RequireUnchanged() const {clocks_.RequireUnchanged();source_.RequireUnchanged();}
std::vector<double> SourceLaunchClockView::LaunchTimes(const std::string& pin,const std::string& kind,
    const std::map<std::string,double>& targets) {
  std::vector<double> result;
  for(const auto& fact:Facts(pin,kind,targets))result.push_back(fact.launch_ns);
  return result;
}
void SourceLaunchClockView::RequireSourceIdentity(const SourceLaunchIdentity& identity) {
  auto row=clocks_.database().Prepare("SELECT json_extract(value,?1) FROM metadata WHERE key='source'");
  for(const auto& item:std::map<std::string,std::string>{{"original_ir_sha256",identity.original_ir_sha256},
      {"path_database_sha256",identity.path_database_sha256},{"routes_sha256",identity.routes_sha256},
      {"schedule_sha256",identity.schedule_sha256},{"semantic_sha256",identity.semantic_sha256},
      {"timing_io_sha256",identity.timing_io_sha256}}) {
    Digest(item.second);row.BindText(1,"$."+item.first);
    Check(row.Step() && row.TypedText(0)==item.second,"source launch context belongs to another run");row.Reset();
  }
}
void SourceLaunchClockView::RequireOwner(const SourceLaunchFact& fact) const {
  clocks_.RequireOwner(fact.source);clocks_.RequireOwner(fact.clock);
}
std::vector<SourceLaunchFact> SourceLaunchClockView::Facts(const std::string& pin,const std::string& kind,
    const std::map<std::string,double>& targets) {
  const auto point=source_.Resolve(pin,kind);auto& db=clocks_.database();
  auto rows=db.Prepare("SELECT c.name,c.engine_period_ns,f.phase_ns,s.source_seq,c.clock_seq,typeof(c.engine_period_ns),typeof(f.phase_ns) "
      "FROM sources s JOIN facts f ON f.source_seq=s.source_seq "
      "JOIN clocks c ON c.clock_seq=f.clock_seq WHERE s.cell_seq=? AND s.port_seq=? AND s.bit=? ORDER BY c.clock_seq,f.phase_ns");
  BindPoint(rows,point);std::vector<SourceLaunchFact> result;
  while(rows.Step()) {
    const auto target=targets.find(rows.TypedText(0));
    Check(target!=targets.end() && std::isfinite(target->second) && target->second>0 && !rows.IsNull(1),
          "source launch target clock is missing or invalid");
    const auto period=rows.Real(1),phase=rows.Real(2);
    Check((rows.TypedText(5)=="real" || rows.TypedText(5)=="integer") &&
          (rows.TypedText(6)=="real" || rows.TypedText(6)=="integer") &&
          std::isfinite(period) && period>0 && std::isfinite(phase),"source launch stored clock fact is invalid");
    const auto launch=(phase/period)*target->second;
    Check(std::isfinite(launch) && rows.TypedInteger(3)>=0 && rows.TypedInteger(4)>=0,
          "source launch target phase or identity is unrepresentable");
    result.push_back({clocks_.Id<LaunchSourceTag>(rows.Integer(3)),clocks_.Id<LaunchClockTag>(rows.Integer(4)),period,phase,launch});
  }
  Check(!result.empty(),"source launch original driver has no clock facts");return result;
}
}  // namespace emuflow::core
