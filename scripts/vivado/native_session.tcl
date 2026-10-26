# Small stage plans bridge the native design and source-bound SQLite queries.
proc emuflow_validate_synthesized_identity {part top} {
  # NAME is the checkpoint container label after open_checkpoint. TOP is the
  # HDL module identity and survives changing the checkpoint's filename.
  set design [current_design]
  if {[get_property PART $design] ne $part || [get_property TOP $design] ne $top} {
    error "synthesized continuation part/top differs"
  }
}

proc emuflow_session_child {stage} {
  global emuflow_child_command emuflow_session_manifest emuflow_session_out
  set log "$emuflow_session_out/$stage.log"
  if {[catch {exec {*}$emuflow_child_command $stage $emuflow_session_manifest >$log 2>@1} message]} {
    error "source-bound native-session stage $stage failed; see $log: $message"
  }
}
proc emuflow_session_source {script arguments} {
  set saved_argv $::argv
  set saved_argc $::argc
  set ::argv $arguments
  set ::argc [llength $arguments]
  try {uplevel #0 [list source $script]} finally {
    set ::argv $saved_argv
    set ::argc $saved_argc
  }
}
proc emuflow_session_frontiers {directory} {
  global emuflow_session_out emuflow_preflight_stage
  emuflow_session_child prepare-frontiers
  set ::emuflow_native_export_stage $emuflow_preflight_stage
  emuflow_session_source "$directory/inspect_constant_frontiers.tcl" \
    [list @current-design "$emuflow_session_out/constant-frontier-query.tsv" "$emuflow_session_out/constant-frontiers.tsv"]
  emuflow_session_child confirm-frontiers
  unset ::emuflow_native_export_stage
}
proc emuflow_session_timings {directory prefix fixed_io_map} {
  global emuflow_session_out emuflow_timing_query_mode
  if {$emuflow_timing_query_mode ni {datapath-only clock-aware-reference}} {
    error "unsupported native timing query mode"
  }
  emuflow_session_child prepare-timing
  set ::emuflow_native_export_stage routed
  set stream [open "$emuflow_session_out/timing-exports.tsv" r]
  if {[gets $stream header] < 0 || $header ne "kind\tquery_hex\toutput_hex"} {error "invalid native timing export plan"}
  set seen [dict create]
  while {[gets $stream row] >= 0} {
    set fields [split $row "\t"]
    if {[llength $fields] != 3} {error "invalid native timing export record"}
    lassign $fields kind query_hex output_hex
    if {$kind ni {boundary_timing logic_segment_timing local_path_timing} || [dict exists $seen $kind]} {
      error "invalid or duplicated native timing export class"
    }
    dict set seen $kind 1
    set query [encoding convertfrom utf-8 [binary decode hex $query_hex]]
    set output [encoding convertfrom utf-8 [binary decode hex $output_hex]]
    set script [expr {$kind eq "boundary_timing" ? "export_boundary_timing.tcl" : "export_logic_segment_timing.tcl"}]
    set arguments [list @current-design $query $output $prefix]
    if {$fixed_io_map ne "" || $emuflow_timing_query_mode eq "clock-aware-reference"} {lappend arguments $fixed_io_map}
    if {$emuflow_timing_query_mode eq "clock-aware-reference"} {lappend arguments $emuflow_timing_query_mode}
    emuflow_session_source "$directory/$script" $arguments
    file delete $query
  }
  close $stream
  unset ::emuflow_native_export_stage
}
proc emuflow_validate_clock_contract {path {allow_generated false}} {
  set stream [open $path r]
  if {[gets $stream header] < 0 || $header ne "clock_hex\tport_hex\tperiod_ns"} {error "invalid native clock contract"}
  set seen [dict create]
  while {[gets $stream row] >= 0} {
    lassign [split $row "\t"] clock_hex port_hex period
    set name [encoding convertfrom utf-8 [binary format H* $clock_hex]]
    set port [encoding convertfrom utf-8 [binary format H* $port_hex]]
    set clock [get_clocks -quiet [list $name]]
    set object [get_ports -quiet [list $port]]
    if {[dict exists $seen $name] || [llength $clock] != 1 || [llength $object] != 1 ||
        [llength [get_clocks -quiet -of_objects $object]] != 1 ||
        [get_property NAME [get_clocks -quiet -of_objects $object]] ne $name ||
        abs([get_property PERIOD $clock]-$period)>0.000001} {
      error "native clock identity differs from Phase6: $name"
    }
    dict set seen $name 1
  }
  close $stream
  foreach clock [get_clocks -quiet] {
    set name [get_property NAME $clock]
    if {[dict exists $seen $name]} {continue}
    if {!$allow_generated} {error "native design has unexpected clocks"}
    set chain [dict create]
    while {![dict exists $seen [get_property NAME $clock]]} {
      set name [get_property NAME $clock]
      if {[dict exists $chain $name] || ![get_property IS_GENERATED $clock] || [get_property PERIOD $clock] <= 0} {
        error "native word clock is not derived from a declared root: $name"
      }
      dict set chain $name 1
      set master [get_property MASTER_CLOCK $clock]
      set clock [get_clocks -quiet [list $master]]
      if {[llength $clock] != 1} {error "native word clock master is absent or ambiguous: $master"}
    }
  }
}
