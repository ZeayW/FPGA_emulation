"""Bounded test snapshots of the current typed ScheduleStore.

Only the production C++ request path is installed. This fixture publisher does
not implement a scheduler and does not produce any retired storage version.
"""

from __future__ import annotations
from pathlib import Path
from collections.abc import Mapping
from typing import Any
import hashlib
import json
import os
import sqlite3
import uuid
from emuflow.errors import ValidationError
from emuflow.io import file_sha256, write_json
from emuflow.tdm_schedule_storage import (
    TDM_SCHEDULE_LOGICAL_SCHEMA,
    TDM_SCHEDULE_SECTIONS,
    TYPED_COMPLETION_RELATION_SCHEMA,
    TYPED_ENTRY_RELATION_SCHEMA,
    TYPED_ROUTE_RELATION_SCHEMA,
    _canonical,
    _pretty_canonical_record,
    _section_identity,
    _typed_completion_canonical,
    _typed_completion_values,
    _typed_entry_canonical,
    _typed_entry_values,
    _typed_route_canonical,
    _typed_route_values,
    _update_digest,
    _validate_typed_entry_values,
)
from emuflow.tdm_schedule_storage import (
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_USER_VERSION,
    TDM_SCHEDULE_APPLICATION_ID,
    _content_addressed_store_name,
    write_tdm_schedule_storage as _write_native,
)
import emuflow.tdm_schedule_storage as _storage

_SCHEMA_DIRECTORY = Path(_storage.__file__).parent / "data/schema"
SCHEDULE_DDL = (_SCHEMA_DIRECTORY / "phase5_schedule.sql").read_text()
SCHEDULE_INDEX_DDL = (_SCHEMA_DIRECTORY / "phase5_schedule_indexes.sql").read_text()


