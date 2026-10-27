// Small native model facts and publication-order gates. No Python model producer.
#include "partition/patron_ingress.hpp"
#include "common/hash.hpp"
#include "common/json_record.hpp"
#include <iostream>
#include <iomanip>
#include <type_traits>
#include <numeric>
#include <algorithm>

namespace {
using emuflow::core::patron::Model;
std::array<unsigned char,32> Signature(const Model& m) {
  emuflow::core::Sha256 hash;
  auto put=[&](const auto& value) {
    static_assert(std::is_trivially_copyable_v<std::decay_t<decltype(value)>>);
    hash.update(reinterpret_cast<const unsigned char*>(&value),sizeof value);
  };
#define FIELD(name) put(m.name)
  FIELD(parts); FIELD(clusters); FIELD(dimensions); FIELD(domains); FIELD(nets); FIELD(paths);
  FIELD(max_hops); FIELD(max_ratio); FIELD(ratio_quantum); FIELD(min_ratio); FIELD(min_used_parts);
  FIELD(max_moves); FIELD(max_sweeps); FIELD(ejection_critical_limit); FIELD(ejection_donor_limit); FIELD(max_ejections);
  put(true); FIELD(flow_max_clusters); FIELD(flow_corridor_distance); FIELD(flow_piercing_strategy);
  FIELD(flow_max_legal_candidates); FIELD(flow_max_polish_moves); FIELD(flow_max_frontier_paths); FIELD(flow_max_tail_moves);
  put(14); FIELD(positive_scale); FIELD(negative_scale); FIELD(max_period);
  FIELD(boundary_fanout_penalty_scale_ns); put(0.0);
#undef FIELD
  for (const auto& row:m.hard_capacity) for (auto value:row) put(value);
  for (const auto& row:m.balance_capacity) for (auto value:row) put(value);
  for (const auto& c:m.cluster) { put(c.part); put(c.fixed); }
  for (auto value:m.cluster_weights) put(value);
  for (const auto& d:m.domain) {
    put(d.lanes); put(d.capacity_bits); put(d.is_sll); put(d.ratio_delay_slope_ns); put(d.opposite); put(d.banks); put(d.wires);
  }
  for (const auto& row:m.route) for (const auto& r:row) {
    put(r.reachable); put(r.arcs.size());
    for (const auto& arc:r.arcs) { put(arc.domain); put(arc.delay_ns); }
  }
  for (const auto& net:m.net) {
    put(net.drivers.size()); for (int c:net.drivers) put(c);
    put(net.sinks.size()); for (int c:net.sinks) put(c);
  }
  for (const auto& path:m.path) {
    put(path.period_ns); put(path.slack_ns); put(path.start_cluster); put(path.end_cluster);
    put(path.nets.size()); for (int net:path.nets) put(net);
  }
  return hash.finish();
}
std::string Facts(const Model& m) {
  using emuflow::core::JsonRecord;
  using I=std::int64_t;using A=std::vector<JsonRecord>;using O=std::map<std::string,JsonRecord>;
  A nets,paths,domains,routes,hard,balance,weights;
  for(const auto& row:m.hard_capacity){A values;for(auto value:row)values.emplace_back(I(value));hard.emplace_back(values);}
  for(const auto& row:m.balance_capacity){A values;for(auto value:row)values.emplace_back(value);balance.emplace_back(values);}
  for(auto value:m.cluster_weights)weights.emplace_back(I(value));
  for(const auto& net:m.net) {
    A drivers,sinks;for(auto v:net.drivers)drivers.emplace_back(I(v));for(auto v:net.sinks)sinks.emplace_back(I(v));
    nets.emplace_back(O{{"drivers",drivers},{"sinks",sinks}});
  }
  for(const auto& path:m.path) {
    A members;for(auto v:path.nets)members.emplace_back(I(v));
    paths.emplace_back(O{{"period_ns",path.period_ns},{"slack_ns",path.slack_ns},
      {"start_cluster",I(path.start_cluster)},{"end_cluster",I(path.end_cluster)},{"nets",members}});
  }
  for(const auto& d:m.domain)domains.emplace_back(O{{"lanes",I(d.lanes)},
    {"capacity_bits",I(d.capacity_bits)},{"opposite",I(d.opposite)},
    {"banks",I(d.banks)},{"wires",I(d.wires)},{"ratio_delay_slope_ns",d.ratio_delay_slope_ns}});
  for(const auto& row:m.route) {
    A destinations;for(const auto& route:row) {
      A arcs;for(const auto& arc:route.arcs)arcs.emplace_back(O{{"domain",I(arc.domain)},{"delay_ns",arc.delay_ns}});
      destinations.emplace_back(O{{"reachable",route.reachable},{"arcs",arcs}});
    }routes.emplace_back(destinations);
  }
  return JsonRecord(O{{"clusters",I(m.clusters)},{"dimensions",I(m.dimensions)},
    {"nets",nets},{"paths",paths},{"domains",domains},{"routes",routes},
    {"hard_capacity",hard},{"balance_capacity",balance},{"cluster_weights",weights}}).Encode();
}
}
int main(int argc,char** argv) {
  try {
    if (argc==2 && std::string(argv[1])=="--order") {
      auto order=emuflow::core::CanonicalPatronClusterOrder(1000013);
      if (order[99999]!=99999 || order[100000]!=100000 || order[100001]!=1000000 ||
          order[100010]!=1000009 || order[100011]!=100001 || order.back()!=999999)
        throw std::runtime_error("canonical PATRON million-cluster lexical boundary changed");
      return 0;
    }
    const bool emit=argc>1 && std::string(argv[1])=="--signature";
    const bool facts=argc>1 && std::string(argv[1])=="--facts";
    const bool publish=argc>1 && std::string(argv[1])=="--publish-order-from-primary";
    if (!emit && !facts && !publish) return 2;
    const int first=2, count=argc-first-(publish?1:0);
    if (count!=5) return 2;
    using emuflow::core::ArtifactRef;
    std::vector<ArtifactRef> refs{
      ArtifactRef::Bind(argv[first],"emuir-compact-v2"), ArtifactRef::Bind(argv[first+1],"timing-path-store-v1"),
      ArtifactRef::Bind(argv[first+2],"partition-cluster-columns-v2")};
    refs.push_back(ArtifactRef::Bind(argv[first+3],"patron-initial-columns-v1"));
    refs.push_back(ArtifactRef::Bind(argv[first+count-1],"patron-board-request-v2"));
    emuflow::core::PatronBuildContext context;
    auto actual=emuflow::core::BuildPatronModel(refs,context);
    const auto signature=Signature(actual);
    if(facts){std::cout<<Facts(actual)<<'\n';return 0;}
    if(publish){
      actual.max_moves=0;actual.output_to_model.resize(actual.clusters);
      std::iota(actual.output_to_model.begin(),actual.output_to_model.end(),0U);
      std::reverse(actual.output_to_model.begin(),actual.output_to_model.end());
      emuflow::native::RefinePatronModel(actual,argv[argc-1],true,true,refs);return 0;
    }
    if (emit) {
      std::cout<<"MODEL_SIGNATURE ";
      for (auto byte:signature) std::cout<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(byte);
      std::cout<<std::dec<<' '<<actual.clusters<<' '<<actual.nets<<' '<<actual.paths<<' '<<actual.dimensions<<' '<<actual.domains<<'\n';
    }
    return 0;
  } catch (const std::exception& error) { std::cerr<<error.what()<<'\n'; return 1; }
}
