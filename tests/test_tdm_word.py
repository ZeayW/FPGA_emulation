from dataclasses import replace

import pytest

from emuflow.default_platform import research_platform_path
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.platform import Platform
from emuflow.tdm_word import LVDSWordTiming


def board():
    value = read_json(research_platform_path(2))
    link = value["links"][0]
    link.update(mode="source_synchronous", payload_bits_per_lane_per_cycle=8,
                max_line_rate_gbps_per_lane=1.6, latency_cycles=6)
    return value


def test_explicit_lvds_format_distinguishes_physical_lanes_and_word_bits():
    value = board()
    link = Platform.from_dict(value).links[0]
    timing = link.word_timing
    assert timing.serial_ui_ns == 0.625
    assert timing.word_period_ns == 5.0
    assert timing.line_rate_gbps == 1.6
    assert timing.fixed_latency_word_cycles == 6
    assert link.data_lanes_per_direction == 360
    assert link.transport_bits_per_cycle_per_direction == 2880
    assert timing.channel_position(0, 360) == (0, 0)
    assert timing.channel_position(7, 360) == (0, 7)
    assert timing.channel_position(8, 360) == (1, 0)
    assert timing.channel_position(2879, 360) == (359, 7)
    assert Platform.from_dict(Platform.from_dict(value).to_dict()).links[0].word_timing == timing
    # Existing abstract presets are not relabelled as a completed PHY by the reader.
    assert Platform.load(research_platform_path(2)).links[0].word_timing is None


@pytest.mark.parametrize("ratio,words,delay", [(8,1,35), (16,2,40), (24,3,45),
                                              (40,5,55), (224,28,170), (256,32,190)])
def test_ratio_domain_is_serial_uis_and_rounds_are_whole_words(ratio, words, delay):
    timing = LVDSWordTiming(5.0, 30.0)
    assert timing.word_cycles_for_ratio(ratio) == words
    assert timing.pre_schedule_delay_ns(ratio) == delay


def test_all_bits_of_a_word_share_rx_visibility_despite_serial_order():
    timing = LVDSWordTiming(5.0, 30.0)
    events = [timing.transfer(ready_ns=0.0, word_index=1, bit_in_word=bit, ratio=8)
              for bit in range(8)]
    assert [event.serial_tx_ns for event in events] == [5.0 + 0.625*i for i in range(8)]
    assert {event.word_load_ns for event in events} == {5.0}
    assert {event.rx_visible_ns for event in events} == {35.0}
    assert {event.delay_ns for event in events} == {35.0}


def test_ready_after_word_load_fails_even_before_the_assigned_bit_is_serialized():
    with pytest.raises(ValidationError, match="misses its assigned word-load"):
        LVDSWordTiming(5.0,30.0).transfer(ready_ns=5.1, word_index=1, bit_in_word=7, ratio=8)


def test_post_schedule_replaces_the_variable_estimate_and_propagates_multiple_hops():
    timing = LVDSWordTiming(5.0,30.0)
    first = timing.transfer(ready_ns=0.0, word_index=1, bit_in_word=7, ratio=16)
    second = timing.transfer(ready_ns=first.rx_visible_ns+3.0, word_index=8, bit_in_word=0, ratio=16)
    assert first.delay_ns == 35.0 < timing.pre_schedule_delay_ns(16)
    assert second.tdm_wait_ns == 2.0
    assert second.delay_ns == 32.0
    assert first.delay_ns+3.0+second.delay_ns == second.rx_visible_ns == 70.0


def test_exceeded_upper_bound_fails_without_clamping_or_moving_the_event():
    timing = LVDSWordTiming(5.0,30.0)
    assert timing.transfer(ready_ns=0,word_index=28,bit_in_word=0,ratio=224).delay_ns == 170.0
    with pytest.raises(ValidationError,match="exceeds the declared ratio upper bound"):
        timing.transfer(ready_ns=0,word_index=29,bit_in_word=0,ratio=224)


@pytest.mark.parametrize("change", [
    {"payload_bits_per_lane_per_cycle":4}, {"fabric_clock_mhz":250},
    {"latency_cycles":7}, {"max_line_rate_gbps_per_lane":1.0},
    {"max_line_rate_gbps_per_lane":None}, {"tdm_delay_model":None},
])
def test_inconsistent_board_time_units_or_double_counted_pipeline_are_rejected(change):
    value = board()
    value["links"][0].update(change)
    with pytest.raises(ValidationError):
        Platform.from_dict(value)


def test_ratio_ui_coefficient_is_checked_independently_of_word_clock():
    value = board()
    value["links"][0]["tdm_delay_model"]["ratio_coefficient_ns"] = 5.0
    with pytest.raises(ValidationError, match="coefficient must equal one serial UI"):
        Platform.from_dict(value)


