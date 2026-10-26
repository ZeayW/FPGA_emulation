# Re-search the source timing graph after adding communication costs to data
# wires. This is an engine adapter: costs and endpoint identities are supplied
# by the caller, not inferred from an already selected timing-path population.
# Keep the incremental cost separately so exported source members retain their
# original local delay/slack; a later partition evaluator must not charge the
# same communication delay twice.

proc emuflow_communication_pin {encoded direction} {
  if {![regexp {^(?:[0-9a-f]{2})+$} $encoded]} {
    error "invalid communication pin encoding"
  }
  set name [emuflow_hex_decode $encoded]
  if {[string first \x00 $name] >= 0 || [emuflow_hex_encode $name] ne $encoded} {
    error "invalid communication pin identity"
  }
  # A mapped instance can literally contain [0:0], *, or ?. get_pins treats
  # its argument as a pattern even when it is supplied as a one-element list.
  # Use the engine's exact network lookup and check its canonical spelling.
  set pin [sta::find_pin $name]
  if {$pin eq "NULL" || [get_property $pin full_name] ne $name} {
    error "communication pin is absent or ambiguous: $name"
  }
  if {[get_property $pin direction] ne $direction || [get_property $pin is_register_clock]} {
    error "communication endpoint is not a data-wire pin: $name"
  }
  return $pin
}

proc emuflow_communication_require_data_clock {pin} {
  # Annotating a clock cone would alter launch/capture timing instead of a
  # data crossing. Reject any clock-tagged endpoint, including a clock buffer
  # input which is not itself a register's clock pin.
  # The engine owns a clock-network index for the current analysis mode.
  # Walking every launch-path tag at a data pin repeats unrelated data paths
  # and can dwarf the wire update on a large reconvergent cone.
  if {[sta::is_clock $pin]} {
    error "communication delay touches a clock cone: [get_property $pin full_name]"
  }
}

