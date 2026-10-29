"""CE and synchronous/asynchronous reset paths must reach timing checks."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.indexed_timing import IndexedTimingView
from emuflow.opensta import (DEFAULT_TIMING_MODEL, classify_through_net_timing_endpoints,
    load_timing_model, render_opensta_liberty, run_opensta_path_database,
    build_architecture_opensta_timing_model)
from emuflow.sta import iter_sta_path_database_paths
from emuflow.yosys import import_yosys_json
from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract


CONTROLS = {'FDRE': 'R', 'FDSE': 'S', 'FDCE': 'CLR', 'FDPE': 'PRE'}


def source_fixture(root, cell_type, pin, negative=False):
    reset = CONTROLS[cell_type]
    launch = dict(type='FDRE', parameters={},
        port_directions=dict(C='input', D='input', Q='output', CE='input', R='input'),
        connections=dict(C=[2], D=[3], Q=[10], CE=['1'], R=['0']))
    connections = dict(C=[2], D=['0'], Q=[12], CE=['1'], **{reset: ['0']})
    connections[pin] = [11]
    cells = dict(launch=launch,
        delay=dict(type='LUT1', parameters={'INIT': '10'},
            port_directions=dict(I0='input', O='output'), connections=dict(I0=[10], O=[11])),
        capture=dict(type=cell_type, parameters={'IS_C_INVERTED': '1' if negative else '0'},
            port_directions={p: 'output' if p == 'Q' else 'input' for p in connections},
            connections=connections))
    yosys = root/'source.json'
    yosys.write_text(json.dumps({'modules': {'controls': dict(attributes={'top': '1'},
        ports=dict(clk=dict(direction='input', bits=[2]), a=dict(direction='input', bits=[3]),
                   q=dict(direction='output', bits=[12])), cells=cells, netnames={})}}))
    ir = import_yosys_json(yosys, top='controls', clocks=['clk'])
    path = root/'source.emuir.sqlite'
    ir.save(path)
    return ir, path


@pytest.mark.parametrize('cell_type', CONTROLS)
@pytest.mark.parametrize('enable', [False, True])
def test_source_classifier_counts_control_capture_pins_in_both_views(tmp_path, cell_type, enable):
    pin = 'CE' if enable else CONTROLS[cell_type]
    ir, source = source_fixture(tmp_path, cell_type, pin)
    net = next(n['id'] for n in ir.value['nets'] if any(
        s['instance'] == 'capture' and s['port'] == pin for s in n['sinks']))
    model = load_timing_model(DEFAULT_TIMING_MODEL)
    views = [ir, IndexedTimingView(source)]
    try:
        for view in views:
            row = classify_through_net_timing_endpoints(view, model, [net])[net]
            assert row['status'] == 'timed'
            assert row['direct_timed_endpoints'] == 1
            assert row['direct_timed_endpoint_pins'] == [f'capture/{pin}']
    finally:
        views[1].close()


@pytest.mark.parametrize('damage', ['missing', 'extra', 'kind', 'negative', 'boolean'])
def test_model_cannot_silently_omit_or_misdescribe_ff_control_checks(tmp_path, damage):
    model = json.loads(DEFAULT_TIMING_MODEL.read_text())
    checks = model['cells']['FDRE']['control_checks']
    if damage == 'missing': del checks['R']
    elif damage == 'extra': checks['D'] = dict(kind='setup', margin_ns=.1)
    elif damage == 'kind': checks['R']['kind'] = 'none'
    elif damage == 'negative': checks['R']['margin_ns'] = -.1
    else: checks['R']['margin_ns'] = True
    path = tmp_path/'model.json'; path.write_text(json.dumps(model))
    with pytest.raises(ValidationError): load_timing_model(path)


def test_architecture_uses_separate_device_control_margins_and_rejects_missing_values(tmp_path):
    ir, _ = source_fixture(tmp_path, 'FDCE', 'CLR')
    path = write_mock_xilinx_timing_contract(tmp_path/'architecture.json')
    model, _ = build_architecture_opensta_timing_model(ir, path)
    assert model['source']['ff_control_timing']['vivado_version'] == 'synthetic fixture'
    assert model['cells']['FDCE']['control_checks']['CLR'] == dict(kind='recovery', margin_ns=.55)
    assert model['cells']['FDRE']['control_checks']['CE']['margin_ns'] == pytest.approx(.45)
    assert 'timing_type : recovery_rising;' in render_opensta_liberty(model)
    value = json.loads(path.read_text()); del value['ff_control_timing']
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match='lacks device FF control timing'):
        build_architecture_opensta_timing_model(ir, path)


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'), reason='qualified real OpenSTA required')
@pytest.mark.parametrize('cell_type', CONTROLS)
@pytest.mark.parametrize('enable', [False, True])
@pytest.mark.parametrize('negative', [False, True])
def test_real_source_paths_include_control_endpoint_and_its_own_margin(tmp_path, cell_type, enable, negative):
    pin = 'CE' if enable else CONTROLS[cell_type]
    _, source = source_fixture(tmp_path, cell_type, pin, negative)
    model = json.loads(DEFAULT_TIMING_MODEL.read_text())
    model['cells'][cell_type]['control_checks'][pin]['margin_ns'] = .35
    model_path = tmp_path/'model.json'; model_path.write_text(json.dumps(model))
    output = tmp_path/'paths.json'
    run_opensta_path_database(source, output, clocks={'clk': 10.},
        timing_io=dict(input_groups=[dict(clock='clk', delay_ns=0., ports=['a'])],
                       output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
        timing_model_path=model_path, executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'],
        _stream_output=True, clock_budget_policy='engine-edge-v1', log_path=tmp_path/'opensta.log')
    rows = [r for r in iter_sta_path_database_paths(output, json.loads(output.read_text()))
            if r['endpoint']['instance'] == 'capture']
    assert len(rows) == 1
    row = rows[0]
    assert row['endpoint']['port'] == pin
    assert row['startpoint']['instance'] == 'launch'
    assert row['fixed_delay_ns'] == pytest.approx(.22, abs=2e-6)
    assert row['slack_ns'] == pytest.approx((5. if negative else 10.) - .22 - .35, abs=2e-6)
    assert row['clock_budget']['check_margin_ns'] == pytest.approx(.35, abs=2e-6)


@pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'), reason='qualified real OpenSTA required')
@pytest.mark.parametrize('cell_type', ['FDCE', 'FDPE'])
@pytest.mark.parametrize('negative', [False, True])
def test_real_unclocked_recovery_input_fails_source_coverage(tmp_path, cell_type, negative):
    from emuflow.opensta_source_context import emit_opensta_source_context
    from emuflow.store_publication import bind_store_source
    pin = CONTROLS[cell_type]
    source_fixture(tmp_path, cell_type, pin, negative)
    path = tmp_path/'source.json'; value = json.loads(path.read_text())
    top = value['modules']['controls']; capture = top['cells']['capture']
    capture['connections'][pin] = [3]
    top['cells'] = dict(capture=capture)
    path.write_text(json.dumps(value))
    source = tmp_path/'unclocked.emuir.sqlite'
    import_yosys_json(path, top='controls', clocks=['clk']).save(source)
    engine = os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE']
    context = emit_opensta_source_context(bind_store_source(source, 'emuir-compact-v2'), tmp_path/'context',
        clocks={'clk': 10.}, timing_io=dict(input_groups=[],
            output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]), executable=engine)
    script = tmp_path/'check.tcl'
    script.write_text('''source $env(PROBE_CONTEXT)
emuflow_read_design_context
set found 0
foreach error [sta::check_timing_cmd 0 0 0 1 1 1 0] {
 if {[lsearch -exact [lrange $error 1 end] $env(PROBE_CAPTURE)] >= 0} {set found 1}
}
if {!$found} {error "unclocked recovery input escaped coverage checks"}
puts "EMUFLOW_RECOVERY_COVERAGE status=pass"
exit 0
''')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([engine, '-no_init', '-exit', str(script)], text=True, capture_output=True,
        env={**os.environ, **context.environment, 'EMUFLOW_STA_TOP': 'controls',
             'PROBE_CONTEXT': str(root/'scripts/opensta/design_context.tcl'),
             'PROBE_CAPTURE': 'capture/'+pin})
    context.require_unchanged()
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EMUFLOW_RECOVERY_COVERAGE status=pass' in result.stdout
    # The production exporter must enforce that native result; a standalone
    # diagnostic warning is not sufficient to prevent a partial Phase2 owner.
    with pytest.raises(EmuFlowError, match='source timing coverage failed'):
        run_opensta_path_database(source, tmp_path/'invalid-paths.json', clocks={'clk': 10.},
            timing_io=dict(input_groups=[],
                output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
            executable=engine, _stream_output=True, clock_budget_policy='engine-edge-v1')
    assert not (tmp_path/'invalid-paths.json').exists()

    # An explicit arrival restores the actual recovery checks; the exporter
    # must not need a false path or constant reset to accept this source.
    output = tmp_path/'constrained-paths.json'
    run_opensta_path_database(source, output, clocks={'clk': 10.},
        timing_io=dict(input_groups=[dict(clock='clk', delay_ns=1.25, ports=['a'])],
            output_groups=[dict(clock='clk', delay_ns=0., ports=['q'])]),
        executable=engine, _stream_output=True, clock_budget_policy='engine-edge-v1')
    paths = list(iter_sta_path_database_paths(output, json.loads(output.read_text())))
    recovery = [row for row in paths if row['endpoint']['instance'] == 'capture'
                and row['endpoint']['port'] == pin]
    assert len(recovery) == 1
    assert recovery[0]['startpoint']['port'] == 'a'
    margin = load_timing_model(DEFAULT_TIMING_MODEL)['cells'][cell_type]['control_checks'][pin]['margin_ns']
    assert recovery[0]['slack_ns'] == pytest.approx((5. if negative else 10.)-1.25-margin, abs=2e-6)
