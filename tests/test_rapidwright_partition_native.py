"""Complete production partition owner with controlled external EDA processes.

These are contract regressions. Whole-design qualification continues to use NVDLA.
"""
from emuflow_tools.ir.emuir_fixture import write_emuir_fixture
import copy
import json
from pathlib import Path
import sqlite3
from unittest import mock

import pytest
from emuflow.common.errors import ValidationError
from tests.yosys_reference import import_yosys_json
from emuflow_tools.physical.xilinx_physical_backend import run_rapidwright_openparf_native_candidate_backend
from tests.test_openparf_runtime_native import executable as placement_tool
from tests.test_rwroute_stage_native import setup as route_tool
from tests.test_phase2_native import _fixed_opensta
from tests.openparf_runtime_fixture import write_openparf_runtime_fixture
from tests.test_boundary_identity_storage import _header
from emuflow_tools.timing.boundary_identity_storage import write_boundary_identity_storage


def case(root):
    root.mkdir(exist_ok=True)
    vendor=root/'vendor';options,contract=route_tool(vendor)
    module={'attributes':{'top':'1'},'cells':{},'ports':{'clk':{'direction':'input','bits':[9]},'a':{'direction':'input','bits':[1]}},
            'netnames':{n:{'bits':[b]} for n,b in [('a',1),('middle',2),('q1',3),('q2',4),('clk',9)]}}
    for i in range(3):
        module['cells'][f'c{i}']={'type':'LUT1','parameters':{'INIT':'10'},'attributes':{},
            'connections':{'I0':[1 if i==0 else 2],'O':[2+i]},'port_directions':{'I0':'input','O':'output'}}
    for i in (1,2):
        module['cells'][f'c{i}']={'type':'FDRE','parameters':{},'attributes':{},
            'connections':{'C':[9],'D':[2],'Q':[2+i],'CE':['1'],'R':['0']},
            'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'}}
    module['ports']['q1']={'direction':'output','bits':[3]}
    source=root/'source.json';source.write_text(json.dumps({'creator':'fixture','modules':{'top':module}}))
    ir=root/'source.emuir.sqlite';write_emuir_fixture(ir, (import_yosys_json(source,clocks=['clk'])).value)
    _,_,arch=write_openparf_runtime_fixture(root/'architecture')
    doc=json.loads(arch.read_text());doc['part']='xcvu19p-test'
    for site in doc['sites']:site['physical_region']={'slr':'SLR0','clock_region':'X0Y0'}
    arch.write_text(json.dumps(doc))
    placement_root=root/'placement-tool';placement_root.mkdir()
    placement,count,control=placement_tool(placement_root)
    code=placement['python_executable'].read_text()
    start=code.index("if not control.get('omit_placement'):")
    end=code.index("if not control.get('omit_metrics'):",start)
    code=code[:start]+'''if not control.get('omit_placement'):
    import sqlite3
    directory=Path(config['aux_input']).parent
    selector=json.loads((directory/'name_map.json').read_text())
    with sqlite3.connect(directory/selector['artifact']) as db: atoms=db.execute('SELECT openparf,resource FROM atoms ORDER BY seq').fetchall()
    with sqlite3.connect(directory/'site-map.sqlite3') as db: sites={r:db.execute("SELECT dense_x,dense_y FROM resources WHERE resource=? ORDER BY dense_x,dense_y",(r,)).fetchall() for r in ('LUT','FF')}
    result.write_text(''.join(f'{a[0]} {sites[a[1]][i][0]} {sites[a[1]][i][1]} {int(a[1]=="LUT")}\\n' for i,a in enumerate(atoms)))
'''+code[end:]
    placement['python_executable'].write_text(code)
    for name in ('java','javac'):
        path=vendor/name;code=path.read_text()
        start=code.index("v['source'].update(");end=code.index("v['schema']=",start)
        code=code[:start]+'''rows=[line.split('\\t') for line in source.read_text().splitlines()]
physical={f[1]:f[4] for f in rows if f[0]=='CELL'}
mapped=source.parent.parent/'partition.mapped.sqlite'
# The input belongs to a private route generation directly under the partition.
physical_db=next((source.parent.parent/'openparf-native').glob('**/physical-handoff.sqlite'))
v['source'].update(mapped_sha256=hashlib.sha256(mapped.read_bytes()).hexdigest(),
 packed_sha256=hashlib.sha256(physical_db.read_bytes()).hexdigest(),
 placement_sha256=hashlib.sha256(physical_db.read_bytes()).hexdigest(),rwroute_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
net=next(f[1] for f in rows if f[0]=='NET')
v['nets'][0]['net']=net
pins=[f for f in rows if f[0]=='PIN' and f[1]==net]
for actual,observed in zip(v['nets'][0]['pins'],pins):actual['site']=physical[observed[2]]
v['excluded_nets']=[dict(net=f[1],reason=f[2],**({'qualification':f[3]} if len(f)>3 else {})) for f in rows if f[0]=='EXCLUDED']
v['summary']['excluded_nets']=len(v['excluded_nets'])
v['summary']['boundary_clock_nets']=sum(e['reason']=='boundary_clock' for e in v['excluded_nets'])
v['physical_resources']={'lut':1,'ff':2,'bram18k':0,'dsp48':0,'carry8':0,'uram288':0}
'''+code[end:]
        path.write_text(code)
    boundary=root/'boundary.json'
    with write_boundary_identity_storage(boundary,_header(),[]):pass
    rows=[['c0/O->c1/D#00000000'.encode().hex(),'clk'.encode().hex(),'10','0.5','9.5','middle'.encode().hex()]]
    engine,trace=_fixed_opensta(root,rows)
    runtime={'fabric_clock':{'period_ns':4.0},'virtual_dut_clocks':{'clk':{'nominal_period_ns':10.0}},
        'virtual_dut_clock':{'nominal_period_ns':10.0},'resource_budget':{'id':'fpga0','part':'xcvu19p-test',
        'capacity':{'lut':100,'ff':100,'bram18k':100,'dsp48':100,'carry8':100,'uram288':100},'utilization_limit':0.25,
        'effective_capacity':{k:25 for k in ('lut','ff','bram18k','dsp48','carry8','uram288')}}}
    kwargs=dict(fpga='fpga0',part='xcvu19p-test',merged_ir_path=ir,architecture_path=arch,runtime=runtime,
        original_cells=3,transport_cells=0,output_dir=root/'physical',boundary_identity_path=boundary,
        rapidwright_jar=options['rapidwright_jar'],java=options['java'],java_source=options['java_source'],
        device_data_root=vendor,timing_data_dir=vendor,openparf_install=placement['install_root'],
        openparf_python=placement['python_executable'],opensta=str(engine))
    return kwargs,contract,control,count,trace


