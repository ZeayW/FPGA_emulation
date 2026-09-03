"""Exact small-instance oracle for Phase 4 routing-tree selection.

The production router is deliberately heuristic.  This module exhaustively
enumerates direction-feasible directed arborescences and their global
combinations, and is therefore only suitable for tiny academic/regression
instances.  It provides an independent optimum for the same lexicographic
objective used by the C++ Phase 4 provider.
"""

from __future__ import annotations

import itertools
import math
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import ValidationError
from .platform import Platform
from .routing import (
    ArcKey,
    build_directed_graph,
    demands_from_assignment,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
)


def _tree_distances(
    source: str,
    edges: Sequence[ArcKey],
    edge_delay: Mapping[ArcKey, float],
) -> Dict[str, float]:
    graph: Dict[str, List[ArcKey]] = defaultdict(list)
    for edge in edges:
        graph[edge[1]].append(edge)
    distances = {source: 0.0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for edge in graph[node]:
            distances[edge[2]] = distances[node] + edge_delay[edge]
            queue.append(edge[2])
    return distances


def _tree_hops(
    source: str,
    edges: Sequence[ArcKey],
) -> Dict[str, int]:
    graph: Dict[str, List[str]] = defaultdict(list)
    for _link, start, end in edges:
        graph[start].append(end)
    hops = {source: 0}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for sink in graph[node]:
            hops[sink] = hops[node] + 1
            queue.append(sink)
    return hops


def _is_arborescence(
    source: str,
    sinks: Sequence[str],
    edges: Sequence[ArcKey],
) -> bool:
    indegree: Dict[str, int] = defaultdict(int)
    graph: Dict[str, List[str]] = defaultdict(list)
    for _link, start, end in edges:
        if end == source:
            return False
        indegree[end] += 1
        if indegree[end] > 1:
            return False
        graph[start].append(end)
    reached = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for end in graph[node]:
            if end in reached:
                return False
            reached.add(end)
            queue.append(end)
    if any(start not in reached or end not in reached for _, start, end in edges):
        return False
    if not set(sinks) <= reached or len(edges) != len(reached) - 1:
        return False
    sink_set = set(sinks)
    return all(graph[node] or node in sink_set for node in reached - {source})


def _tree_candidates(
    source: str,
    sinks: Sequence[str],
    arcs: Sequence[ArcKey],
    node_count: int,
) -> List[Tuple[ArcKey, ...]]:
    candidates = []
    for count in range(1, node_count):
        for edges in itertools.combinations(arcs, count):
            if _is_arborescence(source, sinks, edges):
                candidates.append(tuple(sorted(edges)))
    return sorted(set(candidates), key=lambda value: (len(value), value))


def _normalized_slack(
    period: float,
    slack: float,
    normalization: Mapping[str, Any],
) -> float:
    if slack >= 0.0:
        return (
            slack
            * period
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
        )
    return slack / (
        normalization["negative_slack_scale_ns"] * period
    )


def exact_route_tree_selection(
    assignment: Mapping[str, Any],
    platform: Platform,
    constraints_value: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
    *,
    max_arcs: int = 16,
    max_combinations: int = 2_000_000,
) -> Dict[str, Any]:
    """Return the exact Phase 4 optimum for a tiny directed board graph."""

    constraints = normalize_route_constraints(constraints_value, platform)
    demands = demands_from_assignment(assignment, platform)
    _adjacency, arcs, capacities = build_directed_graph(
        platform, constraints
    )
    if len(arcs) > max_arcs:
        raise ValidationError(
            f"routing oracle supports at most {max_arcs} directed arcs"
        )
    route_by_net = {demand["net"]: demand for demand in demands}
    timing_nets = {
        net for path in timing_paths["paths"] for net in path["cut_nets"]
    }
    if not timing_nets <= set(route_by_net):
        raise ValidationError("routing oracle timing paths name unknown nets")

    half_duplex = [
        link for link in platform.links if link.direction == "half_duplex"
    ]
    lock_choices = itertools.product((0, 1), repeat=len(half_duplex))
    link_by_id = {link.id: link for link in platform.links}
    edge_delay = {
        key: route_link_delay_ns(
            platform, key[0], key[1], key[2], constraints
        )
        for key in arcs
    }
    sll_links = set(constraints["sll_links"])
    best = None
    enumerated = 0

    for choice in lock_choices:
        allowed = set(arcs)
        locks = {}
        for link, direction in zip(half_duplex, choice):
            start = link.endpoints[direction]
            end = link.endpoints[1 - direction]
            locks[link.id] = (start, end)
            allowed = {
                edge
                for edge in allowed
                if edge[0] != link.id or edge[1:] == (start, end)
            }
        candidates_by_demand = []
        for demand in demands:
            candidates = _tree_candidates(
                demand["source"],
                demand["sinks"],
                sorted(allowed),
                len(platform.fpgas),
            )
            maximum_hops = constraints.get("max_route_hops")
            if maximum_hops is not None:
                candidates = [
                    tree
                    for tree in candidates
                    if max(
                        _tree_hops(demand["source"], tree)[sink]
                        for sink in demand["sinks"]
                    )
                    <= maximum_hops
                ]
            if not candidates:
                candidates_by_demand = []
                break
            candidates_by_demand.append(candidates)
        if not candidates_by_demand:
            continue
        combination_count = math.prod(
            len(candidates) for candidates in candidates_by_demand
        )
        if enumerated + combination_count > max_combinations:
            raise ValidationError(
                "routing oracle candidate product exceeds limit"
            )
        enumerated += combination_count

        for selected in itertools.product(*candidates_by_demand):
            usage = {key: 0 for key in capacities}
            legal = True
            for demand, tree in zip(demands, selected):
                for edge in tree:
                    capacity_key = arcs[edge]["capacity_key"]
                    usage[capacity_key] += demand["width_bits"]
                    if usage[capacity_key] > capacities[capacity_key][
                        "capacity_bits"
                    ]:
                        legal = False
                        break
                if not legal:
                    break
            if not legal:
                continue

            ratios = {}
            for key, capacity in capacities.items():
                link = link_by_id[capacity["link"]]
                signals = usage[key]
                ratios[key] = estimate_tdm_ratio(
                    signals,
                    link.transport_bits_per_cycle_per_direction,
                    constraints,
                    is_sll=capacity["link"] in sll_links,
                )

            route_delay = {}
            route_tdm_delay = {}
            for demand, tree in zip(demands, selected):
                distances = _tree_distances(
                    demand["source"], tree, edge_delay
                )
                tdm_edge_delay = {}
                for edge in tree:
                    link = link_by_id[edge[0]]
                    capacity_key = arcs[edge]["capacity_key"]
                    tdm_edge_delay[edge] = edge_delay[edge] + (
                        0.0
                        if edge[0] in sll_links
                        else (1000.0 / link.fabric_clock_mhz)
                        * (ratios[capacity_key] - 1)
                    )
                tdm_distances = _tree_distances(
                    demand["source"], tree, tdm_edge_delay
                )
                route_delay[demand["net"]] = max(
                    distances[sink] for sink in demand["sinks"]
                )
                if constraints.get("tree_edge_sum_tdm", False):
                    route_tdm_delay[demand["net"]] = sum(
                        tdm_edge_delay[edge] for edge in tree
                    )
                else:
                    route_tdm_delay[demand["net"]] = max(
                        tdm_distances[sink] for sink in demand["sinks"]
                    )

            worst_route = float("inf")
            worst_tdm = float("inf")
            for path in timing_paths["paths"]:
                required_time = path.get(
                    "required_time_ns",
                    (
                        path["fixed_delay_ns"] + path["slack_ns"]
                        if "slack_ns" in path
                        else path["clock_period_ns"]
                    ),
                )
                route_total = path["fixed_delay_ns"] + sum(
                    route_delay[net] for net in path["cut_nets"]
                )
                tdm_total = path["fixed_delay_ns"] + sum(
                    route_tdm_delay[net] for net in path["cut_nets"]
                )
                worst_route = min(
                    worst_route,
                    _normalized_slack(
                        path["clock_period_ns"],
                        required_time - route_total,
                        timing_paths["normalization"],
                    ),
                )
                worst_tdm = min(
                    worst_tdm,
                    _normalized_slack(
                        path["clock_period_ns"],
                        required_time - tdm_total,
                        timing_paths["normalization"],
                    ),
                )
            max_utilization = max(
                (
                    usage[key] / capacity["capacity_bits"]
                    for key, capacity in capacities.items()
                ),
                default=0.0,
            )
            bit_hops = sum(usage.values())
            objective = (
                worst_tdm,
                worst_route,
                -max_utilization,
                -bit_hops,
            )
            record = {
                "objective": objective,
                "trees": {
                    demand["net"]: [list(edge) for edge in tree]
                    for demand, tree in zip(demands, selected)
                },
                "direction_locks": locks,
                "metrics": {
                    "worst_tdm_normalized_slack": worst_tdm,
                    "worst_normalized_slack": worst_route,
                    "max_utilization": max_utilization,
                    "total_link_bit_hops": bit_hops,
                },
            }
            if best is None or record["objective"] > best["objective"]:
                best = record

    if best is None:
        raise ValidationError("routing oracle found no legal solution")
    best["enumerated_combinations"] = enumerated
    return best
