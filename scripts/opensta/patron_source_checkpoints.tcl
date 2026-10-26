# Full-source planning checks. OpenSTA owns the timing graph and searches;
# this adapter serializes bounded critical-path witnesses, not a path library.
source [file join [file dirname [info script]] design_context.tcl]
source [file join [file dirname [info script]] communication_delays.tcl]

proc emuflow_checkpoint_witnesses {output label metric integer_metric} {
  global emuflow_checkpoint_clocks
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
    set names {}
    set start [get_property [get_property $path startpoint] full_name]
    set end [get_property [get_property $path endpoint] full_name]
    set in_data 0
    foreach point $points {
      set pin [get_property [get_property $point pin] full_name]
      if {$pin eq $start} {set in_data 1}
      if {$in_data} {lappend names [emuflow_hex_encode $pin]}
    }
    if {!$in_data || [lindex $names end] ne [emuflow_hex_encode $end]} {
      error "source checkpoint witness endpoints differ from its data chain"
    }
    set score [emuflow_path_communication_delay $points]
    if {$integer_metric} {
      set score [expr {$score / 1.0e9}]
      if {$score != round($score)} {error "source checkpoint metric is not an exact integer"}
      set score [expr {round($score)}]
      set maximum [expr {max($maximum,$score)}]
    }
    puts $output "$label\t$metric\t[emuflow_hex_encode $clock_name]\t$score\t[emuflow_hex_encode $start]\t[emuflow_hex_encode $end]\t[join $names ,]"
    incr count
  }
  if {!$count} {error "source checkpoint has no timed witnesses"}
  return [list $maximum $count]
}

if {[catch {
  set emuflow_checkpoint_clocks [emuflow_read_design_context]
  emuflow_require_source_timing_coverage
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
    incr count
  }
  close $input; close $output; close $witnesses
  if {!$count || $count > 64} {error "source checkpoint request population is invalid"}
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_PATRON_SOURCE_CHECKPOINTS status=pass contexts=1 checkpoints=$count"
exit 0
