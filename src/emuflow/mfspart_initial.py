"""MFSPart delayed candidate propagation and two-phase initialization.

The native executable carries the performance path.  This module writes its
versioned protocol and independently replays Eqs. 5--8 as a correctness oracle.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import math
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .mfspart import MFSPART_HIERARCHY_SCHEMA, _splitmix64
from .native_tools import resolve_native_executable


MFSPART_INITIAL_INPUT_SCHEMA = "emuflow.mfspart-initial-input/v1"
MFSPART_INITIAL_SCHEMA = "emuflow.mfspart-initial-partition/v1"
MFSPART_INITIAL_PROVIDER = "mfspart-paper-two-phase-initialization-v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_problem(
    hierarchy: Mapping[str, Any],
    parts: Sequence[str],
    distances: Mapping[str, Mapping[str, int]],
    capacities: Mapping[str, Mapping[str, int]],
    part_degrees: Mapping[str, float],
    *,
    hmax: int,
    seed: int,
    theta: float,
    eta: float,
    violation_lambda: float,
    mu: float,
    temperature: float,
) -> Dict[str, Any]:
    if hierarchy.get("schema") != MFSPART_HIERARCHY_SCHEMA:
        raise ValidationError("invalid MFSPart hierarchy input")
    if not parts or len(set(parts)) != len(parts):
        raise ValidationError("MFSPart FPGA ids must be non-empty and unique")
    dimensions = hierarchy.get("dimensions")
    levels = hierarchy.get("levels")
    if not isinstance(dimensions, list) or not dimensions or not isinstance(levels, list) or not levels:
        raise ValidationError("MFSPart hierarchy is incomplete")
    if hmax < 1 or seed < 0 or seed >= 1 << 64:
        raise ValidationError("invalid MFSPart initialization hmax or seed")
    parameters = (theta, eta, violation_lambda, mu, temperature)
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in parameters) or any(value < 0 for value in parameters[:-1]) or temperature <= 0:
        raise ValidationError("invalid MFSPart initialization score parameter")
    distance_matrix = []
    capacity_matrix = []
    degrees = []
    for source in parts:
        row = []
        for target in parts:
            value = distances.get(source, {}).get(target)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValidationError("MFSPart FPGA distance matrix is incomplete")
            row.append(value)
        distance_matrix.append(row)
        cap_row = []
        for dimension in dimensions:
            value = capacities.get(source, {}).get(dimension)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValidationError("MFSPart FPGA capacity matrix is incomplete")
            cap_row.append(value)
        capacity_matrix.append(cap_row)
        degree = part_degrees.get(source)
        if not isinstance(degree, (int, float)) or not math.isfinite(degree) or degree < 0:
            raise ValidationError("invalid MFSPart FPGA degree")
        degrees.append(float(degree))
    for left in range(len(parts)):
        if distance_matrix[left][left] != 0:
            raise ValidationError("MFSPart FPGA self-distance must be zero")
        for right in range(len(parts)):
            if distance_matrix[left][right] != distance_matrix[right][left]:
                raise ValidationError("paper-mode FPGA distances must be symmetric")
    graph = levels[-1]
    for node in graph["nodes"]:
        fixed_part = node["fixed_part"]
        if fixed_part >= len(parts):
            raise ValidationError("coarsest node has invalid fixed_part")
    return {
        "schema": MFSPART_INITIAL_INPUT_SCHEMA,
        "provider": MFSPART_INITIAL_PROVIDER,
        "parts": list(parts),
        "dimensions": list(dimensions),
        "distances": distance_matrix,
        "capacities": capacity_matrix,
        "part_degrees": degrees,
        "graph": graph,
        "hmax": hmax,
        "seed": seed,
        "theta": float(theta),
        "eta": float(eta),
        "lambda": float(violation_lambda),
        "mu": float(mu),
        "temperature": float(temperature),
    }


def _write_native_input(path: Path, problem: Mapping[str, Any]) -> None:
    graph = problem["graph"]
    lines = [
        "EMUFLOW_MFSPART_INITIALIZER_INPUT_V1",
        "PARAM "
        + " ".join(
            str(value)
            for value in (
                len(problem["parts"]),
                len(graph["nodes"]),
                len(problem["dimensions"]),
                len(graph["nets"]),
                problem["hmax"],
                problem["seed"],
                format(problem["theta"], ".17g"),
                format(problem["eta"], ".17g"),
                format(problem["lambda"], ".17g"),
                format(problem["mu"], ".17g"),
                format(problem["temperature"], ".17g"),
            )
        ),
    ]
    for source, row in enumerate(problem["distances"]):
        for target, distance in enumerate(row):
            lines.append(f"DIST {source} {target} {distance}")
    for part, row in enumerate(problem["capacities"]):
        for dimension, capacity in enumerate(row):
            lines.append(f"CAP {part} {dimension} {capacity}")
    lines.extend(
        f"DEG {part} {format(degree, '.17g')}"
        for part, degree in enumerate(problem["part_degrees"])
    )
    for index, node in enumerate(graph["nodes"]):
        lines.append(
            "NODE "
            + " ".join(
                str(value)
                for value in (index, node["fixed_part"], *node["weights"])
            )
        )
    for index, net in enumerate(graph["nets"]):
        lines.append(
            "NET "
            + " ".join(
                str(value)
                for value in (
                    index,
                    format(net["weight"], ".17g"),
                    net["source"],
                    len(net["sinks"]),
                    *net["sinks"],
                )
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_native_output(path: Path, node_count: int) -> Dict[str, Any]:
    if not path.is_file():
        raise EmuFlowError("MFSPart initializer produced no output")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_MFSPART_INITIALIZER_OUTPUT_V1":
        raise ValidationError("invalid MFSPart initializer output header")
    status = None
    candidates: Dict[int, List[int]] = {}
    assignments = []
    domain_trace = []
    assigned_nodes = set()
    metrics: Dict[str, float] = {}
    for line in lines[1:]:
        fields = line.split()
        try:
            if fields[0] == "STATUS" and len(fields) == 2:
                if status is not None:
                    raise ValidationError("duplicate MFSPart initializer status")
                status = fields[1]
            elif fields[0] == "CAND" and len(fields) >= 3:
                node, count = map(int, fields[1:3])
                values = list(map(int, fields[3:]))
                if node in candidates or count != len(values):
                    raise ValidationError("invalid MFSPart candidate record")
                candidates[node] = values
            elif fields[0] == "ASSIGN" and len(fields) == 5:
                node, part, phase = map(int, fields[1:4])
                if node in assigned_nodes:
                    raise ValidationError("duplicate MFSPart assignment")
                assigned_nodes.add(node)
                assignments.append(
                    {
                        "node": node,
                        "part": part,
                        "phase": phase,
                        "score": float(fields[4]),
                    }
                )
            elif fields[0] == "DOMAIN" and len(fields) >= 4:
                step, node, count = map(int, fields[1:4])
                parts = list(map(int, fields[4:]))
                if count != len(parts):
                    raise ValidationError("invalid MFSPart domain record")
                domain_trace.append(
                    {"assignment_step": step, "node": node, "parts": parts}
                )
            elif fields[0] == "METRIC" and len(fields) == 3:
                if fields[1] in metrics:
                    raise ValidationError("duplicate MFSPart metric")
                metrics[fields[1]] = float(fields[2])
            else:
                raise ValidationError(f"invalid MFSPart initializer output record {line!r}")
        except (ValueError, IndexError) as error:
            raise ValidationError(f"malformed MFSPart initializer record {line!r}") from error
    if status != "PASS" or set(candidates) != set(range(node_count)) or assigned_nodes != set(range(node_count)):
        raise ValidationError("incomplete MFSPart initializer output")
    return {
        "candidates": [candidates[index] for index in range(node_count)],
        "assignments": assignments,
        "domain_trace": domain_trace,
        "metrics": metrics,
    }


def _adjacency(problem: Mapping[str, Any]):
    adjacency = [[] for _ in problem["graph"]["nodes"]]
    for net in problem["graph"]["nets"]:
        for sink in net["sinks"]:
            adjacency[net["source"]].append((sink, net["weight"]))
            adjacency[sink].append((net["source"], net["weight"]))
    return adjacency


def _sequential_float_sum(values: Iterable[float]) -> float:
    """Match the native protocol's ordered IEEE-754 accumulation."""

    total = 0.0
    for value in values:
        total += value
    return total


