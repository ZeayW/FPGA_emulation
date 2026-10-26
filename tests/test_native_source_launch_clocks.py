"""Native physical-source binding and one canonical launch-clock owner."""
from contextlib import closing
import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import signal
import time

import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.logic_segment_timing_storage import write_logic_segment_timing_storage
from emuflow.local_path_timing_storage import write_local_path_timing_storage
from emuflow.sampling_cost import SAMPLING_COST_POLICY
from emuflow.source_launch_clocks import build_source_launch_clock_store
from emuflow.errors import EmuFlowError,ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.store_publication import StoreSource
from emuflow.yosys import import_yosys_json
from tests.native_build import ROOT, emuflow_native, source_launch_clocks_test


FAKE_ENGINE=r'''
import os,pathlib,time
damage=os.environ.get('EMUFLOW_TEST_DAMAGE','')
if damage=='wait':
 pathlib.Path(os.environ['EMUFLOW_TEST_SOURCE']).write_text(str(os.getpid()))
 time.sleep(60)
rows=pathlib.Path(os.environ['EMUFLOW_LAUNCH_REQUESTS']).read_text().splitlines()[1:]
facts=[]
for row in rows:
 seq,pin=row.split('\t');name=bytes.fromhex(pin).decode()
 period=10.000000222044612
 phases=[period/2]if 'negative'in name else[0.]
 if 'positive'in name:phases.append(period/4)
 for phase in phases:facts.append([seq,'636c6b',str(period),str(phase)])
if damage=='omit-source':facts=[r for r in facts if r[0]!='1']
if damage=='duplicate-fact':facts.append(facts[-1])
if damage=='undeclared-clock':facts[0][1]='6f74686572'
if damage=='wrong-period':facts[0][2]='20'
if damage=='nonfinite':facts[0][3]='nan'
if damage=='reverse':facts.reverse()
if damage=='extra-source':facts.append([str(len(rows)),'636c6b','10','0'])
output=pathlib.Path(os.environ['EMUFLOW_LAUNCH_OUTPUT'])
output.write_text('source_seq\tclock_hex\tperiod_ns\tphase_ns\n'+''.join('\t'.join(r)+'\n'for r in facts))
if damage=='truncate':output.write_bytes(output.read_bytes()[:-1])
if damage=='changed-input':os.utime(os.environ['EMUFLOW_TEST_SOURCE'],None)
marker=f'EMUFLOW_LAUNCH_CLOCK_EXPORT status=pass sources={len(rows)} facts={len(facts)} clocks=1'
if damage=='wrong-count':marker=marker.replace('facts='+str(len(facts)),'facts=999')
if damage!='no-completion':print(marker)
if damage=='duplicate-completion':print(marker)
'''


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        native=tmp_path/'native';(native/'bin').mkdir(parents=True)
        (native/'bin/emuflow-native').symlink_to(emuflow_native())
        monkeypatch.setenv('EMUFLOW_NATIVE_ROOT',str(native))
    ir=import_yosys_json(ROOT/'examples/yosys/counter.json',top='counter',clocks=['clk'])
    ff=[cell for cell in ir.value['instances']if cell['type']=='FDRE'][:2]
    names=['positive','negative\\雪/hier']
    for cell,name in zip(ff,names):
        previous=cell['id'];cell['id']=name
        if name.startswith('negative'):cell.setdefault('parameters',{})['IS_C_INVERTED']='1'
        for net in ir.value['nets']:
            for endpoint in net['drivers']+net['sinks']:
                if endpoint['instance']==previous:endpoint['instance']=name
    source=tmp_path/'source.emuir.sqlite';write_compact_emuir(source,ir.value)
    seal=hashlib.sha256(source.read_bytes()).hexdigest()
    pins=['positive/Q','negative\\\\雪/hier/Q','rst']
    logic_header=dict(schema='emuflow.logic-segment-timing/v1',status='pass',design='counter',platform='board',
        fpga='fpga0',provider='test',qualification='endpoint-exact',coverage={},system_routes_sha256='c'*64,
        tdm_schedule_sha256='d'*64,semantic_contract_sha256='e'*64,sampling_cost_policy=SAMPLING_COST_POLICY)
    segments=[dict(id=f's{i}',kind='launch',system_path='static-exact-contract:segment000000',member_path=f'm{i}',
        cut_index=i,fpga='fpga0',replace_tx_endpoint=f'tx{i}',start_pin=pin,end_pin=f'tx{i}/D',delay_ns=2.,
        sampling_cost_ns=1.,measurement='endpoint-exact',static_exact_segment_id='segment000000',
        start_object_kind='port'if pin=='rst'else'pin',end_object_kind='pin')
        for i,pin in enumerate(pins)]
    # Repeated source across TX endpoints and original local timing must keep
    # one original-coordinate row rather than duplicate its clock facts.
    segments.append({**segments[0],'id':'duplicate-start','member_path':'m3','cut_index':3,'replace_tx_endpoint':'tx3','end_pin':'tx3/D'})
    with write_logic_segment_timing_storage(tmp_path/'logic.json',header=logic_header,segments=segments)as view:logic=view.store_path
    local_header=dict(schema='emuflow.local-path-timing/v1',status='pass',design='counter',fpga='fpga0',provider='test',
        qualification='endpoint-exact',identity_schema='emuflow.local-path-identity/v1',coverage={'local_paths':1},
        source=dict(path_database_sha256='b'*64,original_ir_sha256=seal,assignment_sha256='a'*64,routes_sha256='c'*64,
            original_paths=1,original_path_ids_sha256='a'*64),sampling_cost_policy=SAMPLING_COST_POLICY)
    local_record=dict(id='local0',kind='local',fpga='fpga0',clock_domain='clk',clock_period_ns=100.,
        start_pin=pins[0],end_pin='capture/D',delay_ns=3.,sampling_cost_ns=2.)
    with write_local_path_timing_storage(tmp_path/'local.json',local_header,[local_record])as view:local=view.store_path
    engine=tmp_path/'fake-sta';engine.write_text('#!'+sys.executable+'\n'+FAKE_ENGINE);engine.chmod(0o700)
    script=ROOT/'scripts/opensta/export_launch_clock_edges.tcl'
    files={key:tmp_path/(key+'.txt')for key in ('liberty','verilog','clocks','timing_io')}
    for path in files.values():path.write_text('fixture\n')
    files['clocks'].write_text('clock_hex\tperiod_ns\n636c6b\t10\n')
    inputs=[engine,script,script.with_name('design_context.tcl'),script.with_name('launch_clock_edges.tcl'),*files.values()]
    config=dict(original_ir_sha256=seal,engine=str(engine),script=str(script),**{k:str(v)for k,v in files.items()},
        inputs=[dict(path=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest())for path in inputs],
        logic=[str(logic)],local=[str(local)],queries=[dict(pin=pin,kind='port'if pin=='rst'else'pin')for pin in pins],
        targets=dict(clk=100.),environment={})
    config['generations']={str(path):native_file_generation(path)for path in [source,logic,local,*inputs]}
    return source,config,tmp_path/'launch-clocks.sqlite'


