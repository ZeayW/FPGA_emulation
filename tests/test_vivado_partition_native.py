"""Complete native Vivado partition transaction using an external engine double.

These qualify process/control contracts; NVDLA vendor closure is a separate gate.
"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.ir.emuir_fixture import write_compact_emuir
from emuflow_tools.ir.indexed_physical_ir import IndexedPhysicalIR
from emuflow_tools.physical.vivado.vivado_netlist import emit_vivado_mapped_verilog
from emuflow_tools.physical.vivado.vivado_physical import run_indexed_vivado_partition
from emuflow.physical.physical_backend import validate_physical_partition_result
from tests.test_vivado_indexed_physical import fixture
from tests.test_vivado_backend import RUNTIME


def engine(root, mapped, damage=''):
    path = root/'vendor'
    path.write_text('#!'+sys.executable+'\n'+r'''
import json,sqlite3,sys,signal,os,subprocess,time
from pathlib import Path
DAMAGE=__DAMAGE__
MAPPED=__MAPPED__
a=sys.argv;script=Path(a[a.index('-source')+1]).name;args=a[a.index('-tclargs')+1:]
print('vendor control fixture: '+script,flush=True)
if DAMAGE=='hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
 (Path(args[4])/'vendor-processes.json').write_text(json.dumps([os.getpid(),child.pid]))
 time.sleep(60)
if DAMAGE=='source':Path(MAPPED).write_bytes(Path(MAPPED).read_bytes()+b'changed')

if DAMAGE=='critical' or DAMAGE=='early-warning':
 print('CRITICAL WARNING: ignored constraint',file=sys.stderr,flush=True)
 if DAMAGE=='early-warning':
  for _ in range(12000): print('ordinary diagnostics '*16)
if DAMAGE=='exit':raise SystemExit(7)
if script=='implement_indexed_partition.tcl':
 part,rtl,top,xdc,out,cells,prefix,adapter,wrapper,io_xdc,io_query,place,route,clocks,threads=args
 root=Path(out)
 with sqlite3.connect(MAPPED) as db:rows=db.execute('SELECT seq,id,type FROM cells ORDER BY seq').fetchall()
 inventory='name\tref_name\tsource_origin\ttransformations_hex\tsite\tbel\ttile_column\ttile_row\tprimitive_level\n'
 for i,(seq,name,kind) in enumerate(rows):
  inventory+='\t'.join([name,kind,'s'+str(seq if DAMAGE!='lineage' else 999),'','SLICE_X'+str(i)+'Y0','AFF',str(i),'0','LEAF'])+'\n'
 for name in ('mapped_cells.tsv','routed_mapped_cells.tsv'):(root/name).write_text(inventory)
 metrics=dict(part=part,dut_clock_count=clocks,routed_cells=len(rows),physical_cells=len(rows),
  infrastructure_cells=0,optimization_cells=0,wns_ns=0.5,unrouted_nets=0,drc_violations=0,drc_warnings=0,
  lut=0,ff=len(rows),carry8=0,dsp48=0,uram288=0,ramb18_cells=0,ramb36_cells=0,fabric_period_ns=4.,
  dut_wns_ns=90.,fabric_wns_ns=0.5,fabric_to_dut_wns_ns=2.,critical_path_ns=10.,dut_delay_ns=1.,
  fabric_delay_ns=2.,fabric_to_dut_delay_ns=3.,vivado_version='fixture-vendor',virtual_io_pairs=1 if io_query else 0)
 if DAMAGE=='clock':metrics['dut_clock_count']=99
 if DAMAGE=='census':metrics['routed_cells']+=1
 if DAMAGE=='closure':metrics['drc_violations']=1
 wns,tns,failed,total=.5,0.,0,10
 if DAMAGE=='negative-timing':wns,tns,failed=-.125,-.25,2;metrics['wns_ns']=wns
 if DAMAGE=='aggregate':failed,total=11,10
 if DAMAGE=='aggregate-wns':metrics['wns_ns']=2.
 if DAMAGE=='nonfinite':metrics['critical_path_ns']='NaN'
 (root/'implementation_metrics.tsv').write_text('metric\tvalue\n'+''.join(str(k)+'\t'+str(v)+'\n' for k,v in metrics.items()))
 (root/'timing_summary.rpt').write_text('WNS(ns) TNS(ns) failing total hold a b c\n----- ----- ----- ----- ----- ----- ----- -----\n'+f'{wns} {tns} {failed} {total} 0.1 0.0 0 10\n')
 (root/'route_status.rpt').write_text('Unrouted Nets : 0\nPartially Routed Nets : '+('1' if DAMAGE=='route' else '0')+'\n')
 for name in ('synthesized.dcp','placed.dcp','routed.dcp','drc.rpt','utilization.rpt'):
  if DAMAGE!='missing-dcp' or name!='placed.dcp':(root/name).write_text('fixture '+name+'\n')
else:
 checkpoint,query,measurement,*rest=args
 rows=Path(query).read_text().splitlines()
 header='endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex'
 boundary=script=='export_boundary_timing.tcl'
 if not boundary:header+='\tmeasurement\tactual_start_object_hex\tactual_end_object_hex'
 output=[header]
 for line in rows[1:]:
  f=line.split('\t')
  if boundary:record=[f[0],f[1],'1.25','start'.encode().hex(),'end'.encode().hex()]
  else:record=[f[0],f[1],'1.25',f[3],f[5],'endpoint-exact',f[8].split(',')[0] or f[3],f[9].split(',')[0] or f[5]]
  output.append('\t'.join(record))
 if DAMAGE=='measurement':output=output[:1]
 Path(measurement).write_text('\n'.join(output)+'\n')
'''.replace('__DAMAGE__',repr(damage)).replace('__MAPPED__',repr(str(mapped))))
    path.chmod(0o755)
    return path


def case(root, damage='',timing=False):
    ir=fixture(root)
    if timing:
        # The physical namespace is the real lowering contract, not a new design.
        with sqlite3.connect(ir) as db:
            raw=json.loads(db.execute("SELECT value FROM metadata WHERE key='design_json'").fetchone()[0])
            raw['name']='dut__fpga0'
            db.execute("UPDATE metadata SET value=? WHERE key='design_json'",(json.dumps(raw),))
    mapped=emit_vivado_mapped_verilog(ir,root/'partition.v')
    kwargs=dict(fpga='fpga0',part='xcvu19p-fsva3824-2-e',ir_path=ir,mapped_verilog_path=root/'partition.v',
        mapped_report=mapped,runtime=copy.deepcopy(RUNTIME),original_cells=2,transport_cells=0,
        output_dir=root/'physical',executable=str(engine(root,Path(mapped['mapped_index']['path']),damage)))
    return kwargs


def identities(root):
    from emuflow_tools.timing.boundary_identity_storage import write_boundary_identity_storage
    identity=root/'boundary.json'
    header={'schema':'emuflow.boundary-identity/v1','status':'pass','design':'dut','platform':'board','fpga':'fpga0','provider':'fixture'}
    row={'id':'tx','kind':'tx','schedule_entry':'s0','demand':'d0','logical_net':'middle','link':'link','peer':'fpga1',
         'lane':0,'logical_lane':0,'internal_signal':'signal','source_class':'dut-net',
         'merged_ir':{'external_port':'q','external_port_bit':0,'external_net':'q','logical_net':'middle','boundary_register_instances':[]}}
    with write_boundary_identity_storage(identity,header,[row]):pass
    from tests.test_local_path_timing_storage import _header,_record
    from tests.timing_facts import local_timing_facts
    from tests.logic_identity_fixture import identity_facts
    local=root/'local.json';local_header=_header();local_header['qualification']='typed-local-path-query-identity-owner-v1'
    local_header['coverage']={'local_paths':1};local_header['source']['original_paths']=1
    local_row=_record('path0',0.);local_row.update(start_pin='launch/Q',end_pin='capture/D',measurement='endpoint-longest-path-fallback',path_pins=[])
    with local_timing_facts(local,local_header,[local_row]):pass
    logic=root/'logic.json'
    logic_header={'schema':'emuflow.logic-segment-identity/v1','status':'pass','design':'dut','platform':'board','fpga':'fpga0',
        'provider':'fixture','coverage':{},'system_routes_sha256':'a'*64,'tdm_schedule_sha256':'b'*64,'semantic_contract_sha256':'c'*64}
    segment={'id':'segment0','kind':'launch','system_path':'path0','member_path':'member0','cut_index':0,'fpga':'fpga0',
             'replace_tx_endpoint':'tx','start_pin':'launch/Q','end_pin':'capture/D','static_exact_segment_id':'segment0'}
    with identity_facts(logic,header=logic_header,segments=[segment]):pass
    header='endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex\n'
    for path,identity,kind in ((root/'local.tsv','path0','local'),(root/'logic.tsv','segment0','launch')):
        path.write_text(header+'\t'.join([identity.encode().hex(),kind,'pin','launch/Q'.encode().hex(),'pin','capture/D'.encode().hex(),'',''])+'\n')
    return dict(boundary_identity_path=root/'boundary.json',local_identity_path=local,
                local_query_path=root/'local.tsv',logic_identity_path=logic,logic_query_path=root/'logic.tsv')


def test_native_partition_owns_implementation_census_and_final_report(tmp_path):
    kwargs=case(tmp_path)
    with patch('emuflow.ir.ir.EmuIR.load',side_effect=AssertionError('Python graph')), \
         patch('csv.reader',side_effect=AssertionError('Python inventory')):
        report=run_indexed_vivado_partition(**kwargs)
    assert report['implementation_id']=='native-vivado-partition-v1'
    assert report['validation']==validate_physical_partition_result(report['result'],backend='vivado',fpga='fpga0',
        part=kwargs['part'],original_cells=2,transport_cells=0)
    assert report['cell_coverage']['stages']['routed']['placed_objects']==2
    assert not (kwargs['output_dir']/'mapped_cells.tsv').exists()
    assert not list(kwargs['output_dir'].glob('.vivado-partition-*'))
    assert Path(report['implementation']['log']).is_file()


def test_native_partition_imports_all_three_timing_populations(tmp_path):
    kwargs=case(tmp_path,timing=True);kwargs.update(identities(tmp_path))
    report=run_indexed_vivado_partition(**kwargs)
    assert report['boundary_timing']['import']['endpoints']==1
    assert report['logic_segment_timing']['import']['segments']==1
    assert report['local_path_timing']['import']['local_paths']==1
    from emuflow_tools.timing.boundary_timing_storage import read_boundary_timing_storage
    from emuflow_tools.timing.logic_segment_timing_storage import read_logic_segment_timing_storage
    from emuflow_tools.timing.local_path_timing_storage import read_local_path_timing_storage
    for field,reader,records in [('boundary_timing',read_boundary_timing_storage,'endpoints'),
        ('logic_segment_timing',read_logic_segment_timing_storage,'segments'),('local_path_timing',read_local_path_timing_storage,'paths')]:
        with reader(Path(report[field]['import']['output'])) as view:
            values=list(view[records]);assert len(values)==1;assert values[0]['delay_ns']==1.25


@pytest.mark.parametrize('damage',['critical','early-warning','exit','clock','census','closure','aggregate','aggregate-wns',
    'nonfinite','lineage','route','missing-dcp','budget','measurement','source'])
def test_native_partition_rejects_bad_vendor_evidence_without_pass_report(tmp_path,damage):
    kwargs=case(tmp_path,damage,timing=damage=='measurement')
    if damage=='measurement':kwargs.update(identities(tmp_path))
    if damage=='budget':kwargs['runtime']['resource_budget']={'id':'fpga0','part':kwargs['part'],'capacity':{'ff':4},'utilization_limit':.25}
    with pytest.raises(ValidationError):run_indexed_vivado_partition(**kwargs)
    assert not (kwargs['output_dir']/'vivado-partition-report.json').exists()
    if damage in {'critical','early-warning','exit'}:
        assert (kwargs['output_dir']/'vivado-implementation.log').exists()
        if damage=='early-warning':assert (kwargs['output_dir']/'vivado-implementation.log').stat().st_size>2*1024*1024


def test_native_partition_preserves_real_negative_endpoint_aggregate(tmp_path):
    report=run_indexed_vivado_partition(**case(tmp_path,'negative-timing'))
    assert report['result']['timing']['tns_ns']==-.25
    assert report['result']['timing']['failing_endpoints']==2
    assert report['result']['timing']['timing_met'] is False


def captured_request(kwargs):
    """Capture the public bridge's actual envelope without starting a tool."""
    from types import SimpleNamespace
    captured = {}
    class Capture:
        def __init__(self, native, command, **unused):
            captured.update(json.loads(Path(command[-2]).read_text()))
        def __enter__(self):return self
        def __exit__(self,*unused):pass
        def finish(self):return SimpleNamespace(returncode=1,stdout='captured request')
    with patch('emuflow_tools.physical.vivado.vivado_physical.NativeToolProcess',Capture), pytest.raises(ValidationError,match='captured request'):
        run_indexed_vivado_partition(**kwargs)
    return captured


