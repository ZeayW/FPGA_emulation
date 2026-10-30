"""Whole baseline-stage publication and original-source failure boundaries."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emuflow.common.io import read_json
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.stage_clients.transport import validate_phase6
from emuflow_tools.stage_clients.transport_native import run_native_phase6
from emuflow.transport.runtime import build_virtual_runtime
from emuflow.transport.split_netlist_storage import SplitNetlistView
from emuflow.transport.transport_native import transport_request
from tests.native_build import emuflow_native
from tests.test_split_overlay_native import owners


def configuration(case, source, assignment, schedule, root):
    board=root/'board.json';board.write_text(json.dumps(case.platform.to_dict()))
    periods={clock['id']:1.0 for clock in case.ir.value['clocks']}
    runtime=build_virtual_runtime(schedule, case.platform, periods)
    transport,_=transport_request(assignment, schedule, case.platform,
        design=case.ir.value['design'], clocks=case.ir.value['clocks'],
        runtime=runtime, emit_rtl=True)
    request={'schema':'emuflow.phase6-stage/v1', 'transport':transport,
             'configuration_sources':[{'path':str(board.resolve()), 'schema':'boarddb-v1',
                 'generation':list(native_file_generation(board))}],
             'small_design_equivalence':None, 'managed_storage':False,
             'transport_cost':None}
    return board, runtime, request


@pytest.mark.parametrize('managed', [False, True])
def test_whole_stage_preserves_all_source_copies_and_independent_coverage(owners, tmp_path, managed):
    case,source,assignment,schedule=owners
    board,runtime,_=configuration(case,source,assignment,schedule,tmp_path)
    before={path:native_file_generation(path) for path in (source,assignment,schedule.store_path)}
    output=tmp_path/'complete'
    report=run_native_phase6(source,assignment,schedule,case.platform,board,output,
        design=case.ir.value['design'],clocks=case.ir.value['clocks'],
        runtime=runtime,virtual_board=None,managed_storage=managed)
    assert report['stage_implementation_id']=='native-baseline-phase6-v1'
    manifest=read_json(output/'manifest.json')
    assert manifest['clock_periods']=={clock['id']:1.0 for clock in case.ir.value['clocks']}
    assert [p['fpga'] for p in manifest['fpgas']]==[p.id for p in case.platform.fpgas]
    validation=validate_phase6(source,assignment,schedule.manifest_path,board,output/'manifest.json')
    assert validation['status']=='pass'
    assert validation['instances']==report['validation']['instances']
    assert report['validation']['scheduled_hops']==len(schedule['entries'])
    seen=[]
    for part in manifest['fpgas']:
        with SplitNetlistView(output/'split-netlists.sqlite',part['fpga']) as view:
            seen.extend(instance['id'] for instance in view.instances())
        assert (output/part['transport_rtl']).is_file()
        assert (output/part['virtual_xdc_template']).is_file()
    assert len(seen)==report['validation']['instances']
    assert before=={path:native_file_generation(path) for path in before}
    assert not list(tmp_path.glob('.transport-stage-*'))
    assert not list(tmp_path.glob('.native-phase6-*'))


@pytest.mark.parametrize('damage',['late-source-mismatch','original-generation','existing-output',
                                  'board-part','board-link','source-clock','runtime-clock'])
def test_failed_complete_stage_never_exposes_a_partial_generation(owners,tmp_path,damage):
    case,source,assignment,schedule=owners
    board,runtime,request=configuration(case,source,assignment,schedule,tmp_path)
    output=tmp_path/'complete'
    if damage=='late-source-mismatch':
        # Transport generation succeeds first. The subsequent split must reject
        # the different original circuit before exposing any phase artifact.
        with closing(sqlite3.connect(source)) as db,db:
            design=dict(case.ir.value['design']);design['name']='different-design'
            db.execute("UPDATE metadata SET value=? WHERE key='design_json'",(json.dumps(design),))
    elif damage=='original-generation':
        request['configuration_sources'][0]['generation'][2]+=1
    elif damage=='existing-output':
        output.mkdir();(output/'existing.txt').write_text('published generation')
    elif damage=='board-part':
        request['transport']['fpgas'][0]['part']='different-part'
    elif damage=='board-link':
        request['transport']['links'][0]['width']+=1
    elif damage=='source-clock':
        request['transport']['clocks'][0]['id']='different-clock'
    else:
        clocks=request['transport']['runtime']['virtual_dut_clocks']
        clocks['different-clock']=clocks.pop(next(iter(clocks)))
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    result=subprocess.run([str(emuflow_native()),'phase6','run',str(source),str(assignment),
        str(schedule.store_path),str(path),str(output)],capture_output=True,text=True)
    assert result.returncode!=0,result.stdout
    if damage=='existing-output':
        assert {p.name:p.read_text() for p in output.iterdir()}=={'existing.txt':'published generation'}
    else:
        assert not output.exists()
    assert not list(tmp_path.glob('.transport-stage-*'))
