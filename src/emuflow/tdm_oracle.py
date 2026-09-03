"""Exact small-instance oracles for Phase 5 ratio legalization."""

from __future__ import annotations

import functools
import itertools
import math
from collections import Counter, defaultdict, deque
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError
from .platform import Platform
from .tdm import COMBINATIONAL_SETTLE_SLOTS, RUNTIME_BARRIER_SLOTS


def exact_discrete_ratio_legalization(
    continuous: Sequence[float],
    directions: Sequence[int],
    *,
    lanes: int,
    allowed_ratios: Sequence[int],
    displacement_bound: float,
) -> Dict[str, Any]:
    """Exhaustively solve TODAES 2020 Equation (18) on one domain.

    Signals are ordered by direction and continuous ratio as justified by the
    exchange argument in the paper.  Every group represents one physical
    lane, has one direction and ratio, and contains no more signals than that
    ratio.  The objective is minimum total displacement under a fixed optimal
    maximum-displacement bound.
    """

    if len(continuous) != len(directions) or not continuous:
        raise ValidationError("TDM oracle input vectors must be non-empty")
    if lanes <= 0 or displacement_bound < 0.0:
        raise ValidationError("TDM oracle capacity/bound is invalid")
    allowed = sorted(set(allowed_ratios))
    if not allowed or allowed[0] <= 0:
        raise ValidationError("TDM oracle allowed ratios are invalid")
    order = sorted(
        range(len(continuous)),
        key=lambda index: (
            directions[index],
            continuous[index],
            index,
        ),
    )

    @functools.lru_cache(maxsize=None)
    def solve(position: int, remaining: int) -> Tuple[float, Tuple[Any, ...]]:
        if position == len(order):
            return 0.0, ()
        if remaining == 0:
            return math.inf, ()
        best = (math.inf, ())
        first = order[position]
        for ratio in allowed:
            if (
                abs(continuous[first] - ratio)
                > displacement_bound + 1.0e-9
            ):
                continue
            displacement = 0.0
            for end in range(
                position,
                min(len(order), position + ratio),
            ):
                signal = order[end]
                if directions[signal] != directions[first]:
                    break
                delta = abs(continuous[signal] - ratio)
                if delta > displacement_bound + 1.0e-9:
                    break
                displacement += delta
                suffix_cost, suffix = solve(end + 1, remaining - 1)
                candidate = (
                    displacement + suffix_cost,
                    ((position, end + 1, ratio), *suffix),
                )
                if candidate[0] < best[0] - 1.0e-9 or (
                    abs(candidate[0] - best[0]) <= 1.0e-9
                    and candidate[1] < best[1]
                ):
                    best = candidate
        return best

    cost, groups = solve(0, lanes)
    if not math.isfinite(cost):
        raise ValidationError("TDM oracle found no legal ratio assignment")
    discrete = [0] * len(order)
    lane_by_signal = [-1] * len(order)
    for lane, (start, end, ratio) in enumerate(groups):
        for position in range(start, end):
            signal = order[position]
            discrete[signal] = ratio
            lane_by_signal[signal] = lane
    return {
        "total_displacement": cost,
        "maximum_displacement": max(
            abs(value - discrete[index])
            for index, value in enumerate(continuous)
        ),
        "discrete_ratios": discrete,
        "lanes": lane_by_signal,
        "groups": [list(group) for group in groups],
    }


