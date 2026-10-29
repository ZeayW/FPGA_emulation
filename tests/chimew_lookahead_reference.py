"""Independent fixture oracle for the retired Python lookahead producer.

The virtual region encoding is an explicitly labelled heuristic; it is not a
physical SLR/SLL certificate. All package channels use the existing board wiring
and obey the concrete Phase5 whole-bank direction and ratio/slot contracts.
"""
from __future__ import annotations
import hashlib
from collections import defaultdict
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from emuflow.transport.chimew.chimew_bank_channel import CHIMEW_BANK_CHANNEL_INPUT_PROVIDER, CHIMEW_BANK_CHANNEL_INPUT_SCHEMA
from emuflow.transport.chimew.chimew_bank_channel_storage import write_chimew_bank_channel_storage
from emuflow.transport.chimew.chimew_grouping import CHIMEW_ACADEMIC_CROSSING_PROVIDER, CHIMEW_CROSSING_SCHEMA, CHIMEW_TIMING_GUARD_PROVIDER, build_chimew_initial_groups
from emuflow.transport.chimew.chimew_qualification import canonical_sha256
from emuflow.transport.chimew.chimew_refinement import CHIMEW_POSITION_PROVIDER, CHIMEW_POSITION_SCHEMA, refine_chimew_groups
from emuflow.transport.chimew.chimew_rudy import CHIMEW_RUDY_INPUT_PROVIDER, CHIMEW_RUDY_INPUT_SCHEMA
from tests.chimew_fixed_wiring_reference import build_fixed_wiring_chimew_electrical_map
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json
from emuflow.platform.platform import Platform
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.transport.virtual_board import INVENTORY_PATH
from emuflow.transport.chimew.geometry import XilinxChimewGeometry
from emuflow.transport.chimew.signals import ChimewSignals
XILINX_CHIMEW_LOOKAHEAD_SCHEMA = "emuflow.xilinx-chimew-lookahead/v1"
XILINX_CHIMEW_LOOKAHEAD_PROVIDER = "indexed-xilinx-placement+fixed-lvds-lookahead-v1"
XILINX_CHIMEW_TIMING_WEIGHT_PROVIDER = "partition-projected-sta-criticality-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def _coalesce_timing_guard_lanes(
    schedule: Mapping[str, Any],
    refined: Mapping[str, Any],
    protected_entries: set[str],
) -> Tuple[Dict[str, int], Dict[str, int]]:
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
    if not isinstance(schedule_entries, Sequence) or not isinstance(refined_entries, list):
        raise ValidationError("Xilinx Chimew timing guard inputs are malformed")

    groups_by_entry: Dict[str, int] = {}
    for index, item in enumerate(refined_entries):
        if not isinstance(item, Mapping):
            raise ValidationError(
                f"Xilinx Chimew refinement entry {index} is malformed"
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
                f"Xilinx Chimew refinement entry {index} is invalid"
            )
        groups_by_entry[entry_id] = group

    entries_by_id: Dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(schedule_entries):
        if not isinstance(entry, Mapping):
            raise ValidationError(
                f"Xilinx Chimew schedule entry {index} is malformed"
            )
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in entries_by_id:
            raise ValidationError(
                f"Xilinx Chimew schedule entry {index} has invalid identity"
            )
        entries_by_id[entry_id] = entry

    if set(groups_by_entry) != set(entries_by_id):
        raise ValidationError(
            "Xilinx Chimew refinement does not cover the materialized schedule"
        )
    unknown_protected = protected_entries - set(entries_by_id)
    if unknown_protected:
        raise ValidationError(
            "Xilinx Chimew timing guard references an unknown schedule entry"
        )

    effective_groups = dict(groups_by_entry)
    if not protected_entries:
        return effective_groups, {"lanes":0,"refined_groups":0}

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
            raise ValidationError("Xilinx Chimew timing guard lane is malformed")
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
    refined_group_count = 0
    for lane in sorted(members_by_lane):
        members = members_by_lane[lane]
        native_groups = sorted({groups_by_entry[entry_id] for entry_id in members})
        materialized_group = next_group
        next_group += 1
        for entry_id in members:
            effective_groups[entry_id] = materialized_group
        refined_group_count += len(native_groups)
    return effective_groups, {"lanes":len(members_by_lane), "refined_groups":refined_group_count}

