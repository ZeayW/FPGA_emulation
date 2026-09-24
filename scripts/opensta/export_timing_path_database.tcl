# Export partition-independent OpenSTA paths with ordered EmuIR net identity.
#
# Required environment variables:
#   EMUFLOW_STA_LIBERTY
#   EMUFLOW_STA_VERILOG
#   EMUFLOW_STA_TOP
#   EMUFLOW_STA_NET_MAP
#   EMUFLOW_STA_CLOCKS
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
  # OpenSTA can be linked against Tcl 8.5 on supported HPC hosts.  The
  # `binary encode/decode` subcommands were only added in Tcl 8.6, while the
  # H* format and scan forms have been available since Tcl 8.4.
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
set output_path [file normalize [emuflow_required_env EMUFLOW_STA_OUTPUT]]
set max_paths [emuflow_required_env EMUFLOW_STA_MAX_PATHS]
if {![string is integer -strict $max_paths] || $max_paths <= 0} {
  error "EMUFLOW_STA_MAX_PATHS must be a positive integer"
}

read_liberty $liberty_path
read_verilog $verilog_path
link_design $top

set clock_input [open $clock_path r]
set clock_lines [split [read $clock_input] "\n"]
close $clock_input
if {[lindex $clock_lines 0] ne "clock_hex\tperiod_ns"} {
  error "invalid OpenSTA clock-map header"
}
set clock_count 0
foreach line [lrange $clock_lines 1 end] {
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
if {$clock_count == 0} {
  error "OpenSTA requires at least one clock"
}

set map_input [open $map_path r]
set map_lines [split [read $map_input] "\n"]
close $map_input
set map_header [lindex $map_lines 0]
if {$map_header ne "mapped_net_hex\temuir_net_hex" &&
    $map_header ne "vivado_net_hex\temuir_net_hex"} {
  error "invalid EmuIR net-map header"
}
array set emuir_by_mapped_net {}
foreach line [lrange $map_lines 1 end] {
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

set output [open $output_path w]
puts $output "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex"
set emitted 0
set queried_paths 0

# Path handles returned by find_timing_paths are owned by OpenSTA and may be
# invalidated by a subsequent query.  Serialize each query immediately instead
# of retaining those handles across the per-cut-net loop.
proc emuflow_emit_timing_paths {
    timing_paths output_var emitted_var {required_net ""}} {
  global emuir_by_mapped_net
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
      foreach net [get_nets -quiet -of_objects $pin] {
        set mapped_name [get_property $net full_name]
        if {![info exists emuir_by_mapped_net($mapped_name)]} {
          set mapped_name [get_property $net name]
        }
        if {[info exists emuir_by_mapped_net($mapped_name)]} {
          set emuir_name $emuir_by_mapped_net($mapped_name)
          if {![info exists seen_net($emuir_name)]} {
            set seen_net($emuir_name) 1
            lappend path_nets $emuir_name
          }
        }
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
  set endpoint_lines [split [read $endpoint_input] "\n"]
  close $endpoint_input
  if {[lindex $endpoint_lines 0] ne "emuir_net_hex\tendpoint_pin_hex"} {
    error "invalid OpenSTA through-endpoint map header"
  }
  array set timed_endpoints {}
  foreach endpoint_line [lrange $endpoint_lines 1 end] {
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
  set coverage_output [open $coverage_path w]
  puts $coverage_output "emuir_net_hex\tdriver_count\tqueried_paths\temitted_paths"
  set through_path [file normalize $env(EMUFLOW_STA_THROUGH_NETS)]
  set through_input [open $through_path r]
  set through_lines [split [read $through_input] "\n"]
  close $through_input
  if {[lindex $through_lines 0] ne "mapped_net_hex\temuir_net_hex"} {
    error "invalid OpenSTA through-net map header"
  }
  foreach line [lrange $through_lines 1 end] {
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
    # OpenSTA 2.6 accepts pins and nets for -through, but its Tcl net
    # collection path can dereference invalid state.  Resolve the net to its
    # connected pins first; this is semantically equivalent for a timing path.
    set through_pins [get_pins -quiet -of_objects $through_net]
    if {[llength $through_pins] == 0} {
      error "through net '$mapped_name' has no timing pins"
    }
    # OpenSTA does not treat an internal combinational driver as a legal timing
    # startpoint, so querying -from the cut-net driver silently returns no path.
    # Its 2.6 -through collection path is also unsafe.  Instead, independently
    # reconstruct the cut's timing cone and query from its real sequential/input
    # startpoints to its real sequential/output endpoints.  The serialized path
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
              -group_count 1 -endpoint_count 1 \
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
            -to $endpoint_pin -group_count 1 -endpoint_count 1 \
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
  close $coverage_output
} else {
  # Do not retain a bulk collection of PathEnd handles while traversing the
  # points and nets of every path.  OpenSTA owns those handles, and its 2.6
  # Tcl interface can corrupt the collection while nested object queries are
  # in progress.  Query one timed endpoint at a time and serialize the result
  # before issuing the next query.  This still exports exactly the worst setup
  # path per sequential endpoint, which is the contract formerly requested by
  # `-endpoint_count 1` on the bulk query.
  unset -nocomplain seen_endpoint
  array set seen_endpoint {}
  foreach endpoint [all_registers -data_pins] {
    set endpoint_name [get_property $endpoint full_name]
    if {[info exists seen_endpoint($endpoint_name)]} {
      continue
    }
    set seen_endpoint($endpoint_name) 1
    foreach path_end [find_timing_paths -path_delay max \
        -to [list $endpoint] -group_count 1 -endpoint_count 1 \
        -sort_by_slack] {
      set timing_paths [list $path_end]
      incr queried_paths
      emuflow_emit_timing_paths $timing_paths output emitted
    }
    if {$queried_paths >= $max_paths} {
      break
    }
  }
}
close $output

if {$emitted == 0} {
  error "OpenSTA found no timing paths containing mapped EmuIR nets"
}
puts "EMUFLOW_OPENSTA_DATABASE status=pass clocks=$clock_count queried_paths=$queried_paths emitted_paths=$emitted output=$output_path"
