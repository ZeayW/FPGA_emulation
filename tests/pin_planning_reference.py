"""Independent small-fixture pin metrics and legality oracle."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Dict, Mapping, Sequence, Tuple

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform


SIGNAL_POSITION_HINTS_SCHEMA = "emuflow.signal-position-hints/v1"
PIN_PLAN_SCHEMA = "emuflow.placement-aware-pin-plan/v1"
CHIMEW_PIN_PLAN_PROVIDER = "chimew-paper-plus-emuflow-electrical-slot-v1"
CHIMEW_PHYSICAL_POSITION_PROVIDER = "chimew-physical-site-projection-v1"


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


def _validate_positions(
    schedule: Mapping[str, Any], positions: Mapping[str, Any]
) -> Dict[str, Mapping[str, Any]]:
    region_count = _validate_position_header(schedule, positions)
    raw = positions.get("entries")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError("positions.entries: expected an array")
    result = {}
    fallback_count = 0
    for item in raw:
        entry_id, item_fallbacks = _validate_position_item(item, region_count)
        if entry_id in result:
            raise ValidationError(
                f"duplicate position hint for {entry_id!r}"
            )
        fallback_count += item_fallbacks
        result[entry_id] = item
    expected = {entry["id"] for entry in schedule["entries"]}
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ValidationError(
            "position hints must cover schedule entries exactly; "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    _validate_position_metrics(positions, len(raw), fallback_count)
    return result


def _validate_position_header(
    schedule: Mapping[str, Any], positions: Mapping[str, Any]
) -> int:
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
    return region_count


def _validate_position_item(
    item: Mapping[str, Any], region_count: int
) -> tuple[str, int]:
    if not isinstance(item, dict) or not isinstance(
        item.get("schedule_entry"), str
    ):
        raise ValidationError("position hint is malformed")
    entry_id = item["schedule_entry"]
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
    fallback_count = 0
    for field in ("source_fallback", "sink_fallback"):
        if not isinstance(item.get(field), bool):
            raise ValidationError(
                f"position {entry_id}.{field}: expected boolean"
            )
        fallback_count += int(item[field])
    return entry_id, fallback_count


def _validate_position_metrics(
    positions: Mapping[str, Any], count: int, fallback_count: int
) -> None:
    metrics = positions.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("signals") != count
        or metrics.get("endpoint_centroid_fallbacks") != fallback_count
    ):
        raise ValidationError("position hint metrics do not agree")


def _iter_position_pairs(
    schedule: Mapping[str, Any],
    positions: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Merge an indexed hint cursor with schedule IDs using bounded state."""

    iterator_factory = getattr(positions, "iter_by_identity", None)
    if not callable(iterator_factory):
        hints = _validate_positions(schedule, positions)
        return ((entry, hints[entry["id"]]) for entry in entries)
    region_count = _validate_position_header(schedule, positions)

    def records() -> Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        iterator = iter(iterator_factory())
        count = 0
        fallback_count = 0
        for entry in entries:
            try:
                hint = next(iterator)
            except StopIteration as error:
                raise ValidationError(
                    "position hints must cover schedule entries exactly"
                ) from error
            entry_id, item_fallbacks = _validate_position_item(
                hint, region_count
            )
            if entry_id != entry["id"]:
                raise ValidationError(
                    "position hints must cover schedule entries exactly"
                )
            count += 1
            fallback_count += item_fallbacks
            yield entry, hint
        try:
            next(iterator)
        except StopIteration:
            pass
        else:
            raise ValidationError(
                "position hints must cover schedule entries exactly"
            )
        _validate_position_metrics(positions, count, fallback_count)

    return records()


def _assignment_metrics(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    assignment: Mapping[str, Tuple[int, int]],
    crossing_weight: float,
    position_weight: float,
) -> Dict[str, float]:
    entries = sorted(schedule["entries"], key=lambda item: item["id"])
    group_stats: Dict[int, list[float | int]] = {}
    crossing_bits = 0
    position_sse = 0.0
    pin_distance = 0.0
    link_by_id = {link.id: link for link in platform.links}
    for entry, hint in _iter_position_pairs(schedule, positions, entries):
        group = assignment[entry["id"]][0]
        stats = group_stats.setdefault(group, [0, 0, 0.0, 0.0])
        stats[0] = int(stats[0]) | (
            (1 << hint["source_region"])
            | (1 << (32 + hint["sink_region"]))
        )
        for value in (float(hint["source_y"]), float(hint["sink_y"])):
            count = int(stats[1]) + 1
            delta = value - float(stats[2])
            mean = float(stats[2]) + delta / count
            stats[3] = float(stats[3]) + delta * (value - mean)
            stats[1] = count
            stats[2] = mean
        pin = assignment[entry["id"]][1]
        lanes = link_by_id[
            entry["link"]
        ].transport_bits_per_cycle_per_direction
        pin_y = 0.5 if lanes == 1 else pin / (lanes - 1)
        pin_distance += abs(hint["source_y"] - pin_y)
        pin_distance += abs(hint["sink_y"] - pin_y)
    for crossing, _, _, group_sse in group_stats.values():
        crossing_bits += bin(int(crossing)).count("1")
        position_sse += float(group_sse)
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


