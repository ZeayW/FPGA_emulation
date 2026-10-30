"""Transport synthesis owns the engine, source import, transform and publication.

Tool doubles exercise contracts; the whole-flow qualification design is NVDLA.
"""
import copy
import json
import os
from pathlib import Path
import sys
from unittest import mock
import pytest
from emuflow.common.errors import ValidationError
from emuflow.ir.ir import EmuIR
from tests.yosys_reference import import_yosys_json
from emuflow_tools.physical.transport_synthesis import synthesize_transport
from tests.transport_fanout_reference import buffer_transport_high_fanout_records as reference_buffer


def source(fanout=17,*,cut_class='combinational',two=False):
    cells={};netnames={};ports={'fabric_clk':{'direction':'input','bits':[1]}}
    for n in range(2 if two else 1):
        bit=100+n;name='wide'+str(n);netnames[name]={'bits':[bit]}
        cells['driver'+str(n)]={'type':'$lut','parameters':{'LUT':'10','WIDTH':'1'},'attributes':{},
            'connections':{'A':['0'],'Y':[bit]},'port_directions':{'A':'input','Y':'output'}}
        for i in range(fanout):
            cells[f'sink{n}_{i:06d}']={'type':'$lut','parameters':{'LUT':'10','WIDTH':'1'},'attributes':{},
                'connections':{'A':[bit],'Y':[1000+n*fanout+i]},'port_directions':{'A':'input','Y':'output'}}
        if cut_class in ('clock','reset'):
            # Keep these excluded classes source-declared, with the same driver.
            ports['clk' if cut_class=='clock' else 'reset']={'direction':'output','bits':[bit]}
    return {'creator':'controlled-yosys','modules':{'transport':{'attributes':{'top':'1'},'cells':cells,'ports':ports,'netnames':netnames}}}


def case(root,fanout=17,*,damage='',two=False):
    root.mkdir(exist_ok=True)
    runtime=root/'runtime.sv';rtl=root/'transport.sv';engine=root/'yosys';trace=root/'observed.json'
    runtime.write_text('module runtime; endmodule\n');rtl.write_text('module transport(input fabric_clk); endmodule\n')
    data=source(fanout,two=two)
    engine.write_text('#!'+sys.executable+'\n'+f'''
import json,os,sys,time,signal,subprocess
from pathlib import Path
DAMAGE={damage!r}
Path({str(trace)!r}).write_text(json.dumps(sys.argv))
if DAMAGE=='hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
 Path({str(root/'children.json')!r}).write_text(json.dumps([os.getpid(),child.pid]))
 time.sleep(60)
if DAMAGE=='exit':
 print('early diagnostic sentinel',file=sys.stderr)
 for i in range(15000):print('ordinary diagnostics '*16,file=sys.stderr)
 raise SystemExit(7)
if DAMAGE=='source':Path({str(rtl)!r}).write_text('mutated during synthesis')
if DAMAGE=='invalid':sys.stdout.write('not a Yosys document');raise SystemExit(0)
print('generic diagnostic tail',file=sys.stderr)
print(json.dumps({data!r}))
''');engine.chmod(0o755)
    return dict(runtime_rtl=runtime,transport_rtl=rtl,output_dir=root/'synthesis',executable=str(engine),maximum_fanout=4),data,trace


def test_complete_native_transport_source_import_and_tree(tmp_path):
    options,data,trace=case(tmp_path)
    with mock.patch('tests.yosys_reference.import_yosys_json_indexed',side_effect=AssertionError('host graph import')):
        result=synthesize_transport(**options)
    assert result['implementation_id']=='native-transport-synthesis-v1'
    assert result['fanout_buffering']['summary']=={'buffered_nets':1,'buffer_cells':7,'added_nets':7,
        'maximum_original_fanout':17,'maximum_result_fanout':4}
    assert result['instances']==25 and result['resources']=={'lut':25}
    assert result['import']['emuir_records']['cell_records']==25
    assert Path(result['log']).read_text()=='generic diagnostic tail\n'
    assert result['engine']['returncode']==0
    command=json.loads(trace.read_text());assert command[1:3]==['-q','-p']
    assert 'abc -lut 6; dffunmap; delete t:$scopeinfo' in command[3]
    assert command[3].endswith('check; write_json')
    assert not list(tmp_path.glob('**/*stdout*.json'))
    actual=EmuIR.load(Path(result['output']));assert len(actual.value['instances'])==25


@pytest.mark.parametrize('fanout,maximum,two',[(1,4,False),(4,4,False),(5,4,False),(17,4,False),(65,4,True),(513,512,False)])
def test_native_tree_and_every_final_fact_match_the_independent_reference(tmp_path,fanout,maximum,two):
    options,data,_=case(tmp_path,fanout,two=two);options['maximum_fanout']=maximum
    actual=synthesize_transport(**options)
    source_path=tmp_path/'reference.json';source_path.write_text(json.dumps(data))
    ir=import_yosys_json(source_path,top='transport',clocks=['fabric_clk'])
    from emuflow_tools.ir.emuir_fixture import write_compact_emuir
    reports=[];reference=tmp_path/'reference.emuir.sqlite'
    write_compact_emuir(reference,ir.value,_finalize=lambda db:reports.append(reference_buffer(db,max_fanout=maximum)))
    expected=EmuIR.load(reference).value;observed=EmuIR.load(Path(actual['output'])).value
    for key in ('instances','nets','ports','clocks','warnings'):assert observed[key]==expected[key],key
    assert json.loads(Path(actual['fanout_buffering']['output']).read_text())==reports[0]


