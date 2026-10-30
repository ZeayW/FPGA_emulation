"""Whole native RTL stage contracts; fixtures do not qualify NVDLA."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from emuflow_tools.frontend.frontend_native import prepare_frontend, read_frontend_summary
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import native_file_generation
from emuflow.platform.platform import Platform
from tests.frontend_report_reference import indexed_phase1_facts, analyze_indexed_clock_topology
from emuflow.physical.xilinx_primitives import DEFAULT_XILINX_PRIMITIVE_LIBRARY
from tests.native_build import emuflow_native
from tests.test_yosys_emuir_native import old_source_to_emuir
from emuflow.ir.ir import EmuIR

ROOT=Path(__file__).resolve().parents[1]
PLATFORM=ROOT/'platforms/virtual/xcvu3p_2fpga_p2p.json'


@pytest.fixture(scope='module')
def native():
    return str(emuflow_native())


def source(gated=False,fanout=1):
    cells={f'f{i}':{'type':'FDRE','parameters':{'INIT':'0'},'connections':{'C':[5 if gated else 2],'D':[3],'Q':[10+i],'CE':['1'],'R':['0']},'port_directions':{'C':'input','D':'input','Q':'output','CE':'input','R':'input'}} for i in range(fanout)}
    if gated:cells['gate']={'type':'LUT1','parameters':{'INIT':'10'},'connections':{'I0':[2],'O':[5]},'port_directions':{'I0':'input','O':'output'}}
    return {'creator':'fixture','modules':{'top':{'ports':{'clk':{'direction':'input','bits':[2]},'d':{'direction':'input','bits':[3]}},'cells':cells,'netnames':{'clk':{'bits':[2]},'gated':{'bits':[5]}}}}}


def request(tmp_path,value=None,*,engine='',strict=False,timeout=0,output=None,executable=None):
    rtl=tmp_path/'rtl.v';rtl.write_text('module top; endmodule')
    program=tmp_path/'engine.py';program.write_text('import sys,os,time\n'+engine+'\n'+'sys.stdout.write('+repr(json.dumps(value or source()))+')\nsys.stdout.flush()\n')
    library=DEFAULT_XILINX_PRIMITIVE_LIBRARY.resolve();root=output or tmp_path/'frontend/phase1'
    root.parent.mkdir(parents=True,exist_ok=True)
    path=tmp_path/'request.bin';writer=CheckedRequestWriter(path)
    try:
        writer.put('QI',0x31544E52464D5545,1);writer.text(str(root));writer.text('top');writer.catalog(('clk',))
        writer.text(json.dumps(Platform.load(PLATFORM).to_dict()));writer.text(json.dumps({'provider':'yosys','tool_report':{}}));writer.put('I',int(strict))
        writer.catalog((executable or sys.executable,str(program)));writer.text(str(tmp_path));writer.put('Q',timeout)
        writer.text(str(library));writer.put('3Q4q',*native_file_generation(library))
        writer.put('I',1);writer.text(str(rtl));writer.put('3Q4q',*native_file_generation(rtl));writer.finish()
    finally:writer.close()
    return path,root,program,rtl


def run(native,path,*,expected=None,timeout=20):
    command=[native,'--report',str(path.parent/'operation.json'),'phase1','run',str(path)]
    if expected is not None:command.append(str(expected))
    return subprocess.run(command,capture_output=True,text=True,timeout=timeout)


@pytest.mark.parametrize('gated',[False,True])
def test_complete_native_frontend_matches_source_and_independent_phase1_audits(native,tmp_path,gated):
    value=source(gated,17);path,root,_,_=request(tmp_path,value)
    result=run(native,path);assert result.returncode==0,result.stderr
    actual=root/'design.emuir.sqlite';expected=tmp_path/'expected.emuir.sqlite'
    old_source_to_emuir(value,expected,clocks=('clk',),normalize=True)
    a,b=EmuIR.load(actual).to_dict(),EmuIR.load(expected).to_dict()
    a['design']['source']=b['design']['source']
    assert a==b
    report=json.loads((root/'phase1_report.json').read_text())
    name,warnings,totals,classes=indexed_phase1_facts(actual)
    assert report['design']==name and report['warnings']==warnings
    assert report['resource_totals']==totals.to_dict(include_zeros=False)
    assert report['cut_classes']==dict(classes)
    assert report['clock_topology']==analyze_indexed_clock_topology(actual)
    assert report['fits_on_platform'] is True
    summary=read_frontend_summary(root/'frontend-summary.json',native_file_generation(root/'frontend-summary.json'))
    assert set(summary)=={'schema','phase','status','design','platform','synthesis'}
    assert json.loads((tmp_path/'operation.json').read_text())['stage']=='phase1.run'
    assert set(p.name for p in root.iterdir())=={'design.emuir.sqlite','frontend-summary.json','phase1_report.json','platform.normalized.json','yosys-execution.json'}
    assert not list(root.parent.glob('.frontend-generation-*'))


@pytest.mark.parametrize('failure',['exit-after-output','invalid-stream','source-change','strict-clock','timeout','crc','escape','existing'])
def test_failed_whole_stage_never_publishes_an_emuir_or_report(native,tmp_path,failure):
    path,root,program,rtl=request(tmp_path,source(True),strict=failure=='strict-clock',timeout=100 if failure=='timeout' else 0)
    text=program.read_text()
    if failure=='exit-after-output':text+='raise SystemExit(7)\n'
    elif failure=='invalid-stream':text='print("{bad")\n'
    elif failure=='source-change':text+='from pathlib import Path\nPath('+repr(str(rtl))+').write_text("changed")\n'
    elif failure=='timeout':text+='os.close(1)\ntime.sleep(60)\n'
    program.write_text(text)
    if failure=='crc':path.write_bytes(path.read_bytes()[:-1])
    if failure=='existing':root.mkdir();(root/'previous').write_text('kept')
    result=run(native,path,expected=tmp_path/'elsewhere/phase1' if failure=='escape' else None)
    assert result.returncode!=0
    if failure=='existing':assert (root/'previous').read_text()=='kept'
    else:assert not root.exists()
    assert not list(root.parent.glob('.frontend-generation-*'))


def test_pipe_backpressure_and_noisy_stderr_are_serviced_together(native,tmp_path):
    path,root,_,_=request(tmp_path,source(fanout=18000),engine='sys.stderr.write("noise"*1000000);sys.stderr.flush()',timeout=120000)
    # NFS publication/import is part of this whole-stage fixture. Keep a real
    # engine deadline and give the supervisor time to reap before outer timeout.
    result=run(native,path,timeout=180);assert result.returncode==0,result.stderr
    report=json.loads((root/'phase1_report.json').read_text())
    assert report['clock_topology']['maximum_fabric_clock_fanout']==0
    assert report['clock_topology']['ff_clock_nets']==1
    tool=json.loads((root/'yosys-execution.json').read_text())
    assert tool['diagnostic_bytes']==5000000
    assert len(tool['diagnostic_tail_hex'])==131072


def test_signal_cancellation_reaps_engine_before_returning_and_removes_private_generation(native,tmp_path):
    path,root,program,_=request(tmp_path)
    program.write_text('import os,time\nfrom pathlib import Path\nPath('+repr(str(tmp_path/'engine.pid'))+').write_text(str(os.getpid()))\nprint("{",flush=True)\ntime.sleep(60)\n')
    process=subprocess.Popen([native,'phase1','run',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    deadline=time.monotonic()+5
    while not (tmp_path/'engine.pid').exists() and time.monotonic()<deadline:time.sleep(.01)
    assert (tmp_path/'engine.pid').exists()
    pid=int((tmp_path/'engine.pid').read_text());process.send_signal(signal.SIGTERM)
    _,error=process.communicate(timeout=5)
    assert process.returncode!=0,error
    with pytest.raises(ProcessLookupError):os.kill(pid,0)
    assert not root.exists() and not list(root.parent.glob('.frontend-generation-*'))


def test_summary_rejects_a_changed_published_generation(native,tmp_path):
    path,root,_,_=request(tmp_path);assert run(native,path).returncode==0
    summary=root/'frontend-summary.json';generation=native_file_generation(summary);summary.write_text(summary.read_text()+' ')
    with pytest.raises(Exception,match='generation changed'):read_frontend_summary(summary,generation)


def test_preflight_request_is_only_configuration_and_original_source_generations(tmp_path,monkeypatch):
    from emuflow_tools.frontend import frontend_native as host
    rtl=tmp_path/'source.v';rtl.write_text('module top; endmodule')
    monkeypatch.setattr(host,'require_native_schema',lambda _:None)
    monkeypatch.setattr(host,'resolve_native_executable',lambda *a:sys.executable)
    ctx=SimpleNamespace(sources=(rtl,),include_dirs=(),defines=(),top='top',yosys=None,clocks=('clk',),
        xilinx_mapping_strategy=None,platform_path=PLATFORM,output_dir=tmp_path/'out')
    prepare_frontend(ctx)
    assert ctx.native_frontend_request.stat().st_size<20000
    assert ctx.native_frontend_generation==tuple(str(x) for x in native_file_generation(ctx.native_frontend_request))


def raw_compile_request(tmp_path):
    """Genuine native preparation with controlled external Yosys/OpenSTA engines."""
    from tests.test_compile_preflight_native import request_options
    from emuflow.flow.native_compile import _request
    options=request_options(tmp_path)
    yosys=tmp_path/'yosys'
    yosys.write_text('#!'+sys.executable+'\nprint('+repr(json.dumps(source()))+')\n')
    yosys.chmod(0o700)
    options['yosys']=str(yosys)
    # Any phase adapter on the baseline path is a regression. The controlled
    # STA engine proves its runtime, but deliberately emits no extracted paths.
    shadow=tmp_path/'source/emuflow/flow';shadow.mkdir(parents=True)
    (shadow/'native_compile_adapter.py').write_text("raise AssertionError('baseline compile launched a Python phase adapter')\n")
    value,_=_request((),options);value['python_source']=str(shadow.parents[1])
    request_path=tmp_path/'request.json';request_path.write_text(json.dumps(value))
    return request_path


def test_native_compile_executes_whole_frontend_without_starting_its_python_adapter(native,tmp_path):
    compile_request=raw_compile_request(tmp_path)
    result=subprocess.run([native,'compile',str(compile_request)],capture_output=True,text=True,timeout=30)
    assert result.returncode!=0,result.stderr
    root=tmp_path/'native'
    report=json.loads((root/'native-compile-result.json').read_text())
    stages=report['stages']
    assert [row['stage'] for row in stages]==['preflight','frontend','timing-policy','timing'],result.stderr
    assert stages[0]['implementation_id']=='native-preflight-v1'
    assert stages[1]['implementation_id']=='native-rtl-frontend-v1'
    assert stages[1]['status']=='pass'
    assert (root/'frontend/phase1/phase1_report.json').is_file()
    assert not (root/'calls').exists()
    assert 'Python phase adapter' not in result.stderr
    assert not list(root.glob('.native-frontend*request'))


def test_native_compile_preserves_engine_failure_with_many_source_bindings(native,tmp_path):
    from emuflow.flow.native_compile import _request
    from tests.test_compile_preflight_native import request_options

    options=request_options(tmp_path)
    sources=[]
    for index in range(350):
        path=tmp_path/(f'source_{index:04d}_'+('x'*160)+'.v')
        path.write_text('// Controlled source binding for the native error contract.\n')
        sources.append(path)
    options['sources']=sources
    marker='SOURCE_ENGINE_FAILURE_MUST_REACH_THE_NATIVE_CONTROLLER'
    engine=tmp_path/'failing-yosys'
    engine.write_text('#!'+sys.executable+'\nimport sys\nprint('+repr(json.dumps(source()))+')\n'
                      'print('+repr(marker)+',file=sys.stderr)\nsys.exit(17)\n')
    engine.chmod(0o700)
    options['yosys']=str(engine)
    value,_=_request((),options)
    request_path=tmp_path/'compile-request.json'
    request_path.write_text(json.dumps(value))

    result=subprocess.run([native,'compile',str(request_path)],capture_output=True,text=True,timeout=60)
    assert result.returncode!=0
    output=tmp_path/'native'
    control=json.loads((output/'native-compile-result.json').read_text())
    assert [(row['stage'],row['status']) for row in control['stages']]==[
        ('preflight','pass'),('frontend','fail')]
    assert marker in control['operation']['error']['message']
    assert marker in result.stderr
    assert not (output/'frontend/phase1').exists()
    assert not (output/'timing').exists()

    # The bounded diagnostic must preserve the cause without dropping the
    # producer's complete source-generation evidence or changing its schema.
    producer=tmp_path/'producer-failure.json'
    failed=subprocess.run([native,'--report',str(producer),'phase1','run',
                           str(output/'frontend/native-frontend-request.bin')],
                          capture_output=True,text=True,timeout=60)
    assert failed.returncode!=0 and len(failed.stderr.encode())>32768
    evidence=json.loads(producer.read_text())
    assert evidence['schema']=='emuflow.native-stage-result/v1'
    assert evidence['status']=='fail' and marker in evidence['error']['message']
    assert set(map(str,sources))<={row['path'] for row in evidence['inputs']}


def test_detached_stdout_writer_is_rejected_without_hanging_the_owner(native,tmp_path):
    path,root,program,_=request(tmp_path)
    pidfile=tmp_path/'detached.pid'
    program.write_text('import os,time\nfrom pathlib import Path\npid=os.fork()\nif not pid:\n os.setsid()\n Path('+repr(str(pidfile))+').write_text(str(os.getpid()))\n os.close(2)\n time.sleep(60)\nelse:\n print("{",flush=True)\n')
    try:
        result=run(native,path)
        assert result.returncode!=0
        assert 'detached stdout writer' in result.stderr
        assert not root.exists()
    finally:
        if pidfile.exists():
            try:os.kill(int(pidfile.read_text()),signal.SIGKILL)
            except ProcessLookupError:pass


def test_raw_rtl_control_rejects_a_missing_native_request_instead_of_starting_python(native,tmp_path):
    from tests.native_controller_fault_fixture import install
    compile_request=raw_compile_request(tmp_path)
    command=install(compile_request,"ctx.native_frontend_request.unlink()")
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode!=0 and 'native compile frontend' in result.stderr,result.stderr
    assert 'missing-artifact' in result.stderr and 'cannot open native input' in result.stderr,result.stderr
    assert 'native-frontend-request.bin' in result.stderr,result.stderr
    report=json.loads((tmp_path/'native/native-compile-result.json').read_text())
    assert [row['stage'] for row in report['stages']]==['preflight','frontend']
    assert not (tmp_path/'native/frontend/phase1').exists()
    assert not list(tmp_path.glob('.emuflow-compile-*'))


@pytest.mark.parametrize('damage', ['none','rtl','include'])
def test_prepared_source_checksums_bind_original_rtl_and_include_files(tmp_path, monkeypatch, damage):
    from emuflow_tools.frontend import frontend_native as host
    from emuflow.common.json_io import file_sha256
    rtl=tmp_path/'source.v';rtl.write_text('module top; endmodule')
    header=tmp_path/'config.vh';header.write_text('`define MODEL 1\n')
    checksums={str(p):file_sha256(p) for p in (rtl,header)}
    monkeypatch.setattr(host,'require_native_schema',lambda _:None)
    monkeypatch.setattr(host,'resolve_native_executable',lambda *a:sys.executable)
    ctx=SimpleNamespace(sources=(rtl,),include_dirs=(tmp_path,),defines=(),top='top',yosys=None,clocks=('clk',),
        xilinx_mapping_strategy=None,platform_path=PLATFORM,output_dir=tmp_path/'out',source_checksums=checksums)
    if damage!='none':
        (rtl if damage=='rtl' else header).write_text('changed')
        with pytest.raises(Exception,match='changed after run preparation'):prepare_frontend(ctx)
        assert not ctx.output_dir.exists()
    else:
        prepare_frontend(ctx)
        assert str(header).encode() in ctx.native_frontend_request.read_bytes()
        assert str(rtl).encode() in ctx.native_frontend_request.read_bytes()


def test_native_parser_rejection_reaps_a_live_producer_and_keeps_diagnostics(native,tmp_path):
    path,root,program,_=request(tmp_path)
    pid_path=tmp_path/'rejected-engine.pid'
    program.write_text('import os,sys,time\nfrom pathlib import Path\nPath('+repr(str(pid_path))+').write_text(str(os.getpid()))\nprint("diagnostic",file=sys.stderr,flush=True)\nprint("!invalid",flush=True)\ntime.sleep(60)\n')
    result=run(native,path,timeout=5)
    assert result.returncode!=0
    assert 'diagnostic' in result.stderr
    with pytest.raises(ProcessLookupError):os.kill(int(pid_path.read_text()),0)
    assert not root.exists() and not list(root.parent.glob('.frontend-generation-*'))


def test_native_producer_exit_precedes_incomplete_interchange(native,tmp_path):
    path,root,program,_=request(tmp_path)
    program.write_text('import sys\nprint("{",flush=True)\nprint("engine failure",file=sys.stderr,flush=True)\nraise SystemExit(7)\n')
    result=run(native,path,timeout=5)
    assert result.returncode!=0
    assert 'exited with code 7' in result.stderr
    assert 'engine failure' in result.stderr
    assert not root.exists() and not list(root.parent.glob('.frontend-generation-*'))
