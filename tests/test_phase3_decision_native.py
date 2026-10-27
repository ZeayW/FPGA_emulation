"""Independent decision/resource gates; full NVDLA acceptance stays separate."""
import copy
from pathlib import Path
import struct
from unittest.mock import patch
import zlib

import pytest

from emuflow.errors import ValidationError
from emuflow.partition import (build_partition_assignment,
    validate_partition_artifacts_online, _cluster_assignment_summary)
from emuflow.partition_init_native import initialize
from emuflow.phase3_pressure_native import build_native_pressure_model
from tests.phase3_decision_reference import _select_patron_timing_assignment
from emuflow.phase3_decision_native import select_assignment, audit_assignment
from emuflow.store_publication import bind_store_source
from tests.test_phase3_pressure_native import inputs, run


@pytest.fixture
def decision(inputs, tmp_path):
    args, reference, timing = inputs
    ir, platform, clusters, constraints, _, _ = args
    parts = tuple(fpga.id for fpga in platform.fpgas)
    imported = tmp_path/'initial.solution'
    imported.write_text(''.join(str(parts.index(reference['cluster_assignment'][f'c{i:06d}']))+'\n'
                                for i in range(len(clusters['clusters']))))
    owners, _ = initialize(ir, platform, clusters, constraints, seed=0,
                          executable=None, solution_input=imported)
    initial = build_partition_assignment(ir, platform, clusters, constraints,
        owners, 'tritonpart', 0, _include_semantic_contract=False,
        _stream_semantic_records=True, _defer_derived_records=True)
    model = build_native_pressure_model(*args, timing_database_path=timing)
    candidate, trace = run(args, model, initial, max_moves=12)
    return args, initial, candidate, trace


def test_original_owner_pair_matches_reference_choice_and_independent_legality(decision):
    args, initial, candidate, trace = decision
    _, platform, clusters, *_ = args
    expected, selection = _select_patron_timing_assignment(initial, candidate, trace)
    with patch('emuflow.phase3_decision_native.audit_assignment', return_value=None):
        reference = [validate_partition_artifacts_online(platform, clusters, value)
                     for value in (initial, candidate)]
    actual = select_assignment(platform, clusters, initial, candidate)
    assert actual['selection'] == selection
    assert (candidate if actual['selection']['selected'] == 'candidate' else initial) is expected
    for value, audit, certificate, expected_validation in zip((initial, candidate), actual['audits'], actual['native_audits'], reference):
        assert validate_partition_artifacts_online(platform, clusters, value, _native_audit=certificate) == expected_validation
        totals, loads, counts = _cluster_assignment_summary(clusters['clusters'], value['cluster_assignment'], tuple(audit['parts']))
        assert list(totals.values()) == audit['totals']
        assert [list(loads[part].values()) for part in audit['parts']] == audit['loads']
        assert [counts[part] for part in audit['parts']] == audit['cluster_counts']


def test_native_decision_and_owned_reduction_never_serialize_or_compare_host_columns(decision):
    args, initial, candidate, _ = decision
    _, platform, clusters, *_ = args
    from emuflow.phase3_pressure_native import _Writer
    from emuflow.partition import _PackedClusterAssignment
    original_bytes = _Writer.bytes
    def bounded_bytes(writer, data):
        if not isinstance(data, (bytes, bytearray)):
            raise AssertionError('host ownership serialized')
        return original_bytes(writer, data)
    with (patch.object(_Writer, 'bytes', bounded_bytes),
          patch.object(_PackedClusterAssignment, '__eq__', side_effect=AssertionError('host ownership compared')),
          patch.object(type(clusters['clusters']), '_record', side_effect=AssertionError('cluster records expanded'))):
        result = select_assignment(platform, clusters, initial, candidate)
        for value, audit, certificate in zip((initial, candidate), result['audits'], result['native_audits']):
            _cluster_assignment_summary(clusters['clusters'], value['cluster_assignment'], tuple(audit['parts']))
            validate_partition_artifacts_online(platform, clusters, value, _native_audit=certificate)


