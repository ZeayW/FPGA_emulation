"""Small contract gates for bounded Phase 2 preparation and clock polarity."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from emuflow.common.errors import ValidationError
from emuflow_tools.timing.indexed_timing import IndexedTimingView, _NEGATIVE_TIMING_CELLS, _TimingTypes, _timing_parameter_is_one
from emuflow_tools.timing.phase2_native import _POLICY_LIMIT, _encode_policy, prepare_policy


PARAMETERS = [
    (0, False), (1, True), (True, False), (False, False), (1.0, False),
    (None, False), ('', False), ('01', True), ('10', False),
    ('0001', True), (' 1 ', True), ('\t+0001\r\n', True),
    ('0_1', True), ('1_0', False), ('1__0', False), ('_1', False),
    ('1_', False), ('+_1', False), ('+0_1', True), ('-1', False),
    ('1e0', False), ('1.0', False), ('1\x00suffix', False),
    ('\u0661', False), ('\u00a01', False), ('0' * 10000 + '1', True),
]


def timing_view():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        CREATE TABLE payloads(seq INTEGER PRIMARY KEY,value TEXT);
        CREATE TABLE cell_records(seq INTEGER PRIMARY KEY,id TEXT,type TEXT,
                                  parameters_seq INTEGER,constants_seq INTEGER);
        INSERT INTO payloads VALUES(0,'[]');
    ''')
    for seq, (value, _expected) in enumerate(PARAMETERS):
        connection.execute('INSERT INTO payloads VALUES(?,?)',
                           (seq + 1, json.dumps({'IS_C_INVERTED': value})))
        connection.execute('INSERT INTO cell_records VALUES(?,?,?,?,0)',
                           (seq, 'ff' + str(seq), 'FDRE', seq + 1))
    connection.execute('INSERT INTO cell_records VALUES(?,?,?,?,0)',
                       (len(PARAMETERS), 'lut', 'LUT1', None))
    view = object.__new__(IndexedTimingView)
    view.connection = connection
    view.value = {'design': {'name': 'fixture'}, 'ports': [], 'clocks': [],
                  'instances': SimpleNamespace(), 'nets': SimpleNamespace()}
    view.pins_for = lambda _seq: ({('D', 0)}, {('Q', 0)})
    return view


def test_sql_clock_polarity_matches_native_integer_one_contract():
    view = timing_view()
    try:
        actual = {row[0] for row in view.connection.execute(
            _NEGATIVE_TIMING_CELLS + 'SELECT seq FROM negative_timing_cells')}
        expected = {seq for seq, (_value, wanted) in enumerate(PARAMETERS) if wanted}
        assert actual == expected
        assert [_timing_parameter_is_one(value) for value, _expected in PARAMETERS] == [
            expected for _value, expected in PARAMETERS]
        assert view.used_timing_types(_TimingTypes(view)) == ['FDRE', 'FDRE__NEG', 'LUT1']
    finally:
        view.close()


def test_representatives_do_not_iterate_python_instance_rows():
    view = timing_view()
    try:
        # The complete instances/nets slots intentionally are not iterable.
        graph = view.representative_graph()
        assert [item['id'] for item in graph.value['instances']] == [
            'ff0', 'ff1', 'lut']
        assert len(graph.value['nets']) == 6
    finally:
        view.close()


def test_phase2_policy_bound_includes_the_final_newline():
    value = {'liberty': ''}
    empty_size = len(_encode_policy(value).encode())
    value['liberty'] = 'x' * (_POLICY_LIMIT - empty_size)
    assert len(_encode_policy(value).encode()) == _POLICY_LIMIT
    value['liberty'] += 'x'
    with pytest.raises(ValidationError, match='configuration exceeds bound'):
        _encode_policy(value)


@pytest.mark.parametrize('architecture,expected_mode', [(False, 0), (True, 1)])
def test_bounded_policy_preserves_default_and_architecture_modes(tmp_path, phase2_runtime,
                                                                architecture, expected_mode):
    from unittest.mock import patch
    source, records = _counter_stage_input(tmp_path)
    executable, _trace = _fixed_opensta(tmp_path, records)
    database = _architecture_stage_contract(tmp_path) if architecture else None
    with patch('emuflow_tools.timing.indexed_timing.IndexedTimingView', side_effect=AssertionError('Python timing graph')), \
         patch('emuflow_tools.stage_clients.opensta.render_opensta_liberty', side_effect=AssertionError('Python Liberty producer')), \
         patch('emuflow_tools.stage_clients.opensta.require_opensta_engine', side_effect=AssertionError('Python engine qualification')):
        policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10.0},
            executable=str(executable), architecture_timing_db_path=database)
    value = json.loads(policy.read_text())
    assert value['schema'] == 'emuflow.phase2-policy/v1'
    assert value['configuration']['export_mode'] == expected_mode
    assert value['configuration']['clock_map'] == {'clk': 10.0}
    assert value['configuration']['timing_io'] is None
    assert value['configuration']['timing_io_sha256'] is None
    assert all(len(row['generation']) == 7 for row in value['sources'])
    assert len(value['configuration']['model']['cell_types']) <= 4096
    assert ('FDRE__NEG' in value['configuration']['model']['cell_types']) is architecture
    assert not (tmp_path / 'policy' / 'mapped.v').exists()
    assert not list((tmp_path / 'policy').glob('.phase2-policy-host-*'))


# These fixtures exercise the retained native stage boundary. They do not
# replace NVDLA qualification or claim that synthetic scalar delays are measured.
@pytest.fixture
def phase2_runtime(tmp_path, monkeypatch):
    import os
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def _counter_stage_input(root):
    from pathlib import Path
    from tests.sta_path_reference import endpoint_facts
    from tests.yosys_reference import import_yosys_json
    repository = Path(__file__).resolve().parents[1]
    ir = import_yosys_json(repository / 'examples/yosys/counter.json',
                           top='counter', clocks=['clk'])
    next(cell for cell in ir.value['instances'] if cell['type'] == 'FDRE')[
        'parameters']['IS_C_INVERTED'] = '1'
    source = root / 'counter ?#é.emuir.sqlite'
    ir.save(source)
    objects = sorted(endpoint_facts(ir))
    nets = [net['id'] for net in ir.value['nets']]
    records = []
    for seq, slack in enumerate((1.5, -1e16, -1.0, -1e-16, 0.0, -2.3, 1e-30)):
        identity = (f'{objects[seq % len(objects)]}->{objects[-1]}#{seq:08d}'
                    if seq % 2 else f'legacy-{seq}')
        records.append([identity.encode().hex(), b'clk'.hex(), '10', str(slack), '0.25',
                        ','.join(net.encode().hex() for net in nets[:1 + seq % len(nets)])])
    return source, records


def _architecture_stage_contract(root):
    from emuflow.timing.xilinx_preplacement_timing import (
        XILINX_PREPLACEMENT_TIMING_SCHEMA, _REQUIRED_DELAYS,
    )
    from emuflow.timing.rapidwright_contract import RAPIDWRIGHT_TIMING_DATA_REVISION, RAPIDWRIGHT_TIMING_DATA_SHA256
    # Consumer-contract fixture: positive synthetic values and the required
    # source vocabulary, without fetching or claiming to extract upstream data.
    value = {
        'schema': XILINX_PREPLACEMENT_TIMING_SCHEMA,
        'family': 'UltraScale+', 'mapping_profile': 'xilinx-ultrascaleplus-open-v1',
        'source': {'provider': 'rapidwright-delay-model-v0.5',
                   'revision': RAPIDWRIGHT_TIMING_DATA_REVISION,
                   'qualification': 'analytical_uncharacterized',
                   'files_sha256': RAPIDWRIGHT_TIMING_DATA_SHA256},
        'delays_ps': {name: 100.0 + seq for seq, name in enumerate(sorted(_REQUIRED_DELAYS))},
        'limitations': {'authoritative_setup': 'Phase 7 routed timing plus global OpenSTA'},
    }
    path = root / 'architecture-timing.json'
    path.write_text(json.dumps(value))
    return path


def _timing_io():
    return {'input_groups': [{'clock': 'clk', 'delay_ns': 0.125, 'ports': ['rst']}],
            'output_groups': [{'clock': 'clk', 'delay_ns': 0.25, 'ports': ['q']}]}


def _fixed_opensta(root, records, *, failure=None, replace_source=None):
    import sys
    path = root / 'fixed-opensta.py'
    trace = root / 'opensta-invocations.jsonl'
    path.write_text(f'''#!{sys.executable}
import json
import os
from pathlib import Path
import sys

trace = Path({str(trace)!r})
arguments = sys.argv[1:]
if arguments == ['-version']:
    event = {{'kind': 'version', 'argv': arguments}}
elif '-no_init' in arguments:
    event = {{'kind': 'runtime', 'argv': arguments}}
else:
    event = {{'kind': 'paths', 'argv': arguments, 'inputs': {{
        key: Path(os.environ[key]).read_text() for key in (
            'EMUFLOW_STA_VERILOG', 'EMUFLOW_STA_LIBERTY', 'EMUFLOW_STA_NET_MAP',
            'EMUFLOW_STA_CLOCKS', 'EMUFLOW_STA_TIMING_IO')}}}}
with trace.open('a') as stream:
    stream.write(json.dumps(event, sort_keys=True) + '\\n')
if event['kind'] == 'version':
    print('3.1.0')
    raise SystemExit(0)
if event['kind'] == 'runtime':
    print('EMUFLOW_OPENSTA_RUNTIME status=pass tcl=8.6.16')
    raise SystemExit(0)
if {failure!r} == 'exit':
    print('FIXED_OPENSTA intentional extraction failure')
    raise SystemExit(7)
if {replace_source is not None!r}:
    source = Path({str(replace_source)!r})
    replacement = source.with_name(source.name + '.replacement')
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
if {failure!r} != 'missing-tsv':
    header = 'path_id_hex\\tclock_domain_hex\\tclock_period_ns\\tslack_ns\\tfixed_delay_ns\\tpath_nets_hex'
    records = {records!r}
    Path(os.environ['EMUFLOW_STA_OUTPUT']).write_text(
        header + '\\n' + '\\n'.join('\\t'.join(row) for row in records) + '\\n')
print('FIXED_OPENSTA extracted records={len(records)}')
''')
    path.chmod(0o755)
    return path, trace


def _stage_command(source, policy, output):
    import subprocess
    from emuflow.common.native_tools import resolve_native_executable
    executable = resolve_native_executable('emuflow-native')
    operation = output.parent / (output.name + '-operation.json')
    result = subprocess.run([executable, '--report', str(operation), 'phase2', 'run',
                             str(source.resolve()), str(policy.resolve()), str(output.resolve())],
                            capture_output=True, text=True)
    return result, operation


def _assert_unpublished_failure(source, policy, output, *, message=None):
    result, operation = _stage_command(source, policy, output)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not output.exists()
    if operation.exists():
        proof = json.loads(operation.read_text())
    else:
        # The unified CLI deliberately keeps a report destination untouched on
        # invalid requests; stderr carries the same structured failure.
        assert result.returncode == 2, result.stderr
        proof = json.loads(result.stderr.removeprefix('emuflow-native: '))
        assert proof['error']['code'] == 'invalid-request'
    assert proof['stage'] == 'phase2.run' and proof['status'] != 'pass'
    if message:
        assert message in (result.stdout + result.stderr + operation.read_text())
    assert not list(output.parent.glob('.phase2-stage-*'))
    return proof


def _assert_published_tool_evidence(output, engine):
    evidence = json.loads((output / 'opensta-execution.json').read_text())
    assert evidence['schema'] == 'emuflow.external-tool-result/v1'
    assert evidence['started'] is True and evidence['returncode'] == 0
    assert evidence['operation']['status'] == 'pass'
    assert evidence['timed_out'] is False and evidence['cancelled_signal'] == 0
    assert evidence['engine_metrics']['cpu_scope'] == 'wait4-child-and-reaped-descendants'
    assert evidence['engine_metrics']['wall_seconds'] >= 0
    assert any(row['path'] == str(engine.resolve())
               for row in evidence['operation']['inputs'])
    assert 'FIXED_OPENSTA extracted' in (output / 'opensta.log').read_text()


@pytest.mark.parametrize('architecture', [False, True])
@pytest.mark.parametrize('optimization_enabled', [False, True])
def test_complete_native_stage_matches_retained_provider_and_typed_weights(
        tmp_path, phase2_runtime, architecture, optimization_enabled):
    from emuflow_tools.stage_clients.opensta import run_opensta_path_database
    from emuflow_tools.timing.phase2_native import run_native_phase2
    from emuflow_tools.timing.sta import derive_partition_net_weights, iter_partition_net_weight_records, iter_sta_path_database_paths, validate_partition_net_weights, validate_sta_path_database
    source, records = _counter_stage_input(tmp_path)
    engine, trace = _fixed_opensta(tmp_path, records)
    architecture_path = _architecture_stage_contract(tmp_path) if architecture else None
    options = {'clocks': {'clk': 10.0}, 'timing_io': _timing_io(),
               'architecture_timing_db_path': architecture_path, 'executable': str(engine),
               'max_paths': 1000}
    output = tmp_path / 'native'
    from unittest.mock import patch
    with patch('emuflow_tools.stage_clients.opensta.run_opensta_path_database',
               side_effect=AssertionError('old Python timing stage was invoked')), \
         patch('emuflow.ir.ir.EmuIR.load', side_effect=AssertionError('whole Python graph was loaded')):
        report = run_native_phase2(source, output, optimization_enabled=optimization_enabled,
                                   criticality_scale=7.5, criticality_exponent=1.25, **options)
    reference = tmp_path / 'reference-paths.json'
    expected = run_opensta_path_database(source, reference, **options)
    actual = output / 'path-database.json'
    expected_manifest, actual_manifest = json.loads(reference.read_text()), json.loads(actual.read_text())
    assert list(iter_sta_path_database_paths(actual, actual_manifest)) == list(iter_sta_path_database_paths(reference, json.loads(reference.read_text())))
    assert actual_manifest['summary'] == expected_manifest['summary']
    assert actual_manifest['normalization'] == expected_manifest['normalization']
    assert report['sta']['path_qor'] == expected['path_qor']
    assert report['sta']['checker'] == expected['checker']
    assert report['sta']['used_cell_type_summary'] == expected['used_cell_type_summary']
    assert report['sta']['timing_io'] == expected['timing_io']
    assert report['sta']['input_export']['mode'] == int(architecture)
    assert report['optimization_enabled'] is optimization_enabled
    assert validate_sta_path_database(actual, source)['status'] == 'pass'
    assert len((output / 'phase2_report.json').read_bytes()) <= 262144
    assert report['sta']['path_limit_reached'] is False
    if optimization_enabled:
        reference_weights = tmp_path / 'reference-weights.json'
        expected_weights = derive_partition_net_weights(reference, source, reference_weights,
            criticality_scale=7.5, criticality_exponent=1.25)
        actual_weights = output / 'partition-net-weights.json'
        assert list(iter_partition_net_weight_records(actual_weights)) == list(
            iter_partition_net_weight_records(reference_weights))
        for field in ('paths', 'timed_nets', 'weighted_nets', 'minimum_weight', 'maximum_weight'):
            assert report['partition_weights'][field] == expected_weights[field]
        assert validate_partition_net_weights(actual_weights, actual, source)['status'] == 'pass'
    else:
        assert 'partition_weights' not in report
        assert not (output / 'partition-net-weights.json').exists()
        assert not list(output.glob('*.weights.*.sqlite'))
    _assert_published_tool_evidence(output, engine)
    invocations = [json.loads(line) for line in trace.read_text().splitlines()]
    extraction = [event for event in invocations if event['kind'] == 'paths']
    assert len(extraction) == 2
    assert extraction[0]['inputs'] == extraction[1]['inputs']
    assert ('__NEG' in extraction[0]['inputs']['EMUFLOW_STA_VERILOG']) is architecture
    assert sum(event['kind'] == 'version' for event in invocations) >= 3
    assert sum(event['kind'] == 'runtime' for event in invocations) >= 3


@pytest.mark.parametrize('role', ['emuir', 'timing-model', 'opensta-executable',
                                  'export-script', 'runtime-check-script', 'architecture-timing-db'])
def test_native_stage_rejects_replaced_original_source_generation(tmp_path, phase2_runtime, role):
    from pathlib import Path
    from emuflow.common.native_tools import native_file_generation
    source, records = _counter_stage_input(tmp_path)
    engine, _trace = _fixed_opensta(tmp_path, records)
    architecture = _architecture_stage_contract(tmp_path)
    policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10},
                            executable=str(engine), architecture_timing_db_path=architecture)
    value = json.loads(policy.read_text())
    record = next(item for item in value['sources'] if item['role'] == role)
    # All replacements stay in test-owned scratch, including Tcl/model files.
    victim = tmp_path / ('bound-' + role)
    victim.write_bytes(Path(record['path']).read_bytes())
    victim.chmod(0o755 if role == 'opensta-executable' else 0o644)
    record['path'], record['generation'] = str(victim.resolve()), list(native_file_generation(victim))
    policy.write_text(json.dumps(value))
    if role == 'emuir':
        source = victim
    replacement = tmp_path / ('replacement-' + role)
    replacement.write_bytes(victim.read_bytes())
    replacement.chmod(victim.stat().st_mode)
    replacement.replace(victim)
    _assert_unpublished_failure(source, policy, tmp_path / 'native', message='generation')


@pytest.mark.parametrize('failure', ['missing-tsv', 'exit', 'unknown-net', 'duplicate-path'])
def test_native_stage_rejects_failed_or_invalid_engine_output(tmp_path, phase2_runtime, failure):
    source, records = _counter_stage_input(tmp_path)
    if failure == 'unknown-net':
        records[0][5] = b'not-an-emuir-net'.hex()
    elif failure == 'duplicate-path':
        records[1][0] = records[0][0]
    engine, _trace = _fixed_opensta(tmp_path, records, failure=failure)
    policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10}, executable=str(engine))
    _assert_unpublished_failure(source, policy, tmp_path / 'native')


@pytest.mark.parametrize('damage', ['unknown-clock', 'zero-period', 'clock-bool',
                                    'io-direction', 'optimization-string', 'optimization-int',
                                    'max-paths-bool'])
def test_native_stage_independently_rejects_invalid_policy_semantics(tmp_path, phase2_runtime, damage):
    from emuflow_tools.timing.timing_io import timing_io_sha256
    source, records = _counter_stage_input(tmp_path)
    engine, _trace = _fixed_opensta(tmp_path, records)
    policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10}, executable=str(engine))
    value = json.loads(policy.read_text())
    configuration = value['configuration']
    if damage == 'unknown-clock':
        configuration['clock_map'] = {'absent': 10}
    elif damage == 'zero-period':
        configuration['clock_map'] = {'clk': 0}
    elif damage == 'clock-bool':
        configuration['clock_map'] = {'clk': True}
    elif damage == 'io-direction':
        configuration['timing_io'] = {'input_groups': [
            {'clock': 'clk', 'delay_ns': 0.25, 'ports': ['q']}], 'output_groups': []}
        configuration['timing_io_sha256'] = timing_io_sha256(configuration['timing_io'])
    elif damage.startswith('optimization-'):
        configuration['optimization_enabled'] = 'false' if damage.endswith('string') else 1
    else:
        configuration['max_paths'] = True
    policy.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
    _assert_unpublished_failure(source, policy, tmp_path / 'native')


def test_native_stage_discards_outputs_if_source_changes_inside_engine(tmp_path, phase2_runtime):
    source, records = _counter_stage_input(tmp_path)
    engine, trace = _fixed_opensta(tmp_path, records, replace_source=source)
    policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10}, executable=str(engine))
    _assert_unpublished_failure(source, policy, tmp_path / 'native')
    assert any(json.loads(line)['kind'] == 'paths' for line in trace.read_text().splitlines())


def test_native_stage_and_host_never_replace_existing_public_directory(tmp_path, phase2_runtime):
    from emuflow_tools.timing.phase2_native import run_native_phase2
    source, records = _counter_stage_input(tmp_path)
    engine, _trace = _fixed_opensta(tmp_path, records)
    policy = prepare_policy(tmp_path / 'policy', source, clocks={'clk': 10}, executable=str(engine))
    output = tmp_path / 'native'
    output.mkdir()
    sentinel = output / 'existing-result.json'
    sentinel.write_bytes(b'{"retained":"prior generation"}\n')
    previous = {path.name: path.read_bytes() for path in output.iterdir()}
    result, _operation = _stage_command(source, policy, output)
    assert result.returncode != 0
    with pytest.raises(ValidationError, match='fresh stage directory'):
        run_native_phase2(source, output, clocks={'clk': 10}, executable=str(engine))
    assert {path.name: path.read_bytes() for path in output.iterdir()} == previous


def test_real_opensta_stage_matches_retained_provider_when_installed(tmp_path, phase2_runtime):
    import os
    from pathlib import Path
    from emuflow.common.native_tools import resolve_native_executable
    from emuflow_tools.stage_clients.opensta import run_opensta_path_database
    from emuflow_tools.timing.phase2_native import run_native_phase2
    from emuflow_tools.timing.sta import iter_sta_path_database_paths
    from emuflow.common.errors import EmuFlowError
    try:
        engine = Path(resolve_native_executable('sta'))
    except EmuFlowError:
        pytest.skip('optional source-built OpenSTA is not installed')
    if not engine.is_file() or not os.access(engine, os.X_OK):
        pytest.skip('optional source-built OpenSTA is not executable')
    source, _records = _counter_stage_input(tmp_path)
    options = {'clocks': {'clk': 10.0}, 'timing_io': _timing_io(), 'executable': str(engine)}
    output = tmp_path / 'real-native'
    actual = run_native_phase2(source, output, optimization_enabled=False, **options)
    reference = tmp_path / 'real-reference.json'
    expected = run_opensta_path_database(source, reference, **options)
    assert list(iter_sta_path_database_paths(output / 'path-database.json', json.loads((output / 'path-database.json').read_text()))) == list(
        iter_sta_path_database_paths(reference, json.loads(reference.read_text())))
    assert actual['sta']['path_qor'] == expected['path_qor']
    evidence = json.loads((output / 'opensta-execution.json').read_text())
    assert evidence['started'] is True and evidence['returncode'] == 0
    assert evidence['operation']['status'] == 'pass'


def test_native_policy_matches_all_specialized_pin_roles_and_clock_polarities(tmp_path, phase2_runtime):
    from copy import deepcopy
    from unittest.mock import patch
    from tests.test_xilinx_preplacement_timing import _mapped_fixture
    from tests.yosys_reference import import_yosys_json
    from emuflow_tools.stage_clients.opensta import (build_architecture_opensta_timing_model,
        render_opensta_liberty, validate_timing_model_coverage, _sealed_string_set_summary)
    mapped = tmp_path / 'mapped.json'
    _mapped_fixture(mapped)
    graph = json.loads(mapped.read_text())
    cells = graph['modules']['top']['cells']
    # First-occurrence pin sets, constant pins, bus scalarization, and the
    # distinct memory clock/control/data roles must all survive migration.
    cells['ram']['connections']['ADDR'] = [7, '0', '1']
    cells['ram']['connections']['DOUT'] = [8, 11]
    cells['ram']['port_directions']['RSTREGARSTREG'] = 'input'
    cells['ram']['connections']['RSTREGARSTREG'] = ['0']
    for n, kind in enumerate(('RAMB36E2', 'URAM288')):
        cells[kind] = deepcopy(cells['ram'])
        cells[kind]['type'] = kind
        cells[kind]['connections']['DOUT'] = [12 + 2*n, 13 + 2*n]
    cells['zzz_later_ram'] = deepcopy(cells['ram'])
    cells['zzz_later_ram']['connections']['ADDR'] = [7]
    cells['zzz_later_ram']['connections']['DOUT'] = [30]
    for seq, (value, _) in enumerate(PARAMETERS):
        cell = cells[f'polarity_{seq:03d}'] = deepcopy(cells['ff'])
        cell['parameters']['IS_C_INVERTED'] = value
        cell['connections']['Q'] = [31 + seq]
    mapped.write_text(json.dumps(graph))
    ir = import_yosys_json(mapped, top='top', clocks=['clk'])
    source = tmp_path / 'mixed.emuir.sqlite'
    ir.save(source)
    database = _architecture_stage_contract(tmp_path)
    from contextlib import closing
    with closing(IndexedTimingView(source)) as view:
        model, types = build_architecture_opensta_timing_model(view, database)
        coverage = validate_timing_model_coverage(view, model, types)
    expected_liberty = render_opensta_liberty(model)
    engine, _trace = _fixed_opensta(tmp_path, [])
    with patch('emuflow_tools.timing.indexed_timing.IndexedTimingView', side_effect=AssertionError('Python model preparation')):
        policy = prepare_policy(tmp_path / 'native-policy', source, clocks={'clk': 10},
            architecture_timing_db_path=database, executable=str(engine))
    actual = json.loads(policy.read_text())['configuration']
    assert actual['liberty'] == expected_liberty
    assert actual['model']['cell_types'] == sorted(model['cells'])
    assert actual['model']['source'] == model['source']
    assert actual['used_cell_type_summary'] == _sealed_string_set_summary(coverage['used_cell_types'])


@pytest.mark.parametrize('damage', ['clock', 'max-paths-bool', 'io-direction', 'architecture-seal', 'architecture-delay', 'model-output'])
def test_native_policy_rejects_invalid_sources_without_publishing(tmp_path, phase2_runtime, damage):
    from pathlib import Path
    from emuflow_tools.stage_clients.opensta import DEFAULT_TIMING_MODEL
    from emuflow.common.native_tools import native_file_generation
    source, records = _counter_stage_input(tmp_path)
    engine, _trace = _fixed_opensta(tmp_path, records)
    architecture = _architecture_stage_contract(tmp_path)
    options = {'clocks': {'clk': 10}, 'executable': str(engine)}
    if damage == 'clock':
        options['clocks'] = {'unknown': 10}
    elif damage == 'max-paths-bool':
        options['max_paths'] = True
    elif damage == 'io-direction':
        options['timing_io'] = {'input_groups': [{'clock': 'clk', 'delay_ns': 0, 'ports': ['q']}], 'output_groups': []}
    elif damage.startswith('architecture'):
        value = json.loads(architecture.read_text())
        if damage == 'architecture-seal':
            value['source']['files_sha256']['intrasite_delay_terms.txt'] = '0' * 64
        else:
            value['delays_ps']['lut6'] = False
        architecture.write_text(json.dumps(value))
        options['architecture_timing_db_path'] = architecture
    else:
        value = json.loads(DEFAULT_TIMING_MODEL.read_text())
        value['cells']['broken'] = {'kind': 'constant', 'outputs': []}
        model = tmp_path / 'invalid-model.json'
        model.write_text(json.dumps(value))
        options['timing_model_path'] = model
    generation = native_file_generation(source)
    with pytest.raises(ValidationError):
        prepare_policy(tmp_path / 'rejected', source, **options)
    assert not (tmp_path / 'rejected').exists()
    assert native_file_generation(source) == generation
