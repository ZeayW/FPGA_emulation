from __future__ import annotations

import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .errors import ValidationError
from .combinational_cut import (
    GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA,
    STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2,
    STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS,
    semantic_contract_sha256,
)
from .io import read_json
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .platform import BoardLink, Platform


SYSTEM_ROUTES_SCHEMA = "emuflow.system-routes/v1"
SYSTEM_ROUTE_CONSTRAINTS_SCHEMA = "emuflow.system-route-constraints/v1"
ArcKey = Tuple[str, str, str]


def static_exact_contract_from_assignment(
    assignment: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    contract = assignment.get("semantic_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict):
        raise ValidationError("assignment.semantic_contract: expected an object")
    if (
        contract.get("schema") not in STATIC_EXACT_COMBINATIONAL_CUT_SCHEMAS
        or contract.get("mode") != "static-exact-combinational"
        or contract.get("qualification")
        != "partition-legality-only-provisional"
    ):
        raise ValidationError(
            "assignment.semantic_contract is not a supported exact-cut contract"
        )
    if contract.get("schema") == GENERALIZED_STATIC_EXACT_COMBINATIONAL_CUT_SCHEMA:
        lower_bound = contract.get("uncongested_schedule_lower_bound")
        if (
            contract.get("candidate_selection_policy")
            != STATIC_EXACT_CANDIDATE_ASSIGNMENT_V2
            or not isinstance(lower_bound, dict)
            or lower_bound.get("provider")
            != "board-minimum-latency-dag-lower-bound-v1"
            or lower_bound.get("qualification")
            != "necessary-not-sufficient-before-routing"
        ):
            raise ValidationError(
                "assignment generalized exact-cut certificate is incomplete"
            )
    raw_cuts = assignment.get("cut_nets")
    raw_nodes = contract.get("cut_nodes")
    if not isinstance(raw_cuts, list) or not isinstance(raw_nodes, list):
        raise ValidationError(
            "exact assignment cut and contract nodes must be arrays"
        )
    if not all(
        isinstance(item, dict)
        and isinstance(item.get("net"), str)
        and bool(item["net"])
        for item in raw_cuts + raw_nodes
    ):
        raise ValidationError(
            "exact assignment cuts and contract nodes require non-empty net "
            "identities"
        )
    cuts = {
        item["net"]: item for item in raw_cuts
    }
    nodes = {
        item["net"]: item for item in raw_nodes
    }
    if (
        len(cuts) != len(raw_cuts)
        or len(nodes) != len(raw_nodes)
        or set(cuts) != set(nodes)
    ):
        raise ValidationError(
            "exact semantic contract cut-node coverage is not exact"
        )
    for net_id in sorted(cuts):
        cut = cuts[net_id]
        node = nodes[net_id]
        expected = {
            "cut_class": cut.get("cut_class"),
            "source_fpgas": cut.get("source_fpgas"),
            "sink_fpgas": cut.get("sink_fpgas"),
        }
        for field, value in expected.items():
            if node.get(field) != value:
                raise ValidationError(
                    f"exact semantic contract node {net_id!r}.{field} "
                    "does not match assignment"
                )
        if cut.get("cut_class") == "combinational":
            for field in (
                "dependency_level",
                "combinational_dependency_depth",
                "predecessor_cut_nets",
            ):
                if cut.get(field) != node.get(field):
                    raise ValidationError(
                        f"exact semantic contract node {net_id!r}.{field} "
                        "does not match assignment"
                    )
    return contract


def normalize_route_constraints(
    value: Optional[Mapping[str, Any]],
    platform: Platform,
    frame_slots: Optional[int] = None,
    max_iterations: Optional[int] = None,
) -> Dict[str, Any]:
    raw: Mapping[str, Any] = value or {}
    if raw and raw.get("schema") != SYSTEM_ROUTE_CONSTRAINTS_SCHEMA:
        raise ValidationError(
            "route constraints.schema: expected "
            f"{SYSTEM_ROUTE_CONSTRAINTS_SCHEMA!r}, "
            f"got {raw.get('schema')!r}"
        )

    raw_frame_slots = raw.get("frame_slots", 32)
    if frame_slots is not None:
        raw_frame_slots = frame_slots
    if (
        isinstance(raw_frame_slots, bool)
        or not isinstance(raw_frame_slots, int)
        or raw_frame_slots <= 0
    ):
        raise ValidationError(
            "route constraints.frame_slots: expected a positive integer"
        )

    raw_iterations = raw.get("max_iterations", 20)
    if max_iterations is not None:
        raw_iterations = max_iterations
    if (
        isinstance(raw_iterations, bool)
        or not isinstance(raw_iterations, int)
        or raw_iterations <= 0
    ):
        raise ValidationError(
            "route constraints.max_iterations: expected a positive integer"
        )

    raw_max_route_hops = raw.get("max_route_hops")
    if raw_max_route_hops is not None and (
        isinstance(raw_max_route_hops, bool)
        or not isinstance(raw_max_route_hops, int)
        or raw_max_route_hops <= 0
    ):
        raise ValidationError(
            "route constraints.max_route_hops: expected null or a positive "
            "integer"
        )

    raw_unavailable = raw.get("unavailable_links", [])
    if not isinstance(raw_unavailable, list) or not all(
        isinstance(link_id, str) for link_id in raw_unavailable
    ):
        raise ValidationError(
            "route constraints.unavailable_links: expected an array of strings"
        )
    link_ids = {link.id for link in platform.links}
    unknown = sorted(set(raw_unavailable) - link_ids)
    if unknown:
        raise ValidationError(
            f"route constraints.unavailable_links: unknown links {unknown}"
        )

    raw_link_delays = raw.get("link_delay_ns", {})
    if not isinstance(raw_link_delays, dict):
        raise ValidationError(
            "route constraints.link_delay_ns: expected an object"
        )
    unknown_delays = sorted(set(raw_link_delays) - link_ids)
    if unknown_delays:
        raise ValidationError(
            "route constraints.link_delay_ns: unknown links "
            f"{unknown_delays}"
        )
    link_delays = {}
    for link_id, value in raw_link_delays.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            raise ValidationError(
                f"route constraints.link_delay_ns.{link_id}: "
                "expected a non-negative number"
            )
        link_delays[link_id] = float(value)

    raw_directed_delays = raw.get("directed_link_delay_ns", {})
    if not isinstance(raw_directed_delays, dict):
        raise ValidationError(
            "route constraints.directed_link_delay_ns: expected an object"
        )
    legal_arcs = set()
    for link in platform.links:
        left, right = link.endpoints
        legal_arcs.add((link.id, left, right))
        if link.direction in {"full_duplex", "half_duplex"}:
            legal_arcs.add((link.id, right, left))
    directed_delays: Dict[str, Dict[str, Dict[str, float]]] = {}
    for link_id, by_source in raw_directed_delays.items():
        if not isinstance(link_id, str) or not isinstance(by_source, dict):
            raise ValidationError(
                "route constraints.directed_link_delay_ns: invalid link map"
            )
        for source, by_sink in by_source.items():
            if not isinstance(source, str) or not isinstance(by_sink, dict):
                raise ValidationError(
                    "route constraints.directed_link_delay_ns: "
                    "invalid source map"
                )
            for sink, value in by_sink.items():
                if (link_id, source, sink) not in legal_arcs:
                    raise ValidationError(
                        "route constraints.directed_link_delay_ns: "
                        f"unknown arc {(link_id, source, sink)}"
                    )
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or float(value) < 0.0
                ):
                    raise ValidationError(
                        "route constraints.directed_link_delay_ns: "
                        f"invalid delay for {(link_id, source, sink)}"
                    )
                directed_delays.setdefault(link_id, {}).setdefault(
                    source, {}
                )[sink] = float(value)

    raw_sll_links = raw.get("sll_links", [])
    if not isinstance(raw_sll_links, list) or not all(
        isinstance(link_id, str) for link_id in raw_sll_links
    ):
        raise ValidationError(
            "route constraints.sll_links: expected an array of strings"
        )
    unknown_sll = sorted(set(raw_sll_links) - link_ids)
    if unknown_sll:
        raise ValidationError(
            f"route constraints.sll_links: unknown links {unknown_sll}"
        )

    raw_shared_links = raw.get("shared_capacity_links", [])
    if not isinstance(raw_shared_links, list) or not all(
        isinstance(link_id, str) for link_id in raw_shared_links
    ):
        raise ValidationError(
            "route constraints.shared_capacity_links: expected an array "
            "of strings"
        )
    unknown_shared = sorted(set(raw_shared_links) - link_ids)
    if unknown_shared:
        raise ValidationError(
            "route constraints.shared_capacity_links: unknown links "
            f"{unknown_shared}"
        )

    optimization_values = {}
    for key, default, integer in (
        ("reroute_rounds", 8, True),
        ("lambda_load", 2.0, False),
        ("lambda_timing", 4.0, False),
        ("lambda_history", 1.0, False),
        ("lambda_tdm", 0.1, False),
        ("tdm_ratio_quantum", 8, True),
        ("tdm_min_ratio", 1, True),
    ):
        value = raw.get(key, default)
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int if integer else (int, float))
            and value >= 0
        )
        if not valid:
            kind = "non-negative integer" if integer else "non-negative number"
            raise ValidationError(
                f"route constraints.{key}: expected a {kind}"
            )
        optimization_values[key] = int(value) if integer else float(value)
    if optimization_values["tdm_ratio_quantum"] <= 0:
        raise ValidationError(
            "route constraints.tdm_ratio_quantum: expected a positive integer"
        )
    minimum_ratio = optimization_values["tdm_min_ratio"]
    ratio_quantum = optimization_values["tdm_ratio_quantum"]
    if minimum_ratio <= 0:
        raise ValidationError(
            "route constraints.tdm_min_ratio: expected a positive integer"
        )
    if minimum_ratio != 1 and minimum_ratio % ratio_quantum:
        raise ValidationError(
            "route constraints.tdm_min_ratio: expected 1 or a multiple of "
            "tdm_ratio_quantum"
        )
    if minimum_ratio > raw_frame_slots:
        raise ValidationError(
            "route constraints.tdm_min_ratio: cannot exceed frame_slots"
        )
    tree_edge_sum_tdm = raw.get("tree_edge_sum_tdm", False)
    if not isinstance(tree_edge_sum_tdm, bool):
        raise ValidationError(
            "route constraints.tree_edge_sum_tdm: expected a boolean"
        )
    hard_sll_capacity = raw.get("hard_sll_capacity", False)
    if not isinstance(hard_sll_capacity, bool):
        raise ValidationError(
            "route constraints.hard_sll_capacity: expected a boolean"
        )

    boarddb_shared_links = {
        link.id
        for link in platform.links
        if link.capacity_sharing == "shared_bidirectional"
    }
    return {
        "schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
        "frame_slots": raw_frame_slots,
        "max_iterations": raw_iterations,
        "max_route_hops": raw_max_route_hops,
        "unavailable_links": sorted(set(raw_unavailable)),
        "link_delay_ns": dict(sorted(link_delays.items())),
        "directed_link_delay_ns": {
            link_id: {
                source: dict(sorted(by_sink.items()))
                for source, by_sink in sorted(by_source.items())
            }
            for link_id, by_source in sorted(directed_delays.items())
        },
        "sll_links": sorted(set(raw_sll_links)),
        "shared_capacity_links": sorted(
            set(raw_shared_links) | boarddb_shared_links
        ),
        "tree_edge_sum_tdm": tree_edge_sum_tdm,
        "hard_sll_capacity": hard_sll_capacity,
        **optimization_values,
    }


