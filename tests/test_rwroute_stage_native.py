"""Complete native route transaction tests with an isolated vendor process.

These exercise ownership and rejection contracts; hardware qualification uses
NVDLA and the pinned external device/tool installation.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest import mock

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation
from emuflow.physical.rapidwright.route_stage import run_native_rwroute_stage
from emuflow.physical.rapidwright.xilinx_rwroute import validate_xilinx_route_db
from tests.native_build import emuflow_native
from tests.physical_handoff_fixture import write_handoff_fixture
from tests.test_rwroute_native import write_mapped
from tests import test_xilinx_rwroute as route_fixture


def setup(root,damage=''):
    root.mkdir(exist_ok=True)
    mapped=root/'mapped.sqlite';handoff=root/'physical.sqlite'
    cells={}
    for i in range(3):
        cells['c'+str(i)]={'type':'LUT1','parameters':{'INIT':'10'},'attributes':{},
            'connections':{'I0':[1 if i==0 else 2],'O':[2+i]},
            'port_directions':{'I0':'input','O':'output'}}
    write_mapped(mapped,{'modules':{'top':{'cells':cells,'ports':{'in':{'direction':'input','bits':[1]}},'netnames':{}}}})
    packed={'schema':'emuflow.packed-site-netlist/v1','status':'pass','top':'top','mapping_profile':'fixture',
            'source':{},'policy':{},'clusters':[],'cascade_chains':[],'unplaced_constants':[],'summary':{}}
    placement={'schema':'emuflow.xilinx-placement/v1','status':'pass','part':'xcvu19p-test',
               'provider':'fixture','source':{},'policy':{},'clusters':[],'summary':{}}
    for i in range(3):
        a={'instance':'c'+str(i),'cell_type':'LUT1','bel':'A6LUT'}
        packed['clusters'].append({'id':str(i),'kind':'slice','site_templates':['SLICEL'],'control_set':None,'assignments':[a]})
        placement['clusters'].append({'cluster':str(i),'site':'S'+str(i),'site_type':'SLICEL','x':0,'y':i,
            'assignments':[{**a,'placement_mode':'fixed','site':'S'+str(i)}]})
    write_handoff_fixture(handoff,packed,placement)
    value=route_fixture.XilinxRWRouteTest()._route()
    contract={'schema':'emuflow.rapidwright-timing-contract/v1','provider':value['provider'],'timing':copy.deepcopy(value['timing'])}
    for field in ('logic_coefficients_ps','routed_endpoints','maximum_route_delay_ps'):contract['timing'].pop(field)
    for relative in contract['timing']['device_data_md5']:
        p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'vendor fixture: '+relative.encode())
        contract['timing']['device_data_md5'][relative]=hashlib.md5(p.read_bytes()).hexdigest()
    for name in contract['timing']['source_data_sha256']:
        p=root/name;p.write_text('fixture '+name)
        contract['timing']['source_data_sha256'][name]=hashlib.sha256(p.read_bytes()).hexdigest()
    value['timing'].update(contract['timing'])
    contract_path=root/'timing-contract.json';contract_path.write_text(json.dumps(contract))
    fixture=root/'expected-route.json';fixture.write_text(json.dumps(value))
    jar=root/'vendor.jar';jar.write_text('fixture jar')
    java_source=root/'EmuFlowRWRoute.java';java_source.write_text('fixture source')
    script='#!'+sys.executable+'\n'+r'''
import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
DAMAGE=__DAMAGE__;ROOT=Path(__ROOT__)
a=sys.argv
if Path(a[0]).name=='javac':
 print('compile vendor adapter',flush=True)
 if DAMAGE=='compiler':raise SystemExit(4)
 (Path(a[a.index('-d')+1])/'EmuFlowRWRoute.class').write_bytes(b'compiled fixture')
 raise SystemExit(0)
print('route vendor fixture',flush=True)
args=a[a.index('EmuFlowRWRoute')+1:];source,out=map(Path,args[:2])
(ROOT/'observed.json').write_text(json.dumps({'args':a,'env':os.environ.get('RAPIDWRIGHT_PATH')}))
if DAMAGE=='hang':
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
 (ROOT/'processes.json').write_text(json.dumps([os.getpid(),child.pid]))
 time.sleep(60)
if DAMAGE=='exit':
 print('failure sentinel before long log',flush=True)
 for _ in range(15000):print('ordinary diagnostic '*16)
 raise SystemExit(7)
if DAMAGE=='source':source.write_text('changed while routing')
v=json.loads((ROOT/'expected-route.json').read_text())
v['source'].update(mapped_sha256=hashlib.sha256((ROOT/'mapped.sqlite').read_bytes()).hexdigest(),
 packed_sha256=hashlib.sha256((ROOT/'physical.sqlite').read_bytes()).hexdigest(),
 placement_sha256=hashlib.sha256((ROOT/'physical.sqlite').read_bytes()).hexdigest(),
 rwroute_input_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
v['schema']='emuflow.xilinx-route-db/v2';nets=v.pop('nets');net_lines=[];pip_lines=[]
for n in nets:
 pips=n.pop('pips');n['pip_count']=len(pips);net_lines.append(json.dumps(n))
 for p in pips:pip_lines.append(json.dumps([n['net'],*[p[k] for k in ('tile','start_wire','end_wire','start_node','end_node')],False,False]))
v['payloads']={}
for name,lines,fmt in (('nets',net_lines,'jsonl-object/v1'),('pips',pip_lines,'jsonl-array/v1')):
 p=out.with_name(out.name+'.'+name+'.jsonl');p.write_text('\n'.join(lines)+'\n')
 v['payloads'][name]={'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'records':len(lines),'format':fmt}
if DAMAGE=='gap':
 n=json.loads(net_lines[0]);n['has_gap']=True;p=out.with_name(out.name+'.nets.jsonl');p.write_text(json.dumps(n)+'\n')
 v['payloads']['nets']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
if DAMAGE=='payload':out.with_name(out.name+'.pips.jsonl').write_text('corrupt\n')
if DAMAGE=='census':v['cells']+=1
if DAMAGE=='input-seal':v['source']['rwroute_input_sha256']='a'*64
if DAMAGE=='timing':v['timing']['source_revision']='0'*40
out.write_text(json.dumps(v))
'''.replace('__ROOT__',repr(str(root))).replace('__DAMAGE__',repr(damage))
    for name in ('java','javac'):
        p=root/name;p.write_text(script);p.chmod(0o755)
    options=dict(rapidwright_jar=jar,java=root/'java',java_source=java_source,
        device_data_root=root,timing_data_dir=root,mapped_path=mapped,handoff_path=handoff,
        placement_path=handoff,export_input=True,resource_limits={'lut':100,'ff':100})
    return options,contract_path


def run(root,options,contract):
    with mock.patch('emuflow.physical.rapidwright.xilinx_rwroute.RAPIDWRIGHT_TIMING_CONTRACT',contract):
        return run_native_rwroute_stage(None,root/'route.json',**options)


def test_complete_route_owner_exports_routes_and_independently_validates(tmp_path):
    options,contract=setup(tmp_path)
    result=run(tmp_path,options,contract)
    assert result['implementation_id']=='native-rwroute-stage-v1'
    assert result['route']['nets']==1 and result['route']['pips']==3
    assert result['export']['logical_cells']==3 and result['export']['physical_cells']==3
    assert result['route']['source_generations']['route']==list(native_file_generation(tmp_path/'route.json'))
    assert result['source_sha256']['mapped_sha256']==hashlib.sha256((tmp_path/'mapped.sqlite').read_bytes()).hexdigest()
    observation=json.loads((tmp_path/'observed.json').read_text())
    assert '-Xmx32g' in observation['args']
    runtime=Path(observation['env']);assert (runtime/'data/parts.db').is_file()
    for kind in ('nets','pips'):
        assert (tmp_path/result['manifest']['payloads'][kind]['path']).is_file()
    with mock.patch('emuflow.physical.rapidwright.xilinx_rwroute.RAPIDWRIGHT_TIMING_CONTRACT',contract):
        independent=validate_xilinx_route_db(tmp_path/'route.json',rwroute_input_path=Path(result['input']),
            mapped_path=options['mapped_path'],packed_path=options['handoff_path'],placement_path=options['handoff_path'])
    assert independent['route_sha256']==result['route']['route_sha256']


@pytest.mark.parametrize('damage,reason',[
    ('compiler','tool failed'),('exit','tool failed'),('gap','gap routing'),('payload','JSON'),
    ('census','route-cell count'),('input-seal','routed input'),('timing','timing'),('source','generation changed'),
])
def test_failed_route_never_replaces_accepted_manifest(tmp_path,damage,reason):
    options,contract=setup(tmp_path)
    accepted=run(tmp_path,options,contract);before=(tmp_path/'route.json').read_bytes()
    for name in ('java','javac'):
        p=tmp_path/name;p.write_text(p.read_text().replace("DAMAGE=''","DAMAGE="+repr(damage)))
    with pytest.raises(ValidationError,match=reason):run(tmp_path,options,contract)
    assert (tmp_path/'route.json').read_bytes()==before
    for kind in ('nets','pips'):
        path=tmp_path/accepted['manifest']['payloads'][kind]['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==accepted['manifest']['payloads'][kind]['sha256']
    if damage=='exit':
        logs=list(tmp_path.glob('.rwroute-generation.*/rwroute.log'))
        assert any(p.stat().st_size>2*1024*1024 and 'failure sentinel before long log' in p.read_text() for p in logs)


@pytest.mark.parametrize('role',['data/parts.db','data/devices/virtexuplus/xcvu19p_db.dat',
                                 'intersite_delay_terms.txt','intrasite_delay_terms.txt'])
def test_pinned_data_rejected_before_java(tmp_path,role):
    options,contract=setup(tmp_path);(tmp_path/role).write_bytes(b'changed provider')
    with pytest.raises(ValidationError,match='pinned'):run(tmp_path,options,contract)
    assert not (tmp_path/'observed.json').exists()


def test_native_resume_revalidates_without_compilation_or_router(tmp_path):
    options,contract=setup(tmp_path);first=run(tmp_path,options,contract)
    before=(tmp_path/'observed.json').stat().st_mtime_ns
    second=run(tmp_path,{**options,'resume':True},contract)
    assert second['route']['reused'] is True
    assert second['engine']=={'compilation':None,'routing':None}
    assert (tmp_path/'observed.json').stat().st_mtime_ns==before
    assert second['route']['route_sha256']==first['route']['route_sha256']
    manifest=json.loads((tmp_path/'route.json').read_text());manifest['source']['mapped_sha256']='0'*64
    (tmp_path/'route.json').write_text(json.dumps(manifest))
    with pytest.raises(ValidationError,match='does not match input'):
        run(tmp_path,{**options,'resume':True},contract)
    assert (tmp_path/'observed.json').stat().st_mtime_ns==before


def test_wrong_heap_rejected_before_tools(tmp_path):
    options,contract=setup(tmp_path)
    with mock.patch.dict(os.environ,{'EMUFLOW_RAPIDWRIGHT_MAX_HEAP':'0g'}),pytest.raises(ValidationError,match='heap'):
        run(tmp_path,options,contract)
    assert not (tmp_path/'observed.json').exists()


def request_file(root,options,contract):
    def source(path):
        path=Path(path).resolve()
        return {'path':str(path),'generation':list(native_file_generation(path))}
    sources={role:source(path) for role,path in (
        ('java',options['java']),('javac',root/'javac'),('jar',options['rapidwright_jar']),
        ('java_source',options['java_source']),('contract',contract),('parts',root/'data/parts.db'),
        ('device',root/'data/devices/virtexuplus/xcvu19p_db.dat'),
        ('intersite',root/'intersite_delay_terms.txt'),('intrasite',root/'intrasite_delay_terms.txt'),
        ('mapped',options['mapped_path']),('packed',options['handoff_path']),('placement',options['handoff_path']))}
    config={'schema':'emuflow.rwroute-stage-request/v1','sources':sources,'output_path':str(root/'route.json'),
        'max_heap':'32g','device_data_root':str(root),'virtual_io':None,'virtual_io_sha256':None,'resource_limits':{'lut':100,'ff':100},
        'export_input':True,'resume':False,'timeout_ms':21600000}
    path=root/'request.json';path.write_text(json.dumps(config,separators=(',',':')))
    return path


def test_native_source_generation_checked_before_tool_launch(tmp_path):
    options,contract=setup(tmp_path);request=request_file(tmp_path,options,contract)
    options['mapped_path'].write_bytes(options['mapped_path'].read_bytes()+b'changed')
    process=subprocess.run([str(emuflow_native()),'physical','run-rwroute',str(request),str(tmp_path/'report.json')],
                           capture_output=True,text=True)
    assert process.returncode and 'source generation changed: mapped' in process.stderr
    assert not (tmp_path/'observed.json').exists() and not (tmp_path/'report.json').exists()


def test_native_cancellation_reaps_java_group_and_rejects_concurrent_writer(tmp_path):
    options,contract=setup(tmp_path,'hang');request=request_file(tmp_path,options,contract)
    command=[str(emuflow_native()),'physical','run-rwroute',str(request),str(tmp_path/'report.json')]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+15
        while not (tmp_path/'processes.json').is_file() and process.poll() is None and time.monotonic()<deadline:
            time.sleep(.03)
        assert (tmp_path/'processes.json').is_file(),process.communicate(timeout=2)
        pids=json.loads((tmp_path/'processes.json').read_text())
        duplicate=subprocess.run(command,capture_output=True,text=True)
        assert duplicate.returncode and 'active owner' in duplicate.stderr
        process.send_signal(signal.SIGTERM)
        _,stderr=process.communicate(timeout=10)
        assert process.returncode and 'cancel' in stderr
        assert not (tmp_path/'route.json').exists() and not (tmp_path/'report.json').exists()
        for pid in pids:
            observed=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
            assert not observed.stdout.strip() or observed.stdout.strip().startswith('Z')
    finally:
        if process.poll() is None:
            process.kill();process.communicate(timeout=5)
