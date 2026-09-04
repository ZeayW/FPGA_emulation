from __future__ import annotations

import hashlib
import math
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import TDMScheduleInfeasibleError, ValidationError
from .platform import Platform
from .routing import (
    SYSTEM_ROUTES_SCHEMA,
    build_directed_graph,
    normalize_route_constraints,
)


TDM_SCHEDULE_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_BASELINE_PROVIDER = "deterministic-round-barrier-earliest-slot-v2"
SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA = (
    "emuflow.sampled-virtual-wire-schedule-certificate/v1"
)
SAMPLED_VIRTUAL_WIRE_CONSTRAINTS_SCHEMA = (
    "emuflow.sampled-virtual-wire-timing-constraints/v1"
)
TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER = (
    "lagrangian-kkt-ratio-aware-list-schedule-v1"
)
TDM_ACADEMIC_SCHEDULE_PROVIDER = (
    "lagrangian-kkt-ratio-aware-path-local-search-v2"
)
COMBINATIONAL_SETTLE_SLOTS = 1
RUNTIME_BARRIER_SLOTS = 1
HopKey = Tuple[str, str, str, str]


def sampled_virtual_wire_timing_constraints(
    frame_slots: int,
) -> Dict[str, Any]:
    """Create the Phase-5-owned causal timing policy.

    Phase 3 owns only structural cut/dependency identities.  Slot readiness,
    settle allowance, and the frame commit edge are scheduling policy and are
    therefore materialized here by the unified Phase 5 implementation.
    """

    if isinstance(frame_slots, bool) or not isinstance(frame_slots, int) or frame_slots < 2:
        raise ValidationError("sampled virtual-wire frame must have at least two slots")
    return {
        "schema": SAMPLED_VIRTUAL_WIRE_CONSTRAINTS_SCHEMA,
        "settle_slots": COMBINATIONAL_SETTLE_SLOTS,
        "commit_slot": frame_slots - RUNTIME_BARRIER_SLOTS,
        "slot_edge_convention": "tx-sample-before-rx-shadow-update-v1",
    }


def sampled_logic_segment_budget_slots(
    segment: Mapping[str, Any],
    timing_constraints: Mapping[str, Any],
) -> int:
    if (
        segment.get("kind") == "launch_to_tx"
        and segment.get("source_semantics") == "configuration-stable-constant"
    ):
        return 0
    value = timing_constraints.get("settle_slots")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("sampled virtual-wire settle slots are invalid")
    return value


def is_sampled_virtual_wire_schedule(schedule: Mapping[str, Any]) -> bool:
    """Return whether transport semantics require readiness/capture checks.

    Solver/provider identity is intentionally irrelevant: any unified Phase 5
    implementation may produce the schedule as long as the semantic contract
    and its independently reconstructible certificate are present.
    """

    return (
        schedule.get("transport_semantics") == "sampled-virtual-wire"
        and isinstance(schedule.get("semantic_contract_sha256"), str)
        and isinstance(schedule.get("schedule_dependency_certificate"), dict)
    )


def _static_exact_contract_from_routes(
    routes: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    contract = routes.get("semantic_contract")
    digest = routes.get("semantic_contract_sha256")
    if contract is None and digest is None:
        return None
    if not isinstance(contract, dict) or not isinstance(digest, str):
        raise ValidationError(
            "routes exact semantic contract binding is incomplete"
        )
    from .combinational_cut import (
        STATIC_EXACT_CANDIDATE_POLICIES,
        STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS,
        STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA,
        semantic_contract_sha256,
    )

    if (
        contract.get("schema") not in STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS
        or contract.get("mode") != "static-exact-combinational"
        or contract.get("qualification")
        != "structural-partition-legality"
        or semantic_contract_sha256(contract) != digest
    ):
        raise ValidationError("routes exact semantic contract binding is invalid")
    if (
        contract.get("schema") == STATIC_EXACT_STRUCTURAL_CONTRACT_SCHEMA
        and contract.get("candidate_selection_policy")
        not in STATIC_EXACT_CANDIDATE_POLICIES
    ):
        raise ValidationError(
            "routes structural exact-cut certificate is incomplete"
        )
    nodes = contract.get("cut_nodes")
    raw_routes = routes.get("routes")
    if not isinstance(nodes, list) or not isinstance(raw_routes, list):
        raise ValidationError("exact contract nodes and routes must be arrays")
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("net"), str)
        and bool(item["net"])
        for item in nodes + raw_routes
    ):
        raise ValidationError(
            "exact contract nodes and routed demands require non-empty net "
            "identities"
        )
    nodes_by_net = {
        item["net"]: item for item in nodes
    }
    routes_by_net = {
        item["net"]: item for item in raw_routes
    }
    if (
        len(nodes_by_net) != len(nodes)
        or len(routes_by_net) != len(raw_routes)
        or set(nodes_by_net) != set(routes_by_net)
    ):
        raise ValidationError(
            "exact contract node coverage does not match routed demands"
        )
    for net_id in sorted(nodes_by_net):
        node = nodes_by_net[net_id]
        route = routes_by_net[net_id]
        expected = {
            "cut_class": route.get("cut_class"),
            "source_fpgas": [route.get("source")],
            "sink_fpgas": route.get("sinks"),
            "dependency_level": route.get("dependency_level"),
            "combinational_dependency_depth": route.get(
                "combinational_dependency_depth"
            ),
            "predecessor_cut_nets": route.get("predecessor_cut_nets"),
        }
        for field, value in expected.items():
            if node.get(field) != value:
                raise ValidationError(
                    f"exact route {net_id!r} does not match contract {field}"
                )
    return contract


def _hop_key(
    demand_id: str, link_id: str, source: str, sink: str
) -> HopKey:
    return (demand_id, link_id, source, sink)


def _route_hops(
    route: Mapping[str, Any],
) -> List[Tuple[int, Mapping[str, Any]]]:
    adjacency: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for edge in route["tree_edges"]:
        adjacency[edge["from"]].append(edge)
    for node in adjacency:
        adjacency[node].sort(
            key=lambda edge: (edge["to"], edge["link"])
        )
    result: List[Tuple[int, Mapping[str, Any]]] = []
    queue = deque([(route["source"], 0)])
    seen = {route["source"]}
    while queue:
        node, depth = queue.popleft()
        for edge in adjacency.get(node, []):
            if edge["to"] in seen:
                raise ValidationError(
                    f"TDM input route {route['id']!r} is not an acyclic tree"
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
            f"TDM input route {route['id']!r} has disconnected tree edges"
        )
    return result


def _link_by_id(platform: Platform):
    return {link.id: link for link in platform.links}


def _round_order(
    routes: Sequence[Mapping[str, Any]],
    planned_hops: Optional[Mapping[HopKey, Mapping[str, Any]]] = None,
) -> Tuple[List[Mapping[str, Any]], List[int]]:
    for route in routes:
        transport_round = route.get("transport_round", 0)
        if (
            isinstance(transport_round, bool)
            or not isinstance(transport_round, int)
            or transport_round < 0
        ):
            raise ValidationError(
                f"TDM route {route['id']!r}.transport_round must be a "
                "non-negative integer"
            )
    ordered = sorted(
        routes,
        key=lambda route: (
            route.get("transport_round", 0),
            min(
                (
                    planned_hops[
                        _hop_key(
                            route["id"],
                            edge["link"],
                            edge["from"],
                            edge["to"],
                        )
                    ]["continuous_ratio"]
                    for _depth, edge in _route_hops(route)
                ),
                default=float("inf"),
            )
            if planned_hops is not None
            else 0.0,
            -max((depth for depth, _ in _route_hops(route)), default=0),
            route["net"],
            route["id"],
        ),
    )
    active_rounds = sorted(
        {route.get("transport_round", 0) for route in routes}
    )
    return ordered, active_rounds


def _round_barrier_realization(
    active_rounds: Sequence[int],
    completion_by_round: Mapping[int, int],
    planned_source_ready_slot: Optional[int],
) -> Dict[str, Any]:
    """Describe the concrete barrier realized by a legal slot schedule.

    The ratio planner's source-ready slot is a capacity-split estimate.  It
    deliberately ignores multicast-tree precedence and therefore cannot be a
    hard upper bound on the concrete schedule.  The schedule is the
    authoritative realization and is checked independently below.
    """
    realized = None
    if len(active_rounds) > 1:
        second_round = active_rounds[1]
        realized = max(
            (
                completion + COMBINATIONAL_SETTLE_SLOTS
                for transport_round, completion in completion_by_round.items()
                if transport_round < second_round
            ),
            default=0,
        )
    return {
        "active_rounds": list(active_rounds),
        "capacity_split_slot": planned_source_ready_slot,
        "source_ready_slot": realized,
        "shift_slots": (
            realized - planned_source_ready_slot
            if realized is not None and planned_source_ready_slot is not None
            else 0
        ),
    }


def _exact_route_metadata(route: Mapping[str, Any]) -> Dict[str, Any]:
    record = {
        "id": route["id"],
        "net": route["net"],
        "source": route["source"],
        "sinks": list(route["sinks"]),
        "cut_class": route["cut_class"],
        "dependency_level": route["dependency_level"],
        "combinational_dependency_depth": route[
            "combinational_dependency_depth"
        ],
        "predecessor_cut_nets": list(route["predecessor_cut_nets"]),
        "tree_edges": list(route["tree_edges"]),
    }
    if "transport_round" in route:
        record["transport_round"] = route["transport_round"]
    return record


