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
puts "EMUFLOW_OPENSTA_RUNTIME status=$emuflow_runtime_probe tcl=[info patchlevel]"
exit 0
