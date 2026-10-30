"""Official-format adapter and independent checker for ICCAD 2019 Problem B."""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections import defaultdict, deque
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

from .contest_boarddb import materialize_homogeneous_boarddb
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow.common.native_tools import resolve_native_executable
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA

ICCAD2019_INSTANCE_SCHEMA = "emuflow.contest-iccad2019-instance/v1"
ICCAD2019_EVALUATION_SCHEMA = "emuflow.contest-iccad2019-evaluation/v1"
ICCAD2019_BOARDDB_MATERIALIZATION_SCHEMA = (
    "emuflow.contest-iccad2019-boarddb-materialization/v1"
)
ICCAD2019_SOURCE_URL = (
    "https://drive.google.com/file/d/"
    "1aJkaKgjJ56lWehHR_K9Lm8T-SFal00pU/view"
)

def _lines(path: Path) -> Iterator[Tuple[int, str]]:
    with path.open("r", encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.strip()
            if line:
                yield number, line

def _integers(path: Path, number: int, line: str) -> List[int]:
    try:
        return [int(item) for item in line.split()]
    except ValueError as error:
        raise ValidationError(
            f"{path}:{number}: expected whitespace-separated integers"
        ) from error

def parse_iccad2019_instance(path: Path, name: str) -> Dict[str, Any]:
    records = iter(_lines(path))
    try:
        number, line = next(records)
    except StopIteration as error:
        raise ValidationError(f"{path}: empty ICCAD 2019 instance") from error
    header = _integers(path, number, line)
    if len(header) != 4:
        raise ValidationError(
            f"{path}:{number}: expected '<FPGAs> <edges> <nets> <groups>'"
        )
    fpga_count, edge_count, net_count, group_count = header
    if not (1 <= fpga_count <= 500):
        raise ValidationError(f"{path}:{number}: FPGA count is out of range")
    if edge_count <= 0 or net_count <= 0 or group_count <= 0:
        raise ValidationError(f"{path}:{number}: counts must be positive")

    edges = []
    pairs = set()
    for index in range(edge_count):
        try:
            line_number, line = next(records)
        except StopIteration as error:
            raise ValidationError(f"{path}: truncated edge section") from error
        endpoints = _integers(path, line_number, line)
        if (
            len(endpoints) != 2
            or not 0 <= endpoints[0] < endpoints[1] < fpga_count
        ):
            raise ValidationError(
                f"{path}:{line_number}: invalid undirected FPGA edge"
            )
        pair = tuple(endpoints)
        if pair in pairs:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA edge {pair}"
            )
        pairs.add(pair)
        edges.append({"id": index, "endpoints": endpoints})

    adjacency = {index: [] for index in range(fpga_count)}
    for edge in edges:
        left, right = edge["endpoints"]
        adjacency[left].append(right)
        adjacency[right].append(left)
    reached = {0}
    queue = deque([0])
    while queue:
        for neighbor in adjacency[queue.popleft()]:
            if neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    if len(reached) != fpga_count:
        raise ValidationError(f"{path}: FPGA connection graph is disconnected")

    nets = []
    for index in range(net_count):
        try:
            line_number, line = next(records)
        except StopIteration as error:
            raise ValidationError(f"{path}: truncated net section") from error
        endpoints = _integers(path, line_number, line)
        if len(endpoints) < 2 or any(
            endpoint < 0 or endpoint >= fpga_count for endpoint in endpoints
        ):
            raise ValidationError(f"{path}:{line_number}: invalid net endpoints")
        if len(set(endpoints)) != len(endpoints):
            raise ValidationError(
                f"{path}:{line_number}: a net repeats an FPGA endpoint"
            )
        nets.append(
            {"id": index, "source": endpoints[0], "sinks": endpoints[1:]}
        )

    groups = []
    net_membership = [0] * net_count
    for index in range(group_count):
        try:
            line_number, line = next(records)
        except StopIteration as error:
            raise ValidationError(f"{path}: truncated net-group section") from error
        members = _integers(path, line_number, line)
        if not members or any(member < 0 or member >= net_count for member in members):
            raise ValidationError(f"{path}:{line_number}: invalid net-group member")
        if len(set(members)) != len(members):
            raise ValidationError(
                f"{path}:{line_number}: duplicate net in one group"
            )
        for member in members:
            net_membership[member] += 1
        groups.append({"id": index, "nets": members})
    try:
        extra_number, _ = next(records)
    except StopIteration:
        extra_number = None
    if extra_number is not None:
        raise ValidationError(f"{path}:{extra_number}: unexpected trailing record")
    missing = [index for index, count in enumerate(net_membership) if count == 0]
    if missing:
        raise ValidationError(
            f"{path}: {len(missing)} nets are absent from every net group"
        )
    return {
        "schema": ICCAD2019_INSTANCE_SCHEMA,
        "name": name,
        "source": {
            "contest": "2019 CAD Contest at ICCAD",
            "problem": "Problem B: System-level FPGA Routing with TDM",
            "specification_url": ICCAD2019_SOURCE_URL,
        },
        "fpga_count": fpga_count,
        "edges": edges,
        "nets": nets,
        "net_groups": groups,
    }

