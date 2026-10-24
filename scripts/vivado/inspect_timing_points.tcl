# Bounded provider qualification diagnostic, never a production timing owner.
if {$argc != 2} {error "expected INPUT_DCP OUTPUT_DIRECTORY"}
lassign $argv checkpoint out
file mkdir $out
open_checkpoint $checkpoint
set path [get_timing_paths -quiet -max_paths 1 -nworst 1]
if {[llength $path] != 1} {error "no timing path for provider inspection"}
set stream [open "$out/timing-properties.txt" w]
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
report_timing -max_paths 1 -nworst 1 -input_pins -nets -file "$out/timing-path.rpt"
exit 0
