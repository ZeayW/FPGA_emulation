import pytest

from emuflow.clock_budget import ClockEdgeBudget,clock_edge_budget_from_engine_fields
from emuflow.errors import ValidationError


def budget(**changes):
    values=dict(source_clock='base',target_clock='base',source_period_ns=10.,target_period_ns=10.,
        launch_edge_ns=0.,capture_edge_ns=0.,source_offset_ns=0.,target_cycle_time_ns=10.,multicycle_adjustment_ns=0.)
    return ClockEdgeBudget(**{**values,**changes})


def test_half_cycle_launch_and_capture_scale_their_window_not_a_fixed_phase_penalty():
    for b in (budget(launch_edge_ns=5),budget(capture_edge_ns=5,target_cycle_time_ns=5)):
        assert b.target_period_coefficient==.5
        assert b.scaled_edge_window_ns(source_period_ns=100,target_period_ns=100)==50
    assert budget(launch_edge_ns=5).launch_relative_arrival_ns(5.12)==pytest.approx(.12)


def test_multicycle_and_rational_cycle_accounting_preserve_engine_offsets():
    assert budget(multicycle_adjustment_ns=10).scaled_edge_window_ns(source_period_ns=100,target_period_ns=100)==200
    b=budget(source_period_ns=9,source_offset_ns=9,target_cycle_time_ns=1)
    assert b.launch_relative_arrival_ns(9.12)==pytest.approx(.12)
    assert b.scaled_edge_window_ns(source_period_ns=90,target_period_ns=100)==10
    with pytest.raises(ValidationError,match='uniform'):
        b.scaled_edge_window_ns(source_period_ns=100,target_period_ns=100)


@pytest.mark.parametrize('changes',[{'source_period_ns':0},{'target_period_ns':float('inf')},
    {'launch_edge_ns':True},{'source_offset_ns':float('nan')},{'target_clock':'NULL'},
    {'target_cycle_time_ns':0}])
def test_invalid_or_nonpositive_clock_windows_are_not_guessed(changes):
    with pytest.raises(ValidationError):budget(**changes)


def test_engine_reader_rejects_missing_cycle_facts_and_unsupported_check_types():
    fields={k:str(v)for k,v in budget().__dict__.items()};fields.update(path_delay='0',latch_check='0')
    assert clock_edge_budget_from_engine_fields(fields)==budget()
    for damage in ({'source_offset_ns':None},{'path_delay':'1'},{'latch_check':'1'}):
        with pytest.raises(ValidationError):clock_edge_budget_from_engine_fields({**fields,**damage})
