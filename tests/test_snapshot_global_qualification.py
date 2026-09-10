import pytest
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.snapshot_global_qualification import _classify_members
from emuflow.snapshot_timing_population import boundary_id
from emuflow.snapshot_global_qualification import qualify_snapshot_global_timing
from test_snapshot_path_events import fixture


def path(name, a, z, board='board0'):
    return dict(path=name,segments=[dict(board=board,launch=a,capture=z)])


def test_retention_is_counted_without_zero_delay_or_renamed_member():
    q=boundary_id('state','q');r=boundary_id('state','r')
    rows=[path('timed',q,r),path('retained',q,q)]
    numeric,classified=_classify_members(rows,{('board0',q,r):dict(delay_ns=2)},
                                        {('board0',q,q):'state_hold'})
    assert numeric==rows[:1]
    assert classified=={'retained':['state_hold']}


def test_unknown_missing_relation_and_merged_other_register_are_not_waived():
    q=boundary_id('state','q');r=boundary_id('state','r')
    for reason in (None,'unexplained','state_hold'):
        with pytest.raises(ValidationError):
            _classify_members([path('p',q,r)],{}, {('board0',q,r):reason})


def test_independence_does_not_hide_a_second_missing_segment():
    p=path('p','a','b');p['segments'].append(dict(board='board1',launch='b',capture='c'))
    with pytest.raises(ValidationError,match='unqualified'):
        _classify_members([p],{}, {('board0','a','b'):'boolean_independent'})


def test_contradictory_timed_and_nontimed_records_fail():
    key=('board0','a','b')
    with pytest.raises(ValidationError,match='both timed and nontimed'):
        _classify_members([path('p','a','b')],{key:{}},{key:'boolean_independent'})


def test_orchestrator_preserves_native_scalars_and_early_failure(tmp_path):
    paths,pair,protocol=fixture()
    bounds={(s['board'],s['launch'],s['capture']):dict(delay_ns=d,setup_ns=1)
            for s,d in zip(paths[0]['segments'],(25,2,3))}
    def measured(pop,*args,**kw):
        return dict(timed={k:v for k,v in bounds.items() if k[0]==pop['board']},untimed={},
                    native_physical_pairs=1,source_relations=1)
    def native(checks,*args,**kw):
        assert [c.role for c in checks]==['tx','tx','commit','target','runtime']
        # Distinct fixture engine scalars deliberately cannot be recovered from
        # Python's input-arc sums. Aggregation must preserve the selected engine.
        return [dict(path=c.path,role=c.role,event=c.event,arrival_ns=123,required_ns=456,
                     slack_ns={'tx':-7.25,'commit':6.25,'target':-53.25,'runtime':6.25}[c.role])
                for c in checks]
    module='emuflow.snapshot_global_qualification.'
    with patch(module+'qualify_snapshot_path_coverage',return_value={'original_members':1}), \
         patch(module+'iter_snapshot_path_bindings',return_value=iter(paths)), \
         patch(module+'build_snapshot_timing_population',side_effect=lambda *a,**kw:dict(board=kw['board'])), \
         patch(module+'qualify_snapshot_source_correspondence',return_value={'sensitivity_proofs':[]}), \
         patch(module+'measure_snapshot_segment_bounds',side_effect=measured), \
         patch(module+'run_snapshot_global_checks',side_effect=native):
        result=qualify_snapshot_global_timing({'paths':[{'id':'p','clock_period_ns':40}]},None,{},
            pair=pair,protocol=protocol,physical_models={b:dict(bindings={},graph={}) for b in ('board0','board1')},
            port_owners={},initial_launch_ns={boundary_id('state','q'):1},cycle=0,
            setup_uncertainty_ns=0,output_dir=tmp_path,yosys='yosys',sta='sta')
    assert result['metrics']['target']['wns_ns']==result['metrics']['target']['tns_ns']==-53.25
    assert result['metrics']['runtime']['wns_ns']==6.25
    assert result['metrics']['runtime']['tns_ns']==0
    assert result['readiness_violations']==2
    assert result['status']=='readiness_violations'
    assert result['nontimed_members']==0
    assert not result['full_flow_qualified']
