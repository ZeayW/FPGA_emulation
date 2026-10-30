"""Independent, bounded pin projection oracle for small contract fixtures."""
from __future__ import annotations
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping
from typing import Any, Dict, Tuple
from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform
from emuflow.transport.chimew.chimew_bank_channel import validate_chimew_bank_channel_input, evaluate_chimew_bank_channel_assignment
from emuflow.transport.chimew.chimew_fixed_wiring import FixedWiringChannels, FIXED_WIRING_CLASS, FIXED_WIRING_MAP_SCHEMA, FIXED_WIRING_BINDING_SCHEMA, electrical_channel_pin_keys
from emuflow.transport.chimew.chimew_phase6 import CHIMEW_ELECTRICAL_MAP_SCHEMA_V1, CHIMEW_ELECTRICAL_MAP_SCHEMA, CHIMEW_ELECTRICAL_MAP_PROVIDER, CHIMEW_PHASE6_BINDING_SCHEMA, CHIMEW_PHASE6_BINDING_PROVIDER
from emuflow.scheduling.tdm_schedule_storage import canonical_tdm_schedule_sha256
from tests.pin_planning_reference import _assignment_metrics, _baseline_assignment, validate_pin_plan, SIGNAL_POSITION_HINTS_SCHEMA, PIN_PLAN_SCHEMA, CHIMEW_PIN_PLAN_PROVIDER, CHIMEW_PHYSICAL_POSITION_PROVIDER
_IOSTANDARD_VOLTAGES = {"LVCMOS12":1.2,"LVCMOS15":1.5,"LVCMOS18":1.8,"LVCMOS25":2.5,"LVCMOS33":3.3}


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
    try:
        number = float(value)
    except OverflowError as error:
        raise ValidationError(f"{label}: expected a finite number") from error
    if not math.isfinite(number):
        raise ValidationError(f"{label}: expected a finite number")
    return number

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
    if schema not in {CHIMEW_ELECTRICAL_MAP_SCHEMA_V1, CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}:
        raise ValidationError("Chimew electrical channel-map schema is invalid")
    fixed = FixedWiringChannels(platform) if schema == FIXED_WIRING_MAP_SCHEMA else None
    if platform.virtual_board_model is not None and fixed is None:
        raise ValidationError("fixed LVDS BoardDB requires a differential Chimew electrical map")
    if fixed is not None:
        fixed.validate_source(document.get("fixed_wiring"))
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
        if schema in {CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}:
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
            schema in {CHIMEW_ELECTRICAL_MAP_SCHEMA, FIXED_WIRING_MAP_SCHEMA}
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
        differential = (fixed.validate_channel(record, domain["fpga_a"], domain["fpga_b"])
                        if fixed is not None else {})
        pin_keys = electrical_channel_pin_keys({**record, "fpga_a": domain["fpga_a"],
                                               "fpga_b": domain["fpga_b"]}, fixed)
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
        if fixed is None and record.get("electrical_class") != "single_ended_parallel":
            raise ValidationError(
                "Chimew electrical maps require single-ended parallel channels"
            )
        voltage = _number(
            record.get("bank_voltage"), f"electrical.{channel_id}.bank_voltage"
        )
        expected_voltage = 1.8 if fixed is not None else _IOSTANDARD_VOLTAGES.get(iostandard)
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
            "electrical_class": FIXED_WIRING_CLASS if fixed is not None else "single_ended_parallel",
            "placement_anchor": placement_anchor,
            **differential,
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
        "fixed_wiring": fixed.source if fixed is not None else None,
    }

def _normalized_y(y: float, bounds: Tuple[float, float], label: str) -> float:
    low, high = bounds
    tolerance = 1.0e-9 * max(1.0, abs(low), abs(high))
    if y < low - tolerance or y > high + tolerance:
        raise ValidationError(f"{label}: physical y lies outside FPGA bounds")
    return min(1.0, max(0.0, (y - low) / (high - low)))


