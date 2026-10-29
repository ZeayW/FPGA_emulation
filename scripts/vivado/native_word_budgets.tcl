# Native endpoint timing in a disposable routed view. Budgets include upstream
# transport arrivals; the original physical clocks and clock trees are kept.
source [file join [file dirname [info script]] native_word_timing.tcl]

proc emuflow_native_budget_decode {encoded} {
  if {![regexp {^([0-9a-f][0-9a-f])+$} $encoded]} {error "invalid native budget identity"}
  return [encoding convertfrom utf-8 [binary decode hex $encoded]]
}

proc emuflow_native_word_apply_budgets {sources fpga path output} {
  emuflow_word_phase_readonly
  set input [open $path r]
  set rows [dict create]
  set ceiling 1000
  set clocks [dict create]
  foreach clock [dict get $sources dut_clocks] {dict set clocks [get_property NAME $clock] $clock}
  try {
    if {[gets $input header]<0 || $header ne "fpga_hex\tsource_origin\tcapture_clock_hex\tcapture_edge\tbudget_ps\tlaunch_clock_hex\tlaunch_edge\treference_window_ps\tarrival_ps"} {
      error "invalid native RX budget header"
    }
    while {[gets $input row]>=0} {
      set fields [split $row \t]
      if {[llength $fields]!=9} {error "invalid native RX budget row"}
      lassign $fields owner origin capture edge budget launch launch_edge window arrival
      if {[emuflow_native_budget_decode $owner] ne $fpga} {continue}
      set capture [emuflow_native_budget_decode $capture]
      if {![regexp {^s[0-9]+$} $origin] || $edge ni {1 2} || $launch_edge ni {1 2} ||
          ![string is entier -strict $budget] || ![string is entier -strict $window] ||
          ![string is entier -strict $arrival] || $window<=0 || $budget!=$window-$arrival ||
          ![dict exists $clocks $capture]} {error "native RX budget owner or units differ"}
      emuflow_native_budget_decode $launch
      set key [list $origin $capture $edge]
      if {[dict exists $rows $key]} {error "native RX budget repeats a capture relation"}
      dict set rows $key $budget
      set ceiling [expr {max($ceiling,$budget+1)}]
    }
  } finally {close $input}
  set missing [open [file join $output missing-budget-rx.tsv] w]
  puts $missing "source_origin\tcapture_clock_hex\tcapture_edge"
  set starts [dict create]
  set absent 0
  dict for {key budget} $rows {
    lassign $key origin capture edge
    if {![dict exists $sources rx $origin]} {
      puts $missing "$origin\t[binary encode hex $capture]\t$edge"
      incr absent
      continue
    }
    foreach pin [dict get $sources rx $origin] {dict set starts [get_property NAME $pin] $pin}
  }
  close $missing
  # A broad low default can clamp a positive edge-specific budget. The reset
  # baseline must exceed every bound before applying the signed constraints.
  if {[dict size $starts] && [dict size $clocks]} {
    set_max_delay -reset_path -from [dict values $starts] -to [dict values $clocks] [expr {$ceiling/1000.0}]
  }
  set applied 0
  dict for {key budget} $rows {
    lassign $key origin capture edge
    if {![dict exists $sources rx $origin]} {continue}
    set option [expr {$edge==1 ? "-rise_to" : "-fall_to"}]
    set_max_delay -from [dict get $sources rx $origin] $option [list [dict get $clocks $capture]] [expr {$budget/1000.0}]
    incr applied
  }
  return [dict create starts [dict values $starts] applied $applied missing $absent reset_ceiling_ps $ceiling]
}

proc emuflow_native_word_endpoint_slacks {sources budget_view output} {
  emuflow_word_phase_readonly
  set clocks [dict get $sources dut_clocks]
  set cells [dict keys [dict get $sources dut_cells]]
  set targets [dict create]
  if {[llength $cells]} {
    foreach pin [get_pins -quiet -of_objects [get_cells -quiet $cells] -filter {DIRECTION == IN && IS_CLOCK == 0}] {
      dict set targets [get_property NAME $pin] 1
    }
  }
  # Explicitly clocked original outputs are capture endpoints as well. All
  # outputs give a safe query ceiling; the clock selector excludes PHY ports.
  foreach port [all_outputs] {dict set targets [get_property NAME $port] 1}
  set starts [concat $clocks [dict get $budget_view starts]]
  set upper [dict size $targets]
  set rows [dict create]
  if {$upper && [llength $starts] && [llength $clocks]} {
    foreach path [get_timing_paths -quiet -from $starts -to $clocks -max_paths $upper -nworst 1 -delay_type max -sort_by group] {
      set endpoint [get_property ENDPOINT_PIN $path]
      set capture [get_property ENDPOINT_CLOCK $path]
      if {![dict exists $targets $endpoint]} {error "native timing endpoint is outside the target census"}
      set key [list $endpoint $capture]
      set slack [get_property SLACK $path]
      # One physical endpoint can belong to more than one native path group.
      # Retain its worst check for each capture clock before summing TNS.
      if {![dict exists $rows $key] || $slack<[lindex [dict get $rows $key] 0]} {
        dict set rows $key [list $slack [get_property DATAPATH_DELAY $path] \
            [get_property STARTPOINT_PIN $path] [get_property STARTPOINT_CLOCK $path] \
            [get_property REQUIREMENT $path] [get_property STARTPOINT_CLOCK_EDGE $path] [get_property ENDPOINT_CLOCK_EDGE $path]]
      }
    }
  }
  set stream [open [file join $output native-endpoint-slacks.tsv] w]
  puts $stream "endpoint_hex\tcapture_clock_hex\tslack_ns\tdata_on_worst_slack_path_ns\tstart_hex\tlaunch_clock_hex\trequirement_ns\tlaunch_edge_ns\tcapture_edge_ns"
  dict for {key row} $rows {
    lassign $key endpoint capture
    lassign $row slack data start launch requirement launch_edge capture_edge
    puts $stream [join [list [binary encode hex $endpoint] [binary encode hex $capture] $slack $data \
        [binary encode hex $start] [binary encode hex $launch] $requirement $launch_edge $capture_edge] \t]
  }
  close $stream
  set stream [open [file join $output native-endpoint-metrics.tsv] w]
  puts $stream "metric\tvalue"
  foreach {metric value} [list target_pin_upper_bound $upper endpoint_checks [dict size $rows] \
      capture_clocks [llength $clocks] \
      applied_rx_budgets [dict get $budget_view applied] missing_native_rx_budgets [dict get $budget_view missing] \
      reset_ceiling_ps [dict get $budget_view reset_ceiling_ps]] {puts $stream "$metric\t$value"}
  close $stream
  return [dict size $rows]
}
