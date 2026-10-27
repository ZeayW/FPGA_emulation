// Current in-process hop kernel contracts, independent of retired text ingress.
#include "emuflow_core/partition_hops.hpp"
#include <iostream>
#include <set>
#include <stdexcept>

namespace {
using emuflow::core::PartitionHopModel;
using emuflow::core::ComputePartitionHops;
void Require(bool valid,const char* message) {
  if(!valid)throw std::runtime_error(message);
}
PartitionHopModel Model(int parts,int clusters) {
  PartitionHopModel model;
  model.num_parts=parts;model.num_clusters=clusters;model.num_dimensions=1;
  model.hop_limit=1;model.min_used_parts=1;
  model.distance.assign(parts,std::vector<int>(parts,1));
  model.balance_bound.assign(parts,std::vector<double>(1,clusters));
  model.hard_capacity.assign(parts,std::vector<long long>(1,clusters));
  for(int part=0;part<parts;++part)model.distance[part][part]=0;
  model.clusters.assign(clusters,emuflow::core::PartitionHopCluster{0,-1,{1}});
  return model;
}
}
int main() {
  try {
    // This old regression protects the retained kernel from falling into the
    // quadratic exact repair search after crossing its large-instance guard.
    auto large=Model(2,50001);
    large.distance={{0,2},{2,0}};
    for(int i=1;i<large.num_clusters;++i)large.clusters[i].part=1;
    large.num_nets=1;large.nets.push_back({1.0,0,{50000}});
    auto guarded=ComputePartitionHops(large);
    Require(!guarded.pass,"infeasible guarded graph was accepted");
    Require(guarded.metrics.at("initial_violations")==1 && guarded.metrics.at("final_violations")==1,
            "guarded graph violation accounting changed");
    Require(guarded.metrics.at("scale_guard")==1,"large graph bypassed repair search guard");
    auto minimum=Model(3,3);minimum.min_used_parts=3;
    auto repaired=ComputePartitionHops(minimum);
    Require(repaired.pass,"minimum used partition repair failed");
    Require(repaired.metrics.at("initial_used_part_deficit")==2 &&
            repaired.metrics.at("final_used_part_deficit")==0,"minimum used accounting changed");
    Require(std::set<int>(repaired.assignment.begin(),repaired.assignment.end())==std::set<int>{0,1,2},
            "minimum used repair omitted a target");
    auto invalid=Model(2,2);invalid.num_nets=1;invalid.nets.push_back({1.0,0,{2}});
    bool rejected=false;
    try {(void)ComputePartitionHops(invalid);}catch(const std::runtime_error& error) {
      rejected=std::string(error.what()).find("invalid NET sink")!=std::string::npos;
    }
    Require(rejected,"kernel accepted an out-of-domain sink identity");
    return 0;
  } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
