"""Checked, board-independent multi-FPGA compilation orchestration."""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .board_link_timing import (
    directed_route_link_delays,
    validate_board_link_timing,
)
from .academic_chimew import materialize_academic_chimew_inputs
from .chimew_pipeline import run_chimew_phase6_pipeline
from .cross_stage import run_cross_stage_optimization
from .cut_segment_qualification import build_cut_segment_qualification
from .errors import EmuFlowError, ValidationError
from .frame_search import (
    run_frame_length_search,
    validate_frame_search_report,
)
from .io import read_json, write_json
from .multi_fpga_physical_flow import (
    run_multi_fpga_physical_flow,
    validate_multi_fpga_physical_report,
)
from .multi_fpga_bsp_flow import (
    run_multi_fpga_bsp_flow,
    validate_multi_fpga_bsp_flow_report,
)
from .opensta import DEFAULT_TIMING_MODEL, run_opensta_path_database
from .phase1 import run_phase1
from .phase3 import promote_patron_baseline, run_phase3, validate_phase3
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5
from .phase6 import run_phase6, validate_phase6
from .phase7c import run_phase7c
from .partition import CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT
from .combinational_cut import (
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)
from .mfspart_refine import DEFAULT_TIMING_PATH_BETA
from .platform import Platform
from .runtime import validate_virtual_runtime
from .routing import SYSTEM_ROUTE_CONSTRAINTS_SCHEMA
from .tdm import TDM_BASELINE_PROVIDER, TDM_STATIC_EXACT_PROVIDER
from .timing_routing import (
    NATIVE_ROUTER_PROVIDER,
    NATIVE_TIMING_EVALUATED_PROVIDER,
)
from .sta import (
    derive_partition_net_weights,
    project_sta_path_database,
)
from .synthesis import run_generic_yosys
from .vivado_backend import run_vivado_timing_path_database
from .vpr import VTR_HARD_BLOCK_PROFILE, run_vtr_yosys
from .vtr_netlist import normalize_vtr_hard_block_json


MULTI_FPGA_FLOW_SCHEMA = "emuflow.multi-fpga-flow/v2"
MULTI_FPGA_FLOW_PROVIDER = (
    "profiled-yosys+partition+system-route+tdm+split-transport+runtime"
)
MULTI_FPGA_MAPPING_PROFILES = ("vtr-hard-blocks", "generic-soft")
MULTI_FPGA_PHASE6_PROVIDERS = ("auto", "chimew", "baseline")
PHASE6_AB_COMPARISON_SCHEMA = "emuflow.phase6-ab-comparison/v2"
_REQUIRED_STAGES = ("frontend", "partition", "system_route", "tdm", "split")


def _checked_flow_member(root: Path, path: Path, label: str) -> Path:
    """Return a regular, non-symlink file contained by a flow root."""

    root = root.resolve()
    path = path if path.is_absolute() else root / path
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"multi-FPGA checkpoint {label} is missing")
    resolved = path.resolve()
    if resolved.parent != root and root not in resolved.parents:
        raise ValidationError(f"multi-FPGA checkpoint {label} escapes its flow")
    return resolved


def finalize_multi_fpga_physical_checkpoint(
    flow_root: Path,
    physical_root: Path,
    *,
    runtime_directory: str = "runtime-final",
) -> Dict[str, Any]:
    """Finish Phase 7C and seal a flow after an independently resumed Phase 7.

    Physical backends are intentionally restartable because their external tool
    environments can fail after Phase 1--6 have completed.  This entry point
    consumes only checked artifacts already below ``flow_root``; it does not
    rerun or silently alter any earlier optimization stage.
    """

    root = flow_root.resolve()
    if not root.is_dir():
        raise ValidationError("multi-FPGA checkpoint flow root is missing")
    if (
        not runtime_directory
        or Path(runtime_directory).is_absolute()
        or len(Path(runtime_directory).parts) != 1
        or runtime_directory in {".", ".."}
    ):
        raise ValidationError("multi-FPGA checkpoint runtime directory is unsafe")
    physical_root = (
        physical_root if physical_root.is_absolute() else root / physical_root
    )
    physical_report_path = _checked_flow_member(
        root,
        physical_root / "multi-fpga-physical-flow-report.json",
        "physical flow report",
    )
    physical_summary_path = _checked_flow_member(
        root,
        physical_root / "physical-summary.json",
        "physical summary",
    )
    members = {
        "frontend": "frontend/phase1/phase1_report.json",
        "platform": "frontend/phase1/platform.normalized.json",
        "emuir": "frontend/phase1/design.emuir.json",
        "partition": "partition/phase3_report.json",
        "assignment": "partition/assignment.json",
        "system_route": "system-route/phase4_report.json",
        "routes": "system-route/routes.json",
        "tdm": "tdm/phase5_report.json",
        "schedule": "tdm/schedule.json",
        "split": "split/phase6_report.json",
        "split_manifest": "split/manifest.json",
    }
    paths = {
        key: _checked_flow_member(root, Path(value), key)
        for key, value in members.items()
    }
    stages = {
        name: read_json(paths[name])
        for name in ("frontend", "partition", "system_route", "tdm", "split")
    }
    physical_report = read_json(physical_report_path)
    validate_multi_fpga_physical_report(physical_report)

    runtime_root = root / runtime_directory
    if runtime_root.is_symlink():
        raise ValidationError("multi-FPGA checkpoint runtime directory is a symlink")
    runtime_report = run_phase7c(
        paths["schedule"],
        paths["platform"],
        paths["partition"],
        paths["system_route"],
        paths["tdm"],
        paths["split"],
        runtime_root,
        physical_summary_path=physical_summary_path,
        routes_path=paths["routes"],
    )
    if runtime_report.get("status") != "pass":
        raise ValidationError("resumed physical flow did not close Phase 7C timing")

    def artifact(path: Path) -> Dict[str, str]:
        checked = _checked_flow_member(root, path, path.name)
        return {
            "path": checked.relative_to(root).as_posix(),
            "sha256": _sha256(checked),
        }

    report: Dict[str, Any] = {
        "schema": MULTI_FPGA_FLOW_SCHEMA,
        "status": "pass",
        "provider": MULTI_FPGA_FLOW_PROVIDER,
        "architecture_policy": "provider-neutral",
        "runtime": runtime_report,
        "physical": physical_report,
        "stages": stages,
        "artifacts": {
            "platform": artifact(paths["platform"]),
            "emuir": artifact(paths["emuir"]),
            "partition_constraints": artifact(
                root / "partition/constraints.normalized.json"
            ),
            "route_constraints": artifact(
                root / "system-route/route_constraints.normalized.json"
            ),
            "assignment": artifact(paths["assignment"]),
            "routes": artifact(paths["routes"]),
            "schedule": artifact(paths["schedule"]),
            "split_manifest": artifact(paths["split_manifest"]),
            "runtime_contract": artifact(runtime_root / "runtime_contract.json"),
            "qor_report": artifact(runtime_root / "qor_report.json"),
            "physical_flow_report": artifact(physical_report_path),
            "physical_summary": artifact(physical_summary_path),
        },
    }
    optional = {
        "timing_path_database": root / "timing/path-database.json",
        "partition_net_weights": root / "timing/partition-net-weights.json",
        "cut_segment_qualification": root / "timing/cut-segment-qualification.json",
        "cut_timing_paths": root / "timing/cut-timing-paths.json",
    }
    for name, path in optional.items():
        if path.is_file() and not path.is_symlink():
            report["artifacts"][name] = artifact(path)
    report["summary"] = validate_multi_fpga_flow_report(report)
    write_json(root / "multi-fpga-flow-report.json", report)
    return report


def _phase6_physical_metrics(value: Dict[str, Any]) -> Dict[str, Any]:
    records = value["fpgas"]
    return {
        "total_wirelength": sum(
            int(record["stages"]["vpr_route"]["metrics"]["wirelength"])
            for record in records
        ),
        "worst_critical_path_ns": max(
            float(record["critical_path_ns"]) for record in records
        ),
        "worst_wns_ns": min(
            float(record["physical_result"]["timing"]["wns_ns"])
            for record in records
        ),
        "total_tns_ns": sum(
            float(record["physical_result"]["timing"]["tns_ns"])
            for record in records
        ),
        "failing_endpoints": sum(
            int(record["physical_result"]["timing"]["failing_endpoints"])
            for record in records
        ),
        "failing_endpoint_constraints": sum(
            int(
                record["physical_result"]["timing"][
                    "failing_endpoint_constraints"
                ]
            )
            for record in records
        ),
        "unrouted_nets": sum(
            int(record["physical_result"]["closure"]["unrouted_nets"])
            for record in records
        ),
        "drc_violations": sum(
            int(record["physical_result"]["closure"]["drc_violations"])
            for record in records
        ),
    }


def _phase6_system_timing_metrics(value: Dict[str, Any]) -> Dict[str, Any]:
    timing = value.get("system_timing")
    if not isinstance(timing, dict) or timing.get("status") not in {
        "pass",
        "fail",
    }:
        raise ValidationError("Phase 6 A/B arm lacks Phase 7C system timing")
    metrics: Dict[str, Any] = {}
    for clock in ("target_clock", "runtime_clock"):
        record = timing.get(clock)
        if not isinstance(record, dict):
            raise ValidationError(
                f"Phase 6 A/B {clock} system timing is missing"
            )
        for field in (
            "worst_slack_bound_ns",
            "total_negative_slack_bound_ns",
            "negative_slack_paths",
        ):
            metric = record.get(field)
            if (
                isinstance(metric, bool)
                or not isinstance(metric, (int, float))
                or not math.isfinite(float(metric))
            ):
                raise ValidationError(
                    f"Phase 6 A/B {clock}.{field} is invalid"
                )
            metrics[f"{clock}_{field}"] = metric
    return metrics


