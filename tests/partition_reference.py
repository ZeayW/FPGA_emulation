"""Small independent partition initializer oracle; no production entry point."""
from __future__ import annotations
import hashlib
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from emuflow.errors import ValidationError
from emuflow.ir import EmuIR
from emuflow.platform import Platform
from emuflow.partition import (active_partition_fpgas, build_partition_assignment,
                              transported_cut_classes_for_clusters)
from emuflow.partition_limits import planning_resource_capacities, strict_balance_limits
from emuflow.resources import RESOURCE_FIELDS, ResourceVector


def _cluster_adjacency(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Dict[str, int]]:
    adjacency: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                adjacency[driver_cluster][sink_cluster] += 1
                adjacency[sink_cluster][driver_cluster] += 1
    return adjacency


def _cluster_transport_arcs(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    transported_cut_classes: Set[str],
) -> Dict[str, Set[Tuple[str, bool]]]:
    """Return neighbor arcs and whether the keyed cluster drives the arc."""
    arcs: Dict[str, Set[Tuple[str, bool]]] = defaultdict(set)
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["drivers"]
            if endpoint["instance"] is not None
        }
        sink_clusters = {
            cluster_by_instance[endpoint["instance"]]
            for endpoint in net["sinks"]
            if endpoint["instance"] is not None
        }
        for driver_cluster in driver_clusters:
            for sink_cluster in sink_clusters:
                if driver_cluster == sink_cluster:
                    continue
                arcs[driver_cluster].add((sink_cluster, True))
                arcs[sink_cluster].add((driver_cluster, False))
    return arcs


def _resource_add(
    left: Mapping[str, int], right: Mapping[str, int]
) -> Dict[str, int]:
    return {
        field: left.get(field, 0) + right.get(field, 0)
        for field in RESOURCE_FIELDS
    }


def _fits(resources: Mapping[str, int], capacity: Mapping[str, int]) -> bool:
    return all(resources.get(field, 0) <= limit for field, limit in capacity.items())


def _seeded_tie(seed: int, cluster_id: str) -> str:
    return hashlib.sha256(f"{seed}:{cluster_id}".encode("utf-8")).hexdigest()


def _partition_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    # Import locally because routing owns the canonical directed BoardDB graph
    # and imports the partition artifact schema at module initialization.
    from emuflow.routing import (
        SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
        build_directed_graph,
        normalize_route_constraints,
    )

    raw_constraints = dict(route_constraints)
    raw_constraints.setdefault("schema", SYSTEM_ROUTE_CONSTRAINTS_SCHEMA)
    normalized = normalize_route_constraints(raw_constraints, platform)
    adjacency, _, _ = build_directed_graph(platform, normalized)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distance = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                neighbor = arc["to"]
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)
        result[source] = {
            sink: distance.get(sink) for sink in sorted(adjacency)
        }
    return result