def route_link_delay_ns(
    platform: Platform,
    link_id: str,
    source: str,
    sink: str,
    constraints: Mapping[str, Any],
) -> float:
    """Return a direction-exact delay override with legacy fallback."""
    directed = constraints.get("directed_link_delay_ns", {})
    override = directed.get(link_id, {}).get(source, {}).get(sink)
    if override is not None:
        return float(override)
    link_override = constraints.get("link_delay_ns", {}).get(link_id)
    if link_override is not None:
        return float(link_override)
    link = next(link for link in platform.links if link.id == link_id)
    return link.latency_cycles * 1000.0 / link.fabric_clock_mhz


def load_route_constraints(
    path: Optional[Path],
    platform: Platform,
    frame_slots: Optional[int] = None,
    max_iterations: Optional[int] = None,
) -> Dict[str, Any]:
    value = read_json(path) if path is not None else None
    return normalize_route_constraints(
        value,
        platform,
        frame_slots=frame_slots,
        max_iterations=max_iterations,
    )


def _arc_key(link_id: str, source: str, sink: str) -> ArcKey:
    return (link_id, source, sink)


def estimate_tdm_ratio(
    signals: int,
    lanes: int,
    constraints: Mapping[str, Any],
    *,
    is_sll: bool = False,
) -> int:
    """Return the route/TDM proxy ratio for one capacity domain."""
    if is_sll or signals <= 0:
        return 1
    minimum = int(constraints.get("tdm_min_ratio", 1))
    raw = max(minimum, (signals + lanes - 1) // lanes)
    if raw == 1:
        return 1
    quantum = int(constraints["tdm_ratio_quantum"])
    return min(
        int(constraints["frame_slots"]),
        ((raw + quantum - 1) // quantum) * quantum,
    )


def _capacity_key(
    link: BoardLink,
    source: str,
    sink: str,
    shared_capacity_links: Set[str],
) -> str:
    if link.direction == "half_duplex" or link.id in shared_capacity_links:
        return f"{link.id}:shared"
    return f"{link.id}:{source}->{sink}"


def build_directed_graph(
    platform: Platform,
    constraints: Mapping[str, Any],
) -> Tuple[
    Dict[str, List[Dict[str, Any]]],
    Dict[ArcKey, Dict[str, Any]],
    Dict[str, Dict[str, Any]],
]:
    unavailable = set(constraints["unavailable_links"])
    shared_capacity_links = set(constraints.get("shared_capacity_links", []))
    adjacency: Dict[str, List[Dict[str, Any]]] = {
        fpga.id: [] for fpga in platform.fpgas
    }
    arcs: Dict[ArcKey, Dict[str, Any]] = {}
    capacity_records: Dict[str, Dict[str, Any]] = {}

    for link in platform.links:
        if link.id in unavailable:
            continue
        left, right = link.endpoints
        directions = [(left, right)]
        if link.direction in {"full_duplex", "half_duplex"}:
            directions.append((right, left))
        for source, sink in directions:
            capacity_key = _capacity_key(
                link, source, sink, shared_capacity_links
            )
            arc = {
                "link": link.id,
                "from": source,
                "to": sink,
                "latency_cycles": link.latency_cycles,
                "capacity_key": capacity_key,
            }
            arcs[_arc_key(link.id, source, sink)] = arc
            adjacency[source].append(arc)
            if capacity_key not in capacity_records:
                capacity_records[capacity_key] = {
                    "key": capacity_key,
                    "link": link.id,
                    "direction": (
                        "shared"
                        if (
                            link.direction == "half_duplex"
                            or link.id in shared_capacity_links
                        )
                        else f"{source}->{sink}"
                    ),
                    "capacity_bits": (
                        link.transport_bits_per_cycle_per_direction
                        * (
                            1
                            if link.id in constraints["sll_links"]
                            else constraints["frame_slots"]
                        )
                    ),
                }

    for source in adjacency:
        adjacency[source].sort(
            key=lambda arc: (arc["to"], arc["link"], arc["from"])
        )
    return adjacency, arcs, capacity_records


def demands_from_assignment(
    assignment: Mapping[str, Any],
    platform: Platform,
) -> List[Dict[str, Any]]:
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}, "
            f"got {assignment.get('schema')!r}"
        )
    if assignment.get("platform") != platform.name:
        raise ValidationError(
            f"assignment.platform: expected {platform.name!r}, "
            f"got {assignment.get('platform')!r}"
        )
    fpga_ids = {fpga.id for fpga in platform.fpgas}
    exact_contract = static_exact_contract_from_assignment(assignment)
    contract_nodes = (
        {item["net"]: item for item in exact_contract["cut_nodes"]}
        if exact_contract is not None
        else {}
    )
    raw_cuts = assignment.get("cut_nets")
    if not isinstance(raw_cuts, list):
        raise ValidationError("assignment.cut_nets: expected an array")

    demands: List[Dict[str, Any]] = []
    demand_ids: Set[str] = set()
    for index, cut in enumerate(raw_cuts):
        if not isinstance(cut, dict):
            raise ValidationError(f"assignment.cut_nets[{index}]: expected an object")
        net_id = cut.get("net")
        sources = cut.get("source_fpgas")
        sinks = cut.get("sink_fpgas")
        if not isinstance(net_id, str) or not net_id:
            raise ValidationError(
                f"assignment.cut_nets[{index}].net: expected a non-empty string"
            )
        if net_id in demand_ids:
            raise ValidationError(
                f"assignment.cut_nets[{index}].net: duplicate {net_id!r}"
            )
        demand_ids.add(net_id)
        if not isinstance(sources, list) or len(sources) != 1:
            raise ValidationError(
                f"assignment.cut_nets[{index}].source_fpgas: "
                "expected exactly one source FPGA"
            )
        if (
            not isinstance(sinks, list)
            or not sinks
            or not all(isinstance(sink, str) for sink in sinks)
        ):
            raise ValidationError(
                f"assignment.cut_nets[{index}].sink_fpgas: "
                "expected a non-empty string array"
            )
        source = sources[0]
        unknown = sorted(({source} | set(sinks)) - fpga_ids)
        if unknown:
            raise ValidationError(
                f"assignment.cut_nets[{index}]: unknown FPGAs {unknown}"
            )
        normalized_sinks = sorted(set(sinks) - {source})
        if not normalized_sinks:
            raise ValidationError(
                f"assignment.cut_nets[{index}]: no remote sink FPGA"
            )
        raw_round = cut.get("transport_round", 0)
        if (
            isinstance(raw_round, bool)
            or not isinstance(raw_round, int)
            or raw_round < 0
        ):
            raise ValidationError(
                f"assignment.cut_nets[{index}].transport_round: "
                "expected a non-negative integer"
            )
        demand = {
            "id": f"d{index:06d}",
            "net": net_id,
            "source": source,
            "sinks": normalized_sinks,
            "width_bits": 1,
        }
        if "transport_round" in cut:
            demand["transport_round"] = raw_round
        if exact_contract is not None:
            node = contract_nodes[net_id]
            demand.update(
                {
                    "cut_class": node["cut_class"],
                    "dependency_level": node["dependency_level"],
                    "combinational_dependency_depth": node[
                        "combinational_dependency_depth"
                    ],
                    "predecessor_cut_nets": list(
                        node["predecessor_cut_nets"]
                    ),
                }
            )
        demands.append(demand)
    return sorted(demands, key=lambda demand: demand["net"])