def _publish_fixture_records(
    connection: sqlite3.Connection, schedule: Mapping[str, Any]
) -> tuple[str, str, dict[str, dict[str, Any]], dict[str, int]]:
    """Hash and insert the schedule with one traversal of each large section."""
    compact_digest = hashlib.sha256()
    pretty_digest = hashlib.sha256()
    section_summaries: dict[str, dict[str, Any]] = {}
    relation_counts = {"route_sinks": 0, "route_edges": 0, "completion_arrivals": 0}
    header = {
        key: value
        for (key, value) in schedule.items()
        if key not in TDM_SCHEDULE_SECTIONS
    }
    keys = sorted(schedule)
    _update_digest(compact_digest, "{")
    _update_digest(pretty_digest, "{\n" if keys else "{}\n")
    for key_index, key in enumerate(keys):
        if key_index:
            _update_digest(compact_digest, ",")
        encoded_key = _canonical(key)
        _update_digest(compact_digest, f"{encoded_key}:")
        _update_digest(pretty_digest, f"  {encoded_key}: ")
        value = schedule[key]
        if key in TDM_SCHEDULE_SECTIONS:
            if not isinstance(value, list):
                raise ValidationError(f"schedule.{key}: expected an array")
            section_digest = hashlib.sha256()
            count = len(value)
            _update_digest(compact_digest, "[")
            _update_digest(pretty_digest, "[\n" if count else "[]")
            pending: list[tuple[str, int, str, str]] = []
            pending_entries: list[tuple[Any, ...]] = []
            pending_routes: list[tuple[Any, ...]] = []
            pending_sinks: list[tuple[Any, ...]] = []
            pending_edges: list[tuple[Any, ...]] = []
            pending_completions: list[tuple[Any, ...]] = []
            pending_arrivals: list[tuple[Any, ...]] = []

            def flush_typed() -> None:
                if pending_entries:
                    connection.executemany(
                        "INSERT INTO schedule_entries(seq,entry_id,demand,net,hop,link_id,source_fpga,sink_fpga,capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        pending_entries,
                    )
                    pending_entries.clear()
                if pending_routes:
                    connection.executemany(
                        "INSERT INTO schedule_routes(seq,demand,net,source_fpga) VALUES (?,?,?,?)",
                        pending_routes,
                    )
                    pending_routes.clear()
                if pending_sinks:
                    connection.executemany(
                        "INSERT INTO schedule_route_sinks(route_seq,seq,sink_fpga) VALUES (?,?,?)",
                        pending_sinks,
                    )
                    pending_sinks.clear()
                if pending_edges:
                    connection.executemany(
                        "INSERT INTO schedule_route_edges(route_seq,seq,link_id,source_fpga,sink_fpga) VALUES (?,?,?,?,?)",
                        pending_edges,
                    )
                    pending_edges.clear()
                if pending_completions:
                    connection.executemany(
                        "INSERT INTO schedule_completions(seq,demand,net,source_ready_slot,completion_slot) VALUES (?,?,?,?,?)",
                        pending_completions,
                    )
                    pending_completions.clear()
                if pending_arrivals:
                    connection.executemany(
                        "INSERT INTO schedule_completion_arrivals(completion_seq,seq,sink_fpga,arrival_slot) VALUES (?,?,?,?)",
                        pending_arrivals,
                    )
                    pending_arrivals.clear()

            observed = 0
            source = (
                (
                    (_section_identity(key, item), _canonical(item), item)
                    if isinstance(item, Mapping)
                    else (None, None, None)
                )
                for item in value
            )
            for seq, (identity, encoded, typed_value) in enumerate(source):
                observed += 1
                stored_typed = False
                if key == "entries":
                    if typed_value is None:
                        raise ValidationError(
                            "typed schedule entry cursor is unavailable"
                        )
                    entry_values = (
                        _typed_entry_values(identity, typed_value)
                        if isinstance(typed_value, Mapping)
                        else _validate_typed_entry_values(identity, typed_value)
                    )
                    canonical = _typed_entry_canonical(entry_values)
                    if encoded is not None and canonical != encoded:
                        raise ValidationError(
                            "typed schedule entry disagrees with canonical record"
                        )
                    encoded = canonical
                    pending_entries.append((seq, *entry_values))
                    stored_typed = True
                elif key == "routes":
                    if not isinstance(typed_value, Mapping):
                        raise ValidationError("typed schedule route is malformed")
                    (demand, net, route_source, sinks, edges) = _typed_route_values(
                        identity, typed_value
                    )
                    encoded = _typed_route_canonical(
                        demand, net, route_source, sinks, edges
                    )
                    pending_routes.append((seq, demand, net, route_source))
                    pending_sinks.extend(
                        (
                            (seq, child_seq, sink)
                            for (child_seq, sink) in enumerate(sinks)
                        )
                    )
                    pending_edges.extend(
                        (
                            (seq, child_seq, *edge)
                            for (child_seq, edge) in enumerate(edges)
                        )
                    )
                    relation_counts["route_sinks"] += len(sinks)
                    relation_counts["route_edges"] += len(edges)
                    stored_typed = True
                elif key == "demand_completions":
                    if not isinstance(typed_value, Mapping):
                        raise ValidationError("typed schedule completion is malformed")
                    (demand, net, ready, completion, arrivals) = (
                        _typed_completion_values(identity, typed_value)
                    )
                    encoded = _typed_completion_canonical(
                        demand, net, ready, completion, arrivals
                    )
                    pending_completions.append((seq, demand, net, ready, completion))
                    pending_arrivals.extend(
                        (
                            (seq, child_seq, sink, arrival)
                            for (child_seq, (sink, arrival)) in enumerate(arrivals)
                        )
                    )
                    relation_counts["completion_arrivals"] += len(arrivals)
                    stored_typed = True
                if (
                    not isinstance(identity, str)
                    or not identity
                    or (not isinstance(encoded, str))
                    or (not encoded.startswith("{"))
                    or (not encoded.endswith("}"))
                    or ("\n" in encoded)
                ):
                    raise ValidationError(f"schedule.{key}[{seq}]: expected an object")
                if seq:
                    _update_digest(compact_digest, ",")
                _update_digest(compact_digest, encoded)
                _update_digest(section_digest, encoded)
                _update_digest(section_digest, "\n")
                pretty = _pretty_canonical_record(encoded)
                for line_index, line in enumerate(pretty):
                    _update_digest(pretty_digest, f"    {line}")
                    if line_index + 1 < len(pretty):
                        _update_digest(pretty_digest, "\n")
                if seq + 1 < count:
                    _update_digest(pretty_digest, ",\n")
                if not stored_typed:
                    pending.append((key, seq, identity, encoded))
                if len(pending) >= 4096:
                    connection.executemany(
                        "INSERT INTO records(section,seq,identity,record_json) VALUES (?,?,?,?)",
                        pending,
                    )
                    pending.clear()
                if (
                    max(
                        len(pending_entries),
                        len(pending_routes),
                        len(pending_sinks),
                        len(pending_edges),
                        len(pending_completions),
                        len(pending_arrivals),
                    )
                    >= 4096
                ):
                    flush_typed()
            if observed != count:
                raise ValidationError(
                    f"schedule.{key}: length changed during publication"
                )
            if pending:
                connection.executemany(
                    "INSERT INTO records(section,seq,identity,record_json) VALUES (?,?,?,?)",
                    pending,
                )
            flush_typed()
            _update_digest(compact_digest, "]")
            if count:
                _update_digest(pretty_digest, "\n  ]")
            section_summaries[key] = {
                "records": count,
                "sha256": section_digest.hexdigest(),
            }
        else:
            encoded = _canonical(value)
            _update_digest(compact_digest, encoded)
            pretty = json.dumps(
                value, ensure_ascii=True, indent=2, sort_keys=True
            ).splitlines()
            _update_digest(pretty_digest, pretty[0])
            for line in pretty[1:]:
                _update_digest(pretty_digest, f"\n  {line}")
        if key_index + 1 < len(keys):
            _update_digest(pretty_digest, ",\n")
        else:
            _update_digest(pretty_digest, "\n}\n")
    _update_digest(compact_digest, "}")
    metadata = [
        ("logical_schema", _canonical(TDM_SCHEDULE_LOGICAL_SCHEMA)),
        ("header", _canonical(header)),
        ("logical_sha256", _canonical(compact_digest.hexdigest())),
        # Current consumers bind selector sections to this in-store seal. The
        # fixture computes every section above; it does not claim a solver run.
        ("native_publication_summary", _canonical({"sections": section_summaries})),
    ]
    metadata.extend(
        (
            ("entry_relation_schema", _canonical(TYPED_ENTRY_RELATION_SCHEMA)),
            (
                "entry_relation_records",
                _canonical(section_summaries["entries"]["records"]),
            ),
        )
    )
    metadata.extend(
        (
            ("route_relation_schema", _canonical(TYPED_ROUTE_RELATION_SCHEMA)),
            (
                "route_relation_records",
                _canonical(section_summaries["routes"]["records"]),
            ),
            ("route_relation_sink_records", _canonical(relation_counts["route_sinks"])),
            ("route_relation_edge_records", _canonical(relation_counts["route_edges"])),
            (
                "completion_relation_schema",
                _canonical(TYPED_COMPLETION_RELATION_SCHEMA),
            ),
            (
                "completion_relation_records",
                _canonical(section_summaries["demand_completions"]["records"]),
            ),
            (
                "completion_relation_arrival_records",
                _canonical(relation_counts["completion_arrivals"]),
            ),
        )
    )
    connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", metadata)
    return (
        compact_digest.hexdigest(),
        pretty_digest.hexdigest(),
        section_summaries,
        relation_counts,
    )


