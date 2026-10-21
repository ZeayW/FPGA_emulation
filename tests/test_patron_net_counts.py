"""Incremental net histograms retain endpoint multiplicity and move semantics."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_patron_ratio_cache import ROOT, HARNESS as PATH_HARNESS

HELPERS = r'''
void augment(Model& m, bool large) {
  // Duplicate drivers only on a legacy path; endpoint-exact intentionally
  // retains its pre-existing single-driver requirement.
  m.net[0].drivers.push_back(m.net[0].drivers.front());
  for(auto& net:m.net) {
    int original_sink=net.sinks.back();
    net.sinks.push_back(net.drivers.front());
    net.sinks.push_back(original_sink);
    net.sinks.push_back(0);net.sinks.push_back(0);
  }
  if(large) {
    int original=m.clusters;m.clusters=500000;m.cluster.resize(m.clusters);
    for(int i=original;i<m.clusters;++i) {m.cluster[i].part=i%m.parts;m.cluster[i].weight={1};}
    m.hard_capacity.assign(m.parts,{double(m.clusters*2)});m.balance_capacity=m.hard_capacity;
  }
  Net shared;shared.drivers={0,0,2};
  for(int i=0;i<m.clusters;++i) shared.sinks.push_back(i);
  shared.sinks.push_back(0);shared.sinks.push_back(2);
  m.net.push_back(shared);++m.nets;
}
void verify_changes(const Model& m, ProxyState& state, const ProxyDelta& delta,
                    const std::vector<std::pair<int,int>>& changes) {
  if(!delta.feasible) return;
  auto assignment=state.assignment;
  for(auto change:changes) state.assignment[change.first]=change.second;
  for(const auto& item:delta.nets) {
    const auto full=build_proxy_net(m,state.assignment,item.first);
    const auto& got=item.second;
#ifdef CHECK_INCREMENTAL_COUNTS
    require(got.driver_counts==full.driver_counts,"incremental driver multiplicity differs");
#endif
    require(got.feasible==full.feasible && got.delay_ns==full.delay_ns
        && got.worst_transition==full.worst_transition && got.sink_counts==full.sink_counts
        && got.domain_counts==full.domain_counts && got.hops==full.hops && got.cuts==full.cuts
        && got.transitions.size()==full.transitions.size(),"incremental net differs");
    for(std::size_t i=0;i<got.transitions.size();++i) {
      const auto& a=got.transitions[i];const auto& b=full.transitions[i];
      require(std::tie(a.source,a.sink,a.sink_clusters)==std::tie(b.source,b.sink,b.sink_clusters),
              "incremental transition differs");
    }
  }
  auto loads=state.domain_load;
  for(auto item:delta.domain_delta) loads[item.first]+=item.second;
  auto ratios=paired_domain_ratios(m,loads);
  for(const auto& item:delta.paths)
    check(item.second,build_proxy_path(m,state,delta.nets,ratios,item.first));
  state.assignment=std::move(assignment);
}
void trace_delta(const char* label,const ProxyDelta& delta) {
  std::cout<<label<<':'<<delta.feasible;
  if(delta.feasible) {
    for(auto rank:delta.evaluation.ranked) std::cout<<':'<<rank;
    for(auto value:delta.evaluation.objective) std::cout<<':'<<std::hexfloat<<value;
  }
  std::cout<<'\n';
}
'''
HARNESS = PATH_HARNESS.replace('int main(int argc,char** argv) {',HELPERS+'\nint main(int argc,char** argv) {')
HARNESS = HARNESS.replace('ProxyState state=build_proxy_state(m);','augment(m,perf); ProxyState state=build_proxy_state(m);')
HARNESS = HARNESS.replace('  const auto start=std::chrono::steady_clock::now();',r'''
  if(!perf) {
    auto e=evaluate_proxy_ejection(m,state,cn,np,0,1,0);
    trace_delta("ejection",e);verify_changes(m,state,e,{{0,1},{1,0}});
    int partner=m.parts;
    auto c=evaluate_proxy_comove(m,state,cn,np,0,partner,1);
    trace_delta("comove",c);verify_changes(m,state,c,{{0,1},{partner,1}});
    int endpoint_sink=2*m.parts-1;
    auto same_counts=evaluate_proxy_ejection(m,state,cn,np,0,endpoint_sink,0);
    trace_delta("same-count-endpoint-swap",same_counts);
    verify_changes(m,state,same_counts,{{0,m.parts-1},{endpoint_sink,0}});
    require(same_counts.feasible,"endpoint-swap candidate unavailable");
    int final_net=2*(m.parts-1)-1;
    require(same_counts.nets.at(final_net).sink_counts==state.net[final_net].sink_counts,
            "fixture must preserve sink partition counts");
    auto batch=evaluate_proxy_changes(m,state,cn,np,0,1,1,0,{{partner,1}},true);
    trace_delta("three-point",batch);verify_changes(m,state,batch,{{0,1},{1,0},{partner,1}});
  }
  const auto start=std::chrono::steady_clock::now();''')
HARNESS = HARNESS.replace('        if(!perf) {\n          auto loads=state.domain_load;',
    '        if(!perf) {\n          verify_changes(m,state,delta,{{cluster,target}});\n          auto loads=state.domain_load;')

def build(root, source=ROOT/'src/native/patron_refiner.cpp', name='check', check_counts=False):
    wrapper=root/(name+'.cpp')
    wrapper.write_text(HARNESS.replace('PATRON_SOURCE',str(source)))
    binary=root/name
    subprocess.run([shutil.which('c++'),'-std=c++17','-O2',*(['-DCHECK_INCREMENTAL_COUNTS'] if check_counts else []),'-I'+str(ROOT/'src/native'),str(wrapper),'-o',str(binary)],check=True)
    return binary

class NetCountsTest(unittest.TestCase):
    def test_multiplicity_overlap_ejection_comove_and_batch_match_full_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            binary=build(Path(temp),check_counts=True)
            for args in (['24'],['24','chain']):
                result=subprocess.run([str(binary),*args],capture_output=True,text=True,check=True,timeout=30)
                self.assertIn('ejection:1',result.stdout)
                self.assertIn('comove:1',result.stdout)
                self.assertIn('three-point:1',result.stdout)
                self.assertIn('same-count-endpoint-swap:1',result.stdout)

if __name__=='__main__':
    unittest.main()
