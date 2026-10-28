"""Native comparison assembly against independently validated physical evidence."""
import copy
import json
import subprocess

import pytest

from tests.native_build import emuflow_native
from tests.test_physical_backend import PhysicalBackendContractTest
from emuflow.physical.physical_backend import physical_backend_descriptor
from emuflow.flow.multi_fpga_physical_flow import validate_multi_fpga_physical_report
from emuflow.flow.multi_fpga_flow import (
    _phase6_physical_metrics, validate_phase6_ab_comparison,
)


def physical_report(backend="rapidwright", *, improved=False, inactive=False):
    result = PhysicalBackendContractTest()._result()
    result["identity"]["backend"] = backend
    if improved:
        result["timing"].update(wns_ns=-0.5, tns_ns=-2.0,
                               failing_endpoints=2, failing_endpoint_constraints=3,
                               timing_met=False, critical_path_ns=11.0)
    stages = {
        "transport_synthesis": {"status": "pass"},
        "placement_ir": {"status": "pass", "instances": 105,
                         "boundary_identity": {"validation": {"status": "pass"}}},
    }
    if backend == "rapidwright":
        stages["rapidwright_implementation"] = {"status": "pass", **{
            key: {} for key in ("mapped_netlist", "packing", "placement", "route",
                                "routed_timing", "opensta", "boundary_timing")}}
    else:
        stages.update(mapped_verilog={"status": "pass"}, vivado_implementation={"status": "pass"})
    report = {
        "schema": "emuflow.multi-fpga-physical-flow/v1", "status": "pass",
        "design": "component-contract", "platform": "board",
        "backend": physical_backend_descriptor(backend),
        "architecture": {"part": "academic-part"}, "execution": {"workers": 1, "seed": 1},
        "expected_fpgas": ["fpga0"], "physical_summary_ref": "physical-summary.json",
        "fpgas": [{"fpga": "fpga0", "part": "academic-part", "status": "pass",
                   "original_cells": 100, "transport_cells": 5, "critical_path_ns": 12.5,
                   "physical_result": result, "stages": stages}],
    }
    if inactive:
        record = copy.deepcopy(report["fpgas"][0])
        record.update(fpga="fpga1", active=False, original_cells=0, transport_cells=0,
                      critical_path_ns=0.0)
        result = record["physical_result"]
        result["identity"]["fpga"] = "fpga1"
        result["implementation_status"] = "inactive"
        result["cell_accounting"] = dict.fromkeys(result["cell_accounting"], 0)
        result["timing"] = dict.fromkeys(result["timing"], 0.0)
        result["timing"].update(applicable=False, timing_met=True,
            failing_endpoints=0, failing_endpoint_constraints=0,
            clock_domain_presence=dict.fromkeys(("fabric", "dut", "cross"), False),
            clock_domain_delays_ns=dict.fromkeys(("overall", "fabric", "dut", "cross"), 0.0))
        record["stages"] = {
            "transport_synthesis": {"status": "pass"},
            "placement_ir": {"status": "pass", "instances": 0,
                             "boundary_identity": {"validation": {"status": "pass"}}},
            "boundary_timing": {"status": "pass"},
        }
        report["fpgas"].append(record)
        report["expected_fpgas"].append("fpga1")
    return report


def run_report(tmp_path, report):
    source, output = tmp_path / "report.json", tmp_path / "facts.json"
    source.write_text(json.dumps(report))
    result = subprocess.run([str(emuflow_native()), "physical", "validate-flow-report",
                             str(source), str(output)], capture_output=True, text=True)
    return result, output


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
@pytest.mark.parametrize("inactive", [False, True])
def test_native_physical_report_matches_independent_validator(tmp_path, backend, inactive):
    report = physical_report(backend, improved=True, inactive=inactive)
    report["summary"] = validate_multi_fpga_physical_report(report)
    result, output = run_report(tmp_path, report)
    assert result.returncode == 0, result.stderr
    facts = json.loads(output.read_text())
    assert facts["summary"] == report["summary"]
    assert facts["metrics"] == _phase6_physical_metrics(report)


@pytest.mark.parametrize("damage", ["backend", "census", "closure", "endpoints", "clock",
    "stage", "inactive", "summary", "duplicate", "lineage"])