def _bank_pin_budgets(
    schedule: Mapping[str, Any], platform: Platform
) -> tuple[Dict[str, Any], Dict[Tuple[str, str, str], Tuple[int, int]]]:
    links = {link.id: link for link in platform.links if link.bank_count is not None}
    if not links:
        return links, {}
    expected = {(link.id, source, sink) for link in links.values()
                for source, sink in (link.endpoints, tuple(reversed(link.endpoints)))}
    raw = schedule.get("bank_allocations")
    if not isinstance(raw, list):
        raise ValidationError("bank pin assignment requires Phase 5 bank_allocations")
    budgets = {}
    owners = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("bank pin allocation is malformed")
        key = (item.get("link"), item.get("from"), item.get("to"))
        if key not in expected or key in budgets:
            raise ValidationError("bank pin allocation direction coverage is invalid")
        link = links[key[0]]
        count, offset, lanes = (item.get(field) for field in
                               ("bank_count", "lane_offset", "lanes"))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (count, offset, lanes)):
            raise ValidationError("bank pin allocation requires nonnegative integers")
        width = link.wires_per_bank
        if (item.get("wires_per_bank") != width or offset % width
                or lanes != count * width
                or offset + lanes > link.transport_bits_per_cycle_per_direction):
            raise ValidationError("bank pin allocation disagrees with BoardDB capacity")
        for bank in range(offset // width, (offset + lanes) // width):
            bank_key = (link.id, bank)
            if bank_key in owners:
                raise ValidationError("bank pin allocation assigns one bank to both directions")
            owners[bank_key] = key
        budgets[key] = (offset, offset + lanes)
    if set(budgets) != expected:
        raise ValidationError("bank pin allocation direction coverage is incomplete")
    return links, budgets


def _validate_bank_pin_rows(
    schedule: Mapping[str, Any],
    platform: Platform,
    rows: Iterable[Tuple[str, str, str, Any, Any]],
) -> None:
    links, budgets = _bank_pin_budgets(schedule, platform)
    if not links:
        return
    for link_id, source, sink, logical_lane, physical_lane in rows:
        if link_id not in links:
            continue
        key = (link_id, source, sink)
        if key not in budgets:
            raise ValidationError("bank pin assignment has an unknown direction")
        low, high = budgets[key]
        for lane in (logical_lane, physical_lane):
            if (isinstance(lane, bool) or not isinstance(lane, int)
                    or not low <= lane < high):
                raise ValidationError(
                    "pin assignment leaves the schedule's allocated direction banks"
                )


def validate_bank_pin_assignment(
    schedule: Mapping[str, Any], platform: Platform,
    physical_lane_by_entry: Mapping[str, int],
) -> None:
    """Preserve Phase 5's whole-bank direction allocation through pin mapping."""

    _validate_bank_pin_rows(
        schedule,
        platform,
        (
            (
                entry.get("link"),
                entry.get("from"),
                entry.get("to"),
                entry.get("lane"),
                physical_lane_by_entry.get(entry["id"], entry.get("lane")),
            )
            for entry in schedule.get("entries", [])
        ),
    )


def validate_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    if plan.get("schema") != PIN_PLAN_SCHEMA:
        raise ValidationError(f"pin plan schema must be {PIN_PLAN_SCHEMA!r}")
    if plan.get("provider") != CHIMEW_PIN_PLAN_PROVIDER:
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
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValidationError("pin plan entries must be an array")
    if any(not isinstance(item, dict) for item in raw):
        raise ValidationError("pin plan entry is malformed")
    by_id = {item.get("schedule_entry"): item for item in raw}
    expected = {entry["id"] for entry in schedule["entries"]}
    if set(by_id) != expected or len(by_id) != len(raw):
        raise ValidationError("pin plan must cover schedule entries exactly")
    validate_bank_pin_assignment(schedule, platform, {
        entry_id: item.get("physical_lane") for entry_id, item in by_id.items()
    })
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
            and link_by_id[entries[0]["link"]].bank_count is None
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
