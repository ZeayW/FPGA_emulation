# Stream source provenance without constraining implementation.
proc emuflow_inventory {path prefix} {
  set stream [open $path w]
  puts $stream "name\tref_name\tsource_origin\ttransformations_hex"
  set count 0
  foreach cell [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}] {
    set name [get_property NAME $cell]
    if {[regexp {[\t\r\n]} $name]} {error "unsupported whitespace in source object name"}
    if {$prefix ne ""} {
      if {[string first "$prefix/" $name] != 0} {error "mapped cell outside partition: $name"}
    }
    set origin [get_property EMUFLOW_SOURCE $cell]
    if {![regexp {^s[0-9]+$} $origin]} {error "invalid source lineage tag on $cell"}
    set transformations "[get_property -quiet OPT_MODIFIED $cell] [get_property -quiet PHYS_OPT_MODIFIED $cell]"
    puts $stream "$name\t[get_property REF_NAME $cell]\t$origin\t[binary encode hex [encoding convertto utf-8 $transformations]]"
    incr count
  }
  close $stream
  return $count
}
