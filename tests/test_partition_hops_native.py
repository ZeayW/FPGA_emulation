"""Source-owned hop generation matches the retained independent numeric oracle."""
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.partition import build_clusters, build_partition_assignment, normalize_partition_constraints
from emuflow.partition_hops import refine_partition_hops, validate_assignment_hops
from emuflow.partition_hops_native import run
from emuflow.partition_init_native import initialize
from emuflow.partition_ir import load_partition_ir
from emuflow.routing import load_route_constraints
from emuflow.store_publication import bind_store_source
from tests import test_partition_hops as hop_fixture
from tests.test_partition_init_native import weights


@pytest.fixture
def case(tmp_path, request):
    f = hop_fixture.HopPartitionRefinementTest();f.setUp()
    constraints, reference_clusters, reference = f._artifacts(fixed=getattr(request, "param", False))
    path = tmp_path/'source.sqlite';write_compact_emuir(path, f.ir.value)
    ir = load_partition_ir(path)
    constraints = normalize_partition_constraints(constraints, ir, f.platform)
    clusters = build_clusters(ir, constraints, cut_mode='sequential-only', _native_compact_clusters=True)
    parts = [fpga.id for fpga in f.platform.fpgas]
    solution = tmp_path/'initial.txt'
    solution.write_text('\n'.join(str(parts.index(reference['cluster_assignment'][f'c{i:06d}'])) for i in range(len(clusters['clusters'])))+'\n')
    owner, _ = initialize(ir, f.platform, clusters, constraints, seed=7, executable=None, solution_input=solution)
    assignment = build_partition_assignment(ir, f.platform, clusters, constraints, owner,
        provider='test-provider', seed=7, _include_semantic_contract=False, _stream_semantic_records=True)
    route_path = f._route_constraints(tmp_path)
    return f, ir, constraints, clusters, assignment, route_path, reference_clusters, reference


@pytest.mark.parametrize('weighted', [False, True])
def test_original_source_solver_and_independent_audit_match_reference(case, tmp_path, weighted):
    f, ir, constraints, clusters, assignment, route_path, reference_clusters, reference = case
    weight_path = None
    if weighted:
        names = [net['id'] for net in f.ir.value['nets']]
        weight_path = weights(tmp_path, [(names[0], 1.0, 1, 3.25), (names[-1], 0.0, 1, 8.0)])
    expected, expected_report = refine_partition_hops(f.ir, f.platform, reference_clusters,
        constraints, reference, tmp_path/'reference', route_constraints_path=route_path,
        net_weights_path=weight_path, defer_semantic_contract=True)
    with patch('emuflow.partition_hops._compact_primary_net_records', side_effect=AssertionError('host net population')), \
         patch('emuflow.partition_hops.load_partition_net_weights', side_effect=AssertionError('host weight population')), \
         patch('emuflow.partition_hops._parse_native_output', side_effect=AssertionError('host owner parsing')), \
         patch('emuflow.partition_hops.audit_assignment_hops', side_effect=AssertionError('host cut audit')):
        actual, report = refine_partition_hops(ir, f.platform, clusters, constraints,
            assignment, tmp_path/'native', route_constraints_path=route_path,
            net_weights_path=weight_path, defer_semantic_contract=True)
    assert dict(actual['cluster_assignment']) == dict(expected['cluster_assignment'])
    assert report['before'] == expected_report['before']
    assert report['after'] == expected_report['after']
    assert report['native_metrics'] == expected_report['native_metrics']
    assert report['move_count'] == expected_report['move_count']
    assert report['implementation_id'] == 'native-primary-hop-refinement-v1'
    routes = load_route_constraints(route_path, f.platform)
    with patch('emuflow.partition_hops.audit_assignment_hops', side_effect=AssertionError('host audit')):
        audit = validate_assignment_hops(f.platform, actual, routes,
            _native_context=(ir, clusters, constraints))
    assert audit == report['after']


def test_zero_move_owner_keeps_original_artifact(case, tmp_path):
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    route_path.write_text(json.dumps({'schema': 'emuflow.system-route-constraints/v1', 'max_route_hops': 3}))
    actual, report = refine_partition_hops(ir, f.platform, clusters, constraints, assignment,
        tmp_path/'native', route_constraints_path=route_path, defer_semantic_contract=True)
    assert report['move_count'] == 0
    assert actual['cluster_assignment'] is assignment['cluster_assignment']


def test_audit_rejects_original_violations_without_repair(case):
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    with pytest.raises(ValidationError, match='hard hop constraints'):
        run(ir, f.platform, clusters, constraints, assignment,
            load_route_constraints(route_path, f.platform), audit_only=True)


def test_corrupt_original_owner_rejected_without_reference_retry(case, tmp_path):
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    owner = assignment['cluster_assignment']
    data = bytearray(owner.native_initial_source.path.read_bytes());data[-1] ^= 1
    corrupt = tmp_path/'corrupt.bin';corrupt.write_bytes(data)
    owner.native_initial_source = bind_store_source(corrupt, 'patron-initial-columns-v1')
    with patch('emuflow.partition_hops._write_native_input', side_effect=AssertionError('reference retry')):
        with pytest.raises(ValidationError, match='checksum'):
            refine_partition_hops(ir, f.platform, clusters, constraints, assignment,
                tmp_path/'native', route_constraints_path=route_path, defer_semantic_contract=True)


