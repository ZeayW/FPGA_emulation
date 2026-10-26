# Query-only qualification on an explicitly selected routed FF -> word TX
# path. Clock waveforms change only in this in-memory diagnostic design.
# No checkpoint is written and this does not qualify whole-design timing.
if {$argc != 4} {error "expected INPUT_DCP START_Q_PIN END_D_PIN OUTPUT_TSV"}
lassign $argv checkpoint source_name target_name output
source [file join [file dirname [info script]] timing_contract.tcl]
if {[catch {
  open_checkpoint $checkpoint
  set start [emuflow_exact_hier_pin $source_name]
  set finish [emuflow_exact_hier_pin $target_name]
  set cell [get_cells -quiet -of_objects $start]
  if {[llength $cell]!=1 || [get_property REF_NAME $cell] ni {FDRE FDCE FDSE FDPE} ||
      [get_property REF_PIN_NAME $start] ne "Q"} {error "phase qualifier requires one explicit original FF Q pin"}
  set clock_pin [get_pins -quiet -of_objects $cell -filter {REF_PIN_NAME == C}]
  set clock [get_clocks -quiet -of_objects $clock_pin]
  if {[llength $clock]!=1 || [get_property IS_GENERATED $clock]} {error "phase qualifier requires one primary source clock"}
  set name [get_property NAME $clock]
  set ports [get_ports -quiet [get_property SOURCE_PINS $clock]]
  if {[llength $ports]!=1} {error "phase qualifier primary clock has no unique source port"}
  set period [get_property PERIOD $clock]
  if {![string is double -strict $period] || !($period>0 && $period<Inf)} {error "phase qualifier source period is invalid"}
  set stream [open $output w]
  puts $stream "phase_ns\tperiod_ns\tdata_delay_ns\treference_cost_ns\tcheck_clock_adjustment_ns\trequirement_ns\tslack_ns"
  set baseline {}
  foreach fraction {0.0 0.25 0.5} {
    set phase [expr {$period*$fraction}]
    create_clock -name $name -period $period -waveform [list $phase [expr {$phase+$period/2.0}]] $ports
    emuflow_use_clock_aware_reference_constraints
    set paths [get_timing_paths -quiet -from $start -to $finish -max_paths 1 -nworst 1]
    if {[llength $paths]!=1} {error "phase qualifier lost its explicit source/target path"}
    set path [lindex $paths 0]
    lassign [emuflow_reference_path_cost $path] cost adjustment
    set data [get_property DATAPATH_DELAY $path]
    if {[llength $baseline]==0} {set baseline [list $data $cost]}
    if {abs($data-[lindex $baseline 0])>0.0011 || abs($cost-[lindex $baseline 1])>0.0011} {
      error "common-reference sampling cost contains a nominal source phase or changed its path"
    }
    puts $stream [join [list $phase $period $data $cost $adjustment [get_property REQUIREMENT $path] [get_property SLACK $path]] "\t"]
  }
  close $stream
} message options]} {
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_CLOCK_REFERENCE_PHASE_QUALIFICATION status=pass cases=3 scope=synthetic-query-only"
exit 0
