"""Native timing coverage must not depend on original DUT FF names/types."""
import csv
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86

pytestmark = pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')

MOCK = r'''
set emuflow_native_export_stage routed
set clocks {emuflow_dut_clk_0 core_half emuflow_tdm_clk unused_phy}
set registers [dict create emuflow_dut_clk_0 {core_renamed/C dsp_optimized/CLK} core_half divided/C \
 emuflow_tdm_clk {rx/C rx_copy/C phase/C} unused_phy {}]
set origins [dict create rx s7 rx_copy s7 phase s42]
set parent [dict create core_half emuflow_dut_clk_0 unused_phy emuflow_tdm_clk]
set pin_clock [dict create rx/C emuflow_tdm_clk rx_copy/C emuflow_tdm_clk]
set source_points {rx/Q rx_copy/Q core_renamed/Q dsp_optimized/P phase/Q dut_input service_input unknown/O}
set constraints {}
set queries {}
set requirement 1.0
proc get_clocks {args} {
 if {$args eq {-quiet}} {return $::clocks}
 if {[lsearch -exact $args -of_objects]>=0} {return [dict get $::pin_clock [lindex $args end]]}
 return [lindex $args end]
}
proc all_registers {args} {return [dict get $::registers [lindex $args 1]]}
proc get_cells {args} {
 set result {}
 foreach pin [lindex $args end] {lappend result [string range $pin 0 [expr {[string last / $pin]-1}]]}
 return [lsort -unique $result]
}
proc get_property {args} {
 lassign [lrange $args end-1 end] key object
 switch $key {
  NAME {return $object}
  IS_GENERATED {return [dict exists $::parent $object]}
  MASTER_CLOCK {return [dict get $::parent $object]}
  PERIOD {return [expr {$object eq "emuflow_tdm_clk" ? 5.0 : 100.0}]}
  CLASS {return [expr {$object in {dut_input service_input} ? "port" : "pin"}]}
  EMUFLOW_SOURCE {
   if {[dict exists $::origins $object]} {return [dict get $::origins $object]}
   return ""
  }
  REQUIREMENT {return $::requirement}
  DATAPATH_DELAY {return [expr {$object eq "rx_path" ? 6.0 : $object eq "control_path" ? 3.0 : 4.25}]}
  SLACK {return [expr {$object eq "rx_path" ? -6.5 : $object eq "control_path" ? -2.5 : -4.5}]}
  STARTPOINT_PIN {return [expr {$object eq "rx_path" ? "rx_copy/C" : $object eq "control_path" ? "phase/C" : "dsp_optimized/CLK"}]}
  ENDPOINT_PIN {return {native_serializer/D[3]}}
  STARTPOINT_CLOCK {return [expr {$object in {rx_path control_path} ? "emuflow_tdm_clk" : "emuflow_dut_clk_0"}]}
  ENDPOINT_CLOCK {return native_serializer_div}
  STARTPOINT_CLOCK_EDGE {return 0.0}
  ENDPOINT_CLOCK_EDGE {return 5.0}
 }
 error "unexpected property $key"
}
proc all_fanin {args} {
 if {[lsearch -exact $args -to]>=0 || [lindex $args end]ne{native_serializer/D[3]}} {error "invalid fanin query"}
 if {[lindex $args [expr {[lsearch -exact $args -trace_arcs]+1}]]ne"enabled"} {error "fanin must retain case-enabled arcs"}
 return $::source_points
}
proc get_timing_paths {args} {
 lappend ::queries $args
 if {[lsearch -exact $args -fall_from]>=0} {return {}}
 if {[lsearch -exact $args -from]>=0} {
  set pins [lindex $args [expr {[lsearch -exact $args -from]+1}]]
  if {$pins eq "phase/C"} {return control_path}
  if {[lsort $pins]ne{rx/C rx_copy/C}} {error "RX replicas were lost"}
  return rx_path
 }
 if {[lsearch -exact $args -rise_from]<0} {error "missing native clock edge"}
 return dut_path
}
proc set_max_delay {args} {lappend ::constraints $args}
proc set_min_delay {args} {error "hold constraint changed"}
proc set_false_path {args} {error "path hidden"}
proc create_clock {args} {error "clock changed"}
proc set_clock_groups {args} {error "clock relationship hidden"}
'''


def execute(tmp_path, commands, roles='s7\ns9\n'):
    source = ROOT/'scripts/vivado/native_word_timing.tcl'
    path = tmp_path/'rx-roles.tsv'
    path.write_text('source_origin\n'+roles)
    output = tmp_path/'costs.tsv'
    program = f'source {_tcl_word(source)}\n'+MOCK
    program += f'set role_path {_tcl_word(path)}\nset output_path {_tcl_word(output)}\n'+commands
    done = subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)
    return done,output


def test_native_clock_ownership_accepts_renamed_and_absorbed_dut_state(tmp_path):
    done,_ = execute(tmp_path, r'''
set sources [emuflow_native_word_sources $role_path]
puts [dict get $sources dut_clocks]
puts [lsort [dict keys [dict get $sources dut_cells]]]
puts [dict get $sources missing_rx_origins]
puts [dict get $sources rx s7]
set fanin [emuflow_native_word_fanin $sources {native_serializer/D[3]}]
foreach key {rx dut_cells infrastructure_cells ports unclassified} {puts "$key=[lsort [dict keys [dict get $fanin $key]]]"}
puts "native_startpoints=[dict get $fanin native_startpoints]"
''')
    assert not done.stderr
    assert done.stdout.splitlines()==[
        'emuflow_dut_clk_0 core_half','core_renamed divided dsp_optimized','s9','rx/C rx_copy/C',
        'rx=s7','dut_cells=core_renamed dsp_optimized','infrastructure_cells=phase',
        'ports=dut_input service_input','unclassified=unknown/O','native_startpoints=8']


