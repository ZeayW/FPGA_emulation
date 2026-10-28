# Native clock/interface timing views. Architectural FF names and primitive
# types are not query endpoints; only the source-bound transport roles need
# an association with the actual routed RX registers. The caller owns the
# immutable checkpoint, role catalog and complete query/coverage ledger.
source [file join [file dirname [info script]] word_phase_context.tcl]
source [file join [file dirname [info script]] timing_contract.tcl]

proc emuflow_native_word_sources {rx_roles} {
  emuflow_word_phase_readonly
  if {[file size $rx_roles]>16777216} {error "native RX role catalog exceeds its bounded transport scope"}
  set requested [dict create]
  set input [open $rx_roles r]
  try {
    if {[gets $input header]<0 || $header ne "source_origin"} {error "invalid native RX role header"}
    while {[gets $input origin]>=0} {
      if {![regexp {^s[0-9]+$} $origin] || [dict exists $requested $origin]} {error "invalid or repeated native RX source role"}
      dict set requested $origin 1
    }
  } finally {close $input}
  set clocks [get_clocks -quiet]
  set dut [list]
  set infrastructure [list]
  set dut_cells [dict create]
  set infrastructure_cells [dict create]
  foreach clock $clocks {
    set root $clock
    set seen [dict create]
    while {[get_property IS_GENERATED $root]} {
      set name [get_property NAME $root]
      if {[dict exists $seen $name]} {error "native clock ancestry contains a cycle"}
      dict set seen $name 1
      set root [get_clocks -quiet [list [get_property MASTER_CLOCK $root]]]
      if {[llength $root]!=1} {error "native clock master is absent or ambiguous"}
    }
    set registers [all_registers -clock [get_property NAME $clock] -clock_pins]
    set cells [list]
    if {[llength $registers]} {set cells [get_cells -quiet -of_objects $registers]}
    if {[regexp {^emuflow_dut_clk_[0-9]+$} [get_property NAME $root]]} {
      lappend dut $clock
      if {[llength $cells]} {
        foreach name [get_property NAME $cells] {dict set dut_cells $name 1}
      }
    } else {
      lappend infrastructure $clock
      if {[llength $cells]} {
        foreach name [get_property NAME $cells] {dict set infrastructure_cells $name 1}
      }
    }
  }
  if {![llength $dut]} {error "native word timing has no declared DUT clock"}
  set rx [dict create]
  set rx_pins [dict create]
  set infrastructure_pins [dict create]
  set examined [dict create]
  foreach clock $infrastructure {
    foreach pin [all_registers -clock [get_property NAME $clock] -clock_pins] {
      set name [get_property NAME $pin]
      if {[dict exists $examined $name]} {continue}
      dict set examined $name 1
      set cell [get_cells -quiet -of_objects $pin]
      if {[llength $cell]!=1} {error "native clock pin has no unique sequential owner"}
      dict lappend infrastructure_pins [get_property NAME $cell] $pin
      set origin [get_property -quiet EMUFLOW_SOURCE $cell]
      if {![dict exists $requested $origin]} {continue}
      set actual [get_clocks -quiet -of_objects $pin]
      if {[llength $actual]!=1 || [get_property NAME $actual] ne "emuflow_tdm_clk" ||
          abs([get_property PERIOD $actual]-5.0)>0.000001} {
        error "native RX source requires an explicit word-clock projection: $origin"
      }
      dict lappend rx $origin $pin
      dict set rx_pins $name $pin
    }
  }
  set missing [list]
  foreach origin [dict keys $requested] {
    if {![dict exists $rx $origin]} {lappend missing $origin}
  }
  # Missing roles are facts for the coverage/optimization ledger, never
  # constants, zero-delay arcs or an automatic elimination certificate.
  return [dict create dut_clocks $dut infrastructure_clocks $infrastructure clocks $clocks \
      dut_cells $dut_cells infrastructure_cells $infrastructure_cells rx $rx \
      infrastructure_clock_pins $infrastructure_pins \
      rx_clock_pins [dict values $rx_pins] missing_rx_origins [lsort $missing] \
      requested_rx_origins [dict size $requested]]
}

proc emuflow_native_word_reference {sources} {
  emuflow_word_phase_readonly
  emuflow_use_clock_aware_reference_constraints
  # Override the same specific pin/clock exceptions used by the implementation
  # data budgets. A broad clock reference alone need not override those paths.
  foreach {from to} [list [dict get $sources rx_clock_pins] [dict get $sources clocks] \
      [dict get $sources dut_clocks] [dict get $sources infrastructure_clocks]] {
    if {[llength $from] && [llength $to]} {set_max_delay -reset_path -from $from -to $to 1.0}
  }
}

