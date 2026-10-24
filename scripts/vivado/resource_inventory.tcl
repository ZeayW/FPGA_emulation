# Count implemented capacity, excluding logical macro wrappers. UltraScale+
# exposes DSP/IO components as INTERNAL primitives and LUT6_2 halves as LEAF.
proc emuflow_placed_sites {cell} {
  set sites [get_sites -quiet -of_objects $cell]
  if {[llength $sites] != 1} {error "primitive lacks exactly one placed site: $cell"}
  return [get_property NAME $sites]
}
proc emuflow_resource_inventory {} {
  set counts [dict create physical_cells 0 infrastructure_cells 0 routed_cells 0 optimization_cells 0 lut 0 ff 0 \
    dsp48 0 carry8 0 uram288 0 ramb18_cells 0 ramb36_cells 0]
  set occupied [dict create]
  foreach cell [get_cells -quiet -hier -filter {IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC}] {
    set level [get_property PRIMITIVE_LEVEL $cell]
    if {$level eq "MACRO"} {continue}
    if {$level ni {LEAF INTERNAL}} {error "unknown primitive level $level: $cell"}
    dict incr counts physical_cells
    set ref [get_property REF_NAME $cell]
    if {[string match BUFG* $ref] || [string match IBUF* $ref] ||
        [string match OBUF* $ref] || $ref eq "DIFFINBUF"} {
      dict incr counts infrastructure_cells
    } elseif {[get_property EMUFLOW_MAPPED $cell] eq "yes"} {
      dict incr counts routed_cells
    } else {
      dict incr counts optimization_cells
    }
    if {[string match LUT* $ref]} {
      set site [emuflow_placed_sites $cell]
      set bel [get_property BEL $cell]
      if {![regexp {^(?:SLICE[LM]\.)?([A-H])[56]LUT$} $bel -> letter]} {error "unknown LUT BEL $bel: $cell"}
      # O5/O6 halves, including separately packed source LUTs, share capacity.
      dict set occupied lut "$site/$letter" 1
    } elseif {[string match FD* $ref]} {
      dict incr counts ff
    } elseif {[string match DSP* $ref]} {
      dict set occupied dsp48 [emuflow_placed_sites $cell] 1
    } elseif {$ref eq "CARRY8"} {
      dict set occupied carry8 [emuflow_placed_sites $cell] 1
    } elseif {[string match URAM* $ref]} {
      dict set occupied uram288 [emuflow_placed_sites $cell] 1
    } elseif {[string match RAMB18* $ref]} {
      dict incr counts ramb18_cells
    } elseif {[string match RAMB36* $ref]} {
      dict incr counts ramb36_cells
    }
  }
  foreach key {lut dsp48 carry8 uram288} {
    if {[dict exists $occupied $key]} {dict set counts $key [dict size [dict get $occupied $key]]}
  }
  return $counts
}
