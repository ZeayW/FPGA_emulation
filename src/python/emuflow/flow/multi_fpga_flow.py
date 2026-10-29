"""Public compile request API, bounded stage adapters and flow validation.

The sole production stage order and process lifecycle belong to the native
controller. Functions below execute one adapter responsibility, never a second
Python controller.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from emuflow.transport.chimew.chimew_lookahead import materialize_xilinx_chimew_inputs
from emuflow.transport.chimew.chimew_pipeline import read_chimew_phase6_pipeline, run_chimew_phase6_pipeline
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.scheduling.frame_search import validate_frame_search_report
from emuflow.common.io import read_json, write_json
from emuflow.flow.multi_fpga_physical_flow import run_multi_fpga_physical_flow, validate_multi_fpga_physical_report
from emuflow.partition.phase3 import validate_phase3_indexed
from emuflow.routing.phase4 import validate_phase4
from emuflow.scheduling.phase5 import validate_phase5
from emuflow.transport.phase6 import run_phase6, validate_phase6
from emuflow.timing.phase7c import run_phase7c
from emuflow.timing.native_signoff import run_native_physical_signoff
from emuflow.transport.pin_plan_storage import read_pin_plan
from emuflow.physical.physical_summary_storage import read_physical_summary
from emuflow.partition.partition import CUT_MODE_STATIC_EXACT
from emuflow.platform.platform import Platform
from emuflow.transport.runtime import validate_virtual_runtime
from emuflow.scheduling.tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.scheduling.timing_routing import GLOBAL_CANDIDATE_PROVIDER

MULTI_FPGA_FLOW_SCHEMA = "emuflow.multi-fpga-flow/v2"
MULTI_FPGA_FLOW_PROVIDER = (
    "profiled-yosys+partition+system-route+tdm+split-transport+runtime"
)
PHASE6_AB_COMPARISON_SCHEMA = "emuflow.phase6-ab-comparison/v3"
_REQUIRED_STAGES = ("frontend", "partition", "system_route", "tdm", "split")

def _schedule_has_entries(schedule_path: Path) -> bool:
    """Answer the Phase 6 dispatch question without decoding hop records."""

    schedule = read_tdm_schedule(schedule_path)
    entries = schedule.get("entries", ())
    try:
        return len(entries) > 0
    except TypeError as error:
        raise ValidationError("TDM schedule entries are not sized") from error



_STAGE_SUMMARY_FIELDS = {
    "frontend": ("schema", "phase", "status", "design", "platform", "synthesis"),
    "partition": (
        "schema",
        "phase",
        "status",
        "design",
        "platform",
        "provider",
        "qualification",
        "cut_mode",
        "static_exact_candidate_policy",
        "semantic_implementation_id",
        "finalization_implementation_id",
        "stage_implementation_id",
        "configuration_implementation_id",
        "configuration_pattern_policy",
        "assignment_descriptor_writer_implementation_id",
        "validation",
    ),
    "system_route": (
        "schema",
        "phase",
        "status",
        "design",
        "platform",
        "provider",
        "qualification",
        "cut_mode",
        "candidate_generation",
        "validation",
    ),
    "tdm": (
        "schema",
        "phase",
        "status",
        "design",
        "platform",
        "provider",
        "qualification",
        "cut_mode",
        "validation",
        "timing_validation",
        "cross_layer_timing_validation",
    ),
    "split": (
        "schema",
        "phase",
        "status",
        "design",
        "platform",
        "provider",
        "validation",
        "equivalence",
        "board_binding",
    ),
}

def _stage_summary(name: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """Project a canonical stage report to its bounded orchestration view."""

    fields = _STAGE_SUMMARY_FIELDS[name]
    return {field: report[field] for field in fields if field in report}

def _timing_report_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return timing orchestration metadata without canonical path payloads."""

    return {
        field: report[field]
        for field in (
            "status",
            "mode",
            "backend",
            "sta",
            "optimization_enabled",
            "partition_weights",
            "partition_weights_applied",
            "partition_provider_weights_applied",
            "hop_refinement_weights_applied",
            "partition_weight_consumers",
            "cut_path_projection",
        )
        if field in report
    }

