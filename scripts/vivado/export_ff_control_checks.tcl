# Bounded device-library probe; no placement or routing is performed.
# Keep only these probe cells intact so every requested mode can be queried.
if {$argc != 2} {error "usage: export_ff_control_checks.tcl PART OUTPUT_TSV"}
lassign $argv part output
if {![regexp {^[A-Za-z0-9_-]+$} $part] || [file exists $output]} {error "invalid part or existing output"}
set_param general.maxThreads 1
set stem "$output.[pid]"
set rtl "$stem.v"
set temporary "$stem.tmp"
proc emuflow_probe_inverted {cell property} {
  set value [get_property $property $cell]
  if {$value in {0 1'b0 1'h0 false FALSE}} {return 0}
  if {$value in {1 1'b1 1'h1 true TRUE}} {return 1}
  error "unsupported probe inversion value: $cell $property=$value"
}
if {[catch {
  set stream [open $rtl {WRONLY CREAT EXCL}]
  puts $stream {module ff_control_probe(input clk,d,ce,sr, output [15:0] q);}
  set index 0
  foreach {type reset parameter} {FDRE R R FDSE S S FDCE CLR CLR FDPE PRE PRE} {
    foreach clock_inverted {0 1} {
      foreach reset_inverted {0 1} {
        set name "ff_${type}_c${clock_inverted}_r${reset_inverted}"
        puts $stream [format {(* DONT_TOUCH = "TRUE" *) %s #(.IS_C_INVERTED(1'b%d),.IS_%s_INVERTED(1'b%d)) %s (.C(clk),.D(d),.CE(ce),.%s(sr),.Q(q[%d]));} $type $clock_inverted $parameter $reset_inverted $name $reset $index]
        incr index
      }
    }
  }
  puts $stream {endmodule}
  close $stream
  create_project -in_memory -part $part
  read_verilog $rtl
  synth_design -top ff_control_probe -part $part -mode out_of_context -flatten_hierarchy none
  create_clock -name clk -period 100 [get_ports clk]
  set_input_delay -max 0 -clock clk [get_ports {d ce sr}]
  set_output_delay -max 0 -clock clk [get_ports q*]
  set fields {DELAY_FAST_MAX_RISE DELAY_FAST_MAX_FALL DELAY_FAST_MIN_RISE DELAY_FAST_MIN_FALL DELAY_SLOW_MAX_RISE DELAY_SLOW_MAX_FALL DELAY_SLOW_MIN_RISE DELAY_SLOW_MIN_FALL DELAY_MAX_RISE DELAY_MAX_FALL DELAY_MIN_RISE DELAY_MIN_FALL}
  set stream [open $temporary {WRONLY CREAT EXCL}]
  puts $stream "schema\temuflow.vivado-ff-control-raw/v2"
  puts $stream "part\t$part"
  puts $stream "vivado_version\t[version -short]"
  puts $stream "unit\tns"
  puts $stream "transition_frame\tlibrary-pin"
  puts $stream [join [concat {cell_type clock_inverted reset_inverted pin kind transitions} $fields] "\t"]
  set count 0
  foreach {type reset kind} {FDRE R setup FDSE S setup FDCE CLR recovery FDPE PRE recovery} {
    foreach clock_inverted {0 1} {
      foreach reset_inverted {0 1} {
        set name "ff_${type}_c${clock_inverted}_r${reset_inverted}"
        set cell [get_cells -quiet $name]
        if {[llength $cell] != 1 || [get_property REF_NAME $cell] ne $type} {error "probe cell changed: $name"}
        if {[emuflow_probe_inverted $cell IS_C_INVERTED] != $clock_inverted ||
            [emuflow_probe_inverted $cell IS_${reset}_INVERTED] != $reset_inverted} {error "probe inversion mode changed: $name"}
        foreach {pin expected} [list CE setup $reset $kind] {
          set selected [list]
          foreach arc [get_timing_arcs -of_objects $cell] {
            if {[get_property FROM_PIN $arc] eq "$name/C" &&
                [get_property TO_PIN $arc] eq "$name/$pin" &&
                [string tolower [get_property TYPE $arc]] eq $expected} {lappend selected $arc}
          }
          if {[llength $selected] != 1} {error "missing or ambiguous device check: $name/$pin"}
          set arc [lindex $selected 0]
          if {[get_property IS_DISABLED $arc] || [get_property IS_USER_DISABLED $arc]} {error "disabled device check: $name/$pin"}
          set transitions [get_property TRANSITION $arc]
          if {[llength $transitions] == 0} {error "device check lacks transitions: $name/$pin"}
          set row [list $type $clock_inverted $reset_inverted $pin $expected [join $transitions ,]]
          foreach field $fields {
            set required_transition [expr {[string match *_RISE $field] ? "r" : "f"}]
            set applicable 0
            foreach transition $transitions {
              if {[string index $transition 1] eq $required_transition} {set applicable 1}
            }
            if {!$applicable} {lappend row "";continue}
            set value [get_property $field $arc]
            if {![string is double -strict $value] || !($value > -Inf && $value < Inf)} {error "non-finite device check: $field"}
            lappend row $value
          }
          puts $stream [join $row "\t"]
          incr count
        }
      }
    }
  }
  close $stream
  if {$count != 32} {error "device control check coverage differs"}
  file rename $temporary $output
  file delete $rtl
} message options]} {
  catch {close $stream}
  catch {file delete $temporary}
  catch {file delete $rtl}
  puts stderr [dict get $options -errorinfo]
  exit 1
}
puts "EMUFLOW_FF_CONTROL_EXPORT status=pass cells=16 checks=32 part=$part output=$output"
exit 0