def write_tdm_schedule_storage(manifest_path: Path, schedule) -> dict[str, Any]:
    from emuflow.tdm import NativeExactScheduleRequest

    if isinstance(schedule, NativeExactScheduleRequest):
        return _write_native(manifest_path, schedule)
    if (
        not isinstance(schedule, Mapping)
        or schedule.get("schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
    ):
        raise ValidationError("fixture schedule has an invalid schema")
    for section in TDM_SCHEDULE_SECTIONS:
        if not isinstance(schedule.get(section), list):
            raise ValidationError(f"schedule.{section}: expected an array")
    if sum((len(schedule[section]) for section in TDM_SCHEDULE_SECTIONS)) > 65536:
        raise ValidationError("schedule fixture exceeds bounded population")
    storage_schema = TDM_SCHEDULE_STORAGE_SCHEMA
    user_version = TDM_SCHEDULE_USER_VERSION
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    staging_base = manifest_path.with_name("schedule.sqlite")
    temporary = staging_base.with_name(f".{staging_base.name}.{uuid.uuid4().hex}.tmp")
    store_path = staging_base
    store_sha256: str | None = None
    store_bytes: int | None = None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA application_id={TDM_SCHEDULE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={user_version}")
        connection.executescript(SCHEDULE_DDL)
        (logical_sha256, logical_pretty_sha256, section_summaries, relation_counts) = (
            _publish_fixture_records(connection, schedule)
        )
        connection.executescript(SCHEDULE_INDEX_DDL)
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        store_bytes = temporary.stat().st_size
        store_sha256 = file_sha256(temporary)
        store_path = manifest_path.with_name(
            _content_addressed_store_name(store_sha256)
        )
        if store_path.exists():
            if (
                store_path.stat().st_size != store_bytes
                or file_sha256(store_path) != store_sha256
            ):
                raise ValidationError("schedule content-addressed artifact disagrees")
            temporary.unlink()
        else:
            os.replace(temporary, store_path)
    except sqlite3.IntegrityError as error:
        try:
            if connection is not None:
                connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        if "UNIQUE constraint failed" not in str(error):
            raise ValidationError(
                "schedule storage rejected an invalid record payload"
            ) from error
        raise ValidationError(
            "schedule storage contains duplicate record identities"
        ) from error
    except BaseException:
        try:
            if connection is not None:
                connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    assert store_sha256 is not None
    assert store_bytes is not None
    manifest = {
        "schema": storage_schema,
        "logical_schema": TDM_SCHEDULE_LOGICAL_SCHEMA,
        "artifact": store_path.name,
        "application_id": TDM_SCHEDULE_APPLICATION_ID,
        "user_version": user_version,
        "store_bytes": store_bytes,
        "store_sha256": store_sha256,
        "logical_sha256": logical_sha256,
        "logical_pretty_sha256": logical_pretty_sha256,
        "sections": section_summaries,
    }
    manifest["entry_relation"] = {
        "schema": TYPED_ENTRY_RELATION_SCHEMA,
        "records": section_summaries["entries"]["records"],
    }
    manifest["route_relation"] = {
        "schema": TYPED_ROUTE_RELATION_SCHEMA,
        "records": section_summaries["routes"]["records"],
        "sink_records": relation_counts["route_sinks"],
        "edge_records": relation_counts["route_edges"],
    }
    manifest["completion_relation"] = {
        "schema": TYPED_COMPLETION_RELATION_SCHEMA,
        "records": section_summaries["demand_completions"]["records"],
        "arrival_records": relation_counts["completion_arrivals"],
    }
    write_json(manifest_path, manifest, compact=True)
    return manifest
