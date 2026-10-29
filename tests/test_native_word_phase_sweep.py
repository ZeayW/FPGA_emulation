"""Continuous phase coverage against exact event enumeration and the native projector."""
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def sweep_kernel(tmp_path_factory):
    executable = tmp_path_factory.mktemp('word-phase-sweep')/'sweep'
    compiler = shutil.which('c++') or shutil.which('clang++')
    assert compiler, 'C++17 compiler required'
    subprocess.run([compiler,'-std=c++17','-O2','-I',str(ROOT/'src/native'),
                    str(ROOT/'tests/native_word_phase_sweep_test.cpp'),
                    str(ROOT/'src/native/emuflow_core/word_phase_sweep.cpp'),'-o',str(executable)],check=True)
    return executable


@pytest.fixture(scope='module')
def event_kernel(tmp_path_factory):
    executable = tmp_path_factory.mktemp('word-phase-events')/'events'
    compiler = shutil.which('c++') or shutil.which('clang++')
    assert compiler, 'C++17 compiler required'
    subprocess.run([compiler,'-std=c++17','-O2','-I',str(ROOT/'src/native'),
                    str(ROOT/'tests/native_word_events_test.cpp'),
                    str(ROOT/'src/native/emuflow_core/word_events.cpp'),'-o',str(executable)],check=True)
    return executable


def node(*,phase=0,ratio=8,latency=6,root=None,fanins=(),capture=None):
    return dict(phase=phase,ratio=ratio,latency=latency,root=root,fanins=list(fanins),capture=capture)


def sweep(kernel,nodes,period=5000,bound=4096,fail=None,summary=False):
    lines = [f'{period} {bound} {len(nodes)}']
    for item in nodes:
        lines.append(f"{item['phase']} {item['ratio']} {item['latency']} "
                     f"{int(item['root'] is not None)} {item['root'] or 0} "
                     f"{int(item['capture'] is not None)} {item['capture'] or 0} {len(item['fanins'])}")
        lines.extend(f'{source} {cost}'for source,cost in item['fanins'])
    done = subprocess.run([str(kernel),'summary'if summary else'details'],
                          input='\n'.join(lines)+'\n',text=True,capture_output=True)
    if fail:
        assert done.returncode!=0 and fail in done.stderr
        return
    assert done.returncode==0,done.stderr
    return json.loads(done.stdout)


