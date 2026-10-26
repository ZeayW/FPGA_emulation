"""Physical compilation and continuation require original engine clock edges."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import ValidationError
from emuflow.multi_fpga_flow import (_CompileContext, _compile_timing,
                                     _require_physical_source_timing,
                                     finalize_multi_fpga_physical_checkpoint)
from emuflow.opensta import DEFAULT_TIMING_MODEL
from emuflow.sta import iter_sta_path_database_paths
from tests.test_opensta_communication_research import prepare_source


def selector():
    # Admission metadata only. Indexed row integrity is checked by the importer
    # and prepared-input binder, not by this bounded controller gate.
    return dict(schema='emuflow.sta-path-database/v4', source=dict(
        model_pin_coverage='checked-source-emission-directions-v1',
        source_timing_coverage=dict(policy='engine-no-clock-unconstrained-loops-v1', status='pass'),
        clock_budget_policy='engine-edge-v1',
        path_selection=dict(policy='worst-per-endpoint-per-path-group-v1',
                            max_paths_per_group=None, endpoint_path_count=1)),
        payloads=dict(paths=dict(format='sqlite-sta-path/v2', records=2)),
        summary=dict(clock_budget_paths=2))


@pytest.mark.parametrize('damage', ['policy', 'schema', 'format', 'coverage',
                                  'boolean-count', 'missing-count', 'capped', 'missing-selection',
                                  'missing-source-coverage', 'failed-source-coverage'])
def test_legacy_or_partial_prepared_clock_metadata_cannot_enter_physical_flow(tmp_path, damage):
    value = selector()
    if damage == 'policy': value['source']['clock_budget_policy'] = 'legacy-absolute-arrival-v1'
    elif damage == 'schema': value['schema'] = 'emuflow.sta-path-database/v3'
    elif damage == 'format': value['payloads']['paths']['format'] = 'sqlite-sta-path/v1'
    elif damage == 'coverage': value['summary']['clock_budget_paths'] = 1
    elif damage == 'boolean-count':
        value['payloads']['paths']['records'] = 1
        value['summary']['clock_budget_paths'] = True
    elif damage == 'missing-count': del value['summary']['clock_budget_paths']
    elif damage == 'capped': value['source']['path_selection']['max_paths_per_group'] = 200000
    elif damage == 'missing-selection': del value['source']['path_selection']
    elif damage == 'missing-source-coverage': del value['source']['source_timing_coverage']
    elif damage == 'failed-source-coverage': value['source']['source_timing_coverage']['status']='failed'
    path = tmp_path / 'paths.json'
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match='Phase2|clock budgets'):
        _require_physical_source_timing(path)


def context(root, source, *, physical=True, engine=None):
    return _CompileContext(dict(output_dir=root, ir_path=source,
        prepared_timing=None, internal_timing_database=True, physical=physical,
        clock_periods={'clk': 10.}, timing_io={
            'input_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['a', 'b']}],
            'output_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['q']}]},
        timing_model=DEFAULT_TIMING_MODEL, architecture_timing_db=None,
        opensta=engine, sta_max_paths=None, timing_driven=False,
        route_constraints=None, board_link_timing_db=None))


@pytest.mark.parametrize('physical', [False, True])
def test_fresh_physical_compile_explicitly_requests_engine_edge_export(tmp_path, physical):
    ctx = context(tmp_path, tmp_path / 'source.sqlite', physical=physical)
    def export(**kwargs):
        Path(kwargs['output_path']).write_text(json.dumps(selector()))
        return dict(status='pass')
    with patch('emuflow.multi_fpga_flow.run_opensta_path_database', side_effect=export) as run, \
         patch('emuflow.multi_fpga_flow.sta_path_database_payload_path', return_value=tmp_path / 'paths.sqlite'):
        _compile_timing(ctx)
    assert run.call_args.kwargs['clock_budget_policy'] == (
        'engine-edge-v1' if physical else 'legacy-absolute-arrival-v1')


def test_continuation_cannot_bypass_source_clock_admission(tmp_path):
    timing = tmp_path / 'timing'
    timing.mkdir()
    value = selector()
    value['source']['clock_budget_policy'] = 'legacy-absolute-arrival-v1'
    (timing / 'path-database.json').write_text(json.dumps(value))
    with patch('emuflow.multi_fpga_flow.run_phase7c') as run:
        with pytest.raises(ValidationError, match='engine-edge clock budgets'):
            finalize_multi_fpga_physical_checkpoint(tmp_path, tmp_path / 'physical')
    run.assert_not_called()


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'), reason='qualified real OpenSTA required')
@pytest.mark.parametrize('negative_capture', [False, True])
def test_real_physical_phase2_retains_full_and_half_cycle_capture_edges(tmp_path, negative_capture):
    source = prepare_source(tmp_path, negative_capture=negative_capture)
    ctx = context(tmp_path / 'flow', source, engine=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'])
    _compile_timing(ctx)
    value = json.loads(ctx.path_database_path.read_text())
    row, = [r for r in iter_sta_path_database_paths(ctx.path_database_path, value)
            if r['endpoint']['instance'] == 'capture']
    assert value['source']['clock_budget_policy'] == 'engine-edge-v1'
    assert value['summary']['clock_budget_paths'] == value['payloads']['paths']['records']
    facts = row['clock_budget']
    assert facts['launch_edge_ns'] == pytest.approx(0., abs=1e-6)
    assert facts['capture_edge_ns'] == pytest.approx(5. if negative_capture else 0., abs=1e-6)
    assert facts['target_cycle_time_ns'] == pytest.approx(5. if negative_capture else 10., abs=1e-6)
