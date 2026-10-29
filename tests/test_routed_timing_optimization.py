"""Native post-route continuation preserves completed routes and transformations."""
import shutil
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86

TCL = TCL86 or shutil.which('tclsh')
pytestmark = pytest.mark.skipif(TCL is None, reason='Tcl required')


def run_optimizer(tmp_path, failure=None, extra=()):
    checkpoint=tmp_path/'input.dcp';checkpoint.write_text('immutable native snapshot\n')
    output=tmp_path/'output'
    program='set failure '+_tcl_word(failure or '')+'\n'+r'''
set optimized 0
set repaired 0
proc open_checkpoint {path} {puts NATIVE_OPEN}
proc get_timing_paths {args} {
  return [expr {[lsearch -exact $args min]>=0 ? "hold_path" : "setup_path"}]
}
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
    SLACK {
      if {$objects eq "hold_path"} {return [expr {$::failure eq "hold" ? -0.2 : 0.01}]}
      return [expr {$::optimized && $::failure eq "timing" ? -0.5 : 0.5}]
    }
    SEVERITY {
      if {$::failure eq "advisory"} {return Warning}
      if {$::failure eq "critical-drc"} {return {Critical Warning}}
      return Error
    }
  }
  error "unexpected property: $key"
}
proc phys_opt_design {args} {
  if {[lsearch -exact $args -force_replication_on_nets]>=0} {
    error "native post-route mode rejects forced net replication"
  }
  puts "NATIVE_PHYS_OPT $args"
  set ::optimized 1
}
proc route_design {args} {puts "NATIVE_ROUTE $args";set ::repaired 1}
proc write_checkpoint {args} {set f [open [lindex $args end] w];puts $f snapshot;close $f}
foreach command {report_timing_summary report_route_status report_drc} {
  proc $command {args} {set f [open [lindex $args end] w];puts $f report;close $f}
}
proc get_nets {args} {
  set status {}
  switch -- $::failure {
    routing {set status UNROUTED}
    partial {set status PARTIAL}
    conflicts {set status CONFLICTS}
    repair {if {!$::repaired} {set status PARTIAL}}
  }
  if {$status ne "" && [string first $status [lindex $args end]]>=0} {return incomplete}
  return {}
}
proc get_drc_violations {args} {
  return [expr {$::failure in {drc critical-drc advisory} ? "violation" : ""}]
}
'''
    args=(checkpoint,output,*extra)
    program+='set argc '+str(len(args))+'\nset argv [list '+' '.join(_tcl_word(a)for a in args)+']\n'
    program+='if {[catch {source '+_tcl_word(ROOT/'scripts/vivado/optimize_routed_timing.tcl')+'} message]} {puts stderr $message;exit 2}\n'
    result=subprocess.run([TCL],input=program,text=True,capture_output=True)
    assert checkpoint.read_text()=='immutable native snapshot\n'
    return result,output


@pytest.mark.parametrize('failure',[None,'repair','routing','partial','conflicts','drc','critical-drc','hold','timing','advisory'])
def test_transformations_are_observations_while_native_physical_failures_reject(tmp_path,failure):
    result,output=run_optimizer(tmp_path,failure)
    # Keep the transformed snapshot and diagnostics when native qualification
    # fails. Removed/replaced objects do not constitute a physical failure.
    assert (output/'optimized_routed.dcp').is_file()
    assert (output/'missing_cells.tsv').read_text().splitlines()==['name\tbaseline_ref_name','merged\tLUT6']
    assert (output/'added_cells.tsv').read_text().splitlines()==['name\tref_name','replica\tLUT6']
    assert (output/'changed_cell_types.tsv').read_text().splitlines()==['name\tbaseline_ref_name\toptimized_ref_name','changed\tLUT6\tLUT5']
    assert 'NATIVE_PHYS_OPT -directive ExploreWithHoldFix'in result.stdout
    if failure in (None,'repair','advisory'):
        assert result.returncode==0 and not result.stderr,result.stderr
        assert 'EMUFLOW_TIMING_OPT status=pass'in result.stdout
        assert 'missing_cells=1 changed_cell_types=1'in result.stdout.splitlines()[-1]
    else:
        assert result.returncode==2
        expected={'routing':'incomplete nets','partial':'incomplete nets','conflicts':'incomplete nets',
                  'drc':'blocking DRC','critical-drc':'blocking DRC','hold':'hold slack','timing':'below target'}
        assert expected[failure]in result.stderr
        assert 'status=pass'not in result.stdout
    if failure=='repair':
        assert result.stdout.count('NATIVE_ROUTE -preserve')==1
    elif failure not in ('routing','partial','conflicts'):
        assert 'NATIVE_ROUTE'not in result.stdout


@pytest.mark.parametrize('options',['Explore','-critical_cell_opt -slr_crossing_opt -tns_cleanup'])
def test_native_directive_or_explicit_postroute_options_and_requested_route(tmp_path,options):
    result,_=run_optimizer(tmp_path,extra=('0',options,'Explore'))
    assert result.returncode==0,result.stderr
    expected=options if options.startswith('-')else '-directive '+options
    assert 'NATIVE_PHYS_OPT '+expected in result.stdout
    assert 'NATIVE_ROUTE -directive Explore'in result.stdout


@pytest.mark.parametrize('extra',[
    ('0','Explore','Explore','measured_decode_net'),
    ('0','-force_replication_on_nets measured_decode_net'),
])
def test_unsupported_postroute_force_request_rejected_before_checkpoint_load(tmp_path,extra):
    result,output=run_optimizer(tmp_path,extra=extra)
    assert result.returncode==2
    assert 'forced net replication is unavailable post-route'in result.stderr
    assert 'NATIVE_OPEN'not in result.stdout
    assert 'NATIVE_PHYS_OPT'not in result.stdout
    assert not output.exists()
