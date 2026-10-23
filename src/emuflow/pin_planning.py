"""Placement-aware TDM grouping and virtual pin assignment."""

from __future__ import annotations

import subprocess
import sqlite3
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .sqlite_artifacts import immutable_sqlite_uri
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
    output_path: Optional[Path] = None,
) -> Mapping[str, Any]:
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

    fallbacks = 0
    signals = 0

    def records() -> Iterable[Mapping[str, Any]]:
        nonlocal fallbacks, signals
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
            signals += 1
            yield {
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
    header = {
        "schema": SIGNAL_POSITION_HINTS_SCHEMA,
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": "openparf-lookahead-centroid-v1",
        "region_count": region_count,
    }
    if output_path is not None:
        from .position_hints_storage import write_position_hints_storage

        return write_position_hints_storage(
            output_path,
            {**header, "entries": records()},
            derive_metrics=True,
        )
    hints = list(records())
    return {
        **header,
        "metrics": {
            "signals": signals,
            "endpoint_centroid_fallbacks": fallbacks,
        },
        "entries": hints,
    }


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
    for index, (entry, hint) in enumerate(
        _iter_position_pairs(schedule, positions, entries)
    ):
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


def _indexed_pin_plan_validation_available(
    schedule: Mapping[str, Any], plan: Mapping[str, Any]
) -> bool:
    """Return whether both hop-sized relations have sealed SQLite stores."""

    return all(
        isinstance(getattr(value, "store_path", None), Path)
        for value in (schedule, plan)
    )


def _install_indexed_schedule_entry_relation(
    connection: sqlite3.Connection,
) -> int:
    """Expose v1/v2 JSON rows and v3 typed rows through one SQL view."""

    typed = connection.execute(
        "SELECT 1 FROM schedule_store.sqlite_master "
        "WHERE type='table' AND name='schedule_entries'"
    ).fetchone()
    if typed is not None:
        connection.execute(
            """
            CREATE TEMP VIEW indexed_schedule_entries AS
            SELECT entry_id AS identity,
                   link_id,source_fpga,sink_fpga,slot,lane,
                   coalesce(tdm_ratio,1) AS tdm_ratio,
                   CASE WHEN
                     typeof(entry_id)='text' AND length(entry_id)>0 AND
                     typeof(link_id)='text' AND length(link_id)>0 AND
                     typeof(source_fpga)='text' AND length(source_fpga)>0 AND
                     typeof(sink_fpga)='text' AND length(sink_fpga)>0 AND
                     typeof(slot)='integer' AND slot>=0 AND
                     typeof(lane)='integer' AND lane>=0 AND
                     (tdm_ratio IS NULL OR
                      (typeof(tdm_ratio)='integer' AND tdm_ratio>=1))
                   THEN 1 ELSE 0 END AS well_typed
            FROM schedule_store.schedule_entries
            """
        )
    else:
        connection.execute(
            """
            CREATE TEMP VIEW indexed_schedule_entries AS
            SELECT identity,
                   json_extract(record_json,'$.link') AS link_id,
                   json_extract(record_json,'$.from') AS source_fpga,
                   json_extract(record_json,'$.to') AS sink_fpga,
                   json_extract(record_json,'$.slot') AS slot,
                   json_extract(record_json,'$.lane') AS lane,
                   coalesce(json_extract(record_json,'$.tdm_ratio'),1)
                     AS tdm_ratio,
                   CASE WHEN
                     json_type(record_json,'$.id')='text' AND
                     json_extract(record_json,'$.id')=identity AND
                     json_type(record_json,'$.link')='text' AND
                     json_type(record_json,'$.from')='text' AND
                     json_type(record_json,'$.to')='text' AND
                     json_type(record_json,'$.slot')='integer' AND
                     json_extract(record_json,'$.slot')>=0 AND
                     json_type(record_json,'$.lane')='integer' AND
                     json_extract(record_json,'$.lane')>=0 AND
                     (json_type(record_json,'$.tdm_ratio') IS NULL OR (
                       json_type(record_json,'$.tdm_ratio')='integer' AND
                       json_extract(record_json,'$.tdm_ratio')>=1
                     ))
                   THEN 1 ELSE 0 END AS well_typed
            FROM schedule_store.records
            WHERE section='entries'
            """
        )
    malformed = connection.execute(
        "SELECT identity FROM indexed_schedule_entries "
        "WHERE well_typed!=1 LIMIT 1"
    ).fetchone()
    if malformed is not None:
        raise ValidationError(
            f"schedule entry {malformed[0]!r} is malformed"
        )
    return int(
        connection.execute(
            "SELECT count(*) FROM indexed_schedule_entries"
        ).fetchone()[0]
    )


def _open_indexed_pin_plan_validation(
    schedule: Mapping[str, Any],
    plan: Mapping[str, Any],
    platform: Platform,
) -> tuple[sqlite3.Connection, int]:
    """Build a file-backed validation index without Python hop maps."""

    connection = sqlite3.connect("", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=FILE")
    try:
        connection.execute(
            "ATTACH DATABASE ? AS schedule_store",
            (
                immutable_sqlite_uri(schedule.store_path),
            ),
        )
        connection.execute(
            "ATTACH DATABASE ? AS pin_store",
            (
                immutable_sqlite_uri(plan.store_path),
            ),
        )
        schedule_count = _install_indexed_schedule_entry_relation(connection)
        pin_mismatch = connection.execute(
            """
            SELECT schedule_entry
            FROM pin_store.entries
            WHERE schedule_entry IS NOT json_extract(
                    record_json,'$.schedule_entry'
                  )
               OR group_id IS NOT json_extract(record_json,'$.group')
               OR physical_lane IS NOT json_extract(
                    record_json,'$.physical_lane'
                  )
               OR logical_lane IS NOT json_extract(
                    record_json,'$.logical_lane'
                  )
            LIMIT 1
            """
        ).fetchone()
        if pin_mismatch is not None:
            raise ValidationError(
                f"pin-plan indexed record {pin_mismatch[0]!r} disagrees"
            )
        plan_count = connection.execute(
            "SELECT count(*) FROM pin_store.entries"
        ).fetchone()[0]
        joined_count = connection.execute(
            """
            SELECT count(*)
            FROM indexed_schedule_entries AS schedule
            JOIN pin_store.entries AS pin
              ON pin.schedule_entry=schedule.identity
            """
        ).fetchone()[0]
        if schedule_count != plan_count or joined_count != schedule_count:
            raise ValidationError(
                "pin plan must cover schedule entries exactly"
            )
        connection.executescript(
            """
            CREATE TABLE validation_rows(
              entry_id TEXT PRIMARY KEY,
              group_id INTEGER NOT NULL,
              physical_lane INTEGER NOT NULL,
              logical_lane INTEGER NOT NULL,
              link_id TEXT NOT NULL,
              source_fpga TEXT NOT NULL,
              sink_fpga TEXT NOT NULL,
              slot INTEGER NOT NULL,
              schedule_lane INTEGER NOT NULL,
              tdm_ratio INTEGER NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE link_specs(
              link_id TEXT PRIMARY KEY,
              capacity INTEGER NOT NULL,
              shared_bidirectional INTEGER NOT NULL,
              full_duplex INTEGER NOT NULL,
              banked INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )
        try:
            connection.execute(
                """
                INSERT INTO validation_rows
                SELECT schedule.identity,
                       pin.group_id,
                       pin.physical_lane,
                       pin.logical_lane,
                       schedule.link_id,
                       schedule.source_fpga,
                       schedule.sink_fpga,
                       schedule.slot,
                       schedule.lane,
                       schedule.tdm_ratio
                FROM indexed_schedule_entries AS schedule
                JOIN pin_store.entries AS pin
                  ON pin.schedule_entry=schedule.identity
                """
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "indexed pin-plan validation rows are malformed"
            ) from error
        connection.executemany(
            "INSERT INTO link_specs VALUES (?,?,?,?,?)",
            (
                (
                    link.id,
                    link.transport_bits_per_cycle_per_direction,
                    int(link.capacity_sharing == "shared_bidirectional"),
                    int(link.direction == "full_duplex"),
                    int(link.bank_count is not None),
                )
                for link in platform.links
            ),
        )
        connection.execute(
            "CREATE INDEX validation_group_idx ON "
            "validation_rows(group_id,entry_id)"
        )
        connection.execute(
            "CREATE INDEX validation_domain_idx ON "
            "validation_rows(link_id,source_fpga,sink_fpga,schedule_lane)"
        )
        return connection, int(schedule_count)
    except BaseException:
        connection.close()
        raise


def _stage_indexed_positions(
    connection: sqlite3.Connection,
    schedule: Mapping[str, Any],
    positions: Mapping[str, Any],
) -> None:
    """Validate and index position hints with bounded Python state."""

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
            raise ValidationError(
                "Chimew position projection is not physical-site based"
            )
        provenance = positions.get("provenance")
        if not isinstance(provenance, dict):
            raise ValidationError(
                "Chimew position projection provenance is missing"
            )
        for field in ("producer", "producer_version"):
            if not isinstance(provenance.get(field), str) or not provenance[field]:
                raise ValidationError(
                    "Chimew position projection provenance is invalid"
                )
        for field in (
            "assignment_input_sha256",
            "placement_sha256",
            "architecture_sha256",
        ):
            digest = provenance.get(field)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
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
    attach_relation = getattr(positions, "attach_validation_relation", None)
    if callable(attach_relation):
        position_count, fallback_count = attach_relation(connection)
        invalid = connection.execute(
            """
            SELECT entry_id FROM position_rows
            WHERE source_y < 0.0 OR source_y > 1.0
               OR sink_y < 0.0 OR sink_y > 1.0
               OR source_region < 0 OR source_region >= ?
               OR sink_region < 0 OR sink_region >= ?
               OR source_fallback NOT IN (0,1)
               OR sink_fallback NOT IN (0,1)
            LIMIT 1
            """,
            (region_count, region_count),
        ).fetchone()
        if invalid is not None:
            raise ValidationError(
                f"position hint {invalid[0]!r} is malformed"
            )
    else:
        raw = positions.get("entries")
        if not isinstance(raw, list):
            raise ValidationError("positions.entries: expected an array")
        connection.execute(
            """
            CREATE TABLE position_rows(
              entry_id TEXT PRIMARY KEY,
              source_y REAL NOT NULL,
              sink_y REAL NOT NULL,
              source_region INTEGER NOT NULL,
              sink_region INTEGER NOT NULL,
              source_fallback INTEGER NOT NULL,
              sink_fallback INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        fallback_count = 0
        pending: list[tuple[Any, ...]] = []
        try:
            for item in raw:
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
                            f"position {entry_id}.{field}: "
                            "expected 0 <= value <= 1"
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
                pending.append(
                    (
                        entry_id,
                        float(item["source_y"]),
                        float(item["sink_y"]),
                        item["source_region"],
                        item["sink_region"],
                        int(item["source_fallback"]),
                        int(item["sink_fallback"]),
                    )
                )
                if len(pending) >= 4096:
                    connection.executemany(
                        "INSERT INTO position_rows VALUES (?,?,?,?,?,?,?)",
                        pending,
                    )
                    pending.clear()
            if pending:
                connection.executemany(
                    "INSERT INTO position_rows VALUES (?,?,?,?,?,?,?)",
                    pending,
                )
        except sqlite3.IntegrityError as error:
            raise ValidationError("duplicate position hint") from error
        position_count = connection.execute(
            "SELECT count(*) FROM position_rows"
        ).fetchone()[0]
    covered_count = connection.execute(
        """
        SELECT count(*)
        FROM validation_rows AS validation
        JOIN position_rows AS position
          ON position.entry_id=validation.entry_id
        """
    ).fetchone()[0]
    validation_count = connection.execute(
        "SELECT count(*) FROM validation_rows"
    ).fetchone()[0]
    if position_count != validation_count or covered_count != validation_count:
        missing = [
            row[0]
            for row in connection.execute(
                """
                SELECT validation.entry_id
                FROM validation_rows AS validation
                LEFT JOIN position_rows AS position
                  ON position.entry_id=validation.entry_id
                WHERE position.entry_id IS NULL
                ORDER BY validation.entry_id LIMIT 5
                """
            )
        ]
        extra = [
            row[0]
            for row in connection.execute(
                """
                SELECT position.entry_id
                FROM position_rows AS position
                LEFT JOIN validation_rows AS validation
                  ON validation.entry_id=position.entry_id
                WHERE validation.entry_id IS NULL
                ORDER BY position.entry_id LIMIT 5
                """
            )
        ]
        raise ValidationError(
            "position hints must cover schedule entries exactly; "
            f"missing={missing}, extra={extra}"
        )
    metrics = positions.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("signals") != position_count
        or metrics.get("endpoint_centroid_fallbacks") != fallback_count
    ):
        raise ValidationError("position hint metrics do not agree")


def _stream_indexed_assignment_metrics(
    connection: sqlite3.Connection,
    *,
    baseline: bool,
    crossing_weight: float,
    position_weight: float,
) -> Dict[str, float]:
    """Recompute metrics while retaining only one scalar group accumulator."""

    if baseline:
        domains = list(
            connection.execute(
                """
                SELECT DISTINCT link_id,source_fpga,sink_fpga
                FROM validation_rows
                ORDER BY link_id,source_fpga,sink_fpga
                """
            )
        )
        connection.execute(
            """
            CREATE TABLE baseline_domains(
              link_id TEXT NOT NULL,
              source_fpga TEXT NOT NULL,
              sink_fpga TEXT NOT NULL,
              domain_index INTEGER NOT NULL,
              PRIMARY KEY(link_id,source_fpga,sink_fpga)
            ) WITHOUT ROWID
            """
        )
        connection.executemany(
            "INSERT INTO baseline_domains VALUES (?,?,?,?)",
            (
                (row[0], row[1], row[2], index)
                for index, row in enumerate(domains)
            ),
        )
        rows = connection.execute(
            """
            SELECT domains.domain_index * 4294967296
                     + validation.schedule_lane AS metric_group,
                   position.source_y,position.sink_y,
                   position.source_region,position.sink_region,
                   links.capacity,validation.schedule_lane
            FROM validation_rows AS validation
            JOIN position_rows AS position
              ON position.entry_id=validation.entry_id
            JOIN link_specs AS links ON links.link_id=validation.link_id
            JOIN baseline_domains AS domains
              ON domains.link_id=validation.link_id
             AND domains.source_fpga=validation.source_fpga
             AND domains.sink_fpga=validation.sink_fpga
            ORDER BY metric_group,validation.entry_id
            """
        )
    else:
        rows = connection.execute(
            """
            SELECT validation.group_id AS metric_group,
                   position.source_y,position.sink_y,
                   position.source_region,position.sink_region,
                   links.capacity,validation.physical_lane
            FROM validation_rows AS validation
            JOIN position_rows AS position
              ON position.entry_id=validation.entry_id
            JOIN link_specs AS links ON links.link_id=validation.link_id
            ORDER BY metric_group,validation.entry_id
            """
        )
    current_group = None
    crossing = 0
    samples = 0
    mean = 0.0
    group_sse = 0.0
    crossing_bits = 0
    position_sse = 0.0
    pin_distance = 0.0
    for row in rows:
        group = int(row[0])
        if current_group is not None and group != current_group:
            crossing_bits += bin(crossing).count("1")
            position_sse += group_sse
            crossing = 0
            samples = 0
            mean = 0.0
            group_sse = 0.0
        current_group = group
        crossing |= (1 << int(row[3])) | (1 << (32 + int(row[4])))
        for value in (float(row[1]), float(row[2])):
            samples += 1
            delta = value - mean
            mean += delta / samples
            group_sse += delta * (value - mean)
        capacity = int(row[5])
        pin = int(row[6])
        pin_y = 0.5 if capacity == 1 else pin / (capacity - 1)
        pin_distance += abs(float(row[1]) - pin_y)
        pin_distance += abs(float(row[2]) - pin_y)
    if current_group is not None:
        crossing_bits += bin(crossing).count("1")
        position_sse += group_sse
    return {
        "objective": crossing_weight * crossing_bits
        + position_weight * position_sse,
        "crossing_bits": float(crossing_bits),
        "position_sse": position_sse,
        "pin_distance": pin_distance,
    }


def _validate_indexed_bank_pin_assignment(
    connection: sqlite3.Connection,
    schedule: Mapping[str, Any],
    platform: Platform,
) -> None:
    _validate_bank_pin_rows(
        schedule,
        platform,
        connection.execute(
            """
            SELECT link_id,source_fpga,sink_fpga,
                   schedule_lane,physical_lane
            FROM validation_rows ORDER BY entry_id
            """
        ),
    )


def _validate_indexed_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    plan: Mapping[str, Any],
    weights: Mapping[str, Any],
    metrics_document: Mapping[str, Any],
) -> Dict[str, Any]:
    connection, signal_count = _open_indexed_pin_plan_validation(
        schedule, plan, platform
    )
    try:
        _stage_indexed_positions(connection, schedule, positions)
        unknown_link = connection.execute(
            """
            SELECT validation.entry_id
            FROM validation_rows AS validation
            LEFT JOIN link_specs AS links
              ON links.link_id=validation.link_id
            WHERE links.link_id IS NULL LIMIT 1
            """
        ).fetchone()
        if unknown_link is not None:
            raise ValidationError(
                f"pin assignment for {unknown_link[0]!r} is invalid"
            )
        invalid = connection.execute(
            """
            SELECT validation.entry_id
            FROM validation_rows AS validation
            JOIN link_specs AS links ON links.link_id=validation.link_id
            WHERE validation.group_id < 0
               OR validation.physical_lane < 0
               OR validation.physical_lane >= links.capacity
               OR validation.tdm_ratio < 1
            LIMIT 1
            """
        ).fetchone()
        if invalid is not None:
            raise ValidationError(
                f"pin assignment for {invalid[0]!r} is invalid"
            )
        changed_lane = connection.execute(
            """
            SELECT entry_id FROM validation_rows
            WHERE logical_lane != schedule_lane LIMIT 1
            """
        ).fetchone()
        if changed_lane is not None:
            raise ValidationError(
                f"pin assignment for {changed_lane[0]!r} "
                "changed logical lane"
            )
        _validate_indexed_bank_pin_assignment(connection, schedule, platform)
        collision = connection.execute(
            """
            SELECT validation.link_id,
                   CASE WHEN links.shared_bidirectional
                        THEN 'shared_bidirectional'
                        ELSE validation.source_fpga END,
                   CASE WHEN links.shared_bidirectional
                        THEN 'shared_bidirectional'
                        ELSE validation.sink_fpga END,
                   validation.physical_lane,validation.slot
            FROM validation_rows AS validation
            JOIN link_specs AS links ON links.link_id=validation.link_id
            GROUP BY 1,2,3,4,5 HAVING count(*) > 1 LIMIT 1
            """
        ).fetchone()
        if collision is not None:
            raise ValidationError(
                f"physical lane/slot collision at {tuple(collision)}"
            )
        multiple_groups = connection.execute(
            """
            SELECT validation.link_id,
                   CASE WHEN links.shared_bidirectional
                        THEN 'shared_bidirectional'
                        ELSE validation.source_fpga END,
                   CASE WHEN links.shared_bidirectional
                        THEN 'shared_bidirectional'
                        ELSE validation.sink_fpga END,
                   validation.physical_lane
            FROM validation_rows AS validation
            JOIN link_specs AS links ON links.link_id=validation.link_id
            GROUP BY 1,2,3,4
            HAVING min(validation.group_id) != max(validation.group_id)
            LIMIT 1
            """
        ).fetchone()
        if multiple_groups is not None:
            raise ValidationError(
                f"physical pin {tuple(multiple_groups)} "
                "is assigned to multiple groups"
            )
        group_count = 0
        for row in connection.execute(
            """
            SELECT validation.group_id,
                   count(DISTINCT json_array(
                     validation.link_id,
                     validation.source_fpga,
                     validation.sink_fpga
                   )) AS domain_count,
                   count(DISTINCT validation.physical_lane) AS pin_count,
                   count(DISTINCT validation.link_id) AS link_count,
                   count(DISTINCT json_array(
                     validation.link_id,
                     CASE WHEN links.shared_bidirectional
                          THEN 'shared_bidirectional'
                          ELSE validation.source_fpga END,
                     CASE WHEN links.shared_bidirectional
                          THEN 'shared_bidirectional'
                          ELSE validation.sink_fpga END
                   )) AS physical_domain_count,
                   min(validation.link_id) AS link_id
            FROM validation_rows AS validation
            JOIN link_specs AS links ON links.link_id=validation.link_id
            GROUP BY validation.group_id ORDER BY validation.group_id
            """
        ):
            group_count += 1
            link = next(
                item for item in platform.links if item.id == row[5]
            )
            shared_chimew_bundle = (
                plan.get("provider") == CHIMEW_PIN_PLAN_PROVIDER
                and link.bank_count is None
                and row[1] == 2
                and row[3] == 1
                and row[4] == 1
                and link.direction == "full_duplex"
                and link.capacity_sharing == "shared_bidirectional"
            )
            if (
                (row[1] != 1 and not shared_chimew_bundle)
                or row[2] != 1
            ):
                raise ValidationError(
                    f"group {row[0]} is not homogeneous"
                )
        capacity_violation = connection.execute(
            """
            SELECT group_id
            FROM validation_rows
            GROUP BY group_id,link_id,source_fpga,sink_fpga
            HAVING min(tdm_ratio) != max(tdm_ratio)
                OR count(*) > min(tdm_ratio)
            LIMIT 1
            """
        ).fetchone()
        if capacity_violation is None:
            capacity_violation = connection.execute(
                """
                SELECT group_id FROM validation_rows
                GROUP BY group_id,slot HAVING count(*) > 1 LIMIT 1
                """
            ).fetchone()
        if capacity_violation is not None:
            raise ValidationError(
                f"group {capacity_violation[0]} violates TDM capacity"
            )
        metrics = _stream_indexed_assignment_metrics(
            connection,
            baseline=False,
            crossing_weight=float(weights["crossing"]),
            position_weight=float(weights["position"]),
        )
        baseline_metrics = _stream_indexed_assignment_metrics(
            connection,
            baseline=True,
            crossing_weight=float(weights["crossing"]),
            position_weight=float(weights["position"]),
        )
    finally:
        connection.close()
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
        metrics_document.get("signals") != signal_count
        or metrics_document.get("groups") != group_count
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
        * (baseline_metrics["pin_distance"] - metrics["pin_distance"])
        / baseline_metrics["pin_distance"]
        if baseline_metrics["pin_distance"]
        else 0.0
    )
    return {
        "status": "pass",
        "signals": signal_count,
        "groups": group_count,
        "physical_lane_slot_collisions": 0,
        **metrics,
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
        "objective_improvement_percent": objective_improvement,
        "crossing_bits_reduction": (
            baseline_metrics["crossing_bits"] - metrics["crossing_bits"]
        ),
        "pin_distance_improvement_percent": pin_distance_improvement,
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
    if _indexed_pin_plan_validation_available(schedule, plan):
        return _validate_indexed_pin_plan(
            schedule,
            platform,
            positions,
            plan,
            weights,
            metrics_document,
        )
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


def build_pin_plan(
    schedule: Mapping[str, Any],
    platform: Platform,
    positions: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    refinement_iterations: int = 100,
    crossing_weight: float = 1.0,
    position_weight: float = 1.0,
    output_path: Optional[Path] = None,
) -> Mapping[str, Any]:
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
    if output_path is not None:
        from .pin_plan_storage import write_pin_plan_storage

        indexed_plan = write_pin_plan_storage(output_path, plan)
        validate_pin_plan(schedule, platform, positions, indexed_plan)
        return indexed_plan
    validate_pin_plan(schedule, platform, positions, plan)
    return plan
