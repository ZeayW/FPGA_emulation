"""Native multi-FPGA control contracts; whole-design qualification uses NVDLA."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from emuflow.common.native_tools import native_file_generation
from tests.yosys_reference import import_yosys_json
from emuflow.platform.platform import Platform
from emuflow.platform.default_platform import research_platform_path
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.transport.phase6_native import run_native_phase6
from emuflow.transport.runtime import build_virtual_runtime
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from tests.native_build import emuflow_native
from tests.phase3_store_fixture import write_assignment_index_fixture
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.test_vivado_partition_native import engine

ROOT=Path(__file__).resolve().parents[1]


def setup(root, source_ir=None):
    root.mkdir(exist_ok=True)
    module={'attributes':{'top':'1'},'cells':{'ff':{'type':'FDRE','parameters':{},'attributes':{},
        'connections':{'C':[9],'D':[1],'Q':[2],'CE':['1'],'R':['0']},
        'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'}}},
        'ports':{'clk':{'direction':'input','bits':[9]},'a':{'direction':'input','bits':[1]},'q':{'direction':'output','bits':[2]}},
        'netnames':{n:{'bits':[b]} for n,b in [('clk',9),('a',1),('q',2)]}}
    raw=root/'source.json';raw.write_text(json.dumps({'creator':'fixture','modules':{'top':module}}))
    if source_ir is None:
        ir=import_yosys_json(raw,clocks=['clk']);source=root/'source.emuir.sqlite';ir.save(source)
    else:
        from emuflow.ir.ir import EmuIR
        source=source_ir;ir=EmuIR.load(source)
    board=json.loads(research_platform_path(2).read_text())
    board['platform'].pop('virtual_board_model',None);board['platform'].pop('virtual_board_wiring',None)
    for fpga in board['fpgas']:
        fpga.update(part='xcvu19p-test',capacity={r:100 for r in ('lut','ff','bram18k','dsp48','carry8','uram288')},utilization_limit=.25)
    platform=Platform.from_dict(board);board_path=root/'board.json';board_path.write_text(json.dumps(board))
    assignment={'design':ir.value['design']['name'],'platform':platform.name,'constraints':{},
        'instance_assignment':{i['id']:'fpga0' for i in ir.value['instances']},'cut_nets':[],
        'partitions':[{'fpga':f.id} for f in platform.fpgas]}
    owner=root/'assignment.sqlite';write_assignment_index_fixture(owner,ir,assignment)
    schedule={'schema':'emuflow.tdm-schedule/v1','design':ir.value['design']['name'],'platform':platform.name,
        'metrics':{'frame_slots':32,'completion_slot':0},'routes':[],'entries':[],
        'domain_schedules':[],'demand_completions':[],'bank_allocations':[]}
    write_tdm_schedule_storage(root/'schedule.json',schedule)
    with read_tdm_schedule(root/'schedule.json') as typed:
        runtime=build_virtual_runtime(typed,platform,{'clk':10.0})
        run_native_phase6(source,owner,typed,platform,board_path,root/'split',design=ir.value['design'],
            clocks=ir.value['clocks'],runtime=runtime,virtual_board=None)
    vendor=engine(root,root/'unused.sqlite')
    text=vendor.read_text().replace("print('vendor control fixture: '+script,flush=True)",
        "if script=='implement_indexed_partition.tcl':MAPPED=str(Path(args[1]).with_suffix('.mapped.sqlite'))\nprint('vendor control fixture: '+script,flush=True)")
    text=text.replace("if DAMAGE=='clock':metrics['dut_clock_count']=99",'''metrics['lut']=sum(str(kind).startswith('LUT') for _,_,kind in rows)
 metrics['ff']=sum(str(kind).startswith('FD') for _,_,kind in rows)
 if DAMAGE=='clock':metrics['dut_clock_count']=99''')
    vendor.write_text(text)
    yosys=root/'yosys'
    yosys.write_text('#!'+sys.executable+'\n'+r'''
import sys,json,re
if '--version' in sys.argv:print('Yosys 0.57');raise SystemExit(0)
top=re.search(r'hierarchy -check -top ([^; ]+)',sys.argv[-1])[1]
ports={n:{'direction':'input','bits':[i]} for i,n in enumerate(('fabric_clk','reset','links_ready','source_values'),1)}
ports.update({n:{'direction':'output','bits':[v]} for n,v in [('shadow_values','0'),('virtual_clock_enable','1'),('slot_debug','0')]})
print(json.dumps({'creator':'Yosys 0.57','modules':{top:{'attributes':{'top':'1'},'cells':{},'ports':ports,'netnames':{}}}}))
''');yosys.chmod(0o755)
    rows=[('split-manifest',root/'split/manifest.json','physical-input-document-v1'),
        ('board',board_path,'boarddb-v1'),('schedule-selector',root/'schedule.json','tdm-schedule-selector-v4'),
        ('library',DEFAULT_XILINX_PRIMITIVE_LIBRARY,'physical-input-document-v1'),
        ('yosys',yosys,'external-tool-executable-v1'),('backend.tool',vendor,'external-tool-executable-v1')]
    rows += [('backend.script.'+p.name,p,'exact-sha256-text-v1') for p in (ROOT/'scripts/vivado').glob('*.tcl')]
    return {'schema':'emuflow.physical-flow-request/v1','sources':[{'role':role,'path':str(path.resolve()),
        'schema':schema,'generation':list(native_file_generation(path))} for role,path,schema in rows],
        'backend':'vivado','backend_options':{'place_directive':'Default','route_directive':'Default','threads':2},
        'workers':2,'seed':1,'resume':False,'selected_fpgas':None}


def run(root,request):
    path=root/'request.json';path.write_text(json.dumps(request))
    proof=root/'operation.json';output=root/'physical'
    result=subprocess.run([str(emuflow_native()),'--report',str(proof),'physical','run-flow',str(path),str(output)],capture_output=True,text=True)
    status = json.loads(proof.read_text()) if proof.exists() else (json.loads(result.stderr.removeprefix('emuflow-native: ')) if result.stderr.startswith('emuflow-native: ') else None)
    return result,status,output


@pytest.mark.parametrize('workers',[1,2])
def test_native_full_flow_owns_synthesis_lowering_backend_inactive_and_assembly(tmp_path,workers):
    request=setup(tmp_path);request['workers']=workers
    result,proof,output=run(tmp_path,request)
    assert result.returncode==0,proof or result.stderr
    assert proof['stage']=='physical.run-flow' and proof['status']=='pass'
    report=json.loads((output/'multi-fpga-physical-flow-report.json').read_text())
    assert report['implementation_id']=='native-multi-fpga-physical-v1'
    assert [p['fpga'] for p in report['fpgas']]==['fpga0','fpga1']
    assert report['summary']['active_fpgas']==1 and report['summary']['inactive_fpgas']==1
    assert report['summary']['original_cells']==1 and report['summary']['transport_cells']==0
    assert report['execution']['effective_workers']==workers
    active,inactive=report['fpgas']
    assert active['stages']['vivado_implementation']['implementation_id']=='native-vivado-partition-v1'
    for record in report['fpgas']:
        lowering=record['stages']['placement_ir']
        assert lowering['implementation_id']=='native-placement-lowering-v1'
        assert not Path(lowering['output']).exists()
        assert lowering['intermediate_lifecycle']['state']=='retired'
        assert record['stages']['transport_synthesis']['intermediate_lifecycle']['state']=='retired'
    assert inactive['physical_result']['timing']['applicable'] is False
    assert 'vivado_implementation' not in inactive['stages']
    summary=json.loads((output/'physical-summary.json').read_text())
    assert set(summary['boundary_identities'])==set(summary['boundary_timing'])=={'fpga0','fpga1'}
    for group in ('boundary_identities','boundary_timing'):
        assert all((output/value['path']).is_file() for value in summary[group].values())


def test_partial_inactive_partition_does_not_run_external_engines_or_publish_whole_summary(tmp_path):
    request=setup(tmp_path);request['selected_fpgas']=['fpga1']
    # Both are valid bound executables which must never be invoked.
    for row in request['sources']:
        if row['role'] in ('yosys','backend.tool'):
            path=Path(row['path']);path.write_text('#!'+sys.executable+'\nraise SystemExit(92)\n');path.chmod(0o755)
            row['generation']=list(native_file_generation(path))
    result,proof,output=run(tmp_path,request)
    assert result.returncode==0,proof or result.stderr
    batch=json.loads((output/'physical-partition-batch.json').read_text())
    assert batch['selected_fpgas']==['fpga1'] and batch['summary']['completed_partitions']==1
    assert not (output/'physical-summary.json').exists()
    assert not (output/'multi-fpga-physical-flow-report.json').exists()


@pytest.mark.parametrize('damage',['version','unknown-field','duplicate-source','generation','selection','workers','schedule-selector','netlist-selector'])
def test_admission_failures_never_publish_physical_results(tmp_path,damage):
    request=setup(tmp_path)
    if damage=='version':request['schema']='emuflow.physical-flow-request/v2'
    elif damage=='unknown-field':request['ignored']=True
    elif damage=='duplicate-source':request['sources'].append(request['sources'][0])
    elif damage=='generation':request['sources'][0]['generation'][2]+=1
    elif damage=='selection':request['selected_fpgas']=['fpga-missing']
    elif damage=='workers':request['workers']=0
    else:
        row=next(r for r in request['sources'] if r['role']==('split-manifest' if damage=='netlist-selector' else 'schedule-selector'))
        path=Path(row['path']);value=json.loads(path.read_text())
        if damage=='netlist-selector':value['fpgas'][0]['netlist_selector']='fpga1'
        else:value['schema']='emuflow.tdm-schedule-storage/v3'
        path.write_text(json.dumps(value));row['generation']=list(native_file_generation(path))
    result,proof,output=run(tmp_path,request)
    assert result.returncode!=0 and proof['status']=='fail'
    assert not (output/'physical-summary.json').exists()
    assert not (output/'multi-fpga-physical-flow-report.json').exists()


def test_native_whole_rapidwright_flow_never_returns_to_python_stage_coordinator(tmp_path):
    from unittest import mock
    from tests.test_rapidwright_partition_native import case, run as run_partition
    kwargs, contract, *_ = case(tmp_path/'vendor-case')
    request=setup(tmp_path/'flow',kwargs['merged_ir_path'])
    captured={}
    class Capture(Exception): pass
    def intercept(executable, argv, **options):
        captured.update(json.loads(Path(argv[-2]).read_text()))
        raise Capture()
    with mock.patch('emuflow.physical.xilinx_physical_backend.NativeToolProcess',side_effect=intercept):
        with pytest.raises(Capture):run_partition(kwargs,contract)
    request['sources']=[r for r in request['sources'] if not r['role'].startswith('backend.')]
    for source in captured['sources']:
        if source['role'] not in {'ir','library','boundary','logic','local'}:
            request['sources'].append(dict(source,role='backend.'+source['role']))
    request.update(backend='rapidwright',backend_options={k:captured[k] for k in
        ('openparf_runtime','max_heap','device_data_root')})
    result,proof,output=run(tmp_path/'flow',request)
    assert result.returncode==0,proof or result.stderr
    report=json.loads((output/'multi-fpga-physical-flow-report.json').read_text())
    assert report['summary']['original_cells']==3
    assert report['fpgas'][0]['stages']['rapidwright_implementation']['implementation_id']=='native-rapidwright-partition-v1'
    assert report['fpgas'][0]['physical_result']['timing']['wns_ns']==.5
    assert report['fpgas'][1]['active'] is False
    assert not list(output.glob('**/placement.json'))
    assert not list(output.glob('**/packed-sites.json'))


@pytest.mark.parametrize("linked_paths", [False, True])
def test_full_native_controller_binds_original_paths_exact_schedule_and_both_query_populations(tmp_path, linked_paths):
    from dataclasses import replace
    from tests.logic_segment_query_fixture import sources
    from tests.phase3_store_fixture import write_current_assignment_fixture
    from emuflow.transport.transport_storage import read_transport
    from emuflow.transport.netlist import transport_endpoint_port
    value=sources(tmp_path/'timing')
    if linked_paths:
        description=json.loads(value.paths.read_text())['payloads']['paths']
        payload=value.paths.parent/description['path']
        original=tmp_path/'published-paths.sqlite'
        payload.rename(original);payload.symlink_to(original)
    request=setup(tmp_path/'host')
    platform=replace(value.platform,fpgas=tuple(replace(f,part='xcvu19p-test',capacity={r:100 for r in ('lut','ff','bram18k','dsp48','carry8','uram288')},utilization_limit=.25) for f in value.platform.fpgas))
    board=tmp_path/'query-board.json';board.write_text(json.dumps(platform.to_dict()))
    selector=value.original.parent/'assignment.json'
    write_current_assignment_fixture(selector,value.case.ir,value.assignment_value)
    with read_tdm_schedule(value.schedule) as schedule:
        runtime=build_virtual_runtime(schedule,platform,{'clk':20.})
        split=tmp_path/'query-split'
        run_native_phase6(value.original,value.assignment,schedule,platform,board,split,
            design=value.case.ir.value['design'],clocks=value.case.ir.value['clocks'],virtual_board=None,runtime=runtime)
    modules={}
    for fpga in ('fpga0','fpga1'):
        transport=read_transport(split/fpga/'transport.json').materialize()
        modules[fpga]=transport_module(transport)
    yosys=tmp_path/'query-yosys'
    yosys.write_text('#!'+sys.executable+'\nimport sys,json,re\nmodules='+repr(modules)+"\nif '--version' in sys.argv:print('Yosys 0.57');raise SystemExit(0)\ntop=re.search(r'hierarchy -check -top ([^; ]+)',sys.argv[-1])[1]\nfpga=next(f for f in modules if f in top)\nprint(json.dumps({'creator':'Yosys 0.57','modules':{top:modules[fpga]}}))\n");yosys.chmod(0o755)
    replace_sources={'split-manifest':(split/'manifest.json','physical-input-document-v1'),
        'board':(board,'boarddb-v1'),'schedule-selector':(value.schedule,'tdm-schedule-selector-v4'),
        'yosys':(yosys,'external-tool-executable-v1'),'original':(value.original,'emuir-compact-v2'),
        'assignment-selector':(selector,'physical-input-document-v1'),'route-selector':(value.routes,'system-route-selector-v1'),
        'path-selector':(value.paths,'sta-path-selector-v1'),
        'schedule-board':(value.schedule.with_name('schedule-board.bin'),'exact-schedule-board-request-v1'),
        'phase5-report':(value.schedule.with_name('phase5_report.json'),'phase5-report-v1')}
    request['sources']=[r for r in request['sources'] if r['role'] not in replace_sources]
    request['sources'] += [{'role':role,'path':str(path.resolve()),'schema':schema,'generation':list(native_file_generation(path))}
        for role,(path,schema) in replace_sources.items()]
    result,proof,output=run(tmp_path,request)
    assert result.returncode==0,proof or result.stderr
    summary=json.loads((output/'physical-summary.json').read_text())
    assert set(summary['logic_segment_timing'])==set(summary['local_path_timing'])=={'fpga0','fpga1'}
    assert summary['logic_segment_semantic_contract_sha256']==value.assignment_value['semantic_contract_sha256']


def transport_module(transport):
    from emuflow.transport.netlist import transport_endpoint_port
    bits={s['signal']:i+20 for i,s in enumerate(transport['source_signals']+transport['shadow_signals'])}
    ports={n:{'direction':'input','bits':[i+1]} for i,n in enumerate(('fabric_clk','reset','links_ready'))}
    for kind,name,direction in [('source','source_values','input'),('shadow','shadow_values','output')]:
        ports[name]={'direction':direction,'bits':[bits[s['signal']] for s in transport[kind+'_signals']] or ['0']}
    ports.update(virtual_clock_enable={'direction':'output','bits':['1']},slot_debug={'direction':'output','bits':['0']})
    cells={}
    for index,endpoint in enumerate(transport['endpoints']):
        external=100+index
        tx=endpoint['kind']=='tx'
        cells['endpoint'+str(index)]={'type':'LUT1' if tx else 'FDRE','parameters':{'INIT':'10' if tx else '0'},'attributes':{},
            'connections':{'I0':[bits[endpoint['signal']]],'O':[external]} if tx else
                {'D':[external],'Q':[bits[endpoint['signal']]],'C':[1],'CE':['1'],'R':['0']},
            'port_directions':{'I0':'input','O':'output'} if tx else {'D':'input','Q':'output','C':'input','CE':'input','R':'input'}}
        port=ports.setdefault(transport_endpoint_port(endpoint),{'direction':'output' if endpoint['kind']=='tx' else 'input','bits':[]})
        while len(port['bits'])<=endpoint['lane']:port['bits'].append('0')
        port['bits'][endpoint['lane']]=external
    return {'attributes':{'top':'1'},'cells':cells,'ports':ports,'netnames':{n:{'bits':v['bits']} for n,v in ports.items()}}


def write_flow_transport_tool(path,split):
    import inspect
    body=inspect.getsource(transport_module)
    path.write_text('#!'+sys.executable+'\nimport sys,json,re\nfrom pathlib import Path\nfrom emuflow.transport.transport_storage import read_transport\n'+body+
        "if '--version' in sys.argv or '-V' in sys.argv:print('Yosys 0.57');raise SystemExit(0)\n"+
        "top=re.search(r'hierarchy -check -top ([^; ]+)',sys.argv[-1])[1]\nfpga=re.search(r'fpga[0-9]+',top)[0]\n"+
        'source=Path('+repr(str(split))+')/fpga/"transport.json"\n'+
        "print(json.dumps({'creator':'Yosys 0.57','modules':{top:transport_module(read_transport(source).materialize())}}))\n")
    path.chmod(0o755)
    return path



def test_late_original_board_mutation_rejects_whole_flow_publication(tmp_path):
    request=setup(tmp_path)
    board=next(row for row in request['sources'] if row['role']=='board')
    vendor=next(row for row in request['sources'] if row['role']=='backend.tool')
    path=Path(vendor['path'])
    path.write_text(path.read_text()+"\nif script=='implement_indexed_partition.tcl':\n p=Path("+repr(board['path'])+");p.chmod(p.stat().st_mode ^ 0o100)\n")
    vendor['generation']=list(native_file_generation(path))
    result,proof,output=run(tmp_path,request)
    assert result.returncode!=0 and proof['status']=='fail'
    assert 'generation' in proof['error']['message']
    assert not (output/'physical-summary.json').exists()
    assert not (output/'multi-fpga-physical-flow-report.json').exists()
