"""Independent load reduction and final assignment consumers over typed columns."""
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
from emuflow.phase3_pressure_native import _Writer
from tests.test_phase3_clusters_native import source
from tests import test_partition_pressure as reference_fixture
from tests.native_build import emuflow_native


def prepare(source):
    ir, constraints = source
    fixture = reference_fixture.PartitionPressureTest(); fixture.setUp()
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
