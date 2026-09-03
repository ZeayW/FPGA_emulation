"""Provider-neutral candidate-tree contract for system-level routing."""

from __future__ import annotations

import math
import itertools
from collections import defaultdict, deque
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError
from .platform import Platform
from .routing import (
    _arc_key,
    _validate_route_tree,
    build_directed_graph,
    demands_from_assignment,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
)


ROUTE_CANDIDATE_POOL_SCHEMA = "emuflow.route-candidate-pool/v1"
ROUTE_CANDIDATE_POOL_PROVIDER = "native-route-candidate-pool-v1"
ROUTE_CANDIDATE_GENERATORS = (
    "shortest-path-tree",
    "delay-demand-balanced",
    "nearest-terminal-steiner",
    "directed-metric-closure",
    "shallow-light-tree",
    "adaptive-hop-tree",
    "refined-final",
)
ROUTE_MASTER_GENERATORS = ROUTE_CANDIDATE_GENERATORS[:-1]


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


def _tree_sink_delay(
    candidate: Mapping[str, Any],
    edge_delay: Mapping[Tuple[str, str, str], float],
) -> float:
    graph = defaultdict(list)
    for edge in candidate["tree_edges"]:
        key = _arc_key(edge["link"], edge["from"], edge["to"])
        graph[key[1]].append(key)
    delay = {candidate["source"]: 0.0}
    queue = deque([candidate["source"]])
    while queue:
        node = queue.popleft()
        for edge in graph[node]:
            delay[edge[2]] = delay[node] + edge_delay[edge]
            queue.append(edge[2])
    return max(delay[sink] for sink in candidate["sinks"])


def exact_route_candidate_selection(
    assignment: Mapping[str, Any],
    platform: Platform,
    pool: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
    *,
    max_combinations: int = 200_000,
) -> Dict[str, Any]:
    """Exhaustively solve the restricted candidate-tree master problem."""

    validate_route_candidate_pool(assignment, platform, pool)
    constraints = normalize_route_constraints(pool["constraints"], platform)
    demands = demands_from_assignment(assignment, platform)
    _adjacency, arcs, capacities = build_directed_graph(
        platform, constraints
    )
    candidates_by_demand = defaultdict(dict)
    for candidate in pool["candidates"]:
        if candidate["generator"] in ROUTE_MASTER_GENERATORS:
            candidates_by_demand[candidate["demand_id"]][
                candidate["generator"]
            ] = candidate
    ordered_candidates = []
    for demand in demands:
        by_generator = candidates_by_demand[demand["id"]]
        choices = [
            by_generator[generator]
            for generator in ROUTE_MASTER_GENERATORS
            if generator in by_generator
        ]
        if not choices:
            raise ValidationError(
                f"route master has no candidate for {demand['id']}"
            )
        ordered_candidates.append(choices)
    combinations = math.prod(len(choices) for choices in ordered_candidates)
    if combinations > max_combinations:
        raise ValidationError(
            "route master candidate product exceeds exact-oracle limit"
        )

    edge_delay = {
        key: route_link_delay_ns(
            platform, key[0], key[1], key[2], constraints
        )
        for key in arcs
    }
    link_by_id = {link.id: link for link in platform.links}
    sll_links = set(constraints["sll_links"])
    best_objective: Optional[Tuple[float, float, float, int]] = None
    optimal = []
    legal_combinations = 0
    for selected in itertools.product(*ordered_candidates):
        usage = {key: 0 for key in capacities}
        legal = True
        for demand, candidate in zip(demands, selected):
            for edge in candidate["tree_edges"]:
                key = _arc_key(edge["link"], edge["from"], edge["to"])
                capacity_key = arcs[key]["capacity_key"]
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
        legal_combinations += 1
        ratios = {}
        for key, capacity in capacities.items():
            link = link_by_id[capacity["link"]]
            ratios[key] = estimate_tdm_ratio(
                usage[key],
                link.transport_bits_per_cycle_per_direction,
                constraints,
                is_sll=capacity["link"] in sll_links,
            )

        route_delay = {}
        route_tdm_delay = {}
        for demand, candidate in zip(demands, selected):
            route_delay[demand["net"]] = _tree_sink_delay(
                candidate, edge_delay
            )
            tdm_edge_delay = {}
            for edge in candidate["tree_edges"]:
                arc_key = _arc_key(
                    edge["link"], edge["from"], edge["to"]
                )
                link = link_by_id[edge["link"]]
                capacity_key = arcs[arc_key]["capacity_key"]
                tdm_edge_delay[arc_key] = edge_delay[arc_key] + (
                    0.0
                    if edge["link"] in sll_links
                    else (1000.0 / link.fabric_clock_mhz)
                    * (ratios[capacity_key] - 1)
                )
            if constraints.get("tree_edge_sum_tdm", False):
                route_tdm_delay[demand["net"]] = sum(
                    tdm_edge_delay.values()
                )
            else:
                route_tdm_delay[demand["net"]] = _tree_sink_delay(
                    candidate, tdm_edge_delay
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
            "selection": [
                {
                    "demand": demand["id"],
                    "generator": candidate["generator"],
                }
                for demand, candidate in zip(demands, selected)
            ],
            "objective": list(objective),
            "metrics": {
                "worst_tdm_normalized_slack": worst_tdm,
                "worst_normalized_slack": worst_route,
                "max_utilization": max_utilization,
                "total_link_bit_hops": bit_hops,
            },
        }
        if best_objective is None or objective > best_objective:
            best_objective = objective
            optimal = [record]
        elif objective == best_objective:
            optimal.append(record)
    if best_objective is None:
        raise ValidationError("route master exact oracle found no legal solution")
    return {
        "status": "pass",
        "enumerated_combinations": combinations,
        "legal_combinations": legal_combinations,
        "objective": list(best_objective),
        "optimal_selections": [record["selection"] for record in optimal],
        "metrics": optimal[0]["metrics"],
    }