def _propagate(problem: Mapping[str, Any], adjacency, assigned: Sequence[int]):
    part_count = len(problem["parts"])
    candidates = [([part] if part >= 0 else list(range(part_count))) for part in assigned]
    queue = deque(node for node, part in enumerate(assigned) if part >= 0)
    queued = {node for node, part in enumerate(assigned) if part >= 0}
    while queue:
        anchor = queue.popleft()
        if not candidates[anchor]:
            continue
        anchor_part = candidates[anchor][0]
        maximum = max(problem["distances"][anchor_part])
        circuit_distance = [-1] * len(assigned)
        circuit_distance[anchor] = 0
        bfs = deque([anchor])
        while bfs:
            node = bfs.popleft()
            if (circuit_distance[node] + 1) * problem["hmax"] >= maximum:
                continue
            for neighbor, _ in adjacency[node]:
                if circuit_distance[neighbor] < 0:
                    circuit_distance[neighbor] = circuit_distance[node] + 1
                    bfs.append(neighbor)
        for node, distance in enumerate(circuit_distance):
            if node == anchor or distance < 0 or distance * problem["hmax"] >= maximum:
                continue
            filtered = [part for part in candidates[node] if problem["distances"][anchor_part][part] <= distance * problem["hmax"]]
            if filtered != candidates[node]:
                candidates[node] = filtered
                if len(filtered) == 1 and node not in queued:
                    queue.append(node)
                    queued.add(node)
    return candidates


