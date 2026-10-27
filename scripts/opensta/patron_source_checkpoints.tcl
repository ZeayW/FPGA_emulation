# Full-source planning checks. OpenSTA owns the timing graph and searches;
# this adapter serializes bounded critical-path witnesses, not a path library.
source [file join [file dirname [info script]] design_context.tcl]
source [file join [file dirname [info script]] communication_delays.tcl]
source [file join [file dirname [info script]] timing_path_rows.tcl]

proc emuflow_checkpoint_data_chain {path} {
  set start [get_property [get_property $path startpoint] full_name]
  set end [get_property [get_property $path endpoint] full_name]
  set names {}
  set in_data 0
  foreach point [get_property $path points] {
    set pin [get_property [get_property $point pin] full_name]
    if {$pin eq $start} {set in_data 1}
    if {$in_data} {lappend names [emuflow_hex_encode $pin]}
  }
  if {!$in_data || [lindex $names end] ne [emuflow_hex_encode $end]} {
    error "source checkpoint witness endpoints differ from its data chain"
  }
  return [list $start $end $names]
}

proc emuflow_checkpoint_edge_identity {path} {
  set source [$path source_clk_edge]
  set target [$path target_clk_edge]
  if {$source eq "NULL" || $target eq "NULL"} {error "source witness has no clock edge identity"}
  return [list [get_property [$source clock] name] [$source time] \
    [get_property [$target clock] name] [$target time] [$path is_output_delay]]
}

proc emuflow_checkpoint_witnesses {output label metric integer_metric} {
  global emuflow_checkpoint_clocks emuflow_checkpoint_pending
  set maximum 0
  set count 0
  # Serialize immediately: a later engine query invalidates PathEnd handles.
  foreach path [find_timing_paths -path_delay max -group_path_count 1 -endpoint_path_count 1] {
    set clock [get_property $path endpoint_clock]
    if {$clock eq "NULL"} {error "source checkpoint witness has no capture clock"}
    set clock_name [get_property $clock name]
    if {[lsearch -exact $emuflow_checkpoint_clocks $clock_name] < 0} {
      error "source checkpoint witness has an undeclared capture clock"
    }
    set points [get_property $path points]
    if {![llength $points]} {error "source checkpoint witness has no points"}
    lassign [emuflow_checkpoint_data_chain $path] start end names
    set score [emuflow_path_communication_delay $points]
    if {$integer_metric} {
      set score [expr {$score / 1.0e9}]
      if {$score != round($score)} {error "source checkpoint metric is not an exact integer"}
      set score [expr {round($score)}]
      set maximum [expr {max($maximum,$score)}]
    }
    puts $output "$label\t$metric\t[emuflow_hex_encode $clock_name]\t$score\t[emuflow_hex_encode $start]\t[emuflow_hex_encode $end]\t[join $names ,]"
    lappend emuflow_checkpoint_pending [list $label $metric $names [emuflow_checkpoint_edge_identity $path]]
    incr count
  }
  if {!$count} {error "source checkpoint has no timed witnesses"}
  return [list $maximum $count]
}

proc emuflow_checkpoint_export_feedback {feedback_var links_var emitted_var} {
  global emuflow_checkpoint_pending emuflow_communication_active
  upvar 1 $feedback_var feedback
  upvar 1 $links_var links
  upvar 1 $emitted_var emitted
  # Replay the exact structural witness after restoring nominal source costs.
  # Reusing zero-derate arrivals or subtracting huge integer metric weights
  # would invent local delay/slack and lose the original clock check margin.
  set emuflow_communication_active 0
  foreach record $emuflow_checkpoint_pending {
    lassign $record label metric names edges
    set pins {}
    foreach encoded $names {
      set name [emuflow_hex_decode $encoded]
      set pin [sta::find_pin $name]
      if {$pin eq "NULL" || [get_property $pin full_name] ne $name} {
        error "source feedback data pin is absent or ambiguous: $name"
      }
      lappend pins $pin
    }
    set query [list find_timing_paths -path_delay max -from [list [lindex $pins 0]] \
      -to [list [lindex $pins end]] -group_path_count 1 -endpoint_path_count 1]
    foreach pin [lrange $pins 1 end-1] {lappend query -through [list $pin]}
    set before $emitted
    foreach path [{*}$query] {
      if {[lindex [emuflow_checkpoint_data_chain $path] 2] ne $names ||
          [emuflow_checkpoint_edge_identity $path] ne $edges} {continue}
      puts $links "$label\t$metric\t$emitted"
      emuflow_emit_timing_paths [list $path] feedback emitted
    }
    if {$emitted != $before+1} {error "source feedback replay lost or changed its witness chain/clock edges"}
  }
}

