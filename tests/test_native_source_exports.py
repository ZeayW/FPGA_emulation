"""Unconditional native source exports include clocked I/O and static hardblocks."""
import csv
import subprocess

import pytest

from emuflow.native_word_source_constants import read_native_source_constants
from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86

pytestmark = pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')


def test_capture_census_includes_clocked_outputs_and_empty_clock_families(tmp_path):
    xdc = tmp_path / 'runtime.xdc'
    xdc.write_text('set_output_delay -max 3 -clock [get_clocks {core}] [get_ports {result[*]}]\n')
    output = tmp_path / 'census'
    program = f'source {_tcl_word(ROOT/"scripts/vivado/native_source_census.tcl")}\n' + r'''
proc get_property {args} {
 lassign [lrange $args end-1 end] key object
 if {$key eq "NAME"} {return $object}
 if {$key eq "EMUFLOW_SOURCE"} {return s42}
 error "unexpected property"
}
proc all_registers {args} {return [expr {[lindex $args 1] eq "core" ? "state/C" : ""}]}
proc get_cells {args} {return state}
proc get_pins {args} {return {state/D state/CE}}
proc get_ports {args} {return {{result[0]} {result[1]}}}
proc set_case_analysis {args} {error "capture source census must be unconditional"}
proc emuflow_native_word_fanin {sources targets} {
 set ::observed_targets [lsort $targets]
 return [dict create native_startpoints 5 dut_cells {state 1} rx {s7 rx/C} \
   infrastructure_cells {phase phase} ports {data data} unclassified {hardblock/O hardblock/O}]
}
'''
    program += f'''
set unknown [emuflow_native_capture_sources [dict create dut_clocks {{core empty}}] {_tcl_word(xdc)} {_tcl_word(output)}]
puts [dict keys $unknown]
puts $observed_targets
'''
    done = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    assert done.stdout.splitlines() == ['hardblock/O', '{result[0]} {result[1]} state/CE state/D']
    with (output / 'source-census.tsv').open(newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    assert [int(r['target_pins']) for r in rows] == [4, 0]
    assert [int(r['native_startpoints']) for r in rows] == [5, 0]
    with (output / 'external-sources.tsv').open(newline='') as stream:
        assert {r['source_kind'] for r in csv.DictReader(stream, delimiter='\t')} == {
            'infrastructure_cells', 'ports', 'unclassified'}


def test_native_primitive_properties_keep_missing_sources_visible_without_global_net_expansion(tmp_path):
    output = tmp_path / 'properties.tsv'
    program = f'source {_tcl_word(ROOT/"scripts/vivado/native_source_properties.tcl")}\n' + r'''
proc get_cells {args} {
 if {[string first missing [lindex $args end]]>=0} {return {}}
 return macro
}
proc get_pins {args} {
 if {[lindex $args end] eq "DIRECTION == IN"} {return {{macro/A[21]}}}
 return {{macro/A2_DATA[21]}}
}
proc get_property {key object} {
 set object [lindex $object 0]
 switch $key {
  NAME {return $object}
  REF_NAME {return DSP_A_B_DATA}
  AREG - BREG {return 0}
  A_INPUT - B_INPUT {return DIRECT}
  REF_PIN_NAME {return [lindex [split $object /] end]}
  DIRECTION {return [expr {$object eq {macro/A[21]} ? "IN" : "OUT"}]}
  TYPE {return [expr {$object eq "ground" ? "GROUND" : "SIGNAL"}]}
 }
 error "unexpected property $key"
}
proc get_nets {args} {
 if {[lsearch -exact $args -segments]>=0} {error "global net aliases must not be expanded"}
 return [expr {[lindex [lindex $args end] 0] eq {macro/A[21]} ? "ground" : "signal"}]
}
proc set_case_analysis {args} {error "source properties must be unconditional"}
'''
    program += f'''
puts [emuflow_native_source_properties [dict create {{macro/A2_DATA[21]}} 1 missing/O 1] {_tcl_word(output)}]
'''
    done = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    assert done.stdout.strip() == '2'
    constants, requested = read_native_source_constants(output)
    assert requested == {'macro/A2_DATA[21]', 'missing/O'}
    assert set(constants) == {'macro/A2_DATA[21]'}
    assert constants['macro/A2_DATA[21]']['value'] == 0
