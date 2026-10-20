"""Adapters and an independent checker for the 2025 EDA Elite contest.

The contest model is deliberately preserved separately from BoardDB.  The
BoardDB and partition-assignment artifacts are execution adapters for EmuFlow;
the normalized contest instance remains the source of truth for scoring.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contest_boarddb import materialize_homogeneous_boarddb
from .errors import EmuFlowError, ValidationError
from .io import read_json, write_json
from .native_tools import resolve_native_executable
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .routing import SYSTEM_ROUTE_CONSTRAINTS_SCHEMA, SYSTEM_ROUTES_SCHEMA


EDA2025_INSTANCE_SCHEMA = "emuflow.contest-eda2025-instance/v1"
EDA2025_EVALUATION_SCHEMA = "emuflow.contest-eda2025-evaluation/v1"
EDA2025_TOPOLOGY_OPTIMIZATION_SCHEMA = (
    "emuflow.contest-eda2025-topology-optimization/v1"
)
EDA2025_ROUTING_OPTIMIZATION_SCHEMA = (
    "emuflow.contest-eda2025-routing-optimization/v2"
)
EDA2025_BOARDDB_MATERIALIZATION_SCHEMA = (
    "emuflow.contest-eda2025-boarddb-materialization/v1"
)
EDA2025_SOURCE_URL = (
    "https://edaoss.icisc.cn/file/cacheFile/2025/8/11/"
    "1e213a00cbd94e2b91e997740753cb60.pdf"
)


def _data_lines(path: Path) -> List[Tuple[int, str]]:
    result = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            line = raw.split("#", 1)[0].strip()
            if line:
                result.append((line_number, line))
    return result


def _positive_int(raw: str, context: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise ValidationError(f"{context}: expected an integer") from error
    if value <= 0:
        raise ValidationError(f"{context}: expected a positive integer")
    return value


def parse_design_info(path: Path) -> Tuple[List[str], Dict[str, int]]:
    fpga_ids: List[str] = []
    limits: Dict[str, int] = {}
    for line_number, line in _data_lines(path):
        fields = line.split()
        if len(fields) != 2:
            raise ValidationError(
                f"{path}:{line_number}: expected '<FPGA_ID> <Max_IO>'"
            )
        fpga_id, raw_limit = fields
        if fpga_id in limits:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA {fpga_id!r}"
            )
        fpga_ids.append(fpga_id)
        limits[fpga_id] = _positive_int(
            raw_limit, f"{path}:{line_number} Max_IO"
        )
    if not fpga_ids:
        raise ValidationError(f"{path}: expected at least one FPGA")
    return fpga_ids, limits


def parse_topology(path: Path, fpga_ids: Sequence[str]) -> List[List[int]]:
    expected = list(fpga_ids)
    rows: Dict[str, List[int]] = {}
    for line_number, line in _data_lines(path):
        fpga_id, separator, values = line.partition(":")
        fpga_id = fpga_id.strip()
        if not separator or not fpga_id:
            raise ValidationError(
                f"{path}:{line_number}: expected '<FPGA_ID>: c1,c2,...'"
            )
        if fpga_id not in expected:
            raise ValidationError(
                f"{path}:{line_number}: unknown FPGA {fpga_id!r}"
            )
        if fpga_id in rows:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA {fpga_id!r}"
            )
        raw_channels = [item.strip() for item in values.split(",")]
        if len(raw_channels) != len(expected):
            raise ValidationError(
                f"{path}:{line_number}: expected {len(expected)} channels"
            )
        channels = []
        for index, raw_channel in enumerate(raw_channels):
            try:
                channel = int(raw_channel)
            except ValueError as error:
                raise ValidationError(
                    f"{path}:{line_number}: channel {index} is not an integer"
                ) from error
            if channel < 0:
                raise ValidationError(
                    f"{path}:{line_number}: channel {index} is negative"
                )
            channels.append(channel)
        rows[fpga_id] = channels
    missing = [fpga_id for fpga_id in expected if fpga_id not in rows]
    if missing:
        raise ValidationError(f"{path}: missing topology rows {missing}")
    matrix = [rows[fpga_id] for fpga_id in expected]
    for left in range(len(expected)):
        if matrix[left][left] != 0:
            raise ValidationError(
                f"{path}: topology diagonal for {expected[left]!r} must be zero"
            )
        for right in range(left + 1, len(expected)):
            if matrix[left][right] != matrix[right][left]:
                raise ValidationError(
                    f"{path}: topology must be symmetric at "
                    f"{expected[left]!r}, {expected[right]!r}"
                )
    return matrix


def parse_design_nets(path: Path) -> List[Dict[str, Any]]:
    nets = []
    for net_index, (line_number, line) in enumerate(_data_lines(path), start=1):
        fields = line.split()
        if len(fields) < 3:
            raise ValidationError(
                f"{path}:{line_number}: expected source, weight, and sinks"
            )
        source = fields[0]
        weight = _positive_int(fields[1], f"{path}:{line_number} weight")
        if weight != 1:
            raise ValidationError(
                f"{path}:{line_number}: the published 2025 benchmark model "
                "requires unit net weights"
            )
        sinks = fields[2:]
        if source in sinks or len(set(sinks)) != len(sinks):
            raise ValidationError(
                f"{path}:{line_number}: net endpoints must be distinct"
            )
        nets.append(
            {
                "id": f"net_{net_index:06d}",
                "source_node": source,
                "sink_nodes": sinks,
                "weight": weight,
                "source_line": line_number,
            }
        )
    if not nets:
        raise ValidationError(f"{path}: expected at least one net")
    return nets


def parse_partition_assignment(
    path: Path, fpga_ids: Sequence[str], required_nodes: Sequence[str]
) -> Dict[str, str]:
    known = set(fpga_ids)
    assignment: Dict[str, str] = {}
    seen_fpgas = set()
    for line_number, line in _data_lines(path):
        fpga_id, separator, nodes = line.partition(":")
        fpga_id = fpga_id.strip()
        if not separator or fpga_id not in known:
            raise ValidationError(
                f"{path}:{line_number}: expected a known '<FPGA_ID>: ...'"
            )
        if fpga_id in seen_fpgas:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA {fpga_id!r}"
            )
        seen_fpgas.add(fpga_id)
        for node in nodes.split():
            if node in assignment:
                raise ValidationError(
                    f"{path}:{line_number}: node {node!r} is assigned twice"
                )
            assignment[node] = fpga_id
    missing_fpgas = sorted(known - seen_fpgas)
    if missing_fpgas:
        raise ValidationError(
            f"{path}: missing partition rows for FPGAs {missing_fpgas}"
        )
    missing = sorted(set(required_nodes) - set(assignment))
    if missing:
        preview = missing[:8]
        raise ValidationError(
            f"{path}: {len(missing)} referenced nodes are unassigned: {preview}"
        )
    return assignment


def _validate_parameters(
    alpha_ns: float,
    beta_ns: float,
    ratio_quantum: int,
    max_ratio: int,
    topology_change_fraction: float,
) -> None:
    if (
        isinstance(alpha_ns, bool)
        or not isinstance(alpha_ns, (int, float))
        or alpha_ns <= 0
    ):
        raise ValidationError("alpha_ns: expected a positive number")
    if (
        isinstance(beta_ns, bool)
        or not isinstance(beta_ns, (int, float))
        or beta_ns < 0
    ):
        raise ValidationError("beta_ns: expected a non-negative number")
    for name, value in (("ratio_quantum", ratio_quantum), ("max_ratio", max_ratio)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValidationError(f"{name}: expected a positive integer")
    if max_ratio % ratio_quantum:
        raise ValidationError("max_ratio must be a multiple of ratio_quantum")
    if (
        isinstance(topology_change_fraction, bool)
        or not isinstance(topology_change_fraction, (int, float))
        or not 0.0 <= topology_change_fraction <= 1.0
    ):
        raise ValidationError(
            "topology_change_fraction: expected a number between zero and one"
        )


def _check_io_limits(
    matrix: Sequence[Sequence[int]],
    fpga_ids: Sequence[str],
    limits: Mapping[str, int],
    context: str,
) -> None:
    for index, fpga_id in enumerate(fpga_ids):
        used = sum(matrix[index])
        if used > limits[fpga_id]:
            raise ValidationError(
                f"{context}: FPGA {fpga_id!r} uses {used} channels, "
                f"above Max_IO {limits[fpga_id]}"
            )


def import_eda2025_instance(
    info_path: Path,
    net_path: Path,
    topology_path: Path,
    assignment_path: Path,
    output_dir: Path,
    name: str,
    alpha_ns: float = 0.7,
    beta_ns: float = 30.0,
    ratio_quantum: int = 8,
    max_ratio: int = 512,
    topology_change_fraction: float = 0.3,
) -> Dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: expected a non-empty string")
    _validate_parameters(
        alpha_ns, beta_ns, ratio_quantum, max_ratio, topology_change_fraction
    )
    fpga_ids, limits = parse_design_info(info_path)
    topology = parse_topology(topology_path, fpga_ids)
    _check_io_limits(topology, fpga_ids, limits, "initial topology")
    nets = parse_design_nets(net_path)
    required_nodes = sorted(
        {
            node
            for net in nets
            for node in [net["source_node"], *net["sink_nodes"]]
        }
    )
    node_assignment = parse_partition_assignment(
        assignment_path, fpga_ids, required_nodes
    )

    instance = {
        "schema": EDA2025_INSTANCE_SCHEMA,
        "name": name,
        "source": {
            "contest": "2025 EDA Elite Challenge",
            "problem": "Reconfigurable multi-FPGA system routing",
            "specification_url": EDA2025_SOURCE_URL,
        },
        "parameters": {
            "alpha_ns": float(alpha_ns),
            "beta_ns": float(beta_ns),
            "ratio_quantum": ratio_quantum,
            "max_ratio": max_ratio,
            "topology_change_fraction": float(topology_change_fraction),
        },
        "fpga_ids": fpga_ids,
        "max_external_channels": limits,
        "initial_topology": topology,
        "nets": nets,
        "node_assignment": dict(sorted(node_assignment.items())),
    }

    artifacts = _write_execution_adapter(instance, topology, output_dir)
    cut_net_count = sum(
        any(
            node_assignment[node] != node_assignment[net["source_node"]]
            for node in net["sink_nodes"]
        )
        for net in nets
    )
    physical_pair_count = sum(
        topology[left][right] > 0
        for left in range(len(fpga_ids))
        for right in range(left + 1, len(fpga_ids))
    )
    return {
        "status": "pass",
        "schema": EDA2025_INSTANCE_SCHEMA,
        "name": name,
        "fpgas": len(fpga_ids),
        "logical_nodes": len(node_assignment),
        "nets": len(nets),
        "cut_nets": cut_net_count,
        "physical_channel_pairs": physical_pair_count,
        "artifacts": artifacts,
    }


def _validate_instance(value: Mapping[str, Any]) -> None:
    if value.get("schema") != EDA2025_INSTANCE_SCHEMA:
        raise ValidationError(
            f"instance.schema: expected {EDA2025_INSTANCE_SCHEMA!r}"
        )
    fpga_ids = value.get("fpga_ids")
    if (
        not isinstance(fpga_ids, list)
        or not fpga_ids
        or not all(isinstance(item, str) and item for item in fpga_ids)
        or len(set(fpga_ids)) != len(fpga_ids)
    ):
        raise ValidationError("instance.fpga_ids: invalid")
    limits = value.get("max_external_channels")
    if not isinstance(limits, dict) or set(limits) != set(fpga_ids):
        raise ValidationError("instance.max_external_channels: invalid")
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        for limit in limits.values()
    ):
        raise ValidationError(
            "instance.max_external_channels: limits must be positive integers"
        )
    topology = value.get("initial_topology")
    if (
        not isinstance(topology, list)
        or len(topology) != len(fpga_ids)
        or any(not isinstance(row, list) or len(row) != len(fpga_ids) for row in topology)
    ):
        raise ValidationError("instance.initial_topology: invalid dimensions")
    for left, row in enumerate(topology):
        for right, channel in enumerate(row):
            if (
                isinstance(channel, bool)
                or not isinstance(channel, int)
                or channel < 0
            ):
                raise ValidationError(
                    "instance.initial_topology: channels must be "
                    "non-negative integers"
                )
            if left == right and channel != 0:
                raise ValidationError(
                    "instance.initial_topology: diagonal must be zero"
                )
            if channel != topology[right][left]:
                raise ValidationError(
                    "instance.initial_topology: matrix must be symmetric"
                )
    _check_io_limits(topology, fpga_ids, limits, "initial topology")
    parameters = value.get("parameters")
    expected_parameters = {
        "alpha_ns",
        "beta_ns",
        "ratio_quantum",
        "max_ratio",
        "topology_change_fraction",
    }
    if not isinstance(parameters, dict) or set(parameters) != expected_parameters:
        raise ValidationError("instance.parameters: invalid field coverage")
    _validate_parameters(**parameters)
    assignment = value.get("node_assignment")
    if not isinstance(assignment, dict) or any(
        not isinstance(node, str)
        or not node
        or fpga_id not in fpga_ids
        for node, fpga_id in assignment.items()
    ):
        raise ValidationError("instance.node_assignment: invalid")
    nets = value.get("nets")
    if not isinstance(nets, list) or not nets:
        raise ValidationError("instance.nets: expected a non-empty array")
    net_ids = set()
    for index, net in enumerate(nets):
        context = f"instance.nets[{index}]"
        if not isinstance(net, dict):
            raise ValidationError(f"{context}: expected an object")
        net_id = net.get("id")
        source = net.get("source_node")
        sinks = net.get("sink_nodes")
        if (
            not isinstance(net_id, str)
            or not net_id
            or net_id in net_ids
        ):
            raise ValidationError(f"{context}.id: invalid or duplicate")
        net_ids.add(net_id)
        if source not in assignment:
            raise ValidationError(f"{context}.source_node: unassigned")
        if (
            not isinstance(sinks, list)
            or not sinks
            or not all(isinstance(sink, str) and sink in assignment for sink in sinks)
            or source in sinks
            or len(set(sinks)) != len(sinks)
        ):
            raise ValidationError(f"{context}.sink_nodes: invalid")
        if net.get("weight") != 1:
            raise ValidationError(f"{context}.weight: expected one")


def _topology_change(
    initial: Sequence[Sequence[int]], updated: Sequence[Sequence[int]]
) -> Tuple[int, int]:
    initial_channels = 0
    changed_channels = 0
    for left in range(len(initial)):
        for right in range(left + 1, len(initial)):
            initial_channels += initial[left][right]
            changed_channels += abs(initial[left][right] - updated[left][right])
    return initial_channels, changed_channels


def _route_sink_paths(
    route: Mapping[str, Any],
    pair_delays: Mapping[Tuple[str, str], float],
) -> Dict[str, Tuple[List[str], float]]:
    source = route["source"]
    graph: Dict[str, List[str]] = defaultdict(list)
    indegree: Dict[str, int] = defaultdict(int)
    edge_set = set()
    for edge in route.get("tree_edges", []):
        if not isinstance(edge, dict):
            raise ValidationError(f"route {route.get('id')!r}: malformed edge")
        left, right = edge.get("from"), edge.get("to")
        if not isinstance(left, str) or not isinstance(right, str) or left == right:
            raise ValidationError(f"route {route.get('id')!r}: malformed edge")
        directed = (left, right)
        pair = tuple(sorted(directed))
        if directed in edge_set or pair not in pair_delays:
            raise ValidationError(
                f"route {route.get('id')!r}: duplicate or unavailable edge {directed}"
            )
        edge_set.add(directed)
        graph[left].append(right)
        indegree[right] += 1
    delay = {source: 0.0}
    paths = {source: [source]}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for sink in graph[node]:
            if sink in delay:
                raise ValidationError(
                    f"route {route.get('id')!r}: tree has a cycle or reconvergence"
                )
            delay[sink] = delay[node] + pair_delays[tuple(sorted((node, sink)))]
            paths[sink] = [*paths[node], sink]
            queue.append(sink)
    edge_nodes = {node for edge in edge_set for node in edge}
    if not edge_nodes <= set(delay):
        raise ValidationError(f"route {route.get('id')!r}: disconnected tree")
    missing = sorted(set(route["sinks"]) - set(delay))
    if missing:
        raise ValidationError(
            f"route {route.get('id')!r}: unreachable sinks {missing}"
        )
    if any(count != 1 for node, count in indegree.items() if node != source):
        raise ValidationError(f"route {route.get('id')!r}: not an arborescence")
    return {sink: (paths[sink], delay[sink]) for sink in route["sinks"]}


def _route_path_delays(
    route: Mapping[str, Any],
    pair_delays: Mapping[Tuple[str, str], float],
) -> Dict[str, float]:
    return {
        sink: delay
        for sink, (_, delay) in _route_sink_paths(route, pair_delays).items()
    }


def _write_official_solution(
    output_dir: Path,
    instance: Mapping[str, Any],
    route_by_net: Mapping[str, Mapping[str, Any]],
    pair_delays: Mapping[Tuple[str, str], float],
    topology: Sequence[Sequence[int]],
) -> None:
    """Write the contest's text outputs from independently checked routes."""
    output_dir.mkdir(parents=True, exist_ok=True)
    fpga_ids = instance["fpga_ids"]
    fpga_number = {
        fpga_id: index for index, fpga_id in enumerate(fpga_ids, start=1)
    }
    assignment = instance["node_assignment"]
    records = []
    for net in instance["nets"]:
        net_id = net["id"]
        route = route_by_net.get(net_id)
        if route is None:
            continue
        sink_paths = _route_sink_paths(route, pair_delays)
        endpoint_paths = []
        source_fpga = assignment[net["source_node"]]
        for sink_node in net["sink_nodes"]:
            sink_fpga = assignment[sink_node]
            if sink_fpga == source_fpga:
                continue
            path, delay = sink_paths[sink_fpga]
            endpoint_paths.append((path, delay))
        records.append(
            (
                -max(delay for _, delay in endpoint_paths),
                net["source_line"],
                endpoint_paths,
            )
        )
    records.sort(key=lambda record: (record[0], record[1]))
    route_lines = []
    for _, source_line, endpoint_paths in records:
        route_lines.append(f"[net {source_line}]")
        for path, delay in endpoint_paths:
            encoded_path = ",".join(str(fpga_number[node]) for node in path)
            route_lines.append(f"[{encoded_path}] [{delay:.10g}]")
        route_lines.append("")
    (output_dir / "design.route.out").write_text(
        "\n".join(route_lines), encoding="utf-8"
    )

    topology_lines = [
        f"{fpga_id}: {','.join(str(channel) for channel in topology[index])}"
        for index, fpga_id in enumerate(fpga_ids)
    ]
    (output_dir / "design.newtopo").write_text(
        "\n".join(topology_lines) + "\n", encoding="utf-8"
    )


