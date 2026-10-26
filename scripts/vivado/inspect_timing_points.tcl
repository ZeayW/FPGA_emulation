# Bounded provider qualification diagnostic, never a production timing owner.
if {$argc ni {2 3}} {error "expected INPUT_DCP OUTPUT_DIRECTORY ?original|datapath-only|clock-aware-reference?"}
lassign $argv checkpoint out mode
if {$argc==2} {set mode original}
if {$mode ni {original datapath-only clock-aware-reference}} {error "unsupported timing inspection mode"}
file mkdir $out
open_checkpoint $checkpoint
if {$mode ne "original"} {
  source [file join [file dirname [info script]] timing_contract.tcl]
  if {$mode eq "datapath-only"} {
    emuflow_use_data_path_constraints
  } else {
    emuflow_use_clock_aware_reference_constraints
  }
}
set path [get_timing_paths -quiet -max_paths 1 -nworst 1]
if {[llength $path] != 1} {error "no timing path for provider inspection"}
set stream [open "$out/timing-properties.txt" w]
puts $stream "constraint_mode = $mode"
if {$mode ne "original"} {
  lassign [emuflow_reference_path_cost $path] cost adjustment
  puts $stream "reference_cost_ns = $cost"
  puts $stream "check_clock_adjustment_ns = $adjustment"
  puts $stream "selection_policy = worst-slack-under-common-reference"
}
foreach property [list_property $path] {
  if {[string match *POINTS* $property]} {
    set points [get_property $property $path]
    puts $stream "$property count=[llength $points]"
    foreach point [lrange $points 0 15] {
      puts $stream "POINT $point"
      foreach field [list_property $point] {
        puts $stream "  $field = [get_property $field $point]"
      }
    }
  } else {
    puts $stream "$property = [get_property $property $path]"
  }
}
close $stream
report_timing -max_paths 1 -nworst 1 -input_pins -file "$out/timing-path.rpt"
puts "EMUFLOW_TIMING_PROPERTIES status=complete mode=$mode scope=diagnostic-only"
exit 0
