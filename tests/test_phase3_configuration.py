"""Independent bounded policy differential and source/publication failures."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from emuflow.common.native_tools import resolve_native_executable
from emuflow.partition.phase3_native import _configuration_value, write_request
from tests.phase3_configuration_reference import write_request as reference_request
from tests.test_phase3_pressure_native import inputs
from tests.test_phase3_native_stage import options


@pytest.mark.parametrize('variant', ['default', 'directed', 'shared', 'unavailable', 'sll', 'ratio', 'unicode'])
def test_configuration_matches_independent_reference_and_binary_policy(inputs, tmp_path, variant):
    ir, board, config = options(inputs, tmp_path)
    data = json.loads(board.read_text())
    routes = {'schema': 'emuflow.system-route-constraints/v1', 'frame_slots': 64}
    link = data['links'][0]
    if variant == 'directed':
        routes['directed_link_delay_ns'] = {link['id']: {link['endpoints'][0]: {link['endpoints'][1]: 3.25}}}
    elif variant == 'shared':
        routes['shared_capacity_links'] = [link['id']]
        link['direction'] = 'half_duplex'
    elif variant == 'unavailable':
        routes['unavailable_links'] = [row['id'] for row in data['links']]
    elif variant == 'sll':
        routes['sll_links'] = [link['id']]
    elif variant == 'ratio':
        for row in data['links']:
            row['tdm_delay_model'] = {'model': 'linear-ratio-v1', 'intercept_ns': 1.25,
                'ratio_coefficient_ns': 0.5, 'min_ratio': 4, 'max_ratio': 64, 'ratio_quantum': 4}
        routes['link_delay_ns'] = {link['id']: 3.25}
    elif variant == 'unicode':
        data['platform']['name'] = 'board-板级-😀'
    board.write_text(json.dumps(data))
    config['route_constraints_path'].write_text(json.dumps(routes))
    with patch('emuflow.ir.ir.EmuIR.load', side_effect=AssertionError('Python graph materialization')), patch(
            'emuflow.partition.partition_ir.load_partition_ir', side_effect=AssertionError('Python circuit count view')):
        generated = write_request(tmp_path/'native-policy', ir.path, board, **config)
    reference = reference_request(tmp_path/'reference-policy', ir.path, board, **config)
    actual, expected = json.loads(generated.read_text()), json.loads(reference.read_text())
    actual['inputs'].pop('candidate_policy'); expected['inputs'].pop('candidate_policy')
    assert actual == expected
    assert (generated.parent/'candidate-policy.bin').read_bytes() == (reference.parent/'candidate-policy.bin').read_bytes()


@pytest.mark.parametrize('damage', ['generation', 'version', 'duplicate-role', 'unknown-options', 'bound-source', 'host-capacity', 'host-topology', 'existing'])
def test_configuration_rejects_invalid_sources_without_publishing(inputs, tmp_path, damage):
    ir, board, config = options(inputs, tmp_path)
    encoded, _ = _configuration_value(ir.path, board, **config)
    value = json.loads(encoded)
    if damage == 'generation': value['sources'][0]['generation'][2] += 1
    elif damage == 'version': value['schema'] = 'emuflow.phase3-configuration-request/v99'
    elif damage == 'duplicate-role': value['sources'].append(value['sources'][0])
    elif damage == 'unknown-options': value['options']['implicit-fallback'] = True
    elif damage == 'bound-source': board.write_text(board.read_text() + '\n')
    elif damage == 'host-capacity': value['board']['fpgas'][0]['effective_capacity']['lut'] += 1
    elif damage == 'host-topology': value['board']['links'][0]['data_lanes_per_direction'] += 1
    settings = tmp_path/'settings.json'; settings.write_text(json.dumps(value))
    output = tmp_path/'rejected'
    if damage == 'existing': output.mkdir(); (output/'prior').write_text('preserved')
    result = subprocess.run([resolve_native_executable('emuflow-native'), 'phase3', 'prepare-policy', str(settings), str(output)], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    if damage == 'existing': assert [p.name for p in output.iterdir()] == ['prior']
    else: assert not output.exists()
    assert not list(tmp_path.glob('.partition-configuration-*'))


@pytest.mark.parametrize('damage', [None, 'signoff', 'coverage', 'source', 'observations', 'duplicate'])
def test_board_link_projection_matches_independent_contract(inputs, tmp_path, damage):
    from emuflow.platform.platform import Platform
    from emuflow.platform.board_link_timing import build_board_link_timing_model, directed_route_link_delays, validate_board_link_timing
    from emuflow.common.errors import ValidationError
    ir, board, config = options(inputs, tmp_path)
    platform = Platform.load(board)
    database = build_board_link_timing_model(platform)
    if damage == 'signoff': database['final_link_timing_signoff'] = True
    elif damage == 'coverage': database['links'].pop()
    elif damage == 'source': database['links'][0]['source']['kind'] = 'hardware-measurement'
    elif damage == 'observations':
        database['links'][0].update(qualification='measured-upper-bound', source={'kind': 'hardware-measurement', 'reference': 'measurement', 'observations': 0})
    elif damage == 'duplicate': database['links'].append(database['links'][0])
    timing = tmp_path/'board-timing.json'; timing.write_text(json.dumps(database))
    if damage:
        with pytest.raises(ValidationError): validate_board_link_timing(database, platform)
        with pytest.raises(ValidationError): write_request(tmp_path/'rejected-link-policy', ir.path, board, board_link_timing_db=timing, **config)
        assert not (tmp_path/'rejected-link-policy').exists()
        return
    actual = write_request(tmp_path/'native-link-policy', ir.path, board, board_link_timing_db=timing, timing_driven=False, **config)
    summary = json.loads((actual.parent/'configuration-summary.json').read_text())
    delays, projection = directed_route_link_delays(database, platform)
    assert summary['link_timing_report']['validation'] == validate_board_link_timing(database, platform)
    assert summary['link_timing_report']['routing_projection'] == projection
    assert summary['link_timing_report']['optimization_enabled'] is False
    routes = json.loads(config['route_constraints_path'].read_text()); routes['directed_link_delay_ns'] = delays
    assert summary['effective_route_constraints'] == routes
    config['route_constraints_path'].write_text(json.dumps(routes))
    reference = reference_request(tmp_path/'reference-link-policy', ir.path, board, **config)
    generated, expected = json.loads(actual.read_text()), json.loads(reference.read_text())
    generated['inputs'].pop('candidate_policy');expected['inputs'].pop('candidate_policy')
    assert generated == expected


@pytest.mark.parametrize('chain', [False, True])
def test_shared_bank_ratio_domains_match_reference(inputs, tmp_path, chain):
    from tests.test_phase5_bank_ratio_model import platform_and_routes
    ir, board, config = options(inputs, tmp_path)
    platform, routes = platform_and_routes(chain=chain)
    board.write_text(json.dumps(platform.to_dict()))
    config['route_constraints_path'].write_text(json.dumps(routes['constraints']))
    actual = write_request(tmp_path/'native-banks', ir.path, board, **config)
    reference = reference_request(tmp_path/'reference-banks', ir.path, board, **config)
    generated, expected = json.loads(actual.read_text()), json.loads(reference.read_text())
    generated['inputs'].pop('candidate_policy'); expected['inputs'].pop('candidate_policy')
    assert generated == expected
    assert all(domain['bank_count'] == 18 and domain['wires_per_bank'] == 20 and domain['opposite'] >= 0 for domain in generated['domains'])


def test_installed_policy_data_and_retired_python_encoder():
    import os
    import importlib.util
    from emuflow.partition.structural_policy import STRUCTURAL_POLICY
    from emuflow.ir.resources import RESOURCE_FIELDS
    from emuflow.ir.ir import VALID_CUT_CLASSES
    assert importlib.util.find_spec('emuflow.partition.phase3_policy') is None
    assert tuple(STRUCTURAL_POLICY['resource_fields']) == RESOURCE_FIELDS
    assert set(STRUCTURAL_POLICY['valid_cut_classes']) == VALID_CUT_CLASSES
    installed = Path(os.environ['EMUFLOW_NATIVE_ROOT'])/'lib/emuflow/data/partition/structural-policy.json'
    assert json.loads(installed.read_text()) == STRUCTURAL_POLICY


def test_configuration_reads_the_bound_board_when_an_alias_changes(inputs, tmp_path):
    from emuflow.platform.platform import Platform
    ir, board, config = options(inputs, tmp_path)
    alias = tmp_path/'board-alias.json'; alias.symlink_to(board)
    replacement = tmp_path/'other-board.json'
    value = json.loads(board.read_text()); value['platform']['name'] = 'different-board'
    replacement.write_text(json.dumps(value))
    load = Platform.load
    def switch_alias(path):
        assert Path(path) == board.resolve()
        alias.unlink(); alias.symlink_to(replacement)
        return load(path)
    with patch.object(Platform, 'load', side_effect=switch_alias):
        encoded, _ = _configuration_value(ir.path, alias, **config)
    generated = json.loads(encoded)
    assert generated['board']['platform']['name'] == load(board).name
    assert next(row for row in generated['sources'] if row['role'] == 'board')['path'] == str(board.resolve())
