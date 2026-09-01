"""Topology-aware Phase 3 legality audit and native FM refinement."""

from __future__ import annotations

import hashlib
import math
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .io import read_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .partition import (
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
)
from .platform import Platform
from .replication import apply_replication
from .resources import RESOURCE_FIELDS
from .routing import build_directed_graph, load_route_constraints
from .tritonpart import load_partition_net_weights


HOP_REFINER_INPUT_SCHEMA = "emuflow.hop-partition-refiner-input/v1"
HOP_REFINER_REPORT_SCHEMA = "emuflow.hop-partition-refinement/v1"
HOP_REFINER_PROVIDER = "topology-constrained-fm-v1"


def _shortest_hop_distances(
    platform: Platform,
    route_constraints: Mapping[str, Any],
) -> Dict[str, Dict[str, Optional[int]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[int]]] = {}
    for source in sorted(adjacency):
        distances = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for arc in adjacency[node]:
                sink = arc["to"]
                if sink in distances:
                    continue
                distances[sink] = distances[node] + 1
                queue.append(sink)
        result[source] = {
            sink: distances.get(sink) for sink in sorted(adjacency)
        }
    return result


def audit_assignment_hops(
    assignment: Mapping[str, Any],
    distances: Mapping[str, Mapping[str, Optional[int]]],
    hop_limit: int,
) -> Dict[str, Any]:
    violations = []
    routed_endpoints = 0
    total_hops = 0
    maximum_hops = 0
    unreachable = 0
    for cut in assignment["cut_nets"]:
        sources = cut.get("source_fpgas", [])
        if len(sources) != 1:
            raise ValidationError(
                f"hop audit requires one source FPGA for net {cut['net']!r}"
            )
        source = sources[0]
        for sink in sorted(set(cut["sink_fpgas"])):
            routed_endpoints += 1
            hops = distances[source][sink]
            if hops is None:
                unreachable += 1
            else:
                total_hops += hops
                maximum_hops = max(maximum_hops, hops)
            if hops is None or hops > hop_limit:
                violations.append(
                    {
                        "net": cut["net"],
                        "source": source,
                        "sink": sink,
                        "hops": hops,
                        "limit": hop_limit,
                    }
                )
    return {
        "status": "pass" if not violations else "violation",
        "max_route_hops": hop_limit,
        "routed_net_sink_pairs": routed_endpoints,
        "violating_net_sink_pairs": len(violations),
        "unreachable_net_sink_pairs": unreachable,
        "maximum_observed_hops": maximum_hops,
        "total_shortest_path_hops": total_hops,
        "violations": violations[:64],
        "violations_truncated": max(0, len(violations) - 64),
    }


def validate_assignment_hop_constraints(
    assignment_path: Path,
    platform_path: Path,
    route_constraints_path: Path,
) -> Dict[str, Any]:
    """Independently recheck Phase 3 reachability and maximum-hop legality."""

    platform = Platform.load(platform_path)
    constraints = load_route_constraints(route_constraints_path, platform)
    hop_limit = constraints.get("max_route_hops")
    if hop_limit is None:
        return {"status": "not-requested", "max_route_hops": None}
    audit = audit_assignment_hops(
        read_json(assignment_path),
        _shortest_hop_distances(platform, constraints),
        hop_limit,
    )
    if audit["status"] != "pass":
        raise ValidationError(
            "partition assignment violates route reachability/max-hop constraints"
        )
    return audit


def validate_assignment_hops(
    platform: Platform,
    assignment: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
) -> Dict[str, Any]:
    """Check an in-memory assignment without materializing refiner input."""

    hop_limit = route_constraints.get("max_route_hops")
    if hop_limit is None:
        return {"status": "not-requested", "max_route_hops": None}
    audit = audit_assignment_hops(
        assignment,
        _shortest_hop_distances(platform, route_constraints),
        hop_limit,
    )
    if audit["status"] != "pass":
        raise ValidationError(
            "partition assignment violates route reachability/max-hop "
            "constraints"
        )
    return audit


def _primary_net_records(
    ir: EmuIR,
    clusters_artifact: Mapping[str, Any],
    net_weights: Mapping[str, float],
) -> List[Dict[str, Any]]:
    cluster_by_instance = {
        instance_id: cluster["id"]
        for cluster in clusters_artifact["clusters"]
        for instance_id in cluster["instances"]
    }
    records = []
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        sources = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        sinks = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["sinks"]
                if endpoint["instance"] is not None
            }
        )
        if not sources or not sinks:
            continue
        if len(sources) != 1:
            raise ValidationError(
                f"hop refinement requires one driver cluster for net {net['id']!r}"
            )
        sinks = [cluster for cluster in sinks if cluster != sources[0]]
        if sinks:
            records.append(
                {
                    "net": net["id"],
                    "weight": float(net_weights.get(net["id"], 1.0)),
                    "source": sources[0],
                    "sinks": sinks,
                }
            )
    return records


