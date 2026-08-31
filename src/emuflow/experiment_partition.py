"""Partition-only experiment checkpoint producer and independent validator.

This module deliberately isolates Phase 3 policy from the reusable frontend,
timing, routing, and scheduling checkpoint implementations.  A partition-only
change must not invalidate those unrelated implementation closures.
"""

from __future__ import annotations

import hashlib
import time
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
from .ir import EmuIR
from .partition_hops import validate_assignment_hop_constraints
from .partition import CUT_MODE_SEQUENTIAL_ONLY, CUT_MODE_STATIC_EXACT
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)
from .mfspart_refine import (
    DEFAULT_BOTTLENECK_BETA,
    DEFAULT_TIMING_PATH_BETA,
    validate_mfspart_native_certificate,
)
from .phase3 import run_phase3, validate_phase3
from .sta import STA_PATH_DATABASE_SCHEMA


def _rebuild_timing_path_objective(
    ir_path: Path,
    clusters_path: Path,
    database_path: Path,
) -> Dict[str, Any]:
    """Independently bind native PATH records to TimingDB semantics."""

    ir = EmuIR.load(ir_path)
    clusters = read_json(clusters_path)
    database = read_json(database_path)
    if (
        database.get("schema") != STA_PATH_DATABASE_SCHEMA
        or database.get("design") != ir.value["design"]["name"]
        or not isinstance(database.get("paths"), list)
    ):
        raise ValidationError("invalid partition timing-path database")
    ordered_clusters = sorted(clusters["clusters"], key=lambda item: item["id"])
    node_index = {
        cluster["id"]: index for index, cluster in enumerate(ordered_clusters)
    }
    cluster_by_instance = {
        instance: cluster["id"]
        for cluster in ordered_clusters
        for instance in cluster["instances"]
    }
    net_driver_clusters: Dict[str, tuple[int, ...]] = {}
    net_sink_clusters: Dict[str, tuple[int, ...]] = {}
    for net in ir.value["nets"]:
        net_driver_clusters[net["id"]] = tuple(
            sorted(
                {
                    node_index[cluster_by_instance[endpoint["instance"]]]
                    for endpoint in net["drivers"]
                    if endpoint["instance"] in cluster_by_instance
                }
            )
        )
        net_sink_clusters[net["id"]] = tuple(
            sorted(
                {
                    node_index[cluster_by_instance[endpoint["instance"]]]
                    for endpoint in net["sinks"]
                    if endpoint["instance"] in cluster_by_instance
                }
            )
        )
    grouped: Dict[tuple[int, ...], float] = {}
    eligible_paths = 0
    unmaterialized_paths = 0
    for path in database["paths"]:
        try:
            pins = {
                pin
                for net_id in path["path_nets"]
                for pin in net_driver_clusters[net_id]
            }
            for endpoint_name in ("startpoint", "endpoint"):
                endpoint = path.get(endpoint_name)
                if (
                    endpoint is not None
                    and endpoint["instance"] in cluster_by_instance
                ):
                    pins.add(
                        node_index[cluster_by_instance[endpoint["instance"]]]
                    )
            if "endpoint" not in path:
                pins.update(net_sink_clusters[path["path_nets"][-1]])
        except (KeyError, IndexError, TypeError) as error:
            raise ValidationError(
                "malformed partition timing-path database record"
            ) from error
        if len(pins) < 2:
            unmaterialized_paths += 1
            continue
        eligible_paths += 1
        key = tuple(sorted(pins))
        grouped[key] = grouped.get(key, 0.0) + 1.0
    digest = hashlib.sha256()
    compressed_pins = 0
    for index, (pins, weight) in enumerate(sorted(grouped.items())):
        compressed_pins += len(pins)
        record = "PATH " + " ".join(
            str(value)
            for value in (index, format(weight, ".17g"), len(pins), *pins)
        )
        digest.update((record + "\n").encode("utf-8"))
    return {
        "schema": "emuflow.mfspart-timing-path-objective/v1",
        "database_sha256": _sha256(database_path),
        "database_paths": len(database["paths"]),
        "eligible_paths": eligible_paths,
        "unmaterialized_paths": unmaterialized_paths,
        "compressed_groups": len(grouped),
        "compressed_pins": compressed_pins,
        "objective_sha256": digest.hexdigest(),
        "weighting": "uniform-path-count-with-identical-pin-set-aggregation",
    }


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
    tritonpart_solution: Optional[Path] = None,
    hop_refiner: Optional[str] = None,
    mfspart_post_refinement: Optional[bool] = None,
    mfspart_post_refinement_early_stop: int = 1000,
    mfspart_post_refinement_bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    mfspart_post_refinement_timing_path_beta: float = DEFAULT_TIMING_PATH_BETA,
    timeout_seconds: int = 3600,
    seed_attempts: int = 1,
    repair_balance: bool = False,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    cut_mode: str = CUT_MODE_SEQUENTIAL_ONLY,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    comb_segment_budget_slots: int = 1,
    minimum_combinational_cut_nets: int = 0,
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    if mfspart_post_refinement is None:
        mfspart_post_refinement = (
            cut_mode == CUT_MODE_STATIC_EXACT and provider == "tritonpart"
        )
    if (
        isinstance(minimum_combinational_cut_nets, bool)
        or not isinstance(minimum_combinational_cut_nets, int)
        or minimum_combinational_cut_nets < 0
    ):
        raise ValidationError(
            "minimum combinational cut nets must be a non-negative integer"
        )
    if (
        minimum_combinational_cut_nets
        and cut_mode == CUT_MODE_SEQUENTIAL_ONLY
    ):
        raise ValidationError(
            "minimum combinational cut nets requires static exact cut mode"
        )
    if tritonpart_solution is not None and provider != "tritonpart":
        raise ValidationError(
            "a precomputed TritonPart solution requires provider=tritonpart"
        )
    if mfspart_post_refinement and provider != "tritonpart":
        raise ValidationError(
            "MFSPart post-refinement requires provider=tritonpart"
        )
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    if not managed_dag_node:
        validate_timing_checkpoint(frontend_root, timing_root)
    weights_path = _require(timing_root, "partition-net-weights.json")
    timing_path_database = _require(timing_root, "path-database.json")
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
        tritonpart_solution=tritonpart_solution,
        tritonpart_timeout_seconds=timeout_seconds,
        tritonpart_seed_attempts=seed_attempts,
        tritonpart_repair_balance=repair_balance,
        tritonpart_num_initial_solutions=num_initial_solutions,
        tritonpart_num_best_initial_solutions=num_best_initial_solutions,
        net_weights_path=weights_path,
        route_constraints_path=route_constraints_path,
        hop_refiner=hop_refiner,
        mfspart_post_refinement=mfspart_post_refinement,
        mfspart_post_refinement_early_stop=(
            mfspart_post_refinement_early_stop
        ),
        mfspart_post_refinement_bottleneck_beta=(
            mfspart_post_refinement_bottleneck_beta
        ),
        timing_path_database_path=timing_path_database,
        mfspart_post_refinement_timing_path_beta=(
            mfspart_post_refinement_timing_path_beta
        ),
        cut_mode=cut_mode,
        max_cross_fpga_dependency_depth=max_cross_fpga_dependency_depth,
        comb_segment_budget_slots=comb_segment_budget_slots,
        static_exact_candidate_policy=static_exact_candidate_policy,
        managed_dag_node=managed_dag_node,
    )
    report = {
        "schema": EXPERIMENT_PARTITION_SCHEMA,
        "status": "pass",
        "provider": provider,
        "seed": seed,
        "seed_attempts": seed_attempts,
        "repair_balance": repair_balance,
        "mfspart_post_refinement": mfspart_post_refinement,
        "mfspart_post_refinement_early_stop": (
            mfspart_post_refinement_early_stop
        ),
        "mfspart_post_refinement_bottleneck_beta": (
            mfspart_post_refinement_bottleneck_beta
        ),
        "mfspart_post_refinement_timing_path_beta": (
            mfspart_post_refinement_timing_path_beta
        ),
        "cut_mode": cut_mode,
        "max_cross_fpga_dependency_depth": (
            max_cross_fpga_dependency_depth
        ),
        "comb_segment_budget_slots": comb_segment_budget_slots,
        "static_exact_candidate_policy": static_exact_candidate_policy,
        "minimum_combinational_cut_nets": minimum_combinational_cut_nets,
        "static_exact_combinational_cut_exercised": (
            cut_mode != CUT_MODE_SEQUENTIAL_ONLY
            and phase3["validation"].get("combinational_cut_nets", 0) > 0
        ),
        "phase3": phase3,
    }
    if not managed_dag_node:
        # The standalone deep-qualification command retains its historical
        # seals.  Managed production DAGs intentionally perform no content
        # hashing in the Phase-3 hot path.
        report.update(
            {
                "emuir_sha256": _sha256(ir_path),
                "platform_sha256": _sha256(platform_path.resolve()),
                "weights_sha256": _sha256(weights_path),
                "timing_path_database_sha256": _sha256(
                    timing_path_database
                ),
                "assignment_sha256": _sha256(
                    output_dir / "assignment.json"
                ),
                "clusters_sha256": _sha256(output_dir / "clusters.json"),
                "phase3_report_sha256": _sha256(
                    output_dir / "phase3_report.json"
                ),
                "tritonpart_solution_sha256": (
                    _sha256(tritonpart_solution.resolve())
                    if tritonpart_solution is not None
                    else None
                ),
                "route_constraints_sha256": (
                    _sha256(route_constraints_path.resolve())
                    if route_constraints_path is not None
                    else None
                ),
                "constraints_sha256": (
                    _sha256(constraints_path.resolve())
                    if constraints_path is not None
                    else None
                ),
            }
        )
    write_json(
        output_dir / "experiment-partition-report.json", report, compact=True
    )
    if not managed_dag_node:
        validate_partition_checkpoint(
            frontend_root,
            timing_root,
            platform_path,
            output_dir,
            constraints_path=constraints_path,
            route_constraints_path=route_constraints_path,
            tritonpart_solution=tritonpart_solution,
            expected_provider=provider,
            expected_seed=seed,
            expected_seed_attempts=seed_attempts,
            expected_repair_balance=repair_balance,
            expected_mfspart_post_refinement=mfspart_post_refinement,
            expected_mfspart_post_refinement_early_stop=(
                mfspart_post_refinement_early_stop
            ),
            expected_mfspart_post_refinement_bottleneck_beta=(
                mfspart_post_refinement_bottleneck_beta
            ),
            expected_mfspart_post_refinement_timing_path_beta=(
                mfspart_post_refinement_timing_path_beta
            ),
            expected_cut_mode=cut_mode,
            expected_max_cross_fpga_dependency_depth=(
                max_cross_fpga_dependency_depth
            ),
            expected_comb_segment_budget_slots=comb_segment_budget_slots,
            expected_minimum_combinational_cut_nets=(
                minimum_combinational_cut_nets
            ),
            expected_static_exact_candidate_policy=(
                static_exact_candidate_policy
            ),
        )
    return report


