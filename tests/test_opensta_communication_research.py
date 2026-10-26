"""Communication re-search must discover previously unselected source paths."""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.errors import EmuFlowError, ValidationError
from emuflow.io import file_sha256
from emuflow.opensta import run_opensta_path_database
from emuflow.sta import iter_sta_path_database_paths, validate_sta_path_database
from emuflow.yosys import import_yosys_json


def prepare_source(root, *, negative_capture=False, fast_name='fast'):
    def ff(data, q, negative=False):
        return dict(type='FDRE', parameters={'IS_C_INVERTED': '1' if negative else '0'},
                    port_directions=dict(C='input', D='input', Q='output', CE='input', R='input'),
                    connections=dict(C=[2], D=[data], Q=[q], CE=['1'], R=['0']))

    def buffer(data, out):
        return dict(type='LUT1', parameters={'INIT': '10'},
                    port_directions=dict(I0='input', O='output'), connections=dict(I0=[data], O=[out]))

    cells = {'launch_a': ff(3, 10), 'launch_b': ff(4, 11),
             'slow0': buffer(10, 12), 'slow1': buffer(12, 13), 'slow2': buffer(13, 14),
             'fast': buffer(11, 15), 'capture': ff(16, 17, negative_capture),
             'join': dict(type='LUT2', parameters={'INIT': '1110'},
                          port_directions=dict(I0='input', I1='input', O='output'),
                          connections=dict(I0=[14], I1=[15], O=[16]))}
    if fast_name != 'fast':
        cells[fast_name] = cells.pop('fast')
    ports = {name: dict(direction=direction, bits=[bit]) for name, direction, bit in
             [('clk', 'input', 2), ('a', 'input', 3), ('b', 'input', 4), ('q', 'output', 17)]}
    yosys = root / 'source.json'
    yosys.write_text(json.dumps({'modules': {'retime': dict(attributes={'top': '1'},
                                                         ports=ports, cells=cells, netnames={})}}))
    ir = root / 'source.emuir.sqlite'
    import_yosys_json(yosys, top='retime', clocks=['clk']).save(ir)
    return ir


def write_costs(path, rows):
    path.write_text('from_pin_hex\tto_pin_hex\textra_delay_ns\n' + ''.join(
        f'{a.encode().hex()}\t{b.encode().hex()}\t{delay}\n' for a, b, delay in rows))
    return path


def call_engine(source, output, **kwargs):
    return run_opensta_path_database(source, output, clocks={'clk': 10.},
        timing_io={'input_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['a', 'b']}],
                   'output_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['q']}]},
        executable=os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE'], _stream_output=True,
        clock_budget_policy='engine-edge-v1', **kwargs)


def capture_row(path):
    rows = list(iter_sta_path_database_paths(path, json.loads(path.read_text())))
    selected = [row for row in rows if row['endpoint']['instance'] == 'capture']
    assert len(selected) == 1
    return selected[0]


@pytest.mark.parametrize('option', [dict(max_paths=1), dict(through_nets=['n']),
                                  dict(clock_budget_policy='legacy-absolute-arrival-v1'), dict(_stream_output=False)])
def test_partial_or_legacy_search_cannot_claim_communication_research(tmp_path, option):
    kwargs = dict(_stream_output=True, clock_budget_policy='engine-edge-v1')
    kwargs.update(option)
    with patch('emuflow.native_tools.require_native_schema'):
        with pytest.raises(ValidationError, match='uncapped indexed clock-aware'):
            run_opensta_path_database(tmp_path / 'absent.sqlite', tmp_path / 'out.json',
                communication_delays_path=tmp_path / 'costs.tsv', **kwargs)


real_engine = pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),
                                reason='explicit qualified OpenSTA required')


@real_engine
@pytest.mark.parametrize('negative_capture', [False, True])
def test_whole_graph_research_replaces_the_old_worst_branch_and_preserves_source_costs(tmp_path, negative_capture):
    source = prepare_source(tmp_path, negative_capture=negative_capture)
    baseline, retimed, repeated = (tmp_path / name for name in ('before.json', 'retimed.json', 'again.json'))
    call_engine(source, baseline)
    before = capture_row(baseline)
    assert before['startpoint']['instance'] == 'launch_a'
    costs = write_costs(tmp_path / 'costs.tsv', [('launch_b/Q', 'fast/I0', 3.), ('fast/O', 'join/I1', 4.)])
    # No whole EmuIR graph or old selected-path list is reconstructed in Python.
    with patch('emuflow.opensta.EmuIR.load', side_effect=AssertionError('whole graph load')):
        call_engine(source, retimed, communication_delays_path=costs)
    after = capture_row(retimed)
    assert after['startpoint']['instance'] == 'launch_b'
    assert after['fixed_delay_ns'] == pytest.approx(.34, abs=2e-6)
    assert after['fixed_delay_ns'] < before['fixed_delay_ns']
    assert after['slack_ns'] == pytest.approx((5. if negative_capture else 10.) - .34 - .08, abs=2e-6)
    assert after['clock_budget'] == before['clock_budget']
    manifest = json.loads(retimed.read_text())
    assert manifest['source']['communication_research']['costs_sha256'] == file_sha256(costs)
    assert manifest['source']['communication_research']['exported_costs'] == 'original-local-delay-and-slack'
    assert validate_sta_path_database(retimed, source)['paths'] > 0
    call_engine(source, repeated, communication_delays_path=costs)
    assert capture_row(repeated) == after


