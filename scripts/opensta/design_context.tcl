# One source-design timing context shared by path and launch-tag export.
proc emuflow_required_env {name} {
  global env
  if {![info exists env($name)] || $env($name) eq ""} {
    error "missing required environment variable $name"
  }
  return $env($name)
}

proc emuflow_hex_decode {value} {
  # Keep the on-disk transport independent of Tcl's newer encode/decode
  # subcommands; the H* representation is stable across supported runtimes.
  return [encoding convertfrom utf-8 [binary format H* $value]]
}

proc emuflow_hex_encode {value} {
  binary scan [encoding convertto utf-8 $value] H* encoded
  return $encoded
}

proc emuflow_read_design_context {} {
  set liberty_path [file normalize [emuflow_required_env EMUFLOW_STA_LIBERTY]]
  set verilog_path [file normalize [emuflow_required_env EMUFLOW_STA_VERILOG]]
  set top [emuflow_required_env EMUFLOW_STA_TOP]
  set clock_path [file normalize [emuflow_required_env EMUFLOW_STA_CLOCKS]]
  set timing_io_path [file normalize [emuflow_required_env EMUFLOW_STA_TIMING_IO]]
  read_liberty $liberty_path
  read_verilog $verilog_path
  link_design $top

  set clock_input [open $clock_path r]
  if {[gets $clock_input clock_header] < 0 ||
      $clock_header ne "clock_hex\tperiod_ns"} {
    close $clock_input
    error "invalid OpenSTA clock-map header"
  }
  set allowed_clocks {}
  while {[gets $clock_input line] >= 0} {
    if {$line eq ""} {
      continue
    }
    set fields [split $line "\t"]
    if {[llength $fields] != 2} {
      error "malformed OpenSTA clock-map row"
    }
    set clock_name [emuflow_hex_decode [lindex $fields 0]]
    set period [lindex $fields 1]
    set port [get_ports -quiet [list $clock_name]]
    if {[llength $port] != 1} {
      error "clock port '$clock_name' is absent or ambiguous"
    }
    create_clock -name $clock_name -period $period $port
    lappend allowed_clocks $clock_name
  }
  close $clock_input
  if {[llength $allowed_clocks] == 0} {
    error "OpenSTA requires at least one clock"
  }

  # Apply the same explicit top-level timing environment used by the black-box
  # PPro holdout.  Python has already validated each base identifier against the
  # EmuIR port inventory and its direction.  Resolve a scalar exact name first;
  # if it is a vector, fall back to the exact-base bus spelling rather than a
  # loose prefix that could match an unrelated interface.
  set timing_io_input [open $timing_io_path r]
  if {[gets $timing_io_input timing_io_header] < 0 ||
      $timing_io_header ne "direction\tclock_hex\tdelay_ns\tport_hex"} {
    close $timing_io_input
    error "invalid OpenSTA timing-I/O header"
  }
  while {[gets $timing_io_input line] >= 0} {
    if {$line eq ""} {
      continue
    }
    set fields [split $line "\t"]
    if {[llength $fields] != 4} {
      error "malformed OpenSTA timing-I/O row"
    }
    set direction [lindex $fields 0]
    set clock_name [emuflow_hex_decode [lindex $fields 1]]
    set delay [lindex $fields 2]
    set port_name [emuflow_hex_decode [lindex $fields 3]]
    set clock [get_clocks -quiet [list $clock_name]]
    if {[llength $clock] != 1} {
      error "timing-I/O clock '$clock_name' is absent or ambiguous"
    }
    set port_objects [get_ports -quiet [list $port_name]]
    if {[llength $port_objects] == 0} {
      set bus_pattern $port_name
      append bus_pattern {[*]}
      set port_objects [get_ports -quiet [list $bus_pattern]]
    }
    if {[llength $port_objects] == 0} {
      error "timing-I/O port '$port_name' is absent"
    }
    if {$direction eq "input"} {
      set_input_delay -clock $clock -max $delay $port_objects
    } elseif {$direction eq "output"} {
      set_output_delay -clock $clock -max $delay $port_objects
    } else {
      error "unsupported timing-I/O direction '$direction'"
    }
  }
  close $timing_io_input
  return $allowed_clocks
}
