"""Versioned indexed storage for the Phase 5 schedule contract.

The JSON file at the public boundary is a compact, relocatable manifest.  The
hop-sized ordered relations live once in SQLite and are exposed through a lazy
read-only view.  ``read_json`` remains the explicit compatibility API that
materializes the historical logical document.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .io import file_sha256, write_json


LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v1"
TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v2"
TDM_SCHEDULE_STORAGE_SCHEMAS = {
    LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_STORAGE_SCHEMA,
}
TDM_SCHEDULE_LOGICAL_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_SCHEDULE_APPLICATION_ID = 0x454D5554  # ASCII "EMUT"
TDM_SCHEDULE_USER_VERSION = 1
TDM_SCHEDULE_STORE_NAME = "schedule.sqlite"
TDM_SCHEDULE_SECTIONS = (
    "routes",
    "entries",
    "demand_completions",
    "domain_schedules",
)


DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE records(
  section TEXT NOT NULL,
  seq INTEGER NOT NULL,
  identity TEXT,
  record_json TEXT NOT NULL,
  PRIMARY KEY(section,seq),
  UNIQUE(section,identity)
) WITHOUT ROWID;
"""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _streaming_digest(
    value: Any,
    *,
    indent: int | None = None,
    trailing_newline: bool = False,
) -> str:
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
        sort_keys=True,
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    if trailing_newline:
        digest.update(b"\n")
    return digest.hexdigest()


def _section_identity(section: str, record: Mapping[str, Any]) -> str | None:
    key = "key" if section == "domain_schedules" else "id"
    value = record.get(key)
    if value is None and section == "demand_completions":
        value = record.get("demand")
    if value is None:
        raise ValidationError(
            f"schedule {section} record has no identity"
        )
    if not isinstance(value, str) or not value:
        raise ValidationError(
            f"schedule {section} record has an invalid identity"
        )
    return value


def _raw_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"{path}: schedule manifest is unreadable") from error
    if not isinstance(value, dict):
        raise ValidationError(f"{path}: schedule manifest root is not an object")
    return value


def _descriptor_store_path(
    manifest_path: Path, descriptor: Mapping[str, Any]
) -> Path:
    artifact = descriptor.get("artifact")
    if (
        not isinstance(artifact, str)
        or not artifact
        or Path(artifact).name != artifact
    ):
        raise ValidationError("schedule storage artifact path is invalid")
    return manifest_path.with_name(artifact)