def _propagate_new_anchors(problem, adjacency, candidates, processed, anchors) -> List[int]:
    queue = deque()
    queued = set()
    changed = set()
    for anchor in anchors:
        if not processed[anchor] and anchor not in queued:
            queue.append(anchor)
            queued.add(anchor)
    while queue:
        anchor = queue.popleft()
        queued.discard(anchor)
        if processed[anchor] or len(candidates[anchor]) != 1:
            continue
        processed[anchor] = True
        anchor_part = candidates[anchor][0]
        maximum = max(problem["distances"][anchor_part])
        if maximum <= problem["hmax"]:
            continue
        circuit_distance = [-1] * len(candidates)
        circuit_distance[anchor] = 0
        bfs = deque([anchor])
        reached = [anchor]
        while bfs:
            node = bfs.popleft()
            if (circuit_distance[node] + 1) * problem["hmax"] >= maximum:
                continue
            for neighbor, _ in adjacency[node]:
                if circuit_distance[neighbor] < 0:
                    circuit_distance[neighbor] = circuit_distance[node] + 1
                    bfs.append(neighbor)
                    reached.append(neighbor)
        for node in reached:
            distance = circuit_distance[node]
            if node == anchor or distance * problem["hmax"] >= maximum:
                continue
            filtered = [
                part
                for part in candidates[node]
                if problem["distances"][anchor_part][part]
                <= distance * problem["hmax"]
            ]
            if filtered != candidates[node]:
                candidates[node] = filtered
                changed.add(node)
                if len(filtered) == 1 and not processed[node] and node not in queued:
                    queue.append(node)
                    queued.add(node)
    return sorted(changed)


def _initial_propagation(problem, adjacency, assigned):
    part_count = len(problem["parts"])
    candidates = [
        ([part] if part >= 0 else list(range(part_count))) for part in assigned
    ]
    processed = [False] * len(assigned)
    _propagate_new_anchors(
        problem,
        adjacency,
        candidates,
        processed,
        [node for node, part in enumerate(assigned) if part >= 0],
    )
    return candidates, processed


