# Explicit checkpoint I/O diagnostic; it does not qualify placement or routing.
if {$argc != 3} {error "expected INPUT_DCP OUTPUT_DIRECTORY EXPECTED_PART"}
lassign $argv checkpoint out part
file mkdir $out
set started [clock seconds]
puts "EMUFLOW_CHECKPOINT_IO stage=read input=$checkpoint"
open_checkpoint $checkpoint
if {[get_property PART [current_design]] ne $part} {error "checkpoint part differs"}
puts "EMUFLOW_CHECKPOINT_IO stage=write output=$out/roundtrip.dcp"
write_checkpoint -force "$out/roundtrip.dcp"
puts "EMUFLOW_CHECKPOINT_IO status=pass elapsed_seconds=[expr {[clock seconds]-$started}] bytes=[file size $out/roundtrip.dcp]"
exit 0