def test_native_physical_report_rejects_corrupt_evidence(tmp_path, damage):
    report = physical_report(inactive=True)
    first = report["fpgas"][0]
    result = first["physical_result"]
    if damage == "backend": report["backend"]["timing_engine"] = "wrong"
    elif damage == "census": result["cell_accounting"]["physical_cells"] += 1
    elif damage == "closure": result["closure"]["unrouted_nets"] = 1
    elif damage == "endpoints": result["timing"]["tns_ns"] = -1
    elif damage == "clock":
        result["timing"].update(clock_domain_presence={"fabric": True, "dut": False, "cross": True},
            clock_domain_delays_ns={"overall": 1., "fabric": 1., "dut": 0., "cross": 0.})
    elif damage == "stage": del first["stages"]["rapidwright_implementation"]["route"]
    elif damage == "inactive": report["fpgas"][1]["physical_result"]["timing"]["wns_ns"] = 1.
    elif damage == "summary": report["summary"] = {"status": "pass"}
    elif damage == "duplicate": report["expected_fpgas"][1] = "fpga0"
    else:
        result["analysis_scope"] = {"cell_accounting_policy": "vivado-optimized-source-lineage-v1"}
    command, output = run_report(tmp_path, report)
    assert command.returncode != 0
    assert not output.exists()


def comparison_case(root, backend="rapidwright"):
    def runtime(wns):
        return {"status": "pass", "design": "component-contract", "platform": "board",
                "system_timing_summary": {"status": "pass", **{
                    clock: {"worst_slack_bound_ns": wns, "total_negative_slack_bound_ns": 0.,
                            "negative_slack_paths": 0}
                    for clock in ("target_clock", "runtime_clock")}}}
    phase6 = {"status": "pass", "design": "component-contract", "platform": "board"}
    baseline = {"phase6": phase6, "physical": physical_report(backend), "runtime": runtime(0.5),
                "phase6_runtime_seconds": 1., "physical_runtime_seconds": 2.}
    selected = {"phase6": phase6, "physical": physical_report(backend, improved=True),
                "runtime": runtime(0.25)}
    for arm, prefix in ((baseline, root / "phase6-comparison/baseline"), (selected, root)):
        for field, relative in (("phase6", "split/phase6_report.json"),
            ("physical", "physical/multi-fpga-physical-flow-report.json"),
            ("runtime", "runtime/phase7c_report.json")):
            path = prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(arm[field]))
    draft = {"schema": "emuflow.phase6-ab-comparison/v3", "status": "pending-physical-comparison",
        "selected_provider": "chimew", "baseline_provider": "baseline",
        "qualification": "xilinx-placed-lookahead+fixed-lvds-wiring",
        "frozen_upstream": {key + "_sha256": "a" * 64 for key in
                            ("emuir", "assignment", "routes", "schedule", "platform")},
        "baseline": baseline, "pin_plan_metrics": {"signals": 4},
        "chimew_phase6_runtime_seconds": 1.5, "chimew_physical_runtime_seconds": 2.5}
    path = root / "phase6-comparison/comparison-input.json"
    path.write_text(json.dumps(draft))
    return draft, path


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
def test_native_comparison_reconstructs_reports_and_deltas(tmp_path, backend):
    _, source = comparison_case(tmp_path, backend)
    command = subprocess.run([str(emuflow_native()), "phase6", "finalize-comparison",
                              str(source), str(tmp_path)], capture_output=True, text=True)
    assert command.returncode == 0, command.stderr
    report = json.loads((tmp_path / "phase6-comparison/comparison-report.json").read_text())
    assert report["validation"] == validate_phase6_ab_comparison(report)
    assert report["physical_delta"]["failing_endpoints"] == 2
    assert report["system_timing_delta"]["target_clock_worst_slack_bound_ns"] == -.25


@pytest.mark.parametrize("damage", ["baseline", "execution", "namespace", "seal", "pins", "physical"])
def test_native_comparison_rejects_mismatched_arms_and_preserves_prior_report(tmp_path, damage):
    draft, source = comparison_case(tmp_path)
    if damage == "baseline": draft["baseline"]["runtime"]["design"] = "other"
    elif damage == "seal": draft["frozen_upstream"]["routes_sha256"] = "invalid"
    elif damage == "pins": draft["pin_plan_metrics"]["signals"] = True
    else:
        path = tmp_path / "physical/multi-fpga-physical-flow-report.json"
        value = json.loads(path.read_text())
        if damage == "execution": value["execution"]["seed"] = 3
        elif damage == "namespace": value["design"] = "other"
        else: value["status"] = "fail"
        path.write_text(json.dumps(value))
    source.write_text(json.dumps(draft))
    output = tmp_path / "phase6-comparison/comparison-report.json"
    output.write_text("prior generation")
    command = subprocess.run([str(emuflow_native()), "phase6", "finalize-comparison",
                              str(source), str(tmp_path)], capture_output=True, text=True)
    assert command.returncode != 0
    assert output.read_text() == "prior generation"