def _trial_empties_domain(
    problem,
    adjacency,
    candidate_masks,
    processed,
    allowed_masks,
    selected_node: int,
    selected_part: int,
) -> bool:
    overlay = {selected_node: 1 << selected_part}
    queue = deque([selected_node])
    queued = {selected_node}
    trial_processed = set()

    def candidate_mask(node: int):
        return overlay.get(node, candidate_masks[node])

    while queue:
        anchor = queue.popleft()
        queued.discard(anchor)
        anchor_candidates = candidate_mask(anchor)
        if (
            processed[anchor]
            or anchor in trial_processed
            or anchor_candidates == 0
            or anchor_candidates & (anchor_candidates - 1)
        ):
            continue
        trial_processed.add(anchor)
        anchor_part = anchor_candidates.bit_length() - 1
        maximum = max(problem["distances"][anchor_part])
        if maximum <= problem["hmax"]:
            continue
        distance = {anchor: 0}
        bfs = deque([anchor])
        while bfs:
            node = bfs.popleft()
            if (distance[node] + 1) * problem["hmax"] >= maximum:
                continue
            for neighbor, _ in adjacency[node]:
                if neighbor not in distance:
                    distance[neighbor] = distance[node] + 1
                    bfs.append(neighbor)
        for node, circuit_distance in distance.items():
            if (
                node == anchor
                or circuit_distance * problem["hmax"] >= maximum
            ):
                continue
            filtered = candidate_mask(node) & allowed_masks[anchor_part][circuit_distance]
            if filtered == 0:
                return True
            if filtered != candidate_mask(node):
                overlay[node] = filtered
                if (
                    filtered & (filtered - 1) == 0
                    and not processed[node]
                    and node not in trial_processed
                    and node not in queued
                ):
                    queue.append(node)
                    queued.add(node)
    return False


def _fits(problem, loads, node: int, part: int) -> bool:
    weights = problem["graph"]["nodes"][node]["weights"]
    return all(loads[part][dimension] + weight <= problem["capacities"][part][dimension] for dimension, weight in enumerate(weights))


def _score(problem, adjacency, assigned, loads, node: int, part: int, penalty: bool) -> float:
    connected = 0.0
    violation = 0.0
    for neighbor, weight in adjacency[node]:
        if assigned[neighbor] == part:
            connected += weight
        if penalty and assigned[neighbor] >= 0:
            distance = problem["distances"][assigned[neighbor]][part]
            if distance > problem["hmax"]:
                violation += weight * (1.0 + problem["mu"] * (distance - problem["hmax"]))
    remaining = problem["capacities"][part][0] - loads[part][0] - problem["graph"]["nodes"][node]["weights"][0]
    return connected - problem["theta"] / max(1.0, float(remaining)) + problem["eta"] * problem["part_degrees"][part] - problem["lambda"] * violation


def _assign(problem, assigned, loads, node: int, part: int) -> None:
    assigned[node] = part
    for dimension, weight in enumerate(problem["graph"]["nodes"][node]["weights"]):
        loads[part][dimension] += weight


def _partition_metrics(problem: Mapping[str, Any], assigned: Sequence[int]) -> Dict[str, float]:
    cut = 0.0
    connectivity = 0.0
    violating = 0
    weighted_hops = 0.0
    total_pair_weight = 0.0
    loads = [[0] * len(problem["dimensions"]) for _ in problem["parts"]]
    fixed_violations = 0
    for node, part in enumerate(assigned):
        fixed = problem["graph"]["nodes"][node]["fixed_part"]
        fixed_violations += int(fixed >= 0 and fixed != part)
        for dimension, weight in enumerate(problem["graph"]["nodes"][node]["weights"]):
            loads[part][dimension] += weight
    capacity_violations = sum(
        loads[part][dimension] > problem["capacities"][part][dimension]
        for part in range(len(problem["parts"]))
        for dimension in range(len(problem["dimensions"]))
    )
    for net in problem["graph"]["nets"]:
        source_part = assigned[net["source"]]
        remote_sink_parts = set()
        for sink in net["sinks"]:
            sink_part = assigned[sink]
            distance = problem["distances"][source_part][sink_part]
            if source_part != sink_part:
                cut += net["weight"]
                remote_sink_parts.add(sink_part)
            violating += int(distance > problem["hmax"])
            weighted_hops += net["weight"] * distance
            total_pair_weight += net["weight"]
        connectivity += net["weight"] * len(remote_sink_parts)
    return {
        "driver_sink_cut": cut,
        "connectivity": connectivity,
        "violating_pairs": float(violating),
        "weighted_hops": weighted_hops,
        "mean_hops": weighted_hops / total_pair_weight if total_pair_weight else 0.0,
        "capacity_violations": float(capacity_violations),
        "fixed_violations": float(fixed_violations),
    }


