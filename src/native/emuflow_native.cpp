#include <iostream>
#include <string>
#include <vector>
#include <csignal>
#include <filesystem>

#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/tool_runtime.hpp"
#include "emuflow_core/compile.hpp"
#include "emuflow_core/yosys_hierarchy.hpp"
#include "emuflow_core/frontend.hpp"
#include "emuflow_core/partition_init.hpp"
#include "emuflow_core/partition_hops.hpp"
#include "emuflow_core/partition_semantics.hpp"
#include "emuflow_core/phase3_exact_binary.hpp"
#include "emuflow_core/partition_publish.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/store_publication.hpp"

namespace {
using emuflow::native::Stage;
using emuflow::native::StageRequest;
constexpr const char* kUsage =
    "usage: emuflow-native [--report RESULT_JSON] PHASE OPERATION ...\n"
    "  check-runtime [SCHEMA]\n"
    "  publish-store REQUEST OUTPUT\n"
    "  compile REQUEST_JSON\n"
    "  phase1 stage-yosys-hierarchy REQUEST STORE SUMMARY [PRIMITIVE_LIBRARY] < YOSYS_STREAM\n"
    "  phase3 refine-hops-from-primary EMUIR PRIMARY REQUEST OUTPUT_DIR [OWNER [ORDER]] [WEIGHTS_SELECTOR WEIGHTS_STORE]\n"
    "  phase3 audit-hops-from-primary EMUIR PRIMARY REQUEST OUTPUT_DIR [OWNER [ORDER]]\n"
    "  phase3 initialize-tritonpart EMUIR PRIMARY REQUEST OUTPUT_DIR [WEIGHTS_SELECTOR WEIGHTS_STORE] [SOLUTION]\n"
    "  phase3 finalize-assignment-semantics EMUIR PRIMARY POLICY REQUEST OUTPUT_DIR [OWNER [ORDER]]\n"
    "  phase3 publish-exact-binary NODES CAPTURES REQUEST OUTPUT_DIR\n"
    "  phase3 validate-exact-binary BINARY REQUEST SUMMARY\n"
    "  phase3 publish-partition EMUIR PRIMARY NODES CAPTURES COUNTS CATALOG_REQUEST CANDIDATES [OWNER [ORDER]] REQUEST OUTPUT_DIR\n"
    "  phase1 run REQUEST\n"
    "  phase1 import-yosys REQUEST EMUIR SUMMARY [PRIMITIVE_LIBRARY] < YOSYS_STREAM\n"
    "  run-tool REQUEST RESULT_JSON\n"
    "  phase3 analyze INPUT OUTPUT\n"
    "  phase3 refine-hops [--trace-output] INPUT OUTPUT\n"
    "  phase3 analyze --indexed-input EMUIR INPUT OUTPUT\n"
    "  phase3 finalize-semantics TOPOLOGY METADATA OUTPUT\n"
    "  phase3 build-captures EMUIR REQUEST TOPOLOGY PARTS OUTPUT\n"
    "  phase3 validate-captures EMUIR REQUEST TOPOLOGY PARTS CAPTURES OUTPUT\n"
    "  phase3 hash-semantics NODES CAPTURES HEADER OUTPUT\n"
    "  phase3 write-exact-sections NODES CAPTURES REQUEST OUTPUT\n"
    "  phase3 refine-patron [--summary-output|--trace-output] [--typed-output] "
        "--typed-input INPUT OUTPUT\n"
    "  phase3 refine-patron-from-primary EMUIR TIMING PRIMARY INITIAL REQUEST OUTPUT\n"
    "  phase3 build-candidate-ids EMUIR POLICY OUTPUT\n"
    "  phase3 build-clusters EMUIR REQUEST OUTPUT\n"
    "  phase3 validate-cuts ASSIGNMENT SUMMARY\n"
    "  phase3 publish-assignment EMUIR PRIMARY REQUEST [NATIVE_NODES [COUNTS [OWNER [ORDER]]]] OUTPUT\n"
    "  phase3 publish-assignment-descriptor EMUIR PRIMARY INDEX REQUEST OUTPUT\n"
    "  phase3 publish-cluster-catalog EMUIR PRIMARY REQUEST [CANDIDATES] OUTPUT\n"
    "  phase3 reduce-loads EMUIR PRIMARY REQUEST [OWNER [ORDER]] OUTPUT\n"
    "  phase3 audit-decision EMUIR PRIMARY REQUEST [INITIAL [CANDIDATE ORDER]] OUTPUT\n"
    "  phase3 normalize-configuration EMUIR REQUEST [RAW_CONSTRAINTS] OUTPUT\n"
    "  phase4 route --typed-output INPUT OUTPUT\n"
    "  phase4 seal-routes STORE REQUEST SUMMARY\n"
    "  phase5 timing-contract ROUTE_STORE [SCHEDULE_STORE [BOARD_REQUEST]] REQUEST SUMMARY\n"
    "  phase5 validate-schedule ROUTE_STORE BOARD_REQUEST SCHEDULE_STORE REQUEST SUMMARY\n"
    "  phase5 compile ROUTE_SELECTOR ASSIGNMENT_STORE NORMALIZED_BOARD REPORT\n"
    "  phase5 run ROUTE_STORE BOARD_REQUEST GENERATION_REQUEST STAGE_REQUEST REPORT\n"
    "  phase5 build-schedule ROUTE_STORE BOARD_REQUEST GENERATION_REQUEST OUTPUT\n"
    "  global-sta import-measurements PRIMARY INDEX TSV REQUEST STORE\n"
    "  global-sta audit-measurements PRIMARY INDEX STORE REQUEST SUMMARY\n"
    "  global-sta publish-qor PRIMARY INDEX STORE REQUEST QOR\n"
    "  global-sta validate-qor STORE REQUEST SUMMARY\n"
    "  global-sta bind-events ROUTES SCHEDULE BOARD [PHYSICAL_STORES...] REQUEST PRIMARY\n"
    "  physical import-boundary-timing IDENTITY TSV REQUEST STORE SUMMARY\n"
    "  physical import-logic-timing IDENTITY TSV REQUEST STORE SUMMARY\n"
    "  physical import-local-timing IDENTITY TSV REQUEST STORE SUMMARY\n"
    "  physical validate-evidence STORE REQUEST SUMMARY\n"
    "  global-sta index-events PRIMARY INDEX\n"
    "  global-sta export-events PRIMARY INDEX REQUEST SUMMARY\n"
    "  phase6 assign-banks --typed-input INPUT OUTPUT\n"
    "  phase6 group-signals --typed-input INPUT OUTPUT EXPECTED_SHA256\n"
    "  phase6 refine-groups --typed-input INPUT OUTPUT EXPECTED_SHA256\n"
    "  phase6 rudy INPUT OUTPUT\n"
    "  phase7 validate-atomic-ownership MAPPED SOURCE CERTIFICATE REQUEST SUMMARY\n"
    "  phase7 validate-physical-cells MAPPED PHYSICAL REQUEST SUMMARY\n"
    "  phase7 export-bookshelf MAPPED ATOMS NETS SUMMARY SPOOL\n"
    "  phase7 export-rwroute MAPPED PHYSICAL REQUEST TSV SUMMARY\n"
    "  phase7 export-vivado MAPPED REQUEST RTL FUNCTIONS SUMMARY\n"
    "  physical build-routed-segments SUMMARY DIRECTORY MAPPED IR TIMING ENDPOINTS BOUNDARY [LOGIC] [LOCAL] REQUEST\n"
    "  physical stage-routed-opensta MAPPED TIMING ENDPOINTS REQUEST EMUIR LIBERTY SUMMARY\n"
    "  physical export-opensta-inputs EMUIR REQUEST VERILOG|- NET_MAP|- SUMMARY\n"
    "  physical build-sta-weights EMUIR PATH_SELECTOR PATH_STORE REQUEST STORE SUMMARY\n"
    "  physical validate-sta-weights EMUIR PATH_SELECTOR PATH_STORE WEIGHT_SELECTOR WEIGHT_STORE REQUEST SUMMARY\n"
    "  physical import-sta-paths EMUIR TSV REQUEST STORE SUMMARY\n"
    "  physical validate-sta-paths EMUIR STORE REQUEST SUMMARY\n"
    "  physical build-routed-endpoints MAPPED PHYSICAL ROUTE NETS REQUEST ENDPOINTS MANIFEST SUMMARY\n"
    "  phase7 import-vivado-lineage MAPPED FUNCTIONS REQUEST STORE SUMMARY INVENTORY...\n";

int Finish(emuflow::core::StageResult result, const std::string& report) {
  // Invalid requests may name a report which aliases an input/output. Never
  // overwrite that artifact merely to report rejection of the alias itself.
  if (!report.empty() && result.error != emuflow::core::ErrorCode::kInvalidRequest) {
    try { emuflow::core::PublishStageResult(report, result); }
    catch (const emuflow::core::StageError& e) {
      result.error = e.code(); result.message = e.what();
    }
  }
  if (result.ok()) return 0;
  std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
  return result.error == emuflow::core::ErrorCode::kInvalidRequest ? 2 : 1;
}

int Execute(StageRequest request, const std::string& report) {
  request.report_path = report;
  return Finish(emuflow::native::ExecuteStage(request),report);
}

volatile std::sig_atomic_t tool_cancelled = 0;
void CancelTool(int signal) { tool_cancelled = signal; }
struct ToolSignals {
  struct sigaction old_int{}, old_term{}, old_hup{};
  ToolSignals() {
    struct sigaction action{}; action.sa_handler = CancelTool; sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, &old_int); sigaction(SIGTERM, &action, &old_term);
    sigaction(SIGHUP, &action, &old_hup);
  }
  ~ToolSignals() {
    sigaction(SIGINT, &old_int, nullptr); sigaction(SIGTERM, &old_term, nullptr);
    sigaction(SIGHUP, &old_hup, nullptr);
  }
};
bool SamePath(const std::string& a, const std::string& b) {
  std::error_code error;
  return std::filesystem::equivalent(a,b,error) ||
      std::filesystem::weakly_canonical(a) == std::filesystem::weakly_canonical(b);
}
int RunTool(const std::string& input, const std::string& output) {
  // Reject aliases before launching an engine, not after it has had side effects.
  emuflow::core::BinaryReader header(input);
  (void)header.Read<std::uint64_t>();
  const auto version=header.Read<std::uint32_t>();
  auto ref = emuflow::core::ArtifactRef::Bind(input,version==2?"external-tool-request-v2":"external-tool-request-v1");
  auto request = emuflow::core::ReadToolRequest(ref.path());
  if (output.empty() || output == "-" || SamePath(input,output) || SamePath(request.argv[0],output))
    throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,
                                  "external tool result aliases a source or is not a file");
  const auto parent = std::filesystem::absolute(output).parent_path();
  if (!std::filesystem::is_directory(parent) || std::filesystem::is_directory(output))
    throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,
                                  "external tool result directory is unavailable");
  ToolSignals signals;
  const auto result = emuflow::core::RunExternalTool(request,&tool_cancelled,{ref});
  emuflow::core::PublishToolResult(output,result);
  // A cancelled supervisor completes group cleanup and evidence publication
  // before returning. No tool output is mixed into forwarded Yosys stdout.
  if (!result.operation.ok())
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result.operation);
  return result.operation.ok() ? 0 : 1;
}

