"""Path-, topology-, and TDM-pressure-aware Phase 3 reference model.

This module is intentionally the exhaustive Python specification.  It is used
for compact correctness tests and for checking the native scalable refiner; it
is not the large-design production optimizer.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .io import read_json, write_json
from .partition import (
    CUT_MODE_STATIC_EXACT,
    build_partition_assignment,
    transported_cut_classes_for_clusters,
    validate_cluster_assignment_balance,
    validate_partition_artifacts,
    validate_partition_artifacts_online,
)
from .platform import Platform
from .native_tools import resolve_native_executable
from .partition_physical_feedback import (
    PARTITION_PHYSICAL_FEEDBACK_SCHEMA,
    validate_partition_physical_feedback_seal,
)
from .resources import RESOURCE_FIELDS
from .routing import (
    build_directed_graph,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
)
from .sta import _normalized_slack, _validate_database_normalization


PARTITION_PRESSURE_MODEL_SCHEMA = "emuflow.partition-pressure-model/v6"
PARTITION_PRESSURE_TRACE_SCHEMA = "emuflow.partition-pressure-trace/v6"
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7 = (
    "emuflow.partition-pressure-trace/v7"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8 = (
    "emuflow.partition-pressure-trace/v8"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9 = (
    "emuflow.partition-pressure-trace/v9"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA = "emuflow.partition-pressure-trace/v10"
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11 = (
    "emuflow.partition-pressure-trace/v11"
)
PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V12 = (
    "emuflow.partition-pressure-trace/v12"
)

PATRON_FLOW_REFINEMENT_ALGORITHM_V1 = (
    "flowcutter-bidirectional-piercing-v1"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V2 = (
    "flowcutter-bidirectional-piercing-frontier-closure-v2"
)
PATRON_FLOW_REFINEMENT_ALGORITHM = (
    "flowcutter-bidirectional-piercing-physical-hop-guard-v4"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V5 = (
    "flowcutter-bidirectional-piercing-endpoint-residual-v5"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V6 = (
    "flowcutter-ranked-frontier-static-exact-topology-guard-v6"
)
PATRON_FLOW_REFINEMENT_ALGORITHM_V3 = (
    "flowcutter-bidirectional-piercing-ranked-frontier-closure-v3"
)
PATRON_FLOW_CORRIDOR_DISTANCE = 4
PATRON_FLOW_PIERCING_STRATEGY = 0
PATRON_FLOW_MAX_LEGAL_CANDIDATES = 8
PATRON_FLOW_MAX_POLISH_MOVES = 512
PATRON_FLOW_MAX_FRONTIER_PATHS = 256
PATRON_FLOW_MAX_TAIL_MOVES_V2 = 16
PATRON_FLOW_MAX_TAIL_MOVES = 256
PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS = 5.0
PARTITION_PRESSURE_REPORT_SCHEMA = "emuflow.partition-pressure-report/v6"
PARTITION_PRESSURE_PROVIDER = "patron-endpoint-exact-reference-v6"
PARTITION_PRESSURE_NATIVE_PROVIDER = "patron-endpoint-exact-native-v6"
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7 = (
    "patron-endpoint-exact-flow-native-v7"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8 = (
    "patron-endpoint-exact-flow-native-v8"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9 = (
    "patron-endpoint-exact-flow-native-v9"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER = (
    "patron-endpoint-exact-flow-native-v10"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11 = (
    "patron-endpoint-exact-flow-native-v11"
)
PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V12 = (
    "patron-static-exact-topology-guard-flow-native-v12"
)
GAIN_QUANTUM = 1.0e-9
BOUNDARY_FANOUT_PENALTY_SCALE_NS = 0.0
MAX_SCALABLE_SWEEPS = 4
SCALABLE_EJECTION_CRITICAL_LIMIT = 2048
SCALABLE_EJECTION_DONOR_LIMIT = 32
MAX_SCALABLE_EJECTIONS = 64


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _flow_refinement_configuration(
    enabled: bool,
    cluster_count: int,
    *,
    version: int = 4,
    physical_feedback_sha256: Optional[str] = None,
    physical_feedback_scale: float = 0.0,
) -> Dict[str, Any]:
    if version not in (1, 2, 3, 4, 5, 6):
        raise ValidationError("native PATRON flow version is invalid")
    if (
        isinstance(physical_feedback_scale, bool)
        or not isinstance(physical_feedback_scale, (int, float))
        or not math.isfinite(float(physical_feedback_scale))
        or float(physical_feedback_scale) < 0.0
    ):
        raise ValidationError("native PATRON physical feedback scale is invalid")
    if version == 5 and (
        not enabled
        or not isinstance(physical_feedback_sha256, str)
        or len(physical_feedback_sha256) != 64
        or any(character not in "0123456789abcdef" for character in physical_feedback_sha256)
        or float(physical_feedback_scale) <= 0.0
    ):
        raise ValidationError("native PATRON physical feedback seal is invalid")
    if version != 5 and (
        physical_feedback_sha256 is not None
        or float(physical_feedback_scale) != 0.0
    ):
        raise ValidationError("legacy PATRON flow cannot consume physical feedback")
    result = {
        "enabled": enabled,
        "algorithm": (
            PATRON_FLOW_REFINEMENT_ALGORITHM_V6
            if version == 6
            else PATRON_FLOW_REFINEMENT_ALGORITHM_V5
            if version == 5
            else (
                PATRON_FLOW_REFINEMENT_ALGORITHM
                if version == 4
                else (
                    PATRON_FLOW_REFINEMENT_ALGORITHM_V3
                    if version == 3
                    else (
                        PATRON_FLOW_REFINEMENT_ALGORITHM_V2
                        if version == 2
                        else PATRON_FLOW_REFINEMENT_ALGORITHM_V1
                    )
                )
            )
        ),
        "maximum_corridor_clusters": cluster_count if enabled else 0,
        "corridor_distance": (
            PATRON_FLOW_CORRIDOR_DISTANCE if enabled else 0
        ),
        "piercing_strategy": (
            PATRON_FLOW_PIERCING_STRATEGY if enabled else 0
        ),
        "maximum_legal_candidates": (
            PATRON_FLOW_MAX_LEGAL_CANDIDATES if enabled else 0
        ),
        "maximum_polish_moves": (
            PATRON_FLOW_MAX_POLISH_MOVES if enabled else 0
        ),
    }
    if version >= 2:
        result.update(
            {
                "maximum_frontier_paths": (
                    PATRON_FLOW_MAX_FRONTIER_PATHS if enabled else 0
                ),
                "maximum_tail_moves": (
                    (
                        PATRON_FLOW_MAX_TAIL_MOVES
                        if version >= 3
                        else PATRON_FLOW_MAX_TAIL_MOVES_V2
                    )
                    if enabled
                    else 0
                ),
            }
        )
    if version >= 3:
        result["frontier_selection"] = "ranked-worst-path-window-v1"
    if version == 4:
        result.update(
            {
                "physical_hop_guard": "scale_ns*route_hops",
                "physical_hop_guard_scale_ns": (
                    PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS
                    if enabled
                    else 0.0
                ),
            }
        )
    if version == 5:
        result.update(
            {
                "physical_hop_guard": "disabled-by-endpoint-feedback",
                "physical_hop_guard_scale_ns": 0.0,
                "physical_feedback_schema": (
                    PARTITION_PHYSICAL_FEEDBACK_SCHEMA
                ),
                "physical_feedback_matching": (
                    "exact-current-endpoint-pair-v1"
                ),
                "physical_feedback_sha256": physical_feedback_sha256,
                "physical_feedback_scale": float(physical_feedback_scale),
            }
        )
    if version == 6:
        result.update(
            {
                "physical_hop_guard": "disabled-by-static-exact-topology-guard",
                "physical_hop_guard_scale_ns": 0.0,
                "static_exact_topology_guard": (
                    "initial-transported-non-combinational-"
                    "worst-hop-non-regression-v1"
                ),
            }
        )
    return result


def _require_finite(value: Any, context: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"{context} must be finite")
    return float(value)


def _instance_to_cluster(
    clusters_artifact: Mapping[str, Any],
) -> Dict[str, str]:
    return {
        instance: cluster["id"]
        for cluster in clusters_artifact["clusters"]
        for instance in cluster["instances"]
    }


def _net_records(
    ir: EmuIR, clusters_artifact: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    cluster_by_instance = _instance_to_cluster(clusters_artifact)
    transported_cut_classes = transported_cut_classes_for_clusters(
        clusters_artifact
    )
    records = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in transported_cut_classes:
            continue
        drivers = sorted(
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
        if not drivers or not sinks:
            continue
        records.append(
            {
                "net": net["id"],
                "cut_class": net["cut_class"],
                "drivers": drivers,
                "sinks": sinks,
            }
        )
    return sorted(records, key=lambda record: record["net"])


def _shortest_routes(
    platform: Platform, route_constraints: Mapping[str, Any]
) -> Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]]:
    adjacency, _, _ = build_directed_graph(platform, route_constraints)
    result: Dict[str, Dict[str, Optional[List[Dict[str, Any]]]]] = {}
    for source in sorted(adjacency):
        # The tuple deliberately includes the complete arc identity.  Equal
        # delay/hop alternatives therefore have a stable provider-independent
        # ordering rather than depending on heap insertion order.
        best: Dict[str, Tuple[float, int, Tuple[Tuple[str, str, str], ...]]] = {
            source: (0.0, 0, ())
        }
        queue: List[
            Tuple[float, int, Tuple[Tuple[str, str, str], ...], str]
        ] = [(0.0, 0, (), source)]
        while queue:
            delay, hops, identity, node = heapq.heappop(queue)
            if best.get(node) != (delay, hops, identity):
                continue
            for arc in adjacency[node]:
                arc_identity = (arc["link"], arc["from"], arc["to"])
                candidate = (
                    delay
                    + route_link_delay_ns(
                        platform,
                        arc["link"],
                        arc["from"],
                        arc["to"],
                        route_constraints,
                    ),
                    hops + 1,
                    identity + (arc_identity,),
                )
                if arc["to"] not in best or candidate < best[arc["to"]]:
                    best[arc["to"]] = candidate
                    heapq.heappush(queue, (*candidate, arc["to"]))
        by_sink: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        for sink in sorted(adjacency):
            if sink not in best:
                by_sink[sink] = None
                continue
            by_sink[sink] = [
                {
                    "link": link,
                    "from": left,
                    "to": right,
                }
                for link, left, right in best[sink][2]
            ]
        result[source] = by_sink
    return result


def _reconstruct_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
) -> Dict[str, Any]:

    if timing_database.get("design") != ir.value["design"]["name"]:
        raise ValidationError("partition pressure timing design mismatch")
    normalization = _validate_database_normalization(
        timing_database.get("normalization")
    )
    known_nets = {net["id"] for net in ir.value["nets"]}
    cluster_by_instance = _instance_to_cluster(clusters_artifact)
    pressure_nets = {
        record["net"]: record
        for record in _net_records(ir, clusters_artifact)
    }
    paths = []
    seen_paths = set()
    for index, raw in enumerate(timing_database.get("paths", [])):
        if not isinstance(raw, dict):
            raise ValidationError(
                f"partition pressure timing path {index} is invalid"
            )
        path_id = raw.get("id")
        path_nets = raw.get("path_nets")
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in seen_paths
            or not isinstance(path_nets, list)
            or not all(isinstance(net, str) for net in path_nets)
            or not path_nets
        ):
            raise ValidationError(
                f"partition pressure timing path {index} is invalid"
            )
        unknown = sorted(set(path_nets) - known_nets)
        if unknown:
            raise ValidationError(
                f"partition pressure path {path_id!r} references unknown nets "
                f"{unknown[:8]}"
            )
        period = _require_finite(
            raw.get("clock_period_ns"), f"path {path_id!r} period"
        )
        slack = _require_finite(
            raw.get("slack_ns"), f"path {path_id!r} slack"
        )
        if period <= 0.0:
            raise ValidationError(
                f"path {path_id!r} clock period must be positive"
            )
        seen_paths.add(path_id)
        startpoint = raw.get("startpoint")
        endpoint = raw.get("endpoint")
        start_instance = (
            startpoint.get("instance")
            if isinstance(startpoint, dict)
            else None
        )
        end_instance = (
            endpoint.get("instance")
            if isinstance(endpoint, dict)
            else None
        )
        start_cluster = cluster_by_instance.get(start_instance)
        end_cluster = cluster_by_instance.get(end_instance)
        considered_nets = [net for net in path_nets if net in pressure_nets]
        endpoint_exact = (
            start_cluster is not None
            and end_cluster is not None
            and all(
                len(pressure_nets[net]["drivers"]) == 1
                for net in considered_nets
            )
        )
        record = {
            "path": path_id,
            "clock_domain": raw.get("clock_domain"),
            "clock_period_ns": period,
            "base_slack_ns": slack,
            "path_nets": list(path_nets),
            "transition_model": (
                "endpoint-exact-reverse-chain-v1"
                if endpoint_exact
                else "conservative-net-worst-v1"
            ),
        }
        if endpoint_exact:
            record["start_cluster"] = start_cluster
            record["end_cluster"] = end_cluster
        paths.append(record)
    if not paths:
        raise ValidationError("partition pressure requires timing paths")

    adjacency, _, capacities = build_directed_graph(
        platform, route_constraints
    )
    if not any(adjacency.values()):
        raise ValidationError("partition pressure requires routable links")
    links = {link.id: link for link in platform.links}
    capacity_records = []
    for key, record in sorted(capacities.items()):
        link = links[record["link"]]
        capacity_records.append(
            {
                **record,
                "lanes": link.transport_bits_per_cycle_per_direction,
                "fabric_clock_mhz": link.fabric_clock_mhz,
            }
        )
    clusters = [
        {
            "cluster": cluster["id"],
            "cells": len(cluster["instances"]),
            "resources": dict(sorted(cluster["resources"].items())),
            "fixed_fpga": cluster["fixed_fpga"],
        }
        for cluster in clusters_artifact["clusters"]
    ]
    return {
        "schema": PARTITION_PRESSURE_MODEL_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": PARTITION_PRESSURE_PROVIDER,
        "sources": {
            "ir_sha256": _canonical_digest(ir.value),
            "clusters_sha256": _canonical_digest(clusters_artifact),
            "constraints_sha256": _canonical_digest(constraints),
            "timing_database_sha256": _canonical_digest(timing_database),
            "route_constraints_sha256": _canonical_digest(
                route_constraints
            ),
        },
        "configuration": {
            "gain_quantum": GAIN_QUANTUM,
            "boundary_fanout_penalty": "scale*log2(1+remote_sink_clusters)",
            "boundary_fanout_penalty_scale_ns": (
                BOUNDARY_FANOUT_PENALTY_SCALE_NS
            ),
            "max_scalable_sweeps": MAX_SCALABLE_SWEEPS,
            "scalable_ejection_critical_limit": (
                SCALABLE_EJECTION_CRITICAL_LIMIT
            ),
            "scalable_ejection_donor_limit": SCALABLE_EJECTION_DONOR_LIMIT,
            "max_scalable_ejections": MAX_SCALABLE_EJECTIONS,
            "max_route_hops": route_constraints.get("max_route_hops"),
            "frame_slots": route_constraints["frame_slots"],
            "tdm_ratio_quantum": route_constraints["tdm_ratio_quantum"],
            "predicted_wait": "sum((domain_ratio-1)*link_cycle_ns)",
            "path_delay": (
                "endpoint_exact_reverse_chain_else_"
                "sum(max_remote_sink_delay_per_path_net)"
            ),
        },
        "normalization": normalization,
        "clusters": clusters,
        "fpgas": [
            {
                "fpga": fpga.id,
                "effective_capacity": dict(
                    sorted(fpga.effective_capacity.items())
                ),
            }
            for fpga in platform.fpgas
        ],
        "capacities": capacity_records,
        "shortest_routes": _shortest_routes(platform, route_constraints),
        "nets": list(pressure_nets.values()),
        "paths": paths,
    }
def build_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    *,
    independent_validation: bool = True,
) -> Dict[str, Any]:
    """Build the immutable source-bound reference model."""

    model = _reconstruct_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
    )
    if independent_validation:
        validate_partition_pressure_model(
            ir,
            platform,
            clusters_artifact,
            constraints,
            timing_database,
            route_constraints,
            model,
        )
    return model


def validate_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
) -> Dict[str, Any]:
    if model.get("schema") != PARTITION_PRESSURE_MODEL_SCHEMA:
        raise ValidationError("partition pressure model schema is invalid")
    # Rebuilding from primary inputs is the independent source-binding gate.
    expected = _reconstruct_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
    )
    if model != expected:
        raise ValidationError("partition pressure model reconstruction mismatch")
    expected_sources = {
        "ir_sha256": _canonical_digest(ir.value),
        "clusters_sha256": _canonical_digest(clusters_artifact),
        "constraints_sha256": _canonical_digest(constraints),
        "timing_database_sha256": _canonical_digest(timing_database),
        "route_constraints_sha256": _canonical_digest(route_constraints),
    }
    if model.get("sources") != expected_sources:
        raise ValidationError("partition pressure model source seal mismatch")
    if model.get("design") != ir.value["design"]["name"]:
        raise ValidationError("partition pressure model design mismatch")
    if model.get("platform") != platform.name:
        raise ValidationError("partition pressure model platform mismatch")
    if model.get("provider") != PARTITION_PRESSURE_PROVIDER:
        raise ValidationError("partition pressure model provider mismatch")
    return {
        "status": "pass",
        "clusters": len(model.get("clusters", [])),
        "nets": len(model.get("nets", [])),
        "paths": len(model.get("paths", [])),
        "capacity_domains": len(model.get("capacities", [])),
    }


def _capacity_key_by_arc(model: Mapping[str, Any]) -> Dict[Tuple[str, str, str], str]:
    by_link = defaultdict(list)
    for record in model["capacities"]:
        by_link[record["link"]].append(record)
    result = {}
    for source, by_sink in model["shortest_routes"].items():
        for sink, route in by_sink.items():
            if route is None:
                continue
            for arc in route:
                candidates = by_link[arc["link"]]
                shared = next(
                    (item for item in candidates if item["direction"] == "shared"),
                    None,
                )
                record = shared or next(
                    item
                    for item in candidates
                    if item["direction"] == f"{arc['from']}->{arc['to']}"
                )
                result[(arc["link"], arc["from"], arc["to"])] = record["key"]
    return result


def _static_exact_topology_guard_limits(
    clusters_artifact: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
) -> Dict[str, int]:
    """Freeze the initial hop bound of architectural transport nets.

    Generalized combinational cuts remain unconstrained because they are the
    new Static Exact degree of freedom.  An architectural net that is already
    transported may improve or become local, but it may not acquire a longer
    worst source-to-sink board path during PATRON refinement.
    """

    if (
        clusters_artifact.get("policy", {}).get("cut_mode")
        != CUT_MODE_STATIC_EXACT
    ):
        raise ValidationError(
            "PATRON static-exact topology guard requires Static Exact clusters"
        )
    cluster_assignment = initial_assignment.get("cluster_assignment")
    if not isinstance(cluster_assignment, dict):
        raise ValidationError(
            "PATRON static-exact topology guard assignment is invalid"
        )
    limits: Dict[str, int] = {}
    for net in model["nets"]:
        if net["cut_class"] == "combinational":
            limits[net["net"]] = -1
            continue
        sources = {cluster_assignment[item] for item in net["drivers"]}
        sinks = {cluster_assignment[item] for item in net["sinks"]}
        maximum = 0
        for source in sources:
            for sink in sinks:
                if source == sink:
                    continue
                route = model["shortest_routes"][source][sink]
                if route is None:
                    raise ValidationError(
                        "PATRON initial architectural transport is unreachable"
                    )
                maximum = max(maximum, len(route))
        limits[net["net"]] = maximum if maximum > 0 else -1
    return limits


def _check_static_exact_topology_guard(
    model: Mapping[str, Any],
    cluster_assignment: Mapping[str, str],
    limits: Mapping[str, int],
) -> None:
    for net in model["nets"]:
        limit = limits.get(net["net"])
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValidationError(
                "PATRON static-exact topology guard coverage is invalid"
            )
        if limit < 0:
            continue
        sources = {cluster_assignment[item] for item in net["drivers"]}
        sinks = {cluster_assignment[item] for item in net["sinks"]}
        for source in sources:
            for sink in sinks:
                if source == sink:
                    continue
                route = model["shortest_routes"][source][sink]
                if route is None or len(route) > limit:
                    raise ValidationError(
                        "PATRON static-exact topology guard regressed net "
                        f"{net['net']!r}: {source!r}->{sink!r} exceeds "
                        f"{limit} hops"
                    )


def _predicted_route_delay(
    platform: Platform,
    route_constraints: Mapping[str, Any],
    domain_by_arc: Mapping[Tuple[str, str, str], str],
    domain_ratios: Mapping[str, int],
    route: Iterable[Mapping[str, Any]],
    *,
    include_tdm_wait: bool,
    physical_hop_guard_scale_ns: float = 0.0,
) -> float:
    link_by_id = {link.id: link for link in platform.links}
    delay = 0.0
    for arc in route:
        domain = domain_by_arc[(arc["link"], arc["from"], arc["to"])]
        link = link_by_id[arc["link"]]
        delay += route_link_delay_ns(
            platform,
            arc["link"],
            arc["from"],
            arc["to"],
            route_constraints,
        )
        if include_tdm_wait:
            delay += (
                max(0, domain_ratios[domain] - 1)
                * 1000.0
                / link.fabric_clock_mhz
            )
        delay += physical_hop_guard_scale_ns
    return delay


def _boundary_fanout_penalty(
    model: Mapping[str, Any], remote_sink_clusters: int
) -> float:
    if remote_sink_clusters <= 0:
        return 0.0
    scale = _require_finite(
        model["configuration"]["boundary_fanout_penalty_scale_ns"],
        "partition pressure boundary fanout penalty scale",
    )
    if scale < 0.0:
        raise ValidationError(
            "partition pressure boundary fanout penalty scale must be "
            "non-negative"
        )
    return scale * math.log2(1.0 + remote_sink_clusters)


def _endpoint_exact_path_transport(
    path: Mapping[str, Any],
    net_by_id: Mapping[str, Mapping[str, Any]],
    cluster_parts: Mapping[str, str],
    model: Mapping[str, Any],
    platform: Platform,
    route_constraints: Mapping[str, Any],
    domain_by_arc: Mapping[Tuple[str, str, str], str],
    domain_ratios: Mapping[str, int],
    *,
    include_tdm_wait: bool,
    physical_hop_guard_scale_ns: float = 0.0,
) -> Tuple[float, List[str], int]:
    """Recover the concrete fanout branch used by one timing path."""

    target = cluster_parts[path["end_cluster"]]
    reverse_transitions: List[Tuple[str, str]] = []
    transport = 0.0
    max_hops = model["configuration"]["max_route_hops"]
    for net_id in reversed(path["path_nets"]):
        net = net_by_id.get(net_id)
        if net is None:
            continue
        sources = {cluster_parts[cluster] for cluster in net["drivers"]}
        if len(sources) != 1:
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} has a multi-driver "
                f"pressure net {net_id!r}"
            )
        source = next(iter(sources))
        sink_parts = {cluster_parts[cluster] for cluster in net["sinks"]}
        if target not in sink_parts:
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} cannot reach "
                f"partition {target!r} through net {net_id!r}"
            )
        if source == target:
            continue
        remote_sink_clusters = sum(
            cluster_parts[sink] == target for sink in net["sinks"]
        )
        route = model["shortest_routes"][source][target]
        if route is None or (
            max_hops is not None and len(route) > max_hops
        ):
            raise ValidationError(
                f"endpoint-exact path {path['path']!r} has an illegal "
                f"transition {source!r}->{target!r}"
            )
        transport += _predicted_route_delay(
            platform,
            route_constraints,
            domain_by_arc,
            domain_ratios,
            route,
            include_tdm_wait=include_tdm_wait,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        )
        transport += _boundary_fanout_penalty(
            model, remote_sink_clusters
        )
        reverse_transitions.append((source, target))
        target = source
    if cluster_parts[path["start_cluster"]] != target:
        raise ValidationError(
            f"endpoint-exact path {path['path']!r} launch/capture chain "
            "is inconsistent"
        )
    partition_sequence: List[str] = []
    for source, sink in reversed(reverse_transitions):
        for part in (source, sink):
            if not partition_sequence or partition_sequence[-1] != part:
                partition_sequence.append(part)
    return transport, partition_sequence, len(reverse_transitions)


def evaluate_partition_pressure(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    cluster_assignment: Mapping[str, str],
    *,
    include_tdm_wait: bool = True,
    physical_hop_guard_scale_ns: float = 0.0,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    static_exact_topology_guard_limits: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """Fully recompute the PATRON objective for one compact assignment."""

    assignment = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=PARTITION_PRESSURE_PROVIDER,
        seed=0,
    )
    validate_partition_artifacts(ir, platform, clusters_artifact, assignment)
    physical_hop_guard_scale_ns = _require_finite(
        physical_hop_guard_scale_ns,
        "partition pressure physical hop guard scale",
    )
    if physical_hop_guard_scale_ns < 0.0:
        raise ValidationError(
            "partition pressure physical hop guard scale must be non-negative"
        )
    physical_feedback_scale = _require_finite(
        physical_feedback_scale,
        "partition pressure physical feedback scale",
    )
    if physical_feedback_scale < 0.0:
        raise ValidationError(
            "partition pressure physical feedback scale must be non-negative"
        )
    if physical_feedback is None:
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "partition pressure physical feedback scale has no artifact"
            )
        feedback_by_path: Dict[str, Mapping[str, Any]] = {}
    else:
        if (
            physical_feedback.get("schema")
            != PARTITION_PHYSICAL_FEEDBACK_SCHEMA
            or physical_feedback_scale <= 0.0
            or not isinstance(physical_feedback.get("paths"), list)
        ):
            raise ValidationError(
                "partition pressure physical feedback configuration is invalid"
            )
        feedback_by_path = {
            record["path"]: record for record in physical_feedback["paths"]
        }
        if len(feedback_by_path) != len(physical_feedback["paths"]):
            raise ValidationError(
                "partition pressure physical feedback paths are duplicated"
            )
    cluster_parts = dict(cluster_assignment)
    domain_by_arc = _capacity_key_by_arc(model)
    capacity_by_key = {record["key"]: record for record in model["capacities"]}
    domain_loads = {key: 0 for key in capacity_by_key}
    routes_by_net: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    max_hops = model["configuration"]["max_route_hops"]
    unreachable = []

    if static_exact_topology_guard_limits is not None:
        _check_static_exact_topology_guard(
            model, cluster_parts, static_exact_topology_guard_limits
        )

    for net in model["nets"]:
        sources = sorted({cluster_parts[item] for item in net["drivers"]})
        sink_part_counts: Dict[str, int] = defaultdict(int)
        for cluster in net["sinks"]:
            sink_part_counts[cluster_parts[cluster]] += 1
        sinks = sorted(sink_part_counts)
        for source in sources:
            for sink in sinks:
                if sink == source:
                    continue
                route = model["shortest_routes"][source][sink]
                if route is None or (
                    max_hops is not None and len(route) > max_hops
                ):
                    unreachable.append(
                        {"net": net["net"], "from": source, "to": sink}
                    )
                    continue
                route_record = {
                    "from": source,
                    "to": sink,
                    "arcs": route,
                    "remote_sink_clusters": sink_part_counts[sink],
                }
                routes_by_net[net["net"]].append(route_record)
                for arc in route:
                    domain_loads[
                        domain_by_arc[(arc["link"], arc["from"], arc["to"])]
                    ] += 1
    if unreachable:
        raise ValidationError(
            "partition pressure assignment violates route reachability/hops: "
            f"{unreachable[:8]}"
        )

    domain_ratios = {
        key: estimate_tdm_ratio(
            load,
            capacity_by_key[key]["lanes"],
            route_constraints,
            is_sll=(
                capacity_by_key[key]["link"]
                in set(route_constraints["sll_links"])
            ),
        )
        for key, load in domain_loads.items()
    }
    net_delay: Dict[str, float] = {}
    net_worst_transition: Dict[str, Tuple[str, str]] = {}
    total_bit_hops = 0
    for net, routes in routes_by_net.items():
        maximum = 0.0
        worst_transition: Optional[Tuple[str, str]] = None
        for route in routes:
            delay = _predicted_route_delay(
                platform,
                route_constraints,
                domain_by_arc,
                domain_ratios,
                route["arcs"],
                include_tdm_wait=include_tdm_wait,
                physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            )
            delay += _boundary_fanout_penalty(
                model, route["remote_sink_clusters"]
            )
            transition = (route["from"], route["to"])
            if delay > maximum or (
                abs(delay - maximum) <= 1.0e-12
                and (
                    worst_transition is None
                    or transition < worst_transition
                )
            ):
                maximum = delay
                worst_transition = transition
            total_bit_hops += len(route["arcs"])
        net_delay[net] = maximum
        if worst_transition is not None:
            net_worst_transition[net] = worst_transition

    normalization = model["normalization"]
    net_by_id = {net["net"]: net for net in model["nets"]}
    path_records = []
    for path in model["paths"]:
        if path["transition_model"] == "endpoint-exact-reverse-chain-v1":
            transport, partition_sequence, transitions = (
                _endpoint_exact_path_transport(
                    path,
                    net_by_id,
                    cluster_parts,
                    model,
                    platform,
                    route_constraints,
                    domain_by_arc,
                    domain_ratios,
                    include_tdm_wait=include_tdm_wait,
                    physical_hop_guard_scale_ns=(
                        physical_hop_guard_scale_ns
                    ),
                )
            )
        else:
            transport = sum(
                net_delay.get(net, 0.0) for net in path["path_nets"]
            )
            partition_sequence = []
            for net in path["path_nets"]:
                transition = net_worst_transition.get(net)
                if transition is None:
                    continue
                for part in transition:
                    if (
                        not partition_sequence
                        or partition_sequence[-1] != part
                    ):
                        partition_sequence.append(part)
            transitions = max(0, len(partition_sequence) - 1)
        feedback_record = feedback_by_path.get(path["path"])
        feedback_delay = 0.0
        if (
            feedback_record is not None
            and len(partition_sequence) > 1
            and partition_sequence[0]
            == feedback_record["observed_source_fpga"]
            and partition_sequence[-1]
            == feedback_record["observed_sink_fpga"]
        ):
            feedback_delay = (
                _require_finite(
                    feedback_record["positive_residual_ns"],
                    "partition pressure physical feedback residual",
                )
                * physical_feedback_scale
            )
            transport += feedback_delay
        predicted_slack = path["base_slack_ns"] - transport
        normalized = _normalized_slack(
            path["clock_period_ns"], predicted_slack, normalization
        )
        seen_parts = set()
        snaking = 0
        for part in partition_sequence:
            if part in seen_parts:
                snaking += 1
            seen_parts.add(part)
        path_records.append(
            {
                "path": path["path"],
                "transition_model": path["transition_model"],
                "transport_delay_ns": transport,
                "physical_feedback_delay_ns": feedback_delay,
                "predicted_slack_ns": predicted_slack,
                "normalized_slack": normalized,
                "partition_transitions": transitions,
                "snaking": snaking,
                "partition_sequence": partition_sequence,
            }
        )
    worst = min(record["normalized_slack"] for record in path_records)
    total_negative = sum(
        min(0.0, record["normalized_slack"]) for record in path_records
    )
    maximum_ratio = max(domain_ratios.values(), default=1)
    total_snaking = sum(record["snaking"] for record in path_records)
    cut_bits = sum(len(routes) for routes in routes_by_net.values())
    metrics = {
        "worst_normalized_slack": worst,
        "total_negative_normalized_slack": total_negative,
        "negative_paths": sum(
            record["normalized_slack"] < 0.0 for record in path_records
        ),
        "maximum_predicted_tdm_ratio": maximum_ratio,
        "maximum_capacity_domain_load": max(domain_loads.values(), default=0),
        "total_path_snaking": total_snaking,
        "total_bit_hops": total_bit_hops,
        "cut_bits": cut_bits,
    }
    metrics["objective_key"] = [
        -worst,
        -total_negative,
        metrics["negative_paths"],
        maximum_ratio,
        metrics["maximum_capacity_domain_load"],
        total_snaking,
        total_bit_hops,
        cut_bits,
    ]
    return {
        "assignment": assignment,
        "metrics": metrics,
        "paths": path_records,
        "capacity_domains": [
            {
                "capacity_domain": key,
                "load": domain_loads[key],
                "predicted_tdm_ratio": domain_ratios[key],
            }
            for key in sorted(domain_loads)
        ],
    }


def _ranked_key(evaluation: Mapping[str, Any]) -> Tuple[int, ...]:
    result = []
    for value in evaluation["metrics"]["objective_key"]:
        if isinstance(value, int):
            result.append(value)
        else:
            result.append(round(float(value) / GAIN_QUANTUM))
    return tuple(result)


def refine_partition_pressure_exhaustive(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    *,
    max_moves: Optional[int] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Reference global-best direct/ejection refinement for compact graphs."""

    current = dict(initial_assignment["cluster_assignment"])
    initial = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
    )
    current_evaluation = initial
    moves = []
    fixed = {
        cluster["cluster"]: cluster["fixed_fpga"]
        for cluster in model["clusters"]
    }
    fpga_ids = sorted(record["fpga"] for record in model["fpgas"])
    limit = len(current) if max_moves is None else max_moves
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("partition pressure max_moves is invalid")

    while len(moves) < limit:
        incumbent_key = _ranked_key(current_evaluation)
        candidates = []
        for cluster in sorted(current):
            if fixed[cluster] is not None:
                continue
            source = current[cluster]
            for target in fpga_ids:
                if target == source:
                    continue
                trial = dict(current)
                trial[cluster] = target
                try:
                    evaluation = evaluate_partition_pressure(
                        ir,
                        platform,
                        clusters_artifact,
                        constraints,
                        route_constraints,
                        model,
                        trial,
                    )
                except ValidationError:
                    continue
                ranked = _ranked_key(evaluation)
                if ranked >= incumbent_key:
                    continue
                candidates.append(
                    (ranked, 0, cluster, target, None, None, evaluation)
                )
        movable = [
            cluster for cluster in sorted(current) if fixed[cluster] is None
        ]
        for cluster in movable:
            source = current[cluster]
            for target in fpga_ids:
                if target == source:
                    continue
                for partner in movable:
                    if partner == cluster or current[partner] != target:
                        continue
                    for partner_target in fpga_ids:
                        if partner_target == target:
                            continue
                        trial = dict(current)
                        trial[cluster] = target
                        trial[partner] = partner_target
                        try:
                            evaluation = evaluate_partition_pressure(
                                ir,
                                platform,
                                clusters_artifact,
                                constraints,
                                route_constraints,
                                model,
                                trial,
                            )
                        except ValidationError:
                            continue
                        ranked = _ranked_key(evaluation)
                        if ranked >= incumbent_key:
                            continue
                        candidates.append(
                            (
                                ranked,
                                1,
                                cluster,
                                target,
                                partner,
                                partner_target,
                                evaluation,
                            )
                        )
        if not candidates:
            break
        (
            ranked,
            phase,
            cluster,
            target,
            partner,
            partner_target,
            selected,
        ) = min(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3],
                "" if item[4] is None else item[4],
                "" if item[5] is None else item[5],
            ),
        )
        source = current[cluster]
        partner_source = None if partner is None else current[partner]
        moves.append(
            {
                "index": len(moves),
                "kind": "move" if phase == 0 else "ejection",
                "phase": phase,
                "sweep": 0,
                "cluster": cluster,
                "source": source,
                "target": target,
                "partner": partner,
                "partner_source": partner_source,
                "partner_target": partner_target,
                "before_objective_key": current_evaluation["metrics"][
                    "objective_key"
                ],
                "after_objective_key": selected["metrics"]["objective_key"],
                "ranked_objective_key": list(ranked),
            }
        )
        current[cluster] = target
        if partner is not None:
            current[partner] = partner_target
        current_evaluation = selected

    final_assignment = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        current,
        provider=PARTITION_PRESSURE_PROVIDER,
        seed=initial_assignment.get("seed", 0),
        provider_metadata={
            "initial_provider": initial_assignment.get("provider"),
            "model_sha256": _canonical_digest(model),
        },
    )
    trace = {
        "schema": PARTITION_PRESSURE_TRACE_SCHEMA,
        "design": model["design"],
        "platform": model["platform"],
        "provider": PARTITION_PRESSURE_PROVIDER,
        "model_sha256": _canonical_digest(model),
        "initial_assignment_sha256": _canonical_digest(initial_assignment),
        "initial_metrics": initial["metrics"],
        "moves": moves,
        "final_metrics": current_evaluation["metrics"],
        "final_cluster_assignment_sha256": _canonical_digest(current),
    }
    return final_assignment, trace


