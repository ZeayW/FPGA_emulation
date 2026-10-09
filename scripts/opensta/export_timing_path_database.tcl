# Export partition-independent OpenSTA paths with ordered EmuIR net identity.
#
# Required environment variables:
#   EMUFLOW_STA_LIBERTY
#   EMUFLOW_STA_VERILOG
#   EMUFLOW_STA_TOP
#   EMUFLOW_STA_NET_MAP
#   EMUFLOW_STA_CLOCKS
#   EMUFLOW_STA_TIMING_IO
#   EMUFLOW_STA_OUTPUT
#   EMUFLOW_STA_MAX_PATHS
# Optional environment variable:
#   EMUFLOW_STA_THROUGH_NETS

proc emuflow_required_env {name} {
  global env
  if {![info exists env($name)] || $env($name) eq ""} {
    error "missing required environment variable $name"
  }
  return $env($name)
}

proc emuflow_hex_decode {value} {
  # Keep the on-disk transport independent of Tcl's newer encode/decode
  # subcommands; the H* representation is stable across supported runtimes.
  return [encoding convertfrom utf-8 [binary format H* $value]]
}

proc emuflow_hex_encode {value} {
  binary scan [encoding convertto utf-8 $value] H* encoded
  return $encoded
}

set liberty_path [file normalize [emuflow_required_env EMUFLOW_STA_LIBERTY]]
set verilog_path [file normalize [emuflow_required_env EMUFLOW_STA_VERILOG]]
set top [emuflow_required_env EMUFLOW_STA_TOP]
set map_path [file normalize [emuflow_required_env EMUFLOW_STA_NET_MAP]]
set clock_path [file normalize [emuflow_required_env EMUFLOW_STA_CLOCKS]]
set timing_io_path [file normalize [emuflow_required_env EMUFLOW_STA_TIMING_IO]]
set output_path [file normalize [emuflow_required_env EMUFLOW_STA_OUTPUT]]
set max_paths [emuflow_required_env EMUFLOW_STA_MAX_PATHS]
if {![string is integer -strict $max_paths] || $max_paths <= 0} {
  error "EMUFLOW_STA_MAX_PATHS must be a positive integer"
}

read_liberty $liberty_path
read_verilog $verilog_path
link_design $top

set clock_input [open $clock_path r]
if {[gets $clock_input clock_header] < 0 ||
    $clock_header ne "clock_hex\tperiod_ns"} {
  close $clock_input
  error "invalid OpenSTA clock-map header"
}
set clock_count 0
while {[gets $clock_input line] >= 0} {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  if {[llength $fields] != 2} {
    error "malformed OpenSTA clock-map row"
  }
  set clock_name [emuflow_hex_decode [lindex $fields 0]]
  set period [lindex $fields 1]
  set port [get_ports -quiet [list $clock_name]]
  if {[llength $port] != 1} {
    error "clock port '$clock_name' is absent or ambiguous"
  }
  create_clock -name $clock_name -period $period $port
  incr clock_count
}
close $clock_input
if {$clock_count == 0} {
  error "OpenSTA requires at least one clock"
}

# Apply the same explicit top-level timing environment used by the black-box
# PPro holdout.  Python has already validated each base identifier against the
# EmuIR port inventory and its direction.  Resolve a scalar exact name first;
# if it is a vector, fall back to the exact-base bus spelling rather than a
# loose prefix that could match an unrelated interface.
set timing_io_input [open $timing_io_path r]
if {[gets $timing_io_input timing_io_header] < 0 ||
    $timing_io_header ne "direction\tclock_hex\tdelay_ns\tport_hex"} {
  close $timing_io_input
  error "invalid OpenSTA timing-I/O header"
}
while {[gets $timing_io_input line] >= 0} {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  if {[llength $fields] != 4} {
    error "malformed OpenSTA timing-I/O row"
  }
  set direction [lindex $fields 0]
  set clock_name [emuflow_hex_decode [lindex $fields 1]]
  set delay [lindex $fields 2]
  set port_name [emuflow_hex_decode [lindex $fields 3]]
  set clock [get_clocks -quiet [list $clock_name]]
  if {[llength $clock] != 1} {
    error "timing-I/O clock '$clock_name' is absent or ambiguous"
  }
  set port_objects [get_ports -quiet [list $port_name]]
  if {[llength $port_objects] == 0} {
    set bus_pattern $port_name
    append bus_pattern {[*]}
    set port_objects [get_ports -quiet [list $bus_pattern]]
  }
  if {[llength $port_objects] == 0} {
    error "timing-I/O port '$port_name' is absent"
  }
  if {$direction eq "input"} {
    set_input_delay -clock $clock -max $delay $port_objects
  } elseif {$direction eq "output"} {
    set_output_delay -clock $clock -max $delay $port_objects
  } else {
    error "unsupported timing-I/O direction '$direction'"
  }
}
close $timing_io_input

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
array set emuir_by_pin_full_name {}

