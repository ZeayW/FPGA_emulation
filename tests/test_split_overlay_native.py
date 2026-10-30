"""Source-owned DUT split equivalence and downstream projection contracts."""
from tests.phase3_store_fixture import write_assignment_index_fixture
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import pytest

from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.common.native_tools import native_file_generation
from emuflow_tools.stage_clients.schedule_native import run_native_phase5
from emuflow.transport.split_netlist_storage import SplitNetlistView,validate_split_netlists_sqlite
from emuflow.transport.split_overlay import build_native_split,bind_split_sources
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from tests.test_phase5_from_stores import native_inputs
from tests.split_reference import write_split_netlists_sqlite


@pytest.fixture(params=[False,True,'banks','tree'])
def owners(tmp_path,request):
    case,routes,producer,inputs=native_inputs(tmp_path,request.param)
    # The scheduler's tree case adds a relay-only FPGA. Its source assignment
    # must declare that empty partition before testing a complete split.
    from copy import deepcopy
    assignment=deepcopy(case._phase5_assignment)
    declared={row['fpga'] for row in assignment['partitions']}
    for fpga in case.platform.fpgas:
        if fpga.id not in declared:
            assignment['partitions'].append({'fpga':fpga.id,'resources':{}})
    write_assignment_index_fixture(inputs[1],case.ir,assignment)
    source=tmp_path/'source.sqlite';write_compact_emuir(source,case.ir.value)
    run_native_phase5(producer,tmp_path/'schedule')
    schedule=read_tdm_schedule(tmp_path/'schedule/schedule.json')
    return case,source,inputs[1],schedule


def test_source_overlay_matches_complete_split_without_copying_original_populations(owners,tmp_path):
    case,source,assignment,schedule=owners
    reference=tmp_path/'reference/split.sqlite'
    expected=write_split_netlists_sqlite(reference,source,assignment,schedule,
        expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    exact=assignment.with_name('assignment.exact.bin')
    before=native_file_generation(exact)
    output=tmp_path/'native/split.sqlite'
    actual=build_native_split(output,source,assignment,schedule,
        expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    assert actual==expected
    assert native_file_generation(exact)==before
    for fpga in case.platform.fpgas:
        with SplitNetlistView(reference,fpga.id) as a,SplitNetlistView(output,fpga.id) as b:
            assert a.ports==b.ports
            assert a.resources==b.resources
            assert list(a.instances())==list(b.instances())
            assert list(a.nets())==list(b.nets())
            assert a.counts()==b.counts()
            from tests.native_build import native_pipeline_api_test
            result=subprocess.run([str(native_pipeline_api_test()),'split-view',str(output),str(b.part_index)],capture_output=True,text=True)
            assert result.returncode==0,result.stderr
            assert json.loads(result.stdout)==[b.counts()['instances'],sum(len(n['drivers'])+len(n['sinks']) for n in b.nets())]
    with closing(sqlite3.connect(output)) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables=={'metadata','parts','net_segments','shadow_drivers','port_memberships','resources'}
    assert bind_split_sources(output)[1].path==source.resolve()
    assert not output.with_name('assignment.exact.bin').exists()
    assert validate_split_netlists_sqlite(output,source,assignment,
        expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)==expected


@pytest.mark.parametrize('damage',['source','generation','missing','platform','shadow','membership'])
def test_split_source_and_overlay_damage_is_rejected_independently(owners,tmp_path,damage):
    from emuflow.common.errors import ValidationError
    from emuflow.transport.split_netlist_storage import open_split_netlists_sqlite
    case,source,assignment,schedule=owners;output=tmp_path/'split.sqlite'
    build_native_split(output,source,assignment,schedule,expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
    if damage=='source':source.chmod(source.stat().st_mode ^ 0o100)
    else:
        with closing(sqlite3.connect(output)) as db,db:
            if damage in ('generation','missing'):
                record=json.loads(db.execute("SELECT value FROM metadata WHERE key='source_bindings'").fetchone()[0])
                if damage=='missing':record.pop('source')
                else:record['source']['generation'][2]+=1
                db.execute("UPDATE metadata SET value=? WHERE key='source_bindings'",(json.dumps(record),))
            elif damage=='platform':db.execute("UPDATE metadata SET value='\"foreign\"' WHERE key='platform'")
            elif damage=='shadow':
                assert db.execute('SELECT count(*) FROM shadow_drivers').fetchone()[0]>0
                db.execute('DELETE FROM shadow_drivers WHERE schedule_entry_seq IN (SELECT schedule_entry_seq FROM shadow_drivers LIMIT 1)')
            else:db.execute('DELETE FROM net_segments WHERE (part_index,source_net_seq) IN (SELECT part_index,source_net_seq FROM net_segments LIMIT 1)')
    if damage not in ('shadow','membership'):
        with pytest.raises(ValidationError):open_split_netlists_sqlite(output)
    with pytest.raises(ValidationError):
        validate_split_netlists_sqlite(output,source,assignment,expected_design=case.ir.value['design']['name'],expected_platform=case.platform.name)