def test_native_partition_cancellation_reaps_vendor_and_helpers(tmp_path):
    import os,signal,subprocess,time
    from tests.native_build import emuflow_native
    kwargs=case(tmp_path,'hang');request=tmp_path/'request.json'
    request.write_text(json.dumps(captured_request(kwargs),sort_keys=True,separators=(',',':')))
    process=subprocess.Popen([str(emuflow_native()),'physical','run-vivado-partition',str(request),str(kwargs['output_dir'])],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    pids=[]
    try:
        witness=kwargs['output_dir']/'vendor-processes.json'
        limit=time.monotonic()+10
        while not witness.exists() and process.poll() is None and time.monotonic()<limit:time.sleep(.02)
        assert witness.exists()
        pids=json.loads(witness.read_text());process.send_signal(signal.SIGTERM)
        process.communicate(timeout=6)
        assert process.returncode != 0
        for pid in pids:
            state=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True).stdout.strip()
            assert not state or state.startswith('Z'),(pid,state)
        assert not (kwargs['output_dir']/'vivado-partition-report.json').exists()
    finally:
        if process.poll() is None:process.kill();process.wait()
        if pids:
            try:os.killpg(pids[0],signal.SIGKILL)
            except ProcessLookupError:pass


@pytest.mark.parametrize('field',['rtl_sha256','functions_sha256','mapped_sha256'])
def test_native_partition_rejects_changed_source_seals_before_vendor_launch(tmp_path,field):
    import subprocess
    from tests.native_build import emuflow_native
    kwargs=case(tmp_path);config=captured_request(kwargs);config['mapped_report'][field]='0'*64
    request=tmp_path/'request.json';request.write_text(json.dumps(config,sort_keys=True,separators=(',',':')))
    command=subprocess.run([str(emuflow_native()),'physical','run-vivado-partition',str(request),str(kwargs['output_dir'])],capture_output=True,text=True)
    assert command.returncode != 0
    assert not kwargs['output_dir'].exists()


