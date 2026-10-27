"""Bounded earliest-slot correctness oracle; no production entry point."""
from __future__ import annotations
from collections import defaultdict, deque
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple
from emuflow.scheduling import tdm as _tdm
REFERENCE_TDM_PROVIDER = "deterministic-round-barrier-earliest-slot-v2"
from emuflow.common.errors import TDMScheduleInfeasibleError, ValidationError
from emuflow.platform.platform import Platform
from emuflow.routing.routing import (
    RUNTIME_BARRIER_SLOTS, SYSTEM_ROUTES_SCHEMA, build_directed_graph,
    normalize_route_constraints, route_link_delay_coefficients_ns,
)

def build_tdm_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, Any]:
    if len(routes.get("routes", [])) > 4096:
        raise ValueError("test scheduling oracle is limited to 4096 routes")
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(
            f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}, "
            f"got {routes.get('schema')!r}"
        )
    exact_contract = _tdm._static_exact_contract_from_routes(routes)
    constraints = normalize_route_constraints(
        routes.get("constraints"),
        platform,
    )
    frame_slots = constraints["frame_slots"]
    sampled_timing_constraints = (
        _tdm.sampled_virtual_wire_timing_constraints(frame_slots)
        if exact_contract is not None
        else None
    )
    _, arcs, capacity_records = build_directed_graph(platform, constraints)
    links = _tdm._link_by_id(platform)
    bank_allocations = _tdm._bank_direction_allocations(routes, platform, constraints)
    planned_hops = None
    planned_round_one_ready = None
    slot_fill: Dict[str, List[int]] = {
        key: [0] * frame_slots for key in capacity_records
    }
    next_available: Dict[str, List[int]] = {
        key: list(range(frame_slots + 1)) for key in capacity_records
    }
    planned_occupancy: Dict[str, Set[Tuple[int, int]]] = defaultdict(set)
    baseline_hops = {}

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
    indexed_exact = bool(
        exact_contract is not None and exact_contract.get("indexed") is True
    )
    baseline_hops = _tdm._baseline_ratio_hops(routes,platform,arcs,bank_allocations,constraints)
    if exact_contract is None:
        ordered_routes = sorted(raw_routes, key=lambda row: (row.get('transport_round', 0), -max((hop for hop, _ in _tdm._route_hops(row)), default=0), row['net'], row['id']))
        active_rounds = sorted({row.get('transport_round', 0) for row in raw_routes})
    else:
        if indexed_exact:
            exact_nodes = _tdm._indexed_exact_nodes(raw_routes)
        else:
            exact_nodes, exact_segments, exact_captures = _tdm._exact_contract_indexes(exact_contract)
        ordered_routes = _tdm._exact_dependency_order(exact_nodes, raw_routes, planned_hops)
        # Sampled virtual wires use path-local readiness, not transport rounds.
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
                prior_round_completion + _tdm.COMBINATIONAL_SETTLE_SLOTS
                if prior_round_completion >= 0
                else 0
            )
        else:
            node = exact_nodes[route["net"]]
            if indexed_exact:
                source_ready_slot, _source_evidence = (
                    _tdm._indexed_source_readiness(
                        route,
                        exact_arrivals,
                        sampled_timing_constraints,
                    )
                )
            else:
                source_ready_slot, source_evidence = _tdm._independently_reconstruct_exact_source_readiness(
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
        for depth, edge in _tdm._route_hops(route):
            arc_key = (edge["link"], edge["from"], edge["to"])
            if arc_key not in arcs:
                raise ValidationError(
                    f"TDM route {route['id']!r} uses illegal edge {arc_key}"
                )
            arc = arcs[arc_key]
            link = links[edge["link"]]
            bank_budget = bank_allocations.get(arc_key)
            lane_offset = bank_budget["lane_offset"] if bank_budget else 0
            lane_budget = bank_budget["lanes"] if bank_budget else link.transport_bits_per_cycle_per_direction
            ready_slot = (
                source_ready_slot
                if edge["from"] == route["source"]
                else arrival_by_node[edge["from"]] + 1
            )
            plan_hop = (
                None
            )
            baseline_hop = baseline_hops.get(_tdm._hop_key(
                route["id"], edge["link"], edge["from"], edge["to"]))
            assigned_hop = plan_hop or baseline_hop
            ratio = assigned_hop["discrete_ratio"] if assigned_hop else 1
            base, _slope = route_link_delay_coefficients_ns(
                platform, edge["link"], edge["from"], edge["to"], constraints)
            latency_cycles = _tdm.tdm_hop_latency_cycles(link, ratio, base_delay_ns=base)
            latest_exclusive = frame_slots - RUNTIME_BARRIER_SLOTS - latency_cycles
            if assigned_hop is None:
                slot = (
                    frame_slots
                    if ready_slot >= frame_slots
                    else first_available_slot(
                        arc["capacity_key"],
                        ready_slot,
                    )
                )
                lane = (
                    lane_offset + slot_fill[arc["capacity_key"]][slot]
                    if slot < frame_slots
                    else 0
                )
            else:
                lane = assigned_hop["lane"]
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
                    f"latency={latency_cycles}"
                )
            if assigned_hop is not None and slot >= ready_slot + ratio:
                raise TDMScheduleInfeasibleError(
                    f"TDM ratio schedule is infeasible for demand "
                    f"{route['id']!r} edge {arc_key}: ready={ready_slot}, "
                    f"ratio={ratio}, lane={lane}"
                )
            if assigned_hop is None:
                slot_fill[arc["capacity_key"]][slot] += 1
                if (
                    slot_fill[arc["capacity_key"]][slot]
                    == lane_budget
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
            arrival_slot = slot + latency_cycles
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
            if baseline_hop is not None:
                entry["tdm_ratio"] = ratio
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
            "source_ready_slot": source_ready_slot,
            "completion_slot": completion_slot,
        }
        if exact_contract is None:
            completion["transport_round"] = transport_round
        if exact_contract is not None:
            sink_arrivals = {
                sink: arrival_by_node[sink] for sink in sorted(route["sinks"])
            }
            completion["sink_arrival_slots"] = sink_arrivals
            for sink, arrival in sink_arrivals.items():
                exact_arrivals[(route["net"], sink)] = arrival
        demand_completions.append(completion)

    entries.sort(key=lambda entry: (entry["slot"], entry["capacity_key"], entry["lane"], entry["demand"]))
    domain_schedules = _tdm._domain_schedule_records(
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
        "collisions": 0,
    }
    if exact_contract is None:
        metrics.update(
            {
                "transport_rounds": len(active_rounds),
                "round_barriers": max(0, len(active_rounds) - 1),
                "max_transport_round": max(active_rounds, default=0),
                "combinational_settle_slots": _tdm.COMBINATIONAL_SETTLE_SLOTS,
            }
        )
    else:
        metrics["transport_ordering_policy"] = (
            "path-local-dependency-dag-v1"
        )
    exact_certificate = None
    if exact_contract is not None:
        if indexed_exact:
            exact_certificate = _tdm._indexed_certificate(
                ordered_routes,
                exact_arrivals,
                sampled_timing_constraints,
            )
            metrics.update(
                {
                    "commit_slot": sampled_timing_constraints["commit_slot"],
                    "dependency_edges": exact_certificate[
                        "dependency_edges"
                    ],
                    "maximum_combinational_dependency_depth": max(
                        (
                            route["combinational_dependency_depth"]
                            for route in ordered_routes
                        ),
                        default=0,
                    ),
                    "conservative_sink_readiness_checks": exact_certificate[
                        "sink_readiness_checks"
                    ],
                    "minimum_capture_slack_slots": exact_certificate[
                        "minimum_sink_slack_slots"
                    ],
                }
            )
        else:
            capture_records, minimum_capture_slack = _tdm._independently_reconstruct_exact_captures(
                exact_segments,
                exact_captures,
                exact_arrivals,
                sampled_timing_constraints,
            )
            exact_certificate = {
                "schema": _tdm.SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
                "provider": "independent-readiness-certificate-v1",
                "topological_cut_order": [
                    route["net"] for route in ordered_routes
                ],
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
        "schema": _tdm.TDM_SCHEDULE_SCHEMA,
        "design": routes.get("design"),
        "platform": platform.name,
        "provider": (
            _tdm.TDM_EXACT_DEPENDENCY_PROVIDER
                if exact_contract is not None
                else REFERENCE_TDM_PROVIDER
        ),
        "transport_semantics": (
            "sampled-virtual-wire"
            if exact_contract is not None
            else "registered-boundary"
        ),
        "route_constraints": constraints,
        **({"bank_allocations": _tdm._bank_allocation_records(bank_allocations)} if bank_allocations else {}),
        "routes": (
            [
                {
                    "id": route["id"],
                    "net": route["net"],
                    "source": route["source"],
                    "sinks": list(route["sinks"]),
                    "tree_edges": list(route["tree_edges"]),
                    **(
                        {"transport_round": route.get("transport_round", 0)}
                        if exact_contract is None
                        else {}
                    ),
                }
                for route in sorted(raw_routes, key=lambda item: item["id"])
            ]
        ),
        "entries": entries,
        "demand_completions": (
            demand_completions
            if getattr(demand_completions, "demand_ordered", False)
            else sorted(demand_completions, key=lambda item: item["demand"])
        ),
        "domain_schedules": domain_schedules,
        "metrics": metrics,
    }
    if exact_contract is not None:
        result.update(
            {
                "qualification": "dependency-schedule-readiness-pass",
                "timing_constraints": sampled_timing_constraints,
                "semantic_contract_schema": exact_contract["schema"],
                "semantic_contract_sha256": routes[
                    "semantic_contract_sha256"
                ],
                "schedule_dependency_certificate": exact_certificate,
            }
        )
    return result