def _exact_contract_indexes(
    contract: Mapping[str, Any],
) -> Tuple[
    Dict[str, Mapping[str, Any]],
    Dict[str, Mapping[str, Any]],
    Dict[str, Mapping[str, Any]],
]:
    raw_nodes = contract.get("cut_nodes")
    raw_segments = contract.get("logic_segments")
    raw_captures = contract.get("capture_requirements")
    for field, value in (
        ("cut_nodes", raw_nodes),
        ("logic_segments", raw_segments),
        ("capture_requirements", raw_captures),
    ):
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValidationError(
                f"exact semantic contract {field} must be an object array"
            )
    if not all(
        isinstance(item.get("net"), str) and item["net"]
        for item in raw_nodes
    ):
        raise ValidationError("exact cut nodes require non-empty net identities")
    if not all(
        isinstance(item.get("id"), str) and item["id"]
        for item in raw_segments + raw_captures
    ):
        raise ValidationError(
            "exact logic segments and captures require non-empty identities"
        )
    nodes = {item["net"]: item for item in raw_nodes}
    segments = {item["id"]: item for item in raw_segments}
    captures = {item["id"]: item for item in raw_captures}
    if (
        len(nodes) != len(raw_nodes)
        or len(segments) != len(raw_segments)
        or len(captures) != len(raw_captures)
    ):
        raise ValidationError("exact semantic contract identities are not unique")
    return nodes, segments, captures


