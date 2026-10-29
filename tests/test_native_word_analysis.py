"""Production native entry consumes indexed owners and immutable native exports."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.native_word_analysis import (
    INDEX_SCHEMA, ROW_SCHEMA, ROLES, _write_projection_request,
    analyze_native_word_network, write_native_word_index,
    export_native_word_checks, verify_native_word_checks,
)
from emuflow.store_publication import bind_store_source
from tests.native_build import emuflow_native
from tests.test_native_word_costs import HEADER
from tests.test_native_word_tx_binding import row


@pytest.fixture(scope='module')
def native():
    supplied=os.environ.get('EMUFLOW_TEST_NATIVE_WORD_DRIVER')
    program=Path(supplied)if supplied else emuflow_native()
    assert subprocess.run([str(program),'check-runtime',INDEX_SCHEMA],capture_output=True).returncode==0
    return program


@pytest.fixture(autouse=True)
def runtime(native,monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native.parent.parent))


def fixture(tmp_path,*,ratio=8,calendar=(1,),observed=('fpga0','fpga1'),root=1.,capture=2.):
    index=tmp_path/'index.sqlite'
    parts=[dict(fpga='fpga0',clocks={'native_clk':'core'},calendar_periods=calendar,
                tx_entries=[('transfer','word[0]')],rx_entries=[]),
           dict(fpga='fpga1',clocks={'native_clk':'core'},calendar_periods=calendar,
                tx_entries=[],rx_entries=[('s7','transfer')])]
    write_native_word_index(index,transfers=[('transfer','fpga0','fpga1',0,ratio,6)],partitions=parts,
        clock_windows=[('core',a,'core',b,100000 if a==b else 50000)for a in(1,2)for b in(1,2)],
        source_context={'scope':'routed-provider interface fixture'})
    observations={}
    for fpga in observed:
        directory=tmp_path/fpga;directory.mkdir()
        record={role:directory/(role+'.tsv')for role in ROLES}
        tx=[row(0,source='native_clk',cost=root),row(0,source='native_clk',edge='fall',measured=False)]if fpga=='fpga0'else[]
        record['tx_costs'].write_text(HEADER+'\n'+''.join('\t'.join(r)+'\n'for r in tx))
        fanin='phase_word\tquery_hex\tnative_startpoints\trx_sources\tdut_cells\tinfrastructure_cells\tports\tunclassified\n'
        if tx:fanin+='0\t'+('word[0]'.encode().hex())+'\t1\t0\t1\t0\t0\t0\n'
        record['fanins'].write_text(fanin)
        cap=[]
        for target in('rise','fall'):
            query=f'dut-capture:native_clk:{target}'
            for source_edge in('rise','fall'):
                r=row(-1,query,source='native_clk',edge=source_edge,measured=False);cap.append(r)
            if fpga=='fpga1':
                r=row(-1,query,kind='rx',source='s7',edge='',cost=capture,measured=target=='rise')
                if target=='rise':r[13]='native_clk'.encode().hex()
                cap.append(r)
        record['captures'].write_text(HEADER+'\n'+''.join('\t'.join(r)+'\n'for r in cap))
        record['missing_rx'].write_text('source_origin\n')
        observations[fpga]=record
    return index,observations,tmp_path/'analysis.json'


def test_production_command_projects_actual_stream_roles_and_clock_windows(tmp_path):
    index,observations,out=fixture(tmp_path)
    analyze_native_word_network(index,observations,out)
    result=json.loads(out.read_text())
    assert result['status']=='setup-model-projected'
    assert result['maximum_sampling_cost_ps']==result['required_reference_period_ps']==38000
    assert result['setup_model_frequency_mhz']==pytest.approx(1000./38.)
    assert result['observed_partitions']==result['expected_partitions']==2
    assert result['transfer_order']==['transfer']
    assert result['original_path_sta_acceptance']==result['physical_acceptance']=='pending'


def test_missing_partition_preserves_the_full_requested_population(tmp_path):
    index,observations,out=fixture(tmp_path,observed=('fpga0',))
    analyze_native_word_network(index,observations,out)
    result=json.loads(out.read_text())
    assert result['status']=='awaiting-observations'
    assert result['expected_partitions']==2 and result['observed_partitions']==1
    assert result['maximum_sampling_cost_ps']is result['setup_model_frequency_mhz']is None
    assert result['partitions'][1]['observed']is False


def test_no_observations_requires_no_fake_empty_measurements(tmp_path):
    index,_,out=fixture(tmp_path,observed=())
    analyze_native_word_network(index,{},out)
    result=json.loads(out.read_text())
    assert result['status']=='awaiting-observations' and result['observed_tx_contexts']==0
    assert result['expected_tx_contexts']==1 and result['incomplete_entries']==1


def test_receive_only_periods_keep_incomplete_calendar_records_pending(tmp_path):
    index,observations,out=fixture(tmp_path,ratio=16,calendar=(2,3))
    analyze_native_word_network(index,observations,out)
    result=json.loads(out.read_text())
    assert result['expected_tx_contexts']==3 and result['observed_tx_contexts']==1
    assert result['status']=='awaiting-observations' and result['incomplete_entries']==1


def test_bind_only_mode_does_not_publish_a_frequency(tmp_path):
    index,observations,out=fixture(tmp_path)
    analyze_native_word_network(index,observations,out,project=False)
    result=json.loads(out.read_text())
    assert result['status']=='observations-bound' and result['setup_model_frequency_mhz']is None


@pytest.mark.parametrize('kind',['fanin','missing','capture'])
def test_bad_native_streams_preserve_the_previous_output(tmp_path,kind):
    index,observations,out=fixture(tmp_path)
    out.write_text('previous qualified result\n')
    if kind=='fanin':observations['fpga0']['fanins'].write_text('invalid header\n')
    elif kind=='missing':observations['fpga1']['missing_rx'].write_text('source_origin\ns999\n')
    else:
        p=observations['fpga1']['captures'];lines=p.read_text().splitlines();p.write_text('\n'.join(lines[:-1])+'\n')
    with pytest.raises(EmuFlowError):analyze_native_word_network(index,observations,out)
    assert out.read_text()=='previous qualified result\n'


def test_request_generation_guard_rejects_replaced_input_before_publication(tmp_path,native):
    index,observations,out=fixture(tmp_path)
    files=[observations[name][role]for name in('fpga0','fpga1')for role in ROLES]
    parents=[bind_store_source(index,INDEX_SCHEMA),*(bind_store_source(p,ROW_SCHEMA)for p in files)]
    request=tmp_path/'request.bin';_write_projection_request(request,sources=parents,covered=[(0,()),(1,())],project=True)
    observations['fpga0']['tx_costs'].write_text('changed after source seal\n');out.write_text('old\n')
    done=subprocess.run([str(native),'phase7','analyze-word-network',str(index),str(request),str(out),
                         *(str(p)for p in files)],capture_output=True,text=True)
    assert done.returncode!=0 and 'generation differs' in done.stderr
    assert out.read_text()=='old\n'


def test_output_alias_and_unknown_partition_are_rejected(tmp_path):
    index,observations,out=fixture(tmp_path)
    with pytest.raises(ValidationError,match='aliases'):analyze_native_word_network(index,observations,index)
    with pytest.raises(ValidationError,match='unknown partition'):analyze_native_word_network(index,{'wrong':{}},out)


def test_production_export_preserves_readiness_prior_occurrence_and_capture_probes(tmp_path):
    index,observations,_=fixture(tmp_path)
    checks=tmp_path/'checks.tsv'
    export_native_word_checks(index,observations,checks)
    import csv
    with checks.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==5
    assert [r['expectation']for r in rows]==['accept','reject','accept','accept','reject']
    assert [bytes.fromhex(r['event_hex']).decode()for r in rows]==[
        'dut-root','previous-occurrence:dut-root','declared-link','capture-bound','reduced-cost-bound']
    assert sum(r['verification_arc_ps']=='1000'for r in rows)==4


def test_incomplete_native_exports_preserve_the_previous_verification_file(tmp_path):
    index,observations,_=fixture(tmp_path,observed=('fpga0',))
    out=tmp_path/'checks.tsv';out.write_text('previous\n')
    with pytest.raises(EmuFlowError,match='requires complete observations'):
        export_native_word_checks(index,observations,out)
    assert out.read_text()=='previous\n'


def test_verification_engine_destination_cannot_overwrite_input(tmp_path):
    out=tmp_path/'checks.tsv';out.write_text('original\n')
    with pytest.raises(ValidationError,match='aliases'):
        verify_native_word_checks(out,tmp_path)
    assert out.read_text()=='original\n'


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_production_witness_is_independently_checked_by_opensta(tmp_path):
    index,observations,_=fixture(tmp_path)
    checks=tmp_path/'checks.tsv';export_native_word_checks(index,observations,checks)
    result=verify_native_word_checks(checks,tmp_path/'engine',executable=os.environ['EMUFLOW_TEST_OPENSTA'])
    assert result['status']=='verified' and result['checks']==5 and result['expected_rejections']==2
    assert result['physical_acceptance']==result['original_path_sta_acceptance']=='pending'


def chain_fixture(tmp_path):
    """Four native partitions with recurring phases and signed relay credits."""
    import math
    transfers=[('a','fpga0','fpga1',0,24,6),('b','fpga1','fpga2',2,32,6),('c','fpga2','fpga3',1,24,6)]
    parts=[];observations={}
    for i in range(4):
        fpga=f'fpga{i}';incoming=transfers[i-1]if i else None;outgoing=transfers[i]if i<3 else None
        periods=sorted({t[4]//8 for t in(incoming,outgoing)if t})
        parts.append(dict(fpga=fpga,clocks={'native_clk':'core'},calendar_periods=periods,
            tx_entries=[(outgoing[0],'word[0]')]if outgoing else[],rx_entries=[(f's{i}',incoming[0])]if incoming else[]))
        directory=tmp_path/fpga;directory.mkdir()
        record={role:directory/(role+'.tsv')for role in ROLES};observations[fpga]=record
        tx=[];fanin='phase_word\tquery_hex\tnative_startpoints\trx_sources\tdut_cells\tinfrastructure_cells\tports\tunclassified\n'
        if outgoing:
            for phase in range(math.lcm(*periods)):
                if phase%(outgoing[4]//8)!=outgoing[3]:continue
                for edge in('rise','fall'):
                    tx.append(row(phase,source='native_clk',edge=edge,cost=1.371,measured=i==0 and edge=='rise'))
                if incoming:tx.append(row(phase,kind='rx',source=f's{i}',edge='',cost=-.8 if i==1 else 5.001))
                fanin+=f'{phase}\t'+b'word[0]'.hex()+f'\t1\t{int(bool(incoming))}\t{int(not incoming)}\t0\t0\t0\n'
        record['tx_costs'].write_text(HEADER+'\n'+''.join('\t'.join(r)+'\n'for r in tx));record['fanins'].write_text(fanin)
        cap=[]
        for target in('rise','fall'):
            query=f'dut-capture:native_clk:{target}'
            for edge in('rise','fall'):cap.append(row(-1,query,source='native_clk',edge=edge,measured=False))
            if incoming:
                r=row(-1,query,kind='rx',source=f's{i}',edge='',cost=3.245,measured=i==3 and target=='fall')
                if i==3 and target=='fall':r[13]=b'native_clk'.hex()
                cap.append(r)
        record['captures'].write_text(HEADER+'\n'+''.join('\t'.join(r)+'\n'for r in cap));record['missing_rx'].write_text('source_origin\n')
    index=tmp_path/'index.sqlite'
    write_native_word_index(index,transfers=transfers,partitions=parts,
        clock_windows=[('core',a,'core',b,100000 if a==b else 50000)for a in(1,2)for b in(1,2)],source_context={'scope':'four-partition native interface fixture'})
    return index,observations


def test_complete_relay_calendar_exports_both_readiness_and_strict_next_word_checks(tmp_path):
    import csv
    index,observations=chain_fixture(tmp_path);out=tmp_path/'checks.tsv'
    export_native_word_checks(index,observations,out)
    with out.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    names=[bytes.fromhex(r['event_hex']).decode()for r in rows]
    assert 'fanin-data:a'in names and 'fanin-next-word:a'in names
    assert 'previous-occurrence:fanin-next-word:a'in names
    assert names.count('declared-link')==3
    assert sum(r['expectation']=='reject'for r in rows)==4


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_opensta_checks_complete_multihop_native_witness_with_half_cycle_capture(tmp_path):
    index,observations=chain_fixture(tmp_path);out=tmp_path/'checks.tsv';summary=tmp_path/'analysis.json'
    analyze_native_word_network(index,observations,summary)
    model=json.loads(summary.read_text());assert model['required_reference_period_ps']==2*model['maximum_sampling_cost_ps']
    export_native_word_checks(index,observations,out)
    result=verify_native_word_checks(out,tmp_path/'engine',executable=os.environ['EMUFLOW_TEST_OPENSTA'])
    assert result['status']=='verified' and result['expected_rejections']==4 and result['windows']==1


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_saved_witness_replay_carries_independent_engine_evidence(tmp_path,monkeypatch):
    import shutil
    index,observations,_=fixture(tmp_path)
    checks=tmp_path/'checks.tsv';export_native_word_checks(index,observations,checks)
    live=tmp_path/'engine'
    result=verify_native_word_checks(checks,live,executable=os.environ['EMUFLOW_TEST_OPENSTA'])
    from emuflow import native_word_verification
    monkeypatch.setattr(native_word_verification,'run_native_event_checks',lambda *a,**kw:pytest.fail('saved replay launched an engine'))
    replay=tmp_path/'replay'
    verified=verify_native_word_checks(checks,replay,results_dir=live)
    assert verified['execution']=='saved-results'and verified['checks']==result['checks']
    for name in ('measurements.tsv','opensta.log'):assert(replay/name).read_bytes()==(live/name).read_bytes()
    shutil.rmtree(live)
    again=verify_native_word_checks(checks,tmp_path/'again',results_dir=replay)
    assert again['checks']==verified['checks']and again['engine']==verified['engine']


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='explicit qualified OpenSTA required')
def test_saved_evidence_change_during_copy_preserves_previous_replay(tmp_path,monkeypatch):
    import shutil
    index,observations,_=fixture(tmp_path)
    checks=tmp_path/'checks.tsv';export_native_word_checks(index,observations,checks)
    live=tmp_path/'engine'
    verify_native_word_checks(checks,live,executable=os.environ['EMUFLOW_TEST_OPENSTA'])
    output=tmp_path/'retained';output.mkdir()
    for name in ('measurements.tsv','opensta.log','native-word-witness-verification.json'):
        (output/name).write_text('previous '+name)
    copyfile=shutil.copyfile
    def changed(source,target):
        copyfile(source,target)
        if Path(source).name=='measurements.tsv':
            with Path(source).open('a')as stream:stream.write('changed during copy\n')
    monkeypatch.setattr(shutil,'copyfile',changed)
    with pytest.raises(ValidationError,match='differs|generation changed'):
        verify_native_word_checks(checks,output,results_dir=live)
    for name in ('measurements.tsv','opensta.log','native-word-witness-verification.json'):
        assert(output/name).read_text()=='previous '+name
