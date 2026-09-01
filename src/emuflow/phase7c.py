from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from .board_link_timing import validate_board_link_timing
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
        validate_board_link_timing(board_link_timing, platform)
        physical_summary = dict(physical_summary)
        physical_summary["board_link_timing"] = board_link_timing
    routes = read_json(routes_path) if routes_path is not None else None
    routes_artifact_sha256 = (
        _sha256(routes_path) if routes_path is not None else None
    )
    qor = aggregate_qor(
        runtime,
        read_json(phase3_report_path),
        read_json(phase4_report_path),
        read_json(phase5_report_path),
        read_json(phase6_report_path),
        physical_summary,
        platform,
        routes=routes,
        schedule=schedule,
        routes_artifact_sha256=routes_artifact_sha256,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "runtime_contract.json", runtime)
    write_json(output_dir / "qor_report.json", qor)
    if physical_summary is not None:
        write_json(output_dir / "system_timing.json", qor["timing"])
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
            else "incomplete"
            if qor["status"] == "incomplete"
            else "generated"
            if qor["status"] == "pending"
            else "fail"
        ),
        "design": runtime["design"],
        "platform": platform.name,
        "provider": runtime["provider"],
        "validation": validation,
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
        if materialize_physical_summary:
            report["artifacts"]["physical_summary"] = "physical_summary.json"
        report["artifacts"]["system_timing"] = "system_timing.json"
    else:
        report["runtime_timing"] = qor["timing"]
    write_json(output_dir / "phase7c_report.json", report)
    return report