@real_engine
@pytest.mark.parametrize('name', ['fast[0:0]', 'fast*literal?', 'fast\\雪/hier[0:0]'])
def test_literal_pattern_characters_and_backslashes_resolve_one_exact_source_pin(tmp_path, name):
    source = prepare_source(tmp_path, fast_name=name)
    sdc = name.replace('\\', '\\\\')
    costs = write_costs(tmp_path / 'costs.tsv', [('launch_b/Q', sdc + '/I0', 3.), (sdc + '/O', 'join/I1', 4.)])
    output = tmp_path / 'retimed.json'
    call_engine(source, output, communication_delays_path=costs)
    assert capture_row(output)['startpoint']['instance'] == 'launch_b'
    assert capture_row(output)['fixed_delay_ns'] == pytest.approx(.34, abs=2e-6)


@real_engine
def test_later_invalid_wire_leaves_the_live_timing_graph_unchanged(tmp_path):
    from emuflow.opensta_source_context import emit_opensta_source_context
    from emuflow.store_publication import bind_store_source
    source = prepare_source(tmp_path)
    engine = os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE']
    context = emit_opensta_source_context(bind_store_source(source, 'emuir-compact-v2'),
        tmp_path / 'context', clocks={'clk': 10.}, timing_io={
            'input_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['a', 'b']}],
            'output_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['q']}]}, executable=engine)
    costs = write_costs(tmp_path / 'costs.tsv', [('launch_b/Q', 'fast/I0', 3.), ('launch_b/Q', 'slow0/I0', 4.)])
    script = tmp_path / 'reject.tcl'
    script.write_text('''
source $env(EMUFLOW_TEST_CONTEXT)
source $env(EMUFLOW_TEST_DELAYS_SCRIPT)
if {[catch {
  emuflow_read_design_context
  proc selected_start {} {
    set paths [find_timing_paths -path_delay max -to [sta::find_pin capture/D] -group_path_count 1 -endpoint_path_count 1]
    if {[llength $paths] != 1} {error "missing capture path"}
    return [get_property [get_property [lindex $paths 0] startpoint] full_name]
  }
  set before [selected_start]
  if {![catch {emuflow_apply_communication_delays $env(EMUFLOW_TEST_DELAYS)} message]} {
    error "invalid communication wire accepted"
  }
  if {$message ne "communication endpoints have no source timing wire"} {error $message}
  if {$before ne "launch_a/Q" || [selected_start] ne $before} {error "failed table changed the live timing graph"}
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_COMMUNICATION_ATOMIC_REJECTION status=pass"
exit 0
''')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([engine, '-no_init', '-exit', str(script)], capture_output=True, text=True,
        env={**os.environ, **context.environment, 'EMUFLOW_STA_TOP': 'retime',
             'EMUFLOW_TEST_CONTEXT': str(root / 'scripts/opensta/design_context.tcl'),
             'EMUFLOW_TEST_DELAYS_SCRIPT': str(root / 'scripts/opensta/communication_delays.tcl'),
             'EMUFLOW_TEST_DELAYS': str(costs)})
    context.require_unchanged()
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EMUFLOW_COMMUNICATION_ATOMIC_REJECTION status=pass' in result.stdout


@real_engine
@pytest.mark.parametrize('rows', [
    [('launch_b/Q', 'fast/I0', 'NaN')], [('launch_b/Q', 'fast/I0', 'Inf')],
    [('launch_b/Q', 'fast/I0', '-1')], [('missing/Q', 'fast/I0', '1')],
    [('launch_b/Q', 'slow0/I0', '1')], [('launch_b/Q', 'fast/I0', '1'), ('launch_b/Q', 'fast/I0', '2')],
    [('slow0/O', 'launch_a/C', '1')],
])
def test_invalid_or_unconnected_costs_cannot_replace_a_path_owner(tmp_path, rows):
    source = prepare_source(tmp_path)
    output = tmp_path / 'kept.json'
    call_engine(source, output)
    before = output.read_bytes()
    costs = write_costs(tmp_path / 'costs.tsv', rows)
    with pytest.raises(EmuFlowError):
        call_engine(source, output, communication_delays_path=costs)
    assert output.read_bytes() == before
