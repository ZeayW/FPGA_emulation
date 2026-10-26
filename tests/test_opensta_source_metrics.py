"""Integer full-graph metrics must discover independently worst branches."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from emuflow.opensta_source_context import emit_opensta_source_context
from emuflow.store_publication import bind_store_source
from tests.test_opensta_communication_research import prepare_source, write_costs
from tests.test_source_communication import fixture, produce_metric, seal_cuts


real_engine = pytest.mark.skipif(not os.environ.get('EMUFLOW_OPENSTA_LAUNCH_ENGINE'),
                                reason='qualified real OpenSTA required')


def run_probe(source, root, body):
    engine = os.environ['EMUFLOW_OPENSTA_LAUNCH_ENGINE']
    context = emit_opensta_source_context(bind_store_source(source, 'emuir-compact-v2'), root / 'context',
        clocks={'clk': 10.}, timing_io={
            'input_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['a', 'b']}],
            'output_groups': [{'clock': 'clk', 'delay_ns': 0., 'ports': ['q']}]}, executable=engine)
    write_costs(root / 'empty.tsv', [])
    script = root / 'probe.tcl'
    script.write_text('''
source $env(PROBE_CONTEXT)
source $env(PROBE_COSTS)
if {[catch {
 emuflow_read_design_context
 emuflow_apply_communication_delays [file join $env(PROBE_ROOT) empty.tsv]
 proc selected {} {
  set paths [find_timing_paths -path_delay max -to [sta::find_pin capture/D] -group_path_count 1 -endpoint_path_count 1]
  if {[llength $paths] != 1} {error "missing capture"}
  set path [lindex $paths 0]
  return [list [get_property [get_property $path startpoint] full_name] [$path data_arrival_time] [$path slack] [expr {[emuflow_path_communication_delay [get_property $path points]]/1.0e9}]]
 }
 set baseline [selected]
 foreach sense {-early -late} {set_timing_derate $sense -cell_delay -cell_check 0}
''' + body + '''
 emuflow_apply_communication_delays [file join $env(PROBE_ROOT) empty.tsv]
 foreach sense {-early -late} {set_timing_derate $sense -cell_delay -cell_check 1}
 if {[selected] ne $baseline} {error "integer metrics changed nominal source timing"}
} message options]} {
 puts stderr [dict get $options -errorinfo]
 exit 1
}
puts "EMUFLOW_INTEGER_SOURCE_METRICS status=pass"
exit 0
''')
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run([engine, '-no_init', '-exit', str(script)], text=True, capture_output=True,
        env={**os.environ, **context.environment, 'EMUFLOW_STA_TOP': 'retime',
             'PROBE_CONTEXT': str(project / 'scripts/opensta/design_context.tcl'),
             'PROBE_COSTS': str(project / 'scripts/opensta/communication_delays.tcl'),
             'PROBE_ROOT': str(root)})
    context.require_unchanged()
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EMUFLOW_INTEGER_SOURCE_METRICS status=pass' in result.stdout


@real_engine
@pytest.mark.parametrize('negative_capture', [False, True])
def test_integer_native_units_avoid_large_ui_rounding_and_reject_invalid_weights(tmp_path, negative_capture):
    source = prepare_source(tmp_path, negative_capture=negative_capture)
    for weight in [1, 248, 8388607, 8388608]:
        path = write_costs(tmp_path / f'w{weight}.tsv', [('launch_b/Q', 'fast/I0', weight)])
        path.write_text(path.read_text().replace('extra_delay_ns', 'integer_weight'))
    path = write_costs(tmp_path / 'fractional.tsv', [('launch_b/Q', 'fast/I0', 12.5)])
    path.write_text(path.read_text().replace('extra_delay_ns', 'integer_weight'))
    write_costs(tmp_path / 'ordinary-delay.tsv', [('launch_b/Q', 'fast/I0', 3.)])
    run_probe(source, tmp_path, '''
 foreach weight {1 248 8388607} {
  emuflow_apply_communication_delays [file join $env(PROBE_ROOT) w$weight.tsv] metric-integer-seconds-v1
  set row [selected]
  if {[lindex $row 0] ne "launch_b/Q" || [lindex $row 1] != $weight || [lindex $row 3] != $weight} {
   error "integer score was rounded: $weight $row"
  }
 }
 set before [selected]
 if {![catch {emuflow_apply_communication_delays [file join $env(PROBE_ROOT) w248.tsv]}]} {
  error "integer metric accepted as physical nanoseconds"
 }
 foreach name {w8388608 fractional ordinary-delay} {
  if {![catch {emuflow_apply_communication_delays [file join $env(PROBE_ROOT) $name.tsv] metric-integer-seconds-v1}]} {
   error "invalid integer metric accepted: $name"
  }
  if {[selected] ne $before} {error "invalid metric changed the graph"}
 }
''')


@real_engine
def test_native_routes_and_full_graph_find_different_maximum_hop_and_ratio_paths(fixture):
    original, assignment, schedule, output = fixture
    with closing(sqlite3.connect(original)) as db:
        net_a = db.execute("SELECT n.id,n.cut_class FROM endpoint_records e JOIN net_records n ON n.seq=e.net_seq JOIN cell_records c ON c.seq=e.instance_seq WHERE c.id='launch_a' AND e.role=0").fetchone()
    with closing(sqlite3.connect(assignment)) as db, db:
        db.execute("UPDATE copies SET part_index=CASE WHEN copy_id IN ('launch_a','launch_b') THEN 0 WHEN copy_id='fast' THEN 2 ELSE 1 END")
        db.execute('DELETE FROM cut_sinks')
        db.execute('UPDATE cuts SET source_part_index=2 WHERE cut_index=1')
        for index, source, sink in [(0, 0, 2), (1, 2, 1)]:
            net = db.execute('SELECT net_id FROM cuts WHERE cut_index=?', (index,)).fetchone()[0]
            db.execute('INSERT INTO cut_sinks VALUES(?,?,?)', (net, source, sink))
        db.execute('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)', (2, net_a[0], 0, net_a[1], 1, None, 0, 0))
        db.execute('INSERT INTO cut_sinks VALUES(?,0,1)', (net_a[0],))
        # Published cuts have canonical net order, independently of schedule
        # route order. Preserve predecessor coordinates when inserting net A.
        cuts = list(db.execute('SELECT * FROM cuts ORDER BY net_id'))
        predecessors = list(db.execute('SELECT * FROM cut_predecessors'))
        reindex = {row[0]: index for index, row in enumerate(cuts)}
        db.execute('DELETE FROM cuts')
        db.execute('DELETE FROM cut_predecessors')
        db.executemany('INSERT INTO cuts VALUES(?,?,?,?,?,?,?,?)',
                       [(index, *row[1:]) for index, row in enumerate(cuts)])
        db.executemany('INSERT INTO cut_predecessors VALUES(?,?,?)',
                       [(reindex[index], ordinal, net) for index, ordinal, net in predecessors])
    seal_cuts(assignment)
    with closing(sqlite3.connect(schedule)) as db, db:
        db.execute('DELETE FROM schedule_route_edges')
        db.execute('DELETE FROM schedule_entries')
        db.execute('DELETE FROM schedule_route_sinks')
        db.execute("UPDATE schedule_routes SET source_fpga='fpga2' WHERE seq=1")
        db.execute('INSERT INTO schedule_routes VALUES(?,?,?,?)', (2, 'd2', net_a[0], 'fpga0'))
        for route, source, sink, ratio in [(0, 0, 2, 8), (1, 2, 1, 8), (2, 0, 1, 248)]:
            net = db.execute('SELECT net FROM schedule_routes WHERE seq=?', (route,)).fetchone()[0]
            db.execute('INSERT INTO schedule_route_sinks VALUES(?,0,?)', (route, f'fpga{sink}'))
            db.execute('INSERT INTO schedule_route_edges VALUES(?,0,?,?,?)', (route, f'l{route}', f'fpga{source}', f'fpga{sink}'))
            db.execute('INSERT INTO schedule_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (route, f'e{route}', f'd{route}', net, 0, f'l{route}', f'fpga{source}', f'fpga{sink}', f'l{route}', route + 1, 0, 0, 7, ratio))
    for metric in ['physical-hops', 'maximum-net-route-ratio', 'serial-ratio-sum']:
        produce_metric((*fixture[:3], output.parent / (metric + '.tsv')), metric)
    run_probe(original, output.parent, '''
 foreach {metric expected_start expected_score} {physical-hops launch_b 2 maximum-net-route-ratio launch_a 248 serial-ratio-sum launch_a 248} {
  emuflow_apply_communication_delays [file join $env(PROBE_ROOT) $metric.tsv] metric-integer-seconds-v1
  set row [selected]
  if {[lindex $row 0] ne "$expected_start/Q" || [lindex $row 1] != $expected_score || [lindex $row 3] != $expected_score} {
   error "wrong independent full-graph maximum: $metric $row"
  }
 }
''')