def _expected_exhaustive(problem: Mapping[str, Any]) -> Tuple[List[List[int]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, float]]:
    graph = problem["graph"]
    adjacency = _adjacency(problem)
    assigned = [-1] * len(graph["nodes"])
    loads = [[0] * len(problem["dimensions"]) for _ in problem["parts"]]
    records = []
    domain_trace = []
    fixed = [index for index, node in enumerate(graph["nodes"]) if node["fixed_part"] >= 0]
    if fixed:
        for node in fixed:
            part = graph["nodes"][node]["fixed_part"]
            if not _fits(problem, loads, node, part):
                raise ValidationError("fixed nodes exceed FPGA capacity")
            _assign(problem, assigned, loads, node, part)
            records.append({"node": node, "part": part, "phase": 0, "score": 0.0})
    else:
        normalized = []
        for node in range(len(graph["nodes"])):
            degree = _sequential_float_sum(
                weight for _, weight in adjacency[node]
            )
            normalized.append(degree / graph["nodes"][node]["weights"][0])
        node = max(range(len(graph["nodes"])), key=lambda item: (normalized[item], -item))
        feasible = [part for part in range(len(problem["parts"])) if _fits(problem, loads, node, part)]
        if not feasible:
            raise ValidationError("no FPGA can hold propagation start node")
        part = max(feasible, key=lambda item: (problem["part_degrees"][item], -item))
        _assign(problem, assigned, loads, node, part)
        records.append({"node": node, "part": part, "phase": 0, "score": 0.0})
    initial_candidates = _propagate(problem, adjacency, assigned)
    phase_two = [False] * len(assigned)
    event = 0
    while True:
        candidates = _propagate(problem, adjacency, assigned)
        selectable = []
        for node in range(len(assigned)):
            if assigned[node] >= 0 or not candidates[node] or phase_two[node]:
                continue
            total = _sequential_float_sum(
                weight for _, weight in adjacency[node]
            )
            connected = _sequential_float_sum(
                weight
                for neighbor, weight in adjacency[node]
                if assigned[neighbor] >= 0
            )
            selectable.append((connected / total if total else 0.0, -node, node))
        if not selectable:
            break
        node = max(selectable)[2]
        choices = []
        for part in candidates[node]:
            if not _fits(problem, loads, node, part):
                continue
            trial = list(assigned)
            trial[node] = part
            if any(not values for values in _propagate(problem, adjacency, trial)):
                continue
            choices.append((part, _score(problem, adjacency, assigned, loads, node, part, False)))
        if not choices:
            phase_two[node] = True
            continue
        maximum = max(value for _, value in choices)
        weights = [math.exp((value - maximum) / problem["temperature"]) for _, value in choices]
        unit = ((_splitmix64(problem["seed"] ^ _splitmix64(event + 1)) >> 11) * (1.0 / 9007199254740992.0))
        event += 1
        draw = unit * _sequential_float_sum(weights)
        selected_part, selected_score = choices[-1]
        for (part, value), weight in zip(choices, weights):
            draw -= weight
            if draw <= 0:
                selected_part, selected_score = part, value
                break
        _assign(problem, assigned, loads, node, selected_part)
        records.append({"node": node, "part": selected_part, "phase": 1, "score": selected_score})
        updated_candidates = _propagate(problem, adjacency, assigned)
        for changed_node in range(len(assigned)):
            if len(updated_candidates[changed_node]) < len(candidates[changed_node]):
                domain_trace.append(
                    {
                        "assignment_step": len(records) - 1,
                        "node": changed_node,
                        "parts": updated_candidates[changed_node],
                    }
                )
    while any(part < 0 for part in assigned):
        choices = []
        for node in range(len(assigned)):
            if assigned[node] >= 0:
                continue
            for part in range(len(problem["parts"])):
                if _fits(problem, loads, node, part):
                    choices.append((_score(problem, adjacency, assigned, loads, node, part, True), -node, -part, node, part))
        if not choices:
            raise ValidationError("initial partition cannot satisfy capacity")
        value, _, _, node, part = max(choices)
        _assign(problem, assigned, loads, node, part)
        records.append({"node": node, "part": part, "phase": 2, "score": value})
    return initial_candidates, records, domain_trace, _partition_metrics(problem, assigned)


