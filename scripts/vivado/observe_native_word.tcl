# Standalone collector: every annotation belongs to this disposable snapshot.
source [file join [file dirname [info script]] native_word_observation.tcl]
if {[catch {
  if {$argc!=5} {error "expected CHECKPOINT ROLES BOUNDARIES OUTPUT THREADS"}
  emuflow_observe_native_word {*}$argv
} message options]} {
  puts stderr $message
  puts stderr [dict get $options -errorinfo]
  exit 1
}
exit 0
