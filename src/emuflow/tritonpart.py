from __future__ import annotations

import math
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .partition import (
    active_partition_fpgas,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .partition_limits import (
    planning_resource_capacities,
    strict_balance_limits,
    validate_indivisible_limits,
)
from .phase3_runtime import phase3_runtime_call
from .platform import Platform
from .resources import RESOURCE_FIELDS
from .sta import (
    PARTITION_NET_WEIGHTS_INDEX_SCHEMA,
    load_partition_net_weights_indexed,
)


TRITONPART_INPUT_SCHEMA = "emuflow.tritonpart-input/v1"
PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"
TRITONPART_PROVIDER = "tritonpart-openroad-hypergraph-v1"
TRITONPART_OBJECTIVE_NET_CUT = "net-cut-v1"
TRITONPART_INITIAL_ILP_VERTEX_THRESHOLD = 1000


def load_partition_net_weights(path: Optional[Path]) -> Dict[str, float]:
    if path is None:
        return {}
    value = read_json(path)
    if value.get("schema") == PARTITION_NET_WEIGHTS_INDEX_SCHEMA:
        return load_partition_net_weights_indexed(path)
    if value.get("schema") != PARTITION_NET_WEIGHTS_SCHEMA:
        raise ValidationError(
            "net weights schema: expected "
            f"{PARTITION_NET_WEIGHTS_SCHEMA!r}, got {value.get('schema')!r}"
        )
    raw_weights = value.get("weights")
    if not isinstance(raw_weights, dict):
        raise ValidationError("net weights.weights: expected an object")
    weights: Dict[str, float] = {}
    for net_id, raw_weight in raw_weights.items():
        if not isinstance(net_id, str) or not net_id:
            raise ValidationError(
                "net weights.weights: keys must be non-empty strings"
            )
        if (
            isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or float(raw_weight) <= 0.0
        ):
            raise ValidationError(
                f"net weights.weights[{net_id!r}]: expected a positive number"
            )
        weights[net_id] = float(raw_weight)
    return weights


def _active_resource_fields(
    clusters: Sequence[Mapping[str, Any]],
) -> List[str]:
    fields = []
    for field in RESOURCE_FIELDS:
        if not any(cluster["resources"].get(field, 0) for cluster in clusters):
            continue
        fields.append(field)
    return fields


def _vertex_weights(
    cluster: Mapping[str, Any],
    resource_fields: Sequence[str],
) -> List[int]:
    return [
        len(cluster["instances"]),
        *(cluster["resources"].get(field, 0) for field in resource_fields),
    ]


def _tritonpart_ubfactor_percent_points(
    base_balance: Sequence[float],
    fpga_order: Sequence[str],
    totals: Mapping[str, int],
    limits: Mapping[str, Mapping[str, int]],
) -> float:
    """Cover all exact upper-bound-feasible loads with TritonPart's scalar.

    TritonPart uses target +/- UBfactor/100 in every weight dimension. Our
    contract specifies only per-resource upper bounds, so its implicit lower
    bound must not exclude legal assignments either. Infer a partition's lowest
    feasible share from the other partitions' upper bounds. The exported scalar
    is retained for scalar-only interfaces. Native partitioning and evaluation
    use the independent absolute bounds, also checked after parsing.
    """
    margin = 0.0
    for field, total in totals.items():
        if total <= 0:
            continue
        upper = [min(total, limits[fpga][field]) / total for fpga in fpga_order]
        for index, target in enumerate(base_balance):
            lower = max(0.0, 1.0 - sum(upper[:index]) - sum(upper[index + 1:]))
            margin = max(margin, upper[index] - target, target - lower)
    # Round outward for the decimal Tcl interface; never truncate below the
    # feasible envelope through floating-point/formatting error.
    return math.ceil(max(0.0, margin) * 100.0 * 1_000_000) / 1_000_000


def _legal_hyperedges(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    vertex_number: Mapping[str, int],
    net_weights: Mapping[str, float],
    transported_cut_classes: set[str],
) -> List[Dict[str, Any]]:
    known_nets = {net["id"] for net in ir.value["nets"]}
    unknown_weights = sorted(set(net_weights) - known_nets)
    if unknown_weights:
        raise ValidationError(
            f"net weights reference unknown nets {unknown_weights[:8]}"
        )

    hyperedges = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        drivers = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sinks = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
        )
        cluster_ids = sorted(set(drivers) | set(sinks))
        if len(cluster_ids) < 2:
            continue
        weight = float(net_weights.get(net["id"], 1.0))
        hyperedges.append(
            {
                "net": net["id"],
                "cut_class": net["cut_class"],
                "objective_component": "net-cut",
                "weight": weight,
                "clusters": cluster_ids,
                "vertices": [
                    vertex_number[cluster_id]
                    for cluster_id in cluster_ids
                ],
            }
        )
    return hyperedges