def build_chimew_phase6_pin_plan(schedule, platform, bank_channel_input, electrical_map, *, bank_channel_report_output_path=None):
    """Project at most 256 records independently, without SQL or production writers."""
    if len(schedule["entries"]) > 256:
        raise ValueError("pin oracle is restricted to small fixtures")
    problem = validate_chimew_bank_channel_input(bank_channel_input)
    groups = list(problem["groups"])
    if len(groups) > 256:
        raise ValueError("pin oracle is restricted to small fixtures")
    electrical = validate_chimew_electrical_map(electrical_map, platform, problem)
    report = evaluate_chimew_bank_channel_assignment(bank_channel_input, output_path=bank_channel_report_output_path)
    try:
        assignments = {row["group"]: row for row in report["assignments"]}
        schedule_rows = {row["id"]: row for row in schedule["entries"]}
        by_member, positions, pin_rows, binding_rows, pins = {}, [], [], [], []
        fixed = FixedWiringChannels(platform) if electrical["fixed_wiring"] is not None else None
        for index, group in enumerate(groups):
            assigned = assignments[group["id"]]
            channel = electrical["channels"][assigned["channel"]]
            domain = problem["domains"][group["domain"]]
            direction = ("a_to_b", "b_to_a", "bidirectional")[group["direction"]]
            for member in group["members"]:
                identity = member["id"]
                if identity in by_member:
                    raise ValidationError("repeated oracle member")
                by_member[identity] = index
                entry = schedule_rows[identity]
                a, b = domain["fpga_a"], domain["fpga_b"]
                source, sink = (a,b) if member["direction"] == 0 else (b,a)
                if (entry["link"], entry["from"], entry["to"]) != (channel["link"], source, sink):
                    raise ValidationError("oracle schedule domain mismatch")
                sy = _normalized_y(member["fanout"][1], electrical["bounds"][source], identity)
                ty = _normalized_y(sum(point[1] for point in member["fanins"])/len(member["fanins"]), electrical["bounds"][sink], identity)
                positions.append({"schedule_entry":identity,"source_y":sy,"sink_y":ty,"source_region":min(30,int(sy*31)),"sink_region":min(30,int(ty*31)),"source_fallback":False,"sink_fallback":False})
                pin_rows.append({"schedule_entry":identity,"group":index,"physical_lane":channel["physical_lane"],"logical_lane":entry["lane"]})
            pins.extend(electrical_channel_pin_keys(channel, fixed))
            binding_rows.append({"group":group["id"],"schedule_entries":sorted(m["id"] for m in group["members"]),"bank_pair":assigned["bank_pair"],"channel":assigned["channel"],**channel,"direction":direction})
        if set(by_member) != set(schedule_rows) or any(v>1 for v in Counter(pins).values()):
            raise ValidationError("oracle coverage or pin ownership mismatch")
        positions.sort(key=lambda row:row["schedule_entry"])
        pin_rows.sort(key=lambda row:row["schedule_entry"])
        binding_rows.sort(key=lambda row:row["group"])
        count = len(schedule_rows)
        position_document = {"schema":SIGNAL_POSITION_HINTS_SCHEMA,"design":schedule["design"],"platform":platform.name,"provider":CHIMEW_PHYSICAL_POSITION_PROVIDER,"source_coordinate_system":"physical-site-xy","region_count":31,
            "provenance":{"producer":problem["provenance"]["producer"],"producer_version":problem["provenance"]["producer_version"],"assignment_input_sha256":report["input_sha256"],"placement_sha256":problem["provenance"]["placement_sha256"],"architecture_sha256":problem["provenance"]["architecture_sha256"]},
            "metrics":{"signals":count,"endpoint_centroid_fallbacks":0},"entries":positions}
        assignment = {row["schedule_entry"]:(row["group"],row["physical_lane"]) for row in pin_rows}
        metrics = _assignment_metrics(schedule,platform,position_document,assignment,1.0,1.0)
        baseline = _assignment_metrics(schedule,platform,position_document,_baseline_assignment(schedule),1.0,1.0)
        schedule_hash = canonical_tdm_schedule_sha256(schedule)
        plan = {"schema":PIN_PLAN_SCHEMA,"design":schedule["design"],"platform":platform.name,"provider":CHIMEW_PIN_PLAN_PROVIDER,
            "configuration":{"refinement_iterations":0,"paper_assignment_provider":report["provider"],"electrical_map_sha256":electrical["map_sha256"],"schedule_sha256":schedule_hash,"lookahead_qualification":"bank-electrical-only"},
            "weights":{"crossing":1.0,"position":1.0},"metrics":{"signals":count,"groups":len(groups),**metrics,**{"baseline_logical_lane_"+k:v for k,v in baseline.items()}},"entries":pin_rows}
        validation = validate_pin_plan(schedule,platform,position_document,plan)
        binding = {"schema":FIXED_WIRING_BINDING_SCHEMA if fixed else CHIMEW_PHASE6_BINDING_SCHEMA,
            **({"fixed_wiring":electrical["fixed_wiring"]} if fixed else {}),"status":"pass","integration_status":"phase6-pin-plan","design":schedule["design"],"platform":platform.name,"provider":CHIMEW_PHASE6_BINDING_PROVIDER,"paper_provider":report["provider"],
            "extension_scope":"EmuFlow concrete lane, bank voltage, IOSTANDARD, and package-pin legality",
            "fpga_y_bounds":[{"fpga":k,"y_min":v[0],"y_max":v[1]} for k,v in sorted(electrical["bounds"].items())],
            "provenance":{**electrical["provenance"],"electrical_map_sha256":electrical["map_sha256"],"assignment_input_sha256":report["input_sha256"],"schedule_sha256":schedule_hash},
            "metrics":{"signals":count,"groups":len(groups),"channels":len(groups),"package_pins":len(pins),"lane_slot_collisions":validation["physical_lane_slot_collisions"],"package_pin_collisions":0},"entries":binding_rows}
        return {"status":"pass","provider":CHIMEW_PHASE6_BINDING_PROVIDER,"bank_channel_report":report,"position_hints":position_document,"pin_plan":plan,"electrical_binding":binding,"qualification_validation":None,"validation":validation}
    except BaseException:
        report.close()
        raise
