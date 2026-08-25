"""Independent end-to-end QoR comparison for Static Exact cut policies.

Unlike the Phase 6 provider comparison, these arms deliberately have different
Phase 3 assignments, Phase 4 routes, and Phase 5 schedules.  The comparison
therefore binds each terminal to its own complete Phase 1--7 chain while
requiring the original RTL/EmuIR, timing database, platform, constraints, and
physical seed to be common.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from .canonical_qor import (
    EXPERIMENT_PHASE7_SCHEMA,
    LEGACY_EXPERIMENT_PHASE7_SCHEMA,
    _clock_metrics,
    _digest,
    _number,
    _require,
    _sha256,
    _write_json_sha256,
)
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
)
from .errors import EmuFlowError, ValidationError
from .experiment_stages import validate_phase7_checkpoint
from .experiment_storage import validate_experiment_write_path
from .io import read_json, write_json
from .partition import CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT
from .runtime import QOR_REPORT_SCHEMA


STATIC_EXACT_QOR_COMPARISON_SCHEMA = "emuflow.static-exact-qor-comparison/v1"
STATIC_EXACT_ARM_LABELS = (
    "sequential-only",
    "legacy-static-exact-v1",
    "generalized-static-exact-v2",
)
_LABEL_CONTRACTS = {
    "sequential-only": (CUT_MODE_SEQUENTIAL_ONLY, None),
    "legacy-static-exact-v1": (
        CUT_MODE_STATIC_EXACT,
        STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    ),
    "generalized-static-exact-v2": (
        CUT_MODE_STATIC_EXACT,
        STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    ),
}
_TIMING_METRICS = (
    "global_target_clock_wns_ns",
    "global_target_clock_tns_ns",
    "global_runtime_clock_wns_ns",
    "global_runtime_clock_tns_ns",
    "per_fpga_wns_ns",
    "per_fpga_tns_ns",
)
_SCALAR_METRICS = (
    *_TIMING_METRICS,
    "nominal_virtual_frequency_mhz",
    "transport_cells",
    "physical_cells",
    "cut_nets",
    "scheduled_bit_hops",
    "frame_slots",
    "completion_slot",
    "physical_wall_seconds",
    "phase3_to_7_wall_seconds",
)


@dataclass(frozen=True)
class StaticExactArm:
    shared_root: Path
    lookahead_root: Path
    phase6_root: Path
    phase7_root: Path


def _checkpoint(root: Path, expected_stage: str) -> Dict[str, Any] | None:
    """Read execution metadata for a managed content-addressed output."""

    manifest_path = root.parent / "checkpoint.json"
    if not manifest_path.is_file():
        return None
    value = read_json(manifest_path)
    if (
        value.get("schema") != "emuflow.experiment-checkpoint/v2"
        or value.get("storage") != "managed"
        or value.get("output_immutable") is not True
        or value.get("execution_key") != root.parent.name
        or manifest_path.stat().st_mode & 0o222
        or value.get("stage") != expected_stage
        or Path(str(value.get("output_dir", ""))).resolve() != root.resolve()
        or value.get("status") != "pass"
    ):
        raise ValidationError(
            f"Static Exact QoR {expected_stage} checkpoint metadata is invalid"
        )
    elapsed = value.get("execution_elapsed_seconds")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or elapsed < 0
    ):
        raise ValidationError(
            f"Static Exact QoR {expected_stage} runtime evidence is missing"
        )
    return value


def _execution_runtime(
    shared_root: Path,
    lookahead_root: Path,
    phase6_root: Path,
    phase7_root: Path,
) -> Dict[str, float] | None:
    """Collect sealed wall times without consulting mutable farm state."""

    shared = _checkpoint(shared_root, "shared")
    lookahead = _checkpoint(lookahead_root, "physical-lookahead")
    phase6 = _checkpoint(phase6_root, "phase6")
    phase7 = _checkpoint(phase7_root, "phase7")
    checkpoints = (shared, lookahead, phase6, phase7)
    if all(value is None for value in checkpoints):
        # Unit fixtures and historical imported checkpoints can legitimately
        # predate sealed runtime metadata. New canonical executions cannot.
        return None
    if any(value is None for value in checkpoints):
        raise ValidationError("Static Exact QoR runtime checkpoint set is incomplete")
    assert shared is not None and lookahead is not None
    assert phase6 is not None and phase7 is not None
    cache_root = shared_root.parent.parent.parent
    dependencies = shared.get("dependency_keys")
    if not isinstance(dependencies, dict):
        raise ValidationError("Static Exact QoR shared dependency seal is invalid")
    records: Dict[str, float] = {}
    for label, stage in (
        ("partition", "partition"),
        ("route", "route"),
        ("tdm", "tdm"),
    ):
        key = dependencies.get(label)
        if not isinstance(key, str):
            raise ValidationError(
                f"Static Exact QoR {label} runtime dependency is missing"
            )
        output = cache_root / "objects" / key / "output"
        value = _checkpoint(output, stage)
        if value is None:
            raise ValidationError(
                f"Static Exact QoR {label} runtime checkpoint is missing"
            )
        elapsed = value.get("execution_elapsed_seconds")
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or elapsed < 0
        ):
            raise ValidationError(
                f"Static Exact QoR {label} runtime evidence is invalid"
            )
        records[f"{label}_wall_seconds"] = float(elapsed)
    records.update(
        {
            "phase6_wall_seconds": float(phase6["execution_elapsed_seconds"]),
            "physical_lookahead_wall_seconds": float(
                lookahead["execution_elapsed_seconds"]
            ),
            "phase7_wall_seconds": float(phase7["execution_elapsed_seconds"]),
        }
    )
    records["physical_wall_seconds"] = (
        records["physical_lookahead_wall_seconds"]
        + records["phase7_wall_seconds"]
    )
    records["phase3_to_7_wall_seconds"] = sum(
        records[name]
        for name in (
            "partition_wall_seconds",
            "route_wall_seconds",
            "tdm_wall_seconds",
            "phase6_wall_seconds",
            "physical_lookahead_wall_seconds",
            "phase7_wall_seconds",
        )
    )
    return records


def _directory(text: str, label: str) -> Path:
    supplied = Path(text).expanduser()
    if supplied.is_symlink() or not supplied.is_dir():
        raise ValidationError(f"Static Exact QoR {label} is not a directory")
    return supplied.resolve()


def parse_static_exact_qor_arms(
    records: Sequence[Sequence[str]],
) -> Dict[Tuple[str, int], StaticExactArm]:
    """Parse repeated ``LABEL SEED SHARED LOOKAHEAD PHASE6 PHASE7`` records."""

    arms: Dict[Tuple[str, int], StaticExactArm] = {}
    roots: set[Path] = set()
    for record in records:
        if len(record) != 6:
            raise ValidationError("Static Exact QoR arm must have six fields")
        label, seed_text, shared, lookahead, phase6, phase7 = record
        try:
            seed = int(seed_text)
        except ValueError as error:
            raise ValidationError("Static Exact QoR arm seed is invalid") from error
        key = (label, seed)
        if label not in _LABEL_CONTRACTS or seed < 1 or key in arms:
            raise ValidationError(
                "Static Exact QoR arm identity is invalid or duplicated"
            )
        arm = StaticExactArm(
            shared_root=_directory(shared, "shared root"),
            lookahead_root=_directory(lookahead, "lookahead root"),
            phase6_root=_directory(phase6, "Phase 6 root"),
            phase7_root=_directory(phase7, "Phase 7 root"),
        )
        for root in (
            arm.shared_root,
            arm.lookahead_root,
            arm.phase6_root,
            arm.phase7_root,
        ):
            if root in roots:
                raise ValidationError(
                    "Static Exact QoR arm checkpoint roots must be distinct"
                )
            roots.add(root)
        arms[key] = arm
    _physical_seeds(arms)
    return arms


def _physical_seeds(
    arms: Mapping[Tuple[str, int], StaticExactArm],
) -> tuple[int, ...]:
    seeds_by_label = {label: set() for label in STATIC_EXACT_ARM_LABELS}
    for key in arms:
        if not isinstance(key, tuple) or len(key) != 2:
            raise ValidationError(
                "Static Exact QoR comparison requires complete label/seed sets"
            )
        label, seed = key
        if (
            label not in seeds_by_label
            or isinstance(seed, bool)
            or not isinstance(seed, int)
            or seed < 1
        ):
            raise ValidationError(
                "Static Exact QoR comparison requires complete label/seed sets"
            )
        seeds_by_label[label].add(seed)
    seed_sets = list(seeds_by_label.values())
    if any(not seeds for seeds in seed_sets) or any(
        seeds != seed_sets[0] for seeds in seed_sets[1:]
    ):
        raise ValidationError(
            "Static Exact QoR comparison requires complete label/seed sets"
        )
    expected = {
        (label, seed)
        for label in STATIC_EXACT_ARM_LABELS
        for seed in seed_sets[0]
    }
    if set(arms) != expected:
        raise ValidationError(
            "Static Exact QoR comparison requires complete label/seed sets"
        )
    return tuple(sorted(seed_sets[0]))


def _common_source_record(shared_root: Path) -> Dict[str, Any]:
    timing_report = read_json(
        _require(shared_root, "timing/experiment-timing-report.json")
    )
    partition_report = read_json(
        _require(shared_root, "partition/experiment-partition-report.json")
    )
    for name, report in (
        ("timing", timing_report),
        ("partition", partition_report),
    ):
        if not isinstance(report, dict) or report.get("status") != "pass":
            raise ValidationError(f"Static Exact QoR {name} report is invalid")
    source = {
        "emuir_sha256": _sha256(
            _require(shared_root, "frontend/phase1/design.emuir.json")
        ),
        "timing_path_database_sha256": _sha256(
            _require(shared_root, "timing/path-database.json")
        ),
        "partition_weights_sha256": _sha256(
            _require(shared_root, "timing/partition-net-weights.json")
        ),
        "platform_sha256": _digest(
            partition_report.get("platform_sha256"), "platform"
        ),
        "route_constraints_sha256": partition_report.get(
            "route_constraints_sha256"
        ),
        "partition_constraints_sha256": partition_report.get(
            "constraints_sha256"
        ),
        "target_clocks": timing_report.get("clocks"),
        "timing_model_sha256": timing_report.get("timing_model_sha256"),
        "architecture_timing_db_sha256": timing_report.get(
            "architecture_timing_db_sha256"
        ),
    }
    if timing_report.get("frontend_emuir_sha256") != source["emuir_sha256"]:
        raise ValidationError("Static Exact QoR timing/frontend seal is broken")
    if (
        timing_report.get("path_database_sha256")
        != source["timing_path_database_sha256"]
        or timing_report.get("partition_net_weights_sha256")
        != source["partition_weights_sha256"]
        or partition_report.get("emuir_sha256") != source["emuir_sha256"]
        or partition_report.get("weights_sha256")
        != source["partition_weights_sha256"]
    ):
        raise ValidationError("Static Exact QoR common source seal is broken")
    return source


def _partition_evidence(label: str, shared_root: Path) -> Dict[str, Any]:
    report_path = _require(
        shared_root, "partition/experiment-partition-report.json"
    )
    report = read_json(report_path)
    expected_mode, expected_policy = _LABEL_CONTRACTS[label]
    mode = report.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    policy = report.get(
        "static_exact_candidate_policy", STATIC_EXACT_CANDIDATE_FRONTIER_V1
    )
    if mode != expected_mode or (
        expected_policy is not None and policy != expected_policy
    ):
        raise ValidationError(
            f"Static Exact QoR arm {label} does not match its cut contract"
        )
    phase3 = report.get("phase3")
    validation = phase3.get("validation") if isinstance(phase3, dict) else None
    if not isinstance(validation, dict) or validation.get("status") != "pass":
        raise ValidationError("Static Exact QoR partition validation is missing")
    combinational = validation.get("combinational_cut_nets", 0)
    dependency_depth = validation.get(
        "maximum_combinational_dependency_depth", 0
    )
    for name, value in (
        ("combinational cut count", combinational),
        ("dependency depth", dependency_depth),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"Static Exact QoR {name} is invalid")
    if label == "sequential-only" and combinational != 0:
        raise ValidationError(
            "sequential-only QoR arm contains combinational cut evidence"
        )
    return {
        "cut_mode": mode,
        "candidate_policy": policy,
        "configured_max_dependency_depth": report.get(
            "max_cross_fpga_dependency_depth", 1
        ),
        "combinational_cut_nets": combinational,
        "maximum_combinational_dependency_depth": dependency_depth,
        "static_exact_combinational_cut_exercised": report.get(
            "static_exact_combinational_cut_exercised", False
        ),
        "assignment_sha256": _sha256(
            _require(shared_root, "partition/assignment.json")
        ),
        "routes_sha256": _sha256(
            _require(shared_root, "system-route/routes.json")
        ),
        "phase5_schedule_sha256": _sha256(
            _require(shared_root, "tdm/schedule.json")
        ),
        "partition_report_sha256": _sha256(report_path),
    }


def _arm_record(
    label: str,
    seed: int,
    arm: StaticExactArm,
    platform_path: Path,
    *,
    reuse_validated_phase6_equivalence: bool,
) -> Dict[str, Any]:
    validation = validate_phase7_checkpoint(
        arm.phase7_root,
        arm.shared_root,
        arm.lookahead_root,
        arm.phase6_root,
        platform_path,
        expected_seed=seed,
        reuse_validated_phase6_equivalence=(
            reuse_validated_phase6_equivalence
        ),
    )
    if validation.get("provider") != "baseline":
        raise ValidationError(
            "Static Exact QoR comparison requires the common baseline Phase 6"
        )
    report_path = _require(arm.phase7_root, "experiment-phase7-report.json")
    report = read_json(report_path)
    schema = report.get("schema")
    if (
        schema not in {LEGACY_EXPERIMENT_PHASE7_SCHEMA, EXPERIMENT_PHASE7_SCHEMA}
        or report.get("status") != "pass"
        or report.get("provider") != "baseline"
        or report.get("physical_seed") != seed
    ):
        raise ValidationError("Static Exact QoR Phase 7 terminal is invalid")
    partition = _partition_evidence(label, arm.shared_root)
    source = _common_source_record(arm.shared_root)
    expected_upstream = {
        "emuir_sha256": source["emuir_sha256"],
        "assignment_sha256": partition["assignment_sha256"],
        "routes_sha256": partition["routes_sha256"],
        "schedule_sha256": _sha256(
            _require(arm.phase6_root, "schedule.json")
        ),
    }
    if report.get("frozen_upstream") != expected_upstream:
        raise ValidationError("Static Exact QoR Phase 7 upstream seal is broken")
    qor_path = _require(arm.phase7_root, "runtime/qor_report.json")
    qor = read_json(qor_path)
    if qor.get("schema") != QOR_REPORT_SCHEMA or qor.get("status") != "pass":
        raise ValidationError("Static Exact QoR arm did not complete Phase 7C")
    if schema == LEGACY_EXPERIMENT_PHASE7_SCHEMA:
        if (
            report.get("qor") != qor
            or _write_json_sha256(qor) != _sha256(qor_path)
        ):
            raise ValidationError("Static Exact QoR legacy terminal seal is broken")
    elif report.get("qor_sha256") != _sha256(qor_path):
        raise ValidationError("Static Exact QoR terminal seal is broken")
    timing = qor.get("timing")
    if not isinstance(timing, dict) or timing.get("status") != "pass":
        raise ValidationError("Static Exact QoR system timing is incomplete")
    target_wns, target_tns, target_failing = _clock_metrics(
        timing.get("target_clock"), "Static Exact target clock"
    )
    runtime_wns, runtime_tns, runtime_failing = _clock_metrics(
        timing.get("runtime_clock"), "Static Exact runtime clock"
    )
    physical = qor.get("physical")
    runtime = qor.get("runtime")
    partition_qor = qor.get("partition")
    routing = qor.get("system_routing")
    tdm = qor.get("tdm")
    if not all(
        isinstance(item, dict)
        for item in (physical, runtime, partition_qor, routing, tdm)
    ):
        raise ValidationError("Static Exact QoR metrics are incomplete")
    metrics = {
        "global_target_clock_wns_ns": target_wns,
        "global_target_clock_tns_ns": target_tns,
        "global_target_clock_failing_endpoints": target_failing,
        "global_runtime_clock_wns_ns": runtime_wns,
        "global_runtime_clock_tns_ns": runtime_tns,
        "global_runtime_clock_failing_endpoints": runtime_failing,
        "per_fpga_wns_ns": _number(
            physical.get("worst_wns_ns"), "Static Exact per-FPGA WNS"
        ),
        "per_fpga_tns_ns": _number(
            physical.get("total_tns_ns"), "Static Exact per-FPGA TNS"
        ),
        "nominal_virtual_frequency_mhz": _number(
            runtime.get("nominal_virtual_frequency_mhz"),
            "Static Exact virtual frequency",
        ),
        "transport_cells": _number(
            physical.get("transport_cells"), "Static Exact transport cells"
        ),
        "physical_cells": _number(
            physical.get("physical_cells"), "Static Exact physical cells"
        ),
        "cut_nets": _number(
            partition_qor.get("cut_nets"), "Static Exact cut nets"
        ),
        "scheduled_bit_hops": _number(
            tdm.get("scheduled_bit_hops"), "Static Exact scheduled bit hops"
        ),
        "frame_slots": _number(
            tdm.get("frame_slots"), "Static Exact frame slots"
        ),
        "completion_slot": _number(
            tdm.get("completion_slot"), "Static Exact completion slot"
        ),
    }
    execution_runtime = _execution_runtime(
        arm.shared_root,
        arm.lookahead_root,
        arm.phase6_root,
        arm.phase7_root,
    )
    if execution_runtime is not None:
        metrics.update(
            {
                "physical_wall_seconds": execution_runtime[
                    "physical_wall_seconds"
                ],
                "phase3_to_7_wall_seconds": execution_runtime[
                    "phase3_to_7_wall_seconds"
                ],
            }
        )
    return {
        "label": label,
        "physical_seed": seed,
        "design": qor.get("design"),
        "platform": qor.get("platform"),
        "common_source": source,
        "partition": partition,
        "phase7_configuration": {
            "workers": report.get("workers"),
            "route_channel_width": report.get("route_channel_width"),
        },
        "system_timing_qualification": timing.get("qualification"),
        "path_exactness": timing.get("path_exactness"),
        "metrics": metrics,
        "execution_runtime": execution_runtime,
        "artifacts": {
            "phase6_manifest_sha256": _digest(
                report.get("phase6_manifest_sha256"), "Phase 6 manifest"
            ),
            "phase7_report_sha256": _sha256(report_path),
            "physical_summary_sha256": _digest(
                report.get("physical_summary_sha256"), "physical summary"
            ),
            "qor_sha256": _sha256(qor_path),
        },
    }


def _classification(values: Sequence[float], tolerance: float = 1.0e-9) -> str:
    if all(abs(value) <= tolerance for value in values):
        return "unchanged"
    if statistics.fmean(values) > tolerance and all(
        value >= -tolerance for value in values
    ):
        return "improved"
    if statistics.fmean(values) < -tolerance and all(
        value <= tolerance for value in values
    ):
        return "regressed"
    return "mixed"


def _comparison(
    candidate: str,
    reference: str,
    by_key: Mapping[Tuple[str, int], Mapping[str, Any]],
    seeds: Sequence[int],
) -> Dict[str, Any]:
    deltas = []
    for seed in seeds:
        baseline = by_key[(reference, seed)]["metrics"]
        selected = by_key[(candidate, seed)]["metrics"]
        scalar_metrics = tuple(
            metric
            for metric in _SCALAR_METRICS
            if metric in baseline and metric in selected
        )
        deltas.append(
            {
                "physical_seed": seed,
                **{
                    metric: float(selected[metric]) - float(baseline[metric])
                    for metric in scalar_metrics
                },
            }
        )
    classes = {
        metric: _classification([record[metric] for record in deltas])
        for metric in _TIMING_METRICS
    }
    target_classes = {
        classes["global_target_clock_wns_ns"],
        classes["global_target_clock_tns_ns"],
    }
    if "mixed" in target_classes or (
        "improved" in target_classes and "regressed" in target_classes
    ):
        target_result = "mixed"
    elif "regressed" in target_classes:
        target_result = "regressed"
    elif "improved" in target_classes:
        target_result = "improved"
    else:
        target_result = "unchanged"
    return {
        "candidate": candidate,
        "reference": reference,
        "paired_seed_deltas": deltas,
        "mean_deltas": {
            metric: statistics.fmean([record[metric] for record in deltas])
            for metric in deltas[0]
            if metric != "physical_seed"
        },
        "timing_metric_classification": classes,
        "target_clock_result": target_result,
    }


def build_static_exact_qor_comparison(
    platform_path: Path,
    arms: Mapping[Tuple[str, int], StaticExactArm],
    *,
    reuse_validated_phase6_equivalence: bool = False,
) -> Dict[str, Any]:
    if platform_path.is_symlink() or not platform_path.is_file():
        raise ValidationError("Static Exact QoR platform is invalid")
    platform_path = platform_path.resolve()
    seeds = _physical_seeds(arms)
    records = [
        _arm_record(
            label,
            seed,
            arms[(label, seed)],
            platform_path,
            reuse_validated_phase6_equivalence=(
                reuse_validated_phase6_equivalence
            ),
        )
        for label in STATIC_EXACT_ARM_LABELS
        for seed in seeds
    ]
    by_key = {
        (record["label"], record["physical_seed"]): record
        for record in records
    }
    source_identities = {
        json.dumps(record["common_source"], sort_keys=True, separators=(",", ":"))
        for record in records
    }
    if len(source_identities) != 1:
        raise ValidationError(
            "Static Exact QoR arms do not share exact source/timing inputs"
        )
    design_platform = {
        (record["design"], record["platform"]) for record in records
    }
    if len(design_platform) != 1:
        raise ValidationError("Static Exact QoR arms do not share design/platform")
    channel_widths = {
        record["phase7_configuration"]["route_channel_width"]
        for record in records
    }
    if len(channel_widths) != 1:
        raise ValidationError(
            "Static Exact QoR arms do not share physical channel width"
        )
    design, platform = next(iter(design_platform))
    common_source = records[0]["common_source"]
    public_records = [
        {
            key: value
            for key, value in record.items()
            if key not in {"design", "platform", "common_source"}
        }
        for record in records
    ]
    runtime_qualified = all(
        record["execution_runtime"] is not None for record in records
    )
    if any(record["execution_runtime"] is not None for record in records) and not (
        runtime_qualified
    ):
        raise ValidationError(
            "Static Exact QoR runtime evidence is incomplete across arms"
        )
    comparisons = {
        "legacy-v1-vs-sequential": _comparison(
            "legacy-static-exact-v1", "sequential-only", by_key, seeds
        ),
        "generalized-v2-vs-sequential": _comparison(
            "generalized-static-exact-v2", "sequential-only", by_key, seeds
        ),
        "generalized-v2-vs-legacy-v1": _comparison(
            "generalized-static-exact-v2",
            "legacy-static-exact-v1",
            by_key,
            seeds,
        ),
    }
    generalized_exercised = all(
        by_key[("generalized-static-exact-v2", seed)]["partition"][
            "combinational_cut_nets"
        ]
        > 0
        for seed in seeds
    )
    target_result = comparisons["generalized-v2-vs-sequential"][
        "target_clock_result"
    ]
    return {
        "schema": STATIC_EXACT_QOR_COMPARISON_SCHEMA,
        "status": "pass",
        "design": design,
        "platform": platform,
        "qualification": (
            "single-seed-complete-phase1-7c-static-exact-ab"
            if len(seeds) == 1
            else "paired-multi-seed-complete-phase1-7c-static-exact-ab"
        ),
        "physical_seeds": list(seeds),
        "common_source": common_source,
        "platform_file_sha256": _sha256(platform_path),
        "physical_route_channel_width": next(iter(channel_widths)),
        "claim_scope": (
            "whole-original-design target/runtime timing after complete open "
            "physical Phase 7; per-FPGA timing is diagnostic"
        ),
        "arms": public_records,
        "comparisons": comparisons,
        "promotion_gate": {
            "generalized_v2_exercised_real_combinational_cuts": (
                generalized_exercised
            ),
            "generalized_v2_target_clock_result": target_result,
            "sealed_execution_runtime_available": runtime_qualified,
            "eligible_for_default_promotion": (
                generalized_exercised
                and target_result == "improved"
                and runtime_qualified
            ),
        },
    }


def run_static_exact_qor_comparison(
    platform_path: Path,
    arms: Mapping[Tuple[str, int], StaticExactArm],
    output_dir: Path,
    *,
    reuse_validated_phase6_equivalence: bool = False,
) -> Dict[str, Any]:
    output_dir = validate_experiment_write_path(output_dir)
    if output_dir.exists() and (
        not output_dir.is_dir() or any(output_dir.iterdir())
    ):
        raise EmuFlowError("Static Exact QoR output must be an empty directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_static_exact_qor_comparison(
        platform_path,
        arms,
        reuse_validated_phase6_equivalence=(
            reuse_validated_phase6_equivalence
        ),
    )
    write_json(output_dir / "static-exact-qor-comparison.json", report)
    validate_static_exact_qor_comparison(
        output_dir,
        platform_path,
        arms,
        reuse_validated_phase6_equivalence=(
            reuse_validated_phase6_equivalence
        ),
    )
    return report


def validate_static_exact_qor_comparison(
    root: Path,
    platform_path: Path,
    arms: Mapping[Tuple[str, int], StaticExactArm],
    *,
    reuse_validated_phase6_equivalence: bool = False,
) -> Dict[str, Any]:
    report = read_json(_require(root, "static-exact-qor-comparison.json"))
    expected = build_static_exact_qor_comparison(
        platform_path,
        arms,
        reuse_validated_phase6_equivalence=(
            reuse_validated_phase6_equivalence
        ),
    )
    if report != expected:
        raise ValidationError("Static Exact QoR comparison replay disagrees")
    return {
        "status": "pass",
        "design": report["design"],
        "platform": report["platform"],
        "arms": len(report["arms"]),
        "generalized_v2_target_clock_result": report["promotion_gate"][
            "generalized_v2_target_clock_result"
        ],
        "eligible_for_default_promotion": report["promotion_gate"][
            "eligible_for_default_promotion"
        ],
    }
