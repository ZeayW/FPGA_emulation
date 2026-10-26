"""Full policy differential gates; NVDLA remains the real-design acceptance."""
import copy
from contextlib import closing
import fnmatch
import itertools
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sqlite3
import sys
from unittest.mock import patch

import pytest

from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import normalize_partition_constraints, _normalize_partition_constraints_reference
from emuflow.partition_ir import load_partition_ir
from emuflow.platform import Platform, FpgaNode, BoardLink
from emuflow.resources import RESOURCE_FIELDS
from tests.native_build import emuflow_native
from tests import test_partition_pressure as reference_fixture


@pytest.fixture(params=['normal', 'unicode', 'nul'])
def configuration(request, tmp_path, monkeypatch):
    monkeypatch.setenv('EMUFLOW_NATIVE_ROOT', str(emuflow_native().parent.parent))
    fixture = reference_fixture.PartitionPressureTest()
    fixture.setUp()
    value = copy.deepcopy(fixture.ir.value)
    if request.param != 'normal':
        names = {cell['id']: ('目录/🙂'+cell['id']+'[0]' if request.param == 'unicode' else cell['id']+'\x00tail')
                 for cell in value['instances']}
        for cell in value['instances']:
            cell['id'] = names[cell['id']]
        for net in value['nets']:
            for key in ('drivers', 'sinks'):
                for endpoint in net[key]:
                    if endpoint.get('instance') in names:
                        endpoint['instance'] = names[endpoint['instance']]
    reference = EmuIR(value)
    reference.validate()
    path = tmp_path/'configuration-source.sqlite'
    write_compact_emuir(path, value)
    return reference, load_partition_ir(path), fixture.platform


def compare(configuration, raw, **overrides):
    reference, indexed, platform = configuration
    expected = _normalize_partition_constraints_reference(raw, reference, platform, **overrides)
    actual = normalize_partition_constraints(raw, indexed, platform, **overrides)
    assert actual == expected
    assert actual.implementation_id == 'native-partition-configuration-v1'
    return actual


@pytest.mark.parametrize('case', ['default', 'all', 'explicit', 'minimum', 'override', 'groups', 'fixed', 'reservations', 'dimensions'])
def test_complete_configuration_matches_reference(configuration, case):
    reference, _, platform = configuration
    names = sorted(cell['id'] for cell in reference.value['instances'])
    raw = {'schema': 'emuflow.partition-constraints/v1'}
    overrides = {}
    if case == 'default':
        raw = None
    elif case == 'explicit':
        raw['active_fpgas'] = [platform.fpgas[-1].id]
        raw['min_used_fpgas'] = 1
    elif case == 'minimum':
        raw['fpga_selection_policy'] = 'minimum-capacity'
    elif case == 'override':
        raw.update(min_used_fpgas=2, balance_tolerance=0.5)
        overrides = {'min_used_fpgas': 1, 'balance_tolerance': 0.0}
    elif case == 'groups':
        raw['groups'] = [{'id': 'first', 'instances': [names[1], names[0], names[0]], 'patterns': ['*']},
                         {'id': '重复\x00组', 'patterns': ['?*']}]
    elif case == 'fixed':
        raw['fixed'] = [{'instance': names[0], 'fpga': platform.fpgas[0].id},
                        {'instances': [names[0]], 'patterns': ['*'], 'fpga': platform.fpgas[0].id}]
    elif case == 'reservations':
        raw['resource_reservations_by_fpga'] = {platform.fpgas[0].id: {'lut': 1, 'ff': 0}}
    elif case == 'dimensions':
        raw.update(balance_dimensions=['ff', 'lut', 'cells'], balance_tolerance_by_dimension={'lut': 0.2, 'ff': 0.0})
    compare(configuration, raw, **overrides)