def event_enumeration(nodes,period,phase,offset):
    """Enumerate legal occurrences, testing each contributing input separately."""
    pending = set(range(len(nodes)))
    chosen = {}
    while pending:
        progress = False
        for index in list(pending):
            item = nodes[index]
            if any(source not in chosen for source,_ in item['fanins']):
                continue
            incoming = [(source,cost)for source,cost in item['fanins']if chosen[source] is not None]
            if item['root'] is None and not incoming:
                chosen[index] = None
            else:
                for word in range(-64,512):
                    if (word+phase)%(item['ratio']//8)!=item['phase']:
                        continue
                    time = offset+word*period
                    if item['root'] is not None and time<item['root']:
                        continue
                    if all(word>chosen[source]+nodes[source]['latency'] and
                           time>=offset+(chosen[source]+nodes[source]['latency'])*period+cost
                           for source,cost in incoming):
                        chosen[index] = word
                        break
                else:
                    raise AssertionError('oracle occurrence extent was insufficient')
            pending.remove(index);progress = True
        assert progress,'oracle graph contains a cycle'
    values = [offset+(chosen[i]+item['latency'])*period+item['capture']
              for i,item in enumerate(nodes)if chosen[i] is not None and item['capture'] is not None]
    return max(values)if values else None,chosen


def exhaustive_supremum(nodes,period):
    repeat = 1
    for item in nodes:
        words = item['ratio']//8
        repeat = repeat*words//math.gcd(repeat,words)
    best = None
    for phase in range(repeat):
        for quarter in range(1,4*period):
            value,_ = event_enumeration(nodes,period,phase,Fraction(quarter,4))
            if value is not None and(best is None or value>best):
                best = value
    # All root costs and therefore phase-cell boundaries are integral. Every
    # left-limit maximum is reached to within exactly a quarter time quantum.
    return best+Fraction(1,4)if best is not None else None


def exhaustive_arrivals(nodes,period):
    """Independent event enumeration for each RX, including unused captures."""
    repeat = math.lcm(*(item['ratio']//8 for item in nodes))
    best = [None]*len(nodes)
    for phase in range(repeat):
        for quarter in range(1,4*period):
            offset = Fraction(quarter,4)
            _,loads = event_enumeration(nodes,period,phase,offset)
            for i,item in enumerate(nodes):
                if loads[i] is None:
                    continue
                value = offset+(loads[i]+item['latency'])*period
                if best[i] is None or value>best[i]:
                    best[i] = value
    return [v+Fraction(1,4)if v is not None else None for v in best]


def test_one_crossing_includes_readiness_word_wait_fixed_arc_and_capture(sweep_kernel):
    result = sweep(sweep_kernel,[node(root=1000,capture=2000)])
    assert result['maximum_capture_cost_ps']==38000
    assert result['maximum_arrival_ps']==[36000]
    assert result['phase_cells']==2 and result['repeat_words']==1
    assert result['witness']==dict(phase_word=0,lower_offset_ps=0,upper_offset_ps=1000,
                                 capture_transfer=0,load_words=[1])


def test_relative_word_order_changes_two_crossing_delay(sweep_kernel):
    first = node(ratio=24,root=1000)
    early = sweep(sweep_kernel,[first,node(phase=1,ratio=24,fanins=[(0,0)],capture=2000)])
    late = sweep(sweep_kernel,[first,node(phase=0,ratio=24,fanins=[(0,0)],capture=2000)])
    assert early['maximum_capture_cost_ps']==83000
    assert late['maximum_capture_cost_ps']==93000
    assert early['repeat_words']==late['repeat_words']==3


def test_fractional_phase_supremum_is_not_an_integer_phase_sample(sweep_kernel):
    items = [node(root=1371,capture=2123)]
    result = sweep(sweep_kernel,items)
    at_integer,_ = event_enumeration(items,5000,0,Fraction(0))
    assert result['maximum_capture_cost_ps']==38494
    assert at_integer<result['maximum_capture_cost_ps']
    assert result['witness']['upper_offset_ps']==1371


def test_exact_relay_word_boundary_and_next_quantum_have_different_loads(sweep_kernel):
    first = node(root=0)
    exact = sweep(sweep_kernel,[first,node(fanins=[(0,5000)],capture=0)])
    after = sweep(sweep_kernel,[first,node(fanins=[(0,5001)],capture=0)])
    assert after['maximum_capture_cost_ps']-exact['maximum_capture_cost_ps']==5000
    credit = sweep(sweep_kernel,[first,node(fanins=[(0,-9000)],capture=0)])
    assert credit['maximum_capture_cost_ps']==exact['maximum_capture_cost_ps']
    assert credit['witness']['load_words'][1]>credit['witness']['load_words'][0]+6


def test_absent_root_contributions_remain_absent_not_zero_or_eliminated(sweep_kernel):
    result = sweep(sweep_kernel,[node(capture=0),node(fanins=[(0,0)],capture=0)])
    assert result['maximum_capture_cost_ps'] is None and result['witness'] is None
    assert result['maximum_arrival_ps']==[None,None]
    assert result['node_recomputations']==0
    items = [node(capture=0),node(root=0),node(fanins=[(0,100000),(1,0)],capture=0)]
    result = sweep(sweep_kernel,items)
    assert result['witness']['load_words'][0] is None
    assert result['maximum_capture_cost_ps']<100000


def test_signed_clock_credit_can_select_a_negative_nominal_word(sweep_kernel):
    result = sweep(sweep_kernel,[node(root=-6000,capture=0)])
    assert result['maximum_capture_cost_ps']==29000
    assert result['witness']['load_words']==[-1]


def test_random_shuffled_graphs_match_complete_rational_event_enumeration(sweep_kernel):
    generator = random.Random(28391)
    for _ in range(48):
        items = []
        for i in range(generator.randrange(1,8)):
            words = generator.choice([1,2,3])
            parents = generator.sample(range(i),min(i,generator.randrange(3)))
            items.append(node(phase=generator.randrange(words),ratio=8*words,
                              latency=generator.randrange(1,4),
                              root=generator.randrange(-12,18)if generator.randrange(3)else None,
                              fanins=[(source,generator.randrange(-7,14))for source in parents],
                              capture=generator.randrange(-5,8)if generator.randrange(3)else None))
        order = list(range(len(items)));generator.shuffle(order)
        inverse = {original:shuffled for shuffled,original in enumerate(order)}
        shuffled = [dict(items[i],fanins=[(inverse[p],c)for p,c in items[i]['fanins']])for i in order]
        result = sweep(sweep_kernel,shuffled,period=5)
        expected = exhaustive_supremum(shuffled,5)
        assert result['maximum_capture_cost_ps']==expected
        assert result['maximum_arrival_ps']==exhaustive_arrivals(shuffled,5)
        if result['witness'] is not None:
            w = result['witness'];middle = Fraction(w['lower_offset_ps']+w['upper_offset_ps'],2)
            actual,loads = event_enumeration(shuffled,5,w['phase_word'],middle)
            assert w['load_words']==[loads[i]for i in range(len(shuffled))]
            assert actual+w['upper_offset_ps']-middle==expected


def test_arrivals_cover_uncaptured_transfers_and_distinct_worst_phases(sweep_kernel):
    items = [node(root=1),node(root=3),node(fanins=[(0,0)],capture=2),
             node(),node(fanins=[(3,0)])]
    result = sweep(sweep_kernel,items,period=5)
    assert result['maximum_arrival_ps']==exhaustive_arrivals(items,5)
    assert result['maximum_arrival_ps'][:2]==[36,38]
    assert result['maximum_arrival_ps'][3:]==[None,None]
    w = result['witness']
    at_witness = [(load+items[i]['latency'])*5+w['upper_offset_ps']
                  if load is not None else None for i,load in enumerate(w['load_words'])]
    assert at_witness[1]<result['maximum_arrival_ps'][1]


def test_witnesses_match_the_existing_native_continuous_calendar(sweep_kernel,event_kernel):
    from tests.test_native_word_events import project,transfer
    generator = random.Random(28392)
    for _ in range(24):
        items = []
        for i in range(8):
            words = generator.choice([1,3,4])
            parents = generator.sample(range(i),min(i,generator.randrange(3)))
            items.append(node(phase=generator.randrange(words),ratio=8*words,
                              root=generator.randrange(-2000,13000)if not parents or generator.randrange(2)else None,
                              fanins=[(p,generator.randrange(-7000,13000))for p in parents],
                              capture=generator.randrange(-1000,2000)))
        result = sweep(sweep_kernel,items);w = result['witness']
        middle = Fraction(w['lower_offset_ps']+w['upper_offset_ps'],2)
        anchor = result['repeat_words']*1024
        phase = w['phase_word'];extent = 2*anchor+2048
        records = []
        for item in items:
            fanins = [(False,0,0.,item['root']/1000.)]if item['root'] is not None else []
            fanins += [(True,p,0.,cost/1000.)for p,cost in item['fanins']]
            records.append(transfer(fanins,phase=(item['phase']-phase)%(item['ratio']//8),
                                    ratio=item['ratio'],latency=item['latency']))
        observed = project(event_kernel,[0.],records,period=5.,
                           epoch=float(middle/1000-anchor*5),horizon=extent)
        assert [word-anchor for word in observed]==w['load_words']


@pytest.mark.parametrize('items,period,bound,message',[
    ([node(root=0,capture=0)],0,4096,'bounds'),
    ([node(root=0,capture=0)],5000,0,'bounds'),
    ([node(root=0,ratio=9,capture=0)],5000,4096,'eight-bit'),
    ([node(root=0,phase=1,capture=0)],5000,4096,'eight-bit'),
    ([node(root=0,latency=0,capture=0)],5000,4096,'eight-bit'),
    ([node(fanins=[(0,0)])],5000,4096,'self-referential'),
    ([node(fanins=[(1,0)])],5000,4096,'unknown'),
    ([node(fanins=[(1,0)]),node(fanins=[(0,0)])],5000,4096,'cycle'),
    ([node(root=0,ratio=24),node(root=0,ratio=32,capture=0)],5000,11,'execution bound'),
    ([node(root=2**63-1,capture=0)],1,4096,'signed time domain'),
    ([node(root=0,capture=2**63-1)],5000,4096,'signed time domain'),
])
def test_invalid_calendar_or_incomplete_phase_budget_is_not_sampled(sweep_kernel,items,period,bound,message):
    sweep(sweep_kernel,items,period=period,bound=bound,fail=message)


def test_incremental_sweep_does_not_reproject_every_node_at_every_boundary(sweep_kernel):
    items = [node(root=i%4999+1,capture=0)for i in range(30000)]
    result = sweep(sweep_kernel,items,summary=True)
    assert result['phase_cells']==5000
    assert result['maximum_capture_cost_ps']==39999
    assert result['node_recomputations']==90000
    assert result['node_recomputations']<len(items)*result['phase_cells']//1000


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_real_opensta_validates_phase_witness_support_and_capture_costs(sweep_kernel,event_kernel,tmp_path):
    from emuflow.global_sta import EventCheck,run_event_checks
    from tests.test_native_word_events import project,transfer
    items = [node(ratio=24,root=1371),node(phase=2,ratio=32,root=7321),
             node(phase=1,ratio=24,fanins=[(0,-800),(1,2734)],capture=3245),
             node(fanins=[(2,5001),(0,1000)],capture=2123)]
    result = sweep(sweep_kernel,items);w = result['witness']
    middle = Fraction(w['lower_offset_ps']+w['upper_offset_ps'],2)
    phase = w['phase_word'];anchor = result['repeat_words']*1024
    event_time = lambda word: float((middle+word*5000)/1000)
    capture_time = float((result['maximum_capture_cost_ps']-w['upper_offset_ps']+middle)/1000)
    checks,records,support_keys = [],[],[]
    for i,item in enumerate(items):
        name = f'phase-transfer-{i}'
        fanins = [(False,0,2.,item['root']/1000.)]if item['root'] is not None else []
        fanins += [(True,p,2.,cost/1000.)for p,cost in item['fanins']]
        for j,(event,source,data,cost) in enumerate(fanins):
            launch = event_time(w['load_words'][source]+items[source]['latency'])if event else 0.
            key = (name,'tx',str(j));support_keys.append(key)
            checks.append(EventCheck(*key,launch,(data,),event_time(w['load_words'][i])-(cost-data)))
        load = event_time(w['load_words'][i]);visible = event_time(w['load_words'][i]+item['latency'])
        for role in ('target','runtime'):
            checks.append(EventCheck(name,role,'observation',load,(item['latency']*5.,),visible))
        records.append(transfer(fanins,phase=(item['phase']-phase)%(item['ratio']//8),
                                ratio=item['ratio'],latency=item['latency']))
        if item['capture'] is not None:
            name = f'phase-capture-{i}'
            checks.append(EventCheck(name,'tx','native-capture',visible,(2.,),capture_time-(item['capture']/1000.-2.)))
            for role in ('target','runtime'):
                checks.append(EventCheck(name,role,'observation',capture_time,(0.,),capture_time))
    measured = run_event_checks(checks,tmp_path,os.environ['EMUFLOW_TEST_OPENSTA'])
    indexed = {(row['path'],row['role'],row['event']):row['slack_ns']for row in measured}
    assert all(value>=-1e-5 for value in indexed.values())
    assert abs(indexed[f"phase-capture-{w['capture_transfer']}",'tx','native-capture'])<=1e-5
    selected = [word+anchor for word in w['load_words']]
    assert project(event_kernel,[0.],records,epoch=event_time(-anchor),horizon=2*anchor+2048,
                   chosen=selected,slacks=[indexed[key]for key in support_keys])is True
