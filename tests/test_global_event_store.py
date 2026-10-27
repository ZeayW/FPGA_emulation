"""Global event ownership, independent native validation and model differentials."""
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
from unittest.mock import patch

import pytest

from tests.event_facts import EventFacts as EventBuilder, MetadataFacts as MetadataBuilder
from emuflow.errors import ValidationError
from emuflow.global_event_store import (
    event_model_sources,
    export_event_model,
)
from emuflow.global_sta import (
    EventCheck, bind_physical_checks,
    export_event_checks, run_event_checks,
)
from emuflow.global_measurements import import_measurements
from emuflow.native_tools import resolve_native_executable
from tests.test_global_sta import example


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    if not os.environ.get("EMUFLOW_NATIVE_ROOT"):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def _rows(values):
    builder = EventBuilder()
    for row in values:
        builder.append(row)
    return builder.finish()


@pytest.mark.parametrize("corruption", [
    "version", "schema", "header", "counts", "missing", "duplicate-observation",
    "role", "nan", "negative", "physical-chain", "orphan-path", "orphan-event", "text-numbers",
])
def test_native_independent_validation_rejects_untrusted_store(tmp_path, corruption):
    builder = EventBuilder()
    owner = builder._owner
    try:
        for row in example():
            builder.append(row)
        db = owner.connection
        header = {"schema": "emuflow.global-event-checks/v1", "mode": 1,
                  "paths": len(builder._path_ids), "checks": owner.checks, "events": len(builder._event_ids)}
        if corruption == "version":
            db.execute("PRAGMA user_version=99")
        elif corruption == "schema":
            db.execute("CREATE TABLE unexpected(value)")
        elif corruption == "header":
            header["extra"] = True
        elif corruption == "counts":
            header["checks"] += 1
        elif corruption == "missing":
            db.execute("DELETE FROM checks WHERE seq=3")
        elif corruption == "duplicate-observation":
            db.execute("UPDATE checks SET role=0 WHERE seq=0")
        elif corruption == "role":
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE checks SET role=8 WHERE seq=0")
        elif corruption in {"nan", "negative", "physical-chain"}:
            value = float('nan') if corruption == "nan" else -1.0 if corruption == "negative" else 17.0
            db.execute("UPDATE checks SET numbers=? WHERE seq=2", (struct.pack('<5d', value,18,8,.5,7),))
        elif corruption == "orphan-path":
            db.execute("UPDATE checks SET path_seq=99 WHERE seq=0")
        elif corruption == "orphan-event":
            db.execute("UPDATE checks SET event_seq=99 WHERE seq=0")
        elif corruption == "text-numbers":
            db.execute("UPDATE checks SET numbers=? WHERE seq=0", ('x' * 24,))
        db.execute("INSERT INTO metadata VALUES('header',?)", (json.dumps(header),))
        db.commit(); db.close(); owner.connection = None
        destination = tmp_path / "previous-index"
        destination.write_bytes(b"previously-published-result")
        result = subprocess.run([resolve_native_executable('emuflow-native'), 'global-sta',
                                 'index-events', str(owner.primary), str(destination)],
                                capture_output=True, text=True)
        assert result.returncode != 0, result.stdout
        assert '"status":"fail"' in result.stderr
        assert destination.read_bytes() == b"previously-published-result"
        assert not list(tmp_path.glob('*.tmp.*'))
    finally:
        owner.close()


def test_bound_events_share_metadata_owner_and_unicode_identity(tmp_path):
    from emuflow.local_path_timing import path_id_set_sha256
    builder = EventBuilder(bound=True)
    metadata = MetadataBuilder(builder)
    names = ['路径😀\x7f\n"\\', 'a', '雪']
    for name in names:
        metadata.append(name, representative_path='代表', path_scope='cross-fpga',
                        clock_domain='时钟', target_period_ns=10.0,
                        physical_logic_segments_cone_bound=False)
        for role, deadline in [('target', 10.0), ('runtime', 100.0)]:
            builder.append_values(name, role, '事件😀', 0.0, (), deadline)
    rows, info = builder.finish(), metadata.finish()
    assert rows._owner is info._owner
    assert list(info) == sorted(names)
    assert info.path_id_set_sha256() == path_id_set_sha256(names)
    assert rows._owner.header['numeric_bytes'] == 6 * 16
    selected, mapping = rows.compressed()
    assert len(selected) == 2
    assert list(mapping) == [0, 1] * 3
    assert rows[-1].path == names[-1]
    root = rows._owner.root
    info.close()
    assert not root.exists()
    with pytest.raises(ValidationError, match='closed'):
        selected[0]


