"""Bounded board policy and original-source host for the complete native stage."""
import json
import os
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.tool_runtime import NativeToolProcess


def _configuration_value(ir_path, platform_path, *, constraints_path=None,
        seed=0, min_used_fpgas=None, balance_tolerance=None, provider='patron', openroad=None,
        tritonpart_solution=None, net_weights_path=None, tritonpart_timeout_seconds=3600,
        tritonpart_seed_attempts=1, tritonpart_num_initial_solutions=50,
        tritonpart_num_best_initial_solutions=10, tritonpart_repair_min_used_fpgas=True,
        tritonpart_run_unweighted_baseline=False, route_constraints_path=None,
        max_cross_fpga_dependency_depth=0, static_exact_candidate_policy=None,
        timing_database_path=None, patron_max_moves=None, board_link_timing_db=None, timing_driven=True):
    from emuflow.platform.platform import Platform
    from emuflow.partition.structural_policy import _pattern_range_policy
    from emuflow.common.paths import DATA_ROOT
    from emuflow.common.errors import EmuFlowError
    from emuflow.timing.sta import sta_path_database_payload_path, partition_net_weights_payload_path
    from emuflow.timing.sta_weights_native import _selector
    require_native_schema('phase3-configuration-request-v1')
    tool = resolve_native_executable('emuflow-native')
    engine = ''
    if tritonpart_solution is None:
        try:
            engine = resolve_native_executable('openroad', openroad)
        except EmuFlowError:
            if openroad is not None:
                raise
    roles = [('board', Path(platform_path), 'boarddb-v1'),
        ('facts', DATA_ROOT / 'partition/structural-policy.json', 'phase3-configuration-source-v1'),
        ('native', Path(tool), 'external-tool-executable-v1')]
    if ir_path is not None:
        roles.append(('source', Path(ir_path), 'emuir-compact-v2'))
    for role, path, schema in (('constraints', constraints_path, 'partition-constraints-input-v1'),
            ('route_constraints', route_constraints_path, 'phase3-configuration-source-v1'),
            ('board_link_timing', board_link_timing_db, 'phase3-configuration-source-v1'),
            ('solution', tritonpart_solution, 'tritonpart-solution-v1')):
        if path is not None:
            roles.append((role, Path(path), schema))
    sources = [(role, bind_store_source(path.resolve(), schema)) for role, path, schema in roles]
    for role, path, selector_schema, payload_schema, payload_reader in (
            ('timing', timing_database_path, 'sta-path-selector-v1', 'timing-path-store-v1', sta_path_database_payload_path),
            ('weights', net_weights_path, 'net-weight-selector-v2', 'net-weight-store-v1', partition_net_weights_payload_path)):
        if path is not None:
            manifest, selector = _selector(Path(path).resolve(), selector_schema)
            payload = payload_reader(Path(path), manifest)
            if payload is None:
                raise ValidationError('native Phase 3 requires indexed timing/weights')
            sources.extend([('timing_selector' if role == 'timing' else 'weight_selector', selector),
                (role, bind_store_source(payload, payload_schema))])
    options = {'pattern_range_policy': _pattern_range_policy(), 'min_used_fpgas': min_used_fpgas,
        'balance_tolerance': balance_tolerance, 'provider': provider, 'seed': seed, 'openroad': engine,
        'timeout_seconds': tritonpart_timeout_seconds, 'seed_attempts': tritonpart_seed_attempts,
        'num_initial_solutions': tritonpart_num_initial_solutions,
        'num_best_initial_solutions': tritonpart_num_best_initial_solutions,
        'repair_min_used': tritonpart_repair_min_used_fpgas, 'unweighted_baseline': tritonpart_run_unweighted_baseline,
        'max_moves': patron_max_moves, 'max_cross_fpga_dependency_depth': max_cross_fpga_dependency_depth,
        'candidate_selection_policy': static_exact_candidate_policy or 'assignment-derived-acyclic-v2',
        'timing_driven': timing_driven}
    board_source = next(ref for role, ref in sources if role == 'board')
    board = Platform.load(board_source.path).to_dict()  # Finite BoardDB configuration only.
    value = {'schema': 'emuflow.phase3-configuration-request/v1', 'board': board, 'options': options,
        'sources': [{'role': role, 'path': str(ref.path), 'schema': ref.schema,
                     'generation': list(ref.generation)} for role, ref in sources]}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n'
    if len(encoded.encode()) > 8*1024*1024:
        raise ValidationError('native Phase 3 configuration exceeds bound')
    require_store_sources_unchanged(ref for role, ref in sources)
    return encoded, sources


