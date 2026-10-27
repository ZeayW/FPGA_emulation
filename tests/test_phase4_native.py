"""Whole-stage records, source binding and publication failures for Phase 4."""
from tests.phase3_store_fixture import write_assignment_index_fixture
import json
import sqlite3
from contextlib import closing
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.routing.phase4 import run_phase4
from emuflow.routing.phase4_native import prepare_policy, run_native_phase4
from emuflow.routing.routing import demands_from_assignment
from emuflow.routing.system_route_storage import read_system_routes
from emuflow.scheduling.timing_routing import load_sta_paths, validate_native_system_routes
from tests.native_build import emuflow_native
from tests.native_schedule_case import _case

# Canonical record hashes from the last production coordinator, before its
# retirement. The small inputs are explicit below; the old flow is not retained.
GOLDEN = {
    (False, False): '0988ecf9133c58d057132fe5cf31a611703317f64e7c7d8d08abc636b2b6b141',
    (False, True): '11f25934ae84ebf4cae69ad94c4c2d11090574dda7481ab51103fb97780a1b1d',
    (True, False): 'bb4e71c7b7649d7cd124878aa612a717aacb3d19acd5ce631a29882f0e865aed',
    (True, True): 'd2486db47e67e3d50ceba3148778fd949124c8f04d9905633da0ecbc27b4ea24',
    ('banks', False): '5dfc1bcd60bd8d54fa2f985e3d892342ed69592fd5050de39afb31159a86d3f8',
    ('banks', True): '230f7afa7fb2be24bf4c6dee2913c68ae5a8e015b48e91dbf19bdf07008abf49',
    ('tree', False): 'a8d222112dba8a446019fc3e72c52a2199dc73d7a43594196639724210fdba30',
    ('tree', True): '557252ddc52d356086bf0b1c96555c0854b1e38918a67e2d4b21fa6d099e8406',
}


def inputs(root, mode=False):
    case, _ = _case(root, mode, False)
    assignment = root / 'assignment.sqlite'
    write_assignment_index_fixture(assignment, case.ir, case._phase5_assignment)
    board = root / 'board.json'
    board.write_text(json.dumps(case.platform.to_dict()))
    demands = demands_from_assignment(case._phase5_assignment, case.platform)
    timing = root / 'paths.json'
    timing.write_text(json.dumps({'schema': 'emuflow.sta-paths/v1',
        'design': case._phase5_assignment['design'],
        'paths': [{'id': f'p{i}', 'clock_domain': 'clk', 'clock_period_ns': 10.0,
                   'slack_ns': 3.0-i*4, 'fixed_delay_ns': 2.0,
                   'cut_nets': [demand['net']]} for i, demand in enumerate(demands)]}))
    return case, assignment, board, timing


@pytest.mark.parametrize('mode', [False, True, 'banks', 'tree'])
@pytest.mark.parametrize('driven', [False, True])
@pytest.mark.parametrize('normalized', [False, True])
def test_complete_native_stage_preserves_records_hashes_and_independent_checks(tmp_path, mode, driven, normalized):
    case, assignment, board, timing = inputs(tmp_path, mode)
    paths = load_sta_paths(timing, demands_from_assignment(case._phase5_assignment, case.platform))
    if normalized:
        timing.write_text(json.dumps(paths))
    # The public API hands off references. It cannot invoke the independent
    # Python validator or reconstruct route-scale input/output objects.
    with patch('emuflow.routing.phase4.validate_native_system_routes', side_effect=AssertionError('Python stage validator')):
        report = run_phase4(assignment, board, tmp_path/'native',
            timing_paths_path=timing, frame_slots=16, max_iterations=3,
            timing_driven=driven, router=str(emuflow_native()))
    assert report['stage_implementation_id'] == 'native-phase4-v1'
    assert report['route_storage']['logical_sha256'] == GOLDEN[mode, driven]
    routes = read_system_routes(tmp_path/'native/routes.json')
    independent = validate_native_system_routes(case._phase5_assignment, case.platform, routes, paths)
    assert report['validation'] == independent
    assert set(p.name for p in (tmp_path/'native').iterdir()) == set(report['artifacts'].values())
    assert not list(tmp_path.glob('.native-phase4-*'))
    assert not list(tmp_path.glob('.phase4-stage-*'))


@pytest.mark.parametrize('damage', ['assignment-generation', 'board-generation', 'timing-generation',
                                   'duplicate-path', 'truncated-path', 'unknown-cut', 'normalized-slack', 'normalized-signature'])