def _physical_report_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return a bounded physical-flow projection for the parent report."""

    return {
        field: report[field]
        for field in (
            "schema",
            "status",
            "provider",
            "backend",
            "design",
            "platform",
            "execution",
            "expected_fpgas",
            "physical_summary_ref",
            "summary",
        )
        if field in report
    }

def _validate_physical_report_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the bounded parent projection without reparsing FPGA records."""

    if (
        report.get("schema") != "emuflow.multi-fpga-physical-flow/v1"
        or report.get("status") != "pass"
        or report.get("physical_summary_ref") != "physical-summary.json"
        or not isinstance(report.get("backend"), dict)
        or not isinstance(report.get("expected_fpgas"), list)
        or not report["expected_fpgas"]
        or not isinstance(report.get("summary"), dict)
        or report["summary"].get("status") != "pass"
    ):
        raise ValidationError("multi-FPGA physical-flow summary is invalid")
    return report["summary"]

def _checked_flow_member(root: Path, path: Path, label: str) -> Path:
    """Check a flow member, including its explicitly bound prepared EmuIR."""

    root = root.resolve()
    path = path if path.is_absolute() else root / path
    if path.is_symlink() and path == root / "frontend/phase1/design.emuir.sqlite":
        frontend_path = _checked_flow_member(
            root, Path("frontend/phase1/phase1_report.json"), "frontend report"
        )
        frontend = read_json(frontend_path)
        binding = frontend.get("continuation", {})
        source = binding.get("source")
        if (frontend.get("status") == "pass"
                and binding.get("mode") == "read-only-indexed-boundary"
                and isinstance(source, str) and Path(source).is_absolute()
                and path.is_file() and path.resolve() == Path(source).resolve()):
            # Preserve the local artifact name for sealing. The terminal
            # validator hashes these input bytes just like every other member.
            return path
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
        "emuir": "frontend/phase1/design.emuir.sqlite",
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
        name: _stage_summary(name, read_json(paths[name]))
        for name in ("frontend", "partition", "system_route", "tdm", "split")
    }
    physical_report = read_json(physical_report_path)
    validate_multi_fpga_physical_report(physical_report)

    runtime_root = root / runtime_directory
    if runtime_root.is_symlink():
        raise ValidationError("multi-FPGA checkpoint runtime directory is a symlink")
    runtime_report = run_native_physical_signoff(root, root / "split", physical_root, runtime_root)
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
        "physical": _physical_report_summary(physical_report),
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
    records = [record for record in value["fpgas"] if record.get("active",True)]
    return {
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
    timing = value.get("system_timing_summary", value.get("system_timing"))
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
        or report.get("baseline_provider") != "baseline"
        or report.get("qualification") != "xilinx-placed-lookahead+fixed-lvds-wiring"
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
    lhs, rhs = baseline_arm["physical"], chimew_arm["physical"]
    backend = lhs.get("backend")
    if (not isinstance(backend,dict) or backend.get("id") not in {"rapidwright","vivado"}
            or backend != rhs.get("backend") or lhs.get("architecture") != rhs.get("architecture")
            or not isinstance(lhs.get("execution"),dict) or lhs["execution"] != rhs.get("execution")):
        raise ValidationError("Phase 6 A/B backend, device or execution options differ")
    baseline = report.get("baseline_physical")
    chimew = report.get("chimew_physical")
    delta = report.get("physical_delta")
    required_metrics = {
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
            expected_tdm_provider = TDM_EXACT_DEPENDENCY_PROVIDER
            if (
                stages["system_route"].get("provider")
                != GLOBAL_CANDIDATE_PROVIDER
                or stages["system_route"].get("validation", {}).get(
                    "timing_optimization_enabled"
                ) is not False
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
    runtime_timing = (
        runtime.get("runtime_timing_summary", runtime.get("runtime_timing", {}))
        if isinstance(runtime, dict)
        else {}
    )
    if (
        not isinstance(runtime, dict)
        or runtime.get("design") != design
        or runtime.get("platform") != platform
        or runtime.get("validation", {}).get("status") != "pass"
        or runtime_timing.get("status") == "fail"
    ):
        raise ValidationError("multi-FPGA runtime contract did not pass")
    physical = report.get("physical")
    if physical is not None:
        physical_validation = (
            validate_multi_fpga_physical_report(physical)
            if "fpgas" in physical
            else _validate_physical_report_summary(physical)
        )
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
            _physical_report_summary(
                phase6_comparison["chimew"]["physical"]
            )
            != _physical_report_summary(physical)
            or _stage_summary(
                "split", phase6_comparison["chimew"]["phase6"]
            )
            != stages["split"]
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
    if "hardware_bsp" in report:
        raise ValidationError("hardware BSP reports are no longer supported")
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
    if report.get("cross_stage") is not None:
        raise ValidationError("retired cross-stage ratio-feedback flow is not supported")
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
        flow_root, Path("frontend/phase1/design.emuir.sqlite"), "EmuIR"
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
        if _stage_summary(stage, live) != report["stages"][stage]:
            raise ValidationError(
                f"multi-FPGA live {stage} report disagrees with the flow report"
            )

    phase3_validation = validate_phase3_indexed(
        ir_path,
        platform_path,
        assignment_path.with_name("phase3_report.json"),
        assignment_path.with_name("assignment.sqlite"),
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

    projected_paths = flow_root / "timing/cut-timing-paths.json"
    timing_paths_path = (
        _checked_flow_member(flow_root, Path("timing/cut-timing-paths.json"), "projected timing paths")
        if projected_paths.exists() else None
    )
    phase4_validation = validate_phase4(
        assignment_path,
        platform_path,
        routes_path,
        timing_paths_path=timing_paths_path,
    )
    if phase4_validation != report["stages"]["system_route"]["validation"]:
        raise ValidationError("independent Phase 4 replay disagrees")

    phase5_validation = validate_phase5(
        routes_path,
        platform_path,
        schedule_path,
        assignment_path=assignment_path,
    )
    timing_validation = phase5_validation.pop("timing", None)
    cross_layer_validation = phase5_validation.pop(
        "cross_layer_timing", None
    )
    if phase5_validation != report["stages"]["tdm"]["validation"]:
        raise ValidationError("independent Phase 5 replay disagrees")
    if timing_validation is not None and timing_validation != report["stages"][
        "tdm"
    ].get("timing_validation"):
        raise ValidationError("independent Phase 5 timing replay disagrees")
    if cross_layer_validation != report["stages"]["tdm"].get(
        "cross_layer_timing_validation"
    ):
        raise ValidationError(
            "independent cross-layer timing replay disagrees"
        )

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
    split_manifest = read_json(manifest_path)
    clock_periods = split_manifest.get("clock_periods")
    if not isinstance(clock_periods, dict) or not clock_periods:
        raise ValidationError(
            "split manifest lacks the DUT clock-period contract"
        )
    terminal_schedule = read_tdm_schedule(schedule_path)
    try:
        runtime_validation = validate_virtual_runtime(
            runtime_contract, terminal_schedule, platform, clock_periods
        )
    finally:
        close_schedule = getattr(terminal_schedule, "close", None)
        if callable(close_schedule):
            close_schedule()
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
        physical_matches = (
            live_physical == physical
            if "fpgas" in physical
            else _physical_report_summary(live_physical) == physical
        )
        if not physical_matches:
            raise ValidationError(
                "live physical report disagrees with the multi-FPGA flow report"
            )
        physical_summary_path = _checked_flow_member(
            flow_root,
            Path("physical/physical-summary.json"),
            "physical summary",
        )
        with read_physical_summary(physical_summary_path) as summary_view:
            validate_multi_fpga_physical_report(live_physical, summary_view)
        candidate_link_timing = flow_root / "timing/board-link-timing.json"
        if candidate_link_timing.is_file():
            board_link_timing_path = _checked_flow_member(
                flow_root, candidate_link_timing, "board-link timing"
            )

    from emuflow.timing.qor_storage import qor_validation_proof
    stored_qor_path = _checked_flow_member(flow_root, Path("runtime/qor_report.json"), "QoR report")
    stored_proof, standalone_sta = qor_validation_proof(stored_qor_path)
    if physical_summary_path is not None and not standalone_sta:
        raise ValidationError(
            "physical terminal validation requires standalone OpenSTA evidence"
        )
    if standalone_sta:
        for name in ("measurements.json", "opensta.log"):
            _checked_flow_member(flow_root, Path("runtime/global-opensta") / name,
                                 "standalone OpenSTA result")
    with tempfile.TemporaryDirectory(prefix="emuflow-flow-validate-") as temporary:
        replay = run_phase7c(
            schedule_path,
            platform_path,
            flow_root / "partition/phase3_report.json",
            flow_root / "system-route/phase4_report.json",
            flow_root / "tdm/phase5_report.json",
            flow_root / "split/phase6_report.json",
            Path(temporary),
            assignment_path=assignment_path,
            physical_summary_path=physical_summary_path,
            routes_path=routes_path if physical_summary_path is not None else None,
            board_link_timing_path=board_link_timing_path,
            global_sta_results_dir=flow_root / "runtime/global-opensta" if standalone_sta else None,
            global_timing_engine="opensta",
        )
        replay_proof, _ = qor_validation_proof(Path(temporary) / "qor_report.json")
    if replay_proof != stored_proof:
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

class _CompileContext:
    """Execution-scoped configuration and bounded inter-stage state.

    Native orchestration serializes only this state between isolated adapters;
    design graphs remain in their phase artifacts, not in this controller.
    """

    def __init__(self, values):
        self.__dict__.update(values)

    def measured(self, stage, function, *args, **kwargs):
        from emuflow.common.stage_metrics import measure_stage
        return measure_stage(self.output_dir, stage, function, *args, **kwargs)

def _compile_chimew_admission(ctx: _CompileContext):
    """Admit the remaining Chimew device catalog after native common preflight."""
    if ctx.phase6_provider != "chimew":
        raise EmuFlowError("Chimew package admission requires the Chimew provider")
    from emuflow.transport.chimew.xilinx_chimew_geometry import XilinxPackageGeometry
    from emuflow.transport.chimew.chimew_fixed_wiring import FixedWiringChannels
    board = FixedWiringChannels(Platform.load(ctx.platform_path))
    parts = {fpga.part for fpga in Platform.load(ctx.platform_path).fpgas}
    if len(parts) != 1:
        raise EmuFlowError("Chimew requires a single Xilinx device model")
    ctx.chimew_package_geometry = XilinxPackageGeometry.open(ctx.physical_architecture, next(iter(parts)))
    pins = ctx.chimew_package_geometry.pins
    for wire in board.board["wires"]:
        for end in wire["endpoints"]:
            for polarity in ("p", "n"):
                pin = end[polarity]
                if pins.get(pin["package_pin"], (None,))[0] != pin["site"]:
                    raise ValidationError("Chimew native package geometry does not cover the fixed P/N wiring")


def _run_selected_physical(ctx, split_root, schedule_path, output_dir):
    catalog = getattr(ctx,"chimew_package_geometry",None)
    if catalog is not None:
        catalog.require_unchanged()
    return ctx.measured("phase7", run_multi_fpga_physical_flow,
        split_root,
        ctx.platform_path,
        schedule_path,
        output_dir,
        backend=ctx.physical_backend,
        architecture=ctx.effective_physical_architecture,
        yosys=ctx.yosys,
        openparf_install=ctx.physical_openparf_install,
        openparf_python=ctx.physical_openparf_python,
        seed=ctx.physical_seed,
        vivado=ctx.physical_vivado,
        vivado_place_directive=ctx.physical_vivado_place_directive,
        vivado_route_directive=ctx.physical_vivado_route_directive,
        rapidwright_jar=ctx.physical_rapidwright_jar,
        rapidwright_java=ctx.physical_rapidwright_java,
        rapidwright_java_source=ctx.physical_rapidwright_java_source,
        rapidwright_device_data=ctx.physical_rapidwright_device_data,
        rapidwright_timing_data=ctx.physical_rapidwright_timing_data,
        rapidwright_opensta=ctx.physical_rapidwright_opensta,
        rapidwright_native_constraints=(
            ctx.physical_rapidwright_native_constraints
        ),
        rapidwright_provider_manifest=(
            ctx.physical_rapidwright_provider_manifest
        ),
        original_ir_path=ctx.ir_path,
        assignment_path=ctx.assignment_path,
        routes_path=ctx.routes_path,
        path_database_path=ctx.path_database_path,
        logic_path_database_path=ctx.path_database_path,
        workers=ctx.physical_workers,
    )

def _compile_split(ctx: _CompileContext):
    if ctx.phase6_provider != "chimew":
        raise EmuFlowError("native compiler baseline Phase 6 is owned by the C++ stage")
    ctx.routes_path = ctx.phase4_root / "routes.json"
    ctx.schedule_path = ctx.phase5_root / "schedule.json"

    ctx.phase6_root = ctx.output_dir / "split"
    has_scheduled_signals = _schedule_has_entries(ctx.schedule_path)
    if ctx.phase6_provider == "chimew" and not has_scheduled_signals:
        raise EmuFlowError(
            "--phase6-provider chimew requires at least one scheduled "
            "inter-FPGA signal"
        )
    ctx.baseline_physical_report = None
    ctx.baseline_runtime_report = None
    ctx.phase6_comparison = None
    ctx.effective_physical_architecture = ctx.physical_architecture
    if ctx.phase6_provider == "chimew":
        comparison_root = ctx.output_dir / "phase6-comparison"
        baseline_root = comparison_root / "baseline"
        baseline_split = baseline_root / "split"
        baseline_phase6_started = time.monotonic()
        baseline_phase6_report = ctx.measured("phase6", run_phase6,
            ctx.ir_path,
            ctx.assignment_path,
            ctx.schedule_path,
            ctx.platform_path,
            baseline_split,
            equivalence_cycles=ctx.equivalence_cycles,
            equivalence_seed=ctx.equivalence_seed,
            transport_cost_path=ctx.transport_cost_db,
            clock_periods=ctx.clock_periods,
        )
        baseline_phase6_seconds = time.monotonic() - baseline_phase6_started
        baseline_physical_started = time.monotonic()
        ctx.baseline_physical_report = _run_selected_physical(ctx, baseline_split, ctx.schedule_path, baseline_root / "physical")
        baseline_physical_seconds = time.monotonic() - baseline_physical_started
        ctx.baseline_runtime_report = ctx.measured("phase7c", run_native_physical_signoff,
            ctx.output_dir, baseline_split, baseline_root / "physical", baseline_root / "runtime",
            executable=ctx.global_sta_executable, launcher_reference=ctx.global_sta_launcher_reference,
        )
        lookahead_root = comparison_root / "lookahead"
        lookahead = materialize_xilinx_chimew_inputs(
            architecture_path=ctx.physical_architecture,
            package_geometry=ctx.chimew_package_geometry,
            ir_path=ctx.ir_path,
            schedule_path=ctx.schedule_path,
            routes_path=ctx.routes_path,
            platform_path=ctx.platform_path,
            physical_report=ctx.baseline_physical_report,
            output_dir=lookahead_root,
            timing_paths_path=(
                ctx.projected_timing_paths if ctx.timing_driven else None
            ),
            region_count=ctx.phase6_chimew_region_count,
            grouper=ctx.phase6_chimew_grouper,
            refiner=ctx.phase6_chimew_refiner,
        )
        inputs = lookahead["artifacts"]
        chimew_schedule_path = Path(inputs["schedule"]["path"])
        sources_map = lookahead["sources"]
        chimew_pipeline_root = comparison_root / "chimew"
        pipeline = run_chimew_phase6_pipeline(
            chimew_schedule_path,
            ctx.platform_path,
            Path(inputs["crossings"]["path"]),
            Path(inputs["positions"]["path"]),
            Path(inputs["rudy_input"]["path"]),
            Path(inputs["bank_channel_input"]["path"]),
            Path(inputs["electrical_map"]["path"]),
            chimew_pipeline_root,
            source_paths={
                label: Path(path) for label, path in sources_map.items()
            },
            grouper=ctx.phase6_chimew_grouper,
            refiner=ctx.phase6_chimew_refiner,
            rudy=ctx.phase6_chimew_rudy,
            assigner=ctx.phase6_chimew_assigner,
            region_count=ctx.phase6_chimew_region_count,
        )
        with read_chimew_phase6_pipeline(chimew_pipeline_root) as (
            leased_pipeline,
            _pipeline_validation,
        ):
            pipeline = dict(leased_pipeline)
            adapter_artifacts = pipeline["artifacts"]
            chimew_phase6_started = time.monotonic()
            ctx.phase6_report = ctx.measured("phase6", run_phase6,
                ctx.ir_path,
                ctx.assignment_path,
                chimew_schedule_path,
                ctx.platform_path,
                ctx.phase6_root,
                equivalence_cycles=ctx.equivalence_cycles,
                equivalence_seed=ctx.equivalence_seed,
                pin_plan_path=(
                    chimew_pipeline_root
                    / adapter_artifacts["adapter_pin_plan"]["path"]
                ),
                position_hints_path=(
                    chimew_pipeline_root
                    / adapter_artifacts["adapter_position_hints"]["path"]
                ),
                electrical_binding_path=(
                    chimew_pipeline_root
                    / adapter_artifacts["adapter_electrical_binding"]["path"]
                ),
                transport_cost_path=ctx.transport_cost_db,
                clock_periods=ctx.clock_periods,
            )
            ctx.chimew_pin_metrics = read_pin_plan(
                chimew_pipeline_root
                / adapter_artifacts["adapter_pin_plan"]["path"]
            )["metrics"]
        chimew_phase6_seconds = time.monotonic() - chimew_phase6_started
        ctx.phase6_comparison = {
            "schema": PHASE6_AB_COMPARISON_SCHEMA,
            "status": "pending-physical-comparison",
            "selected_provider": "chimew",
            "baseline_provider": "baseline",
            "qualification": "xilinx-placed-lookahead+fixed-lvds-wiring",
            "frozen_upstream": {
                "emuir_sha256": _sha256(ctx.ir_path),
                "assignment_sha256": _sha256(ctx.assignment_path),
                "routes_sha256": _sha256(ctx.routes_path),
                "schedule_sha256": _sha256(ctx.schedule_path),
                "platform_sha256": _sha256(ctx.platform_path),
            },
            "claim_boundary": {
                "lookahead": (
                    "normalized virtual regions from the selected Xilinx physical "
                    "prepass, not final device SLR/SLL closure"
                ),
                "electrical": (
                    "fixed source-qualified LVDS P/N wiring; not board electrical signoff"
                ),
            },
            "baseline": {
                "phase6": baseline_phase6_report,
                "physical": ctx.baseline_physical_report,
                "runtime": ctx.baseline_runtime_report,
                "phase6_runtime_seconds": baseline_phase6_seconds,
                "physical_runtime_seconds": baseline_physical_seconds,
            },
            "lookahead": lookahead,
            "chimew_pipeline": pipeline,
            "chimew_phase6_runtime_seconds": chimew_phase6_seconds,
        }

def _compile_physical(ctx: _CompileContext):
    if ctx.phase6_provider == "baseline":
        raise EmuFlowError("native compiler physical stage is owned by the C++ controller")
    ctx.physical_report = None
    ctx.physical_summary_path = None
    if ctx.physical:
        physical_started = time.monotonic()
        ctx.physical_report = _run_selected_physical(ctx, ctx.phase6_root, ctx.schedule_path, ctx.output_dir / "physical")
        physical_seconds = time.monotonic() - physical_started
        ctx.physical_summary_path = ctx.output_dir / "physical/physical-summary.json"
        if ctx.phase6_comparison is not None:
            from emuflow.common.native_tools import native_file_generation
            ctx.native_phase6_comparison_input = ctx.output_dir / "phase6-comparison/comparison-input.json"
            write_json(ctx.native_phase6_comparison_input, {
                **ctx.phase6_comparison,
                "pin_plan_metrics": ctx.chimew_pin_metrics,
                "chimew_physical_runtime_seconds": physical_seconds,
            })
            ctx.native_phase6_comparison_generation = tuple(
                str(value) for value in native_file_generation(ctx.native_phase6_comparison_input)
            )

from emuflow.flow.compile_options import COMPILE_DEFAULTS


def run_multi_fpga_flow(
    platform_path: Path = COMPILE_DEFAULTS['platform_path'],
    output_dir: Optional[Path] = COMPILE_DEFAULTS['output_dir'],
    *,
    sources: Iterable[Path] = COMPILE_DEFAULTS['sources'],
    include_dirs: Iterable[Path] = COMPILE_DEFAULTS['include_dirs'],
    defines: Iterable[str] = COMPILE_DEFAULTS['defines'],
    source_checksums: Optional[Dict[str, str]] = COMPILE_DEFAULTS['source_checksums'],
    top: Optional[str] = COMPILE_DEFAULTS['top'],
    clocks: Iterable[str] = COMPILE_DEFAULTS['clocks'],
    yosys_json: Optional[Path] = COMPILE_DEFAULTS['yosys_json'],
    prepared_frontend: Optional[Path] = COMPILE_DEFAULTS['prepared_frontend'],
    prepared_timing: Optional[Path] = COMPILE_DEFAULTS['prepared_timing'],
    yosys: Optional[str] = COMPILE_DEFAULTS['yosys'],
    mapping_profile: str = COMPILE_DEFAULTS['mapping_profile'],
    xilinx_mapping_strategy: Optional[str] = COMPILE_DEFAULTS['xilinx_mapping_strategy'],
    partition_constraints: Optional[Path] = COMPILE_DEFAULTS['partition_constraints'],
    partition_provider: str = COMPILE_DEFAULTS['partition_provider'],
    seed: int = COMPILE_DEFAULTS['seed'],
    min_used_fpgas: Optional[int] = COMPILE_DEFAULTS['min_used_fpgas'],
    balance_tolerance: Optional[float] = COMPILE_DEFAULTS['balance_tolerance'],
    openroad: Optional[str] = COMPILE_DEFAULTS['openroad'],
    tritonpart_solution: Optional[Path] = COMPILE_DEFAULTS['tritonpart_solution'],
    patron_refiner: Optional[str] = COMPILE_DEFAULTS['patron_refiner'],
    patron_max_moves: Optional[int] = COMPILE_DEFAULTS['patron_max_moves'],
    patron_algorithm_version: int = COMPILE_DEFAULTS['patron_algorithm_version'],
    partition_timeout_seconds: int = COMPILE_DEFAULTS['partition_timeout_seconds'],
    partition_seed_attempts: int = COMPILE_DEFAULTS['partition_seed_attempts'],
    partition_num_initial_solutions: int = COMPILE_DEFAULTS['partition_num_initial_solutions'],
    partition_num_best_initial_solutions: int = COMPILE_DEFAULTS['partition_num_best_initial_solutions'],
    partition_repair_min_used_fpgas: bool = COMPILE_DEFAULTS['partition_repair_min_used_fpgas'],
    cut_mode: str = COMPILE_DEFAULTS['cut_mode'],
    max_cross_fpga_dependency_depth: int = (
        COMPILE_DEFAULTS['max_cross_fpga_dependency_depth']
    ),
    static_exact_candidate_policy: str = COMPILE_DEFAULTS['static_exact_candidate_policy'],
    timing_driven: bool = COMPILE_DEFAULTS['timing_driven'],
    clock_periods: Optional[Dict[str, float]] = COMPILE_DEFAULTS['clock_periods'],
    timing_io: Optional[Mapping[str, Any]] = COMPILE_DEFAULTS['timing_io'],
    timing_model: Path = COMPILE_DEFAULTS['timing_model'],
    architecture_timing_db: Optional[Path] = COMPILE_DEFAULTS['architecture_timing_db'],
    opensta: Optional[str] = COMPILE_DEFAULTS['opensta'],
    sta_max_paths: int = COMPILE_DEFAULTS['sta_max_paths'],
    timing_criticality_scale: float = COMPILE_DEFAULTS['timing_criticality_scale'],
    timing_criticality_exponent: float = COMPILE_DEFAULTS['timing_criticality_exponent'],
    route_constraints: Optional[Path] = COMPILE_DEFAULTS['route_constraints'],
    board_link_timing_db: Optional[Path] = COMPILE_DEFAULTS['board_link_timing_db'],
    transport_cost_db: Optional[Path] = COMPILE_DEFAULTS['transport_cost_db'],
    timing_paths: Optional[Path] = COMPILE_DEFAULTS['timing_paths'],
    router: Optional[str] = COMPILE_DEFAULTS['router'],
    route_provider: Optional[str] = COMPILE_DEFAULTS['route_provider'],
    route_candidate_workers: int = COMPILE_DEFAULTS['route_candidate_workers'],
    frame_slots: Optional[int] = COMPILE_DEFAULTS['frame_slots'],
    optimize_frame_slots: bool = COMPILE_DEFAULTS['optimize_frame_slots'],
    route_max_iterations: Optional[int] = COMPILE_DEFAULTS['route_max_iterations'],
    tdm_provider: Optional[str] = COMPILE_DEFAULTS['tdm_provider'],

    simulation_frames: int = COMPILE_DEFAULTS['simulation_frames'],
    equivalence_cycles: int = COMPILE_DEFAULTS['equivalence_cycles'],
    equivalence_seed: int = COMPILE_DEFAULTS['equivalence_seed'],
    phase6_provider: str = COMPILE_DEFAULTS['phase6_provider'],
    phase6_chimew_region_count: int = COMPILE_DEFAULTS['phase6_chimew_region_count'],
    phase6_chimew_grouper: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_grouper'],
    phase6_chimew_refiner: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_refiner'],
    phase6_chimew_rudy: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_rudy'],
    phase6_chimew_assigner: Optional[str] = COMPILE_DEFAULTS['phase6_chimew_assigner'],
    physical: bool = COMPILE_DEFAULTS['physical'],
    physical_backend: str = COMPILE_DEFAULTS['physical_backend'],
    physical_architecture: Optional[Path] = COMPILE_DEFAULTS['physical_architecture'],
    physical_openparf_install: Optional[Path] = COMPILE_DEFAULTS['physical_openparf_install'],
    physical_openparf_python: Optional[Path] = COMPILE_DEFAULTS['physical_openparf_python'],
    physical_seed: int = COMPILE_DEFAULTS['physical_seed'],
    physical_vivado: Optional[str] = COMPILE_DEFAULTS['physical_vivado'],
    physical_vivado_place_directive: str = COMPILE_DEFAULTS['physical_vivado_place_directive'],
    physical_vivado_route_directive: str = COMPILE_DEFAULTS['physical_vivado_route_directive'],
    physical_rapidwright_jar: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_jar'],
    physical_rapidwright_java: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_java'],
    physical_rapidwright_java_source: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_java_source'],
    physical_rapidwright_device_data: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_device_data'],
    physical_rapidwright_timing_data: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_timing_data'],
    physical_rapidwright_opensta: Optional[str] = COMPILE_DEFAULTS['physical_rapidwright_opensta'],
    physical_rapidwright_native_constraints: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_native_constraints'],
    physical_rapidwright_provider_manifest: Optional[Path] = COMPILE_DEFAULTS['physical_rapidwright_provider_manifest'],
    physical_workers: int = COMPILE_DEFAULTS['physical_workers'],
    global_sta_executable: Optional[str] = COMPILE_DEFAULTS['global_sta_executable'],
    global_timing_engine: str = COMPILE_DEFAULTS['global_timing_engine'],
) -> Dict[str, Any]:
    """Submit a bounded request to the sole native production controller.

    Physical, signoff and Chimew adapters remain explicit transitional owners;
    selecting this entry does not assert complete native data-plane migration.
    """
    options = locals()
    from emuflow.flow.native_compile import run_native_multi_fpga_flow

    return run_native_multi_fpga_flow(**options)