if {[catch {
  set emuflow_checkpoint_clocks [emuflow_read_design_context]
  emuflow_require_source_timing_coverage
  set emuflow_feedback_emitted 0
  set feedback_active [expr {[info exists env(EMUFLOW_PATRON_FEEDBACK_OUTPUT)] && $env(EMUFLOW_PATRON_FEEDBACK_OUTPUT) ne ""}]
  if {$feedback_active} {
    emuflow_read_source_net_map [emuflow_required_env EMUFLOW_STA_NET_MAP]
    set feedback [open $env(EMUFLOW_PATRON_FEEDBACK_OUTPUT) w]
    puts $feedback [emuflow_path_export_header engine-edge-v1]
    set links [open [emuflow_required_env EMUFLOW_PATRON_FEEDBACK_LINKS] w]
    puts $links "checkpoint_hex\tmetric\tfeedback_path_ordinal"
  }
  # One integer unit is one STA second. Clock and I/O offsets must stay well
  # below one unit so slack ordering cannot reorder different integer costs.
  # This context creates only primary clocks and explicit max I/O delays;
  # it does not load arbitrary SDC exceptions or external delay writers.
  foreach name $emuflow_checkpoint_clocks {
    set period [get_property [get_clocks $name] period]
    if {$period <= 0 || $period > 1.0e6} {error "source checkpoint clock exceeds integer-metric offset range"}
  }
  set io [open [emuflow_required_env EMUFLOW_STA_TIMING_IO] r]
  gets $io header
  while {[gets $io line] >= 0} {
    if {$line ne "" && [lindex [split $line "\t"] 2] > 1.0e6} {
      error "source checkpoint I/O delay exceeds integer-metric offset range"
    }
  }
  close $io
  set input [open [emuflow_required_env EMUFLOW_PATRON_CHECKPOINTS] r]
  if {[gets $input header] < 0 || $header ne "checkpoint_hex\tprofile_path_hex"} {
    error "invalid source checkpoint request header"
  }
  set output [open [emuflow_required_env EMUFLOW_PATRON_METRICS] w]
  puts $output "checkpoint_hex\tmaximum_physical_hops\tmaximum_path_route_ratio_sum\tworst_slack_ns\ttotal_negative_slack_ns\twitnesses"
  set witnesses [open [emuflow_required_env EMUFLOW_PATRON_WITNESSES] w]
  puts $witnesses "checkpoint_hex\tmetric\tclock_hex\tcommunication_score\tstart_pin_hex\tend_pin_hex\tpins_hex"
  set count 0
  while {[gets $input line] >= 0} {
    if {$count >= 64} {error "source checkpoint request population is invalid"}
    set fields [split $line "\t"]
    if {[llength $fields] != 2} {error "malformed source checkpoint request"}
    lassign $fields label profile_hex
    set profile [emuflow_hex_decode $profile_hex]
    set number 0
    set emuflow_checkpoint_pending {}
    foreach sense {-early -late} {set_timing_derate $sense -cell_delay -cell_check 0}
    foreach {mode metric} {patron-source-hops physical-hops patron-source-ratio path-route-ratio-sum} {
      emuflow_apply_communication_delays $profile $mode
      lassign [emuflow_checkpoint_witnesses $witnesses $label $metric 1] score found
      if {$mode eq "patron-source-hops"} {set hops $score} else {set ratios $score}
      incr number $found
    }
    foreach sense {-early -late} {set_timing_derate $sense -cell_delay -cell_check 1}
    emuflow_apply_communication_delays $profile patron-source-delay-ns
    lassign [emuflow_checkpoint_witnesses $witnesses $label setup 0] ignored found
    incr number $found
    set worst [sta::worst_slack -max]
    set tns [sta::total_negative_slack -max]
    puts $output "$label\t$hops\t$ratios\t$worst\t$tns\t$number"
    if {$feedback_active} {
      emuflow_apply_communication_delays [emuflow_required_env EMUFLOW_PATRON_NOMINAL_PROFILE] patron-source-delay-ns
      emuflow_checkpoint_export_feedback feedback links emuflow_feedback_emitted
    }
    incr count
  }
  close $input; close $output; close $witnesses
  if {$feedback_active} {close $feedback; close $links}
  if {!$count || $count > 64} {error "source checkpoint request population is invalid"}
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_PATRON_SOURCE_CHECKPOINTS status=pass contexts=1 checkpoints=$count feedback_paths=$emuflow_feedback_emitted"
exit 0
