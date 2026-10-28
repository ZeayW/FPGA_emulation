#include "flow/finalize.hpp"

#include "common/artifact_io.hpp"
#include "common/bounded_json.hpp"
#include "common/hash.hpp"

#include <array>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace emuflow::core {
namespace {
namespace fs = std::filesystem;
using Raw = std::map<std::string,std::string>;

void Check(bool value,const std::string& why) {
  if(!value) throw StageError(ErrorCode::kInvalidArtifact,"flow finalization: "+why);
}
std::string Hex(Sha256& hash) {
  static constexpr char digits[]="0123456789abcdef";std::string result;
  for(const auto byte:hash.finish()){result+=digits[byte>>4];result+=digits[byte&15];}return result;
}
std::string FileHash(const ArtifactRef& source) {
  std::ifstream stream(source.path(),std::ios::binary);Check(bool(stream),"cannot read "+source.path());
  Sha256 hash;std::array<char,1024*1024> block{};
  while(stream){stream.read(block.data(),block.size());hash.update(
      reinterpret_cast<const unsigned char*>(block.data()),static_cast<std::size_t>(stream.gcount()));}
  Check(stream.eof(),"cannot hash "+source.path());source.RequireUnchanged();return Hex(hash);
}
ArtifactRef Text(const fs::path& path,const std::string& schema="exact-sha256-text-v1") {
  Check(path.is_absolute(),"source path is relative");return ArtifactRef::Bind(path.string(),schema);
}
BoundedJson Document(const ArtifactRef& source,std::size_t maximum=8*1024*1024) {
  return BoundedJson(BoundedJson::Read(source,maximum),maximum);
}
std::string Project(BoundedJson& document,const std::vector<std::string>& names) {
  const auto fields=document.Fields();Raw result;
  for(const auto& name:names){const auto field=fields.find(name);if(field!=fields.end())result.emplace(*field);}
  return BoundedJson::Encode(result);
}
void Pass(BoundedJson& document,const std::string& owner) {
  Check(document.Text("status")=="pass",owner+" did not pass");
}
std::string Nested(const std::string& token,const std::string& key) {
  const auto fields=BoundedJson(token).Fields();const auto value=fields.find(key);
  return value==fields.end()?"null":value->second;
}
std::string TextValue(const std::string& token,const std::string& key) {
  return BoundedJson(token).Text(key);
}
std::string ResolveStore(const ArtifactRef& selector,const fs::path& root,const std::string& schema) {
  auto document=Document(selector);const auto artifact=document.Text("artifact");const fs::path name(artifact);
  Check(!artifact.empty() && name==name.filename() && artifact!="." && artifact!="..",
        "typed selector store name is unsafe");
  const auto store=ArtifactRef::Bind((root/name).string(),schema);store.RequireUnchanged();return artifact;
}
void AddArtifact(Raw& artifacts,std::vector<ArtifactRef>& bindings,const fs::path& output,
                 const std::string& name,const fs::path& relative,
                 const std::string& schema="exact-sha256-text-v1",bool required=true) {
  const auto path=output/relative;
  if(!fs::exists(path)){Check(!required,"required artifact is missing: "+relative.string());return;}
  auto source=ArtifactRef::Bind(path.string(),schema);const auto digest=FileHash(source);bindings.push_back(source);
  artifacts.emplace(name,BoundedJson::Encode({{"path",JsonQuote(relative.generic_string())},
                                              {"sha256",JsonQuote(digest)}}));
}
std::string OptionalSelectorPayload(const fs::path& selector) {
  if(!fs::is_regular_file(selector))return {};
  auto source=Text(selector);auto document=Document(source);if(!document.Has("artifact"))return {};
  const fs::path artifact(document.Text("artifact"));Check(artifact==artifact.filename(),"selector payload escapes owner");
  return artifact.string();
}
}  // namespace

