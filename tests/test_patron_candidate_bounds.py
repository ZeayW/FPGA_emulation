"""Exact lower bounds may omit a losing objective, never an improving move."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.test_patron_net_counts import HARNESS as ENDPOINT_HARNESS
from tests.test_patron_ratio_cache import ROOT

HARNESS = ENDPOINT_HARNESS.replace('augment(m,perf);', r'''
  augment(m,false);
  if(perf) {
    // Every timing path shares one locally assigned net. Moving its driver
    // introduces a crossing, so any one path proves a worse primary objective.
    for(auto& cluster:m.cluster) cluster.part=0;
    for(auto& path:m.path) {
      path.nets={1,1}; path.start_cluster=2;path.end_cluster=3;
    }
  }
''').replace('for(int cluster=0;cluster<(perf?1:m.clusters);++cluster)',
             'for(int cluster=(perf?2:0);cluster<(perf?3:m.clusters);++cluster)')
HARNESS = HARNESS.replace('''        auto delta=evaluate_proxy_move(m,state,cn,np,cluster,target);''',r'''
#ifdef CHECK_IMPROVEMENT_BOUND
        auto bounded=evaluate_proxy_move(m,state,cn,np,cluster,target,&state.evaluation);
        if(perf) {
          if(target!=state.assignment[cluster]) require(bounded.bounded_out,"benchmark should reject");
          continue;
        }
#endif
        auto delta=evaluate_proxy_move(m,state,cn,np,cluster,target);
#ifdef CHECK_IMPROVEMENT_BOUND
        if(bounded.bounded_out) {
          require(delta.feasible && !less_ranked(delta.evaluation.ranked,state.evaluation.ranked),
                  "bound discarded improving candidate");
          require(bounded.evaluation.ranked.empty(),"partial result must not claim full QoR");
          require(!std::lexicographical_compare(delta.evaluation.ranked.begin(),
              delta.evaluation.ranked.begin()+3,bounded.objective_lower_bound.begin(),
              bounded.objective_lower_bound.end()),"invalid objective lower bound");
        } else {
          require(bounded.feasible==delta.feasible,"bound changed feasibility");
          if(delta.feasible) require(bounded.evaluation.ranked==delta.evaluation.ranked
              && bounded.evaluation.objective==delta.evaluation.objective,"surviving objective changed");
        }
#endif
''')
HARNESS = HARNESS.replace('''        if(!delta.feasible) continue;''',r'''
        if(!delta.feasible) continue;
#ifdef CHECK_IMPROVEMENT_BOUND
        if(bounded.bounded_out) continue;
        delta=std::move(bounded);
#endif
''')
HARNESS = HARNESS.replace('''  if(!perf) {for(int part:state.assignment)''',r'''
#ifdef CHECK_IMPROVEMENT_BOUND
  require(progress.bound_rejections>0,"fixture did not exercise early rejection");
#endif
  if(!perf) {for(int part:state.assignment)''')

def build(root, source=ROOT/'src/native/patron_refiner.cpp', name='check', bounded=True):
    wrapper=root/(name+'.cpp')
    wrapper.write_text(HARNESS.replace('PATRON_SOURCE',str(source)))
    binary=root/name
    subprocess.run([shutil.which('c++'),'-std=c++17','-O2',
        *(['-DCHECK_IMPROVEMENT_BOUND'] if bounded else []),
        '-I'+str(ROOT/'src/native'),str(wrapper),'-o',str(binary)],check=True)
    return binary

SCALABLE_HARNESS = ENDPOINT_HARNESS.split('int main(int argc,char** argv) {')[0] + r'''
int main(int argc,char** argv) {
  auto m=fixture(std::stoi(argv[1]),std::string(argv[2])=="chain");
  augment(m,false);m.max_moves=m.clusters*4;m.max_sweeps=4;
  run_scalable(m,argv[3],true);
}
'''

class CandidateBoundsTest(unittest.TestCase):
    def test_scalable_move_trace_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=(ROOT/'src/native/patron_refiner.cpp').read_text()
            full=root/'unbounded_source.cpp'
            full.write_text(source.replace('cluster, target, &state.evaluation);',
                                           'cluster, target);'))
            binaries=[]
            for label,path in [('bounded',ROOT/'src/native/patron_refiner.cpp'),('full',full)]:
                wrapper=root/(label+'.cpp')
                wrapper.write_text(SCALABLE_HARNESS.replace('PATRON_SOURCE',str(path)))
                binary=root/label
                subprocess.run([shutil.which('c++'),'-std=c++17','-O2',
                    '-I'+str(ROOT/'src/native'),str(wrapper),'-o',str(binary)],check=True)
                binaries.append(binary)
            for n in ('8','24'):
                for mode in ('plain','chain'):
                    outputs=[]
                    for binary in binaries:
                        out=root/(binary.name+'.out')
                        subprocess.run([str(binary),n,mode,str(out)],capture_output=True,
                                       check=True,timeout=30)
                        outputs.append(out.read_bytes())
                    self.assertEqual(*outputs)

    def test_bound_rejects_only_losing_moves_and_survivors_are_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            bounded=build(root,name='bounded')
            full=build(root,name='full',bounded=False)
            for args in (['8'],['24'],['8','chain'],['24','chain']):
                a=subprocess.run([str(full),*args],capture_output=True,check=True,timeout=30)
                b=subprocess.run([str(bounded),*args],capture_output=True,check=True,timeout=30)
                self.assertEqual(a.stdout,b.stdout)

if __name__=='__main__':
    unittest.main()
