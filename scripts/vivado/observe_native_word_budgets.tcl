source [file join [file dirname [info script]] native_word_budgets.tcl]
source [file join [file dirname [info script]] native_session.tcl]
if {[catch {
  if {$argc!=7} {error "expected CHECKPOINT ROLES BUDGETS FPGA CLOCK_CONTRACT OUTPUT THREADS"}
  lassign $argv checkpoint roles budgets fpga contract output threads
  file mkdir $output
  set_param general.maxThreads $threads
  open_checkpoint $checkpoint
  set ::emuflow_native_export_stage routed
  emuflow_validate_clock_contract $contract true
  set sources [emuflow_native_word_sources [file join $roles rx-roles.tsv]]
  set view [emuflow_native_word_apply_budgets $sources $fpga $budgets $output]
  set count [emuflow_native_word_endpoint_slacks $sources $view $output]
  emuflow_validate_clock_contract $contract true
  puts "EMUFLOW_NATIVE_ENDPOINT_BUDGETS status=complete endpoints=$count"
} message options]} {
  puts stderr $message
  puts stderr [dict get $options -errorinfo]
  exit 1
}
exit 0
