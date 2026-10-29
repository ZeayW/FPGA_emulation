"""Completed analysis publishes its evidence without converting its outcome."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from emuflow.errors import ValidationError
from emuflow.flow_outcome import ANALYSIS_FLOW_SCHEMA, flow_execution_completed
from emuflow.io import write_json
from emuflow import multi_fpga_flow as flow
from emuflow.cli import _build_parser, _dispatch


def runtime(status="incomplete"):
    return {
        "schema": "emuflow.phase7c-report/v3", "increment": "native-word-system-timing",
        "status": status, "execution_status": "pass", "design": "tiny", "platform": "board",
        "physical": {"status": "pass", "timing_met": False},
        "system_timing_summary": {"status": status, "summary": {
            "maximum_sampling_cost_ns": 120., "required_dut_period_ns": 240.,
            "inferred_dut_frequency_mhz": 1000. / 240., "raw_datapath_maximum_ns": None}},
        "original_members": {"status": "complete-source-lineage-observations", "members": 3},
        "implementation_clocks": {"tdm_period_ns": 5., "dut_clock_periods_ns": {"clk": 100.}},
        "artifacts": {"report": "phase7c_report.json", "native_word_analysis": "native-word-analysis.json"},
    }


def stages():
    result = {n: {"status": "pass", "design": "tiny", "platform": "board",
                  "validation": {"status": "pass"}}
              for n in ("frontend", "partition", "system_route", "tdm", "split")}
    result["partition"]["validation"].update(instances=10, used_fpgas=2, cut_nets=1)
    result["tdm"]["validation"]["frame_slots"] = 8
    result["split"]["equivalence"] = {"status": "pass", "mismatches": 0}
    return result


def physical():
    return {"schema": "emuflow.multi-fpga-physical-flow/v1", "status": "pass",
            "physical_summary_ref": "physical-summary.json", "backend": {"id": "vivado"},
            "expected_fpgas": ["fpga0", "fpga1"], "summary": {"status": "pass", "original_cells": 10}}


def report(status="incomplete"):
    return {"schema": ANALYSIS_FLOW_SCHEMA, "status": status, "execution_status": "pass",
            "provider": "fixture", "architecture_policy": "provider-neutral",
            "stages": stages(), "physical": physical(), "runtime": runtime(status), "artifacts": {}}


@pytest.mark.parametrize("status", ["incomplete", "fail"])
def test_report_preserves_completed_nonpassing_analysis(status):
    value = report(status)
    summary = flow.validate_multi_fpga_flow_report(value)
    assert summary["status"] == status and summary["execution_status"] == "pass"
    assert summary["frame_slots"] == 8
    assert summary["system_timing"]["required_dut_period_ns"] == 240.
    assert summary["implementation_clocks"]["tdm_period_ns"] == 5.
    assert summary["physical_timing_met"] is False
    assert "nominal_virtual_frequency_mhz" not in summary
    assert value["runtime"]["system_timing_summary"]["summary"]["raw_datapath_maximum_ns"] is None


@pytest.mark.parametrize("damage", ["promote", "execution", "runtime-execution", "profile", "stage", "physical", "identity"])
def test_terminal_publication_does_not_waive_execution_or_source_contracts(damage):
    value = report()
    if damage == "promote": value["status"] = "pass"
    elif damage == "execution": value["execution_status"] = "fail"
    elif damage == "runtime-execution": value["runtime"]["execution_status"] = "fail"
    elif damage == "profile": value["runtime"]["increment"] = "other"
    elif damage == "stage": value["stages"]["split"]["equivalence"]["status"] = "fail"
    elif damage == "physical": del value["physical"]
    else: value["runtime"]["design"] = "other"
    with pytest.raises(ValidationError): flow.validate_multi_fpga_flow_report(value)


def test_legacy_success_contract_remains_strict():
    assert flow_execution_completed({"schema": "emuflow.multi-fpga-flow/v2", "status": "pass"})
    assert not flow_execution_completed({"schema": "emuflow.multi-fpga-flow/v2", "status": "incomplete"})
    value = report(); value["runtime"]["status"] = []
    assert not flow_execution_completed(value)


@pytest.mark.parametrize("status", ["incomplete", "fail"])
@pytest.mark.parametrize("implementation", ["python-control-v1", "native-control-v1"])
def test_cli_returns_nonzero_for_completed_nonpassing_analysis(monkeypatch, status, implementation):
    monkeypatch.setattr("emuflow.native_compile.run_native_multi_fpga_flow", lambda **kw: report(status))
    monkeypatch.setattr("emuflow.cli.run_multi_fpga_flow", lambda **kw: report(status))
    args = _build_parser().parse_args(["multi-fpga", "compile", "--platform", "platform.json", "--out", "out",
                                      "--compile-implementation", implementation])
    assert _dispatch(args) == 2


@pytest.mark.parametrize("status", ["incomplete", "fail"])
def test_signoff_adapter_continues_only_after_successful_analysis_execution(tmp_path, monkeypatch, status):
    value = runtime(status)
    called = {}
    def signoff(*args, **kwargs):
        called.update(kwargs)
        return value
    monkeypatch.setattr(flow, "run_phase7c", signoff)
    fields = ["schedule_path", "platform_path", "assignment_path", "global_sta_executable", "ir_path",
              "path_database_path", "routes_path", "physical_summary_path", "copied_link_timing_path"]
    ctx = SimpleNamespace(**dict.fromkeys(fields), output_dir=tmp_path,
        phase3_root=tmp_path, phase4_root=tmp_path, phase5_root=tmp_path, phase6_root=tmp_path,
        global_timing_engine="opensta", physical=True, physical_backend="vivado", physical_vivado="vivado",
        phase6_comparison=None,
        measured=lambda stage, function, *a, **kw: function(*a, **kw))
    flow._compile_signoff(ctx)
    assert ctx.runtime_report is value and ctx.runtime_report["status"] == status
    assert called['collect_native_endpoints'] is True and called['native_endpoint_vivado']=='vivado'
    value["execution_status"] = "fail"
    with pytest.raises(ValidationError): flow._compile_signoff(ctx)


def test_finalize_publishes_nonpassing_outcome_and_binds_timing_evidence(tmp_path, monkeypatch):
    value = runtime(); s = stages()
    ctx = SimpleNamespace(output_dir=tmp_path, runtime_root=tmp_path/"runtime", runtime_report=value,
        phase1_root=tmp_path/"frontend/phase1", phase3_root=tmp_path/"partition",
        phase4_root=tmp_path/"system-route", phase5_root=tmp_path/"tdm", phase6_root=tmp_path/"split",
        schedule_path=tmp_path/"tdm/schedule.json", routes_path=tmp_path/"system-route/routes.json",
        ir_path=tmp_path/"frontend/phase1/design.emuir.sqlite", assignment_path=tmp_path/"partition/assignment.json",
        frontend_report=s["frontend"], phase3_report=s["partition"], phase4_report=s["system_route"],
        phase5_report=s["tdm"], phase6_report=s["split"], physical_report=physical(),
        timing_report=None, link_timing_report=None, frame_search_report=None, phase6_comparison=None,
        transport_cost_db=None, copied_link_timing_path=None, effective_route_constraints_path=None,
        internal_timing_database=False, serial_bsp_requested=False)
    names = ["frontend/phase1/platform.normalized.json", "frontend/phase1/design.emuir.sqlite",
        "partition/constraints.normalized.json", "partition/assignment.json",
        "system-route/route_constraints.normalized.json", "system-route/routes.json", "system-route/routes.sqlite",
        "tdm/schedule.json", "tdm/schedule.sqlite", "split/manifest.json", "runtime/runtime_contract.json",
        "runtime/qor_report.json", "runtime/phase7c_report.json", "runtime/native-word-analysis.json",
        "runtime/native-word-checks.tsv", "runtime/global-opensta/measurements.tsv",
        "runtime/global-opensta/opensta.log", "runtime/global-opensta/native-word-witness-verification.json",
        "physical/multi-fpga-physical-flow-report.json", "physical/physical-summary.json"]
    for name in names:
        p = tmp_path/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text("fixture evidence\n")
    monkeypatch.setattr(flow, "tdm_schedule_store_path", lambda p: p.with_suffix(".sqlite"))
    monkeypatch.setattr(flow, "system_route_store_path", lambda p: p.with_suffix(".sqlite"))
    saved = flow._compile_finalize(ctx)
    assert saved["status"] == saved["summary"]["status"] == "incomplete"
    assert saved["execution_status"] == "pass"
    assert "runtime_native_word_analysis" in saved["artifacts"]
    assert "runtime_global-opensta_measurements.tsv" in saved["artifacts"]
    assert json.loads((tmp_path/"multi-fpga-flow-report.json").read_text()) == saved
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1]/"schemas/multi-fpga-flow-v3.schema.json").read_text())
    jsonschema.validate(saved, schema)
    broken = copy.deepcopy(saved); broken["status"] = "pass"
    with pytest.raises(jsonschema.ValidationError): jsonschema.validate(broken, schema)


def checkpoint_fixture(tmp_path, monkeypatch, status="incomplete"):
    """Exercise report ownership; numerical engines have separate integration tests."""
    s = stages(); value = runtime(status)
    names = {"frontend": "frontend/phase1/phase1_report.json", "partition": "partition/phase3_report.json",
             "system_route": "system-route/phase4_report.json", "tdm": "tdm/phase5_report.json",
             "split": "split/phase6_report.json"}
    for name, relative in names.items(): write_json(tmp_path/relative, s[name])
    for relative in ["frontend/phase1/platform.normalized.json", "frontend/phase1/design.emuir.sqlite",
            "partition/constraints.normalized.json", "partition/assignment.json", "partition/clusters.json",
            "system-route/route_constraints.normalized.json", "system-route/routes.json", "system-route/routes.sqlite",
            "tdm/schedule.json", "tdm/schedule.sqlite", "timing/path-database.json"]:
        write_json(tmp_path/relative, {})
    write_json(tmp_path/"split/manifest.json", {"clock_periods": {"clk": 100.}})
    physical_root = tmp_path/"physical-resumed"
    write_json(physical_root/"multi-fpga-physical-flow-report.json", physical())
    write_json(physical_root/"physical-summary.json", {})
    monkeypatch.setattr(flow, "_require_physical_source_timing", lambda p: None)
    monkeypatch.setattr(flow, "validate_multi_fpga_physical_report", lambda *a: {"status": "pass"})
    monkeypatch.setattr(flow, "tdm_schedule_store_path", lambda p: p.with_suffix(".sqlite"))
    monkeypatch.setattr(flow, "system_route_store_path", lambda p: p.with_suffix(".sqlite"))
    calls = []
    def phase7c(*args, **kwargs):
        calls.append((args, kwargs))
        directory = args[6]; directory.mkdir(parents=True, exist_ok=True)
        write_json(directory/"runtime_contract.json", {})
        write_json(directory/"qor_report.json", {"timing": {"global_opensta": {"execution": "standalone"}}})
        write_json(directory/"native-word-analysis.json", {})
        write_json(directory/"phase7c_report.json", value)
        for name in ["measurements.tsv", "opensta.log", "native-word-witness-verification.json"]:
            p = directory/"global-opensta"/name; p.parent.mkdir(exist_ok=True); p.write_text("fixture evidence\n")
        return value
    monkeypatch.setattr(flow, "run_phase7c", phase7c)
    return value, s, physical_root, calls


@pytest.mark.parametrize("status", ["incomplete", "fail"])
def test_resumed_checkpoint_publishes_completed_outcome_without_repeating_upstream(tmp_path, monkeypatch, status):
    value, _, physical_root, calls = checkpoint_fixture(tmp_path, monkeypatch, status)
    saved = flow.finalize_multi_fpga_physical_checkpoint(tmp_path, physical_root,
        runtime_directory="runtime-final", global_sta_results_dir=tmp_path/"saved-engine")
    assert saved["schema"] == ANALYSIS_FLOW_SCHEMA
    assert saved["status"] == saved["summary"]["status"] == status
    assert saved["execution_status"] == "pass"
    assert saved["runtime"] == value
    assert saved["artifacts"]["runtime_contract"]["path"] == "runtime-final/runtime_contract.json"
    assert saved["artifacts"]["runtime_native_word_analysis"]["path"] == "runtime-final/native-word-analysis.json"
    assert saved["artifacts"]["route_store"]["path"] == "system-route/routes.sqlite"
    assert saved["artifacts"]["schedule_store"]["path"] == "tdm/schedule.sqlite"
    assert len(calls) == 1 and calls[0][1]["global_sta_results_dir"] == tmp_path/"saved-engine"
    assert json.loads((tmp_path/"multi-fpga-flow-report.json").read_text()) == saved


def test_resumed_checkpoint_keeps_failed_execution_fatal(tmp_path, monkeypatch):
    value, _, physical_root, _ = checkpoint_fixture(tmp_path, monkeypatch)
    value["execution_status"] = "fail"
    with pytest.raises(ValidationError, match="complete Phase 7C"):
        flow.finalize_multi_fpga_physical_checkpoint(tmp_path, physical_root)
    assert not (tmp_path/"multi-fpga-flow-report.json").exists()


@pytest.mark.parametrize("status", ["pass", "incomplete", "fail"])
def test_finalize_cli_preserves_outcome_and_forwards_independent_engine_choice(tmp_path, monkeypatch, status):
    calls = []
    def finalize(*args, **kwargs):
        calls.append((args, kwargs)); return {"status": status, "summary": {"status": status}}
    monkeypatch.setattr("emuflow.cli.finalize_multi_fpga_physical_checkpoint", finalize)
    args = _build_parser().parse_args(["multi-fpga", "finalize-physical", "--flow", str(tmp_path),
        "--physical", "physical-resumed", "--global-sta-executable", "qualified-sta"])
    assert _dispatch(args) == (0 if status == "pass" else 2)
    assert calls[0][1]["global_sta_executable"] == "qualified-sta"


def test_bundle_replay_uses_sealed_resumed_runtime_and_physical_paths(tmp_path, monkeypatch):
    from contextlib import contextmanager
    _, s, physical_root, calls = checkpoint_fixture(tmp_path, monkeypatch)
    saved = flow.finalize_multi_fpga_physical_checkpoint(tmp_path, physical_root)
    for relative in ["runtime/runtime_contract.json", "runtime/qor_report.json",
            "physical/multi-fpga-physical-flow-report.json", "physical/physical-summary.json"]:
        p = tmp_path/relative; p.parent.mkdir(exist_ok=True); p.write_text("wrong canonical directory\n")
    monkeypatch.setattr(flow, "validate_phase3_indexed", lambda *a, **kw: copy.deepcopy(s["partition"]["validation"]))
    monkeypatch.setattr(flow, "validate_phase4", lambda *a, **kw: copy.deepcopy(s["system_route"]["validation"]))
    monkeypatch.setattr(flow, "validate_phase5", lambda *a, **kw: copy.deepcopy(s["tdm"]["validation"]))
    monkeypatch.setattr(flow, "validate_phase6", lambda *a, **kw: copy.deepcopy(s["split"]["validation"]))
    monkeypatch.setattr(flow.Platform, "load", lambda *a: SimpleNamespace())
    monkeypatch.setattr(flow, "validate_virtual_runtime", lambda *a: {"frame_slots": 8})
    monkeypatch.setattr(flow, "read_tdm_schedule", lambda *a: {})
    @contextmanager
    def physical_summary(path):
        assert path == physical_root/"physical-summary.json"
        yield {}
    monkeypatch.setattr(flow, "read_physical_summary", physical_summary)
    replay = flow.validate_multi_fpga_flow_bundle(tmp_path, require_physical=True)
    assert replay["status"] == "incomplete" and len(calls) == 2
    assert calls[-1][1]["physical_summary_path"] == physical_root/"physical-summary.json"
    assert calls[-1][1]["global_sta_results_dir"] == tmp_path/"runtime-final/global-opensta"
    assert saved["status"] == replay["status"]
    (tmp_path/"runtime-final/global-opensta/measurements.tsv").write_text("changed\n")
    with pytest.raises(ValidationError, match="SHA-256"):
        flow.validate_multi_fpga_flow_bundle(tmp_path, require_physical=True)