def _replica_targets(
    assignment: Mapping[str, Any],
) -> Dict[str, List[str]]:
    replication = assignment.get("replication")
    if replication is None:
        return {}
    result: Dict[str, List[str]] = {}
    for record in replication["replicas"]:
        result.setdefault(record["cluster"], []).append(
            record["target_fpga"]
        )
    return {cluster: sorted(targets) for cluster, targets in result.items()}


def _write_native_input(
    path: Path,
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    partition_constraints: Mapping[str, Any],
    assignment: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    distances: Mapping[str, Mapping[str, Optional[int]]],
    net_weights: Mapping[str, float],
) -> Dict[str, Any]:
    hop_limit = route_constraints["max_route_hops"]
    assert hop_limit is not None
    clusters = sorted(
        clusters_artifact["clusters"], key=lambda item: item["id"]
    )
    cluster_index = {
        cluster["id"]: index for index, cluster in enumerate(clusters)
    }
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    balance = validate_cluster_assignment_balance(
        platform,
        clusters,
        assignment["cluster_assignment"],
        partition_constraints["balance_tolerance"],
        partition_constraints.get("balance_tolerance_by_dimension", {}),
    )
    dimensions = balance["balance_dimensions"]
    nets = _primary_net_records(ir, clusters_artifact, net_weights)
    lines = [
        "EMUFLOW_HOP_PARTITION_REFINER_INPUT_V1",
        "PARAM "
        + " ".join(
            str(value)
            for value in (
                len(fpga_ids),
                len(clusters),
                len(dimensions),
                len(nets),
                hop_limit,
                partition_constraints["min_used_fpgas"],
            )
        ),
    ]
    for source in fpga_ids:
        for sink in fpga_ids:
            value = distances[source][sink]
            lines.append(
                f"DIST {fpga_index[source]} {fpga_index[sink]} "
                f"{value if value is not None else -1}"
            )
    for fpga in platform.fpgas:
        for dimension_index, dimension in enumerate(dimensions):
            balance_bound = balance["balance_allowed_loads"][fpga.id][
                dimension
            ]
            hard_capacity = (
                math.ceil(balance_bound)
                if dimension == "cells"
                else fpga.effective_capacity[dimension]
            )
            lines.append(
                f"BOUND {fpga_index[fpga.id]} {dimension_index} "
                f"{balance_bound:.17g} {hard_capacity}"
            )
    for index, cluster in enumerate(clusters):
        current = fpga_index[assignment["cluster_assignment"][cluster["id"]]]
        fixed = (
            -1
            if cluster["fixed_fpga"] is None
            else fpga_index[cluster["fixed_fpga"]]
        )
        weights = [
            len(cluster["instances"]),
            *[
                cluster["resources"].get(dimension, 0)
                for dimension in dimensions
                if dimension != "cells"
            ],
        ]
        lines.append(
            "CLUSTER "
            + " ".join(str(value) for value in (index, current, fixed, *weights))
        )
    for index, net in enumerate(nets):
        sink_indices = [cluster_index[item] for item in net["sinks"]]
        lines.append(
            "NET "
            + " ".join(
                str(value)
                for value in (
                    index,
                    f"{net['weight']:.17g}",
                    cluster_index[net["source"]],
                    len(sink_indices),
                    *sink_indices,
                )
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "schema": HOP_REFINER_INPUT_SCHEMA,
        "algorithm": HOP_REFINER_PROVIDER,
        "fpga_order": fpga_ids,
        "cluster_order": [cluster["id"] for cluster in clusters],
        "dimensions": dimensions,
        "nets": len(nets),
        "max_route_hops": hop_limit,
        "balance": balance,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _parse_native_output(
    path: Path,
    native_input: Mapping[str, Any],
) -> Tuple[str, Dict[str, str], List[Dict[str, Any]], Dict[str, float]]:
    if not path.is_file():
        raise EmuFlowError("hop partition refiner produced no output")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_HOP_PARTITION_REFINER_OUTPUT_V1":
        raise ValidationError("invalid hop partition refiner output header")
    status = None
    assignments: Dict[int, int] = {}
    moves = []
    metrics: Dict[str, float] = {}
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "STATUS" and len(fields) == 2:
            status = fields[1]
        elif fields[0] == "ASSIGN" and len(fields) == 3:
            assignments[int(fields[1])] = int(fields[2])
        elif fields[0] == "MOVE" and len(fields) == 4:
            cluster, source, target = map(int, fields[1:])
            moves.append(
                {
                    "cluster": native_input["cluster_order"][cluster],
                    "source": native_input["fpga_order"][source],
                    "target": native_input["fpga_order"][target],
                }
            )
        elif fields[0] == "METRIC" and len(fields) == 3:
            metrics[fields[1]] = float(fields[2])
        else:
            raise ValidationError(
                f"invalid hop partition refiner output record {line!r}"
            )
    if status not in {"PASS", "STUCK"}:
        raise ValidationError("hop partition refiner output has no valid status")
    if set(assignments) != set(range(len(native_input["cluster_order"]))):
        raise ValidationError("hop partition refiner assignment coverage mismatch")
    fpga_order = native_input["fpga_order"]
    assignment = {}
    for cluster, part in sorted(assignments.items()):
        if part < 0 or part >= len(fpga_order):
            raise ValidationError("hop partition refiner emitted invalid part")
        assignment[native_input["cluster_order"][cluster]] = fpga_order[part]
    return status, assignment, moves, metrics


def refine_partition_hops(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    partition_constraints: Mapping[str, Any],
    assignment: Mapping[str, Any],
    output_dir: Path,
    *,
    route_constraints_path: Optional[Path] = None,
    net_weights_path: Optional[Path] = None,
    executable: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    route_constraints = load_route_constraints(
        route_constraints_path, platform
    )
    hop_limit = route_constraints["max_route_hops"]
    if hop_limit is None:
        return dict(assignment), {
            "schema": HOP_REFINER_REPORT_SCHEMA,
            "status": "not-requested",
            "enabled": False,
            "reason": "route constraints do not define max_route_hops",
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    distances = _shortest_hop_distances(platform, route_constraints)
    before = audit_assignment_hops(assignment, distances, hop_limit)
    net_weights = load_partition_net_weights(net_weights_path)
    native_input_path = output_dir / "hop_refiner.in"
    native_output_path = output_dir / "hop_refiner.out"
    native_input = _write_native_input(
        native_input_path,
        ir,
        platform,
        clusters_artifact,
        partition_constraints,
        assignment,
        route_constraints,
        distances,
        net_weights,
    )
    resolved_executable = resolve_native_executable(
        "emuflow_hop_partition_refiner", executable
    )
    completed = subprocess.run(
        [
            resolved_executable,
            str(native_input_path.resolve()),
            str(native_output_path.resolve()),
        ],
        cwd=output_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    (output_dir / "hop_refiner.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise EmuFlowError(
            "hop partition refiner failed with exit code "
            f"{completed.returncode}: {completed.stdout[-2000:]}"
        )
    status, cluster_assignment, moves, native_metrics = _parse_native_output(
        native_output_path, native_input
    )

    metadata = dict(assignment.get("provider_metadata", {}))
    refinement_summary = {
        "algorithm": HOP_REFINER_PROVIDER,
        "research_basis": [
            "TopoPart (ICCAD 2021)",
            "cut-violation-correction partitioner (DATE 2024)",
            "HoPart direction (DATE 2026 programme abstract)",
        ],
        "claim_scope": (
            "topology-constrained FM post-refinement; not an exact HoPart "
            "reproduction"
        ),
        "native_status": status,
        "move_count": len(moves),
        "native_metrics": native_metrics,
    }
    metadata["hop_feasibility"] = refinement_summary
    if cluster_assignment == assignment.get("cluster_assignment"):
        # The native hop checker commonly proves the input already feasible.
        # Preserve its canonical assignment/Static Exact contract instead of
        # rebuilding the complete instance/net representation solely to add a
        # compact proof summary.
        primary = dict(assignment)
        primary["provider_metadata"] = metadata
    else:
        primary = build_partition_assignment(
            ir,
            platform,
            clusters_artifact,
            partition_constraints,
            cluster_assignment,
            provider=assignment["provider"],
            seed=assignment["seed"],
            provider_metadata=metadata,
        )
    replicas = _replica_targets(assignment)
    if replicas:
        replicas = {
            cluster: [
                target
                for target in targets
                if target != cluster_assignment[cluster]
            ]
            for cluster, targets in replicas.items()
        }
        replicas = {
            cluster: targets
            for cluster, targets in replicas.items()
            if targets
        }
        primary = apply_replication(
            ir, platform, clusters_artifact, primary, replicas
        )
    after = audit_assignment_hops(primary, distances, hop_limit)
    report = {
        "schema": HOP_REFINER_REPORT_SCHEMA,
        "status": "pass" if after["status"] == "pass" else "failed",
        "enabled": True,
        "algorithm": HOP_REFINER_PROVIDER,
        "before": before,
        "after": after,
        "moves": moves,
        "native_metrics": native_metrics,
        "timing_weighted_nets": len(net_weights),
        "artifacts": {
            "input": native_input_path.name,
            "output": native_output_path.name,
            "log": "hop_refiner.log",
        },
        "input": native_input,
    }
    if status != "PASS" or after["status"] != "pass":
        raise ValidationError(
            "hop-feasible partition refinement could not satisfy "
            f"max_route_hops={hop_limit}; remaining violations="
            f"{after['violating_net_sink_pairs']}"
        )
    return primary, report
