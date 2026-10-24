# Shared exact physical boundary resolution for indexed Phase6 timing.
proc emuflow_use_data_path_constraints {} {
  # These fresh checkpoint readers export physical DELAY, never closure slack.
  # Equal datapath-only requirements prevent a short-period clock domain from
  # winning a cone query merely through worse slack instead of longer delay.
  # Nothing is written back to the routed checkpoint or implementation reports.
  set clocks [get_clocks -quiet]
  foreach launch $clocks {
    foreach capture $clocks {
      set_max_delay -reset_path -datapath_only -from $launch -to $capture 1000000
    }
  }
}

proc emuflow_load_fixed_io_map {path} {
  set result [dict create]
  if {$path eq ""} {return $result}
  set stream [open $path r]
  if {[gets $stream header] < 0 || $header ne "logical_hex\tphysical_hex"} {
    error "invalid fixed I/O timing map header"
  }
  while {[gets $stream row] >= 0} {
    lassign [split $row "\t"] logical physical
    set logical [encoding convertfrom utf-8 [binary decode hex $logical]]
    set physical [encoding convertfrom utf-8 [binary decode hex $physical]]
    if {$logical eq "" || $physical eq "" || [dict exists $result $logical]} {
      error "invalid or duplicate fixed I/O timing map"
    }
    dict set result $logical $physical
  }
  close $stream
  return $result
}

proc emuflow_validate_segment_extent {path start end measurement} {
  set actual_start [get_property STARTPOINT_PIN $path]
  set actual_end [get_property ENDPOINT_PIN $path]
  set requested_start [list]
  foreach object $start {lappend requested_start [get_property NAME $object]}
  set requested_end [list]
  foreach object $end {lappend requested_end [get_property NAME $object]}
  if {[lsearch -exact $requested_end $actual_end] < 0} {
    error "timing query includes a suffix beyond requested endpoint $requested_end: $actual_end"
  }
  if {$measurement eq "cut-net-cone-upper-bound" || [lsearch -exact $requested_start $actual_start] >= 0} {return}
  # A sequential Q launch includes clock-to-Q, as does the common routed
  # timing model. Vivado names that path's start at the SAME primitive clock.
  # A through query starting in another cell would include an unrelated prefix.
  if {[lindex [get_property CLASS $start] 0] eq "pin"} {
    set cell [get_cells -quiet -of_objects $start]
    foreach pin [get_pins -quiet -of_objects $cell -filter {IS_CLOCK == 1}] {
      if {[get_property NAME $pin] eq $actual_start} {return}
    }
  }
  error "timing query includes a prefix before requested startpoint $requested_start: $actual_start"
}
