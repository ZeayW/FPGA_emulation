"""Native physical terminal publication, replay, and corruption rejection."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from tests.native_build import emuflow_native
from tests.native_physical_signoff_fixture import make_terminal_case


@pytest.mark.parametrize('board_kind',[None,'model-only','characterized-upper-bound','measured-upper-bound'])
def test_native_physical_terminal_matches_existing_timing_and_replays(tmp_path,board_kind):
    case=make_terminal_case(tmp_path,board_kind=board_kind)
    result=subprocess.run([str(emuflow_native()),'phase7c','run',str(tmp_path),str(case['engine'])],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads((tmp_path/'runtime/phase7c_report.json').read_text())
    assert report['stage_implementation_id']=='native-physical-phase7c-v1'
    assert report['physical_evidence_completeness']['status']=='pass'
    from emuflow.timing.qor_storage import read_qor_report
    with read_qor_report(tmp_path/'runtime/qor_report.json') as qor:
        from emuflow.timing.global_measurements import read_native_measurements
        from emuflow.timing.global_sta import build_opensta_timing
        values=read_native_measurements(tmp_path/'runtime/global-opensta/measurements.json',case['checks'])
        try:
            expected=build_opensta_timing(case['runtime'],case['metadata'],values)
            assert list(qor['timing']['paths'])==list(expected['paths'])
            for field in ['summary','path_exactness','target_clock','runtime_clock','source_binding']:
                assert qor['timing'][field]==expected[field]
        finally:values.close()
    assert not list(tmp_path.glob('.physical-phase7c-*'))
    assert not (tmp_path/'runtime/global-opensta/events.sqlite').exists()
    assert Path(json.loads((tmp_path/'runtime/global-opensta/event-model.json').read_text())['directory']).is_dir()
    from emuflow.physical.physical_summary_storage import read_physical_summary
    from emuflow.physical.physical_coverage import audit_physical_coverage
    with read_physical_summary(tmp_path/'physical/physical-summary.json') as physical:
        if board_kind is not None:physical['board_link_timing']=json.loads((tmp_path/'timing/board-link-timing.json').read_text())
        audit=audit_physical_coverage(json.loads((tmp_path/'tdm/cross_layer_timing.json').read_text()),
            case['schedule'],physical,case['platform'],tmp_path/'runtime/cross_layer_physical_binding.json',
            tmp_path/'partition/assignment.sqlite')
        assert audit['status']=='pass'
    case['checks'].close()


@pytest.mark.parametrize('damage', ['selector','store','namespace','missing','board','summary-metrics','engine-output','changed-source','old-engine'])
def test_native_physical_terminal_rejects_damage_without_partial_publication(tmp_path,damage):
    case=make_terminal_case(tmp_path,members=3)
    summary_path=tmp_path/'physical/physical-summary.json'
    value=json.loads(summary_path.read_text())
    if damage in {'selector','store','namespace'}:
        ref=value['boundary_timing']['fpga0']
        selector_path=summary_path.parent/ref['path']
        if damage=='store':
            payload=selector_path.parent/json.loads(selector_path.read_text())['artifact']
            with payload.open('ab') as stream:stream.write(b'corrupt')
        elif damage=='namespace':
            value['design']='different-design'
            summary_path.write_text(json.dumps(value))
        else:selector_path.write_text('{}')
    elif damage=='missing':
        del value['local_path_timing']['fpga0'];summary_path.write_text(json.dumps(value))
    elif damage=='board':
        with (tmp_path/'tdm/schedule-board.bin').open('ab') as stream:stream.write(b'corrupt')
    elif damage=='summary-metrics':
        value['validation']['original_cells']+=1;summary_path.write_text(json.dumps(value))
    elif damage=='engine-output':
        case['engine'].write_text('#!/bin/sh\necho "OpenSTA 3.1.0 1234567"\nprintf "truncated" > measurements.tsv\n')
    elif damage=='old-engine':
        case['engine'].write_text(case['engine'].read_text().replace('OpenSTA 3.1.0','OpenSTA 2.6.0'))
    else:
        case['engine'].write_text(case['engine'].read_text()+f'\nPath({str(summary_path)!r}).write_text("changed")\n')
    result=subprocess.run([str(emuflow_native()),'phase7c','run',str(tmp_path),str(case['engine'])],capture_output=True,text=True)
    assert result.returncode!=0
    assert not (tmp_path/'runtime').exists()
    assert not list(tmp_path.glob('.physical-phase7c-*'))
    case['checks'].close()


def test_native_physical_terminal_seals_container_binary_and_launcher(tmp_path):
    import hashlib
    import shutil
    case=make_terminal_case(tmp_path)
    binary=Path(shutil.which('true')).resolve()
    environment={**os.environ,'EMUFLOW_OPENSTA_CONTAINER_BINARY':str(binary)}
    result=subprocess.run([str(emuflow_native()),'phase7c','run',str(tmp_path),str(case['engine']),str(case['engine'])],env=environment,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    engine=json.loads((tmp_path/'runtime/phase7c_report.json').read_text())['system_timing_summary']['global_opensta']['engine']
    assert engine['executable_sha256']==hashlib.sha256(binary.read_bytes()).hexdigest()
    assert engine['launcher_sha256']==hashlib.sha256(case['engine'].read_bytes()).hexdigest()
    from emuflow.timing.global_sta import read_engine_identity
    assert read_engine_identity(tmp_path/'runtime/global-opensta/opensta.log')==engine
    case['checks'].close()
