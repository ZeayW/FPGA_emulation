"""Complete native stage validation, source binding and host-entry parity."""
import json
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from unittest.mock import patch
import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow_tools.stage_clients.partition import run_phase3, validate_phase3_indexed
from emuflow_tools.stage_clients.partition_native import run as native_run
from emuflow.partition.phase3_storage import read_phase3_assignment_view
from tests.test_phase3_pressure_native import inputs


@contextmanager
def assignment_view(path):
    value = read_phase3_assignment_view(path, include_instance_assignment=True)
    try:
        yield value
    finally:
        value['instance_assignment'].close()


@pytest.mark.parametrize('name', ['partition_init_native', 'partition_hops_native',
    'phase3_publication_native', 'route_seal_native', 'partition_inputs', 'phase3_pressure_native', 'patron_policy',
    'phase3_cluster_catalog', 'replication'])
def test_retired_stage_wrappers_are_not_production_imports(name):
    assert importlib.util.find_spec('emuflow.'+name) is None


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


def test_native_phase3_runs_complete_stage_with_independent_record_validation(inputs, tmp_path):
    ir, board, config = options(inputs, tmp_path)
    actual = tmp_path/'native-stage'
    import emuflow.partition.partition as partition
    assert not hasattr(partition, 'build_clusters')
    with patch('emuflow.ir.ir.EmuIR.load', side_effect=AssertionError('Python graph import')):
        report = native_run(actual, ir.path, board, **config)
    assert report['stage_implementation_id'] == 'native-phase3-v1'
    assert report['algorithm_validation']['native_input_transport'] == 'native-primary-store-model-v2'
    # These validators reconstruct legality from source IR and final records,
    # independently of the producer's cluster selection and timing decision.
    assert validate_phase3_indexed(ir.path, board, actual/'phase3_report.json', actual/'assignment.sqlite')['status'] == 'pass'
    from emuflow.flow.cli import main
    # The independent positive record validation above remains mandatory.
    # Production exposes complete compile only; its old audit CLI is retired.
    with pytest.raises(SystemExit):
        main(['partition', 'validate', str(actual/'assignment.sqlite'),
              '--phase3-report', str(actual/'phase3_report.json'),
              '--ir', str(ir.path), '--platform', str(board)])
    with assignment_view(actual/'assignment.json') as generated:
        assert len(generated['instance_assignment']) == len(inputs[0][0].value['instances'])


@pytest.mark.parametrize('damage', ['version', 'generation', 'capacity', 'timing-seal', 'solution', 'existing'])
def test_complete_stage_rejects_invalid_sources_without_publishing(inputs, tmp_path, damage):
    import subprocess
    from emuflow_tools.stage_clients.partition_native import write_request
    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
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
    from emuflow_tools.stage_clients.partition import run_phase3 as public
    ir, board, config = options(inputs, tmp_path)
    import emuflow.partition.partition as partition
    for name in ('build_clusters', 'build_partition_assignment', 'validate_partition_artifacts_online'):
        assert not hasattr(partition, name)
    with patch('emuflow.ir.ir.EmuIR.load', side_effect=AssertionError('Python graph import')):
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
    with assignment_view(expected/'assignment.json') as a, assignment_view(actual/'assignment.json') as b:
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
    from emuflow_tools.stage_clients.partition_native import write_request
    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
    ir, board, config = options(inputs, tmp_path)
    pid_file = tmp_path/'native-child.pid'
    child = tmp_path/'stalled-native'
    child.write_text('#!'+sys.executable+'\nimport os,time,pathlib\npathlib.Path('+repr(str(pid_file))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
    child.chmod(0o755)
    request = write_request(tmp_path/'deadline-configuration', ir.path, board, **config)
    # This deadline includes source/configuration setup before child launch.
    # Leave enough startup budget for loaded regression hosts, then prove the
    # deliberately stalled child is still killed by the stage deadline.
    value = json.loads(request.read_text());value['timeout_seconds'] = 10
    value['inputs']['native'] = {'path':str(child), 'schema':'external-tool-executable-v1', 'generation':list(native_file_generation(child))}
    request.write_text(json.dumps(value));output = tmp_path/'timed-out'
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3', 'run', str(request), str(output)], capture_output=True, text=True, timeout=30)
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
    with assignment_view(expected/'assignment.json') as a, assignment_view(actual/'assignment.json') as b:
        assert b['provider'] == a['provider'] == 'tritonpart-openroad-hypergraph-v1'
        assert generated['provider'] == reference['provider']
        assert b['semantic_contract_sha256'] == a['semantic_contract_sha256']
        assert dict(b['instance_assignment']) == dict(a['instance_assignment'])
    assert generated['validation'] == reference['validation']
