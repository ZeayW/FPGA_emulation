from __future__ import annotations

import hashlib
import heapq
import math
import shutil
import subprocess
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .partition import (
    CUT_MODE_STATIC_EXACT,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .platform import Platform
from .resources import RESOURCE_FIELDS


TRITONPART_INPUT_SCHEMA = "emuflow.tritonpart-input/v1"
PARTITION_NET_WEIGHTS_SCHEMA = "emuflow.partition-net-weights/v1"
TRITONPART_PROVIDER = "tritonpart-openroad-hypergraph-v1"


def load_partition_net_weights(path: Optional[Path]) -> Dict[str, float]:
    if path is None:
        return {}
    value = read_json(path)
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
    platform: Platform,
) -> List[str]:
    fields = []
    for field in RESOURCE_FIELDS:
        if not any(cluster["resources"].get(field, 0) for cluster in clusters):
            continue
        if not all(fpga.effective_capacity.get(field, 0) > 0 for fpga in platform.fpgas):
            continue
        fields.append(field)
    return fields


def _capacity_base_balance(
    platform: Platform,
    resource_fields: Sequence[str],
) -> List[float]:
    num_parts = len(platform.fpgas)
    if not resource_fields:
        return [1.0 / num_parts] * num_parts

    reference: Optional[List[float]] = None
    for field in resource_fields:
        capacities = [
            float(fpga.effective_capacity[field]) for fpga in platform.fpgas
        ]
        total = sum(capacities)
        shares = [capacity / total for capacity in capacities]
        if reference is None:
            reference = shares
            continue
        if any(
            not math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-9)
            for left, right in zip(reference, shares)
        ):
            raise ValidationError(
                "TritonPart hypergraph mode cannot represent resource-specific "
                "heterogeneous FPGA capacity ratios; use homogeneous/proportionally "
                "scaled FPGAs or the greedy provider"
            )
    assert reference is not None
    return reference


def _vertex_weights(
    cluster: Mapping[str, Any],
    resource_fields: Sequence[str],
) -> List[int]:
    return [
        len(cluster["instances"]),
        *(cluster["resources"].get(field, 0) for field in resource_fields),
    ]


def _effective_balance_percent(
    vertex_weights: Sequence[Sequence[int]],
    clusters: Sequence[Mapping[str, Any]],
    fpga_ids: Sequence[str],
    base_balance: Sequence[float],
    requested_tolerance: float,
) -> Tuple[float, float]:
    totals = [
        sum(weights[index] for weights in vertex_weights)
        for index in range(len(vertex_weights[0]))
    ]
    required_ratio = 0.0
    largest_target = max(base_balance)
    for weights in vertex_weights:
        for index, total in enumerate(totals):
            if total:
                required_ratio = max(
                    required_ratio,
                    weights[index] / total / largest_target - 1.0,
                )

    fixed_totals = [
        [0] * len(totals) for _ in fpga_ids
    ]
    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    for cluster, weights in zip(clusters, vertex_weights):
        fixed_fpga = cluster["fixed_fpga"]
        if fixed_fpga is None:
            continue
        target = fixed_totals[fpga_index[fixed_fpga]]
        for index, weight in enumerate(weights):
            target[index] += weight
    for part_index, weights in enumerate(fixed_totals):
        for dimension, total in enumerate(totals):
            if total:
                required_ratio = max(
                    required_ratio,
                    weights[dimension]
                    / total
                    / base_balance[part_index]
                    - 1.0,
                )

    requested_percent = requested_tolerance * 100.0
    required_percent = max(0.0, required_ratio * 100.0)
    # TritonPart compares floating-point accumulated weights. A small,
    # deterministic guard prevents an exactly tight atomic cluster from being
    # rejected due to rounding.
    effective_percent = max(requested_percent, required_percent + 0.01)
    return requested_percent, effective_percent


