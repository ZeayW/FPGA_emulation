import copy
import pytest
from emuflow.errors import ValidationError
from emuflow.snapshot_acceptance import validate_snapshot_offline_result


def result():
    hold=dict(physical_pairs=1,negative_pairs=0,minimum_slack_ns=.1)
    physical=dict(status='physical_outputs_generated',stages=[dict(name=n,exit_code=0)
        for n in ('synthesis','place_route','bitstream')],bitstream_sha256='a'*64,
        local_qualification=dict(status='pass',utilization_limit=.75),
        snapshot_qualification={n:dict(status='pass') for n in ('clock_coverage',
            'timing_annotation_coverage','combinational_arc_coverage','uart_data_cdc','reset_structure')})
    return dict(schema='emuflow.ulx3s-one-shot-qualification/v1',status='checks_finished_qualification_pending',
        platform='ulx3s-85f-v3.0.x-pair-gpio',physical_seed=1,physical_workers=2,hardware_qualified=False,timing_cycle=1,
        frontend=dict(source_paths=2),partition=dict(status='pass',used_fpgas=2,illegal_cuts=0,balance_violations=0),
        macrocycle_equivalence=dict(physical_wrappers_simulated=True,host_drive='serial_records',macrocycles=2,
            observed_ff=1,scope='declared-input-trace-and-initial-state'),
        physical={b:copy.deepcopy(physical) for b in ('board0','board1')},
        local_hold={b:copy.deepcopy(hold) for b in ('board0','board1')},
        synchronizer_timing={b:dict(physical_pairs=n,negative_setup_pairs=0,minimum_setup_slack_ns=1,
            hold=dict(hold,physical_pairs=n)) for b,n in [('board0',3),('board1',2)]},
        global_timing=dict(status='native_checks_complete',numerical_engine='OpenSTA',
            scope='whole_source_population_finite_trace_routed_cone_bounds',numerically_timed_members=1,
            nontimed_members=1,original_members=2,source_coverage=dict(status='pass',original_members=2),
            nontimed_slack_policy='no_numeric_slack_or_zero_delay_arc',readiness_violations=0,cycle=1,hardware_qualified=False,
            metrics=dict(target=dict(paths=1,negative_paths=1,wns_ns=-2,tns_ns=-2),
                         runtime=dict(paths=1,negative_paths=0,wns_ns=1,tns_ns=0))))


def test_offline_acceptance_never_means_target_or_hardware_closure():
    summary=validate_snapshot_offline_result(result())
    assert summary['status']=='pass'
    assert not summary['target_frequency_closed'] and not summary['hardware_qualified']
    assert 'asynchronous_reset_recovery_removal' in summary['unverified']


@pytest.mark.parametrize('mutation',['host','missing','capacity','tool','hold','sync','coverage','readiness','runtime','nan','hardware'])
def test_incomplete_or_failed_gates_are_rejected(mutation):
    r=result();g=r['global_timing']
    if mutation=='host':r['macrocycle_equivalence']['physical_wrappers_simulated']=False
    if mutation=='missing':del r['physical']['board1']
    if mutation=='capacity':r['physical']['board0']['local_qualification']['utilization_limit']=1
    if mutation=='tool':r['physical']['board0']['stages'][1]['exit_code']=1
    if mutation=='hold':r['local_hold']['board0']['negative_pairs']=1
    if mutation=='sync':r['synchronizer_timing']['board1']['physical_pairs']=1
    if mutation=='coverage':g['original_members']=3
    if mutation=='readiness':g['readiness_violations']=1
    if mutation=='runtime':g['metrics']['runtime']['wns_ns']=-1
    if mutation=='nan':g['metrics']['target']['wns_ns']=float('nan')
    if mutation=='hardware':r['hardware_qualified']=True
    with pytest.raises(ValidationError):validate_snapshot_offline_result(r)
