# Engine-owned launch facts for one canonical architectural pin at a time.
# The caller updates timing once, then queries required pins without selecting
# a representative endpoint/path or inventing a phase for an absent tag.
proc emuflow_launch_clock_edges {pin allowed_clocks} {
  if {[llength $allowed_clocks] == 0 || [$pin is_hierarchical]} {
    error "launch clock query requires a leaf pin and explicit clock identities"
  }
  set name [get_full_name $pin]
  set facts [dict create]
  foreach vertex [$pin vertices] {
    if {$vertex eq "NULL"} {continue}
    foreach transition {rise fall} {
      set iterator [$vertex path_iterator $transition max]
      try {
        while {[$iterator has_next]} {
          set path [$iterator next]
          if {[$path is_clock]} {continue}
          set start [$path start_path]
          if {$start eq "NULL" || [get_full_name [$start pin]] ne $name} {
            error "launch clock query is not an architectural startpoint: $name"
          }
          set edge [$path source_clk_edge]
          if {$edge eq "NULL"} {error "architectural launch has no clock edge: $name"}
          set clock [$edge clock]
          if {$clock eq "NULL"} {error "architectural launch has no clock: $name"}
          set clock_name [get_property $clock name]
          if {[lsearch -exact $allowed_clocks $clock_name] < 0} {
            error "architectural launch uses an undeclared clock: $name $clock_name"
          }
          set period [sta::time_sta_ui [$clock period]]
          set phase [sta::time_sta_ui [$edge time]]
          if {!($period > 0 && abs($period) < Inf && abs($phase) < Inf)} {
            error "architectural launch clock facts are nonfinite or invalid: $name"
          }
          # Rise/fall data transitions and analysis tags may share one source
          # edge. Preserve distinct clock/phase facts, not duplicate path tags.
          dict set facts [list $clock_name $period $phase] 1
          if {[dict size $facts] > 4096} {error "architectural launch clock set exceeds the supported bound"}
        }
      } finally {$iterator finish}
    }
  }
  if {[dict size $facts] == 0} {error "architectural launch has no data-clock facts: $name"}
  return [lsort [dict keys $facts]]
}
