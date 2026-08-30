"""Bridge certified Chimew assignments to EmuFlow lane/electrical contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_PROVIDER,
    evaluate_chimew_bank_channel_assignment,
    validate_chimew_bank_channel_input,
)
from .chimew_grouping import _tdm_ratio
from .chimew_qualification import (
    canonical_sha256,
    validate_chimew_bank_channel_report_artifact,
    validate_chimew_qualification_binding,
    validate_chimew_qualification_seal,
)
from .errors import ValidationError
from .io import read_json, write_json
from .pin_planning import (
    CHIMEW_PHYSICAL_POSITION_PROVIDER,
    CHIMEW_PIN_PLAN_PROVIDER,
    PIN_PLAN_SCHEMA,
    SIGNAL_POSITION_HINTS_SCHEMA,
    _assignment_metrics,
    _baseline_assignment,
    validate_pin_plan,
)
from .platform import Platform


CHIMEW_ELECTRICAL_MAP_SCHEMA_V1 = "emuflow.chimew-electrical-channel-map/v1"
CHIMEW_ELECTRICAL_MAP_SCHEMA = "emuflow.chimew-electrical-channel-map/v2"
CHIMEW_ELECTRICAL_MAP_PROVIDER = (
    "source-qualified-boarddb-electrical-channel-map-v1"
)
CHIMEW_PHASE6_BINDING_SCHEMA_V1 = "emuflow.chimew-phase6-electrical-binding/v1"
CHIMEW_PHASE6_BINDING_SCHEMA = "emuflow.chimew-phase6-electrical-binding/v2"
CHIMEW_PHASE6_BINDING_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA = "emuflow.chimew-phase6-adapter-report/v1"
_IOSTANDARD_VOLTAGES = {
    "LVCMOS12": 1.2,
    "LVCMOS15": 1.5,
    "LVCMOS18": 1.8,
    "LVCMOS25": 2.5,
    "LVCMOS33": 3.3,
}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}: expected a non-empty string")
    return value


def _digest(value: Any, label: str) -> str:
    digest = _string(value, label).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValidationError(f"{label}: expected a SHA-256 digest")
    return digest


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a number")
    return float(value)


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_chimew_electrical_map(
    document: Mapping[str, Any],
    platform: Platform,
    problem: Mapping[str, Any],
) -> Dict[str, Any]:
    """Check concrete lanes, banks, package pins, and electrical identity."""

    schema = document.get("schema")
    if schema not in {CHIMEW_ELECTRICAL_MAP_SCHEMA_V1, CHIMEW_ELECTRICAL_MAP_SCHEMA}:
        raise ValidationError("Chimew electrical channel-map schema is invalid")
    if document.get("provider") != CHIMEW_ELECTRICAL_MAP_PROVIDER:
        raise ValidationError("Chimew electrical channel map is not source-qualified")
    if (
        document.get("design") != problem["design"]
        or document.get("platform") != platform.name
        or problem["platform"] != platform.name
    ):
        raise ValidationError("Chimew electrical channel-map identity does not agree")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew electrical channel-map provenance is missing")
    normalized_provenance = {
        "producer": _string(provenance.get("producer"), "electrical.producer"),
        "producer_version": _string(
            provenance.get("producer_version"), "electrical.producer_version"
        ),
        "boarddb_sha256": _digest(
            provenance.get("boarddb_sha256"), "electrical.boarddb_sha256"
        ),
        "package_pin_inventory_sha256": _digest(
            provenance.get("package_pin_inventory_sha256"),
            "electrical.package_pin_inventory_sha256",
        ),
    }

    raw_bounds = document.get("fpga_y_bounds")
    if not isinstance(raw_bounds, list):
        raise ValidationError("Chimew electrical FPGA bounds are missing")
    bounds: Dict[str, Tuple[float, float]] = {}
    for index, record in enumerate(raw_bounds):
        if not isinstance(record, dict):
            raise ValidationError(f"electrical.fpga_y_bounds[{index}] is invalid")
        fpga = _string(record.get("fpga"), f"electrical.bounds[{index}].fpga")
        low = _number(record.get("y_min"), f"electrical.bounds[{fpga}].y_min")
        high = _number(record.get("y_max"), f"electrical.bounds[{fpga}].y_max")
        if fpga in bounds or high <= low:
            raise ValidationError("Chimew electrical FPGA bounds are invalid")
        bounds[fpga] = (low, high)
    expected_fpgas = {fpga.id for fpga in platform.fpgas}
    used_fpgas = {
        endpoint
        for domain in problem["domains"]
        for endpoint in (domain["fpga_a"], domain["fpga_b"])
    }
    if set(bounds) != used_fpgas or not used_fpgas <= expected_fpgas:
        raise ValidationError("Chimew electrical FPGA bounds do not cover domains")

    link_by_id = {link.id: link for link in platform.links}
    bank_by_channel = {
        channel["id"]: (bank, channel)
        for bank in problem["banks"]
        for channel in bank["channels"]
    }
    raw_channels = document.get("channels")
    if not isinstance(raw_channels, list):
        raise ValidationError("Chimew electrical channel records are missing")
    channels = {}
    lane_uses: Dict[Tuple[str, int], list[str]] = {}
    used_pins = set()
    for index, record in enumerate(raw_channels):
        if not isinstance(record, dict):
            raise ValidationError(f"electrical.channels[{index}] is invalid")
        channel_id = _string(
            record.get("chimew_channel"), f"electrical.channels[{index}].id"
        )
        if channel_id in channels or channel_id not in bank_by_channel:
            raise ValidationError("Chimew electrical channel identity is invalid")
        bank, assignment_channel = bank_by_channel[channel_id]
        domain = problem["domains"][bank["domain"]]
        link_id = _string(record.get("link"), f"electrical.{channel_id}.link")
        link = link_by_id.get(link_id)
        if link is None or set(link.endpoints) != {
            domain["fpga_a"], domain["fpga_b"]
        }:
            raise ValidationError("Chimew electrical channel link/domain mismatch")
        lane = record.get("physical_lane")
        raw_direction = record.get("direction")
        if schema == CHIMEW_ELECTRICAL_MAP_SCHEMA:
            if raw_direction not in {"a_to_b", "b_to_a", "either"}:
                raise ValidationError(
                    "Chimew electrical channel direction is invalid"
                )
            direction = raw_direction
        else:
            if raw_direction is not None:
                raise ValidationError(
                    "legacy Chimew electrical maps cannot declare a direction"
                )
            direction = "either"
        lane_key = (link_id, lane)
        if (
            isinstance(lane, bool)
            or not isinstance(lane, int)
            or not 0 <= lane < link.transport_bits_per_cycle_per_direction
        ):
            raise ValidationError("Chimew electrical concrete lane is invalid")
        uses = lane_uses.setdefault(lane_key, [])
        direction_qualified_reuse = (
            schema == CHIMEW_ELECTRICAL_MAP_SCHEMA
            and link.direction == "full_duplex"
            and link.capacity_sharing == "per_direction"
        )
        if direction == "either":
            maximum_either_uses = 2 if direction_qualified_reuse else 1
            if (
                any(value != "either" for value in uses)
                or len(uses) >= maximum_either_uses
            ):
                raise ValidationError("Chimew electrical concrete lane is invalid")
        elif (
            "either" in uses
            or direction in uses
            or (uses and not direction_qualified_reuse)
        ):
            raise ValidationError("Chimew electrical concrete lane is invalid")
        uses.append(direction)
        bank_a = _string(record.get("bank_a"), f"electrical.{channel_id}.bank_a")
        bank_b = _string(record.get("bank_b"), f"electrical.{channel_id}.bank_b")
        if bank_a != bank["bank_a"]["id"] or bank_b != bank["bank_b"]["id"]:
            raise ValidationError("Chimew electrical bank identity does not agree")
        pin_a = _string(
            record.get("package_pin_a"), f"electrical.{channel_id}.package_pin_a"
        )
        pin_b = _string(
            record.get("package_pin_b"), f"electrical.{channel_id}.package_pin_b"
        )
        pin_keys = ((domain["fpga_a"], pin_a), (domain["fpga_b"], pin_b))
        if any(key in used_pins for key in pin_keys):
            raise ValidationError("Chimew electrical package-pin collision")
        used_pins.update(pin_keys)
        iostandard = _string(
            record.get("iostandard"), f"electrical.{channel_id}.iostandard"
        )
        supported = record.get("supported_iostandards")
        if (
            not isinstance(supported, list)
            or not supported
            or not all(isinstance(value, str) and value for value in supported)
            or len(set(supported)) != len(supported)
            or iostandard not in supported
        ):
            raise ValidationError("Chimew electrical IOSTANDARD support is invalid")
        if record.get("reserved") is not False:
            raise ValidationError("Chimew electrical channel is reserved")
        placement_anchor = record.get("placement_anchor", True)
        if not isinstance(placement_anchor, bool):
            raise ValidationError("Chimew electrical placement anchor is invalid")
        if record.get("electrical_class") != "single_ended_parallel":
            raise ValidationError(
                "Chimew electrical maps require single-ended parallel channels"
            )
        voltage = _number(
            record.get("bank_voltage"), f"electrical.{channel_id}.bank_voltage"
        )
        expected_voltage = _IOSTANDARD_VOLTAGES.get(iostandard)
        if expected_voltage is None or abs(voltage - expected_voltage) > 1.0e-9:
            raise ValidationError("Chimew electrical bank voltage/IOSTANDARD mismatch")
        channels[channel_id] = {
            "link": link_id,
            "physical_lane": lane,
            "direction": direction,
            "fpga_a": domain["fpga_a"],
            "fpga_b": domain["fpga_b"],
            "bank_a": bank_a,
            "bank_b": bank_b,
            "package_pin_a": pin_a,
            "package_pin_b": pin_b,
            "iostandard": iostandard,
            "supported_iostandards": sorted(supported),
            "bank_voltage": voltage,
            "electrical_class": "single_ended_parallel",
            "placement_anchor": placement_anchor,
            # Preserve the optimizer's physical site coordinates all the way
            # into the sealed Phase-6 binding.  The concrete lane number is
            # an electrical identity, not a device-placement coordinate.
            "pin_a_point": {
                "x": assignment_channel["pin_a"][0],
                "y": assignment_channel["pin_a"][1],
            },
            "pin_b_point": {
                "x": assignment_channel["pin_b"][0],
                "y": assignment_channel["pin_b"][1],
            },
        }
    if set(channels) != set(bank_by_channel):
        raise ValidationError("Chimew electrical channel coverage is incomplete")
    metrics = document.get("metrics")
    expected_metrics = {
        "channels": len(channels),
        "package_pins": len(used_pins),
        "concrete_lanes": sum(len(uses) for uses in lane_uses.values()),
    }
    if not isinstance(metrics, dict) or any(
        metrics.get(field) != value for field, value in expected_metrics.items()
    ):
        raise ValidationError("Chimew electrical channel-map metrics do not agree")
    return {
        "provenance": normalized_provenance,
        "bounds": bounds,
        "channels": channels,
        "metrics": expected_metrics,
        "map_sha256": _canonical_sha256(document),
    }


def _normalized_y(y: float, bounds: Tuple[float, float], label: str) -> float:
    low, high = bounds
    tolerance = 1.0e-9 * max(1.0, abs(low), abs(high))
    if y < low - tolerance or y > high + tolerance:
        raise ValidationError(f"{label}: physical y lies outside FPGA bounds")
    return min(1.0, max(0.0, (y - low) / (high - low)))


def build_chimew_phase6_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    bank_channel_input: Mapping[str, Any],
    electrical_map: Mapping[str, Any],
    *,
    qualification_document: Optional[Mapping[str, Any]] = None,
    bank_channel_report_document: Optional[Mapping[str, Any]] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
) -> Dict[str, Any]:
    """Run certified Chimew assignment and bind it to concrete EmuFlow lanes."""

    if not 1 <= region_count <= 31:
        raise ValueError("region_count must be in [1, 31]")
    problem = validate_chimew_bank_channel_input(bank_channel_input)
    qualification_validation = None
    if qualification_document is not None:
        qualification_validation = validate_chimew_qualification_binding(
            qualification_document, schedule, bank_channel_input
        )
    electrical = validate_chimew_electrical_map(
        electrical_map, platform, problem
    )
    if (
        qualification_validation is not None
        and qualification_validation["qualification_scope"]
        == "byte-bound-source-artifacts"
    ):
        source_binding = qualification_document.get("source_binding")
        if (
            not isinstance(source_binding, dict)
            or not isinstance(source_binding.get("digests"), dict)
            or source_binding["digests"].get("package_pins")
            != electrical["provenance"]["package_pin_inventory_sha256"]
        ):
            raise ValidationError(
                "Chimew byte-bound package-pin provenance does not agree"
            )
    if bank_channel_report_document is not None:
        if qualification_document is None:
            raise ValidationError(
                "a precomputed Chimew assignment report requires qualification"
            )
        validate_chimew_bank_channel_report_artifact(
            bank_channel_input, bank_channel_report_document
        )
        report = dict(bank_channel_report_document)
    else:
        report = evaluate_chimew_bank_channel_assignment(
            bank_channel_input, executable=executable
        )
    if qualification_document is not None and qualification_document[
        "artifacts"
    ].get("bank_channel_report") != canonical_sha256(report):
        raise ValidationError(
            "Chimew assignment report does not match qualification certificate"
        )
    if report["provider"] != CHIMEW_BANK_CHANNEL_PROVIDER:
        raise ValidationError("Chimew assignment provider is invalid")
    schedule_by_id = {entry["id"]: entry for entry in schedule.get("entries", [])}
    if len(schedule_by_id) != len(schedule.get("entries", [])):
        raise ValidationError("schedule entry identities are duplicated")
    if (
        schedule.get("design") != problem["design"]
        or schedule.get("platform") != platform.name
    ):
        raise ValidationError("Chimew assignment does not match schedule identity")
    report_assignment = {row["group"]: row for row in report["assignments"]}
    if len(report_assignment) != len(report["assignments"]):
        raise ValidationError("Chimew assignment duplicates groups")

    plan_assignment: Dict[str, Tuple[int, int]] = {}
    hint_entries = []
    binding_entries = []
    used_schedule_entries = set()
    used_package_pins = set()
    for group_index, group in enumerate(problem["groups"]):
        assignment = report_assignment.get(group["id"])
        if assignment is None:
            raise ValidationError("Chimew assignment does not cover all groups")
        channel = electrical["channels"].get(assignment["channel"])
        if channel is None:
            raise ValidationError("Chimew assignment has no electrical channel")
        domain = problem["domains"][group["domain"]]
        group_direction = (
            "a_to_b"
            if group["direction"] == 0
            else "b_to_a" if group["direction"] == 1 else "bidirectional"
        )
        if (
            group_direction == "bidirectional"
            and channel["direction"] != "either"
        ) or (
            group_direction != "bidirectional"
            and channel["direction"] not in {"either", group_direction}
        ):
            raise ValidationError("Chimew assignment electrical direction mismatch")
        if (channel["fpga_a"], channel["fpga_b"]) != (
            domain["fpga_a"], domain["fpga_b"]
        ):
            raise ValidationError("Chimew assignment electrical domain mismatch")
        member_ids = []
        ratios_by_direction = {"a_to_b": set(), "b_to_a": set()}
        count_by_direction = {"a_to_b": 0, "b_to_a": 0}
        slots = set()
        member_directions = set()
        for member in group["members"]:
            entry_id = member["id"]
            entry = schedule_by_id.get(entry_id)
            if entry is None or entry_id in used_schedule_entries:
                raise ValidationError("Chimew members do not cover unique schedule entries")
            member_direction = (
                "a_to_b" if member["direction"] == 0 else "b_to_a"
            )
            source = (
                domain["fpga_a"]
                if member_direction == "a_to_b"
                else domain["fpga_b"]
            )
            sink = (
                domain["fpga_b"]
                if member_direction == "a_to_b"
                else domain["fpga_a"]
            )
            if (
                entry.get("link") != channel["link"]
                or entry.get("from") != source
                or entry.get("to") != sink
            ):
                raise ValidationError("Chimew member schedule domain does not agree")
            ratio = _tdm_ratio(entry)
            slot = entry.get("slot")
            if (
                isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot < 0
                or slot in slots
            ):
                raise ValidationError(
                    "Chimew group has a TDM slot collision: "
                    f"group={group['id']!r}, entry={entry_id!r}, "
                    f"ratio={ratio!r}, slot={slot!r}, prior_slots={sorted(slots)!r}"
                )
            ratios_by_direction[member_direction].add(ratio)
            count_by_direction[member_direction] += 1
            slots.add(slot)
            member_directions.add(member_direction)
            used_schedule_entries.add(entry_id)
            member_ids.append(entry_id)
            plan_assignment[entry_id] = (group_index, channel["physical_lane"])
            source_y = _normalized_y(
                member["fanout"][1], electrical["bounds"][source], f"{entry_id}.source_y"
            )
            sink_physical_y = sum(point[1] for point in member["fanins"]) / len(
                member["fanins"]
            )
            sink_y = _normalized_y(
                sink_physical_y, electrical["bounds"][sink], f"{entry_id}.sink_y"
            )
            hint_entries.append(
                {
                    "schedule_entry": entry_id,
                    "source_y": source_y,
                    "sink_y": sink_y,
                    "source_region": min(region_count - 1, int(source_y * region_count)),
                    "sink_region": min(region_count - 1, int(sink_y * region_count)),
                    "source_fallback": False,
                    "sink_fallback": False,
                }
            )
        if (
            any(
                len(ratios_by_direction[direction]) != 1
                or count_by_direction[direction]
                > next(iter(ratios_by_direction[direction]))
                for direction in member_directions
            )
            or (
                group_direction == "bidirectional"
                and member_directions != {"a_to_b", "b_to_a"}
            )
            or (
                group_direction != "bidirectional"
                and member_directions != {group_direction}
            )
        ):
            raise ValidationError("Chimew group violates schedule TDM capacity")
        package_pin_keys = (
            (channel["fpga_a"], channel["package_pin_a"]),
            (channel["fpga_b"], channel["package_pin_b"]),
        )
        if any(key in used_package_pins for key in package_pin_keys):
            raise ValidationError("selected Chimew groups collide on package pins")
        used_package_pins.update(package_pin_keys)
        binding_entries.append(
            {
                "group": group["id"],
                "schedule_entries": sorted(member_ids),
                "bank_pair": assignment["bank_pair"],
                "channel": assignment["channel"],
                **channel,
                "direction": group_direction,
            }
        )
    if used_schedule_entries != set(schedule_by_id):
        raise ValidationError("Chimew groups do not cover the schedule exactly")

    positions = {
        "schema": SIGNAL_POSITION_HINTS_SCHEMA,
        "design": schedule["design"],
        "platform": platform.name,
        "provider": CHIMEW_PHYSICAL_POSITION_PROVIDER,
        "source_coordinate_system": "physical-site-xy",
        "region_count": region_count,
        "provenance": {
            "producer": problem["provenance"]["producer"],
            "producer_version": problem["provenance"]["producer_version"],
            "assignment_input_sha256": report["input_sha256"],
            "placement_sha256": problem["provenance"]["placement_sha256"],
            "architecture_sha256": problem["provenance"]["architecture_sha256"],
        },
        "metrics": {
            "signals": len(hint_entries),
            "endpoint_centroid_fallbacks": 0,
        },
        "entries": sorted(hint_entries, key=lambda item: item["schedule_entry"]),
    }
    weights = {"crossing": 1.0, "position": 1.0}
    schedule_sha256 = _canonical_sha256(schedule)
    metrics = _assignment_metrics(
        schedule, platform, positions, plan_assignment, 1.0, 1.0
    )
    baseline = _assignment_metrics(
        schedule, platform, positions, _baseline_assignment(schedule), 1.0, 1.0
    )
    plan = {
        "schema": PIN_PLAN_SCHEMA,
        "design": schedule["design"],
        "platform": platform.name,
        "provider": CHIMEW_PIN_PLAN_PROVIDER,
        "configuration": {
            "refinement_iterations": 0,
            "paper_assignment_provider": report["provider"],
            "electrical_map_sha256": electrical["map_sha256"],
            "schedule_sha256": schedule_sha256,
            "lookahead_qualification": (
                "complete-artifact-chain"
                if qualification_validation is not None
                else "bank-electrical-only"
            ),
            **(
                {
                    "qualification_sha256": qualification_validation[
                        "qualification_sha256"
                    ],
                    "qualification_scope": qualification_validation[
                        "qualification_scope"
                    ],
                }
                if qualification_validation is not None
                else {}
            ),
        },
        "weights": weights,
        "metrics": {
            "signals": len(plan_assignment),
            "groups": len(problem["groups"]),
            **metrics,
            **{
                f"baseline_logical_lane_{field}": value
                for field, value in baseline.items()
            },
        },
        "entries": [
            {
                "schedule_entry": entry["id"],
                "group": plan_assignment[entry["id"]][0],
                "physical_lane": plan_assignment[entry["id"]][1],
                "logical_lane": entry["lane"],
            }
            for entry in sorted(schedule["entries"], key=lambda item: item["id"])
        ],
    }
    validation = validate_pin_plan(schedule, platform, positions, plan)
    binding = {
        "schema": CHIMEW_PHASE6_BINDING_SCHEMA,
        "status": "pass",
        "integration_status": "phase6-pin-plan",
        "design": schedule["design"],
        "platform": platform.name,
        "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "paper_provider": report["provider"],
        "extension_scope": (
            "EmuFlow concrete lane, bank voltage, IOSTANDARD, and package-pin legality"
        ),
        "fpga_y_bounds": [
            {"fpga": fpga, "y_min": bounds[0], "y_max": bounds[1]}
            for fpga, bounds in sorted(electrical["bounds"].items())
        ],
        "provenance": {
            **electrical["provenance"],
            "electrical_map_sha256": electrical["map_sha256"],
            "assignment_input_sha256": report["input_sha256"],
            "schedule_sha256": schedule_sha256,
            **(
                {
                    "qualification_sha256": qualification_validation[
                        "qualification_sha256"
                    ],
                    "qualification_scope": qualification_validation[
                        "qualification_scope"
                    ],
                }
                if qualification_validation is not None
                else {}
            ),
        },
        **(
            {"lookahead_qualification": dict(qualification_document)}
            if qualification_document is not None
            else {}
        ),
        "metrics": {
            "signals": len(plan_assignment),
            "groups": len(problem["groups"]),
            "channels": len(binding_entries),
            "package_pins": len(used_package_pins),
            "lane_slot_collisions": validation["physical_lane_slot_collisions"],
            "package_pin_collisions": 0,
        },
        "entries": sorted(binding_entries, key=lambda item: item["group"]),
    }
    # The assignment channels may be direction-agnostic.  Validate the final,
    # direction-resolved binding before returning so a same-direction lane
    # collision can never escape the build path as a nominal pass.
    validate_chimew_phase6_binding(schedule, platform, plan, binding)
    return {
        "status": "pass",
        "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "bank_channel_report": report,
        "position_hints": positions,
        "pin_plan": plan,
        "electrical_binding": binding,
        "qualification_validation": qualification_validation,
        "validation": validation,
    }


def validate_chimew_phase6_binding(
    schedule: Mapping[str, Any],
    platform: Platform,
    pin_plan: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> Dict[str, Any]:
    """Independently bind a Chimew pin plan to its electrical certificate."""

    binding_schema = binding.get("schema")
    if binding_schema not in {
        CHIMEW_PHASE6_BINDING_SCHEMA_V1,
        CHIMEW_PHASE6_BINDING_SCHEMA,
    }:
        raise ValidationError("Chimew Phase 6 electrical binding schema is invalid")
    if (
        binding.get("status") != "pass"
        or binding.get("integration_status") != "phase6-pin-plan"
        or binding.get("provider") != CHIMEW_PHASE6_BINDING_PROVIDER
        or pin_plan.get("provider") != CHIMEW_PIN_PLAN_PROVIDER
    ):
        raise ValidationError("Chimew Phase 6 electrical binding provider is invalid")
    if (
        binding.get("design") != schedule.get("design")
        or binding.get("platform") != platform.name
        or pin_plan.get("design") != schedule.get("design")
        or pin_plan.get("platform") != platform.name
    ):
        raise ValidationError("Chimew Phase 6 electrical binding identity differs")
    provenance = binding.get("provenance")
    configuration = pin_plan.get("configuration")
    if not isinstance(provenance, dict) or not isinstance(configuration, dict):
        raise ValidationError("Chimew Phase 6 electrical provenance is missing")
    _string(binding.get("paper_provider"), "electrical_binding.paper_provider")
    _string(provenance.get("producer"), "electrical_binding.producer")
    _string(
        provenance.get("producer_version"),
        "electrical_binding.producer_version",
    )
    for field in (
        "boarddb_sha256",
        "package_pin_inventory_sha256",
        "electrical_map_sha256",
        "assignment_input_sha256",
        "schedule_sha256",
    ):
        _digest(provenance.get(field), f"electrical_binding.{field}")
    schedule_sha256 = _canonical_sha256(schedule)
    if (
        provenance.get("schedule_sha256") != schedule_sha256
        or configuration.get("schedule_sha256") != schedule_sha256
        or provenance.get("electrical_map_sha256")
        != configuration.get("electrical_map_sha256")
    ):
        raise ValidationError("Chimew Phase 6 electrical provenance does not agree")
    qualification_status = configuration.get(
        "lookahead_qualification", "bank-electrical-only"
    )
    if qualification_status == "complete-artifact-chain":
        qualification_sha = _digest(
            configuration.get("qualification_sha256"),
            "pin_plan.qualification_sha256",
        )
        if provenance.get("qualification_sha256") != qualification_sha:
            raise ValidationError("Chimew qualification binding does not agree")
        qualification = binding.get("lookahead_qualification")
        if not isinstance(qualification, dict):
            raise ValidationError("Chimew qualification certificate is missing")
        qualification_validation = validate_chimew_qualification_seal(
            qualification, schedule
        )
        qualification_scope = configuration.get("qualification_scope")
        provenance_scope = provenance.get("qualification_scope")
        expected_scope = qualification_validation["qualification_scope"]
        legacy_scope_omitted = (
            expected_scope == "declared-digest-artifact-chain"
            and qualification_scope is None
            and provenance_scope is None
        )
        if qualification_validation["qualification_sha256"] != qualification_sha or (
            not legacy_scope_omitted
            and (
                qualification_scope != expected_scope
                or provenance_scope != expected_scope
            )
        ):
            raise ValidationError("embedded Chimew qualification does not agree")
        if expected_scope == "byte-bound-source-artifacts":
            source_binding = qualification.get("source_binding")
            if (
                not isinstance(source_binding, dict)
                or not isinstance(source_binding.get("digests"), dict)
                or source_binding["digests"].get("package_pins")
                != provenance.get("package_pin_inventory_sha256")
            ):
                raise ValidationError(
                    "Chimew byte-bound package-pin provenance does not agree"
                )
    elif qualification_status == "bank-electrical-only":
        if (
            "qualification_sha256" in configuration
            or "qualification_sha256" in provenance
            or "qualification_scope" in configuration
            or "qualification_scope" in provenance
            or "lookahead_qualification" in binding
        ):
            raise ValidationError("partial Chimew binding contains a qualification seal")
    else:
        raise ValidationError("Chimew lookahead qualification status is invalid")

    schedule_by_id = {entry["id"]: entry for entry in schedule.get("entries", [])}
    plan_by_id = {
        entry["schedule_entry"]: entry for entry in pin_plan.get("entries", [])
    }
    if len(schedule_by_id) != len(schedule.get("entries", [])) or set(plan_by_id) != set(
        schedule_by_id
    ):
        raise ValidationError("Chimew Phase 6 electrical schedule coverage is invalid")
    raw_entries = binding.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("Chimew Phase 6 electrical entries are missing")
    covered = set()
    pins = set()
    lanes = set()
    physical_lane_uses: Dict[Tuple[str, int], set[str]] = {}
    groups = set()
    link_by_id = {link.id: link for link in platform.links}
    binding_bounds = {}
    if binding_schema == CHIMEW_PHASE6_BINDING_SCHEMA:
        raw_bounds = binding.get("fpga_y_bounds")
        if not isinstance(raw_bounds, list):
            raise ValidationError("Chimew electrical binding FPGA bounds are missing")
        for item in raw_bounds:
            if not isinstance(item, dict):
                raise ValidationError("Chimew electrical binding FPGA bounds are invalid")
            fpga = _string(item.get("fpga"), "electrical_binding.bounds.fpga")
            low = _number(item.get("y_min"), f"electrical_binding.bounds.{fpga}.low")
            high = _number(item.get("y_max"), f"electrical_binding.bounds.{fpga}.high")
            if fpga in binding_bounds or high <= low:
                raise ValidationError("Chimew electrical binding FPGA bounds are invalid")
            binding_bounds[fpga] = (low, high)
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"Chimew electrical binding entry {index} is invalid")
        group = _string(record.get("group"), f"electrical_binding[{index}].group")
        if group in groups:
            raise ValidationError("Chimew electrical binding duplicates a group")
        groups.add(group)
        members = record.get("schedule_entries")
        if not isinstance(members, list) or not members or not all(
            isinstance(member, str) and member for member in members
        ):
            raise ValidationError("Chimew electrical binding group is empty")
        if len(set(members)) != len(members) or any(member in covered for member in members):
            raise ValidationError("Chimew electrical binding duplicates schedule entries")
        link = _string(record.get("link"), f"electrical_binding[{index}].link")
        fpga_a = _string(record.get("fpga_a"), f"electrical_binding[{index}].fpga_a")
        fpga_b = _string(record.get("fpga_b"), f"electrical_binding[{index}].fpga_b")
        platform_link = link_by_id.get(link)
        if platform_link is None or set(platform_link.endpoints) != {fpga_a, fpga_b}:
            raise ValidationError("Chimew electrical binding link identity is invalid")
        physical_lane = record.get("physical_lane")
        if (
            isinstance(physical_lane, bool)
            or not isinstance(physical_lane, int)
            or not 0
            <= physical_lane
            < platform_link.transport_bits_per_cycle_per_direction
        ):
            raise ValidationError("Chimew electrical binding physical lane is invalid")
        direction = record.get("direction")
        if direction not in {"a_to_b", "b_to_a", "bidirectional"}:
            raise ValidationError("Chimew electrical binding direction is invalid")
        if direction == "bidirectional" and (
            platform_link.direction != "full_duplex"
            or platform_link.capacity_sharing != "shared_bidirectional"
        ):
            raise ValidationError(
                "a bidirectional Chimew bundle requires shared full-duplex capacity"
            )
        lane_key = (link, direction, physical_lane)
        if lane_key in lanes:
            raise ValidationError("Chimew electrical binding reuses a concrete lane")
        physical_key = (link, physical_lane)
        existing_directions = physical_lane_uses.setdefault(physical_key, set())
        if existing_directions and (
            direction == "bidirectional"
            or "bidirectional" in existing_directions
            or platform_link.direction != "full_duplex"
            or platform_link.capacity_sharing != "per_direction"
        ):
            raise ValidationError("Chimew electrical binding reuses a concrete lane")
        existing_directions.add(direction)
        lanes.add(lane_key)
        member_directions = set()
        slots = set()
        ratios_by_direction = {"a_to_b": set(), "b_to_a": set()}
        count_by_direction = {"a_to_b": 0, "b_to_a": 0}
        for member in members:
            scheduled = schedule_by_id.get(member)
            planned = plan_by_id.get(member)
            if scheduled is None:
                raise ValidationError("Chimew electrical binding conflicts with pin plan")
            if (scheduled.get("from"), scheduled.get("to")) == (fpga_a, fpga_b):
                member_direction = "a_to_b"
            elif (scheduled.get("from"), scheduled.get("to")) == (fpga_b, fpga_a):
                member_direction = "b_to_a"
            else:
                raise ValidationError("Chimew electrical binding direction is invalid")
            ratio = _tdm_ratio(scheduled)
            slot = scheduled.get("slot")
            if (
                planned is None
                or scheduled.get("link") != link
                or planned.get("physical_lane") != physical_lane
                or isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot < 0
                or slot in slots
            ):
                raise ValidationError("Chimew electrical binding conflicts with pin plan")
            member_directions.add(member_direction)
            ratios_by_direction[member_direction].add(ratio)
            count_by_direction[member_direction] += 1
            slots.add(slot)
        if (
            any(
                len(ratios_by_direction[member_direction]) != 1
                or count_by_direction[member_direction]
                > next(iter(ratios_by_direction[member_direction]))
                for member_direction in member_directions
            )
            or (
                direction == "bidirectional"
                and member_directions != {"a_to_b", "b_to_a"}
            )
            or (
                direction != "bidirectional"
                and member_directions != {direction}
            )
        ):
            raise ValidationError("Chimew electrical binding TDM bundle is invalid")
        pin_keys = (
            (fpga_a, _string(record.get("package_pin_a"), "package_pin_a")),
            (fpga_b, _string(record.get("package_pin_b"), "package_pin_b")),
        )
        if any(key in pins for key in pin_keys):
            raise ValidationError("Chimew electrical binding reuses a package pin")
        pins.update(pin_keys)
        _string(record.get("bank_pair"), "electrical_binding.bank_pair")
        _string(record.get("channel"), "electrical_binding.channel")
        _string(record.get("bank_a"), "electrical_binding.bank_a")
        _string(record.get("bank_b"), "electrical_binding.bank_b")
        iostandard = _string(
            record.get("iostandard"), "electrical_binding.iostandard"
        )
        supported = record.get("supported_iostandards")
        if (
            not isinstance(supported, list)
            or iostandard not in supported
            or len(supported) != len(set(supported))
        ):
            raise ValidationError("Chimew electrical binding IOSTANDARD is invalid")
        voltage = _number(
            record.get("bank_voltage"), "electrical_binding.bank_voltage"
        )
        if (
            record.get("electrical_class") != "single_ended_parallel"
            or iostandard not in _IOSTANDARD_VOLTAGES
            or abs(voltage - _IOSTANDARD_VOLTAGES[iostandard]) > 1.0e-9
        ):
            raise ValidationError("Chimew electrical binding voltage is invalid")
        if binding_schema == CHIMEW_PHASE6_BINDING_SCHEMA:
            for endpoint, fpga in (("a", fpga_a), ("b", fpga_b)):
                point = record.get(f"pin_{endpoint}_point")
                if not isinstance(point, dict) or fpga not in binding_bounds:
                    raise ValidationError(
                        "Chimew electrical binding physical pin point is missing"
                    )
                _number(point.get("x"), f"pin_{endpoint}_point.x")
                y = _number(point.get("y"), f"pin_{endpoint}_point.y")
                low, high = binding_bounds[fpga]
                if y < low - 1.0e-9 or y > high + 1.0e-9:
                    raise ValidationError(
                        "Chimew electrical binding pin lies outside FPGA bounds"
                    )
        if not isinstance(record.get("placement_anchor", True), bool):
            raise ValidationError("Chimew electrical placement anchor is invalid")
        covered.update(members)
    if covered != set(schedule_by_id):
        raise ValidationError("Chimew electrical binding coverage is incomplete")
    metrics = binding.get("metrics")
    expected = {
        "signals": len(covered),
        "groups": len(groups),
        "channels": len(raw_entries),
        "package_pins": len(pins),
        "lane_slot_collisions": 0,
        "package_pin_collisions": 0,
    }
    if not isinstance(metrics, dict) or any(
        metrics.get(key) != value for key, value in expected.items()
    ):
        raise ValidationError("Chimew Phase 6 electrical metrics do not agree")
    return {
        "status": "pass",
        "signals": len(covered),
        "groups": len(groups),
        "concrete_lanes": len(lanes),
        "package_pins": len(pins),
        "binding_sha256": _canonical_sha256(binding),
    }


def run_chimew_phase6_adapter(
    schedule_path: Path,
    platform_path: Path,
    bank_channel_input_path: Path,
    electrical_map_path: Path,
    output_dir: Path,
    *,
    qualification_path: Optional[Path] = None,
    bank_channel_report_path: Optional[Path] = None,
    executable: Optional[str] = None,
    region_count: int = 31,
) -> Dict[str, Any]:
    """Materialize adapter artifacts for direct consumption by ``phase6``."""

    schedule = read_json(schedule_path)
    assignment_input = read_json(bank_channel_input_path)
    electrical_map = read_json(electrical_map_path)
    qualification = read_json(qualification_path) if qualification_path else None
    assignment_report = (
        read_json(bank_channel_report_path) if bank_channel_report_path else None
    )
    platform_sha256 = hashlib.sha256(platform_path.read_bytes()).hexdigest()
    provenance = electrical_map.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("boarddb_sha256") != platform_sha256
    ):
        raise ValidationError(
            "Chimew electrical map BoardDB SHA-256 does not match platform"
        )
    result = build_chimew_phase6_pin_plan(
        schedule,
        Platform.load(platform_path),
        assignment_input,
        electrical_map,
        qualification_document=qualification,
        bank_channel_report_document=assignment_report,
        executable=executable,
        region_count=region_count,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "bank_channel_report": "bank_channel_report.json",
        "position_hints": "position_hints.json",
        "pin_plan": "pin_plan.json",
        "electrical_binding": "electrical_binding.json",
    }
    for key, name in artifacts.items():
        write_json(output_dir / name, result[key])
    if qualification is not None:
        artifacts["qualification_certificate"] = "qualification_certificate.json"
        write_json(output_dir / artifacts["qualification_certificate"], qualification)
    report = {
        "schema": CHIMEW_PHASE6_ADAPTER_REPORT_SCHEMA,
        "status": "pass",
        "provider": CHIMEW_PHASE6_BINDING_PROVIDER,
        "design": result["pin_plan"]["design"],
        "platform": result["pin_plan"]["platform"],
        "paper_provider": result["bank_channel_report"]["provider"],
        "validation": result["validation"],
        "electrical_metrics": result["electrical_binding"]["metrics"],
        "lookahead_qualification": (
            result["pin_plan"]["configuration"]["lookahead_qualification"]
        ),
        "qualification_validation": result["qualification_validation"],
        "artifacts": artifacts,
    }
    write_json(output_dir / "adapter_report.json", report)
    return report
