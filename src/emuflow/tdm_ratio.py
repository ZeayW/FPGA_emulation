"""Timing-aware TDM-ratio optimization and independent legality checks."""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .platform import Platform
from .routing import (
    SYSTEM_ROUTES_SCHEMA,
    build_directed_graph,
    normalize_route_constraints,
    route_link_delay_ns,
)
from .tdm import RUNTIME_BARRIER_SLOTS
from .tdm_compatibility import (
    derive_tdm_compatibility,
    validate_tdm_compatibility,
)


TDM_RATIO_PLAN_SCHEMA = "emuflow.tdm-ratio-plan/v1"
TDM_RATIO_PROVIDER = "lagrangian-kkt-timing-aware-v1"
TDM_TIMING_DAG_RATIO_PROVIDER = "aspdac26-timing-dag-lagrangian-v1"
DEFAULT_EXACT_DOMAIN_LIMIT = 2048
HopKey = Tuple[str, str, str, str]


def _hop_key(
    demand: str, link: str, source: str, sink: str
) -> HopKey:
    return (demand, link, source, sink)


def _link_delay_model(
    platform: Platform,
    constraints: Mapping[str, Any],
) -> Dict[Tuple[str, str, str], Tuple[float, float]]:
    result = {}
    for link in platform.links:
        slot_ns = 1000.0 / link.fabric_clock_mhz
        left, right = link.endpoints
        directions = [(left, right)]
        if link.direction in {"full_duplex", "half_duplex"}:
            directions.append((right, left))
        for source, sink in directions:
            result[(link.id, source, sink)] = (
                route_link_delay_ns(
                    platform, link.id, source, sink, constraints
                ),
                slot_ns,
            )
    return result


def _route_edges_in_tree_order(
    route: Mapping[str, Any],
) -> List[Tuple[int, Mapping[str, Any]]]:
    adjacency: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for edge in route["tree_edges"]:
        adjacency[edge["from"]].append(edge)
    for source in adjacency:
        adjacency[source].sort(
            key=lambda edge: (edge["to"], edge["link"])
        )
    result = []
    seen = {route["source"]}
    queue = deque([(route["source"], 0)])
    while queue:
        source, depth = queue.popleft()
        for edge in adjacency.get(source, []):
            if edge["to"] in seen:
                raise ValidationError(
                    f"TDM ratio input route {route['id']!r} is not a tree"
                )
            seen.add(edge["to"])
            result.append((depth, edge))
            queue.append((edge["to"], depth + 1))
    edge_nodes = {
        node
        for edge in route["tree_edges"]
        for node in (edge["from"], edge["to"])
    }
    if not edge_nodes <= seen:
        raise ValidationError(
            f"TDM ratio input route {route['id']!r} is disconnected"
        )
    return result


