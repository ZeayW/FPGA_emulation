# Explicit optimization exercise, independent of default heuristics.
# Consume an active synthetic checkpoint containing duplicate registers.
# This is not a routed physical/global timing qualification.
if {$argc != 3} {error "expected INPUT_CHECKPOINT OUTPUT_DIRECTORY PREFIX"}
lassign $argv checkpoint out prefix
file mkdir $out
source [file join [file dirname [info script]] .. vivado source_inventory.tcl]
set_param general.maxThreads 2
open_checkpoint $checkpoint
set protected 0
foreach cell [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}] {
  if {[string tolower [get_property DONT_TOUCH $cell]] in {1 true yes}} {incr protected}
}
if {$protected != 0} {error "optimization probe contains protected source cells"}
set before [emuflow_inventory "$out/before.tsv" $prefix]
opt_design -merge_equivalent_drivers
set after [emuflow_inventory "$out/after.tsv" $prefix]
if {$after >= $before} {error "optimization feature not exercised: $before -> $after"}
set stream [open "$out/metrics.tsv" w]
puts $stream "metric\tvalue"
foreach {key value} [list source_objects_before $before source_objects_after $after protected_source_cells $protected vivado_version [version -short]] {
  puts $stream "$key\t$value"
}
close $stream
puts "EMUFLOW_OPTIMIZATION_QUALIFICATION status=pass source_objects_before=$before source_objects_after=$after protected_source_cells=$protected scope=logic-optimization-only"
exit 0
