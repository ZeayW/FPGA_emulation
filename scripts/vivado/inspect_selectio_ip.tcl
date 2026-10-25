# Inspect the installed native SelectIO IP before constructing an LVDS PHY.
# This creates one private IP customization, not an implemented design.
if {$argc ni {2 3 4}} {error "expected PART EMPTY_OUTPUT_DIRECTORY ?CONFIG_TSV ?synthesize??"}
lassign $argv part output configuration synthesis
if {$synthesis ne "" && ($synthesis ne "synthesize" || $configuration eq "")} {
  error "SelectIO synthesis requires a configuration and the literal synthesize mode"
}
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
  source [file join [file dirname [info script]] selectio_ip.tcl]
  lassign [emuflow_create_selectio_ip emuflow_selectio_probe [file join $output ip] $configuration] ip definition
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
  puts $stream "ip_definition\t$definition"
  puts $stream "config_properties\t$count"
  close $stream
  if {$configuration ne ""} {
    generate_target all $ip
    report_ip_status -file [file join $output ip-status.rpt]
  }
  if {$synthesis eq "synthesize"} {
    synth_ip $ip
    set checkpoint [file join $output ip emuflow_selectio_probe emuflow_selectio_probe.dcp]
    if {![file isfile $checkpoint]} {error "native IP synthesis did not publish its checkpoint"}
    open_checkpoint $checkpoint
    report_utilization -file [file join $output synthesis-utilization.rpt]
    report_clocks -file [file join $output synthesis-clocks.rpt]
    report_io -file [file join $output synthesis-io.rpt]
    set stream [open [file join $output synthesis-ports.tsv] w]
    puts $stream "name_hex\tdirection"
    foreach port [lsort [get_ports -quiet *]] {
      puts $stream "[binary encode hex [encoding convertto utf-8 [get_property NAME $port]]]\t[get_property DIRECTION $port]"
    }
    close $stream
    close_design
  }
  close_project
} message options]} {
  puts stderr $message
  if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
  exit 1
}
exit 0
