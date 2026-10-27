"""Native Phase 6 configuration, exact clock calendar and entry ownership."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import subprocess
import hashlib
import os
import signal
import time
from pathlib import Path

import pytest

from emuflow.transport.phase6_native import prepare_compile_policy
from emuflow.transport.runtime import build_virtual_runtime
from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation
from tests.native_build import emuflow_native,native_pipeline_api_test
from tests.test_split_overlay_native import owners
from tests.phase3_store_fixture import write_current_assignment_fixture


@pytest.mark.parametrize('periods',[
    {'clk':1.0},{'fast':20.0,'slow':30.0},{'a':0.3,'b':0.2},
    {'a':1e-200,'b':2.5e-200,'c':3e-200},{'a':1e200,'b':2e200},
    {'a':1e-308,'b':2.5e-308},{'z':5e-324,'x':1e-323},
    {'a':1,'b':4096},
])
def test_runtime_matches_every_reference_field_including_empty_ticks(owners,tmp_path,periods):
    case,source,assignment,schedule=owners
    board=tmp_path/'board.json';board.write_text(json.dumps(case.platform.to_dict()))
    expected=build_virtual_runtime(schedule,case.platform,periods)
    result=subprocess.run([str(native_pipeline_api_test()),'runtime',str(schedule.store_path),
        str(board),json.dumps(periods)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==expected


@pytest.mark.parametrize('periods',[{'a':0},{'a':True},{'a':-1},{'a':1,'b':4097},
                                  {'a':1e-300,'b':1e300},{'a':10,'b':10.000000000001}])
def test_native_calendar_rejects_invalid_periods_and_unrepresentable_ratios(owners,tmp_path,periods):
    case,source,assignment,schedule=owners
    board=tmp_path/'board.json';board.write_text(json.dumps(case.platform.to_dict()))
    with pytest.raises(ValidationError):
        build_virtual_runtime(schedule,case.platform,periods)
    result=subprocess.run([str(native_pipeline_api_test()),'runtime',str(schedule.store_path),
        str(board),json.dumps(periods)],capture_output=True,text=True)
    assert result.returncode!=0


def compile_inputs(owners,tmp_path):
    case,source,assignment,schedule=owners
    board=tmp_path/'board.json';board.write_text(json.dumps(case.platform.to_dict()))
    reference=deepcopy(case._phase5_assignment)
    declared={p['fpga'] for p in reference['partitions']}
    reference['partitions'].extend({'fpga':fpga.id,'resources':{}} for fpga in case.platform.fpgas if fpga.id not in declared)
    write_current_assignment_fixture(assignment.with_name('assignment.json'), case.ir, reference)
    periods={clock['id']:1.0 for clock in case.ir.value['clocks']}
    policy=prepare_compile_policy(tmp_path/'policy',source,board,clock_periods=periods)
    output=tmp_path/'native'
    command=[str(emuflow_native()),'phase6','compile',str(source),str(assignment),
             str(schedule.manifest_path),str(board),str(policy),str(output)]
    return case,schedule,periods,policy,output,command


def test_native_compile_constructs_runtime_and_preserves_independent_behavioral_replay(owners,tmp_path):
    case,schedule,periods,policy,output,command=compile_inputs(owners,tmp_path)
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads((output/'phase6_report.json').read_text())
    assert report['equivalence']['mismatches']==0
    assert report['equivalence']['cycles']>0
    with closing(sqlite3.connect(output/'transport.sqlite')) as db:
        configuration=json.loads(db.execute("SELECT value FROM metadata WHERE key='configuration'").fetchone()[0])
    assert configuration['runtime']==build_virtual_runtime(schedule,case.platform,periods)
    assert not list(tmp_path.glob('.transport-stage-*'))


def oracle_source(root,text):
    package=root/'emuflow/transport';package.mkdir(parents=True)
    (package.parent/'__init__.py').write_text('')
    (package/'__init__.py').write_text('')
    source=package/'phase6_oracle.py';source.write_text(text)
    return {'path':str(source),'schema':'phase6-oracle-source-v1',
            'generation':list(native_file_generation(source))}


@pytest.mark.parametrize('damage,expected',[
    ('source-generation','policy source changed'),('board-namespace','board namespace'),
    ('selector-section','envelope disagrees'),('missing-hop','record count disagrees'),
    ('clock','periods must be positive'),('missing-clock','exactly cover declared EmuIR clocks'),
    ('extra-clock','exactly cover declared EmuIR clocks'),('oracle','INJECTED_ORACLE_FAILURE'),
])
def test_native_compile_rejects_invalid_sources_and_failed_oracle_without_publishing(owners,tmp_path,damage,expected):
    case,schedule,periods,policy,output,command=compile_inputs(owners,tmp_path)
    value=json.loads(policy.read_text())
    if damage=='source-generation':value['source']['generation'][2]+=1
    elif damage=='board-namespace':
        clone=tmp_path/'clone-board.json';clone.write_bytes(Path(command[6]).read_bytes())
        value['configuration_sources'][0].update(path=str(clone),generation=list(native_file_generation(clone)))
    elif damage in ('selector-section','missing-hop'):
        selector=json.loads(schedule.manifest_path.read_text())
        if damage=='selector-section':selector['sections']['entries']['records']+=1
        else:
            forged=schedule.store_path.parent/'forged.sqlite';forged.write_bytes(schedule.store_path.read_bytes())
            with closing(sqlite3.connect(forged)) as db,db:
                assert db.execute('SELECT count(*) FROM schedule_entries').fetchone()[0]>0
                db.execute('DELETE FROM schedule_entries WHERE entry_id=(SELECT min(entry_id) FROM schedule_entries)')
            digest=hashlib.sha256(forged.read_bytes()).hexdigest();target=forged.with_name('schedule.'+digest+'.sqlite');forged.rename(target)
            selector.update(artifact=target.name,store_bytes=target.stat().st_size,store_sha256=digest)
        path=schedule.manifest_path.with_name('negative-selector.json');path.write_text(json.dumps(selector));command[5]=str(path)
    elif damage=='clock':value['clock_periods']={clock:0 for clock in periods}
    elif damage=='missing-clock':value['clock_periods']={}
    elif damage=='extra-clock':value['clock_periods']['UNDECLARED_CLOCK']=1.0
    else:value['small_oracle']['source']=oracle_source(tmp_path/'failed-oracle',"raise RuntimeError('INJECTED_ORACLE_FAILURE')\n")
    policy.write_text(json.dumps(value))
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode!=0
    assert expected in result.stderr
    assert not output.exists() and not list(tmp_path.glob('.transport-stage-*'))


def test_native_compile_cancels_and_reaps_its_independent_oracle(owners,tmp_path):
    case,schedule,periods,policy,output,command=compile_inputs(owners,tmp_path)
    marker=tmp_path/'oracle.pid'
    text=("from pathlib import Path\nimport os,signal,time\n"
          "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
          f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
          "while True:time.sleep(.1)\n")
    value=json.loads(policy.read_text());value['small_oracle']['source']=oracle_source(tmp_path/'waiting-oracle',text)
    policy.write_text(json.dumps(value))
    with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as process:
        deadline=time.monotonic()+10
        while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists(),'oracle did not start'
        process.send_signal(signal.SIGTERM)
        stdout,stderr=process.communicate(timeout=10)
        assert process.returncode!=0 and 'cancel' in stderr.lower()
    pid=int(marker.read_text())
    observed=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
    assert 'emuflow.transport.phase6_oracle' not in observed.stdout
    assert not output.exists() and not list(tmp_path.glob('.transport-stage-*'))
