"""Independent checker for the 2024 EDA Elite partitioning contest.

The official case files are also RePart's native input format.  This module
does not ask RePart to validate its own output: it reparses every input,
recomputes resource and external-communication usage, checks the maximum-hop
constraint, and evaluates weighted total hop distance independently.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .contest_boarddb import materialize_homogeneous_boarddb
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import write_json
from emuflow.routing.routing import SYSTEM_ROUTE_CONSTRAINTS_SCHEMA


EDA2024_EVALUATION_SCHEMA = "emuflow.contest-eda2024-evaluation/v1"
EDA2024_IMPORT_SCHEMA = "emuflow.contest-eda2024-import/v1"
EDA2024_BOARDDB_MATERIALIZATION_SCHEMA = (
    "emuflow.contest-eda2024-boarddb-materialization/v1"
)
EDA2024_SOURCE_URL = (
    "https://edaoss.icisc.cn/file/cacheFile/2024/8/1/"
    "8e6b33de567b411d8b159b961ef117aa.pdf"
)
REPART_SOURCE_URL = "https://github.com/Welement-zyf/RePart"
REPART_BENCHMARK_COMMIT = "211a9d8fd526576387cad7ac6dd3531354aeb31c"
RESOURCE_NAMES = ("FF", "LUT", "BUFG", "TBUF", "DCM", "BRAM", "DSP", "PP")


def _data_lines(path: Path) -> Iterable[Tuple[int, str]]:
    if not path.is_file():
        raise ValidationError(f"contest input does not exist: {path}")
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            line = raw.split("#", 1)[0].strip()
            if line:
                yield line_number, line


def _integer(raw: str, context: str, *, positive: bool = False) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise ValidationError(f"{context}: expected an integer") from error
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValidationError(f"{context}: expected a {qualifier} integer")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_design_info(
    path: Path,
) -> Tuple[List[str], Dict[str, int], Dict[str, Tuple[int, ...]]]:
    fpga_ids: List[str] = []
    external_limits: Dict[str, int] = {}
    capacities: Dict[str, Tuple[int, ...]] = {}
    for line_number, line in _data_lines(path):
        fields = line.split()
        if len(fields) != 10:
            raise ValidationError(
                f"{path}:{line_number}: expected FPGA, external limit, and "
                "eight resource capacities"
            )
        fpga_id = fields[0]
        if fpga_id in external_limits:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA {fpga_id!r}"
            )
        fpga_ids.append(fpga_id)
        external_limits[fpga_id] = _integer(
            fields[1], f"{path}:{line_number} external limit"
        )
        capacities[fpga_id] = tuple(
            _integer(value, f"{path}:{line_number} {RESOURCE_NAMES[index]}")
            for index, value in enumerate(fields[2:])
        )
    if not fpga_ids:
        raise ValidationError(f"{path}: expected at least one FPGA")
    return fpga_ids, external_limits, capacities


def parse_topology(
    path: Path, fpga_ids: Sequence[str]
) -> Tuple[int, List[Tuple[str, str]], Dict[str, Dict[str, int]]]:
    lines = list(_data_lines(path))
    if not lines:
        raise ValidationError(f"{path}: topology is empty")
    first_number, first_line = lines[0]
    max_hop = _integer(
        first_line, f"{path}:{first_number} maximum hop", positive=True
    )
    known = set(fpga_ids)
    edges: List[Tuple[str, str]] = []
    edge_set: Set[Tuple[str, str]] = set()
    adjacency = {fpga_id: set() for fpga_id in fpga_ids}
    for line_number, line in lines[1:]:
        fields = line.split()
        if len(fields) != 2 or any(item not in known for item in fields):
            raise ValidationError(
                f"{path}:{line_number}: expected two distinct known FPGAs"
            )
        left, right = fields
        if left == right:
            raise ValidationError(
                f"{path}:{line_number}: topology self-link is invalid"
            )
        key = tuple(sorted((left, right)))
        if key in edge_set:
            raise ValidationError(
                f"{path}:{line_number}: duplicate topology link {key}"
            )
        edge_set.add(key)
        edges.append(key)
        adjacency[left].add(right)
        adjacency[right].add(left)

    distances: Dict[str, Dict[str, int]] = {}
    for source in fpga_ids:
        distance = {source: 0}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for neighbor in sorted(adjacency[current]):
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[current] + 1
                queue.append(neighbor)
        if len(distance) != len(fpga_ids):
            raise ValidationError(
                f"{path}: FPGA topology is disconnected from {source!r}"
            )
        distances[source] = distance
    return max_hop, sorted(edges), distances


def import_eda2024_case(
    case_dir: Path, output_dir: Path, name: str
) -> Dict[str, Any]:
    """Parse all official inputs without requiring a participant solution.

    This is deliberately an import gate, not an evaluation gate.  It proves
    that the pinned source files form one structurally consistent problem
    instance; solution legality and score remain the responsibility of
    :func:`evaluate_eda2024_solution`.
    """

    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: expected a non-empty string")
    info_path = case_dir / "design.info"
    area_path = case_dir / "design.are"
    net_path = case_dir / "design.net"
    topology_path = case_dir / "design.topo"
    fpga_ids, external_limits, capacities = parse_design_info(info_path)
    max_hop, links, distances = parse_topology(topology_path, fpga_ids)

    nodes: Set[str] = set()
    resource_totals = [0] * len(RESOURCE_NAMES)
    for line_number, line in _data_lines(area_path):
        fields = line.split()
        if len(fields) != 9:
            raise ValidationError(
                f"{area_path}:{line_number}: expected node and eight resources"
            )
        node = fields[0]
        if node in nodes:
            raise ValidationError(
                f"{area_path}:{line_number}: duplicate node {node!r}"
            )
        nodes.add(node)
        for index, raw in enumerate(fields[1:]):
            resource_totals[index] += _integer(
                raw, f"{area_path}:{line_number} {RESOURCE_NAMES[index]}"
            )
    if not nodes:
        raise ValidationError(f"{area_path}: expected at least one node")

    net_count = 0
    sink_pins = 0
    total_weight = 0
    maximum_fanout = 0
    referenced_nodes: Set[str] = set()
    for line_number, line in _data_lines(net_path):
        fields = line.split()
        if len(fields) < 3:
            raise ValidationError(
                f"{net_path}:{line_number}: expected source, weight, and sinks"
            )
        source = fields[0]
        sinks = fields[2:]
        if source in sinks or len(sinks) != len(set(sinks)):
            raise ValidationError(
                f"{net_path}:{line_number}: net endpoints must be distinct"
            )
        unknown = [node for node in [source, *sinks] if node not in nodes]
        if unknown:
            raise ValidationError(
                f"{net_path}:{line_number}: unknown nodes {unknown[:8]}"
            )
        weight = _integer(
            fields[1], f"{net_path}:{line_number} weight", positive=True
        )
        referenced_nodes.update([source, *sinks])
        net_count += 1
        sink_pins += len(sinks)
        total_weight += weight
        maximum_fanout = max(maximum_fanout, len(sinks))
    if not net_count:
        raise ValidationError(f"{net_path}: expected at least one net")

    instance = {
        "schema": EDA2024_IMPORT_SCHEMA,
        "name": name,
        "source_format": "eda2024-repart",
        "solution_required_for_evaluation": True,
        "fpgas": [
            {
                "id": fpga_id,
                "external_communication_limit": external_limits[fpga_id],
                "capacity": {
                    resource: value
                    for resource, value in zip(
                        RESOURCE_NAMES, capacities[fpga_id]
                    )
                },
            }
            for fpga_id in fpga_ids
        ],
        "topology": {
            "maximum_legal_hop_distance": max_hop,
            "links": [list(link) for link in links],
            "diameter": max(max(row.values()) for row in distances.values()),
        },
        "problem": {
            "nodes": len(nodes),
            "referenced_nodes": len(referenced_nodes),
            "nets": net_count,
            "sink_pins": sink_pins,
            "total_net_weight": total_weight,
            "maximum_fanout": maximum_fanout,
            "resource_totals": {
                resource: value
                for resource, value in zip(RESOURCE_NAMES, resource_totals)
            },
        },
        "source_sha256": {
            path.name: _sha256(path)
            for path in (info_path, area_path, net_path, topology_path)
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "contest_instance.json"
    write_json(output_path, instance)
    return {
        "schema": EDA2024_IMPORT_SCHEMA,
        "status": "pass",
        "name": name,
        "fpgas": len(fpga_ids),
        "links": len(links),
        **instance["problem"],
        "artifacts": {"instance": str(output_path)},
    }


def materialize_eda2024_rtl_boarddb(
    case_dir: Path,
    device_template_path: Path,
    output_path: Path,
    *,
    name: str,
    lanes_per_edge: int,
    template_fpga_id: Optional[str] = None,
    fabric_clock_mhz: float = 50.0,
    latency_cycles: int = 2,
    link_mode: str = "abstract",
    route_constraints_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Populate a 2024 contest topology with an RTL-capable device template.

    The contest topology is unweighted.  ``lanes_per_edge`` is consequently
    an explicit projection parameter, not a value inferred from the contest's
    per-FPGA external-communication constraint.
    """
    if (
        isinstance(lanes_per_edge, bool)
        or not isinstance(lanes_per_edge, int)
        or lanes_per_edge <= 0
    ):
        raise ValidationError("lanes_per_edge: expected a positive integer")
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

    info_path = case_dir / "design.info"
    topology_path = case_dir / "design.topo"
    fpga_ids, external_limits, contest_capacities = parse_design_info(info_path)
    max_hop, edges, _ = parse_topology(topology_path, fpga_ids)
    if not edges:
        raise ValidationError("EDA 2024 topology has no inter-FPGA edge")

    links = [
        {
            "id": f"eda2024_link_{index:03d}",
            "endpoints": [left, right],
            "direction": "full_duplex",
            "capacity_sharing": "shared_bidirectional",
            "mode": link_mode,
            "data_lanes_per_direction": lanes_per_edge,
            "fabric_clock_mhz": float(fabric_clock_mhz),
            "latency_cycles": latency_cycles,
        }
        for index, (left, right) in enumerate(edges)
    ]
    validated, template_platform, selected = materialize_homogeneous_boarddb(
        output_path=output_path,
        name=name,
        description=(
            "2024 EDA Elite public unweighted multi-FPGA graph populated with "
            "a homogeneous FPGA device template and explicit abstract lanes"
        ),
        fpga_ids=fpga_ids,
        links=links,
        device_template_path=device_template_path,
        template_fpga_id=template_fpga_id,
        provenance={
            "interconnect": {
                "specification_url": EDA2024_SOURCE_URL,
                "benchmark_repository": REPART_SOURCE_URL,
                "benchmark_commit": REPART_BENCHMARK_COMMIT,
                "case_directory": str(case_dir),
                "projection": "unweighted-topology-with-configured-lanes",
                "capacity_semantics": "not-specified-by-contest",
                "configured_lanes_per_edge": lanes_per_edge,
                "maximum_legal_hop_distance": max_hop,
                "external_communication_limits": dict(sorted(external_limits.items())),
                "contest_resource_capacities": {
                    fpga_id: {
                        resource: capacity
                        for resource, capacity in zip(
                            RESOURCE_NAMES, contest_capacities[fpga_id]
                        )
                    }
                    for fpga_id in fpga_ids
                },
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
            "max_route_hops": max_hop,
        },
    )
    return {
        "schema": EDA2024_BOARDDB_MATERIALIZATION_SCHEMA,
        "status": "pass",
        "platform": validated.name,
        "device_template": template_platform.name,
        "template_fpga": selected["id"],
        "fpgas": len(validated.fpgas),
        "links": len(validated.links),
        "configured_lanes_per_edge": lanes_per_edge,
        "data_lanes": sum(
            link.data_lanes_per_direction for link in validated.links
        ),
        "maximum_legal_hop_distance": max_hop,
        "capacity_semantics": "not-specified-by-contest",
        "output": str(output_path),
        "route_constraints": str(constraints_output),
    }


def parse_solution(
    path: Path, fpga_ids: Sequence[str]
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    known_fpgas = set(fpga_ids)
    seen_fpgas = set()
    primary: Dict[str, str] = {}
    replica_sets: Dict[str, Set[str]] = defaultdict(set)
    for line_number, line in _data_lines(path):
        fpga_id, separator, raw_nodes = line.partition(":")
        fpga_id = fpga_id.strip()
        if not separator or fpga_id not in known_fpgas:
            raise ValidationError(
                f"{path}:{line_number}: expected a known '<FPGA>: ...' record"
            )
        if fpga_id in seen_fpgas:
            raise ValidationError(
                f"{path}:{line_number}: duplicate FPGA row {fpga_id!r}"
            )
        seen_fpgas.add(fpga_id)
        for token in raw_nodes.split():
            replicated = token.endswith("*")
            node = token[:-1] if replicated else token
            if not node or "*" in node:
                raise ValidationError(
                    f"{path}:{line_number}: malformed node token {token!r}"
                )
            if replicated:
                if fpga_id in replica_sets[node]:
                    raise ValidationError(
                        f"{path}:{line_number}: duplicate replica {node!r}"
                    )
                replica_sets[node].add(fpga_id)
            else:
                if node in primary:
                    raise ValidationError(
                        f"{path}:{line_number}: node {node!r} has two primary FPGAs"
                    )
                primary[node] = fpga_id
    missing_rows = sorted(known_fpgas - seen_fpgas)
    if missing_rows:
        raise ValidationError(f"{path}: missing FPGA rows {missing_rows}")
    if not primary:
        raise ValidationError(f"{path}: solution contains no primary nodes")
    unknown_replica_nodes = sorted(set(replica_sets) - set(primary))
    if unknown_replica_nodes:
        raise ValidationError(
            f"{path}: replicas have no primary assignment: "
            f"{unknown_replica_nodes[:8]}"
        )
    for node, targets in replica_sets.items():
        if primary[node] in targets:
            raise ValidationError(
                f"{path}: replica {node!r} repeats its primary FPGA"
            )
    return primary, {
        node: sorted(targets) for node, targets in sorted(replica_sets.items())
    }


def _evaluate_resources(
    area_path: Path,
    fpga_ids: Sequence[str],
    capacities: Mapping[str, Sequence[int]],
    primary: Mapping[str, str],
    replicas: Mapping[str, Sequence[str]],
) -> Tuple[int, Dict[str, List[int]]]:
    loads = {fpga_id: [0] * len(RESOURCE_NAMES) for fpga_id in fpga_ids}
    remaining = set(primary)
    node_count = 0
    for line_number, line in _data_lines(area_path):
        fields = line.split()
        if len(fields) != 9:
            raise ValidationError(
                f"{area_path}:{line_number}: expected node and eight resources"
            )
        node = fields[0]
        if node not in primary:
            raise ValidationError(
                f"{area_path}:{line_number}: node {node!r} lacks a primary FPGA"
            )
        if node not in remaining:
            raise ValidationError(
                f"{area_path}:{line_number}: duplicate node {node!r}"
            )
        remaining.remove(node)
        resources = [
            _integer(value, f"{area_path}:{line_number} {RESOURCE_NAMES[index]}")
            for index, value in enumerate(fields[1:])
        ]
        for fpga_id in [primary[node], *replicas.get(node, ())]:
            for index, value in enumerate(resources):
                loads[fpga_id][index] += value
        node_count += 1
    if remaining:
        raise ValidationError(
            f"{area_path}: solution references nodes absent from design.are: "
            f"{sorted(remaining)[:8]}"
        )
    for fpga_id in fpga_ids:
        for index, used in enumerate(loads[fpga_id]):
            capacity = capacities[fpga_id][index]
            if used > capacity:
                raise ValidationError(
                    f"resource overflow on {fpga_id} {RESOURCE_NAMES[index]}: "
                    f"used {used}, capacity {capacity}"
                )
    return node_count, loads


def evaluate_eda2024_solution(
    info_path: Path,
    area_path: Path,
    net_path: Path,
    topology_path: Path,
    solution_path: Path,
    *,
    runtime_seconds: float = 0.0,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    if (
        isinstance(runtime_seconds, bool)
        or not isinstance(runtime_seconds, (int, float))
        or runtime_seconds < 0
    ):
        raise ValidationError("runtime_seconds: expected a non-negative number")

    fpga_ids, external_limits, capacities = parse_design_info(info_path)
    max_hop, links, distances = parse_topology(topology_path, fpga_ids)
    primary, replicas = parse_solution(solution_path, fpga_ids)
    node_count, resource_loads = _evaluate_resources(
        area_path, fpga_ids, capacities, primary, replicas
    )

    communication = {fpga_id: 0 for fpga_id in fpga_ids}
    total_hop_distance = 0
    maximum_observed_hop = 0
    cut_hyperedges = 0
    remote_fpga_sinks = 0
    net_count = 0
    for line_number, line in _data_lines(net_path):
        fields = line.split()
        if len(fields) < 3:
            raise ValidationError(
                f"{net_path}:{line_number}: expected source, weight, and sinks"
            )
        source = fields[0]
        if source not in primary:
            raise ValidationError(
                f"{net_path}:{line_number}: unknown source node {source!r}"
            )
        weight = _integer(
            fields[1], f"{net_path}:{line_number} weight", positive=True
        )
        sinks = fields[2:]
        if source in sinks or len(sinks) != len(set(sinks)):
            raise ValidationError(
                f"{net_path}:{line_number}: net endpoints must be distinct"
            )
        unknown_sinks = [sink for sink in sinks if sink not in primary]
        if unknown_sinks:
            raise ValidationError(
                f"{net_path}:{line_number}: unknown sink nodes "
                f"{unknown_sinks[:8]}"
            )

        source_fpga = primary[source]
        local_source_locations = {source_fpga, *replicas.get(source, ())}
        target_locations = {
            fpga_id
            for sink in sinks
            for fpga_id in [primary[sink], *replicas.get(sink, ())]
        }
        remote_targets = sorted(target_locations - local_source_locations)
        if remote_targets:
            cut_hyperedges += 1
            communication[source_fpga] += weight
        for target_fpga in remote_targets:
            hop = distances[source_fpga][target_fpga]
            if hop > max_hop:
                raise ValidationError(
                    f"{net_path}:{line_number}: {source_fpga}->{target_fpga} "
                    f"requires {hop} hops, above maximum {max_hop}"
                )
            maximum_observed_hop = max(maximum_observed_hop, hop)
            total_hop_distance += weight * hop
            communication[target_fpga] += weight
            remote_fpga_sinks += 1
        net_count += 1

    for fpga_id in fpga_ids:
        if communication[fpga_id] > external_limits[fpga_id]:
            raise ValidationError(
                f"external communication overflow on {fpga_id}: used "
                f"{communication[fpga_id]}, limit {external_limits[fpga_id]}"
            )

    runtime_factor = 1.0 + 0.2 * float(runtime_seconds) / 3600.0
    report = {
        "schema": EDA2024_EVALUATION_SCHEMA,
        "status": "pass",
        "source": {
            "contest": "2024 EDA Elite Challenge",
            "problem": "Hypergraph partitioning with logic replication",
            "specification_url": EDA2024_SOURCE_URL,
            "benchmark_source": REPART_SOURCE_URL,
            "benchmark_commit": REPART_BENCHMARK_COMMIT,
        },
        "model": {
            "objective": "weighted-total-hop-distance",
            "replica_semantics": (
                "a source replica serves sinks on the same FPGA; replicas also "
                "consume resources and receive every input net"
            ),
            "resource_order": list(RESOURCE_NAMES),
            "maximum_hop": max_hop,
            "runtime_factor": runtime_factor,
        },
        "inputs": {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in (
                ("info", info_path),
                ("area", area_path),
                ("net", net_path),
                ("topology", topology_path),
                ("solution", solution_path),
            )
        },
        "resources": [
            {
                "fpga": fpga_id,
                "capacity": {
                    name: capacities[fpga_id][index]
                    for index, name in enumerate(RESOURCE_NAMES)
                },
                "used": {
                    name: resource_loads[fpga_id][index]
                    for index, name in enumerate(RESOURCE_NAMES)
                },
            }
            for fpga_id in fpga_ids
        ],
        "communication": [
            {
                "fpga": fpga_id,
                "limit": external_limits[fpga_id],
                "used": communication[fpga_id],
            }
            for fpga_id in fpga_ids
        ],
        "metrics": {
            "fpgas": len(fpga_ids),
            "links": len(links),
            "nodes": node_count,
            "nets": net_count,
            "replicated_modules": len(replicas),
            "replica_copies": sum(len(targets) for targets in replicas.values()),
            "cut_hyperedges": cut_hyperedges,
            "remote_fpga_sinks": remote_fpga_sinks,
            "maximum_observed_hop": maximum_observed_hop,
            "total_hop_distance": total_hop_distance,
            "runtime_seconds": float(runtime_seconds),
            "contest_score": total_hop_distance * runtime_factor,
        },
    }
    if output_path is not None:
        write_json(output_path, report)
    return report
