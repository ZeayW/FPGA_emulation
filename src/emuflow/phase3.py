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
    build_partition_assignment,
    load_partition_constraints,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from .errors import ValidationError
from .partition_hops import refine_partition_hops, validate_assignment_hops
from .partition_pressure import (
    build_partition_pressure_model,
    run_partition_pressure_native,
    validate_partition_pressure_native_bundle,
)
from .partition_physical_feedback import (
    build_partition_physical_feedback,
    validate_partition_physical_feedback,
)
from .platform import Platform
from .repart import run_repart
from .mfspart_provider import refine_mfspart_partition, run_mfspart
from .mfspart_refine import DEFAULT_BOTTLENECK_BETA, DEFAULT_TIMING_PATH_BETA
from .sta import validate_sta_path_database
from .tritonpart import load_partition_net_weights, run_tritonpart
from .routing import load_route_constraints
from .combinational_cut import (
    STATIC_EXACT_CANDIDATE_FRONTIER_V1,
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
)
from .phase3_storage import pack_phase3_assignment, pack_phase3_clusters


PHASE3_REPORT_SCHEMA = "emuflow.phase3-report/v1"


def _validate_reused_patron_clusters(
    ir: EmuIR,
    clusters: Dict[str, Any],
    *,
    cut_mode: str,
    max_cross_fpga_dependency_depth: int,
    comb_segment_budget_slots: int,
    frame_slots: int,
    static_exact_candidate_policy: str,
) -> None:
    """Check the cheap contract for a validated managed cluster checkpoint."""

    if clusters.get("schema") != "emuflow.clusters/v1":
        raise ValidationError("PATRON initial clusters schema is invalid")
    if clusters.get("design") != ir.value["design"]["name"]:
        raise ValidationError("PATRON initial clusters design disagrees")
    if clusters.get("instances") != len(ir.value["instances"]):
        raise ValidationError("PATRON initial clusters instance count disagrees")
    policy = clusters.get("policy")
    if not isinstance(policy, dict):
        raise ValidationError("PATRON initial clusters policy is invalid")
    actual_cut_mode = policy.get("cut_mode", CUT_MODE_SEQUENTIAL_ONLY)
    if actual_cut_mode != cut_mode:
        raise ValidationError("PATRON initial clusters cut mode disagrees")
    if cut_mode == CUT_MODE_STATIC_EXACT:
        expected = {
            "max_cross_fpga_dependency_depth": (
                max_cross_fpga_dependency_depth
            ),
            "comb_segment_budget_slots": comb_segment_budget_slots,
            "frame_slots": frame_slots,
            "candidate_selection_policy": static_exact_candidate_policy,
        }
        for field, value in expected.items():
            actual = policy.get(field)
            if field == "candidate_selection_policy" and actual is None:
                actual = STATIC_EXACT_CANDIDATE_FRONTIER_V1
            if actual != value:
                raise ValidationError(
                    f"PATRON initial clusters {field} disagrees"
                )


def _rebase_patron_initial_assignment(
    ir: EmuIR,
    platform: Platform,
    clusters: Dict[str, Any],
    constraints: Dict[str, Any],
    frozen: Dict[str, Any],
) -> Dict[str, Any]:
    """Re-express a frozen instance placement using the current clusters."""

    raw = frozen.get("instance_assignment")
    if not isinstance(raw, dict):
        raise ValidationError(
            "PATRON frozen assignment requires instance_assignment"
        )
    instance_ids = {instance["id"] for instance in ir.value["instances"]}
    if set(raw) != instance_ids:
        raise ValidationError(
            "PATRON frozen assignment has incompatible instance coverage"
        )
    fpga_ids = {fpga.id for fpga in platform.fpgas}
    unknown = sorted(set(raw.values()) - fpga_ids)
    if unknown:
        raise ValidationError(
            f"PATRON frozen assignment references unknown FPGAs {unknown}"
        )
    if frozen.get("constraints") != constraints:
        raise ValidationError(
            "PATRON frozen assignment constraints disagree with the current "
            "Phase 3 contract"
        )

    cluster_assignment: Dict[str, str] = {}
    split_clusters = []
    for cluster in clusters["clusters"]:
        targets = {raw[instance] for instance in cluster["instances"]}
        if len(targets) != 1:
            split_clusters.append(cluster["id"])
            continue
        cluster_assignment[cluster["id"]] = next(iter(targets))
    if split_clusters:
        raise ValidationError(
            "PATRON frozen instance placement splits current clusters; "
            f"clusters={split_clusters[:8]}"
        )

    frozen_cluster_assignment = frozen.get("cluster_assignment")
    if (
        isinstance(frozen_cluster_assignment, dict)
        and frozen_cluster_assignment == cluster_assignment
    ):
        # The overwhelmingly common managed-flow case reuses the exact
        # clustering that produced the frozen assignment.  Rebuilding the
        # complete assignment here would duplicate every instance mapping,
        # cut-net record, and Static Exact semantic-contract record merely to
        # recover identical bytes.  The online validator independently checks
        # cluster coverage, resource bounds, and assignment consistency.
        candidate = dict(frozen)
        validate_partition_artifacts_online(platform, clusters, candidate)
        return candidate

    rebased = build_partition_assignment(
        ir,
        platform,
        clusters,
        constraints,
        cluster_assignment,
        provider=str(frozen.get("provider", "frozen-partition-v1")),
        seed=int(frozen.get("seed", 0)),
    )
    if rebased["instance_assignment"] != raw:
        raise ValidationError(
            "PATRON frozen assignment rebase changed instance placement"
        )
    validate_partition_artifacts(ir, platform, clusters, rebased)
    return rebased


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


