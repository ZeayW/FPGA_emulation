// Offline differential only: primary-store producer versus the independently
// generated Python typed model. Production never builds both models or hashes
// an entire graph again for this comparison.
#include "emuflow_core/patron_ingress.hpp"
#include "emuflow_core/hash.hpp"
#include "emuflow_core/sqlite.hpp"
#include <iostream>
#include <iomanip>
#include <type_traits>

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
    const int first=emit?2:1, count=argc-first-(emit?0:1);
    if (count!=5) return 2;
    using emuflow::core::ArtifactRef;
    std::string timing_schema="timing-path-store-v1";
    {
      emuflow::core::SqliteDatabase db(argv[first+1],emuflow::core::SqliteDatabase::Mode::kReadOnly);
      auto version=db.Prepare("PRAGMA user_version");
      if(version.Step() && version.Integer(0)==2) timing_schema="timing-path-store-v2";
    }
    std::vector<ArtifactRef> refs{
      ArtifactRef::Bind(argv[first],"emuir-compact-v2"), ArtifactRef::Bind(argv[first+1],timing_schema),
      ArtifactRef::Bind(argv[first+2],"partition-cluster-columns-v2")};
    refs.push_back(ArtifactRef::Bind(argv[first+3],"patron-initial-columns-v1"));
    refs.push_back(ArtifactRef::Bind(argv[first+count-1],"patron-board-request-v2"));
    emuflow::core::PatronBuildContext context;
    auto actual=emuflow::core::BuildPatronModel(refs,context);
    const auto signature=Signature(actual);
    if (emit) {
      std::cout<<"MODEL_SIGNATURE ";
      for (auto byte:signature) std::cout<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(byte);
      std::cout<<std::dec<<' '<<actual.clusters<<' '<<actual.nets<<' '<<actual.paths<<' '<<actual.dimensions<<' '<<actual.domains<<'\n';
    } else {
      auto expected=emuflow::native::ReadTypedPatronModel(argv[argc-1]);
      if (signature!=Signature(expected)) throw std::runtime_error("native/Python model record signatures disagree");
      std::cout<<"NATIVE_MODEL_RECORD_PARITY_PASS\n";
    }
    return 0;
  } catch (const std::exception& error) { std::cerr<<error.what()<<'\n'; return 1; }
}
