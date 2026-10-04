from __future__ import annotations

import math
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .io import write_json
from .ir import EmuIR
from .native_tools import resolve_native_executable
from .partition import (
    build_partition_assignment,
    transported_cut_classes_for_clusters,
)
from .platform import Platform
from .resources import RESOURCE_FIELDS
from .replication import apply_replication, replicable_clusters
from .tritonpart import load_partition_net_weights


REPART_INPUT_SCHEMA = "emuflow.repart-input/v1"
REPART_PROVIDER = "repart-fpga-aware-multilevel-v1"
REPART_REPLICATION_PROVIDER = "repart-logic-replication-v1"
REPART_UPSTREAM_COMMIT = "211a9d8fd526576387cad7ac6dd3531354aeb31c"
REPART_FIXED_SEED = 42
REPART_RESOURCE_DIMENSIONS = 8


def _resource_dimensions(
    clusters: Sequence[Mapping[str, Any]],
    platform: Platform,
) -> List[str]:
    dimensions = [
        field
        for field in RESOURCE_FIELDS
        if any(cluster["resources"].get(field, 0) for cluster in clusters)
        and all(
            fpga.effective_capacity.get(field, 0) > 0
            for fpga in platform.fpgas
        )
    ]
    maximum_physical_dimensions = REPART_RESOURCE_DIMENSIONS - 1
    if len(dimensions) > maximum_physical_dimensions:
        raise ValidationError(
            "RePart reserves one of its eight resource dimensions for cells "
            f"and supports at most {maximum_physical_dimensions} active physical "
            f"resources, but this design needs {len(dimensions)}: {dimensions}"
        )
    return dimensions


def _cluster_by_instance(
    clusters: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    return {
        instance_id: cluster["id"]
        for cluster in clusters
        for instance_id in cluster["instances"]
    }


def _scaled_edge_weights(
    raw_weights: Sequence[float],
) -> Tuple[int, List[int]]:
    scale = 1
    if any(not float(weight).is_integer() for weight in raw_weights):
        scale = 1000
    scaled = [max(1, int(round(weight * scale))) for weight in raw_weights]
    if any(weight > 2_000_000_000 for weight in scaled):
        raise ValidationError("RePart integer edge weight exceeds 32-bit range")
    return scale, scaled


def _hyperedges(
    ir: EmuIR,
    cluster_by_instance: Mapping[str, str],
    net_weights: Mapping[str, float],
    transported_cut_classes: set[str],
) -> List[Dict[str, Any]]:
    known_nets = {net["id"] for net in ir.value["nets"]}
    unknown_weights = sorted(set(net_weights) - known_nets)
    if unknown_weights:
        raise ValidationError(
            f"net weights reference unknown nets {unknown_weights[:8]}"
        )

    raw: List[Dict[str, Any]] = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        driver_clusters = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for endpoint in net["drivers"]
                if endpoint["instance"] is not None
            }
        )
        all_clusters = sorted(
            {
                cluster_by_instance[endpoint["instance"]]
                for collection in ("drivers", "sinks")
                for endpoint in net[collection]
                if endpoint["instance"] is not None
            }
        )
        if len(all_clusters) < 2:
            continue
        if not driver_clusters:
            # Primary inputs are replicated by the runtime contract and do not
            # have a unique logic source that RePart may legally replicate.
            continue
        source = driver_clusters[0]
        targets = [cluster for cluster in all_clusters if cluster != source]
        if not targets:
            continue
        raw.append(
            {
                "net": net["id"],
                "source": source,
                "targets": targets,
                "weight": float(net_weights.get(net["id"], 1.0)),
            }
        )
    scale, scaled = _scaled_edge_weights([edge["weight"] for edge in raw])
    for edge, integer_weight in zip(raw, scaled):
        edge["integer_weight"] = integer_weight
    return raw


