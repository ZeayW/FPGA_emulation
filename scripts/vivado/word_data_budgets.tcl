# Local implementation budgets for DUT data carried by the periodic word PHY.
# Native TDM/PHY control launches retain their original clock requirements.
proc emuflow_word_data_budgets {origins {apply true} {target_period ""}} {
  set input [open $origins r]
  if {[gets $input header] < 0 || $header ne "source_origin"} {error "invalid word data source header"}
  set sources [dict create]
  while {[gets $input origin] >= 0} {
    if {![regexp {^s[0-9]+$} $origin] || [dict exists $sources $origin]} {error "invalid word data source origin"}
    dict set sources $origin 1
  }
  close $input
  set dut [list]
  set infrastructure [list]
  set clocks [get_clocks -quiet]
  foreach clock $clocks {
    set root $clock
    while {[get_property IS_GENERATED $root]} {set root [get_clocks -quiet [list [get_property MASTER_CLOCK $root]]]}
    if {[string match emuflow_dut_clk* [get_property NAME $root]]} {
      lappend dut $clock
    } else {lappend infrastructure $clock}
  }
  set periods [list]
  foreach clock $dut {
    set period [get_property PERIOD $clock]
    if {![string is double -strict $period] || $period <= 0 || $period == Inf} {error "invalid DUT data budget"}
    lappend periods $period
  }
  # A transit partition can have no local DUT clock. Its RX -> TX logic
  # still uses the explicitly shared DUT implementation target.
  if {$target_period ne ""} {
    if {![string is double -strict $target_period] || !($target_period > 0 && $target_period < Inf)} {
      error "invalid shared DUT data budget"
    }
    set budget $target_period
  } else {
    if {![llength $periods]} {error "word data budget requires a shared target or a local DUT clock"}
    set budget [lindex [lsort -real $periods] 0]
  }
  set rx [dict create]
  foreach clock $infrastructure {
    foreach pin [all_registers -clock [get_property NAME $clock] -clock_pins] {
      set cells [get_cells -quiet -of_objects $pin]
      if {[llength $cells] != 1} {error "word data clock pin has no unique native owner"}
      if {[dict exists $sources [get_property -quiet EMUFLOW_SOURCE $cells]]} {
        dict set rx [get_property NAME $pin] $pin
      }
    }
  }
  # Keep exact pin exceptions available to the diagnostic snapshot. A broad
  # clock-pair reference alone need not override more specific exceptions.
  set ::emuflow_word_data_budget_groups [list [dict values $rx] $clocks $dut $infrastructure]
  if {$apply} {
    foreach {from to} $::emuflow_word_data_budget_groups {
      if {[llength $from] && [llength $to]} {set_max_delay $budget -from $from -to $to}
    }
  }
  return [list $budget [dict size $rx]]
}

proc emuflow_word_reference_data_budgets {} {
  if {![info exists ::emuflow_word_data_budget_groups]} {return}
  foreach {from to} $::emuflow_word_data_budget_groups {
    if {[llength $from] && [llength $to]} {set_max_delay -reset_path -from $from -to $to 1.0}
  }
}
