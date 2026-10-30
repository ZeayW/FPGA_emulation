"""Official-format adapter and checker for the 2023 EDA Elite die router."""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, cast

from .contest_boarddb import materialize_homogeneous_boarddb
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow.common.native_tools import resolve_native_executable
from emuflow.partition.partition import PARTITION_ASSIGNMENT_SCHEMA
from emuflow.routing.routing import SYSTEM_ROUTE_CONSTRAINTS_SCHEMA, SYSTEM_ROUTES_SCHEMA

EDA2023_INSTANCE_SCHEMA = "emuflow.contest-eda2023-instance/v1"
EDA2023_HIERARCHY_SCHEMA = "emuflow.die-hierarchy/v1"
EDA2023_TDM_SCHEMA = "emuflow.contest-eda2023-tdm/v1"
EDA2023_TDM_PROVIDER = "cpp-lagrangian-kkt-direction-separated-v1"
EDA2023_TIMING_DAG_PROVIDER = "aspdac26-timing-dag-lagrangian-v1"
EDA2023_EVALUATION_SCHEMA = "emuflow.contest-eda2023-evaluation/v1"
EDA2023_BOARDDB_MATERIALIZATION_SCHEMA = (
    "emuflow.contest-eda2023-boarddb-materialization/v1"
)
EDA2023_SOURCE_URL = (
    "https://eda.icisc.cn/file/cacheFile/"
    "4f769715b1704172935438d418702f80.pdf"
)
_FPGA = re.compile(r"FPGA([0-9]+)\Z")
_DIE = re.compile(r"Die([0-9]+)\Z")

def _nonempty_lines(path: Path) -> Iterator[Tuple[int, str]]:
    with path.open("r", encoding="utf-8") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.strip()
            if line:
                yield number, line

def _split_mapping(path: Path, number: int, line: str) -> Tuple[str, List[str]]:
    key, separator, values = line.partition(":")
    if not separator or not key.strip():
        raise ValidationError(f"{path}:{number}: expected '<name>: <values>'")
    return key.strip(), values.split()

def _numeric_id(value: str, pattern: re.Pattern[str], context: str) -> int:
    match = pattern.fullmatch(value)
    if match is None:
        raise ValidationError(f"{context}: invalid identifier {value!r}")
    return int(match.group(1))

def _parse_fpga_dies(path: Path) -> Tuple[List[str], List[str], Dict[str, str]]:
    fpgas: List[str] = []
    dies: List[str] = []
    die_to_fpga: Dict[str, str] = {}
    for number, line in _nonempty_lines(path):
        fpga, members = _split_mapping(path, number, line)
        fpga_index = _numeric_id(fpga, _FPGA, f"{path}:{number}")
        if fpga_index != len(fpgas):
            raise ValidationError(f"{path}:{number}: FPGA IDs must be contiguous")
        if not members:
            raise ValidationError(f"{path}:{number}: FPGA has no dies")
        fpgas.append(fpga)
        for die in members:
            _numeric_id(die, _DIE, f"{path}:{number}")
            if die in die_to_fpga:
                raise ValidationError(f"{path}:{number}: die {die!r} is assigned twice")
            dies.append(die)
            die_to_fpga[die] = fpga
    if not fpgas:
        raise ValidationError(f"{path}: expected at least one FPGA")
    ordered = sorted(dies, key=lambda die: _numeric_id(die, _DIE, str(path)))
    if ordered != [f"Die{index}" for index in range(len(ordered))]:
        raise ValidationError(f"{path}: Die IDs must be contiguous from Die0")
    return fpgas, ordered, die_to_fpga