@pytest.mark.parametrize('count', [1, 37])
def test_native_export_preserves_all_arcs_constraints_and_file_seals(tmp_path, count):
    import hashlib
    reference = [EventCheck(name, role, name+'-capture', -0.0,
                           tuple(i / 7.0 for i in range(count)), deadline)
                 for name in ('路径', 'second')
                 for role, deadline in [('target', -2.0), ('runtime', 100.0)]]
    rows = _rows(reference)
    try:
        native = tmp_path / 'native'
        with patch.object(type(rows), '__iter__', side_effect=AssertionError('Python event scan')):
            export_event_checks(rows, native)
        model, sources = event_model_sources(native)
        manifest = json.loads((native / 'model.json').read_text())
        assert len(sources) == 4
        verilog = (model / 'global_timing.v').read_text()
        sdc = (model / 'global_timing.sdc').read_text()
        liberty = (model / 'global_timing.lib').read_text()
        for i, row in enumerate(reference):
            assert f'input i{i};' in verilog and f'output o{i};' in verilog
            for j, delay in enumerate(row.arcs_ns):
                previous = f'i{i}' if j == 0 else f'n{i}_{j-1}'
                following = f'o{i}' if j == count-1 else f'n{i}_{j}'
                assert f'D{j} a{i}_{j} (.A({previous}), .Y({following}));' in verilog
                assert f'values ("{delay:.12g}");' in liberty
            assert f'set_input_delay -clock epoch -max 0 [get_ports i{i}]' in sdc
            assert f'set_output_delay -clock epoch -max {1-row.required_ns:.17g} [get_ports o{i}]' in sdc
        assert verilog.count(' a') == len(reference)*count
        for item in manifest['files']:
            actual = (model / item['name']).read_bytes()
            assert hashlib.sha256(actual).hexdigest() == item['sha256']
            assert len(actual) == item['bytes']
        assert not (native / '.event-export-request.bin').exists()
        path = model / 'analyze.tcl'
        path.write_text(path.read_text()+'\n')
        with pytest.raises(ValidationError, match='generation changed'):
            event_model_sources(native)[0]
    finally:
        rows.close()


def test_signed_zero_and_bundle_events_preserve_numerical_compression():
    first = [EventCheck('p', role, 'p-'+role, -0.0, (-0.0, 1.0), deadline)
             for role, deadline in [('target', 4.0), ('runtime', 40.0)]]
    second = [EventCheck('q', row.role, 'q-'+row.event, 0.0, (0.0, 1.0), row.required_ns) for row in first]
    changed = [EventCheck('r', row.role, 'r-'+row.event, row.launch_ns, row.arcs_ns, row.required_ns+1) for row in first]
    rows = _rows(first+second+changed)
    try:
        selected, mapping = rows.compressed()
        assert list(selected) == first+changed
        assert list(mapping) == [0,1,0,1,2,3]
        assert struct.pack('<d', rows[0].launch_ns) == struct.pack('<d', -0.0)
    finally:
        rows.close()


def test_inline_binding_is_rejected_without_allocating_an_owner():
    from emuflow.global_event_store import _Owner
    with patch.object(_Owner, '__init__', side_effect=AssertionError('retired inline owner')):
        with pytest.raises(ValidationError, match='typed physical owners'):
            bind_physical_checks(None, None, None, None, None)


def test_native_event_execution_restores_all_original_identities(tmp_path, monkeypatch):
    import emuflow.global_sta as sta
    first = example()
    reference = first+[EventCheck('q', r.role, 'q-'+r.event,r.launch_ns,r.arcs_ns,r.required_ns) for r in first]
    rows = _rows(reference)
    engine = tmp_path / 'fixture-sta'
    tsv = 'endpoint\tarrival_ns\trequired_ns\tslack_ns\n'+''.join(
        f'o{i}\t{sum(row.arcs_ns)}\t{row.required_ns-row.launch_ns}\t{row.required_ns-row.launch_ns-sum(row.arcs_ns)}\n'
        for i,row in enumerate(first))
    engine.write_text('#!'+sys.executable+'\nfrom pathlib import Path\n'
                      'print("OpenSTA 3.1.0 fixture")\n'
                      f'Path("measurements.tsv").write_text({tsv!r})\n')
    engine.chmod(0o755)
    monkeypatch.setattr(sta, 'require_opensta_engine', lambda _: {'executable_sha256': 'a'*64})
    try:
        measured = run_event_checks(rows, tmp_path / 'run', str(engine))
        from emuflow.global_measurements import read_native_measurements
        saved = read_native_measurements(tmp_path / 'run/measurements.json', rows)
        try:
            assert list(measured) == list(saved)
        finally:
            saved.close()
        assert not (tmp_path / 'run/measurements.tsv').exists()
        assert measured[5]['path'] == 'q' and measured[5]['event'] == 'q-first'
        assert 'original_checks=10 representative_checks=5' in (tmp_path / 'run/opensta.log').read_text()
    finally:
        rows.close()


