# Shared exact physical boundary resolution for indexed Phase6 timing.
proc emuflow_open_export_design {selector stage} {
  if {$selector eq "@current-design"} {
    if {![info exists ::emuflow_native_export_stage] || $::emuflow_native_export_stage ne $stage ||
        [llength [current_design -quiet]] != 1} {
      error "current design is not a checked $stage native export snapshot"
    }
  } else {
    open_checkpoint [file normalize $selector]
  }
}

proc emuflow_use_data_path_constraints {} {
  # These snapshot readers export physical DELAY, never closure slack.
  # Equal datapath-only requirements prevent a short-period clock domain from
  # winning a cone query merely through worse slack instead of longer delay.
  # A 1 ns query reference also avoids single-precision cancellation from a
  # huge positive requirement. Negative query slack is permitted: this is
  # applied only after implementation, never as a clock or closure target.
  # Implementation reports are finalized before the native-session exports.
  set clocks [get_clocks -quiet]
  if {[llength $clocks] != 0} {set_max_delay -reset_path -datapath_only -from $clocks -to $clocks 1.0}
}

proc emuflow_load_fixed_io_map {path} {
  # Bound the cache to physical word lanes, and reset it for each export/design.
  set ::emuflow_fixed_io_cache [dict create]
  set result [dict create]
  if {$path eq ""} {return $result}
  set stream [open $path r]
  if {[gets $stream header] < 0 || $header ni [list "logical_hex\tphysical_hex" "logical_hex\tkind\tphysical_hex\tanchor_hex"]} {
    error "invalid fixed I/O timing map header"
  }
  while {[gets $stream row] >= 0} {
    set fields [split $row "\t"]
    set kind port
    set anchor ""
    if {$header eq "logical_hex\tphysical_hex" && [llength $fields] == 2} {
      lassign $fields logical physical
    } elseif {$header ne "logical_hex\tphysical_hex" && [llength $fields] == 4} {
      lassign $fields logical kind physical anchor
      if {$kind ni {word-tx word-rx} || $anchor eq ""} {error "invalid native word timing boundary kind/anchor"}
      set anchor [encoding convertfrom utf-8 [binary format H* $anchor]]
    } else {error "invalid fixed I/O timing map record"}
    set logical [encoding convertfrom utf-8 [binary format H* $logical]]
    set physical [encoding convertfrom utf-8 [binary format H* $physical]]
    if {$logical eq "" || $physical eq "" || [dict exists $result $logical]} {
      error "invalid or duplicate fixed I/O timing map"
    }
    dict set result $logical [list $kind $physical $anchor]
  }
  close $stream
  return $result
}

proc emuflow_exact_hier_pin {name} {
  set pin [get_pins -quiet -hier [list $name]]
  if {[llength $pin] == 1 && [get_property NAME $pin] eq $name} {return $pin}
  set separator [string last / $name]
  if {$separator <= 0} {error "native word boundary lacks a hierarchical owner: $name"}
  set parent [string range $name 0 [expr {$separator-1}]]
  set cells [get_cells -quiet -hier -filter "NAME == $parent"]
  if {[llength $cells] != 1} {error "native word boundary owner is absent: $parent"}
  foreach candidate [get_pins -quiet -of_objects $cells] {
    if {[get_property NAME $candidate] eq $name} {return $candidate}
  }
  error "native word boundary pin is absent: $name"
}

proc emuflow_fixed_io_object {mapping logical} {
  global emuflow_fixed_io_cache
  if {![info exists emuflow_fixed_io_cache]} {set emuflow_fixed_io_cache [dict create]}
  set key [dict get $mapping $logical]
  if {[dict exists $emuflow_fixed_io_cache $key]} {return [dict get $emuflow_fixed_io_cache $key]}
  lassign [dict get $mapping $logical] kind name anchor
  if {$kind eq "port"} {
    set object [get_ports -quiet [list $name]]
  } elseif {$kind in {word-tx word-rx}} {
    if {$kind eq "word-tx"} {
      # A shared payload net may feed several serializer lanes. Resolve this
      # lane through its fixed package pad, rather than choosing any sink of
      # that aliased logical net or relying on generated primitive names.
      # The source-sealed word plan/recipe binds this pad and serial bit to
      # the parallel word. Native input hierarchy pins are not identities:
      # synthesis can fold duplicate inputs and tie their unused pins low,
      # while the actual serializer D bits retain the correct live net.
      if {![regexp {\[([0-7])\]$} $name _ bit]} {error "invalid native TX word bit"}
      set pad [get_ports -quiet [list $anchor]]
      if {[llength $pad] != 1} {error "native TX lane pad is absent: $anchor"}
      set pad_nets [get_nets -quiet -segments -of_objects $pad]
      set buffers [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects $pad_nets] -filter {REF_NAME == OBUFDS}]
      if {[llength $buffers] != 1} {error "native TX lane lacks its unique OBUFDS: $anchor"}
      set input [get_pins -quiet -of_objects $buffers -filter {REF_PIN_NAME == I}]
      set serial [get_nets -quiet -segments -of_objects $input]
      set drivers [get_pins -quiet -leaf -of_objects $serial -filter {DIRECTION == OUT}]
      set serializers [get_cells -quiet -of_objects $drivers -filter {REF_NAME == TX_BITSLICE}]
      if {[llength $serializers] != 1} {error "native TX lane lacks its unique TX_BITSLICE: $anchor"}
      set object [list]
      foreach pin [get_pins -quiet -of_objects $serializers -filter {DIRECTION == IN}] {
        if {[get_property REF_PIN_NAME $pin] eq [format {D[%d]} $bit]} {lappend object $pin}
      }
    } else {
      set hierarchical [emuflow_exact_hier_pin $name]
      set nets [get_nets -quiet -segments -of_objects $hierarchical]
      set object [get_pins -quiet -leaf -of_objects $nets -filter {DIRECTION == OUT}]
      if {[llength $object] == 1} {
        set cell [get_cells -quiet -of_objects $object]
        if {[get_property REF_NAME $cell] ne "FDRE" || [get_property REF_PIN_NAME $object] ne "Q"} {
          error "native RX word boundary is not its registered alignment output: $logical"
        }
      }
    }
    if {[llength $object] == 1} {
      set bank [lindex [split $name /] 0]
      if {[string first "$bank/" [get_property NAME $object]] != 0} {
        error "native word boundary leaf has a different bank: $logical"
      }
    }
  } else {error "unsupported fixed I/O timing object kind: $kind"}
  if {[llength $object] != 1} {error "fixed I/O boundary is absent or ambiguous: $logical"}
  if {[dict size $emuflow_fixed_io_cache] >= 16384} {error "fixed I/O resolution exceeds device boundary scope"}
  dict set emuflow_fixed_io_cache $key $object
  return $object
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