def validate_phase6_ab_comparison(report: Dict[str, Any]) -> Dict[str, Any]:
    if (
        report.get("schema") != PHASE6_AB_COMPARISON_SCHEMA
        or report.get("status") != "pass"
        or report.get("selected_provider") != "chimew"
        or report.get("baseline_provider") != "historical-default-static-phase6"
        or report.get("qualification") != "academic-virtual-physical-model"
    ):
        raise ValidationError("Phase 6 A/B comparison identity is invalid")
    upstream = report.get("frozen_upstream")
    if not isinstance(upstream, dict) or set(upstream) != {
        "emuir_sha256",
        "assignment_sha256",
        "routes_sha256",
        "schedule_sha256",
        "platform_sha256",
    }:
        raise ValidationError("Phase 6 A/B frozen-upstream seal is invalid")
    for digest in upstream.values():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError("Phase 6 A/B upstream digest is invalid")
    arms = (report.get("baseline"), report.get("chimew"))
    if not all(isinstance(arm, dict) for arm in arms):
        raise ValidationError("Phase 6 A/B arms are incomplete")
    baseline_arm, chimew_arm = arms
    for label, arm in (("baseline", baseline_arm), ("chimew", chimew_arm)):
        physical_report = arm.get("physical")
        if not isinstance(physical_report, dict):
            raise ValidationError(f"Phase 6 A/B {label} physical report is missing")
        validate_multi_fpga_physical_report(physical_report)
        runtime_report = arm.get("runtime")
        if not isinstance(runtime_report, dict):
            raise ValidationError(f"Phase 6 A/B {label} runtime report is missing")
    baseline = report.get("baseline_physical")
    chimew = report.get("chimew_physical")
    delta = report.get("physical_delta")
    required_metrics = {
        "total_wirelength",
        "worst_critical_path_ns",
        "worst_wns_ns",
        "total_tns_ns",
        "failing_endpoints",
        "failing_endpoint_constraints",
        "unrouted_nets",
        "drc_violations",
    }
    if not all(
        isinstance(value, dict) and set(value) == required_metrics
        for value in (baseline, chimew)
    ) or not isinstance(delta, dict):
        raise ValidationError("Phase 6 A/B physical metrics are incomplete")
    if baseline != _phase6_physical_metrics(baseline_arm["physical"]):
        raise ValidationError("Phase 6 A/B baseline metrics were not reconstructed")
    if chimew != _phase6_physical_metrics(chimew_arm["physical"]):
        raise ValidationError("Phase 6 A/B Chimew metrics were not reconstructed")
    for metrics in (baseline, chimew):
        for key, value in metrics.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"Phase 6 A/B metric {key!r} is not finite"
                )
        if metrics["unrouted_nets"] != 0 or metrics["drc_violations"] != 0:
            raise ValidationError("Phase 6 A/B physical arm did not close")
    expected_delta = {
        "total_wirelength": (
            chimew["total_wirelength"] - baseline["total_wirelength"]
        ),
        "worst_critical_path_ns": (
            chimew["worst_critical_path_ns"]
            - baseline["worst_critical_path_ns"]
        ),
        "worst_wns_ns": chimew["worst_wns_ns"] - baseline["worst_wns_ns"],
        "total_tns_ns": chimew["total_tns_ns"] - baseline["total_tns_ns"],
        "failing_endpoints": (
            chimew["failing_endpoints"] - baseline["failing_endpoints"]
        ),
        "failing_endpoint_constraints": (
            chimew["failing_endpoint_constraints"]
            - baseline["failing_endpoint_constraints"]
        ),
    }
    if delta != expected_delta:
        raise ValidationError("Phase 6 A/B physical deltas disagree")
    baseline_system = report.get("baseline_system_timing")
    chimew_system = report.get("chimew_system_timing")
    system_delta = report.get("system_timing_delta")
    if not all(
        isinstance(value, dict)
        for value in (baseline_system, chimew_system, system_delta)
    ):
        raise ValidationError("Phase 6 A/B system timing metrics are incomplete")
    expected_baseline_system = _phase6_system_timing_metrics(
        baseline_arm["runtime"]
    )
    expected_chimew_system = _phase6_system_timing_metrics(
        chimew_arm["runtime"]
    )
    if baseline_system != expected_baseline_system:
        raise ValidationError("Phase 6 A/B baseline system timing was not reconstructed")
    if chimew_system != expected_chimew_system:
        raise ValidationError("Phase 6 A/B Chimew system timing was not reconstructed")
    expected_system_delta = {
        key: expected_chimew_system[key] - expected_baseline_system[key]
        for key in expected_baseline_system
    }
    if system_delta != expected_system_delta:
        raise ValidationError("Phase 6 A/B system timing deltas disagree")
    pin_metrics = report.get("pin_plan_metrics")
    if not isinstance(pin_metrics, dict) or pin_metrics.get("signals") is None:
        raise ValidationError("Phase 6 A/B Chimew pin metrics are missing")
    return {
        "status": "pass",
        "wirelength_delta": expected_delta["total_wirelength"],
        "critical_path_delta_ns": expected_delta["worst_critical_path_ns"],
        "wns_delta_ns": expected_delta["worst_wns_ns"],
        "tns_delta_ns": expected_delta["total_tns_ns"],
        "failing_endpoint_delta": expected_delta["failing_endpoints"],
        "target_clock_wns_delta_ns": expected_system_delta[
            "target_clock_worst_slack_bound_ns"
        ],
        "target_clock_tns_delta_ns": expected_system_delta[
            "target_clock_total_negative_slack_bound_ns"
        ],
        "runtime_clock_wns_delta_ns": expected_system_delta[
            "runtime_clock_worst_slack_bound_ns"
        ],
        "runtime_clock_tns_delta_ns": expected_system_delta[
            "runtime_clock_total_negative_slack_bound_ns"
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_multi_fpga_flow_report(
    report: Dict[str, Any],
) -> Dict[str, Any]:
    if report.get("schema") != MULTI_FPGA_FLOW_SCHEMA:
        raise ValidationError("multi-FPGA flow report schema is invalid")
    if report.get("status") != "pass":
        raise ValidationError("multi-FPGA flow report did not pass")
    stages = report.get("stages")
    if (
        not isinstance(stages, dict)
        or set(stages) != set(_REQUIRED_STAGES)
    ):
        raise ValidationError("multi-FPGA flow stages are incomplete")
    for name in _REQUIRED_STAGES:
        stage = stages[name]
        if not isinstance(stage, dict) or stage.get("status") != "pass":
            raise ValidationError(
                f"multi-FPGA flow stage {name!r} did not pass"
            )

    design = stages["frontend"].get("design")
    platform = stages["frontend"].get("platform")
    for name in _REQUIRED_STAGES[1:]:
        if stages[name].get("design") != design:
            raise ValidationError(
                f"multi-FPGA flow stage {name!r} design identity disagrees"
            )
        if stages[name].get("platform") != platform:
            raise ValidationError(
                f"multi-FPGA flow stage {name!r} platform identity disagrees"
            )

    partition_validation = stages["partition"].get("validation", {})
    route_validation = stages["system_route"].get("validation", {})
    tdm_validation = stages["tdm"].get("validation", {})
    split_validation = stages["split"].get("validation", {})
    equivalence = stages["split"].get("equivalence", {})
    timing = report.get("timing")
    timing_optimization = None
    if timing is not None:
        if (
            not isinstance(timing, dict)
            or timing.get("status") != "pass"
            or not isinstance(timing.get("optimization_enabled"), bool)
        ):
            raise ValidationError(
                "multi-FPGA TimingPathDB qualification is invalid"
            )
        timing_optimization = timing["optimization_enabled"]
        if timing_optimization and not isinstance(
            timing.get("partition_weights"), dict
        ):
            raise ValidationError(
                "timing-driven report is missing partition weights"
            )
        if not timing_optimization:
            expected_tdm_provider = (
                TDM_STATIC_EXACT_PROVIDER
                if partition_validation.get("cut_mode")
                == CUT_MODE_STATIC_EXACT
                else TDM_BASELINE_PROVIDER
            )
            if (
                stages["system_route"].get("provider")
                != NATIVE_TIMING_EVALUATED_PROVIDER
                or stages["tdm"].get("provider")
                != expected_tdm_provider
                or stages["tdm"].get("timing_validation", {}).get(
                    "status"
                )
                != "pass"
                or "partition_weights" in timing
                or timing.get("partition_weights_applied") is not False
            ):
                raise ValidationError(
                    "--no-timing-driven report does not preserve the "
                    "timing-evaluated Phase 3--5 baseline contract"
                )
    runtime = report.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("design") != design
        or runtime.get("platform") != platform
        or runtime.get("validation", {}).get("status") != "pass"
        or runtime.get("runtime_timing", {}).get("status") == "fail"
    ):
        raise ValidationError("multi-FPGA runtime contract did not pass")
    physical = report.get("physical")
    if physical is not None:
        physical_validation = validate_multi_fpga_physical_report(physical)
        if physical_validation["original_cells"] != partition_validation.get(
            "instances"
        ):
            raise ValidationError(
                "multi-FPGA physical original-cell coverage disagrees"
            )
        if runtime.get("physical", {}).get("status") != "pass":
            raise ValidationError(
                "multi-FPGA runtime is not closed against physical results"
            )
    phase6_comparison = report.get("phase6_comparison")
    phase6_comparison_validation = None
    if phase6_comparison is not None:
        if physical is None:
            raise ValidationError(
                "Phase 6 A/B comparison requires a physical flow result"
            )
        phase6_comparison_validation = validate_phase6_ab_comparison(
            phase6_comparison
        )
        if (
            phase6_comparison["chimew"]["physical"] != physical
            or phase6_comparison["chimew"]["phase6"] != stages["split"]
        ):
            raise ValidationError(
                "Phase 6 A/B selected arm differs from canonical flow"
            )
    link_timing = report.get("board_link_timing")
    if link_timing is not None:
        link_validation = (
            link_timing.get("validation")
            if isinstance(link_timing, dict)
            else None
        )
        route_projection = (
            link_timing.get("routing_projection")
            if isinstance(link_timing, dict)
            else None
        )
        link_optimization = (
            link_timing.get("optimization_enabled")
            if isinstance(link_timing, dict)
            else None
        )
        expected_applications = (
            [
                "phase4-system-routing",
                "phase5-tdm-ratio-and-schedule-timing",
                "phase7c-system-timing-when-physical",
            ]
            if link_optimization
            else [
                "phase4-post-route-timing-evaluation",
                "phase5-baseline-schedule-timing-evaluation",
                "phase7c-system-timing-when-physical",
            ]
        )
        if (
            not isinstance(link_timing, dict)
            or link_timing.get("status") != "pass"
            or not isinstance(link_optimization, bool)
            or not isinstance(link_validation, dict)
            or link_validation.get("status") != "pass"
            or not isinstance(route_projection, dict)
            or route_projection.get("status") != "pass"
            or link_timing.get("applied_to") != expected_applications
            or (
                timing_optimization is not None
                and link_optimization != timing_optimization
            )
        ):
            raise ValidationError(
                "multi-FPGA BoardLinkTimingDB application is invalid"
            )
    hardware_bsp = report.get("hardware_bsp")
    if hardware_bsp is not None:
        bsp_validation = validate_multi_fpga_bsp_flow_report(hardware_bsp)
        if (
            bsp_validation["design"] != design
            or bsp_validation["platform"] != platform
        ):
            raise ValidationError(
                "multi-FPGA hardware BSP identity disagrees"
            )
        source_artifact = report.get("artifacts", {}).get(
            "board_independent_flow_report", {}
        )
        if (
            source_artifact.get("sha256")
            != hardware_bsp.get("source_flow_report_sha256")
        ):
            raise ValidationError(
                "multi-FPGA hardware BSP source-flow hash disagrees"
            )
    frame_search = report.get("frame_search")
    if frame_search is not None:
        frame_validation = validate_frame_search_report(frame_search)
        if (
            frame_validation["selected_frame_slots"]
            != tdm_validation.get("frame_slots")
            or frame_validation["selected_frame_slots"]
            != runtime["validation"].get("frame_slots")
        ):
            raise ValidationError(
                "frame-search selection disagrees with TDM/runtime"
            )
    cross_stage = report.get("cross_stage")
    cross_stage_iteration = None
    if cross_stage is not None:
        if (
            not isinstance(cross_stage, dict)
            or cross_stage.get("status") != "pass"
            or cross_stage.get("design") != design
            or cross_stage.get("platform") != platform
        ):
            raise ValidationError(
                "multi-FPGA cross-stage identity disagrees"
            )
        candidates = cross_stage.get("candidates")
        selected_iteration = cross_stage.get("selected_iteration")
        if (
            not isinstance(candidates, list)
            or isinstance(selected_iteration, bool)
            or not isinstance(selected_iteration, int)
            or selected_iteration < 0
            or selected_iteration >= len(candidates)
        ):
            raise ValidationError(
                "multi-FPGA cross-stage selection is invalid"
            )
        selected = candidates[selected_iteration]
        if (
            not isinstance(selected, dict)
            or selected.get("status") != "pass"
            or selected.get("iteration") != selected_iteration
            or selected.get("candidate_id")
            != cross_stage.get("selected_candidate_id")
            or selected.get("phase3_validation")
            != partition_validation
            or selected.get("phase4_validation") != route_validation
            or selected.get("phase5_validation") != tdm_validation
        ):
            raise ValidationError(
                "selected cross-stage candidate disagrees with canonical "
                "Phase 3--5 stages"
            )
        cross_stage_iteration = selected_iteration
    if any(
        item.get("status") != "pass"
        for item in (
            partition_validation,
            route_validation,
            tdm_validation,
            split_validation,
            equivalence,
        )
    ):
        raise ValidationError(
            "one or more independent multi-FPGA checks did not pass"
        )

    return {
        "status": "pass",
        "design": design,
        "platform": platform,
        "instances": partition_validation.get("instances"),
        "used_fpgas": partition_validation.get("used_fpgas"),
        "cut_nets": partition_validation.get("cut_nets"),
        "scheduled_hops": split_validation.get("scheduled_hops"),
        "equivalence_mismatches": equivalence.get("mismatches"),
        "frame_slots": runtime["validation"].get("frame_slots"),
        "cross_stage_iteration": cross_stage_iteration,
        "timing_optimization": (
            "enabled"
            if timing_optimization
            else (
                "disabled-baseline"
                if timing_optimization is False
                else "external-path-test"
            )
        ),
        "nominal_virtual_frequency_mhz": runtime["validation"].get(
            "nominal_virtual_frequency_mhz"
        ),
        "physical_status": (
            "pass" if physical is not None else "not-requested"
        ),
        "hardware_bsp_status": (
            "pass" if hardware_bsp is not None else "not-requested"
        ),
        "phase6_provider": (
            "chimew" if phase6_comparison is not None else "baseline"
        ),
        "phase6_comparison_status": (
            phase6_comparison_validation["status"]
            if phase6_comparison_validation is not None
            else "not-requested"
        ),
    }


def validate_multi_fpga_flow_bundle(
    flow_root: Path,
    *,
    minimum_combinational_cut_nets: int = 0,
    require_physical: bool = False,
) -> Dict[str, Any]:
    """Rehash and independently replay a complete multi-FPGA flow root."""

    if (
        isinstance(minimum_combinational_cut_nets, bool)
        or not isinstance(minimum_combinational_cut_nets, int)
        or minimum_combinational_cut_nets < 0
    ):
        raise ValidationError(
            "minimum combinational-cut net count must be a non-negative integer"
        )
    flow_root = flow_root.expanduser()
    if flow_root.is_symlink() or not flow_root.is_dir():
        raise ValidationError("multi-FPGA flow root is missing or is a symlink")
    flow_root = flow_root.resolve()
    report = read_json(
        _checked_flow_member(
            flow_root,
            Path("multi-fpga-flow-report.json"),
            "flow report",
        )
    )
    summary = validate_multi_fpga_flow_report(report)

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValidationError("multi-FPGA flow artifact table is missing")
    artifact_digests: Dict[str, str] = {}
    for label, record in sorted(artifacts.items()):
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(record, dict)
            or set(record) != {"path", "sha256"}
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("sha256"), str)
        ):
            raise ValidationError(
                f"multi-FPGA flow artifact {label!r} record is invalid"
            )
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError(
                f"multi-FPGA flow artifact {label!r} path is unsafe"
            )
        path = _checked_flow_member(flow_root, relative, f"artifact {label}")
        digest = _sha256(path)
        if digest != record["sha256"]:
            raise ValidationError(
                f"multi-FPGA flow artifact {label!r} SHA-256 disagrees"
            )
        artifact_digests[label] = digest

    platform_path = _checked_flow_member(
        flow_root, Path("frontend/phase1/platform.normalized.json"), "platform"
    )
    ir_path = _checked_flow_member(
        flow_root, Path("frontend/phase1/design.emuir.json"), "EmuIR"
    )
    assignment_path = _checked_flow_member(
        flow_root, Path("partition/assignment.json"), "assignment"
    )
    clusters_path = _checked_flow_member(
        flow_root, Path("partition/clusters.json"), "clusters"
    )
    routes_path = _checked_flow_member(
        flow_root, Path("system-route/routes.json"), "routes"
    )
    schedule_path = _checked_flow_member(
        flow_root, Path("tdm/schedule.json"), "schedule"
    )
    manifest_path = _checked_flow_member(
        flow_root, Path("split/manifest.json"), "split manifest"
    )

    live_reports = {
        "partition": "partition/phase3_report.json",
        "system_route": "system-route/phase4_report.json",
        "tdm": "tdm/phase5_report.json",
        "split": "split/phase6_report.json",
    }
    for stage, relative in live_reports.items():
        live = read_json(
            _checked_flow_member(flow_root, Path(relative), f"{stage} report")
        )
        if live != report["stages"][stage]:
            raise ValidationError(
                f"multi-FPGA live {stage} report disagrees with the flow report"
            )

    phase3_validation = validate_phase3(
        ir_path, platform_path, clusters_path, assignment_path
    )
    if phase3_validation != report["stages"]["partition"]["validation"]:
        raise ValidationError("independent Phase 3 replay disagrees")
    if minimum_combinational_cut_nets:
        if phase3_validation.get("cut_mode") != CUT_MODE_STATIC_EXACT:
            raise ValidationError(
                "a positive combinational-cut requirement needs static exact mode"
            )
        observed = phase3_validation.get("combinational_cut_nets")
        if not isinstance(observed, int) or observed < minimum_combinational_cut_nets:
            raise ValidationError(
                "independent Phase 3 replay found fewer combinational cut nets "
                "than required"
            )

    route_provider = report["stages"]["system_route"].get("provider")
    timing_paths_path = (
        _checked_flow_member(
            flow_root,
            Path("timing/cut-timing-paths.json"),
            "projected timing paths",
        )
        if route_provider != NATIVE_ROUTER_PROVIDER
        else None
    )
    phase4_validation = validate_phase4(
        assignment_path,
        platform_path,
        routes_path,
        timing_paths_path=timing_paths_path,
    )
    if phase4_validation != report["stages"]["system_route"]["validation"]:
        raise ValidationError("independent Phase 4 replay disagrees")

    ratio_plan_path = flow_root / "tdm/ratio_plan.json"
    phase5_validation = validate_phase5(
        routes_path,
        platform_path,
        schedule_path,
        ratio_plan_path=ratio_plan_path if ratio_plan_path.is_file() else None,
    )
    timing_validation = phase5_validation.pop("timing", None)
    if phase5_validation != report["stages"]["tdm"]["validation"]:
        raise ValidationError("independent Phase 5 replay disagrees")
    if timing_validation is not None and timing_validation != report["stages"][
        "tdm"
    ].get("timing_validation"):
        raise ValidationError("independent Phase 5 timing replay disagrees")

    phase6_validation = validate_phase6(
        ir_path,
        assignment_path,
        schedule_path,
        platform_path,
        manifest_path,
    )
    equivalence = phase6_validation.pop("static_exact_equivalence", None)
    if phase6_validation != report["stages"]["split"]["validation"]:
        raise ValidationError("independent Phase 6 replay disagrees")
    if equivalence is not None and (
        equivalence.get("status") != "pass"
        or equivalence.get("mismatches") != 0
    ):
        raise ValidationError("independent static-exact equivalence replay failed")

    platform = Platform.load(platform_path)
    runtime_contract = read_json(
        _checked_flow_member(
            flow_root, Path("runtime/runtime_contract.json"), "runtime contract"
        )
    )
    runtime_validation = validate_virtual_runtime(
        runtime_contract, read_json(schedule_path), platform
    )
    if runtime_validation != report["runtime"].get("validation"):
        raise ValidationError("independent runtime replay disagrees")

    physical = report.get("physical")
    if require_physical and physical is None:
        raise ValidationError("multi-FPGA flow has no completed physical Phase 7")
    physical_summary_path = None
    board_link_timing_path = None
    if physical is not None:
        live_physical = read_json(
            _checked_flow_member(
                flow_root,
                Path("physical/multi-fpga-physical-flow-report.json"),
                "physical flow report",
            )
        )
        if live_physical != physical:
            raise ValidationError(
                "live physical report disagrees with the multi-FPGA flow report"
            )
        validate_multi_fpga_physical_report(live_physical)
        physical_summary_path = _checked_flow_member(
            flow_root,
            Path("physical/physical-summary.json"),
            "physical summary",
        )
        candidate_link_timing = flow_root / "timing/board-link-timing.json"
        if candidate_link_timing.is_file():
            board_link_timing_path = _checked_flow_member(
                flow_root, candidate_link_timing, "board-link timing"
            )

    with tempfile.TemporaryDirectory(prefix="emuflow-flow-validate-") as temporary:
        replay = run_phase7c(
            schedule_path,
            platform_path,
            flow_root / "partition/phase3_report.json",
            flow_root / "system-route/phase4_report.json",
            flow_root / "tdm/phase5_report.json",
            flow_root / "split/phase6_report.json",
            Path(temporary),
            physical_summary_path=physical_summary_path,
            routes_path=routes_path if physical_summary_path is not None else None,
            board_link_timing_path=board_link_timing_path,
        )
        replay_qor = read_json(Path(temporary) / "qor_report.json")
    if replay_qor != read_json(
        _checked_flow_member(flow_root, Path("runtime/qor_report.json"), "QoR report")
    ):
        raise ValidationError("independent Phase 7C QoR replay disagrees")
    if replay.get("status") != report["runtime"].get("status"):
        raise ValidationError("independent Phase 7C status replay disagrees")

    return {
        **summary,
        "artifact_count": len(artifact_digests),
        "minimum_combinational_cut_nets": minimum_combinational_cut_nets,
        "observed_combinational_cut_nets": phase3_validation.get(
            "combinational_cut_nets", 0
        ),
        "static_exact_combinational_cut_exercised": (
            phase3_validation.get("cut_mode") == CUT_MODE_STATIC_EXACT
            and phase3_validation.get("combinational_cut_nets", 0) > 0
        ),
        "physical_required": require_physical,
    }


