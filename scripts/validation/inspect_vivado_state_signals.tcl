# Bounded diagnosis of rewritten state endpoints. No design changes or timing
# qualification: report the implementation driver of a requested RTL Q signal.
if {$argc != 4} {error "expected CHECKPOINT REQUEST_TSV OUTPUT_TSV PREFIX"}
lassign $argv checkpoint requests output prefix
proc decode {value} {return [encoding convertfrom utf-8 [binary decode hex $value]]}
proc encode {value} {return [binary encode hex [encoding convertto utf-8 $value]]}
proc names_hex {objects} {
  set names [list]
  foreach object $objects {lappend names [encode [get_property NAME $object]]}
  return [join $names ,]
}
proc resolve_signal {hints prefix} {
  foreach hint [split $hints ,] {
    lassign [split $hint :] kind hex
    set name [decode $hex]
    set physical $name
    if {$prefix ne ""} {set physical "$prefix/$name"}
    if {$kind eq "net"} {
      set object [get_nets -quiet [list $physical]]
      if {[llength $object] == 0} {set object [get_nets -quiet -hier [list $physical]]}
    } elseif {$kind eq "port"} {
      set object [get_ports -quiet [list $name]]
    } elseif {$kind eq "pin"} {
      set object [get_pins -quiet [list $physical]]
    } else {error "invalid signal hint kind"}
    if {[llength $object] == 1} {return $object}
  }
  return [list]
}
set_param general.maxThreads 2
open_checkpoint $checkpoint
set input [open $requests r]
if {[gets $input header] < 0 || $header ne "source_cell_hex\trequested_pin_hex\tq_hints\tclock_hints\tsource_init\tsource_clock_inverted\tsource_ref"} {
  error "invalid state signal request header"
}
set stream [open $output w]
puts $stream "source_cell_hex\trequested_pin_hex\tstatus\tq_signal_hex\tdriver_pin_hex\tdriver_cell_hex\tdriver_ref\tdriver_init\tdriver_clock_inverted\tsource_clocks_hex\tdriver_clocks_hex\tinput_pins_hex\tinput_nets_hex\ttransformations_hex\torig_cell_hex"
while {[gets $input row] >= 0} {
  set fields [split $row "\t"]
  if {[llength $fields] != 7} {error "malformed state signal request"}
  lassign $fields source pin q_hints clock_hints source_init source_inverted source_ref
  set result [list $source $pin signal-unresolved "" "" "" "" "" "" "" "" "" "" "" ""]
  set q [resolve_signal $q_hints $prefix]
  set clock [resolve_signal $clock_hints $prefix]
  if {[llength $clock] == 1} {lset result 9 [names_hex [get_clocks -quiet -of_objects $clock]]}
  if {[llength $q] == 1} {
    lset result 3 [encode [get_property NAME $q]]
    if {[get_property CLASS $q] eq "net"} {set net $q} else {set net [get_nets -quiet -of_objects $q]}
    set drivers [get_pins -quiet -leaf -of_objects $net -filter {DIRECTION == OUT}]
    lset result 4 [names_hex $drivers]
    lset result 2 nonunique-driver
    if {[llength $drivers] == 1} {
      set cell [get_cells -quiet -of_objects $drivers]
      if {[llength $cell] != 1} {error "signal driver lacks a unique cell"}
      lset result 2 driver-found
      lset result 5 [encode [get_property NAME $cell]]
      lset result 6 [get_property REF_NAME $cell]
      lset result 7 [get_property -quiet INIT $cell]
      lset result 8 [get_property -quiet IS_C_INVERTED $cell]
      set clocks [get_pins -quiet -of_objects $cell -filter {IS_CLOCK == 1}]
      if {[llength $clocks] != 0} {lset result 10 [names_hex [get_clocks -quiet -of_objects $clocks]]}
      set pins [get_pins -quiet -of_objects $cell -filter {DIRECTION == IN}]
      lset result 11 [names_hex $pins]
      set nets [list]
      foreach input_pin $pins {
        lappend nets [encode "[get_property REF_PIN_NAME $input_pin]=[get_property NAME [get_nets -quiet -of_objects $input_pin]]"]
      }
      lset result 12 [join $nets ,]
      lset result 13 [encode "[get_property -quiet OPT_MODIFIED $cell] [get_property -quiet PHYS_OPT_MODIFIED $cell]"]
      lset result 14 [encode [get_property -quiet ORIG_CELL_NAME $cell]]
    }
  }
  puts $stream [join $result "\t"]
}
close $input
close $stream
puts "EMUFLOW_STATE_SIGNAL_INSPECTION status=complete scope=diagnostic-only output=$output"
exit 0
