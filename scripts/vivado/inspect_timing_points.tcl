# Bounded provider qualification diagnostic, never a production timing owner.
if {$argc ni {2 3}} {error "expected INPUT_DCP OUTPUT_DIRECTORY ?original|datapath-only?"}
lassign $argv checkpoint out mode
if {$argc==2} {set mode original}
if {$mode ni {original datapath-only}} {error "unsupported timing inspection mode"}
file mkdir $out
open_checkpoint $checkpoint
if {$mode eq "datapath-only"} {
  source [file join [file dirname [info script]] timing_contract.tcl]
  emuflow_use_data_path_constraints
}
set path [get_timing_paths -quiet -max_paths 1 -nworst 1]
if {[llength $path] != 1} {error "no timing path for provider inspection"}
set stream [open "$out/timing-properties.txt" w]
puts $stream "constraint_mode = $mode"
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
