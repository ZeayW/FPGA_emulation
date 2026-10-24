# Export routed endpoint-to-endpoint DUT logic delays for Phase 7C.

if {$argc < 3 || $argc > 5} {
  error "usage: export_logic_segment_timing.tcl INPUT_DCP QUERY_TSV OUTPUT_TSV ?HIERARCHY_PREFIX? ?allow-missing?"
}
set input_dcp [file normalize [lindex $argv 0]]
set query_path [file normalize [lindex $argv 1]]
set output_path [file normalize [lindex $argv 2]]
set hierarchy_prefix ""
if {$argc >= 4} {
  set hierarchy_prefix [string trimright [lindex $argv 3] "/"]
}
source [file join [file dirname [info script]] timing_contract.tcl]
set allow_missing false
set fixed_io_map [dict create]
if {$argc == 5} {
  if {[lindex $argv 4] eq "allow-missing"} {
    set allow_missing true
  } else {
    set fixed_io_map [emuflow_load_fixed_io_map [lindex $argv 4]]
  }
}

proc emuflow_hex_decode {value} {
  return [encoding convertfrom utf-8 [binary decode hex $value]]
}
proc emuflow_hex_encode {value} {
  return [binary encode hex [encoding convertto utf-8 $value]]
}
proc emuflow_resolve_object {kind name prefix {resolved_hex ""}} {
  global fixed_io_map
  if {$resolved_hex ne ""} {
    if {$kind ne "pin"} {error "lineage candidates require primitive pins"}
    set result [list]
    foreach candidate [split $resolved_hex ,] {
      set actual [emuflow_hex_decode $candidate]
      set pin [emuflow_resolve_object pin $actual ""]
      if {[get_property NAME $pin] ne $actual} {error "lineage pin identity disagrees: $actual"}
      # get_pins returns an object collection, not an individual object.
      # Flatten collections rather than nesting them in a Tcl list.
      set result [concat $result $pin]
    }
    return $result
  }
  set physical_name $name
  if {$prefix ne ""} {
    set physical_name "$prefix/$name"
  }
  if {$kind eq "pin"} {
    set object [get_pins -quiet -hier [list $physical_name]]
    # Vivado does not consistently resolve a leaf pin by its full hierarchical
    # name after a routed checkpoint is reopened, even when the parent cell and
    # pin are both present. Resolve the exact parent cell first as a stable
    # fallback; this is required for inferred RAMB clock endpoints.
    if {[llength $object] == 0} {
      set separator [string last "/" $physical_name]
      if {$separator > 0} {
        set cell_name [string range $physical_name 0 [expr {$separator - 1}]]
        set pin_name [string range $physical_name [expr {$separator + 1}] end]
        set cell [get_cells -quiet -hier -filter "NAME == $cell_name"]
        if {[llength $cell] == 1} {
          set candidates [list]
          foreach candidate [get_pins -quiet -of_objects $cell] {
            set candidate_name [get_property NAME $candidate]
            set candidate_separator [string last "/" $candidate_name]
            set candidate_leaf [string range $candidate_name \
              [expr {$candidate_separator + 1}] end]
            if {$candidate_leaf eq $pin_name} {
              lappend candidates $candidate
            }
          }
          set object $candidates
        }
      }
    }
  } elseif {$kind eq "port"} {
    set lookup $name
    if {[dict exists $fixed_io_map $name]} {set lookup [dict get $fixed_io_map $name]}
    set object [get_ports -quiet [list $lookup]]
    if {[llength $object] == 0 && $prefix ne ""} {
      set object [get_pins -quiet -hier [list $physical_name]]
    }
  } else {
    error "invalid timing object kind $kind"
  }
  if {[llength $object] != 1} {
    error "timing object $kind $physical_name resolves to [llength $object] objects"
  }
  return $object
}

open_checkpoint $input_dcp
emuflow_use_data_path_constraints
set input [open $query_path r]
gets $input header
set legacy_header "endpoint_hex\tkind\tstart_kind\tstart_object_hex\tend_kind\tend_object_hex"
set cone_header "$legacy_header\tcone_anchor_kind\tcone_anchor_object_hex"
set resolved_header "$cone_header\tresolved_start_pins_hex\tresolved_end_pins_hex\tresolved_cone_pins_hex"
if {$header ni [list $legacy_header $cone_header $resolved_header]} {
  error "invalid logic segment timing query header"
}
set cone_queries [expr {$header ne $legacy_header}]
set resolved_queries [expr {$header eq $resolved_header}]