@pytest.mark.parametrize('case', [True], indirect=True)
def test_fixed_source_constraints_cannot_be_relaxed_by_hop_repair(case, tmp_path):
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    with pytest.raises(ValidationError, match='hard hop constraints'):
        refine_partition_hops(ir, f.platform, clusters, constraints, assignment,
            tmp_path/'native', route_constraints_path=route_path, defer_semantic_contract=True)


def test_native_owner_replay_is_deterministic_and_rejects_changed_weights(case, tmp_path):
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    routes = load_route_constraints(route_path, f.platform)
    a, sa = run(ir, f.platform, clusters, constraints, assignment, routes)
    b, sb = run(ir, f.platform, clusters, constraints, assignment, routes)
    assert a.native_initial_source.path.read_bytes() == b.native_initial_source.path.read_bytes()
    assert sa['native_metrics'] == sb['native_metrics']
    names = [net['id'] for net in f.ir.value['nets']]
    path = weights(tmp_path, [(names[0], 1.0, 1, 2.0)])
    manifest = json.loads(path.read_text());manifest['payloads']['weights']['sha256'] = '0'*64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError, match='weight payload seal'):
        run(ir, f.platform, clusters, constraints, assignment, routes, weights_path=path)


def test_reordered_board_coordinates_preserve_reference_decisions(case, tmp_path):
    from emuflow.platform import Platform
    f, ir, constraints, clusters, assignment, route_path, reference_clusters, reference = case
    value = f.platform.to_dict();value['fpgas'].reverse();platform = Platform.from_dict(value)
    expected, expected_report = refine_partition_hops(f.ir, platform, reference_clusters,
        constraints, reference, tmp_path/'reference', route_constraints_path=route_path,
        defer_semantic_contract=True)
    actual, report = refine_partition_hops(ir, platform, clusters, constraints, assignment,
        tmp_path/'native', route_constraints_path=route_path, defer_semantic_contract=True)
    assert dict(actual['cluster_assignment']) == dict(expected['cluster_assignment'])
    assert report['before'] == expected_report['before']
    assert report['after'] == expected_report['after']
    assert report['native_metrics'] == expected_report['native_metrics']


def test_inactive_devices_remain_legal_topology_relays(case, tmp_path):
    f, ir, constraints, clusters, _, route_path, _, _ = case
    constraints = dict(constraints, active_fpgas=['F0', 'F3'], min_used_fpgas=2)
    path = tmp_path/'two-active.txt';path.write_text('0\n1\n'*4)
    owners, _ = initialize(ir, f.platform, clusters, constraints, seed=0, executable=None,
                           solution_input=path)
    assignment = build_partition_assignment(ir, f.platform, clusters, constraints, owners,
        provider='test-provider', seed=0, _include_semantic_contract=False, _stream_semantic_records=True)
    route_path.write_text(json.dumps({'schema':'emuflow.system-route-constraints/v1', 'max_route_hops':3}))
    _, result = run(ir, f.platform, clusters, constraints, assignment,
                    load_route_constraints(route_path, f.platform), audit_only=True)
    assert result['after']['status'] == 'pass'
    assert result['after']['maximum_observed_hops'] == 3
    assert result['parts'] == ['F0', 'F3']


@pytest.mark.parametrize('failure', ['diagonal', 'fixed'])
def test_audit_request_rejects_malformed_board_or_fixed_identity(case, tmp_path, failure):
    import struct
    import subprocess
    import zlib
    import emuflow.partition_hops_native as host
    from emuflow.native_tools import resolve_native_executable
    f, ir, constraints, clusters, assignment, route_path, _, _ = case
    route_path.write_text(json.dumps({'schema':'emuflow.system-route-constraints/v1', 'max_route_hops':3}))
    captured = {}
    actual_process = host.NativeToolProcess
    class Capture(actual_process):
        def __init__(self, tool, arguments, **options):
            captured['request'] = Path(arguments[7]).read_bytes()
            captured['owner'] = arguments[9:]
            super().__init__(tool, arguments, **options)
    with patch.object(host, 'NativeToolProcess', Capture):
        run(ir, f.platform, clusters, constraints, assignment,
            load_route_constraints(route_path, f.platform), audit_only=True)
    data = bytearray(captured['request'][:-4])
    if failure == 'diagonal':
        offset = 16 + 3*56
        for _ in range(4):
            count = struct.unpack_from('<I', data, offset)[0];offset += 4
            for _ in range(count):
                length = struct.unpack_from('<I', data, offset)[0];offset += 4+length
        struct.pack_into('<i', data, offset+16, 1)
    else:
        assert struct.unpack_from('<I', data, len(data)-4)[0] == 0
        data[-4:] = struct.pack('<I', 1)
        data.extend(struct.pack('<II', len(clusters['clusters']), 0))
    data.extend(struct.pack('<I', zlib.crc32(data)))
    request = tmp_path/'bad.bin';request.write_bytes(data)
    output = tmp_path/'prior';output.mkdir();(output/'keep').write_text('prior generation')
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3',
        'audit-hops-from-primary', str(ir.path), str(clusters['clusters'].native_artifact),
        str(request), str(output), *captured['owner']], capture_output=True, text=True)
    assert result.returncode != 0
    assert ('distance diagonal' if failure == 'diagonal' else 'fixed cluster is unknown') in result.stdout+result.stderr
    assert (output/'keep').read_text() == 'prior generation'
    assert list(output.iterdir()) == [output/'keep']