def validate_route_candidate_pool(
    assignment: Mapping[str, Any],
    platform: Platform,
    pool: Mapping[str, Any],
) -> Dict[str, Any]:
    if pool.get("schema") != ROUTE_CANDIDATE_POOL_SCHEMA:
        raise ValidationError(
            "route candidate pool has an unsupported schema"
        )
    if pool.get("provider") != ROUTE_CANDIDATE_POOL_PROVIDER:
        raise ValidationError(
            "route candidate pool has an unsupported provider"
        )
    if pool.get("design") != assignment.get("design"):
        raise ValidationError("route candidate pool design does not match")
    if pool.get("platform") != platform.name:
        raise ValidationError("route candidate pool platform does not match")

    constraints = normalize_route_constraints(
        pool.get("constraints"), platform
    )
    demands = demands_from_assignment(assignment, platform)
    if pool.get("demands") != demands:
        raise ValidationError(
            "route candidate pool demands do not match partition cuts"
        )
    demand_by_id = {demand["id"]: demand for demand in demands}
    _adjacency, arcs, _capacities = build_directed_graph(
        platform, constraints
    )

    locks = pool.get("direction_locks")
    if not isinstance(locks, list):
        raise ValidationError("route candidate pool locks must be an array")
    lock_by_link = {}
    link_by_id = {link.id: link for link in platform.links}
    for index, lock in enumerate(locks):
        if not isinstance(lock, dict):
            raise ValidationError(
                f"route candidate pool lock {index} is invalid"
            )
        link = link_by_id.get(lock.get("link"))
        direction = (lock.get("from"), lock.get("to"))
        if (
            link is None
            or link.direction != "half_duplex"
            or set(direction) != set(link.endpoints)
            or link.id in lock_by_link
        ):
            raise ValidationError(
                "route candidate pool contains an invalid direction lock"
            )
        lock_by_link[link.id] = direction
    expected_locked = {
        link.id for link in platform.links if link.direction == "half_duplex"
    }
    if set(lock_by_link) != expected_locked:
        raise ValidationError(
            "route candidate pool direction-lock coverage is not exact"
        )

    raw_candidates = pool.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValidationError(
            "route candidate pool candidates must be non-empty"
        )
    seen_ids = set()
    seen_pairs = set()
    coverage = defaultdict(set)
    generator_counts = defaultdict(int)
    maximum_hops = 0
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, dict):
            raise ValidationError(
                f"route candidate pool candidate {index} is invalid"
            )
        demand_id = candidate.get("demand_id")
        generator = candidate.get("generator")
        demand = demand_by_id.get(demand_id)
        if demand is None or generator not in ROUTE_CANDIDATE_GENERATORS:
            raise ValidationError(
                f"route candidate pool candidate {index} identity is invalid"
            )
        candidate_id = f"{demand_id}:{generator}"
        if candidate.get("id") != candidate_id or candidate_id in seen_ids:
            raise ValidationError(
                "route candidate pool candidate IDs are not canonical/unique"
            )
        seen_ids.add(candidate_id)
        pair = (demand_id, generator)
        if pair in seen_pairs:
            raise ValidationError(
                "route candidate pool duplicates a demand/generator pair"
            )
        seen_pairs.add(pair)
        for field in ("net", "source", "sinks", "width_bits"):
            if candidate.get(field) != demand[field]:
                raise ValidationError(
                    f"candidate {candidate_id}.{field} does not match demand"
                )
        if candidate.get("selected") != (generator == "refined-final"):
            raise ValidationError(
                f"candidate {candidate_id}.selected is inconsistent"
            )

        edge_keys, latency, hops = _validate_route_tree(candidate, arcs)
        hop_limit = constraints.get("max_route_hops")
        if hop_limit is not None and hops > hop_limit:
            raise ValidationError(
                f"candidate {candidate_id} exceeds maximum route hops"
            )
        if candidate.get("max_latency_cycles") != latency:
            raise ValidationError(
                f"candidate {candidate_id} latency was not reconstructed"
            )
        for edge in edge_keys:
            locked = lock_by_link.get(edge[0])
            if locked is not None and locked != edge[1:]:
                raise ValidationError(
                    f"candidate {candidate_id} violates a direction lock"
                )

        graph = defaultdict(list)
        for link, source, sink in edge_keys:
            graph[source].append((sink, link))
        delay = {candidate["source"]: 0.0}
        queue = deque([candidate["source"]])
        while queue:
            source = queue.popleft()
            for sink, link in graph[source]:
                delay[sink] = delay[source] + route_link_delay_ns(
                    platform, link, source, sink, constraints
                )
                queue.append(sink)
        predicted = max(delay[sink] for sink in candidate["sinks"])
        reported = candidate.get("predicted_max_delay_ns")
        if (
            isinstance(reported, bool)
            or not isinstance(reported, (int, float))
            or not math.isfinite(float(reported))
            or abs(float(reported) - predicted) > 1.0e-9
        ):
            raise ValidationError(
                f"candidate {candidate_id} delay was not reconstructed"
            )
        coverage[demand_id].add(generator)
        generator_counts[generator] += 1
        maximum_hops = max(maximum_hops, hops)

    expected_demand_ids = set(demand_by_id)
    if set(coverage) != expected_demand_ids:
        raise ValidationError(
            "route candidate pool demand coverage is not exact"
        )
    if any("refined-final" not in coverage[item] for item in expected_demand_ids):
        raise ValidationError(
            "route candidate pool lacks one selected candidate per demand"
        )
    generator_coverage = {
        generator: count
        for generator, count in sorted(generator_counts.items())
    }
    expected_metrics = {
        "demands": len(demands),
        "candidates": len(raw_candidates),
        "generators": len(generator_coverage),
        "candidates_by_generator": generator_coverage,
        "max_route_hops_observed": maximum_hops,
    }
    if pool.get("metrics") != expected_metrics:
        raise ValidationError(
            "route candidate pool metrics were not independently reconstructed"
        )
    return {"status": "pass", **expected_metrics}