def _validate_manifest(value: Mapping[str, Any], path: Path) -> Path:
    if (
        value.get("schema") not in TDM_SCHEDULE_STORAGE_SCHEMAS
        or value.get("logical_schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
        or value.get("application_id") != TDM_SCHEDULE_APPLICATION_ID
        or value.get("user_version") != TDM_SCHEDULE_USER_VERSION
        or not isinstance(value.get("logical_sha256"), str)
        or len(value["logical_sha256"]) != 64
        or not isinstance(value.get("store_sha256"), str)
        or len(value["store_sha256"]) != 64
        or isinstance(value.get("store_bytes"), bool)
        or not isinstance(value.get("store_bytes"), int)
        or value["store_bytes"] < 0
        or not isinstance(value.get("sections"), dict)
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    if value.get("schema") == TDM_SCHEDULE_STORAGE_SCHEMA and (
        not isinstance(value.get("logical_pretty_sha256"), str)
        or len(value["logical_pretty_sha256"]) != 64
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    sections = value["sections"]
    if set(sections) != set(TDM_SCHEDULE_SECTIONS):
        raise ValidationError(f"{path}: schedule storage sections disagree")
    for section in TDM_SCHEDULE_SECTIONS:
        record = sections[section]
        if (
            not isinstance(record, dict)
            or isinstance(record.get("records"), bool)
            or not isinstance(record.get("records"), int)
            or record["records"] < 0
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise ValidationError(
                f"{path}: schedule storage section {section!r} is malformed"
            )
    return _descriptor_store_path(path, value)


def _update_digest(digest: Any, value: str) -> None:
    digest.update(value.encode("utf-8"))


def _publish_logical_schedule(
    connection: sqlite3.Connection,
    schedule: Mapping[str, Any],
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    """Hash and insert the schedule with one traversal of each large section."""

    compact_digest = hashlib.sha256()
    pretty_digest = hashlib.sha256()
    section_summaries: dict[str, dict[str, Any]] = {}
    header = {
        key: value
        for key, value in schedule.items()
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
            pending: list[tuple[str, int, str | None, str]] = []
            observed = 0
            for seq, item in enumerate(value):
                observed += 1
                if not isinstance(item, Mapping):
                    raise ValidationError(
                        f"schedule.{key}[{seq}]: expected an object"
                    )
                encoded = _canonical(item)
                if seq:
                    _update_digest(compact_digest, ",")
                _update_digest(compact_digest, encoded)
                _update_digest(section_digest, encoded)
                _update_digest(section_digest, "\n")
                pretty = json.dumps(
                    item,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                ).splitlines()
                for line_index, line in enumerate(pretty):
                    _update_digest(pretty_digest, f"    {line}")
                    if line_index + 1 < len(pretty):
                        _update_digest(pretty_digest, "\n")
                if seq + 1 < count:
                    _update_digest(pretty_digest, ",\n")
                pending.append(
                    (key, seq, _section_identity(key, item), encoded)
                )
                if len(pending) >= 4096:
                    connection.executemany(
                        "INSERT INTO records(section,seq,identity,record_json) "
                        "VALUES (?,?,?,?)",
                        pending,
                    )
                    pending.clear()
            if observed != count:
                raise ValidationError(
                    f"schedule.{key}: length changed during publication"
                )
            if pending:
                connection.executemany(
                    "INSERT INTO records(section,seq,identity,record_json) "
                    "VALUES (?,?,?,?)",
                    pending,
                )
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
                value,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ).splitlines()
            _update_digest(pretty_digest, pretty[0])
            for line in pretty[1:]:
                _update_digest(pretty_digest, f"\n  {line}")
        if key_index + 1 < len(keys):
            _update_digest(pretty_digest, ",\n")
        else:
            _update_digest(pretty_digest, "\n}\n")
    _update_digest(compact_digest, "}")
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)",
        (
            ("logical_schema", _canonical(TDM_SCHEDULE_LOGICAL_SCHEMA)),
            ("header", _canonical(header)),
            ("logical_sha256", _canonical(compact_digest.hexdigest())),
        ),
    )
    return (
        compact_digest.hexdigest(),
        pretty_digest.hexdigest(),
        section_summaries,
    )


def write_tdm_schedule_storage(
    manifest_path: Path,
    schedule: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically publish the indexed store, then its compact commit manifest."""

    if schedule.get("schema") != TDM_SCHEDULE_LOGICAL_SCHEMA:
        raise ValidationError("cannot store an unsupported TDM schedule schema")
    for section in TDM_SCHEDULE_SECTIONS:
        records = schedule.get(section)
        if not isinstance(records, list):
            raise ValidationError(f"schedule.{section}: expected an array")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    store_path = manifest_path.with_name(TDM_SCHEDULE_STORE_NAME)
    temporary = store_path.with_name(
        f".{store_path.name}.{uuid.uuid4().hex}.tmp"
    )
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA application_id={TDM_SCHEDULE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={TDM_SCHEDULE_USER_VERSION}")
        connection.executescript(DDL)
        (
            logical_sha256,
            logical_pretty_sha256,
            section_summaries,
        ) = _publish_logical_schedule(connection, schedule)
        connection.commit()
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, store_path)
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "schedule storage contains duplicate record identities"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise

    manifest = {
        "schema": TDM_SCHEDULE_STORAGE_SCHEMA,
        "logical_schema": TDM_SCHEDULE_LOGICAL_SCHEMA,
        "artifact": store_path.name,
        "application_id": TDM_SCHEDULE_APPLICATION_ID,
        "user_version": TDM_SCHEDULE_USER_VERSION,
        "store_bytes": store_path.stat().st_size,
        "store_sha256": file_sha256(store_path),
        "logical_sha256": logical_sha256,
        "logical_pretty_sha256": logical_pretty_sha256,
        "sections": section_summaries,
    }
    # The manifest is the publication commit point.  A reader observing the
    # previous manifest continues to reject any mismatched store identity.
    write_json(manifest_path, manifest, compact=True)
    return manifest


class ScheduleRecordSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        section: str,
        count: int,
        digest: str,
    ):
        self._store_path = store_path
        self.section = section
        self._count = count
        self.expected_sha256 = digest

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT record_json FROM records "
                "WHERE section=? ORDER BY seq",
                (self.section,),
            )
            for row in rows:
                try:
                    value = json.loads(row[0])
                except json.JSONDecodeError as error:
                    raise ValidationError(
                        f"schedule {self.section} record is corrupt"
                    ) from error
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"schedule {self.section} record is not an object"
                    )
                yield value
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT record_json FROM records WHERE section=? AND seq=?",
                (self.section, index),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError(
                f"schedule {self.section} sequence is not contiguous"
            )
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"schedule {self.section} record is corrupt"
            ) from error
        if not isinstance(value, dict):
            raise ValidationError(
                f"schedule {self.section} record is not an object"
            )
        return value

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT record_json FROM records "
                "WHERE section=? ORDER BY identity",
                (self.section,),
            )
            for row in rows:
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError(
                        f"schedule {self.section} record is not an object"
                    )
                yield value
        except json.JSONDecodeError as error:
            raise ValidationError(
                f"schedule {self.section} record is corrupt"
            ) from error
        finally:
            connection.close()


class TDMScheduleView(Mapping[str, Any]):
    """Lazy, relocatable view over a sealed schedule store."""

    def __init__(self, manifest_path: Path, *, verify_store_hash: bool = True):
        self.manifest_path = manifest_path.resolve()
        self.manifest = _raw_manifest(self.manifest_path)
        store_path = _validate_manifest(self.manifest, self.manifest_path)
        try:
            stat = store_path.stat()
        except OSError as error:
            raise ValidationError("schedule SQLite store is missing") from error
        if stat.st_size != self.manifest["store_bytes"]:
            raise ValidationError("schedule SQLite store size disagrees")
        if (
            verify_store_hash
            and file_sha256(store_path) != self.manifest["store_sha256"]
        ):
            raise ValidationError("schedule SQLite store digest disagrees")
        self.store_path = store_path
        connection = sqlite3.connect(
            f"file:{store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != TDM_SCHEDULE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != TDM_SCHEDULE_USER_VERSION
            ):
                raise ValidationError("unsupported schedule SQLite artifact")
            metadata = {
                row[0]: json.loads(row[1])
                for row in connection.execute(
                    "SELECT key,value FROM metadata"
                )
            }
            if (
                metadata.get("logical_schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
                or metadata.get("logical_sha256")
                != self.manifest["logical_sha256"]
                or not isinstance(metadata.get("header"), dict)
                or metadata["header"].get("schema")
                != TDM_SCHEDULE_LOGICAL_SCHEMA
            ):
                raise ValidationError("schedule SQLite metadata disagrees")
            self._header = metadata["header"]
            self._sections = {
                section: ScheduleRecordSequence(
                    self.store_path,
                    section,
                    self.manifest["sections"][section]["records"],
                    self.manifest["sections"][section]["sha256"],
                )
                for section in TDM_SCHEDULE_SECTIONS
            }
            actual_counts = dict(
                connection.execute(
                    "SELECT section,count(*) FROM records GROUP BY section"
                )
            )
            for section, records in self._sections.items():
                if int(actual_counts.get(section, 0)) != len(records):
                    raise ValidationError(
                        f"schedule {section} row count disagrees"
                    )
            if set(actual_counts) - set(TDM_SCHEDULE_SECTIONS):
                raise ValidationError("schedule SQLite has an unknown section")
        finally:
            connection.close()

    def __enter__(self) -> "TDMScheduleView":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        pass

    def __getitem__(self, key: str) -> Any:
        if key in self._sections:
            return self._sections[key]
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield from TDM_SCHEDULE_SECTIONS

    def __len__(self) -> int:
        return len(self._header) + len(TDM_SCHEDULE_SECTIONS)

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        result = dict(self._header)
        for section, records in self._sections.items():
            digest = hashlib.sha256()
            materialized = []
            for record in records:
                encoded = _canonical(record)
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                materialized.append(dict(record))
            if verify_records and digest.hexdigest() != records.expected_sha256:
                raise ValidationError(
                    f"schedule {section} logical digest disagrees"
                )
            result[section] = materialized
        if (
            verify_records
            and _streaming_digest(result) != self.manifest["logical_sha256"]
        ):
            raise ValidationError("schedule logical digest disagrees")
        return result

    def iter_entry_route_records(
        self,
    ) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Stream entries in identity order with their owning route record."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            rows = connection.execute(
                """
                SELECT e.record_json,r.record_json
                FROM records e
                JOIN records r
                  ON r.section='routes'
                 AND r.identity=json_extract(e.record_json,'$.demand')
                WHERE e.section='entries'
                ORDER BY e.identity
                """
            )
            count = 0
            for entry_json, route_json in rows:
                count += 1
                entry = json.loads(entry_json)
                route = json.loads(route_json)
                if not isinstance(entry, dict) or not isinstance(route, dict):
                    raise ValidationError(
                        "schedule entry/route join contains a malformed record"
                    )
                yield entry, route
            if count != len(self._sections["entries"]):
                raise ValidationError(
                    "schedule entry/route coverage disagrees"
                )
        except json.JSONDecodeError as error:
            raise ValidationError(
                "schedule entry/route join contains corrupt JSON"
            ) from error
        finally:
            connection.close()

    def iter_incoming_records(self) -> Iterator[tuple[str, str, str]]:
        """Stream (net, target FPGA, entry id) for Phase 6 cut joins."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            duplicate = connection.execute(
                """
                SELECT 1
                FROM records e
                JOIN records r
                  ON r.section='routes'
                 AND r.identity=json_extract(e.record_json,'$.demand')
                WHERE e.section='entries'
                GROUP BY json_extract(r.record_json,'$.net'),
                         json_extract(e.record_json,'$.to')
                HAVING count(*) != 1
                LIMIT 1
                """
            ).fetchone()
            if duplicate is not None:
                raise ValidationError(
                    "a routed demand has multiple incoming hops at one FPGA"
                )
            rows = connection.execute(
                """
                SELECT json_extract(r.record_json,'$.net'),
                       json_extract(e.record_json,'$.to'),e.identity
                FROM records e
                JOIN records r
                  ON r.section='routes'
                 AND r.identity=json_extract(e.record_json,'$.demand')
                WHERE e.section='entries'
                ORDER BY 1,2,3
                """
            )
            count = 0
            for net, target, entry_id in rows:
                count += 1
                if not all(
                    isinstance(value, str) and value
                    for value in (net, target, entry_id)
                ):
                    raise ValidationError(
                        "schedule incoming record is malformed"
                    )
                yield net, target, entry_id
            if count != len(self._sections["entries"]):
                raise ValidationError(
                    "schedule incoming coverage disagrees"
                )
        finally:
            connection.close()


def open_tdm_schedule_view(
    manifest_path: Path, *, verify_store_hash: bool = True
) -> TDMScheduleView:
    return TDMScheduleView(
        manifest_path, verify_store_hash=verify_store_hash
    )


def read_tdm_schedule(
    path: Path,
    *,
    materialize: bool = False,
    verify_store_hash: bool = True,
) -> Mapping[str, Any]:
    """Read a legacy schedule or open the indexed schedule lazily."""

    value = _raw_manifest(path)
    if value.get("schema") in TDM_SCHEDULE_STORAGE_SCHEMAS:
        view = open_tdm_schedule_view(
            path, verify_store_hash=verify_store_hash
        )
        return view.materialize() if materialize else view
    from .io import read_json

    return read_json(path)


def canonical_tdm_schedule_sha256(schedule: Mapping[str, Any]) -> str:
    """Return the canonical digest without forcing indexed expansion."""

    if isinstance(schedule, TDMScheduleView):
        return schedule.manifest["logical_sha256"]
    return _streaming_digest(schedule)


def pretty_tdm_schedule_sha256(schedule: Mapping[str, Any]) -> str:
    """Return the historical indented digest used by timing seals."""

    if isinstance(schedule, TDMScheduleView):
        digest = schedule.manifest.get("logical_pretty_sha256")
        if isinstance(digest, str):
            return digest
        return _streaming_digest(
            schedule.materialize(), indent=2, trailing_newline=True
        )
    return _streaming_digest(schedule, indent=2, trailing_newline=True)


def tdm_schedule_store_path(manifest_path: Path) -> Path | None:
    """Return the indexed sidecar for a storage manifest, if present."""

    value = _raw_manifest(manifest_path)
    if value.get("schema") not in TDM_SCHEDULE_STORAGE_SCHEMAS:
        return None
    return _validate_manifest(value, manifest_path)


def copy_tdm_schedule_storage(
    source_manifest: Path,
    destination_manifest: Path,
    *,
    copy_function: Any = shutil.copy2,
) -> None:
    """Copy a schedule boundary without separating its manifest and store."""

    source_store = tdm_schedule_store_path(source_manifest)
    destination_manifest.parent.mkdir(parents=True, exist_ok=True)
    if source_store is not None:
        destination_store = destination_manifest.with_name(source_store.name)
        copy_function(source_store, destination_store)
    # Publish the commit manifest only after its referenced sidecar exists.
    copy_function(source_manifest, destination_manifest)


def expand_tdm_schedule_storage(
    value: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    # Use the already parsed value to reject a manifest swap between the JSON
    # read and SQLite open.  The view independently validates the same file.
    if dict(value) != _raw_manifest(path):
        raise ValidationError("schedule manifest changed while opening")
    with open_tdm_schedule_view(path) as view:
        return view.materialize()
