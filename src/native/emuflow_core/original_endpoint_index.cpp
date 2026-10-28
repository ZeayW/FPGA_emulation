#include "emuflow_core/original_endpoint_index.hpp"
#include "emuflow_core/original_timing.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/json_record.hpp"
#include <array>
#include <limits>
#include <memory>
#include <set>

namespace emuflow::core {
namespace {
constexpr std::uint64_t kMagic=0x3151454f4e4d5545ULL;
void Check(bool ok,const char* message){if(!ok)throw StageError(ErrorCode::kInvalidArtifact,message);}
struct Request {
  std::uint32_t parts,controls;
  std::vector<FileGeneration> generations;
  std::string selector_sha256,context;
  std::vector<std::string> names,schemas;
};
Request Read(const std::string& path){
  CheckedRequestReader reader(path,1024*1024);Request r;
  Check(reader.Read<std::uint64_t>()==kMagic && reader.Read<std::uint32_t>()==1,"original endpoint index request version differs");
  r.parts=reader.Read<std::uint32_t>();r.controls=reader.Read<std::uint32_t>();
  Check(r.parts>0 && r.parts<=64 && r.controls<=16,"original endpoint index source coverage exceeds its bound");
  for(std::uint32_t i=0;i<4+r.parts+r.controls;++i)
    r.generations.push_back({reader.Read<std::uint64_t>(),reader.Read<std::uint64_t>(),reader.Read<std::uint64_t>(),
        reader.Read<std::int64_t>(),reader.Read<std::int64_t>(),reader.Read<std::int64_t>(),reader.Read<std::int64_t>()});
  r.selector_sha256=reader.ReadString32();r.context=reader.ReadString32();
  for(std::uint32_t i=0;i<r.parts;++i)r.names.push_back(reader.ReadString32());
  const std::set<std::string> allowed{"mapped-normalization-proof-v1","emuir-compact-v2","yosys-source-document-v1","physical-input-document-v1"};
  for(std::uint32_t i=0;i<r.controls;++i){auto s=reader.ReadString32();Check(allowed.count(s),"original endpoint control source schema is unsupported");r.schemas.push_back(std::move(s));}
  reader.RequireEnd();return r;
}
std::string Budget(const OriginalClockBudget& b){
  return JsonRecord(std::map<std::string,JsonRecord>{
    {"source_clock",b.source_clock},{"target_clock",b.target_clock},{"source_period_ns",b.source_period_ns},
    {"target_period_ns",b.target_period_ns},{"launch_edge_ns",b.launch_edge_ns},{"capture_edge_ns",b.capture_edge_ns},
    {"source_offset_ns",b.source_offset_ns},{"target_cycle_time_ns",b.target_cycle_time_ns},
    {"multicycle_adjustment_ns",b.multicycle_adjustment_ns},{"check_margin_ns",b.check_margin_ns},
    {"source_latency_ns",b.source_latency_ns},{"target_clock_delay_ns",b.target_clock_delay_ns},
    {"clock_skew_ns",b.clock_skew_ns},{"output_delay",b.output_delay}}).Encode();
}
JsonRecord Parents(const std::vector<ArtifactRef>& inputs){
  std::vector<JsonRecord> result;
  for(std::size_t i=0;i<inputs.size();++i){
    // The request is invocation-local control; durable parents are the
    // original stores, mapped owners and existing admission receipts.
    if(i==4)continue;
    const auto& input=inputs[i];
    const auto& g=input.generation();std::vector<JsonRecord> generation{g.device,g.inode,g.bytes,g.modified_seconds,
      g.modified_nanoseconds,g.changed_seconds,g.changed_nanoseconds};
    result.emplace_back(std::map<std::string,JsonRecord>{{"path",input.path()},{"schema",input.schema()},{"generation",JsonRecord(std::move(generation))}});
  }
  return JsonRecord(std::move(result));
}
}
std::vector<std::string> OriginalEndpointInputSchemas(const std::string& request){
  const auto r=Read(request);
  std::vector<std::string> schemas{"sta-path-selector-v1","timing-path-store-v2","emuir-compact-v2","assignment-index-v4","native-original-endpoint-request-v1"};
  schemas.resize(5+r.parts,"xilinx-mapped-index-v2");schemas.insert(schemas.end(),r.schemas.begin(),r.schemas.end());return schemas;
}
void BuildOriginalEndpointIndex(const std::vector<ArtifactRef>& inputs,const std::string& output){
  Check(inputs.size()>=6,"original endpoint index input coverage is incomplete");const auto r=Read(inputs[4].path());
  Check(inputs.size()==5+r.parts+r.controls,"original endpoint index input coverage differs");
  for(std::size_t i=0,j=0;i<inputs.size();++i){
    inputs[i].RequireUnchanged();if(i==4)continue;
    Check(inputs[i].generation()==r.generations[j++],"original endpoint input generation differs from its request");
  }
  OriginalTimingView original(inputs[0],inputs[1],r.selector_sha256);
  EmuirView source(inputs[2]);auto& source_db=source.store().database();
  auto context=source_db.Prepare("SELECT json_type(?)");context.BindText(1,r.context);
  Check(context.Step() && context.TypedText(0)=="object","original endpoint source context is not an object");context=SqliteStatement();
  AssignmentView assignment(inputs[3]);
  Check(assignment.partitions()==r.parts && assignment.source_cells()==source.counts().cells,
      "original endpoint assignment source population differs");
  for(std::uint32_t i=0;i<r.parts;++i)Check(assignment.PartitionName(assignment.Partition(i))==r.names[i],
      "original endpoint partition catalog differs");
  std::vector<std::uint8_t> states(source.counts().cells),seen(source.counts().cells);
  source.VisitCellStates([&](const auto& id,bool state){states[id.sequence()]=state;});
  std::vector<std::unique_ptr<SqliteStore>> mapped;
  std::vector<SqliteStatement> mapped_ids;
  for(std::uint32_t i=0;i<r.parts;++i){mapped.push_back(std::make_unique<SqliteStore>(inputs[5+i]));
    mapped_ids.push_back(mapped.back()->database().Prepare("SELECT seq FROM cells WHERE id=?"));}
  SqliteArtifactWriter writer(output,"native-original-endpoint-index-v1",inputs);auto& db=writer.database();
  db.Execute("PRAGMA cache_size=-65536;"
    "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;"
    "CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT UNIQUE NOT NULL);"
    "CREATE TABLE cell_owners(seq INTEGER PRIMARY KEY,source_cell_seq INTEGER NOT NULL,assignment_cell_seq INTEGER NOT NULL,"
      "part_index INTEGER NOT NULL,mapped_cell_seq INTEGER,is_replica INTEGER NOT NULL);"
    "CREATE INDEX source_cell_owners ON cell_owners(source_cell_seq,part_index);"
    "CREATE TABLE endpoints(seq INTEGER PRIMARY KEY,role INTEGER NOT NULL,source_cell_seq INTEGER NOT NULL,"
      "source_port_seq INTEGER NOT NULL,bit INTEGER NOT NULL,source_kind TEXT NOT NULL,engine_example TEXT NOT NULL,"
      "UNIQUE(role,source_cell_seq,source_port_seq,bit));"
    "CREATE TABLE budgets(seq INTEGER PRIMARY KEY,record_json TEXT UNIQUE NOT NULL);"
    "CREATE TABLE members(ordinal INTEGER PRIMARY KEY,start_endpoint INTEGER NOT NULL,end_endpoint INTEGER NOT NULL,budget INTEGER NOT NULL);"
    "CREATE INDEX member_endpoints ON members(end_endpoint,budget);");
  std::uint64_t copies=0,missing=0,endpoints=0,budgets=0,members=0,state_members=0,port_members=0,comb_members=0;
  {
    SqliteTransaction transaction(db);
    auto part=db.Prepare("INSERT INTO parts VALUES(?,?)");
    for(std::uint32_t i=0;i<r.parts;++i){part.BindInteger(1,i);part.BindText(2,r.names[i]);part.Execute();part.Reset();}
    auto find_cell=source_db.Prepare("SELECT seq FROM cell_records WHERE id=?");
    std::vector<std::uint64_t> primary(source.counts().cells,UINT64_MAX);
    auto owner=db.Prepare("INSERT INTO cell_owners VALUES(?,?,?,?,?,?)");
    assignment.VisitCopies([&](const auto& copy){
      const auto original_id=copy.source.sequence();std::uint64_t normalized;
      if(!copy.replica){
        find_cell.BindText(1,copy.identity);Check(find_cell.Step(),"assignment primary cell is absent from the timing source");
        const auto found=find_cell.TypedInteger(0);Check(found>=0 && std::uint64_t(found)<seen.size() && !seen[found] && !find_cell.Step(),
            "assignment primary cell identity repeats or is out of range");find_cell.Reset();seen[found]=1;normalized=found;primary[original_id]=normalized;
      }else{normalized=primary[original_id];Check(normalized<seen.size(),"assignment replica precedes its primary source");}
      auto& query=mapped_ids.at(copy.partition.sequence());query.BindText(1,copy.identity);
      owner.BindInteger(1,copies++);owner.BindInteger(2,normalized);owner.BindInteger(3,original_id);owner.BindInteger(4,copy.partition.sequence());
      if(query.Step()){const auto id=query.TypedInteger(0);Check(id>=0 && !query.Step(),"mapped source identity repeats or is invalid");owner.BindInteger(5,id);}
      else{owner.BindNull(5);++missing;}query.Reset();owner.BindInteger(6,copy.replica);owner.Execute();owner.Reset();
    });
    for(const auto value:seen)Check(value==1,"assignment omits an original timing source cell");
    primary.clear();primary.shrink_to_fit();seen.clear();seen.shrink_to_fit();
    auto cell=source_db.Prepare("SELECT seq FROM cell_records WHERE id=?");
    auto pin=source_db.Prepare("SELECT e.port_seq FROM endpoint_records e JOIN pin_names p ON p.seq=e.port_seq "
      "WHERE e.instance_seq IS ?1 AND p.name=?2 AND e.bit=?3 AND e.role=?4 LIMIT 2");
    auto lookup=db.Prepare("SELECT seq,source_kind FROM endpoints WHERE role=? AND source_cell_seq=? AND source_port_seq=? AND bit=?");
    auto insert=db.Prepare("INSERT INTO endpoints VALUES(?,?,?,?,?,?,?)");
    const auto endpoint=[&](const OriginalEndpoint& point,bool launch){
      std::int64_t cell_id=-1;
      if(point.instance){cell.BindText(1,*point.instance);Check(cell.Step(),"original timing endpoint cell is absent from its source");cell_id=cell.TypedInteger(0);
        Check(cell_id>=0 && std::uint64_t(cell_id)<states.size() && !cell.Step(),"original timing endpoint cell identity is invalid");cell.Reset();}
      if(cell_id<0)pin.BindNull(1);else pin.BindInteger(1,cell_id);
      Check(point.bit<=INT64_MAX,"original endpoint bit exceeds its coordinate domain");
      pin.BindText(2,point.port);pin.BindInteger(3,point.bit);pin.BindInteger(4,launch?0:1);
      Check(pin.Step(),"original timing endpoint is not connected in its source");const auto port=pin.TypedInteger(0);
      Check(port>=0 && !pin.Step(),"original source endpoint has multiple connected projections");pin.Reset();
      const std::string kind=cell_id<0?(launch?"top-input":"top-output"):
        (states[cell_id]?(launch?"state-output":"state-input"):(launch?"cell-output":"cell-input"));
      lookup.BindInteger(1,launch?0:1);lookup.BindInteger(2,cell_id);lookup.BindInteger(3,port);lookup.BindInteger(4,point.bit);
      std::uint64_t id;
      if(lookup.Step()){const auto found=lookup.TypedInteger(0);Check(found>=0 && lookup.TypedText(1)==kind && !lookup.Step(),"original endpoint coordinate ownership differs");id=found;}
      else{Check(endpoints<INT64_MAX,"original endpoint population exceeds its domain");id=endpoints++;
        insert.BindInteger(1,id);insert.BindInteger(2,launch?0:1);insert.BindInteger(3,cell_id);insert.BindInteger(4,port);
        insert.BindInteger(5,point.bit);insert.BindText(6,kind);insert.BindText(7,point.engine_object);insert.Execute();insert.Reset();}
      lookup.Reset();return std::make_pair(id,kind);
    };
    auto get_budget=db.Prepare("SELECT seq FROM budgets WHERE record_json=?");auto put_budget=db.Prepare("INSERT INTO budgets VALUES(?,?)");
    auto member=db.Prepare("INSERT INTO members VALUES(?,?,?,?)");
    std::string previous_budget;std::uint64_t previous_id=0;
    original.Visit([&](const auto& path){
      original.RequireOwner(path.source);const auto start=endpoint(path.start,true),end=endpoint(path.end,false);
      const auto json=Budget(path.budget);std::uint64_t budget;
      if(json==previous_budget)budget=previous_id;
      else{get_budget.BindText(1,json);if(get_budget.Step()){const auto id=get_budget.TypedInteger(0);Check(id>=0 && !get_budget.Step(),"original clock budget profile repeats");budget=id;}
        else{budget=budgets++;put_budget.BindInteger(1,budget);put_budget.BindText(2,json);put_budget.Execute();put_budget.Reset();}
        get_budget.Reset();previous_budget=json;previous_id=budget;}
      Check(path.source.sequence()==members,"original source member order differs");
      member.BindInteger(1,members++);member.BindInteger(2,start.first);member.BindInteger(3,end.first);member.BindInteger(4,budget);member.Execute();member.Reset();
      if(end.second=="top-output")++port_members;else if(end.second=="state-input")++state_members;else ++comb_members;
    });
    Check(members==original.count(),"original endpoint member population is incomplete");
    auto metadata=db.Prepare("INSERT INTO metadata VALUES(?,?)");
    const auto put=[&](const std::string& key,const std::string& value){metadata.BindText(1,key);metadata.BindText(2,value);metadata.Execute();metadata.Reset();};
    put("schema","emuflow.native-original-endpoint-index/v1");put("status","source-endpoints-indexed");
    put("source_context",r.context);put("source_generations",Parents(inputs).Encode());put("selector_sha256",JsonQuote(r.selector_sha256));
    put("members",std::to_string(members));put("source_cells",std::to_string(states.size()));put("copies",std::to_string(copies));
    put("missing_mapped_copies",std::to_string(missing));put("endpoints",std::to_string(endpoints));put("budgets",std::to_string(budgets));
    put("state_members",std::to_string(state_members));put("output_members",std::to_string(port_members));put("cell_input_members",std::to_string(comb_members));
    put("physical_acceptance","pending");put("original_path_sta_acceptance","pending");
    put("scope","Every original selected timing member retains source coordinates, complete clock budgets and mapped-copy owners. Native objects, transformations, eliminated records and numerical physical/global timing remain separate. Missing mapped tags are not removal certificates.");
    transaction.Commit();
  }
  original.RequireUnchanged();source.store().RequireUnchanged();for(const auto& input:inputs)input.RequireUnchanged();
  writer.Commit();
}
}
