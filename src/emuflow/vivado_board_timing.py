"""Routed timing feedback for a board-integrated Vivado implementation."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Mapping

from .board_link_timing import (
    build_board_link_timing_model,
    validate_board_link_timing,
)
from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .boundary_timing import validate_boundary_timing_database
from .logic_segment_timing import (
    import_vivado_logic_segment_timing,
    validate_logic_segment_timing,
    write_vivado_logic_segment_query,
)
from .phase7c import run_phase7c
from .platform import Platform
from .vivado_backend import (
    _BOUNDARY_TIMING_SCRIPT,
    _LOGIC_TIMING_SCRIPT,
    _run_vivado,
    import_vivado_boundary_timing,
    write_vivado_boundary_timing_query,
)
from .vivado_board_flow import (
    VIVADO_BOARD_FLOW_SCHEMA,
    VIVADO_BOARD_FLOW_SCHEMA_V2,
    validate_vivado_board_flow_bundle,
    validate_vivado_board_flow_report,
)


VIVADO_BOARD_TIMING_SCHEMA = "emuflow.vivado-board-timing/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> Dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValidationError(f"board timing artifact is missing: {path}")
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def validate_vivado_board_timing_report(
    report: Mapping[str, Any],
) -> Dict[str, Any]:
    if report.get("schema") != VIVADO_BOARD_TIMING_SCHEMA:
        raise ValidationError("Vivado board timing schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("Vivado board timing did not pass")
    if report.get("qualification") not in {
        "routed-board-stage-timing-plus-link-model-only",
        "routed-board-maxima-plus-interface-measurements-link-model-only",
    }:
        raise ValidationError("Vivado board timing qualification is invalid")
    records = report.get("fpgas")
    if not isinstance(records, list) or not records:
        raise ValidationError("Vivado board timing FPGA inventory is empty")
    ids = [item.get("fpga") for item in records]
    if (
        len(set(ids)) != len(ids)
        or any(not isinstance(item, str) or not item for item in ids)
        or any(
            item.get("status") != "pass"
            or item.get("boundary_endpoints", 0) <= 0
            or item.get("logic_segments", 0) < 0
            or item.get("missing_logic_segments", 0) < 0
            or (
                "endpoint_exact_logic_segments" in item
                and (
                    item.get("endpoint_exact_logic_segments", -1) < 0
                    or item.get("cone_bound_logic_segments", -1) < 0
                    or item["endpoint_exact_logic_segments"]
                    + item["cone_bound_logic_segments"]
                    != item["logic_segments"]
                )
            )
            for item in records
        )
    ):
        raise ValidationError("Vivado board timing FPGA coverage is invalid")
    link_model = report.get("board_link_timing")
    if (
        not isinstance(link_model, dict)
        or link_model.get("status") not in {
            "modeled-or-characterized-upper-bound",
            "measured-upper-bound",
            "modeled-not-measured",
        }
        or link_model.get("final_system_signoff") is not False
        or not isinstance(
            link_model.get("final_link_timing_signoff", False), bool
        )
    ):
        raise ValidationError("Vivado board link timing boundary is invalid")
    system = report.get("system_timing")
    if (
        not isinstance(system, dict)
        or system.get("status") not in {"pass", "fail"}
        or not isinstance(system.get("timing_paths"), int)
        or system["timing_paths"] <= 0
    ):
        raise ValidationError("Vivado board system timing summary is invalid")
    return {
        "status": "pass",
        "design": report.get("design"),
        "platform": report.get("platform"),
        "fpgas": len(records),
        "boundary_endpoints": sum(
            item["boundary_endpoints"] for item in records
        ),
        "logic_segments": sum(item["logic_segments"] for item in records),
        "endpoint_exact_logic_segments": sum(
            item.get("endpoint_exact_logic_segments", item["logic_segments"])
            for item in records
        ),
        "cone_bound_logic_segments": sum(
            item.get("cone_bound_logic_segments", 0) for item in records
        ),
        "missing_logic_segments": sum(
            item.get("missing_logic_segments", 0) for item in records
        ),
        "runtime_wns_ns": system.get("runtime_wns_ns"),
        "final_system_signoff": False,
    }


def run_vivado_board_timing(
    *,
    flow_root: Path,
    board_root: Path,
    platform_path: Path,
    vivado_executable: Path,
    output_dir: Path,
    hierarchy_prefix: str = "mapped_partition",
    workers: int = 3,
    resume: bool = False,
    link_timing_path: Path | None = None,
) -> Dict[str, Any]:
    """Feed routed board DCP timing back into the unified Phase-7C model.

    FPGA-internal logic and mapped-partition boundary delays are queried from
    the joint DUT+transport+PCS+GT checkpoint.  Board-to-board propagation is
    deliberately retained as a BoardDB model until measured or source-backed
    board data is available.
    """

    flow_root = flow_root.resolve()
    board_root = board_root.resolve()
    platform_path = platform_path.resolve()
    vivado_executable = vivado_executable.resolve()
    output_dir = output_dir.resolve()
    if not hierarchy_prefix or "/" in hierarchy_prefix:
        raise ValidationError("board timing hierarchy prefix is invalid")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
        raise ValidationError("board timing worker count is invalid")
    if output_dir.exists() and not output_dir.is_dir():
        raise EmuFlowError(f"Vivado board timing output must be empty: {output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise EmuFlowError(f"Vivado board timing output must be empty: {output_dir}")
    if not vivado_executable.is_file():
        raise ValidationError("Vivado board timing executable is missing")

    board_report_path = board_root / "vivado-board-flow-report.json"
    flow_report_candidates = (
        flow_root / "board-independent-flow-report.json",
        flow_root / "multi-fpga-flow-report.json",
    )
    if not board_report_path.is_file():
        raise ValidationError("Vivado board timing source report is missing")
    board_report = read_json(board_report_path)
    validate_vivado_board_flow_report(board_report)
    if board_report.get("schema") in {
        VIVADO_BOARD_FLOW_SCHEMA_V2,
        VIVADO_BOARD_FLOW_SCHEMA,
    }:
        validate_vivado_board_flow_bundle(board_root)
    expected_flow_hash = board_report["source_bindings"]["flow_report_sha256"]
    flow_report_path = next(
        (
            path
            for path in flow_report_candidates
            if path.is_file() and _sha256(path) == expected_flow_hash
        ),
        None,
    )
    if flow_report_path is None:
        raise ValidationError(
            "Vivado board timing implementation is not bound to this flow"
        )
    flow_report = read_json(flow_report_path)
    physical_summary_path = flow_root / "physical/physical-summary.json"
    if not physical_summary_path.is_file():
        raise ValidationError("Vivado board timing physical summary is missing")
    source_physical = read_json(physical_summary_path)
    platform = Platform.load(platform_path)
    if link_timing_path is None:
        link_timing = build_board_link_timing_model(platform)
    else:
        link_timing_path = link_timing_path.resolve()
        link_timing = read_json(link_timing_path)
    link_timing_validation = validate_board_link_timing(
        link_timing, platform
    )
    if source_physical.get("platform") != platform.name or not isinstance(
        source_physical.get("design"), str
    ):
        raise ValidationError("Vivado board timing source identities disagree")
    board_by_fpga = {item["fpga"]: item for item in board_report["fpgas"]}
    expected_fpgas = {fpga.id for fpga in platform.fpgas}
    if set(board_by_fpga) != expected_fpgas:
        raise ValidationError("Vivado board timing does not cover BoardDB")

    required_flow_paths = {
        "original_ir": flow_root / "frontend/phase1/design.emuir.json",
        "assignment": flow_root / "partition/assignment.json",
        "path_database": flow_root / "timing/path-database.json",
        "routes": flow_root / "system-route/routes.json",
        "schedule": flow_root / "tdm/schedule.json",
    }
    missing = [name for name, path in required_flow_paths.items() if not path.is_file()]
    if missing:
        raise ValidationError(
            f"Vivado board timing source artifacts are missing: {missing}"
        )

    output_dir.mkdir(parents=True, exist_ok=resume)
    def measure_fpga(fpga: Any) -> tuple[Dict[str, Any], Any, Any]:
        fpga_id = fpga.id
        source_root = flow_root / "physical" / fpga_id
        fpga_out = output_dir / fpga_id
        fpga_out.mkdir(exist_ok=resume)
        merged_ir = source_root / "placement.emuir.json"
        identities = source_physical.get("boundary_identities", {}).get(fpga_id)
        if not isinstance(identities, dict) or not merged_ir.is_file():
            raise ValidationError(f"{fpga_id}: physical timing identity is missing")
        identity_path = fpga_out / "boundary-identities.json"
        write_json(identity_path, identities)

        board_dcp_info = board_by_fpga[fpga_id].get("artifacts", {}).get(
            "routed.dcp"
        )
        if not isinstance(board_dcp_info, dict):
            raise ValidationError(f"{fpga_id}: routed board DCP is missing")
        dcp = Path(board_dcp_info.get("path", ""))
        if not dcp.is_absolute():
            dcp = board_root / dcp
        dcp = dcp.resolve()
        if (
            not dcp.is_file()
            or _sha256(dcp) != board_dcp_info.get("sha256")
        ):
            raise ValidationError(f"{fpga_id}: routed board DCP hash disagrees")

        boundary_database = fpga_out / "boundary-timing.json"
        logic_database = fpga_out / "logic-segment-timing.json"
        if resume and boundary_database.is_file() and logic_database.is_file():
            boundary_database_value = read_json(boundary_database)
            boundary_validation = validate_boundary_timing_database(
                boundary_database_value, identities
            )
            logic_database_value = read_json(logic_database)
            logic_validation = validate_logic_segment_timing(
                logic_database_value
            )
            missing_segments = len(
                logic_database_value.get("unmeasured_segments", [])
            )
            record = {
                "fpga": fpga_id,
                "status": "pass",
                "hierarchy_prefix": hierarchy_prefix,
                "boundary_endpoints": boundary_validation["endpoints"],
                "logic_segments": logic_validation["segments"],
                "endpoint_exact_logic_segments": logic_validation[
                    "endpoint_exact_segments"
                ],
                "cone_bound_logic_segments": logic_validation[
                    "cone_bound_segments"
                ],
                "missing_logic_segments": missing_segments,
                "unsupported_logic_member_paths": len(
                    logic_database_value.get("unsupported_member_paths", [])
                ),
                "maximum_boundary_delay_ns": boundary_validation[
                    "maximum_delay_ns"
                ],
                "maximum_logic_segment_delay_ns": logic_validation[
                    "maximum_delay_ns"
                ],
                "queries": {"status": "reused-hash-validated"},
                "exports": {"status": "reused-hash-validated"},
                "artifacts": {
                    "boundary_timing": _artifact(boundary_database),
                    "logic_segment_timing": _artifact(logic_database),
                },
            }
            return record, boundary_database_value, logic_database_value

        boundary_query = fpga_out / "boundary-timing-query.tsv"
        boundary_query_report = write_vivado_boundary_timing_query(
            merged_ir, identity_path, boundary_query
        )
        boundary_tsv = fpga_out / "boundary-timing.tsv"
        boundary_export = _run_vivado(
            vivado_executable,
            _BOUNDARY_TIMING_SCRIPT,
            [str(dcp), str(boundary_query), str(boundary_tsv), hierarchy_prefix],
            fpga_out,
            "vivado-boundary-timing-export.log",
        )
        boundary_import = import_vivado_boundary_timing(
            boundary_tsv,
            identity_path,
            boundary_database,
            provider="vivado-board-integrated-interface-path-v1",
            qualification="routed-board-integrated-interface-path",
            measurement_scope="board-integrated-interface",
        )
        boundary_database_value = read_json(boundary_database)

        logic_query = fpga_out / "logic-segment-timing-query.tsv"
        logic_identity = fpga_out / "logic-segment-identities.json"
        logic_query_report = write_vivado_logic_segment_query(
            required_flow_paths["original_ir"],
            required_flow_paths["assignment"],
            required_flow_paths["path_database"],
            required_flow_paths["routes"],
            required_flow_paths["schedule"],
            platform,
            merged_ir,
            identity_path,
            fpga_id,
            logic_query,
            logic_identity,
        )
        logic_tsv = fpga_out / "logic-segment-timing.tsv"
        logic_export = _run_vivado(
            vivado_executable,
            _LOGIC_TIMING_SCRIPT,
            [
                str(dcp),
                str(logic_query),
                str(logic_tsv),
                hierarchy_prefix,
                "allow-missing",
            ],
            fpga_out,
            "vivado-logic-segment-timing-export.log",
        )
        logic_import = import_vivado_logic_segment_timing(
            logic_tsv,
            logic_identity,
            logic_database,
            provider="vivado-board-integrated-logic-to-interface-path-v1",
            qualification="routed-board-integrated-endpoint-chain",
            allow_missing=True,
        )
        logic_database_value = read_json(logic_database)
        record = {
            "fpga": fpga_id,
            "status": "pass",
            "hierarchy_prefix": hierarchy_prefix,
            "boundary_endpoints": boundary_import["endpoints"],
            "logic_segments": logic_import["segments"],
            "endpoint_exact_logic_segments": logic_import[
                "endpoint_exact_segments"
            ],
            "cone_bound_logic_segments": logic_import[
                "cone_bound_segments"
            ],
            "missing_logic_segments": logic_import["missing_segments"],
            "unsupported_logic_member_paths": logic_query_report[
                "unsupported_member_paths"
            ],
            "maximum_boundary_delay_ns": boundary_import["maximum_delay_ns"],
            "maximum_logic_segment_delay_ns": logic_import["maximum_delay_ns"],
            "queries": {
                "boundary": boundary_query_report,
                "logic": logic_query_report,
            },
            "exports": {
                "boundary": boundary_export,
                "logic": logic_export,
            },
            "artifacts": {
                "boundary_timing": _artifact(boundary_database),
                "logic_segment_timing": _artifact(logic_database),
            },
        }
        return record, boundary_database_value, logic_database_value

    ordered_fpgas = sorted(platform.fpgas, key=lambda item: item.id)
    with ThreadPoolExecutor(max_workers=min(workers, len(ordered_fpgas))) as pool:
        measurements = list(pool.map(measure_fpga, ordered_fpgas))
    records = [item[0] for item in measurements]
    boundary_timing = {
        record["fpga"]: measurement[1]
        for record, measurement in zip(records, measurements)
    }
    logic_timing = {
        record["fpga"]: measurement[2]
        for record, measurement in zip(records, measurements)
    }

    board_physical_fpgas = []
    for source_item in source_physical["fpgas"]:
        fpga_id = source_item["fpga"]
        raw_delay = board_by_fpga[fpga_id]["closure"].get("critical_path_ns")
        try:
            board_delay = float(raw_delay)
        except (TypeError, ValueError) as error:
            raise ValidationError(
                f"{fpga_id}: board critical-path delay is invalid"
            ) from error
        if board_delay < 0.0:
            raise ValidationError(
                f"{fpga_id}: board critical-path delay is negative"
            )
        board_physical_fpgas.append(
            {
                **source_item,
                "critical_path_ns": board_delay,
                "clock_domain_delays_ns": {
                    "dut": board_delay,
                    "cross": board_delay,
                },
                "system_timing_bound_source": (
                    "vivado-board-routed-checkpoint-worst-datapath"
                ),
            }
        )
    feedback_summary = dict(source_physical)
    feedback_summary.update(
        {
            "provider": "vivado-board-integrated-timing-feedback-v1",
            "qualification": "routed-board-stage-timing-link-model-only",
            "fpgas": board_physical_fpgas,
            "boundary_timing": boundary_timing,
            "logic_segment_timing": logic_timing,
            "board_link_timing": link_timing,
            "timing_component_provenance": {
                "fpga_logic_bound": (
                    "vivado-board-routed-checkpoint-worst-datapath"
                ),
                "logic_segment_diagnostics": (
                    "vivado-board-routed-dcp-staging-aware-exact-"
                    "when-complete"
                ),
                "partition_boundary_endpoints": "vivado-board-routed-dcp",
                "board_link_propagation": "platform-model-not-measured",
                "tdm_schedule": "phase5-concrete-schedule",
            },
        }
    )
    feedback_summary_path = output_dir / "physical-summary.json"
    write_json(feedback_summary_path, feedback_summary)

    phase7c_root = output_dir / "runtime"
    phase7c = run_phase7c(
        required_flow_paths["schedule"],
        platform_path,
        flow_root / "partition/phase3_report.json",
        flow_root / "system-route/phase4_report.json",
        flow_root / "tdm/phase5_report.json",
        flow_root / "split/phase6_report.json",
        phase7c_root,
        assignment_path=flow_root / "partition/assignment.json",
        physical_summary_path=feedback_summary_path,
        routes_path=required_flow_paths["routes"],
    )
    system = phase7c["system_timing"]
    system_summary = {
        "status": system["status"],
        "qualification": system["qualification"],
        "timing_paths": system["summary"]["timing_paths"],
        "maximum_system_delay_bound_ns": system["summary"][
            "maximum_system_delay_bound_ns"
        ],
        "runtime_period_ns": system["runtime_clock"]["period_ns"],
        "runtime_wns_ns": system["runtime_clock"]["worst_slack_bound_ns"],
        "path_exactness": system["path_exactness"],
    }
    report = {
        "schema": VIVADO_BOARD_TIMING_SCHEMA,
        "status": "pass",
        "qualification": "routed-board-stage-timing-plus-link-model-only",
        "design": source_physical["design"],
        "platform": platform.name,
        "source_bindings": {
            "flow_report_sha256": _sha256(flow_report_path),
            "board_flow_report_sha256": _sha256(board_report_path),
        },
        "fpgas": records,
        "parallel_workers": min(workers, len(ordered_fpgas)),
        "board_link_timing": {
            "status": (
                "measured-upper-bound"
                if link_timing_validation["final_link_timing_signoff"]
                else "modeled-or-characterized-upper-bound"
            ),
            "source": "BoardLinkTimingDB and Phase-5 TDM schedule",
            "validation": link_timing_validation,
            "known": [
                "routed FPGA logic segment delay for path-continuous segments",
                "routed mapped-partition boundary delay",
                "TDM slot precedence and serialization delay",
                *(
                    ["measured TX-stage-to-RX-stage board-link upper bounds"]
                    if link_timing_validation["final_link_timing_signoff"]
                    else []
                ),
            ],
            "unknown": (
                []
                if link_timing_validation["final_link_timing_signoff"]
                else [
                    "PCB trace and connector propagation",
                    "GT/PCS elastic-buffer and clock-domain latency",
                    "board-to-board skew under hardware operating conditions",
                ]
            ),
            "final_link_timing_signoff": link_timing_validation[
                "final_link_timing_signoff"
            ],
            "final_system_signoff": False,
        },
        "system_timing": system_summary,
        "artifacts": {
            "physical_summary": _artifact(feedback_summary_path),
            "phase7c_report": _artifact(phase7c_root / "phase7c_report.json"),
            "system_timing": _artifact(phase7c_root / "system_timing.json"),
        },
    }
    report["summary"] = validate_vivado_board_timing_report(report)
    write_json(output_dir / "vivado-board-timing-report.json", report)
    return report