@pytest.mark.parametrize('seed', range(25))
def test_capacity_selection_preserves_topology_and_heterogeneous_ties(configuration, seed):
    reference, indexed, base = configuration
    rng = random.Random(seed)
    count = rng.randint(2, 6)
    fpgas = tuple(FpgaNode(id=f'p{p}', part='fixture', utilization_limit=1.0,
        capacity={field: rng.randint(1, 5) if seed % 2 else 3 for field in RESOURCE_FIELDS})
        for p in range(count))
    links = []
    for left in range(count):
        for right in range(left+1, count):
            if rng.random() < 0.55:
                links.append(BoardLink(id=f'l{left}-{right}', endpoints=(f'p{left}', f'p{right}'),
                    direction=rng.choice(['unidirectional', 'full_duplex', 'half_duplex']), mode='abstract',
                    data_lanes_per_direction=rng.choice([1, 32, 64, 128]), fabric_clock_mhz=250.0,
                    latency_cycles=rng.randint(0, 5)))
    platform = Platform(name='selection-fixture', kind='virtual', description='', fpgas=fpgas, links=tuple(links))
    raw = {'schema': 'emuflow.partition-constraints/v1', 'fpga_selection_policy': 'minimum-capacity'}
    if seed % 3:
        raw['min_used_fpgas'] = rng.randint(1, count)
    if seed % 5 == 0:
        raw['fixed'] = [{'instance': reference.value['instances'][0]['id'], 'fpga': fpgas[-1].id}]
    try:
        expected = _normalize_partition_constraints_reference(raw, reference, platform)
    except ValidationError:
        with pytest.raises(ValidationError):
            normalize_partition_constraints(raw, indexed, platform)
    else:
        actual = normalize_partition_constraints(raw, indexed, platform)
        assert actual == expected
        # A sealed choice must revalidate against precisely the same policy.
        assert normalize_partition_constraints(actual, indexed, platform) == expected


def test_homogeneous_32_fpga_selection_has_no_python_cell_or_identity_population(configuration):
    reference, indexed, base = configuration
    template = base.fpgas[0]
    platform = Platform(name='wide-board', kind='virtual', description='',
        fpgas=tuple(FpgaNode(id=f'p{p}', part='fixture', utilization_limit=1.0,
                            capacity=dict(template.capacity)) for p in range(32)), links=())
    raw = {'schema': 'emuflow.partition-constraints/v1', 'fpga_selection_policy': 'minimum-capacity',
           'min_used_fpgas': 3, 'groups': [{'id': 'group', 'patterns': ['*']}],
           'fixed': [{'patterns': ['*'], 'fpga': 'p31'}]}
    expected = _normalize_partition_constraints_reference(raw, reference, platform)
    with (patch('emuflow.partition._normalize_partition_constraints_reference', side_effect=AssertionError('Python policy entered')),
          patch('emuflow.partition._ordered_partition_instance_ids', side_effect=AssertionError('Python cell catalog')),
          patch.object(type(indexed.value['instances']), '__iter__', side_effect=AssertionError('Python resource population'))):
        actual = normalize_partition_constraints(raw, indexed, platform)
    assert actual == expected


def test_glob_expansion_matches_fnmatchcase_without_sqlite_scalar_coercion(configuration):
    reference, _, _ = configuration
    names = [cell['id'] for cell in reference.value['instances']]
    patterns = ['*', '?*', '*?*', '*[[]0]', '*🙂*', '*\x00*', '*[!x]*', '*[^x]*', '*[]x]*', '*[0-9]*', '*[!9]*']
    patterns += [''.join(chars) for size in range(1, 5) for chars in itertools.product('u01?*[]!-', repeat=size)]
    valid = []
    for pattern in patterns:
        try:
            matched = [name for name in names if fnmatch.fnmatchcase(name, pattern)]
        except re.error:
            continue
        if matched:
            valid.append(pattern)
        if len(valid) >= 2048:
            break
    compare(configuration, {'schema': 'emuflow.partition-constraints/v1',
                           'groups': [{'id': str(i), 'patterns': [pattern]} for i, pattern in enumerate(valid)]})


