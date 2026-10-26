#include "emuflow_core/native_pipeline.hpp"
#include "emuflow_core/route_seal.hpp"
#include "emuflow_core/physical_cells.hpp"
#include "emuflow_core/atomic_ownership.hpp"
#include "emuflow_core/artifact_io.hpp"
#include "emuflow_core/native_stages.hpp"
#include "emuflow_core/schedule_store.hpp"
#include "emuflow_core/schedule_ingress.hpp"
#include "emuflow_core/bookshelf.hpp"
#include "emuflow_core/rwroute_export.hpp"
#include "emuflow_core/vivado_lineage.hpp"
#include "emuflow_core/patron_ingress.hpp"
#include "emuflow_core/partition_clusters.hpp"
#include "emuflow_core/partition_loads.hpp"
#include "emuflow_core/assignment_publish.hpp"
#include "emuflow_core/assignment_cuts.hpp"
#include "emuflow_core/cluster_catalog.hpp"
#include "emuflow_core/exact_captures.hpp"
#include "emuflow_core/candidate_ids.hpp"
#include "emuflow_core/sqlite.hpp"
#include "emuflow_core/source_launch_clocks.hpp"

#include <filesystem>
#include <iostream>

namespace emuflow::native {
const char* StageName(Stage stage) {
  switch (stage) {
    case Stage::kInvalid: return "native.request";
    case Stage::kBuildSourceLaunchClocks: return "phase7.build-launch-clocks";
    case Stage::kRefinePartitionHops: return "phase3.refine-hops";
    case Stage::kValidateAtomicOwnership: return "phase7.validate-atomic-ownership";
    case Stage::kValidatePhysicalCells: return "phase7.validate-physical-cells";
    case Stage::kSealRoutes: return "phase4.seal-routes";
    case Stage::kImportVivadoLineage: return "phase7.import-vivado-lineage";
    case Stage::kExportRwroute: return "phase7.export-rwroute";
    case Stage::kBuildCaptures: return "phase3.build-captures";
    case Stage::kValidateCaptures: return "phase3.validate-captures";
    case Stage::kPublishAssignmentIndex: return "phase3.publish-assignment";
    case Stage::kValidateAssignmentCuts: return "phase3.validate-cuts";
    case Stage::kPublishClusterCatalog: return "phase3.publish-cluster-catalog";
    case Stage::kReducePartitionLoads: return "phase3.reduce-loads";
    case Stage::kAnalyzeExact: return "phase3.analyze";
    case Stage::kFinalizeExact: return "phase3.finalize-semantics";
    case Stage::kHashSemantics: return "phase3.hash-semantics";
    case Stage::kWriteExactSections: return "phase3.write-exact-sections";
    case Stage::kRefinePatron: return "phase3.refine-patron";
    case Stage::kRoute: return "phase4.route";
    case Stage::kBuildSchedule: return "phase5.build-schedule";
    case Stage::kAssignBanks: return "phase6.assign-banks";
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
    case Stage::kBuildSourceLaunchClocks: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count<3)throw core::StageError(core::ErrorCode::kInvalidRequest,"source launch inputs are missing");
      const auto control=request.input_artifacts.empty()?core::ArtifactRef::Bind(request.input_paths[1],"source-launch-build-request-v1"):
          request.input_artifacts[1];
      if(control.schema()!="source-launch-build-request-v1")
        throw core::StageError(core::ErrorCode::kInvalidArtifact,"source launch control schema differs");
      control.RequireUnchanged();return core::SourceLaunchInputSchemas(control.path());
    }
    case Stage::kRefinePartitionHops: return {"hop-refiner-input-v1"};
    case Stage::kValidateAtomicOwnership: return {"xilinx-mapped-index-v2","openparf-atomic-source-store-v1","openparf-placement-certificate-v3","atomic-ownership-request-v1"};
    case Stage::kValidatePhysicalCells: return {"xilinx-mapped-index-v2","xilinx-physical-handoff-v1","physical-cell-check-request-v1"};
    case Stage::kSealRoutes: return {"system-route-store-v3","system-route-seal-request-v1"};
    case Stage::kImportVivadoLineage: {
      auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count<4 || count>67) throw core::StageError(core::ErrorCode::kInvalidRequest,"Vivado inventory input count is invalid");
      std::vector<std::string> schemas{"xilinx-mapped-index-v2","vivado-source-functions-v1","vivado-lineage-import-request-v1"};
      schemas.insert(schemas.end(),count-3,"vivado-lineage-inventory-v2");return schemas;
    }
    case Stage::kExportRwroute: return {"xilinx-mapped-index-v2","xilinx-physical-handoff-v1","rwroute-export-request-v2"};
    case Stage::kBuildCaptures: return {"emuir-compact-v2","exact-indexed-request-v4","exact-topology-v3","exact-parts-v1"};
    case Stage::kValidateCaptures: return {"emuir-compact-v2","exact-indexed-request-v4","exact-topology-v3","exact-parts-v1","exact-captures-v1"};
    case Stage::kValidateAssignmentCuts: return {"assignment-index-v4"};
    case Stage::kPublishAssignmentIndex: {
      const auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count==3) return {"emuir-compact-v2","partition-cluster-columns-v2","assignment-publication-request-v1"};
      if(count==4) return {"emuir-compact-v2","partition-cluster-columns-v2","assignment-publication-request-v2","exact-nodes-v2"};
      throw core::StageError(core::ErrorCode::kInvalidRequest,"assignment publication input coverage disagrees");
    }
    case Stage::kPublishClusterCatalog: {
      auto count=request.input_artifacts.empty()?request.input_paths.size():request.input_artifacts.size();
      if(count!=3 && count!=4) throw core::StageError(core::ErrorCode::kInvalidRequest,"cluster catalog source count disagrees");
      std::vector<std::string> schemas{"emuir-compact-v2","partition-cluster-columns-v2","cluster-catalog-request-v1"};
      if(count==4) schemas.push_back("candidate-id-columns-v1");
      return schemas;
    }
    case Stage::kReducePartitionLoads: return {"emuir-compact-v2","partition-cluster-columns-v2","partition-load-request-v1"};
    case Stage::kAnalyzeExact:
      return request.indexed_input
          ? std::vector<std::string>{"emuir-compact-v2", "exact-indexed-request-v4"}
          : std::vector<std::string>{"exact-analysis-input-v3"};
    case Stage::kFinalizeExact: return {"exact-topology-v3", "exact-identities-v1"};
    case Stage::kHashSemantics:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-hash-request-v1"};
    case Stage::kWriteExactSections:
      return {"exact-nodes-v2", "exact-captures-v1", "exact-section-request-v1"};
    case Stage::kRefinePatron:
      if (!request.typed_input)
        throw core::StageError(core::ErrorCode::kInvalidRequest, "PATRON requires typed input");
      return {"patron-input-v1"};
    case Stage::kRoute: return {"global-route-input-v1"};
    case Stage::kBuildSchedule: return {"system-route-store-v3", "exact-schedule-board-request-v2", "exact-schedule-generation-request-v1"};
    case Stage::kAssignBanks:
    case Stage::kGroupSignals:
    case Stage::kRefinePositions:
      if (!request.typed_input)
        throw core::StageError(core::ErrorCode::kInvalidRequest, "Chimew requires typed input");
      if (request.stage == Stage::kAssignBanks) return {"chimew-bank-input-v1"};
      if (request.stage == Stage::kGroupSignals) return {"chimew-group-input-v1"};
      return {"chimew-refine-input-v1"};
    case Stage::kRudy: return {"chimew-rudy-input-v1"};
    case Stage::kExportBookshelf: return {"xilinx-mapped-index-v2", "openparf-atom-index-private-v1"};
    case Stage::kBuildCandidateIds: return {"emuir-compact-v2","candidate-policy-request-v1"};
    case Stage::kBuildPartitionClusters: return {"emuir-compact-v2","partition-cluster-request-v1"};
    case Stage::kRefinePatronFromPrimary: {
      std::string timing_schema="timing-path-store-v1";
      if(request.input_artifacts.size()>1) timing_schema=request.input_artifacts[1].schema();
      else if(request.input_paths.size()>1) {
        core::SqliteDatabase db(request.input_paths[1],core::SqliteDatabase::Mode::kReadOnly);
        auto version=db.Prepare("PRAGMA user_version");
        if(version.Step() && version.Integer(0)==2) timing_schema="timing-path-store-v2";
      }
      if(timing_schema!="timing-path-store-v1" && timing_schema!="timing-path-store-v2")
        throw core::StageError(core::ErrorCode::kInvalidArtifact,"unsupported PATRON TimingPathDB schema");
      return {"emuir-compact-v2",timing_schema,"partition-cluster-columns-v2","patron-initial-columns-v1","patron-board-request-v2"};
    }
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
    if (request.stage == Stage::kExportBookshelf || request.stage == Stage::kExportRwroute || request.stage == Stage::kImportVivadoLineage) {
      if (!bound) {
        try { for (auto& path : paths) path = core::SqliteFilePath(path); }
        catch (const std::exception& error) {
          throw core::StageError(core::ErrorCode::kInvalidRequest,error.what());
        }
      }
      std::vector<std::string> destinations{
          request.output_path,request.auxiliary_output_path};
      if (request.stage == Stage::kExportBookshelf) destinations.push_back(request.scratch_path);
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
      if (std::filesystem::exists(request.scratch_path) || std::filesystem::is_symlink(request.scratch_path))
        throw core::StageError(core::ErrorCode::kInvalidRequest,"Bookshelf scratch relation already exists");
    } else if (!request.auxiliary_output_path.empty() || !request.scratch_path.empty()) {
      throw core::StageError(core::ErrorCode::kInvalidRequest,"unexpected auxiliary stage destinations");
    }
    if (request.stage == Stage::kRefinePatron && request.typed_output && !request.summary_output)
      throw core::StageError(core::ErrorCode::kInvalidRequest, "typed PATRON output requires summary mode");
    const bool stream_stage = request.stage == Stage::kAssignBanks || request.stage == Stage::kGroupSignals ||
        request.stage == Stage::kRefinePositions || request.stage == Stage::kRudy;
    const bool pipe_input = stream_stage || (request.stage == Stage::kRefinePatron && request.typed_input);
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
    if (request.stage == Stage::kAssignBanks || request.stage == Stage::kGroupSignals ||
        request.stage == Stage::kRefinePositions || request.stage == Stage::kRudy) {
      const auto produce = [&](std::ostream& stream) {
        switch (request.stage) {
          case Stage::kAssignBanks: AssignChimewBankChannels(path(0),stream); break;
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
      case Stage::kBuildSourceLaunchClocks: core::BuildSourceLaunchClocksFromRequest(inputs,output,request.cancelled); break;
      case Stage::kRefinePartitionHops:
        RefinePartitionHops(inputs,output,!request.summary_output); break;
      case Stage::kValidateAtomicOwnership:
        core::ValidateAtomicOwnership(inputs,output); break;
      case Stage::kValidatePhysicalCells:
        core::ValidatePhysicalCells(inputs[0],inputs[1],inputs[2],output); break;
      case Stage::kImportVivadoLineage:
        core::ImportVivadoLineage(inputs,output,request.auxiliary_output_path); break;
      case Stage::kExportRwroute:
        core::ExportRwroute(inputs[0],inputs[1],inputs[2],output,request.auxiliary_output_path); break;
      case Stage::kPublishAssignmentIndex: core::PublishAssignmentIndex(inputs,output); break;
      case Stage::kValidateAssignmentCuts: core::ValidateAssignmentCuts(inputs[0],output); break;
      case Stage::kPublishClusterCatalog: core::PublishClusterCatalog(inputs,output); break;
      case Stage::kReducePartitionLoads: core::ReducePartitionLoads(inputs,output); break;
      case Stage::kBuildCaptures: core::BuildExactCaptures(inputs,output); break;
      case Stage::kValidateCaptures: core::ValidateExactCaptures(inputs,output); break;
      case Stage::kAnalyzeExact:
        AnalyzeStaticExact(inputs, output);
        break;
      case Stage::kFinalizeExact: FinalizeStaticExact(inputs, output); break;
      case Stage::kHashSemantics: HashStaticExactSemantics(inputs, output); break;
      case Stage::kWriteExactSections: WriteStaticExactSections(inputs, output); break;
      case Stage::kRefinePatron: RefinePatron(inputs, output, request.summary_output, request.typed_output); break;
      case Stage::kRoute: RouteTlr(inputs, output); break;
      case Stage::kSealRoutes: core::SealSystemRoutes(inputs,output); break;
      case Stage::kBuildSchedule: core::BuildExactScheduleStore(inputs,output); break;
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
      case Stage::kBuildSourceLaunchClocks: schema="source-launch-clock-store-v1"; break;
      case Stage::kRefinePartitionHops: schema = "hop-refiner-output-v1"; break;
      case Stage::kValidateAtomicOwnership: schema = "atomic-ownership-summary-v1"; break;
      case Stage::kValidatePhysicalCells: schema = "physical-cell-check-summary-v1"; break;
      case Stage::kImportVivadoLineage: schema = "vivado-lineage-store-v2"; break;
      case Stage::kExportRwroute: schema = "rwroute-input-tsv-v1"; break;
      case Stage::kPublishAssignmentIndex: schema = "assignment-index-v4"; break;
      case Stage::kValidateAssignmentCuts: schema = "assignment-cut-validation-v1"; break;
      case Stage::kPublishClusterCatalog: schema = "phase3-cluster-catalog-v3"; break;
      case Stage::kReducePartitionLoads: schema = "partition-load-summary-v1"; break;
      case Stage::kBuildCaptures: schema = "exact-captures-v1"; break;
      case Stage::kValidateCaptures: schema = "exact-capture-validation-v1"; break;
      case Stage::kAnalyzeExact: schema = "exact-topology-v3"; break;
      case Stage::kFinalizeExact: schema = "exact-nodes-v2"; break;
      case Stage::kHashSemantics: schema = "exact-sha256-text-v1"; break;
      case Stage::kWriteExactSections: schema = "exact-section-summary-v2"; break;
      case Stage::kRefinePatron: schema = request.typed_output ? "patron-output-v1" : "patron-output-text"; break;
      case Stage::kRoute: schema = "tlr-output-v1"; break;
      case Stage::kSealRoutes: schema = "system-route-seal-summary-v1"; break;
      case Stage::kBuildSchedule: schema = "tdm-schedule-store-v4"; break;
      case Stage::kAssignBanks: schema = "chimew-bank-output-text"; break;
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
    if (request.stage == Stage::kImportVivadoLineage)
      refs.push_back(core::ArtifactRef::Bind(request.auxiliary_output_path,"vivado-lineage-summary-v2"));
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
