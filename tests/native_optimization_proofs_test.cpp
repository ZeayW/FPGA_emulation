#include "emuflow_core/optimization_proofs.hpp"
#include "emuflow_core/json_record.hpp"
#include <fstream>
#include <iostream>
#include <sstream>

int main(int argc,char** argv) {
  try {
    using namespace emuflow::core;
    if(argc<2 || argc>3)return 2;
    std::ifstream stream(argv[1]);std::string config((std::istreambuf_iterator<char>(stream)),{});
    if(config.empty() || config.size()>1024*1024)return 2;
    SqliteDatabase db(":memory:",SqliteDatabase::Mode::kCreateExclusive);
    auto text=[&](const std::string& path) {
      auto row=db.Prepare("SELECT json_extract(?1,?2)");row.BindText(1,config);row.BindText(2,path);
      if(!row.Step())throw std::runtime_error("missing fixture field");return row.TypedText(0);
    };
    CheckedOptimizationContext context;context.fpga=text("$.fpga");context.mapped_sha256=text("$.mapped_sha256");
    context.synthesized_checkpoint_sha256=text("$.synthesized_checkpoint_sha256");
    std::vector<ArtifactRef> inputs;
    for(unsigned i=0;i<3;++i) {
      const auto base="$.inputs["+std::to_string(i)+"]";
      inputs.push_back(ArtifactRef::Bind(text(base+".path"),i==0?"vivado-optimization-proofs-v1":i==1?"xilinx-mapped-index-v2":"vivado-lineage-store-v2"));
      auto values=db.Prepare("SELECT value FROM json_each(?1,?2) ORDER BY key");values.BindText(1,config);values.BindText(2,base+".generation");
      std::vector<std::int64_t> v;while(values.Step())v.push_back(values.TypedInteger(0));if(v.size()!=7)return 2;
      context.generations[i]={std::uint64_t(v[0]),std::uint64_t(v[1]),std::uint64_t(v[2]),v[3],v[4],v[5],v[6]};
    }
    NativeOptimizationProofView proofs(inputs[0],inputs[1],inputs[2],context);
    auto queries=db.Prepare("SELECT json_extract(value,'$.source'),json_extract(value,'$.cells') FROM json_each(?,'$.queries') ORDER BY key");queries.BindText(1,config);
    std::ostringstream result;result<<"[";bool first=true;
    while(queries.Step()) {
      std::vector<std::string> cells;
      auto endpoints=db.Prepare("SELECT value FROM json_each(?) ORDER BY key");endpoints.BindText(1,queries.TypedText(1));
      while(endpoints.Step())cells.push_back(endpoints.TypedText(0));
      auto fact=proofs.Require(queries.TypedText(0),cells);proofs.RequireOwner(fact);
      if(!first)result<<",";first=false;
      result<<"["<<fact.source.sequence()<<","<<(fact.reason==EliminationReason::kConstantState?1:0)<<",";
      if(fact.constant_value)result<<*fact.constant_value;else result<<"null";
      result<<","<<JsonQuote(fact.initialization)<<","<<JsonQuote(fact.source_cell)<<"]";
      if(argc==3 && std::string(argv[2])=="mutate") {std::ofstream changed(inputs[0].path(),std::ios::app);changed.put('\0');}
      if(argc==3 && std::string(argv[2])=="foreign") {
        ArtifactScope other(ArtifactRef::Bind(text("$.other_proof"),"vivado-optimization-proofs-v1"));other.RequireOwner(fact.source);
      }
    }
    proofs.RequireUnchanged();result<<"]\n";std::cout<<result.str();return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
