import os
import subprocess

import pytest

from emuflow.errors import ValidationError
from emuflow.global_sta import (
    EventCheck, adopt_opensta_results, bind_physical_checks, compare_system_timing, export_event_checks,
    read_engine_identity, read_measurements, run_event_checks, validate_checks,
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
    assert "find_port" in (tmp_path / "analyze.tcl").read_text()
    assert "gets $constraints line" in (tmp_path / "analyze.tcl").read_text()
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


def test_engine_identity_from_existing_banner(tmp_path):
    log = tmp_path / "opensta.log"
    log.write_text("startup warning\nOpenSTA 2.6.0 0c6421e022 Copyright (c) 2024\n")
    assert read_engine_identity(log) == {
        "name": "OpenSTA", "version": "2.6.0", "revision": "0c6421e022"}
    log.write_text("unidentified engine\n")
    with pytest.raises(ValidationError, match="engine version"):
        read_engine_identity(log)


@pytest.mark.parametrize("field", [0, 1, 2])
def test_measurement_rejects_corrupt_event_scalars(tmp_path, field):
    rows = example()
    values = [[sum(r.arcs_ns), r.required_ns-r.launch_ns,
               r.required_ns-r.launch_ns-sum(r.arcs_ns)] for r in rows]
    path = tmp_path / "out"
    def write():
        path.write_text("endpoint\tarrival_ns\trequired_ns\tslack_ns\n" +
                        "".join(f"o{i}\t"+"\t".join(map(str, v))+"\n"
                                for i, v in enumerate(values)))
    write()
    assert len(read_measurements(path, rows)) == len(rows)
    values[0][field] += 2
    write()
    with pytest.raises(ValidationError, match="event binding"):
        read_measurements(path, rows)


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


@pytest.mark.parametrize("damage", ["nan", "duplicate", "orphan", "bad-reference"])
def test_comparison_rejects_corrupt_evidence(damage):
    rows = example()
    values = [{"path": r.path, "role": r.role, "event": r.event,
               "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
               "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in rows]
    reference = {"timing_scope": "cross-fpga-subset", "paths": [{
        "path": "p", "system_delay_bound_ns": 31.5,
        "target_required_time_ns": 18, "runtime_required_time_ns": 100,
        "target_clock_slack_bound_ns": -13.5, "runtime_clock_slack_bound_ns": 68.5}]}
    if damage == "nan":
        values[0]["slack_ns"] = float("nan")
    elif damage == "duplicate":
        values.append(values[0])
    elif damage == "orphan":
        values[0]["path"] = "unknown"
    else:
        reference["paths"].append(reference["paths"][0])
    with pytest.raises(ValidationError):
        compare_system_timing(values, reference)


@pytest.mark.parametrize("initial_status,late", [("pass", False), ("incomplete", False),
                                               ("fail", False), ("pass", True)])
def test_authority_projection_updates_paths_and_every_scalar_alias(initial_status, late):
    reference = {"status": initial_status, "timing_scope": "cross-fpga-subset",
        "target_clock": {}, "runtime_clock": {}, "summary": {}, "paths": [{
        "path": "p", "system_delay_bound_ns": 31.5,
        "target_required_time_ns": 18, "runtime_required_time_ns": 100,
        "target_clock_slack_bound_ns": -13.5, "runtime_clock_slack_bound_ns": 68.5}]}
    measured = [{"path": r.path, "role": r.role, "event": r.event,
                 "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
                 "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in example()]
    for row in measured:
        if row["role"] in {"target", "runtime"}:
            row["arrival_ns"] += 1e-5
            row["slack_ns"] -= 1e-5
    if late:
        measured[0]["slack_ns"] = -0.5
    gate = adopt_opensta_results(reference, measured)
    assert gate["authority"] == "opensta"
    assert reference["status"] == ("fail" if late else initial_status)
    assert reference["paths"][0]["system_delay_bound_ns"] == measured[2]["arrival_ns"]
    assert reference["summary"]["maximum_system_delay_bound_ns"] == measured[2]["arrival_ns"]
    for role, row in (("target", measured[2]), ("runtime", measured[3])):
        group = reference[f"{role}_clock"]
        assert group["worst_slack_bound_ns"] == row["slack_ns"]
        assert group["tns_bound_ns"] == group["total_negative_slack_bound_ns"]
        assert group["tns_bound_ns"] == min(0.0, row["slack_ns"])
        assert reference["paths"][0][f"{role}_clock_slack_bound_ns"] == row["slack_ns"]


def test_standalone_phase7c_never_calls_python_composer(tmp_path, monkeypatch):
    from tests.test_phase7c import Phase7CTest
    from emuflow.cross_layer_timing import build_cross_layer_timing_contract
    from emuflow.io import write_json, read_json
    from emuflow.phase7c import run_phase7c
    import emuflow.global_sta as sta
    f = Phase7CTest(); f.setUp()
    for name, value in {"schedule": f.schedule, "platform": f.platform.to_dict(),
                        "physical": f._physical_summary(), "routes": f.routes, **f.reports}.items():
        write_json(tmp_path / f"{name}.json", value)
    write_json(tmp_path / "cross_layer_timing.json",
               build_cross_layer_timing_contract(f.routes, f.schedule))
    def forbidden(*args, **kwargs):
        raise AssertionError("original Python timing calculator was called")
    monkeypatch.setattr("emuflow.runtime.build_system_timing", forbidden)
    monkeypatch.setattr(sta, "compare_system_timing", forbidden)
    monkeypatch.setattr(sta, "adopt_opensta_results", forbidden)
    def bind(*args, metadata, **kwargs):
        metadata.update(paths={"p": {"path_scope": "cross-fpga",
            "physical_logic_segments_cone_bound": False}}, source_binding=None,
            compressed_representative_paths=1)
        return example()
    monkeypatch.setattr(sta, "bind_physical_checks", bind)
    def engine(checks, directory, executable, *, verify_arcs):
        assert not verify_arcs
        return [{"path": r.path, "role": r.role, "event": r.event,
                 "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
                 "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in checks]
    monkeypatch.setattr(sta, "run_event_checks", engine)
    monkeypatch.setattr(sta, "read_engine_identity", lambda p: {"name": "test-sta"})
    run_phase7c(*(tmp_path / f"{n}.json" for n in
                 ("schedule", "platform", "phase3", "phase4", "phase5", "phase6")),
                tmp_path / "out", physical_summary_path=tmp_path / "physical.json",
                routes_path=tmp_path / "routes.json")
    timing = read_json(tmp_path / "out/qor_report.json")["timing"]
    assert timing["global_opensta"]["execution"] == "standalone"
    assert "cross_checker" not in timing["global_opensta"]
    assert timing["target_clock"]["worst_slack_bound_ns"] == -13.5
    assert timing["runtime_clock"]["tns_bound_ns"] == 0


@pytest.mark.parametrize("late", [False, True])
def test_standalone_report_preserves_event_gate_without_comparison(late, monkeypatch):
    import emuflow.global_sta as sta
    def forbidden(*args, **kwargs):
        raise AssertionError("comparison must not run")
    monkeypatch.setattr(sta, "compare_system_timing", forbidden)
    rows = [{"path": r.path, "role": r.role, "event": r.event,
             "arrival_ns": r.launch_ns+sum(r.arcs_ns), "required_ns": r.required_ns,
             "slack_ns": r.required_ns-r.launch_ns-sum(r.arcs_ns)} for r in example()]
    if late:
        rows[0]["slack_ns"] = -1.0
    metadata = {"paths": {"p": {"path_scope": "cross-fpga", "physical_logic_segments_cone_bound": True}},
                "source_binding": None, "compressed_representative_paths": 1}
    report = sta.build_opensta_timing({"virtual_dut_clock": {"nominal_period_ns": 100}}, metadata, rows)
    assert report["status"] == ("fail" if late else "pass")
    assert report["global_opensta"]["event_failures"] == int(late)
    assert report["path_exactness"]["cone_bound_logic_paths"] == 1
    assert report["target_clock"]["tns_bound_ns"] == -13.5
    with pytest.raises(ValidationError):
        sta.build_opensta_timing({"virtual_dut_clock": {"nominal_period_ns": 100}}, metadata,
                                [r for r in rows if r["role"] != "runtime"])


def test_engine_defaults_are_uniform_and_python_is_explicit():
    import inspect
    from emuflow.cli import _build_parser
    from emuflow.phase7c import run_phase7c
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    for fn in (run_phase7c, run_multi_fpga_flow):
        assert inspect.signature(fn).parameters["global_timing_engine"].default == "opensta"
    parser = _build_parser()
    commands = [["multi-fpga", "compile", "--out", "out", "--platform", "p"],
                ["phase7c", "--out", "out", "--schedule", "s", "--platform", "p",
                 "--phase3-report", "a", "--phase4-report", "b", "--phase5-report", "c", "--phase6-report", "d"]]
    for cmd in commands:
        assert parser.parse_args(cmd).global_timing_engine == "opensta"
        assert parser.parse_args(cmd + ["--global-timing-engine", "python"]).global_timing_engine == "python"


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
    del physical["board_link_timing"]
    assert bind_physical_checks(build_virtual_runtime(fixture.schedule, fixture.platform),
                                routes, fixture.schedule, physical, fixture.platform) == checks


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
