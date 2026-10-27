"""Whole-assignment physical coverage has one native typed owner."""
from tests.phase3_store_fixture import write_assignment_index_fixture
import json
import os
import shutil
import sqlite3
from unittest.mock import patch

import pytest

from emuflow.cross_layer_timing import build_cross_layer_timing_contract
from emuflow.errors import ValidationError
from emuflow.physical_coverage import (
    PhysicalCoverageView, build_physical_coverage, audit_physical_coverage,
    read_physical_coverage_certificate, replay_physical_coverage,
)
from tests.test_global_binding_native import physical_case


@pytest.fixture(autouse=True)
def native_runtime(tmp_path, monkeypatch):
    if not os.environ.get('EMUFLOW_NATIVE_ROOT'):
        from tests.test_native_schedule_generation import install_runtime
        install_runtime(tmp_path, monkeypatch)


def case(tmp_path, *, mutate=None, members=1):
    from tests.test_combinational_cut import StaticExactCombinationalCutPartitionTest
    f, runtime, routes, schedule, physical, iroutes, ischedule, indexed = physical_case(
        tmp_path, mutate=mutate, members=members)
    original = StaticExactCombinationalCutPartitionTest(); original.setUp()
    assignment = tmp_path/'assignment.sqlite'
    write_assignment_index_fixture(assignment, original.ir, f.assignment)
    contract = build_cross_layer_timing_contract(
        iroutes, ischedule, {**f.assignment['semantic_contract'], 'indexed': True})
    return f, contract, ischedule, indexed, assignment, physical, schedule


def test_native_assignment_coverage_records_and_small_publication(tmp_path):
    f, contract, schedule, physical, assignment, original, raw_schedule = case(tmp_path, members=7)
    from emuflow.tdm_schedule_storage import ScheduleRecordSequence, TypedScheduleEntrySequence
    from emuflow.logic_segment_timing_storage import LogicSegmentTimingView
    with patch.object(ScheduleRecordSequence, '__iter__', side_effect=AssertionError('Python hop population')), \
         patch.object(TypedScheduleEntrySequence, '__iter__', side_effect=AssertionError('Python hop population')), \
         patch.object(LogicSegmentTimingView, 'iter_ordered_segments', side_effect=AssertionError('Python segment population')):
        binding = build_physical_coverage(contract, schedule, physical, f.platform, assignment,
                                          output_path=tmp_path/'coverage.json')
    try:
        assert binding['status'] == 'pass'
        assert binding['metrics']['required_endpoints'] == 2*len(raw_schedule['entries'])
        expected_endpoints = {
            row['id']: (fpga, row['kind'], row['schedule_entry'], row['delay_ns'])
            for fpga, database in original['boundary_timing'].items() for row in database['endpoints']}
        assert {row['endpoint']: (row['fpga'], row['kind'], row['schedule_entry'],
                row['measured_min_delay_ns']) for row in binding['endpoint_bindings']} == expected_endpoints
        for row in binding['endpoint_bindings']:
            assert row['measured_min_delay_ns'] == row['measured_max_delay_ns']
        expected_segments = {row['id']: row['fpga'] for row in f.assignment['semantic_contract']['logic_segments']}
        assert {row['segment']: row['fpga'] for row in binding['logic_segment_bindings']} == expected_segments
        expected_delays = {'segment000000': 3.0, 'segment000001': 2.5, 'segment000002': 7.0}
        assert {row['segment']: row['measured_max_delay_ns'] for row in binding['logic_segment_bindings']} == expected_delays
        assert list(binding['missing']['endpoints']) == list(binding['missing']['logic_segments']) == []
        assert len(binding['board_bindings']) == len(raw_schedule['entries'])
        assert audit_physical_coverage(contract, schedule, physical, f.platform, binding, assignment)['status'] == 'pass'
        selector = json.loads((tmp_path/'coverage.json').read_text())
        assert selector['schema'] == 'emuflow.cross-layer-physical-binding-certificate/v1'
        assert set(selector) == {'schema','sources','logical_sha256','header','sections'}
        assert not any(key in selector for key in ('endpoint_bindings','logic_segment_bindings','board_bindings','missing'))
        assert (tmp_path/'coverage.json').stat().st_size < 16384
        with replay_physical_coverage(contract,schedule,physical,f.platform,tmp_path/'coverage.json',assignment) as replay:
            assert list(replay['endpoint_bindings']) == list(binding['endpoint_bindings'])
            assert replay.metadata['logical_sha256'] == binding.metadata['logical_sha256']
    finally:
        binding.close()