proc emuflow_native_word_fanin {sources target} {
  emuflow_word_phase_readonly
  set rx [dict create]
  set dut [dict create]
  set infrastructure [dict create]
  set ports [dict create]
  set unclassified [dict create]
  set starts [all_fanin -flat -startpoints_only -trace_arcs enabled $target]
  foreach start $starts {
    set name [get_property NAME $start]
    if {[get_property CLASS $start] eq "port"} {dict set ports $name $start;continue}
    set cell [get_cells -quiet -of_objects $start]
    if {[llength $cell]!=1} {dict set unclassified $name $start;continue}
    set origin [get_property -quiet EMUFLOW_SOURCE $cell]
    set owner [get_property NAME $cell]
    if {[dict exists $sources rx $origin]} {
      dict set rx $origin [dict get $sources rx $origin]
    } elseif {[dict exists $sources dut_cells $owner]} {
      dict set dut $owner $cell
    } elseif {[dict exists $sources infrastructure_cells $owner]} {
      dict set infrastructure $owner $cell
    } else {dict set unclassified $name $start}
  }
  # The caller must account for original input environments, startup/control
  # sources and every unclassified start before claiming complete coverage.
  return [dict create rx $rx dut_cells $dut infrastructure_cells $infrastructure \
      ports $ports unclassified $unclassified native_startpoints [llength $starts]]
}

proc emuflow_native_word_cost_header {stream} {
  puts $stream "phase_word\tquery_hex\tsource_kind\tsource_hex\tlaunch_edge\tmeasurement\trequirement_ns\tdata_ns\tsampling_cost_ns\tclock_adjustment_ns\tstart_hex\tend_hex\tstart_clock_hex\tend_clock_hex\tlaunch_ns\tcapture_ns"
}

proc emuflow_native_word_cost_row {stream phase query kind source edge paths} {
  emuflow_word_phase_readonly
  if {![string is entier -strict $phase] || $phase < -1} {error "invalid native word phase coordinate"}
  if {[llength $paths]>1} {error "native cost query returned multiple worst-path witnesses"}
  set prefix "$phase\t[binary encode hex [encoding convertto utf-8 $query]]\t$kind\t[binary encode hex [encoding convertto utf-8 $source]]\t$edge"
  if {![llength $paths]} {
    puts $stream "$prefix\tnative-no-timed-path\t[join [lrepeat 10 ""] \t]"
    return
  }
  lassign [emuflow_reference_path_cost $paths] cost adjustment
  set row [list [get_property REQUIREMENT $paths] [get_property DATAPATH_DELAY $paths] $cost $adjustment]
  foreach property {STARTPOINT_PIN ENDPOINT_PIN STARTPOINT_CLOCK ENDPOINT_CLOCK} {
    lappend row [binary encode hex [encoding convertto utf-8 [get_property $property $paths]]]
  }
  foreach property {STARTPOINT_CLOCK_EDGE ENDPOINT_CLOCK_EDGE} {lappend row [get_property $property $paths]}
  puts $stream "$prefix\tnative-clock-aware-reference\t[join $row \t]"
}

proc emuflow_native_word_tx_costs {sources stream phase query target} {
  emuflow_word_phase_readonly
  set fanin [emuflow_native_word_fanin $sources $target]
  # Whole native clock families include alternative local paths and replaced
  # FF/DSP/BRAM timing checks without demanding old source endpoints survive.
  foreach clock [dict get $sources dut_clocks] {
    foreach edge {rise fall} {
      set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -${edge}_from [list $clock] -to $target]
      emuflow_native_word_cost_row $stream $phase $query dut-clock [get_property NAME $clock] $edge $paths
    }
  }
  dict for {origin pins} [dict get $fanin rx] {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -from $pins -to $target]
    emuflow_native_word_cost_row $stream $phase $query rx $origin "" $paths
  }
  dict for {name cell} [dict get $fanin infrastructure_cells] {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
        -from [dict get $sources infrastructure_clock_pins $name] -to $target]
    # This is a native witness, not an architectural DUT launch event. Its
    # startup/control or transport role must be bound before global assembly.
    emuflow_native_word_cost_row $stream $phase $query infrastructure $name "" $paths
  }
  foreach kind {ports unclassified} {
    dict for {name object} [dict get $fanin $kind] {
      # Preserve every remaining start, including unconstrained service I/O.
      # Do not invent an input arrival, data cost or optimization disposition.
      puts $stream "$phase\t[binary encode hex [encoding convertto utf-8 $query]]\t$kind\t[binary encode hex [encoding convertto utf-8 $name]]\t\tnative-source-context-required\t[join [lrepeat 10 ""] \t]"
    }
  }
  flush $stream
  return $fanin
}
