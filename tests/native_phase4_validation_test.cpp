// Exercise rejection at the private kernel/result boundary without adding a
// second public routing producer or duplicating its model/parser in tests.
#include "../src/native/emuflow_core/phase4.cpp"
#include <iostream>
#include <sqlite3.h>

namespace {
using namespace emuflow::core;
void Audit(const std::string& damage) {
  Workspace work(std::filesystem::temp_directory_path());
  Model model;
  model.nodes={"a","b"};model.timing_driven=false;
  model.normalization={{"positive_slack_scale_ns","1.0"},{"negative_slack_scale_ns","1.0"},{"max_clock_period_ns","1.0"}};
  model.constraints=Fields(R"({"frame_slots":32,"max_iterations":3,"reroute_rounds":2,"lambda_load":2.0,"lambda_timing":4.0,"lambda_history":1.0,"lambda_tdm":0.1,"tdm_ratio_quantum":8,"tdm_max_ratio":32,"tdm_min_ratio":1,"tree_edge_sum_tdm":false,"hard_sll_capacity":false,"max_route_hops":null})");
  Arc arc;arc.link="ab";arc.from="a";arc.to="b";arc.source=0;arc.sink=1;arc.capacity_key="ab:a->b";arc.capacity=32;arc.lanes=1;arc.latency=1;arc.delay=1;arc.beta=1;
  model.arcs.push_back(arc);
  model.capacities.push_back(Fields(R"({"key":"ab:a->b","link":"ab","direction":"a->b","capacity_bits":32})"));
  Demand demand;demand.id="d000000";demand.net="n";demand.source="a";demand.sinks={"b"};demand.node=0;demand.terminals={1};demand.predicted_delay=1;
  model.demands.push_back(demand);
  const auto request=work.root/"router.in",output=work.root/"router.sqlite";
  WriteRouterInput(model,request,{});
  emuflow::native::RouteTlr({ArtifactRef::Bind(request.string(),"global-route-input-v1")},output.string());
  // Open the completed private test result for a deliberate corruption. No
  // original design input or published stage generation is modified.
  if(!damage.empty()) {
    sqlite3* db=nullptr;
    if(sqlite3_open_v2(output.c_str(),&db,SQLITE_OPEN_READWRITE,nullptr)!=SQLITE_OK)throw std::runtime_error("cannot open kernel fixture");
    try {
      if(damage=="unavailable-candidate") {
        sqlite3_stmt* row=nullptr;
        if(sqlite3_prepare_v2(db,"SELECT records FROM candidate_bundles WHERE demand_index=0",-1,&row,nullptr)!=SQLITE_OK || sqlite3_step(row)!=SQLITE_ROW)throw std::runtime_error("missing candidate fixture");
        const auto* data=static_cast<const char*>(sqlite3_column_blob(row,0));const auto bytes=sqlite3_column_bytes(row,0);
        const std::string original(data,bytes);sqlite3_finalize(row);
        std::string changed;std::size_t offset=0;int count=0;bool removed=false;
        while(offset<original.size()) {
          const auto size=13+4*U32(original,offset+9);
          if(static_cast<unsigned char>(original[offset])==0)removed=true;
          else {changed.append(original,offset,size);++count;}
          offset+=size;
        }
        if(!removed)throw std::runtime_error("fixture lacks baseline candidate");
        sqlite3_stmt* update=nullptr;
        if(sqlite3_prepare_v2(db,"UPDATE candidate_bundles SET records=?,record_count=? WHERE demand_index=0",-1,&update,nullptr)!=SQLITE_OK)throw std::runtime_error("cannot prepare fixture mutation");
        sqlite3_bind_blob(update,1,changed.data(),int(changed.size()),SQLITE_TRANSIENT);sqlite3_bind_int(update,2,count);
        if(sqlite3_step(update)!=SQLITE_DONE)throw std::runtime_error("cannot mutate candidate fixture");sqlite3_finalize(update);
        if(sqlite3_exec(db,"UPDATE selections SET generator=0",nullptr,nullptr,nullptr)!=SQLITE_OK)throw std::runtime_error("cannot mutate selection fixture");
      } else {
        const char* sql=damage=="missing-metric"?"DELETE FROM metrics WHERE name='candidate_workers'":damage=="metric-type"?"UPDATE metrics SET kind=1,integer_value=NULL,real_value=1.0 WHERE name='candidate_workers'":"INSERT INTO metrics VALUES('unknown-metric',0,1,NULL)";
        if(sqlite3_exec(db,sql,nullptr,nullptr,nullptr)!=SQLITE_OK)throw std::runtime_error("cannot mutate metric fixture");
      }
      sqlite3_close(db);
    } catch(...) {sqlite3_close(db);throw;}
  }
  const auto artifact=ArtifactRef::Bind(output.string(),"tlr-output-v1");
  bool rejected=false;
  try {(void)ReadRouter(model,&artifact);}
  catch(const StageError& error) {
    if(damage.empty())throw;
    const auto message=std::string(error.what());
    const auto expected=damage=="unavailable-candidate"?"unavailable candidate":"metric";
    if(message.find(expected)==std::string::npos)throw;
    rejected=true;
  }
  if(rejected!=!damage.empty())throw std::runtime_error("kernel boundary accepted damaged fixture");
}
}
int main() {
  try {
    for(const auto* damage:{"","unavailable-candidate","missing-metric","metric-type","unknown-metric"})Audit(damage);
    return 0;
  }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
