# Stream source provenance without constraining implementation.
proc emuflow_inventory {path prefix {placed 0}} {
  set stream [open $path w]
  puts $stream "name\tref_name\tsource_origin\ttransformations_hex\tsite\tbel\ttile_column\ttile_row\tprimitive_level"
  set count 0
  set geometry_cache [dict create]
  foreach cell [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}] {
    set name [get_property NAME $cell]
    if {[regexp {[\t\r\n]} $name]} {error "unsupported whitespace in source object name"}
    if {$prefix ne ""} {
      if {[string first "$prefix/" $name] != 0} {error "mapped cell outside partition: $name"}
    }
    set origin [get_property EMUFLOW_SOURCE $cell]
    if {![regexp {^s[0-9]+$} $origin]} {error "invalid source lineage tag on $cell"}
    set transformations "[get_property -quiet OPT_MODIFIED $cell] [get_property -quiet PHYS_OPT_MODIFIED $cell]"
    set ref [get_property REF_NAME $cell]
    set geometry {{} {} {} {}}
    set level ""
    if {$placed} {
      set level [get_property PRIMITIVE_LEVEL $cell]
      if {$level ni {LEAF INTERNAL MACRO}} {error "unknown mapped primitive level: $cell"}
    }
    if {$placed && $ref ni {GND VCC} && $level ne "MACRO"} {
      set sites [get_sites -quiet -of_objects $cell]
      if {[llength $sites] != 1} {error "mapped primitive lacks exactly one placed site: $cell"}
      set site [get_property NAME $sites]
      set bel [get_property BEL $cell]
      if {![dict exists $geometry_cache $site]} {
        set tiles [get_tiles -quiet -of_objects $sites]
        if {[llength $tiles] != 1} {error "placed site lacks exactly one native tile: $cell"}
        dict set geometry_cache $site [list [get_property COLUMN $tiles] [get_property ROW $tiles]]
      }
      lassign [dict get $geometry_cache $site] column row
      if {$site eq "" || $bel eq "" || [regexp {[\t\r\n]} "$site$bel"] ||
          ![string is integer -strict $column] || ![string is integer -strict $row] ||
          $column < 0 || $row < 0} {error "mapped primitive has invalid native placement: $cell"}
      set geometry [list $site $bel $column $row]
    }
    puts $stream "$name\t$ref\t$origin\t[binary encode hex [encoding convertto utf-8 $transformations]]\t[join $geometry \t]\t$level"
    incr count
  }
  close $stream
  return $count
}
