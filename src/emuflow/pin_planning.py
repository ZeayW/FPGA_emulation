"""Placement-aware TDM grouping and virtual pin assignment."""

from __future__ import annotations

import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable
from .platform import Platform
from .placement import PLACEMENT_SCHEMA


SIGNAL_POSITION_HINTS_SCHEMA = "emuflow.signal-position-hints/v1"
PIN_PLAN_SCHEMA = "emuflow.placement-aware-pin-plan/v1"
PIN_PLAN_PROVIDER = "placement-aware-region-grouping-mcf-v1"
CHIMEW_PIN_PLAN_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHYSICAL_POSITION_PROVIDER = "chimew-physical-site-projection-v1"
LEGACY_PIN_PLAN_PROVIDERS = frozenset(
    {"chimew-placement-aware-grouping-mcf-v1"}
)


def _domain_key(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (entry["link"], entry["from"], entry["to"])


def _schedule_tdm_ratio(entry: Mapping[str, Any]) -> int:
    """Treat a schedule without ratio-plan metadata as direct-lane transport."""

    value = entry.get("tdm_ratio", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(
            f"schedule entry {entry.get('id')!r} has an invalid TDM ratio"
        )
    return value


def build_signal_position_hints(
    ir: Mapping[str, Any],
    schedule: Mapping[str, Any],
    placements: Mapping[str, Mapping[str, Any]],
    *,
    region_count: int = 3,
) -> Dict[str, Any]:
    """Derive interface-signal centroids from lookahead placements."""
    if region_count <= 0 or region_count >= 32:
        raise ValueError("region_count must be in [1, 31]")
    expected_fpgas = {
        endpoint
        for entry in schedule["entries"]
        for endpoint in (entry["from"], entry["to"])
    }
    if set(placements) != expected_fpgas:
        raise ValidationError(
            "lookahead placements must cover schedule FPGAs exactly; "
            f"expected={sorted(expected_fpgas)}, "
            f"actual={sorted(placements)}"
        )
    cell_locations: Dict[str, Dict[str, Tuple[float, float]]] = {}
    ranges: Dict[str, Tuple[int, int]] = {}
    for fpga, placement in placements.items():
        if placement.get("schema") != PLACEMENT_SCHEMA:
            raise ValidationError(
                f"lookahead placement {fpga!r} has an invalid schema"
            )
        cells = placement.get("cells")
        if not isinstance(cells, list) or not cells:
            raise ValidationError(
                f"lookahead placement {fpga!r} has no cells"
            )
        ys = [cell["y"] for cell in cells]
        y_min, y_max = min(ys), max(ys)
        ranges[fpga] = (y_min, y_max)
        cell_locations[fpga] = {
            cell["instance"]: (float(cell["x"]), float(cell["y"]))
            for cell in cells
        }
    net_by_id = {net["id"]: net for net in ir["nets"]}

    def normalized_y(
        fpga: str, instances: list[str]
    ) -> Tuple[float, bool]:
        locations = [
            cell_locations[fpga][instance][1]
            for instance in instances
            if instance in cell_locations.get(fpga, {})
        ]
        if not locations:
            return 0.5, True
        y_min, y_max = ranges[fpga]
        mean = sum(locations) / len(locations)
        if y_max == y_min:
            return 0.5, False
        return (mean - y_min) / (y_max - y_min), False

    hints = []
    fallbacks = 0
    for entry in sorted(schedule["entries"], key=lambda item: item["id"]):
        net = net_by_id[entry["net"]]
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
        source_y, source_fallback = normalized_y(entry["from"], drivers)
        sink_y, sink_fallback = normalized_y(entry["to"], sinks)
        fallbacks += int(source_fallback) + int(sink_fallback)
        hints.append(
            {
                "schedule_entry": entry["id"],
                "source_y": source_y,
                "sink_y": sink_y,
                "source_region": min(
                    region_count - 1, int(source_y * region_count)
                ),
                "sink_region": min(
                    region_count - 1, int(sink_y * region_count)
                ),
                "source_fallback": source_fallback,
                "sink_fallback": sink_fallback,
            }
        )
    return {
        "schema": SIGNAL_POSITION_HINTS_SCHEMA,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": "openparf-lookahead-centroid-v1",
        "region_count": region_count,
        "metrics": {
            "signals": len(hints),
            "endpoint_centroid_fallbacks": fallbacks,
        },
        "entries": hints,
    }


def _validate_positions(
    schedule: Mapping[str, Any], positions: Mapping[str, Any]
) -> Dict[str, Mapping[str, Any]]:
    if positions.get("schema") != SIGNAL_POSITION_HINTS_SCHEMA:
        raise ValidationError(
            f"positions.schema: expected {SIGNAL_POSITION_HINTS_SCHEMA!r}"
        )
    if (
        positions.get("design") != schedule.get("design")
        or positions.get("platform") != schedule.get("platform")
    ):
        raise ValidationError(
            "position hints do not match the schedule design/platform"
        )
    provider = positions.get("provider")
    if provider not in {
        "openparf-lookahead-centroid-v1",
        CHIMEW_PHYSICAL_POSITION_PROVIDER,
    }:
        raise ValidationError(
            "position hints are not from a supported source-qualified provider"
        )
    if provider == CHIMEW_PHYSICAL_POSITION_PROVIDER:
        if positions.get("source_coordinate_system") != "physical-site-xy":
            raise ValidationError("Chimew position projection is not physical-site based")
        provenance = positions.get("provenance")
        if not isinstance(provenance, dict):
            raise ValidationError("Chimew position projection provenance is missing")
        for field in ("producer", "producer_version"):
            if not isinstance(provenance.get(field), str) or not provenance[field]:
                raise ValidationError("Chimew position projection provenance is invalid")
        for field in (
            "assignment_input_sha256",
            "placement_sha256",
            "architecture_sha256",
        ):
            digest = provenance.get(field)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValidationError(
                    f"Chimew position projection {field} is invalid"
                )
    region_count = positions.get("region_count")
    if (
        isinstance(region_count, bool)
        or not isinstance(region_count, int)
        or not 1 <= region_count <= 31
    ):
        raise ValidationError("position hints have an invalid region count")
    raw = positions.get("entries")
    if not isinstance(raw, list):
        raise ValidationError("positions.entries: expected an array")
    result = {}
    fallback_count = 0
    for item in raw:
        if not isinstance(item, dict) or not isinstance(
            item.get("schedule_entry"), str
        ):
            raise ValidationError("position hint is malformed")
        entry_id = item["schedule_entry"]
        if entry_id in result:
            raise ValidationError(
                f"duplicate position hint for {entry_id!r}"
            )
        for field in ("source_y", "sink_y"):
            value = item.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValidationError(
                    f"position {entry_id}.{field}: expected 0 <= value <= 1"
                )
        for field in ("source_region", "sink_region"):
            value = item.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < region_count
            ):
                raise ValidationError(
                    f"position {entry_id}.{field}: expected integer "
                    f"[0, {region_count - 1}]"
                )
        for field in ("source_fallback", "sink_fallback"):
            if not isinstance(item.get(field), bool):
                raise ValidationError(
                    f"position {entry_id}.{field}: expected boolean"
                )
            fallback_count += int(item[field])
        result[entry_id] = item
    expected = {entry["id"] for entry in schedule["entries"]}
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ValidationError(
            "position hints must cover schedule entries exactly; "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    metrics = positions.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("signals") != len(raw)
        or metrics.get("endpoint_centroid_fallbacks") != fallback_count
    ):
        raise ValidationError("position hint metrics do not agree")
    return result


def _write_model(
    path: Path,
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    *,
    refinement_iterations: int,
    crossing_weight: float,
    position_weight: float,
) -> Tuple[list[Mapping[str, Any]], list[Tuple[str, str, str]]]:
    position_by_entry = _validate_positions(schedule, positions)
    domains = sorted({_domain_key(entry) for entry in schedule["entries"]})
    domain_index = {domain: index for index, domain in enumerate(domains)}
    link_by_id = {link.id: link for link in platform.links}
    entries = sorted(schedule["entries"], key=lambda item: item["id"])
    lines = [
        "EMUFLOW_PIN_PLANNER_INPUT_V1",
        (
            f"PARAM {refinement_iterations} {crossing_weight:.17g} "
            f"{position_weight:.17g}"
        ),
    ]
    for index, domain in enumerate(domains):
        lines.append(
            f"DOMAIN {index} "
            f"{link_by_id[domain[0]].transport_bits_per_cycle_per_direction}"
        )
    for index, entry in enumerate(entries):
        hint = position_by_entry[entry["id"]]
        crossing = (
            (1 << hint["source_region"])
            | (1 << (32 + hint["sink_region"]))
        )
        lines.append(
            "SIGNAL "
            f"{index} {domain_index[_domain_key(entry)]} "
            f"{_schedule_tdm_ratio(entry)} {entry['slot']} {crossing} "
            f"{float(hint['source_y']):.17g} "
            f"{float(hint['sink_y']):.17g}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return entries, domains


def _parse_output(
    path: Path, entries: list[Mapping[str, Any]]
) -> Tuple[Dict[str, Tuple[int, int]], Dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_PIN_PLANNER_OUTPUT_V1":
        raise EmuFlowError("pin planner returned an invalid output header")
    assignments: Dict[str, Tuple[int, int]] = {}
    metrics: Dict[str, Any] = {}
    for line in lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "METRIC" and len(fields) == 3:
            metrics = {
                "groups": int(fields[1]),
                "native_objective": float(fields[2]),
            }
        elif fields[0] == "ASSIGN" and len(fields) == 4:
            index = int(fields[1])
            if not 0 <= index < len(entries):
                raise EmuFlowError("pin planner returned an invalid signal")
            entry_id = entries[index]["id"]
            if entry_id in assignments:
                raise EmuFlowError("pin planner returned a duplicate signal")
            assignments[entry_id] = (int(fields[2]), int(fields[3]))
        else:
            raise EmuFlowError(f"pin planner returned malformed line: {line}")
    if len(assignments) != len(entries) or not metrics:
        raise EmuFlowError("pin planner output is incomplete")
    return assignments, metrics


def _assignment_metrics(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    assignment: Mapping[str, Tuple[int, int]],
    crossing_weight: float,
    position_weight: float,
) -> Dict[str, float]:
    hints = _validate_positions(schedule, positions)
    by_group: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in schedule["entries"]:
        by_group[assignment[entry["id"]][0]].append(entry)
    crossing_bits = 0
    position_sse = 0.0
    pin_distance = 0.0
    link_by_id = {link.id: link for link in platform.links}
    for entries in by_group.values():
        crossing = 0
        values = []
        for entry in entries:
            hint = hints[entry["id"]]
            crossing |= (
                (1 << hint["source_region"])
                | (1 << (32 + hint["sink_region"]))
            )
            values.extend((hint["source_y"], hint["sink_y"]))
        mean = sum(values) / len(values)
        crossing_bits += bin(crossing).count("1")
        position_sse += sum(
            (value - mean) ** 2 for value in values
        )
    for entry in schedule["entries"]:
        hint = hints[entry["id"]]
        pin = assignment[entry["id"]][1]
        lanes = link_by_id[
            entry["link"]
        ].transport_bits_per_cycle_per_direction
        pin_y = 0.5 if lanes == 1 else pin / (lanes - 1)
        pin_distance += abs(hint["source_y"] - pin_y)
        pin_distance += abs(hint["sink_y"] - pin_y)
    return {
        "objective": (
            crossing_weight * crossing_bits
            + position_weight * position_sse
        ),
        "crossing_bits": float(crossing_bits),
        "position_sse": position_sse,
        "pin_distance": pin_distance,
    }


def _baseline_assignment(
    schedule: Mapping[str, Any],
) -> Dict[str, Tuple[int, int]]:
    domains = sorted({_domain_key(entry) for entry in schedule["entries"]})
    domain_index = {domain: index for index, domain in enumerate(domains)}
    return {
        entry["id"]: (
            domain_index[_domain_key(entry)] * (1 << 32) + entry["lane"],
            entry["lane"],
        )
        for entry in schedule["entries"]
    }


def validate_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    if plan.get("schema") != PIN_PLAN_SCHEMA:
        raise ValidationError(f"pin plan schema must be {PIN_PLAN_SCHEMA!r}")
    if plan.get("provider") not in {
        PIN_PLAN_PROVIDER,
        CHIMEW_PIN_PLAN_PROVIDER,
        *LEGACY_PIN_PLAN_PROVIDERS,
    }:
        raise ValidationError("pin plan provider is not source-complete")
    if (
        plan.get("design") != schedule.get("design")
        or plan.get("platform") != platform.name
    ):
        raise ValidationError(
            "pin plan does not match the schedule design/platform"
        )
    weights = plan.get("weights")
    configuration = plan.get("configuration")
    metrics_document = plan.get("metrics")
    if (
        not isinstance(weights, dict)
        or not isinstance(configuration, dict)
        or not isinstance(metrics_document, dict)
    ):
        raise ValidationError(
            "pin plan configuration/weights/metrics are malformed"
        )
    iterations = configuration.get("refinement_iterations")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 0
    ):
        raise ValidationError(
            "pin plan refinement iteration count is invalid"
        )
    for field in ("crossing", "position"):
        value = weights.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) < 0.0
        ):
            raise ValidationError(f"pin plan weight {field!r} is invalid")
    _validate_positions(schedule, positions)
    raw = plan.get("entries")
    if not isinstance(raw, list):
        raise ValidationError("pin plan entries must be an array")
    if any(not isinstance(item, dict) for item in raw):
        raise ValidationError("pin plan entry is malformed")
    by_id = {item.get("schedule_entry"): item for item in raw}
    expected = {entry["id"] for entry in schedule["entries"]}
    if set(by_id) != expected or len(by_id) != len(raw):
        raise ValidationError("pin plan must cover schedule entries exactly")
    link_by_id = {link.id: link for link in platform.links}
    groups: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    collisions = set()
    group_by_domain_pin: Dict[Tuple[str, str, str, int], int] = {}

    def physical_domain(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
        link = link_by_id[entry["link"]]
        if link.capacity_sharing == "shared_bidirectional":
            return (entry["link"], "shared_bidirectional", "shared_bidirectional")
        return _domain_key(entry)

    for entry in schedule["entries"]:
        item = by_id[entry["id"]]
        group = item.get("group")
        pin = item.get("physical_lane")
        if (
            isinstance(group, bool)
            or not isinstance(group, int)
            or group < 0
            or isinstance(pin, bool)
            or not isinstance(pin, int)
            or not 0 <= pin
            < link_by_id[
                entry["link"]
            ].transport_bits_per_cycle_per_direction
        ):
            raise ValidationError(
                f"pin assignment for {entry['id']!r} is invalid"
            )
        if item.get("logical_lane") != entry["lane"]:
            raise ValidationError(
                f"pin assignment for {entry['id']!r} changed logical lane"
            )
        groups[group].append(entry)
        collision = (*physical_domain(entry), pin, entry["slot"])
        if collision in collisions:
            raise ValidationError(
                f"physical lane/slot collision at {collision}"
            )
        collisions.add(collision)
        domain_pin = (*physical_domain(entry), pin)
        owner = group_by_domain_pin.setdefault(domain_pin, group)
        if owner != group:
            raise ValidationError(
                f"physical pin {domain_pin} is assigned to multiple groups"
            )
    for group, entries in groups.items():
        domains = {_domain_key(entry) for entry in entries}
        pins = {by_id[entry["id"]]["physical_lane"] for entry in entries}
        slots = {entry["slot"] for entry in entries}
        shared_chimew_bundle = (
            plan.get("provider") == CHIMEW_PIN_PLAN_PROVIDER
            and len(domains) == 2
            and len({entry["link"] for entry in entries}) == 1
            and len({physical_domain(entry) for entry in entries}) == 1
            and link_by_id[entries[0]["link"]].direction == "full_duplex"
            and link_by_id[entries[0]["link"]].capacity_sharing
            == "shared_bidirectional"
        )
        if (
            (len(domains) != 1 and not shared_chimew_bundle)
            or len(pins) != 1
        ):
            raise ValidationError(f"group {group} is not homogeneous")
        entries_by_domain = {
            domain: [entry for entry in entries if _domain_key(entry) == domain]
            for domain in domains
        }
        for domain_entries in entries_by_domain.values():
            ratios = {_schedule_tdm_ratio(entry) for entry in domain_entries}
            if len(ratios) != 1 or len(domain_entries) > next(iter(ratios)):
                raise ValidationError(f"group {group} violates TDM capacity")
        if len(slots) != len(entries):
            raise ValidationError(f"group {group} violates TDM capacity")
    assignment = {
        entry_id: (item["group"], item["physical_lane"])
        for entry_id, item in by_id.items()
    }
    metrics = _assignment_metrics(
        schedule,
        platform,
        positions,
        assignment,
        float(weights["crossing"]),
        float(weights["position"]),
    )
    for field in (
        "objective",
        "crossing_bits",
        "position_sse",
        "pin_distance",
    ):
        try:
            expected_value = float(metrics_document[field])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError(
                f"pin plan metric {field!r} is invalid"
            ) from error
        tolerance = 1.0e-8 * max(1.0, abs(expected_value))
        if abs(metrics[field] - expected_value) > tolerance:
            raise ValidationError(
                f"pin plan metric {field!r} does not independently agree"
            )
    baseline_metrics = _assignment_metrics(
        schedule,
        platform,
        positions,
        _baseline_assignment(schedule),
        float(weights["crossing"]),
        float(weights["position"]),
    )
    for field, value in baseline_metrics.items():
        metric_name = f"baseline_logical_lane_{field}"
        try:
            expected_value = float(metrics_document[metric_name])
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationError(
                f"pin plan metric {metric_name!r} is invalid"
            ) from error
        tolerance = 1.0e-8 * max(1.0, abs(expected_value))
        if abs(value - expected_value) > tolerance:
            raise ValidationError(
                f"pin plan metric {metric_name!r} does not independently agree"
            )
    if (
        metrics_document.get("signals") != len(raw)
        or metrics_document.get("groups") != len(groups)
    ):
        raise ValidationError("pin plan count metrics do not agree")
    objective_improvement = (
        100.0
        * (baseline_metrics["objective"] - metrics["objective"])
        / baseline_metrics["objective"]
        if baseline_metrics["objective"]
        else 0.0
    )
    pin_distance_improvement = (
        100.0
        * (
            baseline_metrics["pin_distance"]
            - metrics["pin_distance"]
        )
        / baseline_metrics["pin_distance"]
        if baseline_metrics["pin_distance"]
        else 0.0
    )
    return {
        "status": "pass",
        "signals": len(raw),
        "groups": len(groups),
        "physical_lane_slot_collisions": 0,
        **metrics,
        "baseline_logical_lane_objective": baseline_metrics[
            "objective"
        ],
        "baseline_logical_lane_crossing_bits": baseline_metrics[
            "crossing_bits"
        ],
        "baseline_logical_lane_position_sse": baseline_metrics[
            "position_sse"
        ],
        "baseline_logical_lane_pin_distance": baseline_metrics[
            "pin_distance"
        ],
        "objective_improvement_percent": objective_improvement,
        "crossing_bits_reduction": (
            baseline_metrics["crossing_bits"]
            - metrics["crossing_bits"]
        ),
        "pin_distance_improvement_percent": pin_distance_improvement,
    }


def build_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    refinement_iterations: int = 100,
    crossing_weight: float = 1.0,
    position_weight: float = 1.0,
) -> Dict[str, Any]:
    if refinement_iterations < 0:
        raise ValueError("refinement_iterations must be non-negative")
    if crossing_weight < 0.0 or position_weight < 0.0:
        raise ValueError("objective weights must be non-negative")
    native = resolve_native_executable("emuflow_pin_planner", executable)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        entries, _ = _write_model(
            root / "input.txt",
            schedule,
            platform,
            positions,
            refinement_iterations=refinement_iterations,
            crossing_weight=crossing_weight,
            position_weight=position_weight,
        )
        completed = subprocess.run(
            [native, str(root / "input.txt"), str(root / "output.txt")],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise EmuFlowError(
                f"in-tree pin planner failed with exit code "
                f"{completed.returncode}: {detail}"
            )
        assignments, native_metrics = _parse_output(
            root / "output.txt", entries
        )
    assignment_metrics = _assignment_metrics(
        schedule,
        platform,
        positions,
        assignments,
        crossing_weight,
        position_weight,
    )
    tolerance = 1.0e-8 * max(
        1.0, abs(assignment_metrics["objective"])
    )
    if (
        abs(
            native_metrics["native_objective"]
            - assignment_metrics["objective"]
        )
        > tolerance
    ):
        raise EmuFlowError(
            "native pin-planner objective does not independently agree"
        )
    baseline_metrics = _assignment_metrics(
        schedule,
        platform,
        positions,
        _baseline_assignment(schedule),
        crossing_weight,
        position_weight,
    )
    plan = {
        "schema": PIN_PLAN_SCHEMA,
        "design": schedule["design"],
        "platform": platform.name,
        "provider": PIN_PLAN_PROVIDER,
        "configuration": {
            "refinement_iterations": refinement_iterations,
        },
        "weights": {
            "crossing": crossing_weight,
            "position": position_weight,
        },
        "metrics": {
            "signals": len(entries),
            "groups": native_metrics["groups"],
            **assignment_metrics,
            "baseline_logical_lane_objective": baseline_metrics["objective"],
            "baseline_logical_lane_crossing_bits": baseline_metrics[
                "crossing_bits"
            ],
            "baseline_logical_lane_position_sse": baseline_metrics[
                "position_sse"
            ],
            "baseline_logical_lane_pin_distance": baseline_metrics[
                "pin_distance"
            ],
        },
        "entries": [
            {
                "schedule_entry": entry["id"],
                "group": assignments[entry["id"]][0],
                "physical_lane": assignments[entry["id"]][1],
                "logical_lane": entry["lane"],
            }
            for entry in entries
        ],
    }
    validate_pin_plan(schedule, platform, positions, plan)
    return plan
