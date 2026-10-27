"""Current source-owned hop kernels, independent legality and retired API gates."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from emuflow.errors import ValidationError
from tests.native_partition_facts import (build_partition_assignment, validate_partition_artifacts_online)
from tests.native_partition_kernel_driver import run_hops as run
from tests.native_partition_kernel_driver import initialize
from emuflow.partition_topology import _shortest_hop_distances
from emuflow.routing import load_route_constraints, normalize_route_constraints
from emuflow.store_publication import bind_store_source
from tests.partition_hop_fixture import case as build_case, audit, line_platform, weights, weighted_objective


@pytest.fixture
def case(tmp_path, request, monkeypatch):
    from tests.native_build import emuflow_native
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(emuflow_native().parent.parent))
    return build_case(tmp_path, fixed=getattr(request, 'param', False))


def selected(f, owners):
    return build_partition_assignment(f.ir, f.platform, f.clusters, f.constraints,
        owners, provider='fixture', seed=7, _include_semantic_contract=False, _stream_semantic_records=True)


@pytest.mark.parametrize('weighted', [False, True])
def test_source_solver_and_independent_original_endpoint_audit(case, tmp_path, weighted):
    f = case
    routes = load_route_constraints(f.route_path, f.platform)
    weight_path, weights_by_net = None, {}
    if weighted:
        eligible = next(net['id'] for net in f.reference.value['nets']
            if any(p['instance'] is not None for p in net['drivers'])
            and any(p['instance'] is not None for p in net['sinks']))
        other = next(net['id'] for net in f.reference.value['nets'] if net['id'] != eligible)
        weight_path = weights(tmp_path, [(eligible, 1.0, 1, 3.25), (other, 0.0, 1, 8.0)])
        weights_by_net = {eligible: 3.25}
    owners, report = run(f.ir, f.platform, f.clusters, f.constraints, f.assignment,
                          routes, weights_path=weight_path)
    expected_before = audit(f.reference, f.clusters, f.assignment['cluster_assignment'],
                             _shortest_hop_distances(f.platform, routes), 1)
    expected_after = audit(f.reference, f.clusters, owners, _shortest_hop_distances(f.platform, routes), 1)
    assert report['before'] == expected_before
    assert report['after'] == expected_after
    assert expected_before['violating_net_sink_pairs'] > 0
    assert expected_after['violating_net_sink_pairs'] == 0
    assert report['move_count'] > 0
    assert report['move_count'] == report['native_metrics']['committed_moves']
    assert report['timing_weighted_nets'] == int(weighted)
    for prefix, assignment in (('initial', f.assignment['cluster_assignment']), ('final', owners)):
        expected_objective = weighted_objective(f.reference, f.clusters, assignment,
            _shortest_hop_distances(f.platform, routes), 1, weights_by_net)
        for field, value in expected_objective.items():
            assert report['native_metrics'][prefix+'_'+field] == value
    final = selected(f, owners)
    assert validate_partition_artifacts_online(f.platform, f.clusters, final)['status'] == 'pass'
    _, check = run(f.ir, f.platform, f.clusters, f.constraints, final, routes, audit_only=True)
    assert check['after'] == expected_after
    assert report['implementation_id'] == 'native-primary-hop-refinement-v1'


def test_zero_move_owner_preserves_original_assignment_contract(case):
    f = case
    routes = dict(load_route_constraints(f.route_path, f.platform), max_route_hops=3)
    original = f.assignment['cluster_assignment']
    contract = f.assignment.get('semantic_contract')
    with patch('tests.native_partition_facts.build_partition_assignment', side_effect=AssertionError('no-op rebuilt assignment')):
        owners, report = run(f.ir, f.platform, f.clusters, f.constraints, f.assignment, routes)
    assert report['move_count'] == 0
    assert dict(owners) == dict(original)
    assert f.assignment['cluster_assignment'] is original
    assert f.assignment.get('semantic_contract') is contract


def test_audit_rejects_original_violations_without_repair(case):
    f = case
    with pytest.raises(ValidationError, match='hard hop constraints'):
        run(f.ir, f.platform, f.clusters, f.constraints, f.assignment,
             load_route_constraints(f.route_path, f.platform), audit_only=True)


def test_corrupt_original_owner_rejected_without_reference_retry(case, tmp_path):
    f = case
    owner = f.assignment['cluster_assignment']
    data = bytearray(owner.native_initial_source.path.read_bytes())
    data[-1] ^= 1
    corrupt = tmp_path/'corrupt.bin'
    corrupt.write_bytes(data)
    owner.native_initial_source = bind_store_source(corrupt, 'patron-initial-columns-v1')
    with pytest.raises(ValidationError, match='checksum'):
        run(f.ir, f.platform, f.clusters, f.constraints, f.assignment,
             load_route_constraints(f.route_path, f.platform))


@pytest.mark.parametrize('case', [True], indirect=True)
def test_fixed_source_constraints_cannot_be_relaxed_by_hop_repair(case):
    f = case
    with pytest.raises(ValidationError, match='hard hop constraints'):
        run(f.ir, f.platform, f.clusters, f.constraints, f.assignment,
             load_route_constraints(f.route_path, f.platform))


def test_native_owner_replay_is_deterministic_and_rejects_changed_weights(case, tmp_path):
    f = case
    routes = load_route_constraints(f.route_path, f.platform)
    a, sa = run(f.ir, f.platform, f.clusters, f.constraints, f.assignment, routes)
    b, sb = run(f.ir, f.platform, f.clusters, f.constraints, f.assignment, routes)
    assert a.native_initial_source.path.read_bytes() == b.native_initial_source.path.read_bytes()
    assert sa['native_metrics'] == sb['native_metrics']
    names = [net['id'] for net in f.reference.value['nets']]
    path = weights(tmp_path, [(names[0], 1.0, 1, 2.0)])
    manifest = json.loads(path.read_text())
    manifest['payloads']['weights']['sha256'] = '0'*64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError, match='weight payload seal'):
        run(f.ir, f.platform, f.clusters, f.constraints, f.assignment, routes, weights_path=path)


def test_reordered_board_coordinates_preserve_independent_hop_legality(case):
    from emuflow.platform import Platform
    f = case
    value = f.platform.to_dict()
    value['fpgas'].reverse()
    platform = Platform.from_dict(value)
    routes = load_route_constraints(f.route_path, platform)
    owners, report = run(f.ir, platform, f.clusters, f.constraints, f.assignment, routes)
    assert report['before'] == audit(f.reference, f.clusters, f.assignment['cluster_assignment'],
                                     _shortest_hop_distances(platform, routes), 1)
    assert report['after'] == audit(f.reference, f.clusters, owners,
                                    _shortest_hop_distances(platform, routes), 1)
    assert report['after']['status'] == 'pass'
    repeated, second = run(f.ir, platform, f.clusters, f.constraints, f.assignment, routes)
    assert dict(repeated) == dict(owners)
    assert second['native_metrics'] == report['native_metrics']


def test_inactive_devices_remain_legal_topology_relays(case, tmp_path):
    f = case
    constraints = dict(f.constraints, active_fpgas=['F0', 'F3'], min_used_fpgas=2)
    path = tmp_path/'two-active.txt'
    path.write_text('0\n1\n'*4)
    owners, _ = initialize(f.ir, f.platform, f.clusters, constraints, seed=0,
                           executable=None, solution_input=path)
    assignment = build_partition_assignment(f.ir, f.platform, f.clusters, constraints, owners,
        provider='fixture', seed=0, _include_semantic_contract=False, _stream_semantic_records=True)
    routes = dict(load_route_constraints(f.route_path, f.platform), max_route_hops=3)
    _, result = run(f.ir, f.platform, f.clusters, constraints, assignment, routes, audit_only=True)
    assert result['after']['status'] == 'pass'
    assert result['after']['maximum_observed_hops'] == 3
    assert result['parts'] == ['F0', 'F3']


def test_board_distances_preserve_simplex_direction_and_unavailable_links():
    from emuflow.platform import Platform
    value = line_platform().to_dict()
    for link in value['links']:
        link['direction'] = 'unidirectional'
    platform = Platform.from_dict(value)
    distances = _shortest_hop_distances(platform, normalize_route_constraints(None, platform))
    assert distances['F0']['F2'] == 2 and distances['F2']['F0'] is None
    distances = _shortest_hop_distances(platform, normalize_route_constraints({
        'schema': 'emuflow.system-route-constraints/v1', 'unavailable_links': ['L12']}, platform))
    assert distances['F0']['F2'] is None


def test_retired_python_hop_producer_is_unavailable():
    assert importlib.util.find_spec('emuflow.partition_hops') is None


@pytest.mark.parametrize('failure', ['diagonal', 'fixed'])
def test_audit_request_rejects_malformed_board_or_fixed_identity(case, tmp_path, failure):
    import struct
    import subprocess
    import zlib
    import tests.native_partition_kernel_driver as host
    from emuflow.native_tools import resolve_native_executable
    f = case
    ir, constraints, clusters, assignment, route_path = f.ir, f.constraints, f.clusters, f.assignment, f.route_path
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


def test_changed_original_source_generation_is_rejected(case):
    f = case
    path = f.ir.path
    path.chmod(path.stat().st_mode ^ 0o100)
    with pytest.raises(ValidationError, match='generation'):
        run(f.ir, f.platform, f.clusters, f.constraints, f.assignment,
             load_route_constraints(f.route_path, f.platform))


def test_retired_numeric_hop_cli_rejects_before_overwriting_results(case, tmp_path):
    import subprocess
    from emuflow.native_tools import resolve_native_executable
    output = tmp_path/'prior'
    output.write_bytes(b'prior generation')
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3',
        'refine-hops', str(tmp_path/'retired-input'), str(output)], capture_output=True, text=True)
    assert result.returncode == 2
    assert output.read_bytes() == b'prior generation'


def test_complete_stage_still_enforces_hop_contract(case, tmp_path):
    from emuflow.phase3_native import run as run_phase3
    f = case
    board = tmp_path/'board.json'
    board.write_text(json.dumps(f.platform.to_dict()))
    limits = tmp_path/'constraints.json'
    limits.write_text(json.dumps(f.constraints))
    report = run_phase3(tmp_path/'phase3', f.ir.path, board, provider='tritonpart',
        constraints_path=limits, tritonpart_solution=f.solution, route_constraints_path=f.route_path)
    assert report['status'] == 'pass'
    assert report['hop_refinement']['after']['violating_net_sink_pairs'] == 0
    assert report['hop_refinement']['after']['status'] == 'pass'
    assert report['hop_refinement']['before']['violating_net_sink_pairs'] > 0
    assert report['hop_refinement']['move_count'] > 0


def test_unbound_python_ownership_cannot_retry_a_retired_protocol(case):
    f = case
    unbound = dict(f.assignment, cluster_assignment=dict(f.assignment['cluster_assignment']))
    with pytest.raises(ValidationError, match='source-bound primary ownership'):
        run(f.ir, f.platform, f.clusters, f.constraints, unbound,
             load_route_constraints(f.route_path, f.platform))