def _parse_positions(path: Path, dies: Iterable[str]) -> Dict[str, str]:
    known = set(dies)
    current: Optional[str] = None
    seen_dies = set()
    positions: Dict[str, str] = {}
    for number, line in _nonempty_lines(path):
        if ":" in line:
            current, nodes = _split_mapping(path, number, line)
            if current not in known:
                raise ValidationError(f"{path}:{number}: unknown die {current!r}")
            if current in seen_dies:
                raise ValidationError(f"{path}:{number}: duplicate die {current!r}")
            seen_dies.add(current)
        elif current is None:
            raise ValidationError(f"{path}:{number}: continuation before a Die row")
        else:
            nodes = line.split()
        for node in nodes:
            if node in positions:
                raise ValidationError(f"{path}:{number}: node {node!r} is assigned twice")
            positions[node] = current
    missing = sorted(known - seen_dies)
    if missing:
        raise ValidationError(f"{path}: missing Die rows {missing}")
    return positions

def _parse_network(path: Path, count: int) -> List[List[int]]:
    matrix: List[List[int]] = []
    for number, line in _nonempty_lines(path):
        try:
            row = [int(value) for value in line.split()]
        except ValueError as error:
            raise ValidationError(f"{path}:{number}: non-integer capacity") from error
        if len(row) != count or any(value < 0 for value in row):
            raise ValidationError(f"{path}:{number}: expected {count} non-negative capacities")
        matrix.append(row)
    if len(matrix) != count:
        raise ValidationError(f"{path}: expected {count} matrix rows")
    for left in range(count):
        if matrix[left][left] != 0:
            raise ValidationError(f"{path}: network diagonal must be zero")
        for right in range(left + 1, count):
            if matrix[left][right] != matrix[right][left]:
                raise ValidationError(f"{path}: network matrix must be symmetric")
    return matrix

def _parse_nets(path: Path, positions: Mapping[str, str]) -> List[Dict[str, Any]]:
    nets: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_sink_nodes = set()
    with path.open("r", encoding="utf-8") as stream:
        for zero_based_line, raw in enumerate(stream):
            line = raw.strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) not in {2, 3} or fields[1] not in {"s", "l"}:
                raise ValidationError(f"{path}:{zero_based_line + 1}: malformed net record")
            node, kind = fields[:2]
            if node not in positions:
                raise ValidationError(f"{path}:{zero_based_line + 1}: unplaced node {node!r}")
            if kind == "s":
                if len(fields) != 3 or fields[2] != "1":
                    raise ValidationError(f"{path}:{zero_based_line + 1}: source weight must be 1")
                current = {
                    "id": zero_based_line,
                    "source_node": node,
                    "source_die": positions[node],
                    "sink_nodes": [],
                    "sink_dies": [],
                }
                nets.append(current)
                current_sink_nodes = set()
            else:
                if len(fields) != 2 or current is None:
                    raise ValidationError(f"{path}:{zero_based_line + 1}: load has no source")
                # A high-fanout contest net can contain hundreds of thousands
                # of loads.  List membership made parsing quadratic; retain
                # ordered output in the list while checking uniqueness in O(1).
                if node == current["source_node"] or node in current_sink_nodes:
                    raise ValidationError(f"{path}:{zero_based_line + 1}: duplicate net endpoint")
                current_sink_nodes.add(node)
                current["sink_nodes"].append(node)
                current["sink_dies"].append(positions[node])
    if not nets or any(not net["sink_nodes"] for net in nets):
        raise ValidationError(f"{path}: every net must have one or more loads")
    return nets