def test_empty_range_convention_matches_current_reference_without_host_version_drift(configuration):
    reference, indexed, platform = configuration
    executable = sys.executable if sys.version_info >= (3, 12) else shutil.which('python3.12')
    if executable is None:
        candidate = Path('/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12')
        executable = str(candidate) if candidate.is_file() else None
    if executable is None:
        pytest.skip('empty-range reference interpreter is not installed')
    names = [cell['id'] for cell in reference.value['instances']]
    patterns = ['*u[z-a0]*', '*u[!z-a1]*', '*u[z-!!]*', '*u[--a-c]*', '*[!z-a]*', '*[z-aa]*']
    patterns += [''.join(chars) for size in range(1, 5) for chars in itertools.product('u01?*[]!-', repeat=size)]
    code = '''import fnmatch,json,sys
names,patterns=json.load(sys.stdin)
rows=[]
for pattern in patterns:
 matched=[name for name in names if fnmatch.fnmatchcase(name,pattern)]
 if matched:rows.append([pattern,matched])
 if len(rows)>=2048:break
json.dump(rows,sys.stdout)
'''
    rows = json.loads(subprocess.check_output([executable, '-I', '-S', '-c', code],
        input=json.dumps([names, patterns]), text=True))
    raw = {'schema': 'emuflow.partition-constraints/v1',
           'groups': [{'id': str(i), 'patterns': [pattern]} for i, (pattern, _) in enumerate(rows)]}
    expanded = dict(raw, groups=[{'id': str(i), 'instances': matched} for i, (_, matched) in enumerate(rows)])
    expected = _normalize_partition_constraints_reference(expanded, reference, platform)
    with patch('emuflow.partition_config_native._pattern_range_policy', return_value='empty-ranges-v1'):
        actual = normalize_partition_constraints(raw, indexed, platform)
    assert actual == expected


@pytest.mark.parametrize('raw', [
    {'schema': 'wrong'}, {'groups': {}}, {'groups': [{'id': 'g', 'patterns': ['missing*']}]},
    {'groups': [{'id': 'g', 'patterns': ['']}]}, {'groups': [{'id': 'g', 'instances': ['absent']}]},
    {'groups': [{'id': 'g', 'patterns': ['*']}, {'id': 'g', 'patterns': ['*']}]},
    {'fixed': [{'instance': None, 'fpga': 'a'}]}, {'fixed': [{'patterns': ['*'], 'fpga': 'absent'}]},
    {'fixed': [{'patterns': ['*'], 'fpga': 'a'}, {'patterns': ['*'], 'fpga': 'b'}]},
    {'active_fpgas': []}, {'active_fpgas': ['absent']}, {'active_fpgas': ['a', 'a']},
    {'fpga_selection_policy': 'unknown'}, {'fpga_selection_policy': 'explicit'},
    {'min_used_fpgas': 0}, {'min_used_fpgas': -1}, {'min_used_fpgas': True}, {'min_used_fpgas': 2.0},
    {'balance_tolerance': -0.1}, {'balance_tolerance': True}, {'balance_tolerance': float('inf')},
    {'balance_tolerance_by_dimension': {'unknown': 0.1}}, {'balance_tolerance_by_dimension': {'lut': -0.1}},
    {'balance_dimensions': ['lut', 'lut']}, {'balance_dimensions': ['unknown']}, {'balance_dimensions': None},
    {'resource_reservations_by_fpga': {'a': {'lut': True}}},
    {'resource_reservations_by_fpga': {'a': {'cells': 1}}},
    {'resource_reservations_by_fpga': {'a': {'lut': -1}}},
    {'active_fpgas': ['b'], 'resource_reservations_by_fpga': {'a': {'lut': 1}}},
])
def test_invalid_configuration_rejects_without_reference_retry(configuration, raw):
    reference, indexed, platform = configuration
    raw = dict({'schema': 'emuflow.partition-constraints/v1'}, **raw)
    with pytest.raises(ValidationError):
        _normalize_partition_constraints_reference(raw, reference, platform)
    with patch('emuflow.partition._normalize_partition_constraints_reference', side_effect=AssertionError('reference retry')):
        with pytest.raises(ValidationError):
            normalize_partition_constraints(raw, indexed, platform)


