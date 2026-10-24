from __future__ import annotations

import gc
import math
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .io import read_json, write_json
from .ir import EmuIR
from .partition import (
    DEFAULT_PARTITION_CUT_MODE,
    DEFAULT_PATRON_ALGORITHM_VERSION,
    CUT_MODE_SEQUENTIAL_ONLY,
    CUT_MODE_STATIC_EXACT,
    _PackedClusterAssignment,
    build_clusters,
    build_partition_assignment,
    load_partition_constraints,
    summarize_balance_observation,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from .errors import ValidationError
from .native_tools import is_unified_native_executable
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
from .partition_limits import planning_resource_capacities, strict_balance_limits
from .resource_budget import validate_device_resource_usage
from .resources import RESOURCE_FIELDS
from .tritonpart import (
    load_partition_net_weights,
    run_tritonpart,
)
from .routing import load_route_constraints
from .combinational_cut import (
    STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH,
    static_exact_analysis_engine,
)
from .phase3_storage import (
    INDEXED_CLUSTERS_SCHEMA,
    PACKED_ASSIGNMENT_SCHEMA,
    pack_phase3_assignment,
    write_indexed_phase3_clusters,
)
from .phase3_assignment_index import (
    ASSIGNMENT_INDEX_SCHEMA,
    CUT_RECORDS_SCHEMA,
    validate_phase3_assignment_index_against_emuir,
    write_phase3_assignment_index,
)
from .phase3_exact_binary import EXACT_BINARY_ARTIFACT, EXACT_BINARY_SCHEMA
from .phase1 import indexed_phase1_identity
from .partition_ir import load_partition_ir
from .phase3_runtime import (
    measure_phase3_runtime,
    phase3_runtime_call,
    phase3_runtime_observe,
)


PHASE3_REPORT_SCHEMA = "emuflow.phase3-report/v1"
PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER = (
    "patron-path-objective-selection-v2"
)


def _select_patron_timing_assignment(
    initial: Dict[str, Any],
    candidate: Dict[str, Any],
    patron_trace: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Select a legal PATRON candidate using its Phase-3 path objective.

    Exact cut dependencies, schedule readiness, and physical timing are not
    knowable in Phase 3.  They are materialized and checked by the common
    Phase 4--7 timing contract after the assignment has been selected.
    """

    initial_metrics = patron_trace.get("initial_metrics", {})
    candidate_metrics = patron_trace.get("final_metrics", {})
    initial_objective = initial_metrics.get("objective_key")
    candidate_objective = candidate_metrics.get("objective_key")
    initial_path_rank = (
        initial_metrics.get("maximum_path_hops"),
        initial_metrics.get("maximum_path_tdm_ratio_sum"),
    )
    candidate_path_rank = (
        candidate_metrics.get("maximum_path_hops"),
        candidate_metrics.get("maximum_path_tdm_ratio_sum"),
    )
    if (
        not isinstance(initial_objective, list)
        or not isinstance(candidate_objective, list)
        or len(initial_objective) != 8
        or len(candidate_objective) != 8
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            and value >= 0
            for value in initial_path_rank + candidate_path_rank
        )
        or not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in initial_objective + candidate_objective
        )
    ):
        raise ValidationError("PATRON v14 timing objective certificate is invalid")
    initial_rank = initial_path_rank + tuple(
        round(float(value) / 1.0e-9) if index < 2 else round(float(value))
        for index, value in enumerate(initial_objective)
    )
    candidate_rank = candidate_path_rank + tuple(
        round(float(value) / 1.0e-9) if index < 2 else round(float(value))
        for index, value in enumerate(candidate_objective)
    )
    assignment_changed = candidate.get("cluster_assignment") != initial.get(
        "cluster_assignment"
    )
    timing_improved = candidate_rank < initial_rank
    # Both assignments have already passed the scheduler-independent Phase 3
    # legality gate.  PATRON first ranks the maximum physical-hop count over
    # the supplied critical paths, then the maximum per-path sum of crossing
    # net TDM ratios.  Generic timing, load, transition, and cut metrics are
    # lower-priority tie breakers; no downstream schedule certificate
    # participates in this choice.
    accepted = assignment_changed and timing_improved
    selection = {
        "status": "pass",
        "provider": PATRON_PATH_OBJECTIVE_SELECTION_PROVIDER,
        "policy": "path-hop-then-maximum-path-ratio-sum-v1",
        "objective_fields": [
            "maximum_path_hops",
            "maximum_path_tdm_ratio_sum",
            "negative_worst_normalized_slack",
            "negative_total_negative_normalized_slack",
            "negative_paths",
            "maximum_predicted_tdm_ratio",
            "maximum_capacity_domain_load",
            "total_path_partition_transitions",
            "total_bit_hops",
            "cut_bits",
        ],
        "initial_timing_rank": list(initial_rank),
        "candidate_timing_rank": list(candidate_rank),
        "assignment_changed": assignment_changed,
        "timing_improved": timing_improved,
        "selected": "candidate" if accepted else "initial",
        "candidate_provider": candidate.get("provider"),
        "initial_provider": initial.get("provider"),
    }
    return (candidate if accepted else initial), selection


def _validate_reused_patron_clusters(
    ir: EmuIR,
    clusters: Dict[str, Any],
    *,
    cut_mode: str,
    max_cross_fpga_dependency_depth: int,
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
            "candidate_selection_policy": static_exact_candidate_policy,
        }
        for field, value in expected.items():
            actual = policy.get(field)
            if field == "candidate_selection_policy" and actual is None:
                actual = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY
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
    *,
    include_semantic_contract: bool = True,
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
        provider_metadata=frozen.get("provider_metadata"),
        _include_semantic_contract=include_semantic_contract,
    )
    if rebased["instance_assignment"] != raw:
        raise ValidationError(
            "PATRON frozen assignment rebase changed instance placement"
        )
    if include_semantic_contract:
        validate_partition_artifacts(ir, platform, clusters, rebased)
    else:
        validate_partition_artifacts_online(platform, clusters, rebased)
    return rebased


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


def _single_active_hop_audit_report(
    platform: Platform,
    assignment: Dict[str, Any],
    route_constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Audit hop legality when capacity selection leaves one target."""

    audit = validate_assignment_hops(
        platform, assignment, route_constraints
    )
    return {
        "schema": "emuflow.hop-partition-refinement/v1",
        "status": "pass",
        "enabled": False,
        "reason": "single-active-fpga-has-no-partition-hop-decision",
        "algorithm": "single-active-fpga-hop-audit-v1",
        "before": audit,
        "after": audit,
        "moves": [],
    }


@measure_phase3_runtime
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
    tritonpart_repair_min_used_fpgas: bool = True,
    tritonpart_run_unweighted_baseline: bool = False,
    route_constraints_path: Optional[Path] = None,
    hop_refiner: Optional[str] = None,
    cut_mode: str = DEFAULT_PARTITION_CUT_MODE,
    max_cross_fpga_dependency_depth: int = (
        STATIC_EXACT_DEFAULT_MAX_DEPENDENCY_DEPTH
    ),
    static_exact_candidate_policy: str = STATIC_EXACT_DEFAULT_CANDIDATE_POLICY,
    timing_database_path: Optional[Path] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_algorithm_version: int = DEFAULT_PATRON_ALGORITHM_VERSION,
    patron_initial_assignment_path: Optional[Path] = None,
    patron_initial_clusters_path: Optional[Path] = None,
    patron_physical_system_timing_path: Optional[Path] = None,
    patron_physical_feedback_scale: float = 0.0,
    retain_diagnostics: bool = False,
    retain_patron_baseline: bool = False,
) -> Dict[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("partition seed: expected a non-negative integer")
    if provider not in {"patron", "tritonpart"}:
        raise ValidationError(f"unsupported Phase 3 provider {provider!r}")
    if patron_initial_clusters_path is not None and (
        provider != "patron"
        or patron_initial_assignment_path is None
    ):
        raise ValidationError(
            "PATRON initial clusters require provider='patron' and a frozen "
            "initial assignment"
        )
    if not isinstance(retain_diagnostics, bool):
        raise ValidationError("Phase 3 retain_diagnostics flag is invalid")
    if not isinstance(retain_patron_baseline, bool):
        raise ValidationError("Phase 3 retain_patron_baseline flag is invalid")
    if retain_patron_baseline and provider != "patron":
        raise ValidationError(
            "Phase 3 retain_patron_baseline requires provider='patron'"
        )
    if (
        isinstance(patron_algorithm_version, bool)
        or not isinstance(patron_algorithm_version, int)
        or patron_algorithm_version not in {6, 9, 10, 11, 14}
    ):
        raise ValidationError("PATRON algorithm version is invalid")
    patron_flow_refinement = provider == "patron" and patron_algorithm_version != 6

    if (
        provider == "patron"
        and patron_algorithm_version == 14
        and cut_mode != CUT_MODE_STATIC_EXACT
    ):
        raise ValidationError(
            "PATRON v14 requires generalized Static Exact mode"
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
    # Explicit qualification mode may request full canonical source seals and
    # replay artifacts.  Production keeps only the partition facts resident.
    ir = phase3_runtime_call(
        "load_partition_ir",
        EmuIR.load if retain_diagnostics else load_partition_ir,
        ir_path,
    )
    platform = phase3_runtime_call("load_platform", Platform.load, platform_path)
    constraints = phase3_runtime_call(
        "load_partition_constraints",
        load_partition_constraints,
        constraints_path,
        ir,
        platform,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
    )
    route_constraints = phase3_runtime_call(
        "load_route_constraints",
        load_route_constraints,
        route_constraints_path,
        platform,
    )
    if patron_initial_clusters_path is None:
        clusters = phase3_runtime_call(
            "build_clusters",
            build_clusters,
            ir,
            constraints,
            cut_mode=cut_mode,
            max_cross_fpga_dependency_depth=(
                max_cross_fpga_dependency_depth
            ),
            static_exact_candidate_policy=static_exact_candidate_policy,
            _stream_eligible_policy_names=True,
            _native_compact_clusters=(
                not retain_diagnostics
                and (patron_refiner is None or is_unified_native_executable(patron_refiner))
            ),
        )
    else:
        clusters = phase3_runtime_call(
            "load_reused_clusters", read_json, patron_initial_clusters_path
        )
        phase3_runtime_call(
            "validate_reused_clusters",
            _validate_reused_patron_clusters,
            ir,
            clusters,
            cut_mode=cut_mode,
            max_cross_fpga_dependency_depth=(
                max_cross_fpga_dependency_depth
            ),
            static_exact_candidate_policy=static_exact_candidate_policy,
        )
    phase3_runtime_observe(
        instances=int(clusters["instances"]),
        clusters=len(clusters["clusters"]),
        active_fpgas=len(constraints["active_fpgas"]),
    )
    cluster_implementation = getattr(clusters["clusters"], "implementation_id", None)
    packed_cluster_payload_bytes = getattr(
        clusters["clusters"], "packed_payload_bytes", None
    )
    if packed_cluster_payload_bytes is not None:
        phase3_runtime_observe(
            cluster_compact_payload_bytes=int(packed_cluster_payload_bytes)
        )
    if cut_mode == CUT_MODE_STATIC_EXACT:
        phase3_runtime_observe(
            static_exact_native=int(
                static_exact_analysis_engine(len(ir.value["instances"]))
                == "native-cpp-v1"
            )
        )
    patron_validation = None
    patron_initialization = None
    patron_initial_derived_records_deferred = False
    single_active_selection = len(constraints["active_fpgas"]) == 1
    if single_active_selection:
        assignment = phase3_runtime_call(
            "provider.single_active_assignment",
            build_partition_assignment,
            ir,
            platform,
            clusters,
            constraints,
            _PackedClusterAssignment(
                (constraints["active_fpgas"][0],),
                (0 for _ in range(len(clusters["clusters"]))),
            ),
            provider="single-active-fpga-capacity-selection-v1",
            seed=seed,
            _include_semantic_contract=retain_diagnostics,
            _stream_semantic_records=not retain_diagnostics,
        )
        assignment["provider"] = "single-active-fpga-capacity-selection-v1"
        assignment["provider_metadata"] = {
            "requested_provider": provider,
            "reason": "no partition decision exists with one active target",
            "active_fpgas": list(constraints["active_fpgas"]),
        }
    elif provider == "tritonpart":
        tritonpart_assignment = phase3_runtime_call(
            "provider.tritonpart",
            run_tritonpart,
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
            run_unweighted_baseline=tritonpart_run_unweighted_baseline,
            defer_semantic_contract=not retain_diagnostics,
            persist_input_manifest=retain_diagnostics,
        )
        assignment = tritonpart_assignment
    elif provider == "patron":
        if timing_database_path is None:
            raise ValueError(
                "PATRON Phase 3 requires a complete TimingPathDB"
            )
        if patron_initial_assignment_path is None:
            # Start PATRON from an ordinary balanced TritonPart solution on
            # the same generalized clusters and legal net-cut hypergraph.
            # Transport and timing are PATRON objectives; duplicating every
            # driver-sink pair into the initializer hypergraph distorts the
            # native partitioner and can destroy multidimensional balance.
            if tritonpart_solution is None:
                patron_initialization = (
                    "native-generalized-tritonpart-net-cut-v5"
                )
            else:
                patron_initialization = (
                    "caller-supplied-generalized-tritonpart-solution-v1"
                )
            patron_initial_derived_records_deferred = (
                not retain_diagnostics
                and cut_mode == CUT_MODE_STATIC_EXACT
                and route_constraints.get("max_route_hops") is None
            )
            tritonpart_initial = phase3_runtime_call(
                "patron.initialize_tritonpart",
                run_tritonpart,
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
                run_unweighted_baseline=(
                    tritonpart_run_unweighted_baseline
                ),
                defer_semantic_contract=True,
                _defer_derived_records=(
                    patron_initial_derived_records_deferred
                ),
                persist_input_manifest=retain_diagnostics,
            )
            initial = tritonpart_initial
        else:
            patron_initialization = "caller-supplied-frozen-assignment-v1"
            initial = phase3_runtime_call(
                "patron.load_frozen_assignment",
                _rebase_patron_initial_assignment,
                ir,
                platform,
                clusters,
                constraints,
                read_json(patron_initial_assignment_path),
                include_semantic_contract=(patron_algorithm_version != 14),
            )
        patron_feedback_source_assignment = initial
        phase3_runtime_observe(
            patron_initial_derived_records_deferred=int(
                patron_initial_derived_records_deferred
            )
        )
        if patron_initial_assignment_path is not None:
            # A frozen assignment is an exact PATRON input, not a request to
            # silently optimize it again. Fail closed if it is not already
            # hop-feasible. Every arm can therefore reuse the one validated
            # TritonPart plus hop initializer checkpoint.
            patron_initial_hop = phase3_runtime_call(
                "patron.audit_initial_hops",
                _patron_hop_audit_report,
                platform,
                initial,
                route_constraints,
            )
        else:
            initial, patron_initial_hop = phase3_runtime_call(
                "patron.refine_initial_hops",
                refine_partition_hops,
                ir,
                platform,
                clusters,
                constraints,
                initial,
                output_dir / "patron" / "initial-hop-refinement",
                route_constraints_path=route_constraints_path,
                net_weights_path=net_weights_path,
                executable=hop_refiner,
                defer_semantic_contract=True,
            )
        timing_database = phase3_runtime_call(
            "patron.load_timing_database", read_json, timing_database_path
        )
        from .phase3_pressure_native import supports_native_pressure_model
        native_store_model = (
            not retain_diagnostics and patron_initial_clusters_path is None
            and patron_algorithm_version == 14
            and patron_physical_system_timing_path is None
            and (patron_refiner is None or is_unified_native_executable(patron_refiner))
            and not os.environ.get("EMUFLOW_PATRON_INPUT_COPY")
            and supports_native_pressure_model(ir, clusters, timing_database)
        )
        model = phase3_runtime_call(
            "patron.build_pressure_model",
            build_partition_pressure_model,
            ir,
            platform,
            clusters,
            constraints,
            timing_database,
            route_constraints,
            independent_validation=retain_diagnostics,
            timing_database_path=timing_database_path,
            native_store_model=native_store_model,
        )
        patron_model_counts = {
            "clusters": len(model.get("clusters", [])),
            "nets": len(model.get("nets", [])),
            "paths": len(model.get("paths", [])),
            "capacity_domains": len(model.get("capacities", [])),
        }
        if not native_store_model:
            phase3_runtime_observe(
                pressure_clusters=patron_model_counts["clusters"],
                pressure_nets=patron_model_counts["nets"],
                pressure_paths=patron_model_counts["paths"],
            )
        if hasattr(model.get("nets"), "payload_bytes"):
            phase3_runtime_observe(
                # Packed net identity/member/ordinal storage only.  This does
                # not include final cluster/path records or Python overhead.
                pressure_compact_net_payload_bytes=model["nets"].payload_bytes,
                pressure_construction_identity_bytes=(
                    model["nets"].construction_identity_bytes
                ),
            )
        if hasattr(model.get("paths"), "payload_bytes"):
            phase3_runtime_observe(
                pressure_compact_path_payload_bytes=(
                    model["paths"].payload_bytes
                ),
                pressure_indexed_sql_projection=int(
                    bool(
                        getattr(
                            model["paths"],
                            "indexed_sql_projection",
                            False,
                        )
                    )
                ),
            )
        patron_physical_feedback = None
        patron_physical_feedback_validation = None
        if patron_physical_system_timing_path is not None:
            patron_physical_system_timing = phase3_runtime_call(
                "patron.load_physical_feedback",
                read_json,
                patron_physical_system_timing_path,
            )
            patron_physical_feedback = phase3_runtime_call(
                "patron.build_physical_feedback",
                build_partition_physical_feedback,
                model,
                patron_feedback_source_assignment,
                patron_physical_system_timing,
            )
            patron_physical_feedback_validation = phase3_runtime_call(
                "patron.validate_physical_feedback",
                validate_partition_physical_feedback,
                model,
                patron_feedback_source_assignment,
                patron_physical_system_timing,
                patron_physical_feedback,
            )
        assignment, patron_trace = phase3_runtime_call(
            "patron.native_refinement",
            run_partition_pressure_native,
            ir,
            platform,
            clusters,
            constraints,
            route_constraints,
            model,
            initial,
            executable=patron_refiner,
            max_moves=patron_max_moves,
            algorithm_version=patron_algorithm_version,
            physical_feedback=patron_physical_feedback,
            physical_feedback_scale=patron_physical_feedback_scale,
            output_validation=("full" if retain_diagnostics else "caller"),
            retain_trace_seals=retain_diagnostics,
            stream_semantic_records=not retain_diagnostics,
            timeout_seconds=tritonpart_timeout_seconds,
            _release_model_before_assignment=not retain_diagnostics,
            _defer_candidate_derived_records=(
                not retain_diagnostics
                and cut_mode == CUT_MODE_STATIC_EXACT
            ),
        )
        if native_store_model:
            if patron_trace.get("model_implementation_id") != model.implementation_id:
                raise ValidationError("PATRON native model implementation disagrees")
            patron_model_counts = patron_trace["model_counts"]
            phase3_runtime_observe(pressure_clusters=patron_model_counts["clusters"],
                pressure_nets=patron_model_counts["nets"], pressure_paths=patron_model_counts["paths"])
        if retain_diagnostics:
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
            patron_validation = phase3_runtime_call(
                "patron.validate_diagnostic_bundle",
                validate_partition_pressure_native_bundle,
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
                timing_database_path=timing_database_path,
            )
        else:
            initial_validation = phase3_runtime_call(
                "patron.validate_initial_assignment",
                validate_partition_artifacts_online,
                platform,
                clusters,
                initial,
            )
            candidate_validation = phase3_runtime_call(
                "patron.validate_candidate_assignment",
                validate_partition_artifacts_online,
                platform,
                clusters,
                assignment,
            )
            move_count = patron_trace.get("move_count")
            batch_count = patron_trace.get("batch_count")
            change_count = patron_trace.get("change_count")
            native_input_transport = patron_trace.get(
                "native_input_transport"
            )
            native_input_bytes = patron_trace.get("native_input_bytes")
            native_input_records = patron_trace.get("native_input_records")
            native_input_crc32 = patron_trace.get("native_input_crc32")
            native_output_bytes = patron_trace.get("native_output_bytes")
            if (
                isinstance(move_count, bool)
                or not isinstance(move_count, int)
                or move_count < 0
                or isinstance(batch_count, bool)
                or not isinstance(batch_count, int)
                or batch_count < 0
                or isinstance(change_count, bool)
                or not isinstance(change_count, int)
                or change_count < 0
                or native_input_transport
                not in {"typed-binary-pipe", "atomic-binary-file", "native-primary-store-model-v2"}
                or isinstance(native_input_bytes, bool)
                or not isinstance(native_input_bytes, int)
                or native_input_bytes <= 0
                or isinstance(native_input_records, bool)
                or not isinstance(native_input_records, int)
                or native_input_records <= 0
                or (
                    native_input_transport in {"typed-binary-pipe", "atomic-binary-file"}
                    and (
                        isinstance(native_input_crc32, bool)
                        or not isinstance(native_input_crc32, int)
                        or native_input_crc32 < 0
                        or native_input_crc32 > 0xFFFFFFFF
                    )
                )
                or isinstance(native_output_bytes, bool)
                or not isinstance(native_output_bytes, int)
                or native_output_bytes <= 0
                or patron_trace.get("trace_storage") != "summary-only"
            ):
                raise ValidationError("production PATRON summary is invalid")
            patron_validation = {
                "status": "pass",
                "mode": patron_trace.get("mode"),
                "qualification": "online-native-output-contract",
                "deep_replay": "deferred-to-offline-qualification",
                "initial_assignment": initial_validation,
                "candidate_assignment": candidate_validation,
                "model": patron_model_counts,
                "moves": move_count,
                "batches": batch_count,
                "batch_changes": change_count,
                "native_input_transport": native_input_transport,
                "native_input_bytes": native_input_bytes,
                "native_input_records": native_input_records,
                "native_input_crc32": native_input_crc32,
                "native_output_bytes": native_output_bytes,
            }
            if native_store_model:
                patron_validation["model_implementation_id"] = patron_trace["model_implementation_id"]
                patron_validation["native_model_metrics"] = patron_trace["native_model_metrics"]
        if patron_physical_feedback_validation is not None:
            patron_validation["physical_feedback"] = (
                patron_physical_feedback_validation
            )
        if patron_algorithm_version == 14:
            assignment, candidate_selection = phase3_runtime_call(
                "patron.select_candidate",
                _select_patron_timing_assignment,
                initial,
                assignment,
                patron_trace,
            )
            patron_validation["candidate_selection"] = candidate_selection
        if not retain_diagnostics:
            # The pressure graph, timing source and native trace are no longer
            # inputs after online validation and v14 candidate selection.  In
            # production they used to remain live while the final Static Exact
            # semantic contract and assignment index built their own graph-scale
            # state.  Release that complete lifetime before either publication
            # stage begins; diagnostics retain it explicitly for offline replay.
            del model
            del timing_database
            del patron_trace
            del patron_physical_feedback
            del patron_feedback_source_assignment
            if not retain_patron_baseline:
                del initial
                del patron_initial_hop
            phase3_runtime_call(
                "patron.release_post_selection_state", gc.collect
            )
            phase3_runtime_observe(
                pressure_model_released_before_candidate_assignment=1,
                patron_candidate_derived_records_deferred=int(
                    cut_mode == CUT_MODE_STATIC_EXACT
                ),
            )
    else:
        raise ValueError(
            f"unknown Phase 3 provider {provider!r}; "
            "expected 'tritonpart' or 'patron'"
        )
    if single_active_selection:
        hop_refinement = phase3_runtime_call(
            "final_hops.audit_single_active",
            _single_active_hop_audit_report,
            platform,
            assignment,
            route_constraints,
        )
    elif provider == "patron":
        if (
            cut_mode == CUT_MODE_STATIC_EXACT
            and assignment.get("semantic_contract") is None
        ):
            # Native v14 selection is complete and the pressure graph plus
            # unselected candidate have already been released.  Build the
            # cut relation and Static Exact contract exactly once for the
            # selected assignment before the independent hop audit consumes
            # those cut records.
            assignment = phase3_runtime_call(
                "static_exact.build_semantic_contract",
                build_partition_assignment,
                ir,
                platform,
                clusters,
                constraints,
                assignment["cluster_assignment"],
                provider=str(assignment["provider"]),
                seed=int(assignment["seed"]),
                provider_metadata=assignment.get("provider_metadata"),
                _stream_semantic_records=not retain_diagnostics,
            )
        # PATRON already evaluates reachability and max-hop legality for every
        # native candidate. A second topology FM pass duplicates a large input
        # and obscures the exact assignment PATRON selected.
        hop_refinement = phase3_runtime_call(
            "final_hops.audit_patron",
            _patron_hop_audit_report,
            platform,
            assignment,
            route_constraints,
        )
    else:
        assignment, hop_refinement = phase3_runtime_call(
            "final_hops.refine",
            refine_partition_hops,
            ir,
            platform,
            clusters,
            constraints,
            assignment,
            output_dir / "hop-refinement",
            route_constraints_path=route_constraints_path,
            net_weights_path=net_weights_path,
            executable=hop_refiner,
            defer_semantic_contract=True,
        )
    if (
        cut_mode == CUT_MODE_STATIC_EXACT
        and assignment.get("semantic_contract") is None
    ):
        # PATRON selection, optional MFSPart post-refinement, and hop repair
        # can all change the cluster assignment.  Keep the large downstream
        # contract out of those hot paths and construct it exactly once from
        # the final Phase-3 assignment.  Constructing it before the last
        # refinement silently left Static Exact cuts without dependency and
        # logic-segment semantics in Phase 4/5.
        assignment = phase3_runtime_call(
            "static_exact.build_semantic_contract",
            build_partition_assignment,
            ir,
            platform,
            clusters,
            constraints,
            assignment["cluster_assignment"],
            provider=str(assignment["provider"]),
            seed=int(assignment["seed"]),
            provider_metadata=assignment.get("provider_metadata"),
            _stream_semantic_records=not retain_diagnostics,
        )
    if retain_diagnostics:
        validation = phase3_runtime_call(
            "validate_final_assignment_full",
            validate_partition_artifacts,
            ir,
            platform,
            clusters,
            assignment,
        )
    else:
        validation = phase3_runtime_call(
            "validate_final_assignment_online",
            validate_partition_artifacts_online,
            platform,
            clusters,
            assignment,
        )
    phase3_runtime_observe(
        cut_nets=len(assignment.get("cut_nets", [])),
        partitions=len(assignment.get("partitions", [])),
    )
    persisted_hop_refinement = _hop_report_summary(hop_refinement)
    report: Dict[str, Any] = {
        "schema": PHASE3_REPORT_SCHEMA,
        "phase": 3,
        "status": "pass",
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": assignment["provider"],
        "requested_provider": provider,
        "seed": assignment["seed"],
        "validation": validation,
        "hop_refinement": persisted_hop_refinement,
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
            "assignment_index": "assignment.sqlite",
            "report": "phase3_report.json",
            "runtime_breakdown": "phase3-runtime.json",
        },
    }
    if cluster_implementation is not None:
        report["cluster_implementation_id"] = cluster_implementation
        report["native_cluster_metrics"] = clusters["clusters"].native_metrics
    candidate_implementation = getattr(clusters["clusters"], "candidate_implementation_id", None)
    if candidate_implementation is not None:
        report["candidate_implementation_id"] = candidate_implementation
        report["native_candidate_metrics"] = clusters["clusters"].candidate_native_metrics
    if cut_mode != CUT_MODE_SEQUENTIAL_ONLY:
        report["cut_mode"] = cut_mode
        report["static_exact_candidate_policy"] = (
            static_exact_candidate_policy
        )
        report["qualification"] = "structural-partition-legality"
        report["artifacts"]["semantic_contract"] = (
            "assignment.json#/semantic_contract"
        )
        report["artifacts"]["semantic_contract_data"] = EXACT_BINARY_ARTIFACT
    if single_active_selection:
        report["partition_decision"] = {
            "executed": False,
            "reason": "one-active-assignment-target",
            "requested_provider": provider,
        }
    elif provider == "tritonpart":
        if retain_diagnostics:
            report["artifacts"]["tritonpart"] = (
                "tritonpart/tritonpart_input.json"
            )
    elif provider == "patron":
        report["patron_algorithm_version"] = patron_algorithm_version
        report["patron_initialization"] = patron_initialization
        report["algorithm_validation"] = patron_validation
        if retain_diagnostics:
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
        elif retain_patron_baseline:
            report["artifacts"].update(
                {
                    "patron_initial_assignment": (
                        "patron-initial-assignment.json"
                    ),
                    "patron_initial_hop_refinement": (
                        "patron-initial-hop-refinement.json"
                    ),
                }
            )
        else:
            report["patron_diagnostics"] = {
                "storage": "not-persisted",
                "reason": "production-hot-path",
                "offline_qualification": (
                    "available-via-partition-pressure-reference"
                ),
            }
        if (
            patron_physical_system_timing_path is not None
            and retain_diagnostics
        ):
            report["artifacts"]["patron_physical_feedback"] = (
                "patron/physical_feedback.json"
            )
            report["artifacts"][
                "patron_physical_feedback_source_assignment"
            ] = "patron/physical_feedback_source_assignment.json"
        if patron_initial_assignment_path is None and retain_diagnostics:
            report["artifacts"]["tritonpart"] = (
                "patron/tritonpart/tritonpart_input.json"
            )
    if hop_refinement["enabled"]:
        report["artifacts"]["hop_refinement"] = (
            "hop-refinement/hop_refinement.json"
        )
    if not retain_diagnostics:
        report["artifact_storage"] = {
            "clusters": INDEXED_CLUSTERS_SCHEMA,
            "assignment": PACKED_ASSIGNMENT_SCHEMA,
            "assignment_index": ASSIGNMENT_INDEX_SCHEMA,
            "static_exact_data": EXACT_BINARY_SCHEMA,
            "logical_schema": "unchanged-transparent-expansion",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    if not retain_diagnostics and provider == "tritonpart":
        shutil.rmtree(output_dir / "tritonpart", ignore_errors=True)
    if not retain_diagnostics and provider == "patron":
        shutil.rmtree(output_dir / "patron", ignore_errors=True)
    if not retain_diagnostics and hop_refinement["enabled"]:
        shutil.rmtree(output_dir / "hop-refinement", ignore_errors=True)
    indexed_clusters = None
    if retain_diagnostics:
        phase3_runtime_call(
            "publish.write_clusters",
            write_json,
            output_dir / "clusters.json",
            clusters,
            compact=True,
        )
    else:
        indexed_clusters = phase3_runtime_call(
            "publish.write_clusters",
            write_indexed_phase3_clusters,
            output_dir / "clusters.json",
            clusters,
        )
        report["artifacts"]["clusters_data"] = indexed_clusters["artifact"]
    phase3_runtime_call(
        "publish.write_constraints",
        write_json,
        output_dir / "constraints.normalized.json",
        constraints,
    )
    # Publish the typed semantic relation before the lightweight JSON
    # descriptor that names it.  A failed index write can therefore never
    # leave a completed assignment document pointing at a missing sidecar.
    assignment_index_summary = phase3_runtime_call(
        "publish.write_assignment_index",
        write_phase3_assignment_index,
        output_dir / "assignment.sqlite",
        ir,
        assignment,
    )
    persisted_assignment = (
        assignment
        if retain_diagnostics
        else phase3_runtime_call(
            "publish.pack_assignment",
            pack_phase3_assignment,
            assignment,
            clusters,
            cut_nets_index={
                "artifact": "assignment.sqlite",
                "schema": CUT_RECORDS_SCHEMA,
                "records": assignment_index_summary["cut_records"],
                "sha256": assignment_index_summary["cut_records_sha256"],
            },
        )
    )
    phase3_runtime_call(
        "publish.write_assignment",
        write_json,
        output_dir / "assignment.json",
        persisted_assignment,
        compact=True,
    )
    del persisted_assignment
    if (
        provider == "patron"
        and retain_patron_baseline
        and not single_active_selection
    ):
        write_json(
            output_dir / "patron-initial-assignment.json",
            pack_phase3_assignment(initial, clusters),
            compact=True,
        )
        write_json(
            output_dir / "patron-initial-hop-refinement.json",
            patron_initial_hop,
            compact=True,
        )
    if "replication" in assignment:
        write_json(output_dir / "replication.json", assignment["replication"])
    if hop_refinement["enabled"]:
        write_json(
            output_dir / "hop-refinement" / "hop_refinement.json",
            persisted_hop_refinement,
            compact=True,
        )
    phase3_runtime_observe(
        clusters_artifact_bytes=(output_dir / "clusters.json").stat().st_size,
        clusters_data_bytes=(
            (output_dir / indexed_clusters["artifact"]).stat().st_size
            if indexed_clusters is not None
            else 0
        ),
        assignment_artifact_bytes=(output_dir / "assignment.json").stat().st_size,
        assignment_index_bytes=(output_dir / "assignment.sqlite").stat().st_size,
        static_exact_binary_bytes=(
            (output_dir / EXACT_BINARY_ARTIFACT).stat().st_size
            if (output_dir / EXACT_BINARY_ARTIFACT).is_file()
            else 0
        ),
    )
    phase3_runtime_call(
        "publish.write_report",
        write_json,
        output_dir / "phase3_report.json",
        report,
        compact=True,
    )
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
    report = read_json(phase3_root / "phase3_report.json")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValidationError("PATRON Phase 3 artifact map is invalid")
    initial_artifact = artifacts.get("patron_initial_assignment")
    hop_artifact = artifacts.get("patron_initial_hop_refinement")
    if not isinstance(initial_artifact, str) or not isinstance(
        hop_artifact, str
    ):
        raise ValidationError("PATRON baseline artifacts are unavailable")
    initial = read_json(phase3_root / initial_artifact)
    validation = validate_partition_artifacts(ir, platform, clusters, initial)
    hop_refinement = read_json(phase3_root / hop_artifact)
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
            {"hop_refinement": hop_artifact}
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
    write_json(
        phase3_root / "assignment.json",
        pack_phase3_assignment(initial, clusters),
        compact=True,
    )
    write_json(phase3_root / "phase3_report.json", report, compact=True)
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


def validate_phase3_indexed(
    ir_path: Path,
    platform_path: Path,
    phase3_report_path: Path,
    assignment_index_path: Path,
) -> Dict[str, Any]:
    """Validate a completed Phase 3 without replaying its Python graph.

    The Phase 3 producer already performs the exhaustive in-process artifact
    validation before publication.  A complete-flow terminal check needs an
    independent relation check, not a second construction of every EmuIR,
    cluster, endpoint, and cut object.  The assignment sidecar and compact
    EmuIR make that relation check set-oriented and bounded-memory.
    """

    platform = Platform.load(platform_path)
    design, _, _ = indexed_phase1_identity(ir_path)
    proof = validate_phase3_assignment_index_against_emuir(
        assignment_index_path,
        ir_path,
        expected_design=design,
        expected_platform=platform.name,
    )
    report = read_json(phase3_report_path)
    if (
        report.get("schema") != PHASE3_REPORT_SCHEMA
        or report.get("status") != "pass"
        or report.get("design") != design
        or report.get("platform") != platform.name
    ):
        raise ValidationError("Phase 3 report identity is invalid")
    validation = report.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "pass":
        raise ValidationError("Phase 3 report lacks passing validation")
    if validation.get("instances") != proof["source_instances"]:
        raise ValidationError("Phase 3 report instance count disagrees")
    resource_field = (
        "primary_resources_by_fpga"
        if "primary_resources_by_fpga" in validation
        else "resources_by_fpga"
    )
    if validation.get(resource_field) != proof["resources_by_fpga"]:
        raise ValidationError("Phase 3 report resource totals disagree")
    constraints = read_json(phase3_report_path.parent / "constraints.normalized.json")
    if proof.get("constraints") != constraints:
        raise ValidationError("indexed Phase 3 sealed constraints disagree or are missing")
    active = constraints.get("active_fpgas")
    fpgas = [fpga for fpga in platform.fpgas if fpga.id in (active or [])]
    if not fpgas or len(fpgas) != len(set(active)):
        raise ValidationError("indexed Phase 3 active FPGA set is invalid")
    for fpga in platform.fpgas:
        validate_device_resource_usage(
            proof["total_resources_by_fpga"][fpga.id], fpga.to_dict(), stage="indexed-phase3",
        )
    fields = ["cells", *[field for field in RESOURCE_FIELDS
        if any(counts.get(field, 0) for counts in proof["resources_by_fpga"].values())
        and all(fpga.effective_capacity.get(field, 0) > 0 for fpga in fpgas)]]
    loads = {fpga.id: {field: (proof["instances_by_fpga"][fpga.id] if field == "cells"
                              else proof["resources_by_fpga"][fpga.id].get(field, 0))
                       for field in fields} for fpga in fpgas}
    if any(proof["instances_by_fpga"].get(fpga.id, 0) for fpga in platform.fpgas if fpga.id not in active):
        raise ValidationError("indexed Phase 3 assignment uses inactive FPGA")
    totals = {field: sum(row[field] for row in loads.values()) for field in fields}
    tolerance = constraints["balance_tolerance"]
    overrides = constraints.get("balance_tolerance_by_dimension", {})
    strict_dimensions = set(
        constraints.get("balance_dimensions", fields)
    ) & set(fields)
    bounds = strict_balance_limits(
        fpgas,
        totals,
        tolerance,
        overrides,
        strict_dimensions,
        constraints.get("resource_reservations_by_fpga", {}),
    )
    expected_modes = {
        field: ("balance" if field in strict_dimensions else "capacity")
        for field in fields
    }
    for fpga in loads:
        for field in fields:
            if loads[fpga][field] > bounds[fpga][field]:
                mode = expected_modes[field]
                raise ValidationError(
                    f"indexed Phase 3 strict {mode} exceeded: {fpga} "
                    f"{field} used={loads[fpga][field]} "
                    f"allowed={bounds[fpga][field]}; automatic relaxation "
                    "is forbidden"
                )
    percentages = {field: overrides.get(field, tolerance) * 100.0 for field in fields}
    planning_capacity = planning_resource_capacities(
        fpgas, constraints.get("resource_reservations_by_fpga", {})
    )
    expected_capacity_limits = {
        fpga.id: {
            field: (
                totals[field]
                if field == "cells"
                else planning_capacity[fpga.id][field]
            )
            for field in fields
        }
        for fpga in fpgas
    }
    reservation_evidence_disagrees = bool(
        constraints.get("resource_reservations_by_fpga")
        and (
            validation.get("physical_capacity_limits")
            != {
                fpga.id: {
                    field: (
                        totals[field]
                        if field == "cells"
                        else fpga.effective_capacity[field]
                    )
                    for field in fields
                }
                for fpga in fpgas
            }
            or validation.get("resource_reservations_by_fpga")
            != {
                fpga.id: dict(
                    sorted(
                        constraints.get(
                            "resource_reservations_by_fpga", {}
                        ).get(fpga.id, {}).items()
                    )
                )
                for fpga in fpgas
            }
        )
    )
    if (validation.get("balance_auto_relaxed") is not False
        or validation.get("requested_balance_percent") != tolerance * 100.0
        or validation.get("effective_balance_percent") != max(percentages.values())
        or validation.get("effective_balance_percent_by_dimension") != percentages
        or validation.get("requested_balance_percent_by_dimension") != percentages
        or validation.get("strict_balance_dimensions") != sorted(strict_dimensions)
        or validation.get("capacity_only_dimensions") != sorted(set(fields) - strict_dimensions)
        or validation.get("constraint_mode_by_dimension") != expected_modes
        or validation.get("enforced_balance_percent_by_dimension")
            != {field: percentages[field] for field in strict_dimensions}
        or validation.get("capacity_limits") != expected_capacity_limits
        or reservation_evidence_disagrees
        or validation.get("strict_balance_violations") != 0
        or validation.get("capacity_violations") != 0
        or validation.get("balance_loads") != loads
        or validation.get("balance_allowed_loads") != bounds):
        raise ValidationError("indexed Phase 3 strict balance evidence disagrees with sealed constraints")
    # Older sealed reports predate this diagnostic field; when present it must
    # agree with the independently reconstructed indexed loads.
    if ("balance_observation" in validation and validation["balance_observation"]
        != summarize_balance_observation(loads, strict_dimensions)):
        raise ValidationError("indexed Phase 3 balance observation disagrees with loads")
    if proof["semantic_contract_sha256"] is not None:
        if validation.get("cut_mode") != CUT_MODE_STATIC_EXACT:
            raise ValidationError(
                "Phase 3 exact assignment lacks exact validation status"
            )
    elif validation.get("cut_mode") == CUT_MODE_STATIC_EXACT:
        raise ValidationError(
            "Phase 3 sequential assignment claims exact validation status"
        )
    return validation