def test_all_semantic_segments_are_covered_even_outside_original_path_population(tmp_path):
    from tests.physical_coverage_fixture import build_cross_layer_physical_binding
    f, _, _, _, _, _, _, _ = physical_case(tmp_path)
    from tests.cross_layer_facts import coverage_contract_facts
    contract = coverage_contract_facts(f.routes, f.schedule, f.assignment['semantic_contract'])
    contract['logic_segment_bindings'].extend([
        {'segment':'unmeasured-outside-original-paths','kind':'rx_to_capture','fpga':'fpga0'},
        {'segment':'configuration-only','kind':'launch_to_tx','fpga':'fpga0',
         'source_semantics':'configuration-stable-constant'},
    ])
    with build_cross_layer_physical_binding(contract, f.schedule, f._system_physical(), f.platform) as binding:
        assert binding['status'] == 'incomplete'
        assert list(binding['missing']['logic_segments']) == ['unmeasured-outside-original-paths']
        constant = next(row for row in binding['logic_segment_bindings'] if row['segment']=='configuration-only')
        assert constant['measured_min_delay_ns'] == constant['measured_max_delay_ns'] == 0.0
        assert constant['measurement'] == 'structural-configuration-stable-constant'
        assert binding['metrics']['required_logic_segments'] == len(f.assignment['semantic_contract']['logic_segments'])+2


@pytest.mark.parametrize('damage', ['unknown-semantic', 'wrong-fpga', 'global-duplicate', 'missing-endpoints', 'missing-logic'])
def test_native_coverage_rejects_identity_damage_and_preserves_missing_evidence(tmp_path, damage):
    def mutate(routes, schedule, physical):
        launch = physical['logic_segment_timing']['fpga0']['segments'][0]
        transition = physical['logic_segment_timing']['fpga1']['segments'][0]
        if damage == 'unknown-semantic': launch['static_exact_segment_id'] = 'unowned-segment'
        elif damage == 'wrong-fpga': launch['static_exact_segment_id'] = transition['static_exact_segment_id']
        elif damage == 'global-duplicate': transition['id'] = launch['id']
        elif damage == 'missing-endpoints':
            physical['boundary_timing'].clear(); physical['boundary_identities'].clear()
        else:
            physical['logic_segment_timing'].clear()
    f, contract, schedule, physical, assignment, _, raw_schedule = case(tmp_path, mutate=mutate)
    if not damage.startswith('missing-'):
        expected = r'UNIQUE constraint failed: observed_logic\.id' if damage=='global-duplicate' else 'physical coverage|physical evidence'
        with pytest.raises(ValidationError, match=expected) as rejected:
            build_physical_coverage(contract, schedule, physical, f.platform, assignment)
        assert '"stage":"physical.build-coverage"' in str(rejected.value)
        assert '"code":"invalid-artifact"' in str(rejected.value)
        return
    with build_physical_coverage(contract, schedule, physical, f.platform, assignment) as binding:
        assert binding['status'] == 'incomplete'
        if damage == 'missing-endpoints':
            assert set(binding['missing']['endpoints']) == {
                '__emuflow_'+kind+'_'+row['id'] for row in raw_schedule['entries'] for kind in ('tx','rx')}
        else:
            assert set(binding['missing']['logic_segments']) == {
                row['id'] for row in f.assignment['semantic_contract']['logic_segments']}
        assert audit_physical_coverage(contract, schedule, physical, f.platform, binding, assignment)['status'] == 'incomplete'