def test_reference_overrides_specific_data_budgets_without_changing_clocks_or_hold(tmp_path):
    done,_ = execute(tmp_path, r'''
set sources [emuflow_native_word_sources $role_path]
emuflow_native_word_reference $sources
foreach row $constraints {puts $row}
''')
    assert not done.stderr
    rows = done.stdout.splitlines()
    assert len(rows)==3
    assert rows[0]=='-reset_path -from {emuflow_dut_clk_0 core_half emuflow_tdm_clk unused_phy} -to {emuflow_dut_clk_0 core_half emuflow_tdm_clk unused_phy} 1.0'
    assert rows[1]=='-reset_path -from {rx/C rx_copy/C} -to {emuflow_dut_clk_0 core_half emuflow_tdm_clk unused_phy} 1.0'
    assert rows[2]=='-reset_path -from {emuflow_dut_clk_0 core_half} -to {emuflow_tdm_clk unused_phy} 1.0'


def test_every_native_clock_edge_and_replicated_rx_has_an_explicit_query_disposition(tmp_path):
    done,path = execute(tmp_path, r'''
set sources [emuflow_native_word_sources $role_path]
set stream [open $output_path w]
emuflow_native_word_cost_header $stream
emuflow_native_word_tx_costs $sources $stream 3 schedule-entry {native_serializer/D[3]}
close $stream
puts [llength $queries]
''')
    assert not done.stderr and done.stdout.strip()=='6'
    with path.open(newline='')as stream:
        rows = list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==9 and all(None not in row for row in rows)
    assert [(row['source_kind'],bytes.fromhex(row['source_hex']).decode(),row['launch_edge'])for row in rows]==[
        ('dut-clock','emuflow_dut_clk_0','rise'),('dut-clock','emuflow_dut_clk_0','fall'),
        ('dut-clock','core_half','rise'),('dut-clock','core_half','fall'),('rx','s7',''),
        ('infrastructure','phase',''),('ports','dut_input',''),('ports','service_input',''),('unclassified','unknown/O','')]
    assert [row['measurement']for row in rows].count('native-no-timed-path')==2
    for row in rows:
        if row['measurement'] in ('native-no-timed-path','native-source-context-required'):
            assert all(row[key]==''for key in ('data_ns','sampling_cost_ns','start_hex','launch_ns'))
        else:
            assert float(row['sampling_cost_ns'])==1.-({'rx':-6.5,'infrastructure':-2.5}.get(row['source_kind'],-4.5))
            assert bytes.fromhex(row['end_hex']).decode()=='native_serializer/D[3]'
    assert rows[4]['data_ns']=='6.0' and rows[4]['clock_adjustment_ns']=='1.5'
    assert sum(row['measurement']=='native-source-context-required'for row in rows)==3


@pytest.mark.parametrize('roles', ['s7\ns7\n','bad\n'])
def test_bad_source_roles_are_not_accepted_as_complete_inputs(tmp_path,roles):
    done,_ = execute(tmp_path, r'''
if {![catch {emuflow_native_word_sources $role_path} message]} {error "bad roles accepted"}
puts $message
''',roles)
    assert not done.stderr and 'RX source role' in done.stdout


@pytest.mark.parametrize('setup, expected', [
    ('unset emuflow_native_export_stage','read-only'),
    ('dict set parent core_half core_half','cycle'),
    ('dict set pin_clock rx/C core_half','projection'),
    ('set clocks {emuflow_tdm_clk unused_phy}','no declared DUT clock'),
])
def test_incompatible_snapshot_facts_remain_analysis_failures(tmp_path,setup,expected):
    done,_ = execute(tmp_path,setup+r'''
if {![catch {emuflow_native_word_sources $role_path} message]} {error "invalid snapshot accepted"}
puts $message
puts [llength $constraints]
''')
    assert not done.stderr and expected in done.stdout and done.stdout.rstrip().endswith('0')


def test_cost_export_refuses_a_nominal_clock_requirement_instead_of_the_reference(tmp_path):
    done,_ = execute(tmp_path,r'''
set stream [open $output_path w]
set requirement 100.0
if {![catch {emuflow_native_word_cost_row $stream 0 q dut-clock emuflow_dut_clk_0 rise dut_path} message]} {error "biased cost accepted"}
close $stream
puts $message
''')
    assert not done.stderr and 'common 1 ns reference' in done.stdout


def test_query_identity_is_utf8_even_when_the_native_interface_is_renamed(tmp_path):
    query = '入口/跨FPGA'
    done,path = execute(tmp_path,f'''
set stream [open $output_path w]
emuflow_native_word_cost_header $stream
emuflow_native_word_cost_row $stream 0 {_tcl_word(query)} dut-clock emuflow_dut_clk_0 rise dut_path
close $stream
''')
    assert not done.stderr
    with path.open(newline='')as stream:
        row = next(csv.DictReader(stream,delimiter='\t'))
    assert bytes.fromhex(row['query_hex']).decode('utf-8')==query
