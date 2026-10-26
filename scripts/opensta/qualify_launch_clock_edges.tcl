# Bounded engine API qualification, not a production launch-fact owner.
source [file join [file dirname [info script]] launch_clock_edges.tcl]
if {[catch {
  read_liberty $env(EMUFLOW_LAUNCH_LIBERTY)
  read_verilog $env(EMUFLOW_LAUNCH_VERILOG)
  link_design launch_clock_fixture
  create_clock -name base -period 10 [get_ports clk]
  create_clock -name shifted -period 10 -waveform {2.5 7.5} [get_ports other_clk]
  set_input_delay 0 -clock base [get_ports data]
  # Update the graph once. There is no find_timing_paths/endpoint selection.
  sta::find_timing_cmd 1
  set output [open $env(EMUFLOW_LAUNCH_OUTPUT) w]
  puts $output "pin_hex\tclock_hex\tperiod_ns\tphase_ns"
  foreach name {positive/Q negative/Q gated/Q inverted/Q unselected/Q shifted/Q dual/Q data} {
    set pin [sta::get_port_pin_error pin $name]
    set facts [emuflow_launch_clock_edges $pin {base shifted}]
    foreach fact $facts {
      lassign $fact clock period phase
      puts $output "[binary encode hex [encoding convertto utf-8 $name]]\t[binary encode hex [encoding convertto utf-8 $clock]]\t$period\t$phase"
    }
  }
  close $output
  foreach name {unclocked/Q buffer/O} {
    set pin [sta::get_port_pin_error pin $name]
    if {![catch {emuflow_launch_clock_edges $pin {base shifted}}]} {
      error "unsupported launch was assigned a clock phase: $name"
    }
  }
  set pin [sta::get_port_pin_error pin positive/Q]
  if {![catch {emuflow_launch_clock_edges $pin {shifted}}]} {
    error "undeclared launch clock was accepted"
  }
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_LAUNCH_CLOCK_QUALIFICATION status=pass pins=8 rejected=3 scope=synthetic-engine-api-only"
exit 0