set output [open $output_path w]
puts $output "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\tmeasurement\tactual_start_object_hex\tactual_end_object_hex"
set emitted 0
set missing 0
while {[gets $input line] >= 0} {
  if {$line eq ""} {
    continue
  }
  set fields [split $line "\t"]
  set expected_fields [expr {$resolved_queries ? 11 : ($cone_queries ? 8 : 6)}]
  if {[llength $fields] != $expected_fields} {
    error "malformed logic segment timing query row"
  }
  set endpoint_hex [lindex $fields 0]
  set kind [lindex $fields 1]
  set start_kind [lindex $fields 2]
  set start_name [emuflow_hex_decode [lindex $fields 3]]
  set end_kind [lindex $fields 4]
  set end_name [emuflow_hex_decode [lindex $fields 5]]
  set cone_kind ""
  set cone_name ""
  if {$cone_queries} {
    set cone_kind [lindex $fields 6]
    set cone_hex [lindex $fields 7]
    if {$cone_kind ne "" || $cone_hex ne ""} {
      if {$cone_kind eq "" || $cone_hex eq ""} {
        error "logic segment cone anchor is incomplete"
      }
      set cone_name [emuflow_hex_decode $cone_hex]
    }
  }
  set start_hint ""
  set end_hint ""
  set cone_hint ""
  if {$resolved_queries} {
    lassign [lrange $fields 8 10] start_hint end_hint cone_hint
  }
  set start_object [emuflow_resolve_object $start_kind $start_name $hierarchy_prefix $start_hint]
  set end_object [emuflow_resolve_object $end_kind $end_name $hierarchy_prefix $end_hint]
  set measurement "endpoint-exact"
  if {[llength $start_object] > 1 || [llength $end_object] > 1} {
    set measurement "optimized-endpoint-collection-upper-bound"
  }
  set start_is_hier_pin [expr {$start_kind eq "port" && \
    [get_property CLASS $start_object] eq "pin"}]
  set end_is_hier_pin [expr {$end_kind eq "port" && \
    [get_property CLASS $end_object] eq "pin"}]
  if {$start_is_hier_pin && $end_is_hier_pin} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -through $start_object -through $end_object]
  } elseif {$start_is_hier_pin} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -through $start_object -to $end_object]
  } elseif {$end_is_hier_pin} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -from $start_object -through $end_object]
  } else {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -from $start_object -to $end_object]
  }
  # Runtime clock-domain constraints can intentionally false-path a
  # fabric-to-DUT logic segment.  The delay is still required by Phase 7C, so
  # query the user-ignored physical datapath explicitly without turning it
  # into a local Vivado closure requirement.
  if {[llength $paths] == 0} {
    if {$start_is_hier_pin && $end_is_hier_pin} {
      set paths [get_timing_paths -quiet -user_ignored \
        -max_paths 1 -nworst 1 -through $start_object -through $end_object]
    } elseif {$start_is_hier_pin} {
      set paths [get_timing_paths -quiet -user_ignored \
        -max_paths 1 -nworst 1 -through $start_object -to $end_object]
    } elseif {$end_is_hier_pin} {
      set paths [get_timing_paths -quiet -user_ignored \
        -max_paths 1 -nworst 1 -from $start_object -through $end_object]
    } else {
      set paths [get_timing_paths -quiet -user_ignored \
        -max_paths 1 -nworst 1 -from $start_object -to $end_object]
    }
  }
  # A preserved EmuFlow pin can be physically connected while not being a
  # legal Vivado timing startpoint/endpoint (notably a synchronous RAM output
  # or a hierarchy-crossing combinational pin).  Constrain the same ordered
  # pair as through-points and let Vivado recover the enclosing sequential
  # path.  This fallback is also required when the end object is a hierarchy
  # pin: -from a RAM output is illegal even though an ordered through/through
  # query has one routed RAM-clock-to-interface path.
  if {[llength $paths] == 0} {
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -through $start_object -through $end_object]
  }
  if {[llength $paths] == 0} {
    set paths [get_timing_paths -quiet -user_ignored \
      -max_paths 1 -nworst 1 -through $start_object -through $end_object]
  }
  # A provider-neutral hard-macro timing model can conservatively expose an
  # input-to-output arc that the mapped vendor primitive proves is bitwise
  # impossible (for example, multiplier input bit 15 to product bit 0).  The
  # original endpoint pair then has no physical path.  Bound the real launch
  # cone instead by forcing the path through the unique preserved driver of
  # the globally cut net.  This excludes transport-mux control paths and is a
  # routed upper bound, but is deliberately not labeled endpoint-exact.
  if {[llength $paths] == 0 && $cone_name ne ""} {
    set cone_object [emuflow_resolve_object \
      $cone_kind $cone_name $hierarchy_prefix $cone_hint]
    set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 \
      -through $cone_object -through $end_object]
    if {[llength $paths] == 0} {
      set paths [get_timing_paths -quiet -user_ignored \
        -max_paths 1 -nworst 1 \
        -through $cone_object -through $end_object]
    }
    if {[llength $paths] == 1} {
      set measurement "cut-net-cone-upper-bound"
    }
  }
  if {[llength $paths] != 1} {
    if {$allow_missing} {
      puts "EMUFLOW_LOGIC_SEGMENT_MISSING endpoint=[emuflow_hex_decode $endpoint_hex] kind=$kind"
      incr missing
      continue
    }
    error "logic segment [emuflow_hex_decode $endpoint_hex] has no routed timing path"
  }
  emuflow_validate_segment_extent [lindex $paths 0] $start_object $end_object $measurement
  set delay [get_property DATAPATH_DELAY [lindex $paths 0]]
  if {$delay eq ""} {
    error "logic segment [emuflow_hex_decode $endpoint_hex] has no datapath delay"
  }
  set actual_start [get_property STARTPOINT_PIN [lindex $paths 0]]
  set actual_end [get_property ENDPOINT_PIN [lindex $paths 0]]
  if {$actual_start eq "" || $actual_end eq ""} {
    error "logic segment [emuflow_hex_decode $endpoint_hex] has no physical endpoint trace"
  }
  puts $output "$endpoint_hex\t$kind\t$delay\t[emuflow_hex_encode $start_name]\t[emuflow_hex_encode $end_name]\t$measurement\t[emuflow_hex_encode $actual_start]\t[emuflow_hex_encode $actual_end]"
  incr emitted
}
close $output
close $input
puts "EMUFLOW_LOGIC_SEGMENT_TIMING status=pass segments=$emitted missing=$missing output=$output_path"
