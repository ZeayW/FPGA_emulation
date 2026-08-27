"""Materialize an academic Chimew lookahead from an open physical prepass.

The public contest BoardDBs do not carry a real package-pin inventory.  This
adapter deliberately uses the routed baseline only as a *lookahead prepass*:
the OpenPARF placement and imported VTR architecture are converted into the
paper-facing crossing, RUDY, and bank/channel inputs.  The generated package
pins are explicitly virtual academic identities and must never be used as a
hardware BSP.
"""

from __future__ import annotations

import hashlib
import heapq
import math
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
    CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
)
from .chimew_grouping import (
    CHIMEW_ACADEMIC_CROSSING_PROVIDER,
    CHIMEW_CROSSING_SCHEMA,
    CHIMEW_TIMING_GUARD_PROVIDER,
    build_chimew_initial_groups,
    materialize_chimew_schedule_ratios,
)
from .chimew_phase6 import (
    CHIMEW_ELECTRICAL_MAP_PROVIDER,
    CHIMEW_ELECTRICAL_MAP_SCHEMA,
)
from .chimew_qualification import canonical_sha256
from .chimew_refinement import (
    CHIMEW_POSITION_PROVIDER,
    CHIMEW_POSITION_SCHEMA,
    refine_chimew_groups,
)
from .chimew_rudy import CHIMEW_RUDY_INPUT_PROVIDER, CHIMEW_RUDY_INPUT_SCHEMA
from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform


ACADEMIC_CHIMEW_LOOKAHEAD_SCHEMA = "emuflow.academic-chimew-lookahead/v1"
ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER = (
    "openparf-vtr-baseline-lookahead+virtual-electrical-map-v1"
)
ACADEMIC_CHIMEW_TIMING_WEIGHT_PROVIDER = (
    "partition-projected-sta-criticality-v1"
)


_VTR_INSTANCE_ATOM_RE = re.compile(
    r"^i(?P<index>[0-9]+)(?:(?:__bit[0-9]+)|(?:__control))?$"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_vpr_placement(path: Path) -> Dict[str, Tuple[float, float]]:
    result: Dict[str, Tuple[float, float]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith(
                ("Netlist_File:", "Array size:")
            ):
                continue
            fields = line.split()
            if len(fields) != 6 or not fields[5].startswith("#"):
                raise ValidationError(
                    f"{path}:{line_number}: malformed VPR lookahead placement"
                )
            if fields[0] in result:
                raise ValidationError(
                    f"{path}:{line_number}: duplicate placed block {fields[0]!r}"
                )
            result[fields[0]] = (float(fields[1]), float(fields[2]))
    if not result:
        raise ValidationError("academic Chimew lookahead placement is empty")
    return result


def _instance_locations(
    physical_report: Mapping[str, Any], output_dir: Path
) -> Tuple[
    Dict[str, Dict[str, Tuple[float, float, float, float]]],
    Dict[str, Tuple[float, float]],
    Dict[str, Dict[str, Tuple[float, float, float, float]]],
    Path,
    Path,
]:
    records = physical_report.get("fpgas")
    if not isinstance(records, list) or not records:
        raise ValidationError("academic Chimew prepass has no FPGA records")
    placement_source = output_dir / "sources" / "placement.json"
    architecture_source = output_dir / "sources" / "architecture.json"
    placement_records = []
    architecture_records = []
    locations: Dict[
        str, Dict[str, Tuple[float, float, float, float]]
    ] = {}
    boundary_locations: Dict[
        str, Dict[str, Tuple[float, float, float, float]]
    ] = {}
    y_bounds: Dict[str, Tuple[float, float]] = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("status") != "pass":
            raise ValidationError("academic Chimew prepass FPGA did not pass")
        fpga = record.get("fpga")
        stages = record.get("stages")
        if not isinstance(fpga, str) or not isinstance(stages, Mapping):
            raise ValidationError("academic Chimew prepass FPGA is malformed")
        placement_stage = stages.get("openparf_placement")
        packed_stage = stages.get("packed_contract")
        lowering = stages.get("placement_ir")
        if not all(
            isinstance(stage, Mapping)
            for stage in (placement_stage, packed_stage, lowering)
        ):
            raise ValidationError("academic Chimew prepass lacks open placement")
        placement_path = Path(placement_stage["artifacts"]["vpr_placement"])
        packed_path = Path(packed_stage["output"])
        fpga_root = Path(lowering["output"]).parent
        architecture_path = fpga_root / "architecture.json"
        for path in (placement_path, packed_path, architecture_path):
            if not path.is_file():
                raise ValidationError(
                    f"academic Chimew prepass artifact is missing: {path}"
                )
        cluster_locations = _parse_vpr_placement(placement_path)
        packed = read_json(packed_path)
        placement_ir = read_json(Path(lowering["output"]))
        raw_instances = placement_ir.get("instances")
        if not isinstance(raw_instances, list) or not raw_instances:
            raise ValidationError(
                f"academic Chimew prepass FPGA {fpga!r} has no placement instances"
            )
        instance_order = []
        for index, instance in enumerate(raw_instances):
            instance_id = instance.get("id") if isinstance(instance, Mapping) else None
            if not isinstance(instance_id, str) or not instance_id:
                raise ValidationError(
                    f"academic Chimew placement instance {index} is malformed"
                )
            instance_order.append(instance_id)
        if len(instance_order) != len(set(instance_order)):
            raise ValidationError(
                f"academic Chimew prepass FPGA {fpga!r} has duplicate instances"
            )
        known_instances = set(instance_order)
        instance_points: Dict[str, list[Tuple[float, float]]] = defaultdict(list)
        unmapped_atoms = 0
        for cluster in packed.get("clusters", []):
            point = cluster_locations.get(cluster.get("name"))
            if point is None:
                raise ValidationError(
                    f"packed cluster {cluster.get('name')!r} has no placement"
                )
            for atom in cluster.get("atoms", []):
                instance_id = atom if atom in known_instances else None
                if instance_id is None and isinstance(atom, str):
                    match = _VTR_INSTANCE_ATOM_RE.fullmatch(atom)
                    if match is not None:
                        instance_index = int(match.group("index"))
                        if instance_index < len(instance_order):
                            instance_id = instance_order[instance_index]
                if instance_id is None:
                    # VPR may retain constant/helper atoms which are not
                    # source EmuIR instances. They cannot be timing endpoints.
                    unmapped_atoms += 1
                    continue
                instance_points[instance_id].append(point)
        if not instance_points:
            raise ValidationError(
                f"academic Chimew prepass FPGA {fpga!r} has no source-mapped atoms"
            )
        raw_instance_map = {
            instance: (
                sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points),
            )
            for instance, points in instance_points.items()
        }
        x_values = [point[0] for point in raw_instance_map.values()]
        y_values = [point[1] for point in raw_instance_map.values()]
        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = min(y_values), max(y_values)
        y_bounds[fpga] = (y_min, y_max if y_max > y_min else y_min + 1.0)

        def _normalise(value: float, lower: float, upper: float) -> float:
            return 0.5 if upper == lower else (value - lower) / (upper - lower)

        instance_map = {
            atom: (
                point[0],
                point[1],
                _normalise(point[0], x_min, x_max),
                _normalise(point[1], y_min, y_max),
            )
            for atom, point in raw_instance_map.items()
        }
        locations[fpga] = instance_map
        boundary_path = lowering.get("boundary_identity", {}).get("output")
        if not isinstance(boundary_path, str) or not Path(boundary_path).is_file():
            raise ValidationError(
                f"academic Chimew prepass FPGA {fpga!r} has no boundary identity"
            )
        boundary_document = read_json(Path(boundary_path))
        boundary_map: Dict[str, Tuple[float, float, float, float]] = {}
        for endpoint in boundary_document.get("endpoints", []):
            if not isinstance(endpoint, Mapping):
                raise ValidationError("academic Chimew boundary endpoint is malformed")
            entry_id = endpoint.get("schedule_entry")
            registers = endpoint.get("merged_ir", {}).get(
                "boundary_register_instances"
            )
            if not isinstance(entry_id, str) or not isinstance(registers, list):
                raise ValidationError("academic Chimew boundary endpoint is incomplete")
            points = [instance_map.get(instance) for instance in registers]
            present = [point for point in points if point is not None]
            if not present:
                # Some boundary identities are not exposed by the current
                # schedule (for example a superseded lane endpoint).  Keep
                # only placed identities here and require coverage when an
                # active entry actually needs the forwarding fallback below.
                continue
            point = (
                sum(value[0] for value in present) / len(present),
                sum(value[1] for value in present) / len(present),
                sum(value[2] for value in present) / len(present),
                sum(value[3] for value in present) / len(present),
            )
            if entry_id in boundary_map:
                raise ValidationError(
                    f"academic Chimew boundary entry {entry_id!r} is duplicated"
                )
            boundary_map[entry_id] = point
        boundary_locations[fpga] = boundary_map
        placement_records.append(
            {
                "fpga": fpga,
                "placement_sha256": _sha256(placement_path),
                "packed_contract_sha256": _sha256(packed_path),
                "raw_bounds": {
                    "x_min": x_min,
                    "x_max": x_max,
                    "y_min": y_min,
                    "y_max": y_max,
                },
                "instances": [
                    {
                        "id": instance,
                        "raw_x": point[0],
                        "raw_y": point[1],
                        "normalised_x": point[2],
                        "normalised_y": point[3],
                    }
                    for instance, point in sorted(instance_map.items())
                ],
                "atom_mapping": {
                    "provider": "vtr-eblif-deterministic-instance-order-v1",
                    "source_instances": len(instance_order),
                    "placed_source_instances": len(instance_map),
                    "unmapped_helper_atoms": unmapped_atoms,
                },
            }
        )
        architecture_records.append(
            {
                "fpga": fpga,
                "sha256": _sha256(architecture_path),
                "architecture": read_json(architecture_path),
            }
        )
    placement_source.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        placement_source,
        {
            "schema": "emuflow.academic-lookahead-placement-source/v1",
            "provider": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "fpgas": placement_records,
        },
    )
    write_json(
        architecture_source,
        {
            "schema": "emuflow.academic-lookahead-architecture-source/v1",
            "provider": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "fpgas": architecture_records,
        },
    )
    return (
        locations,
        y_bounds,
        boundary_locations,
        placement_source,
        architecture_source,
    )


