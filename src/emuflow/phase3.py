from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .io import read_json, write_json
from .ir import EmuIR
from .partition import (
    CUT_MODE_SEQUENTIAL_ONLY,
    CUT_MODE_STATIC_EXACT,
    assign_clusters,
    build_clusters,
    load_partition_constraints,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from .partition_hops import refine_partition_hops
from .platform import Platform
from .repart import run_repart
from .mfspart_provider import refine_mfspart_partition, run_mfspart
from .mfspart_refine import DEFAULT_BOTTLENECK_BETA, DEFAULT_TIMING_PATH_BETA
from .sta import validate_sta_path_database
from .tritonpart import load_partition_net_weights, run_tritonpart
from .routing import load_route_constraints
from .combinational_cut import (
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)


PHASE3_REPORT_SCHEMA = "emuflow.phase3-report/v1"


def _mfspart_report_summary(report: Optional[Dict[str, Any]]) -> Any:
    if report is None:
        return None
    refinement = report["refinement"]
    return {
        key: value
        for key, value in report.items()
        if key != "refinement"
    } | {
        "refinement": {
            "schema": refinement["schema"],
            "provider": refinement["provider"],
            "claim_scope": refinement["claim_scope"],
            "metrics": refinement["metrics"],
            "validation": refinement["validation"],
            "runtime": refinement.get("runtime"),
            "artifacts": refinement["artifacts"],
        }
    }


def _hop_report_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in report.items()
        if key not in {"moves", "input"}
    } | {"move_count": len(report.get("moves", []))}


