"""Independent route/timing checks and small timing-input normalization."""

from __future__ import annotations

from array import array
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.platform.platform import Platform
from emuflow.transport.bank_allocation import apply_bank_domain_ratios
from emuflow.routing.routing import assignment_has_static_exact_contract, ArcKey, _arc_key, build_directed_graph, estimate_tdm_ratio, normalize_route_constraints, route_link_delay_ns, route_link_delay_coefficients_ns, validate_system_routes
from emuflow.routing.routing_candidates import ROUTE_CANDIDATE_GENERATORS


STA_PATHS_SCHEMA = "emuflow.sta-paths/v1"
GLOBAL_CANDIDATE_PROVIDER = "timing-aware-global-candidate-v1"


def _number(value: Any, context: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    if positive and result <= 0.0:
        raise ValidationError(f"{context}: expected a positive number")
    return result


def _normalize_cut_transitions(
    value: Any,
    cut_nets: Sequence[str],
    demand_by_net: Mapping[str, Mapping[str, Any]],
    context: str,
) -> Optional[List[Dict[str, str]]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != len(cut_nets):
        raise ValidationError(f"{context}: expected one transition per cut net")
    result = []
    for index, (raw, net) in enumerate(zip(value, cut_nets)):
        item_context = f"{context}[{index}]"
        demand = demand_by_net[net]
        if (
            not isinstance(raw, dict)
            or set(raw) != {"net", "from", "to"}
            or raw.get("net") != net
            or raw.get("from") != demand["source"]
            or raw.get("to") not in demand["sinks"]
        ):
            raise ValidationError(f"{item_context}: invalid routed sink")
        result.append(dict(raw))
    if any(
        result[index - 1]["to"] != result[index]["from"]
        for index in range(1, len(result))
    ):
        raise ValidationError(f"{context}: partition chain is discontinuous")
    return result


def normalize_sta_paths(
    value: Mapping[str, Any],
    demands: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    if value.get("schema") != STA_PATHS_SCHEMA:
        raise ValidationError(
            f"timing paths.schema: expected {STA_PATHS_SCHEMA!r}, "
            f"got {value.get('schema')!r}"
        )
    demand_by_net = {demand["net"]: demand for demand in demands}
    raw_paths = value.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("timing paths.paths: expected a non-empty array")

    paths: List[Dict[str, Any]] = []
    path_ids = set()
    demand_nets = set(demand_by_net)
    for index, raw in enumerate(raw_paths):
        context = f"timing paths.paths[{index}]"
        if not isinstance(raw, dict):
            raise ValidationError(f"{context}: expected an object")
        path_id = raw.get("id")
        clock_domain = raw.get("clock_domain")
        if not isinstance(path_id, str) or not path_id:
            raise ValidationError(f"{context}.id: expected a non-empty string")
        if path_id in path_ids:
            raise ValidationError(f"{context}.id: duplicate {path_id!r}")
        path_ids.add(path_id)
        if not isinstance(clock_domain, str) or not clock_domain:
            raise ValidationError(
                f"{context}.clock_domain: expected a non-empty string"
            )
        clock_period = _number(
            raw.get("clock_period_ns"),
            f"{context}.clock_period_ns",
            positive=True,
        )
        slack = _number(raw.get("slack_ns"), f"{context}.slack_ns")
        fixed_delay = _number(
            raw.get("fixed_delay_ns"), f"{context}.fixed_delay_ns"
        )
        if fixed_delay < 0.0:
            raise ValidationError(
                f"{context}.fixed_delay_ns: expected a non-negative number"
            )
        raw_nets = raw.get("cut_nets")
        if (
            not isinstance(raw_nets, list)
            or not raw_nets
            or not all(isinstance(net, str) and net for net in raw_nets)
        ):
            raise ValidationError(
                f"{context}.cut_nets: expected a non-empty string array"
            )
        unknown = sorted(set(raw_nets) - demand_nets)
        if unknown:
            raise ValidationError(
                f"{context}.cut_nets: unknown partition cut nets {unknown}"
            )
        if len(set(raw_nets)) != len(raw_nets):
            raise ValidationError(
                f"{context}.cut_nets: duplicate cut nets are not supported"
            )
        signature = raw.get("cut_signature")
        if signature is None:
            signature = []
            for net in raw_nets:
                demand = demand_by_net[net]
                signature.append(
                    f"{demand['source']}->{','.join(demand['sinks'])}"
                )
        if (
            not isinstance(signature, list)
            or not signature
            or not all(isinstance(item, str) and item for item in signature)
        ):
            raise ValidationError(
                f"{context}.cut_signature: expected a non-empty string array"
            )
        path = {
                "id": path_id,
                "clock_domain": clock_domain,
                "clock_period_ns": clock_period,
                "slack_ns": slack,
                "fixed_delay_ns": fixed_delay,
                "cut_nets": list(raw_nets),
                "cut_signature": list(signature),
            }
        transitions = _normalize_cut_transitions(
            raw.get("cut_transitions"),
            raw_nets,
            demand_by_net,
            f"{context}.cut_transitions",
        )
        if transitions is not None:
            path["cut_transitions"] = transitions
        paths.append(path)

    positive_scale = max(
        (path["slack_ns"] for path in paths if path["slack_ns"] >= 0.0),
        default=1.0,
    )
    # A path exactly on its timing boundary is valid.  Keep its normalized
    # slack at zero without dividing by an all-zero positive scale.
    if positive_scale == 0.0:
        positive_scale = 1.0
    negative_scale = abs(
        min(
            (path["slack_ns"] for path in paths if path["slack_ns"] < 0.0),
            default=-1.0,
        )
    )
    max_period = max(path["clock_period_ns"] for path in paths)
    for path in paths:
        slack = path["slack_ns"]
        if slack >= 0.0:
            normalized = (
                slack
                * path["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            normalized = slack / (negative_scale * path["clock_period_ns"])
        path["normalized_slack"] = normalized

    return {
        "schema": STA_PATHS_SCHEMA,
        "design": value.get("design"),
        "normalization": {
            "positive_slack_scale_ns": positive_scale,
            "negative_slack_scale_ns": negative_scale,
            "max_clock_period_ns": max_period,
        },
        "paths": paths,
    }


def compress_sta_paths(paths_artifact: Mapping[str, Any]) -> Dict[str, Any]:
    """Losslessly compress paths with timing-equivalent ordered cuts.

    Equal cut signatures alone are insufficient across clock domains because
    normalized slack has a different scale.  Requiring the same domain,
    period, source-STA required time, and cut-net sequence makes the
    maximum-fixed-delay representative dominate the other members for every
    possible route delay.
    """

    representatives: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    members: Dict[Tuple[Any, ...], List[str]] = defaultdict(list)
    for path in paths_artifact["paths"]:
        required_time_ns = path["fixed_delay_ns"] + path["slack_ns"]
        signature = (
            path["clock_domain"],
            path["clock_period_ns"],
            required_time_ns,
            tuple(path["cut_signature"]),
            tuple(path["cut_nets"]),
            tuple(
                (item["net"], item["from"], item["to"])
                for item in path.get("cut_transitions", [])
            ),
        )
        members[signature].append(path["id"])
        current = representatives.get(signature)
        key = (
            path["fixed_delay_ns"],
            -path["normalized_slack"],
            path["id"],
        )
        if current is None:
            representatives[signature] = path
        else:
            current_key = (
                current["fixed_delay_ns"],
                -current["normalized_slack"],
                current["id"],
            )
            if key > current_key:
                representatives[signature] = path

    compressed = []
    for signature in sorted(representatives):
        representative = dict(representatives[signature])
        representative["compressed_path_ids"] = sorted(members[signature])
        compressed.append(representative)
    return {
        **dict(paths_artifact),
        "compression": {
            "original_paths": len(paths_artifact["paths"]),
            "compressed_paths": len(compressed),
            "lossless_by": (
                "clock-domain+period+required-time+ordered-cut-signature+"
                "cut-net-sequence+member-sink-transitions/max-fixed-delay"
            ),
        },
        "paths": compressed,
    }


def load_sta_paths(
    path: Path,
    demands: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("timing paths: expected an object")
    if "compression" in value or "normalization" in value:
        if value.get("schema") != STA_PATHS_SCHEMA:
            raise ValidationError(
                f"timing paths.schema: expected {STA_PATHS_SCHEMA!r}"
            )
        compression = value.get("compression")
        normalization = value.get("normalization")
        paths = value.get("paths")
        if (
            not isinstance(compression, dict)
            or not isinstance(normalization, dict)
            or not isinstance(paths, list)
            or not paths
        ):
            raise ValidationError(
                "normalized timing paths: invalid normalization/compression"
            )
        if compression.get("compressed_paths") != len(paths):
            raise ValidationError(
                "normalized timing paths: compressed path count mismatch"
            )
        original_count = compression.get("original_paths")
        if (
            isinstance(original_count, bool)
            or not isinstance(original_count, int)
            or original_count < len(paths)
        ):
            raise ValidationError(
                "normalized timing paths: invalid original path count"
            )
        demand_by_net = {demand["net"]: demand for demand in demands}
        demand_nets = {demand["net"] for demand in demands}
        positive_scale = _number(
            normalization.get("positive_slack_scale_ns"),
            "timing paths.normalization.positive_slack_scale_ns",
            positive=True,
        )
        negative_scale = _number(
            normalization.get("negative_slack_scale_ns"),
            "timing paths.normalization.negative_slack_scale_ns",
            positive=True,
        )
        max_period = _number(
            normalization.get("max_clock_period_ns"),
            "timing paths.normalization.max_clock_period_ns",
            positive=True,
        )
        ids = set()
        for index, item in enumerate(paths):
            context = f"normalized timing paths.paths[{index}]"
            if not isinstance(item, dict):
                raise ValidationError(f"{context}: expected an object")
            path_id = item.get("id")
            if not isinstance(path_id, str) or not path_id or path_id in ids:
                raise ValidationError(f"{context}.id: invalid or duplicate")
            ids.add(path_id)
            period = _number(
                item.get("clock_period_ns"),
                f"{context}.clock_period_ns",
                positive=True,
            )
            slack = _number(item.get("slack_ns"), f"{context}.slack_ns")
            fixed_delay = _number(
                item.get("fixed_delay_ns"), f"{context}.fixed_delay_ns"
            )
            if fixed_delay < 0.0:
                raise ValidationError(
                    f"{context}.fixed_delay_ns: expected non-negative"
                )
            if (
                not isinstance(item.get("clock_domain"), str)
                or not item["clock_domain"]
            ):
                raise ValidationError(f"{context}.clock_domain: invalid")
            nets = item.get("cut_nets")
            if (
                not isinstance(nets, list)
                or not nets
                or not all(isinstance(net, str) and net for net in nets)
                or len(set(nets)) != len(nets)
                or not set(nets) <= demand_nets
            ):
                raise ValidationError(f"{context}.cut_nets: invalid")
            signature = item.get("cut_signature")
            if (
                not isinstance(signature, list)
                or not signature
                or not all(
                    isinstance(part, str) and part for part in signature
                )
            ):
                raise ValidationError(f"{context}.cut_signature: invalid")
            _normalize_cut_transitions(
                item.get("cut_transitions"),
                nets,
                demand_by_net,
                f"{context}.cut_transitions",
            )
            members = item.get("compressed_path_ids")
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(member, str) for member in members)
            ):
                raise ValidationError(
                    f"{context}.compressed_path_ids: invalid"
                )
            expected_normalized = (
                slack * period / (positive_scale * max_period)
                if slack >= 0.0
                else slack / (negative_scale * period)
            )
            if (
                abs(
                    _number(
                        item.get("normalized_slack"),
                        f"{context}.normalized_slack",
                    )
                    - expected_normalized
                )
                > 1.0e-12
            ):
                raise ValidationError(
                    f"{context}.normalized_slack: inconsistent"
                )
        return dict(value)
    return compress_sta_paths(normalize_sta_paths(value, demands))


def reconstruct_system_route_timing(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes: Mapping[str, Any],
    timing_paths: Mapping[str, Any],
    *,
    include_records: bool = False,
) -> Dict[str, Any]:
    """Evaluate any legal route provider against one normalized STA input."""
    validation = validate_system_routes(assignment, platform, routes)
    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    _, arcs, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=(
            assignment_has_static_exact_contract(assignment)
        ),
    )
    link_by_id = {link.id: link for link in platform.links}

    ordered_arc_keys = sorted(arcs)
    arc_index = {key: index for index, key in enumerate(ordered_arc_keys)}
    route_count = len(routes["routes"])
    route_rank_by_net: Dict[str, int] = {}
    route_delays = array("d", [0.0]) * route_count
    route_signature_offsets = array("Q", [0])
    route_signature_arcs = array("Q")
    round_aware = sorted(
        {int(route.get("transport_round", 0)) for route in routes["routes"]}
    ) == [0, 1]
    capacity_usage: Dict[Any, int] = defaultdict(int)
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        signature = []
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            graph[edge["from"]].append((edge["to"], edge["link"]))
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            capacity_usage[capacity_key] += int(route["width_bits"])
            signature.append(arc_index[key])
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id in graph[node]:
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                )
                queue.append(sink)
        net = route["net"]
        if net in route_rank_by_net:
            raise ValidationError(
                f"routes contain duplicate net identity {net!r}"
            )
        route_rank_by_net[net] = route_rank
        route_delays[route_rank] = max(
            delay_by_node[sink] for sink in route["sinks"]
        )
        route_signature_arcs.extend(sorted(signature))
        route_signature_offsets.append(len(route_signature_arcs))

    ratios: Dict[Any, int] = {}
    ratio_keys = (
        [
            (key, round_id)
            for key in capacities
            for round_id in (0, 1)
        ]
        if round_aware
        else list(capacities)
    )
    for ratio_key in ratio_keys:
        key = ratio_key[0] if round_aware else ratio_key
        capacity = capacities[key]
        lanes = link_by_id[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        signals = capacity_usage[ratio_key]
        ratios[ratio_key] = estimate_tdm_ratio(
            signals,
            lanes,
            constraints,
            is_sll=capacity["link"] in constraints["sll_links"],
        )

    ratios = apply_bank_domain_ratios(capacities, capacity_usage, ratios, constraints)
    route_tdm_delays = array("d", [0.0]) * route_count
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            graph[edge["from"]].append(
                (
                    edge["to"],
                    edge["link"],
                    capacity_key,
                )
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id, capacity_key in graph[node]:
                link = link_by_id[link_id]
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                    + (
                        0.0
                        if link_id in constraints["sll_links"]
                        else route_link_delay_coefficients_ns(
                            platform, link_id, node, sink, constraints
                        )[1] * (ratios[capacity_key] - 1)
                    )
                )
                queue.append(sink)
        if constraints.get("tree_edge_sum_tdm", False):
            route_tdm_delays[route_rank] = sum(
                route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                + (
                    0.0
                    if edge["link"] in constraints["sll_links"]
                    else (
                        route_link_delay_coefficients_ns(
                            platform, edge["link"], edge["from"], edge["to"], constraints
                        )[1]
                    )
                    * (
                        ratios[
                            (
                                arcs[
                                    _arc_key(
                                        edge["link"],
                                        edge["from"],
                                        edge["to"],
                                    )
                                ]["capacity_key"],
                                int(route.get("transport_round", 0)),
                            )
                            if round_aware
                            else arcs[
                                _arc_key(
                                    edge["link"],
                                    edge["from"],
                                    edge["to"],
                                )
                            ]["capacity_key"]
                        ]
                        - 1
                    )
                )
                for edge in route["tree_edges"]
            )
        else:
            route_tdm_delays[route_rank] = max(
                delay_by_node[sink] for sink in route["sinks"]
            )

    normalization = timing_paths["normalization"]
    path_records = [] if include_records else None
    path_count = 0
    worst_slack = (float("inf"), "")
    worst_normalized = (float("inf"), "")
    worst_tdm_slack = (float("inf"), "")
    worst_tdm_normalized = (float("inf"), "")
    for path in timing_paths["paths"]:
        path_count += 1
        try:
            path_route_ranks = [
                route_rank_by_net[net] for net in path["cut_nets"]
            ]
        except KeyError as error:
            raise ValidationError(
                f"route timing path {path['id']!r} references an unknown "
                "cut net"
            ) from error
        required_time = path["fixed_delay_ns"] + path["slack_ns"]
        delay = path["fixed_delay_ns"] + sum(
            route_delays[rank] for rank in path_route_ranks
        )
        slack = required_time - delay
        normalized = (
            slack
            * path["clock_period_ns"]
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
            if slack >= 0.0
            else slack
            / (
                normalization["negative_slack_scale_ns"]
                * path["clock_period_ns"]
            )
        )
        tdm_delay = path["fixed_delay_ns"] + sum(
            route_tdm_delays[rank] for rank in path_route_ranks
        )
        tdm_slack = required_time - tdm_delay
        tdm_normalized = (
            tdm_slack
            * path["clock_period_ns"]
            / (
                normalization["positive_slack_scale_ns"]
                * normalization["max_clock_period_ns"]
            )
            if tdm_slack >= 0.0
            else tdm_slack
            / (
                normalization["negative_slack_scale_ns"]
                * path["clock_period_ns"]
            )
        )
        route_signature = (
            ",".join(
                str(arc)
                for rank in path_route_ranks
                for arc in route_signature_arcs[
                    route_signature_offsets[rank]
                    : route_signature_offsets[rank + 1]
                ]
            )
            or "-"
        )
        path_id = path["id"]
        worst_slack = min(worst_slack, (slack, path_id))
        worst_normalized = min(
            worst_normalized, (normalized, path_id)
        )
        worst_tdm_slack = min(
            worst_tdm_slack, (tdm_slack, path_id)
        )
        worst_tdm_normalized = min(
            worst_tdm_normalized, (tdm_normalized, path_id)
        )
        if path_records is not None:
            path_records.append({
                "path": path["id"],
                "clock_domain": path["clock_domain"],
                "clock_period_ns": path["clock_period_ns"],
                "required_time_ns": required_time,
                "fixed_delay_ns": path["fixed_delay_ns"],
                "cut_nets": path["cut_nets"],
                "cut_signature": path["cut_signature"],
                "compressed_path_ids": path["compressed_path_ids"],
                **(
                    {"cut_transitions": path["cut_transitions"]}
                    if "cut_transitions" in path
                    else {}
                ),
                "delay_ns": delay,
                "slack_ns": slack,
                "normalized_slack": normalized,
                "estimated_tdm_slack_ns": tdm_slack,
                "estimated_tdm_normalized_slack": tdm_normalized,
                "route_signature": route_signature,
            })
    if path_count == 0:
        raise ValidationError(
            "route timing reconstruction has no normalized STA paths"
        )
    result = {
        **validation,
        "provider": routes["provider"],
        "timing_paths_original": timing_paths["compression"][
            "original_paths"
        ],
        "timing_paths_compressed": timing_paths["compression"][
            "compressed_paths"
        ],
        "worst_slack_path": worst_slack[1],
        "worst_slack_ns": worst_slack[0],
        "worst_normalized_path": worst_normalized[1],
        "worst_normalized_slack": worst_normalized[0],
        "estimated_worst_tdm_slack_path": worst_tdm_slack[1],
        "estimated_worst_tdm_slack_ns": worst_tdm_slack[0],
        "estimated_worst_tdm_normalized_path": worst_tdm_normalized[1],
        "estimated_worst_tdm_normalized_slack": worst_tdm_normalized[0],
        "estimated_max_tdm_ratio": max(ratios.values(), default=1),
    }
    if include_records:
        assert path_records is not None
        result["_timing_records"] = path_records
    return result


def validate_native_system_routes(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes: Mapping[str, Any],
    timing_paths: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    validation = validate_system_routes(assignment, platform, routes)
    provider = routes.get("provider")
    if provider != GLOBAL_CANDIDATE_PROVIDER:
        raise ValidationError("routes.provider: expected the global candidate provider")
    evaluation = routes.get("timing_evaluation")
    if evaluation is not None and evaluation != {
        "mode": "post-route-evaluation-only",
        "optimization_enabled": False,
    }:
        raise ValidationError("global candidate routes have invalid timing evaluation metadata")
    if routes["routes"]:
        joint = routes.get("joint_optimization")
        if not isinstance(joint, dict) or joint.get("method") != (
            "checked-candidate-pool+deterministic-batch-conflict-lns-v2"
        ):
            raise ValidationError("routes.joint_optimization: invalid global candidate metadata")
        selection = joint.get("candidate_generation", {}).get(
            "master_selection"
        )
        if (
            not isinstance(selection, list)
            or len(selection) != len(routes.get("routes", []))
            or any(
                not isinstance(record, dict)
                or record.get("demand") != route.get("id")
                for record, route in zip(selection, routes["routes"])
            )
            or any(
                not isinstance(record, dict)
                or record.get("generator") not in ROUTE_CANDIDATE_GENERATORS
                or record.get("generator") == "refined-final"
                for record in selection
            )
        ):
            raise ValidationError(
                "routes.joint_optimization master selection is invalid"
            )
    locks = routes.get("direction_locks")
    if not isinstance(locks, list):
        raise ValidationError("routes.direction_locks: expected an array")
    lock_by_link = {}
    for lock in locks:
        if not isinstance(lock, dict):
            raise ValidationError("routes.direction_locks: invalid record")
        link = next(
            (item for item in platform.links if item.id == lock.get("link")),
            None,
        )
        if link is None or link.direction != "half_duplex":
            raise ValidationError(
                "routes.direction_locks: lock must name a half-duplex link"
            )
        direction = (lock.get("from"), lock.get("to"))
        if set(direction) != set(link.endpoints):
            raise ValidationError(
                "routes.direction_locks: direction does not match endpoints"
            )
        if link.id in lock_by_link:
            raise ValidationError(
                f"routes.direction_locks: duplicate link {link.id!r}"
            )
        lock_by_link[link.id] = direction
    expected_locks = {
        link.id for link in platform.links if link.direction == "half_duplex"
    }
    if set(lock_by_link) != expected_locks:
        raise ValidationError(
            "routes.direction_locks: half-duplex lock coverage is not exact"
        )
    for route in routes["routes"]:
        for edge in route["tree_edges"]:
            locked = lock_by_link.get(edge["link"])
            if locked is not None and locked != (edge["from"], edge["to"]):
                raise ValidationError(
                    f"route {route['id']!r}: violates direction lock"
                )

    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    exact_dependency_schedule = assignment_has_static_exact_contract(assignment)
    if routes.get("round_capacity_contract") is not None:
        raise ValidationError("global transport-round capacity routing is retired")
    rounds = {route.get("transport_round", 0) for route in routes["routes"]}
    if not exact_dependency_schedule and rounds == {0, 1}:
        raise ValidationError("registered round-barrier routing is retired; use Static Exact")
    round_capacity_validation = (
        {"transport_round_policy": "dependency-diagnostic-only"}
        if exact_dependency_schedule else {}
    )
    timed_provider = timing_paths is not None
    route_count = len(routes["routes"])
    route_rank_by_net: Dict[str, int] = {}
    route_delays = array("d", [0.0]) * route_count
    route_signature_offsets = array("Q", [0])
    route_signature_arcs = array("Q")
    ordered_arc_keys = sorted(
        build_directed_graph(platform, constraints)[1]
    )
    arc_index = {key: index for index, key in enumerate(ordered_arc_keys)}
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        signature = []
        for edge in route["tree_edges"]:
            graph[edge["from"]].append((edge["to"], edge["link"]))
            signature.append(
                arc_index[_arc_key(edge["link"], edge["from"], edge["to"])]
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id in graph[node]:
                delay_by_node[sink] = delay_by_node[node] + route_link_delay_ns(
                    platform, link_id, node, sink, constraints
                )
                queue.append(sink)
        predicted_delay = max(
            delay_by_node[sink] for sink in route["sinks"]
        )
        if (
            abs(
                float(route.get("predicted_max_delay_ns", float("nan")))
                - predicted_delay
            )
            > 1.0e-9
        ):
            raise ValidationError(
                f"route {route['id']!r}.predicted_max_delay_ns does not "
                "match independent edge-delay recomputation"
            )
        route_delays[route_rank] = predicted_delay
        if timed_provider:
            net = route["net"]
            if net in route_rank_by_net:
                raise ValidationError(
                    f"routes contain duplicate net identity {net!r}"
                )
            route_rank_by_net[net] = route_rank
            route_signature_arcs.extend(sorted(signature))
            route_signature_offsets.append(len(route_signature_arcs))

    if timing_paths is None:
        if "timing" in routes:
            raise ValidationError(
                "routes without STA input must not contain timing records"
            )
        return {
            **validation,
            "provider": provider,
            "timing_optimization_enabled": False,
            "direction_locks": len(lock_by_link),
            "predicted_delay_check": "pass",
            **round_capacity_validation,
        }

    # Independently reconstruct the route/TDM co-optimization proxy used by
    # the native router.  This is deliberately separate from native output:
    # a corrupt or incorrectly implemented optimizer cannot self-certify.
    _, arcs, capacities = build_directed_graph(
        platform,
        constraints,
        reserve_arrival_and_commit=(
            assignment_has_static_exact_contract(assignment)
        ),
    )
    round_aware = (
        not assignment_has_static_exact_contract(assignment)
        and sorted(
            {
                int(route.get("transport_round", 0))
                for route in routes["routes"]
            }
        )
        == [0, 1]
    )
    capacity_usage: Dict[Any, int] = defaultdict(int)
    for route in routes["routes"]:
        width = int(route["width_bits"])
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            capacity_usage[capacity_key] += width
    link_by_id = {link.id: link for link in platform.links}
    ratios: Dict[Any, int] = {}
    ratio_keys = (
        [
            (key, round_id)
            for key in capacities
            for round_id in (0, 1)
        ]
        if round_aware
        else list(capacities)
    )
    for ratio_key in ratio_keys:
        key = ratio_key[0] if round_aware else ratio_key
        capacity = capacities[key]
        lanes = link_by_id[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        signals = capacity_usage[ratio_key]
        ratios[ratio_key] = estimate_tdm_ratio(
            signals,
            lanes,
            constraints,
            is_sll=capacity["link"] in constraints["sll_links"],
        )
    ratios = apply_bank_domain_ratios(capacities, capacity_usage, ratios, constraints)
    route_tdm_delays = array("d", [0.0]) * route_count
    for route_rank, route in enumerate(routes["routes"]):
        graph: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        for edge in route["tree_edges"]:
            key = _arc_key(edge["link"], edge["from"], edge["to"])
            capacity_key: Any = arcs[key]["capacity_key"]
            if round_aware:
                capacity_key = (
                    capacity_key,
                    int(route.get("transport_round", 0)),
                )
            graph[edge["from"]].append(
                (edge["to"], edge["link"], capacity_key)
            )
        delay_by_node = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for sink, link_id, capacity_key in graph[node]:
                link = link_by_id[link_id]
                delay_by_node[sink] = (
                    delay_by_node[node]
                    + route_link_delay_ns(
                        platform, link_id, node, sink, constraints
                    )
                    + route_link_delay_coefficients_ns(
                        platform, link_id, node, sink, constraints
                    )[1] * (ratios[capacity_key] - 1)
                )
                queue.append(sink)
        if constraints.get("tree_edge_sum_tdm", False):
            route_tdm_delays[route_rank] = sum(
                route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                + (
                    0.0
                    if edge["link"] in constraints["sll_links"]
                    else (
                        route_link_delay_coefficients_ns(
                            platform, edge["link"], edge["from"], edge["to"], constraints
                        )[1]
                    )
                    * (
                        ratios[
                            (
                                arcs[
                                    _arc_key(
                                        edge["link"],
                                        edge["from"],
                                        edge["to"],
                                    )
                                ]["capacity_key"],
                                int(route.get("transport_round", 0)),
                            )
                            if round_aware
                            else arcs[
                                _arc_key(
                                    edge["link"],
                                    edge["from"],
                                    edge["to"],
                                )
                            ]["capacity_key"]
                        ]
                        - 1
                    )
                )
                for edge in route["tree_edges"]
            )
        else:
            route_tdm_delays[route_rank] = max(
                delay_by_node[sink] for sink in route["sinks"]
            )

    timing = routes.get("timing")
    if not isinstance(timing, dict) or timing.get("schema") != STA_PATHS_SCHEMA:
        raise ValidationError("routes.timing: invalid timing artifact")
    records = timing.get("paths")
    if not isinstance(records, list) or len(records) != len(
        timing_paths["paths"]
    ):
        raise ValidationError("routes.timing.paths: coverage is not exact")
    if timing.get("normalization") != timing_paths["normalization"]:
        raise ValidationError(
            "routes.timing.normalization does not match normalized STA input"
        )
    if timing.get("compression") != timing_paths["compression"]:
        raise ValidationError(
            "routes.timing.compression does not match normalized STA input"
        )
    positive_scale = timing_paths["normalization"][
        "positive_slack_scale_ns"
    ]
    negative_scale = timing_paths["normalization"][
        "negative_slack_scale_ns"
    ]
    max_period = timing_paths["normalization"]["max_clock_period_ns"]
    worst_slack = float("inf")
    worst_normalized = float("inf")
    estimated_worst_tdm_slack = float("inf")
    estimated_worst_tdm_normalized = float("inf")
    for expected, actual in zip(timing_paths["paths"], records):
        if actual.get("path") != expected["id"]:
            raise ValidationError("routes.timing.paths: order/identity mismatch")
        for key in (
            "clock_domain",
            "clock_period_ns",
            "fixed_delay_ns",
            "cut_nets",
            "cut_signature",
            "compressed_path_ids",
        ):
            if actual.get(key) != expected[key]:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match normalized STA input"
                )
        required_time = expected["fixed_delay_ns"] + expected["slack_ns"]
        if actual.get("required_time_ns") != required_time:
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.required_time_ns: "
                "does not match normalized STA input"
            )
        if actual.get("cut_transitions") != expected.get(
            "cut_transitions"
        ):
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.cut_transitions: "
                "does not match normalized STA input"
            )
        try:
            path_route_ranks = [
                route_rank_by_net[net] for net in expected["cut_nets"]
            ]
        except KeyError as error:
            raise ValidationError(
                f"routes.timing path {expected['id']!r} references an "
                "unknown cut net"
            ) from error
        delay = expected["fixed_delay_ns"] + sum(
            route_delays[rank] for rank in path_route_ranks
        )
        slack = required_time - delay
        if slack >= 0.0:
            normalized = (
                slack
                * expected["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            normalized = (
                slack / (negative_scale * expected["clock_period_ns"])
            )
        for key, value in (
            ("delay_ns", delay),
            ("slack_ns", slack),
            ("normalized_slack", normalized),
        ):
            if abs(float(actual.get(key, float("nan"))) - value) > 1.0e-9:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match independent recomputation"
                )
        expected_signature = ",".join(
            str(arc)
            for rank in path_route_ranks
            for arc in route_signature_arcs[
                route_signature_offsets[rank]
                : route_signature_offsets[rank + 1]
            ]
        )
        if not expected_signature:
            expected_signature = "-"
        if actual.get("route_signature") != expected_signature:
            raise ValidationError(
                f"routes.timing path {expected['id']!r}.route_signature: "
                "does not match independently reconstructed routes"
            )
        worst_slack = min(worst_slack, slack)
        worst_normalized = min(worst_normalized, normalized)
        tdm_delay = expected["fixed_delay_ns"] + sum(
            route_tdm_delays[rank] for rank in path_route_ranks
        )
        tdm_slack = required_time - tdm_delay
        if tdm_slack >= 0.0:
            tdm_normalized = (
                tdm_slack
                * expected["clock_period_ns"]
                / (positive_scale * max_period)
            )
        else:
            tdm_normalized = (
                tdm_slack
                / (negative_scale * expected["clock_period_ns"])
            )
        for key, value in (
            ("estimated_tdm_slack_ns", tdm_slack),
            ("estimated_tdm_normalized_slack", tdm_normalized),
        ):
            if abs(float(actual.get(key, float("nan"))) - value) > 1.0e-9:
                raise ValidationError(
                    f"routes.timing path {expected['id']!r}.{key}: "
                    "does not match independent recomputation"
                )
        estimated_worst_tdm_slack = min(
            estimated_worst_tdm_slack, tdm_slack
        )
        estimated_worst_tdm_normalized = min(
            estimated_worst_tdm_normalized, tdm_normalized
        )
    metrics = routes.get("metrics", {})
    if abs(metrics.get("worst_slack_ns", float("nan")) - worst_slack) > 1.0e-9:
        raise ValidationError(
            "routes.metrics.worst_slack_ns does not match timing paths"
        )
    if (
        abs(
            metrics.get("worst_normalized_slack", float("nan"))
            - worst_normalized
        )
        > 1.0e-9
    ):
        raise ValidationError(
            "routes.metrics.worst_normalized_slack does not match timing paths"
        )
    for key, expected in (
        ("estimated_worst_tdm_slack_ns", estimated_worst_tdm_slack),
        (
            "estimated_worst_tdm_normalized_slack",
            estimated_worst_tdm_normalized,
        ),
        ("estimated_max_tdm_ratio", max(ratios.values(), default=1)),
    ):
        if abs(float(metrics.get(key, float("nan"))) - expected) > 1.0e-9:
            raise ValidationError(
                f"routes.metrics.{key} does not match independent "
                "route/TDM proxy recomputation"
            )
    # The common timing reconstruction below is a separate implementation
    # check.  Release this validator's route-scale identity/metric columns
    # before invoking it so the two independent representations never overlap
    # at peak RSS.
    del route_rank_by_net
    del route_delays
    del route_signature_offsets
    del route_signature_arcs
    del route_tdm_delays
    common_timing = reconstruct_system_route_timing(
        assignment, platform, routes, timing_paths
    )
    for key, expected in (
        ("worst_slack_ns", worst_slack),
        ("worst_normalized_slack", worst_normalized),
        ("estimated_worst_tdm_slack_ns", estimated_worst_tdm_slack),
        (
            "estimated_worst_tdm_normalized_slack",
            estimated_worst_tdm_normalized,
        ),
        ("estimated_max_tdm_ratio", max(ratios.values(), default=1)),
    ):
        if abs(float(common_timing[key]) - expected) > 1.0e-9:
            raise ValidationError(
                f"common route timing reconstruction disagrees on {key}"
            )
    return {
        **common_timing,
        "direction_locks": len(locks),
        "timing_optimization_enabled": evaluation is None,
        "accepted_reroutes": metrics.get("accepted_reroutes"),
        "rolled_back_reroutes": metrics.get("rolled_back_reroutes"),
        **round_capacity_validation,
    }
