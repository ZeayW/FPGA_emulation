"""Checked Phase 3--5 candidate evaluation and feedback orchestration."""

from __future__ import annotations

import hashlib
import itertools
import math
import shutil
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .board_link_timing import (
    directed_route_link_delays,
    validate_board_link_timing,
)
from .errors import EmuFlowError, ValidationError
from .frame_search import (
    run_frame_length_search,
    validate_frame_search_report,
)
from .io import read_json, write_json
from .ir import EmuIR
from .partition import (
    PARTITION_ASSIGNMENT_SCHEMA,
    build_clusters,
    load_partition_constraints,
    validate_partition_artifacts,
)
from .partition_feedback import (
    run_damped_partition_feedback,
    run_partition_feedback,
    validate_damped_partition_feedback,
    validate_partition_feedback,
)
from .phase3 import run_phase3
from .phase4 import run_phase4
from .phase5 import run_phase5
from .platform import Platform
from .routing import (
    SYSTEM_ROUTE_CONSTRAINTS_SCHEMA,
    load_route_constraints,
    route_link_delay_ns,
    validate_system_routes,
)
from .runtime import build_virtual_runtime, validate_virtual_runtime
from .sta import (
    STA_PATH_DATABASE_SCHEMA,
    _normalized_slack,
    _validate_database_normalization,
    project_sta_path_database,
)
from .tdm import validate_tdm_schedule
from .tdm_ratio import (
    TDM_TIMING_DAG_RATIO_PROVIDER,
    validate_tdm_ratio_plan,
)
from .timing_routing import GLOBAL_CANDIDATE_PROVIDER


CROSS_STAGE_CANDIDATE_SCHEMA = "emuflow.cross-stage-candidate/v2"
CROSS_STAGE_REPORT_SCHEMA = "emuflow.cross-stage-report/v2"
CROSS_STAGE_PROVIDER = "throughput-first-tdm-feedback-line-search-v2"
DEFAULT_FEEDBACK_STEPS = (1.0, 0.5, 0.25, 0.125)
CROSS_STAGE_OBJECTIVE = (
    "lexicographic(minimum feasible frame slots, maximum virtual-clock "
    "timing margin, all-path worst normalized original-clock slack, "
    "all-path total negative normalized original-clock slack, negative "
    "path count, maximum TDM ratio, completion slot, link bit-hops, cut "
    "bits, replica LUTs)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_feedback_steps(
    values: Optional[Tuple[float, ...]],
) -> Tuple[float, ...]:
    raw = DEFAULT_FEEDBACK_STEPS if values is None else values
    if not isinstance(raw, tuple) or not raw:
        raise ValidationError(
            "cross-stage feedback steps must be a non-empty tuple"
        )
    steps = []
    for index, value in enumerate(raw):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
            or float(value) > 1.0
        ):
            raise ValidationError(
                f"cross-stage feedback step {index} must be in (0, 1]"
            )
        steps.append(float(value))
    if any(
        left <= right
        for left, right in zip(steps, steps[1:])
    ):
        raise ValidationError(
            "cross-stage feedback steps must be strictly decreasing"
        )
    return tuple(steps)


def _scheduled_transport_delay_by_net(
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
) -> Dict[str, float]:
    link_by_id = {link.id: link for link in platform.links}
    entries = {}
    for index, entry in enumerate(schedule.get("entries", [])):
        if not isinstance(entry, dict):
            raise ValidationError(f"schedule.entries[{index}] is invalid")
        key = (
            entry.get("demand"),
            entry.get("link"),
            entry.get("from"),
            entry.get("to"),
        )
        if key in entries:
            raise ValidationError(f"schedule has duplicate routed hop {key}")
        entries[key] = entry

    constraints = routes.get("constraints")
    if not isinstance(constraints, dict):
        raise ValidationError("routes.constraints is invalid")
    delay_by_net = {}
    for route in routes.get("routes", []):
        graph = defaultdict(list)
        for edge in route["tree_edges"]:
            graph[edge["from"]].append(edge)
        arrival = {route["source"]: 0.0}
        queue = deque([route["source"]])
        while queue:
            node = queue.popleft()
            for edge in sorted(
                graph[node],
                key=lambda item: (
                    item["to"],
                    item["link"],
                ),
            ):
                key = (
                    route["id"],
                    edge["link"],
                    edge["from"],
                    edge["to"],
                )
                entry = entries.get(key)
                if entry is None:
                    raise ValidationError(
                        f"schedule is missing routed hop {key}"
                    )
                wait_slots = entry.get("slot", -1) - entry.get(
                    "ready_slot", 0
                )
                if wait_slots < 0:
                    raise ValidationError(
                        f"schedule routed hop {key} has negative wait"
                    )
                link = link_by_id[edge["link"]]
                edge_delay = route_link_delay_ns(
                    platform,
                    edge["link"],
                    edge["from"],
                    edge["to"],
                    constraints,
                )
                edge_delay += (
                    1000.0 / link.fabric_clock_mhz
                ) * wait_slots
                arrival[edge["to"]] = arrival[node] + edge_delay
                queue.append(edge["to"])
        delay_by_net[route["net"]] = max(
            arrival[sink] for sink in route["sinks"]
        )
    return delay_by_net


