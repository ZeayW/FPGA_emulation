"""Independent load reduction and final assignment consumers over typed columns."""
from tests.phase3_store_fixture import write_assignment_index_fixture
from array import array
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.native_tools import native_file_generation
from emuflow.partition import (
    _CompactClusterRecords, _CompactInstanceAssignment, _PackedClusterAssignment,
    build_clusters, build_partition_assignment, validate_cluster_assignment_balance,
    validate_partition_artifacts_online, CUT_MODE_STATIC_EXACT,
)
from emuflow.phase3_assignment_native import reduce_native_partition_loads
from emuflow.native_protocol import CheckedRequestWriter as _Writer
from tests.test_phase3_clusters_native import source
from tests import partition_pressure_fixture as reference_fixture
from tests.native_build import emuflow_native


def prepare(source):
    ir, constraints = source
    fixture = reference_fixture.PressureFixture(); fixture.setUp()
    constraints = dict(constraints, balance_tolerance=1.0)
    clusters = build_clusters(ir, constraints, cut_mode=CUT_MODE_STATIC_EXACT,
                             _native_compact_clusters=True)
    parts = _PackedClusterAssignment(('a', 'b'), (i % 2 for i in range(len(clusters['clusters']))))
    return ir, fixture.platform, constraints, clusters, parts


@pytest.mark.parametrize('catalog', [('a', 'b'), ('b', 'a'), ('unused', 'b', 'a')])
def test_native_reduction_and_balance_match_independent_records(source, catalog):
    ir, platform, constraints, clusters, initial = prepare(source)
    records = clusters['clusters']
    assignment = _PackedClusterAssignment(catalog, (catalog.index(initial.part_for_index(i)) for i in range(len(initial))))
    expected = validate_cluster_assignment_balance(platform, list(records), dict(assignment), 1.0,
                                                   requested_balance_dimensions=('cells', 'lut'))
    with patch.object(_CompactClusterRecords, '_record', side_effect=AssertionError('expanded cluster')):
        actual = validate_cluster_assignment_balance(platform, records, assignment, 1.0,
                                                     requested_balance_dimensions=('cells', 'lut'))
    assert actual == expected


def test_builder_and_final_validator_never_expand_native_clusters(source):
    ir, platform, constraints, clusters, assignment = prepare(source)
    plain = {'schema': clusters['schema'], **clusters, 'clusters': list(clusters['clusters'])}
    expected = build_partition_assignment(ir, platform, plain, constraints, dict(assignment), 'fixture', 0,
        _include_semantic_contract=False, _defer_derived_records=True)
    expected_validation = validate_partition_artifacts_online(platform, plain, expected)
    with patch.object(_CompactClusterRecords, '_record', side_effect=AssertionError('expanded cluster')):
        actual = build_partition_assignment(ir, platform, clusters, constraints, assignment, 'fixture', 0,
            _include_semantic_contract=False, _stream_semantic_records=True, _defer_derived_records=True)
        assert isinstance(actual['instance_assignment'], _CompactInstanceAssignment)
        assert actual['instance_assignment']._cluster_parts is assignment.part_indices
        assert actual['instance_assignment']._cluster_member_offsets is clusters['clusters']._member_offsets
        validation = validate_partition_artifacts_online(platform, clusters, actual)
    assert validation == expected_validation
    assert [{k: v for k, v in row.items() if k != 'clusters'} for row in expected['partitions']] == actual['partitions']
    assert dict(actual['instance_assignment']) == expected['instance_assignment']


@pytest.mark.parametrize('kind', ['missing', 'extra', 'unknown-part', 'lost-primary', 'stale-primary', 'stale-source'])
def test_selected_native_consumer_rejects_invalid_inputs_without_reference_retry(source, kind):
    ir, platform, constraints, clusters, assignment = prepare(source)
    records = clusters['clusters']
    if kind in ('missing', 'extra'):
        assignment = dict(assignment)
        assignment.pop('c000000')
        if kind == 'extra': assignment['not-canonical'] = 'a'
    elif kind == 'unknown-part':
        assignment = _PackedClusterAssignment(('unknown',), [0] * len(records))
    elif kind == 'lost-primary':
        del records.native_artifact
    else:
        path = records.native_artifact if kind == 'stale-primary' else ir.path
        with Path(path).open('ab') as stream: stream.write(b'changed')
    with patch.object(_CompactClusterRecords, '_record', side_effect=AssertionError('reference retry')):
        with pytest.raises(ValidationError):
            reduce_native_partition_loads(records, assignment, ('a', 'b'))


