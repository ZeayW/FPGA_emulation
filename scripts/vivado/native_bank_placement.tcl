# Keep native TDM bank logic and RX shadow state in their I/O bank SLR.
# Only placement regions and native control-net replication preferences are
# added. No clock exception, endpoint preservation, or pipeline is introduced.
proc emuflow_bank_movable {cell} {
  set ref [get_property REF_NAME $cell]
  return [expr {[regexp {^(FD[A-Z]+|LD[A-Z]+|LUT[1-6](_2)?|CARRY[48]|MUXF[789])$} $ref] &&
      ![get_property IS_LOC_FIXED $cell]}]
}

proc emuflow_native_bank_placement {placement sources output prefix} {
  set source_map $sources
  set bank_slrs [dict create]
  set groups [dict create]
  set targets [dict create]
  set assigned [dict create]
  set ambiguous [dict create]
  set stream [open [file join $output bank-affinity.tsv] w]
  puts $stream "bank\tdirection\tslr\tnative_movable_cells"
  set input [open [file join ${placement} banks.tsv] r]
  gets $input header
  while {[gets $input row]>=0} {
    lassign [split $row \t] bank direction instance
    if {![regexp {^[0-9]+$} $bank] || $direction ni {rx tx} ||
        $instance ne "native_bank${bank}_${direction}"} {error "invalid generated bank role"}
    set objects [get_iobanks -quiet [list $bank]]
    if {[llength $objects]!=1} {error "native I/O bank is not unique: $bank"}
    set slrs [get_slrs -quiet -of_objects $objects]
    if {[llength $slrs]!=1} {error "native I/O bank has no unique SLR: $bank"}
    set slr [get_property NAME $slrs]
    dict set bank_slrs $bank $slr
    set cells [get_cells -quiet -hier -filter "NAME =~ ${instance}/* && IS_PRIMITIVE == 1"]
    set count 0
    foreach cell $cells {
      if {![emuflow_bank_movable $cell]} {continue}
      set name [get_property NAME $cell]
      dict set groups $slr $name $cell
      dict set targets $name $cell
      dict set assigned $name $slr
      incr count
    }
    puts $stream "$bank\t$direction\t$slr\t$count"
  }
  close $input
  close $stream
  # Bind RX shadow state through the canonical source catalog and the actual
  # native word-clock projections, rather than architectural DUT endpoints.
  set input [open [file join ${placement} rx-bank-roles.tsv] r]
  gets $input header
  set shadow_count 0
  set missing 0
  while {[gets $input row]>=0} {
    lassign [split $row \t] origin bank
    if {![dict exists $source_map rx $origin]} {incr missing;continue}
    set slr [dict get $bank_slrs $bank]
    set cells [get_cells -quiet -of_objects [dict get $source_map rx $origin]]
    foreach cell $cells {
      if {![emuflow_bank_movable $cell]} {continue}
      set name [get_property NAME $cell]
      dict set targets $name $cell
      if {[dict exists $ambiguous $name]} {continue}
      if {[dict exists $assigned $name] && [dict get $assigned $name] ne $slr} {
        dict unset groups [dict get $assigned $name] $name
        dict set ambiguous $name 1
        continue
      }
      dict set assigned $name $slr
      dict set groups $slr $name $cell
      incr shadow_count
    }
  }
  close $input
  # Prefer local replication of generated control nets. DUT logic receives
  # no placement region, identity preservation, or added pipeline.
  set transport_cells [get_cells -quiet -hier -filter "NAME =~ ${prefix}/__emuflow_transport__/* && IS_PRIMITIVE == 1"]
  set control_cells [list]
  dict for {name pins} [dict get $source_map infrastructure_clock_pins] {
    if {![string match ${prefix}/__emuflow_transport__/* $name]} {continue}
    set cell [get_cells -quiet -of_objects [lindex $pins 0]]
    if {![emuflow_bank_movable $cell]} {continue}
    set origin [get_property -quiet EMUFLOW_SOURCE $cell]
    if {![dict exists $source_map rx $origin]} {foreach object $cell {lappend control_cells $object}}
  }
  foreach cell $transport_cells {
    if {[emuflow_bank_movable $cell]} {dict set targets [get_property NAME $cell] $cell}
  }
  set control_nets [list]
  if {[llength $control_cells]} {
    set outputs [get_pins -quiet -of_objects $control_cells -filter {DIRECTION == OUT}]
    if {[llength $outputs]} {set control_nets [get_nets -quiet -of_objects $outputs]}
  }
  if {[llength $control_nets]} {set_property MAX_FANOUT_MODE CLOCK_REGION $control_nets}
  dict for {slr members} $groups {
    if {![dict size $members]} {continue}
    set block [create_pblock emuflow_bank_affinity_$slr]
    add_cells_to_pblock $block [dict values $members]
    set_property IS_SOFT false $block
    resize_pblock $block -add $slr -locs trim
  }
  set stream [open [file join $output bank-placement-metrics.tsv] w]
  puts $stream "metric\tvalue"
  puts $stream "target_cells\t[dict size $targets]"
  puts $stream "shadow_projections\t$shadow_count"
  puts $stream "missing_rx_roles\t$missing"
  puts $stream "ambiguous_native_cells\t[dict size $ambiguous]"
  puts $stream "control_nets\t[llength $control_nets]"
  close $stream
  return [dict create target_cells [dict size $targets] shadow_projections $shadow_count missing_rx_roles $missing ambiguous_native_cells [dict size $ambiguous] control_nets [llength $control_nets]]
}