def _fixed_channels(platform, schedule, grouped, fixed_lane_by_group, geometry, fpga_order, coordinate_scale):
    """Expose only eligible wires, with static direction inherited from Phase5."""
    allocations = {}
    for record in schedule.get("bank_allocations", []):
        key = record.get("link"), record.get("from"), record.get("to")
        if key in allocations:
            raise ValidationError("Chimew Phase5 bank allocation is duplicated")
        allocations[key] = record
    expected_allocations = {(link.id, a, b) for link in platform.links
                            for a,b in (tuple(sorted(link.endpoints)),tuple(reversed(sorted(link.endpoints))))}
    if set(allocations) != expected_allocations:
        raise ValidationError("Chimew Phase5 bank directions do not cover the board links")
    for link in platform.links:
        left,right = sorted(link.endpoints)
        forward,reverse = allocations[link.id,left,right], allocations[link.id,right,left]
        if (forward.get("lane_offset") != 0 or reverse.get("lane_offset") != forward.get("lanes")
                or not isinstance(forward.get("lanes"),int) or not isinstance(reverse.get("lanes"),int)
                or forward["lanes"]+reverse["lanes"] != link.transport_bits_per_cycle_per_direction):
            raise ValidationError("Chimew Phase5 direction ranges overlap or omit fixed wires")
    domains, bank_pairs, channels, group_domains, choices = [], [], [], {}, {}
    for link in platform.links:
        if link.bank_count is None or link.wires_per_bank is None or link.capacity_sharing != "shared_bidirectional":
            raise ValidationError("Xilinx Chimew requires fixed shared HP-bank wiring")
        a, b = link.endpoints
        for direction, source, sink in (("a_to_b", a, b), ("b_to_a", b, a)):
            budget = allocations.get((link.id, source, sink))
            if not isinstance(budget, Mapping):
                raise ValidationError("Chimew requires the concrete Phase5 bank direction allocation")
            for name in ("lane_offset", "lanes", "bank_count", "wires_per_bank"):
                if isinstance(budget.get(name), bool) or not isinstance(budget.get(name), int) or budget[name] < 0:
                    raise ValidationError("Chimew Phase5 bank allocation is invalid")
            offset, count = budget["lane_offset"], budget["lanes"]
            if (budget["wires_per_bank"] != link.wires_per_bank
                    or count != budget["bank_count"] * link.wires_per_bank
                    or offset % link.wires_per_bank
                    or offset + count > link.transport_bits_per_cycle_per_direction):
                raise ValidationError("Chimew Phase5 whole-bank wire range disagrees")
            keys = sorted(key for key in grouped if key[:2] == (link.id, direction))
            if not keys:
                continue
            eligible = set(range(offset, offset+count))
            guarded = {fixed_lane_by_group[key]: key for key in keys if key in fixed_lane_by_group}
            if len(guarded) != sum(key in fixed_lane_by_group for key in keys) or set(guarded) - eligible:
                raise ValidationError("Chimew protected lanes conflict with the fixed bank direction")
            free_keys = [key for key in keys if key not in fixed_lane_by_group]
            free_lanes = sorted(eligible-set(guarded))
            if len(free_keys) > len(free_lanes):
                raise ValidationError("Chimew groups exceed the eligible fixed-direction wires")
            partitions = [(f"{link.id}:{direction}", free_keys, free_lanes)] if free_keys else []
            partitions += [(f"{link.id}:{direction}:lane{lane}", [key], [lane]) for lane,key in sorted(guarded.items())]
            for domain, domain_keys, lanes in partitions:
                domains.append({"id": domain, "fpga_a": a, "fpga_b": b})
                group_domains.update((key, domain) for key in domain_keys)
                banks = defaultdict(list)
                for lane in lanes:
                    channel = geometry.fixed.channel(link.id, lane, a, b)
                    banks[channel["bank_a"], channel["bank_b"]].append((lane, channel))
                for (bank_a, bank_b), members in sorted(banks.items()):
                    raw = []
                    for order, (lane, channel) in enumerate(members):
                        identity = f"{link.id}:{direction}:wire{lane}"
                        pa, pb = geometry.pins[a,channel["package_pin_a"]], geometry.pins[b,channel["package_pin_b"]]
                        raw.append({"id": identity, "order": order,
                            "pin_a": {"x": pa[0]+fpga_order[a]*coordinate_scale, "y": pa[1]},
                            "pin_b": {"x": pb[0]+fpga_order[b]*coordinate_scale, "y": pb[1]}})
                        channels.append(identity)
                        choices[identity] = {"link": link.id, "physical_lane": lane, "direction": direction}
                    mean = lambda side,axis: sum(v[f"pin_{side}"][axis] for v in raw)/len(raw)
                    bank_pairs.append({"id": f"{domain}:{bank_a}:{bank_b}", "domain": domain,
                        "bank_a": {"id": bank_a, "x": mean("a","x"), "y": mean("a","y")},
                        "bank_b": {"id": bank_b, "x": mean("b","x"), "y": mean("b","y")}, "channels": raw})
    if set(group_domains) != set(grouped):
        raise ValidationError("Chimew fixed bank domains do not cover every group")
    # The existing electrical validator independently checks disjoint P/N pins
    # and rejects static bank reversals, including forged allocation records.
    return domains, bank_pairs, channels, group_domains, choices