def test_native_independent_coverage_audit_rejects_resealed_output_facts(tmp_path):
    from emuflow.physical_coverage import _inputs, _audit
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    with build_physical_coverage(contract, schedule, physical, f.platform, assignment) as binding:
        damaged = tmp_path/'damaged.sqlite'; shutil.copyfile(binding.store_path, damaged)
        with sqlite3.connect(damaged) as connection:
            connection.execute('UPDATE logic_segment_bindings SET measured_max_delay_ns=measured_max_delay_ns+1')
        sources, config = _inputs(contract, schedule, physical, f.platform, assignment)
        with pytest.raises(ValidationError, match='published logic_segment_bindings'):
            _audit(sources, config, damaged, tmp_path)


def test_native_changed_source_does_not_replace_published_selector(tmp_path):
    import emuflow.physical_coverage as coverage
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    target = tmp_path/'coverage.json'
    with build_physical_coverage(contract, schedule, physical, f.platform, assignment, output_path=target):
        previous = target.read_bytes()
    invoke = coverage._native
    def changed(arguments, root):
        if arguments[0] == 'build-physical-coverage':
            with sqlite3.connect(physical['logic_segment_timing']['fpga0'].store_path) as connection:
                connection.execute('UPDATE segments SET delay_ns=delay_ns+1')
        return invoke(arguments, root)
    with patch.object(coverage, '_native', changed), pytest.raises(ValidationError, match='generation|changed'):
        build_physical_coverage(contract, schedule, physical, f.platform, assignment, output_path=target)
    assert target.read_bytes() == previous


def test_production_rejects_inline_and_missing_assignment_owner(tmp_path):
    f, contract, schedule, physical, assignment, original, raw_schedule = case(tmp_path)
    with pytest.raises(ValidationError, match='inline production is retired'):
        build_physical_coverage(contract, raw_schedule, original, f.platform, assignment)
    with pytest.raises(ValidationError, match='original assignment owner'):
        build_physical_coverage(contract, schedule, physical, f.platform)


def test_direct_native_request_rebound_to_changed_schedule_rows_rejects_old_logical_seal(tmp_path):
    from emuflow.physical_coverage import _inputs, _request, _native
    from emuflow.store_publication import bind_store_source
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    sources, config = _inputs(contract, schedule, physical, f.platform, assignment)
    schedule_path = sources[config['schedule']].path
    with sqlite3.connect(schedule_path) as connection:
        connection.execute('UPDATE schedule_entries SET lane=lane+1 WHERE seq=0')
    # This deliberately bypasses the Python view's prior-generation guard and
    # rebinds the direct native request. The old metadata must still fail.
    sources[config['schedule']] = bind_store_source(schedule_path, 'tdm-schedule-store-v4')
    request = tmp_path/'rebound.bin'; output = tmp_path/'rebound.sqlite'
    _request(request, sources, config)
    with pytest.raises(ValidationError, match='schedule entries logical seal disagrees'):
        _native(['build-physical-coverage',*(source.path for source in sources),request,output],tmp_path)
    assert not output.exists()


def test_source_change_during_selector_commit_restores_prior_generation_and_retires_new_payload(tmp_path, monkeypatch):
    import emuflow.store_publication as publication
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    target = tmp_path/'coverage.json'
    with build_physical_coverage(contract, schedule, physical, f.platform, assignment, output_path=target):
        previous = target.read_bytes()
    scratch = set(tmp_path.glob('.coverage-work-*'))
    physical['qualification'] = 'second-qualification'
    write = publication.write_json
    def change_after_publication(path, artifact, **kwargs):
        write(path, artifact, **kwargs)
        if path == target:
            with sqlite3.connect(physical['logic_segment_timing']['fpga0'].store_path) as connection:
                connection.execute('UPDATE segments SET delay_ns=delay_ns+1')
    monkeypatch.setattr(publication, 'write_json', change_after_publication)
    with pytest.raises(ValidationError, match='source generation changed'):
        build_physical_coverage(contract, schedule, physical, f.platform, assignment, output_path=target)
    assert target.read_bytes() == previous
    assert not list(tmp_path.glob('physical-coverage.*.sqlite'))
    assert set(tmp_path.glob('.coverage-work-*')) == scratch