def _expected(problem: Mapping[str, Any]) -> Tuple[List[List[int]], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, float]]:
    """Replay initialization with monotonic candidate-domain certificates."""

    graph = problem["graph"]
    adjacency = _adjacency(problem)
    assigned = [-1] * len(graph["nodes"])
    loads = [[0] * len(problem["dimensions"]) for _ in problem["parts"]]
    records = []
    domain_trace = []
    fixed = [
        index for index, node in enumerate(graph["nodes"])
        if node["fixed_part"] >= 0
    ]
    if fixed:
        for node in fixed:
            part = graph["nodes"][node]["fixed_part"]
            if not _fits(problem, loads, node, part):
                raise ValidationError("fixed nodes exceed FPGA capacity")
            _assign(problem, assigned, loads, node, part)
            records.append({"node": node, "part": part, "phase": 0, "score": 0.0})
    else:
        normalized = []
        for node in range(len(graph["nodes"])):
            degree = _sequential_float_sum(
                weight for _, weight in adjacency[node]
            )
            normalized.append(degree / graph["nodes"][node]["weights"][0])
        node = max(
            range(len(graph["nodes"])),
            key=lambda item: (normalized[item], -item),
        )
        feasible = [
            part for part in range(len(problem["parts"]))
            if _fits(problem, loads, node, part)
        ]
        if not feasible:
            raise ValidationError("no FPGA can hold propagation start node")
        part = max(
            feasible, key=lambda item: (problem["part_degrees"][item], -item)
        )
        _assign(problem, assigned, loads, node, part)
        records.append({"node": node, "part": part, "phase": 0, "score": 0.0})

    candidates, processed = _initial_propagation(problem, adjacency, assigned)
    initial_candidates = [list(parts) for parts in candidates]
    candidate_masks = [sum(1 << part for part in parts) for parts in candidates]
    maximum_circuit_distance = max(
        (max(row) + problem["hmax"] - 1) // problem["hmax"]
        for row in problem["distances"]
    )
    allowed_masks = []
    for row in problem["distances"]:
        allowed_masks.append(
            [
                sum(
                    1 << part
                    for part, distance in enumerate(row)
                    if distance <= circuit_distance * problem["hmax"]
                )
                for circuit_distance in range(maximum_circuit_distance + 1)
            ]
        )
    phase_two = [False] * len(assigned)
    event = 0
    priority_versions = [0] * len(assigned)
    priority_queue = []

    def refresh_priority(node: int) -> None:
        priority_versions[node] += 1
        if assigned[node] >= 0 or phase_two[node] or not candidates[node]:
            return
        total = _sequential_float_sum(
            weight for _, weight in adjacency[node]
        )
        connected = _sequential_float_sum(
            weight
            for neighbor, weight in adjacency[node]
            if assigned[neighbor] >= 0
        )
        priority = connected / total if total else 0.0
        heapq.heappush(
            priority_queue, (-priority, node, priority_versions[node])
        )

    for node in range(len(assigned)):
        refresh_priority(node)
    while True:
        while (
            priority_queue
            and priority_queue[0][2] != priority_versions[priority_queue[0][1]]
        ):
            heapq.heappop(priority_queue)
        if not priority_queue:
            break
        _, node, _ = heapq.heappop(priority_queue)
        choices = []
        for part in candidates[node]:
            if not _fits(problem, loads, node, part):
                continue
            if max(problem["distances"][part]) > problem["hmax"]:
                if _trial_empties_domain(
                    problem,
                    adjacency,
                    candidate_masks,
                    processed,
                    allowed_masks,
                    node,
                    part,
                ):
                    continue
            choices.append(
                (part, _score(problem, adjacency, assigned, loads, node, part, False))
            )
        if not choices:
            phase_two[node] = True
            priority_versions[node] += 1
            continue
        maximum = max(value for _, value in choices)
        weights = [
            math.exp((value - maximum) / problem["temperature"])
            for _, value in choices
        ]
        unit = (
            (_splitmix64(problem["seed"] ^ _splitmix64(event + 1)) >> 11)
            * (1.0 / 9007199254740992.0)
        )
        event += 1
        draw = unit * _sequential_float_sum(weights)
        selected_part, selected_score = choices[-1]
        for (part, value), weight in zip(choices, weights):
            draw -= weight
            if draw <= 0:
                selected_part, selected_score = part, value
                break
        changed_nodes = set()
        if candidates[node] != [selected_part]:
            changed_nodes.add(node)
        _assign(problem, assigned, loads, node, selected_part)
        priority_versions[node] += 1
        records.append(
            {
                "node": node,
                "part": selected_part,
                "phase": 1,
                "score": selected_score,
            }
        )
        candidates[node] = [selected_part]
        processed[node] = False
        changed_nodes.update(
            _propagate_new_anchors(
                problem, adjacency, candidates, processed, [node]
            )
        )
        for changed_node in changed_nodes:
            candidate_masks[changed_node] = sum(
                1 << part for part in candidates[changed_node]
            )
        for changed_node in sorted(changed_nodes):
            domain_trace.append(
                {
                    "assignment_step": len(records) - 1,
                    "node": changed_node,
                    "parts": list(candidates[changed_node]),
                }
            )
            if changed_node != node:
                refresh_priority(changed_node)
        for neighbor, _ in adjacency[node]:
            if assigned[neighbor] < 0:
                refresh_priority(neighbor)

    while any(part < 0 for part in assigned):
        choices = []
        for node in range(len(assigned)):
            if assigned[node] >= 0:
                continue
            for part in range(len(problem["parts"])):
                if _fits(problem, loads, node, part):
                    choices.append(
                        (
                            _score(problem, adjacency, assigned, loads, node, part, True),
                            -node,
                            -part,
                            node,
                            part,
                        )
                    )
        if not choices:
            raise ValidationError("initial partition cannot satisfy capacity")
        value, _, _, node, part = max(choices)
        _assign(problem, assigned, loads, node, part)
        records.append({"node": node, "part": part, "phase": 2, "score": value})
    return initial_candidates, records, domain_trace, _partition_metrics(problem, assigned)