def run_phase3(
    ir_path: Path,
    platform_path: Path,
    output_dir: Path,
    constraints_path: Optional[Path] = None,
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    provider: str = "tritonpart",
    openroad: Optional[str] = None,
    tritonpart_solution: Optional[Path] = None,
    net_weights_path: Optional[Path] = None,
    tritonpart_timeout_seconds: int = 3600,
    tritonpart_seed_attempts: int = 1,
    tritonpart_num_initial_solutions: int = 50,
    tritonpart_num_best_initial_solutions: int = 10,
    tritonpart_repair_min_used_fpgas: bool = False,
    tritonpart_repair_balance: bool = False,
    repart: Optional[str] = None,
    repart_solution: Optional[Path] = None,
    repart_timeout_seconds: int = 3600,
    route_constraints_path: Optional[Path] = None,
    hop_refiner: Optional[str] = None,
    mfspart_coarsener: Optional[str] = None,
    mfspart_initializer: Optional[str] = None,
    mfspart_refiner: Optional[str] = None,
    mfspart_refiner_checker: Optional[str] = None,
    mfspart_legalizer: Optional[str] = None,
    mfspart_post_refinement: Optional[bool] = None,
    mfspart_post_refinement_early_stop: int = 1000,
    mfspart_post_refinement_bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    timing_path_database_path: Optional[Path] = None,
    mfspart_post_refinement_timing_path_beta: float = DEFAULT_TIMING_PATH_BETA,
    cut_mode: str = CUT_MODE_SEQUENTIAL_ONLY,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    comb_segment_budget_slots: int = 1,
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    if mfspart_post_refinement is None:
        mfspart_post_refinement = (
            cut_mode == CUT_MODE_STATIC_EXACT and provider == "tritonpart"
        )
    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    constraints = load_partition_constraints(
        constraints_path,
        ir,
        platform,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
    )
    route_constraints = load_route_constraints(
        route_constraints_path, platform
    )
    clusters = build_clusters(
        ir,
        constraints,
        cut_mode=cut_mode,
        max_cross_fpga_dependency_depth=(
            max_cross_fpga_dependency_depth
        ),
        comb_segment_budget_slots=comb_segment_budget_slots,
        frame_slots=route_constraints["frame_slots"],
        static_exact_candidate_policy=static_exact_candidate_policy,
    )
    if provider == "greedy":
        assignment = assign_clusters(
            ir,
            platform,
            clusters,
            constraints,
            seed=seed,
            route_constraints=route_constraints,
        )
    elif provider == "tritonpart":
        assignment = run_tritonpart(
            ir,
            platform,
            clusters,
            constraints,
            output_dir / "tritonpart",
            seed=seed,
            executable=openroad,
            solution_input=tritonpart_solution,
            net_weights=load_partition_net_weights(net_weights_path),
            timeout_seconds=tritonpart_timeout_seconds,
            seed_attempts=tritonpart_seed_attempts,
            num_initial_solutions=tritonpart_num_initial_solutions,
            num_best_initial_solutions=(
                tritonpart_num_best_initial_solutions
            ),
            repair_min_used_fpgas=tritonpart_repair_min_used_fpgas,
            repair_balance=tritonpart_repair_balance,
            persist_input_manifest=not managed_dag_node,
        )
    elif provider in {"repart", "repart-replication"}:
        assignment = run_repart(
            ir,
            platform,
            clusters,
            constraints,
            output_dir / "repart",
            executable=repart,
            solution_input=repart_solution,
            net_weights_path=net_weights_path,
            timeout_seconds=repart_timeout_seconds,
            enable_replication=provider == "repart-replication",
        )
    elif provider == "mfspart":
        assignment = run_mfspart(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            output_dir / "mfspart",
            seed=seed,
            net_weights=load_partition_net_weights(net_weights_path),
            coarsener=mfspart_coarsener,
            initializer=mfspart_initializer,
            refiner=mfspart_refiner,
            refiner_checker=mfspart_refiner_checker,
            legalizer=mfspart_legalizer,
        )
    else:
        raise ValueError(
            f"unknown Phase 3 provider {provider!r}; "
            "expected 'repart-replication', 'repart', 'tritonpart', "
            "'mfspart', or 'greedy'"
        )
    mfspart_post_refinement_report = None
    if mfspart_post_refinement:
        if provider != "tritonpart":
            raise ValueError(
                "MFSPart post-refinement requires provider='tritonpart'"
            )
        if timing_path_database_path is not None and not managed_dag_node:
            validate_sta_path_database(timing_path_database_path, ir_path)
        assignment, mfspart_post_refinement_report = refine_mfspart_partition(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            assignment,
            output_dir / "mfspart-post-refinement",
            net_weights=load_partition_net_weights(net_weights_path),
            early_stop=mfspart_post_refinement_early_stop,
            bottleneck_beta=mfspart_post_refinement_bottleneck_beta,
            timing_path_database_path=timing_path_database_path,
            timing_path_beta=mfspart_post_refinement_timing_path_beta,
            refiner=mfspart_refiner,
            refiner_checker=mfspart_refiner_checker,
            defer_semantic_contract=True,
            online_validation=True,
        )
    assignment, hop_refinement = refine_partition_hops(
        ir,
        platform,
        clusters,
        constraints,
        assignment,
        output_dir / "hop-refinement",
        route_constraints_path=route_constraints_path,
        net_weights_path=net_weights_path,
        executable=hop_refiner,
    )
    validation = (
        validate_partition_artifacts_online(platform, clusters, assignment)
        if managed_dag_node
        else validate_partition_artifacts(ir, platform, clusters, assignment)
    )
    persisted_hop_refinement = _hop_report_summary(hop_refinement)
    report: Dict[str, Any] = {
        "schema": PHASE3_REPORT_SCHEMA,
        "phase": 3,
        "status": "pass",
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": assignment["provider"],
        "seed": assignment["seed"],
        "validation": validation,
        "hop_refinement": persisted_hop_refinement,
        "mfspart_post_refinement": _mfspart_report_summary(
            mfspart_post_refinement_report
        ),
        "partitions": [
            {
                key: value
                for key, value in partition.items()
                if key != "clusters"
            }
            for partition in assignment["partitions"]
        ],
        "artifacts": {
            "clusters": "clusters.json",
            "constraints": "constraints.normalized.json",
            "assignment": "assignment.json",
            "report": "phase3_report.json",
        },
    }
    if cut_mode != CUT_MODE_SEQUENTIAL_ONLY:
        report["cut_mode"] = cut_mode
        report["static_exact_candidate_policy"] = (
            static_exact_candidate_policy
        )
        report["qualification"] = "partition-legality-only-provisional"
        report["artifacts"]["semantic_contract"] = (
            "assignment.json#/semantic_contract"
        )
    if provider == "tritonpart":
        if not managed_dag_node:
            report["artifacts"]["tritonpart"] = (
                "tritonpart/tritonpart_input.json"
            )
        if mfspart_post_refinement_report is not None:
            report["artifacts"]["mfspart_post_refinement"] = (
                "mfspart-post-refinement/post_refinement.json"
            )
    elif provider in {"repart", "repart-replication"}:
        report["artifacts"]["repart"] = "repart/repart_input.json"
        if provider == "repart-replication":
            report["artifacts"]["replication"] = "replication.json"
    elif provider == "mfspart":
        report["artifacts"]["mfspart"] = "mfspart/hierarchy.json"
    if hop_refinement["enabled"]:
        report["artifacts"]["hop_refinement"] = (
            "hop-refinement/hop_refinement.json"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    if managed_dag_node and provider == "tritonpart":
        shutil.rmtree(output_dir / "tritonpart", ignore_errors=True)
    if managed_dag_node and hop_refinement["enabled"]:
        shutil.rmtree(output_dir / "hop-refinement", ignore_errors=True)
    write_json(output_dir / "clusters.json", clusters, compact=True)
    write_json(output_dir / "constraints.normalized.json", constraints)
    write_json(output_dir / "assignment.json", assignment, compact=True)
    if "replication" in assignment:
        write_json(output_dir / "replication.json", assignment["replication"])
    if hop_refinement["enabled"]:
        write_json(
            output_dir / "hop-refinement" / "hop_refinement.json",
            persisted_hop_refinement,
            compact=True,
        )
    write_json(output_dir / "phase3_report.json", report, compact=True)
    return report


def validate_phase3(
    ir_path: Path,
    platform_path: Path,
    clusters_path: Path,
    assignment_path: Path,
) -> Dict[str, Any]:
    return validate_partition_artifacts(
        EmuIR.load(ir_path),
        Platform.load(platform_path),
        read_json(clusters_path),
        read_json(assignment_path),
    )
