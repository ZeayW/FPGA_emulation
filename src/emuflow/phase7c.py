from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from .cross_layer_timing import (
    build_cross_layer_physical_binding,
    build_cross_layer_timing_contract,
    validate_cross_layer_timing_contract,
)
from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .runtime import (
    aggregate_qor,
    build_virtual_runtime,
    runtime_controller_testbench,
    runtime_timing_xdc,
    validate_virtual_runtime,
    virtual_runtime_controller_to_systemverilog,
)


PHASE7C_REPORT_SCHEMA = "emuflow.phase7c-report/v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_phase7c(
    schedule_path: Path,
    platform_path: Path,
    phase3_report_path: Path,
    phase4_report_path: Path,
    phase5_report_path: Path,
    phase6_report_path: Path,
    output_dir: Path,
    physical_summary_path: Optional[Path] = None,
    routes_path: Optional[Path] = None,
    board_link_timing_path: Optional[Path] = None,
    simulation_frames: int = 12,
    materialize_physical_summary: bool = True,
) -> Dict[str, Any]:
    schedule = read_json(schedule_path)
    platform = Platform.load(platform_path)
    runtime = build_virtual_runtime(schedule, platform)
    validation = validate_virtual_runtime(runtime, schedule, platform)
    physical_summary = (
        read_json(physical_summary_path)
        if physical_summary_path is not None
        else None
    )
    if physical_summary is not None and routes_path is None:
        raise ValidationError(
            "physical Phase 7C closure requires the Phase 4 routes artifact"
        )
    if board_link_timing_path is not None:
        if physical_summary is None:
            raise ValidationError(
                "BoardLinkTimingDB requires a physical summary for Phase 7C"
            )
        board_link_timing = read_json(board_link_timing_path)
        physical_summary = dict(physical_summary)
        physical_summary["board_link_timing"] = board_link_timing
    routes = read_json(routes_path) if routes_path is not None else None
    routes_artifact_sha256 = (
        _sha256(routes_path) if routes_path is not None else None
    )
    phase5_report = read_json(phase5_report_path)
    phase6_report = read_json(phase6_report_path)
    cross_layer_contract = None
    physical_binding = None
    physical_binding_validation = None
    if routes is not None:
        phase5_contract_path = phase5_report_path.parent / "cross_layer_timing.json"
        if physical_summary is not None and not phase5_contract_path.is_file():
            raise ValidationError(
                "physical Phase 7C closure requires the sealed Phase 5 "
                "cross-layer timing contract"
            )
        if phase5_contract_path.is_file():
            cross_layer_contract = read_json(phase5_contract_path)
            validate_cross_layer_timing_contract(
                routes, cross_layer_contract, schedule
            )
        else:
            cross_layer_contract = build_cross_layer_timing_contract(
                routes, schedule
            )
    if physical_summary is not None:
        assert cross_layer_contract is not None
        physical_binding = build_cross_layer_physical_binding(
            cross_layer_contract, schedule, physical_summary, platform
        )
        physical_binding_validation = {
            "status": physical_binding["status"],
            **physical_binding["metrics"],
        }
    qor = aggregate_qor(
        runtime,
        read_json(phase3_report_path),
        read_json(phase4_report_path),
        phase5_report,
        phase6_report,
        physical_summary,
        platform,
        routes=routes,
        schedule=schedule,
        routes_artifact_sha256=routes_artifact_sha256,
    )
    if physical_binding_validation is not None:
        qor["physical_evidence_completeness"] = physical_binding_validation
        qor["timing"]["physical_evidence_completeness"] = (
            physical_binding_validation
        )
        if (
            physical_binding_validation["status"] == "incomplete"
            and qor["status"] == "pass"
        ):
            qor["status"] = "incomplete"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "runtime_contract.json", runtime)
    write_json(output_dir / "qor_report.json", qor)
    if physical_summary is not None:
        write_json(output_dir / "system_timing.json", qor["timing"])
        write_json(
            output_dir / "cross_layer_physical_binding.json",
            physical_binding,
        )
    if physical_summary is not None and materialize_physical_summary:
        write_json(output_dir / "physical_summary.json", physical_summary)
    (output_dir / "virtual_runtime_controller.sv").write_text(
        virtual_runtime_controller_to_systemverilog(),
        encoding="utf-8",
    )
    (output_dir / "virtual_runtime_controller_tb.sv").write_text(
        runtime_controller_testbench(
            runtime,
            [fpga.id for fpga in platform.fpgas],
            frames=simulation_frames,
        ),
        encoding="utf-8",
    )
    (output_dir / "runtime_timing.xdc").write_text(
        runtime_timing_xdc(runtime),
        encoding="utf-8",
    )
    report = {
        "schema": PHASE7C_REPORT_SCHEMA,
        "phase": "7C",
        "increment": "unified-physical-link-tdm-system-timing",
        "status": (
            "pass"
            if qor["status"] == "pass"
            and (
                physical_binding_validation is None
                or physical_binding_validation["status"] == "pass"
            )
            else "incomplete"
            if qor["status"] == "incomplete"
            or (
                physical_binding_validation is not None
                and physical_binding_validation["status"] == "incomplete"
            )
            else "generated"
            if qor["status"] == "pending"
            else "fail"
        ),
        "design": runtime["design"],
        "platform": platform.name,
        "provider": runtime["provider"],
        "validation": validation,
        "functional_equivalence": {
            "status": (
                "pass"
                if phase6_report["equivalence"].get("mismatches") == 0
                else "fail"
            ),
            "cycles": phase6_report["equivalence"].get("cycles"),
            "trace_sha256": phase6_report["equivalence"].get(
                "trace_sha256"
            ),
        },
        "schedule_legality": {
            "status": (
                "pass"
                if phase5_report.get("status") == "pass"
                and phase5_report.get("validation", {}).get("collisions") == 0
                else "fail"
            ),
            "collisions": phase5_report.get("validation", {}).get(
                "collisions"
            ),
            "cross_layer_timing": phase5_report.get(
                "cross_layer_timing_validation"
            ),
        },
        "physical": qor["physical"],
        "artifacts": {
            "runtime_contract": "runtime_contract.json",
            "runtime_controller_rtl": "virtual_runtime_controller.sv",
            "runtime_controller_testbench": (
                "virtual_runtime_controller_tb.sv"
            ),
            "runtime_timing_xdc": "runtime_timing.xdc",
            "qor_report": "qor_report.json",
            "report": "phase7c_report.json",
        },
    }
    if physical_summary is not None:
        report["system_timing"] = qor["timing"]
        report["physical_evidence_completeness"] = (
            physical_binding_validation
        )
        report["target_clock_wns_ns"] = qor["timing"]["target_clock"][
            "worst_slack_bound_ns"
        ]
        report["target_clock_tns_ns"] = qor["timing"]["target_clock"][
            "total_negative_slack_bound_ns"
        ]
        report["virtual_runtime_wns_ns"] = qor["timing"]["runtime_clock"][
            "worst_slack_bound_ns"
        ]
        report["virtual_runtime_tns_ns"] = qor["timing"]["runtime_clock"][
            "total_negative_slack_bound_ns"
        ]
        if materialize_physical_summary:
            report["artifacts"]["physical_summary"] = "physical_summary.json"
        report["artifacts"]["system_timing"] = "system_timing.json"
        report["artifacts"]["cross_layer_physical_binding"] = (
            "cross_layer_physical_binding.json"
        )
    else:
        report["runtime_timing"] = qor["timing"]
    write_json(output_dir / "phase7c_report.json", report)
    return report