def _path_metrics(
    database: Mapping[str, Any],
    assignment: Mapping[str, Any],
    transport_delay_by_net: Mapping[str, float],
) -> Dict[str, Any]:
    normalization = _validate_database_normalization(
        database.get("normalization")
    )
    cut_nets = {
        cut["net"]
        for cut in assignment.get("cut_nets", [])
        if isinstance(cut, dict) and isinstance(cut.get("net"), str)
    }
    if cut_nets != set(transport_delay_by_net):
        missing = sorted(cut_nets - set(transport_delay_by_net))
        extra = sorted(set(transport_delay_by_net) - cut_nets)
        raise ValidationError(
            "candidate route/cut coverage mismatch: "
            f"missing={missing[:8]} extra={extra[:8]}"
        )

    raw_paths = database.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("STA path database paths are invalid")
    records = []
    path_ids = set()
    for index, path in enumerate(raw_paths):
        if not isinstance(path, dict):
            raise ValidationError(
                f"STA path database paths[{index}] is invalid"
            )
        path_id = path.get("id")
        period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        fixed_delay = path.get("fixed_delay_ns")
        normalized = path.get("normalized_slack")
        if (
            not isinstance(path_id, str)
            or not path_id
            or path_id in path_ids
        ):
            raise ValidationError(
                f"STA path database paths[{index}].id is invalid"
            )
        path_ids.add(path_id)
        for name, value in (
            ("clock_period_ns", period),
            ("slack_ns", slack),
            ("fixed_delay_ns", fixed_delay),
            ("normalized_slack", normalized),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationError(
                    f"STA path database paths[{index}].{name} is invalid"
                )
        if float(period) <= 0.0 or float(fixed_delay) < 0.0:
            raise ValidationError(
                f"STA path database paths[{index}] timing is invalid"
            )
        expected_normalized = _normalized_slack(
            float(period), float(slack), normalization
        )
        if abs(float(normalized) - expected_normalized) > 1.0e-12:
            raise ValidationError(
                f"STA path database paths[{index}].normalized_slack "
                "is inconsistent"
            )
        path_nets = path.get("path_nets")
        if (
            not isinstance(path_nets, list)
            or not path_nets
            or not all(isinstance(net, str) and net for net in path_nets)
            or len(path_nets) != len(set(path_nets))
        ):
            raise ValidationError(
                f"STA path database paths[{index}].path_nets is invalid"
            )
        crossed = [net for net in path_nets if net in cut_nets]
        delay = float(fixed_delay) + sum(
            transport_delay_by_net[net] for net in crossed
        )
        realized_slack = float(period) - delay
        realized_normalized = _normalized_slack(
            float(period),
            realized_slack,
            normalization,
        )
        records.append(
            {
                "path": path_id,
                "crossed_cut_nets": len(crossed),
                "delay_ns": delay,
                "slack_ns": realized_slack,
                "normalized_slack": realized_normalized,
            }
        )
    ordered = sorted(
        records,
        key=lambda item: (item["normalized_slack"], item["path"]),
    )
    normalized_values = sorted(
        record["normalized_slack"] for record in records
    )
    negative = [
        record for record in records if record["slack_ns"] < 0.0
    ]
    worst = ordered[0]
    slowest = max(
        records, key=lambda item: (item["delay_ns"], item["path"])
    )
    return {
        "all_paths": len(records),
        "crossing_paths": sum(
            record["crossed_cut_nets"] > 0 for record in records
        ),
        "no_cut_paths": sum(
            record["crossed_cut_nets"] == 0 for record in records
        ),
        "negative_slack_paths": len(negative),
        "worst_path": worst["path"],
        "worst_delay_ns": worst["delay_ns"],
        "worst_slack_ns": worst["slack_ns"],
        "worst_normalized_slack": worst["normalized_slack"],
        "maximum_delay_path": slowest["path"],
        "maximum_delay_ns": slowest["delay_ns"],
        "total_negative_normalized_slack": sum(
            record["normalized_slack"] for record in negative
        ),
        "p01_normalized_slack": normalized_values[
            len(normalized_values) // 100
        ],
        "median_normalized_slack": normalized_values[
            len(normalized_values) // 2
        ],
    }


def _objective_metrics(
    path_metrics: Mapping[str, Any],
    assignment: Mapping[str, Any],
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> Dict[str, Any]:
    route_metrics = routes.get("metrics", {})
    schedule_metrics = schedule.get("metrics", {})
    ratios = [
        entry.get("tdm_ratio", 1)
        for entry in schedule.get("entries", [])
    ]
    replication = assignment.get("replication", {})
    replication_metrics = (
        replication.get("metrics", {})
        if isinstance(replication, dict)
        else {}
    )
    replica_luts = replication_metrics.get("replica_luts", 0)
    if (
        isinstance(replica_luts, bool)
        or not isinstance(replica_luts, int)
        or replica_luts < 0
    ):
        raise ValidationError("assignment replica LUT count is invalid")
    virtual_period = runtime["virtual_dut_clock"]["nominal_period_ns"]
    runtime_slack = virtual_period - path_metrics["maximum_delay_ns"]
    return {
        "frame_slots": runtime["frame"]["slots"],
        "nominal_virtual_frequency_mhz": runtime["virtual_dut_clock"][
            "nominal_frequency_mhz"
        ],
        "estimated_runtime_slack_ns": runtime_slack,
        "estimated_runtime_closed": runtime_slack >= 0.0,
        "worst_normalized_slack": path_metrics[
            "worst_normalized_slack"
        ],
        "total_negative_normalized_slack": path_metrics[
            "total_negative_normalized_slack"
        ],
        "negative_slack_paths": path_metrics["negative_slack_paths"],
        "max_tdm_ratio": max(ratios, default=1),
        "completion_slot": schedule_metrics["completion_slot"],
        "total_link_bit_hops": route_metrics["total_link_bit_hops"],
        "cut_bits": sum(
            int(route["width_bits"]) for route in routes["routes"]
        ),
        "replica_luts": replica_luts,
    }


def _objective_key(metrics: Mapping[str, Any]) -> Tuple[float, ...]:
    return (
        float(metrics["frame_slots"]),
        -float(metrics["estimated_runtime_slack_ns"]),
        -float(metrics["worst_normalized_slack"]),
        -float(metrics["total_negative_normalized_slack"]),
        float(metrics["negative_slack_paths"]),
        float(metrics["max_tdm_ratio"]),
        float(metrics["completion_slot"]),
        float(metrics["total_link_bit_hops"]),
        float(metrics["cut_bits"]),
        float(metrics["replica_luts"]),
    )


def compare_candidate_objectives(
    candidate: Mapping[str, Any],
    incumbent: Mapping[str, Any],
    *,
    tolerance: float = 1.0e-12,
) -> Dict[str, Any]:
    candidate_key = _objective_key(candidate["objective_metrics"])
    incumbent_key = _objective_key(incumbent["objective_metrics"])
    for index, (new, old) in enumerate(zip(candidate_key, incumbent_key)):
        if new < old - tolerance:
            return {
                "accepted": True,
                "deciding_level": index,
                "candidate_key": list(candidate_key),
                "incumbent_key": list(incumbent_key),
            }
        if new > old + tolerance:
            return {
                "accepted": False,
                "deciding_level": index,
                "candidate_key": list(candidate_key),
                "incumbent_key": list(incumbent_key),
            }
    return {
        "accepted": False,
        "deciding_level": None,
        "candidate_key": list(candidate_key),
        "incumbent_key": list(incumbent_key),
    }


def reconstruct_partition_migration(
    incumbent_assignment: Mapping[str, Any],
    candidate_assignment: Mapping[str, Any],
    platform: Platform,
    route_constraints: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    incumbent = incumbent_assignment.get("cluster_assignment")
    candidate = candidate_assignment.get("cluster_assignment")
    if not isinstance(incumbent, dict) or not isinstance(candidate, dict):
        raise ValidationError(
            "cross-stage partition migration assignments are invalid"
        )
    if set(incumbent) != set(candidate) or not incumbent:
        raise ValidationError(
            "cross-stage partition migration coverage mismatch"
        )
    fpga_ids = tuple(sorted(fpga.id for fpga in platform.fpgas))
    owners = set(incumbent.values()) | set(candidate.values())
    if not owners.issubset(fpga_ids):
        raise ValidationError(
            "cross-stage partition migration owner is not on the platform"
        )
    pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    for cluster in sorted(incumbent):
        source = incumbent[cluster]
        target = candidate[cluster]
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(target, str)
            or not target
        ):
            raise ValidationError(
                "cross-stage partition migration owner is invalid"
            )
        if source != target:
            pair_counts[(source, target)] += 1
    moved = sum(pair_counts.values())
    total = len(incumbent)
    raw = {
        "clusters": total,
        "moved_clusters": moved,
        "moved_fraction": moved / total,
        "moves": [
            {
                "from": source,
                "to": target,
                "clusters": count,
            }
            for (source, target), count in sorted(pair_counts.items())
        ],
    }
    automorphisms = _platform_automorphisms(
        platform, route_constraints
    )
    best_mapping = max(
        automorphisms,
        key=lambda mapping: (
            sum(
                mapping[incumbent[cluster]] == candidate[cluster]
                for cluster in incumbent
            ),
            tuple(mapping[fpga] for fpga in fpga_ids),
        ),
    )
    aligned_pairs: Dict[Tuple[str, str], int] = defaultdict(int)
    for cluster in sorted(incumbent):
        aligned_source = best_mapping[incumbent[cluster]]
        target = candidate[cluster]
        if aligned_source != target:
            aligned_pairs[(aligned_source, target)] += 1
    aligned_moved = sum(aligned_pairs.values())
    if route_constraints is None:
        alignment_qualification = (
            "identity-only-external-route-constraints-unavailable"
        )
    elif len(fpga_ids) > 8:
        alignment_qualification = "identity-only-enumeration-bound"
    else:
        alignment_qualification = "boarddb-and-route-constraints"
    raw["symmetry_alignment"] = {
        "method": "maximum-overlap-exact-platform-automorphism",
        "qualification": alignment_qualification,
        "valid_automorphisms": len(automorphisms),
        "mapping": dict(sorted(best_mapping.items())),
        "moved_clusters": aligned_moved,
        "moved_fraction": aligned_moved / total,
        "moves": [
            {
                "from": source,
                "to": target,
                "clusters": count,
            }
            for (source, target), count in sorted(aligned_pairs.items())
        ],
    }
    return raw


def _platform_automorphisms(
    platform: Platform,
    route_constraints: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, str], ...]:
    """Enumerate exact FPGA-label symmetries relevant to system routing."""
    fpga_ids = tuple(sorted(fpga.id for fpga in platform.fpgas))
    identity = {fpga: fpga for fpga in fpga_ids}
    if route_constraints is None or len(fpga_ids) > 8:
        return (identity,)

    nodes = {fpga.id: fpga for fpga in platform.fpgas}

    def node_signature(fpga_id: str) -> Tuple[Any, ...]:
        fpga = nodes[fpga_id]
        return (
            fpga.part,
            fpga.utilization_limit,
            tuple(sorted(fpga.capacity.items())),
        )

    def link_signature(link: Any, mapping: Mapping[str, str]) -> Tuple[Any, ...]:
        left, right = link.endpoints
        bindings = tuple(
            sorted(
                (
                    mapping[binding.fpga],
                    binding.connector,
                    binding.mgt,
                    tuple(
                        (
                            lane.lane,
                            lane.tx_package_pin_p,
                            lane.tx_package_pin_n,
                            lane.rx_package_pin_p,
                            lane.rx_package_pin_n,
                        )
                        for lane in binding.lanes
                    ),
                )
                for binding in link.endpoint_bindings
            )
        )
        reverse_delay = None
        if link.direction in {"full_duplex", "half_duplex"}:
            reverse_delay = route_link_delay_ns(
                platform, link.id, right, left, route_constraints
            )
        return (
            mapping[left],
            mapping[right],
            link.direction,
            link.mode,
            link.data_lanes_per_direction,
            link.fabric_clock_mhz,
            link.latency_cycles,
            link.capacity_sharing,
            link.payload_bits_per_lane_per_cycle,
            link.max_line_rate_gbps_per_lane,
            bindings,
            link.id in set(route_constraints.get("unavailable_links", [])),
            link.id in set(route_constraints.get("sll_links", [])),
            link.id in set(
                route_constraints.get("shared_capacity_links", [])
            ),
            route_link_delay_ns(
                platform, link.id, left, right, route_constraints
            ),
            reverse_delay,
        )

    target_links = sorted(
        link_signature(link, identity) for link in platform.links
    )
    result = []
    for values in itertools.permutations(fpga_ids):
        mapping = dict(zip(fpga_ids, values))
        if any(
            node_signature(source) != node_signature(target)
            for source, target in mapping.items()
        ):
            continue
        transformed_links = sorted(
            link_signature(link, mapping) for link in platform.links
        )
        if transformed_links == target_links:
            result.append(mapping)
    if not result:
        raise ValidationError("platform identity is not an automorphism")
    return tuple(result)


def reconstruct_partition_class(
    assignment: Mapping[str, Any],
    platform: Platform,
    route_constraints: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build a deterministic partition identity modulo exact board symmetry."""
    cluster_assignment = assignment.get("cluster_assignment")
    if not isinstance(cluster_assignment, dict) or not cluster_assignment:
        raise ValidationError(
            "cross-stage partition class assignment is invalid"
        )
    fpga_ids = tuple(sorted(fpga.id for fpga in platform.fpgas))
    if not set(cluster_assignment.values()).issubset(fpga_ids):
        raise ValidationError(
            "cross-stage partition class owner is not on the platform"
        )
    automorphisms = _platform_automorphisms(platform, route_constraints)
    canonical = min(
        tuple(
            (cluster, mapping[cluster_assignment[cluster]])
            for cluster in sorted(cluster_assignment)
        )
        for mapping in automorphisms
    )
    signature = hashlib.sha256(
        "\n".join(
            f"{len(cluster)}:{cluster}:{len(owner)}:{owner}"
            for cluster, owner in canonical
        ).encode("utf-8")
    ).hexdigest()
    if route_constraints is None:
        qualification = (
            "identity-only-external-route-constraints-unavailable"
        )
    elif len(fpga_ids) > 8:
        qualification = "identity-only-enumeration-bound"
    else:
        qualification = "boarddb-and-route-constraints"
    return {
        "method": "lexicographic-exact-platform-automorphism",
        "qualification": qualification,
        "valid_automorphisms": len(automorphisms),
        "clusters": len(canonical),
        "sha256": signature,
    }


def build_cross_stage_candidate(
    database: Mapping[str, Any],
    assignment: Mapping[str, Any],
    routes: Mapping[str, Any],
    schedule: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    platform: Platform,
    *,
    source_hashes: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    if database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("cross-stage candidate STA database is invalid")
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError("cross-stage candidate assignment is invalid")
    design = database.get("design")
    if (
        design != assignment.get("design")
        or design != routes.get("design")
        or design != schedule.get("design")
    ):
        raise ValidationError("cross-stage candidate design mismatch")
    validate_system_routes(assignment, platform, routes)
    validate_tdm_ratio_plan(routes, platform, ratio_plan)
    validate_tdm_schedule(routes, platform, schedule, ratio_plan)
    runtime = build_virtual_runtime(schedule, platform)
    runtime_validation = validate_virtual_runtime(runtime, schedule, platform)
    transport_delay = _scheduled_transport_delay_by_net(
        routes, schedule, platform
    )
    paths = _path_metrics(database, assignment, transport_delay)
    objective_metrics = _objective_metrics(
        paths, assignment, routes, schedule, runtime
    )
    if not objective_metrics["estimated_runtime_closed"]:
        raise ValidationError(
            "cross-stage candidate scheduled path delay exceeds its "
            "virtual clock period"
        )
    hashes = dict(source_hashes or {})
    candidate_id = hashlib.sha256(
        "\n".join(
            f"{key}:{hashes[key]}" for key in sorted(hashes)
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": CROSS_STAGE_CANDIDATE_SCHEMA,
        "status": "pass",
        "design": design,
        "platform": platform.name,
        "candidate_id": candidate_id,
        "source_sha256": hashes,
        "path_metrics": paths,
        "runtime": {
            "provider": runtime["provider"],
            "validation": runtime_validation,
            "estimated_timing": {
                "qualification": (
                    "academic-preplacement-fixed-delay-plus-concrete-"
                    "tdm-schedule"
                ),
                "maximum_delay_path": paths["maximum_delay_path"],
                "maximum_delay_ns": paths["maximum_delay_ns"],
                "virtual_period_ns": runtime["virtual_dut_clock"][
                    "nominal_period_ns"
                ],
                "estimated_slack_ns": objective_metrics[
                    "estimated_runtime_slack_ns"
                ],
                "estimated_closed": objective_metrics[
                    "estimated_runtime_closed"
                ],
            },
        },
        "objective": CROSS_STAGE_OBJECTIVE,
        "objective_metrics": objective_metrics,
        "objective_key": list(_objective_key(objective_metrics)),
    }


def evaluate_cross_stage_candidate(
    database_path: Path,
    assignment_path: Path,
    routes_path: Path,
    schedule_path: Path,
    ratio_plan_path: Path,
    platform_path: Path,
    output_path: Path,
) -> Dict[str, Any]:
    paths = {
        "database": database_path,
        "assignment": assignment_path,
        "routes": routes_path,
        "schedule": schedule_path,
        "ratio_plan": ratio_plan_path,
        "platform": platform_path,
    }
    candidate = build_cross_stage_candidate(
        read_json(database_path),
        read_json(assignment_path),
        read_json(routes_path),
        read_json(schedule_path),
        read_json(ratio_plan_path),
        Platform.load(platform_path),
        source_hashes={key: _sha256(path) for key, path in paths.items()},
    )
    write_json(output_path, candidate)
    return candidate


def validate_cross_stage_candidate(
    candidate_path: Path,
    database_path: Path,
    assignment_path: Path,
    routes_path: Path,
    schedule_path: Path,
    ratio_plan_path: Path,
    platform_path: Path,
) -> Dict[str, Any]:
    paths = {
        "database": database_path,
        "assignment": assignment_path,
        "routes": routes_path,
        "schedule": schedule_path,
        "ratio_plan": ratio_plan_path,
        "platform": platform_path,
    }
    expected = build_cross_stage_candidate(
        read_json(database_path),
        read_json(assignment_path),
        read_json(routes_path),
        read_json(schedule_path),
        read_json(ratio_plan_path),
        Platform.load(platform_path),
        source_hashes={key: _sha256(path) for key, path in paths.items()},
    )
    actual = read_json(candidate_path)
    if actual != expected:
        raise ValidationError(
            "cross-stage candidate does not match independent reconstruction"
        )
    return {
        "status": "pass",
        "candidate_id": actual["candidate_id"],
        "objective_key": actual["objective_key"],
        "all_paths": actual["path_metrics"]["all_paths"],
    }


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _validated_report_artifact(
    root: Path, record: Any, label: str
) -> Path:
    if not isinstance(record, dict):
        raise ValidationError(f"cross-stage {label} artifact is invalid")
    relative = record.get("path")
    digest = record.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise ValidationError(f"cross-stage {label} artifact is invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise ValidationError(
            f"cross-stage {label} artifact escapes the report root"
        ) from error
    if not path.is_file() or _sha256(path) != digest:
        raise ValidationError(
            f"cross-stage {label} artifact hash mismatch"
        )
    return path


def _run_candidate_flow(
    *,
    root: Path,
    iteration: int,
    assignment_path: Path,
    database_path: Path,
    platform_path: Path,
    route_constraints_path: Optional[Path],
    frame_slots: Optional[int],
    optimize_frame_slots: bool,
    route_max_iterations: Optional[int],
    router: Optional[str],
    route_provider: Optional[str],
    route_candidate_workers: int,
    simulation_frames: int,
    tdm_provider: Optional[str],
    ratio_optimizer: Optional[str],
    timing_dag_optimizer: Optional[str],
    slot_optimizer: Optional[str],
    ratio_max_iterations: int,
    max_ratio: Optional[int],
    ratio_quantum: int,
    post_refinement_iterations: int,
    slot_refinement_iterations: int,
    ratio_convergence: float,
) -> Dict[str, Any]:
    iteration_root = root / f"iteration_{iteration:03d}"
    timing_path = iteration_root / "timing_paths.json"
    phase4_root = iteration_root / "phase4"
    phase5_root = iteration_root / "phase5"
    score_path = iteration_root / "candidate.json"
    projection = project_sta_path_database(
        database_path, assignment_path, timing_path
    )
    frame_search = None
    if optimize_frame_slots:
        if frame_slots is None:
            raise ValidationError(
                "cross-stage frame optimization requires a feasible upper "
                "bound"
            )
        frame_search = run_frame_length_search(
            assignment_path,
            platform_path,
            timing_path,
            iteration_root / "frame-search",
            phase4_root,
            phase5_root,
            max_frame_slots=frame_slots,
            route_constraints=route_constraints_path,
            route_max_iterations=route_max_iterations,
            router=router,
            route_provider=route_provider,
            candidate_workers=route_candidate_workers,
            tdm_provider=tdm_provider,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            simulation_frames=simulation_frames,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            ratio_convergence=ratio_convergence,
        )
        phase4 = read_json(phase4_root / "phase4_report.json")
        phase5 = read_json(phase5_root / "phase5_report.json")
    else:
        phase4 = run_phase4(
            assignment_path,
            platform_path,
            phase4_root,
            constraints_path=route_constraints_path,
            frame_slots=frame_slots,
            max_iterations=route_max_iterations,
            provider=route_provider or GLOBAL_CANDIDATE_PROVIDER,
            timing_paths_path=timing_path,
            router=router,
            candidate_workers=route_candidate_workers,
        )
        phase5 = run_phase5(
            phase4_root / "routes.json",
            platform_path,
            phase5_root,
            simulation_frames=simulation_frames,
            provider=tdm_provider or TDM_TIMING_DAG_RATIO_PROVIDER,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            convergence=ratio_convergence,
        )
    score = evaluate_cross_stage_candidate(
        database_path,
        assignment_path,
        phase4_root / "routes.json",
        phase5_root / "schedule.json",
        phase5_root / "ratio_plan.json",
        platform_path,
        score_path,
    )
    result = {
        "iteration": iteration,
        "status": "pass",
        "assignment": _relative(assignment_path, root),
        "timing_paths": _relative(timing_path, root),
        "routes": _relative(phase4_root / "routes.json", root),
        "schedule": _relative(phase5_root / "schedule.json", root),
        "ratio_plan": _relative(phase5_root / "ratio_plan.json", root),
        "score": _relative(score_path, root),
        "projection": projection,
        "phase4_validation": phase4["validation"],
        "phase5_validation": phase5["validation"],
        "objective_metrics": score["objective_metrics"],
        "objective_key": score["objective_key"],
        "candidate_id": score["candidate_id"],
    }
    if frame_search is not None:
        result["frame_search"] = _relative(
            iteration_root / "frame-search/frame-search-report.json", root
        )
        result["frame_search_validation"] = (
            validate_frame_search_report(frame_search)
        )
    return result


def run_cross_stage_optimization(
    *,
    ir_path: Path,
    platform_path: Path,
    database_path: Path,
    initial_assignment_path: Path,
    output_dir: Path,
    seed_candidate_phase3_root: Optional[Path] = None,
    phase3_constraints_path: Optional[Path] = None,
    route_constraints_path: Optional[Path] = None,
    board_link_timing_path: Optional[Path] = None,
    phase3_provider: str = "repart-replication",
    max_outer_iterations: int = 1,
    seed: int = 0,
    min_used_fpgas: Optional[int] = None,
    balance_tolerance: Optional[float] = None,
    openroad: Optional[str] = None,
    repart: Optional[str] = None,
    patron_refiner: Optional[str] = None,
    patron_max_moves: Optional[int] = None,
    patron_flow_refinement: bool = False,
    partition_timeout_seconds: int = 3600,
    partition_seed_attempts: int = 1,
    partition_num_initial_solutions: int = 50,
    partition_num_best_initial_solutions: int = 10,
    partition_repair_min_used_fpgas: bool = False,
    partition_repair_balance: bool = False,
    router: Optional[str] = None,
    route_provider: Optional[str] = None,
    route_candidate_workers: int = 1,
    frame_slots: Optional[int] = None,
    optimize_frame_slots: bool = False,
    route_max_iterations: Optional[int] = None,
    tdm_provider: Optional[str] = None,
    ratio_optimizer: Optional[str] = None,
    timing_dag_optimizer: Optional[str] = None,
    slot_optimizer: Optional[str] = None,
    feedback_optimizer: Optional[str] = None,
    simulation_frames: int = 4,
    ratio_max_iterations: int = 500,
    max_ratio: Optional[int] = None,
    ratio_quantum: int = 8,
    post_refinement_iterations: int = 200,
    slot_refinement_iterations: int = 0,
    ratio_convergence: float = 1.0e-9,
    pair_pressure_weight: float = 1.0,
    feedback_steps: Optional[Tuple[float, ...]] = None,
) -> Dict[str, Any]:
    if (
        isinstance(max_outer_iterations, bool)
        or not isinstance(max_outer_iterations, int)
        or max_outer_iterations < 0
    ):
        raise ValidationError(
            "cross-stage max outer iterations must be non-negative"
        )
    if (
        isinstance(partition_seed_attempts, bool)
        or not isinstance(partition_seed_attempts, int)
        or partition_seed_attempts <= 0
    ):
        raise ValidationError(
            "cross-stage partition seed attempts must be positive"
        )
    if (
        isinstance(partition_num_initial_solutions, bool)
        or not isinstance(partition_num_initial_solutions, int)
        or partition_num_initial_solutions <= 0
        or isinstance(partition_num_best_initial_solutions, bool)
        or not isinstance(partition_num_best_initial_solutions, int)
        or partition_num_best_initial_solutions <= 0
        or partition_num_best_initial_solutions
        > partition_num_initial_solutions
    ):
        raise ValidationError(
            "cross-stage TritonPart search effort is invalid"
        )
    if not isinstance(partition_repair_min_used_fpgas, bool) or not isinstance(
        partition_repair_balance, bool
    ):
        raise ValidationError(
            "cross-stage partition repair flags must be booleans"
        )
    steps = _normalize_feedback_steps(feedback_steps)
    if optimize_frame_slots and frame_slots is None:
        raise ValidationError(
            "cross-stage --optimize-frame-slots requires --frame-slots"
        )
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValidationError(
            f"cross-stage output directory is not empty: {output_dir}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    database = read_json(database_path)
    if database.get("schema") != STA_PATH_DATABASE_SCHEMA:
        raise ValidationError("cross-stage STA path database is invalid")
    if database.get("design") != ir.value["design"]["name"]:
        raise ValidationError(
            "cross-stage STA path database design does not match EmuIR"
        )
    partition_constraints = load_partition_constraints(
        phase3_constraints_path,
        ir,
        platform,
        min_used_fpgas=min_used_fpgas,
        balance_tolerance=balance_tolerance,
    )
    clusters = build_clusters(ir, partition_constraints)
    initial_assignment = read_json(initial_assignment_path)
    initial_validation = validate_partition_artifacts(
        ir, platform, clusters, initial_assignment
    )
    initial_root = output_dir / "iteration_000" / "phase3"
    initial_root.mkdir(parents=True, exist_ok=True)
    assignment_path = initial_root / "assignment.json"
    write_json(assignment_path, initial_assignment)
    write_json(initial_root / "clusters.json", clusters)
    write_json(
        initial_root / "constraints.normalized.json",
        partition_constraints,
    )

    effective_route_constraints_path = route_constraints_path
    board_link_timing_report = None
    if board_link_timing_path is not None:
        link_timing = read_json(board_link_timing_path.resolve())
        link_validation = validate_board_link_timing(
            link_timing, platform
        )
        directed_delays, projection = directed_route_link_delays(
            link_timing, platform
        )
        inputs_root = output_dir / "inputs"
        inputs_root.mkdir(parents=True, exist_ok=True)
        copied_timing_path = inputs_root / "board-link-timing.json"
        write_json(copied_timing_path, link_timing)
        raw_route_constraints = (
            read_json(route_constraints_path.resolve())
            if route_constraints_path is not None
            else {"schema": SYSTEM_ROUTE_CONSTRAINTS_SCHEMA}
        )
        if not isinstance(raw_route_constraints, dict):
            raise ValidationError("route constraints must be an object")
        effective_constraints = dict(raw_route_constraints)
        effective_constraints["directed_link_delay_ns"] = (
            directed_delays
        )
        effective_route_constraints_path = (
            inputs_root / "board-link-route-constraints.json"
        )
        write_json(
            effective_route_constraints_path, effective_constraints
        )
        board_link_timing_report = {
            "status": "pass",
            "validation": link_validation,
            "routing_projection": projection,
            "applied_to": [
                "every-phase4-candidate",
                "every-phase5-candidate",
                "cross-stage-objective-and-feedback",
            ],
            "artifacts": {
                "database": {
                    "path": _relative(copied_timing_path, output_dir),
                    "sha256": _sha256(copied_timing_path),
                },
                "effective_route_constraints": {
                    "path": _relative(
                        effective_route_constraints_path, output_dir
                    ),
                    "sha256": _sha256(
                        effective_route_constraints_path
                    ),
                },
            },
        }
    migration_route_constraints = (
        None
        if route_constraints_path is not None
        and board_link_timing_path is None
        else load_route_constraints(
            effective_route_constraints_path,
            platform,
            frame_slots=frame_slots,
            max_iterations=route_max_iterations,
        )
    )

    candidates = []
    baseline = _run_candidate_flow(
        root=output_dir,
        iteration=0,
        assignment_path=assignment_path,
        database_path=database_path,
        platform_path=platform_path,
        route_constraints_path=effective_route_constraints_path,
        frame_slots=frame_slots,
        optimize_frame_slots=optimize_frame_slots,
        route_max_iterations=route_max_iterations,
        router=router,
        route_provider=route_provider,
        route_candidate_workers=route_candidate_workers,
        simulation_frames=simulation_frames,
        tdm_provider=tdm_provider,
        ratio_optimizer=ratio_optimizer,
        timing_dag_optimizer=timing_dag_optimizer,
        slot_optimizer=slot_optimizer,
        ratio_max_iterations=ratio_max_iterations,
        max_ratio=max_ratio,
        ratio_quantum=ratio_quantum,
        post_refinement_iterations=post_refinement_iterations,
        slot_refinement_iterations=slot_refinement_iterations,
        ratio_convergence=ratio_convergence,
    )
    baseline["decision"] = {
        "accepted": True,
        "reason": "initial incumbent",
    }
    baseline["phase3_validation"] = initial_validation
    baseline["partition_class"] = reconstruct_partition_class(
        initial_assignment,
        platform,
        migration_route_constraints,
    )
    candidates.append(baseline)
    evaluated_partition_classes = {
        baseline["partition_class"]["sha256"]: 0
    }
    incumbent_index = 0
    termination = "iteration-limit"

    if seed_candidate_phase3_root is not None:
        source_phase3_root = seed_candidate_phase3_root.resolve()
        seed_assignment_path = source_phase3_root / "assignment.json"
        seed_clusters_path = source_phase3_root / "clusters.json"
        seed_constraints_path = (
            source_phase3_root / "constraints.normalized.json"
        )
        for path in (
            seed_assignment_path,
            seed_clusters_path,
            seed_constraints_path,
        ):
            if not path.is_file():
                raise ValidationError(
                    f"cross-stage seed candidate artifact is missing: {path}"
                )
        seed_assignment = read_json(seed_assignment_path)
        seed_clusters = read_json(seed_clusters_path)
        seed_validation = validate_partition_artifacts(
            ir, platform, seed_clusters, seed_assignment
        )
        if seed_clusters != clusters:
            raise ValidationError(
                "cross-stage seed candidate clusters disagree with inputs"
            )
        if read_json(seed_constraints_path) != partition_constraints:
            raise ValidationError(
                "cross-stage seed candidate constraints disagree with inputs"
            )
        iteration = len(candidates)
        seed_root = output_dir / f"iteration_{iteration:03d}"
        shutil.copytree(source_phase3_root, seed_root / "phase3")
        candidate = _run_candidate_flow(
            root=output_dir,
            iteration=iteration,
            assignment_path=seed_root / "phase3/assignment.json",
            database_path=database_path,
            platform_path=platform_path,
            route_constraints_path=effective_route_constraints_path,
            frame_slots=frame_slots,
            optimize_frame_slots=optimize_frame_slots,
            route_max_iterations=route_max_iterations,
            router=router,
            route_provider=route_provider,
            route_candidate_workers=route_candidate_workers,
            simulation_frames=simulation_frames,
            tdm_provider=tdm_provider,
            ratio_optimizer=ratio_optimizer,
            timing_dag_optimizer=timing_dag_optimizer,
            slot_optimizer=slot_optimizer,
            ratio_max_iterations=ratio_max_iterations,
            max_ratio=max_ratio,
            ratio_quantum=ratio_quantum,
            post_refinement_iterations=post_refinement_iterations,
            slot_refinement_iterations=slot_refinement_iterations,
            ratio_convergence=ratio_convergence,
        )
        candidate.update(
            {
                "candidate_origin": "seed",
                "outer_iteration": 0,
                "trial": None,
                "phase3_validation": seed_validation,
                "partition_migration": reconstruct_partition_migration(
                    initial_assignment,
                    seed_assignment,
                    platform,
                    migration_route_constraints,
                ),
                "partition_class": reconstruct_partition_class(
                    seed_assignment,
                    platform,
                    migration_route_constraints,
                ),
            }
        )
        partition_signature = candidate["partition_class"]["sha256"]
        equivalent_iteration = evaluated_partition_classes.get(
            partition_signature
        )
        if equivalent_iteration is not None:
            candidate["equivalent_partition_iteration"] = (
                equivalent_iteration
            )
        else:
            evaluated_partition_classes[partition_signature] = iteration
        decision = compare_candidate_objectives(
            read_json(output_dir / candidate["score"]),
            read_json(output_dir / baseline["score"]),
        )
        candidate["decision"] = decision
        candidates.append(candidate)
        if decision["accepted"]:
            incumbent_index = iteration

    for outer_iteration in range(1, max_outer_iterations + 1):
        incumbent = candidates[incumbent_index]
        incumbent_root = output_dir / f"iteration_{incumbent_index:03d}"
        outer_root = output_dir / f"outer_{outer_iteration:03d}"
        outer_root.mkdir(parents=True, exist_ok=True)
        raw_feedback_path = outer_root / "raw_partition_feedback.json"
        try:
            raw_feedback_validation = run_partition_feedback(
                incumbent_root / "phase4" / "routes.json",
                incumbent_root / "phase5" / "ratio_plan.json",
                platform_path,
                raw_feedback_path,
                executable=feedback_optimizer,
                pair_pressure_weight=pair_pressure_weight,
            )
        except (EmuFlowError, ValidationError, ValueError) as error:
            candidates.append(
                {
                    "iteration": len(candidates),
                    "outer_iteration": outer_iteration,
                    "trial": None,
                    "status": "rejected",
                    "decision": {
                        "accepted": False,
                        "reason": str(error),
                    },
                }
            )
            termination = "feedback-generation-failed"
            break
        accepted_step = False
        feasible_trials = 0
        equivalent_trials = 0
        for trial, step_size in enumerate(steps):
            iteration = len(candidates)
            iteration_root = output_dir / f"iteration_{iteration:03d}"
            iteration_root.mkdir(parents=True, exist_ok=True)
            feedback_path = iteration_root / "partition_feedback.json"
            try:
                feedback_validation = run_damped_partition_feedback(
                    raw_feedback_path,
                    feedback_path,
                    step_size=step_size,
                )
                phase3_root = iteration_root / "phase3"
                phase3_report = run_phase3(
                    ir_path,
                    platform_path,
                    phase3_root,
                    constraints_path=phase3_constraints_path,
                    seed=seed,
                    min_used_fpgas=min_used_fpgas,
                    balance_tolerance=balance_tolerance,
                    provider=phase3_provider,
                    openroad=openroad,
                    net_weights_path=feedback_path,
                    tritonpart_timeout_seconds=(
                        partition_timeout_seconds
                    ),
                    tritonpart_seed_attempts=partition_seed_attempts,
                    tritonpart_num_initial_solutions=(
                        partition_num_initial_solutions
                    ),
                    tritonpart_num_best_initial_solutions=(
                        partition_num_best_initial_solutions
                    ),
                    tritonpart_repair_min_used_fpgas=(
                        partition_repair_min_used_fpgas
                    ),
                    tritonpart_repair_balance=(
                        partition_repair_balance
                    ),
                    repart=repart,
                    repart_timeout_seconds=partition_timeout_seconds,
                    route_constraints_path=(
                        effective_route_constraints_path
                    ),
                    timing_database_path=(
                        database_path
                        if phase3_provider == "patron"
                        else None
                    ),
                    patron_refiner=patron_refiner,
                    patron_max_moves=patron_max_moves,
                    patron_flow_refinement=patron_flow_refinement,
                )
                candidate = _run_candidate_flow(
                    root=output_dir,
                    iteration=iteration,
                    assignment_path=phase3_root / "assignment.json",
                    database_path=database_path,
                    platform_path=platform_path,
                    route_constraints_path=(
                        effective_route_constraints_path
                    ),
                    frame_slots=frame_slots,
                    optimize_frame_slots=optimize_frame_slots,
                    route_max_iterations=route_max_iterations,
                    router=router,
                    route_provider=route_provider,
                    route_candidate_workers=route_candidate_workers,
                    simulation_frames=simulation_frames,
                    tdm_provider=tdm_provider,
                    ratio_optimizer=ratio_optimizer,
                    timing_dag_optimizer=timing_dag_optimizer,
                    slot_optimizer=slot_optimizer,
                    ratio_max_iterations=ratio_max_iterations,
                    max_ratio=max_ratio,
                    ratio_quantum=ratio_quantum,
                    post_refinement_iterations=(
                        post_refinement_iterations
                    ),
                    slot_refinement_iterations=(
                        slot_refinement_iterations
                    ),
                    ratio_convergence=ratio_convergence,
                )
                candidate.update(
                    {
                        "outer_iteration": outer_iteration,
                        "trial": trial,
                        "feedback_step": step_size,
                        "raw_feedback": _relative(
                            raw_feedback_path, output_dir
                        ),
                        "feedback": _relative(
                            feedback_path, output_dir
                        ),
                        "raw_feedback_validation": (
                            raw_feedback_validation
                        ),
                        "feedback_validation": feedback_validation,
                        "phase3_validation": phase3_report["validation"],
                        "partition_migration": (
                            reconstruct_partition_migration(
                                read_json(
                                    output_dir
                                    / incumbent["assignment"]
                                ),
                                read_json(
                                    phase3_root / "assignment.json"
                                ),
                                platform,
                                migration_route_constraints,
                            )
                        ),
                        "partition_class": reconstruct_partition_class(
                            read_json(phase3_root / "assignment.json"),
                            platform,
                            migration_route_constraints,
                        ),
                    }
                )
                partition_signature = candidate["partition_class"][
                    "sha256"
                ]
                equivalent_iteration = evaluated_partition_classes.get(
                    partition_signature
                )
                if equivalent_iteration is not None:
                    candidate["equivalent_partition_iteration"] = (
                        equivalent_iteration
                    )
                    equivalent_trials += 1
                else:
                    evaluated_partition_classes[partition_signature] = (
                        iteration
                    )
                decision = compare_candidate_objectives(
                    read_json(output_dir / candidate["score"]),
                    read_json(output_dir / incumbent["score"]),
                )
                candidate["decision"] = decision
                candidates.append(candidate)
                feasible_trials += 1
                if decision["accepted"]:
                    incumbent_index = iteration
                    accepted_step = True
                    if equivalent_iteration is not None:
                        termination = "symmetry-cycle"
                    break
            except (EmuFlowError, ValidationError, ValueError) as error:
                candidates.append(
                    {
                        "iteration": iteration,
                        "outer_iteration": outer_iteration,
                        "trial": trial,
                        "feedback_step": step_size,
                        "raw_feedback": _relative(
                            raw_feedback_path, output_dir
                        ),
                        "feedback": _relative(
                            feedback_path, output_dir
                        ),
                        "status": "rejected",
                        "decision": {
                            "accepted": False,
                            "reason": str(error),
                        },
                    }
                )
        if accepted_step:
            if termination == "symmetry-cycle":
                break
            continue
        if feasible_trials and equivalent_trials == feasible_trials:
            termination = "symmetry-stagnation"
        elif feasible_trials:
            termination = "line-search-rejected"
        else:
            termination = "line-search-infeasible"
        break

    report = {
        "schema": CROSS_STAGE_REPORT_SCHEMA,
        "status": "pass",
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": CROSS_STAGE_PROVIDER,
        "objective": CROSS_STAGE_OBJECTIVE,
        "configuration": {
            "phase3_provider": phase3_provider,
            "max_outer_iterations": max_outer_iterations,
            "seed": seed,
            "simulation_frames": simulation_frames,
            "pair_pressure_weight": pair_pressure_weight,
            "partition_timeout_seconds": partition_timeout_seconds,
            "partition_seed_attempts": partition_seed_attempts,
            "partition_num_initial_solutions": (
                partition_num_initial_solutions
            ),
            "partition_num_best_initial_solutions": (
                partition_num_best_initial_solutions
            ),
            "partition_repair_min_used_fpgas": (
                partition_repair_min_used_fpgas
            ),
            "partition_repair_balance": partition_repair_balance,
            "patron_max_moves": patron_max_moves,
            "seed_candidate": seed_candidate_phase3_root is not None,
            "frame_slots": frame_slots,
            "optimize_frame_slots": optimize_frame_slots,
            "route_provider": (
                route_provider or GLOBAL_CANDIDATE_PROVIDER
            ),
            "route_candidate_workers": route_candidate_workers,
            "tdm_provider": (
                tdm_provider or TDM_TIMING_DAG_RATIO_PROVIDER
            ),
            "feedback_steps": list(steps),
            "feedback_interpolation": (
                "exp(step_size*log(raw_weight))"
            ),
            "board_link_timing": board_link_timing_report is not None,
        },
        "source_sha256": {
            "ir": _sha256(ir_path),
            "platform": _sha256(platform_path),
            "database": _sha256(database_path),
            "initial_assignment": _sha256(initial_assignment_path),
            **(
                {
                    "seed_candidate_assignment": _sha256(
                        seed_candidate_phase3_root.resolve()
                        / "assignment.json"
                    )
                }
                if seed_candidate_phase3_root is not None
                else {}
            ),
            **(
                {
                    "phase3_constraints": _sha256(
                        phase3_constraints_path
                    )
                }
                if phase3_constraints_path is not None
                else {}
            ),
            **(
                {
                    "route_constraints": _sha256(
                        route_constraints_path
                    )
                }
                if route_constraints_path is not None
                else {}
            ),
            **(
                {
                    "board_link_timing": _sha256(
                        board_link_timing_path
                    )
                }
                if board_link_timing_path is not None
                else {}
            ),
        },
        **(
            {"board_link_timing": board_link_timing_report}
            if board_link_timing_report is not None
            else {}
        ),
        "selected_iteration": incumbent_index,
        "selected_candidate_id": candidates[incumbent_index][
            "candidate_id"
        ],
        "termination": termination,
        "candidates": candidates,
    }
    write_json(output_dir / "cross_stage_report.json", report)
    validate_cross_stage_report(
        output_dir / "cross_stage_report.json",
        ir_path,
        database_path,
        platform_path,
    )
    return report


def validate_cross_stage_report(
    report_path: Path,
    ir_path: Path,
    database_path: Path,
    platform_path: Path,
) -> Dict[str, Any]:
    report = read_json(report_path)
    if report.get("schema") != CROSS_STAGE_REPORT_SCHEMA:
        raise ValidationError("cross-stage report schema is invalid")
    if report.get("provider") != CROSS_STAGE_PROVIDER:
        raise ValidationError("cross-stage report provider is invalid")
    root = report_path.parent
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValidationError("cross-stage report candidates are invalid")
    ir = EmuIR.load(ir_path)
    platform = Platform.load(platform_path)
    source_hashes = report.get("source_sha256")
    if not isinstance(source_hashes, dict):
        raise ValidationError("cross-stage report source hashes are invalid")
    for name, path in (
        ("ir", ir_path),
        ("database", database_path),
        ("platform", platform_path),
    ):
        if source_hashes.get(name) != _sha256(path):
            raise ValidationError(
                f"cross-stage report source hash {name!r} mismatch"
            )
    configuration = report.get("configuration")
    if not isinstance(configuration, dict):
        raise ValidationError(
            "cross-stage report configuration is invalid"
        )
    has_seed_candidate = configuration.get("seed_candidate", False)
    if not isinstance(has_seed_candidate, bool):
        raise ValidationError(
            "cross-stage seed candidate flag is invalid"
        )
    if has_seed_candidate:
        if (
            len(candidates) < 2
            or candidates[1].get("candidate_origin") != "seed"
            or source_hashes.get("seed_candidate_assignment")
            != _sha256(root / candidates[1]["assignment"])
        ):
            raise ValidationError(
                "cross-stage seed candidate seal is invalid"
            )
    elif (
        "seed_candidate_assignment" in source_hashes
        or any(
            candidate.get("candidate_origin") == "seed"
            for candidate in candidates
        )
    ):
        raise ValidationError(
            "cross-stage unexpected seed candidate metadata"
        )
    raw_steps = configuration.get("feedback_steps")
    if not isinstance(raw_steps, list):
        raise ValidationError(
            "cross-stage report feedback steps are invalid"
        )
    steps = _normalize_feedback_steps(tuple(raw_steps))
    if configuration.get("feedback_interpolation") != (
        "exp(step_size*log(raw_weight))"
    ):
        raise ValidationError(
            "cross-stage report feedback interpolation is invalid"
        )
    partition_timeout = configuration.get("partition_timeout_seconds")
    if (
        isinstance(partition_timeout, bool)
        or not isinstance(partition_timeout, int)
        or partition_timeout <= 0
    ):
        raise ValidationError(
            "cross-stage report partition timeout is invalid"
        )
    partition_seed_attempts = configuration.get(
        "partition_seed_attempts"
    )
    if (
        isinstance(partition_seed_attempts, bool)
        or not isinstance(partition_seed_attempts, int)
        or partition_seed_attempts <= 0
    ):
        raise ValidationError(
            "cross-stage report partition seed attempts are invalid"
        )
    partition_num_initial_solutions = configuration.get(
        "partition_num_initial_solutions"
    )
    partition_num_best_initial_solutions = configuration.get(
        "partition_num_best_initial_solutions"
    )
    if (
        isinstance(partition_num_initial_solutions, bool)
        or not isinstance(partition_num_initial_solutions, int)
        or partition_num_initial_solutions <= 0
        or isinstance(partition_num_best_initial_solutions, bool)
        or not isinstance(partition_num_best_initial_solutions, int)
        or partition_num_best_initial_solutions <= 0
        or partition_num_best_initial_solutions
        > partition_num_initial_solutions
    ):
        raise ValidationError(
            "cross-stage report TritonPart search effort is invalid"
        )
    for name in (
        "partition_repair_min_used_fpgas",
        "partition_repair_balance",
    ):
        if not isinstance(configuration.get(name), bool):
            raise ValidationError(
                f"cross-stage report {name} flag is invalid"
            )
    optimize_frame_slots = configuration.get("optimize_frame_slots")
    frame_slots = configuration.get("frame_slots")
    if not isinstance(optimize_frame_slots, bool):
        raise ValidationError(
            "cross-stage report frame optimization flag is invalid"
        )
    if frame_slots is not None and (
        isinstance(frame_slots, bool)
        or not isinstance(frame_slots, int)
        or frame_slots < 2
    ):
        raise ValidationError(
            "cross-stage report frame upper bound is invalid"
        )
    if optimize_frame_slots and frame_slots is None:
        raise ValidationError(
            "cross-stage report optimized frame has no upper bound"
        )
    uses_board_link_timing = configuration.get("board_link_timing")
    if not isinstance(uses_board_link_timing, bool):
        raise ValidationError(
            "cross-stage board link timing flag is invalid"
        )
    link_timing_report = report.get("board_link_timing")
    directed_link_delays = None
    if uses_board_link_timing:
        if (
            not isinstance(link_timing_report, dict)
            or link_timing_report.get("status") != "pass"
            or not isinstance(source_hashes.get("board_link_timing"), str)
            or len(source_hashes["board_link_timing"]) != 64
        ):
            raise ValidationError(
                "cross-stage board link timing report is invalid"
            )
        artifacts = link_timing_report.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ValidationError(
                "cross-stage board link timing artifacts are invalid"
            )
        timing_artifact = _validated_report_artifact(
            root, artifacts.get("database"), "board link timing"
        )
        constraints_artifact = _validated_report_artifact(
            root,
            artifacts.get("effective_route_constraints"),
            "effective route constraints",
        )
        link_timing = read_json(timing_artifact)
        link_validation = validate_board_link_timing(
            link_timing, platform
        )
        directed_link_delays, projection = directed_route_link_delays(
            link_timing, platform
        )
        if (
            link_timing_report.get("validation") != link_validation
            or link_timing_report.get("routing_projection")
            != projection
            or link_timing_report.get("applied_to")
            != [
                "every-phase4-candidate",
                "every-phase5-candidate",
                "cross-stage-objective-and-feedback",
            ]
        ):
            raise ValidationError(
                "cross-stage board link timing reconstruction mismatch"
            )
        effective_constraints = read_json(constraints_artifact)
        if (
            not isinstance(effective_constraints, dict)
            or effective_constraints.get("directed_link_delay_ns")
            != directed_link_delays
        ):
            raise ValidationError(
                "cross-stage effective link timing constraints mismatch"
            )
    elif (
        link_timing_report is not None
        or "board_link_timing" in source_hashes
    ):
        raise ValidationError(
            "cross-stage unexpected board link timing metadata"
        )
    migration_route_constraints = (
        None
        if "route_constraints" in source_hashes
        and not uses_board_link_timing
        else load_route_constraints(
            constraints_artifact if uses_board_link_timing else None,
            platform,
            frame_slots=frame_slots,
        )
    )
    incumbent = None
    incumbent_record = None
    selected = 0
    validated = 0
    evaluated_partition_classes: Dict[str, int] = {}
    active_outer = 1
    expected_trial = 0
    for index, candidate in enumerate(candidates):
        if candidate.get("iteration") != index:
            raise ValidationError(
                "cross-stage candidate iterations are not contiguous"
            )
        is_seed_candidate = (
            index == 1
            and has_seed_candidate
            and candidate.get("candidate_origin") == "seed"
        )
        if index > 0 and not is_seed_candidate:
            if candidate.get("outer_iteration") != active_outer:
                raise ValidationError(
                    "cross-stage candidate outer iteration mismatch"
                )
            trial = candidate.get("trial")
            if trial is None:
                if (
                    candidate.get("status") != "rejected"
                    or index != len(candidates) - 1
                ):
                    raise ValidationError(
                        "cross-stage feedback failure record is invalid"
                    )
            elif (
                isinstance(trial, bool)
                or not isinstance(trial, int)
                or trial != expected_trial
                or trial >= len(steps)
                or candidate.get("feedback_step") != steps[trial]
            ):
                raise ValidationError(
                    "cross-stage line-search trial metadata mismatch"
                )
            else:
                expected_trial += 1
        if candidate.get("status") != "pass":
            if candidate.get("decision", {}).get("accepted"):
                raise ValidationError(
                    "failed cross-stage candidate cannot be accepted"
                )
            continue
        if directed_link_delays is not None:
            candidate_routes = read_json(root / candidate["routes"])
            if candidate_routes.get("constraints", {}).get(
                "directed_link_delay_ns"
            ) != directed_link_delays:
                raise ValidationError(
                    "cross-stage candidate link timing constraints mismatch"
                )
        if index > 0 and not is_seed_candidate:
            assert incumbent_record is not None
            raw_feedback = read_json(root / candidate["raw_feedback"])
            expected_raw_validation = validate_partition_feedback(
                read_json(root / incumbent_record["routes"]),
                read_json(root / incumbent_record["ratio_plan"]),
                platform,
                raw_feedback,
            )
            if (
                candidate.get("raw_feedback_validation")
                != expected_raw_validation
            ):
                raise ValidationError(
                    "cross-stage raw feedback validation mismatch"
                )
            damped_feedback = read_json(root / candidate["feedback"])
            expected_feedback_validation = (
                validate_damped_partition_feedback(
                    raw_feedback, damped_feedback
                )
            )
            if (
                candidate.get("feedback_validation")
                != expected_feedback_validation
                or expected_feedback_validation["step_size"]
                != candidate["feedback_step"]
            ):
                raise ValidationError(
                    "cross-stage damped feedback validation mismatch"
                )
        validate_cross_stage_candidate(
            root / candidate["score"],
            database_path,
            root / candidate["assignment"],
            root / candidate["routes"],
            root / candidate["schedule"],
            root / candidate["ratio_plan"],
            platform_path,
        )
        assignment_path = root / candidate["assignment"]
        clusters_path = assignment_path.parent / "clusters.json"
        expected_partition_class = reconstruct_partition_class(
            read_json(assignment_path),
            platform,
            migration_route_constraints,
        )
        if candidate.get("partition_class") != expected_partition_class:
            raise ValidationError(
                "cross-stage partition class mismatch"
            )
        partition_signature = expected_partition_class["sha256"]
        equivalent_iteration = evaluated_partition_classes.get(
            partition_signature
        )
        if index == 0:
            if candidate.get("equivalent_partition_iteration") is not None:
                raise ValidationError(
                    "cross-stage initial partition cannot be equivalent"
                )
        elif equivalent_iteration is None:
            if candidate.get("equivalent_partition_iteration") is not None:
                raise ValidationError(
                    "cross-stage unexpected equivalent partition"
                )
        else:
            reported_equivalent = candidate.get(
                "equivalent_partition_iteration"
            )
            if (
                isinstance(reported_equivalent, bool)
                or not isinstance(reported_equivalent, int)
                or reported_equivalent != equivalent_iteration
            ):
                raise ValidationError(
                    "cross-stage equivalent partition iteration mismatch"
                )
        if equivalent_iteration is None:
            evaluated_partition_classes[partition_signature] = index
        if index > 0:
            assert incumbent_record is not None
            expected_migration = reconstruct_partition_migration(
                read_json(root / incumbent_record["assignment"]),
                read_json(assignment_path),
                platform,
                migration_route_constraints,
            )
            if candidate.get("partition_migration") != expected_migration:
                raise ValidationError(
                    "cross-stage partition migration mismatch"
                )
        phase3_validation = validate_partition_artifacts(
            ir,
            platform,
            read_json(clusters_path),
            read_json(assignment_path),
        )
        if candidate.get("phase3_validation") != phase3_validation:
            raise ValidationError(
                "cross-stage report Phase 3 validation mismatch"
            )
        score = read_json(root / candidate["score"])
        frame_search_path = candidate.get("frame_search")
        if optimize_frame_slots:
            if not isinstance(frame_search_path, str):
                raise ValidationError(
                    "cross-stage optimized candidate has no frame search"
                )
            frame_validation = validate_frame_search_report(
                read_json(root / frame_search_path)
            )
            if (
                candidate.get("frame_search_validation")
                != frame_validation
                or frame_validation["selected_frame_slots"]
                != score["objective_metrics"]["frame_slots"]
                or frame_validation["selected_frame_slots"]
                != read_json(root / candidate["schedule"])["metrics"][
                    "frame_slots"
                ]
            ):
                raise ValidationError(
                    "cross-stage frame-search selection mismatch"
                )
        elif frame_search_path is not None or (
            "frame_search_validation" in candidate
        ):
            raise ValidationError(
                "cross-stage fixed-frame candidate has frame-search metadata"
            )
        for key in (
            "candidate_id",
            "objective_metrics",
            "objective_key",
        ):
            if candidate.get(key) != score[key]:
                raise ValidationError(
                    f"cross-stage report candidate {key} mismatch"
                )
        decision = candidate.get("decision")
        if index == 0:
            if not isinstance(decision, dict) or not decision.get(
                "accepted"
            ):
                raise ValidationError(
                    "cross-stage initial candidate must be accepted"
                )
            incumbent = score
            incumbent_record = candidate
        else:
            expected = compare_candidate_objectives(score, incumbent)
            if decision != expected:
                raise ValidationError(
                    "cross-stage candidate decision mismatch"
                )
            if expected["accepted"]:
                incumbent = score
                incumbent_record = candidate
                selected = index
                if not is_seed_candidate:
                    active_outer += 1
                    expected_trial = 0
        validated += 1
    if report.get("selected_iteration") != selected:
        raise ValidationError(
            "cross-stage selected iteration does not match decisions"
        )
    if (
        report.get("selected_candidate_id")
        != candidates[selected]["candidate_id"]
    ):
        raise ValidationError(
            "cross-stage selected candidate identity mismatch"
        )
    termination = report.get("termination")
    if termination not in {
        "iteration-limit",
        "feedback-generation-failed",
        "line-search-rejected",
        "line-search-infeasible",
        "symmetry-cycle",
        "symmetry-stagnation",
    }:
        raise ValidationError("cross-stage termination is invalid")
    if termination == "symmetry-cycle":
        final = candidates[-1]
        if (
            final.get("status") != "pass"
            or not final.get("decision", {}).get("accepted")
            or isinstance(
                final.get("equivalent_partition_iteration"), bool
            )
            or not isinstance(
                final.get("equivalent_partition_iteration"), int
            )
            or selected != final["iteration"]
        ):
            raise ValidationError(
                "cross-stage symmetry-cycle termination mismatch"
            )
    if termination == "symmetry-stagnation":
        outer_iterations = [
            candidate.get("outer_iteration")
            for candidate in candidates[1:]
            if isinstance(candidate.get("outer_iteration"), int)
        ]
        last_outer = max(outer_iterations, default=None)
        feasible = [
            candidate
            for candidate in candidates[1:]
            if candidate.get("outer_iteration") == last_outer
            and candidate.get("status") == "pass"
        ]
        if (
            not feasible
            or any(
                candidate.get("decision", {}).get("accepted")
                or isinstance(
                    candidate.get("equivalent_partition_iteration"),
                    bool,
                )
                or not isinstance(
                    candidate.get("equivalent_partition_iteration"), int
                )
                for candidate in feasible
            )
        ):
            raise ValidationError(
                "cross-stage symmetry-stagnation termination mismatch"
            )
    return {
        "status": "pass",
        "validated_candidates": validated,
        "selected_iteration": selected,
        "selected_candidate_id": candidates[selected]["candidate_id"],
    }
