#include "flow/native_pipeline.hpp"
#include "routing/route_seal.hpp"
#include "physical/physical_cells.hpp"
#include "physical/physical_evidence.hpp"
#include "physical/physical_coverage.hpp"
#include "physical/atomic_ownership.hpp"
#include "common/artifact_io.hpp"
#include "flow/native_stages.hpp"
#include "scheduling/schedule_store.hpp"
#include "scheduling/phase5.hpp"
#include "scheduling/schedule_validation.hpp"
#include "scheduling/schedule_timing.hpp"
#include "timing/global_events.hpp"
#include "scheduling/schedule_ingress.hpp"
#include "physical/openparf/bookshelf.hpp"
#include "physical/rapidwright/rwroute_export.hpp"
#include "physical/vivado/vivado_lineage.hpp"
#include "physical/vivado/vivado_export.hpp"
#include "physical/rapidwright/routed_segments.hpp"
#include "physical/rapidwright/routed_endpoints.hpp"
#include "timing/routed_opensta.hpp"
#include "timing/opensta_export.hpp"
#include "timing/sta_paths.hpp"
#include "timing/sta_weights.hpp"
#include "partition/patron_ingress.hpp"
#include "partition/partition_clusters.hpp"
#include "partition/partition_loads.hpp"
#include "partition/partition_decision.hpp"
#include "partition/partition_config.hpp"
#include "partition/assignment_publish.hpp"
#include "partition/phase3_exact_binary.hpp"
#include "partition/assignment_cuts.hpp"
#include "partition/cluster_catalog.hpp"
#include "partition/exact_captures.hpp"
#include "partition/candidate_ids.hpp"
#include "storage/sqlite.hpp"

#include <filesystem>
#include <iostream>

