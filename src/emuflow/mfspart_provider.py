"""Phase 3 adapter for the source-complete serial MFSPart reproduction."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .io import write_json
from .ir import EmuIR
from .mfspart import build_mfspart_hierarchy
from .mfspart_initial import build_mfspart_initial_partition
from .mfspart_legalize import legalize_mfspart_min_used
from .mfspart_refine import (
    DEFAULT_BOTTLENECK_BETA,
    refine_mfspart_hierarchy,
    refine_mfspart_level,
)
from .partition import (
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .partition_hops import _shortest_hop_distances
from .platform import Platform
from .resources import RESOURCE_FIELDS


MFSPART_PHASE3_PROVIDER = "mfspart-serial-paper-reproduction-v1"
MFSPART_POST_REFINEMENT_PROVIDER = (
    "tritonpart-directional-mfspart-post-refinement-v1"
)
MFSPART_POST_REFINEMENT_SCHEMA = "emuflow.mfspart-post-refinement/v1"


def _platform_problem(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> tuple[
    list[str],
    Dict[str, Dict[str, int]],
    Dict[str, float],
    int,
]:
    parts = [fpga.id for fpga in platform.fpgas]
    raw_distances = _shortest_hop_distances(platform, route_constraints)
    distances: Dict[str, Dict[str, int]] = {}
    for source in parts:
        distances[source] = {}
        for target in parts:
            value = raw_distances[source][target]
            if value is None:
                raise ValidationError(
                    "MFSPart serial provider requires a connected BoardDB; "
                    f"{source!r} cannot reach {target!r}"
                )
            reverse = raw_distances[target][source]
            if reverse != value:
                raise ValidationError(
                    "MFSPart paper mode requires symmetric FPGA hop distances; "
                    f"{source!r}->{target!r}={value}, reverse={reverse}"
                )
            distances[source][target] = value
    diameter = max(value for row in distances.values() for value in row.values())
    configured_hmax = route_constraints.get("max_route_hops")
    hmax = configured_hmax if configured_hmax is not None else max(1, diameter)
    degrees = {
        source: float(
            sum(
                target != source and distances[source][target] == 1
                for target in parts
            )
        )
        for source in parts
    }
    return parts, distances, degrees, hmax


def _balanced_capacities(
    nodes: list[dict[str, Any]],
    platform: Platform,
    dimensions: list[str],
    constraints: Mapping[str, Any],
) -> tuple[Dict[str, Dict[str, int]], Dict[str, float]]:
    parts = [fpga.id for fpga in platform.fpgas]
    totals = {
        dimension: sum(node["weights"][dimension] for node in nodes)
        for dimension in dimensions
    }
    shares: Dict[str, Dict[str, float]] = {}
    for dimension in dimensions:
        if dimension == "cells":
            shares[dimension] = {part: 1.0 / len(parts) for part in parts}
        else:
            capacity_total = sum(
                fpga.effective_capacity[dimension] for fpga in platform.fpgas
            )
            shares[dimension] = {
                fpga.id: fpga.effective_capacity[dimension] / capacity_total
                for fpga in platform.fpgas
            }
    required_ratio = {dimension: 0.0 for dimension in dimensions}
    fixed_loads = {
        part: {dimension: 0 for dimension in dimensions} for part in parts
    }
    for node in nodes:
        fixed_part = node["fixed_part"]
        for dimension in dimensions:
            target_share = (
                shares[dimension][parts[fixed_part]]
                if fixed_part >= 0
                else max(shares[dimension].values())
            )
            required_ratio[dimension] = max(
                required_ratio[dimension],
                node["weights"][dimension]
                / totals[dimension]
                / target_share
                - 1.0,
            )
            if fixed_part >= 0:
                fixed_loads[parts[fixed_part]][dimension] += node["weights"][
                    dimension
                ]
    for part in parts:
        for dimension in dimensions:
            required_ratio[dimension] = max(
                required_ratio[dimension],
                fixed_loads[part][dimension]
                / totals[dimension]
                / shares[dimension][part]
                - 1.0,
            )
    overrides = constraints.get("balance_tolerance_by_dimension", {})
    requested = {
        dimension: float(
            overrides.get(dimension, constraints["balance_tolerance"])
        )
        for dimension in dimensions
    }
    if overrides:
        effective = {
            dimension: max(
                requested[dimension], max(0.0, required_ratio[dimension]) + 0.0001
            )
            for dimension in dimensions
        }
    else:
        shared = max(
            constraints["balance_tolerance"],
            max(0.0, max(required_ratio.values(), default=0.0)) + 0.0001,
        )
        effective = {dimension: shared for dimension in dimensions}
    capacities: Dict[str, Dict[str, int]] = {}
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    for part in parts:
        capacities[part] = {}
        for dimension in dimensions:
            allowed = math.floor(
                totals[dimension]
                * shares[dimension][part]
                * (1.0 + effective[dimension])
                + 1e-9
            )
            if dimension != "cells":
                allowed = min(
                    allowed, fpga_by_id[part].effective_capacity[dimension]
                )
            capacities[part][dimension] = max(1, allowed)
    return capacities, effective


def _partition_graph(
    ir: EmuIR,
    clusters_artifact: Mapping[str, Any],
    platform: Platform,
    net_weights: Mapping[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    clusters = sorted(clusters_artifact["clusters"], key=lambda item: item["id"])
    known_nets = {net["id"] for net in ir.value["nets"]}
    unknown_weights = sorted(set(net_weights) - known_nets)
    if unknown_weights:
        raise ValidationError(
            f"MFSPart net weights reference unknown nets {unknown_weights[:8]}"
        )
    active_resources = [
        field
        for field in RESOURCE_FIELDS
        if any(cluster["resources"].get(field, 0) for cluster in clusters)
        and all(fpga.effective_capacity.get(field, 0) > 0 for fpga in platform.fpgas)
    ]
    dimensions = ["cells", *active_resources]
    fpga_index = {fpga.id: index for index, fpga in enumerate(platform.fpgas)}
    nodes = [
        {
            "id": cluster["id"],
            "fixed_part": (
                fpga_index[cluster["fixed_fpga"]]
                if cluster["fixed_fpga"] is not None
                else -1
            ),
            "weights": {
                "cells": len(cluster["instances"]),
                **{
                    field: cluster["resources"].get(field, 0)
                    for field in active_resources
                },
            },
        }
        for cluster in clusters
    ]
    cluster_by_instance = {
        instance: cluster["id"]
        for cluster in clusters
        for instance in cluster["instances"]
    }
    nets = []
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
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
        for driver_index, driver in enumerate(drivers):
            remote_sinks = [sink for sink in sinks if sink != driver]
            if remote_sinks:
                nets.append(
                    {
                        "id": f"{net['id']}#d{driver_index}",
                        "source": driver,
                        "sinks": remote_sinks,
                        "weight": float(net_weights.get(net["id"], 1.0)),
                    }
                )
    if not nets:
        raise ValidationError(
            "MFSPart partition graph has no transported driver-sink edges"
        )
    return nodes, nets, dimensions


def refine_mfspart_partition(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    assignment: Mapping[str, Any],
    output_dir: Path,
    *,
    net_weights: Optional[Mapping[str, float]] = None,
    early_stop: int = 1000,
    bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    refiner: Optional[str] = None,
    refiner_checker: Optional[str] = None,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Directionally refine a sealed TritonPart assignment in one FM level.

    This is deliberately a post-refinement, not another partition provider.
    It preserves the TritonPart assignment as the starting point and derives
    driver/sink identity directly from EmuIR rather than from TritonPart's
    intentionally undirected hypergraph export.
    """

    if early_stop <= 0:
        raise ValidationError("MFSPart post-refinement early-stop must be positive")
    nodes, nets, dimensions = _partition_graph(
        ir, clusters_artifact, platform, net_weights or {}
    )
    parts, distances, _degrees, hmax = _platform_problem(
        platform, route_constraints
    )
    capacities, effective_balance = _balanced_capacities(
        nodes, platform, dimensions, constraints
    )
    node_index = {node["id"]: index for index, node in enumerate(nodes)}
    graph = {
        "nodes": [
            {
                "fixed_part": node["fixed_part"],
                "weights": [
                    node["weights"][dimension] for dimension in dimensions
                ],
            }
            for node in nodes
        ],
        "nets": [
            {
                "weight": net["weight"],
                "source": node_index[net["source"]],
                "sinks": [node_index[sink] for sink in net["sinks"]],
            }
            for net in nets
        ],
    }
    part_index = {part: index for index, part in enumerate(parts)}
    initial = [
        part_index[assignment["cluster_assignment"][node["id"]]]
        for node in nodes
    ]
    refinement = refine_mfspart_level(
        graph,
        dimensions,
        parts,
        distances,
        capacities,
        initial,
        output_dir,
        hmax=hmax,
        early_stop=early_stop,
        bottleneck_beta=bottleneck_beta,
        executable=refiner,
        checker=refiner_checker,
    )
    refined_cluster_assignment = {
        node["id"]: parts[refinement["assignment"][index]]
        for index, node in enumerate(nodes)
    }
    clusters = sorted(
        clusters_artifact["clusters"], key=lambda item: item["id"]
    )
    validate_cluster_assignment_balance(
        platform,
        clusters,
        refined_cluster_assignment,
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
    )
    used = len(set(refined_cluster_assignment.values()))
    if used < constraints["min_used_fpgas"]:
        raise ValidationError(
            "MFSPart post-refinement emptied a required FPGA: "
            f"used={used}, required={constraints['min_used_fpgas']}"
        )
    metadata = dict(assignment.get("provider_metadata", {}))
    metadata["directional_mfspart_post_refinement"] = {
        "algorithm": MFSPART_POST_REFINEMENT_PROVIDER,
        "initial_assignment_provider": assignment["provider"],
        "early_stop": early_stop,
        "bottleneck_beta": bottleneck_beta,
        "hmax": hmax,
        "moves": len(refinement["moves"]),
        "best_prefix": int(refinement["metrics"]["best_prefix"]),
        "best_cumulative_gain": refinement["metrics"][
            "best_cumulative_gain"
        ],
        "direction_source": "EmuIR net drivers/sinks",
    }
    refined = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        refined_cluster_assignment,
        provider=assignment["provider"],
        seed=assignment["seed"],
        provider_metadata=metadata,
    )
    report = {
        "schema": MFSPART_POST_REFINEMENT_SCHEMA,
        "status": "pass",
        "provider": MFSPART_POST_REFINEMENT_PROVIDER,
        "initial_assignment_provider": assignment["provider"],
        "direction_source": "EmuIR net drivers/sinks",
        "nodes": len(nodes),
        "nets": len(nets),
        "parts": parts,
        "dimensions": dimensions,
        "hmax": hmax,
        "early_stop": early_stop,
        "bottleneck_beta": bottleneck_beta,
        "effective_balance_tolerance_by_dimension": effective_balance,
        "refinement": refinement,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "post_refinement.json", report)
    return refined, report


