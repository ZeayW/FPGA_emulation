#!/usr/bin/env python3
"""Qualify state-capture clock tags without selecting source timing paths."""
import argparse
import csv
import math
from pathlib import Path
import subprocess

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.opensta import require_opensta_engine
from scripts.validation.qualify_opensta_launch_clocks import prepare


def _tcl_path(path):
    return '[encoding convertfrom utf-8 [binary format H* '+str(path.resolve()).encode().hex()+']]'


def qualify(sta, root):
    root = root.resolve();root.mkdir(parents=True, exist_ok=False)
    engine = require_opensta_engine(sta)
    liberty, verilog = prepare(root)
    text = liberty.read_text()
    original = 'pin (D) { direction : input; }'
    if text.count(original) != 1:
        raise ValidationError('dual-clock fixture data pin is ambiguous')
    # One data pin has two real setup checks, to different clock pins. Both
    # clocks must survive even if one has no worst selected data path.
    checks = '\n'.join('''timing () { related_pin : "%s"; timing_type : setup_rising;
        rise_constraint (scalar) { values ("0.1"); }
        fall_constraint (scalar) { values ("0.1"); }
      }''' % clock for clock in ('C', 'K'))
    liberty.write_text(text.replace(original, 'pin (D) { direction : input; '+checks+' }'))
    helper = Path(__file__).resolve().parents[1]/'opensta/capture_clock_edges.tcl'
    output = root/'capture-clocks.tsv';script = root/'qualify.tcl'
    program = '\n'.join(['read_liberty '+_tcl_path(liberty),
        'read_verilog '+_tcl_path(verilog), 'link_design launch_clock_fixture',
        'create_clock -name base -period 10 [get_ports clk]',
        'create_clock -name shifted -period 10 -waveform {2.5 7.5} [get_ports other_clk]',
        'source '+_tcl_path(helper), 'set output [open '+_tcl_path(output)+' w]'])
    program += r'''
puts $output "pin_hex\tclock_hex\tperiod_ns\tphase_ns"
sta::find_timing_cmd 1
# No selected data path can supply the answers in this qualification.
rename find_timing_paths forbidden_find_timing_paths
proc find_timing_paths {args} {error "data path selection is forbidden"}
foreach name {positive/D negative/D gated/D inverted/D unselected/D shifted/D dual/D} {
  set pin [sta::get_port_pin_error pin $name]
  foreach fact [emuflow_capture_clock_edges $pin {base shifted}] {
    lassign $fact clock period phase
    puts $output "[binary encode hex [encoding convertto utf-8 $name]]\t[binary encode hex $clock]\t$period\t$phase"
  }
}
close $output
set rejected 0
proc reject {name clocks pattern} {
  set pin [sta::get_port_pin_error pin $name]
  if {![catch {emuflow_capture_clock_edges $pin $clocks} message] || ![string match $pattern $message]} {
    error "capture rejection differs for $name: $message"
  }
  incr ::rejected
}
reject unclocked/D {base shifted} {*no active clocked setup facts*}
reject buffer/I {base shifted} {*no active clocked setup facts*}
reject positive/Q {base shifted} {*no active clocked setup facts*}
reject dual/D {base} {*undeclared clock*}
reject positive/D {} {*explicit clock identities*}
reject clk {base shifted} {*leaf state pin*}
set_disable_timing -from C -to D [get_cells positive]
sta::find_timing_cmd 1
reject positive/D {base shifted} {*no active clocked setup facts*}
puts "EMUFLOW_CAPTURE_CLOCK_QUALIFICATION status=pass endpoints=7 rejected=$rejected"
'''
    script.write_text('if {[catch {\n'+program+'\n} message options]} {puts stderr [dict get $options -errorinfo];exit 2}\nexit 0\n')
    with (root/'tool.log').open('w') as log:
        result = subprocess.run([sta, '-no_init', '-exit', str(script)], cwd=root,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    marker = 'EMUFLOW_CAPTURE_CLOCK_QUALIFICATION status=pass endpoints=7 rejected=7'
    if result.returncode or marker not in (root/'tool.log').read_text():
        raise ValidationError('OpenSTA capture-clock qualification failed; see tool.log')
    expected = {'positive/D': {('base', 0.)}, 'negative/D': {('base', 5.)},
        'gated/D': {('base', 0.)}, 'inverted/D': {('base', 5.)},
        'unselected/D': {('base', 0.)}, 'shifted/D': {('shifted', 2.5)},
        'dual/D': {('base', 0.), ('shifted', 2.5)}}
    observed = {}
    with output.open() as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if reader.fieldnames != ['pin_hex', 'clock_hex', 'period_ns', 'phase_ns']:
            raise ValidationError('capture-clock output header differs')
        for row in reader:
            pin = bytes.fromhex(row['pin_hex']).decode();clock = bytes.fromhex(row['clock_hex']).decode()
            period, phase = float(row['period_ns']), float(row['phase_ns'])
            if not math.isclose(period, 10., abs_tol=1e-5) or not math.isfinite(phase):
                raise ValidationError('capture-clock period/phase differs')
            fact = (clock, phase)
            if fact in observed.setdefault(pin, set()):
                raise ValidationError('capture-clock fact repeats')
            observed[pin].add(fact)
    if set(observed) != set(expected) or any(len(observed[pin]) != len(expected[pin]) or any(
        not any(clock == target_clock and math.isclose(phase, target_phase, rel_tol=0, abs_tol=1e-5)
            for target_clock, target_phase in expected[pin]) for clock, phase in observed[pin]) for pin in expected):
        raise ValidationError('capture-clock population or phase differs: '+repr(observed))
    report = dict(status='pass', scope='synthetic-state-capture-clock-api-only',
        endpoints=7, clock_facts=8, rejected_queries=7, selected_data_paths=False,
        engine=engine, helper_sha256=file_sha256(helper), script_sha256=file_sha256(script),
        liberty_sha256=file_sha256(liberty), verilog_sha256=file_sha256(verilog),
        output_sha256=file_sha256(output))
    write_json(root/'qualification.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sta', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args();qualify(args.sta, args.out)


if __name__ == '__main__':
    main()
