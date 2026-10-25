"""Indexed Phase 6 Chimew pin-plan storage.

The logical v1 pin-plan contract remains unchanged.  Production Phase 6
publishes its hop-sized records once in SQLite and keeps only the small header
and relation seals in the JSON selector.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import file_sha256, read_json
from .pin_planning import PIN_PLAN_SCHEMA
from .store_publication import (
    publish_sqlite_store,
    commit_store_selector,
    require_store_sources_unchanged,
    bind_store_source,
)


PIN_PLAN_STORAGE_SCHEMA = "emuflow.pin-plan-storage/v1"
PIN_PLAN_APPLICATION_ID = 0x454D5550  # ASCII "EMUP"
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


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class PinPlanEntrySequence(Sequence[Mapping[str, Any]]):
    def __init__(self, store_path: Path, count: int, digest: str):
        self.store_path = store_path
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT record_json FROM entries ORDER BY seq"
            ):
                try:
                    value = json.loads(row[0])
                except json.JSONDecodeError as error:
                    raise ValidationError("pin-plan entry is corrupt") from error
                if not isinstance(value, dict):
                    raise ValidationError("pin-plan entry is not an object")
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("pin-plan entry count disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                "SELECT record_json FROM entries ORDER BY seq "
                "LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError("pin-plan sequence is not contiguous")
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError("pin-plan entry is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("pin-plan entry is not an object")
        return value


class PinPlanView(Mapping[str, Any]):
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        selector_source = bind_store_source(self.manifest_path, "pin-plan-selector-v1")
        manifest = read_json(self.manifest_path)
        if (
            manifest.get("schema") != PIN_PLAN_STORAGE_SCHEMA
            or manifest.get("logical_schema") != PIN_PLAN_SCHEMA
            or manifest.get("application_id") != PIN_PLAN_APPLICATION_ID
            or manifest.get("user_version") != PIN_PLAN_USER_VERSION
            or not isinstance(manifest.get("header"), dict)
            or not isinstance(manifest.get("entries"), dict)
        ):
            raise ValidationError("pin-plan storage manifest is malformed")
        entries = manifest["entries"]
        if (
            isinstance(entries.get("records"), bool)
            or not isinstance(entries.get("records"), int)
            or entries["records"] < 0
            or not isinstance(entries.get("sha256"), str)
            or len(entries["sha256"]) != 64
            or not isinstance(manifest.get("logical_sha256"), str)
            or manifest["logical_sha256"]
            != _logical_sha256(
                manifest["header"], entries["records"], entries["sha256"]
            )
        ):
            raise ValidationError("pin-plan logical seal is malformed")
        artifact = manifest.get("artifact")
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
        ):
            raise ValidationError("pin-plan storage path is invalid")
        store_path = self.manifest_path.with_name(artifact)
        try:
            store_source = bind_store_source(store_path, "pin-plan-store-v1")
            stat = store_path.stat()
        except OSError as error:
            raise ValidationError("pin-plan SQLite store is missing") from error
        if (
            isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != stat.st_size
            or not isinstance(manifest.get("store_sha256"), str)
            or len(manifest["store_sha256"]) != 64
            or file_sha256(store_path) != manifest["store_sha256"]
        ):
            raise ValidationError("pin-plan SQLite store seal disagrees")
        connection = _connect(store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != PIN_PLAN_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != PIN_PLAN_USER_VERSION
            ):
                raise ValidationError("unsupported pin-plan SQLite artifact")
            metadata = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            count = connection.execute("SELECT count(*) FROM entries").fetchone()[0]
            stored_entries_sha256 = metadata.get("entries_sha256")
            stored_logical_sha256 = metadata.get("logical_sha256")
            if (stored_entries_sha256 is None) != (
                stored_logical_sha256 is None
            ):
                raise ValidationError("pin-plan SQLite metadata disagrees")
            if stored_entries_sha256 is None:
                legacy_digest = hashlib.sha256()
                for (encoded,) in connection.execute(
                    "SELECT record_json FROM entries ORDER BY seq"
                ):
                    legacy_digest.update(encoded.encode("utf-8"))
                    legacy_digest.update(b"\n")
                entries_bound = (
                    legacy_digest.hexdigest() == entries["sha256"]
                )
            else:
                entries_bound = (
                    stored_entries_sha256 == entries["sha256"]
                    and stored_logical_sha256
                    == manifest["logical_sha256"]
                )
            if (
                metadata.get("header") != manifest["header"]
                or not entries_bound
                or count != entries["records"]
            ):
                raise ValidationError("pin-plan SQLite metadata disagrees")
        except json.JSONDecodeError as error:
            raise ValidationError("pin-plan SQLite metadata is corrupt") from error
        finally:
            connection.close()
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        self._entries = PinPlanEntrySequence(
            store_path, entries["records"], entries["sha256"]
        )

        self._source_bindings = (selector_source, store_source)
        require_store_sources_unchanged(self._source_bindings)

    def bound_store_sources(self):
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def __getitem__(self, key: str) -> Any:
        if key == "entries":
            return self._entries
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield "entries"

    def __len__(self) -> int:
        return len(self._header) + 1

    def iter_lane_records(self) -> Iterator[tuple[str, int, int]]:
        """Yield identity, physical lane, and logical lane in schedule-ID order."""

        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT schedule_entry,physical_lane,logical_lane "
                "FROM entries ORDER BY length(schedule_entry),schedule_entry"
            ):
                yield str(row[0]), int(row[1]), int(row[2])
        finally:
            connection.close()

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        digest = hashlib.sha256()
        values = []
        for record in self._entries:
            value = dict(record)
            digest.update(_canonical(value).encode("utf-8"))
            digest.update(b"\n")
            values.append(value)
        if verify_records and digest.hexdigest() != self._entries.expected_sha256:
            raise ValidationError("pin-plan entry seal disagrees")
        return {**self._header, "entries": values}


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


def read_pin_plan(
    path: Path, *, materialize: bool = False
) -> Mapping[str, Any]:
    value = read_json(path)
    if value.get("schema") == PIN_PLAN_STORAGE_SCHEMA:
        view = PinPlanView(path)
        return view.materialize() if materialize else view
    return value