def test_terminal_certificate_audits_after_private_scratch_retirement(tmp_path):
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    target=tmp_path/'coverage.json'
    view=build_physical_coverage(contract,schedule,physical,f.platform,assignment,output_path=target)
    scratch=view.store_path.parent
    assert scratch.is_dir()
    view.close()
    assert not scratch.exists()
    assert not list(tmp_path.glob('physical-coverage.*.sqlite'))
    certificate=read_physical_coverage_certificate(target)
    assert certificate['header']['status']=='pass'
    assert audit_physical_coverage(contract,schedule,physical,f.platform,target,assignment)['status']=='pass'
    with pytest.raises(ValidationError,match='owner is closed'):
        list(view['endpoint_bindings'])
    with replay_physical_coverage(contract,schedule,physical,f.platform,target,assignment) as replay:
        assert len(replay['endpoint_bindings'])==certificate['sections']['endpoint_bindings']['records']
    original=target.read_bytes()
    for field in ('generation','path','schema'):
        changed=json.loads(original)
        if field=='generation':changed['sources'][0]['generation'][1]+=1
        elif field=='path':changed['sources'][0]['path']+='-foreign-owner'
        else:changed['sources'][0]['schema']='tdm-schedule-selector-v999'
        target.write_text(json.dumps(changed))
        with pytest.raises(ValidationError,match='canonical source facts'):
            audit_physical_coverage(contract,schedule,physical,f.platform,target,assignment)


def test_native_certificate_conforms_to_public_schema(tmp_path):
    jsonschema=pytest.importorskip('jsonschema')
    from pathlib import Path
    f,contract,schedule,physical,assignment,_,_=case(tmp_path)
    with build_physical_coverage(contract,schedule,physical,f.platform,assignment) as view:
        schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/cross-layer-physical-binding-certificate-v1.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        validator=jsonschema.Draft202012Validator(schema)
        certificate=read_physical_coverage_certificate(view.path)
        validator.validate(certificate)
        invalid={**certificate,'endpoint_bindings':[]}
        with pytest.raises(jsonschema.ValidationError):validator.validate(invalid)
        invalid={**certificate,'sources':[{'path':'/source','schema':'tdm-schedule-store-v4','generation':[1,2]}]}
        with pytest.raises(jsonschema.ValidationError):validator.validate(invalid)


def test_assignment_generation_is_bound_before_reading_its_exact_descriptor(tmp_path):
    from emuflow import phase3_assignment_index as assignment_storage
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    original = assignment_storage._exact_binary_reference

    def replace_after_read(connection, source):
        result = original(connection, source)
        replacement = tmp_path / 'replacement-assignment.sqlite'
        shutil.copyfile(assignment, replacement)
        replacement.replace(assignment)
        return result

    with patch.object(assignment_storage, '_exact_binary_reference', side_effect=replace_after_read):
        with pytest.raises(ValidationError, match='source generation changed'):
            build_physical_coverage(contract, schedule, physical, f.platform, assignment,
                                    output_path=tmp_path / 'coverage.json')
    assert not (tmp_path / 'coverage.json').exists()


def test_failed_view_construction_closes_database_before_scratch_cleanup(tmp_path, monkeypatch):
    f, contract, schedule, physical, assignment, _, _ = case(tmp_path)
    with build_physical_coverage(contract, schedule, physical, f.platform, assignment,
                                 output_path=tmp_path/'coverage.json') as view:
        retained = []
        def reject(self):
            retained.append(self)
            raise ValidationError('rejected source generation')
        monkeypatch.setattr(PhysicalCoverageView, 'require', reject)
        with pytest.raises(ValidationError, match='rejected source generation'):
            PhysicalCoverageView(view.path, store_path=view.store_path, temporary=None)
        assert len(retained) == 1 and retained[0].closed
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            retained[0].connection.execute('SELECT 1')