def test_native_partition_preserves_fixed_io_identity_and_canonical_seal(tmp_path):
    import hashlib
    from tests.test_vivado_inputs_native import fixed_contract
    kwargs=case(tmp_path);contract=fixed_contract(tmp_path)
    contract['entries'][0]['p']['port']='link_雪_p'
    kwargs['virtual_io_contract']=contract
    report=run_indexed_vivado_partition(**kwargs)
    io=report['virtual_io_implementation']
    assert io['contract_sha256']==hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert io['differential_pairs']==1 and io['physical_ports']==2
    assert io['top']['top']=='__emuflow_physical_top'


def test_native_partition_replay_uses_fresh_generation_and_preserves_prior_on_failure(tmp_path):
    kwargs=case(tmp_path)
    original=run_indexed_vivado_partition(**kwargs)
    report_path=kwargs['output_dir']/'vivado-partition-report.json'
    before=report_path.read_bytes()
    checkpoint=Path(original['result']['artifacts']['routed.dcp']['path'])
    checkpoint_bytes=checkpoint.read_bytes()
    # A failed retry cannot replace the accepted report or its checkpoint.
    kwargs['executable']=str(engine(tmp_path,Path(kwargs['mapped_report']['mapped_index']['path']),'exit'))
    with pytest.raises(ValidationError):run_indexed_vivado_partition(**kwargs)
    assert report_path.read_bytes()==before and checkpoint.read_bytes()==checkpoint_bytes
    kwargs['executable']=str(engine(tmp_path,Path(kwargs['mapped_report']['mapped_index']['path'])))
    replay=run_indexed_vivado_partition(**kwargs)
    assert replay['result']['artifacts']['routed.dcp']['path'] != str(checkpoint)
    assert checkpoint.read_bytes()==checkpoint_bytes
    assert replay['validation']==original['validation']


def test_native_partition_rejects_concurrent_output_owner_before_tool_launch(tmp_path):
    import fcntl
    kwargs=case(tmp_path);kwargs['output_dir'].mkdir()
    with (kwargs['output_dir']/'.partition-owner.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValidationError,match='active owner'):run_indexed_vivado_partition(**kwargs)
    assert not (kwargs['output_dir']/'vivado-implementation.log').exists()
