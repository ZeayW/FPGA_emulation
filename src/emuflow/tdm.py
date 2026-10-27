from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import struct
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import TDMScheduleInfeasibleError, ValidationError
from .platform import Platform
from .routing import (
    RUNTIME_BARRIER_SLOTS,
    SYSTEM_ROUTES_SCHEMA,
    build_directed_graph,
    normalize_route_constraints,
    route_hop_delay_ns,
    route_link_delay_coefficients_ns,
)

TDM_SCHEDULE_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_EXACT_DEPENDENCY_PROVIDER = "deterministic-dependency-earliest-slot-v1"
SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA = (
    "emuflow.sampled-virtual-wire-schedule-certificate/v1"
)
INDEXED_SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA = (
    "emuflow.sampled-virtual-wire-schedule-certificate/v2"
)
SAMPLED_VIRTUAL_WIRE_CONSTRAINTS_SCHEMA = (
    "emuflow.sampled-virtual-wire-timing-constraints/v1"
)
COMBINATIONAL_SETTLE_SLOTS = 1
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

def is_fixed_slot_transport_schedule(schedule: Mapping[str, Any]) -> bool:
    """Return whether concrete TX events exist for physical readiness checks.

    A declared ratio-delay model owns its target transport delay separately.
    The events still constrain causal TX/RX and commit readiness; legacy
    platforms also use the projected fixed-slot event as the delay objective.
    """

    return schedule.get("transport_semantics") in {
        "registered-boundary",
        "sampled-virtual-wire",
    }

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

    if contract.get("indexed") is True:
        if (
            contract.get("schema") != routes.get("semantic_contract_schema")
            or contract.get("mode") != "static-exact-combinational"
            or contract.get("qualification")
            != "structural-partition-legality"
        ):
            raise ValidationError(
                "routes indexed exact semantic contract binding is invalid"
            )
        raw_routes = routes.get("routes")
        if not isinstance(raw_routes, list):
            raise ValidationError("indexed exact routes must be an array")
        indexed_validator = getattr(
            raw_routes, "validate_indexed_exact_metadata", None
        )
        if callable(indexed_validator):
            indexed_validator()
        else:
            for route in raw_routes:
                if (
                    not isinstance(route, dict)
                    or not isinstance(route.get("net"), str)
                    or route.get("cut_class")
                    not in {
                        "combinational",
                        "register_input",
                        "register_output",
                    }
                    or isinstance(route.get("dependency_level"), bool)
                    or not isinstance(route.get("dependency_level"), int)
                    or route["dependency_level"] < 0
                    or isinstance(
                        route.get("combinational_dependency_depth"), bool
                    )
                    or not isinstance(
                        route.get("combinational_dependency_depth"), int
                    )
                    or route["combinational_dependency_depth"] < 0
                    or not isinstance(route.get("predecessor_cut_nets"), list)
                    or not all(
                        isinstance(item, str) and item
                        for item in route["predecessor_cut_nets"]
                    )
                ):
                    raise ValidationError(
                        "indexed exact route dependency metadata is invalid"
                    )
        return contract
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

def _indexed_exact_nodes(
    routes: Sequence[Mapping[str, Any]],
) -> Dict[str, Mapping[str, Any]]:
    nodes = {route.get("net"): route for route in routes}
    if (
        len(nodes) != len(routes)
        or None in nodes
        or not all(isinstance(net, str) and net for net in nodes)
    ):
        raise ValidationError("indexed exact route identities are invalid")
    return nodes  # type: ignore[return-value]

