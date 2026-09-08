import os

import pytest

from emuflow.errors import ValidationError
from emuflow.global_sta import (
    EventCheck, bind_physical_checks, compare_system_timing, export_event_checks,
    read_measurements, run_event_checks, validate_checks,
)


def example():
    # Two consecutive transported cuts: a missed first TX must not be hidden
    # by a legal final commit. Delays are separate physical arcs, not a total.
    return [EventCheck("p", "tx", "first", 0, (3,), 4),
            EventCheck("p", "tx", "second", 4, (8, .5, 2.5), 16),
            EventCheck("p", "target", "capture", 16, (8, .5, 7), 18),
            EventCheck("p", "runtime", "capture", 16, (8, .5, 7), 100),
            EventCheck("p", "commit", "capture", 16, (8, .5, 7), 96)]


def test_exports_raw_arc_chain_and_absolute_events(tmp_path):
    export_event_checks(example(), tmp_path)
    sdc = (tmp_path / "global_timing.sdc").read_text()
    assert "set_input_delay -clock epoch -max 16" in sdc
    assert "31.5" not in (tmp_path / "global_timing.lib").read_text()
    assert "find_timing_paths" in (tmp_path / "analyze.tcl").read_text()
    assert "-group_count 5" in (tmp_path / "analyze.tcl").read_text()


def test_reject_incomplete_duplicate_and_nan():
    rows = example()
    for bad in (rows[:-2]+rows[-1:], rows+rows[:1],
                rows+[EventCheck("p", "tx", "bad", 0, (float("nan"),), 4)]):
        with pytest.raises(ValidationError):
            validate_checks(bad)


def test_measurement_requires_exact_endpoint_coverage(tmp_path):
    path = tmp_path / "out"
    path.write_text("endpoint\tarrival_ns\trequired_ns\tslack_ns\no0\t3\t4\t1\n")
    with pytest.raises(ValidationError, match="endpoints"):
        read_measurements(path, example())


def test_check_population_validation_is_linear():
    # A quadratic original-member scan previously made global timing unusable.
    rows = [EventCheck(str(i), role, "end", 0, (1,), 2)
            for i in range(20000) for role in ("target", "runtime")]
    assert len(validate_checks(rows)) == 40000


def test_comparison_checks_each_path_and_event():
    reference = {"timing_scope": "cross-fpga-subset", "paths": [{
        "path": "p", "system_delay_bound_ns": 31.5,
        "target_required_time_ns": 18, "runtime_required_time_ns": 100,
        "target_clock_slack_bound_ns": -13.5, "runtime_clock_slack_bound_ns": 68.5}]}
    values = [{"path": r.path, "role": r.role, "event": r.event,
               "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
               "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in example()]
    assert compare_system_timing(values, reference)["status"] == "pass"
    values[0]["slack_ns"] = -.5
    assert compare_system_timing(values, reference)["status"] == "fail"
    values[2]["arrival_ns"] += 1
    with pytest.raises(ValidationError, match="disagrees"):
        compare_system_timing(values, reference)


def test_physical_binding_uses_raw_measurements():
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.runtime import build_virtual_runtime
    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    _, routes, _ = fixture._system_inputs()
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(fixture.platform)
    checks = bind_physical_checks(build_virtual_runtime(fixture.schedule, fixture.platform),
                                  routes, fixture.schedule, physical, fixture.platform)
    assert sum(c.role == "target" for c in checks) == 1
    assert sum(c.role == "tx" for c in checks) == len(fixture.schedule["entries"])
    assert next(c for c in checks if c.role == "target").arcs_ns[-2:] == (7.0, 0.0)


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENSTA"), reason="real OpenSTA not configured")
def test_real_opensta_fixed_events_and_late_tx(tmp_path):
    rows = example()
    measurements = run_event_checks(rows, tmp_path, os.environ["EMUFLOW_TEST_OPENSTA"])
    for row, check in zip(measurements, rows):
        arrival = check.launch_ns + sum(check.arcs_ns)
        assert row["arrival_ns"] == pytest.approx(arrival, abs=1e-3)
        assert row["required_ns"] == pytest.approx(check.required_ns, abs=1e-3)
        assert row["slack_ns"] == pytest.approx(check.required_ns-arrival, abs=1e-3)
    late = rows.copy()
    late[0] = EventCheck("p", "tx", "first", 0, (4.5,), 4)
    result = run_event_checks(late, tmp_path / "late", os.environ["EMUFLOW_TEST_OPENSTA"])
    assert result[0]["slack_ns"] == pytest.approx(-.5, abs=1e-3)
    assert result[3]["slack_ns"] > 0