def import_iccad2019_instance(
    input_path: Path, output_dir: Path, name: str
) -> Dict[str, Any]:
    if not name.strip():
        raise ValidationError("name: expected a non-empty string")
    instance = parse_iccad2019_instance(input_path, name)
    fpga_ids = [f"F{index}" for index in range(instance["fpga_count"])]
    links = [
        {
            "id": f"contest_edge_{edge['id']:06d}",
            "endpoints": [fpga_ids[item] for item in edge["endpoints"]],
            "direction": "full_duplex",
            "mode": "abstract",
            "data_lanes_per_direction": 1,
            "fabric_clock_mhz": 1000.0,
            "latency_cycles": 0,
        }
        for edge in instance["edges"]
    ]
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": (
                "Official ICCAD 2019 Problem B abstract FPGA graph; each "
                "undirected edge is one shared-capacity TDM domain"
            ),
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": "iccad2019-problem-b-abstract",
                "utilization_limit": 1.0,
                "capacity": {"lut": 1},
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
    }
    assignment = {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": name,
        "platform": name,
        "cut_nets": [
            {
                "net": f"net_{net['id']:06d}",
                "cut_class": "register_output",
                "source_fpgas": [fpga_ids[net["source"]]],
                "sink_fpgas": [fpga_ids[item] for item in net["sinks"]],
                "sink_endpoints": len(net["sinks"]),
            }
            for net in instance["nets"]
        ],
    }
    # A uniform even ratio equal to the net count is always a finite legal
    # upper bound on one unit-capacity graph edge.
    max_ratio = max(2, 2 * math.ceil(len(instance["nets"]) / 2))
    common_period = float(
        max_ratio
        * len(instance["edges"])
        * max(len(group["nets"]) for group in instance["net_groups"])
    )
    timing_paths = {
        "schema": "emuflow.sta-paths/v1",
        "design": name,
        "paths": [
            {
                "id": f"net_group_{group['id']:06d}",
                "clock_domain": "iccad2019_max_group_ratio",
                "clock_period_ns": common_period,
                "slack_ns": 0.0,
                "fixed_delay_ns": 0.0,
                "cut_nets": [f"net_{item:06d}" for item in group["nets"]],
            }
            for group in instance["net_groups"]
        ],
    }
    constraints = {
        "schema": "emuflow.system-route-constraints/v1",
        "frame_slots": max_ratio,
        "max_iterations": 20,
        "unavailable_links": [],
        "link_delay_ns": {link["id"]: 1.0 for link in links},
        "sll_links": [],
        "shared_capacity_links": [link["id"] for link in links],
        "tree_edge_sum_tdm": True,
        "reroute_rounds": 8,
        "lambda_load": 2.0,
        "lambda_timing": 4.0,
        "lambda_history": 1.0,
        "lambda_tdm": 0.1,
        "tdm_ratio_quantum": 2,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "contest_instance.json", instance)
    write_json(output_dir / "boarddb.json", boarddb)
    write_json(output_dir / "partition_assignment.json", assignment)
    write_json(output_dir / "route_constraints.json", constraints)
    write_json(output_dir / "contest_timing_paths.json", timing_paths)
    return {
        "status": "pass",
        "schema": ICCAD2019_INSTANCE_SCHEMA,
        "name": name,
        "fpgas": instance["fpga_count"],
        "edges": len(instance["edges"]),
        "nets": len(instance["nets"]),
        "net_groups": len(instance["net_groups"]),
        "max_ratio_bound": max_ratio,
        "artifacts": {
            "instance": str(output_dir / "contest_instance.json"),
            "boarddb": str(output_dir / "boarddb.json"),
            "assignment": str(output_dir / "partition_assignment.json"),
            "route_constraints": str(output_dir / "route_constraints.json"),
            "timing_paths": str(output_dir / "contest_timing_paths.json"),
        },
    }

