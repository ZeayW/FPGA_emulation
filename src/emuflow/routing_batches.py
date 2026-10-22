"""Independent conflict-batch reconstruction for route refinement."""

from __future__ import annotations

from array import array
from collections import defaultdict
from functools import cmp_to_key
from typing import Any, Dict, List, Mapping, Set

from .platform import Platform
from .routing import _arc_key, build_directed_graph, demands_from_assignment
from .routing_candidates import ROUTE_MASTER_GENERATORS


_SLACK_EPSILON = 1.0e-12


def _native_path_slack(
    path: Mapping[str, Any], normalization: Mapping[str, Any]
) -> float:
    slack = float(path["baseline_slack_ns"])
    period = float(path["clock_period_ns"])
    if slack >= 0.0:
        return (
            slack
            * period
            / (
                float(normalization["positive_slack_scale_ns"])
                * float(normalization["max_clock_period_ns"])
            )
        )
    return slack / (
        float(normalization["negative_slack_scale_ns"]) * period
    )


def build_compact_route_refinement_batches(
    native_paths: List[Mapping[str, Any]],
    normalization: Mapping[str, Any],
    domains_by_demand: List[int],
) -> Dict[str, Any]:
    """Reconstruct native conflict batches without candidate dictionaries.

    ``domains_by_demand`` stores the union of capacity domains touched by all
    master candidates as one Python integer bitset per demand.  The inverse
    timing-path relation is kept in two fixed-width arrays.  This preserves
    the independent batch reconstruction while avoiding candidate-sized
    trees, per-path dictionaries and sets on the managed production path.
    """

    path_count = len(native_paths)
    demand_count = len(domains_by_demand)
    degrees = array("I", [0]) * demand_count

    def selected_demands(path_index: int) -> tuple[int, ...]:
        selected = tuple(
            sorted(
                {int(item) for item in native_paths[path_index]["demands"]}
            )
        )
        if any(item < 0 or item >= demand_count for item in selected):
            raise ValueError("route refinement path demand is out of range")
        return selected

    for path_index in range(path_count):
        selected = selected_demands(path_index)
        for demand in selected:
            degrees[demand] += 1

    offsets = array("Q", [0]) * (demand_count + 1)
    for index, degree in enumerate(degrees):
        offsets[index + 1] = offsets[index] + degree
    paths_by_demand = array("I", [0]) * offsets[-1]
    cursors = array("Q", offsets[:-1])
    for path_index in range(path_count):
        selected = selected_demands(path_index)
        for demand in selected:
            position = cursors[demand]
            paths_by_demand[position] = path_index
            cursors[demand] = position + 1
    del degrees, cursors

    slacks = [
        _native_path_slack(path, normalization) for path in native_paths
    ]

    def compare(left: int, right: int) -> int:
        delta = slacks[left] - slacks[right]
        if abs(delta) > _SLACK_EPSILON:
            return -1 if delta < 0.0 else 1
        return (left > right) - (left < right)

    order = sorted(range(path_count), key=cmp_to_key(compare))
    del slacks
    batches: List[List[int]] = []
    if path_count <= 4096:
        batch_footprints: List[List[tuple[int, int]]] = []
        for path_index in order:
            domain_mask = 0
            affected_mask = 0
            for demand in selected_demands(path_index):
                domain_mask |= domains_by_demand[demand]
                for position in range(offsets[demand], offsets[demand + 1]):
                    affected_mask |= 1 << paths_by_demand[position]
            for batch_index, footprints in enumerate(batch_footprints):
                if all(
                    not (domain_mask & other_domains)
                    and not (affected_mask & other_paths)
                    for other_domains, other_paths in footprints
                ):
                    batches[batch_index].append(path_index)
                    footprints.append((domain_mask, affected_mask))
                    break
            else:
                batches.append([path_index])
                batch_footprints.append([(domain_mask, affected_mask)])
    else:
        domain_count = max(
            (mask.bit_length() for mask in domains_by_demand), default=0
        )
        last_domain_batch = array("i", [-1]) * domain_count
        last_path_batch = array("i", [-1]) * path_count
        for path_index in order:
            domain_mask = 0
            batch_index = 0
            selected = selected_demands(path_index)
            for demand in selected:
                domain_mask |= domains_by_demand[demand]
                for position in range(offsets[demand], offsets[demand + 1]):
                    affected = paths_by_demand[position]
                    batch_index = max(
                        batch_index, last_path_batch[affected] + 1
                    )
            remaining = domain_mask
            while remaining:
                least_bit = remaining & -remaining
                domain = least_bit.bit_length() - 1
                batch_index = max(
                    batch_index, last_domain_batch[domain] + 1
                )
                remaining ^= least_bit
            while len(batches) <= batch_index:
                batches.append([])
            batches[batch_index].append(path_index)
            remaining = domain_mask
            while remaining:
                least_bit = remaining & -remaining
                domain = least_bit.bit_length() - 1
                last_domain_batch[domain] = batch_index
                remaining ^= least_bit
            for demand in selected:
                for position in range(offsets[demand], offsets[demand + 1]):
                    last_path_batch[paths_by_demand[position]] = batch_index

    return {
        "batches": batches,
        "batch_count": len(batches),
        "maximum_parallel_batch": max(map(len, batches), default=0),
    }


