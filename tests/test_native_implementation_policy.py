"""One native policy selects valid candidates and recovers rejected trials."""
import csv
import subprocess

import pytest

from emuflow.vivado_session import _tcl_word
from tests.test_vivado_session import ROOT, TCL86

pytestmark=pytest.mark.skipif(TCL86 is None,reason='Tcl 8.6 required')


def state(wns=-.2,tns=-10.,whs=.01,**extra):
    return {'wns':wns,'tns':tns,'whs':whs,'pending':0,'blocking':0,'warning':0,'raises':0,'repair_error':0,'native_critical':0,'drc_native_critical':0,**extra}


def record(value):
    return '[dict create '+' '.join(_tcl_word(k)+' '+_tcl_word(v)for k,v in value.items())+']'


HARNESS=r'''
set calls {}
set repairs 0
set restores 0
set phase 0
set clock_period 5
set native_critical 0
proc get_msg_config {args} {
  return [expr {[lindex $args 1] eq "CRITICAL WARNING" ? $::native_critical : 0}]
}
proc get_timing_paths {args} {
  if {[lsearch -exact $args min]>=0} {
    return [expr {[dict get $::current whs] eq "none" ? "" : "hold"}]
  }
  return setup
}
proc get_property {key object} {
  if {$key eq "SLACK"} {return [dict get $::current [expr {$object eq "hold" ? "whs" : "wns"}]]}
  if {$key eq "SEVERITY"} {return [expr {$object eq "warning" ? "Warning" : "Error"}]}
  error "unexpected native property"
}
proc get_nets {args} {
  if {[dict get $::current pending] && [string first PARTIAL [lindex $args end]]>=0} {return partial_net}
  return {}
}
proc get_drc_violations {args} {
  if {[dict get $::current blocking]} {return blocking}
  return [expr {[dict get $::current warning] ? "warning" : ""}]
}
proc report_drc {args} {
  set path [lindex $args end]
  set f [open $path w];puts $f native_drc;close $f
  if {[file tail $path] eq "locality-drc.rpt"} {incr ::native_critical [dict get $::current drc_native_critical]}
}
proc report_timing_summary {args} {
  set f [open [lindex $args end] w]
  puts $f "WNS(ns) TNS(ns) Failing Total WHS(ns) THS(ns) Failing Total"
  puts $f "------- ------- ------- ------- ------- ------- ------- -------"
  puts $f "[dict get $::current wns] [dict get $::current tns] 1 200 [dict get $::current whs] 0 0 200"
  close $f
}
proc write_checkpoint {args} {
  set f [open [lindex $args end] w];puts $f $::current;close $f
}
proc close_project {} {}
proc open_checkpoint {path} {
  incr ::restores
  set f [open $path r];set ::current [string trim [read $f]];close $f
}
proc emuflow_validate_clock_contract {path generated} {
  if {$path ne "original-contract" || !$generated || $::clock_period!=5} {error "clock contract changed"}
}
proc phys_opt_design {args} {
  lappend ::calls $args
  if {$::phase>2} {error "unbounded optimization retry"}
  set expected [lindex {{-directive ExploreWithHoldFix} {-critical_cell_opt} {-slr_crossing_opt}} $::phase]
  if {$args ne $expected} {error "unsupported post-route options or phase order"}
  set ::current [lindex $::plans $::phase]
  incr ::native_critical [dict get $::current native_critical]
  incr ::phase
  if {[dict get $::current raises]} {error "native optimization failed after changing the trial design"}
}
proc route_design {args} {
  if {$args eq "-unroute"} {return}
  if {$args ne "-preserve"} {error "completed routes were discarded"}
  incr ::repairs
  if {[dict get $::current repair_error]} {error "native route repair failed"}
  dict set ::current pending 0
}
'''


