# Bounded qualification of Vivado's native macro/leaf representation.
if {$argc != 2} {error "expected INPUT_DCP OUTPUT_TSV"}
open_checkpoint [lindex $argv 0]
set stream [open [lindex $argv 1] w]
puts $stream "reference\tobjects\tname\tproperty\tvalue"
foreach ref {LUT6_2 LUT5 LUT6 DSP48E2 DSP_ALU IBUFDS DIFFINBUF IBUFCTRL RAMB18E2 RAMB36E2} {
  set cells [get_cells -quiet -hier -filter "REF_NAME == $ref"]
  set count [llength $cells]
  if {$count == 0} {puts $stream "$ref\t0\t\t\t";continue}
  set cell [lindex $cells 0]
  set name [get_property NAME $cell]
  set properties [list_property $cell]
  foreach field {IS_PRIMITIVE PRIMITIVE_LEVEL PRIMITIVE_GROUP PRIMITIVE_SUBGROUP PARENT LOC BEL} {
    if {[lsearch -exact $properties $field] >= 0} {
      puts $stream "$ref\t$count\t$name\t$field\t[get_property $field $cell]"
    }
  }
}
close $stream
puts "EMUFLOW_PRIMITIVE_INSPECTION status=pass"
exit 0