def run(kwargs,contract):
    with mock.patch('emuflow_tools.physical.xilinx_physical_backend.RAPIDWRIGHT_TIMING_CONTRACT',contract):
        return run_rapidwright_openparf_native_candidate_backend(**kwargs)


def test_complete_partition_is_owned_by_one_native_call(tmp_path):
    kwargs,contract,*_=case(tmp_path)
    with (mock.patch('emuflow_tools.physical.xilinx_netlist.emit_xilinx_mapped_index',side_effect=AssertionError('host mapped graph')),
          mock.patch('emuflow_tools.physical.openparf.qualification_native.run_qualification',side_effect=AssertionError('host placement coordinator')),
          mock.patch('emuflow_tools.physical.rapidwright.route_stage.run_native_rwroute_stage',side_effect=AssertionError('host route coordinator')),
          mock.patch('emuflow_tools.timing.xilinx_opensta.run_xilinx_routed_opensta',side_effect=AssertionError('host STA coordinator'))):
        result=run(kwargs,contract)
    assert result['implementation_id']=='native-rapidwright-partition-v1'
    assert result['result']['cell_accounting']['routed_cells']==3
    assert result['result']['timing']['wns_ns']==.5
    assert result['result']['resource_usage']['physical']['counts']['lut']==1
    assert result['boundary_timing']['import']['endpoints']==0
    assert not list(kwargs['output_dir'].glob('**/placement.json'))
    assert not list(kwargs['output_dir'].glob('**/packed-sites.json'))
    assert json.loads((kwargs['output_dir']/'partition-report.json').read_text())['status']=='pass'