def materialize_iccad2019_rtl_boarddb(
    instance_path: Path,
    device_template_path: Path,
    output_path: Path,
    *,
    name: str,
    template_fpga_id: Optional[str] = None,
    lane_scale: int = 1,
    fabric_clock_mhz: float = 50.0,
    latency_cycles: int = 2,
    link_mode: str = "abstract",
) -> Dict[str, Any]:
    """Populate an ICCAD 2019 FPGA graph with an RTL-capable device model."""
    if (
        isinstance(lane_scale, bool)
        or not isinstance(lane_scale, int)
        or lane_scale <= 0
    ):
        raise ValidationError("lane_scale: expected a positive integer")
    if (
        isinstance(fabric_clock_mhz, bool)
        or not isinstance(fabric_clock_mhz, (int, float))
        or fabric_clock_mhz <= 0
    ):
        raise ValidationError("fabric_clock_mhz: expected a positive number")
    if (
        isinstance(latency_cycles, bool)
        or not isinstance(latency_cycles, int)
        or latency_cycles < 0
    ):
        raise ValidationError("latency_cycles: expected a non-negative integer")
    if link_mode not in {"abstract", "parallel", "serial", "source_synchronous"}:
        raise ValidationError("link_mode: unsupported BoardDB link mode")

    instance = read_json(instance_path)
    if instance.get("schema") != ICCAD2019_INSTANCE_SCHEMA:
        raise ValidationError(
            f"instance.schema: expected {ICCAD2019_INSTANCE_SCHEMA!r}"
        )
    fpga_count = instance.get("fpga_count")
    edges = instance.get("edges")
    if not isinstance(fpga_count, int) or fpga_count <= 0:
        raise ValidationError("instance.fpga_count: expected a positive integer")
    if not isinstance(edges, list) or not edges:
        raise ValidationError("instance.edges: expected a non-empty array")
    fpga_ids = [f"F{index}" for index in range(fpga_count)]
    links = [
        {
            "id": f"iccad2019_edge_{edge['id']:06d}",
            "endpoints": [fpga_ids[index] for index in edge["endpoints"]],
            # Problem B gives every undirected edge one shared harmonic TDM
            # domain. Both directions can be routed, but consume the same
            # capacity domain; this is not half-duplex direction locking.
            "direction": "full_duplex",
            "capacity_sharing": "shared_bidirectional",
            "mode": link_mode,
            "data_lanes_per_direction": lane_scale,
            "fabric_clock_mhz": float(fabric_clock_mhz),
            "latency_cycles": latency_cycles,
            "contest_edge": edge["id"],
            "lane_scale": lane_scale,
            "capacity_semantics": "shared-bidirectional-tdm-projection",
        }
        for edge in edges
    ]
    validated, template_platform, selected = materialize_homogeneous_boarddb(
        output_path=output_path,
        name=name,
        description=(
            "ICCAD 2019 Problem B public FPGA graph populated with a "
            "homogeneous FPGA device template"
        ),
        fpga_ids=fpga_ids,
        links=links,
        device_template_path=device_template_path,
        template_fpga_id=template_fpga_id,
        provenance={
            "interconnect": {
                "schema": instance["schema"],
                "instance": instance["name"],
                "specification_url": instance["source"]["specification_url"],
                "capacity_semantics": "shared-bidirectional-tdm-projection",
            }
        },
    )
    return {
        "schema": ICCAD2019_BOARDDB_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "platform": validated.name,
        "contest_instance": instance["name"],
        "device_template": template_platform.name,
        "template_fpga": selected["id"],
        "fpgas": len(validated.fpgas),
        "links": len(validated.links),
        "data_lanes": sum(
            link.data_lanes_per_direction for link in validated.links
        ),
        "capacity_semantics": "shared-bidirectional-tdm-projection",
        "output": str(output_path),
    }