def execute(prepared,mode='build'):
    source,config,output=prepared;path=output.with_suffix('.request.json');path.write_text(json.dumps(config))
    if mode=='build':
        try:
            build_source_launch_clock_store(source,output,logic_costs=config['logic'],local_costs=config['local'],
                source_identity=dict(original_ir_sha256=config['original_ir_sha256'],path_database_sha256='b'*64,
                    routes_sha256='c'*64,schedule_sha256='d'*64,semantic_sha256='e'*64,timing_io_sha256='f'*64),
                command=[config['engine'],'-no_init','-exit',config['script']],
                environment={**os.environ,'EMUFLOW_STA_LIBERTY':config['liberty'],'EMUFLOW_STA_VERILOG':config['verilog'],
                    'EMUFLOW_STA_CLOCKS':config['clocks'],'EMUFLOW_STA_TIMING_IO':config['timing_io'],**config['environment']},
                timing_inputs={row['path']:row['sha256']for row in config['inputs']},
                source_bindings=[StoreSource(Path(path),'fixture-sealed-parent-v1',tuple(generation))
                    for path,generation in config['generations'].items()],executable=str(emuflow_native()))
        except (EmuFlowError,ValidationError)as error:
            return subprocess.CompletedProcess([],1,'',str(error))
        mode='read'
    return subprocess.run([str(source_launch_clocks_test()),str(source),str(path),str(output),mode],capture_output=True,text=True)


def test_original_coordinates_deduplicate_phases_and_scale_engine_time(prepared):
    result=execute(prepared)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[[0.,25.],[50.],[0.]]
    with closing(sqlite3.connect(prepared[2]))as db:
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0]==3
        assert db.execute('SELECT count(*) FROM facts').fetchone()[0]==4
        assert db.execute("SELECT name FROM sqlite_master WHERE name='requests'").fetchone()is None
        assert db.execute('SELECT cell_seq FROM sources ORDER BY source_seq').fetchall().count((-1,))==1
        assert json.loads(db.execute("SELECT value FROM metadata WHERE key='timing_inputs'").fetchone()[0])
    assert not list(prepared[2].parent.glob('*.facts.tsv'))


@pytest.mark.parametrize('damage',['omit-source','duplicate-fact','undeclared-clock','wrong-period','nonfinite','reverse',
    'extra-source','truncate','changed-input','wrong-count','no-completion','duplicate-completion'])
