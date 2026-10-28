"""Canonical native TX/capture matrices compose across all FPGA owners."""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from tests.test_native_word_phase_sweep import sweep_kernel, sweep, node as sweep_node

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def network(tmp_path_factory):
    executable=tmp_path_factory.mktemp('native-word-network')/'network'
    compiler=shutil.which('c++')or shutil.which('clang++');assert compiler
    subprocess.run([compiler,'-std=c++17','-O2','-I',str(ROOT/'src/native'),
        str(ROOT/'tests/native_word_network_test.cpp'),
        *(str(ROOT/'src/native/emuflow_core'/name)for name in
          ('native_word_network.cpp','native_word_tx_binding.cpp','native_word_costs.cpp','word_phase_sweep.cpp')),
        '-o',str(executable)],check=True)
    return executable


def transfer(source=0,sink=1,*,phase=0,ratio=8,latency=6,root=None,root_family=0,root_edge=1,
             capture=None,capture_family=0,capture_edge=1,fanins=()):
    return dict(source=source,sink=sink,phase=phase,ratio=ratio,latency=latency,root=root,root_family=root_family,
                root_edge=root_edge,capture=capture,capture_family=capture_family,capture_edge=capture_edge,fanins=list(fanins))


def project(binary,nodes,*,windows=((0,1,0,1,100000),),direct=(),parts=4,flags=0,fail=None):
    lines=[f'5000 100000 4096 {parts} {len(nodes)} {len(windows)} {len(direct)} {flags}']
    for n in nodes:
        values=[n[k]for k in ('source','sink','phase','ratio','latency')]
        values += [int(n['root']is not None),n['root']or 0,n['root_family'],n['root_edge'],
                   int(n['capture']is not None),n['capture']or 0,n['capture_family'],n['capture_edge'],len(n['fanins'])]
        lines.append(' '.join(map(str,values)))
        lines+=[' '.join(map(str,f))for f in n['fanins']]
    lines+=[' '.join(map(str,w))for w in windows]
    lines+=[' '.join(map(str,d))for d in direct]
    done=subprocess.run([str(binary)],input='\n'.join(lines)+'\n',text=True,capture_output=True)
    if fail:
        assert done.returncode!=0 and fail in done.stderr
        return
    assert done.returncode==0,done.stderr
    return json.loads(done.stdout)


def test_one_crossing_uses_actual_native_clock_aliases_and_full_capture_matrix(network):
    result=project(network,[transfer(root=1000,capture=2000)])
    assert result['maximum_cost_ps']==result['required_period_ps']==38000
    assert result['transfers']==1 and result['capture_rows']==68


def test_three_crossings_across_four_partitions_preserve_actual_schedule_order(network):
    nodes=[transfer(0,1,ratio=24,root=1000),
           transfer(1,2,ratio=24,phase=1,fanins=[(0,2000)]),
           transfer(2,3,ratio=24,phase=2,fanins=[(1,3000)],capture=2000)]
    result=project(network,nodes)
    assert result['maximum_cost_ps']==118000
    assert result['required_period_ps']==118000
    assert len(result['windows'][0]['words'])==3


def test_half_cycle_local_path_can_require_more_period_than_longer_cross_path(network):
    result=project(network,[transfer(root=1000,capture=2000)],
        windows=[(0,1,0,1,100000),(0,2,0,1,50000)],direct=[(3,0,2,0,1,30000)])
    assert result['maximum_cost_ps']==38000
    assert result['required_period_ps']==60000
    assert [w['required_period_ps']for w in result['windows']]==[38000,60000]


def test_multiple_clock_roots_keep_different_local_and_transport_requirements(network):
    nodes=[transfer(root=1000,capture=2000),transfer(2,3,root=2000,root_family=1,capture=10000,capture_family=1)]
    result=project(network,nodes,windows=[(0,1,0,1,100000),(1,1,1,1,100000)])
    assert result['maximum_cost_ps']==47000
    assert {(w['source'],w['capture'])for w in result['windows']}=={('core','core'),('csb','csb')}


def test_negative_local_sampling_credit_is_preserved(network):
    result=project(network,[],direct=[(0,0,1,0,1,-1941)])
    assert result['maximum_cost_ps']==-1941 and result['required_period_ps']==0


@pytest.mark.parametrize('flags,message',[(1,'incomplete TX calendar'),(2,'omits an active RX'),
    (4,'disposition repeats'),(8,'partition ownership'),(16,'another clock family')])
