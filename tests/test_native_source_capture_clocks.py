"""Complete physical capture endpoints retain original engine clock facts."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.local_path_timing_storage import write_local_path_timing_storage
from emuflow.logic_segment_timing_storage import write_logic_segment_timing_storage
from emuflow.native_tools import native_file_generation
from emuflow.opensta_source_context import emit_opensta_source_context
from emuflow.source_launch_clocks import build_source_capture_clock_store
from emuflow.store_publication import StoreSource, bind_store_source
from tests.native_build import ROOT, emuflow_native, source_launch_clocks_test
from tests.test_native_source_launch_clocks import FAKE_ENGINE, prepared


def refresh(source, config):
    paths=[source,*(Path(p)for p in config['logic']+config['local']),*(Path(row['path'])for row in config['inputs'])]
    config['generations']={str(path):native_file_generation(path)for path in paths}


@pytest.fixture
def capture_prepared(prepared):
    source,config,_=prepared;root=source.parent
    with closing(sqlite3.connect(config['logic'][0]))as db:
        header=json.loads(db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0])
    pins=[query['pin'].rsplit('/',1)[0]+'/D'for query in config['queries'][:2]]
    ends=[*pins,'q[0]',pins[0],'removed/D']
    segments=[]
    for i,pin in enumerate(ends):
        group=f'segment{i:06d}'
        row=dict(id=f'capture{i}',kind='capture',system_path='static-exact-contract:'+group,
            member_path=f'capture{i}',cut_index=0,fpga='fpga0',replace_tx_endpoint=None,
            start_pin='rx/Q',end_pin=pin,delay_ns=2.,sampling_cost_ns=2.1,
            measurement='endpoint-exact',static_exact_segment_id=group,
            start_object_kind='pin',end_object_kind='port'if pin=='q[0]'else'pin')
        if pin=='removed/D':
            row.update(measurement='optimized-away',optimization_source='s0',delay_ns=None)
            row.pop('sampling_cost_ns')
        segments.append(row)
    with write_logic_segment_timing_storage(root/'capture-logic.json',header=header,segments=segments)as view:
        config['logic']=[str(view.store_path)]
    with closing(sqlite3.connect(config['local'][0]))as db:
        local_header=json.loads(db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0])
    local=dict(id='local0',kind='local',fpga='fpga0',clock_domain='clk',clock_period_ns=100.,
        start_pin='positive/Q',end_pin=pins[0],delay_ns=3.,sampling_cost_ns=3.1)
    with write_local_path_timing_storage(root/'capture-local.json',local_header,[local])as view:
        config['local']=[str(view.store_path)]
    engine=root/'fake-capture-sta'
    program=FAKE_ENGINE.replace('EMUFLOW_LAUNCH_','EMUFLOW_CAPTURE_').replace('source_seq','capture_seq').replace('sources={len(rows)}','captures={len(rows)}')
    engine.write_text('#!'+sys.executable+'\n'+program);engine.chmod(0o700)
    script=ROOT/'scripts/opensta/export_capture_clock_edges.tcl'
    paths=[engine,script,script.with_name('design_context.tcl'),script.with_name('capture_clock_edges.tcl'),
        *(Path(config[key])for key in ('liberty','verilog','clocks','timing_io'))]
    config.update(engine=str(engine),script=str(script),inputs=[dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())for path in paths],
        queries=[dict(pin=pin,kind='pin')for pin in pins])
    refresh(source,config)
    return source,config,root/'capture-clocks.sqlite'


def execute(prepared,mode='build'):
    source,config,output=prepared
    if mode=='build':
        try:
            build_source_capture_clock_store(source,output,logic_costs=config['logic'],local_costs=config['local'],
                source_identity=dict(original_ir_sha256=config['original_ir_sha256'],path_database_sha256='b'*64,
                    routes_sha256='c'*64,schedule_sha256='d'*64,semantic_sha256='e'*64,
                    timing_io_sha256=config.get('timing_io_sha256','f'*64)),
                command=[config['engine'],'-no_init','-exit',config['script']],
                environment={**os.environ,'EMUFLOW_STA_LIBERTY':config['liberty'],'EMUFLOW_STA_VERILOG':config['verilog'],
                    'EMUFLOW_STA_CLOCKS':config['clocks'],'EMUFLOW_STA_TIMING_IO':config['timing_io'],**config['environment']},
                timing_inputs={row['path']:row['sha256']for row in config['inputs']},
                source_bindings=[StoreSource(Path(path),'fixture-sealed-parent-v1',tuple(generation))for path,generation in config['generations'].items()],
                executable=str(emuflow_native()))
        except (EmuFlowError,ValidationError)as error:return subprocess.CompletedProcess([],1,'',str(error))
        mode='read'
    request=output.with_suffix('.json');request.write_text(json.dumps(config))
    return subprocess.run([str(source_launch_clocks_test()),str(source),str(request),str(output),'capture-'+mode],capture_output=True,text=True)


def test_complete_capture_rows_supply_clocks_beyond_the_local_path_endpoints(capture_prepared):
    result=execute(capture_prepared);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[[0.,25.],[50.]]
    # Only the positive endpoint exists in the local path owner. The negative
    # endpoint comes from the complete cross-capture rows, without a selector.
    with closing(sqlite3.connect(capture_prepared[2]))as db:
        assert db.execute('SELECT count(*) FROM captures').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0]==3
        assert db.execute('SELECT count(*) FROM top_outputs').fetchone()[0]==1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone()is None
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0]=='emuflow.source-capture-clocks/v1'
    assert not list(capture_prepared[2].parent.glob('*.facts.tsv'))


@pytest.mark.parametrize('damage',['omit-source','duplicate-fact','undeclared-clock','wrong-period','nonfinite','reverse',
    'extra-source','truncate','changed-input','wrong-count','no-completion','duplicate-completion',
    'omit-clock','duplicate-clock','wrong-clock-name','wrong-clock-period','no-clock-catalog'])
def test_incomplete_capture_facts_never_replace_an_existing_owner(capture_prepared,damage):
    source,config,output=capture_prepared;output.write_bytes(b'prior')
    config['environment']={'EMUFLOW_TEST_DAMAGE':damage,'EMUFLOW_TEST_SOURCE':str(source)}
    result=execute(capture_prepared)
    assert result.returncode!=0 and output.read_bytes()==b'prior',result.stdout


@pytest.mark.parametrize('pin,kind',[('positive/Q','pin'),('rst','port'),('absent/D','pin'),('positive/D','port')])
def test_capture_resolution_requires_an_original_sink(capture_prepared,pin,kind):
    capture_prepared[1]['queries']=[dict(pin=pin,kind=kind)]
    result=execute(capture_prepared,'resolve');assert result.returncode!=0 and not result.stdout


def test_top_output_is_retained_without_inventing_a_state_capture_clock(capture_prepared):
    assert execute(capture_prepared).returncode==0
    capture_prepared[1]['queries']=[dict(pin='q[0]',kind='port')]
    result=execute(capture_prepared,'read')
    assert result.returncode!=0 and 'explicit I/O constraints'in result.stderr


@pytest.mark.parametrize('parent',['source','logic','local','capture_clock_edges.tcl','clocks'])
def test_capture_owner_keeps_previously_validated_parent_generations(capture_prepared,parent):
    source,config,output=capture_prepared;output.write_bytes(b'prior')
    path=source if parent=='source'else Path(config[parent][0])if parent in ('logic','local')else(
        Path(config['script']).with_name(parent)if parent.endswith('.tcl')else Path(config[parent]))
    if parent.endswith('.tcl'):
        # Mutate a private copy, preserving the repository's script generation.
        copy=source.parent/parent;copy.write_bytes(path.read_bytes())
        for row in config['inputs']:
            if row['path']==str(path):row['path']=str(copy)
        refresh(source,config);path=copy
    # A byte change is observable even on filesystems whose timestamp
    # resolution cannot distinguish a newly copied file from an immediate touch.
    path.write_bytes(path.read_bytes()+b'\n')
    result=execute(capture_prepared)
    assert result.returncode!=0 and 'generation changed'in result.stderr
    assert output.read_bytes()==b'prior'


def test_capture_view_rejects_a_mixed_run_identity(capture_prepared):
    assert execute(capture_prepared).returncode==0
    with closing(sqlite3.connect(capture_prepared[2]))as db,db:
        db.execute("UPDATE metadata SET value=json_set(value,'$.schedule_sha256',?) WHERE key='source'",('0'*64,))
    result=execute(capture_prepared,'read');assert result.returncode!=0 and not result.stdout


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'),reason='real OpenSTA not configured')
def test_real_capture_producer_binds_positive_and_negative_source_edges(capture_prepared):
    source,config,_=capture_prepared
    context=emit_opensta_source_context(bind_store_source(source,'emuir-compact-v2'),source.parent/'engine-context',
        clocks={'clk':10.},timing_io={'input_groups':[{'clock':'clk','delay_ns':0.,'ports':['rst']}],'output_groups':[]},
        executable=os.environ['EMUFLOW_TEST_OPENSTA']).for_clock_query('state-capture')
    config.update(engine=context.command[0],script=context.command[-1],timing_io_sha256=context.timing_io['sha256'],
        inputs=[dict(path=str(path),sha256=seal)for path,seal in context.timing_inputs.items()])
    for key in ('liberty','verilog','clocks','timing_io'):config[key]=context.environment['EMUFLOW_STA_'+key.upper()]
    refresh(source,config)
    result=execute(capture_prepared);assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[[0.],[50.]]
    context.require_unchanged()