def _platform_hop_diameter(platform: Platform) -> int:
    adjacency = {fpga.id: set() for fpga in platform.fpgas}
    for link in platform.links:
        left, right = link.endpoints
        adjacency[left].add(right)
        adjacency[right].add(left)
    if len(adjacency) == 1:
        return 0
    diameter = 0
    for source in sorted(adjacency):
        distance = {source: 0}
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for neighbor in sorted(adjacency[node]):
                if neighbor in distance:
                    continue
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)
        if len(distance) != len(adjacency):
            raise ValidationError(
                "RePart requires a connected undirected FPGA topology"
            )
        diameter = max(diameter, max(distance.values()))
    return diameter


def _balanced_repart_capacities(
    clusters: Sequence[Mapping[str, Any]],
    platform: Platform,
    resource_dimensions: Sequence[str],
    requested_tolerance: float,
) -> Tuple[List[str], List[List[int]], float]:
    dimensions = ["cells", *resource_dimensions]
    weights = [
        [
            len(cluster["instances"]),
            *[
                cluster["resources"].get(dimension, 0)
                for dimension in resource_dimensions
            ],
        ]
        for cluster in clusters
    ]
    totals = [
        sum(item[dimension] for item in weights)
        for dimension in range(len(dimensions))
    ]
    fpga_ids = [fpga.id for fpga in platform.fpgas]
    shares: List[List[float]] = []
    for dimension in dimensions:
        if dimension == "cells":
            shares.append([1.0 / len(fpga_ids)] * len(fpga_ids))
            continue
        capacities = [
            float(fpga.effective_capacity[dimension])
            for fpga in platform.fpgas
        ]
        total_capacity = sum(capacities)
        shares.append(
            [capacity / total_capacity for capacity in capacities]
        )

    required_tolerance = requested_tolerance
    fpga_index = {fpga_id: index for index, fpga_id in enumerate(fpga_ids)}
    fixed_loads = [
        [0] * len(dimensions) for _ in fpga_ids
    ]
    for cluster, item_weights in zip(clusters, weights):
        fixed_fpga = cluster["fixed_fpga"]
        for dimension, total in enumerate(totals):
            if total == 0:
                continue
            target_share = (
                shares[dimension][fpga_index[fixed_fpga]]
                if fixed_fpga is not None
                else max(shares[dimension])
            )
            required_tolerance = max(
                required_tolerance,
                item_weights[dimension] / total / target_share - 1.0,
            )
        if fixed_fpga is not None:
            target = fixed_loads[fpga_index[fixed_fpga]]
            for dimension, value in enumerate(item_weights):
                target[dimension] += value
    for part, item_weights in enumerate(fixed_loads):
        for dimension, total in enumerate(totals):
            if total == 0:
                continue
            required_tolerance = max(
                required_tolerance,
                item_weights[dimension]
                / total
                / shares[dimension][part]
                - 1.0,
            )
    effective_tolerance = max(0.0, required_tolerance) + 1e-6

    allowed: List[List[int]] = []
    for part, fpga in enumerate(platform.fpgas):
        part_allowed = []
        for dimension_index, dimension in enumerate(dimensions):
            value = math.floor(
                totals[dimension_index]
                * shares[dimension_index][part]
                * (1.0 + effective_tolerance)
                + 1e-7
            )
            if dimension != "cells":
                value = min(value, fpga.effective_capacity[dimension])
            part_allowed.append(value)
        allowed.append(part_allowed)
    return dimensions, allowed, effective_tolerance


