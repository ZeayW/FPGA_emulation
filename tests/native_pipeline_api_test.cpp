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
        CREATE TABLE endpoint_records(net_seq INTEGER,role INTEGER,ordinal INTEGER,instance_seq INTEGER);
        INSERT INTO endpoint_records VALUES(0,0,0,0),(0,1,7,1),(0,1,12,NULL);
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
    auto wrong = request;
    wrong.input_artifacts = {ArtifactRef::Bind(source, "tlr-input-text")};
    const auto mismatch = emuflow::native::ExecuteStage(wrong);
    if (mismatch.error != ErrorCode::kInvalidArtifact)
      throw std::runtime_error("native API guessed a mismatched artifact schema");
    result.outputs[0].RequireUnchanged();
    auto ambiguous = request;
    ambiguous.input_paths = {source};
    if (emuflow::native::ExecuteStage(ambiguous).error != ErrorCode::kInvalidRequest)
      throw std::runtime_error("native API accepted ambiguous ownership");
    StageRequest unsupported_pipe{Stage::kSchedule, {}, output};
    unsupported_pipe.input_artifacts = {ArtifactRef::Bind("-", "exact-schedule-input-v1", true)};
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
  } catch (...) { fs::remove_all(root); throw; }
  fs::remove_all(root);
}