def run_multi_fpga_flow(
    platform_path: Path,
    output_dir: Path,
    *,
    sources: Iterable[Path] = (),
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    yosys_json: Optional[Path] = None,
    yosys: Optional[str] = None,
    mapping_profile: str = "vtr-hard-blocks",
    partition_constraints: Optional[Path] = None,
    partition_provider: str = "patron",
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    openroad: Optional[str] = None,
    repart: Optional[str] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_flow_refinement: bool = False,
    patron_algorithm_version: int = 6,
    partition_timeout_seconds: int = 3600,
    partition_seed_attempts: int = 1,
    partition_num_initial_solutions: int = 50,
    partition_num_best_initial_solutions: int = 10,
    partition_repair_min_used_fpgas: bool = False,
    partition_repair_balance: bool = False,
    cut_mode: str = CUT_MODE_STATIC_EXACT,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    comb_segment_budget_slots: int = 1,
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    mfspart_post_refinement_timing_path_beta: float = DEFAULT_TIMING_PATH_BETA,
    timing_driven: bool = True,
    timing_backend: str = "opensta",
    clock_periods: Optional[Dict[str, float]] = None,
    timing_model: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db: Optional[Path] = None,
    opensta: Optional[str] = None,
    timing_vivado: Optional[str] = None,
    sta_max_paths: int = 200000,
    timing_criticality_scale: float = 9.0,
    timing_criticality_exponent: float = 2.0,
    route_constraints: Optional[Path] = None,
    board_link_timing_db: Optional[Path] = None,
    timing_paths: Optional[Path] = None,
    router: Optional[str] = None,
    route_provider: Optional[str] = None,
    route_candidate_workers: int = 1,
    frame_slots: Optional[int] = None,
    optimize_frame_slots: bool = False,
    route_max_iterations: Optional[int] = None,
    tdm_provider: Optional[str] = None,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: int = 8,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    ratio_convergence: float = 1.0e-9,
    cross_stage_iterations: int = 0,
    cross_stage_feedback_optimizer: Optional[str] = None,
    cross_stage_pair_pressure_weight: float = 1.0,
    simulation_frames: int = 16,
    equivalence_cycles: int = 16,
    equivalence_seed: int = 20260727,
    phase6_provider: str = "baseline",
    phase6_chimew_region_count: int = 4,
    phase6_chimew_grouper: Optional[str] = None,
    phase6_chimew_refiner: Optional[str] = None,
    phase6_chimew_rudy: Optional[str] = None,
    phase6_chimew_assigner: Optional[str] = None,
    physical: bool = False,
    physical_backend: str = "open",
    physical_architecture: Optional[Path] = None,
    physical_architecture_id: str = VTR_HARD_BLOCK_PROFILE,
    physical_vpr: Optional[str] = None,
    physical_architecture_importer: Optional[str] = None,
    physical_packed_importer: Optional[str] = None,
    physical_route_checker: Optional[str] = None,
    physical_openparf_install: Optional[Path] = None,
    physical_openparf_python: Optional[Path] = None,
    physical_seed: int = 1,
    physical_route_channel_width: int = 300,
    physical_vivado: Optional[str] = None,
    physical_vivado_max_timing_paths: int = 10000,
    physical_vivado_place_directive: str = "Default",
    physical_vivado_route_directive: str = "Default",
    physical_workers: int = 1,
    serial_bsp_phy_provider: Optional[Path] = None,
    serial_bsp_runtime_sync_provider: Optional[Path] = None,
    serial_bsp_board_overlay: Optional[Path] = None,
    serial_bsp_gt_site_map: Optional[Path] = None,
    serial_bsp_vivado: Optional[Path] = None,
    serial_bsp_yosys: Optional[Path] = None,
    serial_bsp_runtime_sync_root: Optional[str] = None,
    serial_bsp_ready_stable_cycles: int = 4,
) -> Dict[str, Any]:
    """Compile RTL/EmuIR through the checked board-independent split."""

    if mapping_profile not in MULTI_FPGA_MAPPING_PROFILES:
        raise EmuFlowError(
            "unsupported multi-FPGA mapping profile "
            f"{mapping_profile!r}; expected one of "
            f"{', '.join(MULTI_FPGA_MAPPING_PROFILES)}"
        )
    if phase6_provider not in MULTI_FPGA_PHASE6_PROVIDERS:
        raise EmuFlowError(
            "unsupported Phase 6 provider "
            f"{phase6_provider!r}; expected one of "
            f"{', '.join(MULTI_FPGA_PHASE6_PROVIDERS)}"
        )
    if cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise EmuFlowError("unsupported combinational cut mode")
    exact_cut_mode = cut_mode == CUT_MODE_STATIC_EXACT
    if exact_cut_mode:
        if optimize_frame_slots or cross_stage_iterations:
            raise EmuFlowError(
                "static exact combinational cuts require one fixed Phase 3--5 "
                "frame; frame search/cross-stage optimization is not yet "
                "dependency-qualified"
            )
        if route_provider not in {
            None,
            NATIVE_ROUTER_PROVIDER,
            NATIVE_TIMING_EVALUATED_PROVIDER,
        }:
            raise EmuFlowError(
                "static exact combinational cuts require native routing with "
                "optional post-route timing annotation"
            )
        if tdm_provider not in {None, TDM_STATIC_EXACT_PROVIDER}:
            raise EmuFlowError(
                "static exact combinational cuts require the dependency-aware "
                "TDM provider"
            )
        if route_candidate_workers != 1:
            raise EmuFlowError(
                "static exact native routing requires one route candidate worker"
            )
        if any(
            value is not None
            for value in (ratio_optimizer, timing_dag_optimizer, slot_optimizer)
        ) or slot_refinement_iterations != 0:
            raise EmuFlowError(
                "static exact scheduling does not accept unqualified ratio/slot "
                "optimizers"
            )
    if not 2 <= phase6_chimew_region_count <= 31:
        raise EmuFlowError("--phase6-chimew-region-count must be in [2, 31]")
    if phase6_provider == "chimew" and (
        not physical or physical_backend != "open"
    ):
        raise EmuFlowError(
            "--phase6-provider chimew requires --physical with "
            "--physical-backend open"
        )
    if timing_backend not in {"opensta", "vivado"}:
        raise EmuFlowError(
            "timing backend must be 'opensta' or 'vivado'"
        )
    if timing_backend == "vivado" and architecture_timing_db is not None:
        raise EmuFlowError(
            "--architecture-timing-db applies only to timing-backend=opensta"
        )
    if timing_backend == "vivado" and opensta is not None:
        raise EmuFlowError("--opensta applies only to timing-backend=opensta")
    if timing_backend == "opensta" and timing_vivado is not None:
        raise EmuFlowError(
            "--timing-vivado applies only to timing-backend=vivado"
        )
    internal_timing_database = timing_paths is None
    if partition_provider == "patron" and not internal_timing_database:
        raise EmuFlowError(
            "--partition-provider patron requires the internally generated "
            "complete TimingPathDB"
        )
    if (
        partition_provider == "patron"
        and not exact_cut_mode
        and cross_stage_iterations < 1
    ):
        raise EmuFlowError(
            "--partition-provider patron requires --cross-stage-iterations "
            "of at least 1 so the frozen TritonPart fallback and PATRON "
            "candidate receive exact Phase 4/5 promotion"
        )
    if timing_paths is not None and (
        timing_driven or architecture_timing_db is not None
    ):
        raise EmuFlowError(
            "--timing-driven/--architecture-timing-db cannot be combined "
            "with externally projected --timing-paths"
        )
    if physical and timing_paths is not None:
        raise EmuFlowError(
            "physical Phase 7C requires an internally generated complete "
            "TimingPathDB; externally projected --timing-paths are only "
            "supported by non-physical algorithm tests"
        )
    if (
        isinstance(cross_stage_iterations, bool)
        or not isinstance(cross_stage_iterations, int)
        or cross_stage_iterations < 0
    ):
        raise EmuFlowError("--cross-stage-iterations must be non-negative")
    if cross_stage_iterations and not timing_driven:
        raise EmuFlowError(
            "--cross-stage-iterations requires --timing-driven"
        )
    if optimize_frame_slots and not timing_driven:
        raise EmuFlowError(
            "--optimize-frame-slots requires --timing-driven"
        )
    if internal_timing_database and not clock_periods:
        raise EmuFlowError(
            "multi-FPGA timing analysis requires at least one "
            "--clock-period CLOCK=PERIOD_NS; TimingPathDB remains mandatory "
            "for physical Phase 7C even with --no-timing-driven"
        )
    serial_bsp_requested = serial_bsp_phy_provider is not None
    serial_bsp_auxiliary = any(
        value is not None
        for value in (
            serial_bsp_runtime_sync_provider,
            serial_bsp_board_overlay,
            serial_bsp_gt_site_map,
            serial_bsp_vivado,
            serial_bsp_yosys,
            serial_bsp_runtime_sync_root,
        )
    )
    if serial_bsp_auxiliary and not serial_bsp_requested:
        raise EmuFlowError(
            "serial BSP options require --serial-bsp-phy-provider"
        )
    if serial_bsp_requested and serial_bsp_runtime_sync_provider is None:
        raise EmuFlowError(
            "serial BSP continuation requires --serial-bsp-runtime-sync-provider"
        )
    if serial_bsp_requested and (
        (serial_bsp_vivado is None) == (serial_bsp_yosys is None)
    ):
        raise EmuFlowError(
            "serial BSP continuation requires exactly one of "
            "--serial-bsp-vivado or --serial-bsp-yosys"
        )

    output_dir = output_dir.resolve()
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise EmuFlowError(
                "multi-FPGA output path must be an empty directory: "
                f"{output_dir}"
            )
    output_dir.mkdir(parents=True, exist_ok=True)

    source_list = [path.resolve() for path in sources]
    frontend_root = output_dir / "frontend"
    frontend_root.mkdir(parents=True, exist_ok=True)
    synthesized_json = frontend_root / "synthesized.json"
    synthesis_mode: str
    if yosys_json is not None:
        if source_list:
            raise EmuFlowError(
                "provide RTL sources or --yosys-json, not both"
            )
        source_json = yosys_json.resolve()
        if not source_json.is_file():
            raise EmuFlowError(f"Yosys JSON does not exist: {source_json}")
        shutil.copyfile(source_json, synthesized_json)
        synthesis_mode = "provided-yosys-json"
    else:
        if not source_list:
            raise EmuFlowError(
                "multi-FPGA compilation requires RTL sources or --yosys-json"
            )
        if top is None:
            raise EmuFlowError("--top is required when compiling RTL sources")
        if mapping_profile == "vtr-hard-blocks":
            raw_json = frontend_root / "vtr-hard-block-atoms.json"
            eblif = frontend_root / "design.eblif"
            synthesis_report = run_vtr_yosys(
                source_list,
                top,
                eblif,
                executable=yosys,
                log_path=frontend_root / "yosys.log",
                hard_blocks=True,
                json_output=raw_json,
            )
            normalization_report = normalize_vtr_hard_block_json(
                raw_json,
                synthesized_json,
                top=top,
            )
            synthesis_mode = "vtr-lut6-ff-hard-blocks"
        else:
            synthesis_report = run_generic_yosys(
                source_list,
                top,
                synthesized_json,
                executable=yosys,
                log_path=frontend_root / "yosys.log",
            )
            normalization_report = None
            synthesis_mode = "generic-lut6-ff"

    phase1_root = frontend_root / "phase1"
    frontend_report = run_phase1(
        synthesized_json,
        platform_path,
        phase1_root,
        top=top,
        clocks=clocks,
    )
    if frontend_report["status"] != "pass":
        raise EmuFlowError(
            "multi-FPGA frontend failed capacity or clock-topology checks"
        )
    frontend_report = {
        **frontend_report,
        "synthesis": {
            "provider": "yosys",
            "mode": synthesis_mode,
            "mapping_profile": (
                VTR_HARD_BLOCK_PROFILE
                if synthesis_mode == "vtr-lut6-ff-hard-blocks"
                else (
                    "provided-yosys-json"
                    if synthesis_mode == "provided-yosys-json"
                    else mapping_profile
                )
            ),
            "sources": [str(path) for path in source_list],
            "yosys_json_sha256": _sha256(synthesized_json),
            **(
                {"tool_report": synthesis_report}
                if yosys_json is None
                else {}
            ),
            **(
                {"normalization": normalization_report}
                if yosys_json is None
                and normalization_report is not None
                else {}
            ),
        },
    }
    ir_path = phase1_root / "design.emuir.json"

    timing_root = output_dir / "timing"
    path_database_path = timing_root / "path-database.json"
    cut_segment_qualification_path = None
    net_weights_path = timing_root / "partition-net-weights.json"
    timing_report = None
    if internal_timing_database:
        timing_root.mkdir(parents=True, exist_ok=True)
        if timing_backend == "opensta":
            sta_report = run_opensta_path_database(
                ir_path=ir_path,
                output_path=path_database_path,
                clocks=clock_periods,
                timing_model_path=timing_model,
                architecture_timing_db_path=architecture_timing_db,
                executable=opensta,
                max_paths=sta_max_paths,
                log_path=timing_root / "opensta.log",
            )
            timing_mode = "opensta-preplacement"
        else:
            platform = Platform.load(platform_path)
            parts = {fpga.part for fpga in platform.fpgas}
            if len(parts) != 1:
                raise EmuFlowError(
                    "Vivado timing backend requires one common FPGA part"
                )
            sta_report = run_vivado_timing_path_database(
                ir_path=ir_path,
                output_path=path_database_path,
                clocks=clock_periods,
                part=next(iter(parts)),
                executable=timing_vivado,
                max_paths=sta_max_paths,
            )
            timing_mode = "vivado-post-synthesis"
        weights_report = None
        if timing_driven:
            weights_report = derive_partition_net_weights(
                path_database_path,
                ir_path,
                net_weights_path,
                criticality_scale=timing_criticality_scale,
                criticality_exponent=timing_criticality_exponent,
            )
        timing_report = {
            "status": "pass",
            "mode": timing_mode,
            "backend": timing_backend,
            "sta": sta_report,
            "optimization_enabled": timing_driven,
            **(
                {"partition_weights": weights_report}
                if weights_report is not None
                else {}
            ),
        }

    effective_route_constraints = route_constraints
    link_timing_report = None
    copied_link_timing_path = None
    effective_route_constraints_path = None
    if board_link_timing_db is not None:
        platform = Platform.load(platform_path)
        database = read_json(board_link_timing_db.resolve())
        validation = validate_board_link_timing(database, platform)
        link_delays, projection = directed_route_link_delays(
            database, platform
        )
        timing_root.mkdir(parents=True, exist_ok=True)
        copied_link_timing_path = timing_root / "board-link-timing.json"
        write_json(copied_link_timing_path, database)
        raw_constraints = (
            read_json(route_constraints.resolve())
            if route_constraints is not None
            else {"schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA}
        )
        if not isinstance(raw_constraints, dict):
            raise ValidationError("route constraints must be an object")
        raw_constraints = dict(raw_constraints)
        raw_constraints["directed_link_delay_ns"] = link_delays
        effective_route_constraints_path = (
            timing_root / "board-link-route-constraints.json"
        )
        write_json(effective_route_constraints_path, raw_constraints)
        effective_route_constraints = effective_route_constraints_path
        link_timing_report = {
            "status": "pass",
            "validation": validation,
            "routing_projection": projection,
            "optimization_enabled": timing_driven,
            "applied_to": (
                [
                    "phase4-system-routing",
                    "phase5-tdm-ratio-and-schedule-timing",
                    "phase7c-system-timing-when-physical",
                ]
                if timing_driven
                else [
                    "phase4-post-route-timing-evaluation",
                    "phase5-baseline-schedule-timing-evaluation",
                    "phase7c-system-timing-when-physical",
                ]
            ),
        }

    if timing_report is not None:
        provider_consumes_weights = (
            timing_driven and partition_provider != "greedy"
        )
        hop_refiner_consumes_weights = False
        if effective_route_constraints is not None:
            raw_route_constraints = read_json(
                effective_route_constraints.resolve()
            )
            if not isinstance(raw_route_constraints, dict):
                raise ValidationError("route constraints must be an object")
            hop_refiner_consumes_weights = timing_driven and (
                raw_route_constraints.get("max_route_hops") is not None
            )
        consumers = []
        if provider_consumes_weights:
            consumers.append("phase3-provider")
        if hop_refiner_consumes_weights:
            consumers.append("phase3-hop-refinement")
        timing_report.update(
            {
                "partition_weights_applied": bool(consumers),
                "partition_provider_weights_applied": (
                    provider_consumes_weights
                ),
                "hop_refinement_weights_applied": (
                    hop_refiner_consumes_weights
                ),
                "partition_weight_consumers": consumers,
            }
        )

    phase3_root = output_dir / "partition"
    phase3_report = run_phase3(
        ir_path,
        platform_path,
        phase3_root,
        constraints_path=partition_constraints,
        seed=seed,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
        provider=partition_provider,
        openroad=openroad,
        tritonpart_timeout_seconds=partition_timeout_seconds,
        tritonpart_seed_attempts=partition_seed_attempts,
        tritonpart_num_initial_solutions=partition_num_initial_solutions,
        tritonpart_num_best_initial_solutions=(
            partition_num_best_initial_solutions
        ),
        tritonpart_repair_min_used_fpgas=(
            partition_repair_min_used_fpgas
        ),
        tritonpart_repair_balance=partition_repair_balance,
        repart=repart,
        repart_timeout_seconds=partition_timeout_seconds,
        net_weights_path=(
            net_weights_path
            if timing_driven
            else None
        ),
        route_constraints_path=effective_route_constraints,
        cut_mode=cut_mode,
        max_cross_fpga_dependency_depth=max_cross_fpga_dependency_depth,
        comb_segment_budget_slots=comb_segment_budget_slots,
        timing_database_path=(
            path_database_path if partition_provider == "patron" else None
        ),
        patron_refiner=patron_refiner,
        patron_max_moves=patron_max_moves,
        patron_flow_refinement=patron_flow_refinement,
        patron_algorithm_version=patron_algorithm_version,
        static_exact_candidate_policy=static_exact_candidate_policy,
        mfspart_post_refinement=(
            exact_cut_mode and partition_provider == "tritonpart"
        ),
        timing_path_database_path=(
            path_database_path if exact_cut_mode else None
        ),
        mfspart_post_refinement_timing_path_beta=(
            mfspart_post_refinement_timing_path_beta
        ),
    )
    assignment_path = phase3_root / "assignment.json"

    projected_timing_paths = timing_paths
    if internal_timing_database and not cross_stage_iterations:
        cut_segment_qualification_path = (
            timing_root / "cut-segment-qualification.json"
        )
        cut_qualification = build_cut_segment_qualification(
            ir_path,
            assignment_path,
            path_database_path,
            timing_model_path=timing_model,
            architecture_timing_db_path=architecture_timing_db,
        )
        write_json(cut_segment_qualification_path, cut_qualification)
        timing_report["cut_segment_qualification"] = cut_qualification
        projected_timing_paths = timing_root / "cut-timing-paths.json"
        # Phase 4/5 must optimize the same complete original TimingPathDB
        # population that Phase 7C later reports.  The post-partition
        # structural cut-segment qualification is intentionally separate from
        # this original-path population.
        projection_report = project_sta_path_database(
            path_database_path,
            assignment_path,
            projected_timing_paths,
        )
        timing_report["cut_path_projection"] = projection_report

    effective_route_provider = route_provider
    effective_tdm_provider = tdm_provider
    if exact_cut_mode:
        effective_route_provider = (
            NATIVE_TIMING_EVALUATED_PROVIDER
            if projected_timing_paths is not None
            else NATIVE_ROUTER_PROVIDER
        )
        effective_tdm_provider = TDM_STATIC_EXACT_PROVIDER
    if internal_timing_database and not timing_driven and not exact_cut_mode:
        if route_provider not in {
            None,
            NATIVE_ROUTER_PROVIDER,
            NATIVE_TIMING_EVALUATED_PROVIDER,
        }:
            raise EmuFlowError(
                "--no-timing-driven requires the timing-oblivious Phase 4 "
                "baseline; remove --route-provider"
            )
        if tdm_provider not in {None, TDM_BASELINE_PROVIDER}:
            raise EmuFlowError(
                "--no-timing-driven requires the timing-oblivious Phase 5 "
                "baseline; remove --tdm-provider"
            )
        if route_candidate_workers != 1:
            raise EmuFlowError(
                "--no-timing-driven requires --route-candidate-workers 1"
            )
        effective_route_provider = NATIVE_TIMING_EVALUATED_PROVIDER
        effective_tdm_provider = TDM_BASELINE_PROVIDER

    phase4_root = output_dir / "system-route"
    phase5_root = output_dir / "tdm"
    frame_search_report = None
    cross_stage_report = None
    if cross_stage_iterations:
        cross_stage_root = output_dir / "cross-stage"
        cross_stage_report = run_cross_stage_optimization(
            ir_path=ir_path,
            platform_path=platform_path,
            database_path=path_database_path,
            initial_assignment_path=(
                phase3_root / "patron/initial_assignment.json"
                if partition_provider == "patron"
                else assignment_path
            ),
            output_dir=cross_stage_root,
            seed_candidate_phase3_root=(
                phase3_root if partition_provider == "patron" else None
            ),
            phase3_constraints_path=(
                phase3_root / "constraints.normalized.json"
            ),
            route_constraints_path=effective_route_constraints,
            board_link_timing_path=board_link_timing_db,
            phase3_provider=partition_provider,
            max_outer_iterations=cross_stage_iterations,
            seed=seed,
            min_used_fpgas=min_used_fpgas,
            balance_tolerance=balance_tolerance,
            openroad=openroad,
            repart=repart,
            patron_refiner=patron_refiner,
            patron_max_moves=patron_max_moves,
            patron_flow_refinement=patron_flow_refinement,
            patron_algorithm_version=patron_algorithm_version,
            partition_timeout_seconds=partition_timeout_seconds,
            partition_seed_attempts=partition_seed_attempts,
            partition_num_initial_solutions=partition_num_initial_solutions,
            partition_num_best_initial_solutions=(
                partition_num_best_initial_solutions
            ),
            partition_repair_min_used_fpgas=(
                partition_repair_min_used_fpgas
            ),
            partition_repair_balance=partition_repair_balance,
            router=router,
            route_provider=route_provider,
            route_candidate_workers=route_candidate_workers,
            frame_slots=frame_slots,
            optimize_frame_slots=optimize_frame_slots,
            route_max_iterations=route_max_iterations,
            tdm_provider=tdm_provider,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            ratio_convergence=ratio_convergence,
            feedback_optimizer=cross_stage_feedback_optimizer,
            simulation_frames=simulation_frames,
            pair_pressure_weight=cross_stage_pair_pressure_weight,
        )
        selected = cross_stage_report["candidates"][
            cross_stage_report["selected_iteration"]
        ]
        if cross_stage_report["selected_iteration"] != 0:
            selected_phase3_root = (
                cross_stage_root / selected["assignment"]
            ).parent
            shutil.rmtree(phase3_root)
            shutil.copytree(selected_phase3_root, phase3_root)
            phase3_report = read_json(phase3_root / "phase3_report.json")
            assignment_path = phase3_root / "assignment.json"
        elif partition_provider == "patron":
            phase3_report = promote_patron_baseline(
                ir_path, platform_path, phase3_root
            )
            assignment_path = phase3_root / "assignment.json"
        projected_timing_paths = timing_root / "cut-timing-paths.json"
        shutil.copy2(
            cross_stage_root / selected["timing_paths"],
            projected_timing_paths,
        )
        timing_report["cut_path_projection"] = selected["projection"]
        shutil.copytree(
            (cross_stage_root / selected["routes"]).parent,
            phase4_root,
        )
        shutil.copytree(
            (cross_stage_root / selected["schedule"]).parent,
            phase5_root,
        )
        phase4_report = read_json(phase4_root / "phase4_report.json")
        phase5_report = read_json(phase5_root / "phase5_report.json")
        if optimize_frame_slots:
            selected_frame_search = cross_stage_root / selected[
                "frame_search"
            ]
            shutil.copytree(
                selected_frame_search.parent,
                output_dir / "frame-search",
            )
            frame_search_report = read_json(
                output_dir / "frame-search/frame-search-report.json"
            )
    elif optimize_frame_slots:
        if frame_slots is None:
            raise EmuFlowError(
                "--optimize-frame-slots requires --frame-slots as its "
                "feasible upper bound"
            )
        frame_search_report = run_frame_length_search(
            assignment_path,
            platform_path,
            projected_timing_paths,
            output_dir / "frame-search",
            phase4_root,
            phase5_root,
            max_frame_slots=frame_slots,
            route_constraints=effective_route_constraints,
            route_max_iterations=route_max_iterations,
            router=router,
            route_provider=route_provider,
            candidate_workers=route_candidate_workers,
            tdm_provider=tdm_provider,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            ratio_convergence=ratio_convergence,
            simulation_frames=simulation_frames,
        )
        phase4_report = read_json(phase4_root / "phase4_report.json")
        phase5_report = read_json(phase5_root / "phase5_report.json")
    else:
        phase4_report = run_phase4(
            assignment_path,
            platform_path,
            phase4_root,
            constraints_path=effective_route_constraints,
            frame_slots=frame_slots,
            max_iterations=route_max_iterations,
            timing_paths_path=projected_timing_paths,
            router=router,
            provider=effective_route_provider,
            candidate_workers=route_candidate_workers,
        )
        phase5_report = run_phase5(
            phase4_root / "routes.json",
            platform_path,
            phase5_root,
            simulation_frames=simulation_frames,
            provider=effective_tdm_provider,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            convergence=ratio_convergence,
        )
    routes_path = phase4_root / "routes.json"
    schedule_path = phase5_root / "schedule.json"

    phase6_root = output_dir / "split"
    schedule_document = read_json(schedule_path)
    scheduled_signals = schedule_document.get("entries", [])
    if phase6_provider == "chimew" and not scheduled_signals:
        raise EmuFlowError(
            "--phase6-provider chimew requires at least one scheduled "
            "inter-FPGA signal"
        )
    use_academic_chimew = (
        phase6_provider == "chimew"
        or (
            phase6_provider == "auto"
            and physical
            and physical_backend == "open"
            and bool(scheduled_signals)
        )
    )
    baseline_physical_report = None
    baseline_runtime_report = None
    phase6_comparison = None
    effective_physical_architecture = physical_architecture
    if use_academic_chimew:
        comparison_root = output_dir / "phase6-comparison"
        baseline_root = comparison_root / "baseline"
        baseline_split = baseline_root / "split"
        baseline_phase6_started = time.monotonic()
        baseline_phase6_report = run_phase6(
            ir_path,
            assignment_path,
            schedule_path,
            platform_path,
            baseline_split,
            equivalence_cycles=equivalence_cycles,
            equivalence_seed=equivalence_seed,
        )
        baseline_phase6_seconds = time.monotonic() - baseline_phase6_started
        baseline_physical_started = time.monotonic()
        baseline_physical_report = run_multi_fpga_physical_flow(
            baseline_split,
            platform_path,
            schedule_path,
            baseline_root / "physical",
            backend="open",
            architecture=physical_architecture,
            architecture_id=physical_architecture_id,
            yosys=yosys,
            vpr=physical_vpr,
            architecture_importer=physical_architecture_importer,
            packed_importer=physical_packed_importer,
            route_checker=physical_route_checker,
            openparf_install=physical_openparf_install,
            openparf_python=physical_openparf_python,
            seed=physical_seed,
            route_channel_width=physical_route_channel_width,
            original_ir_path=ir_path,
            assignment_path=assignment_path,
            routes_path=routes_path,
            path_database_path=path_database_path,
            logic_path_database_path=path_database_path,
            workers=physical_workers,
        )
        baseline_physical_seconds = time.monotonic() - baseline_physical_started
        baseline_runtime_report = run_phase7c(
            schedule_path,
            platform_path,
            phase3_root / "phase3_report.json",
            phase4_root / "phase4_report.json",
            phase5_root / "phase5_report.json",
            baseline_split / "phase6_report.json",
            baseline_root / "runtime",
            physical_summary_path=(
                baseline_root / "physical/physical-summary.json"
            ),
            routes_path=routes_path,
            board_link_timing_path=copied_link_timing_path,
        )
        if effective_physical_architecture is None:
            fetched_architecture = (
                baseline_root / "physical/architecture/vtr-flagship.xml"
            )
            if not fetched_architecture.is_file():
                raise ValidationError(
                    "academic Chimew prepass did not retain its VTR "
                    "architecture source"
                )
            effective_physical_architecture = fetched_architecture
        lookahead_root = comparison_root / "lookahead"
        lookahead = materialize_academic_chimew_inputs(
            ir_path=ir_path,
            schedule_path=schedule_path,
            routes_path=routes_path,
            platform_path=platform_path,
            physical_report=baseline_physical_report,
            output_dir=lookahead_root,
            timing_paths_path=(
                projected_timing_paths if timing_driven else None
            ),
            region_count=phase6_chimew_region_count,
            grouper=phase6_chimew_grouper,
            refiner=phase6_chimew_refiner,
        )
        inputs = lookahead["artifacts"]
        chimew_schedule_path = Path(inputs["schedule"]["path"])
        sources_map = lookahead["sources"]
        chimew_pipeline_root = comparison_root / "chimew"
        pipeline = run_chimew_phase6_pipeline(
            chimew_schedule_path,
            platform_path,
            Path(inputs["crossings"]["path"]),
            Path(inputs["positions"]["path"]),
            Path(inputs["rudy_input"]["path"]),
            Path(inputs["bank_channel_input"]["path"]),
            Path(inputs["electrical_map"]["path"]),
            chimew_pipeline_root,
            source_paths={
                label: Path(path) for label, path in sources_map.items()
            },
            grouper=phase6_chimew_grouper,
            refiner=phase6_chimew_refiner,
            rudy=phase6_chimew_rudy,
            assigner=phase6_chimew_assigner,
            region_count=phase6_chimew_region_count,
        )
        adapter_root = chimew_pipeline_root / "phase6-adapter"
        chimew_phase6_started = time.monotonic()
        phase6_report = run_phase6(
            ir_path,
            assignment_path,
            chimew_schedule_path,
            platform_path,
            phase6_root,
            equivalence_cycles=equivalence_cycles,
            equivalence_seed=equivalence_seed,
            pin_plan_path=adapter_root / "pin_plan.json",
            position_hints_path=adapter_root / "position_hints.json",
            electrical_binding_path=adapter_root / "electrical_binding.json",
        )
        chimew_phase6_seconds = time.monotonic() - chimew_phase6_started
        phase6_comparison = {
            "schema": PHASE6_AB_COMPARISON_SCHEMA,
            "status": "pending-physical-comparison",
            "selected_provider": "chimew",
            "baseline_provider": "historical-default-static-phase6",
            "qualification": "academic-virtual-physical-model",
            "frozen_upstream": {
                "emuir_sha256": _sha256(ir_path),
                "assignment_sha256": _sha256(assignment_path),
                "routes_sha256": _sha256(routes_path),
                "schedule_sha256": _sha256(schedule_path),
                "platform_sha256": _sha256(platform_path),
            },
            "claim_boundary": {
                "lookahead": (
                    "normalized virtual regions from an open physical "
                    "prepass, not final device SLR/SLL closure"
                ),
                "electrical": (
                    "synthetic academic package-pin inventory, not a BSP"
                ),
            },
            "baseline": {
                "phase6": baseline_phase6_report,
                "physical": baseline_physical_report,
                "runtime": baseline_runtime_report,
                "phase6_runtime_seconds": baseline_phase6_seconds,
                "physical_runtime_seconds": baseline_physical_seconds,
            },
            "lookahead": lookahead,
            "chimew_pipeline": pipeline,
            "chimew_phase6_runtime_seconds": chimew_phase6_seconds,
        }
    else:
        phase6_report = run_phase6(
            ir_path,
            assignment_path,
            schedule_path,
            platform_path,
            phase6_root,
            equivalence_cycles=equivalence_cycles,
            equivalence_seed=equivalence_seed,
        )

    physical_report = None
    physical_summary_path = None
    if physical:
        physical_started = time.monotonic()
        physical_report = run_multi_fpga_physical_flow(
            phase6_root,
            platform_path,
            schedule_path,
            output_dir / "physical",
            backend=physical_backend,
            architecture=effective_physical_architecture,
            architecture_id=physical_architecture_id,
            yosys=yosys,
            vpr=physical_vpr,
            architecture_importer=physical_architecture_importer,
            packed_importer=physical_packed_importer,
            route_checker=physical_route_checker,
            openparf_install=physical_openparf_install,
            openparf_python=physical_openparf_python,
            seed=physical_seed,
            route_channel_width=physical_route_channel_width,
            vivado=physical_vivado,
            vivado_max_timing_paths=physical_vivado_max_timing_paths,
            vivado_place_directive=physical_vivado_place_directive,
            vivado_route_directive=physical_vivado_route_directive,
            original_ir_path=ir_path,
            assignment_path=assignment_path,
            routes_path=routes_path,
            path_database_path=path_database_path,
            logic_path_database_path=path_database_path,
            workers=physical_workers,
        )
        physical_seconds = time.monotonic() - physical_started
        physical_summary_path = output_dir / "physical/physical-summary.json"
        if phase6_comparison is not None and baseline_physical_report is not None:
            baseline_metrics = _phase6_physical_metrics(
                baseline_physical_report
            )
            chimew_metrics = _phase6_physical_metrics(physical_report)
            pin_metrics = read_json(
                output_dir
                / "phase6-comparison/chimew/phase6-adapter/pin_plan.json"
            )["metrics"]
            phase6_comparison.update(
                {
                    "status": "pass",
                    "chimew": {
                        "phase6": phase6_report,
                        "physical": physical_report,
                        "phase6_runtime_seconds": phase6_comparison[
                            "chimew_phase6_runtime_seconds"
                        ],
                        "physical_runtime_seconds": physical_seconds,
                    },
                    "baseline_physical": baseline_metrics,
                    "chimew_physical": chimew_metrics,
                    "physical_delta": {
                        "total_wirelength": (
                            chimew_metrics["total_wirelength"]
                            - baseline_metrics["total_wirelength"]
                        ),
                        "worst_critical_path_ns": (
                            chimew_metrics["worst_critical_path_ns"]
                            - baseline_metrics["worst_critical_path_ns"]
                        ),
                        "worst_wns_ns": (
                            chimew_metrics["worst_wns_ns"]
                            - baseline_metrics["worst_wns_ns"]
                        ),
                        "total_tns_ns": (
                            chimew_metrics["total_tns_ns"]
                            - baseline_metrics["total_tns_ns"]
                        ),
                        "failing_endpoints": (
                            chimew_metrics["failing_endpoints"]
                            - baseline_metrics["failing_endpoints"]
                        ),
                        "failing_endpoint_constraints": (
                            chimew_metrics["failing_endpoint_constraints"]
                            - baseline_metrics[
                                "failing_endpoint_constraints"
                            ]
                        ),
                    },
                    "pin_plan_metrics": pin_metrics,
                    "chimew_physical_runtime_seconds": physical_seconds,
                }
            )

    runtime_root = output_dir / "runtime"
    runtime_report = run_phase7c(
        schedule_path,
        platform_path,
        phase3_root / "phase3_report.json",
        phase4_root / "phase4_report.json",
        phase5_root / "phase5_report.json",
        phase6_root / "phase6_report.json",
        runtime_root,
        physical_summary_path=physical_summary_path,
        routes_path=routes_path if physical_summary_path is not None else None,
        board_link_timing_path=(
            copied_link_timing_path
            if physical_summary_path is not None
            else None
        ),
    )
    if physical and runtime_report.get("status") != "pass":
        raise ValidationError(
            "full physical flow did not close unified Phase 7C system timing"
        )
    if phase6_comparison is not None:
        if baseline_runtime_report is None:
            raise ValidationError("Phase 6 A/B baseline runtime is missing")
        phase6_comparison["chimew"]["runtime"] = runtime_report
        baseline_system = _phase6_system_timing_metrics(
            baseline_runtime_report
        )
        chimew_system = _phase6_system_timing_metrics(runtime_report)
        phase6_comparison["baseline_system_timing"] = baseline_system
        phase6_comparison["chimew_system_timing"] = chimew_system
        phase6_comparison["system_timing_delta"] = {
            key: chimew_system[key] - baseline_system[key]
            for key in baseline_system
        }
        phase6_comparison["validation"] = validate_phase6_ab_comparison(
            phase6_comparison
        )
        write_json(
            output_dir / "phase6-comparison/comparison-report.json",
            phase6_comparison,
        )

    report = {
        "schema": MULTI_FPGA_FLOW_SCHEMA,
        "status": "pass",
        "provider": MULTI_FPGA_FLOW_PROVIDER,
        "architecture_policy": "provider-neutral",
        **({"timing": timing_report} if timing_report is not None else {}),
        **(
            {"board_link_timing": link_timing_report}
            if link_timing_report is not None
            else {}
        ),
        **(
            {"frame_search": frame_search_report}
            if frame_search_report is not None
            else {}
        ),
        **(
            {"cross_stage": cross_stage_report}
            if cross_stage_report is not None
            else {}
        ),
        "runtime": runtime_report,
        **(
            {"physical": physical_report}
            if physical_report is not None
            else {}
        ),
        **(
            {"phase6_comparison": phase6_comparison}
            if phase6_comparison is not None
            else {}
        ),
        "stages": {
            "frontend": frontend_report,
            "partition": phase3_report,
            "system_route": phase4_report,
            "tdm": phase5_report,
            "split": phase6_report,
        },
        "artifacts": {
            "platform": {
                "path": "frontend/phase1/platform.normalized.json",
                "sha256": _sha256(
                    phase1_root / "platform.normalized.json"
                ),
            },
            "emuir": {
                "path": "frontend/phase1/design.emuir.json",
                "sha256": _sha256(ir_path),
            },
            "partition_constraints": {
                "path": "partition/constraints.normalized.json",
                "sha256": _sha256(
                    phase3_root / "constraints.normalized.json"
                ),
            },
            "route_constraints": {
                "path": "system-route/route_constraints.normalized.json",
                "sha256": _sha256(
                    phase4_root / "route_constraints.normalized.json"
                ),
            },
            "assignment": {
                "path": "partition/assignment.json",
                "sha256": _sha256(assignment_path),
            },
            "routes": {
                "path": "system-route/routes.json",
                "sha256": _sha256(routes_path),
            },
            "schedule": {
                "path": "tdm/schedule.json",
                "sha256": _sha256(schedule_path),
            },
            "split_manifest": {
                "path": "split/manifest.json",
                "sha256": _sha256(phase6_root / "manifest.json"),
            },
            "runtime_contract": {
                "path": "runtime/runtime_contract.json",
                "sha256": _sha256(runtime_root / "runtime_contract.json"),
            },
            "qor_report": {
                "path": "runtime/qor_report.json",
                "sha256": _sha256(runtime_root / "qor_report.json"),
            },
            **(
                {
                    "cross_stage_report": {
                        "path": "cross-stage/cross_stage_report.json",
                        "sha256": _sha256(
                            output_dir
                            / "cross-stage/cross_stage_report.json"
                        ),
                    }
                }
                if cross_stage_report is not None
                else {}
            ),
            **(
                {
                    "physical_flow_report": {
                        "path": "physical/multi-fpga-physical-flow-report.json",
                        "sha256": _sha256(
                            output_dir
                            / "physical/multi-fpga-physical-flow-report.json"
                        ),
                    },
                    "physical_summary": {
                        "path": "physical/physical-summary.json",
                        "sha256": _sha256(
                            output_dir / "physical/physical-summary.json"
                        ),
                    },
                }
                if physical_report is not None
                else {}
            ),
            **(
                {
                    "phase6_comparison_report": {
                        "path": "phase6-comparison/comparison-report.json",
                        "sha256": _sha256(
                            output_dir
                            / "phase6-comparison/comparison-report.json"
                        ),
                    }
                }
                if phase6_comparison is not None
                else {}
            ),
            **(
                {
                    "board_link_timing": {
                        "path": "timing/board-link-timing.json",
                        "sha256": _sha256(copied_link_timing_path),
                    },
                    "board_link_route_constraints": {
                        "path": "timing/board-link-route-constraints.json",
                        "sha256": _sha256(
                            effective_route_constraints_path
                        ),
                    },
                }
                if copied_link_timing_path is not None
                and effective_route_constraints_path is not None
                else {}
            ),
            **(
                {
                    "frame_search_report": {
                        "path": "frame-search/frame-search-report.json",
                        "sha256": _sha256(
                            output_dir
                            / "frame-search/frame-search-report.json"
                        ),
                    }
                }
                if frame_search_report is not None
                else {}
            ),
            **(
                {
                    "timing_path_database": {
                        "path": "timing/path-database.json",
                        "sha256": _sha256(path_database_path),
                    },
                    **(
                        {
                            "cut_segment_qualification": {
                                "path": "timing/cut-segment-qualification.json",
                                "sha256": _sha256(cut_segment_qualification_path),
                            }
                        }
                        if cut_segment_qualification_path is not None
                        else {}
                    ),
                    **(
                        {
                            "partition_net_weights": {
                                "path": "timing/partition-net-weights.json",
                                "sha256": _sha256(net_weights_path),
                            }
                        }
                        if net_weights_path.is_file()
                        else {}
                    ),
                    "cut_timing_paths": {
                        "path": "timing/cut-timing-paths.json",
                        "sha256": _sha256(projected_timing_paths),
                    },
                }
                if internal_timing_database
                else {}
            ),
        },
    }
    report["summary"] = validate_multi_fpga_flow_report(report)
    if serial_bsp_requested:
        write_json(output_dir / "board-independent-flow-report.json", report)
        assert serial_bsp_phy_provider is not None
        assert serial_bsp_runtime_sync_provider is not None
        bsp_report = run_multi_fpga_bsp_flow(
            flow_root=output_dir,
            platform_path=platform_path,
            phy_provider_path=serial_bsp_phy_provider,
            runtime_sync_provider_path=serial_bsp_runtime_sync_provider,
            output_dir=output_dir / "hardware-bsp",
            board_overlay_path=serial_bsp_board_overlay,
            gt_site_map_path=serial_bsp_gt_site_map,
            vivado_executable=serial_bsp_vivado,
            yosys_executable=serial_bsp_yosys,
            runtime_sync_root=serial_bsp_runtime_sync_root,
            ready_stable_cycles=serial_bsp_ready_stable_cycles,
        )
        report["hardware_bsp"] = bsp_report
        report["artifacts"]["board_independent_flow_report"] = {
            "path": "board-independent-flow-report.json",
            "sha256": _sha256(output_dir / "board-independent-flow-report.json"),
        }
        report["artifacts"]["hardware_bsp_flow_report"] = {
            "path": "hardware-bsp/multi-fpga-bsp-flow-report.json",
            "sha256": _sha256(
                output_dir / "hardware-bsp/multi-fpga-bsp-flow-report.json"
            ),
        }
        report["summary"] = validate_multi_fpga_flow_report(report)
    write_json(output_dir / "multi-fpga-flow-report.json", report)
    return report