@pytest.mark.parametrize('damage', ['request-crc', 'generation', 'output-generation'])
def test_producer_bindings_reject_tampered_requests_sources_and_outputs(configuration, damage):
    _, indexed, platform = configuration
    import emuflow.partition_config_native as module
    real = module.NativeToolProcess
    def changed_process(executable, command, **kwargs):
        if damage == 'request-crc':
            path = Path(command[-2]);data = bytearray(path.read_bytes());data[-1] ^= 1;path.write_bytes(data)
        elif damage == 'generation':
            indexed.path.touch()
        child = real(executable, command, **kwargs)
        if damage == 'output-generation':
            original = child.finish
            def finish():
                result = original()
                Path(command[-1]).touch()
                return result
            child.finish = finish
        return child
    with patch.object(module, 'NativeToolProcess', changed_process), patch(
            'emuflow.partition._normalize_partition_constraints_reference', side_effect=AssertionError('reference retry')):
        with pytest.raises(ValidationError):
            normalize_partition_constraints(None, indexed, platform)


@pytest.mark.parametrize('amount', [2**32+1, 2**53+1, 2**64+17, 10**200])
def test_capacity_demand_never_narrows_source_resource_integers(configuration, amount):
    reference, indexed, base = configuration
    for cell in reference.value['instances']:
        cell['resources'] = {'lut': amount}
    with closing(sqlite3.connect(indexed.path)) as db, db:
        rows = db.execute('SELECT DISTINCT resources_seq FROM cell_records').fetchall()
        for row in rows:
            db.execute('UPDATE payloads SET value=? WHERE seq=?', (json.dumps({'lut': amount}), row[0]))
    platform = Platform(name='large-capacity', kind='virtual', description='', links=(),
        fpgas=tuple(FpgaNode(id=f'p{p}', part='fixture', utilization_limit=1.0,
                            capacity={'lut': 2**62}) for p in range(32)))
    raw = {'schema': 'emuflow.partition-constraints/v1', 'fpga_selection_policy': 'minimum-capacity'}
    try:
        expected = _normalize_partition_constraints_reference(raw, reference, platform)
    except ValidationError:
        with pytest.raises(ValidationError):
            normalize_partition_constraints(raw, indexed, platform)
    else:
        assert normalize_partition_constraints(raw, indexed, platform) == expected


def test_resource_keys_cannot_alias_through_sqlite_nul_truncation(configuration):
    _, indexed, platform = configuration
    with closing(sqlite3.connect(indexed.path)) as db, db:
        row = db.execute('SELECT resources_seq FROM cell_records LIMIT 1').fetchone()
        db.execute('UPDATE payloads SET value=? WHERE seq=?', (json.dumps({'lut\x00other': 1}), row[0]))
    with pytest.raises(ValidationError, match='unknown compact resource field'):
        normalize_partition_constraints({'schema': 'emuflow.partition-constraints/v1',
                                        'fpga_selection_policy': 'minimum-capacity'}, indexed, platform)


def test_original_constraint_file_bypasses_host_json_and_small_request_size(configuration, tmp_path):
    reference, indexed, platform = configuration
    from emuflow.partition import load_partition_constraints
    path = tmp_path/'large-whitespace.json'
    with path.open('w') as stream:
        stream.write(' '*(8*1024*1024+1))
        json.dump({'schema': 'emuflow.partition-constraints/v1'}, stream)
    expected = _normalize_partition_constraints_reference(None, reference, platform)
    with patch('emuflow.partition.read_json', side_effect=AssertionError('host JSON input loaded')):
        actual = load_partition_constraints(path, indexed, platform)
    assert actual == expected


@pytest.mark.parametrize('tolerance', ['-0', '-0.0', '-0e0'])
def test_original_numeric_tokens_preserve_integer_and_float_zero(configuration, tmp_path, tolerance):
    reference, indexed, platform = configuration
    from emuflow.partition import load_partition_constraints
    text = ('{"schema":"emuflow.partition-constraints/v1","balance_tolerance":'+tolerance+
            ',"resource_reservations_by_fpga":{"a":{"lut":-0}}}')
    path = tmp_path/'numeric-zero.json'
    path.write_text(text)
    expected = _normalize_partition_constraints_reference(json.loads(text), reference, platform)
    actual = load_partition_constraints(path, indexed, platform)
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
