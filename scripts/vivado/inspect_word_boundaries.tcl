# Bounded read-only diagnosis of native word timing boundaries. No timing claim.
if {$argc != 3} {error "expected INPUT_DCP WORD_BOUNDARIES_TSV OUTPUT_TSV"}
lassign $argv checkpoint mapping_path output
source [file join [file dirname [info script]] timing_contract.tcl]
open_checkpoint $checkpoint
set mapping [emuflow_load_fixed_io_map $mapping_path]
set stream [open $output w]
puts $stream "kind\tlogical\trole\tobject\tnets"
set counts [dict create word-tx 0 word-rx 0]
foreach logical [dict keys $mapping] {
  lassign [dict get $mapping $logical] kind name anchor
  if {$kind ni {word-tx word-rx} || [dict get $counts $kind] >= 2} {continue}
  dict incr counts $kind
  if {[catch {emuflow_exact_hier_pin $name} hierarchical]} {
    puts $stream [join [list $kind $logical hierarchy-absent $hierarchical {}] "\t"]
    continue
  }
  set nets [get_nets -quiet -segments -of_objects $hierarchical]
  set net_names {}
  if {[llength $nets]} {set net_names [get_property NAME $nets]}
  puts $stream [join [list $kind $logical hierarchical $name $net_names] "\t"]
  if {![llength $nets]} {continue}
  if {$kind eq "word-tx"} {
    set pad_nets [get_nets -quiet -segments -of_objects [get_ports -quiet [list $anchor]]]
    set buffers [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects $pad_nets] -filter {REF_NAME == OBUFDS}]
    set serial [get_nets -quiet -segments -of_objects [get_pins -quiet -of_objects $buffers -filter {REF_PIN_NAME == I}]]
    set serializers [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects $serial -filter {DIRECTION == OUT}] -filter {REF_NAME == TX_BITSLICE}]
    if {[llength $serializers] != 1} {error "diagnostic cannot identify the anchored serializer"}
    foreach pin [get_pins -quiet -of_objects $serializers -filter {DIRECTION == IN}] {
      if {[regexp {^D\[[0-7]\]$} [get_property REF_PIN_NAME $pin]]} {
        puts $stream [join [list $kind $logical anchored-data [get_property NAME $pin] [get_property NAME [get_nets -quiet -segments -of_objects $pin]]] "\t"]
      }
    }
  }
  set candidates [get_pins -quiet -leaf -of_objects $nets]
  if {[llength $candidates] > 8192} {error "boundary diagnostic exceeds fanout bound"}
  foreach pin $candidates {
    set cell [get_cells -quiet -of_objects $pin]
    if {[get_property REF_NAME $cell] in {TX_BITSLICE FDRE}} {
      puts $stream [join [list $kind $logical connected [get_property NAME $pin] [get_property NAME [get_nets -quiet -segments -of_objects $pin]]] "\t"]
    }
  }
  if {[catch {emuflow_fixed_io_object $mapping $logical} message]} {
    puts $stream [join [list $kind $logical resolution-error $message {}] "\t"]
  } else {
    puts $stream [join [list $kind $logical resolved [get_property NAME $message] [get_property NAME [get_nets -quiet -segments -of_objects $message]]] "\t"]
  }
}
close $stream
puts "EMUFLOW_WORD_BOUNDARY_INSPECTION status=complete scope=diagnostic-only output=$output"
exit 0
