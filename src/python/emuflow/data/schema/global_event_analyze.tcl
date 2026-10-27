proc analyze {} {
  puts "global STA: read model"
  read_liberty global_timing.lib
  read_verilog global_timing.v
  link_design global_timing
  puts "global STA: read constraints"
  # These generated constraints use only exact internal port names. OpenSTA's
  # get_ports scans every port per call; bind through the indexed cell API.
  # Stream the same portable SDC, without writing another constraint copy.
  set ::emuflow_top [sta::top_instance]
  set ::emuflow_cell [$::emuflow_top cell]
  set constraints [open global_timing.sdc r]
  while {[gets $constraints line] >= 0} {
    uplevel #0 [string map [list {[get_ports } {[$::emuflow_cell find_port }] $line]
  }
  close $constraints
  set out [open measurements.tsv w]
  puts $out "endpoint\tarrival_ns\trequired_ns\tslack_ns"
  puts "global STA: propagate timing"
  find_timing
  puts "global STA: query endpoint scalars"
  puts "global STA: serialize checks"
  for {set i 0} {$i < @CHECK_COUNT@} {incr i} {
    set pin [$::emuflow_top find_pin o$i]
    if {$pin == "NULL"} { error "missing global STA endpoint o$i" }
    set p "NULL"
    foreach vertex [$pin vertices] {
      if {$vertex != "NULL"} {
        set candidate [sta::vertex_worst_slack_path $vertex max]
        if {$candidate != "NULL"} { set p $candidate; break }
      }
    }
    if {$p == "NULL"} { error "unconstrained global STA endpoint o$i" }
    # Path scalar APIs use seconds. Querying the known endpoint directly is
    # linear and avoids materializing/sorting every PathEnd in one giant list.
    set arrival [expr {[$p arrival] * 1.0e9}]
    # Path.required/slack are vertex-tag values and omit the output-delay
    # PathEnd constraint. endpoint_slack includes that constraint and returns
    # user time units.  All generated checks belong to the epoch path group;
    # an empty group name matches no PathEnd in OpenSTA 3.1.
    set slack [sta::endpoint_slack $pin "epoch" max]
    set required [expr {$arrival + $slack}]
    puts $out "o$i\t$arrival\t$required\t$slack"
  }
  close $out
}
if {[catch {analyze} message]} { puts stderr $message; exit 2 }
exit 0
