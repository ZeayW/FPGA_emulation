from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .io import read_json, write_json
from .ir import EmuIR
from .partition import (
    CUT_MODE_SEQUENTIAL_ONLY,
    assign_clusters,
    build_clusters,
    build_partition_assignment,
    load_partition_constraints,
    validate_partition_artifacts,
)
from .errors import ValidationError
from .partition_hops import refine_partition_hops
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
from .mfspart_provider import run_mfspart
from .tritonpart import load_partition_net_weights, run_tritonpart
from .routing import load_route_constraints


PHASE3_REPORT_SCHEMA = "emuflow.phase3-report/v1"


def _rebase_patron_initial_assignment(
    ir: EmuIR,
    platform: Platform,
    clusters: Dict[str, Any],
    constraints: Dict[str, Any],
    frozen: Dict[str, Any],
) -> Dict[str, Any]:
    """Re-express a frozen instance placement using the current clusters.

    Content-addressed experiment reuse may import an assignment produced by an
    older, semantically compatible cluster-ID scheme.  PATRON must preserve the
    actual instance placement, not require those incidental IDs to match.  A
    current cluster is accepted only when all of its instances already occupy
    one FPGA in the frozen assignment; this never invents or changes a move.
    """

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
    cut_mode: str = CUT_MODE_SEQUENTIAL_ONLY,
    max_cross_fpga_dependency_depth: int = 1,
    comb_segment_budget_slots: int = 1,
    timing_database_path: Optional[Path] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_flow_refinement: bool = False,
    patron_initial_assignment_path: Optional[Path] = None,
    patron_physical_system_timing_path: Optional[Path] = None,
    patron_physical_feedback_scale: float = 0.0,
) -> Dict[str, Any]:
    if not isinstance(patron_flow_refinement, bool):
        raise ValidationError("PATRON flow refinement flag is invalid")
    if patron_flow_refinement and provider != "patron":
        raise ValidationError(
            "PATRON flow refinement requires provider='patron'"
        )
    if patron_physical_system_timing_path is not None and (
        provider != "patron"
        or not patron_flow_refinement
        or patron_initial_assignment_path is None
        or patron_physical_feedback_scale <= 0.0
    ):
        raise ValidationError(
            "PATRON physical feedback requires provider='patron', flow "
            "refinement, a frozen initial assignment, and a positive scale"
        )
    if (
        patron_physical_system_timing_path is None
        and patron_physical_feedback_scale != 0.0
    ):
        raise ValidationError(
            "PATRON physical feedback scale has no system timing source"
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
    if provider == "patron" and cut_mode != CUT_MODE_SEQUENTIAL_ONLY:
        raise ValidationError(
            "PATRON does not yet consume static exact dependency constraints"
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
            physical_feedback=patron_physical_feedback,
            physical_feedback_scale=patron_physical_feedback_scale,
        )
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
    validation = validate_partition_artifacts(
        ir,
        platform,
        clusters,
        assignment,
    )
    report: Dict[str, Any] = {
        "schema": PHASE3_REPORT_SCHEMA,
        "phase": 3,
        "status": "pass",
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": assignment["provider"],
        "seed": assignment["seed"],
        "validation": validation,
        "hop_refinement": hop_refinement,
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
        report["qualification"] = "partition-legality-only-provisional"
        report["artifacts"]["semantic_contract"] = (
            "assignment.json#/semantic_contract"
        )
    if provider == "tritonpart":
        report["artifacts"]["tritonpart"] = "tritonpart/tritonpart_input.json"
    elif provider in {"repart", "repart-replication"}:
        report["artifacts"]["repart"] = "repart/repart_input.json"
        if provider == "repart-replication":
            report["artifacts"]["replication"] = "replication.json"
    elif provider == "mfspart":
        report["artifacts"]["mfspart"] = "mfspart/hierarchy.json"
    elif provider == "patron":
        report["algorithm_validation"] = patron_validation
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
        if patron_physical_system_timing_path is not None:
            report["artifacts"]["patron_physical_feedback"] = (
                "patron/physical_feedback.json"
            )
            report["artifacts"][
                "patron_physical_feedback_source_assignment"
            ] = "patron/physical_feedback_source_assignment.json"
        if patron_initial_assignment_path is None:
            report["artifacts"]["tritonpart"] = (
                "patron/tritonpart/tritonpart_input.json"
            )
    if hop_refinement["enabled"]:
        report["artifacts"]["hop_refinement"] = (
            "hop-refinement/hop_refinement.json"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "clusters.json", clusters)
    write_json(output_dir / "constraints.normalized.json", constraints)
    write_json(output_dir / "assignment.json", assignment)
    if "replication" in assignment:
        write_json(output_dir / "replication.json", assignment["replication"])
    if hop_refinement["enabled"]:
        write_json(
            output_dir / "hop-refinement" / "hop_refinement.json",
            hop_refinement,
        )
    write_json(output_dir / "phase3_report.json", report)
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