def export_repart_inputs(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    net_weights: Optional[Mapping[str, float]] = None,
    replication_enabled: bool = False,
) -> Dict[str, Any]:
    platform_fpga_ids = {fpga.id for fpga in platform.fpgas}
    active_fpga_ids = set(
        constraints.get("active_fpgas", platform_fpga_ids)
    )
    if active_fpga_ids != platform_fpga_ids:
        raise ValidationError(
            "RePart does not support an active FPGA subset"
        )
    clusters = sorted(
        clusters_artifact["clusters"], key=lambda item: item["id"]
    )
    if len(clusters) < len(platform.fpgas):
        raise ValidationError("RePart needs at least one atomic cluster per FPGA")

    resource_dimensions = _resource_dimensions(clusters, platform)
    omitted_dimensions = [
        field
        for field in RESOURCE_FIELDS
        if any(cluster["resources"].get(field, 0) for cluster in clusters)
        and field not in resource_dimensions
    ]
    dimensions, allowed_capacities, effective_tolerance = (
        _balanced_repart_capacities(
            clusters,
            platform,
            resource_dimensions,
            float(constraints["balance_tolerance"]),
        )
    )
    padded_dimensions = [
        *dimensions,
        *[
            f"unused_{index}"
            for index in range(REPART_RESOURCE_DIMENSIONS - len(dimensions))
        ],
    ]
    cluster_by_instance = _cluster_by_instance(clusters)
    hyperedges = _hyperedges(
        ir,
        cluster_by_instance,
        net_weights or {},
        transported_cut_classes_for_clusters(clusters_artifact),
    )
    if not hyperedges:
        raise ValidationError(
            "RePart hypergraph has no legal sequential-boundary hyperedges"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    area_path = output_dir / "design.are"
    net_path = output_dir / "design.net"
    info_path = output_dir / "design.info"
    topology_path = output_dir / "design.topo"
    replicability_path = output_dir / "design.rep"
    solution_path = output_dir / "design.fpga.out"

    area_lines = []
    vertex_weights = []
    for cluster in clusters:
        weights = [
            len(cluster["instances"]),
            *[
                cluster["resources"].get(dimension, 0)
                for dimension in resource_dimensions
            ],
        ]
        weights.extend([0] * (REPART_RESOURCE_DIMENSIONS - len(weights)))
        vertex_weights.append(weights)
        area_lines.append(
            " ".join([cluster["id"], *(str(weight) for weight in weights)])
        )
    area_path.write_text("\n".join(area_lines) + "\n", encoding="utf-8")

    net_path.write_text(
        "".join(
            " ".join(
                [
                    edge["source"],
                    str(edge["integer_weight"]),
                    *edge["targets"],
                ]
            )
            + "\n"
            for edge in hyperedges
        ),
        encoding="utf-8",
    )

    # Phase 3A evaluates partition quality with a frozen downstream router, so
    # RePart's per-FPGA communication bound is deliberately nonbinding. Exact
    # BoardDB capacity remains a Phase 4/G5 constraint.
    communication_limit = max(
        1, sum(edge["integer_weight"] for edge in hyperedges)
    )
    info_lines = []
    for fpga, raw_capacities in zip(platform.fpgas, allowed_capacities):
        capacities = list(raw_capacities)
        capacities.extend(
            [0] * (REPART_RESOURCE_DIMENSIONS - len(capacities))
        )
        info_lines.append(
            " ".join(
                [
                    fpga.id,
                    str(communication_limit),
                    *(str(capacity) for capacity in capacities),
                ]
            )
        )
    info_path.write_text("\n".join(info_lines) + "\n", encoding="utf-8")

    undirected_links = sorted(
        {tuple(sorted(link.endpoints)) for link in platform.links}
    )
    max_hop = _platform_hop_diameter(platform)
    topology_path.write_text(
        "\n".join(
            [
                str(max_hop),
                *[f"{left} {right}" for left, right in undirected_links],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    legal_replication_clusters, _ = replicable_clusters(
        ir, clusters_artifact
    )
    legal_replication_cluster_set = set(legal_replication_clusters)
    def replication_mask(cluster_id: str) -> int:
        return int(
            replication_enabled
            and cluster_id in legal_replication_cluster_set
        )

    replicability_path.write_text(
        "".join(
            f"{cluster['id']} {replication_mask(cluster['id'])}\n"
            for cluster in clusters
        ),
        encoding="utf-8",
    )

    weight_scale = (
        hyperedges[0]["integer_weight"] / hyperedges[0]["weight"]
        if hyperedges
        else 1
    )
    artifact = {
        "schema": REPART_INPUT_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "upstream_commit": REPART_UPSTREAM_COMMIT,
        "fpga_order": [fpga.id for fpga in platform.fpgas],
        "cluster_order": [cluster["id"] for cluster in clusters],
        "resource_dimensions": padded_dimensions,
        "active_resource_dimensions": dimensions,
        "active_physical_resource_dimensions": resource_dimensions,
        "omitted_unconstrained_resource_dimensions": omitted_dimensions,
        "vertex_weights": vertex_weights,
        "hyperedges": hyperedges,
        "edge_weight_scale": weight_scale,
        "max_hop_distance": max_hop,
        "communication_limit": communication_limit,
        "requested_balance_tolerance": constraints["balance_tolerance"],
        "effective_balance_tolerance": effective_tolerance,
        "allowed_capacities": {
            fpga.id: {
                dimension: value
                for dimension, value in zip(dimensions, capacities)
            }
            for fpga, capacities in zip(platform.fpgas, allowed_capacities)
        },
        "replication_enabled": replication_enabled,
        "replicable_clusters": (
            legal_replication_clusters if replication_enabled else []
        ),
        "fixed_seed": REPART_FIXED_SEED,
        "files": {
            "area": area_path.name,
            "net": net_path.name,
            "info": info_path.name,
            "topology": topology_path.name,
            "replicability": replicability_path.name,
            "solution": solution_path.name,
        },
    }
    write_json(output_dir / "repart_input.json", artifact)
    return artifact


def parse_repart_solution(
    path: Path,
    repart_input: Mapping[str, Any],
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    if not path.is_file():
        raise ValidationError(f"RePart solution does not exist: {path}")
    fpga_order = list(repart_input["fpga_order"])
    cluster_order = list(repart_input["cluster_order"])
    known_fpgas = set(fpga_order)
    known_clusters = set(cluster_order)
    assignment: Dict[str, str] = {}
    replicas: Dict[str, List[str]] = {}
    seen_fpgas = set()

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue
        fpga_id, separator, raw_clusters = line.partition(":")
        fpga_id = fpga_id.strip()
        if not separator or fpga_id not in known_fpgas:
            raise ValidationError(
                f"RePart solution line {line_number}: invalid FPGA record"
            )
        if fpga_id in seen_fpgas:
            raise ValidationError(
                f"RePart solution line {line_number}: duplicate FPGA {fpga_id!r}"
            )
        seen_fpgas.add(fpga_id)
        for token in raw_clusters.split():
            replicated = token.endswith("*")
            cluster_id = token[:-1] if replicated else token
            if cluster_id not in known_clusters:
                raise ValidationError(
                    f"RePart solution line {line_number}: unknown cluster "
                    f"{cluster_id!r}"
                )
            if replicated:
                replicas.setdefault(cluster_id, []).append(fpga_id)
                continue
            if cluster_id in assignment:
                raise ValidationError(
                    f"RePart solution assigns cluster {cluster_id!r} "
                    "to multiple primary FPGAs"
                )
            assignment[cluster_id] = fpga_id

    if set(assignment) != known_clusters:
        missing = sorted(known_clusters - set(assignment))
        raise ValidationError(
            "RePart primary assignment exact coverage failed; "
            f"missing={missing[:8]}"
        )
    normalized_replicas = {
        cluster_id: sorted(set(fpga_ids))
        for cluster_id, fpga_ids in sorted(replicas.items())
    }
    for cluster_id, fpga_ids in normalized_replicas.items():
        if assignment[cluster_id] in fpga_ids:
            raise ValidationError(
                f"RePart repeats primary FPGA as replica for {cluster_id!r}"
            )
    return assignment, normalized_replicas


def run_repart(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    output_dir: Path,
    executable: Optional[str] = None,
    solution_input: Optional[Path] = None,
    net_weights_path: Optional[Path] = None,
    timeout_seconds: int = 3600,
    enable_replication: bool = False,
) -> Dict[str, Any]:
    repart_input = export_repart_inputs(
        ir,
        platform,
        clusters_artifact,
        constraints,
        output_dir,
        net_weights=load_partition_net_weights(net_weights_path),
        replication_enabled=enable_replication,
    )
    solution_path = output_dir / repart_input["files"]["solution"]
    log_path: Optional[Path] = None
    mode = "import"
    resolved_executable: Optional[str] = None

    if solution_input is not None:
        if not solution_input.is_file():
            raise ValidationError(
                f"precomputed RePart solution does not exist: {solution_input}"
            )
        if solution_input.resolve() != solution_path.resolve():
            shutil.copyfile(solution_input, solution_path)
    else:
        mode = "execute"
        resolved_executable = resolve_native_executable("repart", executable)
        command = [
            resolved_executable,
            "-t",
            str(output_dir.resolve()),
            "-s",
            str(solution_path.resolve()),
            "-r",
            "1" if enable_replication else "0",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=output_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise EmuFlowError(
                f"RePart exceeded timeout of {timeout_seconds} seconds"
            ) from error
        log_path = output_dir / "repart.log"
        log_path.write_text(completed.stdout, encoding="utf-8")
        if completed.returncode != 0:
            tail = "\n".join(completed.stdout.splitlines()[-30:])
            raise EmuFlowError(
                f"RePart failed with exit code {completed.returncode}\n{tail}"
            )
        if not solution_path.is_file():
            raise EmuFlowError(
                "RePart reported success but did not create "
                f"{solution_path}"
            )

    cluster_assignment, replicas = parse_repart_solution(
        solution_path, repart_input
    )
    if replicas and not enable_replication:
        raise ValidationError(
            "Phase 3A requires replication-disabled RePart, but the solution "
            f"contains {sum(len(items) for items in replicas.values())} replicas"
        )

    fixed_repairs = []
    cluster_by_id = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    for cluster_id in repart_input["cluster_order"]:
        fixed_fpga = cluster_by_id[cluster_id]["fixed_fpga"]
        if fixed_fpga is None or cluster_assignment[cluster_id] == fixed_fpga:
            continue
        fixed_repairs.append(
            {
                "cluster": cluster_id,
                "source": cluster_assignment[cluster_id],
                "target": fixed_fpga,
            }
        )
        cluster_assignment[cluster_id] = fixed_fpga

    normalized_replicas = {
        cluster_id: [
            fpga_id
            for fpga_id in fpga_ids
            if fpga_id != cluster_assignment[cluster_id]
        ]
        for cluster_id, fpga_ids in replicas.items()
    }
    normalized_replicas = {
        cluster_id: fpga_ids
        for cluster_id, fpga_ids in normalized_replicas.items()
        if fpga_ids
    }
    primary_assignment = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=(
            REPART_REPLICATION_PROVIDER
            if enable_replication
            else REPART_PROVIDER
        ),
        seed=REPART_FIXED_SEED,
        provider_metadata={
            "mode": mode,
            "executable": resolved_executable,
            "input_schema": REPART_INPUT_SCHEMA,
            "upstream_commit": REPART_UPSTREAM_COMMIT,
            "license": "GPL-3.0-only",
            "replication_enabled": enable_replication,
            "replicable_clusters": len(
                repart_input["replicable_clusters"]
            ),
            "active_resource_dimensions": repart_input[
                "active_resource_dimensions"
            ],
            "active_physical_resource_dimensions": repart_input[
                "active_physical_resource_dimensions"
            ],
            "omitted_unconstrained_resource_dimensions": repart_input[
                "omitted_unconstrained_resource_dimensions"
            ],
            "hyperedges": len(repart_input["hyperedges"]),
            "max_hop_distance": repart_input["max_hop_distance"],
            "communication_limit": repart_input["communication_limit"],
            "requested_balance_tolerance": repart_input[
                "requested_balance_tolerance"
            ],
            "effective_balance_tolerance": repart_input[
                "effective_balance_tolerance"
            ],
            "fixed_repairs": fixed_repairs,
            "artifacts": {
                **repart_input["files"],
                "input": "repart_input.json",
                "log": log_path.name if log_path is not None else None,
            },
        },
    )
    if not enable_replication:
        return primary_assignment
    return apply_replication(
        ir,
        platform,
        clusters_artifact,
        primary_assignment,
        normalized_replicas,
    )
