"""Author-derived DAC 2025 routing kernel and provider-neutral flow adapter.

The kernel selects route trees. EmuFlow independently supplies structural
legality, platform delay evaluation, and the ordinary downstream TDM flow.
"""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .routing import (
    _arc_key, _link_utilization_records, _validate_route_tree,
    assignment_has_static_exact_contract, build_directed_graph,
    normalize_route_constraints, route_link_delay_ns, validate_system_routes,
)
from .tdm import RUNTIME_BARRIER_SLOTS
from .timing_routing import (
    NATIVE_ROUTER_PROVIDER, ROUND_CAPACITY_CONTRACT_SCHEMA, STA_PATHS_SCHEMA,
    _prepare_native_model, _round_available_slots, _output_realization_metadata,
    _validate_round_capacity_contract, _validate_single_node_cut_capacity,
    _write_native_input, reconstruct_system_route_timing,
)


AUTHOR_ROUTER_PROVIDER = "synergistic-author-route-v1"
AUTHOR_OUTPUT_PROTOCOL = "EMUFLOW_SYNERGISTIC_ROUTE_OUTPUT_V1"
AUTHOR_METHOD = "author-global-connection-order-shared-tree-maze-continuous-pressure-v1"
AUTHOR_COST_POLICY = "author-pressure-v1"
AUTHOR_COST_METHODS = {
    AUTHOR_COST_POLICY: AUTHOR_METHOD,
    "author-sll-pressure-v1": "author-global-connection-order-shared-tree-maze-sll-scarcity-v1",
}
_COUNTERS = {"connections", "routed_connections", "search_expansions", "tree_edges"}
_TIMING_METRICS = (
    "worst_slack_ns", "worst_normalized_slack",
    "estimated_worst_tdm_slack_ns", "estimated_worst_tdm_normalized_slack",
    "estimated_max_tdm_ratio",
)


def _parse_author_output(path: Path, model: Mapping[str, Any]):
    trees, counters = {}, {}
    with path.open(encoding="utf-8") as stream:
        if stream.readline().strip() != AUTHOR_OUTPUT_PROTOCOL:
            raise EmuFlowError("author router returned an invalid protocol header")
        for line in stream:
            fields = line.split()
            if not fields:
                continue
            try:
                if fields[0] == "ROUTE" and len(fields) == 3:
                    index = int(fields[1])
                    if index in trees or not 0 <= index < len(model["demands"]):
                        raise ValueError("duplicate or unknown demand index")
                    edges = [] if fields[2] == "-" else [int(x) for x in fields[2].split(",")]
                    if len(set(edges)) != len(edges) or any(
                        not 0 <= edge < len(model["arcs"]) for edge in edges
                    ):
                        raise ValueError("duplicate or unknown arc index")
                    if model["round_boundary"] is not None and any(
                        model["arc_rounds"][edge] != model["demands"][index].get("transport_round", 0)
                        for edge in edges
                    ):
                        raise ValueError("arc belongs to another transport round")
                    trees[index] = edges
                elif fields[0] == "METRIC" and len(fields) == 3:
                    if fields[1] not in _COUNTERS or fields[1] in counters:
                        raise ValueError("duplicate or unknown counter")
                    value = int(fields[2])
                    if value < 0:
                        raise ValueError("negative counter")
                    counters[fields[1]] = value
                else:
                    raise ValueError("unknown record")
            except ValueError as error:
                raise EmuFlowError(f"author router malformed record: {line.strip()}: {error}") from error
    if set(trees) != set(range(len(model["demands"]))) or set(counters) != _COUNTERS:
        raise EmuFlowError("author router output coverage is not exact")
    connections = sum(len(d["sinks"]) for d in model["demands"])
    if (counters["connections"] != connections or
            counters["routed_connections"] != connections or
            counters["tree_edges"] != sum(len(tree) for tree in trees.values())):
        raise EmuFlowError("author router counters disagree with route coverage")
    return trees, counters


def _predicted_delay(route, platform, constraints):
    graph = defaultdict(list)
    for edge in route["tree_edges"]:
        graph[edge["from"]].append(edge)
    delays = {route["source"]: 0.0}
    queue = deque([route["source"]])
    while queue:
        node = queue.popleft()
        for edge in graph[node]:
            delays[edge["to"]] = delays[node] + route_link_delay_ns(
                platform, edge["link"], node, edge["to"], constraints
            )
            queue.append(edge["to"])
    return max(delays[sink] for sink in route["sinks"])