def _prepare_model(
    routes: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(
            f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}"
        )
    timing = routes.get("timing")
    if not isinstance(timing, dict) or not isinstance(
        timing.get("paths"), list
    ):
        raise ValidationError(
            "timing-aware TDM assignment requires routes.timing.paths"
        )
    normalization = timing.get("normalization")
    if not isinstance(normalization, dict):
        raise ValidationError(
            "timing-aware TDM assignment requires timing normalization"
        )
    for key in (
        "positive_slack_scale_ns",
        "negative_slack_scale_ns",
        "max_clock_period_ns",
    ):
        value = normalization.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) <= 0.0
        ):
            raise ValidationError(
                f"routes.timing.normalization.{key}: "
                "expected a positive number"
            )

    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    _, arcs, capacities = build_directed_graph(platform, constraints)
    link_delays = _link_delay_model(platform, constraints)
    links = {link.id: link for link in platform.links}
    raw_hops = []
    route_by_net = {}
    route_hop_keys: Dict[str, List[HopKey]] = {}
    direction_pairs_by_capacity = {}
    for route in sorted(routes["routes"], key=lambda item: item["id"]):
        if route["net"] in route_by_net:
            raise ValidationError(
                f"duplicate route net {route['net']!r}"
            )
        route_by_net[route["net"]] = route
        compatibility_domain = route.get(
            "tdm_compatibility", "global-frame-cdc"
        )
        if (
            not isinstance(compatibility_domain, str)
            or not compatibility_domain
        ):
            raise ValidationError(
                f"route {route['id']!r}.tdm_compatibility: "
                "expected a non-empty string"
            )
        route_hop_keys[route["id"]] = []
        for depth, edge in _route_edges_in_tree_order(route):
            key = _hop_key(
                route["id"], edge["link"], edge["from"], edge["to"]
            )
            arc_key = (edge["link"], edge["from"], edge["to"])
            if arc_key not in arcs:
                raise ValidationError(
                    f"route {route['id']!r}: unknown edge {arc_key}"
                )
            arc = arcs[arc_key]
            base_ns, beta_ns = link_delays[
                (edge["link"], edge["from"], edge["to"])
            ]
            raw_hops.append(
                {
                    "key": key,
                    "demand": route["id"],
                    "net": route["net"],
                    "transport_round": route.get("transport_round", 0),
                    "compatibility_domain": compatibility_domain,
                    "link": edge["link"],
                    "from": edge["from"],
                    "to": edge["to"],
                    "hop": depth,
                    "capacity_key": arc["capacity_key"],
                    "direction_pair": (edge["from"], edge["to"]),
                    "base_delay_ns": base_ns,
                    "beta_ns": beta_ns,
                }
            )
            direction_pairs_by_capacity.setdefault(
                arc["capacity_key"], set()
            ).add((edge["from"], edge["to"]))
            route_hop_keys[route["id"]].append(key)
    if not raw_hops:
        raise ValidationError("TDM ratio optimization has no routed hops")

    capacity_keys = sorted({hop["capacity_key"] for hop in raw_hops})
    domain_index = {
        capacity_key: index
        for index, capacity_key in enumerate(capacity_keys)
    }
    direction_index = {}
    for capacity_key in capacity_keys:
        pairs = sorted(direction_pairs_by_capacity[capacity_key])
        for index, pair in enumerate(pairs):
            direction_index[(capacity_key, pair)] = index

    raw_hops.sort(key=lambda hop: hop["key"])
    hop_index = {}
    hops = []
    for index, hop in enumerate(raw_hops):
        hop_index[hop["key"]] = index
        hops.append(
            {
                "index": index,
                **{
                    key: value
                    for key, value in hop.items()
                    if key not in {"key", "direction_pair"}
                },
                "domain": domain_index[hop["capacity_key"]],
                "direction": direction_index[
                    (hop["capacity_key"], hop["direction_pair"])
                ],
            }
        )

    provisional_model = {
        "hops": hops,
        "timing_paths": [],
    }

    domains = []
    for capacity_key in capacity_keys:
        capacity = capacities[capacity_key]
        domains.append(
            {
                "index": domain_index[capacity_key],
                "key": capacity_key,
                "link": capacity["link"],
                "direction": capacity["direction"],
                "lanes": links[
                    capacity["link"]
                ].transport_bits_per_cycle_per_direction,
            }
        )

    longest_hops_by_net = {}
    longest_sink_by_net = {}
    hops_by_net_sink = {}
    for net, route in route_by_net.items():
        paths_by_node = {route["source"]: (0.0, [])}
        for _depth, edge in _route_edges_in_tree_order(route):
            key = _hop_key(
                route["id"], edge["link"], edge["from"], edge["to"]
            )
            index = hop_index[key]
            parent_delay, parent_path = paths_by_node[edge["from"]]
            paths_by_node[edge["to"]] = (
                parent_delay + hops[index]["base_delay_ns"],
                [*parent_path, index],
            )
        candidates = [
            (paths_by_node[sink][0], paths_by_node[sink][1], sink)
            for sink in route["sinks"]
        ]
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        longest_hops_by_net[net] = candidates[0][1]
        longest_sink_by_net[net] = candidates[0][2]
        hops_by_net_sink[net] = {
            sink: list(paths_by_node[sink][1]) for sink in route["sinks"]
        }

    timing_paths = []
    route_net_names = set(route_by_net)
    for index, path in enumerate(timing["paths"]):
        raw_cut_nets = path.get("cut_nets")
        if (
            not isinstance(raw_cut_nets, list)
            or not raw_cut_nets
            or not all(isinstance(net, str) for net in raw_cut_nets)
        ):
            raise ValidationError(
                f"routes.timing.paths[{index}].cut_nets: invalid"
            )
        unknown = sorted(set(raw_cut_nets) - route_net_names)
        if unknown:
            raise ValidationError(
                f"routes.timing.paths[{index}]: unknown cut nets {unknown}"
            )
        raw_transitions = path.get("cut_transitions")
        if raw_transitions is None:
            transitions = [
                {
                    "net": net,
                    "from": route_by_net[net]["source"],
                    "to": longest_sink_by_net[net],
                }
                for net in raw_cut_nets
            ]
        else:
            if (
                not isinstance(raw_transitions, list)
                or len(raw_transitions) != len(raw_cut_nets)
            ):
                raise ValidationError(
                    f"routes.timing.paths[{index}].cut_transitions: invalid"
                )
            transitions = []
            for net, transition in zip(raw_cut_nets, raw_transitions):
                route = route_by_net[net]
                if (
                    not isinstance(transition, dict)
                    or transition.get("net") != net
                    or transition.get("from") != route["source"]
                    or transition.get("to") not in route["sinks"]
                ):
                    raise ValidationError(
                        f"routes.timing.paths[{index}].cut_transitions: "
                        "invalid member-specific sink"
                    )
                transitions.append(dict(transition))
            if any(
                transitions[position - 1]["to"]
                != transitions[position]["from"]
                for position in range(1, len(transitions))
            ):
                raise ValidationError(
                    f"routes.timing.paths[{index}].cut_transitions: "
                    "discontinuous member partition chain"
                )
        path_hops = [
            hop
            for transition in transitions
            for hop in hops_by_net_sink[transition["net"]][transition["to"]]
        ]
        if len(path_hops) != len(set(path_hops)):
            raise ValidationError(
                f"routes.timing.paths[{index}]: duplicate routed hop"
            )
        period = path.get("clock_period_ns")
        fixed = path.get("fixed_delay_ns")
        required = path.get("required_time_ns", period)
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or float(period) <= 0.0
            or isinstance(fixed, bool)
            or not isinstance(fixed, (int, float))
            or float(fixed) < 0.0
            or isinstance(required, bool)
            or not isinstance(required, (int, float))
            or not math.isfinite(float(required))
            or float(required) <= 0.0
        ):
            raise ValidationError(
                f"routes.timing.paths[{index}]: invalid timing values"
            )
        timing_paths.append(
            {
                "index": index,
                "id": path["path"],
                "clock_domain": path["clock_domain"],
                "clock_period_ns": float(period),
                "required_time_ns": float(required),
                "fixed_delay_ns": float(fixed),
                "cut_nets": list(raw_cut_nets),
                "compressed_path_ids": list(
                    path.get("compressed_path_ids", [path["path"]])
                ),
                "cut_transitions": transitions,
                "hops": path_hops,
            }
        )

    provisional_model["timing_paths"] = timing_paths
    compatibility = derive_tdm_compatibility(provisional_model)
    compatibility_by_hop = {
        record["hop"]: record["compatibility"]
        for record in compatibility["hops"]
    }
    for hop in hops:
        hop["compatibility"] = compatibility_by_hop[hop["index"]]

    return {
        "constraints": constraints,
        "normalization": {
            key: float(normalization[key])
            for key in (
                "positive_slack_scale_ns",
                "negative_slack_scale_ns",
                "max_clock_period_ns",
            )
        },
        "domains": domains,
        "hops": hops,
        "timing_paths": timing_paths,
        "compatibility": compatibility,
    }


def _write_native_input(
    path: Path,
    model: Mapping[str, Any],
    *,
    max_iterations: int,
    max_ratio: int,
    ratio_quantum: int,
    min_ratio: int,
    post_refinement_iterations: int,
    exact_domain_limit: int,
    convergence: float,
    continuous_seed: Optional[Sequence[float]] = None,
) -> None:
    normalization = model["normalization"]
    header = "EMUFLOW_TDM_RATIO_INPUT_V8"
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(header + "\n")
        stream.write(
            f"PARAM {max_iterations} {max_ratio} {ratio_quantum} "
            f"{min_ratio} "
            f"{post_refinement_iterations} {exact_domain_limit} "
            f"{convergence:.17g} "
            f"{normalization['positive_slack_scale_ns']:.17g} "
            f"{normalization['negative_slack_scale_ns']:.17g} "
            f"{normalization['max_clock_period_ns']:.17g}\n"
        )
        stream.writelines(
            f"DOMAIN {domain['index']} {domain['lanes']}\n"
            for domain in model["domains"]
        )
        stream.writelines(
            f"HOP {hop['index']} {hop['domain']} {hop['direction']} "
            f"{hop['compatibility']} "
            f"{hop['base_delay_ns']:.17g} {hop['beta_ns']:.17g}\n"
            for hop in model["hops"]
        )
        for timing_path in model["timing_paths"]:
            hops = ",".join(str(hop) for hop in timing_path["hops"])
            stream.write(
                f"PATH {timing_path['index']} "
                f"{timing_path['clock_period_ns']:.17g} "
                f"{timing_path['required_time_ns']:.17g} "
                f"{timing_path['fixed_delay_ns']:.17g} {hops}\n"
            )
        if continuous_seed is not None:
            stream.writelines(
                f"SEED {index} {ratio:.17g}\n"
                for index, ratio in enumerate(continuous_seed)
            )