def test_incomplete_engine_results_never_replace_existing_owner(prepared,damage):
    prepared[2].write_bytes(b'prior-source-clock-owner')
    prepared[1]['environment']={'EMUFLOW_TEST_DAMAGE':damage,'EMUFLOW_TEST_SOURCE':str(prepared[0])}
    result=execute(prepared)
    assert result.returncode!=0 and not result.stdout
    assert prepared[2].read_bytes()==b'prior-source-clock-owner'


@pytest.mark.parametrize('damage',['routes','schedule','semantic','local-source','physical-policy','missing-local','unbound-context'])
def test_mixed_source_and_physical_contexts_are_rejected(prepared,damage):
    source,config,output=prepared;output.write_bytes(b'prior')
    if damage=='missing-local':config['local']=[]
    elif damage=='unbound-context':config['inputs']=config['inputs'][:-1]
    else:
        local=damage=='local-source';path=config['local'if local else'logic'][0]
        with sqlite3.connect(path)as db:
            header=json.loads(db.execute("SELECT value FROM metadata WHERE key='header'").fetchone()[0])
            if local:header['source']['original_ir_sha256']='0'*64
            else:header[dict(routes='system_routes_sha256',schedule='tdm_schedule_sha256',semantic='semantic_contract_sha256',
                **{'physical-policy':'sampling_cost_policy'})[damage]]='0'*64
            db.execute("UPDATE metadata SET value=? WHERE key='header'",(json.dumps(header),))
        config['generations'][path]=native_file_generation(Path(path))
    result=execute(prepared)
    assert result.returncode!=0 and output.read_bytes()==b'prior'


@pytest.mark.parametrize('pin,kind',[('positive/D','pin'),('absent/Q','pin'),('q[0]','port'),
    ('negative\\雪/hier/Q','pin'),('positive/Q','port')])
def test_only_exact_original_drivers_are_architectural_sources(prepared,pin,kind):
    prepared[1]['queries']=[dict(pin=pin,kind=kind)]
    result=execute(prepared,'resolve')
    assert result.returncode!=0 and not result.stdout


def test_consumer_requires_source_seal_and_every_target_clock(prepared):
    assert execute(prepared).returncode==0
    prepared[1]['original_ir_sha256']='0'*64
    assert execute(prepared,'read').returncode!=0
    prepared[1]['original_ir_sha256']=hashlib.sha256(prepared[0].read_bytes()).hexdigest()
    prepared[1]['targets']={}
    assert execute(prepared,'read').returncode!=0


def test_owner_and_original_source_relocate_without_query_name_payloads(prepared):
    initial=execute(prepared);assert initial.returncode==0,initial.stderr
    moved=prepared[2].parent/'moved';moved.mkdir()
    source=moved/'source.sqlite';output=moved/'clocks.sqlite'
    shutil.copyfile(prepared[0],source);shutil.copyfile(prepared[2],output)
    result=execute((source,prepared[1],output),'read')
    assert result.returncode==0 and result.stdout==initial.stdout,result.stderr


def test_bus_driver_coordinates_match_independent_provider_and_verilog_recipes(prepared):
    from emuflow.ir import EmuIR
    from emuflow.logic_segment_timing import _vivado_object,_instance_pin_inventory
    from emuflow.verilog import _timing_pin_name
    ir=EmuIR.load(prepared[0]);macro='macro\\雪/ram'
    ir.value['instances'].append(dict(id=macro,type='RAMB18E2',resources={'bram18k':1}))
    for bit in (0,3):ir.value['nets'].append(dict(id=f'bus{bit}',name=f'bus{bit}',cut_class='register_output',
        drivers=[dict(instance=macro,port='DOADO',bit=bit)],sinks=[]))
    for port in ir.value['ports']:
        if port['id']=='rst':port['width']=4
    for net in ir.value['nets']:
        for endpoint in net['drivers']:
            if endpoint['instance']is None and endpoint['port']=='rst':endpoint['bit']=3
    source=prepared[0].with_name('bus-source.sqlite');write_compact_emuir(source,ir.value)
    pins=_instance_pin_inventory(ir);instances={cell['id']:cell for cell in ir.value['instances']}
    kind,pin=_vivado_object(ir,dict(instance=macro,port='DOADO',bit=3),pins,instances)
    prepared[1]['queries']=[dict(pin=pin,kind=kind),dict(pin='rst[3]',kind='port')]
    result=execute((source,prepared[1],prepared[2]),'resolve');assert result.returncode==0,result.stderr
    actual=json.loads(result.stdout)
    with closing(sqlite3.connect(source))as db:
        cell=db.execute('SELECT seq FROM cell_records WHERE id=?',(macro,)).fetchone()[0]
        port=db.execute("SELECT seq FROM pin_names WHERE name='DOADO'").fetchone()[0]
    assert actual[0]==[cell,port,3,macro.replace('\\','\\\\')+'/'+_timing_pin_name(pins[macro],'DOADO',3)]
    assert actual[1][0]==-1 and actual[1][2:]==[3,'rst[3]']
    for absent in (pin.replace('[3]','[1]'),'rst[0]'):
        prepared[1]['queries']=[dict(pin=absent,kind='')]
        assert execute((source,prepared[1],prepared[2]),'resolve').returncode!=0