def parse_eda2023_case(case_dir: Path, name: str) -> Dict[str, Any]:
    if not name.strip():
        raise ValidationError("name: expected a non-empty string")
    required = {
        "fpga_die": case_dir / "design.fpga.die",
        "die_position": case_dir / "design.die.position",
        "die_network": case_dir / "design.die.network",
        "net": case_dir / "design.net",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise ValidationError(f"case directory is missing required files: {missing}")
    fpgas, dies, die_to_fpga = _parse_fpga_dies(required["fpga_die"])
    positions = _parse_positions(required["die_position"], dies)
    matrix = _parse_network(required["die_network"], len(dies))
    nets = _parse_nets(required["net"], positions)
    links = []
    for left in range(len(dies)):
        for right in range(left + 1, len(dies)):
            capacity = matrix[left][right]
            if capacity == 0:
                continue
            left_die, right_die = dies[left], dies[right]
            kind = (
                "sll"
                if die_to_fpga[left_die] == die_to_fpga[right_die]
                else "wire"
            )
            links.append(
                {
                    "id": f"die_link_{left:03d}_{right:03d}",
                    "endpoints": [left_die, right_die],
                    "capacity": capacity,
                    "kind": kind,
                }
            )
    if not links:
        raise ValidationError(f"{required['die_network']}: empty topology")
    return {
        "schema": EDA2023_INSTANCE_SCHEMA,
        "name": name,
        "source": {
            "contest": "2023 EDA Elite Challenge",
            "problem": "FPGA Die-level System Routing Algorithm Design",
            "specification_url": EDA2023_SOURCE_URL,
        },
        "parameters": {
            "sll_delay": 1.0,
            "tdm_alpha": 1.0,
            "tdm_beta": 2.0,
            "routing_weight_inter_fpga": 0.5,
            "tdm_ratio_quantum": 4,
        },
        "fpgas": fpgas,
        "dies": dies,
        "die_to_fpga": die_to_fpga,
        "node_positions": positions,
        "links": links,
        "nets": nets,
    }

def _net_name(net_id: int) -> str:
    return f"net_{net_id:07d}"

def _physical_fpga_diameter(instance: Mapping[str, Any]) -> int:
    adjacency = {fpga: set() for fpga in instance["fpgas"]}
    for link in instance["links"]:
        if link["kind"] != "wire":
            continue
        left, right = (
            instance["die_to_fpga"][die] for die in link["endpoints"]
        )
        if left != right:
            adjacency[left].add(right)
            adjacency[right].add(left)
    diameter = 0
    for source in adjacency:
        distance = {source: 0}
        pending = deque([source])
        while pending:
            current = pending.popleft()
            for neighbor in adjacency[current]:
                if neighbor not in distance:
                    distance[neighbor] = distance[current] + 1
                    pending.append(neighbor)
        if len(distance) != len(adjacency):
            raise ValidationError("EDA 2023 physical-FPGA graph is disconnected")
        diameter = max(diameter, max(distance.values(), default=0))
    return diameter

def _timing_weight_for_fpga_diameter(diameter: int) -> float:
    if diameter <= 1:
        return 0.0
    if diameter == 2:
        return 0.5
    return 4.0

def import_eda2023_case(case_dir: Path, output_dir: Path, name: str) -> Dict[str, Any]:
    instance = parse_eda2023_case(case_dir, name)
    wire_links = [link for link in instance["links"] if link["kind"] == "wire"]
    sll_links = [link for link in instance["links"] if link["kind"] == "sll"]
    if not wire_links:
        raise ValidationError("EDA 2023 case has no inter-FPGA Wire")
    fpga_diameter = _physical_fpga_diameter(instance)
    instance["parameters"]["physical_fpga_diameter"] = fpga_diameter
    cross_nets = [
        net
        for net in instance["nets"]
        if any(die != net["source_die"] for die in net["sink_dies"])
    ]
    max_ratio = max(4, 4 * math.ceil(len(cross_nets) / 4))
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": name,
            "kind": "virtual",
            "description": "Official 2023 EDA Elite die graph with SLL and Wire links",
        },
        "fpgas": [
            {
                "id": die,
                "part": "eda2023-contest-die",
                "utilization_limit": 1.0,
                "capacity": {"lut": 1},
            }
            for die in instance["dies"]
        ],
        "links": [
            {
                "id": link["id"],
                "endpoints": link["endpoints"],
                "direction": "full_duplex",
                "mode": "abstract",
                "data_lanes_per_direction": link["capacity"],
                "fabric_clock_mhz": 1000.0,
                "latency_cycles": 0,
            }
            for link in instance["links"]
        ],
    }
    hierarchy = {
        "schema": EDA2023_HIERARCHY_SCHEMA,
        "platform": name,
        "physical_fpgas": [
            {
                "id": fpga,
                "dies": [die for die in instance["dies"] if instance["die_to_fpga"][die] == fpga],
            }
            for fpga in instance["fpgas"]
        ],
        "links": [
            {"id": link["id"], "kind": link["kind"], "capacity": link["capacity"]}
            for link in instance["links"]
        ],
    }
    cut_nets = []
    for net in cross_nets:
        sinks = sorted(set(net["sink_dies"]) - {net["source_die"]})
        if not sinks:
            continue
        cut_nets.append(
            {
                "net": _net_name(net["id"]),
                "cut_class": "register_output",
                "source_fpgas": [net["source_die"]],
                "sink_fpgas": sinks,
                "sink_endpoints": len(sinks),
            }
        )
    assignment = {
        "schema": PARTITION_ASSIGNMENT_SCHEMA,
        "design": name,
        "platform": name,
        "cut_nets": cut_nets,
    }
    longest_tree = max(1, len(instance["dies"]) - 1)
    period = float(longest_tree * (0.5 + max_ratio))
    timing_paths = {
        "schema": "emuflow.sta-paths/v1",
        "design": name,
        "paths": [
            {
                "id": f"contest_path_{net['id']:07d}",
                "clock_domain": "eda2023_max_routing_weight",
                "clock_period_ns": period,
                "slack_ns": 0.0,
                "fixed_delay_ns": 0.0,
                "cut_nets": [_net_name(net["id"])],
            }
            for net in cross_nets
            if set(net["sink_dies"]) - {net["source_die"]}
        ],
    }
    link_delays = {
        link["id"]: (1.0 if link["kind"] == "sll" else 1.5)
        for link in instance["links"]
    }
    constraints = {
        "schema": "emuflow.system-route-constraints/v1",
        "frame_slots": max_ratio,
        "max_iterations": 50,
        "unavailable_links": [],
        "link_delay_ns": link_delays,
        "sll_links": [link["id"] for link in sll_links],
        "shared_capacity_links": [link["id"] for link in instance["links"]],
        "tree_edge_sum_tdm": False,
        "reroute_rounds": 8,
        # Contest paths start with equal criticality.  Normalize the timing
        # term by the physical-FPGA hop diameter: short topologies prioritize
        # capacity balance, while long topologies must price accumulated TDM
        # delay strongly enough to avoid locally cheap but globally slow paths.
        "lambda_load": 68.0,
        "lambda_timing": _timing_weight_for_fpga_diameter(fpga_diameter),
        "lambda_history": 1.0,
        "lambda_tdm": 1.0,
        "tdm_ratio_quantum": 4,
        "tdm_min_ratio": 4,
        "hard_sll_capacity": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "instance": output_dir / "contest_instance.json",
        "boarddb": output_dir / "boarddb.json",
        "hierarchy": output_dir / "die_hierarchy.json",
        "assignment": output_dir / "partition_assignment.json",
        "route_constraints": output_dir / "route_constraints.json",
        "timing_paths": output_dir / "contest_timing_paths.json",
    }
    for key, value in (
        ("instance", instance),
        ("boarddb", boarddb),
        ("hierarchy", hierarchy),
        ("assignment", assignment),
        ("route_constraints", constraints),
        ("timing_paths", timing_paths),
    ):
        write_json(artifacts[key], value)
    return {
        "status": "pass",
        "schema": EDA2023_INSTANCE_SCHEMA,
        "name": name,
        "physical_fpgas": len(instance["fpgas"]),
        "physical_fpga_diameter": fpga_diameter,
        "dies": len(instance["dies"]),
        "sll_links": len(sll_links),
        "wire_links": len(wire_links),
        "nets": len(instance["nets"]),
        "routed_nets": len(cut_nets),
        "max_ratio_bound": max_ratio,
        "artifacts": {key: str(path) for key, path in artifacts.items()},
    }

