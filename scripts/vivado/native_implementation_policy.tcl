# One bounded implementation policy for every native-word partition.
# Candidate selection uses native timing and physical validity, never FPGA IDs
# or preservation of synthesized cell names/types.
proc emuflow_policy_trial {action name} {
  puts "EMUFLOW_NATIVE_TRIAL $action $name"
  flush stdout
}

proc emuflow_policy_messages {} {
  return [list [get_msg_config -severity ERROR -count] \
      [get_msg_config -severity {CRITICAL WARNING} -count] \
      [get_msg_config -id {Vivado 12-4439} -count]]
}

proc emuflow_policy_check_messages {before} {
  lassign $before errors critical advice
  lassign [emuflow_policy_messages] next_errors next_critical next_advice
  if {$next_errors>$errors || $next_critical-$critical>$next_advice-$advice} {
    error "native optional trial emitted an error or critical warning"
  }
}

proc emuflow_policy_setup {} {
  set path [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type max]
  if {[llength $path]!=1} {error "implementation policy has no native setup path"}
  return [get_property SLACK $path]
}

proc emuflow_policy_restore {checkpoint contract} {
  close_project
  open_checkpoint $checkpoint
  emuflow_validate_clock_contract $contract true
}

proc emuflow_policy_pending {} {
  return [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
}

proc emuflow_policy_measure {directory step} {
  report_drc -file [file join $directory ${step}-drc.rpt]
  report_timing_summary -file [file join $directory ${step}-timing.rpt]
  set input [open [file join $directory ${step}-timing.rpt] r]
  set armed false
  set tns ""
  while {[gets $input line]>=0} {
    if {[string match {*WNS(ns)*TNS(ns)*} $line]} {set armed true;continue}
    if {!$armed} {continue}
    set fields [regexp -all -inline {\S+} $line]
    if {[llength $fields]>=8 && [string is double -strict [lindex $fields 0]] &&
        [string is double -strict [lindex $fields 1]] && [string is integer -strict [lindex $fields 2]]} {
      set tns [lindex $fields 1]
      break
    }
  }
  close $input
  if {$tns eq ""} {error "implementation policy has no native aggregate TNS"}
  set blocking 0
  foreach violation [get_drc_violations -quiet] {
    if {[get_property SEVERITY $violation] in {Error {Critical Warning}}} {incr blocking}
  }
  set hold [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type min]
  set whs ""
  if {[llength $hold]} {set whs [get_property SLACK $hold]}
  return [dict create wns [emuflow_policy_setup] tns $tns whs $whs \
      incomplete [llength [emuflow_policy_pending]] blocking_drc $blocking]
}

proc emuflow_policy_score {metrics} {
  set physical [expr {[dict get $metrics incomplete]==0 && [dict get $metrics blocking_drc]==0}]
  set whs [dict get $metrics whs]
  set hold [expr {$whs eq "" || $whs>=0}]
  # The zero below is a ranking key for an absent hold path, not a delay.
  set hold_key [expr {$whs eq "" ? 0.0 : $whs}]
  return [list $physical $hold [dict get $metrics wns] [dict get $metrics tns] $hold_key]
}

proc emuflow_policy_not_worse {candidate best} {
  foreach a [emuflow_policy_score $candidate] b [emuflow_policy_score $best] {
    if {$a>$b} {return true}
    if {$a<$b} {return false}
  }
  return true
}

proc emuflow_policy_closed {metrics} {
  return [expr {[dict get $metrics incomplete]==0 && [dict get $metrics blocking_drc]==0 &&
      [dict get $metrics wns]>=0 && ([dict get $metrics whs] eq "" || [dict get $metrics whs]>=0)}]
}

proc emuflow_policy_row {stream step status metrics message} {
  puts $stream [join [list $step $status [dict get $metrics wns] [dict get $metrics tns] [dict get $metrics whs] \
      [dict get $metrics incomplete] [dict get $metrics blocking_drc] \
      [binary encode hex [encoding convertto utf-8 $message]]] \t]
  flush $stream
}

proc emuflow_policy_localize {placement origins prefix output contract directive} {
  set directory [file join $output implementation-policy]
  file mkdir $directory
  set baseline [emuflow_policy_setup]
  set checkpoint [file join $directory before-locality.dcp]
  write_checkpoint -force $checkpoint
  set stream [open [file join $directory locality.tsv] w]
  puts $stream "status\tmoved_cells\tbaseline_estimated_wns_ns\tcandidate_estimated_wns_ns\terror_hex"
  set moved 0
  set candidate ""
  set messages [emuflow_policy_messages]
  emuflow_policy_trial begin locality-placement
  set code [catch {
    route_design -unroute
    set sources [emuflow_native_word_source_roles $origins]
    set moved [emuflow_rebind_bank_local_replicas $placement $sources $prefix $directory]
    if {$moved} {place_design -directive $directive -no_bufg_opt}
    emuflow_validate_clock_contract $contract true
    set candidate [emuflow_policy_setup]
    # Native physical-equation diagnostics can appear only during DRC, even
    # when a timing query reports an improved estimate. Unrouted-net DRCs
    # are expected here; counted native errors/critical warnings reject the
    # optional placement before spending a full route on it.
    report_drc -file [file join $directory locality-drc.rpt]
    emuflow_policy_check_messages $messages
  } message]
  if {$code || $candidate<$baseline} {
    emuflow_policy_trial discard locality-placement
    emuflow_policy_restore $checkpoint $contract
    emuflow_policy_trial restore locality-placement
    set status [expr {$code ? "retained-after-native-error" : "retained-better-placement"}]
  } else {
    emuflow_policy_trial accept locality-placement
    set status [expr {$moved ? "accepted-local-placement" : "unchanged"}]
  }
  if {!$code} {set message ""}
  puts $stream [join [list $status $moved $baseline $candidate \
      [binary encode hex [encoding convertto utf-8 $message]]] \t]
  close $stream
  return [dict create accepted [expr {!$code && $candidate>=$baseline && $moved>0}] checkpoint $checkpoint]
}

proc emuflow_policy_route {output contract directive locality} {
  if {$locality eq "" || ![dict get $locality accepted]} {
    route_design -directive $directive
    return
  }
  set messages [emuflow_policy_messages]
  emuflow_policy_trial begin locality-route
  set code [catch {
    route_design -directive $directive
    emuflow_validate_clock_contract $contract true
    set metrics [emuflow_policy_measure [file join $output implementation-policy] locality-route]
    if {[dict get $metrics incomplete] || [dict get $metrics blocking_drc]} {
      error "native optional locality route is physically incomplete"
    }
    emuflow_policy_check_messages $messages
  } message]
  if {$code} {
    emuflow_policy_trial discard locality-route
    emuflow_policy_restore [dict get $locality checkpoint] $contract
    emuflow_policy_trial restore locality-route
    set stream [open [file join $output implementation-policy locality-route-error.txt] w]
    puts $stream $message
    close $stream
    # A failed optional placement gets one baseline route, with the original
    # constraints. Errors in that route remain fatal, without another retry.
    route_design -directive $directive
  } else {
    emuflow_policy_trial accept locality-route
  }
}

proc emuflow_policy_postroute {output contract {initial_checkpoint ""}} {
  set directory [file join $output implementation-policy]
  file mkdir $directory
  set stream [open [file join $directory candidates.tsv] w]
  puts $stream "step\tstatus\twns_ns\ttns_ns\twhs_ns\tincomplete_nets\tblocking_drc\terror_hex"
  set best [emuflow_policy_measure $directory baseline]
  set selected baseline
  set initial [file join $directory initial-routed.dcp]
  set checkpoint [file join $directory best-routed.dcp]
  if {$initial_checkpoint eq ""} {
    write_checkpoint -force $initial
  } else {
    file copy -force $initial_checkpoint $initial
  }
  file copy -force $initial $checkpoint
  emuflow_policy_row $stream baseline initial $best ""
  # Each command is supported in post-route mode. Keep critical-cell and SLR
  # operations separate; forced net replication belongs before routing.
  foreach step {explore-hold critical-cell slr-crossing} options {
      {-directive ExploreWithHoldFix}
      {-critical_cell_opt}
      {-slr_crossing_opt}} {
    if {[emuflow_policy_closed $best] || [dict get $best incomplete] || [dict get $best blocking_drc]} {break}
    set messages [emuflow_policy_messages]
    emuflow_policy_trial begin postroute-$step
    set code [catch {
      phys_opt_design {*}$options
      if {[llength [emuflow_policy_pending]]} {route_design -preserve}
      emuflow_validate_clock_contract $contract true
      set candidate [emuflow_policy_measure $directory $step]
      emuflow_policy_check_messages $messages
      set not_worse [emuflow_policy_not_worse $candidate $best]
      if {$not_worse} {
        set next [file join $directory next-routed.dcp]
        write_checkpoint -force $next
        emuflow_policy_check_messages $messages
      }
    } message]
    if {$code} {
      emuflow_policy_row $stream $step retained-after-native-error $best $message
      emuflow_policy_trial discard postroute-$step
      emuflow_policy_restore $checkpoint $contract
      emuflow_policy_trial restore postroute-$step
      continue
    }
    if {$not_worse} {
      # Replace the best snapshot only after a complete checkpoint write.
      file rename -force $next $checkpoint
      set best $candidate
      set selected $step
      emuflow_policy_row $stream $step accepted $candidate ""
      emuflow_policy_trial accept postroute-$step
    } else {
      emuflow_policy_row $stream $step rejected-worse $candidate ""
      emuflow_policy_trial discard postroute-$step
      emuflow_policy_restore $checkpoint $contract
      emuflow_policy_trial restore postroute-$step
    }
  }
  emuflow_policy_row $stream $selected selected $best ""
  close $stream
  set summary [open [file join $directory summary.tsv] w]
  puts $summary "metric\tvalue"
  puts $summary "policy\tbounded-native-word-physical-v1"
  puts $summary "selected_step\t$selected"
  puts $summary "timing_met\t[emuflow_policy_closed $best]"
  dict for {key value} $best {puts $summary "$key\t$value"}
  close $summary
  return $best
}
