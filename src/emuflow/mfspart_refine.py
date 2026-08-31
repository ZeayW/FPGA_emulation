"""MFSPart direct k-way FM uncoarsening and independent replay oracle."""

from __future__ import annotations

import hashlib
import heapq
import math
import subprocess
import time
from bisect import bisect_right
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import EmuFlowError, ValidationError
from .mfspart import MFSPART_HIERARCHY_SCHEMA
from .mfspart_initial import MFSPART_INITIAL_SCHEMA, _partition_metrics
from .native_tools import resolve_native_executable


MFSPART_REFINER_INPUT_SCHEMA = "emuflow.mfspart-refiner-input/v4"
MFSPART_REFINEMENT_SCHEMA = "emuflow.mfspart-refinement/v1"
MFSPART_REFINER_PROVIDER = "mfspart-timing-path-guarded-direct-kway-fm-v4"
_GAIN_RANK_SCALE = 1_000_000_000.0
_DEFAULT_PYTHON_REPLAY_MAX_NODES = 2_000
DEFAULT_BOTTLENECK_BETA = 256.0
# The canonical physical comparison found no incremental Phase-7 QoR benefit
# from the path-set term. Keep it available for explicit research studies, but
# do not perturb the guarded objective by default.
DEFAULT_TIMING_PATH_BETA = 0.0


def _gain_rank(value: float) -> int:
    scaled = value * _GAIN_RANK_SCALE
    return (
        math.floor(scaled + 0.5)
        if scaled >= 0.0
        else math.ceil(scaled - 0.5)
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalise_refinement(
    graph: Mapping[str, Any],
    dimensions: Sequence[str],
    parts: Sequence[str],
    distances: Mapping[str, Mapping[str, int]],
    capacities: Mapping[str, Mapping[str, int]],
    assignment: Union[Mapping[int, int], Sequence[int]],
    *,
    hmax: int,
    move_distance: int,
    early_stop: int,
    gamma: float,
    violation_lambda: float,
    mu: float,
    bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    timing_paths: Sequence[Mapping[str, Any]] = (),
    timing_path_beta: float = 0.0,
) -> Dict[str, Any]:
    if not parts or len(set(parts)) != len(parts):
        raise ValidationError("MFSPart refiner FPGA ids must be unique")
    if not dimensions or len(set(dimensions)) != len(dimensions):
        raise ValidationError("MFSPart refiner dimensions must be unique")
    if hmax < 1 or move_distance < 1 or early_stop < 1:
        raise ValidationError("invalid MFSPart FM distance or early-stop limit")
    if any(
        not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in (
            gamma,
            violation_lambda,
            mu,
            bottleneck_beta,
            timing_path_beta,
        )
    ):
        raise ValidationError("invalid MFSPart FM score parameter")
    nodes = graph.get("nodes")
    nets = graph.get("nets")
    if not isinstance(nodes, list) or not nodes or not isinstance(nets, list):
        raise ValidationError("invalid MFSPart FM graph")
    if isinstance(assignment, Mapping):
        assigned = [assignment.get(index) for index in range(len(nodes))]
    else:
        assigned = list(assignment)
    if len(assigned) != len(nodes) or any(
        not isinstance(part, int)
        or isinstance(part, bool)
        or part < 0
        or part >= len(parts)
        for part in assigned
    ):
        raise ValidationError("invalid MFSPart FM initial assignment")
    distance_matrix = []
    capacity_matrix = []
    for source in parts:
        row = []
        for target in parts:
            distance = distances.get(source, {}).get(target)
            if not isinstance(distance, int) or isinstance(distance, bool) or distance < 0:
                raise ValidationError("incomplete MFSPart FM distance matrix")
            row.append(distance)
        distance_matrix.append(row)
        capacity_row = []
        for dimension in dimensions:
            capacity = capacities.get(source, {}).get(dimension)
            if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
                raise ValidationError("incomplete MFSPart FM capacity matrix")
            capacity_row.append(capacity)
        capacity_matrix.append(capacity_row)
    for left in range(len(parts)):
        if distance_matrix[left][left] != 0:
            raise ValidationError("MFSPart FM self-distance must be zero")
        for right in range(len(parts)):
            if distance_matrix[left][right] != distance_matrix[right][left]:
                raise ValidationError("paper-mode FPGA distances must be symmetric")
    for node in nodes:
        if len(node["weights"]) != len(dimensions):
            raise ValidationError("MFSPart FM node weight dimension mismatch")
    normalised_nets = []
    for net in nets:
        weight = net.get("weight")
        bottleneck_weight = net.get("bottleneck_weight", weight)
        maximum_distance = net.get("max_distance_limit", -1)
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight <= 0.0
            or not isinstance(bottleneck_weight, (int, float))
            or isinstance(bottleneck_weight, bool)
            or not math.isfinite(bottleneck_weight)
            or bottleneck_weight < 0.0
            or not isinstance(maximum_distance, int)
            or isinstance(maximum_distance, bool)
            or maximum_distance < -1
        ):
            raise ValidationError("invalid MFSPart FM net objective metadata")
        normalised_nets.append(
            {
                **dict(net),
                "weight": float(weight),
                "bottleneck_weight": float(bottleneck_weight),
                "max_distance_limit": maximum_distance,
            }
        )
    for net in normalised_nets:
        source = net.get("source")
        sinks = net.get("sinks")
        if (
            not isinstance(source, int)
            or isinstance(source, bool)
            or source < 0
            or source >= len(nodes)
            or not isinstance(sinks, list)
            or not sinks
            or any(
                not isinstance(sink, int)
                or isinstance(sink, bool)
                or sink < 0
                or sink >= len(nodes)
                or sink == source
                for sink in sinks
            )
            or len(set(sinks)) != len(sinks)
        ):
            raise ValidationError("invalid MFSPart FM net endpoints")
        initial_maximum = max(
            distance_matrix[assigned[source]][assigned[sink]]
            for sink in sinks
        )
        if (
            net["max_distance_limit"] >= 0
            and initial_maximum > net["max_distance_limit"]
        ):
            raise ValidationError(
                "MFSPart FM topology guard excludes its initial assignment"
            )
    normalised_paths = []
    for path_index, path in enumerate(timing_paths):
        weight = path.get("weight")
        pins = path.get("pins")
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not math.isfinite(weight)
            or weight <= 0.0
            or not isinstance(pins, (list, tuple))
            or len(pins) < 2
            or any(
                not isinstance(pin, int)
                or isinstance(pin, bool)
                or pin < 0
                or pin >= len(nodes)
                for pin in pins
            )
            or len(set(pins)) != len(pins)
        ):
            raise ValidationError(
                f"invalid MFSPart timing-path objective record {path_index}"
            )
        normalised_paths.append(
            {"weight": float(weight), "pins": sorted(pins)}
        )
    return {
        "schema": MFSPART_REFINER_INPUT_SCHEMA,
        "provider": MFSPART_REFINER_PROVIDER,
        "graph": {**dict(graph), "nets": normalised_nets},
        "dimensions": list(dimensions),
        "parts": list(parts),
        "distances": distance_matrix,
        "capacities": capacity_matrix,
        "assignment": assigned,
        "hmax": hmax,
        "move_distance": move_distance,
        "early_stop": early_stop,
        "gamma": float(gamma),
        "lambda": float(violation_lambda),
        "mu": float(mu),
        "bottleneck_beta": float(bottleneck_beta),
        "timing_paths": normalised_paths,
        "timing_path_beta": float(timing_path_beta),
    }


