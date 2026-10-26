# Read-only engine facts, not a physical timing or operating-frequency model.
# Values from SWIG are in STA units; convert once to the current time unit.
proc emuflow_clock_budget {path_end} {
  set source_edge [$path_end source_clk_edge]
  set target_edge [$path_end target_clk_edge]
  if {$source_edge eq "NULL" || $target_edge eq "NULL"} {
    error "clock budget requires explicit source and capture clock edges"
  }
  set source_clock [$source_edge clock]
  set target_clock [$target_edge clock]
  set launch [sta::time_sta_ui [$source_edge time]]
  set capture [sta::time_sta_ui [$target_edge time]]
  set offset [sta::time_sta_ui [$path_end source_clk_offset]]
  set target_time [sta::time_sta_ui [$path_end target_clk_time]]
  set multicycle [sta::time_sta_ui [$path_end target_clk_mcp_adjustment]]
  set arrival [sta::time_sta_ui [$path_end data_arrival_time]]
  set required [sta::time_sta_ui [$path_end data_required_time]]
  return [dict create source_clock [get_property $source_clock name] \
    target_clock [get_property $target_clock name] \
    source_period_ns [sta::time_sta_ui [$source_clock period]] \
    target_period_ns [sta::time_sta_ui [$target_clock period]] \
    launch_edge_ns $launch capture_edge_ns $capture source_offset_ns $offset \
    target_cycle_time_ns $target_time multicycle_adjustment_ns $multicycle \
    edge_window_ns [expr {$target_time+$multicycle-$launch}] \
    launch_relative_arrival_ns [expr {$arrival-$offset-$launch}] \
    arrival_ns $arrival required_ns $required \
    slack_ns [sta::time_sta_ui [$path_end slack]] \
    check_margin_ns [sta::time_sta_ui [$path_end margin]] \
    source_latency_ns [sta::time_sta_ui [$path_end source_clk_latency]] \
    target_clock_delay_ns [sta::time_sta_ui [$path_end target_clk_delay]] \
    clock_skew_ns [sta::time_sta_ui [$path_end clk_skew]] \
    output_delay [$path_end is_output_delay] path_delay [$path_end is_path_delay] \
    latch_check [$path_end is_latch_check]]
}