def _exact_source_readiness(
    node: Mapping[str, Any],
    segments: Mapping[str, Mapping[str, Any]],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[int, List[Dict[str, Any]]]:
    evidence = []
    for segment_id in node.get("source_segment_ids", []):
        segment = segments.get(segment_id)
        if segment is None:
            raise ValidationError(
                f"exact cut {node.get('net')!r} references unknown segment "
                f"{segment_id!r}"
            )
        budget = sampled_logic_segment_budget_slots(segment, timing_constraints)
        kind = segment.get("kind")
        if kind == "launch_to_tx":
            if segment.get("sink_cut_net") != node.get("net"):
                raise ValidationError(
                    f"exact launch segment {segment_id!r} has wrong sink"
                )
            ready = budget
            evidence.append(
                {
                    "segment": segment_id,
                    "kind": kind,
                    "launch_slot": 0,
                    "budget_slots": budget,
                    "ready_slot": ready,
                }
            )
        elif kind == "rx_to_tx":
            predecessor = segment.get("source_cut_net")
            source_fpga = node["source_fpgas"][0]
            if (
                segment.get("sink_cut_net") != node.get("net")
                or segment.get("fpga") != source_fpga
                or predecessor not in node.get("predecessor_cut_nets", [])
            ):
                raise ValidationError(
                    f"exact dependency segment {segment_id!r} is inconsistent"
                )
            key = (predecessor, source_fpga)
            if key not in arrivals:
                raise ValidationError(
                    f"exact predecessor {predecessor!r} has not arrived at "
                    f"{source_fpga!r}"
                )
            arrival = arrivals[key]
            ready = arrival + budget
            evidence.append(
                {
                    "segment": segment_id,
                    "kind": kind,
                    "predecessor_cut_net": predecessor,
                    "arrival_slot": arrival,
                    "budget_slots": budget,
                    "ready_slot": ready,
                }
            )
        else:
            raise ValidationError(
                f"exact source segment {segment_id!r} has unsupported kind"
            )
    if not evidence:
        raise ValidationError(
            f"exact cut {node.get('net')!r} has no source-readiness segment"
        )
    predecessors = sorted(node.get("predecessor_cut_nets", []))
    evidenced_predecessors = sorted(
        item["predecessor_cut_net"]
        for item in evidence
        if item["kind"] == "rx_to_tx"
    )
    if predecessors != evidenced_predecessors:
        raise ValidationError(
            f"exact cut {node.get('net')!r} predecessor coverage is incomplete"
        )
    return max(item["ready_slot"] for item in evidence), evidence


def _exact_capture_certificate(
    segments: Mapping[str, Mapping[str, Any]],
    captures: Mapping[str, Mapping[str, Any]],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    records = []
    commit_slot = timing_constraints["commit_slot"]
    # A large design can have tens of thousands of terminal captures.  Build
    # the reverse relation once instead of scanning every logic segment for
    # every capture (O(captures * segments)).  The independent validator below
    # deliberately reconstructs its own index and exact coverage check.
    segments_by_capture: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments.values():
        if segment.get("kind") != "rx_to_capture":
            continue
        capture_id = segment.get("capture_requirement")
        if isinstance(capture_id, str):
            segments_by_capture[capture_id].append(segment)
    for capture_id in sorted(captures):
        capture = captures[capture_id]
        matching = segments_by_capture.get(capture_id, [])
        if len(matching) != 1:
            raise ValidationError(
                f"exact capture {capture_id!r} must have one timing segment"
            )
        segment = matching[0]
        cut_net = capture["cut_net"]
        fpga = capture["fpga"]
        key = (cut_net, fpga)
        if (
            segment.get("source_cut_net") != cut_net
            or segment.get("fpga") != fpga
            or key not in arrivals
        ):
            raise ValidationError(
                f"exact capture {capture_id!r} is not bound to an arrival"
            )
        budget = sampled_logic_segment_budget_slots(segment, timing_constraints)
        arrival = arrivals[key]
        ready = arrival + budget
        slack = commit_slot - ready
        if slack < 0:
            raise ValidationError(
                "exact schedule is infeasible at capture "
                f"{capture_id!r}: ready={ready}, commit={commit_slot}"
            )
        records.append(
            {
                "capture": capture_id,
                "cut_net": cut_net,
                "fpga": fpga,
                "segment": segment["id"],
                "arrival_slot": arrival,
                "budget_slots": budget,
                "ready_slot": ready,
                "commit_slot": commit_slot,
                "slack_slots": slack,
            }
        )
    minimum = min((item["slack_slots"] for item in records), default=None)
    return records, minimum


def build_tdm_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    ratio_plan: Optional[Mapping[str, Any]] = None,
    *,
    prepared_ratio_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(
            f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}, "
            f"got {routes.get('schema')!r}"
        )
    exact_contract = _static_exact_contract_from_routes(routes)
    constraints = normalize_route_constraints(
        routes.get("constraints"),
        platform,
    )
    frame_slots = constraints["frame_slots"]
    sampled_timing_constraints = (
        sampled_virtual_wire_timing_constraints(frame_slots)
        if exact_contract is not None
        else None
    )
    _, arcs, capacity_records = build_directed_graph(platform, constraints)
    links = _link_by_id(platform)
    planned_hops = None
    planned_round_one_ready = None
    if ratio_plan is not None:
        from .tdm_ratio import (
            ratio_plan_by_hop,
            validate_tdm_ratio_plan,
        )

        validate_tdm_ratio_plan(
            routes,
            platform,
            ratio_plan,
            prepared_model=prepared_ratio_model,
        )
        planned_hops = ratio_plan_by_hop(ratio_plan)
        planned_round_one_ready = ratio_plan[
            "round_barrier_legalization"
        ]["source_ready_slot"]
    slot_fill: Dict[str, List[int]] = {
        key: [0] * frame_slots for key in capacity_records
    }
    next_available: Dict[str, List[int]] = {
        key: list(range(frame_slots + 1)) for key in capacity_records
    }
    planned_occupancy: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)

    def first_available_slot(capacity_key: str, start: int) -> int:
        parents = next_available[capacity_key]
        slot = start
        while parents[slot] != slot:
            parents[slot] = parents[parents[slot]]
            slot = parents[slot]
        root = slot
        slot = start
        while parents[slot] != slot:
            successor = parents[slot]
            parents[slot] = root
            slot = successor
        return root

    entries: List[Dict[str, Any]] = []
    demand_completions: List[Dict[str, Any]] = []

    raw_routes = routes.get("routes")
    if not isinstance(raw_routes, list):
        raise ValidationError("routes.routes: expected an array")
    exact_nodes = None
    exact_segments = None
    exact_captures = None
    exact_arrivals: Dict[Tuple[str, str], int] = {}
    exact_readiness_records: List[Dict[str, Any]] = []
    if exact_contract is None:
        ordered_routes, active_rounds = _round_order(
            raw_routes, planned_hops
        )
    else:
        exact_nodes, exact_segments, exact_captures = (
            _exact_contract_indexes(exact_contract)
        )
        route_by_net = {route.get("net"): route for route in raw_routes}
        if len(route_by_net) != len(raw_routes) or set(route_by_net) != set(
            exact_nodes
        ):
            raise ValidationError(
                "cross-layer timing cut coverage does not match routes"
            )
        ordered_routes = [
            route_by_net[node["net"]]
            for node in sorted(
                exact_nodes.values(),
                key=lambda item: (item["dependency_level"], item["net"]),
            )
        ]
        # Transport rounds are an implementation detail of registered-boundary
        # transport.  Sampled virtual wires use their path-local readiness DAG.
        active_rounds = [0]
    completion_by_round: Dict[int, int] = {}
    prior_round_completion = -1
    active_round = None
    entry_index = 0
    for route in ordered_routes:
        transport_round = route.get("transport_round", 0)
        if transport_round != active_round:
            if active_round is not None:
                prior_round_completion = max(
                    prior_round_completion,
                    completion_by_round[active_round],
                )
            active_round = transport_round
        if exact_contract is None:
            source_ready_slot = (
                prior_round_completion + COMBINATIONAL_SETTLE_SLOTS
                if prior_round_completion >= 0
                else 0
            )
        else:
            node = exact_nodes[route["net"]]
            source_ready_slot, source_evidence = _exact_source_readiness(
                node,
                exact_segments,
                exact_arrivals,
                sampled_timing_constraints,
            )
            exact_readiness_records.append(
                {
                    "demand": route["id"],
                    "net": route["net"],
                    "source": route["source"],
                    "source_ready_slot": source_ready_slot,
                    "evidence": source_evidence,
                }
            )
        arrival_by_node = {route["source"]: source_ready_slot - 1}
        for depth, edge in _route_hops(route):
            arc_key = (edge["link"], edge["from"], edge["to"])
            if arc_key not in arcs:
                raise ValidationError(
                    f"TDM route {route['id']!r} uses illegal edge {arc_key}"
                )
            arc = arcs[arc_key]
            link = links[edge["link"]]
            ready_slot = (
                source_ready_slot
                if edge["from"] == route["source"]
                else arrival_by_node[edge["from"]] + 1
            )
            # The final frame slot is reserved for the lockstep runtime
            # barrier/virtual-clock release.  A transport arrival must be
            # visible before that slot, not merely before the frame wraps.
            latest_exclusive = (
                frame_slots
                - RUNTIME_BARRIER_SLOTS
                - link.latency_cycles
            )
            plan_hop = (
                planned_hops.get(
                    _hop_key(
                        route["id"],
                        edge["link"],
                        edge["from"],
                        edge["to"],
                    )
                )
                if planned_hops is not None
                else None
            )
            if planned_hops is not None and plan_hop is None:
                raise ValidationError(
                    f"TDM ratio plan is missing demand {route['id']!r} "
                    f"edge {arc_key}"
                )
            if plan_hop is None:
                slot = (
                    frame_slots
                    if ready_slot >= frame_slots
                    else first_available_slot(
                        arc["capacity_key"],
                        ready_slot,
                    )
                )
                lane = (
                    slot_fill[arc["capacity_key"]][slot]
                    if slot < frame_slots
                    else 0
                )
            else:
                lane = plan_hop["lane"]
                ratio = plan_hop["discrete_ratio"]
                ratio_window_end = min(
                    latest_exclusive,
                    ready_slot + ratio,
                )
                slot = ready_slot
                while (
                    slot < ratio_window_end
                    and (slot, lane)
                    in planned_occupancy[arc["capacity_key"]]
                ):
                    slot += 1
            if slot >= latest_exclusive:
                raise TDMScheduleInfeasibleError(
                    f"TDM scheduling is infeasible for demand {route['id']!r} "
                    f"edge {arc_key}: ready={ready_slot}, "
                    f"frame_slots={frame_slots}, "
                    f"latency={link.latency_cycles}"
                )
            if plan_hop is not None and slot >= ready_slot + ratio:
                raise TDMScheduleInfeasibleError(
                    f"TDM ratio schedule is infeasible for demand "
                    f"{route['id']!r} edge {arc_key}: ready={ready_slot}, "
                    f"ratio={ratio}, lane={lane}"
                )
            if plan_hop is None:
                slot_fill[arc["capacity_key"]][slot] += 1
                if (
                    slot_fill[arc["capacity_key"]][slot]
                    == link.transport_bits_per_cycle_per_direction
                ):
                    next_available[arc["capacity_key"]][slot] = (
                        first_available_slot(
                            arc["capacity_key"],
                            slot + 1,
                        )
                    )
            else:
                planned_occupancy[arc["capacity_key"]].add(
                    (slot, lane)
                )
            arrival_slot = slot + link.latency_cycles
            arrival_by_node[edge["to"]] = arrival_slot
            entry = {
                    "id": f"s{entry_index:06d}",
                    "demand": route["id"],
                    "net": route["net"],
                    "hop": depth,
                    "link": edge["link"],
                    "from": edge["from"],
                    "to": edge["to"],
                    "capacity_key": arc["capacity_key"],
                    "slot": slot,
                    "lane": lane,
                    "ready_slot": ready_slot,
                    "arrival_slot": arrival_slot,
                }
            if plan_hop is not None:
                entry.update(
                    {
                        "ratio_plan_hop": plan_hop["index"],
                        "continuous_ratio": plan_hop[
                            "continuous_ratio"
                        ],
                        "tdm_ratio": plan_hop["discrete_ratio"],
                        "ratio_wait_slots": slot - ready_slot,
                    }
                )
            entries.append(entry)
            entry_index += 1

        missing = sorted(set(route["sinks"]) - set(arrival_by_node))
        if missing:
            raise ValidationError(
                f"TDM route {route['id']!r} did not schedule sinks {missing}"
            )
        completion_slot = max(
            arrival_by_node[sink] for sink in route["sinks"]
        )
        completion_by_round[transport_round] = max(
            completion_slot,
            completion_by_round.get(transport_round, completion_slot),
        )
        completion = {
            "demand": route["id"],
            "net": route["net"],
            "transport_round": transport_round,
            "source_ready_slot": source_ready_slot,
            "completion_slot": completion_slot,
        }
        if exact_contract is not None:
            sink_arrivals = {
                sink: arrival_by_node[sink] for sink in sorted(route["sinks"])
            }
            completion["sink_arrival_slots"] = sink_arrivals
            for sink, arrival in sink_arrivals.items():
                exact_arrivals[(route["net"], sink)] = arrival
        demand_completions.append(completion)

    entries.sort(
        key=lambda entry: (
            entry["slot"],
            entry["capacity_key"],
            entry["lane"],
            entry["demand"],
        )
    )
    domain_schedules = _domain_schedule_records(
        platform,
        constraints,
        entries,
    )
    metrics = {
        "demands": len(raw_routes),
        "scheduled_bit_hops": len(entries),
        "frame_slots": frame_slots,
        "completion_slot": max(
            (
                completion["completion_slot"]
                for completion in demand_completions
            ),
            default=0,
        ),
        "max_domain_utilization": max(
            (domain["utilization"] for domain in domain_schedules),
            default=0.0,
        ),
        "transport_rounds": len(active_rounds),
        "round_barriers": max(0, len(active_rounds) - 1),
        "max_transport_round": max(active_rounds, default=0),
        "combinational_settle_slots": COMBINATIONAL_SETTLE_SLOTS,
        "collisions": 0,
    }
    if ratio_plan is not None:
        metrics.update(
            {
                "ratio_constrained_hops": len(entries),
                "max_tdm_ratio": max(
                    entry["tdm_ratio"] for entry in entries
                ),
                "maximum_ratio_wait_slots": max(
                    entry["ratio_wait_slots"] for entry in entries
                ),
            }
        )
    exact_certificate = None
    if exact_contract is not None:
        capture_records, minimum_capture_slack = _exact_capture_certificate(
            exact_segments,
            exact_captures,
            exact_arrivals,
            sampled_timing_constraints,
        )
        exact_certificate = {
            "schema": SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
            "provider": "independent-readiness-certificate-v1",
            "topological_cut_order": [route["net"] for route in ordered_routes],
            "demand_readiness": exact_readiness_records,
            "capture_readiness": capture_records,
            "minimum_capture_slack_slots": minimum_capture_slack,
        }
        metrics.update(
            {
                "commit_slot": sampled_timing_constraints["commit_slot"],
                "dependency_edges": len(exact_contract["dependency_edges"]),
                "maximum_combinational_dependency_depth": exact_contract[
                    "metrics"
                ]["maximum_combinational_dependency_depth"],
                "capture_requirements": len(capture_records),
                "minimum_capture_slack_slots": minimum_capture_slack,
            }
        )
    result = {
        "schema": TDM_SCHEDULE_SCHEMA,
        "design": routes.get("design"),
        "platform": platform.name,
        "provider": (
            TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER
            if ratio_plan is not None
            else TDM_BASELINE_PROVIDER
        ),
        **(
            {
                "ratio_assignment": {
                    "schema": ratio_plan["schema"],
                    "provider": ratio_plan["provider"],
                    "configuration": ratio_plan["configuration"],
                    "metrics": ratio_plan["metrics"],
                    "round_barrier_legalization": ratio_plan[
                        "round_barrier_legalization"
                    ],
                }
            }
            if ratio_plan is not None
            else {}
        ),
        "route_constraints": constraints,
        "routes": [
            {
                "id": route["id"],
                "net": route["net"],
                "source": route["source"],
                "sinks": list(route["sinks"]),
                "transport_round": route.get("transport_round", 0),
                "tree_edges": list(route["tree_edges"]),
            }
            for route in sorted(raw_routes, key=lambda item: item["id"])
        ],
        "entries": entries,
        "demand_completions": sorted(
            demand_completions, key=lambda item: item["demand"]
        ),
        "domain_schedules": domain_schedules,
        "metrics": metrics,
    }
    if exact_contract is not None:
        result.update(
            {
                "qualification": "dependency-schedule-readiness-pass",
                "transport_semantics": "sampled-virtual-wire",
                "timing_constraints": sampled_timing_constraints,
                "semantic_contract_schema": exact_contract["schema"],
                "semantic_contract_sha256": routes[
                    "semantic_contract_sha256"
                ],
                "schedule_dependency_certificate": exact_certificate,
            }
        )
    if ratio_plan is not None:
        result["round_barrier_realization"] = _round_barrier_realization(
            active_rounds,
            completion_by_round,
            planned_round_one_ready,
        )
    return result


