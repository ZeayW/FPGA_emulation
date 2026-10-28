"""Read-only phase views retain every replica and reject incomplete catalogs."""
import subprocess

import pytest

from tests.test_vivado_session import ROOT, TCL86
from emuflow.vivado_session import _tcl_word


MOCK = r'''
set emuflow_native_export_stage routed
set catalog [dict create s0 {moved/a moved/a_copy} s1 moved/b s2 moved/c s3 moved/d]
set state [dict create]
set clock_period 5.0
set clock_name emuflow_tdm_clk
proc get_cells {args} {
 set expression [lindex $args end]
 set origin [lindex $expression end]
 if {[dict exists $::catalog $origin]} {return [dict get $::catalog $origin]}
 return {}
}
proc get_pins {args} {
 set cell [lindex $args [expr {[lsearch -exact $args -of_objects]+1}]]
 set leaf [lindex [lindex $args end] end]
 return $cell/$leaf
}
proc get_clocks {args} {return $::clock_name}
proc get_property {args} {
 lassign [lrange $args end-1 end] property object
 switch $property {
  NAME {return $object}
  REF_NAME {return FDRE}
  PERIOD {return $::clock_period}
 }
 error "unexpected property $property"
}
proc set_case_analysis {value pins} {
 foreach pin $pins {dict set ::state $pin $value}
}
proc close_design {} {set ::state [dict create];set ::snapshot_closed 1}
'''


def execute(tmp_path, roles, commands):
    source = ROOT/'scripts/vivado/word_phase_context.tcl'
    path = tmp_path/'roles.tsv'
    path.write_text('period\tbit\tsource_origin\n'+roles)
    program = f'source {_tcl_word(source)}\n'+MOCK
    program += f'set role_path {_tcl_word(path)}\n'+commands
    return subprocess.run([TCL86], input=program, text=True, capture_output=True, check=True)


pytestmark = pytest.mark.skipif(TCL86 is None, reason='Tcl 8.6 required')
ROLES = '3\t0\ts0\n3\t1\ts1\n4\t0\ts2\n4\t1\ts3\n'


def test_all_joint_phases_bind_source_tags_and_replicas_without_name_preservation(tmp_path):
    result = execute(tmp_path, ROLES, r'''
set context [emuflow_word_phase_bind $role_path]
puts [dict get $context repeat_words]
for {set phase 0} {$phase<12} {incr phase} {
 emuflow_word_phase_select $context $phase
 foreach {pin period bit} {moved/a/Q 3 0 moved/a_copy/Q 3 0 moved/b/Q 3 1 moved/c/Q 4 0 moved/d/Q 4 1} {
  if {[dict get $state $pin]!=(($phase%$period)>>$bit&1)} {error "wrong source phase"}
 }
}
emuflow_word_phase_discard $context
puts "remaining=[dict size $state]"
puts "closed=$snapshot_closed export_context=[info exists emuflow_native_export_stage]"
''')
    assert not result.stderr and result.stdout.splitlines() == ['12', 'remaining=0', 'closed=1 export_context=0']


@pytest.mark.parametrize('roles, expected', [
    ('3\t0\ts0\n', 'bit coverage'),
    (ROLES+'3\t0\ts0\n', 'repeats'),
    ('33\t0\ts0\n', 'coordinate'),
    ('3\t2\ts0\n', 'coordinate'),
    ('3\t0\tbad source\n', 'source origin'),
    ('3\t0\ts99\n', 'no native projection'),
    ('1\t0\ts0\n', 'source origin'),
    ('', 'empty'),
])
def test_bad_roles_do_not_apply_partial_case_values(tmp_path, roles, expected):
    result = execute(tmp_path, roles, r'''
if {![catch {emuflow_word_phase_bind $role_path} message]} {error "bad roles accepted"}
puts $message
puts "changes=[dict size $state]"
''')
    assert not result.stderr
    assert expected in result.stdout and result.stdout.rstrip().endswith('changes=0')


@pytest.mark.parametrize('prefix, expected', [
    ('unset emuflow_native_export_stage', 'read-only routed'),
    ('set emuflow_native_export_stage optimized', 'read-only routed'),
    ('set clock_period 10', 'clock differs'),
    ('set clock_name emuflow_dut_clk_0', 'clock differs'),
])
def test_phase_annotations_are_unavailable_to_implementation_and_other_clock_domains(tmp_path, prefix, expected):
    result = execute(tmp_path, ROLES, prefix+r'''
if {![catch {emuflow_word_phase_bind $role_path} message]} {error "invalid snapshot accepted"}
puts $message
puts "changes=[dict size $state]"
''')
    assert not result.stderr and expected in result.stdout and 'changes=0' in result.stdout


@pytest.mark.parametrize('phase', ['-1', '12', '1.5', 'NaN'])
def test_phase_bounds_do_not_change_snapshot(tmp_path, phase):
    result = execute(tmp_path, ROLES, f'''
set context [emuflow_word_phase_bind $role_path]
if {{![catch {{emuflow_word_phase_select $context {_tcl_word(phase)}}} message]}} {{error "invalid phase accepted"}}
puts $message
puts "changes=[dict size $state]"
''')
    assert not result.stderr and 'recurrence' in result.stdout and 'changes=0' in result.stdout


def test_incompatible_alias_values_are_rejected_before_any_annotation(tmp_path):
    roles = '3\t0\ts0\n3\t1\ts1\n4\t0\ts0\n4\t1\ts3\n'
    result = execute(tmp_path, roles, r'''
set context [emuflow_word_phase_bind $role_path]
if {![catch {emuflow_word_phase_select $context 3} message]} {error "incompatible alias accepted"}
puts $message
puts "changes=[dict size $state]"
''')
    assert not result.stderr and 'inconsistent' in result.stdout and 'changes=0' in result.stdout


def test_ratio_eight_source_literal_needs_no_retained_register(tmp_path):
    result = execute(tmp_path, '1\t0\tconstant:0\n', r'''
set context [emuflow_word_phase_bind $role_path]
emuflow_word_phase_select $context 0
emuflow_word_phase_discard $context
puts "[dict get $context repeat_words] [dict size $state] [dict size [dict get $context pins]]"
''')
    assert not result.stderr and result.stdout.strip() == '1 0 0'