@pytest.mark.parametrize('kind', ['ownership', 'coverage', 'truncated', 'checksum', 'catalog', 'version', 'generation', 'huge-count', 'catalog-budget'])
def test_native_reducer_rejects_bad_requests_before_publication(source, tmp_path, kind):
    ir, _platform, _constraints, clusters, assignment = prepare(source)
    records = clusters['clusters']
    request, output = tmp_path/'request.bin', tmp_path/'output.json'
    writer = _Writer(request)
    try:
        writer.put('QI', 0x3151454C504D5545, 9 if kind == 'version' else 1)
        for path in (ir.path, records.native_artifact):
            generation = list(native_file_generation(path))
            if kind == 'generation': generation[2] += 1
            writer.put('3Q4q', *generation)
        writer.put('QI', 2**64-1 if kind == 'huge-count' else len(records)+(kind == 'coverage'), 2)
        writer.text('x'*(1024*1024) if kind == 'catalog-budget' else 'a')
        writer.text('a' if kind == 'catalog' else 'b')
        indices = array('I', assignment.part_indices)
        if kind == 'ownership': indices[0] = 2
        writer.bytes(indices); writer.finish()
    finally:
        writer.close()
    if kind in ('truncated', 'checksum'):
        data = request.read_bytes()
        request.write_bytes(data[:-1] if kind == 'truncated' else data[:-4]+b'xxxx')
    output.write_text('published-before-error')
    result = subprocess.run([str(emuflow_native()), 'phase3', 'reduce-loads', str(ir.path),
        str(records.native_artifact), str(request), str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_text() == 'published-before-error'
    assert not list(tmp_path.glob('*.tmp*'))


def test_final_validator_rejects_different_instance_ownership(source):
    ir, platform, constraints, clusters, assignment = prepare(source)
    actual = build_partition_assignment(ir, platform, clusters, constraints, assignment, 'fixture', 0,
        _include_semantic_contract=False, _stream_semantic_records=True, _defer_derived_records=True)
    actual['instance_assignment']._cluster_parts = array('I', [0] * len(assignment))
    with pytest.raises(ValidationError, match='disagrees with instances'):
        validate_partition_artifacts_online(platform, clusters, actual)


def test_capacity_and_reserved_budget_remain_hard_constraints(source):
    _ir, platform, _constraints, clusters, _assignment = prepare(source)
    assignment = _PackedClusterAssignment(('a', 'b'), [0] * len(clusters['clusters']))
    with pytest.raises(ValidationError, match='multi-resource balance/capacity'):
        validate_cluster_assignment_balance(platform, clusters['clusters'], assignment, 0.0,
                                           requested_balance_dimensions=('cells',))
    # Full reference reports remain byte-for-byte equivalent under reservations.
    reservations = {'a': {'lut': 1}, 'b': {'lut': 1}}
    balanced = _PackedClusterAssignment(('a', 'b'), (i % 2 for i in range(len(assignment))))
    assert validate_cluster_assignment_balance(platform, clusters['clusters'], balanced, 1.0,
        resource_reservations_by_fpga=reservations) == validate_cluster_assignment_balance(
            platform, list(clusters['clusters']), dict(balanced), 1.0,
            resource_reservations_by_fpga=reservations)


def test_offline_load_differential_seals_summary_and_retires_payloads(source, tmp_path):
    ir, platform, _constraints, _clusters, _assignment = prepare(source)
    board = tmp_path/'board.json'; board.write_text(json.dumps(platform.to_dict()))
    output = tmp_path/'diagnostic'
    script = Path(__file__).resolve().parents[1]/'scripts/validation/qualify_primary_loads.py'
    result = subprocess.run([sys.executable, str(script), '--ir', str(ir.path),
        '--platform', str(board), '--out', str(output), '--expected-design', ir.value['design']['name']],
        env=dict(os.environ), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    summary = json.loads((output/'qualification.json').read_text())
    assert summary['status'] == 'pass'
    assert summary['instances'] == len(ir.value['instances'])
    assert len(summary['ordered_load_sha256']) == 64
    assert summary['reference_wall_seconds'] > 0 and summary['native_wall_seconds'] > 0
    assert 'not a legal candidate' in summary['scope']
    assert not (output/'tmp').exists()
    failed = tmp_path/'bad-design'
    result = subprocess.run([sys.executable, str(script), '--ir', str(ir.path),
        '--platform', str(board), '--out', str(failed), '--expected-design', 'wrong-design'],
        env=dict(os.environ), capture_output=True, text=True)
    assert result.returncode != 0
    assert (failed/'failure.json').is_file() and not (failed/'tmp').exists()


def _published_rows(path):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: sorted(db.execute('SELECT * FROM '+table), key=repr) for table in tables}


@pytest.mark.parametrize('exact', [False, True])
@pytest.mark.parametrize('mode', ['normal', 'unused-part', 'unicode', 'sparse'])
def test_native_index_matches_reference_without_host_instance_records(source, tmp_path, exact, mode):
    import sqlite3
    from contextlib import closing
    if mode in ('unicode', 'sparse'):
        with closing(sqlite3.connect(source[0].path)) as db, db:
            if mode == 'unicode':
                db.execute("UPDATE cell_records SET id='é雪😀' WHERE seq=0")
                db.execute("UPDATE net_records SET id='étude😀' WHERE seq=0")
            else:
                db.execute('UPDATE endpoint_records SET ordinal=ordinal*7+3')
    ir, platform, constraints, clusters, parts = prepare(source)
    if mode == 'unused-part':
        parts = _PackedClusterAssignment(('a', 'b'), [1] * len(parts))
    assignment = build_partition_assignment(ir, platform, clusters, constraints, parts, 'fixture', 7,
        _include_semantic_contract=exact, _stream_semantic_records=True)
    # The independent old writer consumes the same logical assignment, but only
    # this small fixture may expand identities for its reference relation.
    reference = dict(assignment, instance_assignment=dict(assignment['instance_assignment'].items()))
    expected, actual = tmp_path/'reference/index.sqlite', tmp_path/'native/index.sqlite'
    reference_summary = write_assignment_index_fixture(expected, ir, reference)
    with patch.object(_CompactInstanceAssignment, 'iter_source_assignments', side_effect=AssertionError('host instance row')):
        summary = write_assignment_index_fixture(actual, ir, assignment)
    assert summary['writer_implementation'] == 'native-primary-assignment-index-v1'
    assert reference_summary['writer_implementation'] == 'python-reference-v1'
    assert {k:v for k,v in summary.items() if k not in ('writer_implementation','native_writer_metrics')} == {
        k:v for k,v in reference_summary.items() if k not in ('writer_implementation','native_writer_metrics')}
    assert _published_rows(actual) == _published_rows(expected)
    if exact:
        from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT
        assert (actual.parent/EXACT_BINARY_ARTIFACT).read_bytes() == (expected.parent/EXACT_BINARY_ARTIFACT).read_bytes()


@pytest.mark.parametrize('kind', ['checksum', 'truncated', 'version', 'ownership', 'generation', 'source-count', 'dangling-predecessor'])
def test_native_index_failure_preserves_published_pair_without_retry(source, tmp_path, kind):
    import struct
    from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT
    from emuflow.tool_runtime import NativeToolProcess
    import emuflow.phase3_assignment_native as publication
    ir, platform, constraints, clusters, parts = prepare(source)
    assignment = build_partition_assignment(ir, platform, clusters, constraints, parts, 'fixture', 7,
        _include_semantic_contract=False, _stream_semantic_records=True)
    if kind == 'dangling-predecessor':
        assignment['cut_nets'] = [dict(c, dependency_level=0, combinational_dependency_depth=0,
            predecessor_cut_nets=['nonexistent']) for c in assignment['cut_nets']]
        assert assignment['cut_nets']
    path=tmp_path/'index.sqlite'; path.write_bytes(b'prior-index')
    binary=tmp_path/EXACT_BINARY_ARTIFACT; binary.write_bytes(b'prior-exact')
    def corrupted_process(executable, command, **kwargs):
        request=Path(command[-2]); data=bytearray(request.read_bytes())
        if kind == 'checksum': data[-1] ^= 1
        elif kind == 'truncated': data=data[:-1]
        elif kind == 'version': struct.pack_into('<I',data,8,9)
        elif kind == 'generation': data[12] ^= 1
        elif kind in ('ownership', 'source-count'):
            offset=12+2*56
            for _ in range(2):
                count=struct.unpack_from('<I',data,offset)[0];offset+=4
                for _ in range(count):
                    length=struct.unpack_from('<I',data,offset)[0];offset+=4+length
            if kind == 'ownership': struct.pack_into('<I',data,offset+16,999)
            else: struct.pack_into('<Q',data,offset+8,999)
        if kind not in ('checksum', 'truncated'):
            import zlib
            struct.pack_into('<I',data,len(data)-4,zlib.crc32(data[:-4]))
        request.write_bytes(data)
        return NativeToolProcess(executable, command, **kwargs)
    with patch.object(publication, 'NativeToolProcess', side_effect=corrupted_process), patch.object(
            _CompactInstanceAssignment, 'iter_source_assignments', side_effect=AssertionError('reference retry')):
        with pytest.raises(ValidationError):
            write_assignment_index_fixture(path, ir, assignment)
    assert path.read_bytes()==b'prior-index' and binary.read_bytes()==b'prior-exact'
    assert not list(tmp_path.glob('.*.tmp*'))
    assert not list(tmp_path.glob('emuflow-assignment-publish-*'))


@pytest.mark.parametrize('kind', ['lost-primary', 'stale-primary', 'stale-source', 'validator', 'post-validation-source', 'post-validation-primary'])
def test_native_publication_rejection_preserves_existing_index_and_exact_data(source, tmp_path, kind):
    import emuflow.phase3_assignment_index as index
    from emuflow.phase3_exact_binary import EXACT_BINARY_ARTIFACT
    from contextlib import ExitStack
    ir, platform, constraints, clusters, parts = prepare(source)
    assignment = build_partition_assignment(ir, platform, clusters, constraints, parts, 'fixture', 7,
        _include_semantic_contract=True, _stream_semantic_records=True)
    instance = assignment['instance_assignment']
    path=tmp_path/'index.sqlite'; path.write_bytes(b'old-index')
    binary=tmp_path/EXACT_BINARY_ARTIFACT; binary.write_bytes(b'old-exact')
    if kind == 'lost-primary': instance._native_primary_artifact = None
    elif kind in ('stale-primary', 'stale-source'):
        changed=instance._native_primary_artifact if kind=='stale-primary' else ir.path
        with Path(changed).open('ab') as out: out.write(b'changed')
    with ExitStack() as patches:
        patches.enter_context(patch.object(_CompactInstanceAssignment, 'iter_source_assignments', side_effect=AssertionError('reference fallback')))
        if kind.startswith('post-validation-'):
            validate = index.validate_phase3_assignment_index
            def change_validated_source(*args, **kwargs):
                result = validate(*args, **kwargs)
                source_path = ir.path if kind.endswith('source') else instance._native_primary_artifact
                source_path.chmod(source_path.stat().st_mode ^ 0o100)
                return result
            patches.enter_context(patch.object(index, 'validate_phase3_assignment_index', side_effect=change_validated_source))
        elif kind=='validator':
            patches.enter_context(patch.object(index, 'validate_phase3_assignment_index', side_effect=ValidationError('injected validator rejection')))
        with pytest.raises((ValidationError,OSError)):
            write_assignment_index_fixture(path, ir, assignment)
    assert path.read_bytes()==b'old-index' and binary.read_bytes()==b'old-exact'
    assert not list(tmp_path.glob('.*.tmp*'))
    assert not list(tmp_path.glob('.*.previous'))
