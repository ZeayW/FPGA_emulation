# Current indexed Phase6 -> native Vivado implementation. No placement is fixed.
# All tool products are scratch; the Python consumer validates inventories,
# resource budgets, fixed I/O and complete source-bound timing separately.
if {$argc != 15} {
  error "expected PART NETLIST TOP XDC OUT CELLS PREFIX ADAPTER WRAPPER IO_XDC IO_QUERY PLACE ROUTE DUT_CLOCKS THREADS"
}
proc emuflow_decode {value} {
  return [encoding convertfrom utf-8 [binary decode hex $value]]
}
proc emuflow_inventory {path prefix expected} {
  set stream [open $path w]
  puts $stream "name\tref_name"
  set count 0
  foreach cell [get_cells -quiet -hier -filter {EMUFLOW_MAPPED == yes}] {
    set name [get_property NAME $cell]
    if {$prefix ne ""} {
      if {[string first "$prefix/" $name] != 0} {error "mapped cell outside partition: $name"}
      set name [string range $name [expr {[string length $prefix]+1}] end]
    }
    puts $stream "$name\t[get_property REF_NAME $cell]"
    incr count
  }
  close $stream
  if {$count != $expected} {error "mapped cell count $count differs from $expected"}
}
proc emuflow_domain_metrics {from to} {
  if {[llength $from] == 0 || [llength $to] == 0} {return {0.0 0.0}}
  set paths [get_timing_paths -quiet -max_paths 1 -nworst 1 -from $from -to $to]
  if {[llength $paths] == 0} {return {0.0 0.0}}
  return [list [get_property SLACK $paths] [get_property DATAPATH_DELAY $paths]]
}
if {[catch {
  lassign $argv part netlist top timing_xdc out expected prefix adapter wrapper io_xdc io_query place_directive route_directive dut_count threads
  file mkdir $out
  set_param general.maxThreads $threads
  if {[llength [get_parts -quiet $part]] != 1} {error "target part is not installed: $part"}
  create_project -in_memory -part $part emuflow_indexed_partition
  read_verilog $netlist
  if {$adapter ne ""} {read_verilog -sv $adapter}
  if {$wrapper ne ""} {read_verilog -sv $wrapper}
  read_xdc $timing_xdc
  if {$io_xdc ne ""} {read_xdc $io_xdc}
  synth_design -top $top -part $part -flatten_hierarchy none -mode out_of_context
  if {[llength [get_cells -quiet -hier -filter {IS_BLACKBOX == 1}]] != 0} {error "unresolved black boxes"}
  emuflow_inventory "$out/mapped_cells.tsv" $prefix $expected
  set dut_clocks [get_clocks -quiet emuflow_dut_clk*]
  set fabric [get_clocks -quiet emuflow_fabric_clk]
  if {[llength $dut_clocks] != $dut_count || [llength $fabric] != 1} {
    error "runtime clock coverage differs from Phase6"
  }
  # Virtual pads represent an already scheduled board boundary. Their local
  # physical delay is measured separately and is never the board-link model.
  if {$io_query ne ""} {
    set stream [open $io_query r]
    gets $stream header
    while {[gets $stream row] >= 0} {
      if {$row eq ""} {continue}
      lassign [split $row "\t"] direction p n p_pin n_pin standard
      foreach hex [list $p $n] {
        set port [get_ports -quiet [list [emuflow_decode $hex]]]
        if {[llength $port] != 1} {error "fixed differential port is absent"}
        if {$direction eq "tx"} {
          set_output_delay 0.0 -clock $fabric $port
        } else {
          set_input_delay 0.0 -clock $fabric $port
        }
      }
    }
    close $stream
  }
  write_checkpoint -force "$out/synthesized.dcp"
  opt_design
  place_design -directive $place_directive
  phys_opt_design
  write_checkpoint -force "$out/placed.dcp"
  route_design -directive $route_directive
  write_checkpoint -force "$out/routed.dcp"
  emuflow_inventory "$out/routed_mapped_cells.tsv" $prefix $expected
  report_route_status -file "$out/route_status.rpt"
  report_drc -file "$out/drc.rpt"
  report_timing_summary -file "$out/timing_summary.rpt"
  report_utilization -hierarchical -file "$out/utilization.rpt"
  report_utilization -slr -file "$out/slr_utilization.rpt"
  report_design_analysis -congestion -file "$out/congestion.rpt"
  set errors 0
  set warnings 0
  foreach violation [get_drc_violations -quiet] {
    set severity [get_property SEVERITY $violation]
    if {$severity eq "Error" || $severity eq "Critical Warning"} {incr errors} else {incr warnings}
  }
  if {$errors != 0} {error "post-route DRC has $errors blocking violations"}
  set incomplete [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
  if {[llength $incomplete] != 0} {error "post-route design contains incomplete nets"}
  set pairs 0
  if {$io_query ne ""} {
    set stream [open $io_query r]
    gets $stream header
    while {[gets $stream row] >= 0} {
      if {$row eq ""} {continue}
      set fields [split $row "\t"]
      if {[llength $fields] != 6} {error "invalid fixed I/O query"}
      lassign $fields direction p n p_pin n_pin standard
      # UltraScale+ expands IBUFDS into DIFFINBUF plus IBUFCTRL. The P/N
      # package pads must still meet the same physical differential primitive.
      set expected_ref [expr {$direction eq "tx" ? "REF_NAME == OBUFDS" : "REF_NAME == IBUFDS || REF_NAME == DIFFINBUF"}]
      set pair_cell ""
      foreach port_hex [list $p $n] pin_hex [list $p_pin $n_pin] {
        set name [emuflow_decode $port_hex]
        set port [get_ports -quiet [list $name]]
        if {[llength $port] != 1 || [get_property PACKAGE_PIN $port] ne [emuflow_decode $pin_hex] ||
            [get_property IOSTANDARD $port] ne [emuflow_decode $standard]} {
          error "fixed differential port constraint mismatch: $name"
        }
        set nets [get_nets -quiet -of_objects $port]
        set cells [get_cells -quiet -of_objects [get_pins -quiet -leaf -of_objects $nets] -filter $expected_ref]
        if {[llength $cells] != 1} {error "fixed pad $name lacks its differential primitive"}
        set cell_name [get_property NAME $cells]
        if {$pair_cell ne "" && $pair_cell ne $cell_name} {error "P/N pads use different primitives"}
        set pair_cell $cell_name
      }
      incr pairs
    }
    close $stream
  }
  set dut_metrics [emuflow_domain_metrics $dut_clocks $dut_clocks]
  set fabric_metrics [emuflow_domain_metrics $fabric $fabric]
  set cross_metrics [emuflow_domain_metrics $fabric $dut_clocks]
  set paths [get_timing_paths -quiet -max_paths 1 -nworst 1]
  if {[llength $paths] != 1} {error "implemented design has no analyzed timing path"}
  set physical [get_cells -quiet -hier -filter {IS_PRIMITIVE && REF_NAME != GND && REF_NAME != VCC}]
  set infrastructure 0
  foreach cell $physical {
    set ref [get_property REF_NAME $cell]
    if {[string match BUFG* $ref] || [string match IBUF* $ref] || [string match OBUF* $ref] || $ref eq "DIFFINBUF"} {incr infrastructure}
  }
  set optimization [expr {[llength $physical] - $expected - $infrastructure}]
  if {$optimization < 0} {error "mapped physical accounting is inconsistent"}
  set metrics [open "$out/implementation_metrics.tsv" w]
  puts $metrics "metric\tvalue"
  foreach {key value} [list \
    vivado_version [version -short] part $part mapped_cells $expected \
    physical_cells [llength $physical] infrastructure_cells $infrastructure optimization_cells $optimization \
    unrouted_nets 0 drc_violations $errors drc_warnings $warnings virtual_io_pairs $pairs \
    dut_clock_count [llength $dut_clocks] \
    fabric_period_ns [get_property PERIOD $fabric] \
    wns_ns [get_property SLACK $paths] critical_path_ns [get_property DATAPATH_DELAY $paths] \
    dut_wns_ns [lindex $dut_metrics 0] dut_delay_ns [lindex $dut_metrics 1] \
    fabric_wns_ns [lindex $fabric_metrics 0] fabric_delay_ns [lindex $fabric_metrics 1] \
    fabric_to_dut_wns_ns [lindex $cross_metrics 0] fabric_to_dut_delay_ns [lindex $cross_metrics 1]] {
    puts $metrics "$key\t$value"
  }
  foreach {key pattern} {lut LUT* ff FD* dsp48 DSP48* carry8 CARRY8 uram288 URAM288* ramb18_cells RAMB18* ramb36_cells RAMB36*} {
    set cells [get_cells -quiet -hier -filter "IS_PRIMITIVE && REF_NAME =~ $pattern"]
    puts $metrics "$key\t[llength $cells]"
  }
  close $metrics
  puts "EMUFLOW_INDEXED_VIVADO status=pass part=$part fixed_pairs=$pairs"
} message options]} {
  puts stderr "EMUFLOW_INDEXED_VIVADO status=fail message=$message"
  puts stderr [dict get $options -errorinfo]
  exit 1
}
exit 0