def _link_utilization_records(
    capacity_records: Mapping[str, Mapping[str, Any]],
    usage: Mapping[str, int],
) -> List[Dict[str, Any]]:
    records = []
    for key in sorted(capacity_records):
        capacity = capacity_records[key]
        used_bits = usage.get(key, 0)
        records.append(
            {
                **capacity,
                "used_bits": used_bits,
                "utilization": used_bits / capacity["capacity_bits"],
            }
        )
    return records


def _validate_route_tree(
    route: Mapping[str, Any],
    arcs: Mapping[ArcKey, Mapping[str, Any]],
) -> Tuple[List[ArcKey], int, int]:
    raw_edges = route.get("tree_edges")
    if not isinstance(raw_edges, list):
        raise ValidationError(
            f"route {route.get('id')!r}.tree_edges: expected an array"
        )
    edge_keys: List[ArcKey] = []
    graph: Dict[str, List[str]] = defaultdict(list)
    indegree: Dict[str, int] = defaultdict(int)
    latency_by_edge: Dict[Tuple[str, str], int] = {}
    for index, edge in enumerate(raw_edges):
        if not isinstance(edge, dict):
            raise ValidationError(
                f"route {route.get('id')!r}.tree_edges[{index}]: expected an object"
            )
        key = _arc_key(edge.get("link"), edge.get("from"), edge.get("to"))
        if key not in arcs:
            raise ValidationError(
                f"route {route.get('id')!r}: illegal directed edge {key}"
            )
        if key in edge_keys:
            raise ValidationError(
                f"route {route.get('id')!r}: duplicate edge {key}"
            )
        edge_keys.append(key)
        graph[key[1]].append(key[2])
        indegree[key[2]] += 1
        latency_by_edge[(key[1], key[2])] = arcs[key]["latency_cycles"]

    source = route["source"]
    reachable = {source}
    queue = deque([source])
    latency = {source: 0}
    hops = {source: 0}
    while queue:
        node = queue.popleft()
        for sink in sorted(graph.get(node, [])):
            if sink in reachable:
                raise ValidationError(
                    f"route {route.get('id')!r}: tree contains a cycle or "
                    f"multiple path to {sink!r}"
                )
            reachable.add(sink)
            latency[sink] = latency[node] + latency_by_edge[(node, sink)]
            hops[sink] = hops[node] + 1
            queue.append(sink)

    edge_nodes = {node for key in edge_keys for node in (key[1], key[2])}
    if not edge_nodes <= reachable:
        raise ValidationError(
            f"route {route.get('id')!r}: tree has edges disconnected from source"
        )
    missing = sorted(set(route["sinks"]) - reachable)
    if missing:
        raise ValidationError(
            f"route {route.get('id')!r}: sinks are unreachable {missing}"
        )
    for node, count in indegree.items():
        if node != source and count != 1:
            raise ValidationError(
                f"route {route.get('id')!r}: node {node!r} has indegree {count}"
            )
    max_latency = max((latency[sink] for sink in route["sinks"]), default=0)
    max_hops = max((hops[sink] for sink in route["sinks"]), default=0)
    return edge_keys, max_latency, max_hops