def _domain_schedule_records(
    platform: Platform,
    constraints: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    _, _, capacities = build_directed_graph(platform, constraints)
    links = _link_by_id(platform)
    count_by_key: Dict[str, int] = defaultdict(int)
    for entry in entries:
        count_by_key[entry["capacity_key"]] += 1
    records = []
    for key in sorted(capacities):
        capacity = capacities[key]
        lanes = links[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        scheduled = count_by_key[key]
        records.append(
            {
                "key": key,
                "link": capacity["link"],
                "direction": capacity["direction"],
                "lanes": lanes,
                "frame_slots": constraints["frame_slots"],
                "scheduled_bit_hops": scheduled,
                "capacity_bit_hops": lanes * constraints["frame_slots"],
                "utilization": (
                    scheduled
                    / (lanes * constraints["frame_slots"])
                ),
            }
        )
    return records


def _expected_hops(
    routes: Mapping[str, Any],
) -> Dict[HopKey, Tuple[Mapping[str, Any], int]]:
    expected = {}
    for route in routes["routes"]:
        for depth, edge in _route_hops(route):
            key = _hop_key(
                route["id"], edge["link"], edge["from"], edge["to"]
            )
            expected[key] = (route, depth)
    return expected


def _independently_reconstruct_exact_source_readiness(
    node: Mapping[str, Any],
    segments: Mapping[str, Mapping[str, Any]],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[int, List[Dict[str, Any]]]:
    """Rebuild one cut's readiness without calling the scheduler helper."""
    segment_ids = node.get("source_segment_ids")
    predecessors = node.get("predecessor_cut_nets")
    source_fpgas = node.get("source_fpgas")
    if (
        not isinstance(segment_ids, list)
        or not segment_ids
        or not all(isinstance(item, str) for item in segment_ids)
        or len(segment_ids) != len(set(segment_ids))
        or not isinstance(predecessors, list)
        or not all(isinstance(item, str) for item in predecessors)
        or len(predecessors) != len(set(predecessors))
        or not isinstance(source_fpgas, list)
        or len(source_fpgas) != 1
        or not isinstance(source_fpgas[0], str)
    ):
        raise ValidationError(
            f"exact cut {node.get('net')!r} has malformed readiness metadata"
        )
    evidence: List[Dict[str, Any]] = []
    covered_predecessors = []
    for segment_id in segment_ids:
        segment = segments.get(segment_id)
        if segment is None:
            raise ValidationError(
                f"exact cut {node.get('net')!r} references unknown segment "
                f"{segment_id!r}"
            )
        if (
            segment.get("kind") == "launch_to_tx"
            and segment.get("source_semantics") == "configuration-stable-constant"
        ):
            budget = 0
        else:
            budget = timing_constraints.get("settle_slots")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValidationError("exact scheduling settle slots are invalid")
        kind = segment.get("kind")
        if kind == "launch_to_tx":
            if segment.get("sink_cut_net") != node.get("net"):
                raise ValidationError(
                    f"exact launch segment {segment_id!r} has wrong sink"
                )
            evidence.append(
                {
                    "segment": segment_id,
                    "kind": kind,
                    "launch_slot": 0,
                    "budget_slots": budget,
                    "ready_slot": budget,
                }
            )
            continue
        if kind != "rx_to_tx":
            raise ValidationError(
                f"exact source segment {segment_id!r} has unsupported kind"
            )
        predecessor = segment.get("source_cut_net")
        if (
            not isinstance(predecessor, str)
            or segment.get("sink_cut_net") != node.get("net")
            or segment.get("fpga") != source_fpgas[0]
            or predecessor not in predecessors
        ):
            raise ValidationError(
                f"exact dependency segment {segment_id!r} is inconsistent"
            )
        arrival_key = (predecessor, source_fpgas[0])
        if arrival_key not in arrivals:
            raise ValidationError(
                f"exact predecessor {predecessor!r} has not arrived at "
                f"{source_fpgas[0]!r}"
            )
        arrival = arrivals[arrival_key]
        covered_predecessors.append(predecessor)
        evidence.append(
            {
                "segment": segment_id,
                "kind": kind,
                "predecessor_cut_net": predecessor,
                "arrival_slot": arrival,
                "budget_slots": budget,
                "ready_slot": arrival + budget,
            }
        )
    if sorted(covered_predecessors) != sorted(predecessors):
        raise ValidationError(
            f"exact cut {node.get('net')!r} predecessor coverage is incomplete"
        )
    return max(item["ready_slot"] for item in evidence), evidence


def _independently_reconstruct_exact_captures(
    segments: Mapping[str, Mapping[str, Any]],
    captures: Mapping[str, Mapping[str, Any]],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Rebuild final capture readiness without calling builder code."""
    commit_slot = timing_constraints.get("commit_slot")
    if (
        isinstance(commit_slot, bool)
        or not isinstance(commit_slot, int)
        or commit_slot < 0
    ):
        raise ValidationError("exact contract commit slot is invalid")
    segment_by_capture: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments.values():
        if segment.get("kind") == "rx_to_capture":
            capture_id = segment.get("capture_requirement")
            if not isinstance(capture_id, str):
                raise ValidationError(
                    "exact capture segment lacks a capture identity"
                )
            segment_by_capture[capture_id].append(segment)
    if set(segment_by_capture) != set(captures):
        raise ValidationError("exact capture segment coverage is incomplete")
    records = []
    for capture_id in sorted(captures):
        candidates = segment_by_capture[capture_id]
        if len(candidates) != 1:
            raise ValidationError(
                f"exact capture {capture_id!r} must have one timing segment"
            )
        capture = captures[capture_id]
        segment = candidates[0]
        cut_net = capture.get("cut_net")
        fpga = capture.get("fpga")
        if (
            not isinstance(cut_net, str)
            or not isinstance(fpga, str)
            or segment.get("source_cut_net") != cut_net
            or segment.get("fpga") != fpga
            or (cut_net, fpga) not in arrivals
        ):
            raise ValidationError(
                f"exact capture {capture_id!r} is not bound to an arrival"
            )
        budget = timing_constraints.get("settle_slots")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValidationError("exact scheduling settle slots are invalid")
        arrival = arrivals[(cut_net, fpga)]
        ready = arrival + budget
        slack = commit_slot - ready
        if slack < 0:
            raise ValidationError(
                f"exact capture {capture_id!r} misses the commit deadline"
            )
        records.append(
            {
                "capture": capture_id,
                "cut_net": cut_net,
                "fpga": fpga,
                "segment": segment["id"],
                "arrival_slot": arrival,
                "budget_slots": budget,
                "ready_slot": ready,
                "commit_slot": commit_slot,
                "slack_slots": slack,
            }
        )
    minimum = min((item["slack_slots"] for item in records), default=None)
    return records, minimum


def validate_tdm_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    ratio_plan: Optional[Mapping[str, Any]] = None,
    *,
    prepared_ratio_model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    exact_contract = _static_exact_contract_from_routes(routes)
    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError(
            f"schedule.schema: expected {TDM_SCHEDULE_SCHEMA!r}, "
            f"got {schedule.get('schema')!r}"
        )
    constraints = normalize_route_constraints(
        schedule.get("route_constraints"),
        platform,
    )
    if constraints != normalize_route_constraints(
        routes.get("constraints"), platform
    ):
        raise ValidationError(
            "schedule.route_constraints does not match routes"
        )
    frame_slots = constraints["frame_slots"]
    sampled_timing_constraints = None
    if exact_contract is not None:
        sampled_timing_constraints = sampled_virtual_wire_timing_constraints(
            frame_slots
        )
        if schedule.get("timing_constraints") != sampled_timing_constraints:
            raise ValidationError(
                "sampled virtual-wire timing constraints do not match Phase 5 policy"
            )
    _, arcs, _ = build_directed_graph(platform, constraints)
    links = _link_by_id(platform)
    expected = _expected_hops(routes)
    academic_schedule = schedule.get("provider") in {
        TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER,
        TDM_ACADEMIC_SCHEDULE_PROVIDER,
    }
    extended_schedule = schedule.get("provider") in {
        TDM_BASELINE_PROVIDER,
        TDM_ACADEMIC_LIST_SCHEDULE_PROVIDER,
        TDM_ACADEMIC_SCHEDULE_PROVIDER,
    }
    planned_hops = None
    if academic_schedule:
        if ratio_plan is None:
            raise ValidationError(
                "academic TDM schedule validation requires ratio_plan"
            )
        from .tdm_ratio import (
            ratio_plan_by_hop,
            validate_tdm_ratio_plan,
        )

        validate_tdm_ratio_plan(
            routes,
            platform,
            ratio_plan,
            prepared_model=prepared_ratio_model,
        )
        expected_ratio_assignment = {
            "schema": ratio_plan["schema"],
            "provider": ratio_plan["provider"],
            "configuration": ratio_plan["configuration"],
            "metrics": ratio_plan["metrics"],
            "round_barrier_legalization": ratio_plan[
                "round_barrier_legalization"
            ],
        }
        if schedule.get("ratio_assignment") != expected_ratio_assignment:
            raise ValidationError(
                "schedule.ratio_assignment does not match ratio plan"
            )
        planned_hops = ratio_plan_by_hop(ratio_plan)
    elif ratio_plan is not None:
        raise ValidationError(
            "ratio_plan was supplied for a non-academic TDM schedule"
        )
    expected_route_metadata = []
    for route in sorted(routes["routes"], key=lambda item: item["id"]):
        record = {
            "id": route["id"],
            "net": route["net"],
            "source": route["source"],
            "sinks": list(route["sinks"]),
            "tree_edges": list(route["tree_edges"]),
        }
        if extended_schedule:
            record["transport_round"] = route.get("transport_round", 0)
        expected_route_metadata.append(record)
    if schedule.get("routes") != expected_route_metadata:
        raise ValidationError("schedule.routes does not match system routes")

    raw_entries = schedule.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("schedule.entries: expected an array")
    entries_by_hop: Dict[HopKey, Mapping[str, Any]] = {}
    occupancy: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValidationError(f"schedule.entries[{index}]: expected an object")
        key = _hop_key(
            entry.get("demand"),
            entry.get("link"),
            entry.get("from"),
            entry.get("to"),
        )
        if key not in expected:
            raise ValidationError(
                f"schedule.entries[{index}]: unexpected route hop {key}"
            )
        if key in entries_by_hop:
            raise ValidationError(
                f"schedule.entries[{index}]: duplicate route hop {key}"
            )
        entries_by_hop[key] = entry
        route, depth = expected[key]
        if entry.get("net") != route["net"] or entry.get("hop") != depth:
            raise ValidationError(
                f"schedule.entries[{index}]: net/hop does not match route"
            )
        arc = arcs[(entry["link"], entry["from"], entry["to"])]
        if entry.get("capacity_key") != arc["capacity_key"]:
            raise ValidationError(
                f"schedule.entries[{index}].capacity_key: incorrect"
            )
        link = links[entry["link"]]
        slot = entry.get("slot")
        lane = entry.get("lane")
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or slot >= frame_slots
        ):
            raise ValidationError(
                f"schedule.entries[{index}].slot: out of range"
            )
        if (
            isinstance(lane, bool)
            or not isinstance(lane, int)
            or lane < 0
            or lane >= link.transport_bits_per_cycle_per_direction
        ):
            raise ValidationError(
                f"schedule.entries[{index}].lane: out of range"
            )
        collision = (slot, lane)
        if collision in occupancy[arc["capacity_key"]]:
            raise ValidationError(
                f"schedule collision in {arc['capacity_key']!r} at "
                f"slot={slot}, lane={lane}"
            )
        occupancy[arc["capacity_key"]].add(collision)
        if academic_schedule:
            planned = planned_hops[key]
            ready_value = entry.get("ready_slot")
            if (
                isinstance(ready_value, bool)
                or not isinstance(ready_value, int)
            ):
                raise ValidationError(
                    f"schedule.entries[{index}].ready_slot: "
                    "expected an integer"
                )
            expected_ratio_fields = {
                "ratio_plan_hop": planned["index"],
                "continuous_ratio": planned["continuous_ratio"],
                "tdm_ratio": planned["discrete_ratio"],
                "ratio_wait_slots": slot - ready_value,
            }
            for field, value in expected_ratio_fields.items():
                if entry.get(field) != value:
                    raise ValidationError(
                        f"schedule.entries[{index}].{field}: "
                        "does not match ratio plan"
                    )
            if lane != planned["lane"]:
                raise ValidationError(
                    f"schedule.entries[{index}].lane: "
                    "does not match ratio plan"
                )
            if entry["ratio_wait_slots"] >= entry["tdm_ratio"]:
                raise ValidationError(
                    f"schedule.entries[{index}]: "
                    "wait exceeds TDM ratio window"
                )
        expected_arrival = slot + link.latency_cycles
        if entry.get("arrival_slot") != expected_arrival:
            raise ValidationError(
                f"schedule.entries[{index}].arrival_slot: expected "
                f"{expected_arrival}"
            )
        if expected_arrival >= frame_slots - RUNTIME_BARRIER_SLOTS:
            raise ValidationError(
                f"schedule.entries[{index}]: arrival reaches reserved "
                "runtime barrier slot"
            )
    if set(entries_by_hop) != set(expected):
        missing = sorted(set(expected) - set(entries_by_hop))
        raise ValidationError(
            f"schedule.entries: route-hop coverage is incomplete {missing[:8]}"
        )

    exact_nodes = None
    exact_segments = None
    exact_captures = None
    exact_arrivals: Dict[Tuple[str, str], int] = {}
    exact_readiness_records = []
    if exact_contract is None:
        ordered_routes, active_rounds = _round_order(routes["routes"])
    else:
        exact_nodes, exact_segments, exact_captures = (
            _exact_contract_indexes(exact_contract)
        )
        route_by_net = {route["net"]: route for route in routes["routes"]}
        ordered_routes = [
            route_by_net[node["net"]]
            for node in sorted(
                exact_nodes.values(),
                key=lambda item: (item["dependency_level"], item["net"]),
            )
        ]
        active_rounds = [0]
    completion_by_round: Dict[int, int] = {}
    prior_round_completion = -1
    active_round = None
    completions = []
    for route in ordered_routes:
        transport_round = route.get("transport_round", 0)
        if transport_round != active_round:
            if active_round is not None:
                prior_round_completion = max(
                    prior_round_completion,
                    completion_by_round[active_round],
                )
            active_round = transport_round
        if exact_contract is None:
            source_ready_slot = (
                prior_round_completion + COMBINATIONAL_SETTLE_SLOTS
                if prior_round_completion >= 0
                else 0
            )
        else:
            source_ready_slot, source_evidence = (
                _independently_reconstruct_exact_source_readiness(
                    exact_nodes[route["net"]],
                    exact_segments,
                    exact_arrivals,
                    sampled_timing_constraints,
                )
            )
            exact_readiness_records.append(
                {
                    "demand": route["id"],
                    "net": route["net"],
                    "source": route["source"],
                    "source_ready_slot": source_ready_slot,
                    "evidence": source_evidence,
                }
            )
        arrival_by_node = {route["source"]: source_ready_slot - 1}
        for depth, edge in _route_hops(route):
            del depth
            entry = entries_by_hop[
                _hop_key(
                    route["id"],
                    edge["link"],
                    edge["from"],
                    edge["to"],
                )
            ]
            ready = (
                source_ready_slot
                if edge["from"] == route["source"]
                else arrival_by_node[edge["from"]] + 1
            )
            if entry.get("ready_slot") != ready:
                raise ValidationError(
                    f"schedule demand {route['id']!r}: ready-slot mismatch"
                )
            if entry["slot"] < ready:
                raise ValidationError(
                    f"schedule demand {route['id']!r}: precedence violation"
                )
            arrival_by_node[edge["to"]] = entry["arrival_slot"]
        missing_sinks = sorted(set(route["sinks"]) - set(arrival_by_node))
        if missing_sinks:
            raise ValidationError(
                f"schedule demand {route['id']!r}: missing sinks "
                f"{missing_sinks}"
            )
        completion_slot = max(
            arrival_by_node[sink] for sink in route["sinks"]
        )
        completion_by_round[transport_round] = max(
            completion_slot,
            completion_by_round.get(transport_round, completion_slot),
        )
        completion = {
            "demand": route["id"],
            "net": route["net"],
            "completion_slot": completion_slot,
        }
        if extended_schedule:
            completion.update(
                {
                    "transport_round": transport_round,
                    "source_ready_slot": source_ready_slot,
                }
            )
        if exact_contract is not None:
            sink_arrivals = {
                sink: arrival_by_node[sink] for sink in sorted(route["sinks"])
            }
            completion["sink_arrival_slots"] = sink_arrivals
            for sink, arrival in sink_arrivals.items():
                exact_arrivals[(route["net"], sink)] = arrival
        completions.append(completion)
    completions.sort(key=lambda item: item["demand"])
    if schedule.get("demand_completions") != completions:
        raise ValidationError(
            "schedule.demand_completions does not match recomputed values"
        )
    if academic_schedule:
        expected_realization = _round_barrier_realization(
            active_rounds,
            completion_by_round,
            ratio_plan["round_barrier_legalization"].get(
                "source_ready_slot"
            ),
        )
        if schedule.get("round_barrier_realization") != expected_realization:
            raise ValidationError(
                "schedule.round_barrier_realization does not match "
                "recomputed values"
            )

    expected_domains = _domain_schedule_records(
        platform, constraints, raw_entries
    )
    if schedule.get("domain_schedules") != expected_domains:
        raise ValidationError(
            "schedule.domain_schedules does not match recomputed occupancy"
        )
    expected_metrics = {
        "demands": len(routes["routes"]),
        "scheduled_bit_hops": len(expected),
        "frame_slots": frame_slots,
        "completion_slot": max(
            (item["completion_slot"] for item in completions),
            default=0,
        ),
        "max_domain_utilization": max(
            (domain["utilization"] for domain in expected_domains),
            default=0.0,
        ),
        "collisions": 0,
    }
    if extended_schedule:
        expected_metrics.update(
            {
                "transport_rounds": len(active_rounds),
                "round_barriers": max(0, len(active_rounds) - 1),
                "max_transport_round": max(active_rounds, default=0),
                "combinational_settle_slots": COMBINATIONAL_SETTLE_SLOTS,
            }
        )
    if exact_contract is not None:
        if (
            schedule.get("qualification")
            != "dependency-schedule-readiness-pass"
            or schedule.get("transport_semantics") != "sampled-virtual-wire"
            or schedule.get("semantic_contract_schema")
            != exact_contract.get("schema")
            or schedule.get("semantic_contract_sha256")
            != routes.get("semantic_contract_sha256")
        ):
            raise ValidationError(
                "cross-layer timing schedule binding is invalid"
            )
        captures, minimum_capture_slack = (
            _independently_reconstruct_exact_captures(
                exact_segments,
                exact_captures,
                exact_arrivals,
                sampled_timing_constraints,
            )
        )
        expected_certificate = {
            "schema": SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
            "provider": "independent-readiness-certificate-v1",
            "topological_cut_order": [
                route["net"] for route in ordered_routes
            ],
            "demand_readiness": exact_readiness_records,
            "capture_readiness": captures,
            "minimum_capture_slack_slots": minimum_capture_slack,
        }
        if schedule.get("schedule_dependency_certificate") != (
            expected_certificate
        ):
            raise ValidationError(
                "cross-layer timing readiness certificate does not match "
                "independent reconstruction"
            )
        expected_metrics.update(
            {
                "commit_slot": sampled_timing_constraints["commit_slot"],
                "dependency_edges": len(exact_contract["dependency_edges"]),
                "maximum_combinational_dependency_depth": exact_contract[
                    "metrics"
                ]["maximum_combinational_dependency_depth"],
                "capture_requirements": len(captures),
                "minimum_capture_slack_slots": minimum_capture_slack,
            }
        )
    if academic_schedule:
        expected_metrics.update(
            {
                "ratio_constrained_hops": len(expected),
                "max_tdm_ratio": max(
                    entry["tdm_ratio"] for entry in raw_entries
                ),
                "maximum_ratio_wait_slots": max(
                    entry["ratio_wait_slots"] for entry in raw_entries
                ),
            }
        )
    if schedule.get("metrics") != expected_metrics:
        raise ValidationError(
            "schedule.metrics does not match independently recomputed metrics"
        )
    if schedule.get("provider") == TDM_ACADEMIC_SCHEDULE_PROVIDER:
        optimization = schedule.get("slot_optimization")
        optimization_provider = (
            optimization.get("provider")
            if isinstance(optimization, dict)
            else None
        )
        if (
            not isinstance(optimization, dict)
            or optimization_provider
            not in {
                "timing-path-guided-local-search-v1",
                "timing-path-guided-lns-v2",
            }
        ):
            raise ValidationError(
                "native TDM slot optimization metadata is invalid"
            )
        configuration = optimization.get("configuration")
        maximum_iterations = (
            configuration.get("max_iterations")
            if isinstance(configuration, dict)
            else None
        )
        metrics = optimization.get("metrics")
        if (
            isinstance(maximum_iterations, bool)
            or not isinstance(maximum_iterations, int)
            or maximum_iterations < 0
            or not isinstance(metrics, dict)
        ):
            raise ValidationError(
                "native TDM slot optimization configuration is invalid"
            )
        expected_metric_keys = {
            "iterations",
            "accepted_moves",
            "evaluated_moves",
            "worst_normalized_slack",
            "completion_slot",
            "total_wait_slots",
            "baseline_worst_normalized_slack",
        }
        if optimization_provider == "timing-path-guided-lns-v2":
            expected_metric_keys.update(
                {"lns_neighborhoods", "lns_evaluated_orders"}
            )
        if set(metrics) != expected_metric_keys:
            raise ValidationError(
                "native TDM slot optimization metric coverage is invalid"
            )
        count_keys = [
            "iterations",
            "accepted_moves",
            "evaluated_moves",
        ]
        if optimization_provider == "timing-path-guided-lns-v2":
            count_keys.extend(
                ["lns_neighborhoods", "lns_evaluated_orders"]
            )
        for key in count_keys:
            value = metrics[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValidationError(
                    f"native TDM slot optimization {key} is invalid"
                )
        if (
            metrics["iterations"] > maximum_iterations
            or metrics["accepted_moves"] > metrics["iterations"]
            or metrics["completion_slot"]
            != expected_metrics["completion_slot"]
            or metrics["total_wait_slots"]
            != sum(entry["ratio_wait_slots"] for entry in raw_entries)
        ):
            raise ValidationError(
                "native TDM slot optimization metrics are inconsistent"
            )
        timing = reconstruct_tdm_schedule_timing(
            routes,
            platform,
            schedule,
            model=prepared_ratio_model,
        )
        for key in (
            "worst_normalized_slack",
            "baseline_worst_normalized_slack",
        ):
            if (
                isinstance(metrics[key], bool)
                or not isinstance(metrics[key], (int, float))
                or not math.isfinite(float(metrics[key]))
            ):
                raise ValidationError(
                    f"native TDM slot optimization {key} is invalid"
                )
        if (
            not math.isclose(
                metrics["worst_normalized_slack"],
                timing["worst_normalized_slack"],
                rel_tol=1.0e-10,
                abs_tol=1.0e-10,
            )
            or metrics["worst_normalized_slack"] + 1.0e-12
            < metrics["baseline_worst_normalized_slack"]
        ):
            raise ValidationError(
                "native TDM slot optimization timing metrics are inconsistent"
            )
    return {
        "status": "pass",
        **(
            {"qualification": "dependency-schedule-readiness-pass"}
            if exact_contract is not None
            else {}
        ),
        **expected_metrics,
        "routed_sinks": sum(
            len(route["sinks"]) for route in routes["routes"]
        ),
    }


def reconstruct_tdm_schedule_timing(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    *,
    model: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Reconstruct scheduled transport delay on every imported STA path.

    This deliberately uses the concrete slot assignment rather than a TDM
    ratio bound, so baseline and academic schedules are evaluated with the
    same timing model.
    """
    if model is None:
        records = reconstruct_tdm_schedule_timing_paths_from_routes(
            routes, platform, schedule
        )
    else:
        entries = {}
        for entry in schedule["entries"]:
            key = _hop_key(
                entry["demand"],
                entry["link"],
                entry["from"],
                entry["to"],
            )
            if key in entries:
                raise ValidationError(
                    "schedule timing reconstruction found duplicate hop "
                    f"{key}"
                )
            entries[key] = entry
        records = reconstruct_tdm_schedule_timing_paths(
            routes, platform, schedule, model=model, entries=entries
        )
    if not records:
        raise ValidationError(
            "schedule timing reconstruction has no timing paths"
        )
    records.sort(
        key=lambda record: (
            record["normalized_slack"],
            record["path"],
        )
    )
    worst = records[0]
    normalized = sorted(
        record["normalized_slack"] for record in records
    )
    return {
        "status": "pass",
        "timing_paths": len(records),
        "worst_path": worst["path"],
        "worst_delay_ns": worst["delay_ns"],
        "worst_slack_ns": worst["slack_ns"],
        "worst_normalized_slack": worst["normalized_slack"],
        "negative_slack_paths": sum(
            record["slack_ns"] < 0.0 for record in records
        ),
        "p01_normalized_slack": normalized[len(normalized) // 100],
        "median_normalized_slack": normalized[len(normalized) // 2],
    }


def reconstruct_tdm_schedule_timing_paths(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    *,
    model: Optional[Mapping[str, Any]] = None,
    entries: Optional[Mapping[HopKey, Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return independently reconstructed, traceable timing-path records."""
    from .tdm_ratio import _normalized_slack, _prepare_model

    if model is None:
        model = _prepare_model(routes, platform)
    if entries is None:
        entries = {}
        for entry in schedule["entries"]:
            key = _hop_key(
                entry["demand"],
                entry["link"],
                entry["from"],
                entry["to"],
            )
            if key in entries:
                raise ValidationError(
                    f"schedule timing reconstruction found duplicate hop {key}"
                )
            entries[key] = entry

    records: List[Dict[str, Any]] = []
    for timing_path in model["timing_paths"]:
        delay_ns = timing_path["fixed_delay_ns"]
        required_time_ns = timing_path.get(
            "required_time_ns", timing_path["clock_period_ns"]
        )
        transport_delay_ns = 0.0
        scheduled_hops = []
        for hop_index in timing_path["hops"]:
            hop = model["hops"][hop_index]
            key = _hop_key(
                hop["demand"],
                hop["link"],
                hop["from"],
                hop["to"],
            )
            if key not in entries:
                raise ValidationError(
                    "schedule timing reconstruction is missing routed hop "
                    f"{key}"
                )
            entry = entries[key]
            wait_slots = entry["slot"] - entry["ready_slot"]
            if wait_slots < 0:
                raise ValidationError(
                    "schedule timing reconstruction found a negative wait "
                    f"for hop {key}"
                )
            hop_delay_ns = (
                hop["base_delay_ns"]
                + hop["beta_ns"] * wait_slots
            )
            delay_ns += hop_delay_ns
            transport_delay_ns += hop_delay_ns
            scheduled_hops.append(
                {
                    "schedule_entry": entry["id"],
                    "demand": hop["demand"],
                    "link": hop["link"],
                    "from": hop["from"],
                    "to": hop["to"],
                    "tx_endpoint": f"__emuflow_tx_{entry['id']}",
                    "rx_endpoint": f"__emuflow_rx_{entry['id']}",
                    "base_link_delay_ns": hop["base_delay_ns"],
                    "tdm_wait_slots": wait_slots,
                    "tdm_slot_ns": hop["beta_ns"],
                    "link_tdm_delay_ns": hop_delay_ns,
                }
            )
        slack_ns = required_time_ns - delay_ns
        normalized_slack = _normalized_slack(
            timing_path["clock_period_ns"],
            slack_ns,
            model["normalization"],
        )
        records.append(
            {
                "path": timing_path["id"],
                "clock_domain": timing_path["clock_domain"],
                "clock_period_ns": timing_path["clock_period_ns"],
                "required_time_ns": required_time_ns,
                "preplacement_fixed_delay_ns": timing_path[
                    "fixed_delay_ns"
                ],
                "transport_delay_ns": transport_delay_ns,
                "delay_ns": delay_ns,
                "slack_ns": slack_ns,
                "normalized_slack": normalized_slack,
                "cut_nets": list(timing_path["cut_nets"]),
                "compressed_path_ids": list(
                    timing_path.get(
                        "compressed_path_ids", [timing_path["id"]]
                    )
                ),
                "cut_transitions": [
                    dict(item)
                    for item in timing_path.get("cut_transitions", [])
                ],
                "routed_hops": len(timing_path["hops"]),
                "scheduled_hops": scheduled_hops,
            }
        )
    return records


def reconstruct_tdm_schedule_timing_paths_from_routes(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Reconstruct concrete timing without the optimizer's dense model.

    The production ratio optimizer needs a hop-indexed representation, but
    baseline scheduling and concrete feedback only need the route trees used
    by each imported path.  Building the optimizer model on large public
    cases needlessly materializes and indexes every hop/path several times.
    This independent route-key reconstruction keeps the baseline checker
    linear in the serialized route and timing-path sizes.
    """
    from .tdm_ratio import _normalized_slack
    from .routing import normalize_route_constraints, route_link_delay_ns

    timing = routes.get("timing")
    if not isinstance(timing, dict) or not isinstance(
        timing.get("paths"), list
    ):
        raise ValidationError(
            "schedule timing reconstruction requires routes.timing.paths"
        )
    normalization = timing.get("normalization")
    if not isinstance(normalization, dict):
        raise ValidationError(
            "schedule timing reconstruction requires timing normalization"
        )
    constraints = normalize_route_constraints(
        routes.get("constraints"), platform
    )
    route_by_net = {}
    route_paths = {}
    longest_sink = {}
    for route in routes["routes"]:
        net = route["net"]
        if net in route_by_net:
            raise ValidationError(f"duplicate route net {net!r}")
        route_by_net[net] = route
        paths_by_node = {route["source"]: (0.0, [])}
        for _depth, edge in _route_hops(route):
            parent_delay, parent_path = paths_by_node[edge["from"]]
            base_delay = route_link_delay_ns(
                platform,
                edge["link"],
                edge["from"],
                edge["to"],
                constraints,
            )
            paths_by_node[edge["to"]] = (
                parent_delay + base_delay,
                [*parent_path, edge],
            )
        candidates = [
            (paths_by_node[sink][0], sink)
            for sink in route["sinks"]
        ]
        candidates.sort(key=lambda item: (-item[0], item[1]))
        longest_sink[net] = candidates[0][1]
        route_paths[net] = {
            sink: paths_by_node[sink][1] for sink in route["sinks"]
        }
    route_net_names = set(route_by_net)

    entries = {}
    for entry in schedule["entries"]:
        key = _hop_key(
            entry["demand"],
            entry["link"],
            entry["from"],
            entry["to"],
        )
        if key in entries:
            raise ValidationError(
                f"schedule timing reconstruction found duplicate hop {key}"
            )
        entries[key] = entry
    links = _link_by_id(platform)
    records = []
    for index, timing_path in enumerate(timing["paths"]):
        cut_nets = timing_path.get("cut_nets")
        if (
            not isinstance(cut_nets, list)
            or not cut_nets
            or not all(isinstance(net, str) for net in cut_nets)
        ):
            raise ValidationError(
                f"routes.timing.paths[{index}].cut_nets: invalid"
            )
        unknown = sorted(set(cut_nets) - route_net_names)
        if unknown:
            raise ValidationError(
                f"routes.timing.paths[{index}]: unknown cut nets {unknown}"
            )
        transitions = timing_path.get("cut_transitions")
        explicit_transitions = transitions is not None
        if transitions is None:
            transitions = [
                {
                    "net": net,
                    "from": route_by_net[net]["source"],
                    "to": longest_sink[net],
                }
                for net in cut_nets
            ]
        elif (
            not isinstance(transitions, list)
            or len(transitions) != len(cut_nets)
        ):
            raise ValidationError(
                f"routes.timing.paths[{index}].cut_transitions: invalid"
            )
        if any(
            transitions[position - 1].get("to")
            != transitions[position].get("from")
            for position in range(1, len(transitions))
            if isinstance(transitions[position - 1], dict)
            and isinstance(transitions[position], dict)
        ) and explicit_transitions:
            raise ValidationError(
                f"routes.timing.paths[{index}].cut_transitions: "
                "discontinuous member partition chain"
            )
        period = timing_path.get("clock_period_ns")
        required = timing_path.get("required_time_ns", period)
        fixed = timing_path.get("fixed_delay_ns")
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
        delay_ns = float(fixed)
        transport_delay_ns = 0.0
        scheduled_hops = []
        seen_hops = set()
        for net, transition in zip(cut_nets, transitions):
            route = route_by_net.get(net)
            if (
                route is None
                or not isinstance(transition, dict)
                or transition.get("net") != net
                or transition.get("from") != route["source"]
                or transition.get("to") not in route["sinks"]
            ):
                raise ValidationError(
                    f"routes.timing.paths[{index}].cut_transitions: invalid"
                )
            for edge in route_paths[net][transition["to"]]:
                key = _hop_key(
                    route["id"],
                    edge["link"],
                    edge["from"],
                    edge["to"],
                )
                if key in seen_hops:
                    raise ValidationError(
                        f"routes.timing.paths[{index}]: duplicate routed hop"
                    )
                seen_hops.add(key)
                entry = entries.get(key)
                if entry is None:
                    raise ValidationError(
                        "schedule timing reconstruction is missing routed "
                        f"hop {key}"
                    )
                wait_slots = entry["slot"] - entry["ready_slot"]
                if wait_slots < 0:
                    raise ValidationError(
                        "schedule timing reconstruction found a negative "
                        f"wait for hop {key}"
                    )
                link = links[edge["link"]]
                base_delay = route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                beta_ns = 1000.0 / link.fabric_clock_mhz
                hop_delay = base_delay + beta_ns * wait_slots
                delay_ns += hop_delay
                transport_delay_ns += hop_delay
                scheduled_hops.append(
                    {
                        "schedule_entry": entry["id"],
                        "demand": route["id"],
                        "link": edge["link"],
                        "from": edge["from"],
                        "to": edge["to"],
                        "tx_endpoint": f"__emuflow_tx_{entry['id']}",
                        "rx_endpoint": f"__emuflow_rx_{entry['id']}",
                        "base_link_delay_ns": base_delay,
                        "tdm_wait_slots": wait_slots,
                        "tdm_slot_ns": beta_ns,
                        "link_tdm_delay_ns": hop_delay,
                    }
                )
        period = float(period)
        required = float(required)
        slack = required - delay_ns
        records.append(
            {
                "path": timing_path["path"],
                "clock_domain": timing_path["clock_domain"],
                "clock_period_ns": period,
                "required_time_ns": required,
                "preplacement_fixed_delay_ns": float(
                    fixed
                ),
                "transport_delay_ns": transport_delay_ns,
                "delay_ns": delay_ns,
                "slack_ns": slack,
                "normalized_slack": _normalized_slack(
                    period, slack, normalization
                ),
                "cut_nets": list(cut_nets),
                "compressed_path_ids": list(
                    timing_path.get(
                        "compressed_path_ids", [timing_path["path"]]
                    )
                ),
                "cut_transitions": [dict(item) for item in transitions],
                "routed_hops": len(scheduled_hops),
                "scheduled_hops": scheduled_hops,
            }
        )
    return records


def simulate_tdm_schedule(
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    frames: int = 16,
) -> Dict[str, Any]:
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise ValidationError("TDM simulation frames: expected a positive integer")
    entries_by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule["entries"]:
        entries_by_slot[entry["slot"]].append(entry)
    demands = {
        route["id"]: route for route in routes["routes"]
    }
    trace = hashlib.sha256()
    delivered = 0

    for frame in range(frames):
        node_values: Dict[Tuple[str, str], int] = {}
        source_values: Dict[str, int] = {}
        for demand_id, route in demands.items():
            digest = hashlib.sha256(
                f"{frame}:{demand_id}:{route['net']}".encode("utf-8")
            ).digest()
            value = digest[0] & 1
            source_values[demand_id] = value
            node_values[(demand_id, route["source"])] = value

        arrivals: Dict[int, List[Tuple[Mapping[str, Any], int]]] = defaultdict(
            list
        )
        for slot in range(schedule["metrics"]["frame_slots"]):
            for entry in sorted(
                entries_by_slot.get(slot, []),
                key=lambda item: (item["hop"], item["id"]),
            ):
                source_key = (entry["demand"], entry["from"])
                if source_key not in node_values:
                    raise ValidationError(
                        f"TDM simulation: data unavailable for {entry['id']!r}"
                    )
                arrivals[entry["arrival_slot"]].append(
                    (entry, node_values[source_key])
                )
            for entry, value in sorted(
                arrivals.get(slot, []),
                key=lambda item: (item[0]["hop"], item[0]["id"]),
            ):
                node_values[(entry["demand"], entry["to"])] = value

        for demand_id, route in sorted(demands.items()):
            expected = source_values[demand_id]
            for sink in route["sinks"]:
                actual = node_values.get((demand_id, sink))
                if actual != expected:
                    raise ValidationError(
                        f"TDM simulation frame {frame}: demand {demand_id!r} "
                        f"sink {sink!r} expected {expected}, got {actual!r}"
                    )
                trace.update(
                    f"{frame}:{demand_id}:{sink}:{actual}\n".encode("utf-8")
                )
                delivered += 1
    return {
        "status": "pass",
        "frames": frames,
        "demands": len(demands),
        "delivered_sink_values": delivered,
        "trace_sha256": trace.hexdigest(),
    }


def schedule_to_tsv(schedule: Mapping[str, Any]) -> str:
    lines = [
        "entry\tdemand\tnet\tlink\tfrom\tto\tslot\tlane\tready\tarrival"
    ]
    for entry in schedule["entries"]:
        lines.append(
            "\t".join(
                str(entry[field])
                for field in (
                    "id",
                    "demand",
                    "net",
                    "link",
                    "from",
                    "to",
                    "slot",
                    "lane",
                    "ready_slot",
                    "arrival_slot",
                )
            )
        )
    return "\n".join(lines) + "\n"


def build_transport_manifest(
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    demand_index = {
        route["id"]: index
        for index, route in enumerate(
            sorted(routes["routes"], key=lambda item: item["id"])
        )
    }
    endpoints = []
    for fpga in platform.fpgas:
        tx_entries = [
            entry["id"]
            for entry in schedule["entries"]
            if entry["from"] == fpga.id
        ]
        rx_entries = [
            entry["id"]
            for entry in schedule["entries"]
            if entry["to"] == fpga.id
        ]
        endpoints.append(
            {
                "fpga": fpga.id,
                "tx_entries": sorted(tx_entries),
                "rx_entries": sorted(rx_entries),
            }
        )
    return {
        "schema": "emuflow.transport-manifest/v1",
        "design": schedule["design"],
        "platform": platform.name,
        "frame_slots": schedule["metrics"]["frame_slots"],
        "demand_index": dict(sorted(demand_index.items())),
        "endpoints": endpoints,
        "rtl_primitives": [
            "rtl/transport/emuflow_tdm_link.sv",
            "rtl/transport/emuflow_frame_barrier.sv",
        ],
    }


def schedule_to_systemverilog_testbench(
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
    frames: int,
) -> str:
    ordered_routes = sorted(routes["routes"], key=lambda item: item["id"])
    demand_index = {
        route["id"]: index for index, route in enumerate(ordered_routes)
    }
    fpga_index = {
        fpga.id: index for index, fpga in enumerate(platform.fpgas)
    }
    links = _link_by_id(platform)
    channel_keys = sorted(
        {entry["capacity_key"] for entry in schedule["entries"]}
    )
    channel_index = {
        key: index for index, key in enumerate(channel_keys)
    }
    channel_link = {}
    for entry in schedule["entries"]:
        channel_link[entry["capacity_key"]] = links[entry["link"]]

    lines = [
        "`timescale 1ns/1ps",
        "module transport_schedule_tb;",
        f"  localparam integer DEMANDS = {len(ordered_routes)};",
        f"  localparam integer FPGAS = {len(platform.fpgas)};",
        f"  localparam integer FRAME_SLOTS = {schedule['metrics']['frame_slots']};",
        f"  localparam integer FRAMES = {frames};",
        "  reg clk = 1'b0;",
        "  reg reset = 1'b1;",
        "  reg [DEMANDS-1:0] source_bits;",
        "  reg [DEMANDS-1:0] node_value [0:FPGAS-1];",
        "  integer frame_index;",
        "  integer slot_index;",
        "  integer demand_loop;",
        "  integer fpga_loop;",
        "  always #5 clk = ~clk;",
    ]
    for key in channel_keys:
        index = channel_index[key]
        link = channel_link[key]
        lanes = link.transport_bits_per_cycle_per_direction
        lines.extend(
            [
                f"  reg [{lanes - 1}:0] tx_data_{index};",
                f"  reg [{lanes - 1}:0] tx_valid_{index};",
                f"  wire [{lanes - 1}:0] rx_data_{index};",
                f"  wire [{lanes - 1}:0] rx_valid_{index};",
                "  emuflow_tdm_link #(",
                f"    .LANES({lanes}),",
                f"    .LATENCY({link.latency_cycles})",
                f"  ) channel_{index} (",
                "    .clk(clk), .reset(reset),",
                f"    .tx_data(tx_data_{index}),",
                f"    .tx_valid(tx_valid_{index}),",
                f"    .rx_data(rx_data_{index}),",
                f"    .rx_valid(rx_valid_{index})",
                "  );",
            ]
        )

    lines.extend(
        [
            "  task drive_slot(input integer current_slot);",
            "  begin",
        ]
    )
    for key in channel_keys:
        index = channel_index[key]
        lines.extend(
            [
                f"    tx_data_{index} = '0;",
                f"    tx_valid_{index} = '0;",
            ]
        )
    lines.append("    case (current_slot)")
    entries_by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule["entries"]:
        entries_by_slot[entry["slot"]].append(entry)
    for slot in sorted(entries_by_slot):
        lines.append(f"      {slot}: begin")
        for entry in sorted(entries_by_slot[slot], key=lambda item: item["id"]):
            channel = channel_index[entry["capacity_key"]]
            demand = demand_index[entry["demand"]]
            source = fpga_index[entry["from"]]
            lines.append(
                f"        tx_data_{channel}[{entry['lane']}] = "
                f"node_value[{source}][{demand}];"
            )
            lines.append(
                f"        tx_valid_{channel}[{entry['lane']}] = 1'b1;"
            )
        lines.append("      end")
    lines.extend(["      default: begin end", "    endcase", "  end", "  endtask"])

    arrivals_by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule["entries"]:
        arrivals_by_slot[entry["arrival_slot"]].append(entry)
    lines.extend(
        [
            "  task capture_slot(input integer current_slot);",
            "  begin",
            "    case (current_slot)",
        ]
    )
    for slot in sorted(arrivals_by_slot):
        lines.append(f"      {slot}: begin")
        for entry in sorted(
            arrivals_by_slot[slot],
            key=lambda item: (item["hop"], item["id"]),
        ):
            channel = channel_index[entry["capacity_key"]]
            demand = demand_index[entry["demand"]]
            sink = fpga_index[entry["to"]]
            lines.extend(
                [
                    f"        if (!rx_valid_{channel}[{entry['lane']}]) "
                    f"$fatal(1, \"missing valid for {entry['id']}\");",
                    f"        node_value[{sink}][{demand}] = "
                    f"rx_data_{channel}[{entry['lane']}];",
                ]
            )
        lines.append("      end")
    lines.extend(["      default: begin end", "    endcase", "  end", "  endtask"])

    lines.extend(
        [
            "  initial begin",
            "    source_bits = '0;",
        ]
    )
    for key in channel_keys:
        index = channel_index[key]
        lines.extend(
            [f"    tx_data_{index} = '0;", f"    tx_valid_{index} = '0;"]
        )
    lines.extend(
        [
            "    repeat (3) @(posedge clk);",
            "    reset = 1'b0;",
            "    for (frame_index = 0; frame_index < FRAMES; "
            "frame_index = frame_index + 1) begin",
            "      for (demand_loop = 0; demand_loop < DEMANDS; "
            "demand_loop = demand_loop + 1)",
            "        source_bits[demand_loop] = "
            "(frame_index + demand_loop) & 1;",
            "      for (fpga_loop = 0; fpga_loop < FPGAS; "
            "fpga_loop = fpga_loop + 1)",
            "        node_value[fpga_loop] = '0;",
        ]
    )
    for route in ordered_routes:
        demand = demand_index[route["id"]]
        source = fpga_index[route["source"]]
        lines.append(
            f"      node_value[{source}][{demand}] = source_bits[{demand}];"
        )
    lines.extend(
        [
            "      for (slot_index = 0; slot_index < FRAME_SLOTS; "
            "slot_index = slot_index + 1) begin",
            "        drive_slot(slot_index);",
            "        @(posedge clk); #1;",
            "        capture_slot(slot_index);",
            "      end",
        ]
    )
    for route in ordered_routes:
        demand = demand_index[route["id"]]
        for sink_id in route["sinks"]:
            sink = fpga_index[sink_id]
            lines.append(
                f"      if (node_value[{sink}][{demand}] !== "
                f"source_bits[{demand}]) "
                f"$fatal(1, \"delivery mismatch {route['id']}->{sink_id}\");"
            )
    lines.extend(
        [
            "    end",
            f"    $display(\"EMUFLOW_TDM_RTL_SIM status=pass frames=%0d "
            f"demands={len(ordered_routes)} entries={len(schedule['entries'])}\", "
            "FRAMES);",
            "    $finish;",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)
