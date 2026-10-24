# Selected primitive facts for an explicit functional constant-selection check.
# No name preservation, placement constraints, or numerical delay substitution.
if {$argc != 3} {error "expected INPUT_DCP QUERY_TSV OUTPUT_TSV"}
lassign $argv checkpoint query output
proc emuflow_frontier_hex {value} {return [binary encode hex [encoding convertto utf-8 $value]]}
proc emuflow_frontier_decode {value} {return [encoding convertfrom utf-8 [binary decode hex $value]]}
source [file join [file dirname [info script]] timing_contract.tcl]
emuflow_open_export_design $checkpoint synthesized
set input [open $query r]
if {[gets $input header] < 0 || $header ne "source_origin\tnative_cell_hex"} {
  error "invalid constant-frontier query"
}
set stream [open $output w]
puts $stream "source_origin\tnative_cell_hex\tref\tinit\tpin\tdriver_kind\tdriver_source\tdriver_ref\tdriver_pin\tdriver_name_hex\tdriver_transformations_hex\ttransformations_hex"
set count 0
while {[gets $input row] >= 0} {
  if {$row eq ""} {continue}
  set fields [split $row "\t"]
  if {[llength $fields] != 2} {error "malformed constant-frontier query"}
  lassign $fields origin name_hex
  if {![regexp {^s(0|[1-9][0-9]*)$} $origin]} {error "invalid frontier source"}
  set name [emuflow_frontier_decode $name_hex]
  set cell [get_cells -quiet -hier -filter "NAME == $name"]
  if {[llength $cell] != 1 || [get_property EMUFLOW_SOURCE $cell] ne $origin} {
    error "constant-frontier cell source association differs: $name"
  }
  set ref [get_property REF_NAME $cell]
  if {![regexp {^LUT([1-6])$} $ref _ width]} {error "unsupported constant-frontier primitive: $ref"}
  set init [get_property INIT $cell]
  set modifications "[get_property -quiet OPT_MODIFIED $cell] [get_property -quiet PHYS_OPT_MODIFIED $cell]"
  for {set i 0} {$i < $width} {incr i} {
    set pin [get_pins -quiet -of_objects $cell -filter "REF_PIN_NAME == I$i"]
    if {[llength $pin] != 1} {error "frontier input pin is not unique"}
    set nets [get_nets -quiet -of_objects $pin]
    if {[llength $nets] != 1} {error "frontier input net is not unique"}
    set drivers [get_pins -quiet -leaf -of_objects $nets -filter {DIRECTION == OUT}]
    set ports [get_ports -quiet -of_objects $nets -filter {DIRECTION == IN}]
    if {[expr {[llength $drivers]+[llength $ports]}] != 1} {error "frontier driver is not unique"}
    set kind opaque
    set driver_source ""
    set driver_ref ""
    set driver_pin ""
    set driver_changes ""
    if {[llength $drivers] == 1} {
      set driver_cell [get_cells -quiet -of_objects $drivers]
      if {[llength $driver_cell] != 1} {error "frontier driver cell is not unique"}
      set driver_ref [get_property REF_NAME $driver_cell]
      set driver_pin [get_property REF_PIN_NAME $drivers]
      set driver_name [get_property NAME $drivers]
      if {$driver_ref eq "GND"} {
        set kind zero
      } elseif {$driver_ref eq "VCC"} {
        set kind one
      } else {
        set driver_source [get_property -quiet EMUFLOW_SOURCE $driver_cell]
        if {[regexp {^s(0|[1-9][0-9]*)$} $driver_source]} {set kind source}
        set driver_changes "[get_property -quiet OPT_MODIFIED $driver_cell] [get_property -quiet PHYS_OPT_MODIFIED $driver_cell]"
      }
    } else {
      set driver_name [get_property NAME $ports]
    }
    puts $stream [join [list $origin $name_hex $ref $init I$i $kind $driver_source $driver_ref $driver_pin \
      [emuflow_frontier_hex $driver_name] [emuflow_frontier_hex $driver_changes] [emuflow_frontier_hex $modifications]] "\t"]
  }
  incr count
}
close $input
close $stream
puts "EMUFLOW_CONSTANT_FRONTIER_INSPECTION status=pass cells=$count"
if {$checkpoint ne "@current-design"} {exit 0}
