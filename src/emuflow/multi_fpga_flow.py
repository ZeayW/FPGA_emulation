"""Checked, board-independent multi-FPGA compilation orchestration."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .board_link_timing import (
    directed_route_link_delays,
    validate_board_link_timing,
)
from .chimew_lookahead import materialize_xilinx_chimew_inputs
from .chimew_pipeline import (
    read_chimew_phase6_pipeline,
    run_chimew_phase6_pipeline,
)
from .cut_segment_qualification import build_cut_segment_qualification
from .default_platform import DEFAULT_PLATFORM_PATH
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
from .opensta import (
    DEFAULT_TIMING_MODEL,
    check_opensta_engine_version,
    run_opensta_path_database,
)
from .openparf import validate_openparf_runtime
from .phase1 import run_phase1
from .phase3 import (
    promote_patron_baseline,
    run_phase3,
    validate_phase3,
    validate_phase3_indexed,
)
from .phase4 import run_phase4, validate_phase4
from .phase5 import run_phase5, validate_phase5
from .phase6 import run_phase6, validate_phase6
from .phase7c import run_phase7c
from .pin_plan_storage import read_pin_plan
from .physical_summary_storage import read_physical_summary
from .partition import (
    CUT_MODE_SEQUENTIAL_ONLY,
    CUT_MODE_STATIC_EXACT,
    DEFAULT_PARTITION_CUT_MODE,
    DEFAULT_PATRON_ALGORITHM_VERSION,
)
from .combinational_cut import (
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)
from .native_tools import resolve_native_executable
from .platform import Platform
from .ppro_calibrated_platform import validate_transport_cost_database
from .runtime import validate_virtual_runtime
from .routing import SYSTEM_ROUTE_CONSTRAINTS_SCHEMA
from .tdm import TDM_EXACT_DEPENDENCY_PROVIDER
from .tdm_schedule_storage import read_tdm_schedule, tdm_schedule_store_path
from .system_route_storage import read_system_routes, system_route_store_path
from .timing_routing import GLOBAL_CANDIDATE_PROVIDER
from .sta import (
    derive_partition_net_weights,
    partition_net_weights_payload_path,
    project_sta_path_database,
    sta_path_database_payload_path,
)
from .synthesis import (
    DEFAULT_XILINX_MAPPING_STRATEGY,
    VALID_XILINX_MAPPING_STRATEGIES,
    run_xilinx_ultrascaleplus_yosys_stream,
)
from .xilinx_preplacement_timing import XILINX_PREPLACEMENT_TIMING_SCHEMA
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
    normalize_xilinx_indexed_staging,
)

MULTI_FPGA_FLOW_SCHEMA = "emuflow.multi-fpga-flow/v2"
MULTI_FPGA_FLOW_PROVIDER = (
    "profiled-yosys+partition+system-route+tdm+split-transport+runtime"
)
MULTI_FPGA_MAPPING_PROFILES = (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
)
MULTI_FPGA_PHASE6_PROVIDERS = ("chimew", "baseline")
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

def _validate_mapping_timing_contract(
    mapping_profile: str,
    timing_db_path: Optional[Path],
) -> None:
    """Require the maintained Xilinx pre-placement contract before synthesis."""
    if mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
        raise EmuFlowError(f"unsupported multi-FPGA mapping profile {mapping_profile!r}")
    if timing_db_path is None:
        raise EmuFlowError(
            "xilinx-ultrascaleplus-open-v1 requires a source-sealed "
            "Xilinx pre-placement --architecture-timing-db"
        )
    expected_schema = XILINX_PREPLACEMENT_TIMING_SCHEMA

    observed_schema = read_json(timing_db_path).get("schema")
    if observed_schema != expected_schema:
        raise EmuFlowError(
            f"mapping profile {mapping_profile!r} requires Architecture "
            f"TimingDB schema {expected_schema!r}, got {observed_schema!r}"
        )

def _resolve_partition_openroad(
    partition_provider: str,
    executable: Optional[str],
    solution_input: Optional[Path] = None,
) -> Optional[str]:
    """Resolve a required partition engine before producing flow artifacts."""

    if solution_input is not None:
        return executable
    if partition_provider in {"tritonpart", "patron"}:
        return resolve_native_executable("openroad", executable)
    return executable

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


def _runtime_timing_artifacts(runtime_root: Path, flow_root: Path, runtime_report):
    payload_name = runtime_report.get("artifacts",{}).get("system_timing_paths")
    if payload_name is None:
        return {}
    from .system_timing_storage import system_timing_payload_reference
    reference = system_timing_payload_reference(runtime_root/"qor_report.json")
    if payload_name != reference["artifact"]:
        raise ValidationError("Phase 7C system timing payload reference disagrees")
    payload = _checked_flow_member(flow_root,runtime_root/payload_name,"system timing paths")
    return {"system_timing_paths": {"path": payload.relative_to(flow_root).as_posix(), "sha256": reference["sha256"]}}


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
    runtime_report = run_phase7c(
        paths["schedule"],
        paths["platform"],
        paths["partition"],
        paths["system_route"],
        paths["tdm"],
        paths["split"],
        runtime_root,
        assignment_path=paths["assignment"],
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
            **_runtime_timing_artifacts(runtime_root, root, runtime_report),
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

    stored_qor = read_json(
        _checked_flow_member(flow_root, Path("runtime/qor_report.json"), "QoR report")
    )
    standalone_sta = stored_qor.get("timing", {}).get("global_opensta", {}).get("execution") == "standalone"
    empty_sta = stored_qor.get("timing", {}).get("global_opensta", {}).get("authority") == "no-numerical-checks"
    opensta_result = standalone_sta or empty_sta
    if physical_summary_path is not None and not opensta_result:
        raise ValidationError("physical terminal validation requires standalone OpenSTA evidence")
    if opensta_result:
        for name in (("measurements.tsv", "opensta.log") if standalone_sta else ("measurements.tsv",)):
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
            global_sta_results_dir=flow_root / "runtime/global-opensta" if opensta_result else None,
            global_timing_engine="opensta",
        )
        replay_qor = read_json(Path(temporary) / "qor_report.json")
    if replay_qor != stored_qor:
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

    def __init__(self, values, *, native_mode=False):
        self.__dict__.update(values)
        self._native_mode = native_mode

    def measured(self, stage, function, *args, **kwargs):
        from .stage_metrics import measure_stage
        return measure_stage(self.output_dir, stage, function, *args, **kwargs)

def _compile_preflight(ctx: _CompileContext):
    if ctx.physical and ctx.sta_max_paths is not None:
        raise EmuFlowError("physical execution requires all-endpoint Phase2 timing; --sta-max-paths is only for non-physical diagnostics")
    from .vivado_clocks import validate_dut_frequency_mhz
    validate_dut_frequency_mhz(ctx.physical_vivado_dut_frequency_mhz,
                               enabled=ctx.physical and ctx.physical_backend == "vivado")
    if (
        isinstance(ctx.patron_algorithm_version, bool)
        or not isinstance(ctx.patron_algorithm_version, int)
        or ctx.patron_algorithm_version != 14
    ):
        raise EmuFlowError("only PATRON v14 is supported")
    if ctx.partition_provider not in {"patron", "tritonpart"}:
        raise EmuFlowError(f"unsupported Phase 3 provider {ctx.partition_provider!r}")
    if ctx._native_mode and (
        ctx.optimize_frame_slots or
        ctx.serial_bsp_phy_provider is not None
    ):
        raise EmuFlowError(
            "native compile does not yet support frame search "
            "or serial BSP continuation"
        )
    if ctx.output_dir is None:
        raise EmuFlowError("multi-FPGA compilation requires an output directory")

    if ctx.global_timing_engine != "opensta":
        raise EmuFlowError("only the OpenSTA global timing engine is supported")
    if ctx.global_sta_executable is not None and not ctx.physical:
        raise EmuFlowError("global OpenSTA qualification requires --physical")
    if ctx.physical_backend not in {"rapidwright", "vivado"}:
        raise EmuFlowError(f"unsupported physical backend {ctx.physical_backend!r}")
    if ctx.mapping_profile not in MULTI_FPGA_MAPPING_PROFILES:
        raise EmuFlowError(
            "unsupported multi-FPGA mapping profile "
            f"{ctx.mapping_profile!r}; expected one of "
            f"{', '.join(MULTI_FPGA_MAPPING_PROFILES)}"
        )
    if ctx.xilinx_mapping_strategy is not None:
        if ctx.xilinx_mapping_strategy not in VALID_XILINX_MAPPING_STRATEGIES:
            raise EmuFlowError("unsupported Xilinx mapping strategy")
        if ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE or ctx.yosys_json is not None:
            raise EmuFlowError(
                "Xilinx mapping strategy requires native Xilinx RTL synthesis"
            )
    if (
        ctx.physical
        and ctx.physical_backend == "rapidwright"
        and ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE
    ):
        raise EmuFlowError(
            "physical-backend=rapidwright requires "
            "mapping-profile=xilinx-ultrascaleplus-open-v1"
        )
    if ctx.tdm_provider not in {None, TDM_EXACT_DEPENDENCY_PROVIDER}:
        raise EmuFlowError(f"unsupported Phase 5 provider {ctx.tdm_provider!r}")
    if ctx.cut_mode != CUT_MODE_STATIC_EXACT:
        raise EmuFlowError("multi-FPGA compilation requires static-exact-combinational")
    if ctx.route_provider not in {None, GLOBAL_CANDIDATE_PROVIDER}:
        raise EmuFlowError("unsupported Phase 4 routing provider; use " + GLOBAL_CANDIDATE_PROVIDER)
    if ctx.phase6_provider not in MULTI_FPGA_PHASE6_PROVIDERS:
        raise EmuFlowError(
            "unsupported Phase 6 provider "
            f"{ctx.phase6_provider!r}; expected one of "
            f"{', '.join(MULTI_FPGA_PHASE6_PROVIDERS)}"
        )
    if ctx.cut_mode not in {CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT}:
        raise EmuFlowError("unsupported combinational cut mode")
    ctx.exact_cut_mode = ctx.cut_mode == CUT_MODE_STATIC_EXACT
    ctx.clocks = tuple(ctx.clocks)
    if (
        ctx.partition_provider == "patron"
        and ctx.patron_algorithm_version == 14
        and not ctx.exact_cut_mode
    ):
        raise EmuFlowError(
            "PATRON v14 requires generalized Static Exact mode"
        )
    if not 2 <= ctx.phase6_chimew_region_count <= 31:
        raise EmuFlowError("--phase6-chimew-region-count must be in [2, 31]")
    if ctx.phase6_provider == "chimew":
        if (not ctx.physical or ctx.physical_backend not in {"rapidwright", "vivado"}
                or ctx.mapping_profile != XILINX_ULTRASCALEPLUS_OPEN_PROFILE):
            raise EmuFlowError("Chimew requires --physical and the Xilinx mapping with RapidWright or Vivado")
        if ctx.physical_architecture is None:
            raise EmuFlowError("Chimew requires the source-qualified Xilinx ArchitectureDB package geometry")
        from .xilinx_chimew_geometry import XilinxPackageGeometry
        from .chimew_fixed_wiring import FixedWiringChannels
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
    _validate_mapping_timing_contract(
        ctx.mapping_profile, ctx.architecture_timing_db
    )
    ctx.internal_timing_database = ctx.timing_paths is None
    if ctx.partition_provider == "patron" and not ctx.internal_timing_database:
        raise EmuFlowError(
            "--partition-provider patron requires the internally generated "
            "complete TimingPathDB"
        )
    if ctx.timing_paths is not None and (
        ctx.timing_driven or ctx.architecture_timing_db is not None
    ):
        raise EmuFlowError(
            "--timing-driven/--architecture-timing-db cannot be combined "
            "with externally projected --timing-paths"
        )
    if ctx.physical and ctx.timing_paths is not None:
        raise EmuFlowError(
            "physical Phase 7C requires an internally generated complete "
            "TimingPathDB; externally projected --timing-paths are only "
            "supported by non-physical algorithm tests"
        )
    if ctx.optimize_frame_slots and not ctx.timing_driven:
        raise EmuFlowError(
            "--optimize-frame-slots requires --timing-driven"
        )
    if ctx.internal_timing_database and not ctx.clock_periods:
        raise EmuFlowError(
            "multi-FPGA timing analysis requires at least one "
            "--clock-period CLOCK=PERIOD_NS; TimingPathDB remains mandatory "
            "for physical Phase 7C even with --no-timing-driven"
        )
    ctx.serial_bsp_requested = ctx.serial_bsp_phy_provider is not None
    serial_bsp_auxiliary = any(
        value is not None
        for value in (
            ctx.serial_bsp_runtime_sync_provider,
            ctx.serial_bsp_board_overlay,
            ctx.serial_bsp_gt_site_map,
            ctx.serial_bsp_vivado,
            ctx.serial_bsp_yosys,
            ctx.serial_bsp_runtime_sync_root,
        )
    )
    if serial_bsp_auxiliary and not ctx.serial_bsp_requested:
        raise EmuFlowError(
            "serial BSP options require --serial-bsp-phy-provider"
        )
    if ctx.serial_bsp_requested and ctx.serial_bsp_runtime_sync_provider is None:
        raise EmuFlowError(
            "serial BSP continuation requires --serial-bsp-runtime-sync-provider"
        )
    if ctx.serial_bsp_requested and (
        (ctx.serial_bsp_vivado is None) == (ctx.serial_bsp_yosys is None)
    ):
        raise EmuFlowError(
            "serial BSP continuation requires exactly one of "
            "--serial-bsp-vivado or --serial-bsp-yosys"
        )

    # These small board contracts can be checked without a synthesized graph.
    # Reject a renamed stress platform paired with the original timing/cost
    # databases before spending hours on synthesis and timing extraction.
    ctx.checked_link_timing = None
    ctx.checked_link_timing_validation = None
    if ctx.board_link_timing_db is not None or ctx.transport_cost_db is not None:
        board_contract_platform = Platform.load(ctx.platform_path)
        if ctx.board_link_timing_db is not None:
            ctx.checked_link_timing = read_json(ctx.board_link_timing_db.resolve())
            ctx.checked_link_timing_validation = validate_board_link_timing(
                ctx.checked_link_timing, board_contract_platform
            )
        if ctx.transport_cost_db is not None:
            validate_transport_cost_database(
                read_json(ctx.transport_cost_db.resolve()),
                expected_platform=board_contract_platform.name,
            )

    # Phase 7 runs after every board-independent stage, so validating its
    # interpreter inside the physical-flow helper is much too late for a
    # one-shot Phase 1--7 invocation.  Reject an unusable OpenPARF/PyTorch
    # binding before creating any frontend artifact or doing synthesis.
    physical_uses_openparf = ctx.physical and ctx.physical_backend == "rapidwright"
    if physical_uses_openparf:
        from .native_tools import require_native_schema
        require_native_schema("rwroute-export-request-v2")
        validate_openparf_runtime(
            install_root=ctx.physical_openparf_install,
            python_executable=ctx.physical_openparf_python,
        )
    if ctx.physical and ctx.physical_backend == "vivado":
        from .native_tools import require_native_schema
        require_native_schema("vivado-lineage-store-v2")
        for schema in ("boundary-timing-store-v3", "logic-segment-timing-store-v3", "local-path-timing-store-v3"):
            require_native_schema(schema)
    if ctx.internal_timing_database:
        ctx.opensta = resolve_native_executable("sta", ctx.opensta)
        check_opensta_engine_version(ctx.opensta)
    if ctx.physical:
        ctx.global_sta_executable = resolve_native_executable("sta", ctx.global_sta_executable)
        if not Path(ctx.global_sta_executable).is_file() or not os.access(ctx.global_sta_executable, os.X_OK):
            raise EmuFlowError("global OpenSTA executable is unavailable or not executable")
        if ctx.global_sta_executable != ctx.opensta:
            check_opensta_engine_version(ctx.global_sta_executable)

    ctx.output_dir = ctx.output_dir.resolve()
    if ctx.output_dir.exists():
        if not ctx.output_dir.is_dir() or any(ctx.output_dir.iterdir()):
            raise EmuFlowError(
                "multi-FPGA output path must be an empty directory: "
                f"{ctx.output_dir}"
            )
    ctx.openroad = _resolve_partition_openroad(
        ctx.partition_provider, ctx.openroad, ctx.tritonpart_solution
    )
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

def _compile_frontend(ctx: _CompileContext):
    source_list = [path.resolve() for path in ctx.sources]
    include_dir_list = [path.resolve() for path in ctx.include_dirs]
    define_list = list(ctx.defines)
    frontend_root = ctx.output_dir / "frontend"
    frontend_root.mkdir(parents=True, exist_ok=True)
    ctx.phase1_root = frontend_root / "phase1"
    synthesized_json = frontend_root / (
        "synthesized.json.gz"
        if (
            ctx.mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE
            and (ctx.yosys_json is None or ctx.yosys_json.suffix == ".gz")
        )
        else "synthesized.json"
    )
    synthesis_mode: str
    indexed_staging_transform = None
    ctx.frontend_report: Optional[Dict[str, Any]] = None
    synthesized_json_sha256: Optional[str] = None
    if ctx.prepared_frontend is not None or ctx.prepared_timing is not None:
        if ctx.prepared_frontend is None or ctx.prepared_timing is None or source_list or ctx.yosys_json is not None:
            raise EmuFlowError("prepared continuation requires both Phase 1 and Phase 2 boundaries and no synthesis inputs")
        from .prepared_inputs import bind_prepared_inputs
        ctx.frontend_report, ctx.timing_report = bind_prepared_inputs(
            ctx.prepared_frontend, ctx.prepared_timing, ctx.phase1_root,
            ctx.output_dir / "timing", ctx.platform_path, ctx.clock_periods, ctx.mapping_profile,
        )
    else:
        if ctx.yosys_json is not None:
            if source_list:
                raise EmuFlowError(
                    "provide RTL sources or --yosys-json, not both"
                )
            if include_dir_list or define_list:
                raise EmuFlowError(
                    "Verilog include directories and defines apply only when "
                    "compiling RTL sources"
                )
            source_json = ctx.yosys_json.resolve()
            if not source_json.is_file():
                raise EmuFlowError(f"Yosys JSON does not exist: {source_json}")
            shutil.copyfile(source_json, synthesized_json)
            synthesis_mode = "provided-yosys-json"
            if ctx.mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
                synthesis_report = {
                    "status": "pass",
                    "provider": "provided-yosys-json",
                    "mapping_profile": ctx.mapping_profile,
                    "primitive_audit": audit_xilinx_mapped_json(
                        synthesized_json, top=ctx.top
                    ),
                }
        else:
            if not source_list:
                raise EmuFlowError(
                    "multi-FPGA compilation requires RTL sources or --yosys-json"
                )
            if ctx.top is None:
                raise EmuFlowError("--top is required when compiling RTL sources")
            if ctx.mapping_profile == XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
                indexed_staging_transform = normalize_xilinx_indexed_staging
                synthesis_report, ctx.frontend_report = (
                    run_xilinx_ultrascaleplus_yosys_stream(
                        source_list,
                        ctx.top,
                        lambda stream: run_phase1(
                            Path("<yosys-stdout>"),
                            ctx.platform_path,
                            ctx.phase1_root,
                            top=ctx.top,
                            clocks=ctx.clocks,
                            indexed_staging_transform=(
                                normalize_xilinx_indexed_staging
                            ),
                            yosys_stream=stream,
                            yosys_hierarchical_stream=True,
                        ),
                        executable=ctx.yosys,
                        log_path=frontend_root / "yosys.log",
                        include_dirs=include_dir_list,
                        defines=define_list,
                        mapping_strategy=(
                            ctx.xilinx_mapping_strategy or DEFAULT_XILINX_MAPPING_STRATEGY
                        ),
                    )
                )
                normalization_report = None
                synthesis_mode = "xilinx-ultrascaleplus-open"
            else:
                raise ValidationError("frontend mapping profile is invalid")

        # Explicit user-supplied JSON is imported once and its staging copy is
        # removed. RTL synthesis above streams directly into indexed Phase 1.
        if ctx.frontend_report is None:
            synthesized_json_sha256 = _sha256(synthesized_json)
            try:
                ctx.frontend_report = run_phase1(
                    synthesized_json,
                    ctx.platform_path,
                    ctx.phase1_root,
                    top=ctx.top,
                    clocks=ctx.clocks,
                    indexed_staging_transform=indexed_staging_transform,
                )
            finally:
                synthesized_json.unlink(missing_ok=True)
        if ctx.frontend_report["status"] != "pass":
            raise EmuFlowError(
                "multi-FPGA frontend failed capacity or clock-topology checks"
            )
        if indexed_staging_transform is not None:
            indexed_normalization = ctx.frontend_report["indexed_staging_transform"]
            synthesis_report = {
                **synthesis_report,
                "normalization": indexed_normalization,
                "primitive_audit": indexed_normalization["primitive_audit"],
            }
        ctx.frontend_report = {
            **ctx.frontend_report,
            "synthesis": {
                "provider": "yosys",
                "mode": synthesis_mode,
                "mapping_profile": (
                    "provided-yosys-json"
                    if synthesis_mode == "provided-yosys-json"
                    else ctx.mapping_profile
                ),
                "sources": [str(path) for path in source_list],
                "include_dirs": [str(path) for path in include_dir_list],
                "defines": define_list,
                **(
                    {"yosys_json_sha256": synthesized_json_sha256}
                    if synthesized_json_sha256 is not None
                    else {}
                ),
                "interchange_retention": (
                    "never-materialized-stream"
                    if synthesis_mode == "xilinx-ultrascaleplus-open"
                    else "deleted-after-phase1-import"
                ),
                **(
                    {"tool_report": synthesis_report}
                    if ctx.yosys_json is None
                    else {}
                ),
                **(
                    {"normalization": normalization_report}
                    if ctx.yosys_json is None
                    and normalization_report is not None
                    else {}
                ),
            },
        }
    ctx.ir_path = ctx.phase1_root / "design.emuir.sqlite"

def _compile_timing(ctx: _CompileContext):
    ctx.timing_root = ctx.output_dir / "timing"
    ctx.path_database_path = ctx.timing_root / "path-database.json"
    ctx.cut_segment_qualification_path = None
    ctx.net_weights_path = ctx.timing_root / "partition-net-weights.json"
    if ctx.prepared_timing is None:
        ctx.timing_report = None
    if ctx.internal_timing_database and ctx.prepared_timing is None:
        ctx.timing_root.mkdir(parents=True, exist_ok=True)
        sta_report = run_opensta_path_database(
            ir_path=ctx.ir_path,
            output_path=ctx.path_database_path,
            clocks=ctx.clock_periods,
            timing_io=ctx.timing_io,
            timing_model_path=ctx.timing_model,
            architecture_timing_db_path=ctx.architecture_timing_db,
            executable=ctx.opensta,
            max_paths=ctx.sta_max_paths,
            log_path=ctx.timing_root / "opensta.log",
            _stream_output=True,
        )
        timing_mode = "opensta-preplacement"
        weights_report = None
        if ctx.timing_driven:
            weights_report = derive_partition_net_weights(
                ctx.path_database_path,
                ctx.ir_path,
                ctx.net_weights_path,
                criticality_scale=ctx.timing_criticality_scale,
                criticality_exponent=ctx.timing_criticality_exponent,
                input_already_validated=True,
            )
        ctx.timing_report = {
            "status": "pass",
            "mode": timing_mode,
            "backend": "opensta",
            "sta": sta_report,
            "optimization_enabled": ctx.timing_driven,
            **(
                {"partition_weights": weights_report}
                if weights_report is not None
                else {}
            ),
        }

    if ctx.physical:
        # This is the compact selector already owned by Phase2/admission, not
        # the graph-sized path payload. Reject historical capped selections
        # as well as an explicit cap on a fresh physical compilation.
        if ctx.path_database_path.stat().st_size > 1024*1024:
            raise ValidationError("physical execution requires a compact all-endpoint Phase2 selector")
        source=read_json(ctx.path_database_path).get('source',{})
        selection=source.get('path_selection',{})
        if source.get('model_pin_coverage')!='checked-source-emission-directions-v1':
            raise ValidationError('physical execution requires checked source-model pin coverage; regenerate unqualified prepared timing')
        if (not isinstance(selection,dict) or selection.get('policy')!='worst-per-endpoint-per-path-group-v1' or
                'max_paths_per_group' not in selection or selection['max_paths_per_group'] is not None or
                selection.get('endpoint_path_count')!=1):
            raise ValidationError("physical execution requires all-endpoint Phase2 timing; regenerate capped or unqualified prepared timing")

    ctx.path_database_payload = (
        sta_path_database_payload_path(ctx.path_database_path)
        if ctx.internal_timing_database
        else None
    )
    ctx.net_weights_payload = (
        partition_net_weights_payload_path(ctx.net_weights_path)
        if ctx.net_weights_path.is_file()
        else None
    )

    ctx.effective_route_constraints = ctx.route_constraints
    ctx.link_timing_report = None
    ctx.copied_link_timing_path = None
    ctx.effective_route_constraints_path = None
    if ctx.board_link_timing_db is not None:
        platform = Platform.load(ctx.platform_path)
        database = ctx.checked_link_timing
        validation = ctx.checked_link_timing_validation
        link_delays, projection = directed_route_link_delays(
            database, platform
        )
        ctx.timing_root.mkdir(parents=True, exist_ok=True)
        ctx.copied_link_timing_path = ctx.timing_root / "board-link-timing.json"
        write_json(ctx.copied_link_timing_path, database)
        raw_constraints = (
            read_json(ctx.route_constraints.resolve())
            if ctx.route_constraints is not None
            else {"schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA}
        )
        if not isinstance(raw_constraints, dict):
            raise ValidationError("route constraints must be an object")
        raw_constraints = dict(raw_constraints)
        raw_constraints["directed_link_delay_ns"] = link_delays
        ctx.effective_route_constraints_path = (
            ctx.timing_root / "board-link-route-constraints.json"
        )
        write_json(ctx.effective_route_constraints_path, raw_constraints)
        ctx.effective_route_constraints = ctx.effective_route_constraints_path
        ctx.link_timing_report = {
            "status": "pass",
            "validation": validation,
            "routing_projection": projection,
            "optimization_enabled": ctx.timing_driven,
            "applied_to": (
                [
                    "phase4-system-routing",
                    "phase5-tdm-ratio-and-schedule-timing",
                    "phase7c-system-timing-when-physical",
                ]
                if ctx.timing_driven
                else [
                    "phase4-post-route-timing-evaluation",
                    "phase5-baseline-schedule-timing-evaluation",
                    "phase7c-system-timing-when-physical",
                ]
            ),
        }

    if ctx.timing_report is not None:
        provider_consumes_weights = ctx.timing_driven
        hop_refiner_consumes_weights = False
        if ctx.effective_route_constraints is not None:
            raw_route_constraints = read_json(
                ctx.effective_route_constraints.resolve()
            )
            if not isinstance(raw_route_constraints, dict):
                raise ValidationError("route constraints must be an object")
            hop_refiner_consumes_weights = ctx.timing_driven and (
                raw_route_constraints.get("max_route_hops") is not None
            )
        consumers = []
        if provider_consumes_weights:
            consumers.append("phase3-provider")
        if hop_refiner_consumes_weights:
            consumers.append("phase3-hop-refinement")
        ctx.timing_report.update(
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

def _compile_partition(ctx: _CompileContext):
    ctx.phase3_root = ctx.output_dir / "partition"
    ctx.phase3_report = ctx.measured("phase3", run_phase3,
        ctx.ir_path,
        ctx.platform_path,
        ctx.phase3_root,
        constraints_path=ctx.partition_constraints,
        seed=ctx.seed,
        min_used_fpgas=ctx.min_used_fpgas,
        balance_tolerance=ctx.balance_tolerance,
        provider=ctx.partition_provider,
        openroad=ctx.openroad,
        tritonpart_solution=ctx.tritonpart_solution,
        tritonpart_timeout_seconds=ctx.partition_timeout_seconds,
        tritonpart_seed_attempts=ctx.partition_seed_attempts,
        tritonpart_num_initial_solutions=ctx.partition_num_initial_solutions,
        tritonpart_num_best_initial_solutions=(
            ctx.partition_num_best_initial_solutions
        ),
        tritonpart_repair_min_used_fpgas=(
            ctx.partition_repair_min_used_fpgas
        ),
        net_weights_path=(
            ctx.net_weights_path
            if ctx.timing_driven
            else None
        ),
        route_constraints_path=ctx.effective_route_constraints,
        cut_mode=ctx.cut_mode,
        max_cross_fpga_dependency_depth=ctx.max_cross_fpga_dependency_depth,
        timing_database_path=(
            ctx.path_database_path if ctx.partition_provider == "patron" else None
        ),
        patron_refiner=ctx.patron_refiner,
        patron_max_moves=ctx.patron_max_moves,
        patron_algorithm_version=ctx.patron_algorithm_version,
        static_exact_candidate_policy=ctx.static_exact_candidate_policy,
        retain_patron_baseline=False,
    )
    ctx.assignment_path = ctx.phase3_root / "assignment.json"
    phase3_validation = ctx.phase3_report.get("validation")
    inter_fpga_cut_count = (
        phase3_validation.get("cut_nets")
        if isinstance(phase3_validation, dict)
        else None
    )
    if (
        isinstance(inter_fpga_cut_count, bool)
        or not isinstance(inter_fpga_cut_count, int)
        or inter_fpga_cut_count < 0
    ):
        raise ValidationError(
            "Phase 3 report has an invalid inter-FPGA cut-net count"
        )
    ctx.has_inter_fpga_cuts = inter_fpga_cut_count > 0
    ctx.effective_optimize_frame_slots = (
        ctx.optimize_frame_slots if ctx.has_inter_fpga_cuts else False
    )

def _compile_route_policy(ctx: _CompileContext):
    ctx.projected_timing_paths = ctx.timing_paths
    if ctx.internal_timing_database:
        ctx.cut_segment_qualification_path = (
            ctx.timing_root / "cut-segment-qualification.json"
        )
        cut_qualification = build_cut_segment_qualification(
            ctx.ir_path,
            ctx.assignment_path,
            ctx.path_database_path,
            timing_model_path=ctx.timing_model,
            architecture_timing_db_path=ctx.architecture_timing_db,
            input_already_validated=True,
        )
        write_json(ctx.cut_segment_qualification_path, cut_qualification)
        ctx.timing_report["cut_segment_qualification"] = cut_qualification
        if ctx.has_inter_fpga_cuts:
            ctx.projected_timing_paths = ctx.timing_root / "cut-timing-paths.json"
            # Phase 4/5 must optimize the same complete original TimingPathDB
            # population that Phase 7C later reports.  The post-partition
            # structural cut-segment qualification is intentionally separate
            # from this original-path population.
            projection_report = project_sta_path_database(
                ctx.path_database_path,
                ctx.assignment_path,
                ctx.projected_timing_paths,
                input_already_validated=True,
            )
        else:
            # A legal single-FPGA partition has no interconnect timing problem.
            # Do not fabricate an empty STA path record: the Phase 4/5 timing
            # schema intentionally represents only paths that cross at least
            # one partition cut.  The original TimingPathDB remains the input
            # to the physical backend and authoritative global OpenSTA signoff.
            ctx.projected_timing_paths = None
            projection_report = {
                "status": "pass",
                "design": ctx.phase3_report.get("design"),
                "mode": "not-required-no-inter-fpga-cuts",
                "projected_paths": 0,
                "compressed_paths": 0,
                "cut_nets": 0,
                "covered_cut_nets": 0,
                "uncovered_cut_nets": 0,
            }
        ctx.timing_report["cut_path_projection"] = projection_report

    ctx.effective_route_provider = ctx.route_provider or GLOBAL_CANDIDATE_PROVIDER
    ctx.effective_tdm_provider = TDM_EXACT_DEPENDENCY_PROVIDER
    ctx.phase4_root = ctx.output_dir / "system-route"
    ctx.phase5_root = ctx.output_dir / "tdm"
    ctx.frame_search_report = None

def _compile_routing_schedule(ctx: _CompileContext):
    if ctx.effective_optimize_frame_slots:
        if ctx.frame_slots is None:
            raise EmuFlowError(
                "--optimize-frame-slots requires --frame-slots as its "
                "feasible upper bound"
            )
        ctx.frame_search_report = run_frame_length_search(
            ctx.assignment_path,
            ctx.platform_path,
            ctx.projected_timing_paths,
            ctx.output_dir / "frame-search",
            ctx.phase4_root,
            ctx.phase5_root,
            max_frame_slots=ctx.frame_slots,
            route_constraints=ctx.effective_route_constraints,
            route_max_iterations=ctx.route_max_iterations,
            router=ctx.router,
            route_provider=ctx.route_provider,
            candidate_workers=ctx.route_candidate_workers,
            tdm_provider=ctx.tdm_provider,

        )
        ctx.phase4_report = read_json(ctx.phase4_root / "phase4_report.json")
        ctx.phase5_report = read_json(ctx.phase5_root / "phase5_report.json")
    else:
        _compile_route(ctx)
        _compile_schedule(ctx)

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
        vivado_dut_frequency_mhz=ctx.physical_vivado_dut_frequency_mhz,
        dut_timing_io=getattr(ctx,'timing_io',None) if ctx.physical_backend=='vivado' else None,
        rapidwright_jar=ctx.physical_rapidwright_jar,
        rapidwright_java=ctx.physical_rapidwright_java,
        rapidwright_classes=ctx.physical_rapidwright_classes,
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
        ctx.baseline_runtime_report = ctx.measured("phase7c", run_phase7c,
            ctx.schedule_path,
            ctx.platform_path,
            ctx.phase3_root / "phase3_report.json",
            ctx.phase4_root / "phase4_report.json",
            ctx.phase5_root / "phase5_report.json",
            baseline_split / "phase6_report.json",
            baseline_root / "runtime",
            assignment_path=ctx.assignment_path,
            physical_summary_path=(
                baseline_root / "physical/physical-summary.json"
            ),
            routes_path=ctx.routes_path,
            board_link_timing_path=ctx.copied_link_timing_path,
            global_sta_executable=ctx.global_sta_executable,
            global_timing_engine=ctx.global_timing_engine,
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
    else:
        ctx.phase6_report = ctx.measured("phase6", run_phase6,
            ctx.ir_path,
            ctx.assignment_path,
            ctx.schedule_path,
            ctx.platform_path,
            ctx.phase6_root,
            equivalence_cycles=ctx.equivalence_cycles,
            equivalence_seed=ctx.equivalence_seed,
            transport_cost_path=ctx.transport_cost_db,
            clock_periods=ctx.clock_periods,
        )

def _compile_physical(ctx: _CompileContext):
    ctx.physical_report = None
    ctx.physical_summary_path = None
    if ctx.physical:
        physical_started = time.monotonic()
        ctx.physical_report = _run_selected_physical(ctx, ctx.phase6_root, ctx.schedule_path, ctx.output_dir / "physical")
        physical_seconds = time.monotonic() - physical_started
        ctx.physical_summary_path = ctx.output_dir / "physical/physical-summary.json"
        if ctx.phase6_comparison is not None and ctx.baseline_physical_report is not None:
            baseline_metrics = _phase6_physical_metrics(
                ctx.baseline_physical_report
            )
            chimew_metrics = _phase6_physical_metrics(ctx.physical_report)
            pin_metrics = ctx.chimew_pin_metrics
            ctx.phase6_comparison.update(
                {
                    "status": "pass",
                    "chimew": {
                        "phase6": ctx.phase6_report,
                        "physical": ctx.physical_report,
                        "phase6_runtime_seconds": ctx.phase6_comparison[
                            "chimew_phase6_runtime_seconds"
                        ],
                        "physical_runtime_seconds": physical_seconds,
                    },
                    "baseline_physical": baseline_metrics,
                    "chimew_physical": chimew_metrics,
                    "physical_delta": {
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

def _compile_signoff(ctx: _CompileContext):
    ctx.runtime_root = ctx.output_dir / "runtime"
    ctx.runtime_report = ctx.measured("phase7c", run_phase7c,
        ctx.schedule_path,
        ctx.platform_path,
        ctx.phase3_root / "phase3_report.json",
        ctx.phase4_root / "phase4_report.json",
        ctx.phase5_root / "phase5_report.json",
        ctx.phase6_root / "phase6_report.json",
        ctx.runtime_root,
        assignment_path=ctx.assignment_path,
        global_sta_executable=ctx.global_sta_executable,
        global_timing_engine=ctx.global_timing_engine,
        physical_summary_path=ctx.physical_summary_path,
        routes_path=ctx.routes_path if ctx.physical_summary_path is not None else None,
        board_link_timing_path=(
            ctx.copied_link_timing_path
            if ctx.physical_summary_path is not None
            else None
        ),
    )
    if ctx.physical and ctx.runtime_report.get("status") != "pass":
        raise ValidationError(
            "full physical flow did not close unified Phase 7C system timing"
        )
    if ctx.phase6_comparison is not None:
        if ctx.baseline_runtime_report is None:
            raise ValidationError("Phase 6 A/B baseline runtime is missing")
        ctx.phase6_comparison["chimew"]["runtime"] = ctx.runtime_report
        baseline_system = _phase6_system_timing_metrics(
            ctx.baseline_runtime_report
        )
        chimew_system = _phase6_system_timing_metrics(ctx.runtime_report)
        ctx.phase6_comparison["baseline_system_timing"] = baseline_system
        ctx.phase6_comparison["chimew_system_timing"] = chimew_system
        ctx.phase6_comparison["system_timing_delta"] = {
            key: chimew_system[key] - baseline_system[key]
            for key in baseline_system
        }
        ctx.phase6_comparison["validation"] = validate_phase6_ab_comparison(
            ctx.phase6_comparison
        )
        write_json(
            ctx.output_dir / "phase6-comparison/comparison-report.json",
            ctx.phase6_comparison,
        )

def _compile_finalize(ctx: _CompileContext):
    resolved_schedule_store = tdm_schedule_store_path(ctx.schedule_path)
    if resolved_schedule_store is None:
        raise ValidationError("Phase 5 schedule has no indexed store")
    resolved_route_store = system_route_store_path(ctx.routes_path)
    if resolved_route_store is None:
        raise ValidationError("Phase 4 routes have no indexed store")

    ctx.report = {
        "schema": MULTI_FPGA_FLOW_SCHEMA,
        "status": "pass",
        "provider": MULTI_FPGA_FLOW_PROVIDER,
        "architecture_policy": "provider-neutral",
        **(
            {"timing": _timing_report_summary(ctx.timing_report)}
            if ctx.timing_report is not None
            else {}
        ),
        **(
            {"board_link_timing": ctx.link_timing_report}
            if ctx.link_timing_report is not None
            else {}
        ),
        **(
            {"frame_search": ctx.frame_search_report}
            if ctx.frame_search_report is not None
            else {}
        ),
        "runtime": ctx.runtime_report,
        **(
            {"physical": _physical_report_summary(ctx.physical_report)}
            if ctx.physical_report is not None
            else {}
        ),
        **(
            {"phase6_comparison": ctx.phase6_comparison}
            if ctx.phase6_comparison is not None
            else {}
        ),
        "stages": {
            "frontend": _stage_summary("frontend", ctx.frontend_report),
            "partition": _stage_summary("partition", ctx.phase3_report),
            "system_route": _stage_summary("system_route", ctx.phase4_report),
            "tdm": _stage_summary("tdm", ctx.phase5_report),
            "split": _stage_summary("split", ctx.phase6_report),
        },
        "artifacts": {
            "platform": {
                "path": "frontend/phase1/platform.normalized.json",
                "sha256": _sha256(
                    ctx.phase1_root / "platform.normalized.json"
                ),
            },
            "emuir": {
                "path": "frontend/phase1/design.emuir.sqlite",
                "sha256": _sha256(ctx.ir_path),
            },
            "partition_constraints": {
                "path": "partition/constraints.normalized.json",
                "sha256": _sha256(
                    ctx.phase3_root / "constraints.normalized.json"
                ),
            },
            "route_constraints": {
                "path": "system-route/route_constraints.normalized.json",
                "sha256": _sha256(
                    ctx.phase4_root / "route_constraints.normalized.json"
                ),
            },
            "assignment": {
                "path": "partition/assignment.json",
                "sha256": _sha256(ctx.assignment_path),
            },
            "routes": {
                "path": "system-route/routes.json",
                "sha256": _sha256(ctx.routes_path),
            },
            "route_store": {
                "path": (
                    Path("system-route") / resolved_route_store.name
                ).as_posix(),
                "sha256": _sha256(resolved_route_store),
            },
            "schedule": {
                "path": "tdm/schedule.json",
                "sha256": _sha256(ctx.schedule_path),
            },
            "schedule_store": {
                "path": (
                    Path("tdm") / resolved_schedule_store.name
                ).as_posix(),
                "sha256": _sha256(resolved_schedule_store),
            },
            "split_manifest": {
                "path": "split/manifest.json",
                "sha256": _sha256(ctx.phase6_root / "manifest.json"),
            },
            **(
                {
                    "transport_cost": {
                        "path": "split/transport-cost.json",
                        "sha256": _sha256(
                            ctx.phase6_root / "transport-cost.json"
                        ),
                    }
                }
                if ctx.transport_cost_db is not None
                else {}
            ),
            "runtime_contract": {
                "path": "runtime/runtime_contract.json",
                "sha256": _sha256(ctx.runtime_root / "runtime_contract.json"),
            },
            "qor_report": {
                "path": "runtime/qor_report.json",
                "sha256": _sha256(ctx.runtime_root / "qor_report.json"),
            },
            **_runtime_timing_artifacts(ctx.runtime_root, ctx.output_dir, ctx.runtime_report),
            **(
                {
                    "physical_flow_report": {
                        "path": "physical/multi-fpga-physical-flow-report.json",
                        "sha256": _sha256(
                            ctx.output_dir
                            / "physical/multi-fpga-physical-flow-report.json"
                        ),
                    },
                    "physical_summary": {
                        "path": "physical/physical-summary.json",
                        "sha256": _sha256(
                            ctx.output_dir / "physical/physical-summary.json"
                        ),
                    },
                }
                if ctx.physical_report is not None
                else {}
            ),
            **(
                {
                    "phase6_comparison_report": {
                        "path": "phase6-comparison/comparison-report.json",
                        "sha256": _sha256(
                            ctx.output_dir
                            / "phase6-comparison/comparison-report.json"
                        ),
                    }
                }
                if ctx.phase6_comparison is not None
                else {}
            ),
            **(
                {
                    "board_link_timing": {
                        "path": "timing/board-link-timing.json",
                        "sha256": _sha256(ctx.copied_link_timing_path),
                    },
                    "board_link_route_constraints": {
                        "path": "timing/board-link-route-constraints.json",
                        "sha256": _sha256(
                            ctx.effective_route_constraints_path
                        ),
                    },
                }
                if ctx.copied_link_timing_path is not None
                and ctx.effective_route_constraints_path is not None
                else {}
            ),
            **(
                {
                    "frame_search_report": {
                        "path": "frame-search/frame-search-report.json",
                        "sha256": _sha256(
                            ctx.output_dir
                            / "frame-search/frame-search-report.json"
                        ),
                    }
                }
                if ctx.frame_search_report is not None
                else {}
            ),
            **(
                {
                    "timing_path_database": {
                        "path": "timing/path-database.json",
                        "sha256": _sha256(ctx.path_database_path),
                    },
                    **(
                        {
                            "timing_path_database_payload": {
                                "path": str(
                                    Path("timing")
                                    / ctx.path_database_payload.name
                                ),
                                "sha256": _sha256(ctx.path_database_payload),
                            }
                        }
                        if ctx.path_database_payload is not None
                        else {}
                    ),
                    **(
                        {
                            "cut_segment_qualification": {
                                "path": "timing/cut-segment-qualification.json",
                                "sha256": _sha256(ctx.cut_segment_qualification_path),
                            }
                        }
                        if ctx.cut_segment_qualification_path is not None
                        else {}
                    ),
                    **(
                        {
                            "partition_net_weights": {
                                "path": "timing/partition-net-weights.json",
                                "sha256": _sha256(ctx.net_weights_path),
                            }
                        }
                        if ctx.net_weights_path.is_file()
                        else {}
                    ),
                    **(
                        {
                            "partition_net_weights_payload": {
                                "path": str(
                                    Path("timing")
                                    / ctx.net_weights_payload.name
                                ),
                                "sha256": _sha256(ctx.net_weights_payload),
                            }
                        }
                        if ctx.net_weights_payload is not None
                        else {}
                    ),
                    **(
                        {
                            "cut_timing_paths": {
                                "path": "timing/cut-timing-paths.json",
                                "sha256": _sha256(ctx.projected_timing_paths),
                            }
                        }
                        if ctx.projected_timing_paths is not None
                        else {}
                    ),
                }
                if ctx.internal_timing_database
                else {}
            ),
        },
    }
    ctx.report["summary"] = validate_multi_fpga_flow_report(ctx.report)
    if ctx.serial_bsp_requested:
        write_json(ctx.output_dir / "board-independent-flow-report.json", ctx.report)
        assert ctx.serial_bsp_phy_provider is not None
        assert ctx.serial_bsp_runtime_sync_provider is not None
        bsp_report = run_multi_fpga_bsp_flow(
            flow_root=ctx.output_dir,
            platform_path=ctx.platform_path,
            phy_provider_path=ctx.serial_bsp_phy_provider,
            runtime_sync_provider_path=ctx.serial_bsp_runtime_sync_provider,
            output_dir=ctx.output_dir / "hardware-bsp",
            board_overlay_path=ctx.serial_bsp_board_overlay,
            gt_site_map_path=ctx.serial_bsp_gt_site_map,
            vivado_executable=ctx.serial_bsp_vivado,
            yosys_executable=ctx.serial_bsp_yosys,
            runtime_sync_root=ctx.serial_bsp_runtime_sync_root,
            ready_stable_cycles=ctx.serial_bsp_ready_stable_cycles,
        )
        ctx.report["hardware_bsp"] = bsp_report
        ctx.report["artifacts"]["board_independent_flow_report"] = {
            "path": "board-independent-flow-report.json",
            "sha256": _sha256(ctx.output_dir / "board-independent-flow-report.json"),
        }
        ctx.report["artifacts"]["hardware_bsp_flow_report"] = {
            "path": "hardware-bsp/multi-fpga-bsp-flow-report.json",
            "sha256": _sha256(
                ctx.output_dir / "hardware-bsp/multi-fpga-bsp-flow-report.json"
            ),
        }
        ctx.report["summary"] = validate_multi_fpga_flow_report(ctx.report)
    write_json(ctx.output_dir / "multi-fpga-flow-report.json", ctx.report)
    return ctx.report

def _compile_route(ctx: _CompileContext):
    ctx.phase4_report = ctx.measured("phase4", run_phase4,
        ctx.assignment_path,
        ctx.platform_path,
        ctx.phase4_root,
        constraints_path=ctx.effective_route_constraints,
        frame_slots=ctx.frame_slots,
        max_iterations=ctx.route_max_iterations,
        timing_paths_path=ctx.projected_timing_paths,
        router=ctx.router,
        provider=ctx.effective_route_provider,
        timing_driven=ctx.timing_driven,
        candidate_workers=(
            ctx.route_candidate_workers if ctx.has_inter_fpga_cuts else 1
        ),
    )

def _compile_schedule(ctx: _CompileContext):
    ctx.phase5_report = ctx.measured("phase5", run_phase5,
        ctx.phase4_root / "routes.json",
        ctx.platform_path,
        ctx.phase5_root,
        assignment_path=ctx.assignment_path,

        provider=ctx.effective_tdm_provider,

    )

def run_multi_fpga_flow(
    platform_path: Path = DEFAULT_PLATFORM_PATH,
    output_dir: Optional[Path] = None,
    *,
    sources: Iterable[Path] = (),
    include_dirs: Iterable[Path] = (),
    defines: Iterable[str] = (),
    top: Optional[str] = None,
    clocks: Iterable[str] = (),
    yosys_json: Optional[Path] = None,
    prepared_frontend: Optional[Path] = None,
    prepared_timing: Optional[Path] = None,
    yosys: Optional[str] = None,
    mapping_profile: str = XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    xilinx_mapping_strategy: Optional[str] = None,
    partition_constraints: Optional[Path] = None,
    partition_provider: str = "patron",
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    openroad: Optional[str] = None,
    tritonpart_solution: Optional[Path] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_algorithm_version: int = DEFAULT_PATRON_ALGORITHM_VERSION,
    partition_timeout_seconds: int = 3600,
    partition_seed_attempts: int = 1,
    partition_num_initial_solutions: int = 50,
    partition_num_best_initial_solutions: int = 10,
    partition_repair_min_used_fpgas: bool = True,
    cut_mode: str = DEFAULT_PARTITION_CUT_MODE,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    timing_driven: bool = True,
    clock_periods: Optional[Dict[str, float]] = None,
    timing_io: Optional[Mapping[str, Any]] = None,
    timing_model: Path = DEFAULT_TIMING_MODEL,
    architecture_timing_db: Optional[Path] = None,
    opensta: Optional[str] = None,
    sta_max_paths: Optional[int] = None,
    timing_criticality_scale: float = 9.0,
    timing_criticality_exponent: float = 2.0,
    route_constraints: Optional[Path] = None,
    board_link_timing_db: Optional[Path] = None,
    transport_cost_db: Optional[Path] = None,
    timing_paths: Optional[Path] = None,
    router: Optional[str] = None,
    route_provider: Optional[str] = None,
    route_candidate_workers: int = 1,
    frame_slots: Optional[int] = None,
    optimize_frame_slots: bool = False,
    route_max_iterations: Optional[int] = None,
    tdm_provider: Optional[str] = None,

    simulation_frames: int = 16,
    equivalence_cycles: int = 1,
    equivalence_seed: int = 20260727,
    phase6_provider: str = "baseline",
    phase6_chimew_region_count: int = 4,
    phase6_chimew_grouper: Optional[str] = None,
    phase6_chimew_refiner: Optional[str] = None,
    phase6_chimew_rudy: Optional[str] = None,
    phase6_chimew_assigner: Optional[str] = None,
    physical: bool = False,
    physical_backend: str = "rapidwright",
    physical_architecture: Optional[Path] = None,
    physical_openparf_install: Optional[Path] = None,
    physical_openparf_python: Optional[Path] = None,
    physical_seed: int = 1,
    physical_vivado: Optional[str] = None,
    physical_vivado_place_directive: str = "Default",
    physical_vivado_route_directive: str = "Default",
    physical_vivado_dut_frequency_mhz: Optional[float] = None,
    physical_rapidwright_jar: Optional[Path] = None,
    physical_rapidwright_java: Optional[Path] = None,
    physical_rapidwright_classes: Optional[Path] = None,
    physical_rapidwright_java_source: Optional[Path] = None,
    physical_rapidwright_device_data: Optional[Path] = None,
    physical_rapidwright_timing_data: Optional[Path] = None,
    physical_rapidwright_opensta: Optional[str] = None,
    physical_rapidwright_native_constraints: Optional[Path] = None,
    physical_rapidwright_provider_manifest: Optional[Path] = None,
    physical_workers: int = 1,
    global_sta_executable: Optional[str] = None,
    global_timing_engine: str = "opensta",
    serial_bsp_phy_provider: Optional[Path] = None,
    serial_bsp_runtime_sync_provider: Optional[Path] = None,
    serial_bsp_board_overlay: Optional[Path] = None,
    serial_bsp_gt_site_map: Optional[Path] = None,
    serial_bsp_vivado: Optional[Path] = None,
    serial_bsp_yosys: Optional[Path] = None,
    serial_bsp_runtime_sync_root: Optional[str] = None,
    serial_bsp_ready_stable_cycles: int = 4,
) -> Dict[str, Any]:
    """Compile RTL/EmuIR using the shared phase adapters and current defaults."""
    ctx = _CompileContext(locals())
    _compile_preflight(ctx)
    _compile_frontend(ctx)
    _compile_timing(ctx)
    _compile_partition(ctx)
    _compile_route_policy(ctx)
    _compile_routing_schedule(ctx)
    _compile_split(ctx)
    _compile_physical(ctx)
    _compile_signoff(ctx)
    _compile_finalize(ctx)
    return ctx.report