namespace emuflow::native {
const char* StageName(Stage stage) {
  switch (stage) {
    case Stage::kInvalid: return "native.request";
    case Stage::kImportPhysicalBoundary: return "physical.import-boundary-timing";
    case Stage::kImportPhysicalLogic: return "physical.import-logic-timing";
    case Stage::kImportPhysicalLocal: return "physical.import-local-timing";
    case Stage::kValidatePhysicalEvidence: return "physical.validate-evidence";
    case Stage::kBuildPhysicalCoverage: return "physical.build-coverage";
    case Stage::kAuditPhysicalCoverage: return "physical.audit-coverage";
    case Stage::kValidateAtomicOwnership: return "phase7.validate-atomic-ownership";
    case Stage::kValidatePhysicalCells: return "phase7.validate-physical-cells";
    case Stage::kSealRoutes: return "phase4.seal-routes";
    case Stage::kExportVivado: return "phase7.export-vivado";
    case Stage::kBuildRoutedSegments: return "physical.build-routed-segments";
    case Stage::kBuildRoutedEndpoints: return "physical.build-routed-endpoints";
    case Stage::kStageRoutedOpensta: return "physical.stage-routed-opensta";
    case Stage::kExportOpenstaInputs: return "physical.export-opensta-inputs";
    case Stage::kBuildStaWeights: return "physical.build-sta-weights";
    case Stage::kValidateStaWeights: return "physical.validate-sta-weights";
    case Stage::kImportStaPaths: return "physical.import-sta-paths";
    case Stage::kValidateStaPaths: return "physical.validate-sta-paths";
    case Stage::kImportVivadoLineage: return "phase7.import-vivado-lineage";
    case Stage::kExportRwroute: return "phase7.export-rwroute";
    case Stage::kBuildCaptures: return "phase3.build-captures";
    case Stage::kValidateCaptures: return "phase3.validate-captures";
    case Stage::kPublishAssignmentIndex: return "phase3.publish-assignment";
    case Stage::kValidateExactBinary: return "phase3.validate-exact-binary";
    case Stage::kPublishAssignmentDescriptor: return "phase3.publish-assignment-descriptor";
    case Stage::kValidateAssignmentCuts: return "phase3.validate-cuts";
    case Stage::kPublishClusterCatalog: return "phase3.publish-cluster-catalog";
    case Stage::kReducePartitionLoads: return "phase3.reduce-loads";
    case Stage::kAuditPartitionDecision: return "phase3.audit-decision";
    case Stage::kNormalizePartitionConfiguration: return "phase3.normalize-configuration";
    case Stage::kAnalyzeExact: return "phase3.analyze";
    case Stage::kFinalizeExact: return "phase3.finalize-semantics";
    case Stage::kHashSemantics: return "phase3.hash-semantics";
    case Stage::kWriteExactSections: return "phase3.write-exact-sections";
    case Stage::kRoute: return "phase4.route";
    case Stage::kCompilePhase5: return "phase5.compile";
    case Stage::kRunPhase5: return "phase5.run";
    case Stage::kBuildSchedule: return "phase5.build-schedule";
    case Stage::kProjectScheduleTiming: return "phase5.timing-contract";
    case Stage::kBindGlobalEvents: return "global-sta.bind-events";
    case Stage::kIndexGlobalEvents: return "global-sta.index-events";
    case Stage::kExportGlobalEvents: return "global-sta.export-events";
    case Stage::kImportGlobalMeasurements: return "global-sta.import-measurements";
    case Stage::kAuditGlobalMeasurements: return "global-sta.audit-measurements";
    case Stage::kPublishGlobalQor: return "global-sta.publish-qor";
    case Stage::kValidateGlobalQor: return "global-sta.validate-qor";
    case Stage::kValidateSchedule: return "phase5.validate-schedule";
    case Stage::kGroupSignals: return "phase6.group-signals";
    case Stage::kRefinePositions: return "phase6.refine-groups";
    case Stage::kRudy: return "phase6.rudy";
    case Stage::kExportBookshelf: return "phase7.export-bookshelf";
    case Stage::kBuildPartitionClusters: return "phase3.build-clusters";
    case Stage::kBuildCandidateIds: return "phase3.build-candidate-ids";
    case Stage::kRefinePatronFromPrimary: return "phase3.refine-patron-from-primary";
  }
  return "unknown-native-stage";
}
namespace {
std::vector<std::string> InputSchemas(const StageRequest& request) {
  switch (request.stage) {
    case Stage::kInvalid: break;
    case Stage::kValidateAtomicOwnership: return {"xilinx-mapped-index-v2","openparf-atomic-source-store-v1","openparf-placement-certificate-v3","atomic-ownership-request-v1"};
    case Stage::kImportPhysicalBoundary: return {"boundary-identity-store-v1","physical-timing-measurements-v1","physical-measurement-import-request-v1"};
    case Stage::kImportPhysicalLogic: return {"logic-segment-identity-store-v1","physical-timing-measurements-v1","physical-measurement-import-request-v1"};
    case Stage::kImportPhysicalLocal: return {"local-path-timing-store-v1","physical-timing-measurements-v1","physical-measurement-import-request-v1"};
    case Stage::kBuildPhysicalCoverage:
    case Stage::kAuditPhysicalCoverage: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      const auto minimum=request.stage==Stage::kBuildPhysicalCoverage?2u:3u;
      if(count<minimum || count>2049)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"physical coverage source count is invalid");
      const auto path=request.input_artifacts.empty()?request.input_paths.back():request.input_artifacts.back().path();
      return core::PhysicalCoverageInputSchemas(path);
    }
    case Stage::kValidatePhysicalEvidence: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count!=2)throw core::StageError(core::ErrorCode::kInvalidRequest,"physical evidence source coverage disagrees");
      const auto path=request.input_artifacts.empty()?request.input_paths.back():request.input_artifacts.back().path();
      core::CheckedRequestReader reader(path,8*1024*1024);
      if(reader.Read<std::uint64_t>()!=0x31564550464d5545ULL || reader.Read<std::uint32_t>()!=1)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"physical evidence request version is invalid");
      const auto kind=reader.Read<std::uint32_t>();
      const std::vector<std::string> schemas{"boundary-identity-store-v1","boundary-timing-store-v1","logic-segment-timing-store-v1","local-path-timing-store-v1","logic-segment-identity-store-v1"};
      if(kind<1 || kind>schemas.size())throw core::StageError(core::ErrorCode::kInvalidRequest,"physical evidence kind is invalid");
      return {schemas[kind-1],"physical-evidence-validation-request-v1"};
    }
    case Stage::kValidatePhysicalCells: return {"xilinx-mapped-index-v2","xilinx-physical-handoff-v1","physical-cell-check-request-v1"};
    case Stage::kSealRoutes: return {"system-route-store-v3","system-route-seal-request-v1"};
    case Stage::kImportVivadoLineage: {
      auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count<4 || count>67) throw core::StageError(core::ErrorCode::kInvalidRequest,"Vivado inventory input count is invalid");
      std::vector<std::string> schemas{"xilinx-mapped-index-v2","vivado-source-functions-v1","vivado-lineage-import-request-v1"};
      schemas.insert(schemas.end(),count-3,"vivado-lineage-inventory-v2");return schemas;
    }
    case Stage::kBuildRoutedSegments: {
      const auto& refs=request.input_artifacts;
      const auto count=refs.empty()?request.input_paths.size():refs.size();
      if(count<6 || count>8)throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segment source coverage is invalid");
      core::CheckedRequestReader reader(refs.empty()?request.input_paths.back():refs.back().path(),16*1024*1024);
      if(reader.Read<std::uint64_t>()!=0x31544752464d5545ULL || reader.Read<std::uint32_t>()!=1)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segment request version is invalid");
      const auto flags=reader.Read<std::uint32_t>();
      if(flags>3 || count!=6+unsigned(bool(flags&1))+unsigned(bool(flags&2)))
        throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segment projection coverage is invalid");
      std::vector<std::string> schemas{"xilinx-mapped-index-v2","emuir-compact-v2","xilinx-routed-timing-v2","xilinx-routed-endpoints-v1","boundary-identity-store-v1"};
      if(flags&1)schemas.push_back("logic-segment-identity-store-v1");
      if(flags&2)schemas.push_back("local-path-timing-store-v1");
      schemas.push_back("routed-segment-request-v1");return schemas;
    }
    case Stage::kStageRoutedOpensta: return {"xilinx-mapped-index-v2","xilinx-routed-timing-v2","xilinx-routed-endpoints-v1","routed-opensta-request-v1"};
    case Stage::kExportOpenstaInputs: return {"emuir-compact-v2","opensta-input-export-request-v1"};
    case Stage::kBuildStaWeights: return {"emuir-compact-v2","sta-path-selector-v1","timing-path-store-v1","sta-weight-request-v1"};
    case Stage::kValidateStaWeights: return {"emuir-compact-v2","sta-path-selector-v1","timing-path-store-v1","net-weight-selector-v2","net-weight-store-v1","sta-weight-request-v1"};
    case Stage::kImportStaPaths: return {"emuir-compact-v2","sta-source-document-v1","sta-path-request-v1"};
    case Stage::kValidateStaPaths: return {"emuir-compact-v2","timing-path-store-v1","sta-path-request-v1"};
    case Stage::kBuildRoutedEndpoints: return {"xilinx-mapped-index-v2","xilinx-physical-handoff-v1","xilinx-route-manifest-v2","xilinx-route-nets-v1","routed-endpoint-request-v1"};
    case Stage::kExportVivado: return {"xilinx-mapped-index-v2","vivado-export-request-v1"};
    case Stage::kExportRwroute: return {"xilinx-mapped-index-v2","xilinx-physical-handoff-v1","rwroute-export-request-v2"};
    case Stage::kBuildCaptures: return {"emuir-compact-v2","exact-indexed-request-v4","exact-topology-v3","exact-parts-v1"};
    case Stage::kValidateCaptures: return {"emuir-compact-v2","exact-indexed-request-v4","exact-topology-v3","exact-parts-v1","exact-captures-v1"};
    case Stage::kValidateAssignmentCuts: return {"assignment-index-v4"};
    case Stage::kValidateExactBinary: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count!=2)throw core::StageError(core::ErrorCode::kInvalidRequest,"exact binary validation source coverage disagrees");
      const auto path=request.input_artifacts.empty()?request.input_paths[1]:request.input_artifacts[1].path();
      core::CheckedRequestReader input(path,8*1024*1024);
      if(input.Read<std::uint64_t>()!=0x31565145554d5545ULL || input.Read<std::uint32_t>()!=1)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"exact binary validation request is invalid");
      const auto version=input.Read<std::uint32_t>();
      if(version!=1 && version!=2)throw core::StageError(core::ErrorCode::kInvalidRequest,"exact binary validation format version is invalid");
      return {"phase3-exact-data-v"+std::to_string(version),"exact-binary-validation-request-v1"};
    }
    case Stage::kPublishAssignmentDescriptor:
      return {"emuir-compact-v2","partition-cluster-columns-v2","assignment-index-v4","assignment-descriptor-request-v1"};
    case Stage::kPublishAssignmentIndex: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count==3) return {"emuir-compact-v2","partition-cluster-columns-v2","assignment-publication-request-v1"};
      if(count==4) return {"emuir-compact-v2","partition-cluster-columns-v2","assignment-publication-request-v2","exact-nodes-v2"};
      if(count>=5 && count<=7) {
        std::vector<std::string> schemas{"emuir-compact-v2","partition-cluster-columns-v2","assignment-publication-request-v3","exact-nodes-v2","partition-cut-counts-v1"};
        if(count>=6)schemas.push_back(count==6?"patron-initial-columns-v1":"patron-output-v1");
        if(count==7)schemas.push_back("patron-cluster-order-v1");
        return schemas;
      }
      throw core::StageError(core::ErrorCode::kInvalidRequest,"assignment publication input coverage disagrees");
    }
    case Stage::kPublishClusterCatalog: {
      auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count!=3 && count!=4) throw core::StageError(core::ErrorCode::kInvalidRequest,"cluster catalog source count disagrees");
      std::vector<std::string> schemas{"emuir-compact-v2","partition-cluster-columns-v2","cluster-catalog-request-v1"};
      if(count==4) schemas.push_back("candidate-id-columns-v1");
      return schemas;
    }
    case Stage::kNormalizePartitionConfiguration: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count==2)return {"emuir-compact-v2","partition-configuration-request-v1"};
      if(count==3)return {"emuir-compact-v2","partition-configuration-request-v1","partition-constraints-input-v1"};
      throw core::StageError(core::ErrorCode::kInvalidRequest,"partition configuration input coverage disagrees");
    }
    case Stage::kAuditPartitionDecision:
    case Stage::kReducePartitionLoads: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count<3 || count>6)throw core::StageError(core::ErrorCode::kInvalidRequest,"partition owner input coverage disagrees");
      const auto path=request.input_artifacts.empty()?request.input_paths[2]:request.input_artifacts[2].path();
      if(!std::filesystem::exists(path))
        throw core::StageError(core::ErrorCode::kMissingArtifact,"partition ownership request is missing: "+path);
      core::CheckedRequestReader input(path,8*1024*1024);
      const auto magic=input.Read<std::uint64_t>();const auto version=input.Read<std::uint32_t>();
      std::vector<std::string> schemas{"emuir-compact-v2","partition-cluster-columns-v2"};
      if(request.stage==Stage::kAuditPartitionDecision) {
        if(magic!=0x31514450464d5545ULL || version!=1 || input.Read<std::uint32_t>()!=count-3)
          throw core::StageError(core::ErrorCode::kInvalidRequest,"partition decision request kind disagrees");
        schemas.push_back("partition-decision-request-v1");
      } else {
        if(magic==0x3151454c504d5545ULL && version==1 && count==3)
          return {"emuir-compact-v2","partition-cluster-columns-v2","partition-load-request-v1"};
        if(magic!=0x3251454c504d5545ULL || version!=2 || count>5)
          throw core::StageError(core::ErrorCode::kInvalidRequest,"partition load request kind disagrees");
        schemas.push_back("partition-load-request-v2");
      }
      if(count==4 || count==6)schemas.push_back("patron-initial-columns-v1");
      if(count>=5) {schemas.push_back("patron-output-v1");schemas.push_back("patron-cluster-order-v1");}
      return schemas;
    }
    case Stage::kAnalyzeExact:
      return request.indexed_input
          ? std::vector<std::string>{"emuir-compact-v2", "exact-indexed-request-v4"}
          : std::vector<std::string>{"exact-analysis-input-v3"};
    case Stage::kFinalizeExact: return {"exact-topology-v3", "exact-identities-v1"};
    case Stage::kHashSemantics:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-hash-request-v1"};
    case Stage::kWriteExactSections:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-section-request-v1"};
    case Stage::kRoute: return {"global-route-input-v1"};
    case Stage::kProjectScheduleTiming: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count==2) return {"system-route-store-v3","phase5-timing-request-v1"};
      if(count==3) return {"system-route-store-v3","tdm-schedule-store-v4","phase5-timing-request-v1"};
      if(count==4) return {"system-route-store-v3","tdm-schedule-store-v4","exact-schedule-board-request-v1","phase5-timing-request-v1"};
      throw core::StageError(core::ErrorCode::kInvalidRequest,"timing projection input coverage disagrees");
    }
    case Stage::kValidateSchedule: return {"system-route-store-v3", "exact-schedule-board-request-v1", "tdm-schedule-store-v4", "exact-schedule-validation-request-v1"};
    case Stage::kImportGlobalMeasurements: return {"global-event-primary-v1","global-event-index-v1","global-measurement-tsv-v1","global-measurement-import-request-v1"};
    case Stage::kAuditGlobalMeasurements: return {"global-event-primary-v1","global-event-index-v1","global-measurements-store-v1","global-measurement-audit-request-v1"};
    case Stage::kPublishGlobalQor: return {"global-event-primary-v1","global-event-index-v1","global-measurements-store-v1","global-qor-request-v1"};
    case Stage::kValidateGlobalQor: return {"qor-report-store-v1","global-qor-validation-request-v1"};
    case Stage::kBindGlobalEvents: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count<4)throw core::StageError(core::ErrorCode::kInvalidRequest,"global binding source coverage is invalid");
      const auto path=request.input_artifacts.empty()?request.input_paths.back():request.input_artifacts.back().path();
      core::CheckedRequestReader reader(path,8*1024*1024);
      if(reader.Read<std::uint64_t>()!=0x31424747464d5545ULL || reader.Read<std::uint32_t>()!=1)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"global binding request version is invalid");
      const auto boundaries=reader.Read<std::uint32_t>(),locals=reader.Read<std::uint32_t>();
      if(boundaries>256 || locals>256 || count!=4+3*boundaries+locals)
        throw core::StageError(core::ErrorCode::kInvalidRequest,"global binding source coverage is invalid");
      std::vector<std::string> schemas{"system-route-store-v3","tdm-schedule-store-v4","exact-schedule-board-request-v1"};
      for(unsigned i=0;i<boundaries;++i)for(const char* schema:{"boundary-timing-store-v1","boundary-identity-store-v1","logic-segment-timing-store-v1"})schemas.push_back(schema);
      schemas.insert(schemas.end(),locals,"local-path-timing-store-v1");schemas.push_back("global-event-binding-request-v1");return schemas;
    }
    case Stage::kIndexGlobalEvents: return {"global-event-primary-v1"};
    case Stage::kExportGlobalEvents: return {"global-event-primary-v1","global-event-index-v1","global-event-export-request-v1"};
    case Stage::kCompilePhase5: return {"system-route-selector-v1","assignment-index-v4","boarddb-v1"};
    case Stage::kRunPhase5: return {"system-route-store-v3", "exact-schedule-board-request-v1", "exact-schedule-generation-request-v1", "phase5-stage-request-v1"};
    case Stage::kBuildSchedule: return {"system-route-store-v3", "exact-schedule-board-request-v1", "exact-schedule-generation-request-v1"};
    case Stage::kGroupSignals:
    case Stage::kRefinePositions:
      if (!request.typed_input)
        throw core::StageError(core::ErrorCode::kInvalidRequest, "Chimew requires typed input");
      if (request.stage == Stage::kGroupSignals) return {"chimew-group-input-v1"};
      return {"chimew-refine-input-v1"};
    case Stage::kRudy: return {"chimew-rudy-input-v1"};
    case Stage::kExportBookshelf: return {"xilinx-mapped-index-v2", "openparf-atom-index-private-v1"};
    case Stage::kBuildCandidateIds: return {"emuir-compact-v2","candidate-policy-request-v1"};
    case Stage::kBuildPartitionClusters: return {"emuir-compact-v2","partition-cluster-request-v1"};
    case Stage::kRefinePatronFromPrimary:
      return {"emuir-compact-v2","timing-path-store-v1","partition-cluster-columns-v2","patron-initial-columns-v1","patron-board-request-v2"};
  }
  throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
}
bool Equivalent(const std::string& a, const std::string& b) {
  std::error_code error;
  if (std::filesystem::equivalent(a, b, error)) return true;
  return std::filesystem::absolute(a).lexically_normal() ==
         std::filesystem::absolute(b).lexically_normal();
}
}  // namespace
core::StageResult ExecuteStage(const StageRequest& request) {
  return core::RunStage(StageName(request.stage), [&]() {
    const auto schemas = InputSchemas(request);
    const bool bound = !request.input_artifacts.empty();
    if ((bound && !request.input_paths.empty()) ||
        (bound ? request.input_artifacts.size() : request.input_paths.size()) != schemas.size() || request.output_path.empty())
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage artifact coverage is invalid");
    std::vector<std::string> paths = request.input_paths;
    if (bound) for (const auto& input : request.input_artifacts) paths.push_back(input.path());
    if(request.stage==Stage::kBuildRoutedSegments) {
      const auto root=std::filesystem::path(request.auxiliary_output_path);
      if(root.empty() || std::filesystem::is_symlink(root) || !std::filesystem::is_directory(root) || !std::filesystem::is_empty(root) ||
         !request.scratch_path.empty() ||
         !std::filesystem::is_directory(std::filesystem::absolute(request.output_path).parent_path()) ||
         std::filesystem::is_directory(request.output_path) || std::filesystem::is_symlink(request.output_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segments require an empty private output directory");
      const auto prefix=std::filesystem::weakly_canonical(root).string()+"/";
      for(const auto& path:std::vector<std::string>{request.output_path,request.report_path})
        if(!path.empty() && std::filesystem::weakly_canonical(path).string().rfind(prefix,0)==0)
          throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segment summaries must be outside the private store directory");
      for(const auto& input:paths)
        if(Equivalent(input,request.output_path) || std::filesystem::weakly_canonical(input)==std::filesystem::weakly_canonical(request.output_path))
          throw core::StageError(core::ErrorCode::kInvalidRequest,"routed segment summary aliases an input");
    }
    if (request.stage == Stage::kBuildStaWeights || request.stage == Stage::kImportStaPaths || request.stage == Stage::kExportOpenstaInputs || request.stage == Stage::kStageRoutedOpensta || request.stage == Stage::kBuildRoutedEndpoints || request.stage == Stage::kExportVivado || request.stage == Stage::kExportBookshelf || request.stage == Stage::kExportRwroute || request.stage == Stage::kImportVivadoLineage || request.stage == Stage::kImportPhysicalBoundary || request.stage == Stage::kImportPhysicalLogic || request.stage == Stage::kImportPhysicalLocal) {
      if (!bound) {
        try { for (auto& path : paths) path = core::SqliteFilePath(path); }
        catch (const std::exception& error) {
          throw core::StageError(core::ErrorCode::kInvalidRequest,error.what());
        }
      }
      std::vector<std::string> destinations{
          request.output_path,request.auxiliary_output_path};
      if(request.stage==Stage::kExportOpenstaInputs) {
        destinations={request.output_path};
        if(!request.auxiliary_output_path.empty())destinations.push_back(request.auxiliary_output_path);
        if(!request.scratch_path.empty())destinations.push_back(request.scratch_path);
        if(destinations.size()==1)throw core::StageError(core::ErrorCode::kInvalidRequest,"OpenSTA export requires at least one text output");
      }
      else if (request.stage == Stage::kStageRoutedOpensta || request.stage == Stage::kBuildRoutedEndpoints || request.stage == Stage::kExportBookshelf || request.stage == Stage::kExportVivado) destinations.push_back(request.scratch_path);
      else if (!request.scratch_path.empty())
        throw core::StageError(core::ErrorCode::kInvalidRequest,"unexpected RWRoute scratch destination");
      if (!request.report_path.empty()) destinations.push_back(request.report_path);
      const auto same = [&](const auto& a, const auto& b) {
        return Equivalent(a,b) || std::filesystem::weakly_canonical(a) == std::filesystem::weakly_canonical(b);
      };
      for (std::size_t i=0; i<destinations.size(); ++i) {
        const auto& destination = destinations[i];
        if (destination.empty() || destination == "-" ||
            !std::filesystem::is_directory(std::filesystem::absolute(destination).parent_path()) ||
            std::filesystem::is_directory(destination))
          throw core::StageError(core::ErrorCode::kInvalidRequest,"physical export output directory is unavailable");
        for (const auto& input : paths)
          if (same(input,destination))
            throw core::StageError(core::ErrorCode::kInvalidRequest,"physical export destination aliases an input");
        for (std::size_t j=0; j<i; ++j)
          if (same(destinations[j],destination))
            throw core::StageError(core::ErrorCode::kInvalidRequest,"physical export destinations alias each other");
      }
      if (request.stage == Stage::kExportBookshelf && (std::filesystem::exists(request.scratch_path) || std::filesystem::is_symlink(request.scratch_path)))
        throw core::StageError(core::ErrorCode::kInvalidRequest,"Bookshelf scratch relation already exists");
    } else if (request.stage!=Stage::kBuildRoutedSegments && (!request.auxiliary_output_path.empty() || !request.scratch_path.empty())) {
      throw core::StageError(core::ErrorCode::kInvalidRequest,"unexpected auxiliary stage destinations");
    }
    const bool stream_stage = request.stage == Stage::kGroupSignals ||
        request.stage == Stage::kRefinePositions || request.stage == Stage::kRudy;
    const bool pipe_input = stream_stage;
    if (request.output_path == "-" && !stream_stage)
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage output requires a file");
    std::vector<core::ArtifactRef> inputs;
    for (std::size_t i = 0; i < schemas.size(); ++i) {
      if (paths[i] != "-" && !request.report_path.empty() &&
          Equivalent(paths[i], request.report_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases an input artifact");
    }
    if (!request.report_path.empty() && request.output_path != "-" && Equivalent(request.report_path, request.output_path))
      throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases the stage output");
    if (request.stage == Stage::kRefinePatronFromPrimary) {
      for (const auto* suffix : {".order",".summary.json"}) {
        const auto auxiliary=request.output_path+suffix;
        for (const auto& path:paths)
          if (Equivalent(path,auxiliary))
            throw core::StageError(core::ErrorCode::kInvalidRequest,"PATRON auxiliary output aliases an input");
        if (!request.report_path.empty() && Equivalent(request.report_path,auxiliary))
          throw core::StageError(core::ErrorCode::kInvalidRequest,"PATRON report aliases an auxiliary output");
      }
    }
    for (std::size_t i = 0; i < schemas.size(); ++i) {
      auto input = bound ? request.input_artifacts[i] : core::ArtifactRef::Bind(paths[i], schemas[i], pipe_input);
      if (input.is_pipe() && !pipe_input)
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native stage input requires a file");
      if (input.schema() != schemas[i])
        throw core::StageError(core::ErrorCode::kInvalidArtifact, "native input artifact schema disagrees with the stage contract");
      if (!input.is_pipe() && request.output_path != "-" && Equivalent(input.path(), request.output_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native output aliases an input artifact");
      if (!request.report_path.empty() && !input.is_pipe() && Equivalent(input.path(), request.report_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest, "native report aliases an input artifact");
      inputs.push_back(std::move(input));
    }
    return inputs;
  }, [&](const auto& inputs) {
    const auto path = [&](std::size_t i) -> const std::string& { return inputs.at(i).path(); };
    const auto& output = request.output_path;
    if (request.stage == Stage::kGroupSignals ||
        request.stage == Stage::kRefinePositions || request.stage == Stage::kRudy) {
      const auto produce = [&](std::ostream& stream) {
        switch (request.stage) {
          case Stage::kGroupSignals: GroupChimewSignals(path(0),stream,request.expected_sha256); break;
          case Stage::kRefinePositions: RefineChimewPositions(path(0),stream,request.expected_sha256); break;
          case Stage::kRudy: EvaluateChimewRudy(path(0),stream); break;
          default: throw core::StageError(core::ErrorCode::kInvalidRequest,"unknown text kernel");
        }
      };
      if (output == "-") {
        produce(std::cout);
        std::cout.flush();
        if (!std::cout) throw core::StageError(core::ErrorCode::kPublicationFailed,"cannot flush native stage output");
        for (const auto& input : inputs) input.RequireUnchanged();
      } else core::PublishTextArtifact(output,inputs,produce);
      return;
    }
    switch (request.stage) {
      case Stage::kValidateAtomicOwnership:
        core::ValidateAtomicOwnership(inputs,output); break;
      case Stage::kImportPhysicalBoundary: core::ImportPhysicalMeasurements(inputs,2,output,request.auxiliary_output_path); break;
      case Stage::kImportPhysicalLogic: core::ImportPhysicalMeasurements(inputs,3,output,request.auxiliary_output_path); break;
      case Stage::kImportPhysicalLocal: core::ImportPhysicalMeasurements(inputs,4,output,request.auxiliary_output_path); break;
      case Stage::kValidatePhysicalEvidence: core::ValidatePhysicalEvidence(inputs,output); break;
      case Stage::kBuildPhysicalCoverage: core::BuildPhysicalCoverage(inputs,output); break;
      case Stage::kAuditPhysicalCoverage: core::AuditPhysicalCoverage(inputs,output); break;
      case Stage::kValidatePhysicalCells:
        core::ValidatePhysicalCells(inputs[0],inputs[1],inputs[2],output); break;
      case Stage::kImportVivadoLineage:
        core::ImportVivadoLineage(inputs,output,request.auxiliary_output_path); break;
      case Stage::kBuildRoutedSegments:
        core::BuildRoutedSegments(inputs,request.auxiliary_output_path,output);break;
      case Stage::kStageRoutedOpensta:
        core::StageRoutedOpensta(inputs,output,request.auxiliary_output_path,request.scratch_path);break;
      case Stage::kExportOpenstaInputs:
        core::ExportOpenstaInputs(inputs,output,request.auxiliary_output_path,request.scratch_path);break;
      case Stage::kBuildStaWeights:
        core::BuildStaWeights(inputs,output,request.auxiliary_output_path);break;
      case Stage::kValidateStaWeights:
        core::ValidateStaWeights(inputs,output);break;
      case Stage::kImportStaPaths:
        core::ImportStaPaths(inputs,output,request.auxiliary_output_path);break;
      case Stage::kValidateStaPaths:
        core::ValidateStaPaths(inputs,output);break;
      case Stage::kBuildRoutedEndpoints:
        core::BuildRoutedEndpoints(inputs,output,request.auxiliary_output_path,request.scratch_path);break;
      case Stage::kExportVivado:
        core::ExportVivado(inputs,output,request.auxiliary_output_path,request.scratch_path); break;
      case Stage::kExportRwroute:
        core::ExportRwroute(inputs[0],inputs[1],inputs[2],output,request.auxiliary_output_path); break;
      case Stage::kPublishAssignmentIndex: core::PublishAssignmentIndex(inputs,output); break;
      case Stage::kValidateExactBinary: core::ValidateExactBinary(inputs,output); break;
      case Stage::kPublishAssignmentDescriptor: core::PublishAssignmentDescriptor(inputs,output); break;
      case Stage::kValidateAssignmentCuts: core::ValidateAssignmentCuts(inputs[0],output); break;
      case Stage::kPublishClusterCatalog: core::PublishClusterCatalog(inputs,output); break;
      case Stage::kReducePartitionLoads:
        if(inputs[2].schema()=="partition-load-request-v2")core::ReduceOwnedPartitionLoads(inputs,output);
        else core::ReducePartitionLoads(inputs,output);break;
      case Stage::kAuditPartitionDecision: core::AuditPartitionDecision(inputs,output); break;
      case Stage::kNormalizePartitionConfiguration: core::PreparePartitionConfiguration(inputs,output); break;
      case Stage::kBuildCaptures: core::BuildExactCaptures(inputs,output); break;
      case Stage::kValidateCaptures: core::ValidateExactCaptures(inputs,output); break;
      case Stage::kAnalyzeExact:
        AnalyzeStaticExact(inputs, output);
        break;
      case Stage::kFinalizeExact: FinalizeStaticExact(inputs, output); break;
      case Stage::kHashSemantics: HashStaticExactSemantics(inputs, output); break;
      case Stage::kWriteExactSections: WriteStaticExactSections(inputs, output); break;
      case Stage::kRoute: RouteTlr(inputs, output); break;
      case Stage::kSealRoutes: core::SealSystemRoutes(inputs,output); break;
      case Stage::kCompilePhase5: core::RunPhase5FromStores(inputs,output); break;
      case Stage::kRunPhase5: core::RunPhase5(inputs,output); break;
      case Stage::kBuildSchedule: core::BuildExactScheduleStore(inputs,output); break;
      case Stage::kProjectScheduleTiming: core::ProjectScheduleTiming(inputs,output); break;
      case Stage::kImportGlobalMeasurements: core::ImportGlobalMeasurements(inputs,output); break;
      case Stage::kAuditGlobalMeasurements: core::AuditGlobalMeasurements(inputs,output); break;
      case Stage::kPublishGlobalQor: core::PublishGlobalQor(inputs,output); break;
      case Stage::kValidateGlobalQor: core::ValidateGlobalQor(inputs,output); break;
      case Stage::kBindGlobalEvents: core::BindGlobalEvents(inputs,output); break;
      case Stage::kIndexGlobalEvents: core::IndexGlobalEvents(inputs[0],output); break;
      case Stage::kExportGlobalEvents: core::ExportGlobalEvents(inputs,output); break;
      case Stage::kValidateSchedule: core::ValidateExactSchedule(inputs,output); break;
      case Stage::kExportBookshelf:
        core::ExportBookshelf(inputs[0],inputs[1],output,request.auxiliary_output_path,request.scratch_path);
        break;
      case Stage::kRefinePatronFromPrimary: core::RefinePatronFromPrimary(inputs,output); break;
      case Stage::kBuildPartitionClusters: core::BuildPartitionClusters(inputs,output); break;
      case Stage::kBuildCandidateIds: core::BuildCandidateIds(inputs,output); break;
      default: throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
    }
    if (output == "-") {
      std::cout.flush();
      if (!std::cout) throw std::runtime_error("cannot flush native stage output");
    }
  }, [&]() {
    const char* schema = nullptr;
    switch (request.stage) {
      case Stage::kValidateAtomicOwnership: schema = "atomic-ownership-summary-v1"; break;
      case Stage::kImportPhysicalBoundary: schema = "boundary-timing-store-v1"; break;
      case Stage::kImportPhysicalLogic: schema = "logic-segment-timing-store-v1"; break;
      case Stage::kImportPhysicalLocal: schema = "local-path-timing-store-v1"; break;
      case Stage::kValidatePhysicalEvidence: schema = "physical-evidence-validation-summary-v1"; break;
      case Stage::kBuildPhysicalCoverage: schema = "physical-coverage-store-v1"; break;
      case Stage::kAuditPhysicalCoverage: schema = "physical-coverage-summary-v1"; break;
      case Stage::kValidatePhysicalCells: schema = "physical-cell-check-summary-v1"; break;
      case Stage::kImportVivadoLineage: schema = "vivado-lineage-store-v2"; break;
      case Stage::kBuildRoutedSegments: schema = "routed-segment-summary-v1"; break;
      case Stage::kStageRoutedOpensta: schema = "emuir-compact-v2"; break;
      case Stage::kExportOpenstaInputs: schema = "opensta-input-export-summary-v1"; break;
      case Stage::kBuildStaWeights: schema = "net-weight-store-v1"; break;
      case Stage::kValidateStaWeights: schema = "sta-weight-validation-v1"; break;
      case Stage::kImportStaPaths: schema = "timing-path-store-v1"; break;
      case Stage::kValidateStaPaths: schema = "sta-path-validation-v1"; break;
      case Stage::kBuildRoutedEndpoints: schema = "xilinx-routed-endpoints-v1"; break;
      case Stage::kExportVivado: schema = "vivado-primitive-verilog-v1"; break;
      case Stage::kExportRwroute: schema = "rwroute-input-tsv-v1"; break;
      case Stage::kPublishAssignmentIndex: schema = "assignment-index-v4"; break;
      case Stage::kValidateExactBinary: schema = "exact-binary-validation-summary-v1"; break;
      case Stage::kPublishAssignmentDescriptor: schema = "assignment-selector-v4"; break;
      case Stage::kValidateAssignmentCuts: schema = "assignment-cut-validation-v1"; break;
      case Stage::kPublishClusterCatalog: schema = "phase3-cluster-catalog-v3"; break;
      case Stage::kReducePartitionLoads: schema = "partition-load-summary-v1"; break;
      case Stage::kAuditPartitionDecision: schema = "partition-decision-summary-v1"; break;
      case Stage::kNormalizePartitionConfiguration: schema = "partition-constraints-v1"; break;
      case Stage::kBuildCaptures: schema = "exact-captures-v1"; break;
      case Stage::kValidateCaptures: schema = "exact-capture-validation-v1"; break;
      case Stage::kAnalyzeExact: schema = "exact-topology-v3"; break;
      case Stage::kFinalizeExact: schema = "exact-nodes-v2"; break;
      case Stage::kHashSemantics: schema = "exact-sha256-text-v1"; break;
      case Stage::kWriteExactSections: schema = "exact-section-summary-v2"; break;
      case Stage::kRoute: schema = "tlr-output-v1"; break;
      case Stage::kSealRoutes: schema = "system-route-seal-summary-v1"; break;
      case Stage::kCompilePhase5:
      case Stage::kRunPhase5: schema = "phase5-report-v1"; break;
      case Stage::kBuildSchedule: schema = "tdm-schedule-store-v4"; break;
      case Stage::kProjectScheduleTiming: schema = "phase5-timing-summary-v1"; break;
      case Stage::kImportGlobalMeasurements: schema = "global-measurements-store-v1"; break;
      case Stage::kAuditGlobalMeasurements: schema = "global-measurement-summary-v1"; break;
      case Stage::kPublishGlobalQor: schema = "qor-report-store-v1"; break;
      case Stage::kValidateGlobalQor: schema = "global-qor-validation-summary-v1"; break;
      case Stage::kBindGlobalEvents: schema = "global-event-primary-v1"; break;
      case Stage::kIndexGlobalEvents: schema = "global-event-index-v1"; break;
      case Stage::kExportGlobalEvents: schema = "global-event-export-summary-v1"; break;
      case Stage::kValidateSchedule: schema = "exact-schedule-validation-summary-v1"; break;
      case Stage::kGroupSignals: schema = "chimew-group-output-text"; break;
      case Stage::kRefinePositions: schema = "chimew-refine-output-text"; break;
      case Stage::kRudy: schema = "chimew-rudy-output-text"; break;
      case Stage::kExportBookshelf: schema = "openparf-bookshelf-nets-v1"; break;
      case Stage::kRefinePatronFromPrimary: schema = "patron-output-v1"; break;
      case Stage::kBuildPartitionClusters: schema = "partition-cluster-columns-v2"; break;
      case Stage::kBuildCandidateIds: schema = "candidate-id-columns-v1"; break;
      default: throw core::StageError(core::ErrorCode::kInvalidRequest, "unknown native stage");
    }
    std::vector<core::ArtifactRef> refs{core::ArtifactRef::Bind(request.output_path, schema, request.output_path == "-")};
    if(request.stage==Stage::kBuildStaWeights)refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"sta-weight-summary-v1"));
    if(request.stage==Stage::kImportStaPaths)refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"sta-path-import-summary-v1"));
    if(request.stage==Stage::kExportOpenstaInputs) {
      if(!request.auxiliary_output_path.empty())refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"opensta-timing-verilog-v1"));
      if(!request.scratch_path.empty())refs.push_back(core::ArtifactRef::Bind(request.scratch_path,"opensta-net-map-v1"));
    }
    if(request.stage==Stage::kStageRoutedOpensta) {
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"routed-opensta-liberty-v1"));
      refs.push_back(core::ArtifactRef::Bind(request.scratch_path,"routed-opensta-summary-v1"));
    }
    if(request.stage==Stage::kBuildRoutedEndpoints) {
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"xilinx-routed-timing-v2"));
      refs.push_back(core::ArtifactRef::Bind(request.scratch_path,"routed-endpoint-summary-v1"));
    }
    if(request.stage==Stage::kBuildRoutedSegments) {
      for(const auto& item:std::vector<std::pair<std::string,std::string>>{{"boundary.sqlite","boundary-timing-store-v1"},{"logic.sqlite","logic-segment-timing-store-v1"},{"local.sqlite","local-path-timing-store-v1"}}) {
        const auto path=(std::filesystem::path(request.auxiliary_output_path)/item.first).string();
        if(std::filesystem::exists(path))refs.push_back(core::ArtifactRef::Bind(path,item.second));
      }
    }
    if(request.stage == Stage::kImportPhysicalBoundary || request.stage == Stage::kImportPhysicalLogic || request.stage == Stage::kImportPhysicalLocal)
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"physical-measurement-import-summary-v1"));
    if (request.stage == Stage::kImportVivadoLineage)
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"vivado-lineage-summary-v2"));
    if (request.stage == Stage::kExportVivado) {
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"vivado-source-functions-v1"));
      refs.push_back(core::ArtifactRef::Bind(request.scratch_path,"vivado-export-summary-v1"));
    }
    if (request.stage == Stage::kExportRwroute)
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path, "rwroute-export-summary-v1"));
    if (request.stage == Stage::kExportBookshelf)
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path, "openparf-bookshelf-summary-v1"));
    if (request.stage == Stage::kRefinePatronFromPrimary) {
      refs.push_back(core::ArtifactRef::Bind(request.output_path+".order","patron-cluster-order-v1"));
      refs.push_back(core::ArtifactRef::Bind(request.output_path+".summary.json","patron-model-summary-v1"));
    }
    return refs;
  });
}
}  // namespace emuflow::native
