"""Clock-family maxima cover native captures without an output-bit cross product."""
import csv
import pytest

from emuflow.vivado_session import _tcl_word
from tests.test_native_word_timing import ROOT, execute, pytestmark


def test_grouped_capture_queries_keep_edges_rx_replicas_and_declared_inputs(tmp_path):
    source = ROOT/'scripts/vivado/native_word_captures.tcl'
    done, path = execute(tmp_path, f'source {_tcl_word(source)}\n'+r'''
set sources [emuflow_native_word_sources $role_path]
rename get_timing_paths original_get_timing_paths
proc get_timing_paths {args} {
 lappend ::queries $args
 if {[lsearch -exact $args -rise_to]<0 && [lsearch -exact $args -fall_to]<0} {error "per-output query"}
 if {[lsearch -exact $args -fall_from]>=0} {return {}}
 if {[lsearch -exact $args -from]>=0} {
  set from [lindex $args [expr {[lsearch -exact $args -from]+1}]]
  if {$from eq "dut_input"} {return dut_path}
  if {$from ne {rx/C rx_copy/C}} {error "RX replicas lost"}
  return rx_path
 }
 return dut_path
}
set stream [open $output_path w]
emuflow_native_word_cost_header $stream
set progress [open "$output_path.progress" w]
puts [emuflow_native_word_capture_costs $sources $stream [dict create dut_input dut_input] $progress]
close $progress
close $stream
puts [llength $queries]
''')
    assert not done.stderr and done.stdout.splitlines()==['24','24']
    with path.open(newline='')as stream:
        rows = list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==24
    assert {bytes.fromhex(row['query_hex']).decode()for row in rows}=={
        f'dut-capture:{clock}:{edge}'for clock in ('emuflow_dut_clk_0','core_half')for edge in ('rise','fall')}
    assert sum(row['source_kind']=='rx'for row in rows)==4
    assert sum(row['source_kind']=='declared-input'for row in rows)==4
    assert sum(row['measurement']=='native-no-timed-path'for row in rows)==8


