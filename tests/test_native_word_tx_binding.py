"""Bind canonical TDM entries and prove recurring-phase cost invariance."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_native_word_costs import HEADER, fields

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def tx_binder(tmp_path_factory):
    executable = tmp_path_factory.mktemp('native-word-tx-binding')/'bind'
    compiler = shutil.which('c++')or shutil.which('clang++')
    assert compiler, 'C++17 compiler required'
    subprocess.run([compiler,'-std=c++17','-O2','-I',str(ROOT/'src/native'),
        str(ROOT/'tests/native_word_tx_binding_test.cpp'),
        str(ROOT/'src/native/emuflow_core/native_word_tx_binding.cpp'),
        str(ROOT/'src/native/emuflow_core/native_word_costs.cpp'),'-o',str(executable)],check=True)
    return executable


def row(phase=0,query='word[0]',*,kind='dut-clock',source='core',edge='rise',cost=2.5,measured=True):
    result=fields(phase=str(phase),kind=kind,source=source,edge=edge,
        measurement='native-clock-aware-reference'if measured else'native-no-timed-path')
    result[1]=query.encode().hex()
    if measured:
        result[8:10]=[str(cost),str(cost-6.)]
        result[12]=(source if kind=='dut-clock'else'emuflow_tdm_clk').encode().hex()
    return result


def context(phase=0,query='word[0]',**kw):
    return [row(phase,query,**kw),row(phase,query,edge='fall',measured=False)]


def bind(binary,rows,*,entries=(('outgoing','word[0]',0,8),),clocks=('core',),rx=None,
         fanins=None,bound=4096,maximum=1000000,calendar=None,fail=None):
    rx=rx or {}
    if fanins is None:
        keys=sorted({(int(r[0]),bytes.fromhex(r[1]).decode())for r in rows})
        fanins=[(phase,query,0,0,0,0)for phase,query in keys]
    quote=lambda value:json.dumps(value,ensure_ascii=False)if isinstance(value,str)else str(value)
    calendar=sorted({entry[3]//8 for entry in entries})if calendar is None else calendar
    lines=[f'{bound} {maximum} {len(entries)} {len(clocks)} {len(rx)} {len(fanins)} {len(calendar)}',
           ' '.join(map(str,calendar))]
    lines+=[' '.join(map(quote,entry))for entry in entries]
    lines+=[quote(clock)for clock in clocks]
    lines+=[' '.join(map(quote,item))for item in rx.items()]
    lines+=[' '.join(map(quote,f))for f in fanins]
    lines+=[HEADER]+['\t'.join(r)for r in rows]
    done=subprocess.run([str(binary)],input='\n'.join(lines)+'\n',text=True,capture_output=True)
    if fail:
        assert done.returncode!=0 and fail in done.stderr
        return
    assert done.returncode==0,done.stderr
    return json.loads(done.stdout)


def test_canonical_rx_join_signed_grid_and_missing_path_survive(tx_binder):
    rows=context()+[row(kind='rx',source='s7',edge='',cost=-3.781)]
    result=bind(tx_binder,rows,rx={'s7':'incoming'},fanins=[(0,'word[0]',1,0,0,0)])
    assert result['expected_contexts']==result['observed_contexts']==1
    assert result['entries'][0]['data_phase_invariant'] is True
    bound=result['entries'][0]['rows']
    assert bound[-1]==dict(phase=0,source='s7',incoming_entry='incoming',cost_ps=-3781)
    assert bound[1]['cost_ps'] is None


def test_transit_partition_binds_rx_without_invented_dut_launches(tx_binder):
    rows=[row(kind='rx',source='s7',edge='',cost=3.781)]
    result=bind(tx_binder,rows,clocks=(),rx={'s7':'incoming'},fanins=[(0,'word[0]',1,0,0,0)])
    assert result['expected_contexts']==result['observed_contexts']==1
    assert result['entries'][0]['data_phase_invariant']is True
    assert result['entries'][0]['rows']==[dict(phase=0,source='s7',incoming_entry='incoming',cost_ps=3781)]
    bind(tx_binder,context(),clocks=(),fail='undeclared DUT clock')


def test_clockless_empty_fanin_is_a_complete_absent_path_context(tx_binder):
    result=bind(tx_binder,[],clocks=(),fanins=[(0,'word[0]',0,0,0,0)])
    assert result['expected_contexts']==result['observed_contexts']==1
    assert result['entries'][0]['rows']==[]
    assert result['entries'][0]['data_phase_invariant']is True
    bind(tx_binder,[],clocks=(),fanins=[(0,'word[0]',1,0,0,0)],fail='no timing dispositions')
    bind(tx_binder,[],fanins=[(0,'word[0]',0,0,0,0)],fail='no timing dispositions')


def test_complete_joint_calendar_and_shared_bit_members(tx_binder):
    entries=[('even','bit[0]',0,16),('odd','bit[0]',1,16),('third','bit[1]',1,24)]
    rows=[]
    for phase in range(6):
        rows+=context(phase,'bit[0]')
        if phase%3==1:rows+=context(phase,'bit[1]')
    result=bind(tx_binder,rows,entries=entries)
    assert result['repeat_words']==6
    assert result['expected_contexts']==result['observed_contexts']==8
    assert [entry['contexts']for entry in result['entries']]==[3,3,2]
    assert all(entry['data_phase_invariant'] is True for entry in result['entries'])


def test_partial_snapshot_is_incomplete_even_when_observed_costs_agree(tx_binder):
    entries=[('outgoing','word[0]',0,24),('other','other',0,32)]
    result=bind(tx_binder,context()+context(3),entries=entries)
    assert result['expected_contexts']==7 and result['observed_contexts']==2
    assert all(entry['data_phase_invariant'] is None for entry in result['entries'])


@pytest.mark.parametrize('change',['cost','presence','fanin'])
def test_changed_recurrent_data_costs_or_fanins_are_not_collapsed(tx_binder,change):
    entries=[('outgoing','word[0]',0,16),('calendar','other',0,24)]
    rows=[];fanins=[]
    for phase in (0,2,4):
        rows+=context(phase,cost=2.5 if change!='cost'or phase==0 else 2.501,
                      measured=change!='presence'or phase==0)
        rows+=[row(phase,kind='rx',source='s8'if change=='fanin'and phase else's7',edge='')]
        fanins.append((phase,'word[0]',1,0,0,0))
    result=bind(tx_binder,rows,entries=entries,rx={'s7':'in1','s8':'in2'},fanins=fanins)
    assert result['entries'][0]['data_phase_invariant'] is False


def test_no_timed_rx_and_unbound_startup_inputs_remain_explicit(tx_binder):
    rows=context()+[row(kind='rx',source='s7',edge='',measured=False)]
    port=fields(phase='0',kind='ports',source='links_ready',edge='',measurement='native-source-context-required')
    port[1]='word[0]'.encode().hex();rows+=[port]
    result=bind(tx_binder,rows,rx={'s7':'incoming'},fanins=[(0,'word[0]',1,0,1,0)])
    assert result['untimed_rx_rows']==1 and result['unbound_source_rows']==1
    assert result['entries'][0]['rows'][-1]['cost_ps'] is None


def test_receive_only_counter_extends_the_tx_observation_calendar(tx_binder):
    result=bind(tx_binder,context(0)+context(2),entries=[('tx','word[0]',0,16)],calendar=[2,3])
    assert result['repeat_words']==6 and result['expected_contexts']==3
    assert result['observed_contexts']==2 and result['entries'][0]['data_phase_invariant']is None
    complete=bind(tx_binder,context(0)+context(2)+context(4),entries=[('tx','word[0]',0,16)],calendar=[2,3])
    assert complete['entries'][0]['data_phase_invariant']is True


@pytest.mark.parametrize('calendar,message',[
    ([], 'calendar is missing'),([1,1],'repeated'),([33],'invalid'),([2],'absent from its counter calendar')])
def test_calendar_coverage_is_owned_by_the_complete_counter_catalog(tx_binder,calendar,message):
    bind(tx_binder,context(),calendar=calendar,fail=message)


@pytest.mark.parametrize('change,message',[
    ('duplicate','repeats a source'),('phase','unknown calendar'),('clock','undeclared DUT clock'),
    ('edge','omits a declared'),('rx','unbound RX source'),('fanin','fanin counts'),
    ('ledger','independent fanin'),('grid','1 ps provider grid'),('witness','another launch clock'),
])
def test_invalid_binding_does_not_become_a_projectable_model(tx_binder,change,message):
    rows=context();fanins=[(0,'word[0]',0,0,0,0)]
    if change=='duplicate':rows.append(rows[0])
    elif change=='phase':rows[0][0]='1'
    elif change=='clock':rows[0][3]='foreign'.encode().hex()
    elif change=='edge':rows.pop()
    elif change=='rx':rows.append(row(kind='rx',source='s7',edge=''))
    elif change=='fanin':fanins=[(0,'word[0]',1,0,0,0)]
    elif change=='ledger':fanins=[]
    elif change=='grid':rows[0]=row(cost=2.5001)
    elif change=='witness':rows[0][12]='foreign'.encode().hex()
    bind(tx_binder,rows,fanins=fanins,fail=message)


@pytest.mark.parametrize('entries,bound,maximum,message',[
    ([('one','word[0]',0,16),('two','word[0]',0,24)],4096,100000,'multiple canonical'),
    ([('one','a',0,24),('two','b',0,32)],8,100000,'recurrence'),
    ([('one','a',0,24),('two','b',0,32)],4096,6,'contexts'),
    ([('one','a',3,24)],4096,100000,'phase or ratio'),
])
def test_execution_bounds_and_calendar_ambiguity_are_explicit(tx_binder,entries,bound,maximum,message):
    bind(tx_binder,[],entries=entries,bound=bound,maximum=maximum,fail=message)