def _parse_native_output(
    path: Path,
    model: Mapping[str, Any],
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_TDM_RATIO_OUTPUT_V1":
        raise EmuFlowError("TDM ratio optimizer returned an invalid header")
    hops = {}
    timing_paths = {}
    metrics: Dict[str, Any] = {}
    integer_metrics = {
        "iterations",
        "max_discrete_ratio",
        "post_refinement_swaps",
        "dp_legalized_domains",
        "greedy_legalized_domains",
        "group_minimax_improvements",
        "global_minimax_improvements",
    }
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "HOP" and len(fields) == 5:
            try:
                index = int(fields[1])
                record = {
                    "continuous_ratio": float(fields[2]),
                    "discrete_ratio": int(fields[3]),
                    "lane": int(fields[4]),
                }
            except ValueError as error:
                raise EmuFlowError(
                    "TDM ratio optimizer returned an invalid HOP"
                ) from error
            if index in hops:
                raise EmuFlowError(
                    "TDM ratio optimizer returned a duplicate HOP"
                )
            hops[index] = record
        elif fields[0] == "PATH" and len(fields) == 5:
            try:
                index = int(fields[1])
                record = {
                    "delay_ns": float(fields[2]),
                    "slack_ns": float(fields[3]),
                    "normalized_slack": float(fields[4]),
                }
            except ValueError as error:
                raise EmuFlowError(
                    "TDM ratio optimizer returned an invalid PATH"
                ) from error
            if index in timing_paths:
                raise EmuFlowError(
                    "TDM ratio optimizer returned a duplicate PATH"
                )
            timing_paths[index] = record
        elif fields[0] == "METRIC" and len(fields) == 3:
            key = fields[1]
            if key in metrics:
                raise EmuFlowError(
                    f"TDM ratio optimizer returned duplicate metric {key!r}"
                )
            try:
                metrics[key] = (
                    int(fields[2])
                    if key in integer_metrics
                    else float(fields[2])
                )
            except ValueError as error:
                raise EmuFlowError(
                    f"TDM ratio optimizer returned invalid metric {key!r}"
                ) from error
        else:
            raise EmuFlowError(
                f"TDM ratio optimizer returned an invalid record: {line}"
            )
    if set(hops) != set(range(len(model["hops"]))):
        raise EmuFlowError(
            "TDM ratio optimizer HOP coverage is not exact"
        )
    if set(timing_paths) != set(range(len(model["timing_paths"]))):
        raise EmuFlowError(
            "TDM ratio optimizer PATH coverage is not exact"
        )
    expected_metrics = {
        "iterations",
        "continuous_worst_normalized_slack",
        "discrete_worst_normalized_slack",
        "max_discrete_ratio",
        "post_refinement_swaps",
        "dp_legalized_domains",
        "greedy_legalized_domains",
        "group_minimax_improvements",
        "global_minimax_improvements",
        "global_minimax_weight_exponent",
    }
    if set(metrics) != expected_metrics:
        raise EmuFlowError(
            "TDM ratio optimizer metric coverage is not exact"
        )
    return {
        "hops": [hops[index] for index in range(len(hops))],
        "timing_paths": [
            timing_paths[index] for index in range(len(timing_paths))
        ],
        "metrics": metrics,
    }


def _discrete_timing_records(
    model: Mapping[str, Any],
    hop_records: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], float]:
    records = []
    worst = float("inf")
    for timing_path in model["timing_paths"]:
        delay = timing_path["fixed_delay_ns"] + sum(
            hop_records[hop]["base_delay_ns"]
            + hop_records[hop]["beta_ns"]
            * (hop_records[hop]["discrete_ratio"] - 1)
            for hop in timing_path["hops"]
        )
        slack = timing_path.get(
            "required_time_ns", timing_path["clock_period_ns"]
        ) - delay
        normalized = _normalized_slack(
            timing_path["clock_period_ns"],
            slack,
            model["normalization"],
        )
        worst = min(worst, normalized)
        records.append(
            {
                **timing_path,
                "delay_ns": delay,
                "slack_ns": slack,
                "normalized_slack": normalized,
            }
        )
    return records, worst


