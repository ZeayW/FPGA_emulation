# Unconditional native capture-family sources, including original clocked outputs.
proc emuflow_native_capture_sources {sources runtime_xdc output} {
  set outputs [dict create]
  set input [open $runtime_xdc r]
  while {[gets $input line]>=0} {
   if {![string match {set_output_delay *} $line]} {continue}
   if {![regexp {^set_output_delay -max ([^ ]+) -clock \[get_clocks \{([^\}]+)\}\] \[get_ports \{([^\}]+)\}\]$} $line _ delay clock select]} {
    error "capture source census requires the canonical explicit output environment"
   }
   if {![string is double -strict $delay]} {error "invalid original output environment"}
   foreach port [get_ports -quiet [list $select]] {dict lappend outputs $clock $port}
  }
  close $input
  file mkdir $output
  set summary [open [file join $output source-census.tsv] w]
  puts $summary "capture_clock_hex\ttarget_pins\tnative_startpoints\tdut_cells\trx_sources\tinfrastructure_cells\tports\tunclassified"
  set external [open [file join $output external-sources.tsv] w]
  puts $external "capture_clock_hex\tsource_kind\tsource_hex\tsource_origin"
  set receive [open [file join $output rx-sources.tsv] w]
  puts $receive "capture_clock_hex\tsource_origin"
  set progress [open [file join $output progress.tsv] w]
  puts $progress "capture_clock_hex\tstatus"
  set count 0
  set unknown [dict create]
  foreach clock [dict get $sources dut_clocks] {
   set name [get_property NAME $clock]
   set targets [dict create]
   set clock_pins [all_registers -clock $name -clock_pins]
   if {[llength $clock_pins]} {
    set cells [get_cells -quiet -of_objects $clock_pins]
    foreach pin [get_pins -quiet -of_objects $cells -filter {DIRECTION == IN && IS_CLOCK == 0}] {
     dict set targets [get_property NAME $pin] $pin
    }
   }
   if {[dict exists $outputs $name]} {
    foreach port [dict get $outputs $name] {dict set targets [get_property NAME $port] $port}
   }
   if {[dict size $targets]} {
    set fanin [emuflow_native_word_fanin $sources [dict values $targets]]
   } else {set fanin [dict create native_startpoints 0 rx {} dut_cells {} infrastructure_cells {} ports {} unclassified {}]}
   puts $summary "[binary encode hex $name]\t[dict size $targets]\t[dict get $fanin native_startpoints]\t[dict size [dict get $fanin dut_cells]]\t[dict size [dict get $fanin rx]]\t[dict size [dict get $fanin infrastructure_cells]]\t[dict size [dict get $fanin ports]]\t[dict size [dict get $fanin unclassified]]"
   dict for {origin pins} [dict get $fanin rx] {puts $receive "[binary encode hex $name]\t$origin"}
   foreach kind {infrastructure_cells ports unclassified} {
    dict for {owner object} [dict get $fanin $kind] {
     if {$kind eq "unclassified"} {dict set unknown $owner 1}
     set origin ""
     if {$kind eq "infrastructure_cells"} {set origin [get_property -quiet EMUFLOW_SOURCE $object]}
     puts $external "[binary encode hex $name]\t$kind\t[binary encode hex $owner]\t$origin"
    }
   }
   foreach stream [list $summary $external $receive] {flush $stream}
   puts $progress "[binary encode hex $name]\tcomplete"
   flush $progress
   incr count
  }
  foreach stream [list $summary $external $receive $progress] {close $stream}
  return $unknown
}
