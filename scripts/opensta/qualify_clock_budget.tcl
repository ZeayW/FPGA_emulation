# Synthetic engine/API qualification, never physical or whole-design evidence.
foreach key {LIBERTY VERILOG OUTPUT CLOCK_TO_Q_NS} {
  if {![info exists env(EMUFLOW_BUDGET_$key)]} {error "missing EMUFLOW_BUDGET_$key"}
}
set liberty $env(EMUFLOW_BUDGET_LIBERTY)
set verilog $env(EMUFLOW_BUDGET_VERILOG)
set output $env(EMUFLOW_BUDGET_OUTPUT)
set expected_cq $env(EMUFLOW_BUDGET_CLOCK_TO_Q_NS)
source [file join [file dirname [info script]] clock_budget.tcl]
proc emuflow_budget_close {actual expected context} {
  if {abs($actual-$expected)>0.00001} {error "$context: expected $expected observed $actual"}
}
if {[catch {
  read_liberty $liberty
  read_verilog $verilog
  link_design clock_budget_fixture
  create_clock -name base -period 10 [get_ports clkp]
  create_clock -name shifted -period 10 -waveform {5 10} [get_ports clkn]
  set_multicycle_path 2 -setup -from [get_cells launch_multi] -to [get_cells capture_multi]
  set_output_delay 2 -clock base [get_ports raw_output]
  set stream [open $output w]
  set header {}
  foreach record {{full launch_full capture_full/D 10 0} {half launch_half capture_half/D 5 0} {multi launch_multi capture_multi/D 20 10} {output launch_full raw_output 10 0}} {
    lassign $record label source target window adjustment
    if {$label eq "output"} {set endpoint [get_ports $target]} else {set endpoint [get_pins $target]}
    set paths [find_timing_paths -from [get_cells $source] -to $endpoint -path_delay max -group_path_count 1]
    if {[llength $paths]!=1} {error "missing unique qualification path: $label"}
    set budget [emuflow_clock_budget [lindex $paths 0]]
    emuflow_budget_close [dict get $budget edge_window_ns] $window "$label edge window"
    emuflow_budget_close [dict get $budget multicycle_adjustment_ns] $adjustment "$label multicycle"
    emuflow_budget_close [dict get $budget launch_relative_arrival_ns] $expected_cq "$label launch-relative CQ"
    emuflow_budget_close [dict get $budget slack_ns] [expr {[dict get $budget required_ns]-[dict get $budget arrival_ns]}] "$label engine slack"
    if {$label eq "half"} {emuflow_budget_close [dict get $budget launch_edge_ns] 5 "shifted launch"}
    if {$label eq "output" && ![dict get $budget output_delay]} {error "output-delay path not classified"}
    if {$header eq ""} {set header [dict keys $budget];puts $stream "case\t[join $header \t]"}
    set row [list $label]
    foreach key $header {lappend row [dict get $budget $key]}
    puts $stream [join $row "\t"]
  }
  close $stream
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_CLOCK_BUDGET_QUALIFICATION status=pass cases=4 scope=synthetic-engine-api-only"
exit 0