def _load_instance(path: Path) -> Dict[str, Any]:
    instance = read_json(path)
    if instance.get("schema") != EDA2023_INSTANCE_SCHEMA:
        raise ValidationError(f"instance.schema: expected {EDA2023_INSTANCE_SCHEMA!r}")
    return instance

def materialize_eda2023_rtl_boarddb(
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
    route_constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Project public die-level Wire banks onto physical FPGA vertices."""
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

    instance = _load_instance(instance_path)
    wire_links = [link for link in instance["links"] if link["kind"] == "wire"]
    sll_links = [link for link in instance["links"] if link["kind"] == "sll"]
    if not wire_links:
        raise ValidationError("EDA 2023 instance has no inter-FPGA Wire bank")

    links = []
    for link in wire_links:
        left_die, right_die = link["endpoints"]
        endpoints = [
            instance["die_to_fpga"][left_die],
            instance["die_to_fpga"][right_die],
        ]
        if endpoints[0] == endpoints[1]:
            raise ValidationError(
                f"Wire bank {link['id']!r} is not inter-FPGA"
            )
        links.append(
            {
                "id": f"eda2023_{link['id']}",
                "endpoints": endpoints,
                "direction": "full_duplex",
                "capacity_sharing": "shared_bidirectional",
                "mode": link_mode,
                "data_lanes_per_direction": link["capacity"] * lane_scale,
                "fabric_clock_mhz": float(fabric_clock_mhz),
                "latency_cycles": latency_cycles,
            }
        )

    validated, template_platform, selected = materialize_homogeneous_boarddb(
        output_path=output_path,
        name=name,
        description=(
            "2023 EDA Elite public die topology projected onto physical FPGA "
            "vertices with every inter-FPGA Wire bank preserved"
        ),
        fpga_ids=instance["fpgas"],
        links=links,
        device_template_path=device_template_path,
        template_fpga_id=template_fpga_id,
        provenance={
            "interconnect": {
                "schema": instance["schema"],
                "instance": instance["name"],
                "specification_url": instance["source"]["specification_url"],
                "projection": "physical-fpga-wire-bank",
                "capacity_semantics": (
                    "fixed-direction-lane-groups-shared-bank"
                ),
                "collapsed_sll_links": len(sll_links),
            }
        },
    )
    cross_nets = [
        net
        for net in instance["nets"]
        if any(
            instance["die_to_fpga"][sink]
            != instance["die_to_fpga"][net["source_die"]]
            for sink in net["sink_dies"]
        )
    ]
    ratio_quantum = instance["parameters"]["tdm_ratio_quantum"]
    max_ratio = max(
        ratio_quantum,
        ratio_quantum * math.ceil(max(1, len(cross_nets)) / ratio_quantum),
    )
    constraints_output = route_constraints_path or output_path.with_name(
        f"{output_path.stem}.route_constraints.json"
    )
    write_json(
        constraints_output,
        {
            "schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
            "frame_slots": max_ratio,
            "max_route_hops": instance["parameters"].get(
                "physical_fpga_diameter", _physical_fpga_diameter(instance)
            ),
            "tdm_ratio_quantum": ratio_quantum,
            "tdm_min_ratio": ratio_quantum,
            "shared_capacity_links": [link.id for link in validated.links],
        },
    )
    return {
        "schema": EDA2023_BOARDDB_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "platform": validated.name,
        "contest_instance": instance["name"],
        "device_template": template_platform.name,
        "template_fpga": selected["id"],
        "physical_fpgas": len(validated.fpgas),
        "dies": len(instance["dies"]),
        "wire_banks": len(validated.links),
        "collapsed_sll_links": len(sll_links),
        "data_lanes": sum(
            link.data_lanes_per_direction for link in validated.links
        ),
        "capacity_semantics": "fixed-direction-lane-groups-shared-bank",
        "output": str(output_path),
        "route_constraints": str(constraints_output),
    }

def _route_model(instance: Mapping[str, Any], routes: Mapping[str, Any]) -> Dict[str, Any]:
    if routes.get("schema") != SYSTEM_ROUTES_SCHEMA:
        raise ValidationError(f"routes.schema: expected {SYSTEM_ROUTES_SCHEMA!r}")
    all_nets = {_net_name(net["id"]): net for net in instance["nets"]}
    net_by_name = {
        name: net
        for name, net in all_nets.items()
        if set(net["sink_dies"]) - {net["source_die"]}
    }
    route_by_name = {route.get("net"): route for route in routes.get("routes", [])}
    if set(route_by_name) != set(net_by_name):
        raise ValidationError("routes: routed-net coverage is not exact")
    link_by_id = {link["id"]: link for link in instance["links"]}
    cross_hops: List[Dict[str, Any]] = []
    hop_by_key: Dict[Tuple[str, str], int] = {}
    paths = []
    route_paths: Dict[str, List[Dict[str, Any]]] = {}
    sll_usage: Dict[str, int] = defaultdict(int)
    for name in sorted(net_by_name):
        net = net_by_name[name]
        route = route_by_name[name]
        if route.get("source") != net["source_die"]:
            raise ValidationError(f"route {name}: source die mismatch")
        graph: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        parent: Dict[str, Tuple[str, str]] = {}
        used_links = set()
        for edge in route.get("tree_edges", []):
            link_id = edge.get("link")
            link = link_by_id.get(link_id)
            if link is None or [edge.get("from"), edge.get("to")] not in (
                link["endpoints"], list(reversed(link["endpoints"]))
            ):
                raise ValidationError(f"route {name}: invalid tree edge")
            child = edge["to"]
            if child in parent:
                raise ValidationError(f"route {name}: tree node has two parents")
            parent[child] = (edge["from"], link_id)
            graph[edge["from"]].append((child, link_id))
            if link_id not in used_links:
                used_links.add(link_id)
                if link["kind"] == "sll":
                    sll_usage[link_id] += 1
                else:
                    key = (name, link_id)
                    hop_by_key[key] = len(cross_hops)
                    cross_hops.append(
                        {
                            "index": len(cross_hops),
                            "net": name,
                            "official_net_id": net["id"],
                            "link": link_id,
                            "direction": 0 if edge["from"] == link["endpoints"][0] else 1,
                            "from": edge["from"],
                            "to": edge["to"],
                        }
                    )
        reached = {net["source_die"]}
        queue = deque([net["source_die"]])
        while queue:
            for child, _ in graph[queue.popleft()]:
                if child in reached:
                    raise ValidationError(f"route {name}: cycle in tree")
                reached.add(child)
                queue.append(child)
        disconnected = sorted(set(parent) - reached)
        if disconnected:
            raise ValidationError(
                f"route {name}: disconnected tree nodes {disconnected}"
            )
        expected_sinks = sorted(set(net["sink_dies"]) - {net["source_die"]})
        missing = sorted(set(expected_sinks) - reached)
        if missing:
            raise ValidationError(f"route {name}: misses sink dies {missing}")
        per_sink = []
        for sink in expected_sinks:
            reverse_edges = []
            node = sink
            while node != net["source_die"]:
                if node not in parent:
                    raise ValidationError(f"route {name}: disconnected tree component")
                source, link_id = parent[node]
                reverse_edges.append((source, node, link_id))
                node = source
            edges = list(reversed(reverse_edges))
            hop_indices = [
                hop_by_key[(name, link_id)]
                for _, _, link_id in edges
                if link_by_id[link_id]["kind"] == "wire"
            ]
            fixed = sum(
                1.0 for _, _, link_id in edges if link_by_id[link_id]["kind"] == "sll"
            )
            record = {"sink": sink, "edges": edges, "hops": hop_indices, "fixed_delay": fixed}
            per_sink.append(record)
            if hop_indices:
                paths.append(record)
        route_paths[name] = per_sink
    for link_id, used in sll_usage.items():
        if used > link_by_id[link_id]["capacity"]:
            raise ValidationError(f"SLL {link_id}: usage {used} exceeds capacity")
    routed_nets = len(route_by_name)
    return {
        "links": link_by_id,
        "all_nets": all_nets,
        "routed_nets": routed_nets,
        "hops": cross_hops,
        "paths": paths,
        "route_paths": route_paths,
        "sll_usage": dict(sll_usage),
    }

def _validate_plan(
    model: Mapping[str, Any], plan: Mapping[str, Any]
) -> Tuple[List[Mapping[str, Any]], int]:
    if plan.get("schema") != EDA2023_TDM_SCHEMA:
        raise ValidationError(f"tdm plan.schema: expected {EDA2023_TDM_SCHEMA!r}")
    by_index: List[Optional[Mapping[str, Any]]] = [None] * len(model["hops"])
    for hop in plan.get("hops", []):
        index = hop.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(by_index)
            or by_index[index] is not None
        ):
            raise ValidationError("tdm plan: hop coverage is not exact")
        by_index[index] = hop
    if any(hop is None for hop in by_index):
        raise ValidationError("tdm plan: hop coverage is not exact")
    groups: Dict[Tuple[str, int], Tuple[int, int, int]] = {}
    for expected in model["hops"]:
        hop = by_index[expected["index"]]
        assert hop is not None
        for key in ("net", "link", "direction", "from", "to"):
            if hop.get(key) != expected[key]:
                raise ValidationError(f"tdm hop {expected['index']}: {key} mismatch")
        ratio, lane = hop.get("ratio"), hop.get("lane")
        capacity = model["links"][expected["link"]]["capacity"]
        if (
            isinstance(ratio, bool) or not isinstance(ratio, int) or ratio < 4 or ratio % 4
            or isinstance(lane, bool) or not isinstance(lane, int) or not 0 <= lane < capacity
        ):
            raise ValidationError(f"tdm hop {expected['index']}: illegal ratio or lane")
        key = (expected["link"], lane)
        old_direction, old_ratio, count = groups.get(
            key, (expected["direction"], ratio, 0)
        )
        if old_direction != expected["direction"] or old_ratio != ratio:
            raise ValidationError(f"Wire {key}: mixed direction or TDM ratio")
        groups[key] = (old_direction, old_ratio, count + 1)
    for key, (_, ratio, count) in groups.items():
        if count > ratio:
            raise ValidationError(f"Wire {key}: {count} signals exceed ratio {ratio}")
    return cast(List[Mapping[str, Any]], by_index), len(groups)

def _evaluate_eda2023_model(
    instance: Mapping[str, Any],
    model: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    by_index, used_wires = _validate_plan(model, plan)
    path_weights: Dict[Tuple[str, str], float] = {}
    net_weights: Dict[str, float] = {
        name: 0.0 for name in model["all_nets"]
    }
    for name, paths in model["route_paths"].items():
        weights = []
        for path in paths:
            weight = path["fixed_delay"] + sum(
                0.5 + by_index[hop]["ratio"] for hop in path["hops"]
            )
            path_weights[(name, path["sink"])] = weight
            weights.append(weight)
        net_weights[name] = max(weights, default=0.0)
    return {
        "schema": EDA2023_EVALUATION_SCHEMA,
        "status": "pass",
        "instance": instance["name"],
        "metrics": {
            "physical_fpgas": len(instance["fpgas"]),
            "dies": len(instance["dies"]),
            "nets": len(instance["nets"]),
            "routed_nets": model["routed_nets"],
            "wire_hops": len(model["hops"]),
            "used_wires": used_wires,
            "max_routing_weight": max(net_weights.values(), default=0.0),
            "max_tdm_ratio": max((hop["ratio"] for hop in plan["hops"]), default=1),
            "maximum_sll_usage": max(model["sll_usage"].values(), default=0),
        },
        "net_routing_weights": net_weights,
        "path_routing_weights": {
            f"{net}->{sink}": weight for (net, sink), weight in path_weights.items()
        },
    }

def evaluate_eda2023_solution(
    instance_path: Path,
    routes_path: Path,
    tdm_plan_path: Path,
) -> Dict[str, Any]:
    instance = _load_instance(instance_path)
    model = _route_model(instance, read_json(routes_path))
    plan = read_json(tdm_plan_path)
    return _evaluate_eda2023_model(instance, model, plan)

def materialize_eda2023_official_outputs(
    instance_path: Path,
    routes_path: Path,
    tdm_plan_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """Rebuild the two official-format files from a checked candidate.

    This deliberately does not run an optimizer.  It independently parses the
    routed tree and TDM plan, evaluates their legality, and then serializes the
    canonical contest outputs.  The public contest bundle validator uses this
    as a deterministic replay boundary.
    """

    instance = _load_instance(instance_path)
    model = _route_model(instance, read_json(routes_path))
    plan = read_json(tdm_plan_path)
    evaluation = _evaluate_eda2023_model(instance, model, plan)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValidationError(
            f"EDA 2023 official output directory is not empty: {output_dir}"
        )
    _write_official_outputs(output_dir, instance, model, plan, evaluation)
    return evaluation

def _format_weight(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"

def _write_official_outputs(
    output_dir: Path,
    instance: Mapping[str, Any],
    model: Mapping[str, Any],
    plan: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> None:
    weights = evaluation["net_routing_weights"]
    path_weights = evaluation["path_routing_weights"]
    ordered = sorted(
        model["all_nets"],
        key=lambda name: (-weights[name], model["all_nets"][name]["id"]),
    )
    with (output_dir / "design.route.out").open("w", encoding="utf-8") as stream:
        for name in ordered:
            net = model["all_nets"][name]
            stream.write(f"[{net['id']}]\n")
            path_by_sink = {
                path["sink"]: path
                for path in model["route_paths"].get(name, [])
            }
            for sink in net["sink_dies"]:
                path = path_by_sink.get(sink)
                if path is None:
                    if sink != net["source_die"]:
                        raise ValidationError(
                            f"official output: net {name} has no path to {sink}"
                        )
                    dies = [net["source_die"]]
                    weight = 0.0
                else:
                    dies = [net["source_die"]] + [
                        edge[1] for edge in path["edges"]
                    ]
                    weight = path_weights[f"{name}->{path['sink']}"]
                indices = ",".join(
                    str(_numeric_id(die, _DIE, die)) for die in dies
                )
                stream.write(f"[{indices}][{_format_weight(weight)}]\n")
    groups: Dict[Tuple[str, int], List[Mapping[str, Any]]] = defaultdict(list)
    for hop in plan["hops"]:
        groups[(hop["link"], hop["lane"])].append(hop)
    with (output_dir / "design.tdm.out").open("w", encoding="utf-8") as stream:
        for link in (
            item for item in instance["links"] if item["kind"] == "wire"
        ):
            left, right = link["endpoints"]
            stream.write(f"[{left},{right}]\n")
            for lane in range(link["capacity"]):
                hops = groups.get((link["id"], lane), [])
                if not hops:
                    continue
                net_ids = ",".join(
                    str(hop["official_net_id"])
                    for hop in sorted(
                        hops, key=lambda hop: hop["official_net_id"]
                    )
                )
                stream.write(f"[{net_ids}] {hops[0]['ratio']}\n")
