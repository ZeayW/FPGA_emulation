"""Independent full-graph maxima must follow each new assignment profile."""
import json
import os
from unittest.mock import patch

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.opensta_source_context import emit_opensta_source_context
from emuflow.patron_source_checkpoints import (PatronSourceCheckpoint,
    check_patron_source_assignments, evaluate_patron_source_checkpoints)
from emuflow.store_publication import VerifiedStoreFile, bind_store_source
from tests.test_opensta_communication_research import prepare_source


pytestmark = pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),
                               reason='qualified real OpenSTA required')


def context(root, *, negative=False, io=True, fast_name='fast'):
    source = prepare_source(root, negative_capture=negative, fast_name=fast_name)
    return emit_opensta_source_context(bind_store_source(source, 'emuir-compact-v2'),
        root/'context', clocks={'clk': 10.}, timing_io=dict(
            input_groups=[dict(clock='clk', delay_ns=0., ports=['a', 'b'])] if io else [],
            output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'])


def profile(path, rows):
    path.write_text('from_pin_hex\tto_pin_hex\tphysical_hops\tmaximum_net_route_ratio\tpredicted_delay_ns\n'
        + ''.join(f'{a.encode().hex()}\t{b.encode().hex()}\t{h}\t{r}\t{d}\n' for a,b,h,r,d in rows))
    return VerifiedStoreFile(path, 'patron-source-proxy-tsv-v1')


ROWS = [('launch_a/Q', 'slow0/I0', 1, 248, 110.),
        ('launch_b/Q', 'fast/I0', 2, 16, 75.), ('fast/O', 'join/I1', 1, 24, 45.)]


@pytest.mark.parametrize('negative', [False, True])
def test_independent_maxima_and_critical_witnesses_change_and_restore_in_one_graph(tmp_path, negative):
    source = context(tmp_path, negative=negative)
    costly = profile(tmp_path/'costly.tsv', ROWS)
    local = profile(tmp_path/'local.tsv', [])
    output = tmp_path/'checkpoints'
    with patch('emuflow.ir.EmuIR.load', side_effect=AssertionError('second Python graph')):
        result = evaluate_patron_source_checkpoints(source, [
            PatronSourceCheckpoint('candidate', costly), PatronSourceCheckpoint('local', local),
            PatronSourceCheckpoint('restored', costly)], output, top='retime', export_feedback=True)
    assert result['engine_contexts'] == 1
    first, zero, repeated = result['checkpoints']
    assert first['maximum_physical_hops'] == 3
    assert first['maximum_path_route_ratio_sum'] == 248
    assert first['worst_slack_ns'] == pytest.approx((5. if negative else 10.)-.34-120.-.08, abs=2e-5)
    assert first['total_negative_slack_ns'] < 0
    assert {k:v for k,v in first.items() if k != 'checkpoint'} == {
        k:v for k,v in repeated.items() if k != 'checkpoint'}
    assert zero['maximum_physical_hops'] == zero['maximum_path_route_ratio_sum'] == 0
    assert zero['worst_slack_ns'] > 0 and zero['total_negative_slack_ns'] == 0
    assert json.loads((output/'summary.json').read_text()) == result
    rows = [line.split('\t') for line in (output/'witnesses.tsv').read_text().splitlines()[1:]]
    chosen = {row[1]:row for row in rows if bytes.fromhex(row[0]).decode() == 'candidate'}
    for metric, expected, score in [('physical-hops', 'launch_b/Q', 3),
                                   ('path-route-ratio-sum', 'launch_a/Q', 248)]:
        row = chosen[metric]
        assert int(row[3]) == score
        assert bytes.fromhex(row[4]).decode() == expected
    assert bytes.fromhex(chosen['setup'][4]).decode() == 'launch_b/Q'
    for row in rows:
        pins = [bytes.fromhex(p).decode() for p in row[6].split(',')]
        assert pins[0] == bytes.fromhex(row[4]).decode()
        assert pins[-1] == bytes.fromhex(row[5]).decode()
        assert 'clk' not in pins
    from emuflow.sta import iter_sta_path_database_paths, validate_sta_path_database
    feedback = output/result['feedback']['selector']
    manifest = json.loads(feedback.read_text())
    paths = list(iter_sta_path_database_paths(feedback, manifest))
    assert validate_sta_path_database(feedback, source.source_bindings[0].path)['paths'] == len(rows)
    assert len(paths) == result['feedback']['paths'] == len(rows)
    captures = [p for p in paths if p['endpoint']['instance'] == 'capture']
    assert {'launch_a','launch_b'} <= {p['startpoint']['instance'] for p in captures}
    for path in captures:
        expected = .54 if path['startpoint']['instance'] == 'launch_a' else .34
        assert path['fixed_delay_ns'] == pytest.approx(expected, abs=2e-6)
        assert path['slack_ns'] == pytest.approx((5. if negative else 10.)-expected-.08, abs=2e-6)
        assert path['clock_budget']['check_margin_ns'] == pytest.approx(.08, abs=2e-6)
        assert path['clock_budget']['capture_edge_ns'] == pytest.approx(5. if negative else 0., abs=2e-6)


@pytest.mark.parametrize('name', ['fast[0:0]','fast*literal?','fast\\雪/hier'])
def test_feedback_replay_uses_literal_data_pin_names(tmp_path, name):
    source = context(tmp_path, fast_name=name)
    escaped = name.replace('\\','\\\\')
    rows = [(a.replace('fast/',escaped+'/'),b.replace('fast/',escaped+'/'),h,r,d)
            for a,b,h,r,d in ROWS]
    costs = profile(tmp_path/'costs.tsv', rows)
    result = evaluate_patron_source_checkpoints(source, [PatronSourceCheckpoint('literal',costs)],
        tmp_path/'literal',top='retime',export_feedback=True)
    assert result['feedback']['paths'] == result['checkpoints'][0]['witnesses']


def test_feedback_replay_preserves_primary_input_to_output_chain(tmp_path):
    from emuflow.sta import iter_sta_path_database_paths
    from emuflow.yosys import import_yosys_json
    prepare_source(tmp_path)
    path = tmp_path/'source.json'
    value = json.loads(path.read_text())
    design = value['modules']['retime']
    ff = design['cells']['launch_b']
    ff['connections']['Q'] = [17]
    design['cells'] = dict(dummy=ff,
        driver=dict(type='LUT1',parameters={'INIT':'10'},
            port_directions=dict(I0='input',O='output'),connections=dict(I0=[3],O=[10])),
        sink=dict(type='LUT1',parameters={'INIT':'10'},
            port_directions=dict(I0='input',O='output'),connections=dict(I0=[10],O=[11])))
    design['ports']['q']['bits'] = [11]
    design['ports']['dummy_q'] = dict(direction='output',bits=[17])
    path.write_text(json.dumps(value))
    original = tmp_path/'primary.emuir.sqlite'
    import_yosys_json(path,top='retime',clocks=['clk']).save(original)
    source = emit_opensta_source_context(bind_store_source(original,'emuir-compact-v2'),
        tmp_path/'context',clocks={'clk':10.},timing_io=dict(
            input_groups=[dict(clock='clk',delay_ns=0.,ports=['a','b'])],
            output_groups=[dict(clock='clk',delay_ns=0.,ports=['q','dummy_q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'])
    costs = profile(tmp_path/'costs.tsv',[('driver/O','sink/I0',1,8,30.)])
    output = tmp_path/'primary-feedback'
    result = evaluate_patron_source_checkpoints(source,[PatronSourceCheckpoint('primary',costs)],
        output,top='retime',export_feedback=True)
    selector = output/result['feedback']['selector']
    paths = list(iter_sta_path_database_paths(selector,json.loads(selector.read_text())))
    assert len(paths) == 3
    assert all(p['startpoint']['instance'] is p['endpoint']['instance'] is None for p in paths)
    assert all(p['startpoint']['port']=='a' and p['endpoint']['port']=='q' for p in paths)
    assert all(p['fixed_delay_ns'] == pytest.approx(.2,abs=2e-6) for p in paths)


@pytest.mark.parametrize('cell_type,pin', [('FDCE','CLR'),('FDPE','PRE')])
@pytest.mark.parametrize('negative', [False,True])
def test_feedback_retains_recovery_control_check_and_clock_edges(tmp_path,cell_type,pin,negative):
    from emuflow.sta import iter_sta_path_database_paths
    from tests.test_opensta_ff_control_coverage import source_fixture
    _,original = source_fixture(tmp_path,cell_type,pin,negative)
    source = emit_opensta_source_context(bind_store_source(original,'emuir-compact-v2'),
        tmp_path/'context',clocks={'clk':10.},timing_io=dict(
            input_groups=[dict(clock='clk',delay_ns=0.,ports=['a'])],
            output_groups=[dict(clock='clk',delay_ns=0.,ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'])
    costs = profile(tmp_path/'costs.tsv',[('launch/Q','delay/I0',1,8,30.)])
    output = tmp_path/'recovery'
    result = evaluate_patron_source_checkpoints(source,[PatronSourceCheckpoint('recovery',costs)],
        output,top='controls',export_feedback=True)
    selector = output/result['feedback']['selector']
    paths = list(iter_sta_path_database_paths(selector,json.loads(selector.read_text())))
    captures = [p for p in paths if p['endpoint']['instance']=='capture']
    assert len(captures) == 3
    for path in captures:
        assert path['endpoint']['instance']=='capture' and path['endpoint']['port']==pin
        assert path['fixed_delay_ns'] == pytest.approx(.22,abs=2e-6)
        assert path['clock_budget']['check_margin_ns'] == pytest.approx(.08,abs=2e-6)
        assert path['clock_budget']['capture_edge_ns'] == pytest.approx(5. if negative else 0.,abs=2e-6)


@pytest.mark.parametrize('damage', ['unconstrained', 'wrong-pin', 'integer-overflow', 'changed-lease'])
def test_failed_full_graph_checkpoint_never_publishes_a_partial_result(tmp_path, damage):
    source = context(tmp_path, io=damage != 'unconstrained')
    rows = ROWS
    if damage == 'wrong-pin': rows = [('absent/Q', 'fast/I0', 1, 8, 30.)]
    if damage == 'integer-overflow':
        rows = [('launch_b/Q','fast/I0',4194304,8,30.), ('fast/O','join/I1',4194304,8,30.)]
    costs = profile(tmp_path/'costs.tsv', rows)
    if damage == 'changed-lease': (tmp_path/'costs.tsv').touch()
    output = tmp_path/'rejected'
    with pytest.raises((EmuFlowError, ValidationError)):
        evaluate_patron_source_checkpoints(source, [
            PatronSourceCheckpoint('first', profile(tmp_path/'first.tsv', ROWS)),
            PatronSourceCheckpoint('bad', costs)], output, top='retime')
    assert not output.exists()


def test_native_assignment_costs_feed_the_source_graph_without_a_selected_path_payload(tmp_path):
    from emuflow.opensta import run_opensta_path_database
    from emuflow.partition import (build_clusters, build_partition_assignment,
                                  normalize_partition_constraints, CUT_MODE_STATIC_EXACT)
    from emuflow.partition_ir import load_partition_ir
    from emuflow.partition_pressure import build_partition_pressure_model,run_partition_pressure_native
    from emuflow.platform import Platform
    from emuflow.routing import normalize_route_constraints
    from tests.test_phase5 import _platform_value, _link
    source = context(tmp_path)
    ir_path = source.source_bindings[0].path
    timing = tmp_path/'timing.json'
    run_opensta_path_database(ir_path, timing, clocks={'clk':10.}, timing_io=dict(
        input_groups=[dict(clock='clk',delay_ns=0.,ports=['a','b'])],
        output_groups=[dict(clock='clk',delay_ns=0.,ports=['q'])]),
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'], _stream_output=True,
        clock_budget_policy='engine-edge-v1')
    platform = Platform.from_dict(_platform_value('board', ['a','b'], [_link('ab','a','b')]))
    ir = load_partition_ir(ir_path)
    constraints = normalize_partition_constraints(dict(schema='emuflow.partition-constraints/v1',
        min_used_fpgas=2,balance_tolerance=1.), ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode=CUT_MODE_STATIC_EXACT, _native_compact_clusters=True)
    initial = build_partition_assignment(ir, platform, clusters, constraints,
        {f'c{i:06d}':('a' if i%2==0 else 'b') for i in range(len(clusters['clusters']))},
        'fixture',7,_include_semantic_contract=False,_stream_semantic_records=True,_defer_derived_records=True)
    routes = normalize_route_constraints(None, platform)
    model = build_partition_pressure_model(ir, platform, clusters, constraints,
        json.loads(timing.read_text()), routes, independent_validation=False,
        timing_database_path=timing, native_store_model=True)
    model.timing_path.unlink()
    output = tmp_path/'native-checkpoint'
    result = check_patron_source_assignments(model, source, [('actual-assignment',initial)], output)
    row, = result['checkpoints']
    assert row['maximum_physical_hops'] > 0
    assert row['maximum_path_route_ratio_sum'] > 0
    assert result['profiles'][0]['producer']['selected_paths_loaded'] == 0
    assert (output/result['profiles'][0]['path']).is_file()
    assert (output/result['witnesses']['path']).is_file()
    feedback = output/result['feedback']['selector']
    proposal = build_partition_pressure_model(ir,platform,clusters,constraints,
        json.loads(feedback.read_text()),routes,independent_validation=False,
        timing_database_path=feedback,native_store_model=True)
    candidate,trace = run_partition_pressure_native(ir,platform,clusters,constraints,routes,proposal,
        initial,max_moves=1,algorithm_version=14,retain_trace_seals=False,stream_semantic_records=True,
        output_validation='caller',_release_model_before_assignment=True,_defer_candidate_derived_records=True)
    assert trace['model_counts']['paths'] == result['feedback']['paths']
    # A witness-guided proposal still needs a new complete-graph evaluation;
    # its selected-path trace is never the global acceptance result.
    checked = check_patron_source_assignments(model,source,[('proposal',candidate)],tmp_path/'rechecked')
    assert checked['engine_contexts'] == 1 and checked['checkpoints'][0]['witnesses'] > 0
