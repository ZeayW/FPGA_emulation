"""Complete stage parity, source binding and native controller integration."""
import json
from pathlib import Path
from unittest.mock import patch
import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.phase3 import _run_phase3_reference as run_phase3
from emuflow.phase3_native import run as native_run
from tests.test_phase3_pressure_native import inputs


def options(inputs, tmp_path):
    args, initial, _ = inputs
    ir, platform, clusters, constraints, _, routes = args
    board, limits, routing, solution = [tmp_path/name for name in ('board.json','constraints.json','routing.json','solution.txt')]
    board.write_text(json.dumps(platform.to_dict()));limits.write_text(json.dumps(constraints));routing.write_text(json.dumps(routes))
    part_ids = tuple(fpga.id for fpga in platform.fpgas)
    solution.write_text(''.join(str(part_ids.index(initial['cluster_assignment'][f'c{i:06d}']))+'\n' for i in range(len(clusters['clusters']))))
    return ir, board, dict(constraints_path=limits, tritonpart_solution=solution,
        timing_database_path=tmp_path/'timing.json', route_constraints_path=routing,
        patron_max_moves=12)


def test_native_phase3_runs_complete_stage_and_preserves_final_records(inputs, tmp_path):
    ir, board, config = options(inputs, tmp_path)
    reference = tmp_path/'reference'
    expected_report = run_phase3(ir.path, board, reference, **config)
    actual = tmp_path/'native-stage'
    with patch('emuflow.phase3.run_tritonpart', side_effect=AssertionError('Python initializer')), \
         patch('emuflow.partition.build_clusters', side_effect=AssertionError('Python stage clusters')):
        report = native_run(actual, ir.path, board, **config)
    expected = read_json(reference/'assignment.json');generated = read_json(actual/'assignment.json')
    assert dict(generated['instance_assignment']) == dict(expected['instance_assignment'])
    assert list(generated['cut_nets']) == list(expected['cut_nets'])
    assert generated['semantic_contract_sha256'] == expected['semantic_contract_sha256']
    assert generated['metrics'] == expected['metrics']
    assert generated['constraints'] == expected['constraints']
    assert generated['partitions'] == expected['partitions']
    assert list(read_json(actual/'clusters.json')['clusters']) == list(read_json(reference/'clusters.json')['clusters'])
    assert report['validation'] == expected_report['validation']
    assert generated['provider'] == expected['provider']
    assert generated.get('provider_metadata', {}).get('initial_provider') == expected.get('provider_metadata', {}).get('initial_provider')
    assert report['stage_implementation_id'] == 'native-phase3-v1'


@pytest.mark.parametrize('damage', ['version', 'generation', 'capacity', 'timing-seal', 'solution', 'existing'])
def test_complete_stage_rejects_invalid_sources_without_publishing(inputs, tmp_path, damage):
    import subprocess
    from emuflow.phase3_native import write_request
    from emuflow.native_tools import native_file_generation, resolve_native_executable
    ir, board, config = options(inputs, tmp_path)
    request = write_request(tmp_path/'configuration', ir.path, board, **config)
    value = json.loads(request.read_text())
    output = tmp_path/'rejected'
    if damage == 'version': value['schema'] = 'emuflow.phase3-stage/v99'
    elif damage == 'generation': value['inputs']['source']['generation'][2] += 1
    elif damage == 'capacity': value['partition_configuration']['board']['fpgas'][0]['effective_capacity']['lut'] += 1
    elif damage == 'timing-seal':
        path = Path(value['inputs']['timing_selector']['path'])
        data = json.loads(path.read_text());data['payloads']['paths']['sha256'] = '0'*64
        path.write_text(json.dumps(data))
        value['inputs']['timing_selector']['generation'] = list(native_file_generation(path))
    elif damage == 'solution':
        path = Path(value['inputs']['solution']['path']);path.write_text('0\n')
        value['inputs']['solution']['generation'] = list(native_file_generation(path))
    else:
        output.mkdir();(output/'prior').write_bytes(b'previous generation')
    request.write_text(json.dumps(value))
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3', 'run', str(request), str(output)], capture_output=True, text=True)
    assert result.returncode != 0
    assert ('"status":"fail"' in result.stdout+result.stderr)
    if damage == 'existing':
        assert list(output.iterdir()) == [output/'prior']
        assert (output/'prior').read_bytes() == b'previous generation'
    else:
        assert not output.exists()
    assert not list(tmp_path.glob('.phase3-stage-*'))