def build_route_refinement_batches(
    assignment: Mapping[str, Any],
    platform: Platform,
    candidate_pool: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
) -> Dict[str, Any]:
    """Color paths whose candidate-domain/path footprints do not conflict."""

    demands = demands_from_assignment(assignment, platform)
    demand_index = {demand["id"]: index for index, demand in enumerate(demands)}
    _adjacency, arcs, _capacities = build_directed_graph(
        platform, candidate_pool["constraints"]
    )
    domains_by_demand: Dict[int, Set[str]] = defaultdict(set)
    for candidate in candidate_pool["candidates"]:
        if candidate["generator"] not in {
            *ROUTE_MASTER_GENERATORS,
        }:
            continue
        index = demand_index[candidate["demand_id"]]
        transport_round = int(candidate.get("transport_round", 0))
        for edge in candidate["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            domains_by_demand[index].add(
                f"{arcs[key]['capacity_key']}@transport-round-"
                f"{transport_round}"
            )
            if arcs[key].get("bank_group"):
                domains_by_demand[index].add(
                    f"{arcs[key]['opposite_capacity_key']}@transport-round-{transport_round}"
                )

    net_to_demand = {demand["net"]: index for index, demand in enumerate(demands)}
    paths_by_demand: Dict[int, Set[int]] = defaultdict(set)
    path_demands: Dict[int, List[int]] = {}
    for path_index, path in enumerate(timing_paths["paths"]):
        selected = sorted({net_to_demand[net] for net in path["cut_nets"]})
        path_demands[path_index] = selected
        for demand in selected:
            paths_by_demand[demand].add(path_index)

    records = []
    for path_index, path in enumerate(timing_paths["paths"]):
        selected = path_demands[path_index]
        records.append(
            {
                "path_index": path_index,
                "path": path["id"],
                "normalized_slack": path["normalized_slack"],
                "demands": [demands[index]["id"] for index in selected],
                "capacity_domains": sorted(
                    {
                        domain
                        for demand in selected
                        for domain in domains_by_demand[demand]
                    }
                ),
                "affected_paths": sorted(
                    {
                        affected
                        for demand in selected
                        for affected in paths_by_demand[demand]
                    }
                ),
            }
        )
    native_slack = {
        path["id"]: (
            path["slack_ns"]
            * path["clock_period_ns"]
            / (
                timing_paths["normalization"]["positive_slack_scale_ns"]
                * timing_paths["normalization"]["max_clock_period_ns"]
            )
            if path["slack_ns"] >= 0.0
            else path["slack_ns"]
            / (
                timing_paths["normalization"]["negative_slack_scale_ns"]
                * path["clock_period_ns"]
            )
        )
        for path in timing_paths["paths"]
    }
    records.sort(
        key=lambda item: (native_slack[item["path"]], item["path_index"])
    )
    batches: List[List[Dict[str, Any]]] = []
    if len(records) <= 4096:
        for record in records:
            domains = set(record["capacity_domains"])
            affected = set(record["affected_paths"])
            for batch in batches:
                if all(
                    domains.isdisjoint(other["capacity_domains"])
                    and affected.isdisjoint(other["affected_paths"])
                    for other in batch
                ):
                    batch.append(record)
                    break
            else:
                batches.append([record])
        return {
            "batches": [
                [record["path_index"] for record in batch]
                for batch in batches
            ],
            "batch_count": len(batches),
            "maximum_parallel_batch": max(map(len, batches), default=0),
        }
    last_domain_batch: Dict[str, int] = {}
    last_path_batch: Dict[int, int] = {}
    for record in records:
        batch_index = max(
            [last_domain_batch.get(domain, -1) + 1
             for domain in record["capacity_domains"]]
            + [last_path_batch.get(path, -1) + 1
               for path in record["affected_paths"]]
            + [0]
        )
        while len(batches) <= batch_index:
            batches.append([])
        batches[batch_index].append(record)
        for domain in record["capacity_domains"]:
            last_domain_batch[domain] = batch_index
        for path in record["affected_paths"]:
            last_path_batch[path] = batch_index
    return {
        "batches": [
            [record["path_index"] for record in batch] for batch in batches
        ],
        "batch_count": len(batches),
        "maximum_parallel_batch": max(map(len, batches), default=0),
    }
