import pytest

from emuflow.errors import ValidationError
from emuflow.snapshot_global_qualification import _classify_members
from emuflow.snapshot_timing_population import boundary_id


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