@pytest.mark.parametrize("ratio", [True, 0, 7, 9, 264, 8.0])
def test_invalid_ratio_never_becomes_a_word_count(ratio):
    with pytest.raises(ValidationError):
        LVDSWordTiming(5.0,30.0).word_cycles_for_ratio(ratio)


def test_invalid_native_word_positions_and_nonfinite_events_fail_closed():
    timing = LVDSWordTiming(5.0,30.0)
    for changes in ({"bit_in_word":8}, {"word_index":-1}, {"word_index":True},
                    {"word_index":2**32}, {"ready_ns":float("nan")},
                    {"word_epoch_ns":float("inf")}):
        request=dict(ready_ns=0,word_index=1,bit_in_word=0,ratio=8)
        request.update(changes)
        with pytest.raises(ValidationError):
            timing.transfer(**request)
    for channel in (-1, True, 2880):
        with pytest.raises(ValidationError): timing.channel_position(channel, 360)
    for changes in ({"fixed_delay_ns":4.0},{"word_period_ns":0.0},{"bits_per_word":8.0},
                    {"ratio_quantum":4},{"minimum_ratio":9}):
        with pytest.raises(ValidationError): replace(timing,**changes)


def test_periodic_table_resolves_the_next_occurrence_without_changing_order():
    timing = LVDSWordTiming(5.0,30.0)
    first = timing.next_transfer(ready_ns=0.0,phase_word=1,bit_in_word=7,ratio=16)
    later = timing.next_transfer(ready_ns=5.1,phase_word=1,bit_in_word=7,ratio=16)
    assert first.word_load_ns == 5.0
    assert later.word_load_ns == 15.0
    assert later.tdm_wait_ns == pytest.approx(9.9)
    assert later.delay_ns <= timing.pre_schedule_delay_ns(16)
    # A concrete event that was missed remains invalid; periodic selection
    # must be requested explicitly and retains the original table phase.
    with pytest.raises(ValidationError,match="misses its assigned word-load"):
        timing.transfer(ready_ns=5.1,word_index=1,bit_in_word=7,ratio=16)
    with pytest.raises(ValidationError,match="phase lies outside"):
        timing.next_transfer(ready_ns=0,phase_word=2,bit_in_word=0,ratio=16)


def test_periodic_wait_is_bounded_for_all_phases_and_fractional_readiness():
    timing = LVDSWordTiming(5.0,30.0)
    for ratio in (8,16,24,40,224,256):
        for phase in range(ratio//8):
            for ready in (0.0,0.125,4.999,5.0,5.001,31.25,139.875,140.0,185.125):
                event=timing.next_transfer(ready_ns=ready,phase_word=phase,bit_in_word=7,ratio=ratio)
                assert event.word_load_ns >= ready
                assert event.word_load_ns-5.0*(ratio//8) < max(ready,5.0)
                assert 0 <= event.tdm_wait_ns <= ratio*0.625
                assert event.delay_ns <= timing.pre_schedule_delay_ns(ratio)


def test_word_plan_requires_explicit_policy_and_rejects_unqualified_physical_binding(tmp_path):
    from emuflow.netlist import build_split_artifacts, transport_to_systemverilog
    from emuflow.transport_storage import write_transport_storage, write_transport_systemverilog
    platform = Platform.from_dict(board())
    with pytest.raises(ValidationError, match="word event policy"):
        build_split_artifacts(None, {}, {}, platform)
    with pytest.raises(ValidationError, match="word event policy"):
        write_transport_storage(tmp_path, tmp_path/"missing.sqlite", {}, platform, design={}, clocks=[])
    from emuflow.tdm_word import WORD_EVENT_POLICY
    with pytest.raises(ValidationError, match="PHY binding is not yet qualified"):
        write_transport_storage(tmp_path,tmp_path/"missing.sqlite",{"word_event_policy":WORD_EVENT_POLICY},platform,design={},clocks=[],virtual_board={})
    with pytest.raises(ValidationError, match="word event policy"):
        write_transport_systemverilog(tmp_path/"bad.sv", {}, platform)
    with pytest.raises(ValidationError, match="word event policy"):
        transport_to_systemverilog({}, platform)


def test_epoch_release_edge_is_never_used_as_a_new_word_load():
    timing = LVDSWordTiming(5,30)
    assert timing.next_transfer(ready_ns=0,phase_word=0,bit_in_word=0,ratio=8).word_load_ns == 5
    assert timing.next_transfer(ready_ns=0,phase_word=0,bit_in_word=0,ratio=24).word_load_ns == 15
    with pytest.raises(ValidationError, match="index is invalid"):
        timing.transfer(ready_ns=0,word_index=0,bit_in_word=0,ratio=8)


def test_word_plan_cannot_fall_back_to_uniform_ratio_arcs_in_global_physical_timing():
    from emuflow.global_sta import bind_physical_checks
    with pytest.raises(ValidationError, match="ratio estimates cannot substitute"):
        bind_physical_checks({}, {}, {"transport_semantics":"sampled-virtual-wire"}, {}, Platform.from_dict(board()))