def test_public_compact_api_uses_complete_native_stage_without_python_phase_bodies(inputs, tmp_path):
    from contextlib import ExitStack
    from emuflow.phase3 import run_phase3 as public
    ir, board, config = options(inputs, tmp_path)
    with ExitStack() as guards:
        for name in ('build_clusters', 'run_tritonpart', 'run_partition_pressure_native',
                     'build_partition_assignment', 'validate_partition_artifacts_online'):
            guards.enter_context(patch('emuflow.phase3.'+name, side_effect=AssertionError('Python phase body: '+name)))
        output = tmp_path/'public'
        report = public(ir.path, board, output, **config)
    assert report['stage_implementation_id'] == 'native-phase3-v1'
    runtime = read_json(output/'phase3-runtime.json')
    assert runtime['native_stage']['implementation_id'] == 'native-phase3-v1'
    assert runtime['observations']['native_phase3_stage'] == 1
    assert runtime['native_stage']['tools']


def test_native_phase3_retains_initializer_engine_failure_boundary(inputs, tmp_path):
    from tests.test_partition_init_native import engine
    ir, board, config = options(inputs, tmp_path)
    executable, capture = engine(tmp_path, [(0, 1)], fail=True)
    config.pop('tritonpart_solution')
    config['openroad'] = str(executable)
    output = tmp_path/'failed-engine'
    with pytest.raises(ValidationError, match='OpenROAD/TritonPart failed'):
        native_run(output, ir.path, board, **config)
    assert not output.exists()
    assert list(capture.iterdir())
    assert not list(tmp_path.glob('.phase3-stage-*'))


@pytest.mark.parametrize('budget', [None, 0])
def test_complete_stage_preserves_default_and_zero_search_effort(inputs, tmp_path, budget):
    ir, board, config = options(inputs, tmp_path);config['patron_max_moves'] = budget
    expected = tmp_path/'reference-effort';actual = tmp_path/'native-effort'
    run_phase3(ir.path, board, expected, **config)
    native_run(actual, ir.path, board, **config)
    a, b = read_json(expected/'assignment.json'), read_json(actual/'assignment.json')
    assert a['semantic_contract_sha256'] == b['semantic_contract_sha256']
    assert dict(a['instance_assignment']) == dict(b['instance_assignment'])
    assert a['provider'] == b['provider']


def test_complete_stage_supervises_original_openroad_initializer(inputs, tmp_path):
    from tests.test_partition_init_native import engine
    ir, board, config = options(inputs, tmp_path)
    executable, capture = engine(tmp_path, [(0, 1)])
    expected = tmp_path/'reference-engine';actual = tmp_path/'native-engine'
    config.pop('tritonpart_solution');config['openroad'] = str(executable)
    run_phase3(ir.path, board, expected, **config)
    native_run(actual, ir.path, board, **config)
    assert read_json(expected/'assignment.json')['semantic_contract_sha256'] == read_json(actual/'assignment.json')['semantic_contract_sha256']
    assert list(capture.iterdir())


def test_complete_stage_keeps_search_deadline_and_reaps_native_child(inputs, tmp_path):
    import os
    import subprocess
    import sys
    from emuflow.phase3_native import write_request
    from emuflow.native_tools import native_file_generation, resolve_native_executable
    ir, board, config = options(inputs, tmp_path)
    pid_file = tmp_path/'native-child.pid'
    child = tmp_path/'stalled-native'
    child.write_text('#!'+sys.executable+'\nimport os,time,pathlib\npathlib.Path('+repr(str(pid_file))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
    child.chmod(0o755)
    request = write_request(tmp_path/'deadline-configuration', ir.path, board, **config)
    value = json.loads(request.read_text());value['timeout_seconds'] = 1
    value['inputs']['native'] = {'path':str(child), 'schema':'external-tool-executable-v1', 'generation':list(native_file_generation(child))}
    request.write_text(json.dumps(value));output = tmp_path/'timed-out'
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3', 'run', str(request), str(output)], capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert any(word in result.stdout+result.stderr for word in ('timed out', 'time limit', 'timeout'))
    assert pid_file.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert not output.exists()
    assert not list(tmp_path.glob('.phase3-stage-*'))


def test_complete_stage_preserves_initializer_provider_when_selected_directly(inputs, tmp_path):
    ir, board, config = options(inputs, tmp_path);config['provider'] = 'tritonpart'
    expected = tmp_path/'reference-initializer';actual = tmp_path/'native-initializer'
    reference = run_phase3(ir.path, board, expected, **config)
    generated = native_run(actual, ir.path, board, **config)
    a, b = read_json(expected/'assignment.json'), read_json(actual/'assignment.json')
    assert b['provider'] == a['provider'] == 'tritonpart-openroad-hypergraph-v1'
    assert generated['provider'] == reference['provider']
    assert b['semantic_contract_sha256'] == a['semantic_contract_sha256']
    assert dict(b['instance_assignment']) == dict(a['instance_assignment'])
    assert generated['validation'] == reference['validation']