def test_closed_borrowed_cursor_releases_storage_after_rejection():
    rows = _rows(example())
    owner, iterator = rows._owner, iter(rows)
    assert next(iterator) == example()[0]
    rows.close()
    assert owner.root.exists()  # Live cursor retains its backing connection.
    with pytest.raises(ValidationError, match='closed'):
        next(iterator)
    assert not owner.root.exists()


def test_measurement_import_binds_generations_at_iteration_boundaries(tmp_path):
    from emuflow import global_event_store
    rows = _rows([EventCheck(f'p{i}', role, 'capture', 0, (1,), deadline)
                  for i in range(100) for role,deadline in [('target', 10),('runtime', 100)]])
    tsv = tmp_path / 'measurements.tsv'
    tsv.write_text('endpoint\tarrival_ns\trequired_ns\tslack_ns\n'+''.join(
        f'o{i}\t1\t{r.required_ns}\t{r.required_ns-1}\n' for i,r in enumerate(rows)))
    try:
        original = global_event_store.require_store_sources_unchanged
        with patch.object(global_event_store, 'require_store_sources_unchanged', wraps=original) as guard:
            measured = import_measurements(tsv, rows)
            assert len(measured) == 200
            assert list(measured)[-1]['path'] == 'p99'
            assert guard.call_count <= 10
    finally:
        rows.close()


def test_export_rejects_mixed_primary_index_and_preserves_manifest(tmp_path):
    from emuflow.native_protocol import CheckedRequestWriter
    first, other = _rows(example()), _rows(example())
    request, manifest = tmp_path / 'request.bin', tmp_path / 'model.json'
    manifest.write_bytes(b'prior-model-manifest')
    try:
        writer = CheckedRequestWriter(request)
        try:
            writer.put('QII', 0x31584547464D5545, 1, 1)
            for source in (first._owner.sources[0], other._owner.sources[1]):
                writer.put('3Q4q', *source.generation)
            writer.finish()
        finally:
            writer.close()
        result = subprocess.run([resolve_native_executable('emuflow-native'), 'global-sta', 'export-events',
                                 str(first._owner.primary), str(other._owner.index), str(request), str(manifest)],
                                capture_output=True, text=True)
        assert result.returncode != 0
        assert 'another event owner' in result.stderr
        assert manifest.read_bytes() == b'prior-model-manifest'
        assert not list(tmp_path.glob('model-*'))
    finally:
        first.close(); other.close()


@pytest.mark.skipif(not os.environ.get('EMUFLOW_TEST_OPENSTA'), reason='real OpenSTA not configured')
def test_real_opensta_native_store_preserves_full_original_path_tns(tmp_path):
    from emuflow.global_sta import build_opensta_timing
    builder = EventBuilder(bound=True)
    metadata = MetadataBuilder(builder)
    for i in range(50):
        path = f'original-{i:04d}'
        metadata.append(path, representative_path='representative', path_scope='cross-fpga',
                        clock_domain='clk', target_period_ns=18.0,
                        physical_logic_segments_cone_bound=False)
        for row in example():
            builder.append_values(path,row.role,row.event,row.launch_ns,row.arcs_ns,row.required_ns)
    rows, paths = builder.finish(), metadata.finish()
    try:
        measured = run_event_checks(rows, tmp_path, os.environ['EMUFLOW_TEST_OPENSTA'])
        assert len(measured) == 250 and rows._owner.header['representative_checks'] == 5
        timing = build_opensta_timing(
            {'virtual_dut_clocks': {'clk': {'nominal_period_ns': 100}},
             'virtual_dut_clock': {'nominal_period_ns': 100}},
            {'paths': paths, 'source_binding': None, 'compressed_representative_paths': 1}, measured)
        assert len(timing['paths']) == 50
        assert timing['target_clock']['worst_slack_bound_ns'] == pytest.approx(-13.5, abs=1e-3)
        assert timing['target_clock']['total_negative_slack_bound_ns'] == pytest.approx(-675.0, abs=.05)
    finally:
        rows.close()