def export_tritonpart_inputs(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    net_weights: Optional[Mapping[str, float]] = None,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    write_manifest: bool = True,
) -> Dict[str, Any]:
    if (
        isinstance(num_initial_solutions, bool)
        or not isinstance(num_initial_solutions, int)
        or num_initial_solutions <= 0
    ):
        raise ValidationError(
            "TritonPart num_initial_solutions must be a positive integer"
        )
    if (
        isinstance(num_best_initial_solutions, bool)
        or not isinstance(num_best_initial_solutions, int)
        or num_best_initial_solutions <= 0
        or num_best_initial_solutions > num_initial_solutions
    ):
        raise ValidationError(
            "TritonPart num_best_initial_solutions must be in [1, "
            "num_initial_solutions]"
        )
    clusters = sorted(clusters_artifact["clusters"], key=lambda item: item["id"])
    partition_fpgas = active_partition_fpgas(platform, constraints)
    if len(clusters) < len(partition_fpgas):
        raise ValidationError(
            "TritonPart needs at least one atomic cluster per FPGA"
        )

    resource_fields = _active_resource_fields(clusters)
    dimensions = ["cells", *resource_fields]
    weights = [_vertex_weights(cluster, resource_fields) for cluster in clusters]
    base_balance = [1.0 / len(partition_fpgas)] * len(partition_fpgas)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    tolerances = constraints.get("balance_tolerance_by_dimension", {})
    requested_balance = constraints["balance_tolerance"] * 100.0
    effective_balance = max(
        tolerances.get(field, constraints["balance_tolerance"]) * 100.0
        for field in dimensions
    )
    totals = {field: sum(row[index] for row in weights)
              for index, field in enumerate(dimensions)}
    limits = strict_balance_limits(
        partition_fpgas,
        totals,
        constraints["balance_tolerance"],
        tolerances,
        constraints.get("balance_dimensions", ()),
        constraints.get("resource_reservations_by_fpga", {}),
    )
    validate_indivisible_limits(
        ((cluster["id"], dict(zip(dimensions, row)), cluster.get("fixed_fpga"))
         for cluster, row in zip(clusters, weights)), limits,
    )
    tritonpart_ubfactor = _tritonpart_ubfactor_percent_points(
        base_balance, fpga_ids, totals, limits
    )

    vertex_number = {
        cluster["id"]: index for index, cluster in enumerate(clusters, start=1)
    }
    cluster_by_instance = {
        instance_id: cluster["id"]
        for cluster in clusters
        for instance_id in cluster["instances"]
    }
    hyperedges = _legal_hyperedges(
        ir,
        cluster_by_instance,
        vertex_number,
        net_weights or {},
        transported_cut_classes_for_clusters(clusters_artifact),
    )
    if not hyperedges:
        raise ValidationError(
            "TritonPart hypergraph has no legal sequential-boundary "
            "hyperedges; "
            "use the greedy provider for disconnected designs"
        )
    specified_weights = net_weights or {}
    net_cut_hyperedges = [
        edge
        for edge in hyperedges
        if edge["objective_component"] == "net-cut"
    ]
    timed_hyperedges = [
        edge
        for edge in net_cut_hyperedges
        if edge["net"] in specified_weights
    ]
    timing_weight_coverage = {
        "specified_nets": len(specified_weights),
        "legal_hyperedges": len(net_cut_hyperedges),
        "timed_legal_hyperedges": len(timed_hyperedges),
        "timed_legal_hyperedge_fraction": (
            len(timed_hyperedges) / len(net_cut_hyperedges)
        ),
        "minimum_timed_weight": min(
            (edge["weight"] for edge in timed_hyperedges),
            default=1.0,
        ),
        "maximum_timed_weight": max(
            (edge["weight"] for edge in timed_hyperedges),
            default=1.0,
        ),
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    hypergraph_path = output_dir / "partition.hgr"
    baseline_hypergraph_path = output_dir / "partition.unweighted.hgr"
    fixed_path = output_dir / "partition.fix"
    edge_classes_path = output_dir / "partition.edge-classes"
    tcl_path = output_dir / "run_tritonpart.tcl"
    solution_path = output_dir / f"partition.hgr.part.{len(fpga_ids)}"

    hgr_lines = [f"{len(hyperedges)} {len(clusters)} 11"]
    for edge in hyperedges:
        vertices = " ".join(str(vertex) for vertex in edge["vertices"])
        hgr_lines.append(f"{edge['weight']:.9g} {vertices}")
    hgr_lines.extend(" ".join(str(value) for value in item) for item in weights)
    hypergraph_path.write_text("\n".join(hgr_lines) + "\n", encoding="utf-8")
    if specified_weights:
        baseline_lines = [f"{len(hyperedges)} {len(clusters)} 11"]
        for edge in hyperedges:
            vertices = " ".join(
                str(vertex) for vertex in edge["vertices"]
            )
            baseline_lines.append(f"1 {vertices}")
        baseline_lines.extend(
            " ".join(str(value) for value in item) for item in weights
        )
        baseline_hypergraph_path.write_text(
            "\n".join(baseline_lines) + "\n",
            encoding="utf-8",
        )

    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    fixed_path.write_text(
        "".join(
            (
                f"{fpga_index[cluster['fixed_fpga']]}\n"
                if cluster["fixed_fpga"] is not None
                else "-1\n"
            )
            for cluster in clusters
        ),
        encoding="utf-8",
    )
    edge_classes_path.write_text(
        "".join(
            "1\n" if edge["cut_class"] == "combinational" else "0\n"
            for edge in hyperedges
        ),
        encoding="utf-8",
    )

    bounds_path = output_dir / "partition.resource-bounds"
    bounds_path.write_text(
        f"{len(fpga_ids)} {len(dimensions)}\n"
        + "".join(
            " ".join(f"0 {limits[fpga][field]}" for field in dimensions) + "\n"
            for fpga in fpga_ids
        ), encoding="utf-8",
    )

    def tcl_list(values: Sequence[Any]) -> str:
        return "{ " + " ".join(str(value) for value in values) + " }"

    tcl_lines = [
        "triton_part_hypergraph \\",
        f"  -hypergraph_file {{{hypergraph_path.resolve()}}} \\",
        f"  -fixed_file {{{fixed_path.resolve()}}} \\",
        f"  -resource_bounds_file {{{bounds_path.resolve()}}} \\",
        f"  -num_parts {len(fpga_ids)} \\",
        f"  -balance_constraint {tritonpart_ubfactor:.9g} \\",
        f"  -base_balance {tcl_list([f'{value:.12g}' for value in base_balance])} \\",
        f"  -scale_factor {tcl_list([1.0] * len(fpga_ids))} \\",
        f"  -seed 0 \\",
        f"  -num_initial_solutions {num_initial_solutions} \\",
        f"  -num_vertices_threshold_ilp {TRITONPART_INITIAL_ILP_VERTEX_THRESHOLD} \\",
        (
            "  -num_best_initial_solutions "
            f"{num_best_initial_solutions} \\"
        ),
        f"  -vertex_dimension {len(dimensions)} \\",
        "  -hyperedge_dimension 1 \\",
        f"  -v_wt_factors {tcl_list([1.0] * len(dimensions))} \\",
        "  -e_wt_factors { 1.0 } \\",
        "  -min_num_vertices_each_part 1",
        "exit",
    ]
    tcl_path.write_text("\n".join(tcl_lines) + "\n", encoding="utf-8")

    artifact: Dict[str, Any] = {
        "schema": TRITONPART_INPUT_SCHEMA,
        "objective_encoding": TRITONPART_OBJECTIVE_NET_CUT,
        "objective_components": {
            "net_cut_hyperedges": len(net_cut_hyperedges),
        },
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "fpga_order": fpga_ids,
        "cluster_order": [cluster["id"] for cluster in clusters],
        "vertex_dimensions": dimensions,
        "vertex_weights": weights,
        "hyperedges": hyperedges,
        "timing_weight_coverage": timing_weight_coverage,
        "base_balance": base_balance,
        "requested_balance_percent": requested_balance,
        "effective_balance_percent": effective_balance,
        "tritonpart_ubfactor_percent_points": tritonpart_ubfactor,
        "tritonpart_balance_envelope": "native-exact-bounds-v1",
        "strict_balance_dimensions": sorted(constraints.get("balance_dimensions", ())),
        "balance_auto_relaxed": False,
        "balance_allowed_loads": limits,
        "search_effort": {
            "num_initial_solutions": num_initial_solutions,
            "num_best_initial_solutions": num_best_initial_solutions,
            "num_vertices_threshold_ilp": TRITONPART_INITIAL_ILP_VERTEX_THRESHOLD,
        },
        "files": {
            "hypergraph": hypergraph_path.name,
            "unweighted_baseline_hypergraph": (
                baseline_hypergraph_path.name
                if specified_weights
                else None
            ),
            "fixed": fixed_path.name,
            "resource_bounds": bounds_path.name,
            "edge_classes": edge_classes_path.name,
            "tcl": tcl_path.name,
            "solution": solution_path.name,
        },
    }
    if write_manifest:
        write_json(output_dir / "tritonpart_input.json", artifact, compact=True)
    return artifact


def parse_tritonpart_solution(
    path: Path,
    tritonpart_input: Mapping[str, Any],
) -> Dict[str, str]:
    if not path.is_file():
        raise ValidationError(f"TritonPart solution does not exist: {path}")
    tokens = path.read_text(encoding="utf-8").split()
    cluster_order = tritonpart_input["cluster_order"]
    fpga_order = tritonpart_input["fpga_order"]
    if len(tokens) != len(cluster_order):
        raise ValidationError(
            "TritonPart solution vertex count mismatch: "
            f"expected {len(cluster_order)}, got {len(tokens)}"
        )
    assignment: Dict[str, str] = {}
    for index, (cluster_id, token) in enumerate(zip(cluster_order, tokens)):
        try:
            part_id = int(token)
        except ValueError as error:
            raise ValidationError(
                f"TritonPart solution line {index + 1}: expected an integer"
            ) from error
        if part_id < 0 or part_id >= len(fpga_order):
            raise ValidationError(
                f"TritonPart solution line {index + 1}: invalid part {part_id}"
            )
        assignment[cluster_id] = fpga_order[part_id]
    return assignment


def _repair_min_used_fpgas(
    cluster_assignment: Mapping[str, str],
    clusters_artifact: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    hyperedges: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    assignment = dict(cluster_assignment)
    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    partition_fpgas = active_partition_fpgas(platform, constraints)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    capacity = planning_resource_capacities(
        partition_fpgas,
        constraints.get("resource_reservations_by_fpga", {}),
    )
    loads = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_ids
    }
    cluster_counts = {fpga_id: 0 for fpga_id in fpga_ids}
    for cluster_id, fpga_id in assignment.items():
        cluster_counts[fpga_id] += 1
        for field, value in clusters[cluster_id]["resources"].items():
            loads[fpga_id][field] += value

    # Maintain the partition multiplicity of every incident hyperedge.  The
    # previous implementation rebuilt two Python sets over the complete
    # hyperedge for every candidate move.  That is exact but becomes
    # prohibitively expensive on contest-scale netlists (hundreds of
    # thousands of clusters and many high-fanout nets).  Partition counts
    # make the same cut-connectivity delta O(incident edges), independent of
    # hyperedge cardinality.
    edge_clusters = [list(edge["clusters"]) for edge in hyperedges]
    edge_weights = [float(edge["weight"]) for edge in hyperedges]
    incident_edges: Dict[str, List[int]] = defaultdict(list)
    edge_part_counts: List[Dict[str, int]] = []
    for edge_index, cluster_ids in enumerate(edge_clusters):
        counts: Dict[str, int] = defaultdict(int)
        for cluster_id in cluster_ids:
            incident_edges[cluster_id].append(edge_index)
            counts[assignment[cluster_id]] += 1
        edge_part_counts.append(dict(counts))

    def cut_delta(cluster_id: str, source: str, target: str) -> float:
        delta = 0.0
        for edge_index in incident_edges.get(cluster_id, []):
            counts = edge_part_counts[edge_index]
            before_parts = len(counts)
            after_parts = before_parts
            if counts[source] == 1:
                after_parts -= 1
            if counts.get(target, 0) == 0:
                after_parts += 1
            delta += (
                int(after_parts > 1) - int(before_parts > 1)
            ) * edge_weights[edge_index]
        return delta

    def apply_edge_move(cluster_id: str, source: str, target: str) -> None:
        for edge_index in incident_edges.get(cluster_id, []):
            counts = edge_part_counts[edge_index]
            counts[source] -= 1
            if counts[source] == 0:
                del counts[source]
            counts[target] = counts.get(target, 0) + 1

    moves = []
    while sum(count > 0 for count in cluster_counts.values()) < constraints[
        "min_used_fpgas"
    ]:
        target = next(
            fpga_id for fpga_id in fpga_ids if cluster_counts[fpga_id] == 0
        )
        candidates = []
        for cluster_id, source in assignment.items():
            cluster = clusters[cluster_id]
            if cluster_counts[source] <= 1 or cluster["fixed_fpga"] is not None:
                continue
            resources = cluster["resources"]
            if any(
                loads[target][field] + resources.get(field, 0) > limit
                for field, limit in capacity[target].items()
            ):
                continue
            estimated_delta = cut_delta(cluster_id, source, target)
            candidates.append(
                (
                    len(cluster["instances"]),
                    sum(resources.values()),
                    estimated_delta,
                    cluster_id,
                    source,
                )
            )
        if not candidates:
            raise ValidationError(
                "TritonPart min-used-FPGA repair found no legal movable "
                f"cluster for empty partition {target!r}"
            )
        instance_count, _, estimated_cut_delta, cluster_id, source = min(
            candidates
        )
        resources = clusters[cluster_id]["resources"]
        apply_edge_move(cluster_id, source, target)
        assignment[cluster_id] = target
        cluster_counts[source] -= 1
        cluster_counts[target] += 1
        for field in RESOURCE_FIELDS:
            value = resources.get(field, 0)
            loads[source][field] -= value
            loads[target][field] += value
        moves.append(
            {
                "cluster": cluster_id,
                "source": source,
                "target": target,
                "instances": instance_count,
                "resources": dict(resources),
                "estimated_cut_delta": estimated_cut_delta,
            }
        )
    return assignment, moves


def _stream_assignment_cut_summary(
    hyperedges: Iterable[Mapping[str, Any]],
    assignment: Mapping[str, str],
    timed_nets: AbstractSet[str],
) -> Dict[str, float | int]:
    """Accumulate assignment cut metrics without retaining decoded edges."""

    cut_hyperedges = 0
    cut_weight = 0.0
    timed_cut_hyperedges = 0
    timed_cut_weight = 0.0
    for edge in hyperedges:
        clusters = iter(edge["clusters"])
        try:
            first_fpga = assignment[next(clusters)]
        except StopIteration:
            continue
        if not any(assignment[cluster_id] != first_fpga for cluster_id in clusters):
            continue
        weight = float(edge["weight"])
        cut_hyperedges += 1
        cut_weight += weight
        if edge["net"] in timed_nets:
            timed_cut_hyperedges += 1
            timed_cut_weight += weight
    return {
        "cut_hyperedges": cut_hyperedges,
        "cut_weight": cut_weight,
        "timed_cut_hyperedges": timed_cut_hyperedges,
        "timed_cut_weight": timed_cut_weight,
    }


def run_tritonpart(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    seed: int,
    executable: Optional[str] = None,
    solution_input: Optional[Path] = None,
    net_weights: Optional[Mapping[str, float]] = None,
    timeout_seconds: int = 3600,
    seed_attempts: int = 1,
    num_initial_solutions: int = 50,
    num_best_initial_solutions: int = 10,
    repair_min_used_fpgas: bool = True,
    persist_input_manifest: bool = True,
    run_unweighted_baseline: bool = False,
    defer_semantic_contract: bool = False,
) -> Dict[str, Any]:
    if seed_attempts <= 0:
        raise ValueError("TritonPart seed_attempts must be positive")
    if solution_input is not None and seed_attempts != 1:
        raise ValueError(
            "precomputed TritonPart solutions require seed_attempts=1"
        )
    tritonpart_input = phase3_runtime_call(
        "tritonpart.export_inputs",
        export_tritonpart_inputs,
        ir,
        platform,
        clusters_artifact,
        constraints,
        output_dir,
        net_weights=net_weights,
        num_initial_solutions=num_initial_solutions,
        num_best_initial_solutions=num_best_initial_solutions,
        write_manifest=False,
    )
    num_parts = len(tritonpart_input["fpga_order"])
    tritonpart_input["seed"] = seed
    if persist_input_manifest:
        # Standalone qualification retains one human-inspectable manifest.
        # Managed production consumes the native files directly and must not
        # serialize this large duplicate only to delete it after selection.
        write_json(
            output_dir / "tritonpart_input.json", tritonpart_input, compact=True
        )
    tcl_path = output_dir / tritonpart_input["files"]["tcl"]
    tcl_template = tcl_path.read_text(encoding="utf-8")

    solution_path = output_dir / tritonpart_input["files"]["solution"]
    resolved_executable: Optional[str] = None
    mode = "execute"
    if solution_input is None:
        resolved_executable = resolve_native_executable(
            "openroad", executable
        )

    cluster_assignment: Optional[Dict[str, str]] = None
    selected_seed = seed
    selected_log: Optional[Path] = None
    selected_solution_path: Optional[Path] = None
    selected_repair_moves: List[Dict[str, Any]] = []
    selected_timing_cut_summary: Optional[Dict[str, float | int]] = None
    selected_objective: Optional[Tuple[Any, ...]] = None
    selected_attempt_mode: Optional[str] = None
    attempts = []
    timed_nets = set(net_weights or {})
    attempt_specs = [
        {
            "mode": "timing_weighted",
            "seed": seed + offset,
            "hypergraph": output_dir
            / tritonpart_input["files"]["hypergraph"],
        }
        for offset in range(seed_attempts)
    ]
    baseline_name = tritonpart_input["files"][
        "unweighted_baseline_hypergraph"
    ]
    if (
        run_unweighted_baseline
        and net_weights
        and baseline_name is not None
        and solution_input is None
    ):
        attempt_specs.append(
            {
                "mode": "unweighted_baseline",
                "seed": seed,
                "hypergraph": output_dir / baseline_name,
            }
        )
    weighted_hypergraph = (
        output_dir / tritonpart_input["files"]["hypergraph"]
    ).resolve()
    for spec in attempt_specs:
        attempt_seed = spec["seed"]
        provider_solution_path = (
            solution_path
            if spec["mode"] == "timing_weighted"
            else Path(
                f"{spec['hypergraph']}.part.{num_parts}"
            )
        )
        tcl_text = tcl_template.replace(
            "  -seed 0 \\\n", f"  -seed {attempt_seed} \\\n"
        )
        tcl_text = tcl_text.replace(
            str(weighted_hypergraph),
            str(spec["hypergraph"].resolve()),
        )
        tcl_path.write_text(tcl_text, encoding="utf-8")
        tritonpart_input["seed"] = attempt_seed
        if solution_input is not None:
            if not solution_input.is_file():
                raise ValidationError(
                    "precomputed TritonPart solution does not exist: "
                    f"{solution_input}"
                )
            if solution_input.resolve() != solution_path.resolve():
                phase3_runtime_call(
                    "tritonpart.import_solution",
                    shutil.copyfile,
                    solution_input,
                    solution_path,
                )
            mode = "import"
            log_path = None
        else:
            if spec["mode"] == "unweighted_baseline":
                log_name = (
                    "openroad-tritonpart.unweighted-baseline."
                    f"seed-{attempt_seed}.log"
                )
            else:
                log_name = (
                    "openroad-tritonpart.log"
                    if len(attempt_specs) == 1
                    else f"openroad-tritonpart.seed-{attempt_seed}.log"
                )
            log_path = output_dir / log_name
            if provider_solution_path.exists():
                provider_solution_path.unlink()
            try:
                completed = phase3_runtime_call(
                    "tritonpart.execute_native",
                    subprocess.run,
                    [resolved_executable, "-exit", str(tcl_path.resolve())],
                    cwd=output_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                raise EmuFlowError(
                    f"TritonPart exceeded timeout of {timeout_seconds} seconds"
                ) from error
            log_path.write_text(completed.stdout, encoding="utf-8")
            if completed.returncode != 0:
                tail = "\n".join(completed.stdout.splitlines()[-30:])
                raise EmuFlowError(
                    "OpenROAD/TritonPart failed with exit code "
                    f"{completed.returncode}\n{tail}"
                )
            if not provider_solution_path.is_file():
                raise EmuFlowError(
                    "OpenROAD/TritonPart reported success but did not create "
                    f"{provider_solution_path}"
                )

        candidate = phase3_runtime_call(
            "tritonpart.parse_solution",
            parse_tritonpart_solution,
            provider_solution_path,
            tritonpart_input,
        )
        attempt_solution = None
        if len(attempt_specs) > 1:
            solution_stem = (
                f"partition.hgr.seed-{attempt_seed}"
                if spec["mode"] == "timing_weighted"
                else (
                    "partition.hgr.unweighted-baseline."
                    f"seed-{attempt_seed}"
                )
            )
            attempt_solution = output_dir / (
                f"{solution_stem}.part.{num_parts}"
            )
            shutil.copyfile(provider_solution_path, attempt_solution)
        raw_used_fpgas = len(set(candidate.values()))
        attempt: Dict[str, Any] = {
            "mode": spec["mode"],
            "seed": attempt_seed,
            "raw_used_fpgas": raw_used_fpgas,
            "used_fpgas": raw_used_fpgas,
            "repair_moves": [],
            "solution": (
                attempt_solution.name
                if attempt_solution is not None
                else solution_path.name
            ),
            "log": log_path.name if log_path is not None else None,
        }

        def validate_candidate(value: Mapping[str, str]) -> Dict[str, Any]:
            return validate_cluster_assignment_balance(
                platform,
                clusters_artifact["clusters"],
                value,
                constraints["balance_tolerance"],
                constraints.get("balance_tolerance_by_dimension", {}),
                constraints.get("active_fpgas"),
                constraints.get("balance_dimensions", ()),
                constraints.get("resource_reservations_by_fpga", {}),
            )

        # Occupancy repair is not allowed to rescue an illegal resource load.
        # Validate the untouched native assignment before any postprocessing.
        repair_moves: List[Dict[str, Any]] = []
        try:
            attempt["balance_validation"] = validate_candidate(candidate)
            if repair_min_used_fpgas and raw_used_fpgas < constraints["min_used_fpgas"]:
                candidate, repair_moves = _repair_min_used_fpgas(
                    candidate, clusters_artifact, platform, constraints,
                    tritonpart_input["hyperedges"],
                )
                attempt["balance_validation"] = validate_candidate(candidate)
        except ValidationError as error:
            attempt["accepted"] = False
            attempt["rejection"] = "multi_resource_balance"
            attempt["error"] = str(error)
            attempts.append(attempt)
            continue
        used_fpgas = len(set(candidate.values()))
        attempt["used_fpgas"] = used_fpgas
        attempt["repair_moves"] = repair_moves
        if used_fpgas < constraints["min_used_fpgas"]:
            attempt["accepted"] = False
            attempt["rejection"] = "min_used_fpgas"
            attempts.append(attempt)
            continue

        attempt["accepted"] = True
        cut_summary = _stream_assignment_cut_summary(
            tritonpart_input["hyperedges"], candidate, timed_nets
        )
        attempt["cut_hyperedges"] = cut_summary["cut_hyperedges"]
        attempt["cut_weight"] = cut_summary["cut_weight"]
        attempts.append(attempt)
        objective = (
            float(attempt["cut_weight"]),
            int(attempt["cut_hyperedges"]),
            attempt_seed,
        )
        if selected_objective is None or objective < selected_objective:
            cluster_assignment = candidate
            selected_seed = attempt_seed
            selected_objective = objective
            selected_attempt_mode = spec["mode"]
            selected_log = log_path
            selected_solution_path = (
                attempt_solution
                if attempt_solution is not None
                else provider_solution_path
            )
            selected_repair_moves = repair_moves
            selected_timing_cut_summary = {
                "timed_cut_hyperedges": cut_summary["timed_cut_hyperedges"],
                "timed_cut_weight": cut_summary["timed_cut_weight"],
            }

    if cluster_assignment is None:
        raise ValidationError(
            "TritonPart seed sweep produced no independently acceptable "
            "assignment; "
            f"attempts={attempts}"
        )
    if (
        len(attempt_specs) > 1
        and selected_solution_path is not None
        and selected_solution_path.resolve() != solution_path.resolve()
    ):
        shutil.copyfile(selected_solution_path, solution_path)
    tritonpart_input["seed"] = selected_seed
    if selected_timing_cut_summary is None:
        raise ValidationError("TritonPart selected cut summary is missing")
    timing_weight_result = {
        **tritonpart_input["timing_weight_coverage"],
        **selected_timing_cut_summary,
    }
    provider_metadata = {
        "mode": mode,
        "selected_attempt_mode": selected_attempt_mode,
        "executable": resolved_executable,
        "input_schema": TRITONPART_INPUT_SCHEMA,
        "vertex_dimensions": tritonpart_input["vertex_dimensions"],
        "hyperedges": len(tritonpart_input["hyperedges"]),
        "timing_weights": timing_weight_result,
        "requested_balance_percent": tritonpart_input[
            "requested_balance_percent"
        ],
        "effective_balance_percent": tritonpart_input[
            "effective_balance_percent"
        ],
        "tritonpart_ubfactor_percent_points": tritonpart_input[
            "tritonpart_ubfactor_percent_points"
        ],
        "balance_auto_relaxed": tritonpart_input["balance_auto_relaxed"],
        "seed_attempts": attempts,
        "search_effort": tritonpart_input["search_effort"],
        "objective_encoding": tritonpart_input["objective_encoding"],
        "objective_components": tritonpart_input["objective_components"],
        "min_used_fpgas_repair": {
            "enabled": repair_min_used_fpgas,
            "moves": selected_repair_moves,
        },
        "resource_constraints": "native-exact-bounds-v1",
        "artifacts": (
            {
                **tritonpart_input["files"],
                "input": "tritonpart_input.json",
                "log": (
                    selected_log.name
                    if selected_log is not None and mode == "execute"
                    else None
                ),
            }
            if persist_input_manifest
            else {"retained": False}
        ),
    }
    return phase3_runtime_call(
        "tritonpart.build_assignment",
        build_partition_assignment,
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=TRITONPART_PROVIDER,
        seed=selected_seed,
        provider_metadata=provider_metadata,
        _include_semantic_contract=not defer_semantic_contract,
    )
