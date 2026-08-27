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
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .io import read_json, write_json
from .partition import (
    TRANSPORTED_CUT_CLASSES,
    build_partition_assignment,
    validate_cluster_assignment_balance,
    validate_partition_artifacts,
)
from .platform import Platform
from .native_tools import resolve_native_executable
from .resources import RESOURCE_FIELDS
from .routing import (
    build_directed_graph,
    estimate_tdm_ratio,
    normalize_route_constraints,
    route_link_delay_ns,
)
from .sta import _normalized_slack, _validate_database_normalization


PARTITION_PRESSURE_MODEL_SCHEMA = "emuflow.partition-pressure-model/v1"
PARTITION_PRESSURE_TRACE_SCHEMA = "emuflow.partition-pressure-trace/v1"
PARTITION_PRESSURE_REPORT_SCHEMA = "emuflow.partition-pressure-report/v1"
PARTITION_PRESSURE_PROVIDER = "patron-exhaustive-reference-v1"
PARTITION_PRESSURE_NATIVE_PROVIDER = "patron-native-exact-v1"
GAIN_QUANTUM = 1.0e-9


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    records = []
    for net in ir.value["nets"]:
        if net["cut_class"] not in TRANSPORTED_CUT_CLASSES:
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
        paths.append(
            {
                "path": path_id,
                "clock_domain": raw.get("clock_domain"),
                "clock_period_ns": period,
                "base_slack_ns": slack,
                "path_nets": list(path_nets),
            }
        )
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
            "max_route_hops": route_constraints.get("max_route_hops"),
            "frame_slots": route_constraints["frame_slots"],
            "tdm_ratio_quantum": route_constraints["tdm_ratio_quantum"],
            "predicted_wait": "sum((domain_ratio-1)*link_cycle_ns)",
            "path_delay": "sum(max_remote_sink_delay_per_path_net)",
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
        "nets": _net_records(ir, clusters_artifact),
        "paths": paths,
    }
