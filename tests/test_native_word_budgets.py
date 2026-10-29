"""Native budget views retain RX replicas, control pins and original clocks."""
import csv

from emuflow.vivado_session import _tcl_word
from tests.test_native_word_timing import ROOT, execute, pytestmark
from emuflow.native_word_budgets import BUDGET_HEADER


def test_signed_budgets_keep_replicas_and_use_a_nonclamping_reset(tmp_path):
    budgets=tmp_path/'budgets.tsv'
    with budgets.open('w',newline='')as stream:
        writer=csv.writer(stream,delimiter='\t',lineterminator='\n');writer.writerow(BUDGET_HEADER)
        for origin,edge,window,arrival in [('s7',1,50000,20000),('s7',2,50000,130000),('s9',1,100000,60000)]:
            writer.writerow((b'fpga1'.hex(),origin,b'emuflow_dut_clk_0'.hex(),edge,window-arrival,
                             b'core'.hex(),1,window,arrival))
    source=ROOT/'scripts/vivado/native_word_budgets.tcl'
    done,_=execute(tmp_path,f'source {_tcl_word(source)}\n'+f'''
set sources [emuflow_native_word_sources $role_path]
set result [emuflow_native_word_apply_budgets $sources fpga1 {_tcl_word(budgets)} {_tcl_word(tmp_path)}]
puts "[dict get $result applied] [dict get $result missing] [dict get $result reset_ceiling_ps]"
foreach row $constraints {{puts $row}}
''')
    assert not done.stderr
    assert done.stdout.splitlines()==[
        '2 1 40001',
        '-reset_path -from {rx/C rx_copy/C} -to {emuflow_dut_clk_0 core_half} 40.001',
        '-from {rx/C rx_copy/C} -rise_to emuflow_dut_clk_0 30.0',
        '-from {rx/C rx_copy/C} -fall_to emuflow_dut_clk_0 -80.0']
    with (tmp_path/'missing-budget-rx.tsv').open(newline='')as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    assert len(rows)==1 and rows[0]['source_origin']=='s9'


def test_complete_endpoint_query_uses_native_census_and_worst_check_per_capture(tmp_path):
    source=ROOT/'scripts/vivado/native_word_budgets.tcl'
    commands=r'''
set sources [emuflow_native_word_sources $role_path]
proc get_cells {args} {return [lindex $args end]}
proc get_pins {args} {return {new/D new/CE macro/RSTA}}
proc all_outputs {} {return dut_out}
rename get_property saved_get_property
proc get_property {args} {
 lassign [lrange $args end-1 end] property object
 if {$object ni {p0 p1 p2}} {return [saved_get_property {*}$args]}
 switch $property {
  SLACK {return [dict get {p0 -2.0 p1 -3.0 p2 0.5} $object]}
  DATAPATH_DELAY {return 2.0}
  STARTPOINT_PIN {return rx_copy/C}
  STARTPOINT_CLOCK {return emuflow_tdm_clk}
  ENDPOINT_PIN {return [expr {$object eq "p2" ? "macro/RSTA" : "new/D"}]}
  ENDPOINT_CLOCK {return emuflow_dut_clk_0}
  REQUIREMENT {return 100.0}
  STARTPOINT_CLOCK_EDGE {return 0.0}
  ENDPOINT_CLOCK_EDGE {return 100.0}
 }
 error "unexpected path property"
}
rename get_timing_paths saved_get_timing_paths
proc get_timing_paths {args} {
 if {[lindex $args [expr {[lsearch -exact $args -max_paths]+1}]]!=4 ||
     [lindex $args [expr {[lsearch -exact $args -nworst]+1}]]!=1 ||
     [lindex $args end] ne "group"} {error "query is not bounded by native endpoints"}
 return {p0 p1 p2}
}
set view [dict create starts {rx/C rx_copy/C} applied 2 missing 0 reset_ceiling_ps 40001]
'''
    done,_=execute(tmp_path,f'source {_tcl_word(source)}\n'+commands+
                   f'puts [emuflow_native_word_endpoint_slacks $sources $view {_tcl_word(tmp_path)}]\n')
    assert not done.stderr and done.stdout.strip()=='2'
    with (tmp_path/'native-endpoint-slacks.tsv').open(newline='')as stream:
        rows=list(csv.DictReader(stream,delimiter='\t'))
    assert {bytes.fromhex(r['endpoint_hex']).decode():r['slack_ns']for r in rows}=={'new/D':'-3.0','macro/RSTA':'0.5'}
