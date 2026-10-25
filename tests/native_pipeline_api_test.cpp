#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/emuir_view.hpp"
#include "emuflow_core/tool_runtime.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <stdexcept>

int main() {
  namespace fs = std::filesystem;
  using emuflow::core::ArtifactRef;
  using emuflow::core::ErrorCode;
  using emuflow::native::Stage;
  using emuflow::native::StageRequest;
  const auto unique = std::chrono::steady_clock::now().time_since_epoch().count();
  const auto root = fs::temp_directory_path() / ("emuflow-pipeline-api-" + std::to_string(unique));
  if (!fs::create_directory(root)) throw std::runtime_error("test directory already exists");
  try {
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
