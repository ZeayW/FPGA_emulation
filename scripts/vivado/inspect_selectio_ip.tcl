# Inspect the installed native SelectIO IP before constructing an LVDS PHY.
# This creates one private IP customization, not an implemented design.
if {$argc ni {2 3}} {error "expected PART EMPTY_OUTPUT_DIRECTORY ?CONFIG_TSV?"}
lassign $argv part output configuration
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
  file mkdir [file join $output ip]
  create_ip -vlnv [lindex $definitions 0] -module_name emuflow_selectio_probe -dir [file join $output ip]
  set ip [get_ips emuflow_selectio_probe]
  if {$configuration ne ""} {
    set stream [open $configuration r]
    if {[gets $stream header] < 0 || $header ne "property\tvalue"} {error "invalid SelectIO configuration header"}
    set settings [dict create]
    while {[gets $stream row] >= 0} {
      if {[dict size $settings] >= 4096} {error "SelectIO configuration exceeds bound"}
      set fields [split $row "\t"]
      if {[llength $fields] != 2} {error "invalid SelectIO configuration record"}
      lassign $fields property value
      if {![regexp {^CONFIG\.[A-Za-z0-9_.]+$} $property] ||
          [lsearch -exact [list_property $ip] $property] < 0 || [dict exists $settings $property]} {
        error "unknown or duplicate SelectIO configuration property"
      }
      if {[regexp {^CONFIG\.ENABLE_BYTE[0-3]_PIN([0-9]|1[0-2])$} $property] &&
          $value ni {true false}} {
        error "SelectIO pin-enable values must use lowercase true or false"
      }
      dict set settings $property $value
    }
    close $stream
    if {[dict size $settings] == 0} {error "empty SelectIO configuration"}
    # Parameters can enable later controls; preserve the declared TSV order.
    dict for {property requested} $settings {set_property $property $requested $ip}
    dict for {property requested} $settings {
      set actual [get_property $property $ip]
      if {$actual ne $requested && !([string is double -strict $actual] &&
          [string is double -strict $requested] && $actual == $requested) &&
          !([string is boolean -strict $actual] && [string is boolean -strict $requested] &&
            bool($actual) == bool($requested))} {
        error "SelectIO configuration was ignored or changed: $property requested=$requested observed=$actual"
      }
    }
  }
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
  if {$configuration ne ""} {
    generate_target all $ip
    report_ip_status -file [file join $output ip-status.rpt]
  }
  close_project
} message options]} {
  puts stderr $message
  if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
  exit 1
}
exit 0
