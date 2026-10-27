# Extract current physical timing without changing the retained implementation.
if {$argc != 1} {error "expected ROUTED_EXPORT_SESSION_CONFIG"}
set emuflow_script_directory [file dirname [info script]]
source [file join $emuflow_script_directory native_session.tcl]
source [file join $emuflow_script_directory source_inventory.tcl]
if {[catch {
  source [lindex $argv 0]
  set_param general.maxThreads 2
  open_checkpoint $emuflow_routed_checkpoint
  if {[get_property PART [current_design]] ne $emuflow_expected_part} {
    error "retained routed checkpoint belongs to another part"
  }
  emuflow_validate_clock_contract $emuflow_clock_contract $emuflow_allow_generated_clocks
  report_route_status -file "$emuflow_session_out/route_status.rpt"
  report_drc -file "$emuflow_session_out/drc.rpt"
  report_timing_summary -file "$emuflow_session_out/timing_summary.rpt"
  report_clocks -file "$emuflow_session_out/clocks.rpt"
  set incomplete [get_nets -quiet -hier -filter {ROUTE_STATUS == UNROUTED || ROUTE_STATUS == PARTIAL || ROUTE_STATUS == CONFLICTS}]
  if {[llength $incomplete]} {error "retained checkpoint contains incomplete routing"}
  emuflow_inventory "$emuflow_session_out/routed_mapped_cells.tsv" $emuflow_source_prefix 1
  emuflow_session_timings $emuflow_script_directory $emuflow_source_prefix $emuflow_io_timing_map
  puts "EMUFLOW_ROUTED_TIMING status=exported implementation=unchanged"
} message options]} {
  puts stderr "EMUFLOW_ROUTED_TIMING status=failed message=$message"
  puts stderr [dict get $options -errorinfo]
  exit 1
}
exit 0