def _write_topology_text(
    path: Path,
    fpga_ids: Sequence[str],
    topology: Sequence[Sequence[int]],
) -> None:
    path.write_text(
        "\n".join(
            f"{fpga_id}: {','.join(str(channel) for channel in topology[index])}"
            for index, fpga_id in enumerate(fpga_ids)
        )
        + "\n",
        encoding="utf-8",
    )


def _write_execution_adapter(
    instance: Mapping[str, Any],
    topology: Sequence[Sequence[int]],
    output_dir: Path,
) -> Dict[str, str]:
    """Materialize the generic Phase 4 contracts for a contest topology."""
    fpga_ids = instance["fpga_ids"]
    parameters = instance["parameters"]
    alpha_ns = parameters["alpha_ns"]
    beta_ns = parameters["beta_ns"]
    max_ratio = parameters["max_ratio"]
    node_assignment = instance["node_assignment"]
    links = [
        {
            "id": f"contest_link_{left:03d}_{right:03d}",
            "endpoints": [fpga_ids[left], fpga_ids[right]],
            "direction": "full_duplex",
            "mode": "abstract",
            "data_lanes_per_direction": topology[left][right],
            "fabric_clock_mhz": 1000.0 / float(alpha_ns),
            "latency_cycles": 0,
        }
        for left in range(len(fpga_ids))
        for right in range(left + 1, len(fpga_ids))
        if topology[left][right] > 0
    ]
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": instance["name"],
            "kind": "virtual",
            "description": (
                "2025 EDA Elite abstract multi-FPGA topology; exact contest "
                "timing is evaluated from the normalized contest instance"
            ),
        },
        "fpgas": [
            {
                "id": fpga_id,
                "part": "eda-elite-2025-abstract",
                "utilization_limit": 1.0,
                "capacity": {"lut": 1},
            }
            for fpga_id in fpga_ids
        ],
        "links": links,
    }
    cut_nets = []
    for net in instance["nets"]:
        source_fpga = node_assignment[net["source_node"]]
        sink_fpgas = sorted(
            {
                node_assignment[node]
                for node in net["sink_nodes"]
                if node_assignment[node] != source_fpga
            }
        )
        if sink_fpgas:
            cut_nets.append(
                {
                    "net": net["id"],
                    "cut_class": "register_output",
                    "source_fpgas": [source_fpga],
                    "sink_fpgas": sink_fpgas,
                    "sink_endpoints": sum(
                        node_assignment[node] != source_fpga
                        for node in net["sink_nodes"]
                    ),
                }
            )
    assignment = {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": instance["name"],
        "platform": instance["name"],
        "cut_nets": cut_nets,
    }
    contest_period = max(
        1.0,
        (len(fpga_ids) - 1) * (beta_ns + alpha_ns * max_ratio),
    )
    timing_paths = {
        "schema": "emuflow.sta-paths/v1",
        "design": instance["name"],
        "paths": [
            {
                "id": f"contest_path_{cut['net']}_{sink}",
                "clock_domain": "contest_max_path_delay",
                "clock_period_ns": contest_period,
                "slack_ns": 0.0,
                "fixed_delay_ns": 0.0,
                "cut_nets": [cut["net"]],
                "cut_transitions": [
                    {
                        "net": cut["net"],
                        "from": cut["source_fpgas"][0],
                        "to": sink,
                    }
                ],
            }
            for cut in cut_nets
            for sink in cut["sink_fpgas"]
        ],
    }
    constraints = {
        "schema": "emuflow.system-route-constraints/v1",
        "frame_slots": max_ratio,
        "max_iterations": 20,
        "unavailable_links": [],
        "link_delay_ns": {
            link["id"]: float(beta_ns + alpha_ns) for link in links
        },
        "sll_links": [],
        "shared_capacity_links": [link["id"] for link in links],
        "reroute_rounds": 8,
        "lambda_load": 2.0,
        "lambda_timing": 4.0,
        "lambda_history": 1.0,
        "lambda_tdm": 0.1,
        "tdm_ratio_quantum": parameters["ratio_quantum"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "contest_instance.json", dict(instance))
    write_json(output_dir / "boarddb.json", boarddb)
    write_json(output_dir / "partition_assignment.json", assignment)
    write_json(output_dir / "route_constraints.json", constraints)
    write_json(output_dir / "contest_timing_paths.json", timing_paths)
    return {
        "instance": str(output_dir / "contest_instance.json"),
        "boarddb": str(output_dir / "boarddb.json"),
        "assignment": str(output_dir / "partition_assignment.json"),
        "route_constraints": str(output_dir / "route_constraints.json"),
        "timing_paths": str(output_dir / "contest_timing_paths.json"),
    }


def materialize_eda2025_rtl_boarddb(
    instance_path: Path,
    device_template_path: Path,
    output_path: Path,
    *,
    name: str,
    topology_path: Optional[Path] = None,
    template_fpga_id: Optional[str] = None,
    lane_scale: int = 1,
    fabric_clock_mhz: float = 50.0,
    latency_cycles: int = 2,
    link_mode: str = "abstract",
    route_constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Combine a contest interconnect with a real-RTL-capable FPGA template."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: expected a non-empty string")
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
    _validate_instance(instance)
    fpga_ids = instance["fpga_ids"]
    topology = (
        parse_topology(topology_path, fpga_ids)
        if topology_path is not None
        else instance["initial_topology"]
    )
    _check_io_limits(
        topology,
        fpga_ids,
        instance["max_external_channels"],
        "materialized contest topology",
    )
    initial_channels, changed_channels = _topology_change(
        instance["initial_topology"], topology
    )
    allowed_changes = math.floor(
        initial_channels
        * instance["parameters"]["topology_change_fraction"]
        + 1.0e-12
    )
    if changed_channels > allowed_changes:
        raise ValidationError(
            f"topology changes {changed_channels} channels; allowed {allowed_changes}"
        )

    links = [
        {
            "id": f"eda2025_link_{left:03d}_{right:03d}",
            "endpoints": [fpga_ids[left], fpga_ids[right]],
            "direction": "full_duplex",
            "capacity_sharing": "shared_bidirectional",
            "mode": link_mode,
            "data_lanes_per_direction": topology[left][right] * lane_scale,
            "fabric_clock_mhz": float(fabric_clock_mhz),
            "latency_cycles": latency_cycles,
            "contest_channels": topology[left][right],
            "lane_scale": lane_scale,
            "capacity_semantics": "shared-bidirectional-tdm-projection",
        }
        for left in range(len(fpga_ids))
        for right in range(left + 1, len(fpga_ids))
        if topology[left][right] > 0
    ]
    validated, template_platform, selected = materialize_homogeneous_boarddb(
        output_path=output_path,
        name=name,
        description=(
            "2025 EDA Elite public interconnect topology populated with a "
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
                "topology": (
                    str(topology_path)
                    if topology_path is not None
                    else "initial_topology"
                ),
                "capacity_semantics": (
                    "shared-bidirectional-tdm-projection"
                ),
                "timing_projection": (
                    "configured-boarddb-link-clock-and-latency"
                ),
            }
        },
    )
    constraints_output = route_constraints_path or output_path.with_name(
        f"{output_path.stem}.route_constraints.json"
    )
    write_json(
        constraints_output,
        {
            "schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
            "frame_slots": instance["parameters"]["max_ratio"],
            "tdm_ratio_quantum": instance["parameters"][
                "ratio_quantum"
            ],
            "shared_capacity_links": [link["id"] for link in links],
        },
    )
    report = {
        "schema": EDA2025_BOARDDB_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "platform": validated.name,
        "contest_instance": instance["name"],
        "device_template": template_platform.name,
        "template_fpga": selected["id"],
        "fpgas": len(validated.fpgas),
        "links": len(validated.links),
        "contest_channels": sum(
            topology[left][right]
            for left in range(len(fpga_ids))
            for right in range(left + 1, len(fpga_ids))
        ),
        "data_lanes": sum(link.data_lanes_per_direction for link in validated.links),
        "topology_changes": changed_channels,
        "allowed_topology_changes": allowed_changes,
        "capacity_semantics": "shared-bidirectional-tdm-projection",
        "output": str(output_path),
        "route_constraints": str(constraints_output),
    }
    return report


