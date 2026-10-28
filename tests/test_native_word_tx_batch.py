"""Batching preserves every native source/endpoint observation and absent path."""
import csv
import subprocess
import pytest
from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT,TCL86
from tests.test_native_word_timing import MOCK
pytestmark=pytest.mark.skipif(TCL86 is None,reason='Tcl 8.6 required')


@pytest.mark.parametrize('representation',['scalar','collection'])
@pytest.mark.parametrize('batch_size',[1,2,32])
@pytest.mark.parametrize('duplicate',[False,True])
def test_source_wise_batches_equal_individual_queries_with_bounded_vendor_calls(tmp_path,batch_size,duplicate,representation):
    roles=tmp_path/'roles.tsv';roles.write_text('source_origin\ns7\ns9\n')
    original=tmp_path/'original.tsv';batched=tmp_path/'batched.tsv';summary=tmp_path/'summary.tsv'
    program=f'source {_tcl_word(ROOT/"scripts/vivado/native_word_tx_batch.tcl")}\n'+MOCK+r'''
rename get_property original_property
set path_records [dict create]
proc get_property {args} {
 lassign [lrange $args end-1 end] key object
 if {[dict exists $::path_records $object]} {
  lassign [dict get $::path_records $object] base target
  if {$key eq "ENDPOINT_PIN"} {return $target}
  return [original_property $key $base]
 }
 if {$key eq "NAME" && [string match {native_serializer/*} [lindex $object 0]]} {return [lindex $object 0]}
 return [original_property {*}$args]
}
proc all_fanin {args} {return $::source_points}
proc get_timing_paths {args} {
 lappend ::queries $args
 if {[lsearch -exact $args -fall_from]>=0} {return {}}
 set base dut_path
 if {[lsearch -exact $args -from]>=0} {
  set starts [lindex $args [expr {[lsearch -exact $args -from]+1}]]
  set base [expr {$starts eq "phase/C" ? "control_path" : "rx_path"}]
 }
 set result {}
 foreach target [lindex $args [expr {[lsearch -exact $args -to]+1}]] {
  # A native collection nested in another list is not an individual pin.
  if {[lindex $target 0] ne $target} {return {}}
  set path [list path $base $target]
  dict set ::path_records $path [list $base $target]
  lappend result $path
 }
 if {$::duplicate && [llength $result]>1} {lset result 1 [lindex $result 0]}
 return $result
}
'''+f'''
set duplicate {int(duplicate)}
set sources [emuflow_native_word_sources {_tcl_word(roles)}]
set targets {{}}
for {{set i 0}} {{$i<32}} {{incr i}} {{
 set target [format {{native_serializer/D[%d]}} $i]
 if {{{int(representation=='collection')}}} {{set target [list $target]}}
 lappend targets [list query-$i $target]
}}
set old [open {_tcl_word(original)} w]
emuflow_native_word_cost_header $old
foreach pair $targets {{lassign $pair query target;emuflow_native_word_tx_costs $sources $old 3 $query $target}}
close $old
set original_calls [llength $queries]
set queries {{}}
set stream [open {_tcl_word(batched)} w]
set summary [open {_tcl_word(summary)} w]
emuflow_native_word_cost_header $stream
set result [emuflow_native_word_tx_batch $sources $stream $summary 3 $targets {batch_size}]
close $stream
close $summary
puts "$original_calls [llength $queries] [dict get $result timing_queries] [dict get $result contexts]"
'''
    done=subprocess.run([TCL86],input=program,text=True,capture_output=True,check=True)
    assert not done.stderr
    calls,batch_calls,reported,contexts=map(int,done.stdout.strip().split())
    assert calls==192and contexts==32and reported==batch_calls
    assert batch_calls<=calls+192//batch_size
    if not duplicate:assert batch_calls==192//batch_size
    def rows(path):
        with path.open(newline='')as f:return sorted(tuple(row)for row in csv.reader(f,delimiter='\t'))
    assert rows(original)==rows(batched)
    assert len(summary.read_text().splitlines())==32
