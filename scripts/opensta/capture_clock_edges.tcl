# Clocked-state capture facts from enabled setup arcs and propagated clock
# tags. No data path is selected: an unselected or constant-data endpoint can
# still have a capture clock. These facts do not apply timing exceptions,
# multicycle budgets, I/O departures, or latch time borrowing for the caller.
proc emuflow_capture_clock_edges {pin allowed_clocks} {
  if {[llength $allowed_clocks] == 0 || [$pin is_hierarchical] || [$pin is_top_level_port]} {
    error "capture clock query requires a leaf state pin and explicit clock identities"
  }
  set name [get_full_name $pin]
  set facts [dict create]
  foreach vertex [$pin vertices] {
    if {$vertex eq "NULL"} {continue}
    set edges [$vertex in_edge_iterator]
    try {
      while {[$edges has_next]} {
        set edge [$edges next]
        if {[$edge role] ne "setup" || [$edge is_disabled_constraint]} {continue}
        foreach arc [$edge timing_arcs] {
          if {[sta::timing_arc_disabled $edge $arc]} {continue}
          set clock_vertex [$edge from]
          set paths [$clock_vertex path_iterator [$arc from_edge] max]
          try {
            while {[$paths has_next]} {
              set path [$paths next]
              try {
                if {![$path is_clock]} {continue}
                set clock_edge [$path source_clk_edge]
                if {$clock_edge eq "NULL"} {error "capture clock tag has no edge: $name"}
                set clock [$clock_edge clock]
                if {$clock eq "NULL"} {error "capture clock tag has no clock: $name"}
                set clock_name [get_property $clock name]
                if {[lsearch -exact $allowed_clocks $clock_name] < 0} {
                  error "capture uses an undeclared clock: $name $clock_name"
                }
                set period [sta::time_sta_ui [$clock period]]
                set phase [sta::time_sta_ui [$clock_edge time]]
                if {!($period > 0 && abs($period) < Inf && abs($phase) < Inf)} {
                  error "capture clock facts are nonfinite or invalid: $name"
                }
                dict set facts [list $clock_name $period $phase] 1
              } finally {$path -delete}
            }
          } finally {$paths finish}
        }
      }
    } finally {$edges finish}
  }
  if {[dict size $facts] == 0} {error "capture has no active clocked setup facts: $name"}
  return [lsort [dict keys $facts]]
}
