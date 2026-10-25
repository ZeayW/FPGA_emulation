# Behavioral native-IP loopback. No force/bypass of calibration or bit alignment.
if {$argc != 2} {error "expected PART PREPARED_OUTPUT_DIRECTORY"}
lassign $argv part output
set output [file normalize $output]
source [file join [file dirname [info script]] selectio_ip.tcl]
if {[catch {
  if {[file exists [file join $output project]] || [file exists [file join $output ip]]} {
    error "native word-link simulation requires a fresh project/IP directory"
  }
  create_project emuflow_word_link [file join $output project] -part $part
  set_param general.maxThreads 2
  foreach direction {tx rx} {
    lassign [emuflow_create_selectio_ip emuflow_selectio_$direction [file join $output ip] \
      [file join $output $direction-bank.tsv]] ip definition
    if {$definition ne "xilinx.com:ip:high_speed_selectio_wiz:3.6"} {error "unsupported native word-link IP definition"}
    generate_target all $ip
  }
  add_files -norecurse [list [file join $output tx-interface.sv] [file join $output rx-interface.sv]]
  add_files -fileset sim_1 -norecurse [file join $output word-link-tb.sv]
  set_property top emuflow_word_link_tb [get_filesets sim_1]
  set_property xsim.simulate.runtime 0ns [get_filesets sim_1]
  set_property xsim.simulate.log_all_signals false [get_filesets sim_1]
  set_property xsim.elaborate.debug_level off [get_filesets sim_1]
  update_compile_order -fileset sources_1
  update_compile_order -fileset sim_1
  set stream [open [file join $output tool-identity.tsv] w]
  puts $stream "field\tvalue"
  puts $stream "vivado_version\t[version -short]"
  puts $stream "part\t$part"
  puts $stream "ip_definition\t$definition"
  close $stream
  launch_simulation -mode behavioral
  run all
  close_sim
  if {![file isfile [file join $output word-link-results.tsv]]} {error "native word-link simulation did not publish its success record"}
  close_project
} message options]} {
  puts stderr $message
  if {[dict exists $options -errorinfo]} {puts stderr [dict get $options -errorinfo]}
  exit 1
}
exit 0
