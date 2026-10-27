#include "emuflow_core/source_launch_clocks.hpp"
#include "emuflow_core/json_record.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <type_traits>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;
    if(argc!=5)return 2;
    std::string mode=argv[4];const bool capture=mode.rfind("capture-",0)==0;
    if(capture)mode=mode.substr(8);
    const auto source=ArtifactRef::Bind(argv[1],"emuir-compact-v2");
    std::ifstream input(argv[2]);std::string config((std::istreambuf_iterator<char>(input)),{});
    if(config.size()>1024*1024)throw std::runtime_error("fixture context exceeds bound");
    SqliteStore source_store(source);auto& db=source_store.database();
    const auto field=[&](const std::string& path) {
      auto row=db.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,config);row.BindText(2,path);
      if(!row.Step())throw std::runtime_error("missing fixture field");return row.TypedText(0);
    };
    auto io=db.Prepare("SELECT COALESCE(json_extract(?1,'$.timing_io_sha256'),?2)");
    io.BindText(1,config);io.BindText(2,std::string(64,'f'));if(!io.Step())return 3;const auto io_seal=io.TypedText(0);
    const auto queries=[&](const auto& visit) {
      auto rows=db.Prepare("SELECT json_extract(value,'$.pin'),json_extract(value,'$.kind') FROM json_each(?1,'$.queries') ORDER BY key");
      rows.BindText(1,config);while(rows.Step())visit(rows.TypedText(0),rows.TypedText(1));
    };
    std::ostringstream result;result<<std::setprecision(17)<<"[";bool first=true;
    const auto separator=[&](){if(!first)result<<",";first=false;};
    if(mode=="resolve") {
      SourceEndpointResolver resolver(source,capture?EndpointRole::kSink:EndpointRole::kDriver);
      queries([&](const auto& pin,const auto& kind) {
        const auto point=resolver.Resolve(pin,kind);separator();
        result<<"["<<point.cell<<","<<point.port<<","<<point.bit<<","<<JsonQuote(point.engine_pin)<<"]";
      });resolver.RequireUnchanged();
    } else {
      if(mode=="build") {
        SourceLaunchContext context;
        context.original_ir_sha256=field("$.original_ir_sha256");context.path_database_sha256=std::string(64,'b');
        context.routes_sha256=std::string(64,'c');context.schedule_sha256=std::string(64,'d');
        context.semantic_sha256=std::string(64,'e');context.timing_io_sha256=io_seal;
        context.tool.argv={field("$.engine"),"-no_init","-exit",field("$.script")};
        context.tool.working_directory=std::filesystem::absolute(argv[3]).parent_path().string();context.tool.tail_bytes=65536;
        for(const auto* role:{"liberty","verilog","clocks","timing_io"}) {
          const std::map<std::string,std::string> keys{{"liberty","EMUFLOW_STA_LIBERTY"},{"verilog","EMUFLOW_STA_VERILOG"},
              {"clocks","EMUFLOW_STA_CLOCKS"},{"timing_io","EMUFLOW_STA_TIMING_IO"}};
          context.tool.environment.emplace_back(keys.at(role),field(std::string("$.")+role));
        }
        for(const auto* key:{"EMUFLOW_TEST_DAMAGE","EMUFLOW_TEST_SOURCE"}) {
          auto row=db.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,config);row.BindText(2,std::string("$.environment.")+key);
          if(row.Step() && !row.IsNull(0))context.tool.environment.emplace_back(key,row.TypedText(0));
        }
        auto timing=db.Prepare("SELECT json_extract(value,'$.path'),json_extract(value,'$.sha256') FROM json_each(?1,'$.inputs')");
        timing.BindText(1,config);
        while(timing.Step()) {
          auto ref=ArtifactRef::Bind(timing.TypedText(0),"external-tool-executable-v1");
          context.timing_input_sha256.emplace(ref.path(),timing.TypedText(1));context.timing_inputs.push_back(std::move(ref));
        }
        std::vector<ArtifactRef> logic,local;
        for(auto* list:{&logic,&local}) {
          auto rows=db.Prepare("SELECT value FROM json_each(?1,?2) ORDER BY key");
          rows.BindText(1,config);rows.BindText(2,list==&logic?"$.logic":"$.local");
          while(rows.Step())list->push_back(ArtifactRef::Bind(rows.TypedText(0),list==&logic?"logic-segment-timing-store-v3":"local-path-timing-store-v3"));
        }
        if(capture)BuildSourceCaptureClocks(source,logic,local,context,argv[3]);
        else BuildSourceLaunchClocks(source,logic,local,context,argv[3]);
      }
      const auto read=[&](auto& view) {
      view.RequireSourceIdentity({field("$.original_ir_sha256"),std::string(64,'b'),std::string(64,'c'),std::string(64,'d'),std::string(64,'e'),io_seal});
      if(mode=="clock-references") {
        for(const auto& item:view.ClockReferences()) {
          separator();result<<"["<<JsonQuote(item.first)<<","<<item.second.declared_period_ns<<","<<item.second.engine_period_ns<<"]";
        }
        view.RequireUnchanged();return;
      }
      std::map<std::string,double> targets;
      auto clocks=db.Prepare("SELECT key,value FROM json_each(?1,'$.targets')");clocks.BindText(1,config);
      while(clocks.Step())targets.emplace(clocks.TypedText(0),clocks.Real(1));
      queries([&](const auto& pin,const auto& kind) {
        separator();result<<"[";bool initial=true;
        for(const auto& fact:view.Facts(pin,kind,targets)) {
          view.RequireOwner(fact);if(!initial)result<<",";initial=false;
          if constexpr(std::is_same_v<std::decay_t<decltype(fact)>,SourceCaptureFact>)result<<fact.capture_ns;
          else result<<fact.launch_ns;
        }
        result<<"]";
      });view.RequireUnchanged();
      };
      if(capture) {
        SourceCaptureClockView view(ArtifactRef::Bind(argv[3],"source-capture-clock-store-v1"),source,field("$.original_ir_sha256"));read(view);
      } else {
        SourceLaunchClockView view(ArtifactRef::Bind(argv[3],"source-launch-clock-store-v1"),source,field("$.original_ir_sha256"));read(view);
      }
    }
    result<<"]\n";std::cout<<result.str();return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
