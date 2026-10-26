# OpenSTA 3.1's own Tcl procedures use try/finally. A version string alone
# cannot qualify an executable accidentally linked against Tcl 8.5.
if {[package vcompare [info patchlevel] 8.6] < 0 || [llength [info commands try]] != 1} {
  puts stderr "EMUFLOW_OPENSTA_RUNTIME status=fail requires=Tcl8.6+ actual=[info patchlevel]"
  exit 1
}
try {set emuflow_runtime_probe pass} finally {}
if {[llength [info commands read_liberty]] != 1} {
  puts stderr "EMUFLOW_OPENSTA_RUNTIME status=fail missing=read_liberty"
  exit 1
}
# Engines with the same release string can differ in recovery coverage.
# Exercise the actual native check rather than admitting a version or hash
# allowlist. No arrival constraint is supplied for the asynchronous input.
if {[catch {
  set lib [file tempfile lib_path]
  set netlist [file tempfile netlist_path]
  try {
    puts $lib "library(emuflow_coverage_probe) \173"
    puts $lib {delay_model : table_lookup;
      time_unit : "1ns";
      capacitive_load_unit(1,pf);
      nom_voltage : 1; nom_temperature : 25; nom_process : 1;
      input_threshold_pct_rise : 50; input_threshold_pct_fall : 50;
      output_threshold_pct_rise : 50; output_threshold_pct_fall : 50;
      slew_lower_threshold_pct_rise : 20; slew_upper_threshold_pct_rise : 80;
      slew_lower_threshold_pct_fall : 20; slew_upper_threshold_pct_fall : 80;}
    puts $netlist {module emuflow_coverage_probe(clk, reset, q);
      input clk; input reset; output [3:0] q;}
    set index 0
    set expected {}
    foreach {sense expression} {rising C falling !C} {
      foreach control {clear preset} {
        set name probe_${control}_${sense}
        puts $lib [format {
          cell(%s) {
            ff(IQ,IQN) { clocked_on : "%s"; next_state : "D"; %s : "R"; }
            pin(C) { direction : input; clock : true; }
            pin(D) { direction : input; }
            pin(R) { direction : input;
              timing() { related_pin : "C"; timing_type : recovery_%s;
                rise_constraint(scalar) { values("0.1"); }
                fall_constraint(scalar) { values("0.1"); }
              }
            }
            pin(Q) { direction : output; function : "IQ"; }
          }} $name $expression $control $sense]
        puts $netlist [format {%s %s (.C(clk), .D(1'b0), .R(reset), .Q(q[%d]));} $name $name $index]
        lappend expected $name/R
        incr index
      }
    }
    puts $lib "\175"
    puts $netlist "endmodule"
    close $lib; set lib ""
    close $netlist; set netlist ""
    read_liberty $lib_path
    read_verilog $netlist_path
    link_design emuflow_coverage_probe
    create_clock -name clk -period 10 [get_ports clk]
    set found {}
    foreach failure [sta::check_timing_cmd 0 0 0 0 1 0 0] {
      foreach pin [lrange $failure 1 end] {dict set found $pin 1}
    }
    foreach pin $expected {
      if {![dict exists $found $pin]} {error "unclocked recovery endpoint escaped source coverage: $pin"}
    }
  } finally {
    if {$lib ne ""} {close $lib}
    if {$netlist ne ""} {close $netlist}
    file delete -force $lib_path $netlist_path
  }
} message options]} {
  puts stderr "EMUFLOW_OPENSTA_RUNTIME status=fail source_coverage=$message"
  exit 1
}
puts "EMUFLOW_OPENSTA_RUNTIME status=$emuflow_runtime_probe tcl=[info patchlevel] source_coverage=setup-and-recovery-v1"
exit 0
