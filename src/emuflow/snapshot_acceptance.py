"""Terminal contract checks for finite-trace offline reference validation.

No optimizer, waveform replay, numerical STA or hashing occurs here. Producers
own those checks. This joins their compact results and states the exact claim;
it cannot certify analog behavior, all stimuli or measured board operation.
"""
import math
from .errors import ValidationError


def validate_snapshot_offline_result(result):
    def require(condition, message):
        if not condition:
            raise ValidationError('offline snapshot acceptance: '+message)
    def natural(value):
        return type(value) is int and value >= 0
    try:
        require(result['schema']=='emuflow.ulx3s-one-shot-qualification/v1','unknown result schema')
        require(result['status']=='checks_finished_qualification_pending','execution did not pass')
        require(result['physical_seed']==1 and result['physical_workers']==2,'unexpected physical configuration')
        require(result['platform']=='ulx3s-85f-v3.0.x-pair-gpio','unexpected assembly')
        eq=result['macrocycle_equivalence']
        require(eq['physical_wrappers_simulated'] is True and eq['host_drive']=='serial_records','physical host boundary not simulated')
        require(type(eq['macrocycles']) is int and eq['macrocycles']>0 and eq['observed_ff']>0,'empty state trace')
        require(eq['scope']=='declared-input-trace-and-initial-state','unrecognized functional claim')
        partition=result['partition']
        require(partition['status']=='pass' and partition['used_fpgas']==2 and
                partition['illegal_cuts']==0 and partition['balance_violations']==0,'partition gate failed')
        require(set(result['physical'])==set(result['local_hold'])==set(result['synchronizer_timing'])=={'board0','board1'},'missing board')
        for board,p in result['physical'].items():
            require(p['status']=='physical_outputs_generated','physical implementation incomplete')
            require([s['name'] for s in p['stages']]==['synthesis','place_route','bitstream'] and
                    all(s['exit_code']==0 for s in p['stages']),'physical tools failed')
            require(p['local_qualification']['status']=='pass' and
                    p['local_qualification']['utilization_limit']==.75,'capacity/local clock gate failed')
            require(isinstance(p['bitstream_sha256'],str) and len(p['bitstream_sha256'])==64 and
                    all(c in '0123456789abcdef' for c in p['bitstream_sha256']),'missing bitstream identity')
            q=p['snapshot_qualification']
            for gate in ('clock_coverage','timing_annotation_coverage','combinational_arc_coverage','uart_data_cdc','reset_structure'):
                require(q[gate]['status']=='pass','physical '+gate+' failed')
            hold=result['local_hold'][board];sync=result['synchronizer_timing'][board]
            require(hold['physical_pairs']>0 and hold['negative_pairs']==0 and
                    math.isfinite(hold['minimum_slack_ns']) and hold['minimum_slack_ns']>=0,'local hold failed')
            require(sync['physical_pairs']==(3 if board=='board0' else 2) and
                    sync['negative_setup_pairs']==0 and math.isfinite(sync['minimum_setup_slack_ns']) and
                    sync['minimum_setup_slack_ns']>=0 and sync['hold']['negative_pairs']==0 and
                    sync['hold']['physical_pairs']==sync['physical_pairs'] and
                    math.isfinite(sync['hold']['minimum_slack_ns']) and sync['hold']['minimum_slack_ns']>=0,'synchronizer data timing failed')
        g=result['global_timing'];n=g['numerically_timed_members'];other=g['nontimed_members']
        require(g['status']=='native_checks_complete' and g['numerical_engine']=='OpenSTA' and
                g['scope']=='whole_source_population_finite_trace_routed_cone_bounds','native global gate incomplete')
        require(natural(n) and n>0 and natural(other) and n+other==g['original_members']==result['frontend']['source_paths'],'source population mismatch')
        require(g['source_coverage']['status']=='pass' and g['source_coverage']['original_members']==g['original_members'],'source coverage failed')
        require(g['nontimed_slack_policy']=='no_numeric_slack_or_zero_delay_arc' and g['readiness_violations']==0,'readiness or false-path policy invalid')
        require(g['cycle']==result['timing_cycle'] and 0<=g['cycle']<eq['macrocycles'],'selected cycle outside trace')
        for role in ('target','runtime'):
            m=g['metrics'][role]
            require(m['paths']==n and natural(m['negative_paths']) and m['negative_paths']<=n and
                    all(math.isfinite(m[k]) for k in ('wns_ns','tns_ns')) and m['tns_ns']<=0,'invalid global scalars')
        require(g['metrics']['runtime']['negative_paths']==0 and g['metrics']['runtime']['tns_ns']==0 and
                g['metrics']['runtime']['wns_ns']>=0,'observed runtime deadline failed')
        require(result['hardware_qualified'] is False and g['hardware_qualified'] is False,'unsupported hardware promotion')
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise ValidationError('offline snapshot acceptance: incomplete or malformed result') from exc
    return dict(status='pass',scope='finite_trace_offline_physical_host_reference_validation',
        original_members=g['original_members'],numerically_timed_members=n,
        macrocycles=eq['macrocycles'],selected_timing_cycle=g['cycle'],
        numerical_engine='OpenSTA',
        target_frequency_closed=g['metrics']['target']['negative_paths']==0,
        assumptions=['declared_initial_state_and_input_trace','declared_clock_periods_and_phase_offsets',
            'ideal_digital_external_wires_no_jitter_or_metastability',
            'exported_sdf_routed_cone_bounds_with_ideal_clock_skew'],
        unverified=['asynchronous_reset_recovery_removal','metastability_mtbf','electrical_and_power_sequence',
            'measured_link_latency_ber_pvt','all_input_traces_and_clock_relationships'],
        hardware_qualified=False,universal_timing_qualified=False)