def validate_partition_pressure_trace(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    final_assignment: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> Dict[str, Any]:
    if trace.get("schema") != PARTITION_PRESSURE_TRACE_SCHEMA:
        raise ValidationError("partition pressure trace schema is invalid")
    expected_assignment, expected_trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        initial_assignment,
        max_moves=len(trace.get("moves", [])),
    )
    if trace != expected_trace:
        raise ValidationError("partition pressure trace reconstruction mismatch")
    if final_assignment != expected_assignment:
        raise ValidationError("partition pressure final assignment mismatch")
    return {
        "status": "pass",
        "moves": len(expected_trace["moves"]),
        "initial_objective_key": expected_trace["initial_metrics"][
            "objective_key"
        ],
        "final_objective_key": expected_trace["final_metrics"][
            "objective_key"
        ],
    }


def run_partition_pressure_reference(
    ir_path: Path,
    platform_path: Path,
    clusters_path: Path,
    constraints_path: Path,
    timing_database_path: Path,
    route_constraints_path: Path,
    initial_assignment_path: Path,
    output_dir: Path,
    *,
    max_moves: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the compact exhaustive reference and write a checked bundle."""

    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    clusters = read_json(clusters_path)
    constraints = read_json(constraints_path)
    timing = read_json(timing_database_path)
    route_constraints = normalize_route_constraints(
        read_json(route_constraints_path), platform
    )
    initial = read_json(initial_assignment_path)
    validate_partition_artifacts(ir, platform, clusters, initial)
    model = build_partition_pressure_model(
        ir,
        platform,
        clusters,
        constraints,
        timing,
        route_constraints,
    )
    final, trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters,
        constraints,
        route_constraints,
        model,
        initial,
        max_moves=max_moves,
    )
    validation = validate_partition_pressure_trace(
        ir,
        platform,
        clusters,
        constraints,
        route_constraints,
        model,
        initial,
        final,
        trace,
    )
    report = {
        "schema": PARTITION_PRESSURE_REPORT_SCHEMA,
        "status": "pass",
        "design": model["design"],
        "platform": model["platform"],
        "provider": PARTITION_PRESSURE_PROVIDER,
        "qualification": "compact-exhaustive-reference",
        "validation": validation,
        "source_sha256": {
            "ir": _canonical_digest(ir.value),
            "platform": _canonical_digest(platform.to_dict()),
            "clusters": _canonical_digest(clusters),
            "constraints": _canonical_digest(constraints),
            "timing_database": _canonical_digest(timing),
            "route_constraints": _canonical_digest(route_constraints),
            "initial_assignment": _canonical_digest(initial),
        },
        "artifacts": {
            "model": "partition_pressure_model.json",
            "trace": "partition_pressure_trace.json",
            "assignment": "assignment.json",
            "report": "partition_pressure_report.json",
        },
    }
    write_json(output_dir / "partition_pressure_model.json", model)
    write_json(output_dir / "partition_pressure_trace.json", trace)
    write_json(output_dir / "assignment.json", final)
    write_json(output_dir / "partition_pressure_report.json", report)
    return report


def _native_dimensions(
    platform: Platform, model: Mapping[str, Any]
) -> List[str]:
    dimensions = ["cells"]
    dimensions.extend(
        field
        for field in RESOURCE_FIELDS
        if any(
            cluster["resources"].get(field, 0)
            for cluster in model["clusters"]
        )
        and all(
            fpga.effective_capacity.get(field, 0) > 0
            for fpga in platform.fpgas
        )
    )
    return dimensions


def _write_patron_native_input(
    path: Path,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    max_moves: int,
    flow_version: int,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
) -> Dict[str, Any]:
    if flow_version not in {6, 9, 10, 11, 12}:
        raise ValidationError("native PATRON algorithm version is invalid")
    flow_refinement = flow_version != 6
    use_physical_feedback = physical_feedback is not None
    if flow_version == 12 and (
        clusters_artifact.get("policy", {}).get("cut_mode")
        != CUT_MODE_STATIC_EXACT
    ):
        raise ValidationError(
            "native PATRON v12 requires generalized Static Exact clusters"
        )
    if use_physical_feedback:
        if flow_version != 11:
            raise ValidationError(
                "native PATRON physical feedback requires algorithm v11"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        physical_feedback_scale = _require_finite(
            physical_feedback_scale,
            "native PATRON physical feedback scale",
        )
        if physical_feedback_scale <= 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale must be positive"
            )
        feedback_by_index = {
            record["index"]: record for record in physical_feedback["paths"]
        }
    else:
        if flow_version == 11:
            raise ValidationError(
                "native PATRON v11 requires physical feedback"
            )
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale has no artifact"
            )
        feedback_by_index = {}
    parts = sorted(record["fpga"] for record in model["fpgas"])
    part_index = {part: index for index, part in enumerate(parts)}
    clusters = sorted(model["clusters"], key=lambda item: item["cluster"])
    cluster_index = {
        cluster["cluster"]: index for index, cluster in enumerate(clusters)
    }
    dimensions = _native_dimensions(platform, model)
    original_clusters = {
        cluster["id"]: cluster for cluster in clusters_artifact["clusters"]
    }
    balance = validate_cluster_assignment_balance(
        platform,
        list(original_clusters.values()),
        initial_assignment["cluster_assignment"],
        constraints["balance_tolerance"],
        constraints.get("balance_tolerance_by_dimension", {}),
    )
    fpga_by_id = {fpga.id: fpga for fpga in platform.fpgas}
    domains = sorted(model["capacities"], key=lambda item: item["key"])
    domain_index = {
        domain["key"]: index for index, domain in enumerate(domains)
    }
    arc_domain = _capacity_key_by_arc(model)
    link_by_id = {link.id: link for link in platform.links}
    nets = sorted(model["nets"], key=lambda item: item["net"])
    net_index = {net["net"]: index for index, net in enumerate(nets)}
    max_hops = model["configuration"]["max_route_hops"]
    topology_guard_limits = (
        _static_exact_topology_guard_limits(
            clusters_artifact, model, initial_assignment
        )
        if flow_version == 12
        else {net["net"]: -1 for net in nets}
    )

    lines = [
        (
            f"EMUFLOW_PATRON_INPUT_V{flow_version}"
        ),
        (
            f"PARAM {len(parts)} {len(clusters)} {len(dimensions)} "
            f"{len(domains)} {len(nets)} {len(model['paths'])} "
            f"{-1 if max_hops is None else max_hops} "
            f"{model['configuration']['frame_slots']} "
            f"{model['configuration']['tdm_ratio_quantum']} "
            f"{constraints['min_used_fpgas']} {max_moves} "
            f"{model['normalization']['positive_slack_scale_ns']:.17g} "
            f"{model['normalization']['negative_slack_scale_ns']:.17g} "
            f"{model['normalization']['max_clock_period_ns']:.17g} "
            f"{model['configuration']['boundary_fanout_penalty_scale_ns']:.17g} "
            f"{model['configuration']['max_scalable_sweeps']} "
            f"{model['configuration']['scalable_ejection_critical_limit']} "
            f"{model['configuration']['scalable_ejection_donor_limit']} "
            f"{model['configuration']['max_scalable_ejections']}"
        ),
    ]
    if flow_refinement:
        fields = [
            "FLOW",
            "1",
            str(len(clusters)),
            str(PATRON_FLOW_CORRIDOR_DISTANCE),
            str(PATRON_FLOW_PIERCING_STRATEGY),
            str(PATRON_FLOW_MAX_LEGAL_CANDIDATES),
            str(PATRON_FLOW_MAX_POLISH_MOVES),
            str(PATRON_FLOW_MAX_FRONTIER_PATHS),
            str(PATRON_FLOW_MAX_TAIL_MOVES),
        ]
        if flow_version >= 10:
            fields.append(
                f"{(PATRON_FLOW_PHYSICAL_HOP_GUARD_SCALE_NS if flow_version == 10 else 0.0):.17g}"
            )
        lines.append(" ".join(fields))
    total_cells = sum(cluster["cells"] for cluster in clusters)
    for part in parts:
        hard = []
        allowed = []
        for dimension in dimensions:
            hard.append(
                float(total_cells)
                if dimension == "cells"
                else float(fpga_by_id[part].effective_capacity[dimension])
            )
            allowed.append(
                float(balance["balance_allowed_loads"][part][dimension])
            )
        fields = " ".join(
            f"{hard_value:.17g} {allowed_value:.17g}"
            for hard_value, allowed_value in zip(hard, allowed)
        )
        lines.append(f"CAP {part_index[part]} {fields}")
    for index, cluster in enumerate(clusters):
        current = initial_assignment["cluster_assignment"][cluster["cluster"]]
        fixed = cluster["fixed_fpga"]
        weights = [
            cluster["cells"]
            if dimension == "cells"
            else cluster["resources"].get(dimension, 0)
            for dimension in dimensions
        ]
        lines.append(
            " ".join(
                [
                    "CLUSTER",
                    str(index),
                    str(part_index[current]),
                    str(-1 if fixed is None else part_index[fixed]),
                    *(str(weight) for weight in weights),
                ]
            )
        )
    for index, domain in enumerate(domains):
        link = link_by_id[domain["link"]]
        lines.append(
            f"DOMAIN {index} {domain['lanes']} "
            f"{int(domain['link'] in set(route_constraints['sll_links']))} "
            f"{1000.0 / link.fabric_clock_mhz:.17g}"
        )
    for source in parts:
        for sink in parts:
            route = model["shortest_routes"][source][sink]
            if route is None:
                lines.append(
                    f"ROUTE {part_index[source]} {part_index[sink]} -1"
                )
                continue
            fields = []
            for arc in route:
                fields.extend(
                    [
                        str(
                            domain_index[
                                arc_domain[
                                    (arc["link"], arc["from"], arc["to"])
                                ]
                            ]
                        ),
                        f"{route_link_delay_ns(platform, arc['link'], arc['from'], arc['to'], route_constraints):.17g}",
                    ]
                )
            lines.append(
                " ".join(
                    [
                        "ROUTE",
                        str(part_index[source]),
                        str(part_index[sink]),
                        str(len(route)),
                        *fields,
                    ]
                )
            )
    for index, net in enumerate(nets):
        drivers = [cluster_index[item] for item in net["drivers"]]
        sinks = [cluster_index[item] for item in net["sinks"]]
        fields = [
            "NET",
            str(index),
            str(len(drivers)),
            *(str(item) for item in drivers),
            str(len(sinks)),
            *(str(item) for item in sinks),
        ]
        if flow_version == 12:
            fields.append(str(topology_guard_limits[net["net"]]))
        lines.append(" ".join(fields))
    for index, timing_path in enumerate(model["paths"]):
        path_nets = [
            net_index[net]
            for net in timing_path["path_nets"]
            if net in net_index
        ]
        start_cluster = timing_path.get("start_cluster")
        end_cluster = timing_path.get("end_cluster")
        fields = [
                    "PATH",
                    str(index),
                    f"{timing_path['clock_period_ns']:.17g}",
                    f"{timing_path['base_slack_ns']:.17g}",
                    str(
                        -1
                        if start_cluster is None
                        else cluster_index[start_cluster]
                    ),
                    str(
                        -1
                        if end_cluster is None
                        else cluster_index[end_cluster]
                    ),
                    str(len(path_nets)),
                    *(str(item) for item in path_nets),
                ]
        if use_physical_feedback:
            feedback = feedback_by_index.get(index)
            if feedback is None:
                fields.extend(("-1", "-1", "0"))
            else:
                fields.extend(
                    (
                        str(part_index[feedback["observed_source_fpga"]]),
                        str(part_index[feedback["observed_sink_fpga"]]),
                        f"{float(feedback['positive_residual_ns']) * physical_feedback_scale:.17g}",
                    )
                )
        lines.append(" ".join(fields))
    lines.append("END")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "parts": parts,
        "clusters": [cluster["cluster"] for cluster in clusters],
        "static_exact_topology_guard_limits": topology_guard_limits,
    }


def _metrics_from_objective(values: List[float]) -> Dict[str, Any]:
    if len(values) != 8 or not all(math.isfinite(value) for value in values):
        raise ValidationError("native PATRON objective is invalid")
    return {
        "worst_normalized_slack": -values[0],
        "total_negative_normalized_slack": -values[1],
        "negative_paths": int(values[2]),
        "maximum_predicted_tdm_ratio": int(values[3]),
        "maximum_capacity_domain_load": int(values[4]),
        "total_path_snaking": int(values[5]),
        "total_bit_hops": int(values[6]),
        "cut_bits": int(values[7]),
        "objective_key": values,
    }


def _parse_patron_native_output(
    path: Path, indexes: Mapping[str, Any]
) -> Tuple[
    Dict[str, str],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, Any],
    Dict[str, Any],
    str,
]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in (
        "EMUFLOW_PATRON_OUTPUT_V6",
        "EMUFLOW_PATRON_OUTPUT_V7",
        "EMUFLOW_PATRON_OUTPUT_V8",
        "EMUFLOW_PATRON_OUTPUT_V9",
        "EMUFLOW_PATRON_OUTPUT_V10",
        "EMUFLOW_PATRON_OUTPUT_V11",
        "EMUFLOW_PATRON_OUTPUT_V12",
    ):
        raise ValidationError("native PATRON output header is invalid")
    output_version = lines[0]
    def indexed(label: str, values: List[str], index: int) -> str:
        if index < 0 or index >= len(values):
            raise ValidationError(
                f"native PATRON {label} index is out of range"
            )
        return values[index]

    moves = []
    batches = []
    assignment: Dict[str, str] = {}
    final_metrics = None
    initial_metrics = None
    mode = None
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            raise ValidationError("native PATRON output has an empty record")
        if fields[0] == "MODE" and len(fields) == 2:
            if mode is not None:
                raise ValidationError("native PATRON returned duplicate MODE")
            mode = fields[1]
        elif fields[0] == "INITIAL" and len(fields) == 9:
            if initial_metrics is not None:
                raise ValidationError("native PATRON returned duplicate INITIAL")
            initial_metrics = _metrics_from_objective(
                [float(item) for item in fields[1:]]
            )
        elif fields[0] == "STEP" and len(fields) == 34:
            (
                index,
                phase,
                sweep,
                cluster,
                source,
                target,
                partner,
                partner_source,
                partner_target,
            ) = map(int, fields[1:10])
            if index != len(moves):
                raise ValidationError("native PATRON move indexes are invalid")
            if phase not in (0, 1) or (phase == 0) != (partner < 0):
                raise ValidationError("native PATRON step phase is invalid")
            before = [float(item) for item in fields[10:18]]
            after = [float(item) for item in fields[18:26]]
            ranked = [int(item) for item in fields[26:34]]
            moves.append(
                {
                    "index": index,
                    "kind": "move" if phase == 0 else "ejection",
                    "phase": phase,
                    "sweep": sweep,
                    "cluster": indexed(
                        "cluster", indexes["clusters"], cluster
                    ),
                    "source": indexed("part", indexes["parts"], source),
                    "target": indexed("part", indexes["parts"], target),
                    "partner": (
                        None
                        if partner < 0
                        else indexed(
                            "partner cluster", indexes["clusters"], partner
                        )
                    ),
                    "partner_source": (
                        None
                        if partner_source < 0
                        else indexed(
                            "partner part",
                            indexes["parts"],
                            partner_source,
                        )
                    ),
                    "partner_target": (
                        None
                        if partner_target < 0
                        else indexed(
                            "partner part",
                            indexes["parts"],
                            partner_target,
                        )
                    ),
                    "before_objective_key": before,
                    "after_objective_key": after,
                    "ranked_objective_key": ranked,
                }
            )
        elif fields[0] == "BATCH" and len(fields) == 27:
            if output_version not in (
                "EMUFLOW_PATRON_OUTPUT_V7",
                "EMUFLOW_PATRON_OUTPUT_V8",
                "EMUFLOW_PATRON_OUTPUT_V9",
                "EMUFLOW_PATRON_OUTPUT_V10",
                "EMUFLOW_PATRON_OUTPUT_V11",
                "EMUFLOW_PATRON_OUTPUT_V12",
            ):
                raise ValidationError("native PATRON v6 returned a BATCH")
            index, change_count = map(int, fields[1:3])
            if index != len(batches) or change_count <= 0:
                raise ValidationError("native PATRON batch header is invalid")
            batches.append(
                {
                    "index": index,
                    "changes": [],
                    "expected_changes": change_count,
                    "before_objective_key": [
                        float(item) for item in fields[3:11]
                    ],
                    "after_objective_key": [
                        float(item) for item in fields[11:19]
                    ],
                    "ranked_objective_key": [
                        int(item) for item in fields[19:27]
                    ],
                }
            )
        elif fields[0] == "CHANGE" and len(fields) == 5:
            if output_version not in (
                "EMUFLOW_PATRON_OUTPUT_V7",
                "EMUFLOW_PATRON_OUTPUT_V8",
                "EMUFLOW_PATRON_OUTPUT_V9",
                "EMUFLOW_PATRON_OUTPUT_V10",
                "EMUFLOW_PATRON_OUTPUT_V11",
                "EMUFLOW_PATRON_OUTPUT_V12",
            ):
                raise ValidationError("native PATRON v6 returned a CHANGE")
            batch, cluster, source, target = map(int, fields[1:])
            if batch < 0 or batch >= len(batches):
                raise ValidationError("native PATRON batch change is orphaned")
            record = batches[batch]
            if len(record["changes"]) >= record["expected_changes"]:
                raise ValidationError("native PATRON batch has extra changes")
            record["changes"].append(
                {
                    "cluster": indexed(
                        "cluster", indexes["clusters"], cluster
                    ),
                    "source": indexed("part", indexes["parts"], source),
                    "target": indexed("part", indexes["parts"], target),
                }
            )
        elif fields[0] == "FINAL" and len(fields) == 9:
            if final_metrics is not None:
                raise ValidationError("native PATRON returned duplicate FINAL")
            final_metrics = _metrics_from_objective(
                [float(item) for item in fields[1:]]
            )
        elif fields[0] == "ASSIGN" and len(fields) == 3:
            cluster, part = map(int, fields[1:])
            cluster_id = indexed("cluster", indexes["clusters"], cluster)
            if cluster_id in assignment:
                raise ValidationError("native PATRON returned duplicate ASSIGN")
            assignment[cluster_id] = indexed("part", indexes["parts"], part)
        elif fields == ["END"]:
            continue
        else:
            raise ValidationError(
                f"native PATRON output record is invalid: {line}"
            )
    if set(assignment) != set(indexes["clusters"]):
        raise ValidationError("native PATRON assignment coverage is invalid")
    for batch in batches:
        expected_changes = batch.pop("expected_changes")
        if len(batch["changes"]) != expected_changes:
            raise ValidationError(
                "native PATRON batch change coverage is invalid"
            )
    if final_metrics is None or initial_metrics is None or mode is None:
        raise ValidationError("native PATRON output metadata is incomplete")
    return assignment, moves, batches, initial_metrics, final_metrics, mode


def run_partition_pressure_native(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    max_moves: Optional[int] = None,
    flow_refinement: bool = False,
    algorithm_version: Optional[int] = None,
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
    output_validation: str = "full",
    retain_trace_seals: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    limit = len(model["clusters"]) if max_moves is None else max_moves
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("native PATRON max_moves is invalid")
    if not isinstance(flow_refinement, bool):
        raise ValidationError("native PATRON flow_refinement is invalid")
    if output_validation not in {"full", "online", "caller"}:
        raise ValidationError("native PATRON output validation mode is invalid")
    if not isinstance(retain_trace_seals, bool):
        raise ValidationError("native PATRON trace-seal flag is invalid")
    if algorithm_version is None:
        algorithm_version = (
            11
            if physical_feedback is not None
            else (10 if flow_refinement else 6)
        )
    if (
        isinstance(algorithm_version, bool)
        or not isinstance(algorithm_version, int)
        or algorithm_version not in {6, 9, 10, 11, 12}
    ):
        raise ValidationError("native PATRON algorithm version is invalid")
    if flow_refinement and algorithm_version == 6:
        algorithm_version = 10
    flow_refinement = algorithm_version != 6
    use_physical_feedback = physical_feedback is not None
    if use_physical_feedback:
        if algorithm_version != 11:
            raise ValidationError(
                "native PATRON physical feedback requires algorithm v11"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        physical_feedback_scale = _require_finite(
            physical_feedback_scale,
            "native PATRON physical feedback scale",
        )
        if physical_feedback_scale <= 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale must be positive"
            )
        feedback_sha256 = _canonical_digest(physical_feedback)
    else:
        if algorithm_version == 11:
            raise ValidationError(
                "native PATRON v11 requires physical feedback"
            )
        if physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON physical feedback scale has no artifact"
            )
        feedback_sha256 = None
    resolved = resolve_native_executable("emuflow_patron_refiner", executable)
    with tempfile.TemporaryDirectory(prefix="emuflow-patron-") as temporary:
        root = Path(temporary)
        native_input = root / "patron.in"
        native_output = root / "patron.out"
        indexes = _write_patron_native_input(
            native_input,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            initial_assignment,
            limit,
            algorithm_version,
            physical_feedback,
            physical_feedback_scale,
        )
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.startswith("EMUFLOW_PATRON_"):
                del environment[name]
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ValidationError(
                "native PATRON failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        (
            cluster_assignment,
            moves,
            batches,
            initial_metrics,
            final_metrics,
            mode,
        ) = (
            _parse_patron_native_output(native_output, indexes)
        )
    if algorithm_version == 12:
        _check_static_exact_topology_guard(
            model,
            cluster_assignment,
            indexes["static_exact_topology_guard_limits"],
        )
    provider_metadata = {
        "initial_provider": initial_assignment.get("provider"),
    }
    if algorithm_version == 12:
        provider_metadata["static_exact_topology_guard"] = (
            "initial-transported-non-combinational-"
            "worst-hop-non-regression-v1"
        )
    model_sha256 = None
    if retain_trace_seals:
        model_sha256 = _canonical_digest(model)
        provider_metadata["model_sha256"] = model_sha256
    final = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=(
            {
                6: PARTITION_PRESSURE_NATIVE_PROVIDER,
                9: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
                10: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
                11: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
                12: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V12,
            }[algorithm_version]
        ),
        seed=initial_assignment.get("seed", 0),
        provider_metadata=provider_metadata,
    )
    if output_validation == "full":
        validate_partition_artifacts(ir, platform, clusters_artifact, final)
    elif output_validation == "online":
        validate_partition_artifacts_online(platform, clusters_artifact, final)
    trace = {
        "schema": (
            {
                6: PARTITION_PRESSURE_TRACE_SCHEMA,
                9: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
                10: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
                11: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
                12: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V12,
            }[algorithm_version]
        ),
        "design": model["design"],
        "platform": model["platform"],
        "provider": (
            {
                6: PARTITION_PRESSURE_NATIVE_PROVIDER,
                9: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
                10: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
                11: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
                12: PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V12,
            }[algorithm_version]
        ),
        "mode": mode,
        "configuration": {
            "algorithm_version": algorithm_version,
            "max_moves": limit,
            "max_scalable_sweeps": model["configuration"][
                "max_scalable_sweeps"
            ],
            "scalable_ejection_critical_limit": model["configuration"][
                "scalable_ejection_critical_limit"
            ],
            "scalable_ejection_donor_limit": model["configuration"][
                "scalable_ejection_donor_limit"
            ],
            "max_scalable_ejections": model["configuration"][
                "max_scalable_ejections"
            ],
            "flow_refinement": _flow_refinement_configuration(
                flow_refinement,
                len(model["clusters"]),
                version=(
                    {6: 1, 9: 3, 10: 4, 11: 5, 12: 6}[
                        algorithm_version
                    ]
                ),
                physical_feedback_sha256=feedback_sha256,
                physical_feedback_scale=(
                    physical_feedback_scale
                    if use_physical_feedback
                    else 0.0
                ),
            ),
        },
        **(
            {"physical_feedback_sha256": feedback_sha256}
            if use_physical_feedback
            else {}
        ),
        "initial_metrics": initial_metrics,
        "moves": moves,
        "batches": batches,
        "final_metrics": final_metrics,
    }
    if retain_trace_seals:
        trace.update(
            {
                "model_sha256": model_sha256,
                "initial_assignment_sha256": _canonical_digest(
                    initial_assignment
                ),
                "final_cluster_assignment_sha256": _canonical_digest(
                    cluster_assignment
                ),
            }
        )
    return final, trace


def validate_partition_pressure_native_against_exhaustive(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
) -> Dict[str, Any]:
    expected_assignment, expected_trace = refine_partition_pressure_exhaustive(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        initial_assignment,
        max_moves=len(native_trace.get("moves", [])),
    )
    if (
        native_assignment.get("cluster_assignment")
        != expected_assignment.get("cluster_assignment")
    ):
        raise ValidationError("native PATRON assignment differs from exhaustive")
    if len(native_trace["moves"]) != len(expected_trace["moves"]):
        raise ValidationError("native PATRON move count differs from exhaustive")
    maximum_error = 0.0
    for native, expected in zip(
        native_trace["moves"], expected_trace["moves"]
    ):
        for field in (
            "index",
            "kind",
            "phase",
            "sweep",
            "cluster",
            "source",
            "target",
            "partner",
            "partner_source",
            "partner_target",
        ):
            if native[field] != expected[field]:
                raise ValidationError(
                    f"native PATRON move {native['index']} {field} mismatch"
                )
        if native["ranked_objective_key"] != expected["ranked_objective_key"]:
            raise ValidationError(
                f"native PATRON move {native['index']} rank mismatch"
            )
        for native_key, expected_key in (
            (native["before_objective_key"], expected["before_objective_key"]),
            (native["after_objective_key"], expected["after_objective_key"]),
        ):
            maximum_error = max(
                maximum_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(native_key, expected_key)
                ),
            )
    if maximum_error > 1.0e-10:
        raise ValidationError("native PATRON raw objective mismatch")
    return {
        "status": "pass",
        "moves": len(expected_trace["moves"]),
        "maximum_raw_objective_error": maximum_error,
    }


def validate_partition_pressure_native_bundle(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
    physical_feedback: Optional[Mapping[str, Any]] = None,
    physical_feedback_scale: float = 0.0,
) -> Dict[str, Any]:
    """Validate a production PATRON bundle without rerunning its heuristic.

    Compact exact mode is reconstructed move-for-move.  Scalable mode is a
    deterministic heuristic rather than a global-optimality claim; its gate
    independently rebuilds the immutable model, complete initial/final
    objectives, legal assignment, critical multipass order, and every recorded
    assignment transition in linear or near-linear work.
    """

    model_validation = validate_partition_pressure_model(
        ir,
        platform,
        clusters_artifact,
        constraints,
        timing_database,
        route_constraints,
        model,
    )
    trace_schema = native_trace.get("schema")
    if (
        trace_schema
        not in (
            PARTITION_PRESSURE_TRACE_SCHEMA,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V12,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8,
            PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7,
        )
        or native_trace.get("provider")
        not in (
            PARTITION_PRESSURE_NATIVE_PROVIDER,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V12,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8,
            PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7,
        )
        or native_trace.get("model_sha256") != _canonical_digest(model)
        or native_trace.get("initial_assignment_sha256")
        != _canonical_digest(initial_assignment)
    ):
        raise ValidationError("native PATRON trace seal is invalid")
    validate_partition_artifacts(
        ir, platform, clusters_artifact, initial_assignment
    )
    validate_partition_artifacts(
        ir, platform, clusters_artifact, native_assignment
    )
    if native_trace.get("final_cluster_assignment_sha256") != (
        _canonical_digest(native_assignment.get("cluster_assignment"))
    ):
        raise ValidationError("native PATRON final assignment seal is invalid")
    mode = native_trace.get("mode")
    if mode == "endpoint-exact-global-best-v6":
        if (
            trace_schema != PARTITION_PRESSURE_TRACE_SCHEMA
            or native_trace.get("provider")
            != PARTITION_PRESSURE_NATIVE_PROVIDER
        ):
            raise ValidationError("native PATRON exact trace schema is invalid")
        exact = validate_partition_pressure_native_against_exhaustive(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            initial_assignment,
            native_assignment,
            native_trace,
        )
        return {
            "status": "pass",
            "mode": mode,
            "qualification": "move-for-move-exhaustive",
            "model_validation": model_validation,
            **exact,
        }
    if mode not in (
        "endpoint-exact-critical-ejection-v6",
        "endpoint-exact-critical-flow-v7",
        "endpoint-exact-critical-flow-v8",
        "endpoint-exact-critical-flow-v9",
        "endpoint-exact-critical-flow-v10",
        "endpoint-exact-critical-flow-v11",
        "endpoint-exact-critical-flow-v12",
    ):
        raise ValidationError("native PATRON trace mode is invalid")
    expected_provider = (
        {
            "endpoint-exact-critical-flow-v7": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V7
            ),
            "endpoint-exact-critical-flow-v8": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V8
            ),
            "endpoint-exact-critical-flow-v9": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V9
            ),
            "endpoint-exact-critical-flow-v10": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER
            ),
            "endpoint-exact-critical-flow-v11": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V11
            ),
            "endpoint-exact-critical-flow-v12": (
                PARTITION_PRESSURE_FLOW_NATIVE_PROVIDER_V12
            ),
        }[mode]
        if mode in (
            "endpoint-exact-critical-flow-v7",
            "endpoint-exact-critical-flow-v8",
            "endpoint-exact-critical-flow-v9",
            "endpoint-exact-critical-flow-v10",
            "endpoint-exact-critical-flow-v11",
            "endpoint-exact-critical-flow-v12",
        )
        else PARTITION_PRESSURE_NATIVE_PROVIDER
    )
    if native_trace.get("provider") != expected_provider:
        raise ValidationError("native PATRON trace provider is invalid")

    moves = native_trace.get("moves")
    batches = native_trace.get("batches", [])
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    max_sweeps = native_trace.get("configuration", {}).get(
        "max_scalable_sweeps"
    )
    ejection_critical_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_critical_limit"
    )
    ejection_donor_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_donor_limit"
    )
    max_ejections = native_trace.get("configuration", {}).get(
        "max_scalable_ejections"
    )
    flow_configuration = native_trace.get("configuration", {}).get(
        "flow_refinement"
    )
    flow_enabled = mode in (
        "endpoint-exact-critical-flow-v7",
        "endpoint-exact-critical-flow-v8",
        "endpoint-exact-critical-flow-v9",
        "endpoint-exact-critical-flow-v10",
        "endpoint-exact-critical-flow-v11",
        "endpoint-exact-critical-flow-v12",
    )
    flow_version = {
        "endpoint-exact-critical-flow-v7": 1,
        "endpoint-exact-critical-flow-v8": 2,
        "endpoint-exact-critical-flow-v9": 3,
        "endpoint-exact-critical-flow-v10": 4,
        "endpoint-exact-critical-flow-v11": 5,
        "endpoint-exact-critical-flow-v12": 6,
    }.get(mode, 1)
    expected_algorithm_version = (
        {
            1: 7,
            2: 8,
            3: 9,
            4: 10,
            5: 11,
            6: 12,
        }[flow_version]
        if flow_enabled
        else 6
    )
    recorded_algorithm_version = native_trace.get(
        "configuration", {}
    ).get("algorithm_version", expected_algorithm_version)
    if recorded_algorithm_version != expected_algorithm_version:
        raise ValidationError(
            "native PATRON algorithm-version certificate is invalid"
        )
    if flow_version == 5:
        if physical_feedback is None:
            raise ValidationError(
                "native PATRON v11 lacks physical feedback evidence"
            )
        validate_partition_physical_feedback_seal(
            model, physical_feedback
        )
        expected_feedback_sha256 = _canonical_digest(physical_feedback)
        if native_trace.get("physical_feedback_sha256") != (
            expected_feedback_sha256
        ):
            raise ValidationError(
                "native PATRON physical feedback seal is invalid"
            )
    else:
        if physical_feedback is not None or physical_feedback_scale != 0.0:
            raise ValidationError(
                "native PATRON trace has unexpected physical feedback"
            )
        expected_feedback_sha256 = None
    expected_flow_configuration = _flow_refinement_configuration(
        flow_enabled,
        len(model["clusters"]),
        version=flow_version,
        physical_feedback_sha256=expected_feedback_sha256,
        physical_feedback_scale=(
            physical_feedback_scale if flow_version == 5 else 0.0
        ),
    )
    if flow_configuration is None and not flow_enabled:
        # V6 artifacts predate the explicit disabled-flow certificate.
        flow_configuration = expected_flow_configuration
    if (
        not isinstance(moves, list)
        or not isinstance(batches, list)
        or (
            mode
            in (
                "endpoint-exact-critical-flow-v7",
                "endpoint-exact-critical-flow-v8",
                "endpoint-exact-critical-flow-v9",
                "endpoint-exact-critical-flow-v10",
                "endpoint-exact-critical-flow-v11",
                "endpoint-exact-critical-flow-v12",
            )
            and (
                trace_schema
                != (
                    {
                        1: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V7,
                        2: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V8,
                        3: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V9,
                        4: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA,
                        5: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V11,
                        6: PARTITION_PRESSURE_FLOW_TRACE_SCHEMA_V12,
                    }[flow_version]
                )
            )
        )
        or (
            mode == "endpoint-exact-critical-ejection-v6"
            and (
                trace_schema != PARTITION_PRESSURE_TRACE_SCHEMA
                or batches
            )
        )
        or isinstance(max_moves, bool)
        or not isinstance(max_moves, int)
        or max_moves < 0
        or len(moves) > max_moves
        or isinstance(max_sweeps, bool)
        or not isinstance(max_sweeps, int)
        or max_sweeps <= 0
        or max_sweeps
        != model["configuration"].get("max_scalable_sweeps")
        or isinstance(ejection_critical_limit, bool)
        or not isinstance(ejection_critical_limit, int)
        or ejection_critical_limit < 0
        or ejection_critical_limit
        != model["configuration"].get("scalable_ejection_critical_limit")
        or isinstance(ejection_donor_limit, bool)
        or not isinstance(ejection_donor_limit, int)
        or ejection_donor_limit < 0
        or ejection_donor_limit
        != model["configuration"].get("scalable_ejection_donor_limit")
        or isinstance(max_ejections, bool)
        or not isinstance(max_ejections, int)
        or max_ejections < 0
        or max_ejections
        != model["configuration"].get("max_scalable_ejections")
        or flow_configuration != expected_flow_configuration
    ):
        raise ValidationError("native PATRON scalable trace bounds are invalid")
    physical_hop_guard_scale_ns = float(
        expected_flow_configuration.get("physical_hop_guard_scale_ns", 0.0)
    )
    static_exact_topology_guard_limits = (
        _static_exact_topology_guard_limits(
            clusters_artifact, model, initial_assignment
        )
        if flow_version == 6
        else None
    )
    cluster_ids = {
        record["cluster"] for record in model["clusters"]
    }
    fpga_ids = {record["fpga"] for record in model["fpgas"]}
    fixed = {
        record["cluster"]: record["fixed_fpga"]
        for record in model["clusters"]
    }
    exposure = {cluster: 0.0 for cluster in cluster_ids}
    net_by_id = {record["net"]: record for record in model["nets"]}
    for path in model["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - path["base_slack_ns"] / path["clock_period_ns"],
            ),
        )
        weight = 1.0 + 9.0 * criticality * criticality
        for cluster in {
            cluster
            for net in path["path_nets"]
            if net in net_by_id
            for cluster in (
                net_by_id[net]["drivers"] + net_by_id[net]["sinks"]
            )
        }:
            exposure[cluster] += weight
    sweep_order = sorted(
        cluster_ids, key=lambda cluster: (-exposure[cluster], cluster)
    )
    sweep_index = {cluster: index for index, cluster in enumerate(sweep_order)}
    current = dict(initial_assignment["cluster_assignment"])
    previous_sweep = -1
    previous_sweep_index = -1
    previous_phase = 0
    previous_ejection_index = -1
    ejection_count = 0
    previous_after = None
    maximum_chain_error = 0.0
    for index, move in enumerate(moves):
        if not isinstance(move, dict) or move.get("index") != index:
            raise ValidationError("native PATRON move index is invalid")
        cluster = move.get("cluster")
        phase = move.get("phase")
        kind = move.get("kind")
        sweep = move.get("sweep")
        source = move.get("source")
        target = move.get("target")
        partner = move.get("partner")
        partner_source = move.get("partner_source")
        partner_target = move.get("partner_target")
        common_invalid = (
            isinstance(phase, bool)
            or not isinstance(phase, int)
            or phase not in (0, 1)
            or kind != ("move" if phase == 0 else "ejection")
            or isinstance(sweep, bool)
            or not isinstance(sweep, int)
            or cluster not in cluster_ids
            or source not in fpga_ids
            or target not in fpga_ids
            or source == target
            or fixed[cluster] is not None
            or current.get(cluster) != source
            or phase < previous_phase
        )
        direct_invalid = phase == 0 and not common_invalid and (
            sweep < 0
            or sweep >= max_sweeps
            or sweep > previous_sweep + 1
            or (previous_sweep < 0 and sweep != 0)
            or (
                sweep == previous_sweep
                and sweep_index.get(cluster, -1) <= previous_sweep_index
            )
            or partner is not None
            or partner_source is not None
            or partner_target is not None
        )
        ejection_invalid = phase == 1 and not common_invalid and (
            sweep != 0
            or partner not in cluster_ids
            or partner == cluster
            or partner_source not in fpga_ids
            or partner_target not in fpga_ids
            or fixed.get(partner) is not None
            or current.get(partner) != partner_source
            or target != partner_source
            or partner_target == partner_source
            or sweep_index.get(cluster, ejection_critical_limit)
            >= ejection_critical_limit
            or sweep_index.get(cluster, -1) <= previous_ejection_index
            or ejection_count >= max_ejections
        )
        if common_invalid or direct_invalid or ejection_invalid:
            raise ValidationError(
                f"native PATRON move {index} transition is invalid"
            )
        before = move.get("before_objective_key")
        after = move.get("after_objective_key")
        ranked = move.get("ranked_objective_key")
        if (
            not isinstance(before, list)
            or not isinstance(after, list)
            or not isinstance(ranked, list)
            or len(before) != 8
            or len(after) != 8
            or len(ranked) != 8
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in before + after
            )
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in ranked
            )
            or tuple(ranked) >= tuple(
                round(float(value) / GAIN_QUANTUM)
                if objective_index < 2
                else round(float(value))
                for objective_index, value in enumerate(before)
            )
        ):
            raise ValidationError(
                f"native PATRON move {index} objective is invalid"
            )
        expected_rank = [
            round(float(value) / GAIN_QUANTUM)
            if objective_index < 2
            else round(float(value))
            for objective_index, value in enumerate(after)
        ]
        if ranked != expected_rank:
            raise ValidationError(
                f"native PATRON move {index} ranked objective is invalid"
            )
        if previous_after is not None:
            maximum_chain_error = max(
                maximum_chain_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(before, previous_after)
                ),
            )
        current[cluster] = target
        if phase == 1:
            current[partner] = partner_target
            previous_ejection_index = sweep_index[cluster]
            ejection_count += 1
        previous_after = after
        if phase == 0:
            if sweep != previous_sweep:
                previous_sweep_index = -1
            previous_sweep = sweep
            previous_sweep_index = sweep_index[cluster]
        previous_phase = phase
    for batch_index, batch in enumerate(batches):
        if (
            not isinstance(batch, dict)
            or batch.get("index") != batch_index
            or not isinstance(batch.get("changes"), list)
            or not batch["changes"]
        ):
            raise ValidationError("native PATRON batch record is invalid")
        before_evaluation = evaluate_partition_pressure(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            current,
            include_tdm_wait=True,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            physical_feedback=physical_feedback,
            physical_feedback_scale=physical_feedback_scale,
            static_exact_topology_guard_limits=(
                static_exact_topology_guard_limits
            ),
        )
        before_expected = before_evaluation["metrics"]["objective_key"]
        trial = dict(current)
        changed = set()
        previous_cluster = None
        for change in batch["changes"]:
            if not isinstance(change, dict):
                raise ValidationError("native PATRON batch change is invalid")
            cluster = change.get("cluster")
            source = change.get("source")
            target = change.get("target")
            if (
                cluster not in cluster_ids
                or source not in fpga_ids
                or target not in fpga_ids
                or source == target
                or fixed.get(cluster) is not None
                or current.get(cluster) != source
                or cluster in changed
                or (
                    previous_cluster is not None
                    and cluster <= previous_cluster
                )
            ):
                raise ValidationError(
                    "native PATRON batch transition is invalid"
                )
            changed.add(cluster)
            previous_cluster = cluster
            trial[cluster] = target
        after_evaluation = evaluate_partition_pressure(
            ir,
            platform,
            clusters_artifact,
            constraints,
            route_constraints,
            model,
            trial,
            include_tdm_wait=True,
            physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
            physical_feedback=physical_feedback,
            physical_feedback_scale=physical_feedback_scale,
            static_exact_topology_guard_limits=(
                static_exact_topology_guard_limits
            ),
        )
        after_expected = after_evaluation["metrics"]["objective_key"]
        before = batch.get("before_objective_key")
        after = batch.get("after_objective_key")
        ranked = batch.get("ranked_objective_key")
        if (
            not isinstance(before, list)
            or not isinstance(after, list)
            or not isinstance(ranked, list)
            or len(before) != 8
            or len(after) != 8
            or len(ranked) != 8
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in before + after
            )
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in ranked
            )
        ):
            raise ValidationError("native PATRON batch objective is invalid")
        for label, actual, expected in (
            ("before", before, before_expected),
            ("after", after, after_expected),
        ):
            for objective_index, (left, right) in enumerate(
                zip(actual, expected)
            ):
                matches = (
                    math.isclose(
                        float(left),
                        float(right),
                        rel_tol=1.0e-12,
                        abs_tol=1.0e-9,
                    )
                    if objective_index < 2
                    else float(left) == float(right)
                )
                if not matches:
                    raise ValidationError(
                        f"native PATRON batch {label} objective mismatch"
                    )
        expected_rank = list(_ranked_key(after_evaluation))
        before_rank = _ranked_key(before_evaluation)
        if (
            ranked != expected_rank
            or ranked[0] >= before_rank[0]
            or ranked[1] >= before_rank[1]
        ):
            raise ValidationError(
                "native PATRON batch is not dual-objective improving"
            )
        if previous_after is not None:
            maximum_chain_error = max(
                maximum_chain_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(before, previous_after)
                ),
            )
        previous_after = after
        current = trial
    if current != native_assignment["cluster_assignment"]:
        raise ValidationError("native PATRON move chain assignment mismatch")
    if maximum_chain_error > 1.0e-9:
        raise ValidationError("native PATRON objective chain is discontinuous")

    initial_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        initial_assignment["cluster_assignment"],
        include_tdm_wait=True,
        physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        physical_feedback=physical_feedback,
        physical_feedback_scale=physical_feedback_scale,
        static_exact_topology_guard_limits=(
            static_exact_topology_guard_limits
        ),
    )
    final_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        native_assignment["cluster_assignment"],
        include_tdm_wait=True,
        physical_hop_guard_scale_ns=physical_hop_guard_scale_ns,
        physical_feedback=physical_feedback,
        physical_feedback_scale=physical_feedback_scale,
        static_exact_topology_guard_limits=(
            static_exact_topology_guard_limits
        ),
    )
    maximum_endpoint_error = 0.0
    maximum_endpoint_relative_error = 0.0
    for actual, expected in (
        (
            native_trace.get("initial_metrics", {}).get("objective_key"),
            initial_evaluation["metrics"]["objective_key"],
        ),
        (
            native_trace.get("final_metrics", {}).get("objective_key"),
            final_evaluation["metrics"]["objective_key"],
        ),
    ):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValidationError(
                "native PATRON endpoint objective is invalid"
            )
        for objective_index, (left, right) in enumerate(
            zip(actual, expected)
        ):
            left_value = float(left)
            right_value = float(right)
            error = abs(left_value - right_value)
            maximum_endpoint_error = max(maximum_endpoint_error, error)
            maximum_endpoint_relative_error = max(
                maximum_endpoint_relative_error,
                error / max(1.0, abs(right_value)),
            )
            if objective_index < 2:
                matches = math.isclose(
                    left_value,
                    right_value,
                    rel_tol=1.0e-12,
                    abs_tol=1.0e-9,
                )
            else:
                matches = left_value == right_value
            if not matches:
                raise ValidationError(
                    "native PATRON endpoint objective mismatch: "
                    f"field={objective_index}, native={left_value}, "
                    f"independent={right_value}, error={error}"
                )
    return {
        "status": "pass",
        "mode": mode,
        "qualification": "linear-transition-and-endpoint-reconstruction",
        "model_validation": model_validation,
        "moves": len(moves),
        "batches": len(batches),
        "maximum_objective_chain_error": maximum_chain_error,
        "maximum_endpoint_objective_error": maximum_endpoint_error,
        "maximum_endpoint_relative_objective_error": (
            maximum_endpoint_relative_error
        ),
    }


def validate_partition_pressure_scalable_trace(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
    model: Mapping[str, Any],
    initial_assignment: Mapping[str, Any],
    native_assignment: Mapping[str, Any],
    native_trace: Mapping[str, Any],
) -> Dict[str, Any]:
    """Independently replay the scalable criticality-ordered sweep.

    This exhaustive reconstruction is intended for compact/scaled-down
    instances.  Large production bundles use the indexed certificate checker.
    """

    if native_trace.get("mode") != "endpoint-exact-critical-ejection-v6":
        raise ValidationError("native PATRON scalable trace mode is invalid")
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    if isinstance(max_moves, bool) or not isinstance(max_moves, int) or max_moves < 0:
        raise ValidationError("native PATRON scalable max_moves is invalid")
    max_sweeps = native_trace.get("configuration", {}).get(
        "max_scalable_sweeps"
    )
    ejection_critical_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_critical_limit"
    )
    ejection_donor_limit = native_trace.get("configuration", {}).get(
        "scalable_ejection_donor_limit"
    )
    max_ejections = native_trace.get("configuration", {}).get(
        "max_scalable_ejections"
    )
    if (
        isinstance(max_sweeps, bool)
        or not isinstance(max_sweeps, int)
        or max_sweeps <= 0
        or max_sweeps
        != model["configuration"].get("max_scalable_sweeps")
        or isinstance(ejection_critical_limit, bool)
        or not isinstance(ejection_critical_limit, int)
        or ejection_critical_limit < 0
        or ejection_critical_limit
        != model["configuration"].get("scalable_ejection_critical_limit")
        or isinstance(ejection_donor_limit, bool)
        or not isinstance(ejection_donor_limit, int)
        or ejection_donor_limit < 0
        or ejection_donor_limit
        != model["configuration"].get("scalable_ejection_donor_limit")
        or isinstance(max_ejections, bool)
        or not isinstance(max_ejections, int)
        or max_ejections < 0
        or max_ejections
        != model["configuration"].get("max_scalable_ejections")
    ):
        raise ValidationError("native PATRON scalable sweep limit is invalid")
    clusters = sorted(record["cluster"] for record in model["clusters"])
    parts = sorted(record["fpga"] for record in model["fpgas"])
    exposure = {cluster: 0.0 for cluster in clusters}
    net_by_id = {record["net"]: record for record in model["nets"]}
    for path in model["paths"]:
        criticality = max(
            0.0,
            min(
                1.0,
                1.0
                - path["base_slack_ns"] / path["clock_period_ns"],
            ),
        )
        weight = 1.0 + 9.0 * criticality * criticality
        touched = {
            cluster
            for net in path["path_nets"]
            if net in net_by_id
            for cluster in (
                net_by_id[net]["drivers"] + net_by_id[net]["sinks"]
            )
        }
        for cluster in touched:
            exposure[cluster] += weight
    order = sorted(clusters, key=lambda cluster: (-exposure[cluster], cluster))
    fixed = {
        record["cluster"]: record["fixed_fpga"]
        for record in model["clusters"]
    }
    current = dict(initial_assignment["cluster_assignment"])
    current_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
        include_tdm_wait=True,
    )
    moves = native_trace.get("moves")
    if not isinstance(moves, list):
        raise ValidationError("native PATRON scalable moves are invalid")
    move_index = 0
    maximum_error = 0.0
    for _sweep in range(max_sweeps):
        sweep_start = move_index
        for cluster in order:
            if move_index >= max_moves:
                break
            candidates = []
            source = current[cluster]
            for target in parts:
                if target == source:
                    continue
                trial = dict(current)
                trial[cluster] = target
                try:
                    evaluation = evaluate_partition_pressure(
                        ir,
                        platform,
                        clusters_artifact,
                        constraints,
                        route_constraints,
                        model,
                        trial,
                        include_tdm_wait=True,
                    )
                except ValidationError:
                    continue
                ranked = _ranked_key(evaluation)
                if ranked < _ranked_key(current_evaluation):
                    candidates.append((ranked, target, evaluation))
            if not candidates:
                continue
            ranked, target, selected = min(
                candidates, key=lambda item: (item[0], item[1])
            )
            if move_index >= len(moves):
                raise ValidationError(
                    "native PATRON scalable trace omitted a move"
                )
            actual = moves[move_index]
            expected_identity = {
                "index": move_index,
                "kind": "move",
                "phase": 0,
                "sweep": _sweep,
                "cluster": cluster,
                "source": source,
                "target": target,
                "partner": None,
                "partner_source": None,
                "partner_target": None,
            }
            for field, expected in expected_identity.items():
                if actual.get(field) != expected:
                    raise ValidationError(
                        f"native PATRON scalable move {move_index} "
                        f"{field} mismatch"
                    )
            if actual.get("ranked_objective_key") != list(ranked):
                raise ValidationError(
                    f"native PATRON scalable move {move_index} rank mismatch"
                )
            for actual_key, expected_key in (
                (
                    actual["before_objective_key"],
                    current_evaluation["metrics"]["objective_key"],
                ),
                (
                    actual["after_objective_key"],
                    selected["metrics"]["objective_key"],
                ),
            ):
                maximum_error = max(
                    maximum_error,
                    max(
                        abs(float(left) - float(right))
                        for left, right in zip(actual_key, expected_key)
                    ),
                )
            current[cluster] = target
            current_evaluation = selected
            move_index += 1
        if move_index >= max_moves or move_index == sweep_start:
            break

    donors = {part: [] for part in parts}
    for cluster in sorted(clusters, key=lambda item: (exposure[item], item)):
        if fixed[cluster] is None:
            donors[current[cluster]].append(cluster)
    accepted_ejections = 0
    for cluster in order[:ejection_critical_limit]:
        if (
            accepted_ejections >= max_ejections
            or move_index >= max_moves
        ):
            break
        if fixed[cluster] is not None:
            continue
        source = current[cluster]
        candidates = []
        for target in parts:
            if target == source:
                continue
            considered = 0
            for partner in donors[target]:
                if considered >= ejection_donor_limit:
                    break
                if (
                    partner == cluster
                    or current[partner] != target
                    or fixed[partner] is not None
                ):
                    continue
                considered += 1
                for partner_target in parts:
                    if partner_target == target:
                        continue
                    trial = dict(current)
                    trial[cluster] = target
                    trial[partner] = partner_target
                    try:
                        evaluation = evaluate_partition_pressure(
                            ir,
                            platform,
                            clusters_artifact,
                            constraints,
                            route_constraints,
                            model,
                            trial,
                            include_tdm_wait=True,
                        )
                    except ValidationError:
                        continue
                    ranked = _ranked_key(evaluation)
                    if ranked < _ranked_key(current_evaluation):
                        candidates.append(
                            (
                                ranked,
                                target,
                                partner,
                                partner_target,
                                evaluation,
                            )
                        )
        if not candidates:
            continue
        ranked, target, partner, partner_target, selected = min(
            candidates,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
        if move_index >= len(moves):
            raise ValidationError(
                "native PATRON scalable trace omitted an ejection"
            )
        actual = moves[move_index]
        expected_identity = {
            "index": move_index,
            "kind": "ejection",
            "phase": 1,
            "sweep": 0,
            "cluster": cluster,
            "source": source,
            "target": target,
            "partner": partner,
            "partner_source": target,
            "partner_target": partner_target,
        }
        for field, expected in expected_identity.items():
            if actual.get(field) != expected:
                raise ValidationError(
                    f"native PATRON scalable ejection {move_index} "
                    f"{field} mismatch"
                )
        if actual.get("ranked_objective_key") != list(ranked):
            raise ValidationError(
                f"native PATRON scalable ejection {move_index} rank mismatch"
            )
        for actual_key, expected_key in (
            (
                actual["before_objective_key"],
                current_evaluation["metrics"]["objective_key"],
            ),
            (
                actual["after_objective_key"],
                selected["metrics"]["objective_key"],
            ),
        ):
            maximum_error = max(
                maximum_error,
                max(
                    abs(float(left) - float(right))
                    for left, right in zip(actual_key, expected_key)
                ),
            )
        current[cluster] = target
        current[partner] = partner_target
        current_evaluation = selected
        move_index += 1
        accepted_ejections += 1
    if move_index != len(moves):
        raise ValidationError("native PATRON scalable trace has extra moves")
    if current != native_assignment.get("cluster_assignment"):
        raise ValidationError("native PATRON scalable final assignment mismatch")
    if maximum_error > 1.0e-9:
        raise ValidationError("native PATRON scalable raw objective mismatch")
    return {
        "status": "pass",
        "moves": move_index,
        "maximum_raw_objective_error": maximum_error,
    }