def _round_barrier_legalize(
    hop_records: List[Dict[str, Any]],
    model: Mapping[str, Any],
    platform: Platform,
    max_ratio: int,
    ratio_quantum: int,
) -> Dict[str, Any]:
    rounds = sorted({hop["transport_round"] for hop in hop_records})
    if not rounds or any(round_id not in {0, 1} for round_id in rounds):
        raise ValidationError(
            "academic TDM assignment supports transport rounds 0 and 1"
        )
    if len(rounds) > 2:
        raise ValidationError(
            "academic TDM round-barrier legalization currently supports "
            "at most two transport rounds"
        )
    frame_slots = model["constraints"]["frame_slots"]
    link_by_id = {link.id: link for link in platform.links}
    domain_by_index = {
        domain["index"]: domain for domain in model["domains"]
    }
    latency_by_domain = {
        index: link_by_id[domain["link"]].latency_cycles
        for index, domain in domain_by_index.items()
    }

    def buckets() -> Dict[
        Tuple[int, int, int], List[Dict[str, Any]]
    ]:
        result: Dict[
            Tuple[int, int, int], List[Dict[str, Any]]
        ] = defaultdict(list)
        for hop in hop_records:
            result[
                (
                    hop["domain"],
                    hop["direction"],
                    hop["discrete_ratio"],
                )
            ].append(hop)
        return result

    def counts_by_bucket() -> Dict[
        Tuple[int, int, int], Counter
    ]:
        result: Dict[Tuple[int, int, int], Counter] = defaultdict(
            Counter
        )
        for hop in hop_records:
            result[
                (
                    hop["domain"],
                    hop["direction"],
                    hop["discrete_ratio"],
                )
            ][hop["transport_round"]] += 1
        return result

    def lane_need(
        key: Tuple[int, int, int],
        counts: Mapping[int, int],
        source_ready_slot: int,
    ) -> int:
        domain, _direction, ratio = key
        latency = latency_by_domain[domain]
        if len(rounds) == 1:
            available = frame_slots - RUNTIME_BARRIER_SLOTS - latency
            if available <= 0:
                return frame_slots + 1
            return math.ceil(
                counts.get(rounds[0], 0) / min(ratio, available)
            )
        round_zero_slots = source_ready_slot - latency
        round_one_slots = (
            frame_slots
            - RUNTIME_BARRIER_SLOTS
            - latency
            - source_ready_slot
        )
        if round_zero_slots <= 0 or round_one_slots <= 0:
            return frame_slots + 1
        total_slots = round_zero_slots + round_one_slots
        return max(
            math.ceil(
                sum(counts.values()) / min(ratio, total_slots)
            ),
            math.ceil(counts.get(0, 0) / round_zero_slots),
            math.ceil(counts.get(1, 0) / round_one_slots),
        )

    def boundary_score(
        counts: Mapping[Tuple[int, int, int], Mapping[int, int]],
    ) -> Tuple[Tuple[int, int, int, int, int], int, Dict[int, int]]:
        if len(rounds) == 1:
            required = {
                domain: sum(
                    lane_need(key, count, 0)
                    for key, count in counts.items()
                    if key[0] == domain
                )
                for domain in domain_by_index
            }
            excesses = [
                max(0, required[domain] - domain_by_index[domain]["lanes"])
                for domain in required
            ]
            return (
                (
                    max(excesses, default=0),
                    sum(excesses),
                    max(required.values(), default=0),
                    sum(required.values()),
                    abs(frame_slots // 2),
                ),
                0,
                required,
            )

        minimum_latency = max(latency_by_domain.values())
        first_source_ready = minimum_latency + 1
        last_source_ready = (
            frame_slots
            - RUNTIME_BARRIER_SLOTS
            - minimum_latency
            - 1
        )
        if first_source_ready > last_source_ready:
            raise ValidationError(
                "TDM round-barrier legalization has no boundary candidate"
            )

        # Each bucket contributes
        # max(base, ceil(round0 / left_slots), ceil(round1 / right_slots)).
        # The left term is monotone decreasing and the right term monotone
        # increasing.  Accumulate their quotient-constant intervals into
        # per-domain difference arrays instead of evaluating every
        # slot/domain/bucket combination.
        required_differences = {
            domain: [0] * (frame_slots + 1)
            for domain in domain_by_index
        }

        def add_interval(
            differences: List[int], start: int, stop: int, value: int
        ) -> None:
            if start > stop:
                return
            differences[start] += value
            differences[stop + 1] -= value

        def add_decreasing_ceil(
            differences: List[int],
            start: int,
            stop: int,
            *,
            numerator: int,
            denominator_offset: int,
            floor: int,
        ) -> None:
            slot = start
            while slot <= stop:
                denominator = slot - denominator_offset
                quotient = math.ceil(numerator / denominator)
                value = max(floor, quotient)
                if quotient <= floor or quotient <= 1:
                    add_interval(differences, slot, stop, value)
                    break
                last_denominator = (numerator - 1) // (quotient - 1)
                last_slot = min(stop, denominator_offset + last_denominator)
                add_interval(differences, slot, last_slot, value)
                slot = last_slot + 1

        def add_increasing_ceil(
            differences: List[int],
            start: int,
            stop: int,
            *,
            numerator: int,
            denominator_sum: int,
            floor: int,
        ) -> None:
            # Traverse increasing denominators in reverse slot order, then
            # map each quotient-constant denominator interval back to slots.
            denominator = denominator_sum - stop
            maximum_denominator = denominator_sum - start
            while denominator <= maximum_denominator:
                quotient = math.ceil(numerator / denominator)
                value = max(floor, quotient)
                if quotient <= floor or quotient <= 1:
                    add_interval(differences, start, denominator_sum - denominator, value)
                    break
                last_denominator = min(
                    maximum_denominator,
                    (numerator - 1) // (quotient - 1),
                )
                add_interval(
                    differences,
                    denominator_sum - last_denominator,
                    denominator_sum - denominator,
                    value,
                )
                denominator = last_denominator + 1

        for key, count in counts.items():
            domain, _direction, ratio = key
            latency = latency_by_domain[domain]
            denominator_sum = (
                frame_slots - RUNTIME_BARRIER_SLOTS - latency
            )
            total_slots = frame_slots - RUNTIME_BARRIER_SLOTS - 2 * latency
            base = math.ceil(sum(count.values()) / min(ratio, total_slots))
            round_zero = count.get(0, 0)
            round_one = count.get(1, 0)

            def left_dominates(source_ready: int) -> bool:
                left = max(
                    base,
                    math.ceil(round_zero / (source_ready - latency)),
                )
                right = math.ceil(
                    round_one / (denominator_sum - source_ready)
                )
                return left >= right

            low = first_source_ready
            high = last_source_ready
            crossover = first_source_ready - 1
            while low <= high:
                middle = (low + high) // 2
                if left_dominates(middle):
                    crossover = middle
                    low = middle + 1
                else:
                    high = middle - 1

            differences = required_differences[domain]
            add_decreasing_ceil(
                differences,
                first_source_ready,
                crossover,
                numerator=round_zero,
                denominator_offset=latency,
                floor=base,
            )
            add_increasing_ceil(
                differences,
                crossover + 1,
                last_source_ready,
                numerator=round_one,
                denominator_sum=denominator_sum,
                floor=base,
            )

        required_by_slot = {}
        for domain, differences in required_differences.items():
            running = 0
            values = [0] * frame_slots
            for slot in range(first_source_ready, last_source_ready + 1):
                running += differences[slot]
                values[slot] = running
            required_by_slot[domain] = values

        best = None
        midpoint = frame_slots // 2
        for source_ready in range(
            first_source_ready, last_source_ready + 1
        ):
            required = {
                domain: required_by_slot[domain][source_ready]
                for domain in domain_by_index
            }
            excesses = [
                max(
                    0,
                    required[domain]
                    - domain_by_index[domain]["lanes"],
                )
                for domain in required
            ]
            score = (
                max(excesses, default=0),
                sum(excesses),
                max(required.values(), default=0),
                sum(required.values()),
                abs(source_ready - midpoint),
            )
            candidate = (score, source_ready, required)
            if best is None or candidate[0] < best[0]:
                best = candidate
        if best is None:
            raise ValidationError(
                "TDM round-barrier legalization has no boundary candidate"
            )
        return best

    initial_timing, initial_worst = _discrete_timing_records(
        model, hop_records
    )
    del initial_timing
    promoted_indices = set()
    promotion_steps = 0
    current_counts = counts_by_bucket()
    score, source_ready_slot, required_by_domain = boundary_score(
        current_counts
    )
    while score[0] > 0:
        failing_domains = {
            domain
            for domain, required in required_by_domain.items()
            if required > domain_by_index[domain]["lanes"]
        }
        current_buckets = buckets()
        current_timing, current_worst = _discrete_timing_records(
            model, hop_records
        )
        hop_by_index = {hop["index"]: hop for hop in hop_records}
        path_impacts_by_bucket: Dict[
            Tuple[int, int, int], List[Tuple[int, float]]
        ] = defaultdict(list)
        for path_position, timing_path in enumerate(model["timing_paths"]):
            beta_by_bucket: Dict[Tuple[int, int, int], float] = (
                defaultdict(float)
            )
            for hop_index in timing_path["hops"]:
                hop = hop_by_index[hop_index]
                beta_by_bucket[
                    (
                        hop["domain"],
                        hop["direction"],
                        hop["discrete_ratio"],
                    )
                ] += hop["beta_ns"]
            for bucket_key, beta in beta_by_bucket.items():
                path_impacts_by_bucket[bucket_key].append(
                    (path_position, beta)
                )
        timing_order = sorted(
            range(len(current_timing)),
            key=lambda index: current_timing[index]["normalized_slack"],
        )
        candidates = []
        boundary_migration_candidates = []
        for key in sorted(current_buckets):
            domain, direction, ratio = key
            if domain not in failing_domains or ratio >= max_ratio:
                continue
            existing_higher = {
                    candidate_key[2]
                    for candidate_key in current_buckets
                    if candidate_key[0] == domain
                    and candidate_key[1] == direction
                    and candidate_key[2] > ratio
                }
            before = sum(
                lane_need(bucket_key, count, source_ready_slot)
                for bucket_key, count in current_counts.items()
                if bucket_key[0] == domain
            )
            count = current_counts[key]
            total = sum(count.values())
            ratio_thresholds = set()
            for desired_lanes in range(max(1, before - 1), 0, -1):
                required_ratio = math.ceil(total / desired_lanes)
                target = (
                    1
                    if required_ratio <= 1
                    else math.ceil(
                        required_ratio / ratio_quantum
                    )
                    * ratio_quantum
                )
                if ratio < target <= max_ratio:
                    ratio_thresholds.add(target)
            higher = sorted(
                existing_higher | ratio_thresholds | {max_ratio}
            )
            path_impacts = path_impacts_by_bucket.get(key, ())
            affected_paths = {
                path_position for path_position, _beta in path_impacts
            }
            unaffected_worst = next(
                (
                    current_timing[path_position]["normalized_slack"]
                    for path_position in timing_order
                    if path_position not in affected_paths
                ),
                float("inf"),
            )
            for target in higher:
                target_key = (domain, direction, target)
                target_count = current_counts.get(target_key)
                merged_count = Counter(count)
                if target_count is not None:
                    merged_count.update(target_count)
                after = (
                    before
                    - lane_need(key, count, source_ready_slot)
                    - (
                        lane_need(
                            target_key,
                            target_count,
                            source_ready_slot,
                        )
                        if target_count is not None
                        else 0
                    )
                    + lane_need(
                        target_key,
                        merged_count,
                        source_ready_slot,
                    )
                )
                saving = before - after
                candidate_worst = unaffected_worst
                ratio_delta = target - ratio
                for path_position, beta in path_impacts:
                    timing_path = current_timing[path_position]
                    delay = timing_path["delay_ns"] + beta * ratio_delta
                    slack = timing_path.get(
                        "required_time_ns", timing_path["clock_period_ns"]
                    ) - delay
                    candidate_worst = min(
                        candidate_worst,
                        _normalized_slack(
                            timing_path["clock_period_ns"],
                            slack,
                            model["normalization"],
                        ),
                    )
                loss = max(0.0, current_worst - candidate_worst)
                if saving > 0:
                    candidates.append(
                        (
                            loss / saving,
                            loss,
                            -saving,
                            target - ratio,
                            len(current_buckets[key]),
                            key,
                            target,
                        )
                    )
                else:
                    candidate_counts = {
                        bucket_key: Counter(bucket_count)
                        for bucket_key, bucket_count in current_counts.items()
                        if bucket_key != key and bucket_key != target_key
                    }
                    candidate_counts[target_key] = merged_count
                    candidate_boundary = boundary_score(candidate_counts)[0]
                    boundary_migration_candidates.append(
                        (
                            candidate_boundary,
                            loss,
                            target - ratio,
                            len(current_buckets[key]),
                            key,
                            target,
                        )
                    )
        if candidates:
            *_, selected_key, selected_target = min(candidates)
        elif boundary_migration_candidates:
            # A fixed round boundary can hide a legal ratio promotion.  In
            # particular, a promotion may only become useful after the best
            # boundary moves away from the frame midpoint.  Re-evaluate the
            # complete boundary objective for every monotone merge and take
            # the least damaging monotone step.  Repeated merges are
            # complete: ratios only increase through a finite legal set, and
            # the all-max-ratio state is reached before infeasibility is
            # reported.
            *_, selected_key, selected_target = min(
                boundary_migration_candidates
            )
        else:
            raise ValidationError(
                "TDM round-barrier legalization cannot reduce lane "
                "fragmentation to a feasible solution"
            )
        for hop in current_buckets[selected_key]:
            hop["discrete_ratio"] = selected_target
            promoted_indices.add(hop["index"])
        promotion_steps += 1
        if promotion_steps > 1024:
            raise ValidationError(
                "TDM round-barrier legalization did not converge"
            )
        current_counts = counts_by_bucket()
        score, source_ready_slot, required_by_domain = boundary_score(
            current_counts
        )

    final_buckets = buckets()
    current_counts = counts_by_bucket()
    lane_counts = {
        key: lane_need(key, current_counts[key], source_ready_slot)
        for key in final_buckets
    }
    for domain, domain_record in domain_by_index.items():
        domain_keys = sorted(
            key for key in final_buckets if key[0] == domain
        )
        remaining = (
            domain_record["lanes"]
            - sum(lane_counts[key] for key in domain_keys)
        )
        while remaining > 0:
            splittable = [
                key
                for key in domain_keys
                if lane_counts[key] < len(final_buckets[key])
            ]
            if not splittable:
                break

            def split_score(
                key: Tuple[int, int, int],
            ) -> Tuple[int, int, Tuple[int, int, int]]:
                lanes = lane_counts[key]
                count = current_counts[key]
                return (
                    max(
                        math.ceil(value / lanes)
                        for value in count.values()
                    ),
                    math.ceil(sum(count.values()) / lanes),
                    tuple(-value for value in key),
                )

            selected = max(splittable, key=split_score)
            lane_counts[selected] += 1
            remaining -= 1

    rebalanced_hops = 0
    for domain in sorted(domain_by_index):
        next_lane = 0
        for key in sorted(
            key for key in final_buckets if key[0] == domain
        ):
            count = lane_counts[key]
            lanes = list(range(next_lane, next_lane + count))
            next_lane += count
            bucket = final_buckets[key]
            ratio = key[2]
            if len(bucket) > len(lanes) * ratio:
                raise ValidationError(
                    "TDM round-barrier lane group exceeds its ratio"
                )
            bucket.sort(
                key=lambda hop: (
                    hop["transport_round"],
                    hop["demand"],
                    hop["hop"],
                    hop["index"],
                )
            )
            for index, hop in enumerate(bucket):
                lane = lanes[index % len(lanes)]
                if hop["lane"] != lane:
                    hop["lane"] = lane
                    rebalanced_hops += 1
        if next_lane > domain_by_index[domain]["lanes"]:
            raise ValidationError(
                "TDM round-barrier lane assignment exceeds domain"
            )

    return {
        "active_rounds": rounds,
        "source_ready_semantics": "capacity_split_estimate",
        "source_ready_slot": (
            source_ready_slot if len(rounds) == 2 else None
        ),
        "promoted_hops": len(promoted_indices),
        "promotion_steps": promotion_steps,
        "pre_legalization_worst_normalized_slack": initial_worst,
        "round_aware_lane_rebalanced_hops": rebalanced_hops,
    }


def build_tdm_ratio_plan(
    routes: Mapping[str, Any],
    platform: Platform,
    *,
    executable: Optional[str] = None,
    max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: Optional[int] = None,
    post_refinement_iterations: int = 200,
    exact_domain_limit: int = DEFAULT_EXACT_DOMAIN_LIMIT,
    convergence: float = 1.0e-9,
    continuous_seed: Optional[Sequence[float]] = None,
    provider: str = TDM_RATIO_PROVIDER,
    provider_metadata: Optional[Mapping[str, Any]] = None,
    prepared_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    model = (
        prepared_model
        if prepared_model is not None
        else _prepare_model(routes, platform)
    )
    min_ratio = int(model["constraints"].get("tdm_min_ratio", 1))
    if ratio_quantum is None:
        ratio_quantum = int(
            model["constraints"].get("tdm_ratio_quantum", 8)
        )
    if max_ratio is None:
        link_by_id = {link.id: link for link in platform.links}
        usable_slots = min(
            model["constraints"]["frame_slots"]
            - link_by_id[hop["link"]].latency_cycles
            for hop in model["hops"]
        )
        if usable_slots <= 0:
            raise ValidationError(
                "TDM ratio schedule has no slot before link arrival deadline"
            )
        max_ratio = (
            (usable_slots // ratio_quantum) * ratio_quantum
            if usable_slots >= ratio_quantum
            else 1
        )
    for name, value, allow_zero in (
        ("max_iterations", max_iterations, False),
        ("max_ratio", max_ratio, False),
        ("ratio_quantum", ratio_quantum, False),
        (
            "post_refinement_iterations",
            post_refinement_iterations,
            True,
        ),
        ("exact_domain_limit", exact_domain_limit, True),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < (0 if allow_zero else 1)
        ):
            raise ValidationError(
                f"TDM ratio {name}: expected a "
                f"{'non-negative' if allow_zero else 'positive'} integer"
            )
    if max_ratio != 1 and max_ratio % ratio_quantum != 0:
        raise ValidationError(
            "TDM ratio max_ratio must be 1 or a multiple of ratio_quantum"
        )
    if min_ratio != 1 and min_ratio % ratio_quantum != 0:
        raise ValidationError(
            "TDM ratio min_ratio must be 1 or a multiple of ratio_quantum"
        )
    if min_ratio > max_ratio:
        raise ValidationError("TDM ratio min_ratio cannot exceed max_ratio")
    if (
        isinstance(convergence, bool)
        or not isinstance(convergence, (int, float))
        or float(convergence) <= 0.0
    ):
        raise ValidationError(
            "TDM ratio convergence: expected a positive number"
        )
    if provider not in {TDM_RATIO_PROVIDER, TDM_TIMING_DAG_RATIO_PROVIDER}:
        raise ValidationError(f"unsupported TDM ratio provider {provider!r}")
    if (continuous_seed is None) != (provider == TDM_RATIO_PROVIDER):
        raise ValidationError(
            "timing-DAG provider requires a continuous seed and the legacy "
            "provider must solve its own continuous relaxation"
        )
    if continuous_seed is not None:
        if len(continuous_seed) != len(model["hops"]):
            raise ValidationError(
                "TDM ratio continuous seed coverage is not exact"
            )
        for ratio in continuous_seed:
            if (
                isinstance(ratio, bool)
                or not isinstance(ratio, (int, float))
                or not math.isfinite(float(ratio))
                or not float(min_ratio) <= float(ratio) <= max_ratio
            ):
                raise ValidationError(
                    "TDM ratio continuous seed contains an invalid ratio"
                )

    resolved = resolve_native_executable(
        "emuflow_tdm_ratio_optimizer", executable
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-tdm-ratio-") as temporary:
        root = Path(temporary)
        native_input = root / "tdm-ratio.in"
        native_output = root / "tdm-ratio.out"
        _write_native_input(
            native_input,
            model,
            max_iterations=max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            min_ratio=min_ratio,
            post_refinement_iterations=post_refinement_iterations,
            exact_domain_limit=exact_domain_limit,
            convergence=float(convergence),
            continuous_seed=continuous_seed,
        )
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(
                "in-tree TDM ratio optimizer failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        native = _parse_native_output(native_output, model)

    hop_records = []
    for expected, optimized in zip(model["hops"], native["hops"]):
        hop_records.append({**expected, **optimized})
    barrier_legalization = _round_barrier_legalize(
        hop_records, model, platform, max_ratio, ratio_quantum
    )
    hop_records.sort(key=lambda hop: hop["index"])
    timing_records, discrete_worst = _discrete_timing_records(
        model, hop_records
    )
    groups = {
        (
            hop["capacity_key"],
            hop["lane"],
            hop["direction"],
            hop["discrete_ratio"],
        )
        for hop in hop_records
    }
    plan = {
        "schema": TDM_RATIO_PLAN_SCHEMA,
        "design": routes.get("design"),
        "platform": platform.name,
        "provider": provider,
        "configuration": {
            "max_iterations": max_iterations,
            "max_ratio": max_ratio,
            "ratio_quantum": ratio_quantum,
            "min_ratio": min_ratio,
            "post_refinement_iterations": post_refinement_iterations,
            "exact_domain_limit": exact_domain_limit,
            "convergence": float(convergence),
        },
        "normalization": model["normalization"],
        "round_barrier_legalization": barrier_legalization,
        "domains": model["domains"],
        "compatibility": model["compatibility"],
        "hops": hop_records,
        "timing_paths": timing_records,
        "metrics": {
            **native["metrics"],
            "discrete_worst_normalized_slack": discrete_worst,
            "max_discrete_ratio": max(
                hop["discrete_ratio"] for hop in hop_records
            ),
            "domains": len(model["domains"]),
            "hops": len(hop_records),
            "timing_paths": len(timing_records),
            "lane_groups": len(groups),
            "round_aware_lane_rebalanced_hops": (
                barrier_legalization[
                    "round_aware_lane_rebalanced_hops"
                ]
            ),
            "round_barrier_promoted_hops": (
                barrier_legalization["promoted_hops"]
            ),
        },
    }
    if provider_metadata is not None:
        plan["algorithm"] = dict(provider_metadata)
    validate_tdm_ratio_plan(
        routes, platform, plan, prepared_model=model
    )
    return plan


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
    return (
        slack
        / (
            normalization["negative_slack_scale_ns"]
            * period
        )
    )


def validate_tdm_ratio_plan(
    routes: Mapping[str, Any],
    platform: Platform,
    plan: Mapping[str, Any],
    *,
    prepared_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if plan.get("schema") != TDM_RATIO_PLAN_SCHEMA:
        raise ValidationError(
            f"ratio plan.schema: expected {TDM_RATIO_PLAN_SCHEMA!r}"
        )
    if plan.get("provider") not in {
        TDM_RATIO_PROVIDER,
        TDM_TIMING_DAG_RATIO_PROVIDER,
    }:
        raise ValidationError(
            "ratio plan.provider is unsupported"
        )
    if plan.get("provider") == TDM_TIMING_DAG_RATIO_PROVIDER:
        algorithm = plan.get("algorithm")
        if (
            not isinstance(algorithm, dict)
            or algorithm.get("provider")
            != "aspdac26-timing-dag-equations-v1"
            or algorithm.get("validation", {}).get("status") != "pass"
        ):
            raise ValidationError(
                "timing-DAG ratio plan algorithm evidence is invalid"
            )
    if plan.get("design") != routes.get("design"):
        raise ValidationError("ratio plan.design does not match routes")
    if plan.get("platform") != platform.name:
        raise ValidationError("ratio plan.platform does not match BoardDB")
    model = (
        prepared_model
        if prepared_model is not None
        else _prepare_model(routes, platform)
    )
    if plan.get("normalization") != model["normalization"]:
        raise ValidationError(
            "ratio plan.normalization does not match routes"
        )
    if plan.get("domains") != model["domains"]:
        raise ValidationError(
            "ratio plan.domains do not match routed capacity domains"
        )
    if plan.get("compatibility") != model["compatibility"]:
        raise ValidationError(
            "ratio plan compatibility does not match routed timing domains"
        )
    validate_tdm_compatibility(model, plan["compatibility"])
    configuration = plan.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError("ratio plan.configuration: expected an object")
    max_ratio = configuration.get("max_ratio")
    ratio_quantum = configuration.get("ratio_quantum")
    min_ratio = configuration.get("min_ratio", 1)
    exact_domain_limit = configuration.get("exact_domain_limit")
    convergence = configuration.get("convergence")
    if (
        isinstance(max_ratio, bool)
        or not isinstance(max_ratio, int)
        or max_ratio <= 0
        or isinstance(ratio_quantum, bool)
        or not isinstance(ratio_quantum, int)
        or ratio_quantum <= 0
        or isinstance(min_ratio, bool)
        or not isinstance(min_ratio, int)
        or min_ratio <= 0
        or min_ratio > max_ratio
        or isinstance(exact_domain_limit, bool)
        or not isinstance(exact_domain_limit, int)
        or exact_domain_limit < 0
        or isinstance(convergence, bool)
        or not isinstance(convergence, (int, float))
        or float(convergence) <= 0.0
    ):
        raise ValidationError("ratio plan.configuration is invalid")
    if min_ratio != 1 and min_ratio % ratio_quantum != 0:
        raise ValidationError("ratio plan.configuration min_ratio is invalid")
    if min_ratio != int(model["constraints"].get("tdm_min_ratio", 1)):
        raise ValidationError(
            "ratio plan.configuration min_ratio differs from routes"
        )
    allowed_ratios = (
        ({1} if min_ratio == 1 else set())
        | set(
            range(
                ratio_quantum if min_ratio == 1 else min_ratio,
                max_ratio + 1,
                ratio_quantum,
            )
        )
    )

    raw_hops = plan.get("hops")
    if not isinstance(raw_hops, list) or len(raw_hops) != len(
        model["hops"]
    ):
        raise ValidationError("ratio plan.hops coverage is not exact")
    domain_by_index = {
        domain["index"]: domain for domain in model["domains"]
    }
    continuous_usage: Dict[int, float] = defaultdict(float)
    lane_groups: Dict[
        Tuple[int, int], Dict[str, Any]
    ] = {}
    ratios = {}
    continuous_ratios = {}
    for expected, actual in zip(model["hops"], raw_hops):
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ValidationError(
                    f"ratio plan hop {expected['index']}.{key}: "
                    "does not match routes"
                )
        continuous = actual.get("continuous_ratio")
        discrete = actual.get("discrete_ratio")
        lane = actual.get("lane")
        if (
            isinstance(continuous, bool)
            or not isinstance(continuous, (int, float))
            or not math.isfinite(float(continuous))
            or float(continuous) < float(min_ratio) - 1.0e-9
            or float(continuous) > max_ratio + 1.0e-9
        ):
            raise ValidationError(
                f"ratio plan hop {expected['index']}: "
                "continuous ratio is invalid"
            )
        if (
            isinstance(discrete, bool)
            or not isinstance(discrete, int)
            or discrete not in allowed_ratios
        ):
            raise ValidationError(
                f"ratio plan hop {expected['index']}: "
                "discrete ratio is illegal"
            )
        domain = domain_by_index[expected["domain"]]
        if (
            isinstance(lane, bool)
            or not isinstance(lane, int)
            or lane < 0
            or lane >= domain["lanes"]
        ):
            raise ValidationError(
                f"ratio plan hop {expected['index']}: lane is invalid"
            )
        continuous_usage[expected["domain"]] += 1.0 / float(continuous)
        group_key = (expected["domain"], lane)
        group = lane_groups.setdefault(
            group_key,
            {
                "direction": expected["direction"],
                "compatibility": expected["compatibility"],
                "ratio": discrete,
                "hops": 0,
            },
        )
        if (
            group["direction"] != expected["direction"]
            or group["compatibility"] != expected["compatibility"]
            or group["ratio"] != discrete
        ):
            raise ValidationError(
                f"ratio plan lane group {group_key}: "
                "direction or ratio is not homogeneous"
            )
        group["hops"] += 1
        ratios[expected["index"]] = discrete
        continuous_ratios[expected["index"]] = float(continuous)
    for domain, usage in continuous_usage.items():
        if usage > domain_by_index[domain]["lanes"] + 1.0e-8:
            raise ValidationError(
                f"ratio plan domain {domain}: "
                "continuous capacity is exceeded"
            )
    for key, group in lane_groups.items():
        if group["hops"] > group["ratio"]:
            raise ValidationError(
                f"ratio plan lane group {key}: "
                "signal count exceeds discrete ratio"
            )
    barrier = plan.get("round_barrier_legalization")
    active_rounds = sorted(
        {hop["transport_round"] for hop in model["hops"]}
    )
    if not isinstance(barrier, dict) or barrier.get(
        "active_rounds"
    ) != active_rounds:
        raise ValidationError(
            "ratio plan round-barrier active rounds are inconsistent"
        )
    if barrier.get("source_ready_semantics") != "capacity_split_estimate":
        raise ValidationError(
            "ratio plan round-barrier source-ready semantics are invalid"
        )
    promoted_hops = barrier.get("promoted_hops")
    rebalanced_hops = barrier.get(
        "round_aware_lane_rebalanced_hops"
    )
    for key, value in (
        ("promoted_hops", promoted_hops),
        ("round_aware_lane_rebalanced_hops", rebalanced_hops),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > len(model["hops"])
        ):
            raise ValidationError(
                f"ratio plan round-barrier {key} is invalid"
            )
    promotion_steps = barrier.get("promotion_steps")
    if (
        isinstance(promotion_steps, bool)
        or not isinstance(promotion_steps, int)
        or promotion_steps < 0
        or promotion_steps > 1024
    ):
        raise ValidationError(
            "ratio plan round-barrier promotion_steps is invalid"
        )
    pre_worst = barrier.get(
        "pre_legalization_worst_normalized_slack"
    )
    if (
        isinstance(pre_worst, bool)
        or not isinstance(pre_worst, (int, float))
        or not math.isfinite(float(pre_worst))
    ):
        raise ValidationError(
            "ratio plan pre-legalization slack is invalid"
        )
    if len(active_rounds) == 2:
        source_ready = barrier.get("source_ready_slot")
        frame_slots = model["constraints"]["frame_slots"]
        if (
            isinstance(source_ready, bool)
            or not isinstance(source_ready, int)
            or source_ready <= 0
            or source_ready >= frame_slots
        ):
            raise ValidationError(
                "ratio plan round-barrier source-ready slot is invalid"
            )
        link_by_id = {link.id: link for link in platform.links}
        round_counts: Dict[
            Tuple[int, int], Counter
        ] = defaultdict(Counter)
        for hop in raw_hops:
            round_counts[(hop["domain"], hop["lane"])][
                hop["transport_round"]
            ] += 1
        for (domain, lane), counts in round_counts.items():
            domain_record = domain_by_index[domain]
            latency = link_by_id[
                domain_record["link"]
            ].latency_cycles
            if counts.get(0, 0) > source_ready - latency:
                raise ValidationError(
                    f"ratio plan domain {domain} lane {lane}: "
                    "round 0 exceeds barrier capacity"
                )
            if counts.get(1, 0) > (
                frame_slots
                - RUNTIME_BARRIER_SLOTS
                - latency
                - source_ready
            ):
                raise ValidationError(
                    f"ratio plan domain {domain} lane {lane}: "
                    "round 1 exceeds barrier capacity"
                )
    elif barrier.get("source_ready_slot") is not None:
        raise ValidationError(
            "single-round ratio plan must not set a barrier slot"
        )

    raw_paths = plan.get("timing_paths")
    if not isinstance(raw_paths, list) or len(raw_paths) != len(
        model["timing_paths"]
    ):
        raise ValidationError(
            "ratio plan.timing_paths coverage is not exact"
        )
    normalized_values = []
    continuous_normalized_values = []
    for expected, actual in zip(model["timing_paths"], raw_paths):
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ValidationError(
                    f"ratio plan timing path {expected['index']}.{key}: "
                    "does not match routes"
                )
        delay = expected["fixed_delay_ns"] + sum(
            model["hops"][hop]["base_delay_ns"]
            + model["hops"][hop]["beta_ns"] * (ratios[hop] - 1)
            for hop in expected["hops"]
        )
        slack = expected["required_time_ns"] - delay
        normalized = _normalized_slack(
            expected["clock_period_ns"],
            slack,
            model["normalization"],
        )
        for key, value in (
            ("delay_ns", delay),
            ("slack_ns", slack),
            ("normalized_slack", normalized),
        ):
            actual_value = actual.get(key)
            if (
                isinstance(actual_value, bool)
                or not isinstance(actual_value, (int, float))
                or not math.isfinite(float(actual_value))
                or abs(float(actual_value) - value) > 1.0e-8
            ):
                raise ValidationError(
                    f"ratio plan timing path {expected['index']}.{key}: "
                    "does not match independent recomputation"
                )
        normalized_values.append(normalized)
        continuous_delay = expected["fixed_delay_ns"] + sum(
            model["hops"][hop]["base_delay_ns"]
            + model["hops"][hop]["beta_ns"]
            * (continuous_ratios[hop] - 1.0)
            for hop in expected["hops"]
        )
        continuous_slack = expected["required_time_ns"] - continuous_delay
        continuous_normalized_values.append(
            _normalized_slack(
                expected["clock_period_ns"],
                continuous_slack,
                model["normalization"],
            )
        )

    metrics = plan.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("ratio plan.metrics: expected an object")
    expected_discrete_worst = min(normalized_values)
    expected_continuous_worst = min(continuous_normalized_values)
    if (
        abs(
            float(
                metrics.get(
                    "continuous_worst_normalized_slack",
                    float("nan"),
                )
            )
            - expected_continuous_worst
        )
        > 1.0e-8
    ):
        raise ValidationError(
            "ratio plan continuous worst slack metric is inconsistent"
        )
    if (
        abs(
            float(
                metrics.get(
                    "discrete_worst_normalized_slack",
                    float("nan"),
                )
            )
            - expected_discrete_worst
        )
        > 1.0e-8
    ):
        raise ValidationError(
            "ratio plan discrete worst slack metric is inconsistent"
        )
    expected_max_ratio = max(ratios.values())
    if metrics.get("max_discrete_ratio") != expected_max_ratio:
        raise ValidationError(
            "ratio plan maximum discrete ratio metric is inconsistent"
        )
    for key, limit in (
        ("iterations", configuration.get("max_iterations")),
        (
            "post_refinement_swaps",
            configuration.get("post_refinement_iterations"),
        ),
    ):
        value = metrics.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or value > limit
        ):
            raise ValidationError(
                f"ratio plan metric {key!r} is outside configuration"
            )
    expected_static_metrics = {
        "domains": len(model["domains"]),
        "hops": len(model["hops"]),
        "timing_paths": len(model["timing_paths"]),
        "lane_groups": len(lane_groups),
    }
    for key, value in expected_static_metrics.items():
        if metrics.get(key) != value:
            raise ValidationError(
                f"ratio plan metric {key!r} is inconsistent"
            )
    legalization_counts = []
    for key in ("dp_legalized_domains", "greedy_legalized_domains"):
        value = metrics.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValidationError(
                f"ratio plan metric {key!r} is invalid"
            )
        legalization_counts.append(value)
    if sum(legalization_counts) != len(model["domains"]):
        raise ValidationError(
            "ratio plan legalization-domain metrics are inconsistent"
        )
    rebalanced = metrics.get("round_aware_lane_rebalanced_hops")
    if (
        isinstance(rebalanced, bool)
        or not isinstance(rebalanced, int)
        or rebalanced < 0
        or rebalanced > len(model["hops"])
    ):
        raise ValidationError(
            "ratio plan round-aware lane rebalance metric is invalid"
        )
    if rebalanced != rebalanced_hops:
        raise ValidationError(
            "ratio plan lane rebalance metric does not match barrier record"
        )
    if metrics.get("round_barrier_promoted_hops") != promoted_hops:
        raise ValidationError(
            "ratio plan promotion metric does not match barrier record"
        )
    return {
        "status": "pass",
        "provider": plan["provider"],
        **expected_static_metrics,
        "continuous_max_domain_usage": max(
            (
                continuous_usage[index]
                / domain_by_index[index]["lanes"]
                for index in continuous_usage
            ),
            default=0.0,
        ),
        "discrete_worst_normalized_slack": expected_discrete_worst,
        "max_discrete_ratio": expected_max_ratio,
        "post_refinement_swaps": metrics.get("post_refinement_swaps"),
    }


def ratio_plan_by_hop(
    plan: Mapping[str, Any],
) -> Dict[HopKey, Mapping[str, Any]]:
    return {
        _hop_key(
            hop["demand"], hop["link"], hop["from"], hop["to"]
        ): hop
        for hop in plan["hops"]
    }