@pytest.mark.parametrize('extra_origins',[15,130])
def test_empty_native_rx_groups_keep_every_origin_and_other_edge_witness(tmp_path,extra_origins):
    source=ROOT/'scripts/vivado/native_word_captures.tcl'
    done,path=execute(tmp_path,f'source {_tcl_word(source)}\nset extra_origins {extra_origins}\n'+r'''
set sources [emuflow_native_word_sources $role_path]
for {set i 0} {$i<$extra_origins} {incr i} {dict set sources rx s[expr {$i+8}] other$i/C}
rename get_property saved_get_property
proc get_property {args} {
 if {[lindex $args end] eq "other_path"} {
  if {[lindex $args end-1] eq "STARTPOINT_PIN"} {return other0/C}
  return [saved_get_property {*}[lreplace $args end end rx_path]]
 }
 return [saved_get_property {*}$args]
}
rename get_timing_paths saved_get_timing_paths
proc get_timing_paths {args} {
 lappend ::queries $args
 set from_at [lsearch -exact $args -from]
 if {$from_at<0} {return dut_path}
 set from [lindex $args [expr {$from_at+1}]]
 set rise [expr {[lsearch -exact $args -rise_to]>=0}]
 set capture [lindex $args end]
 if {$rise && [lsearch -exact $from rx/C]>=0} {return rx_path}
 if {!$rise && $capture eq "core_half" && [lsearch -exact $from other0/C]>=0} {return other_path}
 return {}
}
set stream [open $output_path w]
emuflow_native_word_cost_header $stream
set progress [open "$output_path.progress" w]
puts [emuflow_native_word_capture_costs $sources $stream {} $progress]
close $progress
close $stream
puts [llength $queries]
puts [llength $constraints]
''')
    origins=extra_origins+1
    row_count=16+4*origins
    query_count=16+3*min(origins,64)+4*((origins+63)//64)
    assert not done.stderr and done.stdout.splitlines()==[str(row_count),str(query_count),'0']
    with path.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    rx=[row for row in rows if row['source_kind']=='rx']
    assert len(rows)==row_count and len(rx)==4*origins
    assert {bytes.fromhex(row['source_hex']).decode()for row in rx}=={f's{i}'for i in range(7,extra_origins+8)}
    for row in rx:
        query=bytes.fromhex(row['query_hex']).decode()
        origin=bytes.fromhex(row['source_hex']).decode()
        measured=(query.endswith(':rise')and origin=='s7')or(
            query=='dut-capture:core_half:fall'and origin=='s8')
        assert (row['measurement']=='native-clock-aware-reference')==measured
        if measured:assert float(row['data_ns'])==6.
        else:assert all(row[key]==''for key in ('data_ns','sampling_cost_ns','start_hex','end_hex'))


def test_state_hold_excludes_primary_io_and_does_not_change_constraints(tmp_path):
    source = ROOT/'scripts/vivado/native_word_captures.tcl'
    done,path = execute(tmp_path,f'source {_tcl_word(source)}\n'+r'''
set sources [emuflow_native_word_sources $role_path]
rename get_cells original_get_cells
proc get_cells {args} {
 if {[lsearch -exact $args -of_objects]>=0} {return [original_get_cells {*}$args]}
 return [lindex $args end]
}
proc get_pins {args} {
 if {[lindex $args end] ne {DIRECTION == IN && IS_CLOCK == 0}} {error "capture clock pin included"}
 if {[lsort [lindex $args 2]]ne{core_renamed divided dsp_optimized}} {error "native state ownership lost"}
 return {core_renamed/D core_renamed/CE dsp_optimized/A divided/R}
}
rename get_timing_paths original_get_timing_paths
proc get_timing_paths {args} {
 lappend ::queries $args
 set from [lindex $args [expr {[lsearch -exact $args -from]+1}]]
 if {[lsort $from]ne{core_renamed/C divided/C dsp_optimized/CLK}} {error "hold source is not DUT state"}
 set to [lindex $args [expr {[lsearch -exact $args -to]+1}]]
 if {$to ne {core_renamed/D core_renamed/CE dsp_optimized/A divided/R}} {error "hold capture is not DUT state"}
 if {[lindex $args [expr {[lsearch -exact $args -delay_type]+1}]]ne"min"} {error "not a hold query"}
 return dut_path
}
set stream [open $output_path w]
puts [emuflow_native_word_state_hold $sources $stream]
close $stream
puts [llength $constraints]
''')
    assert not done.stderr and done.stdout.splitlines()==['1','0']
    with path.open(newline='')as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==1 and rows[0]['scope']=='dut-state-to-state'
    assert float(rows[0]['slack_ns'])==-4.5


@pytest.mark.parametrize('timed',[False,True])
def test_rx_hold_keeps_data_launches_and_native_minimum_checks(tmp_path,timed):
    source=ROOT/'scripts/vivado/native_word_captures.tcl'
    done,path=execute(tmp_path,f'source {_tcl_word(source)}\nset timed {int(timed)}\n'+r'''
set sources [emuflow_native_word_sources $role_path]
rename get_cells original_get_cells
proc get_cells {args} {
 if {[lsearch -exact $args -of_objects]>=0} {return [original_get_cells {*}$args]}
 return [lindex $args end]
}
proc get_pins {args} {
 if {[lindex $args end]ne{DIRECTION == IN && IS_CLOCK == 0}} {error "clock capture pin included"}
 if {[lsort [lindex $args 2]]ne{phase rx rx_copy}} {error "infrastructure ownership lost"}
 return {phase/D rx/D rx_copy/D}
}
rename get_timing_paths original_get_timing_paths
proc get_timing_paths {args} {
 lappend ::queries $args
 if {[lindex $args [expr {[lsearch -exact $args -delay_type]+1}]]ne"min"} {error "setup query used for hold"}
 if {[lindex $args [expr {[lsearch -exact $args -from]+1}]]ne{rx/C rx_copy/C}} {error "RX launch omitted"}
 if {[lindex $args [expr {[lsearch -exact $args -to]+1}]]ne{phase/D rx/D rx_copy/D}} {error "non-infrastructure capture"}
 if {!$::timed} {return {}}
 return rx_path
}
set stream [open $output_path w]
puts [emuflow_native_word_rx_infrastructure_hold $sources $stream]
close $stream
puts [llength $queries]
puts [llength $constraints]
''')
    assert not done.stderr and done.stdout.splitlines()==[str(int(timed)),'1','0']
    with path.open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==1 and rows[0]['scope']=='rx-to-infrastructure'
    if timed:
        assert float(rows[0]['slack_ns'])==-6.5
        assert bytes.fromhex(rows[0]['start_clock_hex']).decode()=='emuflow_tdm_clk'
    else:
        assert rows[0]['measurement']=='native-no-timed-path' and rows[0]['slack_ns']==''