int Dispatch(int argc, char** argv, const std::string& report) {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << kUsage; return 0;
  }
  if ((argc == 2 || argc == 3) && std::string(argv[1]) == "check-runtime") {
    if (argc == 3) (void)emuflow::core::FindSchema(argv[2]);
    return Finish(emuflow::core::CheckNativeRuntime(),report);
  }
  if (argc == 4 && std::string(argv[1]) == "publish-store") {
    const auto request = emuflow::core::ArtifactRef::Bind(argv[2],"sqlite-publication-request-v1");
    return Finish(emuflow::core::PublishSqliteStore(request,argv[3],report),report);
  }
  if(argc>=7 && argc<=11 && std::string(argv[1])=="phase3" && (std::string(argv[2])=="refine-hops-from-primary" || std::string(argv[2])=="audit-hops-from-primary")) {
    std::vector<emuflow::core::ArtifactRef> inputs{emuflow::core::ArtifactRef::Bind(argv[3],"emuir-compact-v2"),emuflow::core::ArtifactRef::Bind(argv[4],"partition-cluster-columns-v2"),emuflow::core::ArtifactRef::Bind(argv[5],"partition-hop-request-v1")};
    emuflow::core::CheckedRequestReader request(inputs[2].path(),8*1024*1024);
    if(request.Read<std::uint64_t>()!=0x31514850464d5545ULL || request.Read<std::uint32_t>()!=1)throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidArtifact,"invalid primary hop request");
    const auto flags=request.Read<std::uint32_t>();const unsigned owners=flags&3,weights=(flags&4)?2:0;
    if(owners>2 || (flags&~15U) || unsigned(argc)!=7+owners+weights)throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,"invalid primary hop source coverage");
    if(owners)inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[7],owners==1?"patron-initial-columns-v1":"patron-output-v1"));
    if(owners==2)inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[8],"patron-cluster-order-v1"));
    if(weights){inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[7+owners],"net-weight-selector-v2"));inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[8+owners],"net-weight-store-v1"));}
    return Finish(emuflow::core::RefinePartitionHopsFromPrimary(inputs,argv[6],std::string(argv[2])=="audit-hops-from-primary"),report);
  }
  if(argc>=7 && argc<=10 && std::string(argv[1])=="phase3" && std::string(argv[2])=="initialize-tritonpart") {
    std::vector<emuflow::core::ArtifactRef> inputs{emuflow::core::ArtifactRef::Bind(argv[3],"emuir-compact-v2"),emuflow::core::ArtifactRef::Bind(argv[4],"partition-cluster-columns-v2"),emuflow::core::ArtifactRef::Bind(argv[5],"partition-initialization-request-v1")};
    if(argc==9 || argc==10){inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[7],"net-weight-selector-v2"));inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[8],"net-weight-store-v1"));}
    if(argc==8 || argc==10)inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[argc-1],"tritonpart-solution-v1"));
    ToolSignals signals;return Finish(emuflow::core::InitializePartition(inputs,argv[6],&tool_cancelled),report);
  }
  if(argc>=8 && argc<=10 && std::string(argv[1])=="phase3" && std::string(argv[2])=="finalize-assignment-semantics") {
    std::vector<emuflow::core::ArtifactRef> inputs{
      emuflow::core::ArtifactRef::Bind(argv[3],"emuir-compact-v2"),
      emuflow::core::ArtifactRef::Bind(argv[4],"partition-cluster-columns-v2"),
      emuflow::core::ArtifactRef::Bind(argv[5],"candidate-policy-request-v1"),
      emuflow::core::ArtifactRef::Bind(argv[6],"partition-finalization-request-v1")};
    if(argc>=9)inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[8],argc==9?"patron-initial-columns-v1":"patron-output-v1"));
    if(argc==10)inputs.push_back(emuflow::core::ArtifactRef::Bind(argv[9],"patron-cluster-order-v1"));
    return Finish(emuflow::core::FinalizePartitionSemantics(inputs,argv[7]),report);
  }
  if((argc==4 || argc==5) && std::string(argv[1])=="phase1" && std::string(argv[2])=="run") {
    ToolSignals signals;
    return Finish(emuflow::core::RunFrontend(emuflow::core::ArtifactRef::Bind(argv[3],"frontend-request-v1"),&tool_cancelled,argc==5?argv[4]:""),report);
  }
  if ((argc == 6 || argc == 7) && std::string(argv[1]) == "phase1" && std::string(argv[2]) == "import-yosys") {
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);
    const auto request=emuflow::core::ArtifactRef::Bind(argv[3],"yosys-emuir-import-request-v1");
    if(argc==7){const auto library=emuflow::core::ArtifactRef::Bind(argv[6],"xilinx-primitive-library-v1");return Finish(emuflow::core::ImportYosysEmuir(request,std::cin,argv[4],argv[5],&library),report);}
    return Finish(emuflow::core::ImportYosysEmuir(request,std::cin,argv[4],argv[5]),report);
  }
  if ((argc == 6 || argc == 7) && std::string(argv[1]) == "phase1" && std::string(argv[2]) == "stage-yosys-hierarchy") {
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);
    if(argc==7){const auto library=emuflow::core::ArtifactRef::Bind(argv[6],"xilinx-primitive-library-v1");return Finish(emuflow::core::StageYosysHierarchy(emuflow::core::ArtifactRef::Bind(argv[3],"yosys-hierarchy-request-v2"),std::cin,argv[4],argv[5],&library),report);}
    return Finish(emuflow::core::StageYosysHierarchy(emuflow::core::ArtifactRef::Bind(argv[3],"yosys-hierarchy-request-v1"),std::cin,argv[4],argv[5]),report);
  }
  if (argc == 4 && std::string(argv[1]) == "run-tool" && report.empty())
    return RunTool(argv[2],argv[3]);
  if (argc == 3 && std::string(argv[1]) == "compile" && report.empty()) {
    ToolSignals signals;
    const auto configuration = emuflow::core::ArtifactRef::Bind(argv[2],"native-compile-request-v1");
    const auto native_executable=emuflow::core::ArtifactRef::Bind(emuflow::core::CurrentExecutablePath(),"external-tool-executable-v1");
    const auto result = emuflow::core::Compile(configuration,native_executable,&tool_cancelled);
    emuflow::core::PublishCompileResult(result);
    return Finish(result.operation,"");
  }
  const std::string phase = argc > 1 ? argv[1] : "";
  const std::string operation = argc > 2 ? argv[2] : "";
  if(phase=="phase3" && operation=="publish-partition" && argc>=12 && argc<=14) {
    using emuflow::core::ArtifactRef;emuflow::core::CheckedRequestReader request(argv[argc-2],8*1024*1024);
    if(request.Read<std::uint64_t>()!=0x31555050464d5545ULL || request.Read<std::uint32_t>()!=1)
      throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,"partition publication request is invalid");
    const auto kind=request.Read<std::uint32_t>();
    if(kind>2 || unsigned(argc)!=12+kind)throw emuflow::core::StageError(emuflow::core::ErrorCode::kInvalidRequest,"partition publication owner coverage disagrees");
    const char* schemas[]={"emuir-compact-v2","partition-cluster-columns-v2","exact-nodes-v2","exact-captures-v1","partition-cut-counts-v1","cluster-catalog-request-v1","candidate-id-columns-v1"};
    std::vector<ArtifactRef> refs;for(unsigned i=0;i<7;++i)refs.push_back(ArtifactRef::Bind(argv[3+i],schemas[i]));
    if(kind)refs.push_back(ArtifactRef::Bind(argv[10],kind==1?"patron-initial-columns-v1":"patron-output-v1"));
    if(kind==2)refs.push_back(ArtifactRef::Bind(argv[11],"patron-cluster-order-v1"));
    refs.push_back(ArtifactRef::Bind(argv[argc-2],"partition-publication-request-v1"));
    return Finish(emuflow::core::PublishPartition(refs,argv[argc-1]),report);
  }
  if(phase=="phase3" && operation=="publish-exact-binary" && argc==7) {
    using emuflow::core::ArtifactRef;
    return Finish(emuflow::core::PublishExactBinary({ArtifactRef::Bind(argv[3],"exact-nodes-v2"),
      ArtifactRef::Bind(argv[4],"exact-captures-v1"),ArtifactRef::Bind(argv[5],"exact-binary-publication-request-v1")},argv[6]),report);
  }
  auto run = [&](Stage stage, std::vector<std::string> inputs,
                 const std::string& output, bool typed = false,
                 bool indexed = false, const std::string& sha = "") {
    StageRequest request{stage, std::move(inputs), output};
    request.typed_input = typed; request.indexed_input = indexed;
    request.typed_output = stage == Stage::kRoute;
    request.expected_sha256 = sha;
    return Execute(std::move(request), report);
  };
  if (phase == "phase7" && operation == "validate-atomic-ownership" && argc == 8)
    return run(Stage::kValidateAtomicOwnership,{argv[3],argv[4],argv[5],argv[6]},argv[7]);
  if (phase == "phase7" && operation == "validate-physical-cells" && argc == 7)
    return run(Stage::kValidatePhysicalCells,{argv[3],argv[4],argv[5]},argv[6]);
  if (phase == "phase7" && operation == "export-vivado" && argc == 8) {
    StageRequest request{Stage::kExportVivado,{argv[3],argv[4]},argv[5]};
    request.auxiliary_output_path=argv[6];request.scratch_path=argv[7];
    return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="build-routed-segments" && argc>=11 && argc<=13) {
    StageRequest request;request.stage=Stage::kBuildRoutedSegments;
    request.output_path=argv[3];request.auxiliary_output_path=argv[4];
    for(int i=5;i<argc;++i)request.input_paths.emplace_back(argv[i]);
    return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="stage-routed-opensta" && argc==10) {
    StageRequest request{Stage::kStageRoutedOpensta,{argv[3],argv[4],argv[5],argv[6]},argv[7]};
    request.auxiliary_output_path=argv[8];request.scratch_path=argv[9];return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="build-sta-weights" && argc==9) {
    StageRequest request{Stage::kBuildStaWeights,{argv[3],argv[4],argv[5],argv[6]},argv[7]};request.auxiliary_output_path=argv[8];return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="validate-sta-weights" && argc==10)
    return run(Stage::kValidateStaWeights,{argv[3],argv[4],argv[5],argv[6],argv[7],argv[8]},argv[9]);
  if(phase=="physical" && operation=="import-sta-paths" && argc==8) {
    StageRequest request{Stage::kImportStaPaths,{argv[3],argv[4],argv[5]},argv[6]};request.auxiliary_output_path=argv[7];return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="validate-sta-paths" && argc==7)
    return run(Stage::kValidateStaPaths,{argv[3],argv[4],argv[5]},argv[6]);
  if(phase=="physical" && operation=="export-opensta-inputs" && argc==8) {
    StageRequest request{Stage::kExportOpenstaInputs,{argv[3],argv[4]},argv[7]};
    if(std::string(argv[5])!="-")request.auxiliary_output_path=argv[5];
    if(std::string(argv[6])!="-")request.scratch_path=argv[6];
    return Execute(std::move(request),report);
  }
  if(phase=="physical" && operation=="build-routed-endpoints" && argc==11) {
    StageRequest request{Stage::kBuildRoutedEndpoints,{argv[3],argv[4],argv[5],argv[6],argv[7]},argv[8]};
    request.auxiliary_output_path=argv[9];request.scratch_path=argv[10];return Execute(std::move(request),report);
  }
  if (phase == "phase7" && operation == "import-vivado-lineage" && argc >= 9) {
    StageRequest request{Stage::kImportVivadoLineage,{argv[3],argv[4],argv[5]},argv[6]};
    request.auxiliary_output_path=argv[7];
    for(int index=8;index<argc;++index) request.input_paths.emplace_back(argv[index]);
    return Execute(std::move(request),report);
  }
  if (phase == "phase7" && operation == "export-rwroute" && argc == 8) {
    StageRequest request{Stage::kExportRwroute,{argv[3],argv[4],argv[5]},argv[6]};
    request.auxiliary_output_path=argv[7];
    return Execute(std::move(request),report);
  }
  if (phase == "phase3" && operation == "refine-patron-from-primary" && argc == 9)
    return run(Stage::kRefinePatronFromPrimary,{argv[3],argv[4],argv[5],argv[6],argv[7]},argv[8]);
  if (phase == "phase4" && operation == "seal-routes" && argc == 6)
    return run(Stage::kSealRoutes,{argv[3],argv[4]},argv[5]);
  if (phase == "phase3" && operation == "build-captures" && argc == 8)
    return run(Stage::kBuildCaptures,{argv[3],argv[4],argv[5],argv[6]},argv[7]);
  if (phase == "phase3" && operation == "validate-captures" && argc == 9)
    return run(Stage::kValidateCaptures,{argv[3],argv[4],argv[5],argv[6],argv[7]},argv[8]);
  if (phase == "phase3" && operation == "publish-cluster-catalog" && (argc == 7 || argc == 8)) {
    std::vector<std::string> inputs{argv[3],argv[4],argv[5]};
    if(argc==8) inputs.push_back(argv[6]);
    return run(Stage::kPublishClusterCatalog,inputs,argv[argc-1]);
  }
  if (phase == "phase3" && operation == "validate-cuts" && argc == 5)
    return run(Stage::kValidateAssignmentCuts,{argv[3]},argv[4]);
  if(phase=="phase3" && operation=="validate-exact-binary" && argc==6)
    return run(Stage::kValidateExactBinary,{argv[3],argv[4]},argv[5]);
  if(phase=="phase3" && operation=="publish-assignment-descriptor" && argc==8)
    return run(Stage::kPublishAssignmentDescriptor,{argv[3],argv[4],argv[5],argv[6]},argv[7]);
  if (phase == "phase3" && operation == "publish-assignment" && argc>=7 && argc<=11) {
    std::vector<std::string> inputs{argv[3],argv[4],argv[5]};
    for(int i=6;i<argc-1;++i)inputs.emplace_back(argv[i]);
    return run(Stage::kPublishAssignmentIndex,inputs,argv[argc-1]);
  }
  if(phase=="phase3" && (operation=="reduce-loads" || operation=="audit-decision") && argc>=7 && argc<=10) {
    std::vector<std::string> inputs;for(int i=3;i<argc-1;++i)inputs.emplace_back(argv[i]);
    return run(operation=="reduce-loads"?Stage::kReducePartitionLoads:Stage::kAuditPartitionDecision,inputs,argv[argc-1]);
  }
  if(phase=="phase3" && operation=="normalize-configuration" && (argc==6 || argc==7)) {
    std::vector<std::string> inputs{argv[3],argv[4]};if(argc==7)inputs.emplace_back(argv[5]);
    return run(Stage::kNormalizePartitionConfiguration,inputs,argv[argc-1]);
  }
  if (phase == "phase3" && operation == "build-candidate-ids" && argc == 6)
    return run(Stage::kBuildCandidateIds,{argv[3],argv[4]},argv[5]);
  if (phase == "phase3" && operation == "build-clusters" && argc == 6)
    return run(Stage::kBuildPartitionClusters,{argv[3],argv[4]},argv[5]);
  if (phase == "phase3") {
    if (operation == "refine-hops") {
      int start = 3;
      bool trace = argc > start && std::string(argv[start]) == "--trace-output";
      if (trace) ++start;
      if (argc == start + 2) {
        StageRequest request{Stage::kRefinePartitionHops,{argv[start]},argv[start+1]};
        request.summary_output = !trace;
        return Execute(std::move(request),report);
      }
    }
    if (operation == "analyze" && argc == 5)
      return run(Stage::kAnalyzeExact, {argv[3]}, argv[4]);
    if (operation == "analyze" && argc == 7 && std::string(argv[3]) == "--indexed-input")
      return run(Stage::kAnalyzeExact, {argv[4], argv[5]}, argv[6], false, true);
    if (operation == "finalize-semantics" && argc == 6)
      return run(Stage::kFinalizeExact, {argv[3], argv[4]}, argv[5]);
    if (operation == "hash-semantics" && argc == 7)
      return run(Stage::kHashSemantics, {argv[3], argv[4], argv[5]}, argv[6]);
    if (operation == "write-exact-sections" && argc == 7)
      return run(Stage::kWriteExactSections, {argv[3], argv[4], argv[5]}, argv[6]);
    if (operation == "refine-patron") {
      StageRequest request{Stage::kRefinePatron, {}, ""};
      request.typed_input = false;  // Require an explicit CLI format flag.
      std::vector<std::string> positional;
      bool invalid = false, trace = false, summary = false, typed_output = false;
      for (int i = 3; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--typed-input") request.typed_input = true;
        else if (arg == "--typed-output") typed_output = true;
        else if (arg == "--summary-output") summary = true;
        else if (arg == "--trace-output") trace = true;
        else if (arg.rfind("--", 0) == 0) invalid = true;
        else positional.push_back(arg);
      }
      if (!invalid && !(trace && (summary || typed_output)) && positional.size() == 2 && request.typed_input) {
        request.summary_output = !trace;
        request.typed_output = !trace;
        request.input_paths = {positional[0]}; request.output_path = positional[1];
        return Execute(std::move(request), report);
      }
    }
  }
  if (phase == "phase4" && operation == "route" && argc == 6 && std::string(argv[3]) == "--typed-output")
    return run(Stage::kRoute, {argv[4]}, argv[5]);
  if(phase=="physical" && argc==8 && (operation=="import-boundary-timing" || operation=="import-logic-timing" || operation=="import-local-timing")) {
    const auto stage=operation=="import-boundary-timing"?Stage::kImportPhysicalBoundary:operation=="import-logic-timing"?Stage::kImportPhysicalLogic:Stage::kImportPhysicalLocal;
    StageRequest request{stage,{argv[3],argv[4],argv[5]},argv[6]};request.auxiliary_output_path=argv[7];return Execute(request,report);
  }
  if (phase == "physical" && operation == "validate-evidence" && argc==6)
    return run(Stage::kValidatePhysicalEvidence,{argv[3],argv[4]},argv[5]);
  if (phase == "global-sta" && operation == "import-measurements" && argc == 8)
    return Execute({Stage::kImportGlobalMeasurements,{argv[3],argv[4],argv[5],argv[6]},argv[7]},report);
  if (phase == "global-sta" && operation == "audit-measurements" && argc == 8)
    return Execute({Stage::kAuditGlobalMeasurements,{argv[3],argv[4],argv[5],argv[6]},argv[7]},report);
  if (phase == "global-sta" && operation == "publish-qor" && argc == 8)
    return Execute({Stage::kPublishGlobalQor,{argv[3],argv[4],argv[5],argv[6]},argv[7]},report);
  if (phase == "global-sta" && operation == "validate-qor" && argc == 6)
    return Execute({Stage::kValidateGlobalQor,{argv[3],argv[4]},argv[5]},report);
  if (phase == "global-sta" && operation == "bind-events" && argc>=8) {
    std::vector<std::string> inputs;for(int i=3;i<argc-1;++i)inputs.push_back(argv[i]);
    return run(Stage::kBindGlobalEvents,inputs,argv[argc-1]);
  }
  if (phase == "global-sta" && operation == "index-events" && argc == 5)
    return Execute({Stage::kIndexGlobalEvents,{argv[3]},argv[4]},report);
  if (phase == "global-sta" && operation == "export-events" && argc == 7)
    return Execute({Stage::kExportGlobalEvents,{argv[3],argv[4],argv[5]},argv[6]},report);
  if (phase == "phase5" && operation == "timing-contract" && argc >= 6 && argc <= 8) {
    std::vector<std::string> inputs;
    for(int i=3;i<argc-1;++i) inputs.push_back(argv[i]);
    return run(Stage::kProjectScheduleTiming,inputs,argv[argc-1]);
  }
  if (phase == "phase5" && operation == "validate-schedule" && argc == 8)
    return run(Stage::kValidateSchedule,{argv[3],argv[4],argv[5],argv[6]},argv[7]);
  if (phase == "phase5" && operation == "compile" && argc == 7)
    return run(Stage::kCompilePhase5, {argv[3],argv[4],argv[5]}, argv[6]);
  if (phase == "phase5" && operation == "run" && argc == 8)
    return run(Stage::kRunPhase5, {argv[3],argv[4],argv[5],argv[6]}, argv[7]);
  if (phase == "phase5" && operation == "build-schedule" && argc == 7)
    return run(Stage::kBuildSchedule, {argv[3],argv[4],argv[5]}, argv[6]);
  if (phase == "phase6") {
    if (operation == "rudy" && argc == 5)
      return run(Stage::kRudy, {argv[3]}, argv[4]);
    const std::string mode = argc > 3 ? argv[3] : "";
    if (operation == "assign-banks" && argc == 6 && mode == "--typed-input")
      return run(Stage::kAssignBanks, {argv[4]}, argv[5], mode == "--typed-input");
    if (operation == "group-signals" || operation == "refine-groups") {
      const auto stage = operation == "group-signals" ? Stage::kGroupSignals : Stage::kRefinePositions;
      if (argc == 7 && mode == "--typed-input")
        return run(stage, {argv[4]}, argv[5], true, false, argv[6]);
    }
  }
  if (phase == "phase7" && operation == "export-bookshelf" && argc == 8) {
    StageRequest request{Stage::kExportBookshelf,{argv[3],argv[4]},argv[5]};
    request.auxiliary_output_path = argv[6]; request.scratch_path = argv[7];
    return Execute(std::move(request),report);
  }
  emuflow::core::StageResult result;
  result.stage = "native.request"; result.error = emuflow::core::ErrorCode::kInvalidRequest;
  result.message = "unknown or invalid native stage request";
  std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result) << kUsage;
  return 2;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    std::string report;
    if (argc > 1 && std::string(argv[1]) == "--report") {
      if (argc < 4 || std::string(argv[2]).empty() || std::string(argv[2]) == "-") {
        std::cerr << kUsage; return 2;
      }
      report = argv[2]; argc -= 2; argv += 2;
    }
    return Dispatch(argc, argv, report);
  } catch (const emuflow::core::StageError& error) {
    emuflow::core::StageResult result;
    result.stage = "native.request"; result.error = error.code();
    result.message = error.what();
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
    return result.error == emuflow::core::ErrorCode::kInvalidRequest ? 2 : 1;
  } catch (const std::exception& error) {
    emuflow::core::StageResult result;
    result.stage = "native.request"; result.error = emuflow::core::ErrorCode::kExecutionFailed;
    result.message = error.what();
    std::cerr << "emuflow-native: " << emuflow::core::StageResultJson(result);
    return 1;
  }
}
