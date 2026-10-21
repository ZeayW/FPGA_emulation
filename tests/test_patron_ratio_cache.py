"""Ratio-only candidates must equal full structural reconstruction bit for bit."""
import subprocess
import tempfile
import unittest
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
# Also usable against a pinned pre-change source for differential experiments.
HARNESS = r'''
#define main patron_program_main
#include "PATRON_SOURCE"
#undef main
Model fixture(int n, bool chain=false) {
  Model m;
  const int parts=chain?4:2, edges=parts-1;
  m.parts=parts; m.clusters=parts*n; m.dimensions=1; m.domains=2*edges;
  m.nets=edges*n; m.paths=n; m.frame_slots=1000000; m.ratio_quantum=1;
  m.min_used_parts=1; m.max_hops=parts; m.flow_version=14;
  m.positive_scale=10.; m.negative_scale=1.; m.max_period=10.;
  m.boundary_fanout_penalty_scale_ns=.13; m.physical_hop_guard_scale_ns=.17;
  m.hard_capacity.assign(parts, {double(parts*2*n)}); m.balance_capacity=m.hard_capacity;
  m.cluster.resize(m.clusters); m.domain.resize(m.domains);
  m.route.assign(parts,std::vector<Route>(parts));
  for(int a=0;a<parts;++a) for(int b=0;b<parts;++b) {
    auto& route=m.route[a][b]; route.reachable=true;
    for(int x=a;x!=b;x+=(b>a?1:-1)) {
      int domain=b>a?2*x:2*(x-1)+1;
      route.arcs.push_back({domain,.19+.07*domain});
    }
  }
  for(auto& d:m.domain) {d.lanes=1; d.ratio_delay_slope_ns=.31;}
  m.net.resize(m.nets); m.path.resize(n);
  for(int i=0;i<n;++i) {
    for(int j=0;j<parts;++j) {m.cluster[parts*i+j].part=j;m.cluster[parts*i+j].weight={1};}
    for(int j=0;j<edges;++j) {
      auto& net=m.net[edges*i+j];net.drivers={parts*i+j};net.sinks={parts*i+j,parts*i+j+1};
    }
    auto& p=m.path[i]; p.period_ns=10.; p.slack_ns=2.+i%3;
    p.start_cluster=parts*i; p.end_cluster=parts*i+parts-1;
    for(int j=0;j<edges;++j) {p.nets.push_back(edges*i+j);p.nets.push_back(edges*i+j);}
    p.feedback_source=0; p.feedback_sink=parts-1; p.feedback_residual_ns=.23;
    if(i%7==0) {p.start_cluster=-1; p.end_cluster=-1;} // legacy branch fallback
  }
  return m;
}
void check(const ProxyPathState& a,const ProxyPathState& b) {
  require(a.normalized_slack==b.normalized_slack && a.negative==b.negative
      && a.snaking==b.snaking && a.transitions==b.transitions && a.hops==b.hops
      && a.ratio_sum==b.ratio_sum,"cached path differs from full reconstruction");
}
int main(int argc,char** argv) {
  int n=argc>1?std::stoi(argv[1]):24;
  bool perf=argc>2 && std::string(argv[2])=="perf";
  Model m=fixture(n, argc>2 && std::string(argv[2])=="chain"); ProxyState state=build_proxy_state(m);
  auto cn=build_cluster_nets(m); auto np=build_net_paths(m);
  const auto start=std::chrono::steady_clock::now();
  int repeated=perf?100:1;
  for(int repetition=0;repetition<repeated;++repetition) {
    for(int cluster=0;cluster<(perf?1:m.clusters);++cluster) {
      ProxyDelta best; bool found=false;
      for(int target=0;target<m.parts;++target) {
        auto delta=evaluate_proxy_move(m,state,cn,np,cluster,target);
        if(!perf) {
          std::cout<<cluster<<':'<<target<<':'<<delta.feasible;
          if(delta.feasible) {
            for(auto rank:delta.evaluation.ranked) std::cout<<':'<<rank;
            for(auto value:delta.evaluation.objective) std::cout<<':'<<std::hexfloat<<value;
          }
          std::cout<<'\n';
        }
        if(!delta.feasible) continue;
        if(!perf) {
          auto loads=state.domain_load;
          for(auto item:delta.domain_delta) loads[item.first]+=item.second;
          auto ratios=paired_domain_ratios(m,loads);
          int saved=state.assignment[cluster]; state.assignment[cluster]=target;
          for(const auto& item:delta.paths)
            check(item.second,build_proxy_path(m,state,delta.nets,ratios,item.first));
          state.assignment[cluster]=saved;
        }
        if(less_ranked(delta.evaluation.ranked,state.evaluation.ranked)
           && (!found || less_ranked(delta.evaluation.ranked,best.evaluation.ranked))) {
          found=true; best=std::move(delta);
        }
      }
      if(found && !perf) apply_proxy_delta(m,state,best);
    }
  }
  if(!perf) {for(int part:state.assignment) std::cout<<part;std::cout<<'\n';}
  std::cerr<<"BENCH_SECONDS "<<std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count()<<'\n';
}
'''

def build(root, source=ROOT/'src/native/patron_refiner.cpp', name='check'):
    wrapper=root/(name+'.cpp')
    wrapper.write_text(HARNESS.replace('PATRON_SOURCE',str(source)))
    binary=root/name
    subprocess.run([shutil.which('c++'),'-std=c++17','-O2','-I'+str(ROOT/'src/native'),str(wrapper),'-o',str(binary)],check=True)
    return binary

class RatioCacheTest(unittest.TestCase):
    def test_candidates_match_full_rebuild_with_ratio_changes_and_duplicate_nets(self):
        with tempfile.TemporaryDirectory() as temp:
            binary=build(Path(temp))
            result=subprocess.run([str(binary)],capture_output=True,text=True,check=True,timeout=30)
            self.assertEqual(len(result.stdout.splitlines()),97)
            chain=subprocess.run([str(binary),'24','chain'],capture_output=True,text=True,check=True,timeout=30)
            self.assertEqual(len(chain.stdout.splitlines()),385)

if __name__=='__main__':
    unittest.main()
