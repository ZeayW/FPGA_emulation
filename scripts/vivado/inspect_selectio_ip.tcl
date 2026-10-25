# Inspect the installed native SelectIO IP before constructing an LVDS PHY.
# This creates one private IP customization, not an implemented design.
if {$argc != 2} {error "expected PART EMPTY_OUTPUT_DIRECTORY"}
lassign $argv part output
if {[file exists $output]} {
  if {![file isdirectory $output]} {error "SelectIO output is not a directory"}
  foreach entry [glob -nocomplain -tails -directory $output * .*] {
    if {$entry ni {. ..}} {error "SelectIO inspection requires an empty output directory"}
  }
}
file mkdir $output
set output [file normalize $output]
if {[catch {
  if {[llength [get_parts -quiet $part]] != 1} {error "target part is not installed"}
  create_project -in_memory -part $part emuflow_selectio_inspection
  set definitions [get_ipdefs -all -quiet xilinx.com:ip:high_speed_selectio_wiz:*]
  if {[llength $definitions] != 1} {
    error "expected exactly one installed High Speed SelectIO Wizard definition: $definitions"
  }
  create_ip -vlnv [lindex $definitions 0] -module_name emuflow_selectio_probe -dir [file join $output ip]
  set ip [get_ips emuflow_selectio_probe]
  set stream [open [file join $output ip-properties.tsv] w]
  puts $stream "property_hex\tvalue_hex"
  set count 0
  foreach property [lsort [list_property $ip]] {
    if {![string match CONFIG.* $property]} {continue}
    if {[incr count] > 16384} {error "SelectIO configuration exceeds inspection bound"}
    set value [get_property $property $ip]
    puts $stream "[binary encode hex [encoding convertto utf-8 $property]]\t[binary encode hex [encoding convertto utf-8 $value]]"
  }
  close $stream
  set stream [open [file join $output tool-identity.tsv] w]
  puts $stream "field\tvalue"
  puts $stream "vivado_version\t[version -short]"
  puts $stream "part\t$part"
  puts $stream "ip_definition\t[lindex $definitions 0]"
  puts $stream "config_properties\t$count"
  close $stream
  close_project
} message options]} {
  puts stderr $message
  if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
  exit 1
}
exit 0
