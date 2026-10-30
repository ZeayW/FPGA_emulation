"""Test-only fixture publisher; production uses the native typed owner."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.transport.pin_planning import PIN_PLAN_SCHEMA
from emuflow.storage.store_publication import publish_sqlite_store, commit_store_selector, require_store_sources_unchanged, bind_store_source
from emuflow.transport.pin_plan_storage import PinPlanView

PIN_PLAN_STORAGE_SCHEMA = "emuflow.pin-plan-storage/v1"
PIN_PLAN_APPLICATION_ID = 0x454D5550
PIN_PLAN_USER_VERSION = 1

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

def write_pin_plan_storage(
    path: Path, plan: Mapping[str, Any], *, source_bindings=()
) -> PinPlanView:
    if plan.get("schema") != PIN_PLAN_SCHEMA:
        raise ValidationError("pin plan has an unsupported schema")
    source_bindings = tuple(source_bindings)
    require_store_sources_unchanged(source_bindings)
    records = plan.get("entries")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValidationError("pin plan entries must be a sequence")
    header = {key: value for key, value in plan.items() if key != "entries"}
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_artifact: Path | None = None
    if path.is_file():
        try:
            previous_manifest = read_json(path)
        except (OSError, ValueError):
            previous_manifest = {}
        previous_name = previous_manifest.get("artifact")
        if (
            previous_manifest.get("schema") == PIN_PLAN_STORAGE_SCHEMA
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
    rows: list[tuple[Any, ...]] = []
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={PIN_PLAN_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={PIN_PLAN_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE entries(
              seq INTEGER NOT NULL UNIQUE,
              schedule_entry TEXT PRIMARY KEY,
              group_id INTEGER NOT NULL,
              physical_lane INTEGER NOT NULL,
              logical_lane INTEGER NOT NULL,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        for record in records:
            if not isinstance(record, Mapping):
                raise ValidationError("pin-plan entry is malformed")
            entry_id = record.get("schedule_entry")
            group = record.get("group")
            physical_lane = record.get("physical_lane")
            logical_lane = record.get("logical_lane")
            if (
                not isinstance(entry_id, str)
                or not entry_id
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in (group, physical_lane, logical_lane)
                )
            ):
                raise ValidationError("pin-plan entry is malformed")
            encoded = _canonical(record)
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
            rows.append(
                (count, entry_id, group, physical_lane, logical_lane, encoded)
            )
            count += 1
            if len(rows) >= 4096:
                connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?)", rows)
                rows.clear()
        if rows:
            connection.executemany("INSERT INTO entries VALUES (?,?,?,?,?,?)", rows)
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
        raise ValidationError("pin plan repeats a schedule entry") from error
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
                temporary, artifact, "pin-plan-store-v1", sources=source_bindings
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("pin-plan artifact digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": PIN_PLAN_STORAGE_SCHEMA,
        "logical_schema": PIN_PLAN_SCHEMA,
        "artifact": artifact.name,
        "application_id": PIN_PLAN_APPLICATION_ID,
        "user_version": PIN_PLAN_USER_VERSION,
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
        validate=lambda: PinPlanView(path),
    )
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view
