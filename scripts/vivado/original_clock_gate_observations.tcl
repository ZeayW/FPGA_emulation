# Read-only native projections of original clock-gating enable checks.
# A CE fanout collection may overapproximate an old gating region. It records
# native candidates and other fanout context, without claiming equivalence or
# treating an absent source tag as a deleted timing path.
proc emuflow_original_gate_observations {request_path output_path} {
  if {[file normalize $request_path] eq [file normalize $output_path]} {error "gate observation output aliases its request"}
  if {[file size $request_path] > 16777216} {error "gate request exceeds its bounded endpoint scope"}
  set input [open $request_path r]
  set requests [list]
  set wanted [dict create]
  set seen [dict create]
  try {
    if {[gets $input header]<0 || $header ne "endpoint_seq\tgate_origin\tgate_pin_hex\tdriver_origin\tdriver_pin_hex"} {error "invalid original gate request header"}
    while {[gets $input line]>=0} {
      set row [split $line \t]
      if {[llength $row]!=5} {error "invalid original gate request row"}
      lassign $row endpoint gate gate_pin driver driver_pin
      if {![regexp {^(0|[1-9][0-9]*)$} $endpoint] || ![regexp {^s[0-9]+$} $gate] ||
          ![regexp {^s[0-9]+$} $driver] || [dict exists $seen [list $endpoint $gate]]} {error "invalid or repeated original gate source"}
      foreach encoded [list $gate_pin $driver_pin] {
        if {$encoded eq "" || [string length $encoded]%2 || ![regexp {^[0-9a-f]+$} $encoded]} {error "invalid original gate pin encoding"}
      }
      dict set seen [list $endpoint $gate] 1
      dict set wanted $gate 1; dict set wanted $driver 1
      lappend requests $row
      if {[llength $requests]>65536} {error "too many original gate requests"}
    }
  } finally {close $input}
  # One native scan, retaining every tagged replica and every actual type.
  set cells [dict create]
  if {[dict size $wanted]} {
    set native [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}]
    foreach cell $native origin [get_property -quiet EMUFLOW_SOURCE $native] {
      if {[dict exists $wanted $origin]} {dict lappend cells $origin $cell}
    }
  }
  set output [open $output_path w]
  set count 0
  try {
    puts $output "endpoint_seq\tgate_origin\tdriver_origin\tprojection\tnative_pin_hex\tnative_ref\tnative_origin\tcapture_clocks_hex"
    foreach row $requests {
      lassign $row endpoint gate gate_pin driver driver_pin
      set prefix "$endpoint\t$gate\t$driver"
      set observed 0
      if {[dict exists $cells $gate]} {
        set pin_name [encoding convertfrom utf-8 [binary decode hex $gate_pin]]
        foreach cell [dict get $cells $gate] {
          foreach pin [get_pins -quiet -of_objects $cell] {
            if {[get_property REF_PIN_NAME $pin] eq $pin_name} {
              puts $output "$prefix\tsource-pin-observed\t[binary encode hex [encoding convertto utf-8 [get_property NAME $pin]]]\t[get_property REF_NAME $cell]\t$gate\t"
              incr observed; incr count
            }
          }
        }
      }
      if {!$observed} {
        puts $output "$prefix\tsource-pin-unretained\t\t\t\t"
        incr count
      }
      if {![dict exists $cells $driver]} {
        puts $output "$prefix\tlocal-driver-unretained\t\t\t\t"
        incr count; continue
      }
      set driver_pins [list]
      set pin_name [encoding convertfrom utf-8 [binary decode hex $driver_pin]]
      foreach cell [dict get $cells $driver] {
        foreach pin [get_pins -quiet -of_objects $cell] {
          if {[get_property REF_PIN_NAME $pin] eq $pin_name} {
            lappend driver_pins $pin
            puts $output "$prefix\tlocal-driver-pin-observed\t[binary encode hex [encoding convertto utf-8 [get_property NAME $pin]]]\t[get_property REF_NAME $cell]\t$driver\t"
            incr count
          }
        }
      }
      if {![llength $driver_pins]} {
        puts $output "$prefix\tlocal-driver-pin-unretained\t\t\t\t"
        incr count; continue
      }
      set fanout [all_fanout -flat -endpoints_only -trace_arcs enabled -from $driver_pins]
      foreach pin $fanout {
        set name [get_property NAME $pin]
        if {[get_property CLASS $pin] ne "pin"} {
          puts $output "$prefix\tnon-ce-fanout-context\t[binary encode hex [encoding convertto utf-8 $name]]\t\t\t"
          incr count; continue
        }
        set owner [get_cells -quiet -of_objects $pin]
        set role [get_property REF_PIN_NAME $pin]
        set projection [expr {$role eq "CE" ? "enable-fanout-ce-candidate" : "non-ce-fanout-context"}]
        set clock_pins [get_pins -quiet -of_objects $owner -filter {IS_CLOCK == 1}]
        set clocks [list]
        if {[llength $clock_pins]} {set clocks [get_property NAME [get_clocks -quiet -of_objects $clock_pins]]}
        puts $output "$prefix\t$projection\t[binary encode hex [encoding convertto utf-8 $name]]\t[get_property REF_NAME $owner]\t[get_property -quiet EMUFLOW_SOURCE $owner]\t[binary encode hex [encoding convertto utf-8 $clocks]]"
        incr count; incr observed
      }
      if {!$observed} {
        puts $output "$prefix\tno-native-endpoint-observed\t\t\t\t"
        incr count
      }
      flush $output
    }
  } finally {close $output}
  return $count
}
