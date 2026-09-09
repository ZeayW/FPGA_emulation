import os
import subprocess

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
    assert "set_input_delay -clock epoch -max 0" in sdc
    assert "set_output_delay -clock epoch -max -1 [get_ports o2]" in sdc
    assert "31.5" not in (tmp_path / "global_timing.lib").read_text()
    assert "find_timing_paths" in (tmp_path / "analyze.tcl").read_text()
    assert "-group_count 5" in (tmp_path / "analyze.tcl").read_text()
    assert "data_arrival_time" in (tmp_path / "analyze.tcl").read_text()
    assert "get_property $p points" not in (tmp_path / "analyze.tcl").read_text()


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


def test_reject_orphan_events_and_divergent_observation_chains():
    with pytest.raises(ValidationError, match="no original-path"):
        validate_checks(example()+[EventCheck("orphan", "tx", "tx", 0, (1,), 2)])
    rows = example()
    rows[3] = EventCheck("p", "runtime", "capture", 16, (1,), 100)
    with pytest.raises(ValidationError, match="physical chains"):
        validate_checks(rows)


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


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENTIMER"), reason="OpenTimer driver not configured")
@pytest.mark.parametrize("extra_paths", [0, 256])
def test_real_opentimer_fixed_events(tmp_path, extra_paths):
    rows = example()
    rows[0] = EventCheck("p", "tx", "first", 0, (4.5,), 4)
    for i in range(extra_paths):
        # Mixed local/event observations, arc-chain lengths and target periods.
        arcs = tuple((i+j+1)/37 for j in range(1+i % 5))
        for role, deadline in (("target", 10+i % 7), ("runtime", 1000)):
            rows.append(EventCheck(f"p{i}", role, "capture", i/8, arcs, deadline))
    export_event_checks(rows, tmp_path)
    subprocess.run([os.environ["EMUFLOW_TEST_OPENTIMER"], str(len(rows))],
                   cwd=tmp_path, check=True)
    result = read_measurements(tmp_path / "opentimer-measurements.tsv", rows)
    for measured, row in zip(result, rows):
        arrival = row.launch_ns+sum(row.arcs_ns)
        assert measured["arrival_ns"] == pytest.approx(arrival, abs=1e-3)
        assert measured["required_ns"] == pytest.approx(row.required_ns, abs=1e-3)
        assert measured["slack_ns"] == pytest.approx(row.required_ns-arrival, abs=1e-3)
    assert result[0]["slack_ns"] < 0 < result[3]["slack_ns"]


@pytest.mark.skipif(not os.environ.get("EMUFLOW_TEST_OPENTIMER"), reason="OpenTimer driver not configured")
def test_real_opentimer_from_physical_binding(tmp_path):
    from tests.test_static_exact_system_timing import StaticExactSystemTimingTest
    from emuflow.board_link_timing import build_board_link_timing_model
    from emuflow.system_timing import build_system_timing
    fixture = StaticExactSystemTimingTest()
    fixture.setUp()
    runtime, routes, phase5 = fixture._system_inputs()
    physical = fixture._system_physical()
    physical["board_link_timing"] = build_board_link_timing_model(fixture.platform)
    checks = bind_physical_checks(runtime, routes, fixture.schedule, physical, fixture.platform)
    export_event_checks(checks, tmp_path)
    subprocess.run([os.environ["EMUFLOW_TEST_OPENTIMER"], str(len(checks))],
                   cwd=tmp_path, check=True)
    measured = read_measurements(tmp_path / "opentimer-measurements.tsv", checks)
    reference = build_system_timing(runtime, routes, fixture.schedule, phase5, physical,
                                    fixture.platform,
                                    semantic_contract=fixture.assignment["semantic_contract"])
    assert compare_system_timing(measured, reference)["status"] == "pass"