def build_partition_pressure_model(
    ir: EmuIR,
    platform: Platform,
    clusters_artifact: Mapping[str, Any],
    constraints: Mapping[str, Any],
    timing_database: Mapping[str, Any],
    route_constraints: Mapping[str, Any],
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
    cluster_parts = dict(cluster_assignment)
    domain_by_arc = _capacity_key_by_arc(model)
    capacity_by_key = {record["key"]: record for record in model["capacities"]}
    domain_loads = {key: 0 for key in capacity_by_key}
    routes_by_net: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    max_hops = model["configuration"]["max_route_hops"]
    unreachable = []

    for net in model["nets"]:
        sources = sorted({cluster_parts[item] for item in net["drivers"]})
        sinks = sorted({cluster_parts[item] for item in net["sinks"]})
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
    link_by_id = {link.id: link for link in platform.links}
    net_delay: Dict[str, float] = {}
    net_worst_transition: Dict[str, Tuple[str, str]] = {}
    total_bit_hops = 0
    for net, routes in routes_by_net.items():
        maximum = 0.0
        worst_transition: Optional[Tuple[str, str]] = None
        for route in routes:
            delay = 0.0
            for arc in route["arcs"]:
                domain = domain_by_arc[
                    (arc["link"], arc["from"], arc["to"])
                ]
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
    path_records = []
    for path in model["paths"]:
        transport = sum(net_delay.get(net, 0.0) for net in path["path_nets"])
        predicted_slack = path["base_slack_ns"] - transport
        normalized = _normalized_slack(
            path["clock_period_ns"], predicted_slack, normalization
        )
        partition_sequence: List[str] = []
        for net in path["path_nets"]:
            transition = net_worst_transition.get(net)
            if transition is None:
                continue
            for part in transition:
                if not partition_sequence or partition_sequence[-1] != part:
                    partition_sequence.append(part)
        transitions = max(0, len(partition_sequence) - 1)
        seen_parts = set()
        snaking = 0
        for part in partition_sequence:
            if part in seen_parts:
                snaking += 1
            seen_parts.add(part)
        path_records.append(
            {
                "path": path["path"],
                "transport_delay_ns": transport,
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
    """Reference global-best direct K-way refinement for compact graphs."""

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
                candidates.append((ranked, cluster, target, evaluation))
        if not candidates:
            break
        ranked, cluster, target, selected = min(
            candidates, key=lambda item: (item[0], item[1], item[2])
        )
        source = current[cluster]
        moves.append(
            {
                "index": len(moves),
                "cluster": cluster,
                "source": source,
                "target": target,
                "before_objective_key": current_evaluation["metrics"][
                    "objective_key"
                ],
                "after_objective_key": selected["metrics"]["objective_key"],
                "ranked_objective_key": list(ranked),
            }
        )
        current[cluster] = target
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
) -> Dict[str, Any]:
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

    lines = [
        "EMUFLOW_PATRON_INPUT_V1",
        (
            f"PARAM {len(parts)} {len(clusters)} {len(dimensions)} "
            f"{len(domains)} {len(nets)} {len(model['paths'])} "
            f"{-1 if max_hops is None else max_hops} "
            f"{model['configuration']['frame_slots']} "
            f"{model['configuration']['tdm_ratio_quantum']} "
            f"{constraints['min_used_fpgas']} {max_moves} "
            f"{model['normalization']['positive_slack_scale_ns']:.17g} "
            f"{model['normalization']['negative_slack_scale_ns']:.17g} "
            f"{model['normalization']['max_clock_period_ns']:.17g}"
        ),
    ]
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
        lines.append(
            " ".join(
                [
                    "NET",
                    str(index),
                    str(len(drivers)),
                    *(str(item) for item in drivers),
                    str(len(sinks)),
                    *(str(item) for item in sinks),
                ]
            )
        )
    for index, timing_path in enumerate(model["paths"]):
        path_nets = [
            net_index[net]
            for net in timing_path["path_nets"]
            if net in net_index
        ]
        lines.append(
            " ".join(
                [
                    "PATH",
                    str(index),
                    f"{timing_path['clock_period_ns']:.17g}",
                    f"{timing_path['base_slack_ns']:.17g}",
                    str(len(path_nets)),
                    *(str(item) for item in path_nets),
                ]
            )
        )
    lines.append("END")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "parts": parts,
        "clusters": [cluster["cluster"] for cluster in clusters],
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
    Dict[str, Any],
    Dict[str, Any],
    str,
]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_PATRON_OUTPUT_V1":
        raise ValidationError("native PATRON output header is invalid")
    moves = []
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
        elif fields[0] == "MOVE" and len(fields) == 29:
            index, cluster, source, target = map(int, fields[1:5])
            if index != len(moves):
                raise ValidationError("native PATRON move indexes are invalid")
            before = [float(item) for item in fields[5:13]]
            after = [float(item) for item in fields[13:21]]
            ranked = [int(item) for item in fields[21:29]]
            moves.append(
                {
                    "index": index,
                    "cluster": indexes["clusters"][cluster],
                    "source": indexes["parts"][source],
                    "target": indexes["parts"][target],
                    "before_objective_key": before,
                    "after_objective_key": after,
                    "ranked_objective_key": ranked,
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
            cluster_id = indexes["clusters"][cluster]
            if cluster_id in assignment:
                raise ValidationError("native PATRON returned duplicate ASSIGN")
            assignment[cluster_id] = indexes["parts"][part]
        elif fields == ["END"]:
            continue
        else:
            raise ValidationError(
                f"native PATRON output record is invalid: {line}"
            )
    if set(assignment) != set(indexes["clusters"]):
        raise ValidationError("native PATRON assignment coverage is invalid")
    if final_metrics is None or initial_metrics is None or mode is None:
        raise ValidationError("native PATRON output metadata is incomplete")
    return assignment, moves, initial_metrics, final_metrics, mode


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
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    limit = len(model["clusters"]) if max_moves is None else max_moves
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValidationError("native PATRON max_moves is invalid")
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
        )
        completed = subprocess.run(
            [resolved, str(native_input), str(native_output)],
            capture_output=True,
            text=True,
            check=False,
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
            initial_metrics,
            final_metrics,
            mode,
        ) = (
            _parse_patron_native_output(native_output, indexes)
        )
    final = build_partition_assignment(
        ir,
        platform,
        clusters_artifact,
        constraints,
        cluster_assignment,
        provider=PARTITION_PRESSURE_NATIVE_PROVIDER,
        seed=initial_assignment.get("seed", 0),
        provider_metadata={
            "initial_provider": initial_assignment.get("provider"),
            "model_sha256": _canonical_digest(model),
        },
    )
    validate_partition_artifacts(ir, platform, clusters_artifact, final)
    return final, {
        "schema": PARTITION_PRESSURE_TRACE_SCHEMA,
        "design": model["design"],
        "platform": model["platform"],
        "provider": PARTITION_PRESSURE_NATIVE_PROVIDER,
        "mode": mode,
        "configuration": {"max_moves": limit},
        "model_sha256": _canonical_digest(model),
        "initial_assignment_sha256": _canonical_digest(initial_assignment),
        "initial_metrics": initial_metrics,
        "moves": moves,
        "final_metrics": final_metrics,
        "final_cluster_assignment_sha256": _canonical_digest(
            cluster_assignment
        ),
    }


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
        for field in ("index", "cluster", "source", "target"):
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
) -> Dict[str, Any]:
    """Validate a production PATRON bundle without rerunning its heuristic.

    Compact exact mode is reconstructed move-for-move.  Scalable mode is a
    deterministic heuristic rather than a global-optimality claim; its gate
    independently rebuilds the immutable model, complete initial/final
    objectives, legal assignment, critical-sweep order, and every recorded
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
    if (
        native_trace.get("schema") != PARTITION_PRESSURE_TRACE_SCHEMA
        or native_trace.get("provider") != PARTITION_PRESSURE_NATIVE_PROVIDER
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
    if mode == "exact-global-best-v1":
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
    if mode != "scalable-critical-sweep-v1":
        raise ValidationError("native PATRON trace mode is invalid")

    moves = native_trace.get("moves")
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    if (
        not isinstance(moves, list)
        or isinstance(max_moves, bool)
        or not isinstance(max_moves, int)
        or max_moves < 0
        or len(moves) > max_moves
    ):
        raise ValidationError("native PATRON scalable trace bounds are invalid")
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
    previous_sweep_index = -1
    previous_after = None
    maximum_chain_error = 0.0
    for index, move in enumerate(moves):
        if not isinstance(move, dict) or move.get("index") != index:
            raise ValidationError("native PATRON move index is invalid")
        cluster = move.get("cluster")
        source = move.get("source")
        target = move.get("target")
        if (
            cluster not in cluster_ids
            or source not in fpga_ids
            or target not in fpga_ids
            or source == target
            or fixed[cluster] is not None
            or current.get(cluster) != source
            or sweep_index[cluster] <= previous_sweep_index
        ):
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
        previous_after = after
        previous_sweep_index = sweep_index[cluster]
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
        include_tdm_wait=False,
    )
    final_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        native_assignment["cluster_assignment"],
        include_tdm_wait=False,
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

    if native_trace.get("mode") != "scalable-critical-sweep-v1":
        raise ValidationError("native PATRON scalable trace mode is invalid")
    max_moves = native_trace.get("configuration", {}).get("max_moves")
    if isinstance(max_moves, bool) or not isinstance(max_moves, int) or max_moves < 0:
        raise ValidationError("native PATRON scalable max_moves is invalid")
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
    current = dict(initial_assignment["cluster_assignment"])
    current_evaluation = evaluate_partition_pressure(
        ir,
        platform,
        clusters_artifact,
        constraints,
        route_constraints,
        model,
        current,
        include_tdm_wait=False,
    )
    moves = native_trace.get("moves")
    if not isinstance(moves, list):
        raise ValidationError("native PATRON scalable moves are invalid")
    move_index = 0
    maximum_error = 0.0
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
                    include_tdm_wait=False,
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
            raise ValidationError("native PATRON scalable trace omitted a move")
        actual = moves[move_index]
        expected_identity = {
            "index": move_index,
            "cluster": cluster,
            "source": source,
            "target": target,
        }
        for field, expected in expected_identity.items():
            if actual.get(field) != expected:
                raise ValidationError(
                    f"native PATRON scalable move {move_index} {field} mismatch"
                )
        if actual.get("ranked_objective_key") != list(ranked):
            raise ValidationError(
                f"native PATRON scalable move {move_index} rank mismatch"
            )
        for actual_key, expected_key in (
            (actual["before_objective_key"], current_evaluation["metrics"]["objective_key"]),
            (actual["after_objective_key"], selected["metrics"]["objective_key"]),
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
    if move_index != len(moves):
        raise ValidationError("native PATRON scalable trace has extra moves")
    if current != native_assignment.get("cluster_assignment"):
        raise ValidationError("native PATRON scalable final assignment mismatch")
    if maximum_error > 1.0e-10:
        raise ValidationError("native PATRON scalable raw objective mismatch")
    return {
        "status": "pass",
        "moves": move_index,
        "maximum_raw_objective_error": maximum_error,
    }