@pytest.mark.parametrize('damage',['exit','invalid','source'])
def test_failure_never_publishes_a_partial_transport_generation(tmp_path,damage):
    options,_,_=case(tmp_path,damage=damage)
    with pytest.raises(ValidationError):synthesize_transport(**options)
    assert not options['output_dir'].exists()
    logs=list(tmp_path.glob('.transport-synthesis.*/transport-yosys.log'))
    assert logs
    assert not list(tmp_path.glob('.transport-synthesis.*/stage'))
    if damage=='exit':
        assert logs[0].stat().st_size>2*1024*1024
        assert 'early diagnostic sentinel' in logs[0].read_text()


def test_preexisting_transport_owner_cannot_be_modified(tmp_path):
    options,*_=case(tmp_path);first=synthesize_transport(**options)
    path=Path(first['output']);previous=path.read_bytes()
    with pytest.raises(ValidationError,match='fresh absolute directory'):synthesize_transport(**options)
    assert path.read_bytes()==previous


@pytest.mark.parametrize('maximum',[1,True,2.5,-1])
def test_invalid_policy_is_rejected_before_starting_yosys(tmp_path,maximum):
    options,_,trace=case(tmp_path);options['maximum_fanout']=maximum
    with pytest.raises(ValidationError):synthesize_transport(**options)
    assert not trace.exists()


def replace_source(options,original,changed):
    path=Path(options['executable']);text=path.read_text()
    assert text.count(repr(original))==1
    path.write_text(text.replace(repr(original),repr(changed)))


@pytest.mark.parametrize('kind',['clock','reset'])
def test_dedicated_clock_and_reset_nets_are_not_buffered(tmp_path,kind):
    options,data,_=case(tmp_path)
    changed=copy.deepcopy(data)
    port='fabric_clk' if kind=='clock' else 'reset'
    changed['modules']['transport']['ports'][port]={'direction':'output','bits':[100]}
    changed['modules']['transport']['netnames'][port]={'bits':[100]}
    replace_source(options,data,changed)
    result=synthesize_transport(**options)
    assert result['fanout_buffering']['summary']['buffered_nets']==0
    actual=EmuIR.load(Path(result['output'])).value
    assert next(n for n in actual['nets'] if n.get('yosys_bit')==100)['cut_class']==kind


def test_identifier_collision_rejects_the_whole_writer_generation(tmp_path):
    options,data,_=case(tmp_path)
    changed=copy.deepcopy(data)
    changed['modules']['transport']['cells']['$emuflow_transport_fanout$000000$l00$g000000$buf']={
        'type':'$lut','parameters':{'WIDTH':'1','LUT':'10'},'attributes':{},
        'connections':{'A':['0'],'Y':[50000]},'port_directions':{'A':'input','Y':'output'}}
    replace_source(options,data,changed)
    with pytest.raises(ValidationError,match='identifier collision'):synthesize_transport(**options)
    assert not options['output_dir'].exists()


def test_unflattened_transport_output_is_rejected_without_a_hierarchy_fallback(tmp_path):
    options,data,_=case(tmp_path)
    changed=copy.deepcopy(data);changed['modules']['child']={'ports':{},'cells':{},'netnames':{}}
    changed['modules']['transport']['cells']['remaining_module']={'type':'child','connections':{}}
    replace_source(options,data,changed)
    with pytest.raises(ValidationError,match='did not flatten'):synthesize_transport(**options)
    assert not options['output_dir'].exists()


def test_changed_published_generation_cannot_be_consumed_by_the_host(tmp_path):
    options,*_=case(tmp_path)
    from emuflow_tools.physical import transport_synthesis as host
    read=host.read_small_object
    def damage(path,*args):
        value=read(path,*args)
        if Path(path).name=='operation.json':
            target=Path(value['outputs'][1]['path']);target.chmod(target.stat().st_mode^0o100)
        return value
    with mock.patch.object(host,'read_small_object',side_effect=damage),pytest.raises(ValidationError,match='output generation changed'):
        synthesize_transport(**options)


def test_an_ambient_container_binary_cannot_reidentify_a_direct_yosys(tmp_path,monkeypatch):
    from emuflow.common.json_io import file_sha256
    options,*_=case(tmp_path)
    monkeypatch.setenv('EMUFLOW_YOSYS_CONTAINER_BINARY',str(tmp_path/'absent'))
    result=synthesize_transport(**options)
    assert result['executable_sha256']==file_sha256(Path(options['executable']))


def test_the_canonical_container_launcher_requires_an_explicit_binary(tmp_path,monkeypatch):
    from emuflow.common.paths import REPOSITORY_ROOT
    options,*_=case(tmp_path)
    wrapper=tmp_path/'container-yosys';wrapper.write_bytes((REPOSITORY_ROOT/'scripts/yosys/container_runtime.sh').read_bytes());wrapper.chmod(0o755)
    options['executable']=str(wrapper);monkeypatch.delenv('EMUFLOW_YOSYS_CONTAINER_BINARY',raising=False)
    with pytest.raises(ValidationError,match='lacks an explicit binary'):synthesize_transport(**options)


def test_transport_timeout_reaps_the_engine_and_its_child(tmp_path):
    import time
    from tests.test_native_tool_runtime import process_alive
    options,_,_=case(tmp_path,damage='hang');options['timeout_ms']=5000
    with pytest.raises(ValidationError,match='wall-time limit'):synthesize_transport(**options)
    children=json.loads((tmp_path/'children.json').read_text())
    deadline=time.monotonic()+5
    while any(process_alive(pid) for pid in children) and time.monotonic()<deadline:time.sleep(.02)
    assert not any(process_alive(pid) for pid in children)
    assert not options['output_dir'].exists()