def _tritonpart_ubfactor_percent_points(
    base_balance: Sequence[float],
    effective_balance_percent: float,
) -> float:
    """Translate EmuFlow's relative tolerance to TritonPart percentage points.

    EmuFlow defines a 10% tolerance around a 25% target as an upper share of
    27.5%. TritonPart instead adds ``UBfactor * 0.01`` directly to the target,
    so the equivalent UBfactor is 2.5 percentage points, not 10.

    A single TritonPart UBfactor is shared by all partitions. Using the largest
    target share guarantees that an auto-relaxed atomic cluster can be placed;
    EmuFlow's independent validator still enforces the stricter relative
    tolerance per FPGA after TritonPart returns.
    """

    return max(base_balance) * effective_balance_percent


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
        cluster_ids = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for collection in ("drivers", "sinks")
                for endpoint in net[collection]
                if endpoint["instance"] is not None
            }
        )
        if len(cluster_ids) < 2:
            continue
        hyperedges.append(
            {
                "net": net["id"],
                "weight": float(net_weights.get(net["id"], 1.0)),
                "clusters": cluster_ids,
                "vertices": [vertex_number[cluster_id] for cluster_id in cluster_ids],
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
    if len(clusters) < len(platform.fpgas):
        raise ValidationError(
            "TritonPart needs at least one atomic cluster per FPGA"
        )

    resource_fields = _active_resource_fields(clusters, platform)
    dimensions = ["cells", *resource_fields]
    weights = [_vertex_weights(cluster, resource_fields) for cluster in clusters]
    base_balance = _capacity_base_balance(platform, resource_fields)
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    requested_balance, effective_balance = _effective_balance_percent(
        weights,
        clusters,
        fpga_ids,
        base_balance,
        constraints["balance_tolerance"],
    )
    tritonpart_ubfactor = _tritonpart_ubfactor_percent_points(
        base_balance,
        effective_balance,
    )

    for dimension_index, field in enumerate(resource_fields, start=1):
        total = sum(item[dimension_index] for item in weights)
        capacity = sum(
            fpga.effective_capacity[field] for fpga in platform.fpgas
        )
        if total > capacity:
            raise ValidationError(
                f"total {field} demand {total} exceeds effective platform "
                f"capacity {capacity}"
            )
    for cluster, item_weights in zip(clusters, weights):
        if not any(
            all(
                item_weights[index]
                <= fpga.effective_capacity[field]
                for index, field in enumerate(resource_fields, start=1)
            )
            for fpga in platform.fpgas
        ):
            raise ValidationError(
                f"atomic cluster {cluster['id']!r} cannot fit any FPGA"
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
    timed_hyperedges = [
        edge for edge in hyperedges if edge["net"] in specified_weights
    ]
    timing_weight_coverage = {
        "specified_nets": len(specified_weights),
        "legal_hyperedges": len(hyperedges),
        "timed_legal_hyperedges": len(timed_hyperedges),
        "timed_legal_hyperedge_fraction": (
            len(timed_hyperedges) / len(hyperedges)
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

    def tcl_list(values: Sequence[Any]) -> str:
        return "{ " + " ".join(str(value) for value in values) + " }"

    tcl_lines = [
        "triton_part_hypergraph \\",
        f"  -hypergraph_file {{{hypergraph_path.resolve()}}} \\",
        f"  -fixed_file {{{fixed_path.resolve()}}} \\",
        f"  -num_parts {len(fpga_ids)} \\",
        f"  -balance_constraint {tritonpart_ubfactor:.9g} \\",
        f"  -base_balance {tcl_list([f'{value:.12g}' for value in base_balance])} \\",
        f"  -scale_factor {tcl_list([1.0] * len(fpga_ids))} \\",
        f"  -seed 0 \\",
        f"  -num_initial_solutions {num_initial_solutions} \\",
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
        "balance_auto_relaxed": effective_balance > requested_balance + 1e-9,
        "search_effort": {
            "num_initial_solutions": num_initial_solutions,
            "num_best_initial_solutions": num_best_initial_solutions,
        },
        "files": {
            "hypergraph": hypergraph_path.name,
            "unweighted_baseline_hypergraph": (
                baseline_hypergraph_path.name
                if specified_weights
                else None
            ),
            "fixed": fixed_path.name,
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
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    capacity = {
        fpga.id: fpga.effective_capacity for fpga in platform.fpgas
    }
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
            delta += (after_parts - before_parts) * edge_weights[edge_index]
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
        instance_count, _, cut_delta, cluster_id, source = min(candidates)
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
                "estimated_cut_delta": cut_delta,
            }
        )
    return assignment, moves


def _repair_multi_resource_balance(
    cluster_assignment: Mapping[str, str],
    clusters_artifact: Mapping[str, Any],
    platform: Platform,
    constraints: Mapping[str, Any],
    tritonpart_input: Mapping[str, Any],
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Legalize a TritonPart solution against EmuFlow's upper load bounds.

    TritonPart can return a best-effort solution when none of its randomized
    initial candidates satisfies every multidimensional constraint. This
    deterministic pass starts from that low-cut solution and moves only
    clusters needed to remove upper-bound violations. Candidate moves are
    ranked by hypergraph cut delta per unit of overload relief.
    """

    assignment = dict(cluster_assignment)
    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    cluster_order = list(tritonpart_input["cluster_order"])
    fpga_order = list(tritonpart_input["fpga_order"])
    dimensions = list(tritonpart_input["vertex_dimensions"])
    vertex_weights = list(tritonpart_input["vertex_weights"])
    fpga_index = {
        fpga_id: index for index, fpga_id in enumerate(fpga_order)
    }
    labels = [
        fpga_index[assignment[cluster_id]] for cluster_id in cluster_order
    ]
    num_parts = len(fpga_order)
    num_dimensions = len(dimensions)
    totals = [
        sum(weights[dimension] for weights in vertex_weights)
        for dimension in range(num_dimensions)
    ]

    base_balance: List[List[float]] = []
    for dimension in dimensions:
        if dimension == "cells":
            base_balance.append([1.0 / num_parts] * num_parts)
            continue
        capacities = [
            float(fpga.effective_capacity[dimension])
            for fpga in platform.fpgas
        ]
        capacity_total = sum(capacities)
        base_balance.append(
            [capacity / capacity_total for capacity in capacities]
        )
    tolerance_overrides = constraints.get(
        "balance_tolerance_by_dimension", {}
    )
    if tolerance_overrides:
        cluster_records = {
            cluster["id"]: cluster
            for cluster in clusters_artifact["clusters"]
        }
        effective_ratio_by_dimension = []
        for dimension_index, dimension in enumerate(dimensions):
            requested = float(
                tolerance_overrides.get(
                    dimension, constraints["balance_tolerance"]
                )
            )
            required = 0.0
            largest_target = max(base_balance[dimension_index])
            total = totals[dimension_index]
            if total:
                for cluster_id, weights in zip(
                    cluster_order, vertex_weights
                ):
                    fixed_fpga = cluster_records[cluster_id]["fixed_fpga"]
                    target_share = (
                        base_balance[dimension_index][fpga_index[fixed_fpga]]
                        if fixed_fpga is not None
                        else largest_target
                    )
                    required = max(
                        required,
                        weights[dimension_index] / total / target_share - 1.0,
                    )
            effective_ratio_by_dimension.append(
                max(requested, max(0.0, required) + 0.0001)
            )
    else:
        effective_ratio = (
            float(tritonpart_input["effective_balance_percent"]) / 100.0
        )
        effective_ratio_by_dimension = [effective_ratio] * num_dimensions
    allowed = [
        [
            math.floor(
                totals[dimension]
                * base_balance[dimension][part]
                * (1.0 + effective_ratio_by_dimension[dimension])
                + 1e-7
            )
            for dimension in range(num_dimensions)
        ]
        for part in range(num_parts)
    ]
    loads = [[0] * num_dimensions for _ in range(num_parts)]
    for part, weights in zip(labels, vertex_weights):
        for dimension, weight in enumerate(weights):
            loads[part][dimension] += weight
    initial_loads = [list(item) for item in loads]

    edge_vertices = [
        [vertex - 1 for vertex in edge["vertices"]]
        for edge in tritonpart_input["hyperedges"]
    ]
    edge_weights = [
        float(edge["weight"]) for edge in tritonpart_input["hyperedges"]
    ]
    incident_edges: List[List[int]] = [
        [] for _ in range(len(cluster_order))
    ]
    for edge_index, vertices in enumerate(edge_vertices):
        for vertex in vertices:
            incident_edges[vertex].append(edge_index)
    edge_part_counts: List[Dict[int, int]] = []
    for vertices in edge_vertices:
        counts: Dict[int, int] = defaultdict(int)
        for vertex in vertices:
            counts[labels[vertex]] += 1
        edge_part_counts.append(dict(counts))
    vertices_by_part: List[List[int]] = [[] for _ in range(num_parts)]
    for vertex, part in enumerate(labels):
        vertices_by_part[part].append(vertex)

    def overload(part: int) -> List[int]:
        return [
            max(0, loads[part][dimension] - allowed[part][dimension])
            for dimension in range(num_dimensions)
        ]

    def fits(vertex: int, part: int) -> bool:
        return all(
            loads[part][dimension] + vertex_weights[vertex][dimension]
            <= allowed[part][dimension]
            for dimension in range(num_dimensions)
        )

    def cut_delta(vertex: int, source: int, target: int) -> float:
        delta = 0.0
        for edge_index in incident_edges[vertex]:
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

    def apply_edge_move(vertex: int, source: int, target: int) -> None:
        for edge_index in incident_edges[vertex]:
            counts = edge_part_counts[edge_index]
            counts[source] -= 1
            if counts[source] == 0:
                del counts[source]
            counts[target] = counts.get(target, 0) + 1

    def cut_summary() -> Tuple[int, float]:
        cut_edges = 0
        cut_weight = 0.0
        for counts, weight in zip(edge_part_counts, edge_weights):
            if len(counts) > 1:
                cut_edges += 1
                cut_weight += weight
        return cut_edges, cut_weight

    initial_cut_edges, initial_cut_weight = cut_summary()
    move_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    moved_weights: Dict[Tuple[int, int], List[int]] = {}
    move_digest = hashlib.sha256()
    estimated_cut_delta = 0.0
    move_total = 0
    paired_move_sequences = 0
    ejection_chain_sequences = 0
    max_ejection_chain_moves = 0

    def apply_move(vertex: int, source: int, target: int) -> float:
        """Apply and audit one deterministic repair move."""

        nonlocal estimated_cut_delta, move_total
        delta = cut_delta(vertex, source, target)
        apply_edge_move(vertex, source, target)
        labels[vertex] = target
        weights = vertex_weights[vertex]
        for dimension, weight in enumerate(weights):
            loads[source][dimension] -= weight
            loads[target][dimension] += weight
        vertices_by_part[target].append(vertex)
        transition = (source, target)
        move_counts[transition] += 1
        if transition not in moved_weights:
            moved_weights[transition] = [0] * num_dimensions
        for dimension, weight in enumerate(weights):
            moved_weights[transition][dimension] += weight
        cluster_id = cluster_order[vertex]
        assignment[cluster_id] = fpga_order[target]
        move_digest.update(
            (
                f"{cluster_id}:{fpga_order[source]}"
                f"->{fpga_order[target]}\n"
            ).encode("utf-8")
        )
        estimated_cut_delta += delta
        move_total += 1
        return delta

    def overload_score(
        part: int, projected: Optional[Sequence[int]] = None
    ) -> float:
        candidate_load = loads[part] if projected is None else projected
        return sum(
            max(0, candidate_load[dimension] - allowed[part][dimension])
            / max(1, allowed[part][dimension])
            for dimension in range(num_dimensions)
        )

    while True:
        overloaded_parts = [
            part for part in range(num_parts) if any(overload(part))
        ]
        if not overloaded_parts:
            break
        source = max(
            overloaded_parts,
            key=lambda part: max(
                (
                    loads[part][dimension] - allowed[part][dimension]
                )
                / max(1, allowed[part][dimension])
                for dimension in range(num_dimensions)
            ),
        )
        source_overload = overload(source)
        ranked: List[Tuple[float, float, float, int]] = []
        for vertex in vertices_by_part[source]:
            if labels[vertex] != source:
                continue
            weights = vertex_weights[vertex]
            cluster_id = cluster_order[vertex]
            if clusters[cluster_id]["fixed_fpga"] is not None:
                continue
            relief = sum(
                min(weights[dimension], source_overload[dimension])
                / max(1, allowed[source][dimension])
                for dimension in range(num_dimensions)
            )
            if relief <= 0.0:
                continue
            choices = []
            for target in range(num_parts):
                if target == source or not fits(vertex, target):
                    continue
                delta = cut_delta(vertex, source, target)
                projected_peak = max(
                    (
                        loads[target][dimension] + weights[dimension]
                    )
                    / max(1, allowed[target][dimension])
                    for dimension in range(num_dimensions)
                )
                choices.append((delta, projected_peak, target))
            if choices:
                best_delta, _, _ = min(choices)
                ranked.append(
                    (
                        best_delta / relief,
                        best_delta,
                        -relief,
                        vertex,
                    )
                )
        ranked.sort()

        moved_from_source = 0
        for _, _, _, vertex in ranked:
            source_overload = overload(source)
            if not any(source_overload):
                break
            if labels[vertex] != source:
                continue
            weights = vertex_weights[vertex]
            if not any(
                weights[dimension] and source_overload[dimension]
                for dimension in range(num_dimensions)
            ):
                continue
            choices = []
            for target in range(num_parts):
                if target == source or not fits(vertex, target):
                    continue
                delta = cut_delta(vertex, source, target)
                projected_peak = max(
                    (
                        loads[target][dimension] + weights[dimension]
                    )
                    / max(1, allowed[target][dimension])
                    for dimension in range(num_dimensions)
                )
                choices.append((delta, projected_peak, target))
            if not choices:
                continue
            _, _, best_target = min(choices)
            apply_move(vertex, source, best_target)
            moved_from_source += 1
        if not moved_from_source:
            # A legal multidimensional assignment may require an exchange:
            # every destination can be full in a resource unrelated to the
            # source overload even though moving one blocker away makes room.
            # Search a deterministic two-move augmenting path
            #   target -> destination, source -> target
            # and commit both moves atomically from the repair perspective.
            best_pair = None
            before_source_score = overload_score(source)

            # A naive exchange search is quadratic in the number of vertices:
            # for every source vertex and target it scans every target vertex
            # and every possible destination.  Real designs can have hundreds
            # of thousands of clusters, even though the blocking resource is
            # usually sparse (for example DSP or URAM).  Index blockers by
            # resource weight and cache repeated deficit queries.  This keeps
            # the search complete while restricting each query to vertices
            # that can actually free the target's missing capacity.
            blocker_indexes: Dict[
                int, List[List[Tuple[int, int]]]
            ] = {}
            eligible_blocker_cache: Dict[
                Tuple[int, Tuple[int, ...]], Tuple[int, ...]
            ] = {}
            destination_cache: Dict[
                Tuple[int, int], Optional[Tuple[float, int]]
            ] = {}

            def blocker_index(target: int) -> List[List[Tuple[int, int]]]:
                cached = blocker_indexes.get(target)
                if cached is not None:
                    return cached
                indexes: List[List[Tuple[int, int]]] = [
                    [] for _ in range(num_dimensions)
                ]
                for blocker in vertices_by_part[target]:
                    if labels[blocker] != target:
                        continue
                    blocker_id = cluster_order[blocker]
                    if clusters[blocker_id]["fixed_fpga"] is not None:
                        continue
                    for dimension, weight in enumerate(
                        vertex_weights[blocker]
                    ):
                        indexes[dimension].append((weight, blocker))
                for index in indexes:
                    index.sort()
                blocker_indexes[target] = indexes
                return indexes

            def eligible_blockers(
                target: int, deficits: Sequence[int]
            ) -> Tuple[int, ...]:
                cache_key = (target, tuple(deficits))
                cached = eligible_blocker_cache.get(cache_key)
                if cached is not None:
                    return cached
                indexes = blocker_index(target)
                pivots = []
                for dimension, deficit in enumerate(deficits):
                    if deficit <= 0:
                        continue
                    start = bisect_left(indexes[dimension], (deficit, -1))
                    pivots.append(
                        (len(indexes[dimension]) - start, dimension, start)
                    )
                if not pivots:
                    eligible_blocker_cache[cache_key] = ()
                    return ()
                _, pivot_dimension, start = min(pivots)
                candidates = []
                for _, blocker in indexes[pivot_dimension][start:]:
                    weights = vertex_weights[blocker]
                    if all(
                        weights[dimension] >= deficits[dimension]
                        for dimension in range(num_dimensions)
                    ):
                        candidates.append(blocker)
                result = tuple(candidates)
                eligible_blocker_cache[cache_key] = result
                return result

            def best_non_source_destination(
                blocker: int, target: int
            ) -> Optional[Tuple[float, int]]:
                cache_key = (blocker, target)
                if cache_key in destination_cache:
                    return destination_cache[cache_key]
                choices = []
                blocker_weights = vertex_weights[blocker]
                for destination in range(num_parts):
                    if destination in {source, target}:
                        continue
                    if not fits(blocker, destination):
                        continue
                    choices.append(
                        (
                            cut_delta(blocker, target, destination),
                            destination,
                        )
                    )
                result = min(choices) if choices else None
                destination_cache[cache_key] = result
                return result

            source_candidates = []
            for vertex in vertices_by_part[source]:
                if labels[vertex] != source:
                    continue
                weights = vertex_weights[vertex]
                cluster_id = cluster_order[vertex]
                if clusters[cluster_id]["fixed_fpga"] is not None:
                    continue
                relief = sum(
                    min(weights[dimension], source_overload[dimension])
                    / max(1, allowed[source][dimension])
                    for dimension in range(num_dimensions)
                )
                if relief <= 0.0:
                    continue
                source_without_vertex = [
                    loads[source][dimension] - weights[dimension]
                    for dimension in range(num_dimensions)
                ]
                source_candidates.append(
                    (
                        overload_score(source, source_without_vertex),
                        cluster_id,
                        vertex,
                        relief,
                    )
                )

            # The first tuple field is a lower bound on every exchange using
            # this source vertex: sending a blocker back to the source can
            # only increase its post-move overload.  Once it exceeds the best
            # complete candidate, later vertices cannot win the deterministic
            # ranking and need not be inspected.
            source_candidates.sort()
            for (
                source_without_score,
                cluster_id,
                vertex,
                relief,
            ) in source_candidates:
                if (
                    best_pair is not None
                    and source_without_score > best_pair[0]
                ):
                    break
                weights = vertex_weights[vertex]
                for target in range(num_parts):
                    if target == source:
                        continue
                    source_delta = cut_delta(vertex, source, target)
                    target_after_vertex = [
                        loads[target][dimension] + weights[dimension]
                        for dimension in range(num_dimensions)
                    ]
                    if all(
                        target_after_vertex[dimension]
                        <= allowed[target][dimension]
                        for dimension in range(num_dimensions)
                    ):
                        continue
                    deficits = [
                        max(
                            0,
                            target_after_vertex[dimension]
                            - allowed[target][dimension],
                        )
                        for dimension in range(num_dimensions)
                    ]
                    for blocker in eligible_blockers(target, deficits):
                        blocker_id = cluster_order[blocker]
                        blocker_weights = vertex_weights[blocker]
                        non_source = best_non_source_destination(
                            blocker, target
                        )
                        if non_source is not None:
                            blocker_delta, destination = non_source
                            pair_delta = blocker_delta + source_delta
                            candidate = (
                                source_without_score,
                                pair_delta / relief,
                                pair_delta,
                                cluster_id,
                                blocker_id,
                                target,
                                destination,
                                vertex,
                                blocker,
                            )
                            if best_pair is None or candidate < best_pair:
                                best_pair = candidate

                        destination_after = [
                            loads[source][dimension]
                            - weights[dimension]
                            + blocker_weights[dimension]
                            for dimension in range(num_dimensions)
                        ]
                        after_source_score = overload_score(
                            source, destination_after
                        )
                        if after_source_score >= before_source_score:
                            continue
                        destination = source
                        pair_delta = (
                            cut_delta(blocker, target, destination)
                            + source_delta
                        )
                        candidate = (
                            after_source_score,
                            pair_delta / relief,
                            pair_delta,
                            cluster_id,
                            blocker_id,
                            target,
                            destination,
                            vertex,
                            blocker,
                        )
                        if best_pair is None or candidate < best_pair:
                            best_pair = candidate
            if best_pair is None:
                # A two-move exchange is still insufficient when the target
                # must evict several complementary resource vectors, or when
                # its blocker itself needs room made in another partition.
                # Search a bounded deterministic ejection chain over the
                # complete set of structurally relevant blockers.  The root
                # move is evaluated atomically: intermediate states may exceed
                # a capacity, but the committed sequence must reduce the
                # selected source overload without worsening another existing
                # violation.
                movable_by_part_dimension: List[List[List[int]]] = [
                    [[] for _ in range(num_dimensions)]
                    for _ in range(num_parts)
                ]
                for part in range(num_parts):
                    for candidate_vertex in vertices_by_part[part]:
                        if labels[candidate_vertex] != part:
                            continue
                        candidate_id = cluster_order[candidate_vertex]
                        if clusters[candidate_id]["fixed_fpga"] is not None:
                            continue
                        for dimension, weight in enumerate(
                            vertex_weights[candidate_vertex]
                        ):
                            if weight:
                                movable_by_part_dimension[part][
                                    dimension
                                ].append(candidate_vertex)
                for part_indexes in movable_by_part_dimension:
                    for index in part_indexes:
                        index.sort(key=lambda item: cluster_order[item])

                def exact_sequence_delta(
                    overrides: Mapping[int, int],
                ) -> float:
                    affected_edges = {
                        edge_index
                        for vertex in overrides
                        for edge_index in incident_edges[vertex]
                    }
                    delta = 0.0
                    for edge_index in affected_edges:
                        before_cut = len(edge_part_counts[edge_index]) > 1
                        after_parts = {
                            overrides.get(vertex, labels[vertex])
                            for vertex in edge_vertices[edge_index]
                        }
                        delta += (
                            int(len(after_parts) > 1) - int(before_cut)
                        ) * edge_weights[edge_index]
                    return delta

                def violation_key(
                    state_loads: Sequence[Sequence[int]],
                    limits: Sequence[Sequence[int]],
                ) -> Tuple[int, float, float, int]:
                    violations = []
                    absolute = 0
                    for part in range(num_parts):
                        for dimension in range(num_dimensions):
                            excess = max(
                                0,
                                state_loads[part][dimension]
                                - limits[part][dimension],
                            )
                            if not excess:
                                continue
                            normalized = excess / max(
                                1, allowed[part][dimension]
                            )
                            violations.append(normalized)
                            absolute += excess
                    return (
                        len(violations),
                        sum(violations),
                        max(violations, default=0.0),
                        absolute,
                    )

                # This is a fallback for rare, nearly legal provider outputs.
                # Its limits scale with the number of bins and resource
                # dimensions, not with a benchmark identity.  Beam pruning is
                # deterministic and prioritizes capacity progress before cut
                # cost; the final assignment is always checked by the ordinary
                # independent balance validator.
                max_chain_moves = max(
                    3, min(16, num_parts * 2 + num_dimensions)
                )
                beam_width = max(64, min(512, num_parts * 64))
                transition_width = max(64, min(256, num_parts * 64))

                def find_ejection_chain(
                    root_vertex: int,
                    root_target: int,
                ) -> Optional[Tuple[Tuple[int, int, int], ...]]:
                    root_weights = vertex_weights[root_vertex]
                    limits = [
                        [
                            max(
                                allowed[part][dimension],
                                loads[part][dimension],
                            )
                            for dimension in range(num_dimensions)
                        ]
                        for part in range(num_parts)
                    ]
                    source_after = [
                        loads[source][dimension] - root_weights[dimension]
                        for dimension in range(num_dimensions)
                    ]
                    limits[source] = [
                        max(
                            allowed[source][dimension],
                            source_after[dimension],
                        )
                        for dimension in range(num_dimensions)
                    ]
                    state_loads = [list(item) for item in loads]
                    for dimension, weight in enumerate(root_weights):
                        state_loads[source][dimension] -= weight
                        state_loads[root_target][dimension] += weight
                    root_move = (root_vertex, source, root_target)
                    root_overrides = ((root_vertex, root_target),)
                    root_state = (
                        tuple(tuple(item) for item in state_loads),
                        root_overrides,
                        (root_vertex,),
                        (root_move,),
                        cut_delta(root_vertex, source, root_target),
                    )
                    if violation_key(state_loads, limits)[0] == 0:
                        return (root_move,)
                    beam = [root_state]
                    for _ in range(1, max_chain_moves):
                        next_states = []
                        goals = []
                        seen = set()
                        for (
                            frozen_loads,
                            frozen_overrides,
                            frozen_moved,
                            sequence,
                            approximate_delta,
                        ) in beam:
                            state_loads = [
                                list(item) for item in frozen_loads
                            ]
                            overrides = dict(frozen_overrides)
                            moved = set(frozen_moved)
                            exceeded = []
                            for part in range(num_parts):
                                dimensions_exceeded = [
                                    dimension
                                    for dimension in range(num_dimensions)
                                    if state_loads[part][dimension]
                                    > limits[part][dimension]
                                ]
                                if not dimensions_exceeded:
                                    continue
                                part_excess = max(
                                    (
                                        state_loads[part][dimension]
                                        - limits[part][dimension]
                                    )
                                    / max(1, allowed[part][dimension])
                                    for dimension in dimensions_exceeded
                                )
                                exceeded.append(
                                    (-part_excess, part, dimensions_exceeded)
                                )
                            if not exceeded:
                                goals.append(
                                    (
                                        exact_sequence_delta(overrides),
                                        sequence,
                                    )
                                )
                                continue
                            _, blocked_part, dimensions_exceeded = min(
                                exceeded
                            )
                            pivot = min(
                                dimensions_exceeded,
                                key=lambda dimension: (
                                    len(
                                        movable_by_part_dimension[
                                            blocked_part
                                        ][dimension]
                                    ),
                                    dimension,
                                ),
                            )
                            deficits = [
                                max(
                                    0,
                                    state_loads[blocked_part][dimension]
                                    - limits[blocked_part][dimension],
                                )
                                for dimension in range(num_dimensions)
                            ]
                            transitions = []
                            for blocker in movable_by_part_dimension[
                                blocked_part
                            ][pivot]:
                                if blocker in moved:
                                    continue
                                if (
                                    overrides.get(blocker, labels[blocker])
                                    != blocked_part
                                ):
                                    continue
                                blocker_weights = vertex_weights[blocker]
                                relief = sum(
                                    min(
                                        blocker_weights[dimension],
                                        deficits[dimension],
                                    )
                                    / max(
                                        1,
                                        allowed[blocked_part][dimension],
                                    )
                                    for dimension in range(num_dimensions)
                                )
                                if relief <= 0.0:
                                    continue
                                for destination in range(num_parts):
                                    if destination == blocked_part:
                                        continue
                                    candidate_loads = [
                                        list(item) for item in state_loads
                                    ]
                                    for dimension, weight in enumerate(
                                        blocker_weights
                                    ):
                                        candidate_loads[blocked_part][
                                            dimension
                                        ] -= weight
                                        candidate_loads[destination][
                                            dimension
                                        ] += weight
                                    move_delta = cut_delta(
                                        blocker,
                                        blocked_part,
                                        destination,
                                    )
                                    move = (
                                        blocker,
                                        blocked_part,
                                        destination,
                                    )
                                    transitions.append(
                                        (
                                            violation_key(
                                                candidate_loads, limits
                                            ),
                                            move_delta / relief,
                                            move_delta,
                                            cluster_order[blocker],
                                            destination,
                                            candidate_loads,
                                            move,
                                        )
                                    )
                            for transition in heapq.nsmallest(
                                transition_width, transitions
                            ):
                                (
                                    key,
                                    _,
                                    move_delta,
                                    _,
                                    destination,
                                    candidate_loads,
                                    move,
                                ) = transition
                                blocker = move[0]
                                candidate_overrides = dict(overrides)
                                candidate_overrides[blocker] = destination
                                candidate_sequence = sequence + (move,)
                                candidate_moved = tuple(
                                    sorted((*moved, blocker))
                                )
                                frozen_candidate_loads = tuple(
                                    tuple(item) for item in candidate_loads
                                )
                                state_identity = (
                                    frozen_candidate_loads,
                                    tuple(
                                        sorted(candidate_overrides.items())
                                    ),
                                )
                                if state_identity in seen:
                                    continue
                                seen.add(state_identity)
                                if key[0] == 0:
                                    goals.append(
                                        (
                                            exact_sequence_delta(
                                                candidate_overrides
                                            ),
                                            candidate_sequence,
                                        )
                                    )
                                    continue
                                next_states.append(
                                    (
                                        key,
                                        approximate_delta + move_delta,
                                        tuple(
                                            (
                                                cluster_order[move_vertex],
                                                move_source,
                                                move_target,
                                            )
                                            for (
                                                move_vertex,
                                                move_source,
                                                move_target,
                                            ) in candidate_sequence
                                        ),
                                        frozen_candidate_loads,
                                        tuple(
                                            sorted(
                                                candidate_overrides.items()
                                            )
                                        ),
                                        candidate_moved,
                                        candidate_sequence,
                                    )
                                )
                        if goals:
                            return min(goals)[1]
                        next_states.sort(key=lambda item: item[:3])
                        beam = [
                            (
                                item[3],
                                item[4],
                                item[5],
                                item[6],
                                item[1],
                            )
                            for item in next_states[:beam_width]
                        ]
                        if not beam:
                            break
                    return None

                best_chain = None
                for (
                    source_without_score,
                    cluster_id,
                    vertex,
                    relief,
                ) in source_candidates:
                    for target in range(num_parts):
                        if target == source:
                            continue
                        chain = find_ejection_chain(vertex, target)
                        if chain is None:
                            continue
                        overrides = {
                            moved_vertex: destination
                            for moved_vertex, _, destination in chain
                        }
                        chain_delta = exact_sequence_delta(overrides)
                        candidate = (
                            len(chain),
                            source_without_score,
                            chain_delta / relief,
                            chain_delta,
                            cluster_id,
                            tuple(
                                (
                                    cluster_order[moved_vertex],
                                    move_source,
                                    destination,
                                )
                                for (
                                    moved_vertex,
                                    move_source,
                                    destination,
                                ) in chain
                            ),
                            chain,
                        )
                        if best_chain is None or candidate < best_chain:
                            best_chain = candidate
                    if best_chain is not None and best_chain[0] == 3:
                        # Direct and two-move solutions were already proven
                        # absent, so three moves is globally minimal.
                        break
                if best_chain is None:
                    raise ValidationError(
                        "TritonPart balance repair found no legal direct, "
                        "two-move, or bounded ejection-chain repair for "
                        f"{fpga_order[source]!r}; overload={overload(source)}"
                    )
                chain = best_chain[-1]
                for vertex, move_source, destination in chain:
                    apply_move(vertex, move_source, destination)
                ejection_chain_sequences += 1
                max_ejection_chain_moves = max(
                    max_ejection_chain_moves, len(chain)
                )
                continue
            (
                _,
                _,
                _,
                _,
                _,
                target,
                destination,
                vertex,
                blocker,
            ) = best_pair
            apply_move(blocker, target, destination)
            apply_move(vertex, source, target)
            paired_move_sequences += 1

    final_cut_edges, final_cut_weight = cut_summary()
    validate_cluster_assignment_balance(
        platform,
        clusters_artifact["clusters"],
        assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
    )
    return assignment, {
        "moves": move_total,
        "paired_move_sequences": paired_move_sequences,
        "ejection_chain_sequences": ejection_chain_sequences,
        "max_ejection_chain_moves": max_ejection_chain_moves,
        "move_sha256": move_digest.hexdigest(),
        "move_counts": {
            f"{fpga_order[source]}->{fpga_order[target]}": count
            for (source, target), count in sorted(move_counts.items())
        },
        "moved_weights": {
            f"{fpga_order[source]}->{fpga_order[target]}": {
                dimension: value
                for dimension, value in zip(
                    dimensions,
                    moved_weights[(source, target)],
                )
            }
            for source, target in sorted(moved_weights)
        },
        "dimensions": dimensions,
        "initial_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(
                    dimensions,
                    initial_loads[part],
                )
            }
            for part in range(num_parts)
        },
        "final_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(dimensions, loads[part])
            }
            for part in range(num_parts)
        },
        "allowed_loads": {
            fpga_order[part]: {
                dimension: value
                for dimension, value in zip(dimensions, allowed[part])
            }
            for part in range(num_parts)
        },
        "initial_cut_hyperedges": initial_cut_edges,
        "final_cut_hyperedges": final_cut_edges,
        "initial_cut_weight": initial_cut_weight,
        "final_cut_weight": final_cut_weight,
        "estimated_cut_delta": estimated_cut_delta,
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
    repair_min_used_fpgas: bool = False,
    repair_balance: bool = False,
    persist_input_manifest: bool = True,
    run_unweighted_baseline: bool = False,
) -> Dict[str, Any]:
    if seed_attempts <= 0:
        raise ValueError("TritonPart seed_attempts must be positive")
    if solution_input is not None and seed_attempts != 1:
        raise ValueError(
            "precomputed TritonPart solutions require seed_attempts=1"
        )
    tritonpart_input = export_tritonpart_inputs(
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
    selected_balance_repair: Optional[Dict[str, Any]] = None
    selected_assignment_preview: Optional[Dict[str, Any]] = None
    selected_objective: Optional[Tuple[Any, ...]] = None
    selected_attempt_mode: Optional[str] = None
    attempts = []
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
                f"{spec['hypergraph']}.part.{len(platform.fpgas)}"
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
                shutil.copyfile(solution_input, solution_path)
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
                completed = subprocess.run(
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

        candidate = parse_tritonpart_solution(
            provider_solution_path, tritonpart_input
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
                f"{solution_stem}.part.{len(platform.fpgas)}"
            )
            shutil.copyfile(provider_solution_path, attempt_solution)
        raw_used_fpgas = len(set(candidate.values()))
        balance_repair = None
        repaired_solution_path = None
        if repair_balance:
            try:
                candidate, balance_repair = _repair_multi_resource_balance(
                    candidate,
                    clusters_artifact,
                    platform,
                    constraints,
                    tritonpart_input,
                )
            except ValidationError as error:
                attempts.append(
                    {
                        "mode": spec["mode"],
                        "seed": attempt_seed,
                        "raw_used_fpgas": raw_used_fpgas,
                        "used_fpgas": raw_used_fpgas,
                        "balance_repair": None,
                        "repair_moves": [],
                        "solution": (
                            attempt_solution.name
                            if attempt_solution is not None
                            else solution_path.name
                        ),
                        "repaired_solution": None,
                        "log": (
                            log_path.name if log_path is not None else None
                        ),
                        "accepted": False,
                        "rejection": "balance_repair",
                        "error": str(error),
                    }
                )
                continue
            repaired_tag = (
                f"seed-{attempt_seed}"
                if spec["mode"] == "timing_weighted"
                else f"unweighted-baseline.seed-{attempt_seed}"
            )
            repaired_solution_path = output_dir / (
                (
                    f"partition.hgr.{repaired_tag}.repaired.part."
                    f"{len(platform.fpgas)}"
                )
                if len(attempt_specs) > 1
                else (
                    "partition.hgr.repaired.part."
                    f"{len(platform.fpgas)}"
                )
            )
            fpga_index = {
                fpga_id: index
                for index, fpga_id in enumerate(
                    tritonpart_input["fpga_order"]
                )
            }
            repaired_solution_path.write_text(
                "".join(
                    f"{fpga_index[candidate[cluster_id]]}\n"
                    for cluster_id in tritonpart_input["cluster_order"]
                ),
                encoding="utf-8",
            )
            balance_repair["solution"] = repaired_solution_path.name
        repair_moves: List[Dict[str, Any]] = []
        if (
            repair_min_used_fpgas
            and raw_used_fpgas < constraints["min_used_fpgas"]
        ):
            candidate, repair_moves = _repair_min_used_fpgas(
                candidate,
                clusters_artifact,
                platform,
                constraints,
                tritonpart_input["hyperedges"],
            )
        used_fpgas = len(set(candidate.values()))
        attempt: Dict[str, Any] = {
            "mode": spec["mode"],
            "seed": attempt_seed,
            "raw_used_fpgas": raw_used_fpgas,
            "used_fpgas": used_fpgas,
            "balance_repair": balance_repair,
            "repair_moves": repair_moves,
            "solution": (
                attempt_solution.name
                if attempt_solution is not None
                else solution_path.name
            ),
            "repaired_solution": (
                repaired_solution_path.name
                if repaired_solution_path is not None
                else None
            ),
            "log": log_path.name if log_path is not None else None,
        }
        if used_fpgas < constraints["min_used_fpgas"]:
            attempt["accepted"] = False
            attempt["rejection"] = "min_used_fpgas"
            attempts.append(attempt)
            continue
        try:
            attempt["balance_validation"] = (
                validate_cluster_assignment_balance(
                    platform,
                    clusters_artifact["clusters"],
                    candidate,
                    constraints["balance_tolerance"],
                    constraints.get("balance_tolerance_by_dimension", {}),
                )
            )
        except ValidationError as error:
            attempt["accepted"] = False
            attempt["rejection"] = "multi_resource_balance"
            attempt["error"] = str(error)
            attempts.append(attempt)
            continue

        exact_risk = None
        if clusters_artifact.get("policy", {}).get("cut_mode") == CUT_MODE_STATIC_EXACT:
            try:
                preview = build_partition_assignment(
                    ir,
                    platform,
                    clusters_artifact,
                    constraints,
                    candidate,
                    provider=TRITONPART_PROVIDER,
                    seed=attempt_seed,
                )
            except ValidationError as error:
                attempt["accepted"] = False
                attempt["rejection"] = "static_exact_contract"
                attempt["error"] = str(error)
                attempts.append(attempt)
                continue
            contract_metrics = preview["semantic_contract"]["metrics"]
            exact_risk = {
                "combinational_cut_nets": contract_metrics[
                    "combinational_cut_nets"
                ],
                "maximum_combinational_dependency_depth": contract_metrics[
                    "maximum_combinational_dependency_depth"
                ],
            }
            attempt["static_exact_risk"] = exact_risk

        attempt["accepted"] = True
        cut_edges = [
            edge
            for edge in tritonpart_input["hyperedges"]
            if len(
                {
                    candidate[cluster_id]
                    for cluster_id in edge["clusters"]
                }
            )
            > 1
        ]
        attempt["cut_hyperedges"] = len(cut_edges)
        attempt["cut_weight"] = sum(
            edge["weight"] for edge in cut_edges
        )
        attempts.append(attempt)
        objective = (
            float(attempt["cut_weight"]),
            *(
                (
                    int(
                        exact_risk[
                            "maximum_combinational_dependency_depth"
                        ]
                    ),
                    int(exact_risk["combinational_cut_nets"]),
                )
                if exact_risk is not None
                else ()
            ),
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
                repaired_solution_path
                if repaired_solution_path is not None
                else (
                    attempt_solution
                    if attempt_solution is not None
                    else provider_solution_path
                )
            )
            selected_repair_moves = repair_moves
            selected_balance_repair = balance_repair
            selected_assignment_preview = preview if exact_risk is not None else None

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
    timed_nets = set(net_weights or {})
    timed_cut_edges = [
        edge
        for edge in tritonpart_input["hyperedges"]
        if edge["net"] in timed_nets
        and len(
            {
                cluster_assignment[cluster_id]
                for cluster_id in edge["clusters"]
            }
        )
        > 1
    ]
    timing_weight_result = {
        **tritonpart_input["timing_weight_coverage"],
        "timed_cut_hyperedges": len(timed_cut_edges),
        "timed_cut_weight": sum(
            edge["weight"] for edge in timed_cut_edges
        ),
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
        "min_used_fpgas_repair": {
            "enabled": repair_min_used_fpgas,
            "moves": selected_repair_moves,
        },
        "balance_repair": {
            "enabled": repair_balance,
            "summary": selected_balance_repair,
        },
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
    if selected_assignment_preview is not None:
        # Static Exact candidate qualification already materialized this exact
        # assignment and its semantic contract.  Reuse it instead of walking
        # the complete instance/net graph a second time merely to attach
        # provider metadata.
        result = dict(selected_assignment_preview)
        result["provider_metadata"] = provider_metadata
        return result
    return build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=TRITONPART_PROVIDER,
        seed=selected_seed,
        provider_metadata=provider_metadata,
    )
