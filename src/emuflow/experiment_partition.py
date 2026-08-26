"""Partition-only experiment checkpoint producer and independent validator.

This module deliberately isolates Phase 3 policy from the reusable frontend,
timing, routing, and scheduling checkpoint implementations.  A partition-only
change must not invalidate those unrelated implementation closures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ValidationError
from .experiment_stages import _prepare_empty_output
from .experiment_upstream import (
    EXPERIMENT_PARTITION_SCHEMA,
    _require,
    _sha256,
    validate_timing_checkpoint,
)
from .io import read_json, write_json
from .partition_hops import validate_assignment_hop_constraints
from .partition_pressure import validate_partition_pressure_native_bundle
from .phase3 import run_phase3, validate_phase3
from .ir import EmuIR
from .platform import Platform
from .routing import load_route_constraints


def run_partition_checkpoint(
    frontend_root: Path,
    timing_root: Path,
    platform_path: Path,
    output_dir: Path,
    *,
    provider: str = "tritonpart",
    seed: int = 0,
    constraints_path: Optional[Path] = None,
    route_constraints_path: Optional[Path] = None,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    openroad: Optional[str] = None,
    hop_refiner: Optional[str] = None,
    timeout_seconds: int = 3600,
    seed_attempts: int = 1,
    repair_balance: bool = False,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_initial_assignment_path: Optional[Path] = None,
) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    validate_timing_checkpoint(frontend_root, timing_root)
    weights_path = _require(timing_root, "partition-net-weights.json")
    output_dir = _prepare_empty_output(output_dir, "partition checkpoint")
    phase3 = run_phase3(
        ir_path,
        platform_path,
        output_dir,
        constraints_path=constraints_path,
        seed=seed,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
        provider=provider,
        openroad=openroad,
        tritonpart_timeout_seconds=timeout_seconds,
        tritonpart_seed_attempts=seed_attempts,
        tritonpart_repair_balance=repair_balance,
        tritonpart_num_initial_solutions=num_initial_solutions,
        tritonpart_num_best_initial_solutions=num_best_initial_solutions,
        net_weights_path=weights_path,
        route_constraints_path=route_constraints_path,
        hop_refiner=hop_refiner,
        timing_database_path=(
            _require(timing_root, "path-database.json")
            if provider == "patron"
            else None
        ),
        patron_refiner=patron_refiner,
        patron_max_moves=patron_max_moves,
        patron_initial_assignment_path=patron_initial_assignment_path,
    )
    report = {
        "schema": EXPERIMENT_PARTITION_SCHEMA,
        "status": "pass",
        "provider": provider,
        "seed": seed,
        "seed_attempts": seed_attempts,
        "repair_balance": repair_balance,
        "emuir_sha256": _sha256(ir_path),
        "platform_sha256": _sha256(platform_path.resolve()),
        "weights_sha256": _sha256(weights_path),
        "assignment_sha256": _sha256(output_dir / "assignment.json"),
        "clusters_sha256": _sha256(output_dir / "clusters.json"),
        "phase3_report_sha256": _sha256(output_dir / "phase3_report.json"),
        "route_constraints_sha256": (
            _sha256(route_constraints_path.resolve())
            if route_constraints_path is not None
            else None
        ),
        "patron_initial_assignment_sha256": (
            _sha256(patron_initial_assignment_path.resolve())
            if patron_initial_assignment_path is not None
            else None
        ),
        "phase3": phase3,
    }
    if provider == "patron":
        report["patron_artifact_sha256"] = {
            "model": _sha256(output_dir / "patron/pressure_model.json"),
            "trace": _sha256(output_dir / "patron/refinement_trace.json"),
            "initial_assignment": _sha256(
                output_dir / "patron/initial_assignment.json"
            ),
        }
    write_json(output_dir / "experiment-partition-report.json", report)
    validate_partition_checkpoint(
        frontend_root,
        timing_root,
        platform_path,
        output_dir,
        route_constraints_path=route_constraints_path,
        expected_provider=provider,
        expected_seed=seed,
        expected_seed_attempts=seed_attempts,
        expected_repair_balance=repair_balance,
        patron_initial_assignment_path=patron_initial_assignment_path,
    )
    return report


def validate_partition_checkpoint(
    frontend_root: Path,
    timing_root: Path,
    platform_path: Path,
    root: Path,
    *,
    route_constraints_path: Path | None = None,
    expected_provider: str | None = None,
    expected_seed: int | None = None,
    expected_seed_attempts: int | None = None,
    expected_repair_balance: bool | None = None,
    patron_initial_assignment_path: Path | None = None,
) -> Dict[str, Any]:
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    weights = _require(timing_root, "partition-net-weights.json")
    report = read_json(_require(root, "experiment-partition-report.json"))
    if (
        report.get("schema") != EXPERIMENT_PARTITION_SCHEMA
        or report.get("status") != "pass"
    ):
        raise ValidationError("partition checkpoint report is invalid")
    if expected_provider is not None and report.get("provider") != expected_provider:
        raise ValidationError("partition provider contract disagrees")
    if expected_seed is not None and report.get("seed") != expected_seed:
        raise ValidationError("partition seed contract disagrees")
    if (
        expected_seed_attempts is not None
        and report.get("seed_attempts") != expected_seed_attempts
    ):
        raise ValidationError("partition seed-attempt contract disagrees")
    if (
        expected_repair_balance is not None
        and report.get("repair_balance") is not expected_repair_balance
    ):
        raise ValidationError("partition balance-repair contract disagrees")
    if route_constraints_path is not None and report.get(
        "route_constraints_sha256"
    ) != _sha256(route_constraints_path.resolve()):
        raise ValidationError("partition route-constraints seal is broken")
    expected_patron_initial = (
        _sha256(patron_initial_assignment_path.resolve())
        if patron_initial_assignment_path is not None
        else None
    )
    if report.get("patron_initial_assignment_sha256") != expected_patron_initial:
        raise ValidationError(
            "partition PATRON initial-assignment seal is broken"
        )
    seals = {
        "emuir_sha256": ir_path,
        "platform_sha256": platform_path.resolve(),
        "weights_sha256": weights,
        "assignment_sha256": _require(root, "assignment.json"),
        "clusters_sha256": _require(root, "clusters.json"),
        "phase3_report_sha256": _require(root, "phase3_report.json"),
    }
    for label, path in seals.items():
        if report.get(label) != _sha256(path):
            raise ValidationError(f"partition checkpoint {label} seal is broken")
    assignment = read_json(seals["assignment_sha256"])
    if report["provider"] == "tritonpart":
        metadata = assignment.get("provider_metadata", {})
        attempts = metadata.get("seed_attempts", [])
        actual_seed_attempts = sum(
            item.get("mode") == "timing_weighted"
            for item in attempts
            if isinstance(item, dict)
        )
        actual_repair_balance = metadata.get("balance_repair", {}).get("enabled")
        if (
            "seed_attempts" in report
            and report["seed_attempts"] != actual_seed_attempts
        ):
            raise ValidationError(
                "partition seed-attempt report disagrees with assignment"
            )
        if (
            "repair_balance" in report
            and report["repair_balance"] is not actual_repair_balance
        ):
            raise ValidationError(
                "partition balance-repair report disagrees with assignment"
            )
    checked = validate_phase3(
        ir_path,
        platform_path,
        seals["clusters_sha256"],
        seals["assignment_sha256"],
    )
    algorithm_validation = None
    if report["provider"] == "patron":
        patron_paths = {
            "model": _require(root, "patron/pressure_model.json"),
            "trace": _require(root, "patron/refinement_trace.json"),
            "initial_assignment": _require(
                root, "patron/initial_assignment.json"
            ),
        }
        expected_artifact_hashes = report.get("patron_artifact_sha256")
        if (
            not isinstance(expected_artifact_hashes, dict)
            or expected_artifact_hashes
            != {
                label: _sha256(path)
                for label, path in patron_paths.items()
            }
        ):
            raise ValidationError(
                "partition PATRON artifact seal is broken"
            )
        ir = EmuIR.load(ir_path)
        platform = Platform.load(platform_path)
        algorithm_validation = validate_partition_pressure_native_bundle(
            ir,
            platform,
            read_json(seals["clusters_sha256"]),
            read_json(_require(root, "constraints.normalized.json")),
            read_json(_require(timing_root, "path-database.json")),
            load_route_constraints(route_constraints_path, platform),
            read_json(patron_paths["model"]),
            read_json(patron_paths["initial_assignment"]),
            assignment,
            read_json(patron_paths["trace"]),
        )
        if report.get("phase3", {}).get("algorithm_validation") != (
            algorithm_validation
        ):
            raise ValidationError(
                "partition PATRON algorithm validation mismatch"
            )
    hop_audit = (
        validate_assignment_hop_constraints(
            seals["assignment_sha256"], platform_path, route_constraints_path
        )
        if route_constraints_path is not None
        else {"status": "not-requested", "max_route_hops": None}
    )
    return {
        "status": "pass",
        "provider": report["provider"],
        "hop_audit": hop_audit,
        **(
            {"algorithm_validation": algorithm_validation}
            if algorithm_validation is not None
            else {}
        ),
        **checked,
    }
