# Disposable post-route observation. No original DUT pin or hierarchy is a
# timing endpoint; canonical RX state tags and physical TX pads own the cuts.
source [file join [file dirname [info script]] native_word_captures.tcl]
source [file join [file dirname [info script]] native_word_tx_batch.tcl]

proc emuflow_observe_native_word {checkpoint roles boundaries output threads} {
  if {![string is integer -strict $threads] || $threads<1} {error "invalid native observation thread count"}
  file mkdir $output
  set_param general.maxThreads $threads
  open_checkpoint $checkpoint
  set ::emuflow_native_export_stage routed
  set incomplete [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
  if {[llength $incomplete]} {error "native observation requires complete routed nets"}
  set context [emuflow_word_phase_bind [file join $roles counter-roles.tsv]]
  set sources [emuflow_native_word_sources [file join $roles rx-roles.tsv]]
  set mapping [emuflow_load_fixed_io_map $boundaries]
  set targets [dict create]
  set seen [dict create]
  set input [open [file join $roles targets.tsv] r]
  try {
    if {[gets $input header]<0 || $header ne "phase_word\tquery_hex"} {error "invalid native TX context header"}
    while {[gets $input row]>=0} {
      if {[llength [split $row \t]]!=2} {error "invalid native TX context record"}
      lassign [split $row \t] phase encoded
      if {![string is integer -strict $phase] || $phase<0 || $phase>=[dict get $context repeat_words] ||
          ![regexp {^([0-9a-f][0-9a-f])+$} $encoded]} {error "invalid native TX context coordinate"}
      set logical [encoding convertfrom utf-8 [binary decode hex $encoded]]
      set key [list $phase $logical]
      if {[dict exists $seen $key] || ![dict exists $mapping $logical] ||
          [lindex [dict get $mapping $logical] 0] ne "word-tx"} {error "native TX context lacks a unique canonical owner"}
      dict set seen $key 1
      dict lappend targets $phase $logical
    }
  } finally {close $input}
  set stream [open [file join $output missing-rx-roles.tsv] w]
  puts $stream "source_origin"
  foreach origin [dict get $sources missing_rx_origins] {puts $stream $origin}
  close $stream

  # Inspect the actual implementation constraints before applying the setup
  # measurement reference or any conditional serializer-counter values.
  set stream [open [file join $output native-dut-hold.tsv] w]
  emuflow_native_word_state_hold $sources $stream
  close $stream
  set stream [open [file join $output native-rx-infrastructure-hold.tsv] w]
  emuflow_native_word_rx_infrastructure_hold $sources $stream
  close $stream
  set rx_names [dict create]
  foreach pin [dict get $sources rx_clock_pins] {dict set rx_names [get_property NAME $pin] 1}
  set controls [dict create]
  dict for {owner pins} [dict get $sources infrastructure_clock_pins] {
    foreach pin $pins {
      if {![dict exists $rx_names [get_property NAME $pin]]} {dict set controls [get_property NAME $pin] $pin}
    }
  }
  set stream [open [file join $output native-control-checks.tsv] w]
  puts $stream "delay_type\tmeasurement\tslack_ns\tdata_ns\tstart_hex\tend_hex"
  foreach type {max min} {
    set paths [list]
    if {[dict size $controls] && [llength [dict get $sources infrastructure_clocks]]} {
      set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type $type \
          -from [dict values $controls] -to [dict get $sources infrastructure_clocks]]
    }
    if {[llength $paths]} {
      puts $stream "$type\tnative-path\t[get_property SLACK $paths]\t[get_property DATAPATH_DELAY $paths]\t[binary encode hex [get_property STARTPOINT_PIN $paths]]\t[binary encode hex [get_property ENDPOINT_PIN $paths]]"
    } else {puts $stream "$type\tnative-no-timed-path\t\t\t\t"}
  }
  close $stream

  emuflow_native_word_reference $sources
  set captures [open [file join $output native-capture-costs.tsv] w]
  emuflow_native_word_cost_header $captures
  set progress [open [file join $output capture-progress.tsv] w]
  puts $progress "target_hex\tqueries_complete"
  # Clocked primary inputs/outputs are included in their native clock family.
  # Unclocked external starts remain explicit context-required TX records.
  set capture_count [emuflow_native_word_capture_costs $sources $captures [dict create] $progress]
  close $captures
  close $progress

  set stream [open [file join $output native-costs.tsv] w]
  emuflow_native_word_cost_header $stream
  set summary [open [file join $output fanin-summary.tsv] w]
  puts $summary "phase_word\tquery_hex\tnative_startpoints\trx_sources\tdut_cells\tinfrastructure_cells\tports\tunclassified"
  set progress [open [file join $output progress.tsv] w]
  puts $progress "phase_word\tqueries_complete\telapsed_seconds"
  set began [clock seconds]
  set count 0
  foreach phase [lsort -integer [dict keys $targets]] {
    emuflow_word_phase_select $context $phase
    set phase_targets [list]
    foreach logical [dict get $targets $phase] {
      lappend phase_targets [list $logical [emuflow_fixed_io_object $mapping $logical]]
    }
    for {set begin 0} {$begin<[llength $phase_targets]} {incr begin 256} {
      set observed [emuflow_native_word_tx_batch $sources $stream $summary $phase \
          [lrange $phase_targets $begin [expr {$begin+255}]] 256]
      incr count [dict get $observed contexts]
      puts $progress "$phase\t$count\t[expr {[clock seconds]-$began}]"
      flush $progress
    }
    flush $summary
    puts $progress "$phase\t$count\t[expr {[clock seconds]-$began}]"
    flush $progress
  }
  close $stream
  close $summary
  close $progress
  emuflow_word_phase_discard $context
  set stream [open [file join $output native-observation-metrics.tsv] w]
  puts $stream "metric\tvalue"
  foreach {key value} [list dut_clocks [llength [dict get $sources dut_clocks]] \
      infrastructure_clocks [llength [dict get $sources infrastructure_clocks]] \
      observed_rx_origins [dict size [dict get $sources rx]] \
      requested_rx_origins [dict get $sources requested_rx_origins] \
      missing_rx_origins [llength [dict get $sources missing_rx_origins]] \
      repeat_words [dict get $context repeat_words] capture_queries $capture_count tx_contexts $count] {
    puts $stream "$key\t$value"
  }
  close $stream
  puts "EMUFLOW_NATIVE_WORD_OBSERVATION status=complete captures=$capture_count tx_contexts=$count"
}