def _patron_hop_audit_report(
    platform: Platform,
    assignment: Dict[str, Any],
    route_constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Audit PATRON's native hop contract without another optimizer pass."""

    audit = validate_assignment_hops(
        platform, assignment, route_constraints
    )
    return {
        "schema": "emuflow.hop-partition-refinement/v1",
        "status": "pass",
        "enabled": False,
        "reason": "patron-native-hop-contract-audited",
        "algorithm": "patron-native-hop-contract-v1",
        "before": audit,
        "after": audit,
        "moves": [],
    }


def run_phase3(
    ir_path: Path,
    platform_path: Path,
    output_dir: Path,
    constraints_path: Optional[Path] = None,
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    provider: str = "patron",
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
    cut_mode: str = CUT_MODE_STATIC_EXACT,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    comb_segment_budget_slots: int = 1,
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    timing_database_path: Optional[Path] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_flow_refinement: bool = False,
    patron_algorithm_version: int = 6,
    patron_initial_assignment_path: Optional[Path] = None,
    patron_initial_clusters_path: Optional[Path] = None,
    patron_physical_system_timing_path: Optional[Path] = None,
    patron_physical_feedback_scale: float = 0.0,
    managed_dag_node: bool = False,
) -> Dict[str, Any]:
    if patron_initial_clusters_path is not None and (
        provider != "patron"
        or patron_initial_assignment_path is None
        or not managed_dag_node
    ):
        raise ValidationError(
            "PATRON initial clusters require a managed PATRON DAG node and "
            "a frozen initial assignment"
        )
    if not isinstance(patron_flow_refinement, bool):
        raise ValidationError("PATRON flow refinement flag is invalid")
    if (
        isinstance(patron_algorithm_version, bool)
        or not isinstance(patron_algorithm_version, int)
        or patron_algorithm_version not in {6, 9, 10, 11, 12}
    ):
        raise ValidationError("PATRON algorithm version is invalid")
    if patron_physical_system_timing_path is not None:
        if patron_algorithm_version == 6 and patron_flow_refinement:
            patron_algorithm_version = 11
    elif patron_algorithm_version == 6 and patron_flow_refinement:
        patron_algorithm_version = 10
    patron_flow_refinement = patron_algorithm_version != 6
    if (
        patron_algorithm_version == 12
        and cut_mode != CUT_MODE_STATIC_EXACT
    ):
        raise ValidationError(
            "PATRON v12 requires generalized Static Exact mode"
        )
    if patron_flow_refinement and provider != "patron":
        raise ValidationError(
            "PATRON flow refinement requires provider='patron'"
        )
    if patron_physical_system_timing_path is not None and (
        provider != "patron"
        or patron_algorithm_version != 11
        or patron_initial_assignment_path is None
        or patron_physical_feedback_scale <= 0.0
    ):
        raise ValidationError(
            "PATRON physical feedback requires provider='patron', algorithm "
            "v11, a frozen initial assignment, and a positive scale"
        )
    if (
        patron_algorithm_version == 11
        and patron_physical_system_timing_path is None
    ):
        raise ValidationError("PATRON v11 requires physical system timing")
    if (
        patron_physical_system_timing_path is None
        and patron_physical_feedback_scale != 0.0
    ):
        raise ValidationError(
            "PATRON physical feedback scale has no system timing source"
        )
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
    if patron_initial_clusters_path is None:
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
    else:
        clusters = read_json(patron_initial_clusters_path)
        _validate_reused_patron_clusters(
            ir,
            clusters,
            cut_mode=cut_mode,
            max_cross_fpga_dependency_depth=(
                max_cross_fpga_dependency_depth
            ),
            comb_segment_budget_slots=comb_segment_budget_slots,
            frame_slots=route_constraints["frame_slots"],
            static_exact_candidate_policy=static_exact_candidate_policy,
        )
    patron_validation = None
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
    elif provider == "patron":
        if timing_database_path is None:
            raise ValueError(
                "PATRON Phase 3 requires a complete TimingPathDB"
            )
        if patron_initial_assignment_path is None:
            initial = run_tritonpart(
                ir,
                platform,
                clusters,
                constraints,
                output_dir / "patron" / "tritonpart",
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
        else:
            initial = _rebase_patron_initial_assignment(
                ir,
                platform,
                clusters,
                constraints,
                read_json(patron_initial_assignment_path),
            )
        patron_feedback_source_assignment = initial
        if patron_initial_assignment_path is not None:
            # A frozen assignment is an exact PATRON input, not a request to
            # silently optimize it again. Fail closed if it is not already
            # hop-feasible. Every arm can therefore reuse the one validated
            # TritonPart plus hop initializer checkpoint.
            patron_initial_hop = _patron_hop_audit_report(
                platform, initial, route_constraints
            )
        else:
            initial, patron_initial_hop = refine_partition_hops(
                ir,
                platform,
                clusters,
                constraints,
                initial,
                output_dir / "patron" / "initial-hop-refinement",
                route_constraints_path=route_constraints_path,
                net_weights_path=net_weights_path,
                executable=hop_refiner,
            )
        timing_database = read_json(timing_database_path)
        model = build_partition_pressure_model(
            ir,
            platform,
            clusters,
            constraints,
            timing_database,
            route_constraints,
            independent_validation=not managed_dag_node,
        )
        patron_physical_feedback = None
        patron_physical_feedback_validation = None
        if patron_physical_system_timing_path is not None:
            patron_physical_system_timing = read_json(
                patron_physical_system_timing_path
            )
            patron_physical_feedback = build_partition_physical_feedback(
                model,
                patron_feedback_source_assignment,
                patron_physical_system_timing,
            )
            patron_physical_feedback_validation = (
                validate_partition_physical_feedback(
                    model,
                    patron_feedback_source_assignment,
                    patron_physical_system_timing,
                    patron_physical_feedback,
                )
            )
        assignment, patron_trace = run_partition_pressure_native(
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=patron_refiner,
            max_moves=patron_max_moves,
            flow_refinement=patron_flow_refinement,
            algorithm_version=patron_algorithm_version,
            physical_feedback=patron_physical_feedback,
            physical_feedback_scale=patron_physical_feedback_scale,
            output_validation=("caller" if managed_dag_node else "full"),
            retain_trace_seals=not managed_dag_node,
        )
        if not managed_dag_node:
            write_json(output_dir / "patron" / "pressure_model.json", model)
            write_json(
                output_dir / "patron" / "refinement_trace.json", patron_trace
            )
            write_json(
                output_dir / "patron" / "initial_assignment.json", initial
            )
            write_json(
                output_dir / "patron" / "candidate_assignment.json",
                assignment,
            )
            write_json(
                output_dir / "patron" / "initial_hop_refinement.json",
                patron_initial_hop,
            )
            if patron_physical_feedback is not None:
                write_json(
                    output_dir / "patron" / "physical_feedback.json",
                    patron_physical_feedback,
                )
                write_json(
                    output_dir
                    / "patron"
                    / "physical_feedback_source_assignment.json",
                    patron_feedback_source_assignment,
                )
            patron_validation = validate_partition_pressure_native_bundle(
                ir,
                platform,
                clusters,
                constraints,
                timing_database,
                route_constraints,
                model,
                initial,
                assignment,
                patron_trace,
                patron_physical_feedback,
                patron_physical_feedback_scale,
            )
        else:
            initial_validation = validate_partition_artifacts_online(
                platform, clusters, initial
            )
            candidate_validation = validate_partition_artifacts_online(
                platform, clusters, assignment
            )
            moves = patron_trace.get("moves")
            batches = patron_trace.get("batches")
            if not isinstance(moves, list) or not isinstance(batches, list):
                raise ValidationError("managed PATRON trace shape is invalid")
            patron_validation = {
                "status": "pass",
                "mode": patron_trace.get("mode"),
                "qualification": "managed-native-output-contract",
                "deep_replay": "deferred-to-offline-qualification",
                "initial_assignment": initial_validation,
                "candidate_assignment": candidate_validation,
                "model": {
                    "clusters": len(model.get("clusters", [])),
                    "nets": len(model.get("nets", [])),
                    "paths": len(model.get("paths", [])),
                    "capacity_domains": len(model.get("capacities", [])),
                },
                "moves": len(moves),
                "batches": len(batches),
            }
        if patron_physical_feedback_validation is not None:
            patron_validation["physical_feedback"] = (
                patron_physical_feedback_validation
            )
    else:
        raise ValueError(
            f"unknown Phase 3 provider {provider!r}; "
            "expected 'repart-replication', 'repart', 'tritonpart', "
            "'mfspart', 'patron', or 'greedy'"
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
    if provider == "patron":
        # PATRON already evaluates reachability and max-hop legality for every
        # native candidate. A second topology FM pass duplicates a large input
        # and obscures the exact assignment PATRON selected.
        hop_refinement = _patron_hop_audit_report(
            platform, assignment, route_constraints
        )
    else:
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
        "patron_initial_clusters_reused": (
            patron_initial_clusters_path is not None
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
    elif provider == "patron":
        report["patron_algorithm_version"] = patron_algorithm_version
        report["algorithm_validation"] = patron_validation
        if not managed_dag_node:
            report["artifacts"].update(
                {
                    "patron_model": "patron/pressure_model.json",
                    "patron_trace": "patron/refinement_trace.json",
                    "patron_initial_assignment": (
                        "patron/initial_assignment.json"
                    ),
                    "patron_candidate_assignment": (
                        "patron/candidate_assignment.json"
                    ),
                    "patron_initial_hop_refinement": (
                        "patron/initial_hop_refinement.json"
                    ),
                }
            )
        else:
            report["patron_diagnostics"] = {
                "storage": "not-persisted",
                "reason": "managed-production-hot-path",
                "offline_qualification": "available-via-standalone-phase3",
            }
        if (
            patron_physical_system_timing_path is not None
            and not managed_dag_node
        ):
            report["artifacts"]["patron_physical_feedback"] = (
                "patron/physical_feedback.json"
            )
            report["artifacts"][
                "patron_physical_feedback_source_assignment"
            ] = "patron/physical_feedback_source_assignment.json"
        if patron_initial_assignment_path is None and not managed_dag_node:
            report["artifacts"]["tritonpart"] = (
                "patron/tritonpart/tritonpart_input.json"
            )
    if hop_refinement["enabled"]:
        report["artifacts"]["hop_refinement"] = (
            "hop-refinement/hop_refinement.json"
        )
    if managed_dag_node:
        report["artifact_storage"] = {
            "clusters": "emuflow.phase3-clusters-storage/v1",
            "assignment": "emuflow.phase3-assignment-storage/v1",
            "logical_schema": "unchanged-transparent-expansion",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    if managed_dag_node and provider == "tritonpart":
        shutil.rmtree(output_dir / "tritonpart", ignore_errors=True)
    if managed_dag_node and provider == "patron":
        shutil.rmtree(output_dir / "patron", ignore_errors=True)
    if managed_dag_node and hop_refinement["enabled"]:
        shutil.rmtree(output_dir / "hop-refinement", ignore_errors=True)
    persisted_clusters = (
        pack_phase3_clusters(clusters) if managed_dag_node else clusters
    )
    persisted_assignment = (
        pack_phase3_assignment(assignment, clusters)
        if managed_dag_node
        else assignment
    )
    write_json(
        output_dir / "clusters.json", persisted_clusters, compact=True
    )
    write_json(output_dir / "constraints.normalized.json", constraints)
    write_json(
        output_dir / "assignment.json", persisted_assignment, compact=True
    )
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


def promote_patron_baseline(
    ir_path: Path,
    platform_path: Path,
    phase3_root: Path,
) -> Dict[str, Any]:
    """Restore PATRON's frozen TritonPart+hop candidate after exact rejection."""

    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    clusters = read_json(phase3_root / "clusters.json")
    initial = read_json(phase3_root / "patron/initial_assignment.json")
    validation = validate_partition_artifacts(ir, platform, clusters, initial)
    hop_refinement = read_json(
        phase3_root / "patron/initial_hop_refinement.json"
    )
    report = read_json(phase3_root / "phase3_report.json")
    report.update(
        {
            "provider": initial["provider"],
            "seed": initial["seed"],
            "validation": validation,
            "hop_refinement": hop_refinement,
            "partitions": [
                {
                    key: value
                    for key, value in partition.items()
                    if key != "clusters"
                }
                for partition in initial["partitions"]
            ],
        }
    )
    report.pop("algorithm_validation", None)
    report["artifacts"] = {
        "clusters": "clusters.json",
        "constraints": "constraints.normalized.json",
        "assignment": "assignment.json",
        "report": "phase3_report.json",
        **(
            {
                "hop_refinement": (
                    "patron/initial_hop_refinement.json"
                )
            }
            if hop_refinement["enabled"]
            else {}
        ),
        **(
            {"tritonpart": "patron/tritonpart/tritonpart_input.json"}
            if (
                phase3_root
                / "patron/tritonpart/tritonpart_input.json"
            ).is_file()
            else {}
        ),
    }
    write_json(phase3_root / "assignment.json", initial)
    write_json(phase3_root / "phase3_report.json", report)
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