def add_timing_identities(kwargs):
    from tests.test_boundary_identity_storage import _record
    from tests.test_local_path_timing_storage import _header as local_header, _record as local_record
    from tests.timing_facts import local_timing_facts
    from tests.logic_identity_fixture import identity_facts
    from emuflow_tools.timing.local_path_timing import path_id_set_sha256
    root=kwargs['merged_ir_path'].parent
    # The output pin belongs to c1; its upstream delay remains independently queried.
    row=_record('tx','tx',0);row['merged_ir']['external_port']='q1';row['merged_ir']['external_port_bit']=0
    with write_boundary_identity_storage(kwargs['boundary_identity_path'],_header(),[row]):pass
    local=root/'local.json';header=local_header();header['qualification']='typed-local-path-query-identity-owner-v1'
    header['coverage']={'local_paths':1};header['source']['original_paths']=1;header['source']['original_path_ids_sha256']=path_id_set_sha256(['p0'])
    record=local_record('p0',0.0);record.update(start_pin='cell:c0/O',end_pin='cell:c1/D',path_pins=[],measurement='endpoint-longest-path-fallback')
    with local_timing_facts(local,header,[record]):pass
    logic=root/'logic.json';header={'schema':'emuflow.logic-segment-identity/v1','status':'pass','design':'dut','platform':'board',
        'fpga':'fpga0','provider':'fixture','coverage':{},'system_routes_sha256':'a'*64,'tdm_schedule_sha256':'b'*64}
    record={'id':'s0','kind':'capture','system_path':'p0','member_path':'p0','cut_index':0,'fpga':'fpga0',
        'replace_tx_endpoint':None,'start_pin':'cell:c0/O','end_pin':'cell:c1/D'}
    with identity_facts(logic,header=header,segments=[record]):pass
    kwargs.update(logic_identity_path=logic,local_identity_path=local)


def test_native_partition_publishes_all_three_typed_timing_owners(tmp_path):
    kwargs,contract,*_=case(tmp_path);add_timing_identities(kwargs)
    result=run(kwargs,contract)
    assert result['boundary_timing']['import']['endpoints']==1
    assert result['logic_segment_timing']['import']['segments']==1
    assert result['local_path_timing']['import']['local_paths']==1
    from emuflow_tools.timing.boundary_timing_storage import read_boundary_timing_storage
    from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage
    from emuflow_tools.timing.local_path_timing_storage import read_local_path_timing_storage
    for key,reader in [('boundary_timing',read_boundary_timing_storage),('logic_segment_timing',read_logic_segment_timing_storage),('local_path_timing',read_local_path_timing_storage)]:
        output=Path(result[key]['import']['output'])
        with reader(output) as view:
            assert view['fpga']=='fpga0'
            assert view['status']=='pass'
            assert json.loads(output.read_text())['store_sha256']


@pytest.mark.parametrize('damage',['original','clock','part','budget','effective','identity','native-provider'])
def test_invalid_partition_contract_is_rejected_before_vendor_execution(tmp_path,damage):
    kwargs,contract,control,count,_=case(tmp_path)
    if damage=='original':kwargs['original_cells']=4
    elif damage=='clock':kwargs['runtime']['virtual_dut_clocks']={}
    elif damage=='part':kwargs['part']='xcvu19p-wrong'
    elif damage=='budget':kwargs['runtime']['resource_budget']['capacity']['lut']=0
    elif damage=='effective':kwargs['runtime']['resource_budget']['effective_capacity']['lut']=24
    elif damage=='identity':kwargs['fpga']='fpga1'
    else:
        path=tmp_path/'native.json';path.write_text('{}');kwargs['openparf_native_constraints']=path
    with pytest.raises(ValidationError):run(kwargs,contract)
    assert not count.exists()
    assert not (kwargs['output_dir']/'partition-report.json').exists()