set emitted 0
set queried_paths 0

# Path handles returned by find_timing_paths are owned by OpenSTA and may be
# invalidated by a subsequent query.  Serialize each query immediately instead
# of retaining those handles across the per-cut-net loop.
proc emuflow_emit_timing_paths {
    timing_paths output_var emitted_var {required_net ""}} {
  global emuir_by_mapped_net emuir_by_pin_full_name
  upvar 1 $output_var output
  upvar 1 $emitted_var emitted
  foreach path_end $timing_paths {
    set endpoint_clock [get_property $path_end endpoint_clock]
    if {$endpoint_clock eq "NULL"} {
      continue
    }
    set clock_name [get_property $endpoint_clock name]
    set clock_period [get_property $endpoint_clock period]
    set slack [get_property $path_end slack]
    set points [get_property $path_end points]
    if {[llength $points] == 0} {
      continue
    }
    set fixed_delay [get_property [lindex $points end] arrival]
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
      continue
    }
    set path_hex [list]
    foreach net $path_nets {
      lappend path_hex [emuflow_hex_encode $net]
    }
    set path_id "$start_name->$end_name#[format %08d $emitted]"
    puts $output "[emuflow_hex_encode $path_id]\t[emuflow_hex_encode $clock_name]\t$clock_period\t$slack\t$fixed_delay\t[join $path_hex ,]"
    incr emitted
  }
}

