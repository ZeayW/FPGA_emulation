# Native word-bank setup; generated vendor HDL remains unmodified.
source [file join [file dirname [info script]] selectio_ip.tcl]
proc emuflow_word_prepare {directory} {
  set stream [open [file join $directory native-word-ips.tsv] r]
  if {[gets $stream header] < 0 || $header ne "module\tconfiguration"} {error "invalid native word IP table"}
  set seen [dict create]
  while {[gets $stream row] >= 0} {
    set fields [split $row "\t"]
    if {[llength $fields] != 2} {error "invalid native word IP row"}
    lassign $fields module configuration
    if {[dict exists $seen $module] || [dict size $seen] >= 37 ||
        ![regexp {^emuflow_selectio_bank[0-9]+_(tx|rx)$} $module] ||
        ![regexp {^bank[0-9]+_(tx|rx)\.tsv$} $configuration]} {error "invalid native word module/configuration"}
    dict set seen $module 1
    set generated [file join $directory native-word-ip]
    lassign [emuflow_create_selectio_ip $module $generated [file join $directory $configuration]] ip definition
    if {$definition ne "xilinx.com:ip:high_speed_selectio_wiz:3.6"} {error "unsupported native word IP definition"}
    set xci [get_files -all -quiet [file join $generated $module $module.xci]]
    if {[llength $xci] != 1} {error "native word IP file is absent"}
    # Integrate native sources in this top's synthesis, without a separate
    # black-box checkpoint or IP-level OOC placeholder clock definitions.
    set_property GENERATE_SYNTH_CHECKPOINT false $xci
    generate_target all $ip
    foreach file [get_files -all -of_objects $ip] {
      if {[string match *_ooc.xdc [file tail [get_property NAME $file]]]} {set_property USED_IN {} $file}
    }
  }
  close $stream
  return [dict size $seen]
}

proc emuflow_word_control_sources {clocks scope_path} {
  set input [open $scope_path r]
  if {[gets $input header] < 0 || $header ne "status\trx_queries\tunresolved_rx_queries\tnative_cells"} {
    error "native word control timing lacks its routed RX scope"
  }
  if {[gets $input row] < 0 || [llength [split $row "\t"]] != 4} {error "invalid routed RX scope"}
  lassign [split $row "\t"] status requested unresolved count
  foreach value [list $requested $unresolved $count] {
    if {![string is integer -strict $value] || $value < 0} {error "invalid routed RX scope counts"}
  }
  if {$unresolved > $requested || $status ni {complete incomplete} ||
      ($status eq "complete") != ($unresolved == 0)} {error "inconsistent routed RX scope"}
  if {[gets $input header] < 0 || $header ne "cell_hex"} {error "invalid routed RX cell header"}
  set rx [dict create]
  while {[gets $input encoded] >= 0} {
    set name [encoding convertfrom utf-8 [binary decode hex $encoded]]
    if {$name eq "" || [dict exists $rx $name]} {error "invalid routed RX cell identity"}
    dict set rx $name 1
  }
  close $input
  if {[dict size $rx] != $count} {error "routed RX cell coverage differs"}
  set launches [dict create]
  set excluded [dict create]
  foreach clock $clocks {
    foreach pin [all_registers -clock [get_property NAME $clock] -clock_pins] {
      set cells [get_cells -quiet -of_objects $pin]
      if {[llength $cells] != 1} {error "control clock pin has no unique native owner"}
      set name [get_property NAME $pin]
      if {[dict exists $rx [get_property NAME $cells]]} {
        dict set excluded $name 1
      } else {
        dict set launches $name $pin
      }
    }
  }
  return [list [dict values $launches] [dict size $excluded] [expr {$status eq "complete"}]]
}

proc emuflow_word_routed_checks {directory rx_scope} {
  set stream [open [file join $directory native-word-clock-census.tsv] w]
  puts $stream "clock_hex\tperiod_ns\tgenerated\tmaster_hex"
  set infrastructure [list]
  foreach clock [get_clocks -quiet] {
    set name [get_property NAME $clock]
    set generated [get_property IS_GENERATED $clock]
    set master ""
    if {$generated} {set master [get_property MASTER_CLOCK $clock]}
    puts $stream "[binary encode hex [encoding convertto utf-8 $name]]\t[get_property PERIOD $clock]\t$generated\t[binary encode hex [encoding convertto utf-8 $master]]"
    set root $clock
    while {[get_property IS_GENERATED $root]} {set root [get_clocks -quiet [list [get_property MASTER_CLOCK $root]]]}
    if {![string match emuflow_dut_clk* [get_property NAME $root]]} {lappend infrastructure $clock}
  }
  close $stream
  # An RX shadow is clocked by TDM but launches original DUT logic. Its next
  # valid TX occurrence belongs to global scheduled timing, not a mandatory
  # next-word check. Keep phase/control and native PHY register launches here.
  lassign [emuflow_word_control_sources $infrastructure $rx_scope] sources excluded complete
  if {[llength $sources] == 0} {return [list 0 {} $excluded 0 0 {}]}
  set slacks [list]
  foreach type {max min} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -delay_type $type -from $sources -to $infrastructure]
    if {[llength $paths] == 0} {return [list 0 {} $excluded [llength $sources] 0 {}]}
    lappend slacks [get_property SLACK $paths]
  }
  return [list 2 [lindex $slacks 0] $excluded [llength $sources] $complete [lindex $slacks 1]]
}