def _centroid(
    fpga: str,
    instances: list[str],
    locations: Mapping[
        str, Mapping[str, Tuple[float, float, float, float]]
    ],
) -> Tuple[float, float, float, bool]:
    points = [locations.get(fpga, {}).get(instance) for instance in instances]
    present = [point for point in points if point is not None]
    if not present:
        return 0.5, 0.5, 0.5, True
    return (
        sum(point[0] for point in present) / len(present),
        sum(point[1] for point in present) / len(present),
        sum(point[3] for point in present) / len(present),
        False,
    )


def _crossed_boundaries(value: float, anchor: float, count: int) -> list[int]:
    if count <= 0:
        return []
    lower, upper = sorted((value, anchor))
    return [
        boundary
        for boundary in range(count)
        if lower < float(boundary + 1) / float(count + 1) <= upper
    ]


def _timing_weights(
    timing_paths_path: Optional[Path],
    schedule: Mapping[str, Any],
    routes: Mapping[str, Any],
    *,
    scale: float = 9.0,
    path_scope: str = "exact-hop",
) -> Tuple[Dict[str, float], Optional[Path], Dict[str, int]]:
    """Return a stable per-entry timing weight for the EmuFlow extension.

    Chimew's published geometric assignment treats signals uniformly.  The
    open-flow integration may additionally bind partition-projected STA paths
    and use the same bounded power-law criticality employed by EmuFlow's
    timing-driven partitioner.  The native kernel still performs the complete
    matching; this adapter merely materializes the source-bound weights.
    """

    if not math.isfinite(scale) or scale < 0.0:
        raise ValidationError("academic Chimew timing weight scale is invalid")
    if path_scope not in {"exact-hop", "whole-net"}:
        raise ValidationError("academic Chimew timing path scope is invalid")
    if timing_paths_path is None:
        return {}, None, {"exact_path_hops": 0, "whole_net_fallbacks": 0}
    document = read_json(timing_paths_path)
    if document.get("schema") != "emuflow.sta-paths/v1":
        raise ValidationError("academic Chimew timing path schema is invalid")
    if document.get("design") != schedule.get("design"):
        raise ValidationError("academic Chimew timing path design differs")
    entries_by_net: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule.get("entries", []):
        entries_by_net[entry["net"]].append(entry)
    route_timing_paths = {
        item["path"]: item
        for item in routes.get("timing", {}).get("paths", [])
        if isinstance(item, Mapping) and isinstance(item.get("path"), str)
    }
    route_by_net = {
        item["net"]: item
        for item in routes.get("routes", [])
        if isinstance(item, Mapping) and isinstance(item.get("net"), str)
    }
    criticality: Dict[str, float] = {}
    paths = document.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValidationError("academic Chimew timing paths are empty")
    validated_paths: list[Tuple[Mapping[str, Any], float]] = []
    negative_deficits: list[float] = []
    for index, path in enumerate(paths):
        if not isinstance(path, Mapping):
            raise ValidationError(
                f"academic Chimew timing paths[{index}] is invalid"
            )
        period = path.get("clock_period_ns")
        slack = path.get("slack_ns")
        cut_nets = path.get("cut_nets")
        if (
            isinstance(period, bool)
            or not isinstance(period, (int, float))
            or not math.isfinite(float(period))
            or not float(period) > 0.0
            or isinstance(slack, bool)
            or not isinstance(slack, (int, float))
            or not math.isfinite(float(slack))
            or not isinstance(cut_nets, list)
        ):
            raise ValidationError(
                f"academic Chimew timing paths[{index}] is malformed"
            )
        normalized = path.get("normalized_slack")
        if normalized is None:
            normalized = float(slack) / float(period)
        if (
            isinstance(normalized, bool)
            or not isinstance(normalized, (int, float))
            or not math.isfinite(float(normalized))
        ):
            raise ValidationError(
                f"academic Chimew timing paths[{index}] has invalid normalized slack"
            )
        normalized_value = float(normalized)
        validated_paths.append((path, normalized_value))
        negative_deficits.append(max(0.0, -normalized_value))
    maximum_deficit = max(negative_deficits, default=0.0)
    exact_path_hops = 0
    whole_net_fallbacks = 0
    for path, normalized_slack in validated_paths:
        if maximum_deficit > 0.0:
            value = max(0.0, -normalized_slack) / maximum_deficit
        else:
            period = float(path["clock_period_ns"])
            slack = float(path["slack_ns"])
            value = max(0.0, min(1.0, 1.0 - slack / period))
        selected_entries: set[str] = set()
        route_timing = (
            route_timing_paths.get(path.get("id"))
            if path_scope == "exact-hop"
            else None
        )
        if route_timing is not None:
            for transition in route_timing.get("cut_transitions", []):
                if not isinstance(transition, Mapping):
                    raise ValidationError(
                        "academic Chimew route timing transition is invalid"
                    )
                net = transition.get("net")
                source = transition.get("from")
                target = transition.get("to")
                route = route_by_net.get(net)
                if route is None:
                    raise ValidationError(
                        f"academic Chimew timing path route for {net!r} is absent"
                    )
                parents: Dict[str, Tuple[str, str]] = {}
                for edge in route.get("tree_edges", []):
                    if not isinstance(edge, Mapping):
                        raise ValidationError(
                            "academic Chimew route tree edge is invalid"
                        )
                    parents[edge["to"]] = (edge["from"], edge["link"])
                current = target
                while current != source:
                    parent = parents.get(current)
                    if parent is None:
                        raise ValidationError(
                            f"academic Chimew route tree does not reach {target!r}"
                        )
                    previous, link = parent
                    matches = [
                        entry["id"]
                        for entry in entries_by_net.get(net, [])
                        if entry.get("from") == previous
                        and entry.get("to") == current
                        and entry.get("link") == link
                    ]
                    if len(matches) != 1:
                        raise ValidationError(
                            "academic Chimew timing hop does not identify one schedule entry"
                        )
                    selected_entries.add(matches[0])
                    current = previous
            exact_path_hops += len(selected_entries)
        else:
            whole_net_fallbacks += 1
            for net in path["cut_nets"]:
                if not isinstance(net, str):
                    raise ValidationError(
                        "academic Chimew timing path has an invalid cut net"
                    )
                selected_entries.update(
                    entry["id"] for entry in entries_by_net.get(net, [])
                )
        for entry in selected_entries:
            criticality[entry] = max(criticality.get(entry, 0.0), value)
    weights = {
        entry["id"]: 1.0 + scale * criticality.get(entry["id"], 0.0) ** 2.0
        for entry in schedule.get("entries", [])
    }
    return weights, timing_paths_path, {
        "exact_path_hops": exact_path_hops,
        "whole_net_fallbacks": whole_net_fallbacks,
    }