@pytest.mark.parametrize('damage', ['fixed', 'group', 'unknown-cell', 'capacity', 'balance', 'minimum', 'inactive'])
def test_independent_native_gate_rejects_illegal_original_owners(decision, damage):
    args, initial, candidate, _ = decision
    _, platform, clusters, *_ = args
    constraints = copy.deepcopy(initial['constraints'])
    owner = initial['instance_assignment']
    identities = list(owner)
    if damage == 'fixed':
        identity = identities[0]
        constraints['fixed'] = [{'instance': identity, 'fpga': next(fpga.id for fpga in platform.fpgas if fpga.id != owner[identity])}]
    elif damage == 'group':
        first = identities[0]
        second = next(identity for identity in identities if owner[identity] != owner[first])
        constraints['groups'] = [{'id': 'illegal-merge', 'instances': [first, second]}]
    elif damage == 'unknown-cell':
        constraints['fixed'] = [{'instance': 'absent-cell', 'fpga': platform.fpgas[0].id}]
    elif damage == 'capacity':
        constraints['resource_reservations_by_fpga'] = {fpga.id: {'lut': fpga.effective_capacity['lut']} for fpga in platform.fpgas}
    elif damage == 'balance':
        constraints['balance_tolerance'] = -1.0
    elif damage == 'minimum':
        constraints['min_used_fpgas'] = len(platform.fpgas)+1
    else:
        constraints['active_fpgas'] = [platform.fpgas[0].id]
        constraints['min_used_fpgas'] = 1
    with pytest.raises(ValidationError):
        select_assignment(platform, clusters, dict(initial, constraints=constraints), dict(candidate, constraints=constraints))


@pytest.mark.parametrize('values', [(0.5, 1.5, -0.5, -1.5), (2.5, -2.5, 3.5, -3.5), (1e30, -1e30, 1e100, -1e100)])
def test_native_objective_ranks_match_python_ties_to_even_and_large_integers(decision, values):
    args, initial, candidate, trace = decision
    _, platform, clusters, *_ = args
    column = candidate['cluster_assignment']
    path = column.native_output_source.path
    data = bytearray(path.read_bytes())
    length = struct.unpack_from('<I', data, 16)[0]
    offset = 20+length
    trace = copy.deepcopy(trace)
    for side, key in enumerate(('initial_metrics', 'final_metrics')):
        objective = list(struct.unpack_from('<8d', data, offset+64*side))
        objective[0], objective[1] = values[side]*1e-9, values[side+2]*1e-9
        objective[2], objective[3] = values[side+2], values[side]
        struct.pack_into('<8d', data, offset+64*side, *objective)
        trace[key]['objective_key'] = objective
    struct.pack_into('<I', data, len(data)-4, zlib.crc32(data[:-4]))
    path.write_bytes(data)
    column.native_output_source = bind_store_source(path, 'patron-output-v1')
    expected, selection = _select_patron_timing_assignment(initial, candidate, trace)
    actual = select_assignment(platform, clusters, initial, candidate)
    assert actual['selection'] == selection


def test_native_audit_rejects_a_replaced_instance_view(decision):
    args, initial, _, _ = decision
    _, platform, clusters, *_ = args
    initial['instance_assignment']._cluster_parts = list(initial['instance_assignment']._cluster_parts)
    with pytest.raises(ValidationError, match='original ownership'):
        validate_partition_artifacts_online(platform, clusters, initial)


def test_mutated_solver_generation_is_never_rebound_or_retried(decision):
    args, initial, candidate, _ = decision
    _, platform, clusters, *_ = args
    candidate['cluster_assignment'].native_output_source.path.touch()
    with pytest.raises(ValidationError, match='generation'):
        select_assignment(platform, clusters, initial, candidate)


@pytest.mark.parametrize('damage', ['swapped-owner', 'changed-policy', 'changed-source', 'unbound-summary'])
def test_reused_native_audit_retains_original_owner_policy_and_generations(decision, damage):
    args, initial, candidate, _ = decision
    _, platform, clusters, *_ = args
    result = select_assignment(platform, clusters, initial, candidate)
    certificate = result['native_audits'][0]
    value = initial
    if damage == 'swapped-owner':
        value = candidate
    elif damage == 'changed-policy':
        value = dict(initial, constraints=dict(initial['constraints'], balance_tolerance=0.9))
    elif damage == 'changed-source':
        initial['cluster_assignment'].native_initial_source.path.touch()
    else:
        certificate = result['audits'][0]
    with pytest.raises(ValidationError):
        validate_partition_artifacts_online(platform, clusters, value, _native_audit=certificate)