if {[info exists env(EMUFLOW_STA_THROUGH_NETS)] &&
    $env(EMUFLOW_STA_THROUGH_NETS) ne ""} {
  set output [open $output_path w]
  puts $output "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex"
  if {![info exists env(EMUFLOW_STA_THROUGH_COVERAGE)] ||
      $env(EMUFLOW_STA_THROUGH_COVERAGE) eq ""} {
    error "EMUFLOW_STA_THROUGH_COVERAGE is required for directed extraction"
  }
  set coverage_path [file normalize $env(EMUFLOW_STA_THROUGH_COVERAGE)]
  if {![info exists env(EMUFLOW_STA_THROUGH_ENDPOINTS)] ||
      $env(EMUFLOW_STA_THROUGH_ENDPOINTS) eq ""} {
    error "EMUFLOW_STA_THROUGH_ENDPOINTS is required for directed extraction"
  }
  set endpoint_path [file normalize $env(EMUFLOW_STA_THROUGH_ENDPOINTS)]
  set endpoint_input [open $endpoint_path r]
  if {[gets $endpoint_input endpoint_header] < 0 ||
      $endpoint_header ne "emuir_net_hex\tendpoint_pin_hex"} {
    close $endpoint_input
    error "invalid OpenSTA through-endpoint map header"
  }
  array set timed_endpoints {}
  while {[gets $endpoint_input endpoint_line] >= 0} {
    if {$endpoint_line eq ""} {
      continue
    }
    set endpoint_fields [split $endpoint_line "\t"]
    if {[llength $endpoint_fields] != 2} {
      error "malformed OpenSTA through-endpoint map row"
    }
    set endpoint_net [emuflow_hex_decode [lindex $endpoint_fields 0]]
    set endpoint_pin [emuflow_hex_decode [lindex $endpoint_fields 1]]
    lappend timed_endpoints($endpoint_net) $endpoint_pin
  }
  close $endpoint_input
  set coverage_output [open $coverage_path w]
  puts $coverage_output "emuir_net_hex\tdriver_count\tqueried_paths\temitted_paths"
  set through_path [file normalize $env(EMUFLOW_STA_THROUGH_NETS)]
  set through_input [open $through_path r]
  if {[gets $through_input through_header] < 0 ||
      $through_header ne "mapped_net_hex\temuir_net_hex"} {
    close $through_input
    error "invalid OpenSTA through-net map header"
  }
  while {[gets $through_input line] >= 0} {
    if {$line eq ""} {
      continue
    }
    set fields [split $line "\t"]
    if {[llength $fields] != 2} {
      error "malformed OpenSTA through-net map row"
    }
    set mapped_name [emuflow_hex_decode [lindex $fields 0]]
    set emuir_name [emuflow_hex_decode [lindex $fields 1]]
    set through_net [get_nets -quiet [list $mapped_name]]
    if {[llength $through_net] != 1} {
      error "through net '$mapped_name' is absent or ambiguous"
    }
    # Resolve the net to its connected pins before reconstructing the bounded
    # timing cone.  This also makes driver selection explicit and auditable.
    set through_pins [get_pins -quiet -of_objects $through_net]
    if {[llength $through_pins] == 0} {
      error "through net '$mapped_name' has no timing pins"
    }
    # OpenSTA does not treat an internal combinational driver as a legal timing
    # startpoint, so querying -from the cut-net driver silently returns no path.
    # Instead, independently reconstruct the cut's timing cone and query from
    # its real sequential/input startpoints to its real sequential/output
    # endpoints.  The serialized path
    # is still checked below (and again by Python) for the requested EmuIR net,
    # so a reconvergent bypass cannot satisfy the coverage certificate.
    set driver_count 0
    set before_queried $queried_paths
    set before_emitted $emitted
    foreach through_pin $through_pins {
      if {[get_property $through_pin direction] ne "output"} {
        continue
      }
      incr driver_count
      set startpoints [get_fanin -flat -startpoints_only \
        -to [list $through_pin]]
      set endpoints [get_fanout -flat -endpoints_only \
        -from [list $through_pin]]
      if {[llength $startpoints] == 0} {
        error "through net '$mapped_name' has no timing startpoints"
      }
      if {[llength $endpoints] == 0} {
        error "through net '$mapped_name' has no timing endpoints"
      }
      # Query one structural startpoint/endpoint pair at a time.  Passing the
      # whole collections asks OpenSTA only for the globally worst path, which
      # can traverse a sibling of the requested net in a reconvergent cone.
      # Stop after one path whose returned points independently prove coverage.
      foreach startpoint $startpoints {
        foreach endpoint $endpoints {
          foreach path_end [find_timing_paths -path_delay max \
              -from [list $startpoint] -to [list $endpoint] \
              -group_path_count 1 -endpoint_path_count 1 \
              -sort_by_slack] {
            set timing_paths [list $path_end]
            incr queried_paths
            emuflow_emit_timing_paths \
              $timing_paths output emitted $emuir_name
          }
          if {$emitted > $before_emitted ||
              $queried_paths - $before_queried >= $max_paths} {
            break
          }
        }
        if {$emitted > $before_emitted ||
            $queried_paths - $before_queried >= $max_paths} {
          break
        }
      }
    }
    # A constant-propagated or otherwise non-startpoint LUT output can be a
    # real cut net that directly feeds a clocked data pin even though OpenSTA
    # declines to use that internal output as a -from startpoint.  In that
    # narrow case, query the independently identified direct timed endpoint.
    # Any path ending at that exact data pin necessarily traverses this net.
    if {$emitted == $before_emitted &&
        [info exists timed_endpoints($emuir_name)]} {
      foreach endpoint_name $timed_endpoints($emuir_name) {
        set endpoint_pin [get_pins -quiet [list $endpoint_name]]
        if {[llength $endpoint_pin] != 1} {
          error "timed endpoint '$endpoint_name' is absent or ambiguous"
        }
        foreach path_end [find_timing_paths -path_delay max \
            -to $endpoint_pin -group_path_count 1 -endpoint_path_count 1 \
            -sort_by_slack] {
          set timing_paths [list $path_end]
          incr queried_paths
          emuflow_emit_timing_paths \
            $timing_paths output emitted $emuir_name
        }
        if {$emitted > $before_emitted} {
          break
        }
      }
    }
    if {$driver_count == 0} {
      error "through net '$mapped_name' has no driver pin"
    }
    puts $coverage_output "[emuflow_hex_encode $emuir_name]\t$driver_count\t[expr {$queried_paths - $before_queried}]\t[expr {$emitted - $before_emitted}]"
  }
  close $through_input
  close $coverage_output
  close $output
  if {$emitted == 0} {
    error "OpenSTA found no timing paths containing mapped EmuIR nets"
  }
  puts "EMUFLOW_OPENSTA_DATABASE status=pass clocks=$clock_count queried_paths=$queried_paths emitted_paths=$emitted output=$output_path"
} else {
  # OpenSTA 3.1 fixes the legacy PathEnd/Tcl object-lifetime corruption, so
  # request the bounded endpoint-complete collection once.  Re-running the
  # path search separately for every endpoint is correct but prohibitively
  # expensive on large designs.
  set endpoints [all_registers -data_pins]
  foreach endpoint [all_outputs] {
    lappend endpoints $endpoint
  }
  set endpoint_count [llength $endpoints]
  set report_limit [expr {min($max_paths, $endpoint_count)}]
  if {$report_limit <= 0} {
    error "OpenSTA found no timing endpoints"
  }
  set output [open $output_path w]
  puts $output "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex"
  set timing_paths [find_timing_paths -path_delay max \
      -group_path_count $report_limit -endpoint_path_count 1 \
      -sort_by_slack]
  set queried_paths [llength $timing_paths]
  emuflow_emit_timing_paths $timing_paths output emitted
  close $output
  if {$emitted == 0} {
    error "OpenSTA found no timing paths containing mapped EmuIR nets"
  }
  puts "EMUFLOW_OPENSTA_DATABASE status=pass clocks=$clock_count queried_paths=$queried_paths emitted_paths=$emitted output=$output_path"
}
