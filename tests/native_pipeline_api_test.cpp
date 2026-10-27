#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/tool_runtime.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/routed_opensta.hpp"
#include "emuflow_core/json_record.hpp"
#include "emuflow_core/phase5.hpp"
#include "emuflow_core/runtime_calendar.hpp"
#include <iostream>
#include <unistd.h>
#include <sqlite3.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <stdexcept>

int main(int argc,char** argv) {
  if(argc==5 && std::string(argv[1])=="runtime") {
    using namespace emuflow::core;
    try {
      std::cout<<BuildVirtualRuntime(ArtifactRef::Bind(argv[2],"tdm-schedule-store-v4"),
        ArtifactRef::Bind(argv[3],"boarddb-v1"),argv[4])<<'\n';return 0;
    } catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
  }
  if(argc==4 && std::string(argv[1])=="split-view") {
    using namespace emuflow::core;
    auto ref=ArtifactRef::Bind(argv[2],"split-netlists-v3");SplitView view(ref);
    const auto partition=view.Partition(std::stoull(argv[3]));std::uint64_t copies=0,endpoints=0;
    ArtifactScope owner(ref);
    view.VisitCopies(partition,[&](const InstanceCopy& row){owner.RequireOwner(row.source);++copies;});
    view.VisitEndpoints(partition,[&](const SplitEndpoint& row){owner.RequireOwner(row.net);++endpoints;});
    std::cout<<"["<<copies<<","<<endpoints<<"]\n";return 0;
  }
  if(argc==9 && std::string(argv[1])=="phase5-fault") {
    using namespace emuflow::core;
    const std::string mode=argv[2],point=argv[3];unsigned published=0;
    try {
      RunPhase5({ArtifactRef::Bind(argv[4],"system-route-store-v3"),
          ArtifactRef::Bind(argv[5],"exact-schedule-board-request-v1"),
          ArtifactRef::Bind(argv[6],"exact-schedule-generation-request-v1"),
          ArtifactRef::Bind(argv[7],"phase5-stage-request-v1")},argv[8],[&](const std::string& stage) {
        if(stage.rfind("published:",0)==0)++published;
        if(stage!=point && !(stage.rfind("published:",0)==0 && std::to_string(published)==point))return;
        if(mode=="corrupt") {
          namespace fs=std::filesystem;
          for(const auto& entry:fs::directory_iterator(fs::path(argv[8]).parent_path())) {
            const auto path=entry.path()/"schedule.sqlite";
            if(entry.path().filename().string().rfind(".phase5-generation-",0)!=0 || !fs::exists(path))continue;
            sqlite3* database=nullptr;
            if(sqlite3_open(path.c_str(),&database)!=SQLITE_OK)throw std::runtime_error("cannot corrupt test store");
            const auto status=sqlite3_exec(database,"UPDATE schedule_entries SET slot=slot+1 WHERE seq=0",nullptr,nullptr,nullptr);
            sqlite3_close(database);
            if(status!=SQLITE_OK)throw std::runtime_error("cannot update test store");
            return;
          }
          throw std::runtime_error("test stage did not expose private schedule");
        }
        if(mode=="exit")::_exit(72);
        throw std::runtime_error("injected native Phase5 failure at "+stage);
      });
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 71;}
    return 0;
  }
  namespace fs = std::filesystem;
  using emuflow::core::ArtifactRef;
  using emuflow::core::ErrorCode;
  using emuflow::native::Stage;
  using emuflow::native::StageRequest;
  const auto unique = std::chrono::steady_clock::now().time_since_epoch().count();
  const auto root = fs::temp_directory_path() / ("emuflow-pipeline-api-" + std::to_string(unique));
  if (!fs::create_directory(root)) throw std::runtime_error("test directory already exists");
  try {
    // Validate final routed records independently from their producer decisions.
    for(unsigned damage=0;damage<11;++damage) {
      using emuflow::core::SqliteDatabase;
      SqliteDatabase db(":memory:",SqliteDatabase::Mode::kCreateExclusive);
      db.Execute("CREATE TABLE cell_records(seq INTEGER PRIMARY KEY,id TEXT,type TEXT,attributes_seq INTEGER); CREATE TABLE pin_names(seq INTEGER PRIMARY KEY,name TEXT); CREATE TABLE endpoint_records(net_seq INTEGER,role INTEGER,ordinal INTEGER,instance_seq INTEGER,port_seq INTEGER,bit INTEGER); CREATE INDEX endpoint_owner ON endpoint_records(instance_seq,port_seq,bit); CREATE TABLE routed_delay_types(seq INTEGER PRIMARY KEY,attributes_seq INTEGER); CREATE TABLE payloads(seq INTEGER PRIMARY KEY,value TEXT); INSERT INTO payloads VALUES(1,'{\"emuflow_route_delay_ns\":0.123}'); INSERT INTO cell_records VALUES(0,'delay','EMUFLOW_RW_ROUTE_DELAY_000000',1); INSERT INTO pin_names VALUES(0,'A'),(1,'Y'); INSERT INTO endpoint_records VALUES(0,1,0,0,0,0),(1,0,0,0,1,0); INSERT INTO routed_delay_types VALUES(0,1)");
      if(damage==1)db.Execute("DELETE FROM endpoint_records WHERE role=1");
      if(damage==2)db.Execute("DELETE FROM endpoint_records WHERE role=0");
      if(damage==3)db.Execute("UPDATE endpoint_records SET net_seq=0");
      if(damage==4)db.Execute("DELETE FROM cell_records");
      if(damage==5)db.Execute("DELETE FROM routed_delay_types");
      if(damage==6)db.Execute("UPDATE cell_records SET attributes_seq=2");
      if(damage==7)db.Execute("UPDATE cell_records SET type='EMUFLOW_RW_ROUTE_DELAY_000001'");
      if(damage==8)db.Execute("DELETE FROM payloads");
      if(damage==9)db.Execute("UPDATE payloads SET value='{}'");
      if(damage==10)db.Execute("UPDATE payloads SET value='{\"emuflow_route_delay_ns\":-1}'");
      bool rejected=false;
      try {emuflow::core::ValidateRoutedOpenstaGraph(db,1,1);}
      catch(const emuflow::core::StageError& e){if(e.code()!=ErrorCode::kInvalidArtifact)throw;rejected=true;}
      if(rejected!=(damage!=0))throw std::runtime_error("native routed final-record validator accepted corruption/rejected valid graph");
    }
    {
      using emuflow::core::SqliteDatabase;
      const auto mapped=(root/"opensta-mapped.sqlite").string(),timing=(root/"opensta-timing.json").string(),endpoints=(root/"opensta-endpoints.jsonl").string(),configuration=(root/"opensta-request.bin").string();
      {SqliteDatabase db(mapped,SqliteDatabase::Mode::kCreateExclusive);
        db.Execute(R"SQL(CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT); INSERT INTO metadata VALUES('schema','emuflow.xilinx-mapped-index/v2'),('top_json','"empty"'),('creator_json','"fixture"'),('ports_json','{}'); CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT UNIQUE,type TEXT,record_json TEXT); CREATE TABLE netnames(seq INTEGER PRIMARY KEY,id TEXT UNIQUE); CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,constant TEXT))SQL");}
      const auto digest=[](const std::string& path){std::ifstream in(path,std::ios::binary);std::string value((std::istreambuf_iterator<char>(in)),{});emuflow::core::Sha256 hash;hash.update(reinterpret_cast<const unsigned char*>(value.data()),value.size());std::ostringstream out;out<<std::hex<<std::setfill('0');for(auto b:hash.finish())out<<std::setw(2)<<unsigned(b);return out.str();};
      {std::ofstream out(endpoints);}
      const auto mapped_sha=digest(mapped);
      {std::ofstream out(timing);out<<"{\"schema\":\"emuflow.xilinx-routed-timing/v2\",\"status\":\"pass\",\"top\":\"empty\",\"source\":{\"mapped_sha256\":\""<<mapped_sha<<"\",\"packed_sha256\":\""<<mapped_sha<<"\",\"placement_sha256\":\""<<mapped_sha<<"\",\"route_sha256\":\""<<mapped_sha<<R"JSON("},"qualification":{"logic_coefficients_ps":{"ff_clock_to_q":25,"carry_co":10,"lut_a1":70,"lut_a2":65,"lut_a3":60,"lut_a4":55,"lut_a5":50,"lut_a6":45}},"summary":{"logical_endpoints":0,"maximum_route_delay_ns":0},"payloads":{"endpoints":{"format":"jsonl-object/v1","records":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}}})JSON";}
      std::vector<ArtifactRef> inputs{ArtifactRef::Bind(mapped,"xilinx-mapped-index-v2"),ArtifactRef::Bind(timing,"xilinx-routed-timing-v2"),ArtifactRef::Bind(endpoints,"xilinx-routed-endpoints-v1")};
      {std::ofstream out(configuration,std::ios::binary);std::uint32_t crc=0;
        const auto put=[&](const auto& value){out.write(reinterpret_cast<const char*>(&value),sizeof(value));emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
        put(std::uint64_t{0x314f5352464d5545ULL});put(std::uint32_t{1});
        for(const auto& input:inputs){const auto& g=input.generation();put(g.device);put(g.inode);put(g.bytes);put(g.modified_seconds);put(g.modified_nanoseconds);put(g.changed_seconds);put(g.changed_nanoseconds);}
        const auto value="{\"base_types\":[\"LUT1\"],\"clock_count\":0,\"clocks\":{},\"source_path\":"+emuflow::core::JsonQuote(mapped)+",\"mapped_sha256\":\""+mapped_sha+"\",\"timing_sha256\":\""+digest(timing)+"\"}";
        put(std::uint32_t(value.size()));out.write(value.data(),value.size());emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(value.data()),value.size());out.write(reinterpret_cast<const char*>(&crc),sizeof(crc));}
      inputs.push_back(ArtifactRef::Bind(configuration,"routed-opensta-request-v1"));
      StageRequest stage;stage.stage=Stage::kStageRoutedOpensta;stage.input_artifacts=inputs;stage.output_path=(root/"opensta-output.sqlite").string();stage.auxiliary_output_path=(root/"opensta-delays.lib").string();stage.scratch_path=(root/"opensta-summary.json").string();
      const auto result=emuflow::native::ExecuteStage(stage);
      if(!result.ok() || result.inputs.size()!=4 || result.outputs.size()!=3 || result.outputs[0].schema()!="emuir-compact-v2" || result.outputs[1].schema()!="routed-opensta-liberty-v1")throw std::runtime_error("bound routed OpenSTA stage failed: "+result.message);
      auto alias=stage;alias.auxiliary_output_path=endpoints;
      if(emuflow::native::ExecuteStage(alias).error!=ErrorCode::kInvalidRequest)throw std::runtime_error("routed OpenSTA library overwrote an input");
      for(const auto& input:inputs)input.RequireUnchanged();for(const auto& output:result.outputs)output.RequireUnchanged();
      const auto export_config=(root/"timing-export.bin").string();
      {std::ofstream out(export_config,std::ios::binary);std::uint32_t crc=0;
        const auto put=[&](const auto& value){out.write(reinterpret_cast<const char*>(&value),sizeof(value));emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
        put(std::uint64_t{0x31455853464d5545ULL});put(std::uint32_t{1});const auto& g=result.outputs[0].generation();
        put(g.device);put(g.inode);put(g.bytes);put(g.modified_seconds);put(g.modified_nanoseconds);put(g.changed_seconds);put(g.changed_nanoseconds);put(std::uint32_t{0});put(std::uint32_t{3});out.write(reinterpret_cast<const char*>(&crc),sizeof(crc));}
      StageRequest exporter;exporter.stage=Stage::kExportOpenstaInputs;
      exporter.input_artifacts={result.outputs[0],ArtifactRef::Bind(export_config,"opensta-input-export-request-v1")};
      exporter.output_path=(root/"timing-export-summary.json").string();exporter.auxiliary_output_path=(root/"timing-export.v").string();exporter.scratch_path=(root/"timing-export.tsv").string();
      const auto exported=emuflow::native::ExecuteStage(exporter);
      if(!exported.ok() || exported.inputs.size()!=2 || exported.outputs.size()!=3 || exported.outputs[1].schema()!="opensta-timing-verilog-v1" || exported.outputs[2].schema()!="opensta-net-map-v1")throw std::runtime_error("bound OpenSTA exporter failed: "+exported.message);
      auto export_alias=exporter;export_alias.auxiliary_output_path=result.outputs[0].path();
      if(emuflow::native::ExecuteStage(export_alias).error!=ErrorCode::kInvalidRequest)throw std::runtime_error("OpenSTA exporter overwrote its source");
    }
    emuflow::core::ToolRequest tool;
    tool.argv = {"/bin/echo", "native tool API"};
    tool.working_directory = root.string();
    const auto engine = emuflow::core::RunExternalTool(tool);
    if (!engine.operation.ok() || engine.return_code || engine.diagnostic_tail != "native tool API\n" ||
        engine.operation.inputs.size() != 1 || !engine.started)
      throw std::runtime_error("shared external tool lifecycle failed");
    volatile std::sig_atomic_t cancelled = SIGTERM;
    const auto stopped = emuflow::core::RunExternalTool(tool, &cancelled);
    if (stopped.operation.ok() || stopped.started || stopped.cancelled_signal != SIGTERM)
      throw std::runtime_error("native compiler cancellation launched a tool");
    {
      using emuflow::core::SqliteDatabase;
      const auto mapped=(root/"vivado mapped ?#%.sqlite").string();
      const auto configuration=(root/"vivado-export-request.bin").string();
      {
        SqliteDatabase db(mapped,SqliteDatabase::Mode::kCreateExclusive);
        db.Execute(R"SQL(
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO metadata VALUES('schema','emuflow.xilinx-mapped-index/v2'),
            ('top_json','"empty"'),('ports_json','{}');
          CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT,type TEXT,record_json TEXT);
          CREATE TABLE netnames(seq INTEGER PRIMARY KEY);
          CREATE TABLE cell_pins(cell_seq INTEGER,cell_id TEXT,port TEXT,port_bit INTEGER,
            port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,constant TEXT);
        )SQL");
      }
      const auto ref=ArtifactRef::Bind(mapped,"xilinx-mapped-index-v2");
      {
        std::ofstream out(configuration,std::ios::binary);std::uint32_t crc=0;
        const auto put=[&](const auto& value){out.write(reinterpret_cast<const char*>(&value),sizeof(value));
          emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
        put(std::uint64_t{0x31585656464d5545ULL});put(std::uint32_t{1});
        const auto& g=ref.generation();put(g.device);put(g.inode);put(g.bytes);
        put(g.modified_seconds);put(g.modified_nanoseconds);put(g.changed_seconds);put(g.changed_nanoseconds);
        put(std::uint32_t{64});const std::string sha(64,'a');out.write(sha.data(),sha.size());
        emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(sha.data()),sha.size());
        put(std::uint64_t{0});out.write(reinterpret_cast<const char*>(&crc),sizeof(crc));
      }
      StageRequest export_request;export_request.stage=Stage::kExportVivado;
      export_request.input_artifacts={ref,ArtifactRef::Bind(configuration,"vivado-export-request-v1")};
      export_request.output_path=(root/"empty.v").string();
      export_request.auxiliary_output_path=(root/"empty.functions.sqlite").string();
      export_request.scratch_path=(root/"empty-summary.json").string();
      const auto exported=emuflow::native::ExecuteStage(export_request);
      if(!exported.ok() || exported.outputs.size()!=3 ||
          exported.outputs[0].schema()!="vivado-primitive-verilog-v1" ||
          exported.outputs[1].schema()!="vivado-source-functions-v1" ||
          exported.outputs[2].schema()!="vivado-export-summary-v1")
        throw std::runtime_error("bound native Vivado export operation failed");
      auto alias=export_request;alias.auxiliary_output_path=mapped;
      if(emuflow::native::ExecuteStage(alias).error!=ErrorCode::kInvalidRequest)
        throw std::runtime_error("bound Vivado output alias was accepted");
      ref.RequireUnchanged();
      {
        SqliteDatabase db(mapped,SqliteDatabase::Mode::kReadOnly);
        // A deferred transaction permits invocation-local TEMP work without
        // turning an immutable circuit owner into a writable main database.
        emuflow::core::SqliteTransaction work(db,emuflow::core::SqliteTransaction::Mode::kDeferred);
        db.Execute("CREATE TEMP TABLE local_rows(value INTEGER); INSERT INTO local_rows VALUES(1)");
        work.Commit();
      }
    }
    {
      using emuflow::core::SqliteDatabase;
      const auto mapped=(root/"routed-mapped.sqlite").string(),ir=(root/"routed-ir.sqlite").string();
      const auto identity=(root/"routed-boundary.sqlite").string(),timing=(root/"routed-timing.json").string();
      const auto endpoints=(root/"routed-endpoints.jsonl").string(),configuration=(root/"routed-request.bin").string();
      {
        SqliteDatabase db(mapped,SqliteDatabase::Mode::kCreateExclusive);
        db.Execute(R"SQL(CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO metadata VALUES('schema','emuflow.xilinx-mapped-index/v2'),('top_json','"empty"'),('ports_json','{}');
          CREATE TABLE cells(id TEXT PRIMARY KEY,type TEXT);
          CREATE TABLE netnames(id TEXT PRIMARY KEY,record_json TEXT);
          CREATE TABLE cell_pins(cell_id TEXT,port TEXT,port_bit INTEGER,port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER);
        )SQL");
      }
      {
        SqliteDatabase db(ir,SqliteDatabase::Mode::kCreateExclusive);
        db.Execute(R"SQL(PRAGMA application_id=0x454d5549; PRAGMA user_version=2;
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO metadata VALUES('storage_schema','emuflow.emuir-sqlite/v2');
          CREATE TABLE net_records(seq INTEGER PRIMARY KEY,id TEXT,cut_class TEXT);
          CREATE TABLE endpoint_records(net_seq INTEGER,role INTEGER);
        )SQL");
      }
      {
        SqliteDatabase db(identity,SqliteDatabase::Mode::kCreateExclusive);
        db.Execute(R"SQL(
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE endpoints(
  id TEXT PRIMARY KEY NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  schedule_entry TEXT NOT NULL CHECK(length(schedule_entry) > 0),
  demand TEXT NOT NULL CHECK(length(demand) > 0),
  logical_net TEXT NOT NULL CHECK(length(logical_net) > 0),
  link TEXT NOT NULL CHECK(length(link) > 0),
  peer TEXT NOT NULL CHECK(length(peer) > 0),
  lane INTEGER NOT NULL CHECK(lane >= 0),
  logical_lane INTEGER NOT NULL CHECK(logical_lane >= 0),
  internal_signal TEXT NOT NULL CHECK(length(internal_signal) > 0),
  source_class TEXT NOT NULL CHECK(
    source_class IN ('dut-net','forwarded-shadow','captured-shadow')
  ),
  external_port TEXT NOT NULL CHECK(length(external_port) > 0),
  external_port_bit INTEGER NOT NULL CHECK(external_port_bit >= 0),
  external_net TEXT NOT NULL CHECK(length(external_net) > 0),
  merged_logical_net TEXT
) WITHOUT ROWID;
CREATE UNIQUE INDEX endpoints_schedule_kind_unique
  ON endpoints(schedule_entry,kind);
CREATE INDEX endpoints_kind_id ON endpoints(kind,id);
CREATE TABLE boundary_registers(
  endpoint_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  instance TEXT NOT NULL CHECK(length(instance) > 0),
  PRIMARY KEY(endpoint_id,ordinal),
  UNIQUE(endpoint_id,instance),
  FOREIGN KEY(endpoint_id) REFERENCES endpoints(id)
) WITHOUT ROWID;
)SQL");
        db.Execute(R"SQL(PRAGMA application_id=0x454d5542; PRAGMA user_version=1;
          INSERT INTO metadata VALUES('header','{"schema":"emuflow.boundary-identity/v1","design":"empty","fpga":"fpga0"}');
        )SQL");
      }
      {std::ofstream out(endpoints);}
      {std::ofstream out(timing);out<<R"JSON({"schema":"emuflow.xilinx-routed-timing/v2","top":"empty","qualification":{"logic_coefficients_ps":{"lut_a1":70,"ff_clock_to_q":25,"carry_co":10}},"payloads":{"endpoints":{"records":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}}})JSON";}
      std::vector<ArtifactRef> inputs{ArtifactRef::Bind(mapped,"xilinx-mapped-index-v2"),ArtifactRef::Bind(ir,"emuir-compact-v2"),
        ArtifactRef::Bind(timing,"xilinx-routed-timing-v2"),ArtifactRef::Bind(endpoints,"xilinx-routed-endpoints-v1"),ArtifactRef::Bind(identity,"boundary-identity-store-v1")};
      {
        std::ofstream out(configuration,std::ios::binary);std::uint32_t crc=0;
        const auto put=[&](const auto& value){out.write(reinterpret_cast<const char*>(&value),sizeof(value));emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
        const auto text=[&](const std::string& value){put(std::uint32_t(value.size()));out.write(value.data(),value.size());emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(value.data()),value.size());};
        put(std::uint64_t{0x31544752464d5545ULL});put(std::uint32_t{1});put(std::uint32_t{0});
        for(const auto& input:inputs){const auto& g=input.generation();put(g.device);put(g.inode);put(g.bytes);put(g.modified_seconds);put(g.modified_nanoseconds);put(g.changed_seconds);put(g.changed_nanoseconds);}
        text(R"JSON({"schema":"emuflow.boundary-timing/v1","status":"pass","design":"empty","platform":"board","fpga":"fpga0","provider":"fixture","qualification":"fixture","measurement_scope":"partition-boundary","coverage":{"endpoints":0,"tx":0,"rx":0}})JSON");
        text("");text("");out.write(reinterpret_cast<const char*>(&crc),sizeof(crc));
      }
      inputs.push_back(ArtifactRef::Bind(configuration,"routed-segment-request-v1"));
      StageRequest request;request.stage=Stage::kBuildRoutedSegments;request.input_artifacts=inputs;
      request.output_path=(root/"routed-summary.json").string();request.auxiliary_output_path=(root/"routed-stores").string();fs::create_directory(request.auxiliary_output_path);
      const auto result=emuflow::native::ExecuteStage(request);
      if(!result.ok() || result.inputs.size()!=6 || result.outputs.size()!=2 || result.outputs[1].schema()!="boundary-timing-store-v1")
        throw std::runtime_error("bound native routed graph operation failed: "+result.message);
      auto alias=request;alias.output_path=timing;
      if(emuflow::native::ExecuteStage(alias).error!=ErrorCode::kInvalidRequest)
        throw std::runtime_error("routed graph accepted an output alias");
      for(const auto& input:inputs)input.RequireUnchanged();
      {
        const auto physical=(root/"endpoint-physical.sqlite").string(),route=(root/"endpoint-route.json").string();
        const auto nets=(root/"endpoint-nets.jsonl").string(),config=(root/"endpoint-request.bin").string();
        {SqliteDatabase db(physical,SqliteDatabase::Mode::kCreateExclusive);db.Execute(R"SQL(
          PRAGMA application_id=0x454d5550; PRAGMA user_version=1;
          CREATE TABLE manifest(singleton INTEGER PRIMARY KEY,schema TEXT);
          INSERT INTO manifest VALUES(1,'emuflow.xilinx-physical-handoff-sqlite/v1');
          CREATE TABLE assignment(instance TEXT PRIMARY KEY,physical_site TEXT);
        )SQL");}
        {std::ofstream out(nets);}
        const std::string sha(64,'a');
        const auto sources="{\"mapped_sha256\":\""+sha+"\",\"packed_sha256\":\""+sha+"\",\"placement_sha256\":\""+sha+"\"}";
        {std::ofstream out(route);out<<"{\"schema\":\"emuflow.xilinx-route-db/v2\",\"status\":\"candidate\",\"provider\":\"rapidwright-rwroute-2026.1.0\",\"part\":\"fixture\",\"source\":"<<sources
          <<R"JSON(,"payloads":{"nets":{"format":"jsonl-object/v1","records":0,"sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}},"timing":{"logic_coefficients_ps":{"ff_clock_to_q":25,"carry_co":10,"lut_a1":70,"lut_a2":65,"lut_a3":60,"lut_a4":55,"lut_a5":50,"lut_a6":45}}})JSON";}
        std::vector<ArtifactRef> endpoint_inputs{ArtifactRef::Bind(mapped,"xilinx-mapped-index-v2"),ArtifactRef::Bind(physical,"xilinx-physical-handoff-v1"),ArtifactRef::Bind(route,"xilinx-route-manifest-v2"),ArtifactRef::Bind(nets,"xilinx-route-nets-v1")};
        {std::ofstream out(config,std::ios::binary);std::uint32_t crc=0;
          const auto put=[&](const auto& value){out.write(reinterpret_cast<const char*>(&value),sizeof(value));emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(&value),sizeof(value));};
          const auto text=[&](const std::string& value){put(std::uint32_t(value.size()));out.write(value.data(),value.size());emuflow::core::UpdateCrc32(crc,reinterpret_cast<const unsigned char*>(value.data()),value.size());};
          put(std::uint64_t{0x31505452464d5545ULL});put(std::uint32_t{1});
          for(const auto& input:endpoint_inputs){const auto& g=input.generation();put(g.device);put(g.inode);put(g.bytes);put(g.modified_seconds);put(g.modified_nanoseconds);put(g.changed_seconds);put(g.changed_nanoseconds);}
          text(sources.substr(0,sources.size()-1)+",\"route_sha256\":\""+sha+"\"}");text("endpoint-output.jsonl");out.write(reinterpret_cast<const char*>(&crc),sizeof(crc));
        }
        endpoint_inputs.push_back(ArtifactRef::Bind(config,"routed-endpoint-request-v1"));
        StageRequest binding;binding.stage=Stage::kBuildRoutedEndpoints;binding.input_artifacts=endpoint_inputs;
        binding.output_path=(root/"endpoint-output.jsonl").string();binding.auxiliary_output_path=(root/"endpoint-output.json").string();binding.scratch_path=(root/"endpoint-summary.json").string();
        auto bound=emuflow::native::ExecuteStage(binding);
        if(!bound.ok() || bound.inputs.size()!=5 || bound.outputs.size()!=3 || bound.outputs[1].schema()!="xilinx-routed-timing-v2")
          throw std::runtime_error("bound native endpoint operation failed: "+bound.message);
        auto alias=binding;alias.output_path=mapped;
        if(emuflow::native::ExecuteStage(alias).error!=ErrorCode::kInvalidRequest)
          throw std::runtime_error("bound endpoint operation accepted a source alias");
        for(const auto& input:endpoint_inputs)input.RequireUnchanged();
      }
    }
    const auto emuir_path = (root / "emuir.sqlite").string();
    {
      emuflow::core::SqliteDatabase db(emuir_path, emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      db.Execute(R"SQL(
        PRAGMA application_id=0x454d5549; PRAGMA user_version=2;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO metadata VALUES('storage_schema','emuflow.emuir-sqlite/v2'),
          ('record_counts_json','{"cell_records":2,"net_records":2,"endpoint_records":3}');
        CREATE TABLE payloads(seq INTEGER PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO payloads VALUES(0,'{"lut":1}'),(1,'{"ff":1}');
        CREATE TABLE cell_records(seq INTEGER PRIMARY KEY,resources_seq INTEGER);
        INSERT INTO cell_records VALUES(0,0),(1,1);
        CREATE TABLE net_records(seq INTEGER PRIMARY KEY,id TEXT NOT NULL);
        INSERT INTO net_records VALUES(0,'data'),(1,'empty');
        CREATE TABLE port_records(seq INTEGER PRIMARY KEY,record_json TEXT);
        INSERT INTO port_records VALUES(0,'{"id":"out"}');
        CREATE TABLE pin_names(seq INTEGER PRIMARY KEY,name TEXT);
        INSERT INTO pin_names VALUES(1,'O'),(2,'D'),(3,'out');
        CREATE TABLE endpoint_records(net_seq INTEGER,role INTEGER,ordinal INTEGER,instance_seq INTEGER,port_seq INTEGER,bit INTEGER);
        INSERT INTO endpoint_records VALUES(0,0,0,0,1,2),(0,1,7,1,2,3),(0,1,12,NULL,3,9);
      )SQL");
    }
    const auto other_emuir_path = (root / "other.sqlite").string();
    fs::copy_file(emuir_path, other_emuir_path);
    emuflow::core::EmuirView emuir(ArtifactRef::Bind(emuir_path, "emuir-compact-v2"));
    emuflow::core::EmuirView other(ArtifactRef::Bind(other_emuir_path, "emuir-compact-v2"));
    unsigned cells = 0, nets = 0, endpoints = 0, top = 0;
    emuir.VisitCellStates([&](auto cell, bool stateful) {
      if (cell.sequence() != cells || stateful != (cells == 1))
        throw std::runtime_error("native EmuIR cell semantics changed");
      ++cells;
    });
    emuir.VisitConnectivity([&](const auto& endpoint) {
      if (endpoint.net.sequence() != 0) throw std::runtime_error("native EmuIR endpoint net changed");
      if (!endpoint.cell) ++top;
      ++endpoints;
    }, [&](auto net) {
      if (net.sequence() != nets++) throw std::runtime_error("native EmuIR net ordering changed");
    });
    if (cells != 2 || nets != 2 || endpoints != 3 || top != 1 || emuir.NetName(emuir.Net(0)) != "data")
      throw std::runtime_error("native EmuIR ordered coverage changed");
    unsigned validated=0;
    emuir.VisitConnectivity([&](const auto& endpoint) {
      const char* names[]={"O","D","out"};const unsigned bits[]={2,3,9};
      if (validated>=3 || endpoint.port!=names[validated] || endpoint.bit!=bits[validated] ||
          !endpoint.pin || endpoint.pin->sequence()!=validated+1)
        throw std::runtime_error("compact endpoint fields changed during validation/emission");
      ++validated;
    },[](auto) {},true);
    if (validated!=3) throw std::runtime_error("compact validated pin coverage is incomplete");

    const auto legacy_path=(root/"legacy-emuir.sqlite").string();
    {
      emuflow::core::SqliteDatabase db(legacy_path,emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      db.Execute(R"SQL(
        PRAGMA application_id=0x454d5549; PRAGMA user_version=1;
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO metadata VALUES('storage_schema','emuflow.emuir-sqlite/v1');
        CREATE TABLE instances(seq INTEGER PRIMARY KEY,id TEXT UNIQUE);INSERT INTO instances VALUES(0,'lut'),(1,'ff');
        CREATE TABLE instance_resources(instance_seq INTEGER,resource TEXT,amount INTEGER);
        INSERT INTO instance_resources VALUES(0,'lut',1),(1,'ff',1);
        CREATE TABLE nets(seq INTEGER PRIMARY KEY,id TEXT UNIQUE);INSERT INTO nets VALUES(0,'data'),(1,'empty');
        CREATE TABLE ports(seq INTEGER PRIMARY KEY,id TEXT);INSERT INTO ports VALUES(0,'out');
        CREATE TABLE endpoints(net_seq INTEGER,role INTEGER,ordinal INTEGER,instance_id TEXT,port TEXT,bit INTEGER);
        INSERT INTO endpoints VALUES(0,0,0,'lut','O',2),(0,1,7,'ff','D',3),(0,1,12,NULL,'out',9);
      )SQL");
    }
    {
      emuflow::core::EmuirView legacy(ArtifactRef::Bind(legacy_path,"emuir-indexed-v1"));
      if (legacy.counts().cells!=emuir.counts().cells || legacy.counts().nets!=emuir.counts().nets ||
          legacy.counts().endpoints!=emuir.counts().endpoints || legacy.NetName(legacy.Net(0))!="data")
        throw std::runtime_error("v1/v2 EmuIR coverage disagrees");
      unsigned state_count=0,pin_count=0,net_count=0;
      legacy.VisitCellStates([&](auto id,bool stateful) {
        if (id.sequence()!=state_count || stateful!=(state_count==1)) throw std::runtime_error("v1 cell semantics changed");
        ++state_count;
      });
      legacy.VisitConnectivity([&](const auto& endpoint) {
        const std::string ports[]={"O","D","out"};
        const unsigned bits[]={2,3,9};
        if (endpoint.port!=ports[pin_count] || endpoint.bit!=bits[pin_count] || endpoint.pin || endpoint.net.sequence()!=0 ||
            (pin_count==2 ? endpoint.cell.has_value() : !endpoint.cell.has_value()))
          throw std::runtime_error("v1 endpoint semantics changed");
        ++pin_count;
      },[&](auto net) { if (net.sequence()!=net_count++) throw std::runtime_error("v1 net order changed"); },true);
      if (state_count!=2 || pin_count!=3 || net_count!=2) throw std::runtime_error("v1 ordered coverage changed");
    }

    for (const auto* retired : {"assignment-index-v1", "assignment-index-v2"}) {
      bool rejected=false;
      try { emuflow::core::FindSchema(retired); }
      catch (const emuflow::core::StageError& error) { rejected=error.code()==ErrorCode::kInvalidRequest; }
      if (!rejected) throw std::runtime_error("retired assignment schema is registered");
    }

    for (unsigned version=3;version<=4;++version) {
      const auto schema="assignment-index-v"+std::to_string(version);
      const auto path=(root/(schema+".sqlite")).string();
      {
        emuflow::core::SqliteArtifactWriter writer(path,schema);
        auto& db=writer.database();
        db.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
            "INSERT INTO metadata VALUES ('schema','\"emuflow.phase3-assignment-sqlite/v"+std::to_string(version)+"\"'),"
            "('source_instance_count','2');"
            "CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT UNIQUE);INSERT INTO parts VALUES(0,'fpga0'),(1,'fpga1');"
            "CREATE TABLE copies(source_instance_seq INTEGER,part_index INTEGER,copy_id TEXT UNIQUE,is_replica INTEGER);"
            "INSERT INTO copies VALUES (0,0,'lut',0),(0,1,'replica_lut',1),(1,1,'ff',0)");
        writer.Commit();
      }
      emuflow::core::AssignmentView assignment(ArtifactRef::Bind(path,schema));
      unsigned count=0,replicas=0;
      assignment.VisitCopies([&](const auto& copy) {
        const unsigned source[]={0,0,1},part[]={0,1,1};
        const char* names[]={"lut","replica_lut","ff"};
        if (count>=3 || copy.source.sequence()!=source[count] || copy.partition.sequence()!=part[count] || copy.identity!=names[count])
          throw std::runtime_error("versioned assignment cursor changed identity/order");
        ++count;replicas+=copy.replica;
      });
      if (count!=3 || replicas!=1 || assignment.source_cells()!=2 || assignment.partitions()!=2)
        throw std::runtime_error("versioned assignment coverage disagrees");
    }
    const auto split_path=(root/"split.sqlite").string();
    {
      emuflow::core::SqliteArtifactWriter writer(split_path,"split-netlists-v2");auto& db=writer.database();
      db.Execute(R"SQL(
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);INSERT INTO metadata VALUES('schema','"emuflow.split-netlists-sqlite/v2"');
        CREATE TABLE parts(part_index INTEGER PRIMARY KEY,fpga TEXT);INSERT INTO parts VALUES(0,'fpga0'),(1,'fpga1');
        CREATE TABLE source_instances(source_instance_seq INTEGER PRIMARY KEY,original_id TEXT);
        INSERT INTO source_instances VALUES(0,'lut'),(1,'ff');
        CREATE TABLE instance_copies(part_index INTEGER,source_instance_seq INTEGER,copy_id TEXT,is_replica INTEGER);
        INSERT INTO instance_copies VALUES(0,0,NULL,0),(1,1,NULL,0),(1,0,'replica_lut',1);
        CREATE TABLE source_nets(source_net_seq INTEGER PRIMARY KEY);INSERT INTO source_nets VALUES(5);
        CREATE TABLE pin_names(seq INTEGER PRIMARY KEY,name TEXT);INSERT INTO pin_names VALUES(1,'D'),(2,'shadow_out');
        CREATE TABLE endpoint_segments(part_index INTEGER,source_net_seq INTEGER,role INTEGER,ordinal INTEGER,
          source_instance_seq INTEGER,synthetic_instance_id TEXT,port_seq INTEGER,bit INTEGER,synthetic INTEGER);
        INSERT INTO endpoint_segments VALUES(1,5,0,7,NULL,'shadow0',2,0,1),(1,5,1,9,1,NULL,1,3,0),
          (1,5,1,12,NULL,NULL,1,0,0);
      )SQL");
      writer.Commit();
    }
    {
      emuflow::core::SplitView split(ArtifactRef::Bind(split_path,"split-netlists-v2"));
      const auto part=split.Partition(1);unsigned copies=0,endpoints=0;
      split.VisitCopies(part,[&](const auto& copy) {
        if (copy.partition.sequence()!=1 || copy.source.sequence()!=(copies ? 1 : 0) || copy.replica!=(copies==0))
          throw std::runtime_error("split ownership changed");
        ++copies;
      });
      split.VisitEndpoints(part,[&](const auto& endpoint) {
        if (endpoint.net.sequence()!=5 || endpoint.partition.sequence()!=1 || endpoint.sink!=(endpoints!=0) ||
            endpoint.cell.has_value()!=(endpoints==1) || endpoint.synthetic_cell!=(endpoints==0 ? "shadow0" : ""))
          throw std::runtime_error("split endpoint coordinates changed");
        ++endpoints;
      });
      if (copies!=2 || endpoints!=3) throw std::runtime_error("split ordered coverage disagrees");
      emuflow::core::AssignmentView foreign(ArtifactRef::Bind((root/"assignment-index-v4.sqlite").string(),"assignment-index-v4"));
      bool rejected=false;
      try { split.VisitEndpoints(foreign.Partition(1),[](const auto&) { throw std::runtime_error("foreign part entered visitor"); }); }
      catch (const emuflow::core::StageError& error) { rejected=error.code()==ErrorCode::kMixedGeneration; }
      if (!rejected) throw std::runtime_error("foreign partition entered split view");
    }
    bool mixed_identity = false;
    try { emuir.NetName(other.Net(0)); }
    catch (const emuflow::core::StageError& e) { mixed_identity = e.code() == ErrorCode::kMixedGeneration; }
    if (!mixed_identity) throw std::runtime_error("native EmuIR mixed artifact-local identities");
    const auto mapped_path = (root / "mapped.sqlite").string();
    const auto atom_path = (root / "atoms.sqlite").string();
    {
      emuflow::core::SqliteDatabase db(mapped_path, emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      db.Execute(R"SQL(
        CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO metadata VALUES('schema','emuflow.xilinx-mapped-index/v2');
        CREATE TABLE cells(id TEXT PRIMARY KEY,type TEXT);
        INSERT INTO cells VALUES('a_sink','FDRE'),('z_driver','LUT1');
        CREATE TABLE cell_pins(cell_id TEXT,port TEXT,port_bit INTEGER,
          port_width INTEGER,direction INTEGER,value_kind INTEGER,net_bit INTEGER,constant TEXT);
        INSERT INTO cell_pins VALUES('a_sink','D',0,1,0,0,2,NULL),
          ('z_driver','O',0,1,1,0,2,NULL);
      )SQL");
    }
    {
      emuflow::core::SqliteDatabase db(atom_path, emuflow::core::SqliteDatabase::Mode::kCreateExclusive);
      db.Execute(R"SQL(
        CREATE TABLE atoms(seq INTEGER PRIMARY KEY,instance TEXT,cell_type TEXT);
        INSERT INTO atoms VALUES(1,'a_sink','FDRE'),(2,'z_driver','LUT1');
      )SQL");
    }
    StageRequest bookshelf{Stage::kExportBookshelf, {}, (root / "design.nets").string()};
    bookshelf.input_artifacts = {ArtifactRef::Bind(mapped_path,"xilinx-mapped-index-v2"),
                                ArtifactRef::Bind(atom_path,"openparf-atom-index-private-v1")};
    bookshelf.auxiliary_output_path = (root / "summary.tsv").string();
    bookshelf.scratch_path = (root / "endpoints.sqlite").string();
    const auto physical = emuflow::native::ExecuteStage(bookshelf);
    if (!physical.ok() || physical.inputs.size()!=2 || physical.outputs.size()!=2 ||
        fs::exists(bookshelf.scratch_path))
      throw std::runtime_error("shared physical export failed: " + physical.message);
    {
      std::ifstream text(bookshelf.output_path);
      const std::string value((std::istreambuf_iterator<char>(text)), {});
      if (value != "net n0 2\n  a1 O\n  a0 D\nendnet\n")
        throw std::runtime_error("shared physical export changed driver/atom order");
    }
    {
      emuflow::core::SqliteDatabase db(atom_path, emuflow::core::SqliteDatabase::Mode::kReadOnly);
      auto values = db.Prepare("SELECT 7,'text',NULL,'7'");
      if (!values.Step() || values.TypedInteger(0)!=7 || values.TypedText(1)!="text" || !values.IsNull(2))
        throw std::runtime_error("shared typed SQL values changed");
      bool rejected = false;
      try { values.TypedInteger(3); } catch (const std::runtime_error&) { rejected = true; }
      if (!rejected) throw std::runtime_error("shared SQL coerced text into an integer");
    }
    fs::rename(atom_path,atom_path + ".previous");
    fs::copy_file(atom_path + ".previous",atom_path);
    if (emuflow::native::ExecuteStage(bookshelf).error != ErrorCode::kMixedGeneration)
      throw std::runtime_error("physical exporter rebound a stale atom relation");
    for (const auto& published : physical.outputs) published.RequireUnchanged();
    if (emuflow::native::ExecuteStage(StageRequest{}).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API executed an unspecified stage");
    {
      const auto source = (root / "hop.input").string();
      const auto output = (root / "hop.output").string();
      const std::string model = "EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1\n"
          "PARAM 1 1 1 0 1 1\nDIST 0 0 0\nBOUND 0 0 1 1\nCLUSTER 0 0 -1 1\n";
      { std::ofstream file(source); file << model; }
      StageRequest hop{Stage::kRefinePartitionHops, {}, output};
      hop.input_artifacts = {ArtifactRef::Bind(source,"hop-refiner-input-v1")};
      const auto result = emuflow::native::ExecuteStage(hop);
      if (!result.ok() || result.inputs.size()!=1 || result.outputs.size()!=1 ||
          result.outputs[0].schema()!="hop-refiner-output-v1")
        throw std::runtime_error("hop typed API failed: "+result.message);
      const auto generation = fs::last_write_time(source);
      { emuflow::core::AtomicBinaryWriter writer(source);
        writer.WriteBytes(model.data(),model.size()); writer.Commit(); }
      fs::last_write_time(source,generation);
      if (emuflow::native::ExecuteStage(hop).error!=ErrorCode::kMixedGeneration)
        throw std::runtime_error("hop API rebound a stale source generation");
      result.outputs[0].RequireUnchanged();
      auto mismatched=hop;
      mismatched.input_artifacts={ArtifactRef::Bind(source,"global-route-input-v1")};
      if (emuflow::native::ExecuteStage(mismatched).error!=ErrorCode::kInvalidArtifact)
        throw std::runtime_error("hop API inferred a mismatched input schema");
      result.outputs[0].RequireUnchanged();
    }
    const auto source = (root / "rudy.input").string();
    const auto output = (root / "rudy.output").string();
    const std::string payload = "EMUFLOW_CHIMEW_RUDY_INPUT_V1\nGRID 0 0 1 1 1 1\nPARAM 1 1\nCAP 0 1\nNET 0 2 0.1 0.1 0.2 0.2\n";
    { std::ofstream stream(source); stream << payload; }
    StageRequest request{Stage::kRudy, {}, output};
    request.input_artifacts = {ArtifactRef::Bind(source, "chimew-rudy-input-v1")};
    const auto result = emuflow::native::ExecuteStage(request);
    if (!result.ok() || result.outputs.size() != 1 || result.inputs.size() != 1 ||
        result.outputs[0].schema() != "chimew-rudy-output-text") {
      throw std::runtime_error("typed native stage failed: " + result.message);
    }
    {
      const auto read_text = [](const std::string& path) {
        std::ifstream file(path); return std::string(std::istreambuf_iterator<char>(file), {});
      };
      const auto before = read_text(output);
      const auto invalid_source = (root / "invalid-rudy.input").string();
      { std::ofstream file(invalid_source); file << "invalid-input"; }
      StageRequest malformed_rudy{Stage::kRudy,{invalid_source},output};
      if (emuflow::native::ExecuteStage(malformed_rudy).ok() || read_text(output) != before)
        throw std::runtime_error("RUDY parse failure destroyed a published result");
      for (const auto stage : {Stage::kGroupSignals,Stage::kRefinePositions,Stage::kAssignBanks}) {
        StageRequest legacy{stage,{invalid_source},output}; legacy.typed_input = false;
        if (emuflow::native::ExecuteStage(legacy).error != ErrorCode::kInvalidRequest || read_text(output) != before)
          throw std::runtime_error("retired Chimew text API executed");
      }
    }
    auto wrong = request;
    wrong.input_artifacts = {ArtifactRef::Bind(source, "global-route-input-v1")};
    const auto mismatch = emuflow::native::ExecuteStage(wrong);
    if (mismatch.error != ErrorCode::kInvalidArtifact)
      throw std::runtime_error("native API guessed a mismatched artifact schema");
    result.outputs[0].RequireUnchanged();
    auto ambiguous = request;
    ambiguous.input_paths = {source};
    if (emuflow::native::ExecuteStage(ambiguous).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted ambiguous ownership");
    StageRequest retired_patron{Stage::kRefinePatron, {source}, output};
    retired_patron.typed_input = false;
    if (emuflow::native::ExecuteStage(retired_patron).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted retired PATRON text input");
    result.outputs[0].RequireUnchanged();
    StageRequest unsupported_pipe{Stage::kAnalyzeExact, {}, output};
    unsupported_pipe.input_artifacts = {ArtifactRef::Bind("-", "exact-analysis-input-v3", true)};
    if (emuflow::native::ExecuteStage(unsupported_pipe).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted a pipe for a file-only reader");
    const auto original_time = fs::last_write_time(source);
    {
      emuflow::core::AtomicBinaryWriter writer(source);
      writer.WriteBytes(payload.data(), payload.size()); writer.Commit();
    }
    fs::last_write_time(source, original_time);
    const auto stale = emuflow::native::ExecuteStage(request);
    if (stale.error != ErrorCode::kMixedGeneration)
      throw std::runtime_error("native API rebound an old reference to a new generation");
    result.outputs[0].RequireUnchanged();
    auto unknown = request;
    unknown.stage = static_cast<Stage>(999);
    if (emuflow::native::ExecuteStage(unknown).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("unknown native stage did not fail closed");
    {
      using emuflow::core::SqliteDatabase;
      const auto mapped=(root/"lineage mapped ?#%.sqlite").string();
      const auto functions=(root/"lineage functions.sqlite").string();
      const auto inventory=(root/"lineage census.tsv").string();
      const auto configuration=(root/"lineage-request.json").string();
      const auto target=(root/"lineage.sqlite").string();
      const auto summary=(root/"lineage-summary.json").string();
      const std::string digest(64,'a');
      {
        SqliteDatabase source(mapped,SqliteDatabase::Mode::kCreateExclusive);
        source.Execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
          "INSERT INTO metadata VALUES('schema','emuflow.xilinx-mapped-index/v2');"
          "CREATE TABLE cells(seq INTEGER PRIMARY KEY,id TEXT,type TEXT);"
          "INSERT INTO cells VALUES(0,'kept','FDRE'),(1,'unretained','FDRE');");
        SqliteDatabase signatures(functions,SqliteDatabase::Mode::kCreateExclusive);
        signatures.Execute("PRAGMA application_id=0x454d5646;PRAGMA user_version=1;"
          "CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
          "INSERT INTO metadata VALUES('schema','emuflow.vivado-source-functions/v1');"
          "CREATE TABLE signatures(seq INTEGER PRIMARY KEY,signature TEXT);"
          "INSERT INTO signatures VALUES(0,'same'),(1,'same');");
        auto owner=signatures.Prepare("INSERT INTO metadata VALUES('mapped_index_sha256',?)");
        owner.BindText(1,digest);owner.Execute();
      }
      bool rejected_uri_create=false;
      try {SqliteDatabase wrong("file:"+mapped+"?mode=rw",SqliteDatabase::Mode::kCreateExclusive);}
      catch(const emuflow::core::StageError& error) {rejected_uri_create=error.code()==ErrorCode::kInvalidRequest;}
      if(!rejected_uri_create) throw std::runtime_error("SQLite URI bypassed exclusive-create ownership");
      {
        std::ofstream data(inventory);
        data<<"name\tref_name\tsource_origin\ttransformations_hex\tsite\tbel\ttile_column\ttile_row\tprimitive_level\n"
               "observed\tFDRE\ts0\t\tSLICE_X9Y8\tAFF\t7\t11\tLEAF\n";
        std::ofstream config(configuration);
        config<<"{\"schema\":\"emuflow.vivado-lineage-import/v1\",\"mapped_sha256\":\""<<digest
              <<"\",\"retain_stage\":\"routed\",\"stages\":[\"routed\"]}";
      }
      StageRequest import{Stage::kImportVivadoLineage,{},target};
      import.auxiliary_output_path=summary;
      import.input_artifacts={ArtifactRef::Bind(mapped,"xilinx-mapped-index-v2"),
        ArtifactRef::Bind(functions,"vivado-source-functions-v1"),
        ArtifactRef::Bind(configuration,"vivado-lineage-import-request-v1"),
        ArtifactRef::Bind(inventory,"vivado-lineage-inventory-v2")};
      const auto result=emuflow::native::ExecuteStage(import);
      if(!result.ok() || result.inputs.size()!=4 || result.outputs.size()!=2 ||
         result.outputs[0].schema()!="vivado-lineage-store-v2")
        throw std::runtime_error("native lineage operation failed: "+result.message);
      {
        SqliteDatabase data(target,SqliteDatabase::Mode::kImmutableReadOnly);
        auto row=data.Prepare("SELECT origin,tile_column,tile_row FROM objects JOIN sites ON sites.seq=objects.site_seq");
        if(!row.Step() || row.Integer(0)!=0 || row.Integer(1)!=7 || row.Integer(2)!=11 || row.Step())
          throw std::runtime_error("native lineage changed source identity or tile coordinates");
      }
      auto aliases=import;aliases.auxiliary_output_path=inventory;
      if(emuflow::native::ExecuteStage(aliases).error!=ErrorCode::kInvalidRequest)
        throw std::runtime_error("lineage summary overwrote an input");
      result.outputs[0].RequireUnchanged();result.outputs[1].RequireUnchanged();
    }
  } catch (...) { fs::remove_all(root); throw; }
  fs::remove_all(root);
}
