"""Test-only fixture publisher; production uses the native typed owner."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged, publish_sqlite_store, commit_store_selector
from emuflow.transport.position_hints_storage import PositionHintsView

POSITION_HINTS_LOGICAL_SCHEMA = "emuflow.signal-position-hints/v1"
POSITION_HINTS_STORAGE_SCHEMA = "emuflow.signal-position-hints-storage/v1"
POSITION_HINTS_APPLICATION_ID = 0x454D5548
POSITION_HINTS_USER_VERSION = 1

def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )

def _logical_sha256(
    header: Mapping[str, Any], records: int, entries_sha256: str
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "header": header,
                "entries": {"records": records, "sha256": entries_sha256},
            }
        ).encode("utf-8")
    ).hexdigest()

def write_position_hints_storage(
    path: Path,
    positions: Mapping[str, Any],
    *,
    derive_metrics: bool = False,
    source_bindings=(),
) -> PositionHintsView:
    if positions.get("schema") != POSITION_HINTS_LOGICAL_SCHEMA:
        raise ValidationError("position hints have an unsupported schema")
    source_bindings = tuple(source_bindings)
    require_store_sources_unchanged(source_bindings)
    records = positions.get("entries")
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise ValidationError("position hint entries must be iterable")
    header = {key: value for key, value in positions.items() if key != "entries"}
    region_count = header.get("region_count")
    if (
        isinstance(region_count, bool)
        or not isinstance(region_count, int)
        or not 1 <= region_count <= 31
    ):
        raise ValidationError("position hints have an invalid region count")
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_artifact: Path | None = None
    if path.is_file():
        try:
            previous_manifest = read_json(path)
        except (OSError, ValueError):
            previous_manifest = {}
        previous_name = previous_manifest.get("artifact")
        if (
            previous_manifest.get("schema") == POSITION_HINTS_STORAGE_SCHEMA
            and isinstance(previous_name, str)
            and Path(previous_name).name == previous_name
            and Path(previous_name).suffix == ".sqlite"
        ):
            previous_artifact = path.with_name(previous_name)
    temporary = path.with_name(
        f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp"
    )
    connection = sqlite3.connect(temporary)
    digest = hashlib.sha256()
    count = 0
    fallbacks = 0
    rows: list[tuple[Any, ...]] = []
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={POSITION_HINTS_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={POSITION_HINTS_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE entries(
              seq INTEGER NOT NULL UNIQUE,
              schedule_entry TEXT PRIMARY KEY,
              source_y REAL NOT NULL,
              sink_y REAL NOT NULL,
              source_region INTEGER NOT NULL,
              sink_region INTEGER NOT NULL,
              source_fallback INTEGER NOT NULL,
              sink_fallback INTEGER NOT NULL,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        for record in records:
            if not isinstance(record, Mapping):
                raise ValidationError("position hint is malformed")
            entry_id = record.get("schedule_entry")
            source_y = record.get("source_y")
            sink_y = record.get("sink_y")
            source_region = record.get("source_region")
            sink_region = record.get("sink_region")
            source_fallback = record.get("source_fallback")
            sink_fallback = record.get("sink_fallback")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValidationError("position hint is malformed")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
                for value in (source_y, sink_y)
            ):
                raise ValidationError(
                    f"position {entry_id}: expected 0 <= value <= 1"
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < region_count
                for value in (source_region, sink_region)
            ):
                raise ValidationError(
                    f"position {entry_id}: invalid region"
                )
            if not isinstance(source_fallback, bool) or not isinstance(
                sink_fallback, bool
            ):
                raise ValidationError(
                    f"position {entry_id}: invalid fallback flag"
                )
            encoded = _canonical(record)
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
            rows.append(
                (
                    count,
                    entry_id,
                    float(source_y),
                    float(sink_y),
                    source_region,
                    sink_region,
                    int(source_fallback),
                    int(sink_fallback),
                    encoded,
                )
            )
            count += 1
            fallbacks += int(source_fallback) + int(sink_fallback)
            if len(rows) >= 4096:
                connection.executemany(
                    "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)", rows
                )
                rows.clear()
        if rows:
            connection.executemany(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
        derived_metrics = {
            "signals": count,
            "endpoint_centroid_fallbacks": fallbacks,
        }
        if derive_metrics and "metrics" not in header:
            header["metrics"] = derived_metrics
        elif header.get("metrics") != derived_metrics:
            raise ValidationError("position hint metrics do not agree")
        entries_sha256 = digest.hexdigest()
        logical_sha256 = _logical_sha256(header, count, entries_sha256)
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("entries_sha256", _canonical(entries_sha256)),
                ("logical_sha256", _canonical(logical_sha256)),
            ),
        )
        connection.commit()
        connection.close()
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError("position hints repeat a schedule entry") from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    try:
        store_sha256 = file_sha256(temporary)
        artifact = path.with_name(f"{path.stem}.{store_sha256}.sqlite")
        created_artifact = not artifact.exists()
        if created_artifact:
            publish_sqlite_store(
                temporary, artifact, "position-hints-store-v1", sources=source_bindings
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("position-hint artifact digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": POSITION_HINTS_STORAGE_SCHEMA,
        "logical_schema": POSITION_HINTS_LOGICAL_SCHEMA,
        "artifact": artifact.name,
        "application_id": POSITION_HINTS_APPLICATION_ID,
        "user_version": POSITION_HINTS_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "entries": {"records": count, "sha256": entries_sha256},
        "logical_sha256": logical_sha256,
    }
    view = commit_store_selector(
        path,
        manifest,
        source_bindings,
        artifact,
        created_artifact,
        validate=lambda: PositionHintsView(path),
    )
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view
