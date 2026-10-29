# Keep native bank logic, TX selection cones and RX shadow state in their
# I/O bank SLR. Shared control remains available for native replication.
# Only placement regions and native control-net replication preferences are
# added. No clock exception, endpoint preservation, or pipeline is introduced.
proc emuflow_bank_movable {cell} {
  set ref [get_property REF_NAME $cell]
  return [expr {[regexp {^(FD[A-Z]+|LD[A-Z]+|LUT[1-6](_2)?|CARRY[48]|MUXF[789])$} $ref] &&
      ![get_property IS_LOC_FIXED $cell]}]
}

proc emuflow_bank_transport_cone {pins prefix instance} {
  # Walk through the bank's own combinational input gating to the generated
  # transport muxes. Stop at every state element and at DUT logic.
  set frontier $pins
  set visited [dict create]
  set members [dict create]
  set examined [dict create]
  while {[llength $frontier]} {
    set pending [list]
    foreach net [get_nets -quiet -segments -of_objects $frontier] {
      set name [get_property NAME $net]
      if {[dict exists $visited $name]} {continue}
      dict set visited $name 1
      lappend pending $net
    }
    if {![llength $pending]} {break}
    set drivers [get_pins -quiet -leaf -of_objects $pending -filter {DIRECTION == OUT}]
    if {![llength $drivers]} {break}
    set next [list]
    foreach cell [get_cells -quiet -of_objects $drivers] {
      set name [get_property NAME $cell]
      set transport [string match ${prefix}/__emuflow_transport__/* $name]
      if {(!$transport && ![string match ${instance}/* $name]) ||
          ![regexp {^(LUT[1-6](_2)?|CARRY[48]|MUXF[789])$} [get_property REF_NAME $cell]] ||
          [dict exists $examined $name]} {continue}
      dict set examined $name 1
      if {$transport} {dict set members $name $cell}
      lappend next $cell
    }
    if {![llength $next]} {break}
    set frontier [get_pins -quiet -of_objects $next -filter {DIRECTION == IN}]
  }
  return [dict values $members]
}

proc emuflow_bank_add_member {groups_name assigned_name ambiguous_name slr cell} {
  upvar 1 $groups_name groups $assigned_name assigned $ambiguous_name ambiguous
  set name [get_property NAME $cell]
  if {[dict exists $ambiguous $name]} {return 0}
  if {[dict exists $assigned $name] && [dict get $assigned $name] ne $slr} {
    dict unset groups [dict get $assigned $name] $name
    dict set ambiguous $name 1
    return 0
  }
  dict set assigned $name $slr
  dict set groups $slr $name $cell
  return 1
}

proc emuflow_bank_control_nets {sources prefix} {
  set cells [list]
  dict for {name pins} [dict get $sources infrastructure_clock_pins] {
    if {![string match ${prefix}/__emuflow_transport__/* $name]} {continue}
    set cell [get_cells -quiet -of_objects [lindex $pins 0]]
    if {![emuflow_bank_movable $cell]} {continue}
    if {![dict exists $sources rx [get_property -quiet EMUFLOW_SOURCE $cell]]} {
      foreach object $cell {lappend cells $object}
    }
  }
  if {![llength $cells]} {return [list]}
  set outputs [get_pins -quiet -of_objects $cells -filter {DIRECTION == OUT}]
  if {![llength $outputs]} {return [list]}
  return [get_nets -quiet -of_objects $outputs]
}

proc emuflow_bank_tx_data_pins {cells} {
  set pins [list]
  foreach cell $cells {
    if {[get_property REF_NAME $cell] ne "TX_BITSLICE"} {continue}
    foreach pin [get_pins -quiet -of_objects $cell -filter {DIRECTION == IN}] {
      if {[regexp {^D\[[0-7]\]$} [get_property REF_PIN_NAME $pin]]} {lappend pins $pin}
    }
  }
  return $pins
}

proc emuflow_native_bank_replication_nets {placement sources prefix} {
  # Placer hints can be consumed or rewritten by placement. Select current
  # drivers again from native source roles and connectivity, independently
  # of hint inheritance, old object names, or primitive preservation.
  set controls [emuflow_bank_control_nets $sources $prefix]
  set assigned [dict create]
  set shared [dict create]
  set input [open [file join $placement banks.tsv] r]
  gets $input header
  while {[gets $input row]>=0} {
    lassign [split $row \t] bank direction instance
    if {$direction ne "tx"} {continue}
    set slr [get_property NAME [get_slrs -quiet -of_objects [get_iobanks -quiet [list $bank]]]]
    set serializers [get_cells -quiet -hier -filter "NAME =~ ${instance}/* && REF_NAME == TX_BITSLICE"]
    set cone [emuflow_bank_transport_cone [emuflow_bank_tx_data_pins $serializers] $prefix $instance]
    foreach cell $cone {
      if {![emuflow_bank_movable $cell]} {continue}
      set name [get_property NAME $cell]
      if {[dict exists $assigned $name] && [dict get $assigned $name] ne $slr} {
        dict set shared $name $cell
      }
      dict set assigned $name $slr
    }
  }
  close $input
  set shared_nets [list]
  if {[dict size $shared]} {
    set outputs [get_pins -quiet -of_objects [dict values $shared] -filter {DIRECTION == OUT}]
    # Only driver-local segments are force targets. Hierarchical aliases of
    # the same driver do not require separate replication operations.
    if {[llength $outputs]} {set shared_nets [get_nets -quiet -of_objects $outputs]}
  }
  return [dict create control_nets $controls shared_tx_nets $shared_nets \
      nets [lsort -unique [concat $controls $shared_nets]]]
}

proc emuflow_native_bank_replicate {placement sources output prefix} {
  set plan [emuflow_native_bank_replication_nets $placement $sources $prefix]
  set nets [dict get $plan nets]
  set stream [open [file join $output bank-replication-nets.tsv] w]
  puts $stream "net_hex"
  foreach net $nets {puts $stream [binary encode hex [get_property NAME $net]]}
  close $stream
  set stream [open [file join $output bank-replication-metrics.tsv] w]
  puts $stream "metric\tvalue"
  puts $stream "control_driver_nets\t[llength [dict get $plan control_nets]]"
  puts $stream "shared_tx_driver_nets\t[llength [dict get $plan shared_tx_nets]]"
  puts $stream "force_replication_targets\t[llength $nets]"
  close $stream
  if {[llength $nets]} {phys_opt_design -force_replication_on_nets $nets}
  return $plan
}

proc emuflow_native_bank_placement {placement sources output prefix} {
  set source_map $sources
  set bank_slrs [dict create]
  set groups [dict create]
  set targets [dict create]
  set assigned [dict create]
  set ambiguous [dict create]
  set tx_candidates [dict create]
  set stream [open [file join $output bank-affinity.tsv] w]
  puts $stream "bank\tdirection\tslr\tnative_movable_cells"
  set tx_stream [open [file join $output bank-tx-cones.tsv] w]
  puts $tx_stream "bank\tslr\ttransport_combinational_cells"
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
    if {$direction eq "tx"} {
      set data_pins [emuflow_bank_tx_data_pins $cells]
      set cone [emuflow_bank_transport_cone $data_pins $prefix $instance]
      foreach cell $cone {
        if {![emuflow_bank_movable $cell]} {continue}
        set name [get_property NAME $cell]
        dict set tx_candidates $name $cell
        dict set targets $name $cell
        emuflow_bank_add_member groups assigned ambiguous $slr $cell
      }
      puts $tx_stream "$bank\t$slr\t[llength $cone]"
    }
  }
  close $input
  close $stream
  close $tx_stream
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
      incr shadow_count [emuflow_bank_add_member groups assigned ambiguous $slr $cell]
    }
  }
  close $input
  # Prefer local replication of generated control nets. DUT logic receives
  # no placement region, identity preservation, or added pipeline.
  set transport_cells [get_cells -quiet -hier -filter "NAME =~ ${prefix}/__emuflow_transport__/* && IS_PRIMITIVE == 1"]
  foreach cell $transport_cells {
    if {[emuflow_bank_movable $cell]} {dict set targets [get_property NAME $cell] $cell}
  }
  set control_nets [emuflow_bank_control_nets $source_map $prefix]
  if {[llength $control_nets]} {set_property MAX_FANOUT_MODE CLOCK_REGION $control_nets}
  set shared_tx [list]
  dict for {name cell} $tx_candidates {
    if {[dict exists $ambiguous $name]} {lappend shared_tx $cell}
  }
  set shared_tx_nets [list]
  if {[llength $shared_tx]} {
    set outputs [get_pins -quiet -of_objects $shared_tx -filter {DIRECTION == OUT}]
    if {[llength $outputs]} {set shared_tx_nets [get_nets -quiet -segments -of_objects $outputs]}
  }
  if {[llength $shared_tx_nets]} {set_property MAX_FANOUT_MODE CLOCK_REGION $shared_tx_nets}
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
  puts $stream "tx_combinational_candidates\t[dict size $tx_candidates]"
  puts $stream "shared_tx_combinational_cells\t[llength $shared_tx]"
  puts $stream "shared_tx_combinational_nets\t[llength $shared_tx_nets]"
  close $stream
  return [dict create target_cells [dict size $targets] shadow_projections $shadow_count missing_rx_roles $missing ambiguous_native_cells [dict size $ambiguous] control_nets [llength $control_nets] tx_combinational_candidates [dict size $tx_candidates] shared_tx_combinational_cells [llength $shared_tx] shared_tx_combinational_nets [llength $shared_tx_nets]]
}
