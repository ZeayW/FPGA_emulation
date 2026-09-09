# Vivado -mode batch -nojournal -nolog -source this-file \
#   -tclargs PART OUTPUT_DIRECTORY
# Device database probe only: not a PCB check or implementation license test.
# Invoke from a permitted scratch directory.
if {$argc != 2} {
    error "expected PART OUTPUT_DIRECTORY"
}
set part [lindex $argv 0]
set out [file normalize [lindex $argv 1]]
if {$part ni {xcvu13p-fhga2104-1-e xcvu9p-flga2104-2L-e}} {
    error "unsupported reference-platform candidate part: $part"
}
if {[llength [get_parts -quiet $part]] != 1} {
    error "installed Vivado does not contain the exact requested device: $part"
}
create_project -in_memory -part $part reference_hardware_preflight
set_property design_mode PinPlanning [current_fileset]
open_io_design -name reference_hardware_io
set channels [get_sites -quiet -filter {SITE_TYPE == GTYE4_CHANNEL}]
set commons [get_sites -quiet -filter {SITE_TYPE == GTYE4_COMMON}]
if {[llength $channels] == 0 || [llength $commons] == 0} {
    error "requested part has no supported GTYE4 channel/common inventory"
}
file mkdir $out
set report [open [file join $out device-preflight.tsv] w]
puts $report "field\tvalue"
puts $report "part\t$part"
puts $report "vivado_version\t[version -short]"
puts $report "gty_channels\t[llength $channels]"
puts $report "gty_commons\t[llength $commons]"
puts $report "device_database\tpass"
puts $report "implementation_license\tnot_checked"
puts $report "physical_implementation\tnot_run"
puts $report "board_wiring\tnot_verified"
close $report
close_project
exit 0
