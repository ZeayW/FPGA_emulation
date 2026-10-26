# Stream engine launch tags for requested original source endpoints. Requests
# are ordered source coordinates from the caller, not a selected path list.
source [file join [file dirname [info script]] design_context.tcl]
source [file join [file dirname [info script]] launch_clock_edges.tcl]
if {[catch {
  set allowed_clocks [emuflow_read_design_context]
  set catalog [open [emuflow_required_env EMUFLOW_LAUNCH_CLOCK_CATALOG] w]
  puts $catalog "clock_seq\tclock_hex\tperiod_ns"
  set clock_seq 0
  foreach name $allowed_clocks {
    set clock [get_clocks -quiet [list $name]]
    if {[llength $clock]!=1} {error "declared launch clock is absent or ambiguous"}
    set period [sta::time_sta_ui [$clock period]]
    puts $catalog "$clock_seq\t[emuflow_hex_encode $name]\t$period"
    incr clock_seq
  }
  close $catalog
  set input [open [emuflow_required_env EMUFLOW_LAUNCH_REQUESTS] r]
  if {[gets $input header] < 0 || $header ne "source_seq\tpin_hex"} {
    error "invalid launch request header"
  }
  set output [open [emuflow_required_env EMUFLOW_LAUNCH_OUTPUT] w]
  puts $output "source_seq\tclock_hex\tperiod_ns\tphase_ns"
  # Update once, then enumerate every data-clock tag on each requested pin.
  sta::find_timing_cmd 1
  set sources 0
  set records 0
  while {[gets $input line] >= 0} {
    set fields [split $line "\t"]
    if {[llength $fields] != 2 || [lindex $fields 0] ne "$sources"} {
      error "launch source coordinate is missing, repeated or out of order"
    }
    set encoded [lindex $fields 1]
    if {[string length $encoded] > 2097152 || [string length $encoded] % 2 ||
        ![regexp {^[0-9a-f]+$} $encoded]} {
      error "launch request pin is not bounded hexadecimal UTF-8"
    }
    set name [emuflow_hex_decode $encoded]
    if {[emuflow_hex_encode $name] ne $encoded || [string first \x00 $name] >= 0} {
      error "launch request pin encoding is not canonical UTF-8"
    }
    set pin [sta::get_port_pin_error pin $name]
    if {[get_full_name $pin] ne $name} {
      error "launch request resolved to another source pin"
    }
    foreach fact [emuflow_launch_clock_edges $pin $allowed_clocks] {
      lassign $fact clock period phase
      puts $output "$sources\t[emuflow_hex_encode $clock]\t$period\t$phase"
      incr records
    }
    incr sources
  }
  close $input
  close $output
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_LAUNCH_CLOCK_EXPORT status=pass sources=$sources facts=$records clocks=[llength $allowed_clocks]"
exit 0
