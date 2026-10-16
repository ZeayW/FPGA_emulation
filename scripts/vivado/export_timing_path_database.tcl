# Export partition-independent STA paths with ordered EmuIR net identities.
#
# All names use UTF-8 hex so the TSV is lossless for escaped identifiers.

if {$argc != 4} {
  error "usage: vivado -mode batch -source export_timing_path_database.tcl -tclargs INPUT_DCP NET_MAP_TSV OUTPUT_TSV MAX_PATHS"
}
set input_dcp [file normalize [lindex $argv 0]]
set map_path [file normalize [lindex $argv 1]]
set output_path [file normalize [lindex $argv 2]]
set max_paths [lindex $argv 3]
if {![string is integer -strict $max_paths] || $max_paths < 0} {
  error "MAX_PATHS must be nonnegative (0 means all endpoints)"
}

proc emuflow_hex_decode {value} {
  return [encoding convertfrom utf-8 [binary decode hex $value]]
}
proc emuflow_hex_encode {value} {
  return [binary encode hex [encoding convertto utf-8 $value]]
}

open_checkpoint $input_dcp
set input [open $map_path r]
set lines [split [read $input] "\n"]
close $input
if {[lindex $lines 0] ne "vivado_net_hex\temuir_net_hex"} {
  error "invalid EmuIR net-map header"
}

array set emuir_by_vivado_net {}
array set object_by_vivado_net {}
foreach object [get_nets -quiet -hier -filter {NAME =~ *__emuflow_net_*}] {
  set object_by_vivado_net([get_property NAME $object]) $object
}
set mapped 0
foreach line [lrange $lines 1 end] {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  if {[llength $fields] != 2} {
    error "malformed EmuIR net-map row"
  }
  set vivado_name [emuflow_hex_decode [lindex $fields 0]]
  set emuir_name [emuflow_hex_decode [lindex $fields 1]]
  if {![info exists object_by_vivado_net($vivado_name)]} {
    continue
  }
  set emuir_by_vivado_net($vivado_name) $emuir_name
  incr mapped
}
if {$mapped == 0} {
  error "none of the mapped EmuIR nets exist in the checkpoint"
}

set all_clocks [get_clocks -quiet]
if {$max_paths == 0} {
  set max_paths [expr {[llength [all_registers -data_pins]] + [llength [all_outputs]]}]
}
set timing_paths [get_timing_paths -quiet -setup -nworst 1 \
  -max_paths $max_paths -from $all_clocks -to $all_clocks]
set output [open $output_path w]
puts $output "path_id_hex\tclock_domain_hex\tclock_period_ns\tslack_ns\tfixed_delay_ns\tpath_nets_hex"
set emitted 0
foreach path $timing_paths {
  set path_nets [list]
  unset -nocomplain seen_net
  array set seen_net {}
  foreach net [get_nets -quiet -of_objects $path] {
    set name [get_property NAME $net]
    if {[info exists emuir_by_vivado_net($name)]} {
      set emuir_name $emuir_by_vivado_net($name)
      if {![info exists seen_net($emuir_name)]} {
        set seen_net($emuir_name) 1
        lappend path_nets $emuir_name
      }
    }
  }
  if {[llength $path_nets] == 0} {
    continue
  }
  set path_hex [list]
  foreach net $path_nets {
    lappend path_hex [emuflow_hex_encode $net]
  }
  set start_name [get_property STARTPOINT_PIN $path]
  set end_name [get_property ENDPOINT_PIN $path]
  if {$start_name eq "" || $end_name eq ""} {
    continue
  }
  set path_id "$start_name->$end_name#[format %08d $emitted]"
  set group [get_property GROUP $path]
  set period [get_property REQUIREMENT $path]
  set slack [get_property SLACK $path]
  set fixed_delay [get_property DATAPATH_DELAY $path]
  if {$period eq "" || $slack eq "" || $fixed_delay eq ""} {
    continue
  }
  puts $output "[emuflow_hex_encode $path_id]\t[emuflow_hex_encode $group]\t$period\t$slack\t$fixed_delay\t[join $path_hex ,]"
  incr emitted
}
close $output
puts "EMUFLOW_STA_DATABASE status=pass mapped_nets=$mapped queried_paths=[llength $timing_paths] emitted_paths=$emitted output=$output_path"
