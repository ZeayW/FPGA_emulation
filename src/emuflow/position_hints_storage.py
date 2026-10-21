"""Versioned indexed storage for Phase 6 signal position hints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import file_sha256, read_json, write_json


POSITION_HINTS_LOGICAL_SCHEMA = "emuflow.signal-position-hints/v1"
POSITION_HINTS_STORAGE_SCHEMA = "emuflow.signal-position-hints-storage/v1"
POSITION_HINTS_APPLICATION_ID = 0x454D5548  # ASCII "EMUH"
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


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class PositionHintEntrySequence(Sequence[Mapping[str, Any]]):
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
                    raise ValidationError(
                        "position-hint entry is corrupt"
                    ) from error
                if not isinstance(value, dict):
                    raise ValidationError(
                        "position-hint entry is not an object"
                    )
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("position-hint entry count disagrees")
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
                "SELECT record_json FROM entries ORDER BY seq LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError("position-hint sequence is not contiguous")
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError("position-hint entry is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("position-hint entry is not an object")
        return value


class PositionHintsView(Mapping[str, Any]):
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        manifest = read_json(self.manifest_path)
        if (
            manifest.get("schema") != POSITION_HINTS_STORAGE_SCHEMA
            or manifest.get("logical_schema") != POSITION_HINTS_LOGICAL_SCHEMA
            or manifest.get("application_id") != POSITION_HINTS_APPLICATION_ID
            or manifest.get("user_version") != POSITION_HINTS_USER_VERSION
            or not isinstance(manifest.get("header"), dict)
            or not isinstance(manifest.get("entries"), dict)
        ):
            raise ValidationError("position-hint storage manifest is malformed")
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
            raise ValidationError("position-hint logical seal is malformed")
        artifact = manifest.get("artifact")
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
        ):
            raise ValidationError("position-hint storage path is invalid")
        store_path = self.manifest_path.with_name(artifact)
        try:
            stat = store_path.stat()
        except OSError as error:
            raise ValidationError("position-hint SQLite store is missing") from error
        if (
            isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != stat.st_size
            or not isinstance(manifest.get("store_sha256"), str)
            or len(manifest["store_sha256"]) != 64
            or file_sha256(store_path) != manifest["store_sha256"]
        ):
            raise ValidationError("position-hint SQLite store seal disagrees")
        connection = _connect(store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != POSITION_HINTS_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != POSITION_HINTS_USER_VERSION
            ):
                raise ValidationError("unsupported position-hint SQLite artifact")
            metadata = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            count = connection.execute(
                "SELECT count(*) FROM entries"
            ).fetchone()[0]
            stored_entries_sha256 = metadata.get("entries_sha256")
            stored_logical_sha256 = metadata.get("logical_sha256")
            if (stored_entries_sha256 is None) != (
                stored_logical_sha256 is None
            ):
                raise ValidationError(
                    "position-hint SQLite metadata disagrees"
                )
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
                raise ValidationError(
                    "position-hint SQLite metadata disagrees"
                )
        except json.JSONDecodeError as error:
            raise ValidationError(
                "position-hint SQLite metadata is corrupt"
            ) from error
        finally:
            connection.close()
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(manifest["header"])
        self._entries = PositionHintEntrySequence(
            store_path, entries["records"], entries["sha256"]
        )

    def __getitem__(self, key: str) -> Any:
        if key == "entries":
            return self._entries
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield "entries"

    def __len__(self) -> int:
        return len(self._header) + 1

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT record_json FROM entries ORDER BY schedule_entry"
            ):
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError(
                        "position-hint entry is not an object"
                    )
                yield value
        except json.JSONDecodeError as error:
            raise ValidationError("position-hint entry is corrupt") from error
        finally:
            connection.close()

    def attach_validation_relation(
        self, connection: sqlite3.Connection
    ) -> tuple[int, int]:
        """Attach a typed read-only relation as ``position_rows``."""

        connection.execute(
            "ATTACH DATABASE ? AS position_store",
            (
                f"file:{self.store_path.resolve()}?mode=ro&immutable=1",
            ),
        )
        mismatch = connection.execute(
            """
            SELECT schedule_entry FROM position_store.entries
            WHERE schedule_entry IS NOT json_extract(
                    record_json,'$.schedule_entry'
                  )
               OR source_y IS NOT json_extract(record_json,'$.source_y')
               OR sink_y IS NOT json_extract(record_json,'$.sink_y')
               OR source_region IS NOT json_extract(
                    record_json,'$.source_region'
                  )
               OR sink_region IS NOT json_extract(
                    record_json,'$.sink_region'
                  )
               OR source_fallback IS NOT json_extract(
                    record_json,'$.source_fallback'
                  )
               OR sink_fallback IS NOT json_extract(
                    record_json,'$.sink_fallback'
                  )
            LIMIT 1
            """
        ).fetchone()
        if mismatch is not None:
            raise ValidationError(
                f"position-hint indexed record {mismatch[0]!r} disagrees"
            )
        connection.execute(
            """
            CREATE TEMP VIEW position_rows AS
            SELECT schedule_entry AS entry_id,
                   source_y,sink_y,source_region,sink_region,
                   source_fallback,sink_fallback
            FROM position_store.entries
            """
        )
        row = connection.execute(
            """
            SELECT count(*),coalesce(sum(source_fallback+sink_fallback),0)
            FROM position_store.entries
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        digest = hashlib.sha256()
        values = []
        for record in self._entries:
            value = dict(record)
            digest.update(_canonical(value).encode("utf-8"))
            digest.update(b"\n")
            values.append(value)
        if verify_records and digest.hexdigest() != self._entries.expected_sha256:
            raise ValidationError("position-hint entry seal disagrees")
        return {**self._header, "entries": values}


def write_position_hints_storage(
    path: Path,
    positions: Mapping[str, Any],
    *,
    derive_metrics: bool = False,
) -> PositionHintsView:
    if positions.get("schema") != POSITION_HINTS_LOGICAL_SCHEMA:
        raise ValidationError("position hints have an unsupported schema")
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
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
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
    store_sha256 = file_sha256(temporary)
    artifact = path.with_name(f"{path.stem}.{store_sha256}.sqlite")
    created_artifact = not artifact.exists()
    if created_artifact:
        os.replace(temporary, artifact)
    else:
        if file_sha256(artifact) != store_sha256:
            temporary.unlink(missing_ok=True)
            raise ValidationError("position-hint artifact digest collision")
        temporary.unlink()
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
    try:
        write_json(path, manifest, compact=True)
    except BaseException:
        if created_artifact and artifact != previous_artifact:
            artifact.unlink(missing_ok=True)
        raise
    view = PositionHintsView(path)
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view


def read_position_hints(
    path: Path, *, materialize: bool = False
) -> Mapping[str, Any]:
    value = read_json(path)
    if value.get("schema") == POSITION_HINTS_STORAGE_SCHEMA:
        view = PositionHintsView(path)
        return view.materialize() if materialize else view
    return value