def exact_timing_ratio_assignment(
    model: Mapping[str, Any],
    allowed_ratios: Sequence[int],
    *,
    maximum_assignments: int = 1_000_000,
) -> Dict[str, Any]:
    """Exhaustively maximize worst normalized slack on a small ratio model.

    Feasibility matches direction-separated grouping: every physical lane has
    one direction and one ratio, and a ratio-r lane carries at most r signals.
    This oracle is exponential and intentionally restricted to unit tests and
    small QoR witnesses.
    """
    hops = model.get("hops")
    domains = model.get("domains")
    timing_paths = model.get("timing_paths")
    normalization = model.get("normalization")
    if (
        not isinstance(hops, list)
        or not hops
        or not isinstance(domains, list)
        or not domains
        or not isinstance(timing_paths, list)
        or not timing_paths
        or not isinstance(normalization, Mapping)
    ):
        raise ValidationError("timing-ratio oracle model is incomplete")
    allowed = sorted(set(allowed_ratios))
    if not allowed or allowed[0] <= 0:
        raise ValidationError("timing-ratio oracle ratios are invalid")
    assignments = len(allowed) ** len(hops)
    if assignments > maximum_assignments:
        raise ValidationError(
            "timing-ratio oracle search exceeds maximum_assignments"
        )
    lanes_by_domain = {
        domain["index"]: domain["lanes"] for domain in domains
    }
    best_score = None
    best_ratios = None
    best_metrics = None
    feasible_assignments = 0
    for ratios in itertools.product(allowed, repeat=len(hops)):
        groups = Counter(
            (
                hop["domain"],
                hop["direction"],
                hop.get("compatibility", 0),
                ratios[hop["index"]],
            )
            for hop in hops
        )
        lanes_used = Counter()
        for (
            domain,
            _direction,
            _compatibility,
            ratio,
        ), count in groups.items():
            lanes_used[domain] += math.ceil(count / ratio)
        if any(
            lanes_used[domain] > lanes
            for domain, lanes in lanes_by_domain.items()
        ):
            continue
        feasible_assignments += 1
        path_metrics = []
        for timing_path in timing_paths:
            delay = timing_path["fixed_delay_ns"] + sum(
                hops[hop]["base_delay_ns"]
                + hops[hop]["beta_ns"] * (ratios[hop] - 1)
                for hop in timing_path["hops"]
            )
            slack = timing_path.get(
                "required_time_ns", timing_path["clock_period_ns"]
            ) - delay
            path_metrics.append(
                (
                    _normalized_slack(
                        timing_path["clock_period_ns"],
                        slack,
                        normalization,
                    ),
                    delay,
                )
            )
        worst_normalized = min(item[0] for item in path_metrics)
        worst_delay = max(item[1] for item in path_metrics)
        score = (
            worst_normalized,
            -max(ratios),
            -sum(ratios),
            tuple(-ratio for ratio in ratios),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_ratios = ratios
            best_metrics = (worst_normalized, worst_delay, dict(lanes_used))
    if best_ratios is None or best_metrics is None:
        raise ValidationError("timing-ratio oracle found no legal assignment")
    return {
        "status": "optimal",
        "evaluated_assignments": assignments,
        "feasible_assignments": feasible_assignments,
        "discrete_ratios": list(best_ratios),
        "worst_normalized_slack": best_metrics[0],
        "worst_delay_ns": best_metrics[1],
        "lanes_used_by_domain": best_metrics[2],
    }


def _normalized_slack(
    period: float,
    slack: float,
    normalization: Mapping[str, float],
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


def _slot_oracle_model(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
) -> Dict[str, Any]:
    from .routing import normalize_route_constraints
    from .tdm_ratio import validate_tdm_ratio_plan

    validate_tdm_ratio_plan(routes, platform, ratio_plan)
    hops = list(ratio_plan["hops"])
    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    link_by_id = {link.id: link for link in platform.links}
    hop_by_key = {
        (hop["demand"], hop["link"], hop["from"], hop["to"]): hop
        for hop in hops
    }
    parent: Dict[int, Optional[int]] = {}
    depth: Dict[int, int] = {}
    round_by_hop: Dict[int, int] = {}
    sink_hops_by_route: Dict[str, List[int]] = {}
    round_by_route: Dict[str, int] = {}
    for route in routes["routes"]:
        incoming = {}
        outgoing = defaultdict(list)
        for edge in route["tree_edges"]:
            key = (
                route["id"],
                edge["link"],
                edge["from"],
                edge["to"],
            )
            hop = hop_by_key[key]
            incoming[edge["to"]] = hop["index"]
            outgoing[edge["from"]].append(hop)
        queue = deque([(route["source"], 0)])
        while queue:
            node, node_depth = queue.popleft()
            for hop in sorted(
                outgoing[node], key=lambda item: item["index"]
            ):
                parent[hop["index"]] = incoming.get(node)
                depth[hop["index"]] = node_depth
                round_by_hop[hop["index"]] = route.get(
                    "transport_round", 0
                )
                queue.append((hop["to"], node_depth + 1))
        try:
            sink_hops_by_route[route["id"]] = [
                incoming[sink] for sink in route["sinks"]
            ]
        except KeyError as error:
            raise ValidationError(
                "slot oracle route sink has no incoming tree hop"
            ) from error
        round_by_route[route["id"]] = route.get("transport_round", 0)
    if set(parent) != {hop["index"] for hop in hops}:
        raise ValidationError("slot oracle route trees are incomplete")

    ordered = sorted(
        (hop["index"] for hop in hops),
        key=lambda index: (round_by_hop[index], depth[index], index),
    )
    return {
        "constraints": constraints,
        "frame_slots": constraints["frame_slots"],
        "link_by_id": link_by_id,
        "hop_by_index": {hop["index"]: hop for hop in hops},
        "parent": parent,
        "depth": depth,
        "round_by_hop": round_by_hop,
        "sink_hops_by_route": sink_hops_by_route,
        "round_by_route": round_by_route,
        "active_rounds": sorted(set(round_by_route.values())),
        "ordered": ordered,
    }


def _reconstruct_slot_oracle_result(
    model: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    slots: Mapping[int, int],
) -> Dict[str, Any]:
    hop_by_index = model["hop_by_index"]
    expected = set(hop_by_index)
    if set(slots) != expected:
        raise ValidationError("slot oracle hop coverage is not exact")
    if any(
        isinstance(slot, bool) or not isinstance(slot, int)
        for slot in slots.values()
    ):
        raise ValidationError("slot oracle slots must be integers")

    link_by_id = model["link_by_id"]
    frame_slots = model["frame_slots"]
    round_completion: Dict[int, int] = {}
    round_ready: Dict[int, int] = {}
    ready_by_hop: Dict[int, int] = {}
    occupancy = set()
    route_completions: Dict[str, int] = {}

    for transport_round in model["active_rounds"]:
        round_ready[transport_round] = max(
            (
                completion + COMBINATIONAL_SETTLE_SLOTS
                for prior_round, completion in round_completion.items()
                if prior_round < transport_round
            ),
            default=0,
        )
        for index in model["ordered"]:
            if model["round_by_hop"][index] != transport_round:
                continue
            hop = hop_by_index[index]
            parent = model["parent"][index]
            ready = (
                round_ready[transport_round]
                if parent is None
                else slots[parent]
                + link_by_id[hop_by_index[parent]["link"]].latency_cycles
                + COMBINATIONAL_SETTLE_SLOTS
            )
            slot = slots[index]
            latest_exclusive = min(
                ready + hop["discrete_ratio"],
                frame_slots
                - RUNTIME_BARRIER_SLOTS
                - link_by_id[hop["link"]].latency_cycles,
            )
            if slot < ready or slot >= latest_exclusive:
                raise ValidationError(
                    f"slot oracle hop {index} violates its legal window"
                )
            collision = (hop["domain"], hop["lane"], slot)
            if collision in occupancy:
                raise ValidationError(
                    f"slot oracle collision at hop {index}"
                )
            occupancy.add(collision)
            ready_by_hop[index] = ready

        completions = []
        for route, route_round in model["round_by_route"].items():
            if route_round != transport_round:
                continue
            completion = max(
                slots[index]
                + link_by_id[hop_by_index[index]["link"]].latency_cycles
                for index in model["sink_hops_by_route"][route]
            )
            route_completions[route] = completion
            completions.append(completion)
        round_completion[transport_round] = max(completions)

    worst = float("inf")
    for path in ratio_plan["timing_paths"]:
        delay = path["fixed_delay_ns"]
        for index in path["hops"]:
            hop = hop_by_index[index]
            delay += hop["base_delay_ns"] + hop["beta_ns"] * (
                slots[index] - ready_by_hop[index]
            )
        slack = path.get("required_time_ns", path["clock_period_ns"]) - delay
        worst = min(
            worst,
            _normalized_slack(
                path["clock_period_ns"],
                slack,
                ratio_plan["normalization"],
            ),
        )
    completion = max(route_completions.values())
    return {
        "worst_normalized_slack": worst,
        "completion_slot": completion,
        "total_wait_slots": sum(
            slots[index] - ready_by_hop[index] for index in slots
        ),
        "slot_by_hop": dict(slots),
        "ready_by_hop": ready_by_hop,
        "active_rounds": list(model["active_rounds"]),
        "round_source_ready_slots": round_ready,
        "completion_by_round": round_completion,
        "demand_completion_slots": route_completions,
    }


def validate_exact_slot_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Independently reconstruct legality and metrics for an oracle result."""
    model = _slot_oracle_model(routes, platform, ratio_plan)
    raw_slots = result.get("slot_by_hop")
    if not isinstance(raw_slots, dict):
        raise ValidationError("slot oracle result has no slot assignment")
    reconstructed = _reconstruct_slot_oracle_result(
        model, ratio_plan, raw_slots
    )
    for key, value in reconstructed.items():
        if result.get(key) != value:
            raise ValidationError(
                f"slot oracle result {key} does not match reconstruction"
            )
    enumerated = result.get("enumerated_schedules")
    if (
        isinstance(enumerated, bool)
        or not isinstance(enumerated, int)
        or enumerated <= 0
    ):
        raise ValidationError(
            "slot oracle enumerated schedule count is invalid"
        )
    return {
        "status": "pass",
        "hops": len(raw_slots),
        "transport_rounds": len(model["active_rounds"]),
        "enumerated_schedules": enumerated,
    }


def exact_multi_round_slot_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    *,
    max_hops: int = 12,
) -> Dict[str, Any]:
    """Exhaustively optimize a compact multi-round time-expanded schedule.

    Fixed ratio/lane assignments are honored.  The search includes lane
    collision, multicast-tree precedence, link latency, per-hop ratio windows,
    inter-round global barriers, and the final runtime-barrier reservation.
    It is an exact small-instance oracle, not the scalable production solver.
    """
    model = _slot_oracle_model(routes, platform, ratio_plan)
    hops = list(ratio_plan["hops"])
    if len(hops) > max_hops:
        raise ValidationError(
            f"slot oracle supports at most {max_hops} routed hops"
        )
    frame_slots = model["frame_slots"]
    link_by_id = model["link_by_id"]
    hop_by_index = model["hop_by_index"]
    occupancy = set()
    slot_by_hop: Dict[int, int] = {}
    best = None
    explored = 0

    def search(position: int) -> None:
        nonlocal best, explored
        if position == len(model["ordered"]):
            explored += 1
            reconstructed = _reconstruct_slot_oracle_result(
                model, ratio_plan, slot_by_hop
            )
            slots = tuple(
                slot_by_hop[index] for index in sorted(slot_by_hop)
            )
            score = (
                reconstructed["worst_normalized_slack"],
                -reconstructed["completion_slot"],
                -reconstructed["total_wait_slots"],
                tuple(-slot for slot in slots),
            )
            if best is None or score > best[0]:
                best = (score, dict(slot_by_hop), reconstructed)
            return
        index = model["ordered"][position]
        hop = hop_by_index[index]
        parent_index = model["parent"][index]
        transport_round = model["round_by_hop"][index]
        if parent_index is None:
            prior_completions = []
            for route, route_round in model["round_by_route"].items():
                if route_round >= transport_round:
                    continue
                prior_completions.append(
                    max(
                        slot_by_hop[sink_hop]
                        + link_by_id[
                            hop_by_index[sink_hop]["link"]
                        ].latency_cycles
                        for sink_hop in model["sink_hops_by_route"][route]
                    )
                )
            ready = max(
                (
                    completion + COMBINATIONAL_SETTLE_SLOTS
                    for completion in prior_completions
                ),
                default=0,
            )
        else:
            ready = (
                slot_by_hop[parent_index]
                + link_by_id[
                    hop_by_index[parent_index]["link"]
                ].latency_cycles
                + COMBINATIONAL_SETTLE_SLOTS
            )
        latest = min(
            ready + hop["discrete_ratio"],
            frame_slots
            - RUNTIME_BARRIER_SLOTS
            - link_by_id[hop["link"]].latency_cycles,
        )
        key_prefix = (hop["domain"], hop["lane"])
        for slot in range(ready, latest):
            occupancy_key = (*key_prefix, slot)
            if occupancy_key in occupancy:
                continue
            occupancy.add(occupancy_key)
            slot_by_hop[index] = slot
            search(position + 1)
            del slot_by_hop[index]
            occupancy.remove(occupancy_key)

    search(0)
    if best is None:
        raise ValidationError("slot oracle found no legal schedule")
    _score, _slots, reconstructed = best
    result = {
        **reconstructed,
        "enumerated_schedules": explored,
    }
    validate_exact_slot_schedule(routes, platform, ratio_plan, result)
    return result


def exact_single_round_slot_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Mapping[str, Any],
    *,
    max_hops: int = 12,
) -> Dict[str, Any]:
    """Compatibility wrapper for the original single-round oracle."""
    if any(
        route.get("transport_round", 0) != 0
        for route in routes["routes"]
    ):
        raise ValidationError("slot oracle wrapper supports one round")
    return exact_multi_round_slot_schedule(
        routes, platform, ratio_plan, max_hops=max_hops
    )
