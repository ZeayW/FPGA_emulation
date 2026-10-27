#include "emuflow_core/word_physical_binding.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <iostream>
#include <iomanip>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;
    if(argc!=2 && argc!=3)return 2;
    std::ifstream input(argv[1]);std::string json((std::istreambuf_iterator<char>(input)),{});
    if(json.size()>1024*1024)throw std::runtime_error("fixture input is too large");
    SqliteDatabase parser(":memory:",SqliteDatabase::Mode::kCreateExclusive);
    const auto text=[&](const std::string& path) {
      auto row=parser.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,json);row.BindText(2,path);
      if(!row.Step())throw std::runtime_error("fixture text is absent");return row.TypedText(0);
    };
    const auto source=ArtifactRef::Bind(text("$.source"),"emuir-compact-v2");
    const auto clock=ArtifactRef::Bind(text("$.clocks"),"source-launch-clock-store-v1");
    WordPhysicalBindingContext context;
    context.design="counter";context.platform="board";context.word_period_ns=5.;context.epoch_ns=0.;
    context.frame_slots=1024;context.fixed_latency_words=6;context.implementation_clock_periods_ns={{"clk",100.}};
    auto extra_clock=parser.Prepare("SELECT json_extract(?1,'$.extra_clock_period_ns')");extra_clock.BindText(1,json);
    if(extra_clock.Step() && !extra_clock.IsNull(0))context.implementation_clock_periods_ns["extra"]=extra_clock.TypedReal(0);
    auto& identity=context.source;identity.original_ir_sha256=text("$.identity.original_ir_sha256");
    identity.path_database_sha256=text("$.identity.path_database_sha256");identity.routes_sha256=text("$.identity.routes_sha256");
    identity.schedule_sha256=text("$.identity.schedule_sha256");identity.semantic_sha256=text("$.identity.semantic_sha256");
    identity.timing_io_sha256=text("$.identity.timing_io_sha256");
    SourceLaunchClockView launches(clock,source,identity.original_ir_sha256);
    std::vector<WordPhysicalPartition> parts;
    auto rows=parser.Prepare("SELECT json_extract(value,'$.logic'),json_extract(value,'$.identity'),"
        "json_extract(value,'$.costs') FROM json_each(?1,'$.partitions') ORDER BY key");rows.BindText(1,json);
    while(rows.Step())parts.push_back({ArtifactRef::Bind(rows.TypedText(0),"logic-segment-timing-store-v3"),
        ArtifactRef::Bind(rows.TypedText(1),"boundary-identity-store-v1"),ArtifactRef::Bind(rows.TypedText(2),"boundary-timing-store-v3")});
    std::unique_ptr<NativeOptimizationProofView> proof;
    auto optional=parser.Prepare("SELECT json_type(?,'$.proof_context')");optional.BindText(1,json);
    if(optional.Step() && !optional.IsNull(0)) {
      CheckedOptimizationContext checked;checked.fpga=text("$.proof_context.fpga");checked.mapped_sha256=text("$.proof_context.mapped_sha256");
      checked.synthesized_checkpoint_sha256=text("$.proof_context.synthesized_checkpoint_sha256");
      std::vector<ArtifactRef> artifacts;
      for(unsigned i=0;i<3;++i) {
        const auto base="$.proof_context.inputs["+std::to_string(i)+"]";
        artifacts.push_back(ArtifactRef::Bind(text(base+".path"),i==0?"vivado-optimization-proofs-v1":i==1?"xilinx-mapped-index-v2":"vivado-lineage-store-v2"));
        auto values=parser.Prepare("SELECT value FROM json_each(?1,?2) ORDER BY key");values.BindText(1,json);values.BindText(2,base+".generation");
        std::vector<std::int64_t> v;while(values.Step())v.push_back(values.TypedInteger(0));if(v.size()!=7)return 2;
        checked.generations[i]={std::uint64_t(v[0]),std::uint64_t(v[1]),std::uint64_t(v[2]),v[3],v[4],v[5],v[6]};
      }
      proof=std::make_unique<NativeOptimizationProofView>(artifacts[0],artifacts[1],artifacts[2],checked);
      parts.at(0).optimization=proof.get();
    }
    auto result=BindPhysicalWordModel(ArtifactRef::Bind(text("$.assignment"),"assignment-index-v4"),
        ArtifactRef::Bind(text("$.semantic"),"exact-semantic-binary-v2"),
        ArtifactRef::Bind(text("$.schedule"),"tdm-schedule-store-v4"),launches,context,parts);
    auto capture=parser.Prepare("SELECT json_extract(?1,'$.capture_edge_ns'),json_extract(?1,'$.capture_period_ns')");capture.BindText(1,json);
    if(capture.Step() && !capture.IsNull(0)) {
      auto targets=context.implementation_clock_periods_ns;
      if(!capture.IsNull(1))targets["clk"]=capture.TypedReal(1);
      result=BindWordCaptureClockContext(result,launches,targets,capture.TypedReal(0));
    }
    if(argc==3 && std::string(argv[2])=="mix-capture-clock-owner") {
      const auto other=ArtifactRef::Bind(text("$.other_clocks"),"source-launch-clock-store-v1");
      SourceLaunchClockView alternate(other,source,identity.original_ir_sha256);
      result=BindWordCaptureClockContext(result,alternate,context.implementation_clock_periods_ns,100.);
    }
    auto selected=ProjectPhysicalWordEvents(result.model);
    auto choices=parser.Prepare("SELECT value FROM json_each(?1,'$.selected') ORDER BY key");choices.BindText(1,json);
    std::vector<std::uint32_t> override;
    while(choices.Step())override.push_back(choices.TypedInteger(0));
    if(!override.empty())selected=std::move(override);
    std::vector<WordSupportCheck> checks;
    const bool summary=argc==3 && std::string(argv[2])=="summary";
    std::uint64_t check_count=0;
    VisitPhysicalWordSupportChecks(result,selected,[&](const auto& check){++check_count;if(!summary)checks.push_back(check);});
    if(argc==3) {
      if(std::string(argv[2])=="mutate") {std::ofstream changed(text("$.clocks"),std::ios::app|std::ios::binary);changed.put('\0');}
      else if(std::string(argv[2])=="mutate-proof") {
        std::ofstream changed(text("$.proof_context.inputs[0].path"),std::ios::app|std::ios::binary);changed.put('\0');
      }
      else if(std::string(argv[2])=="mix-clock-owner") {
        const auto other=ArtifactRef::Bind(text("$.other_clocks"),"source-launch-clock-store-v1");
        SourceLaunchClockView alternate(other,source,identity.original_ir_sha256);
        alternate.RequireOwner(result.roots.at(0));
      } else if(std::string(argv[2])=="validate") {
        std::vector<double> slacks;
        auto measured=parser.Prepare("SELECT value FROM json_each(?1,'$.slacks') ORDER BY key");measured.BindText(1,json);
        while(measured.Step())slacks.push_back(measured.Real(0));
        ValidatePhysicalWordEvents(result.model,selected,slacks,1e-5);
      }
    }
    result.RequireUnchanged();
    if(summary) {
      std::cout<<"{\"events\":"<<selected.size()<<",\"roots\":"<<result.roots.size()<<",\"checks\":"<<check_count
          <<",\"first\":"<<selected.front()<<",\"last\":"<<selected.back()<<"}\n";return 0;
    }
    std::cout<<std::setprecision(17)<<"{\"events\":[";
    for(std::size_t i=0;i<selected.size();++i){if(i)std::cout<<",";std::cout<<selected[i];}
    std::cout<<"],\"roots\":[";
    for(std::size_t i=0;i<result.roots.size();++i) {
      if(i)std::cout<<",";const auto& fact=result.roots[i];
      launches.RequireOwner(fact);std::cout<<"["<<fact.source.sequence()<<","<<fact.clock.sequence()<<","<<fact.launch_ns<<"]";
    }
    std::cout<<"],\"costs\":[";
    for(std::size_t i=0;i<result.model.transfers.size();++i) {
      if(i)std::cout<<",";std::cout<<"[";const auto& fanins=result.model.transfers[i].fanins;
      for(std::size_t j=0;j<fanins.size();++j) {
        if(j)std::cout<<",";const auto& f=fanins[j];
        std::cout<<"["<<(f.from_event?1:0)<<","<<f.source<<","<<f.data_delay_ns<<","<<f.sampling_cost_ns<<"]";
      }std::cout<<"]";
    }
    std::cout<<"],\"checks\":[";
    for(std::size_t i=0;i<checks.size();++i) {
      if(i)std::cout<<",";const auto& c=checks[i];
      std::cout<<"["<<c.transfer<<","<<c.fanin<<","<<c.launch_ns<<","<<c.data_delay_ns<<","<<c.required_ns<<"]";
    }
    std::cout<<"],\"eliminated\":[";
    for(std::size_t i=0;i<result.eliminated_fanins.size();++i) {
      if(i)std::cout<<",";std::cout<<"[";bool first=true;
      for(const auto& removed:result.eliminated_fanins[i]) {
        if(!first)std::cout<<",";first=false;
        if(!proof)throw std::runtime_error("missing elimination owner");proof->RequireOwner(removed.disposition);
        std::cout<<"["<<JsonQuote(removed.disposition.source_cell)<<","<<removed.origin.logic->sequence()<<"]";
      }
      std::cout<<"]";
    }
    std::cout<<"],\"context_launches\":[";
    for(std::size_t i=0;i<result.model.root_launch_ns.size();++i){if(i)std::cout<<",";std::cout<<result.model.root_launch_ns[i];}
    std::cout<<"],\"inputs\":"<<result.inputs.size()<<"}\n";return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