def run_policy(tmp_path,baseline,plans):
    output=tmp_path/'native'
    padded=[*plans,*([baseline]*(3-len(plans)))]
    script='set current '+record(baseline)+'\nset plans [list '+' '.join(record(p)for p in padded)+']\n'
    script+=HARNESS+'\nsource '+_tcl_word(ROOT/'scripts/vivado/native_implementation_policy.tcl')+'\n'
    script+='emuflow_policy_postroute '+_tcl_word(output)+' original-contract\n'
    script+='puts "RESULT [dict get $::current wns] $::phase $::repairs $::restores"\n'
    result=subprocess.run([TCL86],input=script,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    root=output/'implementation-policy'
    with(root/'candidates.tsv').open(newline='')as stream:rows=list(csv.DictReader(stream,delimiter='\t'))
    with(root/'summary.tsv').open(newline='')as stream:summary=dict(list(csv.reader(stream,delimiter='\t'))[1:])
    values=next(l.split()[1:]for l in result.stdout.splitlines()if l.startswith('RESULT '))
    return rows,summary,tuple(float(v)for v in values)


@pytest.mark.parametrize('baseline,plans,expected,closed,repair_count',[
    (state(.1,0),[],.1,True,0),
    (state(),[state(-.4),state(-.1),state(-.05)],-.05,False,0),
    (state(),[state(.3,0,-.2),state(.05,0)],.05,True,0),
    (state(),[state(-9,raises=1),state(.05,0)],.05,True,0),
    (state(),[state(.2,0,native_critical=1),state(.05,0)],.05,True,0),
    (state(),[state(-.2,-20),state(-.2,-5),state(.05,0)],.05,True,0),
    (state(),[state(.1,0,pending=1)],.1,True,1),
    (state(),[state(.1,0,pending=1,repair_error=1),state(.05,0)],.05,True,1),
    (state(),[state(.2,0,blocking=1),state(.05,0)],.05,True,0),
    (state(),[state(.1,0,warning=1)],.1,True,0),
    (state(whs='none'),[state(.05,0,whs='none')],.05,True,0),
])
def test_shared_policy_selects_actual_valid_native_result(tmp_path,baseline,plans,expected,closed,repair_count):
    rows,summary,result=run_policy(tmp_path,baseline,plans)
    assert result[0]==pytest.approx(expected)
    assert result[1]<=3 and result[2]==repair_count
    assert float(summary['wns'])==pytest.approx(expected)
    assert (summary['timing_met']=='1')is closed
    assert rows[-1]['status']=='selected'
    assert float(rows[-1]['wns_ns'])==pytest.approx(expected)
    if baseline['whs']=='none':assert rows[-1]['whs_ns']=='' and summary['whs']==''
    if any(p['raises']or p['repair_error']for p in plans):
        failed=[r for r in rows if r['status']=='retained-after-native-error']
        assert failed and bytes.fromhex(failed[0]['error_hex']).decode().startswith('native ')
        assert result[3]>=1
    bad_hold=bool(plans)and isinstance(plans[0]['whs'],(int,float))and plans[0]['whs']<0
    if plans and (plans[0]['blocking']or bad_hold):
        assert rows[1]['status']=='rejected-worse'


def test_equal_wns_compares_full_native_tns_and_restores_worse_trial(tmp_path):
    rows,summary,result=run_policy(tmp_path,state(),[state(-.2,-20),state(-.2,-5),state(-.2,-6)])
    assert [r['status']for r in rows[1:-1]]==['rejected-worse','accepted','rejected-worse']
    assert float(summary['tns'])==-5. and result[3]==2
    assert summary['timing_met']=='0'


@pytest.mark.parametrize('mode',['better','worse','error','unchanged','critical','drc-critical'])
def test_bank_locality_uses_same_measured_selection_rule(tmp_path,mode):
    output=tmp_path/'native';output.mkdir()
    script='set current '+record(state())+'\nset plans {}\n'+HARNESS
    script+='\nset locality_mode '+_tcl_word(mode)+'\n'+r'''
set places 0
proc emuflow_native_word_source_roles {path} {return current_native_roles}
proc emuflow_rebind_bank_local_replicas {placement sources prefix output} {
  if {$sources ne "current_native_roles"} {error "stale source roles"}
  if {$::locality_mode eq "error"} {dict set ::current wns -9;error "native locality failed"}
  return [expr {$::locality_mode eq "unchanged" ? 0 : 3}]
}
proc place_design {args} {
  incr ::places
  if {$args ne {-directive Explore -no_bufg_opt}} {error "unexpected placement strategy"}
  dict set ::current wns [expr {$::locality_mode eq "better" ? .1 : -.5}]
  if {$::locality_mode eq "critical"} {dict set ::current wns .2;incr ::native_critical}
  if {$::locality_mode eq "drc-critical"} {dict set ::current wns .2;dict set ::current drc_native_critical 2}
}
'''
    script+='source '+_tcl_word(ROOT/'scripts/vivado/native_implementation_policy.tcl')+'\n'
    script+='emuflow_policy_localize plan origins mapped_partition '+_tcl_word(output)+' original-contract Explore\n'
    script+='puts "RESULT [dict get $::current wns] $::places $::restores"\n'
    result=subprocess.run([TCL86],input=script,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    values=next(l.split()[1:]for l in result.stdout.splitlines()if l.startswith('RESULT '))
    assert float(values[0])==pytest.approx(.1 if mode=='better'else-.2)
    assert int(values[1])==(mode in ('better','worse','critical','drc-critical'))
    assert int(values[2])==(mode in ('worse','error','critical','drc-critical'))


@pytest.mark.parametrize('failure',['none','throw','critical','drc','partial','baseline-fails'])
def test_optional_locality_route_automatically_falls_back_once(tmp_path,failure):
    output=tmp_path/'native';directory=output/'implementation-policy';directory.mkdir(parents=True)
    checkpoint=directory/'before-locality.dcp'
    # Native checkpoints are opaque in production. The harness serializes
    # the state dictionary so reopening proves the trial was discarded.
    script='set current '+record(state())+'\nset plans {}\n'+HARNESS
    script+='\nwrite_checkpoint -force '+_tcl_word(checkpoint)+'\n'
    script+='set current '+record(state(.1,0))+'\nset failure '+_tcl_word(failure)+'\n'+r'''
set routes 0
proc route_design {args} {
  incr ::routes
  if {$args ne {-directive Explore} || $::routes>2} {error "unexpected or unbounded route"}
  if {$::routes==2} {
    if {$::failure eq "baseline-fails"} {error "baseline route failed"}
    return
  }
  switch $::failure {
    throw - baseline-fails {dict set ::current wns 9;error "native candidate route failed"}
    critical {incr ::native_critical}
    drc {dict set ::current blocking 1}
    partial {dict set ::current pending 1}
  }
}
'''
    script+='source '+_tcl_word(ROOT/'scripts/vivado/native_implementation_policy.tcl')+'\n'
    script+='set locality [dict create accepted 1 checkpoint '+_tcl_word(checkpoint)+']\n'
    script+='if {[catch {emuflow_policy_route '+_tcl_word(output)+' original-contract Explore $locality} message]} {puts "FAILED $message";exit 17}\n'
    script+='puts "RESULT [dict get $::current wns] $::routes $::restores"\n'
    result=subprocess.run([TCL86],input=script,text=True,capture_output=True)
    if failure=='baseline-fails':
        assert result.returncode==17 and 'FAILED baseline route failed'in result.stdout
        return
    assert result.returncode==0,result.stdout+result.stderr
    values=next(l.split()[1:]for l in result.stdout.splitlines()if l.startswith('RESULT '))
    assert tuple(float(v)for v in values)==pytest.approx((.1,1,0)if failure=='none'else(-.2,2,1))
    if failure!='none':
        assert 'EMUFLOW_NATIVE_TRIAL restore locality-route'in result.stdout
        assert(directory/'locality-route-error.txt').is_file()