def prepare_configuration_request(directory, platform_path, **options):
    """Bind finite settings; the native compiler binds its own IR/timing outputs."""
    encoded, sources = _configuration_value(None, platform_path, **options)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    try:
        request = directory / 'request.json'
        request.write_text(encoded, encoding='utf-8')
        require_store_sources_unchanged(ref for role, ref in sources)
        return request
    except BaseException:
        import shutil
        shutil.rmtree(directory)
        raise


def write_request(directory, ir_path, platform_path, **options):
    """Optional bounded host for the native configuration producer."""
    encoded, sources = _configuration_value(ir_path, platform_path, **options)
    directory = Path(directory).resolve()
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.phase3-configuration-host-', dir=directory.parent) as temporary:
        root = Path(temporary)
        settings, operation = root / 'settings.json', root / 'operation.json'
        settings.write_text(encoded, encoding='utf-8')
        tool = resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool, [tool, '--report', str(operation), 'phase3', 'prepare-policy', str(settings), str(directory)],
                environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native Phase 3 configuration failed: ' + result.stdout)
        if operation.stat().st_size > 256*1024:
            raise ValidationError('native Phase 3 configuration result exceeds bound')
        proof = json.loads(operation.read_text())
        outputs = proof.get('outputs', [])
        names = [('phase3-request.json', 'phase3-stage-request-v1'),
            ('candidate-policy.bin', 'candidate-policy-request-v1'),
            ('configuration-summary.json', 'phase3-configuration-summary-v1')]
        if proof.get('schema') != 'emuflow.native-stage-result/v1' or proof.get('status') != 'pass' or proof.get('stage') != 'phase3.prepare-policy' or len(outputs) != len(names):
            raise ValidationError('native Phase 3 configuration producer coverage disagrees')
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        for record, (name, schema) in zip(outputs, names):
            path = directory / name
            generation = record.get('generation', {})
            if record.get('path') != str(path) or record.get('schema') != schema or any(type(generation.get(k)) is not int for k in fields):
                raise ValidationError('native Phase 3 configuration output identity disagrees')
            require_store_sources_unchanged([StoreSource(path, schema, tuple(generation[k] for k in fields))])
        require_store_sources_unchanged(ref for role, ref in sources)
        return directory / 'phase3-request.json'


def run(output, ir_path, platform_path, **options):
    with tempfile.TemporaryDirectory(prefix='emuflow-phase3-request-') as temporary:
        request = write_request(Path(temporary)/'configuration', ir_path, platform_path, **options)
        operation = Path(temporary)/'operation.json'
        tool = resolve_native_executable('emuflow-native')
        with NativeToolProcess(tool, [tool, '--report', str(operation), 'phase3', 'run', str(request), str(output)],
                               environment=dict(os.environ), scratch_parent=Path(temporary)) as process:
            result = process.finish()
        if result.returncode:
            raise ValidationError('native Phase 3 failed: '+result.stdout)
        if operation.stat().st_size > 256*1024:
            raise ValidationError('native Phase 3 result exceeds bound')
        proof = json.loads(operation.read_text())
        if proof.get('stage') != 'phase3.run' or proof.get('status') != 'pass':
            raise ValidationError('native Phase 3 producer failed to bind its final output')
        outputs = proof.get('outputs')
        names = [('phase3_report.json', 'phase3-report-v1'), ('assignment.sqlite', 'assignment-index-v4'),
                 ('assignment.exact.bin', 'phase3-exact-data-v2'), ('assignment.json', 'assignment-selector-v4'),
                 ('phase3-runtime.json', 'phase3-runtime-v1')]
        if not isinstance(outputs, list) or len(outputs) != len(names):
            raise ValidationError('native Phase 3 output coverage disagrees')
        fields = ('device', 'inode', 'bytes', 'mtime_seconds', 'mtime_nanoseconds', 'ctime_seconds', 'ctime_nanoseconds')
        originals = []
        for record, (name, schema) in zip(outputs, names):
            path = (Path(output)/name).resolve();generation = record.get('generation', {})
            if (record.get('path') != str(path) or record.get('schema') != schema
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ValidationError('native Phase 3 original output binding disagrees')
            originals.append(StoreSource(path, schema, tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(originals)
        report = Path(output)/'phase3_report.json'
        if report.stat().st_size > 8*1024*1024:
            raise ValidationError('native Phase 3 report exceeds bound')
        value = json.loads(report.read_text())
        require_store_sources_unchanged(originals)
        if value.get('stage_implementation_id') != 'native-phase3-v1' or value.get('status') != 'pass':
            raise ValidationError('native Phase 3 final report identity disagrees')
        return value