def validate_system_routes(
    assignment: Mapping[str, Any],
    platform: Platform,
    routes_artifact: Mapping[str, Any],
) -> Dict[str, Any]:
    if routes_artifact.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(
            f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}, "
            f"got {routes_artifact.get('schema')!r}"
        )
    constraints = normalize_route_constraints(
        routes_artifact.get("constraints"),
        platform,
    )
    expected_demands = demands_from_assignment(assignment, platform)
    if routes_artifact.get("demands") != expected_demands:
        raise ValidationError("routes.demands does not match partition cut nets")
    exact_contract = static_exact_contract_from_assignment(assignment)
    if exact_contract is None:
        if any(
            key in routes_artifact
            for key in ("semantic_contract", "semantic_contract_sha256")
        ):
            raise ValidationError(
                "safe routes may not contain an exact semantic contract"
            )
    else:
        if routes_artifact.get("semantic_contract") != exact_contract:
            raise ValidationError(
                "routes.semantic_contract does not match assignment"
            )
        if routes_artifact.get("semantic_contract_sha256") != (
            semantic_contract_sha256(exact_contract)
        ):
            raise ValidationError(
                "routes.semantic_contract_sha256 does not match contract"
            )

    adjacency, arcs, capacities = build_directed_graph(platform, constraints)
    del adjacency
    raw_routes = routes_artifact.get("routes")
    if not isinstance(raw_routes, list):
        raise ValidationError("routes.routes: expected an array")
    route_by_id = {
        route.get("id"): route for route in raw_routes if isinstance(route, dict)
    }
    expected_by_id = {demand["id"]: demand for demand in expected_demands}
    if set(route_by_id) != set(expected_by_id) or len(route_by_id) != len(raw_routes):
        raise ValidationError("routes.routes: demand coverage is not exact")

    usage = {key: 0 for key in capacities}
    routed_sinks = 0
    tree_edge_count = 0
    maximum_observed_hops = 0
    for demand_id in sorted(expected_by_id):
        route = route_by_id[demand_id]
        demand = expected_by_id[demand_id]
        for field in ("id", "net", "source", "sinks", "width_bits"):
            actual = route.get(field)
            if actual != demand[field]:
                raise ValidationError(
                    f"route {demand_id!r}.{field}: does not match demand"
                )
        if route.get("transport_round", 0) != demand.get(
            "transport_round", 0
        ):
            raise ValidationError(
                f"route {demand_id!r}.transport_round: does not match demand"
            )
        edge_keys, max_latency, max_hops = _validate_route_tree(route, arcs)
        hop_limit = constraints.get("max_route_hops")
        if hop_limit is not None and max_hops > hop_limit:
            raise ValidationError(
                f"route {demand_id!r}: source-to-sink path uses {max_hops} "
                f"hops, above maximum {hop_limit}"
            )
        if route.get("max_latency_cycles") != max_latency:
            raise ValidationError(
                f"route {demand_id!r}.max_latency_cycles: expected "
                f"{max_latency}, got {route.get('max_latency_cycles')!r}"
            )
        for key in edge_keys:
            usage[arcs[key]["capacity_key"]] += demand["width_bits"]
        routed_sinks += len(demand["sinks"])
        tree_edge_count += len(edge_keys)
        maximum_observed_hops = max(maximum_observed_hops, max_hops)

    expected_utilization = _link_utilization_records(capacities, usage)
    if routes_artifact.get("link_utilization") != expected_utilization:
        raise ValidationError(
            "routes.link_utilization does not match independently recomputed usage"
        )
    overloaded = [
        record
        for record in expected_utilization
        if record["used_bits"] > record["capacity_bits"]
    ]
    if overloaded:
        raise ValidationError(f"routes exceed modeled link capacity: {overloaded}")

    expected_metrics = {
        "demands": len(expected_demands),
        "routed_sinks": routed_sinks,
        "tree_edges": tree_edge_count,
        "max_link_utilization": max(
            (record["utilization"] for record in expected_utilization),
            default=0.0,
        ),
        "total_link_bit_hops": sum(usage.values()),
    }
    if constraints.get("max_route_hops") is not None:
        expected_metrics["max_route_hops_observed"] = maximum_observed_hops
    metrics = routes_artifact.get("metrics")
    if not isinstance(metrics, dict):
        raise ValidationError("routes.metrics: expected an object")
    for key, expected in expected_metrics.items():
        if metrics.get(key) != expected:
            raise ValidationError(
                f"routes.metrics.{key}: expected {expected}, "
                f"got {metrics.get(key)!r}"
            )

    return {
        "status": "pass",
        **expected_metrics,
        "iterations": metrics.get("iterations"),
        "overloaded_links": 0,
        "link_utilization": expected_utilization,
    }
