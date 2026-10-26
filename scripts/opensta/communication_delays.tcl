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
  # Annotating a clock cone would alter launch/capture timing instead of a
  # data crossing. Reject any clock-tagged endpoint, including a clock buffer
  # input which is not itself a register's clock pin.
  foreach vertex [$pin vertices] {
    if {$vertex eq "NULL"} {continue}
    foreach transition {rise fall} {
      set paths [$vertex path_iterator $transition max]
      set clocked 0
      while {[$paths has_next]} {
        set path [$paths next]
        if {[$path is_clock]} {set clocked 1; break}
      }
      $paths finish
      if {$clocked} {error "communication delay touches a clock cone: $name"}
    }
  }
  return $pin
}

proc emuflow_apply_communication_delays {filename} {
  global emuflow_communication_by_pair
  set emuflow_communication_by_pair [dict create]
  # Materialize engine delays/tags before reading any baseline arc cost. All
  # subsequent annotation invalidation/propagation belongs to OpenSTA.
  sta::find_timing_cmd 1
  set scene [sta::cmd_scene]
  set stream [open $filename r]
  try {
    if {[gets $stream header] < 0 || $header ne "from_pin_hex\tto_pin_hex\textra_delay_ns"} {
      error "invalid communication delay header"
    }
    set records 0
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
      if {[dict exists $emuflow_communication_by_pair $key]} {
        error "duplicate communication wire"
      }
      set extra_sta [sta::time_ui_sta $extra]
      if {![expr {abs(double($extra_sta)) < Inf}]} {error "communication delay exceeds engine units"}
      set matched 0
      foreach from_vertex [$from vertices] {
        if {$from_vertex eq "NULL"} {continue}
        set edges [$from_vertex out_edge_iterator]
        try {
          while {[$edges has_next]} {
            set edge [$edges next]
            if {[$edge to_pin] ne $to || [$edge role] ne "wire"} {continue}
            if {[$edge is_disabled_loop] || [$edge is_disabled_constraint] ||
                [$edge is_disabled_constant] || [$edge is_disabled_cond_default] ||
                [$edge is_disabled_bidirect_inst_path] || [$edge is_disabled_preset_clear]} {
              error "communication wire is disabled in the source timing graph"
            }
            foreach arc [$edge timing_arcs] {
              set delay [expr {[$edge arc_delay $arc $scene max] + $extra_sta}]
              if {![expr {abs(double($delay)) < Inf}]} {error "communication arc delay exceeds engine units"}
              sta::set_arc_delay $edge $arc $scene max $delay
              incr matched
            }
          }
        } finally {$edges finish}
      }
      if {$matched == 0} {error "communication endpoints have no source timing wire"}
      dict set emuflow_communication_by_pair $key [expr {double($extra)}]
      incr records
    }
  } finally {close $stream}
  puts "EMUFLOW_COMMUNICATION_DELAYS status=pass records=$records"
  return $records
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