def run_mfspart(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    output_dir: Path,
    *,
    seed: int = 0,
    net_weights: Optional[Mapping[str, float]] = None,
    coarsener: Optional[str] = None,
    initializer: Optional[str] = None,
    refiner: Optional[str] = None,
    refiner_checker: Optional[str] = None,
    legalizer: Optional[str] = None,
) -> Dict[str, Any]:
    nodes, nets, dimensions = _partition_graph(
        ir, clusters_artifact, platform, net_weights or {}
    )
    parts, distances, degrees, hmax = _platform_problem(
        platform, route_constraints
    )
    capacities, effective_balance = _balanced_capacities(
        nodes, platform, dimensions, constraints
    )
    coarse_bounds = {
        dimension: min(capacities[part][dimension] for part in parts)
        for dimension in dimensions
    }
    distance_matrix = [
        [distances[source][target] for target in parts] for source in parts
    ]
    hierarchy = build_mfspart_hierarchy(
        nodes,
        nets,
        dimensions,
        coarse_bounds,
        output_dir / "coarsening",
        seed=seed,
        fixed_part_distances=distance_matrix,
        executable=coarsener,
    )
    initial = build_mfspart_initial_partition(
        hierarchy,
        parts,
        distances,
        capacities,
        degrees,
        output_dir / "initialization",
        hmax=hmax,
        seed=seed,
        executable=initializer,
    )
    uncoarsening = refine_mfspart_hierarchy(
        hierarchy,
        initial,
        parts,
        distances,
        capacities,
        output_dir / "uncoarsening",
        hmax=hmax,
        executable=refiner,
        checker=refiner_checker,
    )
    legalization = legalize_mfspart_min_used(
        hierarchy["levels"][0],
        dimensions,
        parts,
        capacities,
        uncoarsening["assignment"],
        constraints["min_used_fpgas"],
        output_dir / "legalization",
        executable=legalizer,
    )
    cluster_order = [node["id"] for node in nodes]
    cluster_assignment = {
        cluster_id: parts[legalization["assignment"][index]]
        for index, cluster_id in enumerate(cluster_order)
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "hierarchy.json", hierarchy)
    write_json(output_dir / "initial_partition.json", initial)
    write_json(output_dir / "uncoarsening.json", uncoarsening)
    write_json(output_dir / "legalization.json", legalization)
    return build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=MFSPART_PHASE3_PROVIDER,
        seed=seed,
        provider_metadata={
            "hmax": hmax,
            "dimensions": dimensions,
            "hierarchy_levels": hierarchy["validation"]["levels"],
            "coarsest_nodes": hierarchy["validation"]["coarsest_nodes"],
            "refined_levels": uncoarsening["validation"]["refined_levels"],
            "min_used_legalization_moves": legalization["validation"]["moves"],
            "effective_balance_tolerance_by_dimension": effective_balance,
            "artifacts": {
                "hierarchy": "mfspart/hierarchy.json",
                "initial_partition": "mfspart/initial_partition.json",
                "uncoarsening": "mfspart/uncoarsening.json",
                "legalization": "mfspart/legalization.json",
            },
        },
    )