def _write_topology_optimizer_input(
    path: Path,
    instance: Mapping[str, Any],
    topology: Sequence[Sequence[int]],
    routes: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    change_budget: int,
    enable_shortcuts: bool,
) -> None:
    fpga_ids = instance["fpga_ids"]
    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    pair_records = evaluation["pair_utilization"]
    pairs = [tuple(record["fpgas"]) for record in pair_records]
    pair_index = {pair: index for index, pair in enumerate(pairs)}
    pair_delays = {
        pair: record["hop_delay_ns"]
        for pair, record in zip(pairs, pair_records)
    }
    paths = []
    for route in routes["routes"]:
        for sink, (nodes, _) in _route_sink_paths(route, pair_delays).items():
            paths.append(
                (
                    fpga_index[route["source"]],
                    fpga_index[sink],
                    [
                        pair_index[tuple(sorted((left, right)))]
                        for left, right in zip(nodes, nodes[1:])
                    ],
                )
            )
    parameters = instance["parameters"]
    lines = [
        "EMUFLOW_EDA2025_TOPOLOGY_V2",
        " ".join(
            str(value)
            for value in (
                len(fpga_ids),
                len(pairs),
                change_budget,
                parameters["ratio_quantum"],
                parameters["max_ratio"],
                parameters["alpha_ns"],
                parameters["beta_ns"],
                int(enable_shortcuts),
            )
        ),
        " ".join(
            str(instance["max_external_channels"][fpga_id])
            for fpga_id in fpga_ids
        ),
        *(" ".join(str(channel) for channel in row) for row in topology),
        *(
            f"{min(fpga_index[pair[0]], fpga_index[pair[1]])} "
            f"{max(fpga_index[pair[0]], fpga_index[pair[1]])} "
            f"{pair_records[index]['routed_nets']}"
            for index, pair in enumerate(pairs)
        ),
        str(len(paths)),
        *(
            f"{source} {sink} {len(edges)} "
            f"{' '.join(str(edge) for edge in edges)}"
            for source, sink, edges in paths
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_topology_optimizer_output(
    path: Path, fpga_count: int
) -> Tuple[Dict[str, float], List[Dict[str, int]], List[List[int]]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines or lines.pop(0) != "EMUFLOW_EDA2025_TOPOLOGY_RESULT_V1":
        raise EmuFlowError("topology optimizer returned an invalid schema")
    metrics: Dict[str, float] = {}
    while lines and lines[0].startswith("METRIC "):
        _, name, value = lines.pop(0).split()
        metrics[name] = float(value)
    if not lines or not lines[0].startswith("PAIR_CHANGES "):
        raise EmuFlowError("topology optimizer omitted pair changes")
    change_count = int(lines.pop(0).split()[1])
    changes = []
    for _ in range(change_count):
        left, right, initial, optimized, load = map(int, lines.pop(0).split())
        changes.append(
            {
                "left": left,
                "right": right,
                "initial_channels": initial,
                "optimized_channels": optimized,
                "routed_nets": load,
            }
        )
    if not lines or lines.pop(0) != f"TOPOLOGY {fpga_count}":
        raise EmuFlowError("topology optimizer omitted the topology matrix")
    topology = [list(map(int, lines.pop(0).split())) for _ in range(fpga_count)]
    if any(len(row) != fpga_count for row in topology) or lines:
        raise EmuFlowError("topology optimizer returned a malformed matrix")
    required_metrics = {
        "initial_worst_path_delay_ns",
        "optimized_worst_path_delay_ns",
        "changed_channels",
        "iterations",
    }
    if set(metrics) != required_metrics:
        raise EmuFlowError("topology optimizer returned incomplete metrics")
    return metrics, changes, topology


def optimize_eda2025_topology(
    instance_path: Path,
    routes_path: Path,
    output_dir: Path,
    executable: Optional[str] = None,
    max_changes: Optional[int] = None,
    current_topology_path: Optional[Path] = None,
    enable_shortcuts: bool = False,
) -> Dict[str, Any]:
    """Refine physical channel counts, then emit rerouting-ready contracts."""
    instance = read_json(instance_path)
    _validate_instance(instance)
    routes = read_json(routes_path)
    baseline = evaluate_eda2025_routes(
        instance_path,
        routes_path,
        new_topology_path=current_topology_path,
    )
    topology = (
        parse_topology(current_topology_path, instance["fpga_ids"])
        if current_topology_path is not None
        else instance["initial_topology"]
    )
    allowed = baseline["topology"]["allowed_changed_channels"]
    already_changed = baseline["topology"]["changed_channels"]
    remaining = allowed - already_changed
    if max_changes is None:
        budget = remaining
    elif isinstance(max_changes, bool) or not isinstance(max_changes, int) or max_changes < 0:
        raise ValidationError("max_changes: expected a non-negative integer")
    else:
        budget = min(max_changes, remaining)

    resolved = resolve_native_executable(
        "emuflow_eda2025_topology_optimizer", executable
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-eda2025-topology-") as temporary:
        root = Path(temporary)
        native_input = root / "topology.in"
        native_output = root / "topology.out"
        _write_topology_optimizer_input(
            native_input,
            instance,
            topology,
            routes,
            baseline,
            budget,
            enable_shortcuts,
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
                "in-tree EDA 2025 topology optimizer failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        metrics, raw_changes, optimized = _parse_topology_optimizer_output(
            native_output, len(instance["fpga_ids"])
        )

    _check_io_limits(
        optimized,
        instance["fpga_ids"],
        instance["max_external_channels"],
        "optimized topology",
    )
    _, iteration_changes = _topology_change(topology, optimized)
    initial_channels, changed_channels = _topology_change(
        instance["initial_topology"], optimized
    )
    if changed_channels > allowed:
        raise EmuFlowError("topology optimizer exceeded the contest change budget")
    output_dir.mkdir(parents=True, exist_ok=True)
    topology_path = output_dir / "design.newtopo"
    _write_topology_text(topology_path, instance["fpga_ids"], optimized)
    artifacts = _write_execution_adapter(instance, optimized, output_dir / "normalized")
    changes = [
        {
            "fpgas": [
                instance["fpga_ids"][change["left"]],
                instance["fpga_ids"][change["right"]],
            ],
            "previous_channels": change["initial_channels"],
            "optimized_channels": change["optimized_channels"],
            "routed_nets": change["routed_nets"],
        }
        for change in raw_changes
    ]
    report = {
        "schema": EDA2025_TOPOLOGY_OPTIMIZATION_SCHEMA,
        "status": "pass",
        "provider": "quantized-minimax-topology-refinement-v1",
        "method": {
            "objective": "routing-guided quantized maximum path delay",
            "move": "minimum channel addition crossing a TDM quantum boundary",
            "selection": "critical-window minimax with smooth pressure tie-break",
            "shortcut_links": enable_shortcuts,
            "routing_closure": "rerun Phase 4 on emitted normalized contracts",
        },
        "metrics": {
            "initial_worst_path_delay_ns": metrics[
                "initial_worst_path_delay_ns"
            ],
            "predicted_worst_path_delay_ns": metrics[
                "optimized_worst_path_delay_ns"
            ],
            "iteration_changed_channels": iteration_changes,
            "changed_channels": changed_channels,
            "input_changed_channels": already_changed,
            "allowed_changed_channels": allowed,
            "initial_channels": initial_channels,
            "iterations": int(metrics["iterations"]),
            "changed_pairs": len(changes),
        },
        "changes": changes,
        "artifacts": {"topology": str(topology_path), **artifacts},
    }
    write_json(output_dir / "topology_optimization.json", report)
    return report


def _optimize_eda2025_routing_round(
    instance_path: Path,
    routes_path: Path,
    output_dir: Path,
    router: Optional[str] = None,
    topology_optimizer: Optional[str] = None,
    current_topology_path: Optional[Path] = None,
    enable_shortcut_portfolio: bool = True,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate topology neighborhoods with real Phase 4 rerouting closure."""
    from .phase4 import run_phase4

    instance = read_json(instance_path)
    _validate_instance(instance)
    baseline = evaluate_eda2025_routes(
        instance_path,
        routes_path,
        new_topology_path=current_topology_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_root = output_dir / "candidates"
    candidate_root.mkdir(exist_ok=True)
    records = [
        {
            "name": "baseline",
            "worst_path_delay_ns": baseline["metrics"]["worst_path_delay_ns"],
            "changed_channels": baseline["topology"]["changed_channels"],
            "rerouted": False,
            "accepted": False,
            "root": None,
        }
    ]
    strategies = [("capacity", False)]
    if enable_shortcut_portfolio:
        strategies.append(("capacity-and-shortcuts", True))
    for name, shortcuts in strategies:
        root = candidate_root / name
        proposal = optimize_eda2025_topology(
            instance_path=instance_path,
            routes_path=routes_path,
            output_dir=root,
            executable=topology_optimizer,
            current_topology_path=current_topology_path,
            enable_shortcuts=shortcuts,
        )
        if proposal["metrics"]["iteration_changed_channels"] == 0:
            records.append(
                {
                    "name": name,
                    "worst_path_delay_ns": baseline["metrics"][
                        "worst_path_delay_ns"
                    ],
                    "changed_channels": baseline["topology"][
                        "changed_channels"
                    ],
                    "rerouted": False,
                    "accepted": False,
                    "root": str(root),
                }
            )
            continue
        normalized = root / "normalized"
        routed = root / "routed"
        run_phase4(
            assignment_path=normalized / "partition_assignment.json",
            platform_path=normalized / "boarddb.json",
            output_dir=routed,
            constraints_path=normalized / "route_constraints.json",
            timing_paths_path=normalized / "contest_timing_paths.json",
            provider=provider,
            router=router,
        )
        evaluation = evaluate_eda2025_routes(
            instance_path,
            routed / "routes.json",
            output_path=root / "evaluation.json",
            new_topology_path=root / "design.newtopo",
        )
        records.append(
            {
                "name": name,
                "worst_path_delay_ns": evaluation["metrics"][
                    "worst_path_delay_ns"
                ],
                "changed_channels": evaluation["topology"][
                    "changed_channels"
                ],
                "rerouted": True,
                "accepted": False,
                "root": str(root),
            }
        )
    best = min(
        records,
        key=lambda record: (
            record["worst_path_delay_ns"],
            record["changed_channels"],
            record["name"],
        ),
    )
    if best["worst_path_delay_ns"] >= records[0]["worst_path_delay_ns"] - 1.0e-9:
        best = records[0]
    best["accepted"] = best["name"] != "baseline"
    selected = output_dir / "selected"
    if selected.exists():
        shutil.rmtree(selected)
    selected.mkdir()
    if best["name"] == "baseline":
        topology = (
            parse_topology(current_topology_path, instance["fpga_ids"])
            if current_topology_path is not None
            else instance["initial_topology"]
        )
        _write_topology_text(
            selected / "design.newtopo", instance["fpga_ids"], topology
        )
        _write_execution_adapter(instance, topology, selected / "normalized")
        (selected / "routed").mkdir()
        shutil.copy2(routes_path, selected / "routed" / "routes.json")
        selected_evaluation = baseline
    else:
        source = Path(best["root"])
        shutil.copy2(source / "design.newtopo", selected / "design.newtopo")
        shutil.copytree(source / "normalized", selected / "normalized")
        shutil.copytree(source / "routed", selected / "routed")
        selected_evaluation = read_json(source / "evaluation.json")
    write_json(selected / "evaluation.json", selected_evaluation)
    report = {
        "schema": EDA2025_ROUTING_OPTIMIZATION_SCHEMA,
        "status": "pass",
        "provider": "rerouting-closed-topology-portfolio-v1",
        "objective": "minimum independently evaluated worst path delay",
        "candidates": records,
        "selected": {
            "name": best["name"],
            "improved": best["accepted"],
            "worst_path_delay_ns": best["worst_path_delay_ns"],
            "baseline_worst_path_delay_ns": baseline["metrics"][
                "worst_path_delay_ns"
            ],
            "root": str(selected),
        },
    }
    write_json(output_dir / "routing_optimization.json", report)
    return report


def optimize_eda2025_routing(
    instance_path: Path,
    routes_path: Path,
    output_dir: Path,
    router: Optional[str] = None,
    topology_optimizer: Optional[str] = None,
    current_topology_path: Optional[Path] = None,
    enable_shortcut_portfolio: bool = True,
    max_rounds: int = 4,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Run rerouting-closed topology rounds to convergence or budget."""
    if (
        isinstance(max_rounds, bool)
        or not isinstance(max_rounds, int)
        or max_rounds <= 0
    ):
        raise ValidationError("max_rounds: expected a positive integer")
    instance = read_json(instance_path)
    _validate_instance(instance)
    initial = evaluate_eda2025_routes(
        instance_path,
        routes_path,
        new_topology_path=current_topology_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    rounds_root = output_dir / "rounds"
    rounds_root.mkdir(exist_ok=True)
    active_routes = routes_path
    active_topology = current_topology_path
    round_records = []
    final_round = None
    final_selected = None
    termination = "max_rounds"
    for round_index in range(1, max_rounds + 1):
        round_root = rounds_root / f"round_{round_index:03d}"
        round_report = _optimize_eda2025_routing_round(
            instance_path=instance_path,
            routes_path=active_routes,
            output_dir=round_root,
            router=router,
            provider=provider,
            topology_optimizer=topology_optimizer,
            current_topology_path=active_topology,
            enable_shortcut_portfolio=enable_shortcut_portfolio,
        )
        final_round = round_report
        final_selected = round_root / "selected"
        round_records.append(
            {
                "round": round_index,
                "baseline_worst_path_delay_ns": round_report["selected"][
                    "baseline_worst_path_delay_ns"
                ],
                "selected": round_report["selected"]["name"],
                "improved": round_report["selected"]["improved"],
                "worst_path_delay_ns": round_report["selected"][
                    "worst_path_delay_ns"
                ],
                "candidates": round_report["candidates"],
                "root": str(round_root),
            }
        )
        selected_evaluation = read_json(final_selected / "evaluation.json")
        if not round_report["selected"]["improved"]:
            termination = "no_strict_improvement"
            break
        active_routes = final_selected / "routed" / "routes.json"
        active_topology = final_selected / "design.newtopo"
        if (
            selected_evaluation["topology"]["changed_channels"]
            >= selected_evaluation["topology"]["allowed_changed_channels"]
        ):
            termination = "topology_change_budget_exhausted"
            break
    assert final_round is not None and final_selected is not None
    selected = output_dir / "selected"
    if selected.exists():
        shutil.rmtree(selected)
    shutil.copytree(final_selected, selected)
    final_evaluation = read_json(selected / "evaluation.json")
    final_delay = final_evaluation["metrics"]["worst_path_delay_ns"]
    initial_delay = initial["metrics"]["worst_path_delay_ns"]
    report = {
        "schema": EDA2025_ROUTING_OPTIMIZATION_SCHEMA,
        "status": "pass",
        "provider": "iterative-rerouting-closed-topology-portfolio-v2",
        "objective": "minimum independently evaluated worst path delay",
        "configuration": {
            "max_rounds": max_rounds,
            "shortcut_portfolio": enable_shortcut_portfolio,
        },
        "rounds": round_records,
        "termination": termination,
        "selected": {
            "name": final_round["selected"]["name"],
            "improved": final_delay < initial_delay - 1.0e-9,
            "rounds_completed": len(round_records),
            "worst_path_delay_ns": final_delay,
            "baseline_worst_path_delay_ns": initial_delay,
            "changed_channels": final_evaluation["topology"][
                "changed_channels"
            ],
            "allowed_changed_channels": final_evaluation["topology"][
                "allowed_changed_channels"
            ],
            "root": str(selected),
        },
    }
    write_json(output_dir / "routing_optimization.json", report)
    return report


def evaluate_eda2025_routes(
    instance_path: Path,
    routes_path: Path,
    output_path: Optional[Path] = None,
    new_topology_path: Optional[Path] = None,
    runtime_seconds: float = 0.0,
    official_output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    instance = read_json(instance_path)
    _validate_instance(instance)
    routes = read_json(routes_path)
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(
            f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}"
        )
    if (
        isinstance(runtime_seconds, bool)
        or not isinstance(runtime_seconds, (int, float))
        or not math.isfinite(float(runtime_seconds))
        or runtime_seconds < 0
    ):
        raise ValidationError("runtime_seconds: expected a finite non-negative number")
    fpga_ids = instance["fpga_ids"]
    topology = (
        parse_topology(new_topology_path, fpga_ids)
        if new_topology_path is not None
        else instance["initial_topology"]
    )
    _check_io_limits(
        topology,
        fpga_ids,
        instance["max_external_channels"],
        "evaluated topology",
    )
    initial_channels, changed_channels = _topology_change(
        instance["initial_topology"], topology
    )
    allowed_changes = math.floor(
        initial_channels
        * instance["parameters"]["topology_change_fraction"]
        + 1.0e-12
    )
    if changed_channels > allowed_changes:
        raise ValidationError(
            f"topology changes {changed_channels} channels; allowed {allowed_changes}"
        )

    net_by_id = {net["id"]: net for net in instance["nets"]}
    node_assignment = instance["node_assignment"]
    expected = {}
    for net_id, net in net_by_id.items():
        source = node_assignment[net["source_node"]]
        sinks = sorted(
            {
                node_assignment[node]
                for node in net["sink_nodes"]
                if node_assignment[node] != source
            }
        )
        if sinks:
            expected[net_id] = (source, sinks)
    raw_routes = routes.get("routes")
    if not isinstance(raw_routes, list):
        raise ValidationError("routes.routes: expected an array")
    route_by_net = {
        route.get("net"): route for route in raw_routes if isinstance(route, dict)
    }
    if len(route_by_net) != len(raw_routes) or set(route_by_net) != set(expected):
        raise ValidationError("routes.routes: contest cut-net coverage is not exact")

    pair_nets: Dict[Tuple[str, str], set] = defaultdict(set)
    for net_id, route in route_by_net.items():
        source, sinks = expected[net_id]
        if route.get("source") != source or route.get("sinks") != sinks:
            raise ValidationError(
                f"route for {net_id!r}: source/sinks do not match contest assignment"
            )
        raw_edges = route.get("tree_edges")
        if not isinstance(raw_edges, list):
            raise ValidationError(f"route for {net_id!r}: expected tree_edges")
        for edge in raw_edges:
            if not isinstance(edge, dict):
                raise ValidationError(f"route for {net_id!r}: malformed edge")
            left, right = edge.get("from"), edge.get("to")
            if (
                not isinstance(left, str)
                or not isinstance(right, str)
                or left == right
            ):
                raise ValidationError(f"route for {net_id!r}: malformed edge")
            pair_nets[tuple(sorted((left, right)))].add(net_id)

    index = {fpga_id: item for item, fpga_id in enumerate(fpga_ids)}
    pair_records = []
    pair_delays = {}
    quantum = instance["parameters"]["ratio_quantum"]
    for pair in sorted(pair_nets):
        if len(pair) != 2 or pair[0] not in index or pair[1] not in index:
            raise ValidationError(f"routes use unknown FPGA pair {pair}")
        channels = topology[index[pair[0]]][index[pair[1]]]
        if channels <= 0:
            raise ValidationError(f"routes use disconnected FPGA pair {pair}")
        load = len(pair_nets[pair])
        ratio = math.ceil(load / channels / quantum) * quantum
        if ratio > instance["parameters"]["max_ratio"]:
            raise ValidationError(
                f"FPGA pair {pair} requires TDM ratio {ratio}, above "
                f"Rmax {instance['parameters']['max_ratio']}"
            )
        delay = (
            instance["parameters"]["beta_ns"]
            + instance["parameters"]["alpha_ns"] * ratio
        )
        pair_delays[pair] = delay
        pair_records.append(
            {
                "fpgas": list(pair),
                "channels": channels,
                "routed_nets": load,
                "tdm_ratio": ratio,
                "hop_delay_ns": delay,
            }
        )

    net_records = []
    worst_delay = 0.0
    for net_id in sorted(route_by_net):
        sink_delays = _route_path_delays(route_by_net[net_id], pair_delays)
        maximum = max(sink_delays.values())
        worst_delay = max(worst_delay, maximum)
        net_records.append(
            {
                "net": net_id,
                "sink_delay_ns": dict(sorted(sink_delays.items())),
                "max_delay_ns": maximum,
            }
        )
    score = worst_delay * (1.0 + 0.2 * runtime_seconds / 3600.0)
    result = {
        "schema": EDA2025_EVALUATION_SCHEMA,
        "status": "pass",
        "instance": instance["name"],
        "model": {
            "tdm_ratio": "ceil_to_quantum(unique_routed_nets/channels)",
            "hop_delay_ns": "beta_ns + alpha_ns * tdm_ratio",
            "topology_change_counting": "undirected upper triangle",
        },
        "topology": {
            "initial_channels": initial_channels,
            "changed_channels": changed_channels,
            "allowed_changed_channels": allowed_changes,
        },
        "pair_utilization": pair_records,
        "nets": net_records,
        "metrics": {
            "routed_cut_nets": len(net_records),
            "used_fpga_pairs": len(pair_records),
            "max_tdm_ratio": max(
                (record["tdm_ratio"] for record in pair_records), default=0
            ),
            "worst_path_delay_ns": worst_delay,
            "runtime_seconds": float(runtime_seconds),
            "contest_score": score,
        },
    }
    if official_output_dir is not None:
        _write_official_solution(
            official_output_dir,
            instance,
            route_by_net,
            pair_delays,
            topology,
        )
    if output_path is not None:
        write_json(output_path, result)
    return result