def test_resealed_generation_cannot_hide_solver_checksum_damage(decision):
    args, initial, candidate, _ = decision
    _, platform, clusters, *_ = args
    owner = candidate['cluster_assignment']
    path = owner.native_output_source.path
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    owner.native_output_source = bind_store_source(path, 'patron-output-v1')
    with pytest.raises(ValidationError, match='checksum'):
        select_assignment(platform, clusters, initial, candidate)


def test_assignment_generation_borrows_ownership_without_host_column_iteration(decision):
    args, initial, candidate, _ = decision
    ir, platform, clusters, constraints, *_ = args
    column_type = type(initial['cluster_assignment'].part_indices)
    with patch.object(column_type, '__iter__', side_effect=AssertionError('host ownership scanned')):
        for value in (initial, candidate):
            actual = build_partition_assignment(ir, platform, clusters, constraints,
                value['cluster_assignment'], value['provider'], value['seed'],
                _include_semantic_contract=False, _stream_semantic_records=True,
                _defer_derived_records=True)
            assert actual['partitions'] == value['partitions']


def test_production_phase3_uses_native_decision_before_final_transaction(decision, tmp_path):
    args, initial, _, _ = decision
    ir, platform, _, constraints, manifest, routes = args
    import json
    from emuflow.phase3 import run_phase3
    board, limits, routing = (tmp_path/name for name in ('board.json', 'constraints.json', 'routing.json'))
    board.write_text(json.dumps(platform.to_dict()))
    limits.write_text(json.dumps(constraints))
    routing.write_text(json.dumps(routes))
    with patch('tests.phase3_decision_reference._select_patron_timing_assignment', side_effect=AssertionError('host candidate selector entered')):
        result = run_phase3(ir.path, board, tmp_path/'phase3', constraints_path=limits,
            tritonpart_solution=tmp_path/'initial.solution', timing_database_path=tmp_path/'timing.json',
            route_constraints_path=routing, patron_max_moves=12)
    assert result['algorithm_validation']['decision_implementation_id'] == 'native-partition-decision-v1'
    assert result['finalization_implementation_id'] == 'native-partition-publication-v1'


def test_original_nonlexical_part_coordinates_remain_a_borrowed_view(inputs, tmp_path):
    args, reference, timing = inputs
    ir, platform, clusters, constraints, manifest, routes = args
    from emuflow.platform import Platform
    from emuflow.partition import _TranslatedPartColumn
    document = platform.to_dict()
    document['fpgas'].reverse()
    platform = Platform.from_dict(document)
    parts = tuple(fpga.id for fpga in platform.fpgas)
    imported = tmp_path/'nonlexical.solution'
    imported.write_text(''.join(str(parts.index(reference['cluster_assignment'][f'c{i:06d}']))+'\n'
                                for i in range(len(clusters['clusters']))))
    owner, _ = initialize(ir, platform, clusters, constraints, seed=0, executable=None, solution_input=imported)
    column_type = type(owner.part_indices)
    with patch.object(column_type, '__iter__', side_effect=AssertionError('host coordinate translation')):
        initial = build_partition_assignment(ir, platform, clusters, constraints, owner,
            'tritonpart', 0, _include_semantic_contract=False,
            _stream_semantic_records=True, _defer_derived_records=True)
    assert isinstance(initial['instance_assignment']._cluster_parts, _TranslatedPartColumn)
    args = (ir, platform, clusters, constraints, manifest, routes)
    model = build_native_pressure_model(*args, timing_database_path=timing)
    candidate, trace = run(args, model, initial, max_moves=12)
    result = select_assignment(platform, clusters, initial, candidate)
    assert result['selection'] == _select_patron_timing_assignment(initial, candidate, trace)[1]
