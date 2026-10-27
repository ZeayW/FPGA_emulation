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

source [file join [file dirname [info script]] design_context.tcl]
set emuflow_budget_policy legacy-absolute-arrival-v1
if {[info exists env(EMUFLOW_STA_CLOCK_BUDGET_POLICY)]} {set emuflow_budget_policy $env(EMUFLOW_STA_CLOCK_BUDGET_POLICY)}
if {$emuflow_budget_policy ni {legacy-absolute-arrival-v1 engine-edge-v1}} {error "unsupported clock budget export policy"}
source [file join [file dirname [info script]] timing_path_rows.tcl]
set emuflow_path_header [emuflow_path_export_header $emuflow_budget_policy]

set liberty_path [file normalize [emuflow_required_env EMUFLOW_STA_LIBERTY]]
set verilog_path [file normalize [emuflow_required_env EMUFLOW_STA_VERILOG]]
set top [emuflow_required_env EMUFLOW_STA_TOP]
set map_path [file normalize [emuflow_required_env EMUFLOW_STA_NET_MAP]]
set clock_path [file normalize [emuflow_required_env EMUFLOW_STA_CLOCKS]]
set timing_io_path [file normalize [emuflow_required_env EMUFLOW_STA_TIMING_IO]]
set output_path [file normalize [emuflow_required_env EMUFLOW_STA_OUTPUT]]
set max_paths [emuflow_required_env EMUFLOW_STA_MAX_PATHS]
if {![string is integer -strict $max_paths] || $max_paths < 0} {
  error "EMUFLOW_STA_MAX_PATHS must be nonnegative; zero selects all endpoints"
}

set emuflow_allowed_clocks [emuflow_read_design_context]
set clock_count [llength $emuflow_allowed_clocks]
if {$emuflow_budget_policy eq "engine-edge-v1"} {
  if {[catch {emuflow_require_source_timing_coverage} message options]} {
    puts stderr [dict get $options -errorinfo]
    exit 1
  }
}
set emuflow_communication_active 0
if {[info exists env(EMUFLOW_STA_COMMUNICATION_DELAYS)] &&
    $env(EMUFLOW_STA_COMMUNICATION_DELAYS) ne ""} {
  if {$max_paths != 0 || $emuflow_budget_policy ne "engine-edge-v1" ||
      ([info exists env(EMUFLOW_STA_THROUGH_NETS)] && $env(EMUFLOW_STA_THROUGH_NETS) ne "")} {
    error "communication re-search requires uncapped clock-aware endpoint search"
  }
  source [file join [file dirname [info script]] communication_delays.tcl]
  emuflow_apply_communication_delays $env(EMUFLOW_STA_COMMUNICATION_DELAYS)
  set emuflow_communication_active 1
}

emuflow_read_source_net_map $map_path
set emitted 0
set queried_paths 0

if {[info exists env(EMUFLOW_STA_THROUGH_NETS)] &&
    $env(EMUFLOW_STA_THROUGH_NETS) ne ""} {
  set output [open $output_path w]
  puts $output $emuflow_path_header
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
              ($max_paths > 0 && $queried_paths - $before_queried >= $max_paths)} {
            break
          }
        }
        if {$emitted > $before_emitted ||
            ($max_paths > 0 && $queried_paths - $before_queried >= $max_paths)} {
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
  # request the bounded worst-per-endpoint selection once. This is not a
  # certificate of all endpoint or alternative-path coverage. Re-running the
  # path search separately for every endpoint is correct but prohibitively
  # expensive on large designs.
  set endpoints [all_registers -data_pins]
  foreach endpoint [all_outputs] {
    lappend endpoints $endpoint
  }
  set endpoint_count [llength $endpoints]
  set report_limit [expr {$max_paths == 0 ? $endpoint_count : min($max_paths, $endpoint_count)}]
  if {$report_limit <= 0} {
    error "OpenSTA found no timing endpoints"
  }
  set output [open $output_path w]
  puts $output $emuflow_path_header
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