void FinalizeFlow(const fs::path& output) {
  Check(output.is_absolute() && fs::is_directory(output),"output root is unavailable");
  std::vector<ArtifactRef> bindings;
  const auto load=[&](const fs::path& relative,const std::string& schema="exact-sha256-text-v1") {
    auto source=Text(output/relative,schema);bindings.push_back(source);return Document(source);
  };
  const auto frontend_summary=fs::is_regular_file(output/"frontend/orchestration-summary.json")
      ? fs::path("frontend/orchestration-summary.json")
      : fs::is_regular_file(output/"frontend/phase1/frontend-summary.json")
          ? fs::path("frontend/phase1/frontend-summary.json")
          : fs::path("frontend/phase1/phase1_report.json");
  auto frontend=load(frontend_summary,"phase1-summary-v1");Pass(frontend,"frontend");
  auto partition=load("partition/phase3_report.json","phase3-report-v1");Pass(partition,"partition");
  auto route=load("system-route/phase4_report.json","phase4-report-v1");Pass(route,"system route");
  auto tdm=load("tdm/phase5_report.json","phase5-report-v1");Pass(tdm,"TDM");
  auto split=load("split/phase6_report.json","phase6-report-v1");Pass(split,"split");
  auto runtime=load("runtime/phase7c_report.json");
  const auto design=frontend.Text("design"),platform=frontend.Text("platform");
  for(auto* stage:{&partition,&route,&tdm,&split}){
    Check(stage->Text("design")==design,"cross-stage design identity disagrees");
    Check(stage->Text("platform")==platform,"cross-stage platform identity disagrees");
  }
  Check(runtime.Text("design")==design && runtime.Text("platform")==platform,"runtime identity disagrees");
  const auto runtime_validation=runtime.Token("validation");
  Check(TextValue(runtime_validation,"status")=="pass","runtime validation did not pass");

  Raw stages{
    {"frontend",Project(frontend,{"schema","phase","status","design","platform","synthesis"})},
    {"partition",Project(partition,{"schema","phase","status","design","platform","provider","qualification","cut_mode","static_exact_candidate_policy","semantic_implementation_id","finalization_implementation_id","stage_implementation_id","configuration_implementation_id","configuration_pattern_policy","assignment_descriptor_writer_implementation_id","validation"})},
    {"system_route",Project(route,{"schema","phase","status","design","platform","provider","qualification","cut_mode","candidate_generation","validation"})},
    {"tdm",Project(tdm,{"schema","phase","status","design","platform","provider","qualification","cut_mode","validation","timing_validation","cross_layer_timing_validation"})},
    {"split",Project(split,{"schema","phase","status","design","platform","provider","validation","equivalence","board_binding"})}
  };
  for(auto* stage:{&partition,&route,&tdm,&split})
    Check(TextValue(stage->Token("validation"),"status")=="pass","stage validation did not pass");
  Check(TextValue(split.Token("equivalence"),"status")=="pass","split equivalence did not pass");

  Raw artifacts;
  AddArtifact(artifacts,bindings,output,"platform","frontend/phase1/platform.normalized.json");
  AddArtifact(artifacts,bindings,output,"emuir","frontend/phase1/design.emuir.sqlite","emuir-compact-v2");
  AddArtifact(artifacts,bindings,output,"partition_constraints","partition/constraints.normalized.json");
  AddArtifact(artifacts,bindings,output,"route_constraints","system-route/route_constraints.normalized.json");
  AddArtifact(artifacts,bindings,output,"assignment","partition/assignment.json");
  AddArtifact(artifacts,bindings,output,"routes","system-route/routes.json");
  const auto route_selector=Text(output/"system-route/routes.json");
  const auto route_store=ResolveStore(route_selector,output/"system-route","system-route-store-v3");
  AddArtifact(artifacts,bindings,output,"route_store",fs::path("system-route")/route_store,"system-route-store-v3");
  AddArtifact(artifacts,bindings,output,"schedule","tdm/schedule.json");
  const auto schedule_selector=Text(output/"tdm/schedule.json");
  const auto schedule_store=ResolveStore(schedule_selector,output/"tdm","tdm-schedule-store-v4");
  AddArtifact(artifacts,bindings,output,"schedule_store",fs::path("tdm")/schedule_store,"tdm-schedule-store-v4");
  AddArtifact(artifacts,bindings,output,"split_manifest","split/manifest.json");
  AddArtifact(artifacts,bindings,output,"transport_cost","split/transport-cost.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"runtime_contract","runtime/runtime_contract.json");
  AddArtifact(artifacts,bindings,output,"qor_report","runtime/qor_report.json");
  AddArtifact(artifacts,bindings,output,"physical_flow_report","physical/multi-fpga-physical-flow-report.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"physical_summary","physical/physical-summary.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"phase6_comparison_report","phase6-comparison/comparison-report.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"board_link_timing","timing/board-link-timing.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"board_link_route_constraints","timing/board-link-route-constraints.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"frame_search_report","frame-search/frame-search-report.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"timing_path_database","timing/path-database.json","exact-sha256-text-v1",false);
  const auto path_payload=OptionalSelectorPayload(output/"timing/path-database.json");
  if(!path_payload.empty())AddArtifact(artifacts,bindings,output,"timing_path_database_payload",fs::path("timing")/path_payload);
  AddArtifact(artifacts,bindings,output,"cut_segment_qualification","timing/cut-segment-qualification.json","exact-sha256-text-v1",false);
  AddArtifact(artifacts,bindings,output,"partition_net_weights","timing/partition-net-weights.json","exact-sha256-text-v1",false);
  const auto weight_payload=OptionalSelectorPayload(output/"timing/partition-net-weights.json");
  if(!weight_payload.empty())AddArtifact(artifacts,bindings,output,"partition_net_weights_payload",fs::path("timing")/weight_payload);
  AddArtifact(artifacts,bindings,output,"cut_timing_paths","timing/cut-timing-paths.json","exact-sha256-text-v1",false);

  Raw report{{"schema",JsonQuote("emuflow.multi-fpga-flow/v2")},{"status",JsonQuote("pass")},
             {"provider",JsonQuote("profiled-yosys+partition+system-route+tdm+split-transport+runtime")},
             {"architecture_policy",JsonQuote("provider-neutral")},{"runtime",runtime.text()},
             {"stages",BoundedJson::Encode(stages)},{"artifacts",BoundedJson::Encode(artifacts)}};
  std::optional<bool> timing_optimization;
  if(fs::is_regular_file(output/"timing/phase2_report.json")){
    auto timing=load("timing/phase2_report.json","phase2-report-v1");Pass(timing,"timing");
    const auto summary_path=output/"timing/orchestration-summary.json";
    auto timing_fields=fs::is_regular_file(summary_path)
        ? load("timing/orchestration-summary.json").Fields()
        : BoundedJson(Project(timing,{"status","mode","backend","sta","optimization_enabled","partition_weights","partition_weights_applied","partition_provider_weights_applied","hop_refinement_weights_applied","partition_weight_consumers","cut_path_projection"})).Fields();
    // The native Phase 2 owner records the policy bit. The former Python
    // orchestration view additionally repeated its negative application fact.
    if(timing_fields.at("optimization_enabled")=="false" && !timing_fields.count("partition_weights_applied"))
      timing_fields["partition_weights_applied"]="false";
    timing_optimization=timing_fields.at("optimization_enabled")=="true";
    Check(timing_fields.at("optimization_enabled")=="true" ||
          timing_fields.at("optimization_enabled")=="false","timing optimization policy is invalid");
    if(*timing_optimization)Check(timing_fields.count("partition_weights") &&
        !BoundedJson(timing_fields.at("partition_weights")).Fields().empty(),"timing weights are missing");
    else Check(!timing_fields.count("partition_weights") &&
        timing_fields.at("partition_weights_applied")=="false" &&
        route.Text("provider")=="timing-aware-global-candidate-v1" &&
        Nested(route.Token("validation"),"timing_optimization_enabled")=="false" &&
        tdm.Text("provider")=="deterministic-dependency-earliest-slot-v1" &&
        tdm.Has("timing_validation") && TextValue(tdm.Token("timing_validation"),"status")=="pass",
        "timing-evaluated baseline contract disagrees");
    report["timing"]=BoundedJson::Encode(timing_fields);
  }
  bool physical=false;
  if(fs::is_regular_file(output/"physical/multi-fpga-physical-flow-report.json")){
    auto value=load("physical/multi-fpga-physical-flow-report.json");Pass(value,"physical flow");
    Check(value.Text("design")==design && value.Text("platform")==platform,"physical identity disagrees");
    Check(!BoundedJson::Array(value.Token("expected_fpgas")).empty() &&
          TextValue(value.Token("summary"),"status")=="pass","physical summary is invalid");
    Check(Nested(value.Token("summary"),"original_cells")==Nested(partition.Token("validation"),"instances"),
          "physical original-cell coverage disagrees");
    Check(runtime.Has("physical") && TextValue(runtime.Token("physical"),"status")=="pass",
          "runtime is not closed against physical results");
    report["physical"]=Project(value,{"schema","status","provider","backend","design","platform","execution","expected_fpgas","physical_summary_ref","summary"});physical=true;
  }
  if(fs::is_regular_file(output/"timing/board-link-timing.json")){
    const auto summary=output/"timing/board-link-timing-report.json";
    Check(fs::is_regular_file(summary),"BoardLink timing summary is missing");
    auto link=load("timing/board-link-timing-report.json");Pass(link,"BoardLink timing");
    Check(TextValue(link.Token("validation"),"status")=="pass" &&
          TextValue(link.Token("routing_projection"),"status")=="pass",
          "BoardLink validation did not pass");
    if(timing_optimization)Check(link.Boolean("optimization_enabled")==*timing_optimization,
        "BoardLink/timing optimization policy disagrees");
    report["board_link_timing"]=link.text();
  }
  if(fs::is_regular_file(output/"frame-search/frame-search-report.json"))report["frame_search"]=load("frame-search/frame-search-report.json").text();
  const bool comparison=fs::is_regular_file(output/"phase6-comparison/comparison-report.json");
  if(comparison){
    auto value=load("phase6-comparison/comparison-report.json");Pass(value,"Phase 6 comparison");
    Check(physical && value.Has("validation") && TextValue(value.Token("validation"),"status")=="pass",
          "Phase 6 comparison validation did not pass");
    report["phase6_comparison"]=value.text();
  }

  const auto partition_validation=partition.Token("validation"),split_validation=split.Token("validation"),equivalence=split.Token("equivalence");
  std::string timing_mode=JsonQuote("external-path-test");
  if(report.count("timing")){BoundedJson timing(report.at("timing"));if(timing.Has("optimization_enabled"))
    timing_mode=JsonQuote(timing.Boolean("optimization_enabled")?"enabled":"disabled-baseline");}
  Raw summary{{"status",JsonQuote("pass")},{"design",JsonQuote(design)},{"platform",JsonQuote(platform)},
              {"instances",Nested(partition_validation,"instances")},{"used_fpgas",Nested(partition_validation,"used_fpgas")},
              {"cut_nets",Nested(partition_validation,"cut_nets")},{"scheduled_hops",Nested(split_validation,"scheduled_hops")},
              {"equivalence_mismatches",Nested(equivalence,"mismatches")},{"frame_slots",Nested(runtime_validation,"frame_slots")},
              {"timing_optimization",timing_mode},{"nominal_virtual_frequency_mhz",Nested(runtime_validation,"nominal_virtual_frequency_mhz")},
              {"physical_status",JsonQuote(physical?"pass":"not-requested")},{"phase6_provider",JsonQuote(comparison?"chimew":"baseline")},
              {"phase6_comparison_status",JsonQuote(comparison?"pass":"not-requested")}};
  report["summary"]=BoundedJson::Encode(summary);
  const auto text=BoundedJson::Encode(report)+"\n";
  for(const auto& source:bindings)source.RequireUnchanged();
  PublishTextArtifact((output/"multi-fpga-flow-report.json").string(),bindings,
                      [&](std::ostream& stream){stream<<text;});
}
}  // namespace emuflow::core