def _indexed_source_readiness(
    route: Mapping[str, Any],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Tuple[int, List[Tuple[str, int]]]:
    """Conservatively reconstruct readiness from the routed cut DAG.

    Phase 3's indexed relation proves the predecessor edges.  A root launch
    receives one settle slot; each dependent launch receives one settle slot
    after every predecessor has arrived at the source FPGA.  Treating constant
    roots the same way is conservative and avoids hydrating their individual
    logic-segment records.
    """

    settle = timing_constraints.get("settle_slots")
    predecessors = route.get("predecessor_cut_nets")
    source = route.get("source")
    if (
        isinstance(settle, bool)
        or not isinstance(settle, int)
        or settle < 0
        or not isinstance(predecessors, list)
        or len(predecessors) != len(set(predecessors))
        or not all(isinstance(item, str) and item for item in predecessors)
        or not isinstance(source, str)
    ):
        raise ValidationError("indexed exact readiness metadata is invalid")
    evidence: List[Tuple[str, int]] = []
    for predecessor in sorted(predecessors):
        key = (predecessor, source)
        if key not in arrivals:
            raise ValidationError(
                f"indexed exact predecessor {predecessor!r} has not arrived "
                f"at {source!r}"
            )
        evidence.append((predecessor, arrivals[key]))
    return (
        max((arrival + settle for _net, arrival in evidence), default=settle),
        evidence,
    )

def _digest_record(digest: Any, value: Any) -> None:
    digest.update(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    digest.update(b"\n")

def _indexed_certificate(
    ordered_routes: Sequence[Mapping[str, Any]],
    arrivals: Mapping[Tuple[str, str], int],
    timing_constraints: Mapping[str, Any],
) -> Dict[str, Any]:
    order_digest = hashlib.sha256()
    readiness_digest = hashlib.sha256()
    sink_digest = hashlib.sha256()
    dependency_edges = 0
    readiness_count = 0
    sink_count = 0
    minimum_sink_slack: Optional[int] = None
    commit_slot = timing_constraints.get("commit_slot")
    settle = timing_constraints.get("settle_slots")
    if (
        isinstance(commit_slot, bool)
        or not isinstance(commit_slot, int)
        or isinstance(settle, bool)
        or not isinstance(settle, int)
    ):
        raise ValidationError("indexed exact timing constraints are invalid")
    for route in ordered_routes:
        _digest_record(order_digest, route["net"])
        ready, evidence = _indexed_source_readiness(
            route, arrivals, timing_constraints
        )
        dependency_edges += len(evidence)
        readiness_count += 1
        _digest_record(
            readiness_digest,
            {
                "net": route["net"],
                "source": route["source"],
                "source_ready_slot": ready,
                "predecessor_arrivals": evidence,
            },
        )
        for sink in sorted(route["sinks"]):
            key = (route["net"], sink)
            if key not in arrivals:
                raise ValidationError(
                    f"indexed exact sink {route['net']!r}@{sink!r} lacks "
                    "an arrival"
                )
            arrival = arrivals[key]
            ready_slot = arrival + settle
            slack = commit_slot - ready_slot
            if slack < 0:
                raise ValidationError(
                    f"indexed exact sink {route['net']!r}@{sink!r} misses "
                    "the commit deadline"
                )
            sink_count += 1
            minimum_sink_slack = (
                slack
                if minimum_sink_slack is None
                else min(minimum_sink_slack, slack)
            )
            _digest_record(
                sink_digest,
                {
                    "net": route["net"],
                    "fpga": sink,
                    "arrival_slot": arrival,
                    "budget_slots": settle,
                    "ready_slot": ready_slot,
                    "commit_slot": commit_slot,
                    "slack_slots": slack,
                },
            )
    return {
        "schema": INDEXED_SAMPLED_VIRTUAL_WIRE_SCHEDULE_CERTIFICATE_SCHEMA,
        "provider": "indexed-conservative-readiness-certificate-v2",
        "launch_readiness_policy": "one-settle-slot-conservative-v1",
        "capture_coverage": "all-routed-sinks-conservative-v1",
        "topological_cut_count": len(ordered_routes),
        "topological_cut_order_sha256": order_digest.hexdigest(),
        "dependency_edges": dependency_edges,
        "readiness_records": readiness_count,
        "readiness_sha256": readiness_digest.hexdigest(),
        "sink_readiness_checks": sink_count,
        "sink_readiness_sha256": sink_digest.hexdigest(),
        "minimum_sink_slack_slots": minimum_sink_slack,
    }

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

def _exact_dependency_order(
    exact_nodes: Mapping[str, Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    planned_hops: Optional[Mapping[HopKey, Mapping[str, Any]]] = None,
) -> List[Mapping[str, Any]]:
    """Topologically order exact cuts without imposing level barriers.

    Sorting all level-0 cuts before any level-1 cut is functionally legal but
    catastrophically pessimistic: one ready predecessor can be followed by
    thousands of unrelated transfers before its dependent cut is considered.
    This ready-queue keeps only real DAG precedence and prioritizes chains with
    the greatest remaining dependency depth.
    """

    route_by_net = {route.get("net"): route for route in routes}
    if len(route_by_net) != len(routes) or set(route_by_net) != set(
        exact_nodes
    ):
        raise ValidationError(
            "cross-layer timing cut coverage does not match routes"
        )
    successors: Dict[str, List[str]] = {
        net: [] for net in exact_nodes
    }
    known_nets = set(exact_nodes)
    indegree: Dict[str, int] = {}
    for net, node in exact_nodes.items():
        predecessors = list(node.get("predecessor_cut_nets", []))
        unknown = sorted(set(predecessors) - known_nets)
        if unknown:
            raise ValidationError(
                f"cross-layer timing cut {net!r} has unknown predecessors "
                f"{unknown}"
            )
        indegree[net] = len(predecessors)
        for predecessor in predecessors:
            successors[predecessor].append(net)
    for values in successors.values():
        values.sort()

    remaining_depth = {net: 0 for net in exact_nodes}
    for node in sorted(
        exact_nodes.values(),
        key=lambda item: (item["dependency_level"], item["net"]),
        reverse=True,
    ):
        net = node["net"]
        for predecessor in node.get("predecessor_cut_nets", []):
            remaining_depth[predecessor] = max(
                remaining_depth[predecessor],
                remaining_depth[net] + 1,
            )

    def priority(net: str) -> Tuple[float, float, int, str]:
        route = route_by_net[net]
        ratios = (
            [
                float(
                    planned_hops[
                        _hop_key(
                            route["id"],
                            edge["link"],
                            edge["from"],
                            edge["to"],
                        )
                    ]["continuous_ratio"]
                )
                for _depth, edge in _route_hops(route)
            ]
            if planned_hops is not None
            else []
        )
        return (
            -float(remaining_depth[net]),
            min(ratios, default=0.0),
            -max((depth for depth, _edge in _route_hops(route)), default=0),
            net,
        )

    ready: List[Tuple[Tuple[float, float, int, str], str]] = []
    for net in sorted(net for net, count in indegree.items() if count == 0):
        heapq.heappush(ready, (priority(net), net))
    ordered = []
    while ready:
        _key, net = heapq.heappop(ready)
        ordered.append(route_by_net[net])
        for successor in successors[net]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heapq.heappush(ready, (priority(successor), successor))
    if len(ordered) != len(routes):
        raise ValidationError("cross-layer timing dependency graph is cyclic")
    return ordered



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



def _bank_direction_allocations(
    routes: Mapping[str, Any], platform: Platform,
    constraints: Mapping[str, Any],
) -> Dict[Tuple[str, str, str], Dict[str, int]]:
    """Choose integer bank budgets once for the concrete routed workload.

    The load-based split is a deterministic minimax-ratio heuristic. Native
    ratio optimization and scheduling then obey these fixed direction budgets;
    neither is allowed to borrow individual wires from the opposite direction.
    Lane identifiers remain physical, pair-global wire identifiers.
    """
    from .bank_allocation import allocate_shared_banks

    counts: Dict[Tuple[str, str, str], int] = defaultdict(int)
    arc_counts = getattr(routes["routes"], "iter_arc_counts", None)
    if callable(arc_counts):
        for link, source, sink, count in arc_counts():
            counts[(link, source, sink)] = count
    else:
        for route in routes["routes"]:
            for edge in route["tree_edges"]:
                counts[(edge["link"], edge["from"], edge["to"])] += 1
    result = {}
    for link in platform.links:
        if link.bank_count is None:
            continue
        left, right = sorted(link.endpoints)
        forward = (link.id, left, right)
        reverse = (link.id, right, left)
        allocation = allocate_shared_banks(
            counts[forward], counts[reverse], link.bank_count,
            link.wires_per_bank,
            min_ratio=int(constraints.get("tdm_min_ratio", 1)),
            ratio_quantum=int(constraints["tdm_ratio_quantum"]),
            max_ratio=int(constraints["tdm_max_ratio"]),
        )
        if allocation is None:
            raise TDMScheduleInfeasibleError(
                f"shared bank allocation is infeasible for link {link.id!r}"
            )
        for key, prefix, offset in (
            (forward, "forward", 0),
            (reverse, "reverse", allocation["forward_lanes"]),
        ):
            result[key] = {
                "bank_count": allocation[f"{prefix}_banks"],
                "wires_per_bank": link.wires_per_bank,
                "lane_offset": offset,
                "lanes": allocation[f"{prefix}_lanes"],
            }
    return result

def _bank_allocation_records(
    allocations: Mapping[Tuple[str, str, str], Mapping[str, int]],
) -> List[Dict[str, Any]]:
    return [
        {"link": key[0], "from": key[1], "to": key[2], **value}
        for key, value in sorted(allocations.items())
    ]

def tdm_hop_latency_cycles(
    link: Any, ratio: int, *, base_delay_ns: Optional[float] = None,
) -> int:
    """Round a ratio-model hop delay up to physical fabric-clock cycles."""
    if link.tdm_delay_model is None:
        return link.latency_cycles
    if isinstance(ratio, bool) or not isinstance(ratio, int) or ratio < 1:
        raise ValidationError("TDM hop latency requires a positive integer ratio")
    delay = link.tdm_delay_model.delay_ns(ratio)
    if base_delay_ns is not None:
        delay = base_delay_ns + link.tdm_delay_model.ratio_coefficient_ns * (ratio - 1)
    return math.ceil(delay / (1000.0 / link.fabric_clock_mhz))

def _baseline_ratio_hops(
    routes: Mapping[str, Any], platform: Platform,
    arcs: Mapping[Any, Any], allocations: Mapping[Any, Any],
    constraints: Mapping[str, Any],
) -> Dict[HopKey, Dict[str, int]]:
    """Bind new-model baseline nets to homogeneous fixed wire groups.

    The old earliest-free-wire baseline can put unrelated releases on one
    wire unpredictably. Fixing each wire group first makes its actual ratio
    available before causality/arrival times are scheduled.
    """
    links = _link_by_id(platform)
    if not any(link.tdm_delay_model is not None for link in links.values()):
        return {}
    grouped: Dict[str, List[HopKey]] = defaultdict(list)
    for route in routes["routes"]:
        for edge in route["tree_edges"]:
            if links[edge["link"]].tdm_delay_model is not None:
                key = _hop_key(route["id"], edge["link"], edge["from"], edge["to"])
                grouped[arcs[key[1:]]["capacity_key"]].append(key)
    result = {}
    for keys in grouped.values():
        keys.sort()
        link = links[keys[0][1]]
        budget = allocations.get(keys[0][1:])
        lanes = budget["lanes"] if budget else link.transport_bits_per_cycle_per_direction
        offset = budget["lane_offset"] if budget else 0
        if lanes <= 0:
            raise TDMScheduleInfeasibleError("active direction has no allocated bank")
        for i, key in enumerate(keys):
            local_lane = i % lanes
            count = (len(keys) - 1 - local_lane) // lanes + 1
            raw = max(count, int(constraints.get("tdm_min_ratio", 1)))
            quantum = int(constraints["tdm_ratio_quantum"])
            ratio = 1 if raw == 1 else math.ceil(raw / quantum) * quantum
            if ratio > constraints["tdm_max_ratio"]:
                raise TDMScheduleInfeasibleError("baseline wire group exceeds maximum TDM ratio")
            result[key] = {"lane": offset + local_lane, "discrete_ratio": ratio}
    return result

def _write_native_schedule_board_request(
    path: Path, generation: Sequence[int], platform: Platform,
    constraints: Mapping[str, Any], arcs: Mapping[Any, Any],
    bank_allocations: Mapping[Any, Any], fpga_ids: Sequence[str],
    link_ids: Sequence[str], capacity_keys: Sequence[str],
) -> None:
    """Bounded board/configuration request; contains no route or hop records."""
    fpga_index = {name:i for i,name in enumerate(fpga_ids)}
    link_index = {name:i for i,name in enumerate(link_ids)}
    capacity_index = {name:i for i,name in enumerate(capacity_keys)}
    links = _link_by_id(platform)
    with path.open("wb") as stream:
        stream.write(struct.pack("<QI3Q4q6I",0x3142534D44544545,1,*generation,
            constraints["frame_slots"],RUNTIME_BARRIER_SLOTS,COMBINATIONAL_SETTLE_SLOTS,
            int(constraints.get("tdm_min_ratio",1)),constraints["tdm_max_ratio"],constraints["tdm_ratio_quantum"]))
        for catalog in (fpga_ids,link_ids,capacity_keys):
            stream.write(struct.pack("<I",len(catalog)))
            for name in catalog:
                encoded = name.encode("utf-8")
                stream.write(struct.pack("<I",len(encoded))); stream.write(encoded)
        stream.write(struct.pack("<I",len(arcs)))
        for key,arc in sorted(arcs.items()):
            link = links[key[0]]
            budget = bank_allocations.get(key)
            offset = budget["lane_offset"] if budget else 0
            lanes = budget["lanes"] if budget else link.transport_bits_per_cycle_per_direction
            base,slope = route_link_delay_coefficients_ns(platform,*key,constraints)
            stream.write(struct.pack("<8I3d",link_index[key[0]],fpga_index[key[1]],fpga_index[key[2]],
                capacity_index[arc["capacity_key"]],offset,lanes,int(link.tdm_delay_model is not None),
                link.latency_cycles,base,slope,1000.0/link.fabric_clock_mhz))


@dataclass(frozen=True)
class NativeExactScheduleRequest:
    """Bounded producer request, deliberately not a logical schedule view."""

    route_store: Path
    route_generation: tuple[int, ...]
    header: Mapping[str, Any]
    domain_templates: tuple[Mapping[str, Any], ...]
    platform: Platform
    constraints: Mapping[str, Any]
    arcs: Mapping[Any, Any]
    bank_allocations: Mapping[Any, Any]
    catalogs: tuple[tuple[str, ...], ...]
    source_bindings: tuple

    def write_board_request(self, path: Path) -> None:
        _write_native_schedule_board_request(
            path, self.route_generation, self.platform, self.constraints,
            self.arcs, self.bank_allocations, *self.catalogs,
        )


def build_native_exact_schedule_request(
    routes: Mapping[str, Any],
    platform: Platform,
    *,
    source_bindings=None,
) -> Optional[NativeExactScheduleRequest]:
    """Select the complete producer without importing routes or hop results.

    Indexed production uses one shared-core stage; failure is fatal, with no
    alternate native replay or materialized-result retry.
    """

    raw = routes.get("routes")
    if getattr(raw, "_user_version", None) != 3:
        return None
    contract = _static_exact_contract_from_routes(routes)
    if contract is None or contract.get("indexed") is not True:
        return None
    constraints = normalize_route_constraints(routes.get("constraints"), platform)
    _, arcs, capacities = build_directed_graph(platform, constraints)
    allocations = _bank_direction_allocations(routes, platform, constraints)
    frame = constraints["frame_slots"]
    links = _link_by_id(platform)
    templates = []
    for key in sorted(capacities):
        capacity = capacities[key]
        lanes = links[capacity["link"]].transport_bits_per_cycle_per_direction
        templates.append({"key": key, "link": capacity["link"], "direction": capacity["direction"],
                          "lanes": lanes, "frame_slots": frame, "capacity_bit_hops": lanes * frame})
    header = {
        "schema": TDM_SCHEDULE_SCHEMA, "design": routes.get("design"), "platform": platform.name,
        "provider": TDM_EXACT_DEPENDENCY_PROVIDER, "transport_semantics": "sampled-virtual-wire",
        "route_constraints": constraints, "qualification": "dependency-schedule-readiness-pass",
        "timing_constraints": sampled_virtual_wire_timing_constraints(frame),
        "semantic_contract_schema": contract["schema"],
        "semantic_contract_sha256": routes["semantic_contract_sha256"],
    }
    if allocations:
        header["bank_allocations"] = _bank_allocation_records(allocations)
    from .store_publication import require_store_sources_unchanged

    store = Path(raw.store_path).resolve()
    if source_bindings is None:
        bind_sources = getattr(routes, "bound_store_sources", None)
        if not callable(bind_sources):
            raise ValidationError(
                "indexed Phase 5 input lacks original source bindings"
            )
        source_bindings = bind_sources()
    sources = tuple(source_bindings)
    route_source = next(
        (
            source
            for source in sources
            if source.path == store and source.schema == "system-route-store-v3"
        ),
        None,
    )
    if route_source is None:
        raise ValidationError("indexed Phase 5 route ownership is missing")
    require_store_sources_unchanged(sources)
    return NativeExactScheduleRequest(
        store,
        route_source.generation,
        header,
        tuple(templates),
        platform,
        constraints,
        arcs,
        allocations,
        (
            tuple(sorted(fpga.id for fpga in platform.fpgas)),
            tuple(sorted(links)),
            tuple(sorted(capacities)),
        ),
        sources,
    )


def _domain_schedule_records(
    platform: Platform,
    constraints: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    counts = getattr(entries, "domain_counts", None)
    if counts is not None:
        return _domain_schedule_records_from_counts(platform,constraints,counts)
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

def _domain_schedule_records_from_counts(
    platform: Platform,
    constraints: Mapping[str, Any],
    counts: Mapping[str, int],
) -> List[Dict[str, Any]]:
    _, _, capacities = build_directed_graph(platform, constraints)
    links = _link_by_id(platform)
    unknown = sorted(set(counts) - set(capacities))
    if unknown:
        raise ValidationError(
            f"schedule entries reference unknown capacities {unknown[:8]}"
        )
    records = []
    for key in sorted(capacities):
        capacity = capacities[key]
        lanes = links[
            capacity["link"]
        ].transport_bits_per_cycle_per_direction
        scheduled = counts.get(key, 0)
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
                    scheduled / (lanes * constraints["frame_slots"])
                ),
            }
        )
    return records


def _validate_indexed_exact_tdm_schedule(
    routes,
    platform,
    schedule,
    exact_contract,
    constraints,
    sampled_timing_constraints,
    arcs,
    links,
    bank_allocations,
) -> Dict[str, Any]:
    """Current indexed validation has one independent native owner."""
    if getattr(routes.get("routes"), "_user_version", None) != 3:
        raise ValidationError(
            "indexed schedule validation requires the current typed RouteStore owner"
        )
    if (
        schedule.get("qualification") != "dependency-schedule-readiness-pass"
        or schedule.get("transport_semantics") != "sampled-virtual-wire"
        or schedule.get("semantic_contract_schema") != exact_contract.get("schema")
        or schedule.get("semantic_contract_sha256")
        != routes.get("semantic_contract_sha256")
    ):
        raise ValidationError("cross-layer timing schedule binding is invalid")
    from .tdm_schedule_validation import validate_native_exact_schedule

    return validate_native_exact_schedule(
        routes, platform, schedule, constraints, arcs, bank_allocations
    )


def validate_tdm_schedule(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    if schedule.get("provider") not in {
        TDM_EXACT_DEPENDENCY_PROVIDER, "deterministic-round-barrier-earliest-slot-v2",
    }:
        raise ValidationError("unsupported TDM schedule artifact provider")
    exact_contract = _static_exact_contract_from_routes(routes)
    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError(
            f"schedule.schema: expected {TDM_SCHEDULE_SCHEMA!r}, "
            f"got {schedule.get('schema')!r}"
        )
    expected_transport_semantics = (
        "sampled-virtual-wire"
        if exact_contract is not None
        else "registered-boundary"
    )
    if schedule.get("transport_semantics") != expected_transport_semantics:
        raise ValidationError(
            "schedule.transport_semantics does not match the Phase 3 "
            "timing contract"
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
    bank_allocations = _bank_direction_allocations(routes, platform, constraints)
    if bank_allocations and schedule.get("bank_allocations") != _bank_allocation_records(bank_allocations):
        raise ValidationError("schedule.bank_allocations does not match routed bank allocation")
    indexed_store = callable(
        getattr(schedule, "open_validation_index", None)
    )
    indexed_exact_store = bool(
        indexed_store
        and exact_contract is not None
        and exact_contract.get("indexed") is True
        and schedule.get("provider") == TDM_EXACT_DEPENDENCY_PROVIDER
    )
    if indexed_exact_store:
        return _validate_indexed_exact_tdm_schedule(
            routes,
            platform,
            schedule,
            exact_contract,
            constraints,
            sampled_timing_constraints,
            arcs,
            links,
            bank_allocations,
        )
    if indexed_store:
        materialize = getattr(schedule, "materialize", None)
        if not callable(materialize):
            raise ValidationError(
                "indexed schedule compatibility materializer is unavailable"
            )
        return validate_tdm_schedule(
            routes,
            platform,
            materialize(),
        )
    bank_directions: Dict[Tuple[str, int], Tuple[str, str]] = {}
    expected = _expected_hops(routes)
    extended_schedule = True
    expected_route_metadata = []
    for route in sorted(routes["routes"], key=lambda item: item["id"]):
        record = {
            "id": route["id"],
            "net": route["net"],
            "source": route["source"],
            "sinks": list(route["sinks"]),
            "tree_edges": list(route["tree_edges"]),
        }
        if extended_schedule and exact_contract is None:
            record["transport_round"] = route.get("transport_round", 0)
        expected_route_metadata.append(record)
    if schedule.get("routes") != expected_route_metadata:
        raise ValidationError("schedule.routes does not match system routes")

    baseline_hops = (_baseline_ratio_hops(routes, platform, arcs, bank_allocations, constraints))
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
        bank_budget = bank_allocations.get((entry["link"], entry["from"], entry["to"]))
        if bank_budget is not None:
            offset = bank_budget["lane_offset"]
            if not offset <= lane < offset + bank_budget["lanes"]:
                raise ValidationError("schedule entry wire lies outside its allocated direction banks")
            bank_key = (entry["link"], lane // bank_budget["wires_per_bank"])
            direction = (entry["from"], entry["to"])
            if bank_directions.setdefault(bank_key, direction) != direction:
                raise ValidationError("schedule bank is assigned to both directions")
        if link.tdm_delay_model is not None:
            ratio = entry.get("tdm_ratio")
            quantum = int(constraints["tdm_ratio_quantum"])
            minimum = int(constraints.get("tdm_min_ratio", 1))
            if (isinstance(ratio, bool) or not isinstance(ratio, int)
                    or ratio < minimum or ratio > constraints["tdm_max_ratio"]
                    or ratio % quantum):
                raise ValidationError("schedule entry tdm_ratio is invalid for the net delay model")
        collision = (slot, lane)
        if collision in occupancy[arc["capacity_key"]]:
            raise ValidationError(
                f"schedule collision in {arc['capacity_key']!r} at "
                f"slot={slot}, lane={lane}"
            )
        occupancy[arc["capacity_key"]].add(collision)
        if key in baseline_hops:
            baseline = baseline_hops[key]
            if lane != baseline["lane"] or entry.get("tdm_ratio") != baseline["discrete_ratio"]:
                raise ValidationError("baseline schedule wire/ratio differs from fixed group")
            if slot - entry["ready_slot"] >= entry["tdm_ratio"]:
                raise ValidationError("baseline schedule wait exceeds its ratio window")
        base, _slope = route_link_delay_coefficients_ns(
            platform, entry["link"], entry["from"], entry["to"], constraints)
        expected_arrival = slot + tdm_hop_latency_cycles(
            link, entry.get("tdm_ratio", 1), base_delay_ns=base)
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
    indexed_exact = bool(
        exact_contract is not None and exact_contract.get("indexed") is True
    )
    if exact_contract is None:
        ordered_routes, active_rounds = _round_order(routes["routes"])
    else:
        if indexed_exact:
            exact_nodes = _indexed_exact_nodes(routes["routes"])
        else:
            exact_nodes, exact_segments, exact_captures = (
                _exact_contract_indexes(exact_contract)
            )
        ordered_routes = _exact_dependency_order(
            exact_nodes, routes["routes"]
        )
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
            if indexed_exact:
                source_ready_slot, _source_evidence = (
                    _indexed_source_readiness(
                        route,
                        exact_arrivals,
                        sampled_timing_constraints,
                    )
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
        completion["source_ready_slot"] = source_ready_slot
        if exact_contract is None:
            completion["transport_round"] = transport_round
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
    if extended_schedule and exact_contract is None:
        expected_metrics.update(
            {
                "transport_rounds": len(active_rounds),
                "round_barriers": max(0, len(active_rounds) - 1),
                "max_transport_round": max(active_rounds, default=0),
                "combinational_settle_slots": COMBINATIONAL_SETTLE_SLOTS,
            }
        )
    elif exact_contract is not None:
        expected_metrics["transport_ordering_policy"] = (
            "path-local-dependency-dag-v1"
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
        if indexed_exact:
            expected_certificate = _indexed_certificate(
                ordered_routes,
                exact_arrivals,
                sampled_timing_constraints,
            )
            captures = None
            minimum_capture_slack = expected_certificate[
                "minimum_sink_slack_slots"
            ]
        else:
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
        if indexed_exact:
            expected_metrics.update(
                {
                    "commit_slot": sampled_timing_constraints["commit_slot"],
                    "dependency_edges": expected_certificate[
                        "dependency_edges"
                    ],
                    "maximum_combinational_dependency_depth": max(
                        (
                            route["combinational_dependency_depth"]
                            for route in ordered_routes
                        ),
                        default=0,
                    ),
                    "conservative_sink_readiness_checks": expected_certificate[
                        "sink_readiness_checks"
                    ],
                    "minimum_capture_slack_slots": minimum_capture_slack,
                }
            )
        else:
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
    if schedule.get("metrics") != expected_metrics:
        raise ValidationError(
            "schedule.metrics does not match independently recomputed metrics"
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

def iter_schedule_tsv_lines(schedule: Mapping[str, Any]) -> Iterator[str]:
    """Yield a schedule TSV without constructing a second hop-sized string."""

    yield "entry\tdemand\tnet\tlink\tfrom\tto\tslot\tlane\tready\tarrival\n"
    for entry in schedule["entries"]:
        yield (
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
            + "\n"
        )

def schedule_to_tsv(schedule: Mapping[str, Any]) -> str:
    """Compatibility helper for callers that explicitly need TSV in memory."""

    return "".join(iter_schedule_tsv_lines(schedule))

def write_schedule_tsv(path: Path, schedule: Mapping[str, Any]) -> None:
    """Stream and atomically publish the optional schedule diagnostic."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.writelines(iter_schedule_tsv_lines(schedule))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

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
    def channel_key(entry: Mapping[str, Any]) -> Tuple[str, int]:
        return entry["capacity_key"], entry["arrival_slot"] - entry["slot"]

    channel_keys = sorted({channel_key(entry) for entry in schedule["entries"]})
    channel_index = {key: index for index, key in enumerate(channel_keys)}
    channel_link = {channel_key(entry): links[entry["link"]] for entry in schedule["entries"]}

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
                f"    .LATENCY({key[1]})",
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
            channel = channel_index[channel_key(entry)]
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
            channel = channel_index[channel_key(entry)]
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