def test_incomplete_or_ambiguous_implementation_inputs_are_not_full_graphs(network,flags,message):
    project(network,[transfer(root=1000,capture=2000)],flags=flags,fail=message)


def test_a_timed_clock_pair_needs_its_source_owned_edge_window(network):
    project(network,[transfer(root=1000,capture=2000)],windows=[],fail='original edge-window budget')


def test_native_partition_composition_matches_direct_kernel_on_random_routed_dags(network,sweep_kernel):
    rng=random.Random(29028)
    for _ in range(16):
        nodes=[transfer(0,1,phase=rng.randrange(3),ratio=24,root=rng.randrange(-2000,20000)),
               transfer(1,2,phase=rng.randrange(4),ratio=32,root=rng.randrange(20000),fanins=[(0,rng.randrange(-2000,15000))]),
               transfer(2,3,phase=rng.randrange(3),ratio=24,fanins=[(1,rng.randrange(-2000,15000))],capture=rng.randrange(-2000,10000))]
        result=project(network,nodes)
        model=[sweep_node(phase=n['phase'],ratio=n['ratio'],root=n['root'],capture=n['capture'],fanins=n['fanins'])for n in nodes]
        expected=sweep(sweep_kernel,model)
        assert result['maximum_cost_ps']==expected['maximum_capture_cost_ps']


def test_independent_root_clock_projections_distribute_over_reconvergent_maximum(network,sweep_kernel):
    nodes=[transfer(0,1,ratio=24,root=1371),
           transfer(2,1,ratio=32,phase=2,root=7321,root_family=1),
           transfer(1,3,ratio=24,phase=1,fanins=[(0,-800),(1,2734)],capture=3245)]
    observed=project(network,nodes,windows=[(0,1,0,1,100000),(1,1,0,1,100000)])
    combined=[sweep_node(phase=n['phase'],ratio=n['ratio'],root=n['root'],capture=n['capture'],fanins=n['fanins'])for n in nodes]
    expected=sweep(sweep_kernel,combined)
    assert observed['maximum_cost_ps']==expected['maximum_capture_cost_ps']
    assert len(observed['windows'])==2


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_independent_opensta_accepts_all_joined_tx_events_and_the_normalized_capture(network,tmp_path):
    from emuflow.global_sta import EventCheck,run_event_checks
    nodes=[transfer(0,1,ratio=24,root=1371),
           transfer(1,2,ratio=32,phase=2,fanins=[(0,2734)]),
           transfer(2,3,ratio=24,phase=1,fanins=[(1,5001)],capture=3245,capture_edge=2)]
    result=project(network,nodes,windows=[(0,1,0,2,50000)])
    window=result['windows'][0]
    # Approach the exact left-limit maximum closely enough to distinguish a
    # one-nanosecond period reduction without treating the supremum as attained.
    offset=window['upper']-.1
    event=lambda word:(offset+word*5000)/1000.
    checks=[]
    for i,n in enumerate(nodes):
        chosen=window['words'][i]
        load=event(chosen);visible=event(chosen+n['latency'])
        inputs=[(0.,n['root']/1000.)]if n['root']is not None else[]
        inputs += [(event(window['words'][source]+nodes[source]['latency']),cost/1000.)for source,cost in n['fanins']]
        for j,(launch,cost)in enumerate(inputs):
            checks.append(EventCheck(f'transfer-{i}','tx',str(j),launch,(1.,),load-(cost-1.)))
        for role in ('target','runtime'):
            checks.append(EventCheck(f'transfer-{i}',role,'physical-link',load,(30.,),visible))
        if n['capture']is not None:
            deadline=result['required_period_ps']/2000.
            checks.append(EventCheck('global-capture','target','half-cycle',visible,(1.,),deadline-(n['capture']/1000.-1.)))
            checks.append(EventCheck('global-capture','runtime','reduced-period',visible,(1.,),deadline-.5-(n['capture']/1000.-1.)))
            checks.append(EventCheck('global-capture','tx','observation',0.,(0.,),0.))
    observed=run_event_checks(checks,tmp_path,os.environ['EMUFLOW_TEST_OPENSTA'])
    slacks={(r['path'],r['role'],r['event']):r['slack_ns']for r in observed}
    assert all(v>=-1e-5 for key,v in slacks.items()if key[1]!='runtime'or key[0]!='global-capture')
    assert abs(slacks['global-capture','target','half-cycle'])<.001
    assert slacks['global-capture','runtime','reduced-period']<-.49
