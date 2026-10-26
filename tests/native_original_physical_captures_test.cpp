#include "emuflow_core/original_physical_captures.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <iostream>
#include <iomanip>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;if(argc!=2)return 2;
    std::ifstream input(argv[1]);std::string json((std::istreambuf_iterator<char>(input)),{});
    if(json.size()>1024*1024)return 2;
    SqliteDatabase db(":memory:",SqliteDatabase::Mode::kCreateExclusive);
    auto text=[&](const std::string& key){auto q=db.Prepare("SELECT json_extract(?1,?2)");q.BindText(1,json);q.BindText(2,key);if(!q.Step())throw std::runtime_error("missing control");return q.TypedText(0);};
    auto source=ArtifactRef::Bind(text("$.source"),"emuir-compact-v2");
    SourceLaunchClockView launches(ArtifactRef::Bind(text("$.clocks"),"source-launch-clock-store-v1"),source,text("$.identity.original_ir_sha256"));
    WordPhysicalBindingContext context;context.design="counter";context.platform="board";context.word_period_ns=5.;context.epoch_ns=0.;context.frame_slots=1024;context.fixed_latency_words=6;context.implementation_clock_periods_ns={{"clk",100.}};
    auto& id=context.source;id.original_ir_sha256=text("$.identity.original_ir_sha256");id.path_database_sha256=text("$.identity.path_database_sha256");id.routes_sha256=text("$.identity.routes_sha256");id.schedule_sha256=text("$.identity.schedule_sha256");id.semantic_sha256=text("$.identity.semantic_sha256");id.timing_io_sha256=text("$.identity.timing_io_sha256");
    std::vector<WordPhysicalPartition> word_parts;std::vector<OriginalPhysicalPartition> captures;
    auto rows=db.Prepare("SELECT json_extract(value,'$.logic'),json_extract(value,'$.identity'),json_extract(value,'$.costs'),json_extract(value,'$.local') FROM json_each(?,'$.partitions') ORDER BY key");rows.BindText(1,json);
    while(rows.Step()) {
      auto logic=ArtifactRef::Bind(rows.TypedText(0),"logic-segment-timing-store-v3"),boundary=ArtifactRef::Bind(rows.TypedText(1),"boundary-identity-store-v1");
      word_parts.push_back({logic,boundary,ArtifactRef::Bind(rows.TypedText(2),"boundary-timing-store-v3")});
      captures.push_back({logic,ArtifactRef::Bind(rows.TypedText(3),"local-path-timing-store-v3"),boundary,nullptr});
    }
    auto binding=BindPhysicalWordModel(ArtifactRef::Bind(text("$.assignment"),"assignment-index-v4"),ArtifactRef::Bind(text("$.semantic"),"exact-semantic-binary-v2"),ArtifactRef::Bind(text("$.schedule"),"tdm-schedule-store-v4"),launches,context,word_parts);
    const auto selected=ProjectPhysicalWordEvents(binding.model);
    std::unique_ptr<NativeOptimizationProofView> proof;
    auto optional=db.Prepare("SELECT json_type(?,'$.proof_context')");optional.BindText(1,json);
    if(optional.Step() && !optional.IsNull(0)) {
      CheckedOptimizationContext checked;checked.fpga=text("$.proof_context.fpga");checked.mapped_sha256=text("$.proof_context.mapped_sha256");
      checked.synthesized_checkpoint_sha256=text("$.proof_context.synthesized_checkpoint_sha256");
      std::vector<ArtifactRef> artifacts;
      for(unsigned i=0;i<3;++i) {
        const auto base="$.proof_context.inputs["+std::to_string(i)+"]";
        artifacts.push_back(ArtifactRef::Bind(text(base+".path"),i==0?"vivado-optimization-proofs-v1":i==1?"xilinx-mapped-index-v2":"vivado-lineage-store-v2"));
        auto values=db.Prepare("SELECT value FROM json_each(?1,?2) ORDER BY key");values.BindText(1,json);values.BindText(2,base+".generation");
        std::vector<std::int64_t> v;while(values.Step())v.push_back(values.TypedInteger(0));if(v.size()!=7)return 2;
        checked.generations[i]={std::uint64_t(v[0]),std::uint64_t(v[1]),std::uint64_t(v[2]),v[3],v[4],v[5],v[6]};
      }
      proof=std::make_unique<NativeOptimizationProofView>(artifacts[0],artifacts[1],artifacts[2],checked);
      captures.at(0).optimization=proof.get();
    }
    OriginalTimingView original(ArtifactRef::Bind(text("$.original_selector"),"sta-path-selector-v1"),ArtifactRef::Bind(text("$.original_payload"),"timing-path-store-v2"),id.path_database_sha256);
    const auto clocks=launches.ClockReferences();
    std::ostringstream result;result<<std::setprecision(17)<<"[";bool first=true;
    const auto coverage=VisitOriginalPhysicalCaptures(original,source,binding,std::string(64,'a'),captures,[&](const auto& path,const auto& capture){
      if(!first)result<<",";first=false;
      if(capture.eliminated) {
        if(capture.cost)throw std::runtime_error("eliminated capture acquired numerical costs");
        result<<"["<<JsonQuote(path.identity)<<","<<capture.partition<<","<<(capture.crosses?1:0)<<",null,null,null]";return;
      }
      if(!capture.cost)throw std::runtime_error("capture lacks a physical disposition");
      const auto window=ScaleOriginalWindow(path.budget,clocks,context.implementation_clock_periods_ns);
      const auto event=capture.crosses?WordEventTime(binding.model,selected.at(*capture.transfer)+binding.model.transfers.at(*capture.transfer).fixed_latency_words):window.launch_ns;
      const auto check=BindOriginalCaptureCheck(path,clocks,context.implementation_clock_periods_ns,event,capture.cost->data_delay_ns,capture.cost->sampling_cost_ns);
      result<<"["<<JsonQuote(path.identity)<<","<<capture.partition<<","<<(capture.crosses?1:0)<<","<<event<<","<<check.data_delay_ns<<","<<check.required_ns<<"]";
    });
    coverage.RequireUnchanged();result<<"]";
    std::cout<<"{\"records\":"<<result.str()<<",\"original\":"<<coverage.original<<",\"local\":"<<coverage.local<<",\"cross\":"<<coverage.cross<<",\"eliminated\":"<<coverage.optimized_away<<"}\n";return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