def _materialize_xilinx_chimew_inputs(
    *,
    ir_path: Path,
    schedule_path: Path,
    routes_path: Path,
    platform_path: Path,
    physical_report: Mapping[str, Any],
    geometry: XilinxChimewGeometry,
    source_schedule: Mapping[str, Any],
    signals: ChimewSignals,
    output_dir: Path,
    timing_paths_path: Optional[Path] = None,
    timing_weight_scale: float = 9.0,
    timing_path_scope: str = "exact-hop",
    region_count: int = 4,
    grouper: Optional[str] = None,
    refiner: Optional[str] = None,
) -> Dict[str, Any]:
    """Build source-bound Xilinx Chimew inputs from a baseline prepass."""

    if not 2 <= region_count <= 31:
        raise ValidationError("Xilinx Chimew region count must be in [2, 31]")
    if any("tdm_ratio" not in entry for entry in source_schedule["entries"]):
        raise ValidationError("Xilinx Chimew requires explicit Phase5 ratios")
    schedule = dict(source_schedule)
    platform = Platform.load(platform_path)
    timing_weights = signals.weights
    timing_source = timing_paths_path
    timing_coverage = signals.coverage
    link_by_id = {link.id: link for link in platform.links}
    fpga_y_bounds = {fpga: (bounds[2], bounds[3] if bounds[3] > bounds[2] else bounds[2]+1)
                     for fpga, bounds in geometry.bounds.items()}
    placement_source = output_dir / "sources" / "physical-inputs.json"
    write_json(placement_source, {
        "schema": "emuflow.xilinx-chimew-physical-inputs/v1",
        "coordinate_system": "native-device-tile-column-row-v1",
        "fpgas": geometry.sources,
        "geometry": geometry.reference,
        "signals": signals.reference,
    }, compact=True)
    placement_sha = _sha256(placement_source)
    architecture_source = Path(geometry.architecture["path"])
    architecture_sha = geometry.architecture["sha256"]
    fpga_order = {fpga.id: index for index, fpga in enumerate(platform.fpgas)}
    coordinate_scale = 1.0 + max(bounds[1] for bounds in geometry.bounds.values())
    entry_points = signals.points
    routing_source = routes_path
    routing_sha = _sha256(routes_path)
    crossing_entries = list(signals.crossing_entries())
    total_crossings = sum(len(row['source_slls']) + len(row['sink_slls']) for row in crossing_entries)
    sll_count = region_count - 1
    crossings = {
        "schema": CHIMEW_CROSSING_SCHEMA,
        "provider": CHIMEW_ACADEMIC_CROSSING_PROVIDER,
        "qualification": "academic-virtual-region-lookahead",
        "coordinate_system": "normalized-placement-y",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "slls_per_fpga": sll_count,
        "provenance": {
            "producer": XILINX_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "routing_sha256": routing_sha,
            "claim_boundary": (
                "virtual regions derived from normalized Xilinx-placement "
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
        "qualification": "chimew-Xilinx-placement-lookahead",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "coordinate_system": "physical-site-y",
        "provenance": {
            "producer": XILINX_CHIMEW_LOOKAHEAD_PROVIDER,
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
    protected_entries = signals.protected_entries()
    fixed_lane_entries = protected_entries
    if protected_entries:
        crossings["timing_guard"] = {
            "provider": CHIMEW_TIMING_GUARD_PROVIDER,
            "scope": "EmuFlow extension, not a Chimew paper claim",
            "source_sha256": _sha256(timing_source),
            "maximum_weight": maximum_timing_weight,
            "minimum_protected_weight": min(
                timing_weights[entry_id] for entry_id in protected_entries
            ),
            "protected_entries": sorted(protected_entries),
        }
    initial = build_chimew_initial_groups(
        schedule,
        crossings,
        executable=grouper,
    )
    refined = refine_chimew_groups(
        schedule, crossings, initial, positions, executable=refiner
    )
    group_by_entry, _guard_statistics = _coalesce_timing_guard_lanes(
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
                    "Xilinx Chimew timing guard did not preserve an exact lane"
                )
            fixed_lane_by_group[key] = next(iter(guarded))[3]

    grouped = {key: [{**member, "direction": key[1]} for member in members]
               for key, members in grouped.items()}
    domains, bank_pairs, channels, assignment_domain_by_group, channel_lanes = _fixed_channels(
        platform, schedule, grouped, fixed_lane_by_group, geometry, fpga_order, coordinate_scale)
    package_source = INVENTORY_PATH
    package_sha = _sha256(package_source)

    group_records = [
        {
            "id": f"chimew-{link_id}-{direction}-group-{group_id}",
            "domain": assignment_domain_by_group[
                (link_id, direction, group_id)
            ],
            "kind": "tdm_group",
            "direction": direction,
            "members": members,
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
            "producer": XILINX_CHIMEW_LOOKAHEAD_PROVIDER,
            "producer_version": "1",
            "grouping_sha256": canonical_sha256(refined),
            "placement_sha256": placement_sha,
            "architecture_sha256": architecture_sha,
        },
        "domains": domains,
        "bank_pairs": bank_pairs,
        "groups": group_records,
        "metrics": {
            "groups": len(group_records),
            "timing_guard_fixed_lanes": len(fixed_lane_by_group),
            "signals": len(schedule["entries"]),
            "fanins": len(schedule["entries"]),
            "bank_pairs": len(bank_pairs),
            "channels": len(channels),
        },
    }
    electrical_map = build_fixed_wiring_chimew_electrical_map(
        platform, bank_input, channel_lanes=channel_lanes,
        fpga_y_bounds=[{"fpga": fpga.id, "y_min": fpga_y_bounds[fpga.id][0],
                        "y_max": fpga_y_bounds[fpga.id][1]} for fpga in platform.fpgas],
        boarddb_sha256=_sha256(platform_path))

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
        rudy_nets.append({"id": f"chimew-{entry['id']}", "pins": pins})
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
            "producer": XILINX_CHIMEW_LOOKAHEAD_PROVIDER,
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
        if label == "schedule":
            paths[label] = schedule_path
            continue
        if label == "bank_channel_input":
            indexed_bank_input = write_chimew_bank_channel_storage(path, document)
            indexed_bank_input_store = indexed_bank_input.store_path
            indexed_bank_input.close()
        else:
            write_json(path, document)
        paths[label] = path
    paths["bank_channel_input_store"] = indexed_bank_input_store
    report = {
        "schema": XILINX_CHIMEW_LOOKAHEAD_SCHEMA,
        "status": "pass",
        "provider": XILINX_CHIMEW_LOOKAHEAD_PROVIDER,
        "qualification": "xilinx-placed-lookahead+fixed-lvds-wiring",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "timing_guard": initial["timing_guard"],
        "metrics": {
            "signals": len(schedule["entries"]),
            "placed_boundary_endpoints": geometry.forwarded,



            "predicted_sll_crossings": total_crossings,
            "groups": len(group_records),
            "fixed_package_pins": electrical_map["metrics"]["package_pins"],
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
                    "provider": XILINX_CHIMEW_TIMING_WEIGHT_PROVIDER,
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
    write_json(output_dir / "chimew-lookahead-report.json", report)
    return report
