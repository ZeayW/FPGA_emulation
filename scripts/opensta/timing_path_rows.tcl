proc emuflow_path_export_header {policy} {
  global emuflow_budget_policy emuflow_budget_fields
  set emuflow_budget_policy $policy
  set emuflow_path_header "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex"
  set emuflow_budget_fields {source_clock source_period_ns launch_edge_ns capture_edge_ns source_offset_ns target_cycle_time_ns multicycle_adjustment_ns check_margin_ns source_latency_ns target_clock_delay_ns clock_skew_ns output_delay path_delay latch_check}
  if {$emuflow_budget_policy eq "engine-edge-v1"} {
    source [file join [file dirname [info script]] clock_budget.tcl]
    foreach key $emuflow_budget_fields {
      append emuflow_path_header "\t" [expr {$key eq "source_clock" ? "source_clock_hex" : $key}]
    }
  }
  return $emuflow_path_header
}

# Shared source-path serialization for Phase2 and checkpoint feedback.
# The caller supplies the native original-net map and engine clock policy.
proc emuflow_read_source_net_map {map_path} {
  global emuir_by_mapped_net emuir_by_pin_full_name
  set map_input [open $map_path r]
  if {[gets $map_input map_header] < 0} {
    close $map_input
    error "empty EmuIR net-map"
  }
  if {$map_header ne "mapped_net_hex\temuir_net_hex" &&
      $map_header ne "vivado_net_hex\temuir_net_hex"} {
    close $map_input
    error "invalid EmuIR net-map header"
  }
  unset -nocomplain emuir_by_mapped_net
  array set emuir_by_mapped_net {}
  while {[gets $map_input line] >= 0} {
    if {$line eq ""} {
      continue
    }
    set fields [split $line "\t"]
    if {[llength $fields] != 2} {
      error "malformed EmuIR net-map row"
    }
    set mapped_name [emuflow_hex_decode [lindex $fields 0]]
    set emuir_name [emuflow_hex_decode [lindex $fields 1]]
    if {[info exists emuir_by_mapped_net($mapped_name)] && $emuir_by_mapped_net($mapped_name) ne $emuir_name} {
      error "ambiguous source net-map identity: $mapped_name"
    }
    set emuir_by_mapped_net($mapped_name) $emuir_name
  }
  close $map_input

  # Resolve only pins that actually occur on exported timing paths.  A complete
  # pin map duplicates every flattened instance/pin name and exceeded Tcl's 2 GiB
  # value limit on real Koios netlists.  The mapped Verilog is flat and every
  # connected pin belongs to one generated ``__emuflow_net_<index>`` net, whose
  # independently sealed EmuIR identity is already present in the net map above.
  # Cache those graph lookups lazily so memory and work scale with reported paths,
  # not with every pin in the design.
  unset -nocomplain emuir_by_pin_full_name
  array set emuir_by_pin_full_name {}
}

proc emuflow_emit_timing_paths {
    timing_paths output_var emitted_var {required_net ""}} {
  global emuir_by_mapped_net emuir_by_pin_full_name emuflow_budget_policy emuflow_budget_fields emuflow_communication_active
  upvar 1 $output_var output
  upvar 1 $emitted_var emitted
  foreach path_end $timing_paths {
    set endpoint_clock [get_property $path_end endpoint_clock]
    if {$endpoint_clock eq "NULL"} {
      if {$required_net eq ""} {error "source timing path has no capture clock"}
      continue
    }
    set clock_name [get_property $endpoint_clock name]
    set clock_period [get_property $endpoint_clock period]
    set slack [get_property $path_end slack]
    set points [get_property $path_end points]
    if {[llength $points] == 0} {
      if {$required_net eq ""} {error "source timing path has no points"}
      continue
    }
    set fixed_delay [get_property [lindex $points end] arrival]
    if {$emuflow_budget_policy eq "engine-edge-v1"} {
      set budget [emuflow_clock_budget $path_end]
      if {[dict get $budget path_delay] || [dict get $budget latch_check]} {
        error "clock-edge export requires a supported clocked setup/output check"
      }
      if {[dict get $budget target_clock] ne $clock_name} {error "clock budget capture clock differs"}
      set fixed_delay [dict get $budget launch_relative_arrival_ns]
    }
    if {$emuflow_communication_active} {
      # Selection used the reweighted *whole* timing graph. Restore source
      # costs on each newly discovered member for the partition evaluator.
      set communication [emuflow_path_communication_delay $points]
      set fixed_delay [expr {$fixed_delay - $communication}]
      set slack [expr {$slack + $communication}]
    }
    set startpoint [get_property $path_end startpoint]
    set endpoint [get_property $path_end endpoint]
    set start_name [get_property $startpoint full_name]
    set end_name [get_property $endpoint full_name]

    set path_nets [list]
    unset -nocomplain seen_net
    array set seen_net {}
    foreach point $points {
      set pin [get_property $point pin]
      set pin_full_name [get_property $pin full_name]
      if {![info exists emuir_by_pin_full_name($pin_full_name)]} {
        set pin_nets [get_nets -quiet -of_objects [list $pin]]
        set resolved_emuir_name ""
        if {[llength $pin_nets] == 1 && [lindex $pin_nets 0] ne "NULL"} {
          set mapped_name [get_property [lindex $pin_nets 0] name]
          if {[info exists emuir_by_mapped_net($mapped_name)]} {
            set resolved_emuir_name $emuir_by_mapped_net($mapped_name)
          }
        }
        set emuir_by_pin_full_name($pin_full_name) $resolved_emuir_name
      }
      set emuir_name $emuir_by_pin_full_name($pin_full_name)
      if {$emuir_name ne "" && ![info exists seen_net($emuir_name)]} {
        set seen_net($emuir_name) 1
        lappend path_nets $emuir_name
      }
    }
    # A directed cut-net certificate is valid only when the path returned by
    # OpenSTA actually contains that mapped EmuIR net.  Never insert a requested
    # net synthetically: reconvergent cones can otherwise make an unrelated
    # worst path look like proof for a different branch.
    if {$required_net ne "" && ![info exists seen_net($required_net)]} {
      continue
    }
    if {[llength $path_nets] == 0} {
      if {$required_net eq ""} {error "source timing path cannot resolve original nets: $start_name -> $end_name"}
      continue
    }
    set path_hex [list]
    foreach net $path_nets {
      lappend path_hex [emuflow_hex_encode $net]
    }
    set path_id "$start_name->$end_name#[format %08d $emitted]"
    set row "[emuflow_hex_encode $path_id]\t[emuflow_hex_encode $clock_name]\t$clock_period\t$slack\t$fixed_delay\t[join $path_hex ,]"
    if {$emuflow_budget_policy eq "engine-edge-v1"} {
      foreach key $emuflow_budget_fields {
        set value [dict get $budget $key]
        if {$key eq "source_clock"} {set value [emuflow_hex_encode $value]}
        append row "\t" $value
      }
    }
    puts $output $row
    incr emitted
  }
}