def test_stage_rejects_changed_or_malformed_sources_without_publication(tmp_path, damage):
    case, assignment, board, timing = inputs(tmp_path)
    if damage in {'duplicate-path', 'truncated-path', 'unknown-cut', 'normalized-slack', 'normalized-signature'}:
        value = json.loads(timing.read_text())
        if damage == 'duplicate-path': value['paths'].append(value['paths'][0])
        elif damage == 'unknown-cut': value['paths'][0]['cut_nets'] = ['missing']
        elif damage in {'normalized-slack', 'normalized-signature'}:
            value = load_sta_paths(timing, demands_from_assignment(case._phase5_assignment, case.platform))
            if damage == 'normalized-slack': value['paths'][0]['normalized_slack'] += 0.25
            else: value['paths'][0]['cut_signature'] = None
        text = json.dumps(value)
        timing.write_text(text[:-3] if damage == 'truncated-path' else text)
    policy = prepare_policy(tmp_path/'policy', assignment, board,
        timing_paths=timing, frame_slots=16, max_iterations=3)
    if damage.endswith('-generation'):
        source = {'assignment-generation': assignment, 'board-generation': board,
                  'timing-generation': timing}[damage]
        # Replace with identical bytes: a content-only check must not accept a
        # different producer generation bound by the frozen stage policy.
        replacement = source.with_suffix('.replacement')
        replacement.write_bytes(source.read_bytes()); replacement.replace(source)
    output = tmp_path/'native'
    result = subprocess.run([str(emuflow_native()), 'phase4', 'run', str(assignment),
        str(board), str(policy), str(output), str(timing)], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
    assert not output.exists()
    assert not list(tmp_path.glob('.phase4-stage-*'))


def test_existing_generation_is_never_overwritten(tmp_path):
    _, assignment, board, timing = inputs(tmp_path)
    output = tmp_path/'native'
    report = run_native_phase4(assignment, board, output, timing_paths=timing,
        frame_slots=16, max_iterations=3, executable=str(emuflow_native()))
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    with pytest.raises(ValidationError, match='fresh stage directory'):
        run_native_phase4(assignment, board, output, timing_paths=timing,
            frame_slots=16, max_iterations=3, executable=str(emuflow_native()))
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
    assert report['status'] == 'pass'


def test_native_route_adapter_is_not_a_second_production_entry(tmp_path):
    from emuflow.common.errors import EmuFlowError
    from emuflow.flow.native_compile_adapter import execute
    with pytest.raises(EmuFlowError, match='C[+][+] Phase 4'):
        execute('route', tmp_path/'missing', None, tmp_path/'output', 'run')


@pytest.mark.parametrize('payload', [None, {}, {'schema': 'emuflow.partition-assignment/v1'}])
def test_json_assignment_never_selects_a_sibling_artifact_implicitly(tmp_path, payload):
    _, assignment, board, timing = inputs(tmp_path)
    selector = assignment.with_suffix('.json')
    if payload is not None:
        selector.write_text(json.dumps(payload))
    with pytest.raises(ValidationError, match='explicit assignment SQLite'):
        run_native_phase4(selector, board, tmp_path/'native', timing_paths=timing,
                          executable=str(emuflow_native()))
    assert not (tmp_path/'native').exists()


@pytest.mark.parametrize('key,value', [
    ('semantic_contract_schema', 'emuflow.static-exact-combinational-cut/v99'),
    ('semantic_contract_schema', None),
    ('semantic_contract_sha256', 'bad'),
    ('semantic_contract_sha256', 'G'*64),
    ('semantic_contract_sha256', None),
])
def test_native_stage_rejects_invalid_semantic_identity_with_unchanged_cut_seal(tmp_path, key, value):
    _, assignment, board, timing = inputs(tmp_path)
    with closing(sqlite3.connect(assignment)) as db, db:
        db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(value), key))
    with pytest.raises(ValidationError, match='semantic contract'):
        run_native_phase4(assignment, board, tmp_path/'native', timing_paths=timing,
            frame_slots=16, max_iterations=3, executable=str(emuflow_native()))
    assert not (tmp_path/'native').exists()
    assert not list(tmp_path.glob('.phase4-stage-*'))


def test_report_generation_is_checked_after_decoding(tmp_path):
    _, assignment, board, timing = inputs(tmp_path)
    read = Path.read_text
    changed = []
    def replace_during_read(path, *args, **kwargs):
        value = read(path, *args, **kwargs)
        if path.name == 'phase4_report.json':
            replacement = path.with_suffix('.replacement')
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            changed.append(path)
        return value
    with patch.object(Path, 'read_text', replace_during_read):
        with pytest.raises(ValidationError, match='generation changed'):
            run_native_phase4(assignment, board, tmp_path/'native', timing_paths=timing,
                frame_slots=16, max_iterations=3, executable=str(emuflow_native()))
    assert changed


@pytest.mark.parametrize('flag', ['tree_edge_sum_tdm', 'hard_sll_capacity'])
@pytest.mark.parametrize('value', ['false', 0])
def test_native_policy_rejects_non_boolean_flags(tmp_path, flag, value):
    _, assignment, board, timing = inputs(tmp_path)
    policy = prepare_policy(tmp_path/'policy', assignment, board,
        timing_paths=timing, frame_slots=16, max_iterations=3)
    payload = json.loads(policy.read_text())
    payload['constraints'][flag] = value
    policy.write_text(json.dumps(payload))
    output = tmp_path/'native'
    result = subprocess.run([str(emuflow_native()), 'phase4', 'run', str(assignment),
        str(board), str(policy), str(output), str(timing)], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'policy boolean' in result.stderr
    assert not output.exists()


def test_raw_null_signature_retains_normalization_semantics(tmp_path):
    _, assignment, board, timing = inputs(tmp_path)
    payload = json.loads(timing.read_text())
    for row in payload['paths']: row['cut_signature'] = None
    timing.write_text(json.dumps(payload))
    report = run_native_phase4(assignment, board, tmp_path/'native', timing_paths=timing,
        frame_slots=16, max_iterations=3, executable=str(emuflow_native()))
    assert report['route_storage']['logical_sha256'] == GOLDEN[False, True]
