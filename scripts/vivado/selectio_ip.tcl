# Shared literal customization for native SelectIO inspection and integration.
proc emuflow_create_selectio_ip {module directory configuration} {
  if {![regexp {^[A-Za-z_][A-Za-z0-9_]*$} $module]} {error "invalid SelectIO module identity"}
  set definitions [get_ipdefs -all -quiet xilinx.com:ip:high_speed_selectio_wiz:*]
  if {[llength $definitions] != 1} {
    error "expected exactly one installed High Speed SelectIO Wizard definition: $definitions"
  }
  file mkdir $directory
  create_ip -vlnv [lindex $definitions 0] -module_name $module -dir $directory
  set ip [get_ips $module]
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
          $value ni {true false}} {error "SelectIO pin-enable values must use lowercase true or false"}
      dict set settings $property $value
    }
    close $stream
    if {[dict size $settings] == 0} {error "empty SelectIO configuration"}
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
  return [list $ip [lindex $definitions 0]]
}