proc emuflow_apply_communication_delays {filename} {
  global emuflow_communication_by_pair emuflow_communication_arcs
  global emuflow_communication_binding_edges
  global emuflow_communication_owner emuflow_design_context_epoch
  if {![info exists emuflow_design_context_epoch]} {
    error "communication delays require a linked source design context"
  }
  set scene [sta::cmd_scene]
  set owner [list $emuflow_design_context_epoch $scene]
  if {![info exists emuflow_communication_owner] ||
      [lindex $emuflow_communication_owner 0] != $emuflow_design_context_epoch} {
    # link_design replaced the old graph. Do not dereference its old handles.
    set emuflow_communication_arcs [dict create]
    set emuflow_communication_by_pair [dict create]
    set emuflow_communication_owner $owner
  } elseif {$emuflow_communication_owner ne $owner} {
    error "communication annotation scene changed within the source context"
  }
  set extra_by_pair [dict create]
  set records {}
  # Validate names and the transport table before paying for a full timing
  # update. Keep only bound pin handles; no PathEnd/PathRef survives a query.
  set stream [open $filename r]
  try {
    if {[gets $stream header] < 0 || $header ne "from_pin_hex\tto_pin_hex\textra_delay_ns"} {
      error "invalid communication delay header"
    }
    while {[gets $stream line] >= 0} {
      if {$line eq ""} {error "empty communication delay row"}
      set fields [split $line "\t"]
      if {[llength $fields] != 3} {error "malformed communication delay row"}
      lassign $fields from_hex to_hex extra
      if {![string is double -strict $extra] ||
          ![expr {abs(double($extra)) < Inf && double($extra) >= 0}]} {
        error "communication delay must be finite and nonnegative"
      }
      set from [emuflow_communication_pin $from_hex output]
      set to [emuflow_communication_pin $to_hex input]
      set key [list [get_property $from full_name] [get_property $to full_name]]
      if {[dict exists $extra_by_pair $key]} {
        error "duplicate communication wire"
      }
      set extra_sta [sta::time_ui_sta $extra]
      if {![expr {abs(double($extra_sta)) < Inf}]} {error "communication delay exceeds engine units"}
      dict set extra_by_pair $key [expr {double($extra)}]
      lappend records [list $from $to $extra_sta]
    }
  } finally {close $stream}
  # Prepare every annotation before changing the graph. A malformed later
  # row must not leave an earlier wire annotated if the caller catches failure.
  sta::find_timing_cmd 1
  set plan [dict create]
  set inspected_edges 0
  foreach record $records {
      lassign $record from to extra_sta
      emuflow_communication_require_data_clock $from
      emuflow_communication_require_data_clock $to
      set matched 0
      # A source may drive thousands of sinks. Looking through its fanout for
      # each sink repeats that population quadratically. The input's incoming
      # wire identifies the same graph edge without scanning unrelated loads.
      foreach to_vertex [$to vertices] {
        if {$to_vertex eq "NULL"} {continue}
        set edges [$to_vertex in_edge_iterator]
        try {
          while {[$edges has_next]} {
            set edge [$edges next]
            incr inspected_edges
            if {[$edge from_pin] ne $from || [$edge role] ne "wire"} {continue}
            if {[$edge is_disabled_loop] || [$edge is_disabled_constraint] ||
                [$edge is_disabled_constant] || [$edge is_disabled_cond_default] ||
                [$edge is_disabled_bidirect_inst_path] || [$edge is_disabled_preset_clear]} {
              error "communication wire is disabled in the source timing graph"
            }
            foreach arc [$edge timing_arcs] {
              set key [list $edge $arc]
              if {[dict exists $plan $key]} {error "duplicate communication timing arc"}
              set base [$edge arc_delay $arc $scene max]
              if {[dict exists $emuflow_communication_arcs $key]} {
                set base [lindex [dict get $emuflow_communication_arcs $key] 0]
              }
              set delay [expr {$base + $extra_sta}]
              # OpenSTA stores Delay in single precision, even though Tcl
              # computes in double precision. Reject overflow before mutation.
              if {![expr {abs(double($delay)) <= 3.4028234663852886e38}]} {
                error "communication arc delay exceeds engine units"
              }
              dict set plan $key [list $base $delay]
              incr matched
            }
          }
        } finally {$edges finish}
      }
      if {$matched == 0} {error "communication endpoints have no source timing wire"}
  }
  # Check all previously owned arcs before applying any change. Other delay
  # writers cannot silently become the new baseline for a later assignment.
  dict for {key previous} $emuflow_communication_arcs {
    lassign $key edge arc
    if {[$edge arc_delay $arc $scene max] != [lindex $previous 1]} {
      error "source wire delay changed outside the communication update"
    }
  }
  # A new assignment may make an old crossing local. Restore every omitted arc
  # before replacing retained crossings; never add to a previous candidate.
  dict for {key previous} $emuflow_communication_arcs {
    if {![dict exists $plan $key]} {
      lassign $key edge arc
      sta::set_arc_delay $edge $arc $scene max [lindex $previous 0]
    }
  }
  set owned [dict create]
  dict for {key annotation} $plan {
    lassign $key edge arc
    lassign $annotation base delay
    if {![dict exists $emuflow_communication_arcs $key] ||
        [lindex [dict get $emuflow_communication_arcs $key] 2] != $delay} {
      sta::set_arc_delay $edge $arc $scene max $delay
    }
    # Keep the value read back from the engine, including its float rounding.
    dict set owned $key [list $base [$edge arc_delay $arc $scene max] $delay]
  }
  set emuflow_communication_arcs $owned
  set emuflow_communication_by_pair $extra_by_pair
  set emuflow_communication_binding_edges $inspected_edges
  set count [llength $records]
  puts "EMUFLOW_COMMUNICATION_DELAYS status=pass records=$count"
  puts "EMUFLOW_COMMUNICATION_BINDINGS status=pass records=$count inspected_edges=$inspected_edges"
  return $count
}

proc emuflow_path_communication_delay {points} {
  global emuflow_communication_by_pair
  set extra 0.0
  set previous ""
  foreach point $points {
    set name [get_property [get_property $point pin] full_name]
    set key [list $previous $name]
    if {[dict exists $emuflow_communication_by_pair $key]} {
      set extra [expr {$extra + [dict get $emuflow_communication_by_pair $key]}]
    }
    set previous $name
  }
  return $extra
}
