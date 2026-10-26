#include "emuflow_core/original_timing.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;
    if(argc!=6)return 2;
    const auto selector=ArtifactRef::Bind(argv[1],"sta-path-selector-v1");
    const auto payload=ArtifactRef::Bind(argv[2],"timing-path-store-v2");
    std::ifstream stream(argv[4]);std::string config((std::istreambuf_iterator<char>(stream)),{});
    if(config.empty() || config.size()>1024*1024)throw std::runtime_error("invalid fixture control");
    SqliteDatabase database(payload.path(),SqliteDatabase::Mode::kImmutableReadOnly);
    auto fields=database.Prepare("SELECT key,json_extract(value,'$.declared_period_ns'),"
        "json_extract(value,'$.engine_period_ns') FROM json_each(?,'$.clocks') ORDER BY key");fields.BindText(1,config);
    SourceClockReferences clocks;
    while(fields.Step())clocks.emplace(fields.TypedText(0),SourceClockReference{fields.TypedReal(1),fields.TypedReal(2)});
    auto periods=database.Prepare("SELECT key,value FROM json_each(?,'$.targets') ORDER BY key");periods.BindText(1,config);
    std::map<std::string,double> targets;while(periods.Step())targets.emplace(periods.TypedText(0),periods.TypedReal(1));
    auto numbers=database.Prepare("SELECT json_extract(?1,'$.event_offset_ns'),json_extract(?1,'$.data_delay_ns'),"
        "json_extract(?1,'$.sampling_cost_ns')");numbers.BindText(1,config);if(!numbers.Step())return 2;
    const auto offset=numbers.TypedReal(0),data=numbers.TypedReal(1),sampling=numbers.TypedReal(2);
    const std::string mode=argv[5];OriginalTimingView view(selector,payload,argv[3]);
    std::ostringstream output;output<<std::setprecision(17)<<"[";bool first=true;
    std::uint64_t count=0,full=0,half=0;
    view.Visit([&](const auto& path) {
      view.RequireOwner(path.source);
      const auto window=ScaleOriginalWindow(path.budget,clocks,targets);
      const auto check=BindOriginalCaptureCheck(path,clocks,targets,window.launch_ns+offset,data,sampling);
      if(path.source.sequence()!=count++)throw std::runtime_error("original path identity order changed");
      full+=window.coefficient==1.;half+=window.coefficient==.5;
      if(mode=="mutate" && first){std::ofstream changed(selector.path(),std::ios::app);changed<<' ';}
      if(mode=="windows") {
        if(!first)output<<",";
        output<<"["<<JsonQuote(path.identity)<<","<<window.launch_ns<<","<<window.window_ns<<","<<window.coefficient
              <<","<<check.event_launch_ns<<","<<check.data_delay_ns<<","<<check.required_ns<<","<<path.budget.check_margin_ns<<"]";
      }
      first=false;
    });
    view.RequireUnchanged();
    if(mode=="windows"){output<<"]\n";std::cout<<output.str();}
    else std::cout<<"{\"paths\":"<<count<<",\"full_cycles\":"<<full<<",\"half_cycles\":"<<half<<"}\n";
    return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