@pytest.mark.parametrize('damage',['placement','route','sta','physical-count','source'])
def test_late_failure_preserves_every_accepted_partition_artifact(tmp_path,damage):
    kwargs,contract,control,count,_=case(tmp_path)
    accepted=run(kwargs,contract);report=kwargs['output_dir']/'partition-report.json';previous=report.read_bytes()
    paths=[Path(value['path']) for value in accepted['result']['artifacts'].values()]
    previous_artifacts={p:p.read_bytes() for p in paths}
    if damage=='placement':control.write_text(json.dumps({'fail':True}))
    elif damage in ('route','physical-count'):
        p=kwargs['java'];s=p.read_text()
        if damage=='route':s=s.replace("DAMAGE=''","DAMAGE='census'")
        else:s=s.replace("{'lut':1,'ff':2","{'lut':2,'ff':2")
        p.write_text(s)
    else:
        records=[['c0/O->c1/D#00000000'.encode().hex(),'clk'.encode().hex(),'10','0.5','9.5','middle'.encode().hex()]]
        _fixed_opensta(tmp_path,records,failure='exit' if damage=='sta' else None,
            replace_source=kwargs['merged_ir_path'] if damage=='source' else None)
    with pytest.raises(ValidationError):run(kwargs,contract)
    assert report.read_bytes()==previous
    for p,data in previous_artifacts.items():assert p.read_bytes()==data


def test_native_input_generation_cannot_change_while_the_solver_runs(tmp_path):
    kwargs,contract,control,*_=case(tmp_path)
    control.write_text(json.dumps({'mutate':str(kwargs['architecture_path'])}))
    with pytest.raises(ValidationError,match='generation changed'):run(kwargs,contract)
    assert not (kwargs['output_dir']/'partition-report.json').exists()


def test_host_rejects_changed_published_generation(tmp_path):
    kwargs,contract,*_=case(tmp_path)
    from emuflow_tools.physical import xilinx_physical_backend as backend
    original=backend.read_small_object
    def changed(path,*args):
        value=original(path,*args)
        if Path(path).name=='operation.json':
            report=Path(value['outputs'][0]['path']);report.write_bytes(report.read_bytes()+b'\n')
        return value
    with mock.patch.object(backend,'read_small_object',side_effect=changed),pytest.raises(ValidationError,match='output generation changed'):
        run(kwargs,contract)


def test_partition_directory_cannot_have_two_active_owners(tmp_path):
    import fcntl
    kwargs,contract,control,count,_=case(tmp_path)
    kwargs['output_dir'].mkdir()
    with (kwargs['output_dir']/'.partition-owner.lock').open('w') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValidationError,match='active owner'):run(kwargs,contract)
    assert not count.exists()


def test_retry_uses_a_new_generation_without_mutating_the_accepted_partition(tmp_path):
    kwargs,contract,*_=case(tmp_path)
    first=run(kwargs,contract)
    source=Path(first['result']['artifacts']['mapped']['path']);previous=source.read_bytes()
    second=run({**kwargs,'resume':True},contract)
    assert second['result']['artifacts']['mapped']['path']!=str(source)
    assert source.read_bytes()==previous
    assert first['result']['cell_accounting']==second['result']['cell_accounting']
    assert first['result']['timing']==second['result']['timing']


def test_retired_python_partition_coordinators_are_absent():
    from emuflow_tools.physical import xilinx_physical_backend as backend
    assert not hasattr(backend,'_finish_rapidwright_routed_backend')
    assert not hasattr(backend,'_run_rapidwright_routed_backend_tail')
    assert not hasattr(backend,'_physical_clock_periods')
    assert not hasattr(backend,'_compact_openparf_qualification')
