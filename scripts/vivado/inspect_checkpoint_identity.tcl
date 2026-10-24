# Explicit identity diagnostic; no physical completion claim or checkpoint write.
if {$argc != 1} {error "expected INPUT_DCP"}
open_checkpoint [file normalize [lindex $argv 0]]
foreach object [list [current_design] [current_fileset]] {
  puts "EMUFLOW_CHECKPOINT_IDENTITY object=$object class=[get_property CLASS $object]"
  foreach property [list_property $object] {
    if {[regexp -nocase {(^name$|top|part|design_mode|ref_name)} $property]} {
      puts "EMUFLOW_CHECKPOINT_PROPERTY $property=[get_property $property $object]"
    }
  }
}
exit 0
