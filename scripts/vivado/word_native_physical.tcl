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

proc emuflow_word_routed_checks {directory} {
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
  set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -from $infrastructure -to $infrastructure]
  if {[llength $paths] == 0} {return {0 {}}}
  set slack [get_property SLACK $paths]
  if {$slack < 0} {error "native word infrastructure misses its fixed clock contract: WNS=$slack ns"}
  return [list [llength $paths] $slack]
}
