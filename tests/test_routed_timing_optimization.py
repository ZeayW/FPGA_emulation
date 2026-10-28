"""A native optimization may merge, replace and replicate observed cells."""
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86


@pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')
@pytest.mark.parametrize('failure',[None,'routing','drc','timing'])
def test_transformations_are_observations_while_physical_failures_still_reject(tmp_path,failure):
    checkpoint=tmp_path/'input.dcp';checkpoint.write_text('immutable native snapshot\n')
    output=tmp_path/'output'
    program='set failure '+_tcl_word(failure or '')+'\n'+r'''
set optimized 0
proc open_checkpoint {path} {}
proc get_timing_paths {args} {return timing_path}
proc get_cells {args} {
  if {$::optimized} {return {kept changed replica}}
  return {kept changed merged}
}
proc get_property {key objects} {
  switch -- $key {
    NAME {return $objects}
    REF_NAME {
      set result [list]
      foreach object $objects {
        lappend result [expr {$object eq "changed" && $::optimized ? "LUT5" : "LUT6"}]
      }
      return $result
    }
    SLACK {return [expr {$::optimized && $::failure eq "timing" ? -0.5 : 0.5}]}
  }
  error "unexpected property: $key"
}
proc phys_opt_design {args} {set ::optimized 1}
proc route_design {args} {}
proc write_checkpoint {args} {set f [open [lindex $args end] w];puts $f snapshot;close $f}
foreach command {report_timing_summary report_route_status report_drc} {
  proc $command {args} {set f [open [lindex $args end] w];puts $f report;close $f}
}
proc get_nets {args} {return [expr {$::failure eq "routing" ? "unrouted" : ""}]}
proc get_drc_violations {args} {return [expr {$::failure eq "drc" ? "violation" : ""}]}
set argc 2
'''
    program+='set argv [list '+_tcl_word(checkpoint)+' '+_tcl_word(output)+']\n'
    program+='if {[catch {source '+_tcl_word(ROOT/'scripts/vivado/optimize_routed_timing.tcl')+'} message]} {puts stderr $message;exit 2}\n'
    result=subprocess.run([TCL86],input=program,text=True,capture_output=True)
    # The actual transformed snapshot and all three diagnostic ledgers must
    # survive even when timing/routing qualification fails afterward.
    assert (output/'optimized_routed.dcp').is_file()
    assert (output/'missing_cells.tsv').read_text().splitlines()==['name\tbaseline_ref_name','merged\tLUT6']
    assert (output/'added_cells.tsv').read_text().splitlines()==['name\tref_name','replica\tLUT6']
    assert (output/'changed_cell_types.tsv').read_text().splitlines()==['name\tbaseline_ref_name\toptimized_ref_name','changed\tLUT6\tLUT5']
    assert checkpoint.read_text()=='immutable native snapshot\n'
    if failure is None:
        assert result.returncode==0 and not result.stderr,result.stderr
        assert 'EMUFLOW_TIMING_OPT status=pass'in result.stdout
        assert 'missing_cells=1 changed_cell_types=1'in result.stdout.splitlines()[-1]
    else:
        assert result.returncode==2
        assert {'routing':'unrouted nets','drc':'DRC violations','timing':'below target'}[failure]in result.stderr
        assert 'status=pass'not in result.stdout