def _route_hops(
    instance: Mapping[str, Any], routes: Mapping[str, Any]
) -> Tuple[List[Dict[str, int]], List[List[int]]]:
    route_by_net = {route.get("net"): route for route in routes.get("routes", [])}
    expected = {f"net_{net['id']:06d}" for net in instance["nets"]}
    if set(route_by_net) != expected:
        missing = sorted(expected - set(route_by_net))
        extra = sorted(set(route_by_net) - expected)
        raise ValidationError(
            f"routes: net coverage mismatch; missing={missing[:8]}, extra={extra[:8]}"
        )
    link_to_edge = {
        f"contest_edge_{edge['id']:06d}": edge["id"]
        for edge in instance["edges"]
    }
    hops: List[Dict[str, int]] = []
    hops_by_net: List[List[int]] = []
    for net in instance["nets"]:
        route = route_by_net[f"net_{net['id']:06d}"]
        seen = set()
        net_hops = []
        for tree_edge in route.get("tree_edges", []):
            link = tree_edge.get("link")
            if link not in link_to_edge:
                raise ValidationError(
                    f"net {net['id']}: route uses unknown link {link!r}"
                )
            edge = link_to_edge[link]
            if edge in seen:
                continue
            seen.add(edge)
            net_hops.append(len(hops))
            hops.append({"net": net["id"], "edge": edge})
        if not net_hops:
            raise ValidationError(f"net {net['id']}: route has no physical edge")
        hops_by_net.append(net_hops)
    return hops, hops_by_net


def parse_iccad2019_solution(
    path: Path, net_count: int
) -> List[List[Tuple[int, int]]]:
    records = iter(_lines(path))
    result = []
    for net in range(net_count):
        try:
            number, line = next(records)
        except StopIteration as error:
            raise ValidationError(f"{path}: missing solution for net {net}") from error
        fields = _integers(path, number, line)
        if len(fields) != 1 or fields[0] < 0:
            raise ValidationError(f"{path}:{number}: invalid routed-edge count")
        segments = []
        seen = set()
        for _ in range(fields[0]):
            try:
                segment_number, segment_line = next(records)
            except StopIteration as error:
                raise ValidationError(
                    f"{path}: truncated route for net {net}"
                ) from error
            segment = _integers(path, segment_number, segment_line)
            if len(segment) != 2:
                raise ValidationError(
                    f"{path}:{segment_number}: expected '<edge> <ratio>'"
                )
            edge, ratio = segment
            if edge in seen:
                raise ValidationError(
                    f"{path}:{segment_number}: net {net} repeats edge {edge}"
                )
            if ratio < 2 or ratio % 2:
                raise ValidationError(
                    f"{path}:{segment_number}: ratio must be an even integer >= 2"
                )
            seen.add(edge)
            segments.append((edge, ratio))
        result.append(segments)
    try:
        number, _ = next(records)
    except StopIteration:
        number = None
    if number is not None:
        raise ValidationError(f"{path}:{number}: unexpected trailing solution record")
    return result