def test_cancellation_reaps_the_engine_and_preserves_previous_owner(prepared,monkeypatch):
    marker=prepared[2].with_suffix('.engine-pid');prepared[2].write_bytes(b'prior')
    prepared[1]['environment']={'EMUFLOW_TEST_DAMAGE':'wait','EMUFLOW_TEST_SOURCE':str(marker)}
    real_run=subprocess.run;engine_pid=None
    def cancel(command,**kwargs):
        nonlocal engine_pid
        if command[1:3]!=['phase7','build-launch-clocks']:return real_run(command,**kwargs)
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+10
            while not marker.exists()and time.monotonic()<deadline:time.sleep(.02)
            assert marker.exists(),'engine did not start'
            engine_pid=int(marker.read_text());process.send_signal(signal.SIGTERM)
            stdout,stderr=process.communicate(timeout=10)
            return subprocess.CompletedProcess(command,process.returncode,stdout,stderr)
        finally:
            if process.poll()is None:process.kill();process.wait()
    monkeypatch.setattr('emuflow.source_launch_clocks.subprocess.run',cancel)
    assert execute(prepared).returncode!=0
    assert prepared[2].read_bytes()==b'prior'
    with pytest.raises(ProcessLookupError):os.kill(engine_pid,0)


@pytest.mark.parametrize('damage',['crc','prepared-generation','tool-generation','report-alias'])
def test_native_request_rejects_corruption_generation_changes_and_report_aliases(prepared,monkeypatch,damage):
    prepared[2].write_bytes(b'prior');real_run=subprocess.run
    timing=Path(prepared[1]['liberty']);before=timing.read_bytes()
    def alter(command,**kwargs):
        if command[1:3]==['phase7','build-launch-clocks']:
            if damage=='crc':
                path=Path(command[4]);data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
            elif damage=='prepared-generation':os.utime(prepared[0],None)
            elif damage=='tool-generation':
                path=Path(command[5]);path.write_bytes(path.read_bytes()+b'X')
            else:command=[command[0],'--report',str(timing),*command[1:]]
        return real_run(command,**kwargs)
    monkeypatch.setattr('emuflow.source_launch_clocks.subprocess.run',alter)
    result=execute(prepared)
    assert result.returncode!=0 and prepared[2].read_bytes()==b'prior'
    assert timing.read_bytes()==before


def test_output_cannot_alias_a_bound_timing_input(prepared):
    source,config,_output=prepared;output=Path(config['liberty']);before=output.read_bytes()
    assert execute((source,config,output)).returncode!=0
    assert output.read_bytes()==before


def test_a_previously_changed_parent_cannot_renew_its_old_content_seal(prepared):
    prepared[2].write_bytes(b'prior');os.utime(prepared[0],None)
    result=execute(prepared)
    assert result.returncode!=0 and 'generation changed'in result.stderr
    assert prepared[2].read_bytes()==b'prior'


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),reason='explicit qualified OpenSTA launch API required')
def test_real_engine_uses_original_coordinates_and_negative_launch_phase(prepared):
    from emuflow.indexed_timing import IndexedTimingView
    from emuflow.opensta import load_timing_model,DEFAULT_TIMING_MODEL,render_opensta_liberty,_write_timing_io_map
    from emuflow.sta_clock_logic import specialize_clock_logic
    from emuflow.verilog import write_mapped_verilog
    source,config,_output=prepared;view=IndexedTimingView(source)
    try:
        model=load_timing_model(DEFAULT_TIMING_MODEL);specialize_clock_logic(view,model)
        write_mapped_verilog(view,Path(config['verilog']),timing_only=True,timing_cell_types=view.timing_types())
        Path(config['liberty']).write_text(render_opensta_liberty(model))
        _write_timing_io_map(view,{'clk':10.},{'input_groups':[{'clock':'clk','delay_ns':0.,'ports':['rst']}],
            'output_groups':[]},Path(config['timing_io']))
    finally:view.close()
    previous=config['engine'];config['engine']=str(Path(os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE']).resolve())
    for row in config['inputs']:
        if row['path']==previous:row['path']=config['engine']
        row['sha256']=hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()
    config['generations'].pop(previous)
    for row in config['inputs']:config['generations'][row['path']]=native_file_generation(Path(row['path']))
    result=execute(prepared)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[[0.],[50.],[0.]]