def _coalesce_timing_guard_lanes(
    schedule: Mapping[str, Any],
    refined: Mapping[str, Any],
    protected_entries: set[str],
) -> Tuple[Dict[str, int], list[Dict[str, Any]]]:
    """Materialize each protected Phase-5 lane as one fixed external group.

    The native Chimew kernels deliberately retain their paper-defined grouping
    and refinement identities.  EmuFlow's timing guard is a separate
    integration extension: it freezes *all* Phase-5 mux members of a critical
    physical lane.  The position refiner may place members of that frozen lane
    in distinct algorithmic groups (for example because their TDM slots
    differ), but those groups cannot become separate electrical channels:
    Phase 5 already assigned them to one physical lane.

    A fresh deterministic materialization group is allocated for every
    protected lane.  This also isolates it from an unprotected member that
    happened to share a native group ID.  The original refinement output is
    retained and independently checked; this helper only defines the legal
    Phase-6 channel boundary.
    """

    schedule_entries = schedule.get("entries")
    refined_entries = refined.get("entries")
    if not isinstance(schedule_entries, list) or not isinstance(refined_entries, list):
        raise ValidationError("academic Chimew timing guard inputs are malformed")

    groups_by_entry: Dict[str, int] = {}
    for index, item in enumerate(refined_entries):
        if not isinstance(item, Mapping):
            raise ValidationError(
                f"academic Chimew refinement entry {index} is malformed"
            )
        entry_id = item.get("schedule_entry")
        group = item.get("group")
        if (
            not isinstance(entry_id, str)
            or not entry_id
            or isinstance(group, bool)
            or not isinstance(group, int)
            or group < 0
            or entry_id in groups_by_entry
        ):
            raise ValidationError(
                f"academic Chimew refinement entry {index} is invalid"
            )
        groups_by_entry[entry_id] = group

    entries_by_id: Dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(schedule_entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(
                f"academic Chimew schedule entry {index} is malformed"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in entries_by_id:
            raise ValidationError(
                f"academic Chimew schedule entry {index} has invalid identity"
            )
        entries_by_id[entry_id] = entry

    if set(groups_by_entry) != set(entries_by_id):
        raise ValidationError(
            "academic Chimew refinement does not cover the materialized schedule"
        )
    unknown_protected = protected_entries - set(entries_by_id)
    if unknown_protected:
        raise ValidationError(
            "academic Chimew timing guard references an unknown schedule entry"
        )

    effective_groups = dict(groups_by_entry)
    if not protected_entries:
        return effective_groups, []

    def lane_key(entry: Mapping[str, Any]) -> Tuple[str, str, str, int]:
        link = entry.get("link")
        source = entry.get("from")
        sink = entry.get("to")
        lane = entry.get("lane")
        if (
            not isinstance(link, str)
            or not isinstance(source, str)
            or not isinstance(sink, str)
            or isinstance(lane, bool)
            or not isinstance(lane, int)
            or lane < 0
        ):
            raise ValidationError("academic Chimew timing guard lane is malformed")
        return link, source, sink, lane

    protected_lanes = {
        lane_key(entries_by_id[entry_id]) for entry_id in protected_entries
    }
    members_by_lane: Dict[Tuple[str, str, str, int], list[str]] = defaultdict(list)
    for entry_id, entry in entries_by_id.items():
        key = lane_key(entry)
        if key in protected_lanes:
            members_by_lane[key].append(entry_id)

    next_group = max(groups_by_entry.values(), default=-1) + 1
    bundles = []
    for lane in sorted(members_by_lane):
        members = members_by_lane[lane]
        native_groups = sorted({groups_by_entry[entry_id] for entry_id in members})
        materialized_group = next_group
        next_group += 1
        for entry_id in members:
            effective_groups[entry_id] = materialized_group
        bundles.append(
            {
                "link": lane[0],
                "from": lane[1],
                "to": lane[2],
                "physical_lane": lane[3],
                "schedule_entries": members,
                "refined_groups": native_groups,
                "materialized_group": materialized_group,
            }
        )
    return effective_groups, bundles


def _static_guardable_entries(
    schedule: Mapping[str, Any],
    link_by_id: Mapping[str, Any],
    protected_entries: set[str],
) -> Tuple[set[str], list[Dict[str, Any]]]:
    """Return timing-guard entries that can retain a concrete static lane.

    Bidirectional TDM bundles represent opposite directions sharing one
    concrete lane, so every protected entry can retain its Phase-5 lane.  The
    returned legacy relaxation list is intentionally empty; keeping the field
    preserves report compatibility while making any future relaxation visible.
    """

    entries = schedule.get("entries")
    if not isinstance(entries, list):
        raise ValidationError("academic Chimew schedule entries are malformed")
    entries_by_id: Dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(
                f"academic Chimew schedule entry {index} is malformed"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in entries_by_id:
            raise ValidationError(
                f"academic Chimew schedule entry {index} has invalid identity"
            )
        entries_by_id[entry_id] = entry
    unknown = protected_entries - set(entries_by_id)
    if unknown:
        raise ValidationError(
            "academic Chimew timing guard references an unknown schedule entry"
        )

    for entry_id in protected_entries:
        entry = entries_by_id[entry_id]
        link_id = entry.get("link")
        lane = entry.get("lane")
        if (
            not isinstance(link_id, str)
            or link_id not in link_by_id
            or isinstance(lane, bool)
            or not isinstance(lane, int)
            or lane < 0
        ):
            raise ValidationError("academic Chimew timing guard lane is malformed")
    return set(protected_entries), []


def _group_ratio_slots(
    key: Tuple[str, str, int],
    members: list[Dict[str, Any]],
    schedule_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[int, set[int]]:
    ratios: set[int] = set()
    slots: set[int] = set()
    for member in members:
        entry = schedule_by_id.get(member["id"])
        if entry is None:
            raise ValidationError("academic Chimew group references an unknown entry")
        ratio = entry.get("tdm_ratio")
        slot = entry.get("slot")
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, int)
            or ratio <= 0
            or isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
        ):
            raise ValidationError("academic Chimew group has invalid TDM coordinates")
        ratios.add(ratio)
        if slot in slots:
            raise ValidationError("academic Chimew group has a TDM slot collision")
        slots.add(slot)
    if len(ratios) != 1 or len(members) > next(iter(ratios)):
        raise ValidationError("academic Chimew group violates TDM capacity")
    return next(iter(ratios)), slots


def _group_endpoint_y(
    members: list[Dict[str, Any]], direction: str
) -> Tuple[float, float]:
    endpoint_a = []
    endpoint_b = []
    for member in members:
        fanout_y = float(member["fanout"]["y"])
        fanin_y = sum(float(point["y"]) for point in member["fanins"]) / len(
            member["fanins"]
        )
        if direction == "a_to_b":
            endpoint_a.append(fanout_y)
            endpoint_b.append(fanin_y)
        else:
            endpoint_a.append(fanin_y)
            endpoint_b.append(fanout_y)
    return sum(endpoint_a) / len(endpoint_a), sum(endpoint_b) / len(endpoint_b)


def _maximum_bipartite_matching(
    adjacency: list[list[int]], right_count: int
) -> Tuple[list[int], list[int]]:
    """Deterministic Hopcroft--Karp certificate for bundle feasibility."""

    left_match = [-1] * len(adjacency)
    right_match = [-1] * right_count
    distance = [0] * len(adjacency)
    infinity = len(adjacency) + right_count + 1

    def bfs() -> bool:
        queue: deque[int] = deque()
        found = False
        for left in range(len(adjacency)):
            if left_match[left] < 0:
                distance[left] = 0
                queue.append(left)
            else:
                distance[left] = infinity
        while queue:
            left = queue.popleft()
            for right in adjacency[left]:
                mate = right_match[right]
                if mate < 0:
                    found = True
                elif distance[mate] == infinity:
                    distance[mate] = distance[left] + 1
                    queue.append(mate)
        return found

    def dfs(left: int) -> bool:
        for right in adjacency[left]:
            mate = right_match[right]
            if mate < 0 or (
                distance[mate] == distance[left] + 1 and dfs(mate)
            ):
                left_match[left] = right
                right_match[right] = left
                return True
        distance[left] = infinity
        return False

    while bfs():
        for left in range(len(adjacency)):
            if left_match[left] < 0:
                dfs(left)
    return left_match, right_match


def _minimum_cost_pairs(
    adjacency: list[list[Tuple[int, int]]], right_count: int, target: int
) -> list[Tuple[int, int]]:
    """Return an exact deterministic min-cost cardinality matching."""

    if target == 0:
        return []
    left_count = len(adjacency)
    source = 0
    first_left = 1
    first_right = first_left + left_count
    sink = first_right + right_count
    graph: list[list[list[int]]] = [[] for _ in range(sink + 1)]
    pair_edges: Dict[Tuple[int, int], list[int]] = {}

    def add_edge(start: int, end: int, capacity: int, cost: int) -> list[int]:
        forward = [end, len(graph[end]), capacity, cost]
        reverse = [start, len(graph[start]), 0, -cost]
        graph[start].append(forward)
        graph[end].append(reverse)
        return forward

    for left in range(left_count):
        add_edge(source, first_left + left, 1, 0)
    edge_count = sum(len(row) for row in adjacency)
    lexicographic_scale = target * max(1, edge_count) + 1
    rank = 0
    for left, row in enumerate(adjacency):
        for right, primary_cost in row:
            pair_edges[(left, right)] = add_edge(
                first_left + left,
                first_right + right,
                1,
                primary_cost * lexicographic_scale + rank,
            )
            rank += 1
    for right in range(right_count):
        add_edge(first_right + right, sink, 1, 0)

    potential = [0] * len(graph)
    infinity = 1 << 120
    flow = 0
    while flow < target:
        distance = [infinity] * len(graph)
        previous: list[Optional[Tuple[int, int]]] = [None] * len(graph)
        distance[source] = 0
        queue = [(0, source)]
        while queue:
            current, node = heapq.heappop(queue)
            if current != distance[node]:
                continue
            for edge_index, edge in enumerate(graph[node]):
                end, _reverse, capacity, cost = edge
                if capacity <= 0:
                    continue
                candidate = current + cost + potential[node] - potential[end]
                if candidate < distance[end]:
                    distance[end] = candidate
                    previous[end] = (node, edge_index)
                    heapq.heappush(queue, (candidate, end))
        if distance[sink] == infinity:
            raise ValidationError(
                "shared-bidirectional Chimew bundling is unexpectedly infeasible"
            )
        for node, value in enumerate(distance):
            if value != infinity:
                potential[node] += value
        node = sink
        while node != source:
            predecessor = previous[node]
            if predecessor is None:
                raise ValidationError("shared-bidirectional matching is incomplete")
            start, edge_index = predecessor
            edge = graph[start][edge_index]
            edge[2] -= 1
            graph[node][edge[1]][2] += 1
            node = start
        flow += 1
    return sorted(pair for pair, edge in pair_edges.items() if edge[2] == 0)


def _bundle_shared_bidirectional_groups(
    *,
    link_id: str,
    lane_count: int,
    grouped: Mapping[Tuple[str, str, int], list[Dict[str, Any]]],
    fixed_lane_by_group: Mapping[Tuple[str, str, int], int],
    schedule_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[
    Dict[Tuple[str, str, int], list[Dict[str, Any]]],
    Dict[Tuple[str, str, int], int],
    Dict[Tuple[str, str, int], list[Tuple[str, str, int]]],
    Dict[str, int],
]:
    """Pack opposite directions onto one shared lane when TDM slots permit.

    This is a strict static representation of Phase 5's dynamic
    ``shared_bidirectional`` capacity contract: only disjoint slot sets may be
    combined, and the exact minimum number of pairs required by lane capacity
    is selected with a certified maximum-cardinality feasibility check followed
    by deterministic min-cost matching.
    """

    forward = sorted(
        key for key in grouped if key[0] == link_id and key[1] == "a_to_b"
    )
    reverse = sorted(
        key for key in grouped if key[0] == link_id and key[1] == "b_to_a"
    )
    all_keys = forward + reverse
    required_pairs = max(0, len(all_keys) - lane_count)
    group_tdm = {
        key: _group_ratio_slots(key, grouped[key], schedule_by_id)
        for key in all_keys
    }
    endpoint_y = {
        key: _group_endpoint_y(grouped[key], key[1]) for key in all_keys
    }

    forced_pairs: list[Tuple[Tuple[str, str, int], Tuple[str, str, int]]] = []
    forward_by_lane = {
        fixed_lane_by_group[key]: key
        for key in forward
        if key in fixed_lane_by_group
    }
    reverse_by_lane = {
        fixed_lane_by_group[key]: key
        for key in reverse
        if key in fixed_lane_by_group
    }
    if len(forward_by_lane) != sum(
        key in fixed_lane_by_group for key in forward
    ) or len(reverse_by_lane) != sum(
        key in fixed_lane_by_group for key in reverse
    ):
        raise ValidationError(
            "timing guard assigns two same-direction groups to one shared lane"
        )
    for lane in sorted(set(forward_by_lane) & set(reverse_by_lane)):
        lhs, rhs = forward_by_lane[lane], reverse_by_lane[lane]
        lhs_ratio, lhs_slots = group_tdm[lhs]
        rhs_ratio, rhs_slots = group_tdm[rhs]
        if lhs_ratio != rhs_ratio or lhs_slots & rhs_slots:
            raise ValidationError(
                "timing-guarded opposite directions collide on a shared lane"
            )
        forced_pairs.append((lhs, rhs))

    forced_forward = {lhs for lhs, _rhs in forced_pairs}
    forced_reverse = {rhs for _lhs, rhs in forced_pairs}
    remaining_forward = [key for key in forward if key not in forced_forward]
    remaining_reverse = [key for key in reverse if key not in forced_reverse]

    primary_costs: Dict[Tuple[int, int], int] = {}
    adjacency: list[list[int]] = [[] for _ in remaining_forward]
    for left, lhs in enumerate(remaining_forward):
        lhs_ratio, lhs_slots = group_tdm[lhs]
        lhs_fixed = fixed_lane_by_group.get(lhs)
        for right, rhs in enumerate(remaining_reverse):
            rhs_ratio, rhs_slots = group_tdm[rhs]
            rhs_fixed = fixed_lane_by_group.get(rhs)
            if (
                lhs_ratio != rhs_ratio
                or lhs_slots & rhs_slots
                or (
                    lhs_fixed is not None
                    and rhs_fixed is not None
                    and lhs_fixed != rhs_fixed
                )
            ):
                continue
            adjacency[left].append(right)
            primary_costs[(left, right)] = int(
                math.floor(
                    (
                        abs(endpoint_y[lhs][0] - endpoint_y[rhs][0])
                        + abs(endpoint_y[lhs][1] - endpoint_y[rhs][1])
                    )
                    * 1000.0
                    + 0.5
                )
            )

    maximum_left, _maximum_right = _maximum_bipartite_matching(
        adjacency, len(remaining_reverse)
    )
    maximum_additional = sum(right >= 0 for right in maximum_left)
    additional_target = max(0, required_pairs - len(forced_pairs))
    if maximum_additional < additional_target:
        raise ValidationError(
            "shared-bidirectional Chimew groups cannot fit the physical lane "
            f"budget: groups={len(all_keys)}, lanes={lane_count}, "
            f"required_pairs={required_pairs}, "
            f"maximum_compatible_pairs={len(forced_pairs) + maximum_additional}"
        )
    weighted_adjacency = [
        [(right, primary_costs[(left, right)]) for right in row]
        for left, row in enumerate(adjacency)
    ]
    selected = _minimum_cost_pairs(
        weighted_adjacency, len(remaining_reverse), additional_target
    )
    pairs = forced_pairs + [
        (remaining_forward[left], remaining_reverse[right])
        for left, right in selected
    ]
    pairs.sort()
    paired_keys = {key for pair in pairs for key in pair}

    materialized: Dict[Tuple[str, str, int], list[Dict[str, Any]]] = {}
    materialized_fixed: Dict[Tuple[str, str, int], int] = {}
    sources: Dict[Tuple[str, str, int], list[Tuple[str, str, int]]] = {}
    for key in sorted(set(all_keys) - paired_keys):
        members = [{**member, "direction": key[1]} for member in grouped[key]]
        materialized[key] = members
        sources[key] = [key]
        if key in fixed_lane_by_group:
            materialized_fixed[key] = fixed_lane_by_group[key]
    for bundle_id, (lhs, rhs) in enumerate(pairs):
        key = (link_id, "bidirectional", bundle_id)
        materialized[key] = sorted(
            [
                *({**member, "direction": lhs[1]} for member in grouped[lhs]),
                *({**member, "direction": rhs[1]} for member in grouped[rhs]),
            ],
            key=lambda member: member["id"],
        )
        sources[key] = [lhs, rhs]
        fixed = {
            fixed_lane_by_group[source]
            for source in (lhs, rhs)
            if source in fixed_lane_by_group
        }
        if len(fixed) > 1:
            raise ValidationError(
                "shared-bidirectional bundle has conflicting fixed lanes"
            )
        if fixed:
            materialized_fixed[key] = next(iter(fixed))
    return materialized, materialized_fixed, sources, {
        "required_pairs": required_pairs,
        "maximum_compatible_pairs": len(forced_pairs) + maximum_additional,
        "selected_pairs": len(pairs),
        "forced_pairs": len(forced_pairs),
    }


def materialize_academic_chimew_inputs(
    *,
    ir_path: Path,
    schedule_path: Path,
    routes_path: Path,
    platform_path: Path,
    physical_report: Mapping[str, Any],
    output_dir: Path,
    timing_paths_path: Optional[Path] = None,
    timing_weight_scale: float = 9.0,
    timing_path_scope: str = "exact-hop",
    region_count: int = 4,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
) -> Dict[str, Any]:
    """Build source-bound academic Chimew inputs from a baseline prepass."""

    if not 2 <= region_count <= 31:
        raise ValidationError("academic Chimew region count must be in [2, 31]")
    ir = read_json(ir_path)
    source_schedule = read_json(schedule_path)
    explicit_ratios = [
        "tdm_ratio" in entry for entry in source_schedule.get("entries", [])
    ]
    if any(explicit_ratios) and not all(explicit_ratios):
        raise ValidationError(
            "academic Chimew schedule mixes explicit and implicit TDM ratios"
        )
    schedule = (
        source_schedule
        if explicit_ratios and all(explicit_ratios)
        else materialize_chimew_schedule_ratios(source_schedule)
    )
    platform = Platform.load(platform_path)
    routes = read_json(routes_path)
    timing_weights, timing_source, timing_coverage = _timing_weights(
        timing_paths_path,
        schedule,
        routes,
        scale=timing_weight_scale,
        path_scope=timing_path_scope,
    )
    link_by_id = {link.id: link for link in platform.links}
    (
        locations,
        fpga_y_bounds,
        boundary_locations,
        placement_source,
        architecture_source,
    ) = _instance_locations(physical_report, output_dir)
    placement_mapping_records = read_json(placement_source)["fpgas"]
    total_placed_source_instances = sum(
        record["atom_mapping"]["placed_source_instances"]
        for record in placement_mapping_records
    )
    total_source_instances = sum(
        record["atom_mapping"]["source_instances"]
        for record in placement_mapping_records
    )
    total_unmapped_helper_atoms = sum(
        record["atom_mapping"]["unmapped_helper_atoms"]
        for record in placement_mapping_records
    )
    placement_sha = _sha256(placement_source)
    architecture_sha = _sha256(architecture_source)
    net_by_id = {net["id"]: net for net in ir["nets"]}
    fpga_order = {fpga.id: index for index, fpga in enumerate(platform.fpgas)}
    coordinate_scale = 1.0 + max(
        point[0]
        for per_fpga in locations.values()
        for point in per_fpga.values()
    )
    entry_points: Dict[str, Dict[str, Any]] = {}
    fallbacks = 0
    forwarded_boundary_endpoints = 0
    for entry in schedule.get("entries", []):
        net = net_by_id.get(entry.get("net"))
        if net is None:
            raise ValidationError(
                f"schedule entry {entry.get('id')!r} references an unknown net"
            )
        drivers = [
            endpoint["instance"]
            for endpoint in net["drivers"]
            if endpoint.get("instance") is not None
        ]
        sinks = [
            endpoint["instance"]
            for endpoint in net["sinks"]
            if endpoint.get("instance") is not None
        ]
        source_x, source_y, source_norm_y, source_fallback = _centroid(
            entry["from"], drivers, locations
        )
        if source_fallback:
            boundary = boundary_locations.get(entry["from"], {}).get(entry["id"])
            if boundary is not None:
                source_x, source_y, _source_norm_x, source_norm_y = boundary
                source_fallback = False
                forwarded_boundary_endpoints += 1
        sink_x, sink_y, sink_norm_y, sink_fallback = _centroid(
            entry["to"], sinks, locations
        )
        if sink_fallback:
            boundary = boundary_locations.get(entry["to"], {}).get(entry["id"])
            if boundary is not None:
                sink_x, sink_y, _sink_norm_x, sink_norm_y = boundary
                sink_fallback = False
                forwarded_boundary_endpoints += 1
        fallbacks += int(source_fallback) + int(sink_fallback)
        # Separate FPGA canvases in x while preserving physical-site y.
        source_x += fpga_order[entry["from"]] * coordinate_scale
        sink_x += fpga_order[entry["to"]] * coordinate_scale
        if source_x == sink_x:
            sink_x += 0.25
        entry_points[entry["id"]] = {
            "source": (source_x, source_y),
            "sink": (sink_x, sink_y),
            "source_normalised_y": source_norm_y,
            "sink_normalised_y": sink_norm_y,
            "fallback": source_fallback or sink_fallback,
        }

    routing_source = output_dir / "sources" / "routing.json"
    write_json(
        routing_source,
        {
            "schema": "emuflow.academic-lookahead-routing-source/v1",
            "provider": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "routes_sha256": _sha256(routes_path),
            "placement_sha256": placement_sha,
            "architecture_sha256": architecture_sha,
            "routes": routes,
        },
    )
    routing_sha = _sha256(routing_source)

    crossing_entries = []
    total_crossings = 0
    sll_count = region_count - 1
    for entry in schedule["entries"]:
        source_y = entry_points[entry["id"]]["source_normalised_y"]
        sink_y = entry_points[entry["id"]]["sink_normalised_y"]
        # Virtual academic package banks are evenly distributed by the
        # existing logical lane.  These are lookahead cuts, not final SLLs.
        link = link_by_id[entry["link"]]
        lanes = link.transport_bits_per_cycle_per_direction
        anchor = 0.5 if lanes == 1 else float(entry["lane"]) / float(lanes - 1)
        source_slls = _crossed_boundaries(source_y, anchor, sll_count)
        sink_slls = _crossed_boundaries(sink_y, anchor, sll_count)
        encoding = sum(1 << value for value in source_slls) | sum(
            1 << (sll_count + value) for value in sink_slls
        )
        total_crossings += len(source_slls) + len(sink_slls)
        crossing_entries.append(
            {
                "schedule_entry": entry["id"],
                "source_slls": source_slls,
                "sink_slls": sink_slls,
                "encoding": encoding,
            }
        )
    crossings = {
        "schema": CHIMEW_CROSSING_SCHEMA,
        "provider": CHIMEW_ACADEMIC_CROSSING_PROVIDER,
        "qualification": "academic-virtual-region-lookahead",
        "coordinate_system": "normalized-placement-y",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "slls_per_fpga": sll_count,
        "provenance": {
            "producer": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "routing_sha256": routing_sha,
            "claim_boundary": (
                "virtual regions derived from normalized open-placement "
                "coordinates; not device SLR/SLL closure"
            ),
        },
        "metrics": {
            "signals": len(crossing_entries),
            "physical_sll_crossings": total_crossings,
        },
        "entries": crossing_entries,
    }
    positions = {
        "schema": CHIMEW_POSITION_SCHEMA,
        "provider": CHIMEW_POSITION_PROVIDER,
        "qualification": "academic-open-placement-lookahead",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "coordinate_system": "physical-site-y",
        "provenance": {
            "producer": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "placement_sha256": placement_sha,
        },
        "metrics": {"signals": len(entry_points)},
        "entries": [
            {
                "schedule_entry": entry["id"],
                "source_y": entry_points[entry["id"]]["source"][1],
            }
            for entry in schedule["entries"]
        ],
    }
    maximum_timing_weight = max(timing_weights.values(), default=1.0)
    protected_entries = (
        {
            entry_id
            for entry_id, weight in timing_weights.items()
            if weight > 1.0 and not math.isclose(weight, 1.0, abs_tol=1.0e-12)
        }
        if timing_source is not None
        else set()
    )
    fixed_lane_entries, relaxed_shared_bidirectional_lanes = _static_guardable_entries(
        schedule,
        link_by_id,
        protected_entries,
    )
    if protected_entries:
        schedule["chimew_timing_guard"] = {
            "provider": CHIMEW_TIMING_GUARD_PROVIDER,
            "scope": "EmuFlow extension, not a Chimew paper claim",
            "source_sha256": _sha256(timing_source),
            "maximum_weight": maximum_timing_weight,
            "minimum_protected_weight": min(
                timing_weights[entry_id] for entry_id in protected_entries
            ),
            "protected_entries": sorted(protected_entries),
            "fixed_lane_entries": sorted(fixed_lane_entries),
            "relaxed_shared_bidirectional_entries": sorted(
                protected_entries - fixed_lane_entries
            ),
            "relaxed_shared_bidirectional_lanes": relaxed_shared_bidirectional_lanes,
        }
    initial = build_chimew_initial_groups(
        schedule,
        crossings,
        executable=grouper,
    )
    refined = refine_chimew_groups(
        schedule, crossings, initial, positions, executable=refiner
    )
    group_by_entry, timing_guard_lane_bundles = _coalesce_timing_guard_lanes(
        schedule, refined, fixed_lane_entries
    )

    grouped: Dict[Tuple[str, str, int], list[Dict[str, Any]]] = defaultdict(list)
    for entry in schedule["entries"]:
        link = link_by_id[entry["link"]]
        endpoint_a, endpoint_b = link.endpoints
        direction = (
            "a_to_b"
            if (entry["from"], entry["to"]) == (endpoint_a, endpoint_b)
            else "b_to_a"
        )
        # Keep direction in the grouping identity even when the BoardDB shares
        # capacity across directions.  A Chimew group itself always has one
        # source and one sink direction; only the downstream physical-channel
        # assignment domain changes with the BoardDB capacity contract.
        key = (entry["link"], direction, group_by_entry[entry["id"]])
        source = entry_points[entry["id"]]["source"]
        sink = entry_points[entry["id"]]["sink"]
        grouped[key].append(
            {
                "id": entry["id"],
                "fanout": {"x": source[0], "y": source[1]},
                "fanins": [{"x": sink[0], "y": sink[1]}],
                **(
                    {"timing_weight": timing_weights[entry["id"]]}
                    if timing_source is not None
                    else {}
                ),
            }
        )

    schedule_by_id = {entry["id"]: entry for entry in schedule["entries"]}
    protected_lane_keys = {
        (
            schedule_by_id[entry_id]["link"],
            schedule_by_id[entry_id]["from"],
            schedule_by_id[entry_id]["to"],
            schedule_by_id[entry_id]["lane"],
        )
        for entry_id in fixed_lane_entries
    }
    fixed_lane_by_group: Dict[Tuple[str, str, int], int] = {}
    for key, members in grouped.items():
        member_lanes = {
            (
                schedule_by_id[member["id"]]["link"],
                schedule_by_id[member["id"]]["from"],
                schedule_by_id[member["id"]]["to"],
                schedule_by_id[member["id"]]["lane"],
            )
            for member in members
        }
        guarded = member_lanes & protected_lane_keys
        if guarded:
            if len(member_lanes) != 1 or len(guarded) != 1:
                raise ValidationError(
                    "academic Chimew timing guard did not preserve an exact lane"
                )
            fixed_lane_by_group[key] = next(iter(guarded))[3]

    materialized_grouped: Dict[
        Tuple[str, str, int], list[Dict[str, Any]]
    ] = {}
    materialized_fixed_lanes: Dict[Tuple[str, str, int], int] = {}
    source_groups_by_materialized: Dict[
        Tuple[str, str, int], list[Tuple[str, str, int]]
    ] = {}
    shared_bundle_metrics = {
        "required_pairs": 0,
        "maximum_compatible_pairs": 0,
        "selected_pairs": 0,
        "forced_pairs": 0,
    }
    for link_id in sorted({key[0] for key in grouped}):
        link = link_by_id[link_id]
        if link.capacity_sharing == "shared_bidirectional":
            bundled, bundled_fixed, bundled_sources, bundle_metrics = (
                _bundle_shared_bidirectional_groups(
                    link_id=link_id,
                    lane_count=link.transport_bits_per_cycle_per_direction,
                    grouped=grouped,
                    fixed_lane_by_group=fixed_lane_by_group,
                    schedule_by_id=schedule_by_id,
                )
            )
            materialized_grouped.update(bundled)
            materialized_fixed_lanes.update(bundled_fixed)
            source_groups_by_materialized.update(bundled_sources)
            for field, value in bundle_metrics.items():
                shared_bundle_metrics[field] += value
        else:
            for key in sorted(key for key in grouped if key[0] == link_id):
                materialized_grouped[key] = [
                    {**member, "direction": key[1]} for member in grouped[key]
                ]
                source_groups_by_materialized[key] = [key]
                if key in fixed_lane_by_group:
                    materialized_fixed_lanes[key] = fixed_lane_by_group[key]
    grouped = materialized_grouped
    fixed_lane_by_group = materialized_fixed_lanes

    domains = []
    bank_pairs = []
    channels = []
    package_records = []
    assignment_domain_by_group: Dict[Tuple[str, str, int], str] = {}
    for link_id in sorted({key[0] for key in grouped}):
        link = link_by_id[link_id]
        endpoint_a, endpoint_b = link.endpoints
        if link.direction != "full_duplex":
            raise ValidationError(
                "academic Chimew default currently requires full-duplex "
                f"BoardDB links; {link_id!r} is incompatible"
            )
        lane_count = link.transport_bits_per_cycle_per_direction
        a_y_min, a_y_max = fpga_y_bounds[endpoint_a]
        b_y_min, b_y_max = fpga_y_bounds[endpoint_b]
        active_directions = sorted(
            {key[1] for key in grouped if key[0] == link_id}
        )
        if link.capacity_sharing == "per_direction":
            # A full-duplex/per-direction link exposes an independent lane
            # budget in each direction, so opposite directions may reuse the
            # same physical lane index and direction-qualified package pins.
            domain_partitions = [
                (
                    direction,
                    direction,
                    sorted(
                        key
                        for key in grouped
                        if key[:2] == (link_id, direction)
                    ),
                )
                for direction in active_directions
            ]
        elif link.capacity_sharing == "shared_bidirectional":
            # Contest BoardDBs model one lane pool shared by both directions.
            # Opposite-direction Chimew groups with disjoint Phase-5 slots
            # have already been packed into one bidirectional TDM bundle.
            # Every remaining group/bundle therefore consumes exactly one
            # static direction-agnostic electrical channel.
            domain_partitions = [
                (
                    "shared_bidirectional",
                    "either",
                    sorted(key for key in grouped if key[0] == link_id),
                )
            ]
        else:  # Platform validation should make this unreachable.
            raise ValidationError(
                f"academic Chimew link {link_id!r} has unsupported capacity sharing"
            )
        for domain_qualifier, channel_direction, group_keys in domain_partitions:
            base_domain_id = f"{link_id}:{domain_qualifier}"
            fixed_groups = {
                fixed_lane_by_group[key]: key
                for key in group_keys
                if key in fixed_lane_by_group
            }
            if len(fixed_groups) != sum(
                key in fixed_lane_by_group for key in group_keys
            ):
                raise ValidationError(
                    "academic Chimew timing guard assigned two groups to one lane"
                )
            unguarded_groups = [
                key for key in group_keys if key not in fixed_lane_by_group
            ]
            available_lanes = [
                lane for lane in range(lane_count) if lane not in fixed_groups
            ]
            if len(unguarded_groups) > len(available_lanes):
                raise ValidationError(
                    "academic Chimew timing guard leaves too few assignable lanes"
                )
            domain_specs = []
            if unguarded_groups:
                domain_specs.append(
                    (base_domain_id, unguarded_groups, available_lanes)
                )
            domain_specs.extend(
                (
                    f"{base_domain_id}:timing-guard-lane-{lane:04d}",
                    [group_key],
                    [lane],
                )
                for lane, group_key in sorted(fixed_groups.items())
            )
            for domain_id, domain_groups, domain_lanes in domain_specs:
                guarded_domain = len(domain_groups) == 1 and (
                    domain_groups[0] in fixed_lane_by_group
                )
                guarded_points = None
                if guarded_domain:
                    guarded_members = grouped[domain_groups[0]]
                    endpoint_points = [
                        _group_endpoint_y(
                            [member], member["direction"]
                        )
                        for member in guarded_members
                    ]
                    guarded_points = (
                        sum(point[0] for point in endpoint_points)
                        / len(endpoint_points),
                        sum(point[1] for point in endpoint_points)
                        / len(endpoint_points),
                    )
                for group_key in domain_groups:
                    assignment_domain_by_group[group_key] = domain_id
                bank_a_id = f"academic-{endpoint_a}-{domain_id}-bank"
                bank_b_id = f"academic-{endpoint_b}-{domain_id}-bank"
                domains.append(
                    {
                        "id": domain_id,
                        "fpga_a": endpoint_a,
                        "fpga_b": endpoint_b,
                    }
                )
                raw_channels = []
                for domain_order, lane in enumerate(domain_lanes):
                    channel_id = (
                        f"academic-{link_id}-{domain_qualifier}-channel-{lane:04d}"
                    )
                    fraction = (
                        0.5
                        if lane_count == 1
                        else float(lane) / float(lane_count - 1)
                    )
                    raw_channels.append(
                        {
                            "id": channel_id,
                            "order": domain_order,
                            "pin_a": {
                                "x": fpga_order[endpoint_a] * coordinate_scale,
                                "y": (
                                    guarded_points[0]
                                    if guarded_points is not None
                                    else a_y_min
                                    + fraction * (a_y_max - a_y_min)
                                ),
                            },
                            "pin_b": {
                                "x": fpga_order[endpoint_b] * coordinate_scale,
                                "y": (
                                    guarded_points[1]
                                    if guarded_points is not None
                                    else b_y_min
                                    + fraction * (b_y_max - b_y_min)
                                ),
                            },
                        }
                    )
                    # Per-direction full-duplex capacity has distinct
                    # direction-qualified synthetic pin identities.  Shared
                    # bidirectional capacity has one identity which can serve
                    # either direction, but only one assigned Chimew group.
                    pin_a = (
                        f"ACADEMIC_{endpoint_a}_{link_id}_{domain_qualifier}_P{lane}"
                    )
                    pin_b = (
                        f"ACADEMIC_{endpoint_b}_{link_id}_{domain_qualifier}_P{lane}"
                    )
                    package_records.extend(
                        [
                            {"fpga": endpoint_a, "pin": pin_a},
                            {"fpga": endpoint_b, "pin": pin_b},
                        ]
                    )
                    channels.append(
                        {
                            "chimew_channel": channel_id,
                            "link": link_id,
                            "physical_lane": lane,
                            "direction": channel_direction,
                            "bank_a": bank_a_id,
                            "bank_b": bank_b_id,
                            "package_pin_a": pin_a,
                            "package_pin_b": pin_b,
                            "iostandard": "LVCMOS18",
                            "supported_iostandards": ["LVCMOS18"],
                            "bank_voltage": 1.8,
                            "electrical_class": "single_ended_parallel",
                            "reserved": False,
                            # Academic platforms synthesize virtual package-pin
                            # coordinates for optimization and certification only.
                            # They are not revision-controlled BSP constraints and
                            # therefore must never become fixed Phase 7 I/O targets.
                            "placement_anchor": False,
                        }
                    )
                bank_pairs.append(
                    {
                        "id": f"academic-{domain_id}-bank-pair",
                        "domain": domain_id,
                        "bank_a": {
                            "id": bank_a_id,
                            "x": fpga_order[endpoint_a] * coordinate_scale,
                            "y": (a_y_min + a_y_max) / 2.0,
                        },
                        "bank_b": {
                            "id": bank_b_id,
                            "x": fpga_order[endpoint_b] * coordinate_scale,
                            "y": (b_y_min + b_y_max) / 2.0,
                        },
                        "channels": raw_channels,
                    }
                )

    package_source = output_dir / "sources" / "package-pins.json"
    write_json(
        package_source,
        {
            "schema": "emuflow.academic-virtual-package-pins/v1",
            "qualification": "synthetic-algorithm-validation-only",
            "pins": package_records,
        },
    )
    package_sha = _sha256(package_source)

    group_records = [
        {
            "id": f"academic-{link_id}-{direction}-group-{group_id}",
            "domain": assignment_domain_by_group[
                (link_id, direction, group_id)
            ],
            "kind": "tdm_group",
            "direction": direction,
            "members": members,
            "source_directional_groups": [
                {
                    "link": source[0],
                    "direction": source[1],
                    "group": source[2],
                }
                for source in source_groups_by_materialized[
                    (link_id, direction, group_id)
                ]
            ],
        }
        for (link_id, direction, group_id), members in sorted(grouped.items())
    ]
    bank_input = {
        "schema": CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
        "provider": CHIMEW_BANK_CHANNEL_INPUT_PROVIDER,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "coordinate_system": "physical-site-xy",
        "cost_quantization_per_site": 1000,
        "provenance": {
            "producer": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "grouping_sha256": canonical_sha256(refined),
            "placement_sha256": placement_sha,
            "architecture_sha256": architecture_sha,
        },
        "timing_guard_lane_bundles": timing_guard_lane_bundles,
        "timing_guard_relaxed_shared_bidirectional_lanes": (
            relaxed_shared_bidirectional_lanes
        ),
        "domains": domains,
        "bank_pairs": bank_pairs,
        "groups": group_records,
        "metrics": {
            "groups": len(group_records),
            "timing_guard_fixed_lanes": len(fixed_lane_by_group),
            "timing_guard_lane_bundles": len(timing_guard_lane_bundles),
            "timing_guard_relaxed_shared_bidirectional_lanes": len(
                relaxed_shared_bidirectional_lanes
            ),
            "timing_guard_relaxed_shared_bidirectional_entries": len(
                protected_entries - fixed_lane_entries
            ),
            "timing_guard_coalesced_algorithmic_groups": sum(
                len(bundle["refined_groups"])
                for bundle in timing_guard_lane_bundles
            ),
            "signals": len(schedule["entries"]),
            "fanins": len(schedule["entries"]),
            "bank_pairs": len(bank_pairs),
            "channels": len(channels),
            "bidirectional_bundles": shared_bundle_metrics["selected_pairs"],
            "shared_bidirectional_required_pairs": shared_bundle_metrics[
                "required_pairs"
            ],
            "shared_bidirectional_maximum_compatible_pairs": (
                shared_bundle_metrics["maximum_compatible_pairs"]
            ),
            "shared_bidirectional_forced_pairs": shared_bundle_metrics[
                "forced_pairs"
            ],
        },
    }
    electrical_map = {
        "schema": CHIMEW_ELECTRICAL_MAP_SCHEMA,
        "provider": CHIMEW_ELECTRICAL_MAP_PROVIDER,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provenance": {
            "producer": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "boarddb_sha256": _sha256(platform_path),
            "package_pin_inventory_sha256": package_sha,
        },
        "fpga_y_bounds": [
            {
                "fpga": fpga.id,
                "y_min": fpga_y_bounds[fpga.id][0],
                "y_max": fpga_y_bounds[fpga.id][1],
            }
            for fpga in platform.fpgas
        ],
        "channels": channels,
        "metrics": {
            "channels": len(channels),
            "package_pins": len(package_records),
            "concrete_lanes": len(channels),
        },
    }

    rudy_nets = []
    bbox_guard_points = 0
    for entry in schedule["entries"]:
        source = entry_points[entry["id"]]["source"]
        sink = entry_points[entry["id"]]["sink"]
        pins = [
            {"x": source[0], "y": source[1]},
            {"x": sink[0], "y": sink[1]},
        ]
        if source[1] == sink[1]:
            # Chimew's displayed RUDY equation has no zero-height convention.
            # Add an explicitly reported academic half-site guard point instead
            # of silently changing the paper kernel's reject policy.
            pins.append({"x": (source[0] + sink[0]) / 2.0, "y": source[1] + 0.5})
            bbox_guard_points += 1
        rudy_nets.append({"id": f"academic-{entry['id']}", "pins": pins})
    max_x = max(pin["x"] for net in rudy_nets for pin in net["pins"]) + 1.0
    max_y = max(pin["y"] for net in rudy_nets for pin in net["pins"]) + 1.0
    # The capacity is an explicit academic scaling policy.  It keeps RUDY a
    # comparative metric while avoiding a false real-device capacity claim.
    capacity = max(1.0, float(len(rudy_nets)) * (max_x + max_y) * 4.0)
    rudy_input = {
        "schema": CHIMEW_RUDY_INPUT_SCHEMA,
        "provider": CHIMEW_RUDY_INPUT_PROVIDER,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "coordinate_system": "physical-site-xy",
        "degenerate_bbox_policy": "reject",
        "wire_pitch_per_layer": 1.0,
        "max_utilization": 1.0,
        "provenance": {
            "producer": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "placement_sha256": placement_sha,
            "netlist_sha256": _sha256(ir_path),
            "architecture_sha256": architecture_sha,
        },
        "grid": {
            "origin_x": 0.0,
            "origin_y": 0.0,
            "bin_width": max_x,
            "bin_height": max_y,
            "columns": 1,
            "rows": 1,
            "capacities": [capacity],
        },
        "academic_bbox_guard_points": bbox_guard_points,
        "metrics": {
            "nets": len(rudy_nets),
            "pins": sum(len(net["pins"]) for net in rudy_nets),
        },
        "nets": rudy_nets,
    }

    inputs_dir = output_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "schedule": schedule,
        "crossings": crossings,
        "positions": positions,
        "rudy_input": rudy_input,
        "bank_channel_input": bank_input,
        "electrical_map": electrical_map,
    }
    paths = {}
    for label, document in documents.items():
        path = inputs_dir / f"{label}.json"
        write_json(path, document)
        paths[label] = path
    report = {
        "schema": ACADEMIC_CHIMEW_LOOKAHEAD_SCHEMA,
        "status": "pass",
        "provider": ACADEMIC_CHIMEW_LOOKAHEAD_PROVIDER,
        "qualification": "academic-virtual-physical-model",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "timing_guard": initial["timing_guard"],
        "metrics": {
            "signals": len(schedule["entries"]),
            "placement_endpoint_fallbacks": fallbacks,
            "forwarded_boundary_endpoints": forwarded_boundary_endpoints,
            "placed_source_instances": total_placed_source_instances,
            "source_instances": total_source_instances,
            "unmapped_helper_atoms": total_unmapped_helper_atoms,
            "predicted_sll_crossings": total_crossings,
            "groups": len(group_records),
            "bidirectional_bundles": shared_bundle_metrics["selected_pairs"],
            "shared_bidirectional_required_pairs": shared_bundle_metrics[
                "required_pairs"
            ],
            "shared_bidirectional_maximum_compatible_pairs": (
                shared_bundle_metrics["maximum_compatible_pairs"]
            ),
            "virtual_package_pins": len(package_records),
            "timing_guard_lane_bundles": len(timing_guard_lane_bundles),
            "timing_guard_relaxed_shared_bidirectional_lanes": len(
                relaxed_shared_bidirectional_lanes
            ),
            "timing_guard_relaxed_shared_bidirectional_entries": len(
                protected_entries - fixed_lane_entries
            ),
            "tdm_groups_before_chimew": len(
                {
                    (
                        entry["link"],
                        entry["from"],
                        entry["to"],
                        entry["lane"],
                    )
                    for entry in schedule["entries"]
                }
            ),
        },
        "artifacts": {
            label: {"path": str(path), "sha256": _sha256(path)}
            for label, path in paths.items()
        },
        "sources": {
            "routing": str(routing_source),
            "placement": str(placement_source),
            "netlist": str(ir_path),
            "architecture": str(architecture_source),
            "package_pins": str(package_source),
            **(
                {"timing_paths": str(timing_source)}
                if timing_source is not None
                else {}
            ),
        },
        **(
            {
                "timing_weighting": {
                    "provider": ACADEMIC_CHIMEW_TIMING_WEIGHT_PROVIDER,
                    "source_sha256": _sha256(timing_source),
                    "weighted_signals": sum(
                        weight > 1.0 for weight in timing_weights.values()
                    ),
                    "maximum_weight": max(timing_weights.values()),
                    "weight_scale": timing_weight_scale,
                    "path_scope": timing_path_scope,
                    "exact_path_hops": timing_coverage[
                        "exact_path_hops"
                    ],
                    "whole_net_fallbacks": timing_coverage[
                        "whole_net_fallbacks"
                    ],
                }
            }
            if timing_source is not None
            else {}
        ),
    }
    write_json(output_dir / "academic-chimew-lookahead-report.json", report)
    return report