def validate_mfspart_initial_partition(artifact: Mapping[str, Any], problem: Mapping[str, Any]) -> Dict[str, Any]:
    if artifact.get("schema") != MFSPART_INITIAL_SCHEMA:
        raise ValidationError("invalid MFSPart initial partition schema")
    expected_candidates, expected_records, expected_domains, expected_metrics = _expected(problem)
    if artifact.get("candidate_parts") != expected_candidates:
        raise ValidationError("MFSPart delayed propagation candidate mismatch")
    actual_records = artifact.get("assignment_trace")
    if not isinstance(actual_records, list) or len(actual_records) != len(expected_records):
        raise ValidationError("MFSPart initial assignment trace mismatch")
    for expected, actual in zip(expected_records, actual_records):
        if (actual.get("node"), actual.get("part"), actual.get("phase")) != (expected["node"], expected["part"], expected["phase"]) or not math.isclose(actual.get("score"), expected["score"], rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError("MFSPart initial assignment replay mismatch")
    if artifact.get("domain_trace") != expected_domains:
        raise ValidationError("MFSPart candidate-domain contraction trace mismatch")
    metrics = artifact.get("metrics", {})
    for name, expected in expected_metrics.items():
        if name not in metrics or not math.isclose(metrics[name], expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError(f"MFSPart initial metric mismatch for {name}")
    phase_counts = {phase: sum(record["phase"] == phase for record in actual_records) for phase in (0, 1, 2)}
    return {"status": "pass", "nodes": len(actual_records), "phase_counts": phase_counts, "violating_pairs": int(metrics["violating_pairs"])}


def exhaustively_enumerate_mfspart_assignments(
    problem: Mapping[str, Any], *, max_assignments: int = 1_000_000
) -> Dict[str, Any]:
    """Enumerate every compact assignment as an independent small-graph oracle."""

    nodes = problem["graph"]["nodes"]
    movable = [index for index, node in enumerate(nodes) if node["fixed_part"] < 0]
    search_size = len(problem["parts"]) ** len(movable)
    if search_size > max_assignments:
        raise ValidationError(
            f"exhaustive MFSPart oracle requires {search_size} assignments; "
            f"limit is {max_assignments}"
        )
    feasible = 0
    topology_feasible = 0
    best = None
    best_assignment = None
    for values in itertools.product(range(len(problem["parts"])), repeat=len(movable)):
        assignment = [node["fixed_part"] for node in nodes]
        for node, part in zip(movable, values):
            assignment[node] = part
        loads = [[0] * len(problem["dimensions"]) for _ in problem["parts"]]
        legal_capacity = True
        for node, part in enumerate(assignment):
            for dimension, weight in enumerate(nodes[node]["weights"]):
                loads[part][dimension] += weight
                if loads[part][dimension] > problem["capacities"][part][dimension]:
                    legal_capacity = False
        if not legal_capacity:
            continue
        feasible += 1
        metrics = _partition_metrics(problem, assignment)
        if metrics["violating_pairs"] == 0:
            topology_feasible += 1
        objective = (
            metrics["violating_pairs"],
            metrics["driver_sink_cut"],
            metrics["connectivity"],
            metrics["mean_hops"],
            tuple(assignment),
        )
        if best is None or objective < best:
            best = objective
            best_assignment = list(assignment)
    return {
        "status": "pass",
        "enumerated_assignments": search_size,
        "capacity_feasible_assignments": feasible,
        "topology_feasible_assignments": topology_feasible,
        "best_assignment": best_assignment,
        "best_objective": list(best[:-1]) if best is not None else None,
    }


def build_mfspart_initial_partition(
    hierarchy: Mapping[str, Any],
    parts: Sequence[str],
    distances: Mapping[str, Mapping[str, int]],
    capacities: Mapping[str, Mapping[str, int]],
    part_degrees: Mapping[str, float],
    output_dir: Path,
    *,
    hmax: int,
    seed: int = 0,
    theta: float = 1.0,
    eta: float = 1.0,
    violation_lambda: float = 1.0,
    mu: float = 1.0,
    temperature: float = 1.0,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    problem = _normalise_problem(hierarchy, parts, distances, capacities, part_degrees, hmax=hmax, seed=seed, theta=theta, eta=eta, violation_lambda=violation_lambda, mu=mu, temperature=temperature)
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / "mfspart_initializer.in"
    output_path = output_dir / "mfspart_initializer.out"
    log_path = output_dir / "mfspart_initializer.log"
    _write_native_input(input_path, problem)
    command = resolve_native_executable("emuflow_mfspart_initializer", executable)
    completed = subprocess.run([command, str(input_path.resolve()), str(output_path.resolve())], cwd=output_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise EmuFlowError(f"MFSPart initializer failed with exit code {completed.returncode}: {completed.stdout[-2000:]}")
    parsed = _parse_native_output(output_path, len(problem["graph"]["nodes"]))
    artifact = {
        "schema": MFSPART_INITIAL_SCHEMA,
        "provider": MFSPART_INITIAL_PROVIDER,
        "claim_scope": "independent paper-level delayed propagation and Eqs. 5--8 reproduction",
        "parts": list(parts),
        "candidate_parts": parsed["candidates"],
        "assignment_trace": parsed["assignments"],
        "domain_trace": parsed["domain_trace"],
        "assignment": {},
        "metrics": parsed["metrics"],
        "artifacts": {"input_sha256": _sha256(input_path), "output_sha256": _sha256(output_path)},
    }
    artifact["assignment"] = {record["node"]: record["part"] for record in artifact["assignment_trace"]}
    artifact["validation"] = validate_mfspart_initial_partition(artifact, problem)
    return artifact