def _write_native_input(path: Path, problem: Mapping[str, Any]) -> None:
    graph = problem["graph"]
    input_v4 = bool(problem["timing_paths"]) or problem["timing_path_beta"] != 0.0
    lines = [
        (
            "EMUFLOW_MFSPART_REFINER_INPUT_V4"
            if input_v4
            else "EMUFLOW_MFSPART_REFINER_INPUT_V3"
        ),
        "PARAM "
        + " ".join(
            str(value)
            for value in (
                len(problem["parts"]),
                len(graph["nodes"]),
                len(problem["dimensions"]),
                len(graph["nets"]),
                problem["hmax"],
                problem["move_distance"],
                problem["early_stop"],
                format(problem["gamma"], ".17g"),
                format(problem["lambda"], ".17g"),
                format(problem["mu"], ".17g"),
                format(problem["bottleneck_beta"], ".17g"),
                *(
                    (
                        len(problem["timing_paths"]),
                        format(problem["timing_path_beta"], ".17g"),
                    )
                    if input_v4
                    else ()
                ),
            )
        ),
    ]
    for source, row in enumerate(problem["distances"]):
        for target, distance in enumerate(row):
            lines.append(f"DIST {source} {target} {distance}")
    for part, row in enumerate(problem["capacities"]):
        for dimension, capacity in enumerate(row):
            lines.append(f"CAP {part} {dimension} {capacity}")
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
                    format(net["bottleneck_weight"], ".17g"),
                    net["max_distance_limit"],
                    net["source"],
                    len(net["sinks"]),
                    *net["sinks"],
                )
            )
        )
    if input_v4:
        for index, timing_path in enumerate(problem["timing_paths"]):
            lines.append(
                "PATH "
                + " ".join(
                    str(value)
                    for value in (
                        index,
                        format(timing_path["weight"], ".17g"),
                        len(timing_path["pins"]),
                        *timing_path["pins"],
                    )
                )
            )
    lines.extend(
        f"ASSIGN {node} {part}"
        for node, part in enumerate(problem["assignment"])
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_output(path: Path, node_count: int) -> Dict[str, Any]:
    if not path.is_file():
        raise EmuFlowError("MFSPart refiner produced no output")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_MFSPART_REFINER_OUTPUT_V1":
        raise ValidationError("invalid MFSPart refiner output header")
    status = None
    moves = []
    final: Dict[int, int] = {}
    metrics: Dict[str, float] = {}
    for line in lines[1:]:
        fields = line.split()
        try:
            if fields[0] == "STATUS" and len(fields) == 2:
                if status is not None:
                    raise ValidationError("duplicate MFSPart refiner status")
                status = fields[1]
            elif fields[0] == "MOVE" and len(fields) == 8:
                index, node, source, target = map(int, fields[1:5])
                if index != len(moves):
                    raise ValidationError("MFSPart move sequence mismatch")
                moves.append(
                    {
                        "node": node,
                        "source": source,
                        "target": target,
                        "gain": float(fields[5]),
                        "cumulative_gain": float(fields[6]),
                        "kept": bool(int(fields[7])),
                    }
                )
            elif fields[0] == "FINAL" and len(fields) == 3:
                node, part = map(int, fields[1:])
                if node in final:
                    raise ValidationError("duplicate MFSPart final assignment")
                final[node] = part
            elif fields[0] == "METRIC" and len(fields) == 3:
                if fields[1] in metrics:
                    raise ValidationError("duplicate MFSPart refiner metric")
                metrics[fields[1]] = float(fields[2])
            else:
                raise ValidationError(f"invalid MFSPart refiner output record {line!r}")
        except (ValueError, IndexError) as error:
            raise ValidationError(f"malformed MFSPart refiner output record {line!r}") from error
    if status != "PASS" or set(final) != set(range(node_count)):
        raise ValidationError("incomplete MFSPart refiner output")
    return {
        "moves": moves,
        "assignment": [final[index] for index in range(node_count)],
        "metrics": metrics,
    }


def _parse_checker_output_text(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != "EMUFLOW_MFSPART_REFINER_CHECK_OUTPUT_V1":
        raise ValidationError("invalid MFSPart refiner checker output header")
    status = None
    metrics: Dict[str, float] = {}
    for line in lines[1:]:
        fields = line.split()
        try:
            if fields[0] == "STATUS" and len(fields) == 2:
                if status is not None:
                    raise ValidationError("duplicate MFSPart checker status")
                status = fields[1]
            elif fields[0] == "METRIC" and len(fields) == 3:
                if fields[1] in metrics:
                    raise ValidationError("duplicate MFSPart checker metric")
                value = float(fields[2])
                if not math.isfinite(value):
                    raise ValidationError("non-finite MFSPart checker metric")
                metrics[fields[1]] = value
            else:
                raise ValidationError(
                    f"invalid MFSPart checker output record {line!r}"
                )
        except (ValueError, IndexError) as error:
            raise ValidationError(
                f"malformed MFSPart checker output record {line!r}"
            ) from error
    required = {
        "attempted_moves",
        "kept_moves",
        "objective_recomputations",
        "orthant_tree_nodes_visited",
        "best_cumulative_gain",
    }
    if status != "PASS" or set(metrics) != required:
        raise ValidationError("incomplete MFSPart refiner checker output")
    return metrics


def _parse_checker_output(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise EmuFlowError("MFSPart refiner checker produced no output")
    return _parse_checker_output_text(path.read_text(encoding="utf-8"))


def validate_mfspart_native_certificate(
    input_path: Path,
    output_path: Path,
    *,
    checker: Optional[str] = None,
) -> Dict[str, Any]:
    """Independently replay a native FM certificate without scratch writes."""

    input_lines = input_path.read_text(encoding="utf-8").splitlines()
    header = input_lines[:2]
    if header[0] not in {
        "EMUFLOW_MFSPART_REFINER_INPUT_V2",
        "EMUFLOW_MFSPART_REFINER_INPUT_V3",
        "EMUFLOW_MFSPART_REFINER_INPUT_V4",
    }:
        raise ValidationError("invalid MFSPart native certificate input")
    fields = header[1].split()
    try:
        expected_fields = 14 if header[0].endswith("V4") else 12
        if len(fields) != expected_fields or fields[0] != "PARAM":
            raise ValidationError("invalid MFSPart native certificate PARAM")
        node_count = int(fields[2])
    except ValueError as error:
        raise ValidationError(
            "malformed MFSPart native certificate PARAM"
        ) from error
    if node_count <= 0:
        raise ValidationError("invalid MFSPart native certificate node count")
    guarded_nets = 0
    zero_bottleneck_nets = 0
    timing_paths = 0
    timing_path_pins = 0
    timing_path_objective_digest = hashlib.sha256()
    if header[0] in {
        "EMUFLOW_MFSPART_REFINER_INPUT_V3",
        "EMUFLOW_MFSPART_REFINER_INPUT_V4",
    }:
        for line in input_lines[2:]:
            record = line.split()
            if not record:
                continue
            if record[0] == "PATH":
                timing_paths += 1
                try:
                    if len(record) < 5:
                        raise ValidationError(
                            "invalid MFSPart native certificate PATH record"
                        )
                    path_pin_count = int(record[3])
                except ValueError as error:
                    raise ValidationError(
                        "malformed MFSPart native certificate PATH record"
                    ) from error
                if path_pin_count < 2 or len(record) != 4 + path_pin_count:
                    raise ValidationError(
                        "invalid MFSPart native certificate PATH pin count"
                    )
                timing_path_pins += path_pin_count
                timing_path_objective_digest.update(
                    (line + "\n").encode("utf-8")
                )
                continue
            if record[0] != "NET":
                continue
            try:
                if len(record) < 8:
                    raise ValidationError(
                        "invalid MFSPart native certificate NET record"
                    )
                bottleneck_weight = float(record[3])
                maximum_distance = int(record[4])
            except ValueError as error:
                raise ValidationError(
                    "malformed MFSPart native certificate NET record"
                ) from error
            guarded_nets += maximum_distance >= 0
            zero_bottleneck_nets += bottleneck_weight == 0.0
    parsed = _parse_output(output_path, node_count)
    command = resolve_native_executable(
        "emuflow_mfspart_refiner_checker", checker
    )
    completed = subprocess.run(
        [
            command,
            str(input_path.resolve()),
            str(output_path.resolve()),
            "/dev/stdout",
        ],
        cwd=input_path.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValidationError(
            "MFSPart native certificate checker rejected refinement: "
            + completed.stdout[-2000:]
        )
    checker_metrics = _parse_checker_output_text(completed.stdout)
    if checker_metrics["attempted_moves"] != len(parsed["moves"]):
        raise ValidationError("MFSPart checker move count mismatch")
    if checker_metrics["kept_moves"] != sum(
        bool(move.get("kept")) for move in parsed["moves"]
    ):
        raise ValidationError("MFSPart checker kept-prefix mismatch")
    input_evidence = {
        "native_header": header[0],
        "guarded_nets": guarded_nets,
        "zero_bottleneck_nets": zero_bottleneck_nets,
    }
    if header[0] == "EMUFLOW_MFSPART_REFINER_INPUT_V4":
        input_evidence["timing_paths"] = timing_paths
        input_evidence["timing_path_pins"] = timing_path_pins
        input_evidence["timing_path_objective_sha256"] = (
            timing_path_objective_digest.hexdigest()
        )
    return {
        "parsed": parsed,
        "checker_metrics": checker_metrics,
        "input_evidence": input_evidence,
    }


def _adjacency_and_incidence(problem: Mapping[str, Any]):
    adjacency = [[] for _ in problem["graph"]["nodes"]]
    incidence = [[] for _ in problem["graph"]["nodes"]]
    for net_index, net in enumerate(problem["graph"]["nets"]):
        incidence[net["source"]].append(net_index)
        for sink in net["sinks"]:
            adjacency[net["source"]].append((sink, net["weight"]))
            adjacency[sink].append((net["source"], net["weight"]))
            incidence[sink].append(net_index)
    return adjacency, incidence


def _loads(problem, assignment):
    loads = [[0] * len(problem["dimensions"]) for _ in problem["parts"]]
    for node, part in enumerate(assignment):
        for dimension, weight in enumerate(problem["graph"]["nodes"][node]["weights"]):
            loads[part][dimension] += weight
    return loads


def _fits(problem, loads, node: int, target: int) -> bool:
    return all(
        loads[target][dimension] + weight
        <= problem["capacities"][target][dimension]
        for dimension, weight in enumerate(problem["graph"]["nodes"][node]["weights"])
    )


def _timing_path_metrics(problem, assignment) -> Dict[str, float]:
    crossed = 0
    weighted = 0.0
    for timing_path in problem.get("timing_paths", []):
        if len({assignment[pin] for pin in timing_path["pins"]}) > 1:
            crossed += 1
            weighted += timing_path["weight"]
    return {
        "crossed_timing_paths": float(crossed),
        "weighted_crossed_timing_paths": weighted,
    }


def _refinement_metrics(problem, assignment) -> Dict[str, float]:
    metrics = _partition_metrics(problem, assignment)
    metrics.update(_timing_path_metrics(problem, assignment))
    return metrics


def _timing_path_delta_exhaustive(
    problem, assignment, node: int, candidate: int
) -> float:
    source = assignment[node]
    if candidate == source or problem.get("timing_path_beta", 0.0) == 0.0:
        return 0.0
    delta = 0.0
    for timing_path in problem.get("timing_paths", []):
        pins = timing_path["pins"]
        if node not in pins:
            continue
        before = len({assignment[pin] for pin in pins}) > 1
        after = (
            len(
                {
                    candidate if pin == node else assignment[pin]
                    for pin in pins
                }
            )
            > 1
        )
        delta += timing_path["weight"] * (int(before) - int(after))
    return problem["timing_path_beta"] * delta


def _compatibility(problem, adjacency, incidence, assignment, node: int, candidate: int) -> float:
    hop_score = 0.0
    violation = 0.0
    for neighbor, weight in adjacency[node]:
        distance = problem["distances"][assignment[neighbor]][candidate]
        if distance <= problem["hmax"]:
            hop_score += (problem["hmax"] - distance) * weight
        else:
            violation += weight * (1.0 + problem["mu"] * (distance - problem["hmax"]))
    connectivity = 0.0
    bottleneck_hops = 0.0
    for net_index in incidence[node]:
        net = problem["graph"]["nets"][net_index]
        spanned = {candidate if net["source"] == node else assignment[net["source"]]}
        spanned.update(candidate if sink == node else assignment[sink] for sink in net["sinks"])
        connectivity += net["weight"] * len(spanned)
        driver_part = candidate if net["source"] == node else assignment[net["source"]]
        maximum_distance = max(
            problem["distances"][driver_part][
                candidate if sink == node else assignment[sink]
            ]
            for sink in net["sinks"]
        )
        if (
            net["max_distance_limit"] >= 0
            and maximum_distance > net["max_distance_limit"]
        ):
            return -math.inf
        bottleneck_hops += net["bottleneck_weight"] * maximum_distance
    return (
        hop_score
        - problem["gamma"] * connectivity
        - problem["lambda"] * violation
        - problem["bottleneck_beta"] * bottleneck_hops
        + _timing_path_delta_exhaustive(
            problem, assignment, node, candidate
        )
    )


def _compatibility_indexed(
    problem,
    neighbor_part_weights,
    incidence,
    net_part_counts,
    net_unique_parts,
    assignment,
    node: int,
    candidate: int,
) -> float:
    hop_score = 0.0
    violation = 0.0
    for neighbor_part, weight in enumerate(neighbor_part_weights[node]):
        if weight == 0.0:
            continue
        distance = problem["distances"][neighbor_part][candidate]
        if distance <= problem["hmax"]:
            hop_score += (problem["hmax"] - distance) * weight
        else:
            violation += weight * (
                1.0 + problem["mu"] * (distance - problem["hmax"])
            )
    connectivity = 0.0
    bottleneck_hops = 0.0
    source_part = assignment[node]
    for net_index in incidence[node]:
        net = problem["graph"]["nets"][net_index]
        spanned_parts = net_unique_parts[net_index]
        if candidate != source_part:
            spanned_parts += int(net_part_counts[net_index][candidate] == 0)
            spanned_parts -= int(net_part_counts[net_index][source_part] == 1)
        connectivity += net["weight"] * spanned_parts
        driver_part = candidate if net["source"] == node else assignment[net["source"]]
        maximum_distance = max(
            problem["distances"][driver_part][
                candidate if sink == node else assignment[sink]
            ]
            for sink in net["sinks"]
        )
        if (
            net["max_distance_limit"] >= 0
            and maximum_distance > net["max_distance_limit"]
        ):
            return -math.inf
        bottleneck_hops += net["bottleneck_weight"] * maximum_distance
    return (
        hop_score
        - problem["gamma"] * connectivity
        - problem["lambda"] * violation
        - problem["bottleneck_beta"] * bottleneck_hops
    )


def _pair_compatibility_indexed(
    problem, neighbor_part_weights, node: int, candidate: int
) -> float:
    """Return the Eq. 10 hop/violation terms without hyperedge connectivity."""

    hop_score = 0.0
    violation = 0.0
    for neighbor_part, weight in enumerate(neighbor_part_weights[node]):
        if weight == 0.0:
            continue
        distance = problem["distances"][neighbor_part][candidate]
        if distance <= problem["hmax"]:
            hop_score += (problem["hmax"] - distance) * weight
        else:
            violation += weight * (
                1.0 + problem["mu"] * (distance - problem["hmax"])
            )
    return hop_score - problem["lambda"] * violation


def _replay_exhaustive(problem: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[int], Dict[str, float]]:
    adjacency, incidence = _adjacency_and_incidence(problem)
    assignment = list(problem["assignment"])
    loads = _loads(problem, assignment)
    locked = [False] * len(assignment)
    moves = []
    cumulative = 0.0
    best_cumulative = 0.0
    best_prefix = 0
    ineffective = 0
    while ineffective < problem["early_stop"]:
        choices = []
        for node, source in enumerate(assignment):
            if locked[node] or problem["graph"]["nodes"][node]["fixed_part"] >= 0:
                continue
            source_score = _compatibility(problem, adjacency, incidence, assignment, node, source)
            for target in range(len(problem["parts"])):
                if target == source or problem["distances"][source][target] > problem["move_distance"] or not _fits(problem, loads, node, target):
                    continue
                target_score = _compatibility(
                    problem, adjacency, incidence, assignment, node, target
                )
                if not math.isfinite(target_score):
                    continue
                gain = target_score - source_score
                choices.append((gain, -node, -target, node, source, target))
        if not choices:
            break
        gain, _, _, node, source, target = max(choices)
        for dimension, weight in enumerate(problem["graph"]["nodes"][node]["weights"]):
            loads[source][dimension] -= weight
            loads[target][dimension] += weight
        assignment[node] = target
        locked[node] = True
        cumulative += gain
        moves.append({"node": node, "source": source, "target": target, "gain": gain, "cumulative_gain": cumulative, "kept": False})
        if cumulative > best_cumulative:
            best_cumulative = cumulative
            best_prefix = len(moves)
            ineffective = 0
        else:
            ineffective += 1
    final = list(problem["assignment"])
    for index, move in enumerate(moves):
        move["kept"] = index < best_prefix
        if move["kept"]:
            final[move["node"]] = move["target"]
    initial_metrics = _refinement_metrics(problem, problem["assignment"])
    final_metrics = _refinement_metrics(problem, final)
    metrics = {
        "attempted_moves": float(len(moves)),
        "best_prefix": float(best_prefix),
        "best_cumulative_gain": best_cumulative,
    }
    metrics.update({f"initial_{name}": value for name, value in initial_metrics.items()})
    metrics.update({f"final_{name}": value for name, value in final_metrics.items()})
    return moves, final, metrics


def _replay(problem: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], List[int], Dict[str, float]]:
    """Independently replay FM with exact, certificate-like lazy candidates.

    Every unlocked node has one heap certificate for its globally best legal
    target.  A move invalidates precisely the nodes whose objective or target
    feasibility can change.  Small-graph tests compare this implementation
    move-for-move with :func:`_replay_exhaustive`.
    """

    adjacency, incidence = _adjacency_and_incidence(problem)
    assignment = list(problem["assignment"])
    loads = _loads(problem, assignment)
    node_count = len(assignment)
    dimension_count = len(problem["dimensions"])
    locked = [False] * node_count
    versions = [0] * node_count
    queue = []
    candidate_recomputations = 0
    neighbor_part_weights = [
        [0.0] * len(problem["parts"]) for _ in range(node_count)
    ]
    for node in range(node_count):
        for neighbor, weight in adjacency[node]:
            neighbor_part_weights[node][assignment[neighbor]] += weight
    pair_coefficients = []
    for neighbor_part in range(len(problem["parts"])):
        coefficients = []
        for candidate in range(len(problem["parts"])):
            distance = problem["distances"][neighbor_part][candidate]
            if distance <= problem["hmax"]:
                coefficients.append(float(problem["hmax"] - distance))
            else:
                coefficients.append(
                    -problem["lambda"]
                    * (1.0 + problem["mu"] * (distance - problem["hmax"]))
                )
        pair_coefficients.append(coefficients)
    pair_scores = [
        [
            sum(
                neighbor_part_weights[node][neighbor_part]
                * pair_coefficients[neighbor_part][candidate]
                for neighbor_part in range(len(problem["parts"]))
                if neighbor_part_weights[node][neighbor_part] != 0.0
            )
            for candidate in range(len(problem["parts"]))
        ]
        for node in range(node_count)
    ]
    net_part_counts = [
        [0] * len(problem["parts"]) for _ in problem["graph"]["nets"]
    ]
    net_unique_parts = [0] * len(problem["graph"]["nets"])
    net_sink_part_counts = [
        [0] * len(problem["parts"]) for _ in problem["graph"]["nets"]
    ]
    net_sink_top1 = [
        [0] * len(problem["parts"]) for _ in problem["graph"]["nets"]
    ]
    net_sink_top2 = [
        [0] * len(problem["parts"]) for _ in problem["graph"]["nets"]
    ]
    net_sink_top1_counts = [
        [0] * len(problem["parts"]) for _ in problem["graph"]["nets"]
    ]
    net_pins = []
    for net_index, net in enumerate(problem["graph"]["nets"]):
        pins = [net["source"], *net["sinks"]]
        net_pins.append(pins)
        net_part_counts[net_index][assignment[net["source"]]] += 1
        for sink in net["sinks"]:
            net_part_counts[net_index][assignment[sink]] += 1
            net_sink_part_counts[net_index][assignment[sink]] += 1
        net_unique_parts[net_index] = sum(
            count > 0 for count in net_part_counts[net_index]
        )

    def rebuild_sink_distance_summary(net_index: int) -> None:
        for driver_part in range(len(problem["parts"])):
            first = 0
            second = 0
            first_count = 0
            for sink_part, count in enumerate(net_sink_part_counts[net_index]):
                if count == 0:
                    continue
                distance = problem["distances"][driver_part][sink_part]
                if distance > first:
                    second = first
                    first = distance
                    first_count = 1
                elif distance == first:
                    first_count += 1
                elif distance > second:
                    second = distance
            net_sink_top1[net_index][driver_part] = first
            net_sink_top2[net_index][driver_part] = second
            net_sink_top1_counts[net_index][driver_part] = first_count

    for net_index in range(len(problem["graph"]["nets"])):
        rebuild_sink_distance_summary(net_index)

    timing_paths = problem.get("timing_paths", [])
    timing_path_incidence = [[] for _ in range(node_count)]
    timing_path_part_counts = [
        [0] * len(problem["parts"]) for _ in timing_paths
    ]
    timing_path_unique_parts = [0] * len(timing_paths)
    timing_path_local_penalty = [0.0] * node_count
    timing_path_rescue = [
        [0.0] * len(problem["parts"]) for _ in range(node_count)
    ]
    for path_index, timing_path in enumerate(timing_paths):
        for pin in timing_path["pins"]:
            timing_path_incidence[pin].append(path_index)
            timing_path_part_counts[path_index][assignment[pin]] += 1
        timing_path_unique_parts[path_index] = sum(
            count > 0 for count in timing_path_part_counts[path_index]
        )

    def update_timing_path_contribution(
        path_index: int, sign: int, affected=None
    ) -> None:
        if problem["timing_path_beta"] == 0.0:
            return
        timing_path = timing_paths[path_index]
        weighted = (
            problem["timing_path_beta"] * timing_path["weight"] * sign
        )
        unique = timing_path_unique_parts[path_index]
        if unique == 1:
            for pin in timing_path["pins"]:
                timing_path_local_penalty[pin] += weighted
                if affected is not None:
                    affected.add(pin)
        elif unique == 2:
            present = [
                part
                for part, count in enumerate(
                    timing_path_part_counts[path_index]
                )
                if count > 0
            ]
            for part, other in zip(present, reversed(present)):
                if timing_path_part_counts[path_index][part] != 1:
                    continue
                singleton = next(
                    pin
                    for pin in timing_path["pins"]
                    if assignment[pin] == part
                )
                timing_path_rescue[singleton][other] += weighted
                if affected is not None:
                    affected.add(singleton)

    for path_index in range(len(timing_paths)):
        update_timing_path_contribution(path_index, 1)

    def bottleneck_score(node: int, candidate: int) -> float:
        score = 0.0
        source_part = assignment[node]
        for net_index in incidence[node]:
            net = problem["graph"]["nets"][net_index]
            if net["source"] == node:
                maximum_distance = net_sink_top1[net_index][candidate]
            else:
                driver_part = assignment[net["source"]]
                maximum_distance = net_sink_top1[net_index][driver_part]
                if (
                    candidate != source_part
                    and net_sink_part_counts[net_index][source_part] == 1
                    and problem["distances"][driver_part][source_part]
                    == maximum_distance
                    and net_sink_top1_counts[net_index][driver_part] == 1
                ):
                    maximum_distance = net_sink_top2[net_index][driver_part]
                maximum_distance = max(
                    maximum_distance,
                    problem["distances"][driver_part][candidate],
                )
            if (
                net["max_distance_limit"] >= 0
                and maximum_distance > net["max_distance_limit"]
            ):
                return math.inf
            score += net["bottleneck_weight"] * maximum_distance
        return score
    # For a node/candidate pair, the connectivity delta is the weight of its
    # incident nets that do not yet contain candidate, minus the weight of
    # incident nets from which moving the node removes its source part.  Keep
    # the first term indexed so candidate scoring is O(parts + incidence), not
    # O(parts * incidence).  This index is maintained from independent
    # net-part transition events below.
    incident_weight_totals = [0.0] * node_count
    incident_unique_weighted = [0.0] * node_count
    incident_present_weights = [
        [0.0] * len(problem["parts"]) for _ in range(node_count)
    ]
    incident_singleton_weights = [
        [0.0] * len(problem["parts"]) for _ in range(node_count)
    ]
    for net_index, net in enumerate(problem["graph"]["nets"]):
        present_parts = [
            part
            for part, count in enumerate(net_part_counts[net_index])
            if count > 0
        ]
        for pin in net_pins[net_index]:
            incident_weight_totals[pin] += net["weight"]
            incident_unique_weighted[pin] += (
                net["weight"] * net_unique_parts[net_index]
            )
            for part in present_parts:
                incident_present_weights[pin][part] += net["weight"]
                if net_part_counts[net_index][part] == 1:
                    incident_singleton_weights[pin][part] += net["weight"]

    weight_indexes = []
    for dimension in range(dimension_count):
        weight_indexes.append(
            sorted(
                (problem["graph"]["nodes"][node]["weights"][dimension], node)
                for node in range(node_count)
            )
        )
    fit_violation_counts = [
        [
            sum(
                problem["graph"]["nodes"][node]["weights"][dimension]
                > problem["capacities"][target][dimension]
                - loads[target][dimension]
                for dimension in range(dimension_count)
            )
            for target in range(len(problem["parts"]))
        ]
        for node in range(node_count)
    ]
    fit_cache = [
        [count == 0 for count in node_counts]
        for node_counts in fit_violation_counts
    ]
    cached_gains = [
        [None] * len(problem["parts"]) for _ in range(node_count)
    ]
    cached_gain_ranks = [
        [None] * len(problem["parts"]) for _ in range(node_count)
    ]
    target_versions = [
        [0] * len(problem["parts"]) for _ in range(node_count)
    ]
    target_queues = [[] for _ in range(node_count)]
    best_targets = [-1] * node_count
    best_gains = [-math.inf] * node_count
    best_gain_ranks = [-(1 << 63)] * node_count
    source_fit_marks = [0] * node_count
    target_fit_marks = [0] * node_count

    def select_cached_best(node: int):
        source = assignment[node]
        target_queue = target_queues[node]
        while target_queue:
            negative_rank, target, version, gain = target_queue[0]
            if (
                version == target_versions[node][target]
                and fit_cache[node][target]
                and cached_gain_ranks[node][target] == -negative_rank
                and target != source
                and problem["distances"][source][target]
                <= problem["move_distance"]
            ):
                return target, gain, -negative_rank
            heapq.heappop(target_queue)
        return -1, -math.inf, -(1 << 63)

    def publish_best(node: int, target: int, gain: float, rank: int) -> None:
        versions[node] += 1
        best_targets[node] = target
        best_gains[node] = gain
        best_gain_ranks[node] = rank
        if target >= 0:
            heapq.heappush(
                queue, (-rank, node, target, versions[node], gain)
            )

    def recompute_candidate(node: int) -> None:
        nonlocal candidate_recomputations
        if locked[node] or problem["graph"]["nodes"][node]["fixed_part"] >= 0:
            publish_best(node, -1, -math.inf, -(1 << 63))
            return
        candidate_recomputations += 1
        source = assignment[node]
        source_pair_score = pair_scores[node][source]
        source_connectivity = incident_unique_weighted[node]
        source_singleton_weight = incident_singleton_weights[node][source]
        source_score = (
            source_pair_score
            - problem["gamma"] * source_connectivity
            - problem["bottleneck_beta"] * bottleneck_score(node, source)
        )
        target_queue = []
        for target in range(len(problem["parts"])):
            if (
                target == source
                or problem["distances"][source][target] > problem["move_distance"]
            ):
                cached_gains[node][target] = None
                cached_gain_ranks[node][target] = None
                continue
            connectivity_delta = (
                incident_weight_totals[node]
                - incident_present_weights[node][target]
                - source_singleton_weight
            )
            target_bottleneck_score = bottleneck_score(node, target)
            if not math.isfinite(target_bottleneck_score):
                cached_gains[node][target] = None
                cached_gain_ranks[node][target] = None
                continue
            target_score = (
                pair_scores[node][target]
                - problem["gamma"]
                * (source_connectivity + connectivity_delta)
                - problem["bottleneck_beta"] * target_bottleneck_score
                - timing_path_local_penalty[node]
                + timing_path_rescue[node][target]
            )
            gain = target_score - source_score
            gain_rank = _gain_rank(gain)
            cached_gains[node][target] = gain
            cached_gain_ranks[node][target] = gain_rank
            if fit_cache[node][target]:
                target_queue.append(
                    (-gain_rank, target, target_versions[node][target], gain)
                )
        heapq.heapify(target_queue)
        target_queues[node] = target_queue
        publish_best(node, *select_cached_best(node))

    def refresh_capacity_candidate(
        node: int, part: int, new_fit: bool, publish: bool
    ) -> None:
        old_fit = fit_cache[node][part]
        if old_fit == new_fit:
            return
        fit_cache[node][part] = new_fit
        target_versions[node][part] += 1
        if (
            not publish
            or locked[node]
            or problem["graph"]["nodes"][node]["fixed_part"] >= 0
        ):
            return
        rank = cached_gain_ranks[node][part]
        if new_fit:
            if rank is not None:
                heapq.heappush(
                    target_queues[node],
                    (
                        -rank,
                        part,
                        target_versions[node][part],
                        cached_gains[node][part],
                    ),
                )
        target, gain, best_rank = select_cached_best(node)
        if target != best_targets[node] or best_rank != best_gain_ranks[node]:
            publish_best(node, target, gain, best_rank)

    for node in range(node_count):
        recompute_candidate(node)

    moves = []
    cumulative = 0.0
    best_cumulative = 0.0
    best_prefix = 0
    ineffective = 0
    while ineffective < problem["early_stop"]:
        while queue and (
            locked[queue[0][1]] or queue[0][3] != versions[queue[0][1]]
        ):
            heapq.heappop(queue)
        if not queue:
            break
        _, node, target, _, gain = heapq.heappop(queue)
        source = assignment[node]
        old_source_remaining = [
            problem["capacities"][source][dimension] - loads[source][dimension]
            for dimension in range(dimension_count)
        ]
        old_target_remaining = [
            problem["capacities"][target][dimension] - loads[target][dimension]
            for dimension in range(dimension_count)
        ]
        for dimension, weight in enumerate(
            problem["graph"]["nodes"][node]["weights"]
        ):
            loads[source][dimension] -= weight
            loads[target][dimension] += weight
        path_affected = set()
        for path_index in timing_path_incidence[node]:
            update_timing_path_contribution(path_index, -1, path_affected)
        assignment[node] = target
        for neighbor, weight in adjacency[node]:
            neighbor_part_weights[neighbor][source] -= weight
            neighbor_part_weights[neighbor][target] += weight
            for candidate in range(len(problem["parts"])):
                pair_scores[neighbor][candidate] -= (
                    weight * pair_coefficients[source][candidate]
                )
                pair_scores[neighbor][candidate] += (
                    weight * pair_coefficients[target][candidate]
                )
        for net_index in incidence[node]:
            net = problem["graph"]["nets"][net_index]
            old_source_count = net_part_counts[net_index][source]
            old_target_count = net_part_counts[net_index][target]
            source_disappears = old_source_count == 1
            target_appears = old_target_count == 0
            net_part_counts[net_index][source] -= 1
            if net_part_counts[net_index][source] == 0:
                net_unique_parts[net_index] -= 1
            if net_part_counts[net_index][target] == 0:
                net_unique_parts[net_index] += 1
            net_part_counts[net_index][target] += 1
            if net["source"] != node:
                net_sink_part_counts[net_index][source] -= 1
                net_sink_part_counts[net_index][target] += 1
                rebuild_sink_distance_summary(net_index)
            net_weight = problem["graph"]["nets"][net_index]["weight"]
            unique_delta = int(target_appears) - int(source_disappears)
            source_singleton_delta = (
                -1 if old_source_count == 1 else 1 if old_source_count == 2 else 0
            )
            target_singleton_delta = (
                1 if old_target_count == 0 else -1 if old_target_count == 1 else 0
            )
            for pin in net_pins[net_index]:
                incident_unique_weighted[pin] += net_weight * unique_delta
                incident_singleton_weights[pin][source] += (
                    net_weight * source_singleton_delta
                )
                incident_singleton_weights[pin][target] += (
                    net_weight * target_singleton_delta
                )
            if source_disappears:
                for pin in net_pins[net_index]:
                    incident_present_weights[pin][source] -= net_weight
            if target_appears:
                for pin in net_pins[net_index]:
                    incident_present_weights[pin][target] += net_weight
        for path_index in timing_path_incidence[node]:
            counts = timing_path_part_counts[path_index]
            counts[source] -= 1
            if counts[source] == 0:
                timing_path_unique_parts[path_index] -= 1
            if counts[target] == 0:
                timing_path_unique_parts[path_index] += 1
            counts[target] += 1
            update_timing_path_contribution(path_index, 1, path_affected)
        locked[node] = True
        versions[node] += 1
        cumulative += gain
        moves.append(
            {
                "node": node,
                "source": source,
                "target": target,
                "gain": gain,
                "cumulative_gain": cumulative,
                "kept": False,
            }
        )
        if cumulative > best_cumulative:
            best_cumulative = cumulative
            best_prefix = len(moves)
            ineffective = 0
        else:
            ineffective += 1

        affected = {neighbor for neighbor, _ in adjacency[node]}
        affected.update(path_affected)
        for net_index in incidence[node]:
            net = problem["graph"]["nets"][net_index]
            affected.add(net["source"])
            affected.update(net["sinks"])

        source_fit_nodes = []
        target_fit_nodes = []
        capacity_epoch = len(moves)

        def invalidate_capacity_interval(
            dimension: int,
            low: int,
            high: int,
            part: int,
            violation_delta: int,
            fit_nodes,
            fit_marks,
        ) -> None:
            if low >= high:
                return
            index = weight_indexes[dimension]
            begin = bisect_right(index, (low, node_count))
            end = bisect_right(index, (high, node_count))
            for _, candidate_node in index[begin:end]:
                fit_violation_counts[candidate_node][part] += violation_delta
                if fit_marks[candidate_node] != capacity_epoch:
                    fit_marks[candidate_node] = capacity_epoch
                    fit_nodes.append(candidate_node)

        for dimension in range(dimension_count):
            new_source_remaining = (
                problem["capacities"][source][dimension] - loads[source][dimension]
            )
            new_target_remaining = (
                problem["capacities"][target][dimension] - loads[target][dimension]
            )
            invalidate_capacity_interval(
                dimension,
                old_source_remaining[dimension],
                new_source_remaining,
                source,
                -1,
                source_fit_nodes,
                source_fit_marks,
            )
            invalidate_capacity_interval(
                dimension,
                new_target_remaining,
                old_target_remaining[dimension],
                target,
                1,
                target_fit_nodes,
                target_fit_marks,
            )
        affected.discard(node)
        for candidate_node in source_fit_nodes:
            refresh_capacity_candidate(
                candidate_node,
                source,
                fit_violation_counts[candidate_node][source] == 0,
                candidate_node not in affected,
            )
        for candidate_node in target_fit_nodes:
            refresh_capacity_candidate(
                candidate_node,
                target,
                fit_violation_counts[candidate_node][target] == 0,
                candidate_node not in affected,
            )
        for affected_node in sorted(affected):
            recompute_candidate(affected_node)

    final = list(problem["assignment"])
    for index, move in enumerate(moves):
        move["kept"] = index < best_prefix
        if move["kept"]:
            final[move["node"]] = move["target"]
    initial_metrics = _refinement_metrics(problem, problem["assignment"])
    final_metrics = _refinement_metrics(problem, final)
    metrics = {
        "attempted_moves": float(len(moves)),
        "best_prefix": float(best_prefix),
        "best_cumulative_gain": best_cumulative,
        "oracle_candidate_recomputations": float(candidate_recomputations),
    }
    metrics.update({f"initial_{name}": value for name, value in initial_metrics.items()})
    metrics.update({f"final_{name}": value for name, value in final_metrics.items()})
    return moves, final, metrics


def validate_mfspart_refinement(
    artifact: Mapping[str, Any],
    problem: Mapping[str, Any],
    *,
    native_input_path: Optional[Path] = None,
    native_output_path: Optional[Path] = None,
    checker_output_path: Optional[Path] = None,
    checker: Optional[str] = None,
    python_replay_max_nodes: int = _DEFAULT_PYTHON_REPLAY_MAX_NODES,
    force_python_replay: bool = False,
) -> Dict[str, Any]:
    if artifact.get("schema") != MFSPART_REFINEMENT_SCHEMA:
        raise ValidationError("invalid MFSPart refinement schema")
    if (
        isinstance(python_replay_max_nodes, bool)
        or not isinstance(python_replay_max_nodes, int)
        or python_replay_max_nodes < 0
    ):
        raise ValidationError("invalid MFSPart Python replay threshold")
    node_count = len(problem["graph"]["nodes"])
    use_python_replay = force_python_replay or node_count <= python_replay_max_nodes
    if not use_python_replay:
        if (
            native_input_path is None
            or native_output_path is None
            or checker_output_path is None
        ):
            raise ValidationError(
                "large MFSPart refinement requires native certificate paths"
            )
        parsed = _parse_output(native_output_path, node_count)
        if (
            artifact.get("moves") != parsed["moves"]
            or artifact.get("assignment") != parsed["assignment"]
            or artifact.get("metrics") != parsed["metrics"]
        ):
            raise ValidationError("MFSPart artifact differs from sealed native output")
        command = resolve_native_executable(
            "emuflow_mfspart_refiner_checker", checker
        )
        completed = subprocess.run(
            [
                command,
                str(native_input_path.resolve()),
                str(native_output_path.resolve()),
                str(checker_output_path.resolve()),
            ],
            cwd=checker_output_path.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        (checker_output_path.parent / "mfspart_refiner_checker.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        if completed.returncode != 0:
            raise ValidationError(
                "MFSPart native certificate checker rejected refinement: "
                + completed.stdout[-2000:]
            )
        checker_metrics = _parse_checker_output(checker_output_path)
        actual_moves = artifact.get("moves")
        if not isinstance(actual_moves, list):
            raise ValidationError("MFSPart FM moves are invalid")
        if checker_metrics["attempted_moves"] != len(actual_moves):
            raise ValidationError("MFSPart checker move count mismatch")
        kept_moves = sum(bool(move.get("kept")) for move in actual_moves)
        if checker_metrics["kept_moves"] != kept_moves:
            raise ValidationError("MFSPart checker kept-prefix mismatch")
        expected_initial = _refinement_metrics(problem, problem["assignment"])
        expected_final = _refinement_metrics(problem, artifact["assignment"])
        for prefix, expected_metrics in (
            ("initial", expected_initial),
            ("final", expected_final),
        ):
            for name, expected in expected_metrics.items():
                actual = artifact.get("metrics", {}).get(f"{prefix}_{name}")
                if actual is None or not math.isclose(
                    actual, expected, rel_tol=1e-12, abs_tol=1e-12
                ):
                    raise ValidationError(
                        f"MFSPart FM metric mismatch for {prefix}_{name}"
                    )
        return {
            "status": "pass",
            "mode": "native-orthant-global-best-certificate",
            "attempted_moves": len(actual_moves),
            "kept_moves": kept_moves,
            "best_cumulative_gain": artifact["metrics"][
                "best_cumulative_gain"
            ],
            "checker_objective_recomputations": int(
                checker_metrics["objective_recomputations"]
            ),
            "checker_orthant_tree_nodes_visited": int(
                checker_metrics["orthant_tree_nodes_visited"]
            ),
        }
    expected_moves, expected_assignment, expected_metrics = _replay(problem)
    actual_moves = artifact.get("moves")
    if not isinstance(actual_moves, list) or len(actual_moves) != len(expected_moves):
        raise ValidationError("MFSPart FM move count mismatch")
    for expected, actual in zip(expected_moves, actual_moves):
        if (actual.get("node"), actual.get("source"), actual.get("target"), actual.get("kept")) != (expected["node"], expected["source"], expected["target"], expected["kept"]) or not math.isclose(actual.get("gain"), expected["gain"], rel_tol=1e-12, abs_tol=1e-12) or not math.isclose(actual.get("cumulative_gain"), expected["cumulative_gain"], rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError("MFSPart FM move replay mismatch")
    if artifact.get("assignment") != expected_assignment:
        raise ValidationError("MFSPart FM best-prefix rollback mismatch")
    for name, expected in expected_metrics.items():
        if name == "oracle_candidate_recomputations":
            continue
        actual = artifact.get("metrics", {}).get(name)
        if actual is None or not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValidationError(f"MFSPart FM metric mismatch for {name}")
    return {
        "status": "pass",
        "mode": "python-incremental-full-replay",
        "attempted_moves": len(actual_moves),
        "kept_moves": sum(move["kept"] for move in actual_moves),
        "best_cumulative_gain": artifact["metrics"]["best_cumulative_gain"],
        "oracle_candidate_recomputations": int(
            expected_metrics["oracle_candidate_recomputations"]
        ),
    }


def validate_mfspart_refinement_online(
    artifact: Mapping[str, Any],
    problem: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate only the Phase-3 output contract in linear time.

    The full global-best FM replay is an algorithm qualification test, not a
    production-flow legality requirement.  The online path checks the facts
    needed by downstream stages without recomputing candidate gains.
    """

    if artifact.get("schema") != MFSPART_REFINEMENT_SCHEMA:
        raise ValidationError("invalid MFSPart refinement schema")
    moves = artifact.get("moves")
    final = artifact.get("assignment")
    metrics = artifact.get("metrics")
    if not isinstance(moves, list) or not isinstance(final, list):
        raise ValidationError("invalid MFSPart online refinement result")
    if not isinstance(metrics, dict):
        raise ValidationError("invalid MFSPart online refinement metrics")

    nodes = problem["graph"]["nodes"]
    node_count = len(nodes)
    part_count = len(problem["parts"])
    dimension_count = len(problem["dimensions"])
    initial = list(problem["assignment"])
    if len(final) != node_count:
        raise ValidationError("MFSPart final assignment size mismatch")
    if any(
        isinstance(part, bool)
        or not isinstance(part, int)
        or part < 0
        or part >= part_count
        for part in final
    ):
        raise ValidationError("MFSPart final assignment contains an invalid part")

    attempted = list(initial)
    kept = list(initial)
    seen_nodes = set()
    kept_prefix = 0
    previous_cumulative = 0.0
    saw_unkept = False
    for index, move in enumerate(moves):
        if not isinstance(move, dict):
            raise ValidationError("MFSPart move must be an object")
        node = move.get("node")
        source = move.get("source")
        target = move.get("target")
        gain = move.get("gain")
        cumulative = move.get("cumulative_gain")
        is_kept = move.get("kept")
        if (
            isinstance(node, bool)
            or not isinstance(node, int)
            or node < 0
            or node >= node_count
            or node in seen_nodes
        ):
            raise ValidationError("MFSPart move node is invalid or repeated")
        if (
            isinstance(source, bool)
            or not isinstance(source, int)
            or isinstance(target, bool)
            or not isinstance(target, int)
            or source < 0
            or source >= part_count
            or target < 0
            or target >= part_count
            or source == target
            or attempted[node] != source
        ):
            raise ValidationError("MFSPart move source/target is invalid")
        if (
            not isinstance(gain, (int, float))
            or not math.isfinite(float(gain))
            or not isinstance(cumulative, (int, float))
            or not math.isfinite(float(cumulative))
            or not math.isclose(
                float(cumulative),
                previous_cumulative + float(gain),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValidationError("MFSPart move gain certificate is invalid")
        if not isinstance(is_kept, bool) or (saw_unkept and is_kept):
            raise ValidationError("MFSPart kept moves are not a prefix")
        fixed_part = nodes[node]["fixed_part"]
        if fixed_part >= 0 and target != fixed_part:
            raise ValidationError("MFSPart moved a fixed node")
        seen_nodes.add(node)
        attempted[node] = target
        previous_cumulative = float(cumulative)
        if is_kept:
            kept[node] = target
            kept_prefix = index + 1
        else:
            saw_unkept = True

    if final != kept:
        raise ValidationError("MFSPart final assignment differs from kept prefix")
    if metrics.get("attempted_moves") != float(len(moves)):
        raise ValidationError("MFSPart attempted-move count mismatch")
    if metrics.get("best_prefix") != float(kept_prefix):
        raise ValidationError("MFSPart best-prefix count mismatch")
    expected_best_gain = (
        float(moves[kept_prefix - 1]["cumulative_gain"])
        if kept_prefix
        else 0.0
    )
    actual_best_gain = metrics.get("best_cumulative_gain")
    if (
        not isinstance(actual_best_gain, (int, float))
        or not math.isclose(
            float(actual_best_gain),
            expected_best_gain,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
    ):
        raise ValidationError("MFSPart best cumulative gain mismatch")

    loads = [[0] * dimension_count for _ in range(part_count)]
    for node, part in enumerate(final):
        fixed_part = nodes[node]["fixed_part"]
        if fixed_part >= 0 and part != fixed_part:
            raise ValidationError("MFSPart final assignment violates a fixed node")
        for dimension, weight in enumerate(nodes[node]["weights"]):
            loads[part][dimension] += weight
    for part in range(part_count):
        for dimension in range(dimension_count):
            if loads[part][dimension] > problem["capacities"][part][dimension]:
                raise ValidationError("MFSPart final assignment exceeds capacity")

    guarded_nets = 0
    distances = problem["distances"]
    for net in problem["graph"]["nets"]:
        limit = net.get("max_distance_limit", -1)
        if limit < 0:
            continue
        guarded_nets += 1
        source_part = final[net["source"]]
        if any(
            distances[source_part][final[sink]] > limit
            for sink in net["sinks"]
        ):
            raise ValidationError("MFSPart final assignment violates topology guard")

    return {
        "status": "pass",
        "mode": "linear-phase3-output-contract",
        "attempted_moves": len(moves),
        "kept_moves": kept_prefix,
        "best_cumulative_gain": expected_best_gain,
        "guarded_nets": guarded_nets,
    }


def refine_mfspart_level(
    graph: Mapping[str, Any],
    dimensions: Sequence[str],
    parts: Sequence[str],
    distances: Mapping[str, Mapping[str, int]],
    capacities: Mapping[str, Mapping[str, int]],
    assignment: Union[Mapping[int, int], Sequence[int]],
    output_dir: Path,
    *,
    hmax: int,
    move_distance: int = 2,
    early_stop: int,
    gamma: float = 15.0,
    violation_lambda: float = 10_000.0,
    mu: float = 0.1,
    bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    timing_paths: Sequence[Mapping[str, Any]] = (),
    timing_path_beta: float = 0.0,
    executable: Optional[str] = None,
    checker: Optional[str] = None,
    python_replay_max_nodes: int = _DEFAULT_PYTHON_REPLAY_MAX_NODES,
    force_python_replay: bool = False,
    online_validation: bool = False,
) -> Dict[str, Any]:
    problem = _normalise_refinement(
        graph,
        dimensions,
        parts,
        distances,
        capacities,
        assignment,
        hmax=hmax,
        move_distance=move_distance,
        early_stop=early_stop,
        gamma=gamma,
        violation_lambda=violation_lambda,
        mu=mu,
        bottleneck_beta=bottleneck_beta,
        timing_paths=timing_paths,
        timing_path_beta=timing_path_beta,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    input_path = output_dir / "mfspart_refiner.in"
    output_path = output_dir / "mfspart_refiner.out"
    log_path = output_dir / "mfspart_refiner.log"
    checker_output_path = output_dir / "mfspart_refiner.check"
    _write_native_input(input_path, problem)
    command = resolve_native_executable("emuflow_mfspart_refiner", executable)
    optimizer_started = time.perf_counter()
    completed = subprocess.run(
        [command, str(input_path.resolve()), str(output_path.resolve())],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    optimizer_wall_seconds = time.perf_counter() - optimizer_started
    log_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        raise EmuFlowError(f"MFSPart refiner failed with exit code {completed.returncode}: {completed.stdout[-2000:]}")
    check_started = time.perf_counter()
    parsed = _parse_output(output_path, len(graph["nodes"]))
    artifact = {
        "schema": MFSPART_REFINEMENT_SCHEMA,
        "provider": MFSPART_REFINER_PROVIDER,
        "claim_scope": "paper-level direct k-way FM Eqs. 9--10 with EmuFlow class-weighted worst-sink-hop and immutable per-net topology guards",
        "moves": parsed["moves"],
        "assignment": parsed["assignment"],
        "metrics": parsed["metrics"],
        "artifacts": {
            "input": input_path.name,
            "output": output_path.name,
        },
    }
    if online_validation:
        artifact["validation"] = validate_mfspart_refinement_online(
            artifact, problem
        )
    else:
        artifact["validation"] = validate_mfspart_refinement(
            artifact,
            problem,
            native_input_path=input_path,
            native_output_path=output_path,
            checker_output_path=checker_output_path,
            checker=checker,
            python_replay_max_nodes=python_replay_max_nodes,
            force_python_replay=force_python_replay,
        )
        if checker_output_path.is_file():
            artifact["artifacts"]["checker_output"] = checker_output_path.name
    candidate_check_wall_seconds = time.perf_counter() - check_started
    artifact["runtime"] = {
        "optimizer_wall_seconds": optimizer_wall_seconds,
        "candidate_check_wall_seconds": candidate_check_wall_seconds,
        "candidate_check_within_optimizer_budget": (
            candidate_check_wall_seconds <= optimizer_wall_seconds
        ),
    }
    if (
        online_validation
        and candidate_check_wall_seconds > optimizer_wall_seconds
    ):
        raise ValidationError(
            "MFSPart online check exceeded optimizer runtime: "
            f"check={candidate_check_wall_seconds:.6f}s, "
            f"optimizer={optimizer_wall_seconds:.6f}s"
        )
    return artifact


def refine_mfspart_hierarchy(
    hierarchy: Mapping[str, Any],
    initial_partition: Mapping[str, Any],
    parts: Sequence[str],
    distances: Mapping[str, Mapping[str, int]],
    capacities: Mapping[str, Mapping[str, int]],
    output_dir: Path,
    *,
    hmax: int,
    move_distance: int = 2,
    early_stop_fraction: float = 0.2,
    gamma: float = 15.0,
    violation_lambda: float = 10_000.0,
    mu: float = 0.1,
    bottleneck_beta: float = DEFAULT_BOTTLENECK_BETA,
    executable: Optional[str] = None,
    checker: Optional[str] = None,
    python_replay_max_nodes: int = _DEFAULT_PYTHON_REPLAY_MAX_NODES,
    python_replay_levels: Sequence[int] = (),
) -> Dict[str, Any]:
    if hierarchy.get("schema") != MFSPART_HIERARCHY_SCHEMA or initial_partition.get("schema") != MFSPART_INITIAL_SCHEMA:
        raise ValidationError("invalid MFSPart hierarchy or initial partition")
    if not 0 < early_stop_fraction <= 1:
        raise ValidationError("invalid MFSPart early-stop fraction")
    levels = hierarchy["levels"]
    mappings = hierarchy["fine_to_coarse"]
    current = [initial_partition["assignment"][index] for index in range(len(levels[-1]["nodes"]))]
    reports = []
    replay_levels = set(python_replay_levels)
    if any(
        isinstance(level, bool)
        or not isinstance(level, int)
        or level < 0
        or level >= len(levels)
        for level in replay_levels
    ):
        raise ValidationError("invalid MFSPart Python replay level")
    for level in range(len(levels) - 1, -1, -1):
        if level < len(levels) - 1:
            current = [current[mappings[level][fine]] for fine in range(len(levels[level]["nodes"]))]
        early_stop = max(1, math.ceil(early_stop_fraction * len(current)))
        report = refine_mfspart_level(
            levels[level],
            hierarchy["dimensions"],
            parts,
            distances,
            capacities,
            current,
            output_dir / f"level_{level:03d}",
            hmax=hmax,
            move_distance=move_distance,
            early_stop=early_stop,
            gamma=gamma,
            violation_lambda=violation_lambda,
            mu=mu,
            bottleneck_beta=bottleneck_beta,
            executable=executable,
            checker=checker,
            python_replay_max_nodes=python_replay_max_nodes,
            force_python_replay=level in replay_levels,
        )
        current = report["assignment"]
        reports.append({"level": level, "refinement": report})
    return {
        "schema": "emuflow.mfspart-uncoarsening/v1",
        "provider": MFSPART_REFINER_PROVIDER,
        "levels": reports,
        "assignment": current,
        "validation": {
            "status": "pass",
            "refined_levels": len(reports),
            "original_nodes": len(current),
        },
    }