def validate_partition_checkpoint(
    frontend_root: Path,
    timing_root: Path,
    platform_path: Path,
    root: Path,
    *,
    constraints_path: Path | None = None,
    route_constraints_path: Path | None = None,
    tritonpart_solution: Path | None = None,
    expected_provider: str | None = None,
    expected_seed: int | None = None,
    expected_seed_attempts: int | None = None,
    expected_repair_balance: bool | None = None,
    expected_mfspart_post_refinement: bool | None = None,
    expected_mfspart_post_refinement_early_stop: int | None = None,
    expected_mfspart_post_refinement_bottleneck_beta: float | None = None,
    expected_mfspart_post_refinement_timing_path_beta: float | None = None,
    expected_cut_mode: str | None = None,
    expected_max_cross_fpga_dependency_depth: int | None = None,
    expected_comb_segment_budget_slots: int | None = None,
    expected_minimum_combinational_cut_nets: int | None = None,
    expected_static_exact_candidate_policy: str | None = None,
    online_validation: bool = False,
) -> Dict[str, Any]:
    validation_started = time.perf_counter()
    ir_path = _require(frontend_root, "phase1/design.emuir.json")
    weights = _require(timing_root, "partition-net-weights.json")
    timing_path_database = timing_root / "path-database.json"
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
    if (
        expected_mfspart_post_refinement is not None
        and report.get("mfspart_post_refinement", False)
        is not expected_mfspart_post_refinement
    ):
        raise ValidationError("partition MFSPart post-refinement contract disagrees")
    if (
        expected_mfspart_post_refinement_early_stop is not None
        and report.get("mfspart_post_refinement_early_stop", 1000)
        != expected_mfspart_post_refinement_early_stop
    ):
        raise ValidationError(
            "partition MFSPart post-refinement early-stop contract disagrees"
        )
    if (
        expected_mfspart_post_refinement_timing_path_beta is not None
        and report.get(
            "mfspart_post_refinement_timing_path_beta",
            DEFAULT_TIMING_PATH_BETA,
        ) != expected_mfspart_post_refinement_timing_path_beta
    ):
        raise ValidationError(
            "partition MFSPart timing-path-beta contract disagrees"
        )
    if (
        expected_mfspart_post_refinement_bottleneck_beta is not None
        and report.get(
            "mfspart_post_refinement_bottleneck_beta",
            DEFAULT_BOTTLENECK_BETA,
        ) != expected_mfspart_post_refinement_bottleneck_beta
    ):
        raise ValidationError(
            "partition MFSPart post-refinement bottleneck-beta contract "
            "disagrees"
        )
    legacy_cut_defaults = {
        "cut_mode": CUT_MODE_SEQUENTIAL_ONLY,
        "max_cross_fpga_dependency_depth": 1,
        "comb_segment_budget_slots": 1,
        "static_exact_candidate_policy": STATIC_EXACT_CANDIDATE_FRONTIER_V1,
        "minimum_combinational_cut_nets": 0,
    }
    for field, expected in (
        ("cut_mode", expected_cut_mode),
        (
            "max_cross_fpga_dependency_depth",
            expected_max_cross_fpga_dependency_depth,
        ),
        ("comb_segment_budget_slots", expected_comb_segment_budget_slots),
        (
            "static_exact_candidate_policy",
            expected_static_exact_candidate_policy,
        ),
        (
            "minimum_combinational_cut_nets",
            expected_minimum_combinational_cut_nets,
        ),
    ):
        if (
            expected is not None
            and report.get(field, legacy_cut_defaults[field]) != expected
        ):
            raise ValidationError(
                f"partition {field.replace('_', '-')} contract disagrees"
            )
    if online_validation:
        for relative in (
            "assignment.json",
            "clusters.json",
            "phase3_report.json",
        ):
            _require(root, relative)
        phase3 = report.get("phase3")
        if (
            not isinstance(phase3, dict)
            or phase3.get("status") != "pass"
            or not isinstance(phase3.get("validation"), dict)
            or phase3["validation"].get("status") != "pass"
        ):
            raise ValidationError("partition online legality evidence is invalid")
        runtime = (
            phase3.get("mfspart_post_refinement") or {}
        ).get("refinement", {}).get("runtime")
        validator_wall_seconds = time.perf_counter() - validation_started
        if isinstance(runtime, dict):
            optimizer_wall_seconds = runtime.get("optimizer_wall_seconds")
            candidate_check_wall_seconds = runtime.get(
                "candidate_check_wall_seconds"
            )
            if (
                not isinstance(optimizer_wall_seconds, (int, float))
                or not isinstance(candidate_check_wall_seconds, (int, float))
                or candidate_check_wall_seconds > optimizer_wall_seconds
                or validator_wall_seconds > optimizer_wall_seconds
            ):
                raise ValidationError(
                    "partition online validation exceeded optimizer runtime"
                )
        return {
            "status": "pass",
            "provider": report["provider"],
            "validation": phase3["validation"],
            "runtime": {
                "validator_wall_seconds": validator_wall_seconds,
                "optimizer_wall_seconds": (
                    runtime.get("optimizer_wall_seconds")
                    if isinstance(runtime, dict)
                    else None
                ),
            },
        }
    if route_constraints_path is not None and report.get(
        "route_constraints_sha256"
    ) != _sha256(route_constraints_path.resolve()):
        raise ValidationError("partition route-constraints seal is broken")
    expected_constraints_sha256 = (
        _sha256(constraints_path.resolve())
        if constraints_path is not None
        else None
    )
    if report.get("constraints_sha256") != expected_constraints_sha256:
        raise ValidationError("partition constraints seal is broken")
    expected_tritonpart_solution_sha256 = (
        _sha256(tritonpart_solution.resolve())
        if tritonpart_solution is not None
        else None
    )
    if report.get("tritonpart_solution_sha256") != (
        expected_tritonpart_solution_sha256
    ):
        raise ValidationError("partition TritonPart solution seal is broken")
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
    if "timing_path_database_sha256" in report:
        timing_path_database = _require(timing_root, "path-database.json")
        if report.get("timing_path_database_sha256") != _sha256(
            timing_path_database
        ):
            raise ValidationError(
                "partition checkpoint timing-path database seal is broken"
            )
    assignment = read_json(seals["assignment_sha256"])
    post_refinement = report.get("mfspart_post_refinement", False)
    if not isinstance(post_refinement, bool):
        raise ValidationError("partition MFSPart post-refinement flag is invalid")
    post_metadata = assignment.get("provider_metadata", {}).get(
        "directional_mfspart_post_refinement"
    )
    if post_refinement != (post_metadata is not None):
        raise ValidationError(
            "partition MFSPart post-refinement evidence disagrees with assignment"
        )
    if post_refinement:
        if not isinstance(post_metadata, dict):
            raise ValidationError(
                "partition MFSPart post-refinement metadata is invalid"
            )
        post_root = root / "mfspart-post-refinement"
        post_report = read_json(
            _require(post_root, "post_refinement.json")
        )
        post_schema = post_report.get("schema")
        if (
            post_schema not in {
                "emuflow.mfspart-post-refinement/v1",
                "emuflow.mfspart-post-refinement/v2",
                "emuflow.mfspart-post-refinement/v3",
            }
            or post_report.get("status") != "pass"
            or post_report.get("direction_source")
            != "EmuIR net drivers/sinks"
            or post_report.get("early_stop")
            != report.get("mfspart_post_refinement_early_stop")
            or post_report.get("bottleneck_beta")
            != report.get("mfspart_post_refinement_bottleneck_beta")
            or post_report.get("timing_path_beta", 0.0)
            != report.get("mfspart_post_refinement_timing_path_beta", 0.0)
        ):
            raise ValidationError("partition MFSPart post-refinement report is invalid")
        if post_schema in {
            "emuflow.mfspart-post-refinement/v2",
            "emuflow.mfspart-post-refinement/v3",
        }:
            expected_guard = (
                "non-combinational-net-worst-sink-distance-non-regression-v1"
                if report.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
                != CUT_MODE_SEQUENTIAL_ONLY
                else "not-requested"
            )
            if (
                post_report.get("topology_guard") != expected_guard
                or not isinstance(post_report.get("guarded_nets"), int)
                or isinstance(post_report.get("guarded_nets"), bool)
                or post_report.get("guarded_nets") < 0
                or post_metadata.get("topology_guard") != expected_guard
            ):
                raise ValidationError(
                    "partition MFSPart topology-guard evidence is invalid"
                )
        refinement = post_report.get("refinement")
        if not isinstance(refinement, dict):
            raise ValidationError(
                "partition MFSPart post-refinement certificate is missing"
            )
        artifacts = refinement.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValidationError(
                "partition MFSPart post-refinement artifact seals are missing"
            )
        native_paths = {
            "input": _require(post_root, "mfspart_refiner.in"),
            "output": _require(post_root, "mfspart_refiner.out"),
            "checker_output": _require(post_root, "mfspart_refiner.check"),
        }
        for label, path in native_paths.items():
            if artifacts.get(label) != path.name:
                raise ValidationError(
                    f"partition MFSPart post-refinement {label} path is invalid"
                )
        certificate = validate_mfspart_native_certificate(
            native_paths["input"], native_paths["output"]
        )
        if post_schema in {
            "emuflow.mfspart-post-refinement/v2",
            "emuflow.mfspart-post-refinement/v3",
        }:
            input_evidence = certificate["input_evidence"]
            if (
                post_report.get("guarded_nets")
                != input_evidence["guarded_nets"]
                or post_report.get("combinational_candidate_nets")
                != input_evidence["zero_bottleneck_nets"]
                or post_metadata.get("guarded_nets")
                != input_evidence["guarded_nets"]
                or post_metadata.get("combinational_candidate_nets")
                != input_evidence["zero_bottleneck_nets"]
            ):
                raise ValidationError(
                    "partition MFSPart topology-guard counts disagree with "
                    "the independently checked native input"
                )
        if post_schema == "emuflow.mfspart-post-refinement/v3":
            input_evidence = certificate["input_evidence"]
            objective = post_report.get("timing_path_objective")
            reconstructed_objective = _rebuild_timing_path_objective(
                ir_path,
                seals["clusters_sha256"],
                timing_path_database,
            )
            if (
                not isinstance(objective, dict)
                or "timing_path_database_sha256" not in report
                or objective
                != {
                    **reconstructed_objective,
                    "status": "enabled",
                    "beta": post_report.get("timing_path_beta"),
                }
                or input_evidence["timing_paths"]
                != reconstructed_objective["compressed_groups"]
                or input_evidence["timing_path_pins"]
                != reconstructed_objective["compressed_pins"]
                or input_evidence["timing_path_objective_sha256"]
                != reconstructed_objective["objective_sha256"]
            ):
                raise ValidationError(
                    "partition MFSPart timing-path objective evidence is invalid"
                )
        parsed = certificate["parsed"]
        for label in ("moves", "assignment", "metrics"):
            if refinement.get(label) != parsed[label]:
                raise ValidationError(
                    "partition MFSPart post-refinement JSON differs from "
                    "the independently checked native certificate"
                )
        clusters = sorted(
            read_json(_require(root, "clusters.json"))["clusters"],
            key=lambda item: item["id"],
        )
        parts = post_report.get("parts")
        if not isinstance(parts, list) or any(
            not isinstance(part, str) for part in parts
        ):
            raise ValidationError("partition MFSPart FPGA order is invalid")
        try:
            expected_cluster_assignment = {
                cluster["id"]: parts[parsed["assignment"][index]]
                for index, cluster in enumerate(clusters)
            }
        except (IndexError, KeyError, TypeError) as error:
            raise ValidationError(
                "partition MFSPart kept-prefix assignment is invalid"
            ) from error
        if assignment.get("cluster_assignment") != expected_cluster_assignment:
            raise ValidationError(
                "partition assignment differs from MFSPart kept prefix"
            )
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
    minimum_combinational_cut_nets = report.get(
        "minimum_combinational_cut_nets", 0
    )
    if (
        isinstance(minimum_combinational_cut_nets, bool)
        or not isinstance(minimum_combinational_cut_nets, int)
        or minimum_combinational_cut_nets < 0
    ):
        raise ValidationError(
            "partition minimum-combinational-cut-nets contract is invalid"
        )
    if minimum_combinational_cut_nets and report.get(
        "cut_mode", CUT_MODE_SEQUENTIAL_ONLY
    ) == CUT_MODE_SEQUENTIAL_ONLY:
        raise ValidationError(
            "partition minimum combinational cut nets requires static exact mode"
        )
    actual_combinational_cut_nets = checked.get("combinational_cut_nets", 0)
    if (
        isinstance(actual_combinational_cut_nets, bool)
        or not isinstance(actual_combinational_cut_nets, int)
        or actual_combinational_cut_nets < minimum_combinational_cut_nets
    ):
        raise ValidationError(
            "partition exact-cut evidence selected fewer combinational cut "
            "nets than required"
        )
    expected_exercised = (
        report.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
        != CUT_MODE_SEQUENTIAL_ONLY
        and actual_combinational_cut_nets > 0
    )
    if (
        "static_exact_combinational_cut_exercised" in report
        and report["static_exact_combinational_cut_exercised"] != expected_exercised
    ):
        raise ValidationError(
            "partition static-exact exercise status disagrees with assignment"
        )
    cut_policy = read_json(seals["clusters_sha256"]).get("policy", {})
    independently_reconstructed = {
        "cut_mode": cut_policy.get(
            "cut_mode", CUT_MODE_SEQUENTIAL_ONLY
        ),
        "max_cross_fpga_dependency_depth": cut_policy.get(
            "max_cross_fpga_dependency_depth", 1
        ),
        "comb_segment_budget_slots": cut_policy.get(
            "comb_segment_budget_slots", 1
        ),
        "static_exact_candidate_policy": cut_policy.get(
            "candidate_selection_policy",
            STATIC_EXACT_CANDIDATE_FRONTIER_V1,
        ),
    }
    for field, actual in independently_reconstructed.items():
        if report.get(field, legacy_cut_defaults[field]) != actual:
            raise ValidationError(
                f"partition {field.replace('_', '-')} seal disagrees with "
                "the independently validated assignment"
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
        **checked,
    }
