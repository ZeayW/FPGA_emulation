# Exact native TX observations with bounded, source-wise endpoint batches.
source [file join [file dirname [info script]] native_word_timing.tcl]

proc emuflow_native_word_tx_group {sources stream phase key pairs} {
  lassign $key kind source edge
  set option -from
  switch -- $kind {
    dut-clock {set option -${edge}_from;set starts [get_clocks -quiet [list $source]]}
    rx {set starts [dict get $sources rx $source]}
    infrastructure {set starts [dict get $sources infrastructure_clock_pins $source]}
    default {error "unknown native TX batch source"}
  }
  set targets [dict create]
  foreach pair $pairs {
    lassign $pair query target
    # The resolver returns a one-pin collection. Flatten its members before
    # constructing the batched collection; a list of collections is not a
    # Vivado pin collection and can silently yield no timing paths.
    foreach pin $target {dict set targets [get_property NAME $pin] $pin}
  }
  # -nworst 1 supplies the worst path for each endpoint. The global path
  # bound covers every distinct requested endpoint, so no timed endpoint is
  # discarded in favor of repeated paths to a different endpoint.
  set paths [get_timing_paths -sort_by slack -max_paths [dict size $targets] \
      -nworst 1 $option $starts -to [dict values $targets]]
  set selected [dict create]
  set duplicate 0
  foreach path $paths {
    set endpoint [get_property ENDPOINT_PIN $path]
    if {![dict exists $targets $endpoint]} {error "native TX batch returned an unrequested endpoint"}
    if {[dict exists $selected $endpoint]} {set duplicate 1}
    dict set selected $endpoint $path
  }
  set count 1
  foreach pair $pairs {
    lassign $pair query target
    set endpoint [get_property NAME $target]
    set result [list]
    if {$duplicate} {
      # Retain exact per-endpoint behavior if a vendor version returns more
      # than one path for an endpoint despite -nworst 1.
      set result [get_timing_paths -quiet -max_paths 1 -nworst 1 $option $starts -to $target]
      incr count
    } elseif {[dict exists $selected $endpoint]} {set result [list [dict get $selected $endpoint]]}
    emuflow_native_word_cost_row $stream $phase $query $kind $source $edge $result
  }
  return $count
}

proc emuflow_native_word_tx_batch {sources stream summary phase targets {batch_size 256}} {
  emuflow_word_phase_readonly
  if {![string is entier -strict $batch_size] || $batch_size<1 || $batch_size>4096} {
    error "native TX batch size must be between one and 4096"
  }
  set contexts 0
  set queries 0
  for {set begin 0} {$begin<[llength $targets]} {incr begin $batch_size} {
    set requests [dict create]
    foreach pair [lrange $targets $begin [expr {$begin+$batch_size-1}]] {
      if {[llength $pair]!=2} {error "invalid native TX batch target"}
      lassign $pair query target
      set fanin [emuflow_native_word_fanin $sources $target]
      foreach clock [dict get $sources dut_clocks] {
        foreach edge {rise fall} {
          dict lappend requests [list dut-clock [get_property NAME $clock] $edge] $pair
        }
      }
      dict for {origin pins} [dict get $fanin rx] {dict lappend requests [list rx $origin ""] $pair}
      dict for {name cell} [dict get $fanin infrastructure_cells] {
        dict lappend requests [list infrastructure $name ""] $pair
      }
      foreach kind {ports unclassified} {
        dict for {name object} [dict get $fanin $kind] {
          puts $stream "$phase\t[binary encode hex [encoding convertto utf-8 $query]]\t$kind\t[binary encode hex [encoding convertto utf-8 $name]]\t\tnative-source-context-required\t[join [lrepeat 10 ""] \t]"
        }
      }
      puts $summary "$phase\t[binary encode hex [encoding convertto utf-8 $query]]\t[dict get $fanin native_startpoints]\t[dict size [dict get $fanin rx]]\t[dict size [dict get $fanin dut_cells]]\t[dict size [dict get $fanin infrastructure_cells]]\t[dict size [dict get $fanin ports]]\t[dict size [dict get $fanin unclassified]]"
      incr contexts
    }
    dict for {key pairs} $requests {
      incr queries [emuflow_native_word_tx_group $sources $stream $phase $key $pairs]
    }
    flush $stream
    flush $summary
  }
  return [dict create contexts $contexts timing_queries $queries]
}
