"""Current ScheduleStore source/identity negatives, with no retired wire protocol."""
from contextlib import closing

from tests.schedule_store_fixture import write_tdm_schedule_storage
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.tdm import build_native_exact_schedule_request
from tests.schedule_reference import build_tdm_schedule
from emuflow.tdm_schedule_storage import (read_tdm_schedule)
from tests.native_build import emuflow_native
from tests.native_schedule_case import _case
from tests.test_native_schedule_generation import install_runtime


@pytest.mark.parametrize('corruption', [
    'predecessor', 'dependency_cycle', 'cycle', 'arc', 'metadata', 'orphan', 'count', 'oversized_count',
    'route_schema', 'replaced_generation', 'request_trailing', 'request_truncated', 'config_max',
])
def test_complete_producer_rejects_invalid_sources_without_replacing_commit(tmp_path, monkeypatch, corruption):
    case, routes = _case(tmp_path, False, False)
    expected = build_tdm_schedule(routes, case.platform)
    manifest = tmp_path / 'published/schedule.json'
    write_tdm_schedule_storage(manifest, expected)
    expected = read_tdm_schedule(manifest, materialize=True)
    previous = manifest.read_bytes()
    install_runtime(tmp_path, monkeypatch)
    request = build_native_exact_schedule_request(routes, case.platform)
    assert request is not None
    if corruption == 'replaced_generation':
        replacement = tmp_path / 'replacement.sqlite'
        replacement.write_bytes(request.route_store.read_bytes())
        replacement.replace(request.route_store)
    elif corruption.startswith('request_') or corruption == 'config_max':
        original = type(request).write_board_request
        def corrupt(self, path):
            original(self, path)
            data = bytearray(path.read_bytes())
            if corruption == 'request_trailing':data += b'x'
            elif corruption == 'request_truncated':data = data[:-1]
            else:data[84:88] = struct.pack('<I', 32)
            path.write_bytes(data)
        monkeypatch.setattr(type(request), 'write_board_request', corrupt)
    else:
        with closing(sqlite3.connect(request.route_store)) as db, db:
            if corruption == 'predecessor':db.execute("UPDATE route_predecessors SET predecessor_net='unknown'")
            elif corruption == 'dependency_cycle':
                records = db.execute('SELECT seq,net_id FROM routes ORDER BY seq').fetchall()
                assert len(records) == 2
                db.execute('DELETE FROM route_predecessors')
                db.executemany('INSERT INTO route_predecessors VALUES (?,?,?)', [(records[0][0], 0, records[1][1]), (records[1][0], 0, records[0][1])])
            elif corruption == 'cycle':db.execute('UPDATE route_edges SET sink_fpga=source_fpga WHERE route_seq=0')
            elif corruption == 'arc':db.execute("UPDATE route_edges SET link_id='unknown' WHERE route_seq=0")
            elif corruption == 'metadata':db.execute('UPDATE routes SET dependency_level=NULL,combinational_dependency_depth=NULL WHERE seq=0')
            elif corruption == 'orphan':db.execute("INSERT INTO route_edges VALUES (999,0,'link_0_1','fpga0','fpga1')")
            elif corruption == 'route_schema':db.execute("UPDATE metadata SET value=json_set(value,'$.schema','unknown') WHERE key='header'")
            else:db.execute("UPDATE metadata SET value=json_set(value,'$.routes.records',?) WHERE key='sections'", (2**32-1 if corruption == 'oversized_count' else 1,))
        # Forging fresh local metadata does not make malformed records valid.
        request = replace(request, route_generation=native_file_generation(request.route_store))
    with patch('tests.schedule_store_fixture._publish_fixture_records', side_effect=AssertionError('silent Python fallback')):
        with pytest.raises(ValidationError):
            write_tdm_schedule_storage(manifest, request)
    assert manifest.read_bytes() == previous
    assert read_tdm_schedule(manifest, materialize=True) == expected
    assert not list(manifest.parent.glob('*.publishing.*'))
    assert not list(manifest.parent.glob('.*.tmp*'))
    assert not list(manifest.parent.glob('*request*'))


def test_producer_rejects_source_and_report_aliases_before_writing(tmp_path, monkeypatch):
    case, routes = _case(tmp_path, False, False)
    install_runtime(tmp_path, monkeypatch)
    producer = build_native_exact_schedule_request(routes, case.platform)
    from emuflow.phase5_native import _write_inputs
    request_root = tmp_path / 'request'
    request_root.mkdir()
    inputs = _write_inputs(producer, request_root)
    source = producer.route_store
    snapshots = {Path(p): Path(p).read_bytes() for p in inputs}
    run = subprocess.run
    tool = str(emuflow_native())
    for target in [Path(p) for p in inputs]:
        result = run([tool, 'phase5', 'run', *inputs, str(target)], capture_output=True, text=True)
        assert result.returncode == 2, result.stderr
    output = tmp_path / 'phase5_report.json'
    output.write_bytes(b'previous output')
    for report in [*[Path(p) for p in inputs], output]:
        result = run([tool, '--report', str(report), 'phase5', 'run', *inputs, str(output)], capture_output=True, text=True)
        assert result.returncode == 2, result.stderr
    for p, content in snapshots.items():
        assert p.read_bytes() == content
    assert output.read_bytes() == b'previous output'


@pytest.mark.parametrize('operation,arity', [('schedule', 2), ('schedule-from-store', 3), ('publish-schedule', 5)])
def test_retired_phase5_commands_reject_without_opening_inputs_or_replacing_outputs(tmp_path, operation, arity):
    output = tmp_path / 'previous'
    output.write_bytes(b'previous')
    result = subprocess.run([str(emuflow_native()), 'phase5', operation, *([str(tmp_path/'missing')]*(arity-1)), str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert output.read_bytes() == b'previous'
    assert list(tmp_path.iterdir()) == [output]