def _round_contract(model, capacities, usage, platform, constraints):
    if model["round_boundary"] is None:
        return None
    links = {link.id: link for link in platform.links}
    domains = []
    for key in sorted(capacities):
        capacity = capacities[key]
        link = links[capacity["link"]]
        for round_id in model["active_rounds"]:
            slots = _round_available_slots(constraints["frame_slots"], model["round_boundary"], link.latency_cycles, round_id)
            bits = capacity["capacity_bits"] if link.id in constraints["sll_links"] else link.transport_bits_per_cycle_per_direction * slots
            used = usage[(key, round_id)]
            domains.append({
                "key": key, "link": capacity["link"], "direction": capacity["direction"],
                "transport_round": round_id, "latency_cycles": link.latency_cycles,
                "lanes": link.transport_bits_per_cycle_per_direction,
                "available_slots": slots, "capacity_bits": bits,
                "used_bits": used, "utilization": used / bits,
            })
    return {
        "schema": ROUND_CAPACITY_CONTRACT_SCHEMA,
        "frame_slots": constraints["frame_slots"], "barrier_slots": RUNTIME_BARRIER_SLOTS,
        "source_ready_slot": model["round_boundary"],
        "active_rounds": model["active_rounds"], "domains": domains,
    }


def route_system_author(assignment, platform, constraints, timing_paths=None, *, executable=None,
                        cost_policy=AUTHOR_COST_POLICY):
    if not isinstance(cost_policy, str) or cost_policy not in AUTHOR_COST_METHODS:
        raise ValueError(f"unsupported author routing cost policy: {cost_policy}")
    nodes, model = _prepare_native_model(assignment, platform, constraints, None)
    _validate_single_node_cut_capacity(nodes, model)
    resolved = resolve_native_executable("emuflow_synergistic_author_router", executable)
    digest = hashlib.sha256()
    with Path(resolved).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    with tempfile.TemporaryDirectory(prefix="emuflow-author-route-") as temporary:
        root = Path(temporary)
        _write_native_input(root / "input", len(nodes), model, constraints, NATIVE_ROUTER_PROVIDER)
        completed = subprocess.run(
            [resolved, str(root / "input"), str(root / "output"),
             "--cost-policy", cost_policy],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(f"author router failed with exit code {completed.returncode}: {detail}")
        trees, counters = _parse_author_output(root / "output", model)
    exact = assignment_has_static_exact_contract(assignment)
    _, arcs, capacities = build_directed_graph(platform, constraints, reserve_arrival_and_commit=exact)
    routes, usage, round_usage = [], {key: 0 for key in capacities}, defaultdict(int)
    maximum_hops = 0
    direction_by_link = {}
    half_duplex = {link.id for link in platform.links if link.direction == "half_duplex"}
    for index, demand in enumerate(model["demands"]):
        keys = [model["arc_keys"][edge] for edge in trees[index]]
        route = {**demand, "tree_edges": [{"link": k[0], "from": k[1], "to": k[2]} for k in keys]}
        _, latency, hops = _validate_route_tree(route, arcs)
        maximum_hops = max(maximum_hops, hops)
        route["max_latency_cycles"] = latency
        route["predicted_max_delay_ns"] = _predicted_delay(route, platform, constraints)
        routes.append(route)
        for key in keys:
            capacity_key = arcs[key]["capacity_key"]
            usage[capacity_key] += demand["width_bits"]
            round_usage[(capacity_key, demand.get("transport_round", 0))] += demand["width_bits"]
            if key[0] in half_duplex:
                direction = (key[1], key[2])
                if direction_by_link.setdefault(key[0], direction) != direction:
                    raise ValidationError("author routes violate half-duplex direction lock")
    utilization = _link_utilization_records(capacities, usage)
    locks = [{"group": group, "link": link.id,
              "from": direction_by_link.get(link.id, link.endpoints)[0],
              "to": direction_by_link.get(link.id, link.endpoints)[1]}
             for group, link in enumerate(sorted((link for link in platform.links if link.id in half_duplex), key=lambda link: link.id))]
    result = {
        "schema": "emuflow.system-routes/v1", "provider": AUTHOR_ROUTER_PROVIDER,
        "design": assignment.get("design"), "platform": platform.name,
        "constraints": dict(constraints), "demands": model["demands"], "routes": routes,
        "link_utilization": utilization, "direction_locks": locks,
        "author_routing": {
            "method": AUTHOR_COST_METHODS[cost_policy], "output_protocol": AUTHOR_OUTPUT_PROTOCOL,
            "cost_policy": cost_policy,
            "executable_sha256": digest.hexdigest(), "counters": counters,
            "timing_optimization": False, "tdm_assignment": "downstream-phase5",
        },
        "metrics": {
            "demands": len(routes), "routed_sinks": sum(len(route["sinks"]) for route in routes),
            "tree_edges": sum(len(route["tree_edges"]) for route in routes),
            "max_link_utilization": max((record["utilization"] for record in utilization), default=0.0),
            "total_link_bit_hops": sum(usage.values()),
            **({"max_route_hops_observed": maximum_hops} if constraints.get("max_route_hops") is not None else {}),
        },
    }
    if exact:
        result["semantic_contract_schema"] = assignment.get("semantic_contract_schema") or assignment["semantic_contract"]["schema"]
        result["semantic_contract_sha256"] = assignment["semantic_contract_sha256"]
    contract = _round_contract(model, capacities, round_usage, platform, constraints)
    if contract is not None:
        result["round_capacity_contract"] = contract
    if timing_paths is not None:
        evaluation = reconstruct_system_route_timing(assignment, platform, result, timing_paths, include_records=True)
        result["timing"] = {"schema": STA_PATHS_SCHEMA, "normalization": timing_paths["normalization"],
                            "compression": timing_paths["compression"], "paths": evaluation.pop("_timing_records"),
                            **_output_realization_metadata(timing_paths)}
        result["metrics"].update({key: evaluation[key] for key in _TIMING_METRICS})
    return result


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("author routes contain nonfinite numeric data")
    if isinstance(value, Mapping):
        for item in value.values():
            _finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _finite(item)


def validate_author_system_routes(assignment, platform, routes, timing_paths=None):
    _finite(routes)
    if routes.get("provider") != AUTHOR_ROUTER_PROVIDER:
        raise ValidationError("invalid author routing provider")
    if routes.get("design") != assignment.get("design") or routes.get("platform") != platform.name:
        raise ValidationError("author route design/platform identity mismatch")
    validation = validate_system_routes(assignment, platform, routes)
    demand_by_id = {demand["id"]: demand for demand in routes["demands"]}
    for route in routes["routes"]:
        if any(route.get(key) != value for key, value in demand_by_id[route["id"]].items()):
            raise ValidationError("author route lost demand/dependency fields")
    metadata = routes.get("author_routing", {})
    if not isinstance(metadata, dict):
        raise ValidationError("invalid author routing execution metadata")
    counters = metadata.get("counters", {})
    policy = metadata.get("cost_policy")
    expected_method = AUTHOR_COST_METHODS.get(policy) if isinstance(policy, str) else None
    if (expected_method is None or metadata.get("method") != expected_method
            or metadata.get("output_protocol") != AUTHOR_OUTPUT_PROTOCOL
            or metadata.get("timing_optimization") is not False or metadata.get("tdm_assignment") != "downstream-phase5"
            or not isinstance(metadata.get("executable_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["executable_sha256"])
            or not isinstance(counters, dict) or set(counters) != _COUNTERS
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters.values())
            or counters["connections"] != validation["routed_sinks"]
            or counters["routed_connections"] != validation["routed_sinks"]
            or counters["tree_edges"] != validation["tree_edges"]):
        raise ValidationError("invalid author routing execution metadata")
    constraints = normalize_route_constraints(routes.get("constraints"), platform)
    round_validation = _validate_round_capacity_contract(routes, platform, constraints, exact_dependency_schedule=assignment_has_static_exact_contract(assignment))
    expected_locks = sorted((link for link in platform.links if link.direction == "half_duplex"), key=lambda link: link.id)
    locks = routes.get("direction_locks")
    if not isinstance(locks, list) or len(locks) != len(expected_locks):
        raise ValidationError("author direction-lock coverage is not exact")
    direction_by_link = {}
    for index, (lock, link) in enumerate(zip(locks, expected_locks)):
        if (not isinstance(lock, dict) or lock.get("group") != index or lock.get("link") != link.id
                or {lock.get("from"), lock.get("to")} != set(link.endpoints)):
            raise ValidationError("invalid author direction lock")
        direction_by_link[link.id] = (lock["from"], lock["to"])
    for route in routes["routes"]:
        for edge in route["tree_edges"]:
            if edge["link"] in direction_by_link and direction_by_link[edge["link"]] != (edge["from"], edge["to"]):
                raise ValidationError("author route violates direction lock")
        expected = _predicted_delay(route, platform, constraints)
        actual = route.get("predicted_max_delay_ns")
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or abs(actual - expected) > 1e-9:
            raise ValidationError("author predicted delay does not match platform")
    if timing_paths is None:
        if "timing" in routes:
            raise ValidationError("author timing records require normalized STA input")
    else:
        checked = reconstruct_system_route_timing(assignment, platform, routes, timing_paths, include_records=True)
        expected_timing = {"schema": STA_PATHS_SCHEMA, "normalization": timing_paths["normalization"],
                           "compression": timing_paths["compression"], "paths": checked.pop("_timing_records"),
                           **_output_realization_metadata(timing_paths)}
        if routes.get("timing") != expected_timing:
            raise ValidationError("author timing records do not match independent reconstruction")
        if any(routes["metrics"].get(key) != checked[key] for key in _TIMING_METRICS):
            raise ValidationError("author timing metrics do not match independent reconstruction")
        validation.update({key: checked[key] for key in _TIMING_METRICS})
    return {**validation, **round_validation, "provider": AUTHOR_ROUTER_PROVIDER,
            "predicted_delay_check": "pass", "direction_locks": len(locks)}