def evaluate_iccad2019_solution(
    instance_path: Path,
    solution_path: Path,
    *,
    runtime_seconds: Optional[float] = None,
    median_runtime_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    instance = read_json(instance_path)
    if instance.get("schema") != ICCAD2019_INSTANCE_SCHEMA:
        raise ValidationError(
            f"instance.schema: expected {ICCAD2019_INSTANCE_SCHEMA!r}"
        )
    if runtime_seconds is not None and runtime_seconds < 0:
        raise ValidationError("runtime_seconds: expected a non-negative number")
    if median_runtime_seconds is not None and median_runtime_seconds <= 0:
        raise ValidationError(
            "median_runtime_seconds: expected a positive number"
        )
    if (
        runtime_seconds is not None
        and median_runtime_seconds is not None
        and runtime_seconds <= 0
    ):
        raise ValidationError(
            "runtime_seconds: contest scoring requires a positive runtime"
        )
    solution = parse_iccad2019_solution(solution_path, len(instance["nets"]))
    edge_by_id = {edge["id"]: edge for edge in instance["edges"]}
    edge_usage: Dict[int, Fraction] = defaultdict(Fraction)
    net_costs = []
    for net, segments in zip(instance["nets"], solution):
        route_adjacency: Dict[int, List[int]] = defaultdict(list)
        for edge_id, ratio in segments:
            if edge_id not in edge_by_id:
                raise ValidationError(
                    f"net {net['id']}: unknown FPGA edge {edge_id}"
                )
            left, right = edge_by_id[edge_id]["endpoints"]
            route_adjacency[left].append(right)
            route_adjacency[right].append(left)
            edge_usage[edge_id] += Fraction(1, ratio)
        reached = {net["source"]}
        queue = deque([net["source"]])
        while queue:
            for neighbor in route_adjacency[queue.popleft()]:
                if neighbor not in reached:
                    reached.add(neighbor)
                    queue.append(neighbor)
        missing = sorted(set(net["sinks"]) - reached)
        if missing:
            raise ValidationError(
                f"net {net['id']}: routed subgraph misses sinks {missing}"
            )
        route_vertices = set(route_adjacency)
        if route_vertices - reached:
            raise ValidationError(
                f"net {net['id']}: routed subgraph has a disconnected component"
            )
        net_costs.append(sum(ratio for _, ratio in segments))
    overloaded = [
        (edge, usage) for edge, usage in edge_usage.items() if usage > 1
    ]
    if overloaded:
        edge, usage = max(overloaded, key=lambda item: item[1])
        raise ValidationError(
            f"edge {edge}: harmonic TDM use {usage} exceeds capacity 1"
        )
    group_costs = [
        sum(net_costs[net] for net in group["nets"])
        for group in instance["net_groups"]
    ]
    objective = max(group_costs)
    score = None
    if runtime_seconds is not None and median_runtime_seconds is not None:
        score = objective * (
            1.0 + math.log2(runtime_seconds / median_runtime_seconds) * 0.01
        )
    metrics: Dict[str, Any] = {
        "fpgas": instance["fpga_count"],
        "edges": len(instance["edges"]),
        "nets": len(instance["nets"]),
        "net_groups": len(instance["net_groups"]),
        "routed_segments": sum(len(segments) for segments in solution),
        "maximum_total_tdm_ratio": objective,
        "maximum_edge_harmonic_use": float(max(edge_usage.values(), default=0)),
    }
    if runtime_seconds is not None:
        metrics["runtime_seconds"] = float(runtime_seconds)
    if median_runtime_seconds is not None:
        metrics["median_runtime_seconds"] = float(median_runtime_seconds)
    if score is not None:
        metrics["contest_score"] = score
    return {
        "schema": ICCAD2019_EVALUATION_SCHEMA,
        "status": "pass",
        "instance": instance["name"],
        "metrics": metrics,
    }