def assign_clusters(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    seed: int = 0,
    route_constraints: Optional[Mapping[str, Any]] = None,
    *,
    _include_semantic_contract: bool = True,
) -> Dict[str, Any]:
    if len(ir.value["instances"]) > 4096:
        raise AssertionError("partition oracle is limited to small contract fixtures")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("partition seed: expected a non-negative integer")

    clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    if len(clusters) < constraints["min_used_fpgas"]:
        raise ValidationError(
            f"partitioning has {len(clusters)} atomic clusters but "
            f"{constraints['min_used_fpgas']} FPGAs must be used"
        )
    cluster_by_instance = {
        instance_id: cluster_id
        for cluster_id, cluster in clusters.items()
        for instance_id in cluster["instances"]
    }
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    adjacency = _cluster_adjacency(
        ir, cluster_by_instance, transported_cut_classes
    )
    transport_arcs = _cluster_transport_arcs(
        ir, cluster_by_instance, transported_cut_classes
    )
    hop_limit = (
        route_constraints.get("max_route_hops")
        if route_constraints is not None
        else None
    )
    hop_distances = (
        _partition_hop_distances(platform, route_constraints)
        if hop_limit is not None
        else None
    )
    partition_fpgas = active_partition_fpgas(platform, constraints)
    fpga_ids = [fpga.id for fpga in partition_fpgas]
    planning_capacity = planning_resource_capacities(
        partition_fpgas,
        constraints.get("resource_reservations_by_fpga", {}),
    )
    loads = {
        fpga_id: {field: 0 for field in RESOURCE_FIELDS}
        for fpga_id in fpga_ids
    }

    total_resources = ResourceVector.sum(
        ResourceVector.from_mapping(cluster["resources"])
        for cluster in clusters.values()
    ).to_dict()
    tolerance = constraints["balance_tolerance"]
    dimension_tolerances = constraints.get(
        "balance_tolerance_by_dimension", {}
    )
    soft_caps = strict_balance_limits(
        partition_fpgas,
        total_resources,
        tolerance,
        dimension_tolerances,
        constraints.get("balance_dimensions", ()),
        constraints.get("resource_reservations_by_fpga", {}),
    )

    def dominant_size(cluster: Mapping[str, Any]) -> float:
        resources = cluster["resources"]
        ratios = []
        for fpga_id in fpga_ids:
            capacity = planning_capacity[fpga_id]
            ratios.extend(
                resources.get(field, 0) / capacity[field]
                for field in capacity
                if capacity[field] > 0
            )
        return max(ratios, default=0.0)

    order = sorted(
        clusters,
        key=lambda cluster_id: (
            -sum(adjacency.get(cluster_id, {}).values())
            if hop_limit is not None
            else 0,
            -dominant_size(clusters[cluster_id]),
            -len(clusters[cluster_id]["instances"]),
            _seeded_tie(seed, cluster_id),
            cluster_id,
        ),
    )
    assignment: Dict[str, str] = {}

    def hop_legal(cluster_id: str, fpga_id: str) -> bool:
        if hop_limit is None or hop_distances is None:
            return True
        for neighbor, cluster_is_driver in transport_arcs.get(
            cluster_id, set()
        ):
            if neighbor not in assignment:
                continue
            if cluster_is_driver:
                distance = hop_distances[fpga_id][assignment[neighbor]]
            else:
                distance = hop_distances[assignment[neighbor]][fpga_id]
            if distance is None or distance > hop_limit:
                return False
        return True

    def place(cluster_id: str, fpga_id: str) -> None:
        cluster_resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        projected = _resource_add(loads[fpga_id], cluster_resources)
        if not _fits(projected, planning_capacity[fpga_id]):
            raise ValidationError(
                f"cluster {cluster_id!r} does not fit FPGA {fpga_id!r}"
            )
        assignment[cluster_id] = fpga_id
        loads[fpga_id] = projected

    for cluster_id in order:
        fixed_fpga = clusters[cluster_id]["fixed_fpga"]
        if fixed_fpga is not None:
            place(cluster_id, fixed_fpga)

    populated = {fpga_id for fpga_id in assignment.values()}
    for fpga_id in fpga_ids:
        if len(populated) >= constraints["min_used_fpgas"]:
            break
        if fpga_id in populated:
            continue
        candidate = next(
            (
                cluster_id
                for cluster_id in order
                if cluster_id not in assignment
                and hop_legal(cluster_id, fpga_id)
                and _fits(
                    _resource_add(
                        loads[fpga_id],
                        ResourceVector.from_mapping(
                            clusters[cluster_id]["resources"]
                        ).to_dict(),
                    ),
                    planning_capacity[fpga_id],
                )
            ),
            None,
        )
        if candidate is None and hop_limit is not None:
            candidate = next(
                (
                    cluster_id
                    for cluster_id in order
                    if cluster_id not in assignment
                    and _fits(
                        _resource_add(
                            loads[fpga_id],
                            ResourceVector.from_mapping(
                                clusters[cluster_id]["resources"]
                            ).to_dict(),
                        ),
                        planning_capacity[fpga_id],
                    )
                ),
                None,
            )
        if candidate is None:
            raise ValidationError(
                f"cannot populate required FPGA {fpga_id!r} within capacity"
            )
        place(candidate, fpga_id)
        populated.add(fpga_id)

    for cluster_id in order:
        if cluster_id in assignment:
            continue
        resources = ResourceVector.from_mapping(
            clusters[cluster_id]["resources"]
        ).to_dict()
        capacity_candidates = [
            fpga_id
            for fpga_id in fpga_ids
            if _fits(
                _resource_add(loads[fpga_id], resources),
                planning_capacity[fpga_id],
            )
        ]
        if not capacity_candidates:
            raise ValidationError(
                f"cluster {cluster_id!r} cannot fit any FPGA; resources "
                f"{clusters[cluster_id]['resources']}"
            )
        topology_candidates = [
            fpga_id
            for fpga_id in capacity_candidates
            if hop_legal(cluster_id, fpga_id)
        ]
        actual_candidates = topology_candidates or capacity_candidates
        balanced_candidates = [
            fpga_id
            for fpga_id in actual_candidates
            if _fits(
                _resource_add(loads[fpga_id], resources),
                soft_caps[fpga_id],
            )
        ]
        candidates = balanced_candidates or actual_candidates

        def score(fpga_id: str) -> Tuple[float, float, float, str]:
            cut_cost = sum(
                weight
                for neighbor, weight in adjacency.get(cluster_id, {}).items()
                if neighbor in assignment and assignment[neighbor] != fpga_id
            )
            projected = _resource_add(loads[fpga_id], resources)
            ratios = [
                projected[field] / planning_capacity[fpga_id][field]
                for field in planning_capacity[fpga_id]
                if planning_capacity[fpga_id][field] > 0
            ]
            return (
                float(cut_cost),
                max(ratios, default=0.0),
                sum(ratios),
                fpga_id,
            )

        place(cluster_id, min(candidates, key=score))

    return build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        assignment,
        provider="deterministic-multiresource-greedy-v1",
        seed=seed,
        _include_semantic_contract=_include_semantic_contract,
    )


