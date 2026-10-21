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
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Optional

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
  identity TEXT NOT NULL,
  record_json TEXT NOT NULL,
  CHECK(json_valid(record_json)),
  CHECK(
    identity IS CASE section
      WHEN 'domain_schedules' THEN json_extract(record_json,'$.key')
      WHEN 'demand_completions' THEN json_extract(record_json,'$.demand')
      ELSE json_extract(record_json,'$.id')
    END
  ),
  PRIMARY KEY(section,seq),
  UNIQUE(section,identity)
) WITHOUT ROWID;
"""

NATIVE_EXACT_CANONICAL_ENTRY_CURSOR_SCHEMA = (
    "emuflow.native-exact-schedule-entry-cursor/v1"
)


class ScheduleTransportRecord(NamedTuple):
    entry_id: str
    demand: str
    net: str
    link: str
    source: str
    sink: str
    slot: int
    arrival_slot: int
    lane: int
    tdm_ratio: Optional[int]
    route_source: str


_CANONICAL_JSON_STRING = (
    r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7f]|'
    r'\\(?:["\\bfnrt]|u[0-9a-f]{4}))*"'
)
_CANONICAL_UINT = r"(?:0|[1-9][0-9]*)"
_NATIVE_EXACT_CANONICAL_ENTRY = re.compile(
    rf'^\{{"arrival_slot":{_CANONICAL_UINT},'
    rf'"capacity_key":{_CANONICAL_JSON_STRING},'
    rf'"demand":{_CANONICAL_JSON_STRING},'
    rf'"from":{_CANONICAL_JSON_STRING},'
    rf'"hop":{_CANONICAL_UINT},'
    rf'"id":(?P<identity>{_CANONICAL_JSON_STRING}),'
    rf'"lane":{_CANONICAL_UINT},'
    rf'"link":{_CANONICAL_JSON_STRING},'
    rf'"net":{_CANONICAL_JSON_STRING},'
    rf'"ready_slot":{_CANONICAL_UINT},'
    rf'"slot":{_CANONICAL_UINT}'
    rf'(?:,"tdm_ratio":[1-9][0-9]*)?,'
    rf'"to":{_CANONICAL_JSON_STRING}\}}$'
)


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


def _pretty_canonical_record(encoded: str) -> list[str]:
    """Format canonical JSON like json.dumps(indent=2), without decoding it."""

    lines: list[str] = []
    current: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    skip_index = -1
    for index, character in enumerate(encoded):
        if index == skip_index:
            continue
        if in_string:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            current.append(character)
        elif character in "{[":
            current.append(character)
            if index + 1 < len(encoded) and encoded[index + 1] in "}]":
                current.append(encoded[index + 1])
                skip_index = index + 1
            else:
                lines.append("".join(current))
                depth += 1
                current = ["  " * depth]
        elif character in "}]":
            if current and "".join(current).strip():
                lines.append("".join(current))
                depth -= 1
                current = ["  " * depth, character]
            else:
                current.append(character)
        elif character == ",":
            current.append(character)
            lines.append("".join(current))
            current = ["  " * depth]
        elif character == ":":
            current.extend((character, " "))
        else:
            current.append(character)
    if current:
        lines.append("".join(current))
    return lines


def _validate_native_exact_canonical_entry(
    identity: str, encoded: str
) -> None:
    if re.fullmatch(r"s[0-9]{6,}", identity) is None:
        raise ValidationError(
            "native exact canonical schedule entry identity is invalid"
        )
    match = _NATIVE_EXACT_CANONICAL_ENTRY.fullmatch(encoded)
    if match is None or match.group("identity") != _canonical(identity):
        raise ValidationError(
            "native exact canonical schedule entry is malformed or mismatched"
        )


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
            canonical_cursor = getattr(value, "iter_canonical_records", None)
            cursor_schema = getattr(
                value, "canonical_record_cursor_schema", None
            )
            if (
                key == "entries"
                and cursor_schema
                == NATIVE_EXACT_CANONICAL_ENTRY_CURSOR_SCHEMA
                and callable(canonical_cursor)
            ):
                source = canonical_cursor()
                validate_canonical = _validate_native_exact_canonical_entry
            else:
                source = (
                    (_section_identity(key, item), _canonical(item))
                    if isinstance(item, Mapping)
                    else (None, None)
                    for item in value
                )
                validate_canonical = None
            for seq, (identity, encoded) in enumerate(source):
                observed += 1
                if (
                    not isinstance(identity, str)
                    or not identity
                    or not isinstance(encoded, str)
                    or not encoded.startswith("{")
                    or not encoded.endswith("}")
                    or "\n" in encoded
                ):
                    raise ValidationError(
                        f"schedule.{key}[{seq}]: expected an object"
                    )
                if validate_canonical is not None:
                    validate_canonical(identity, encoded)
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
                pending.append((key, seq, identity, encoded))
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
        if "UNIQUE constraint failed" not in str(error):
            raise ValidationError(
                "schedule storage rejected an invalid record payload"
            ) from error
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

    def iter_transport_records(
        self, *, identity_order: bool = False
    ) -> Iterator[ScheduleTransportRecord]:
        """Stream the typed Phase 6 projection without JSON object decoding."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            order_clause = (
                "length(e.identity),e.identity" if identity_order else "e.seq"
            )
            rows = connection.execute(
                f"""
                SELECT e.identity,
                       json_extract(e.record_json,'$.id'),
                       json_extract(e.record_json,'$.demand'),
                       json_extract(e.record_json,'$.net'),
                       json_extract(e.record_json,'$.link'),
                       json_extract(e.record_json,'$.from'),
                       json_extract(e.record_json,'$.to'),
                       json_extract(e.record_json,'$.slot'),
                       json_extract(e.record_json,'$.arrival_slot'),
                       json_extract(e.record_json,'$.lane'),
                       json_extract(e.record_json,'$.tdm_ratio'),
                       r.identity,
                       json_extract(r.record_json,'$.id'),
                       json_extract(r.record_json,'$.net'),
                       json_extract(r.record_json,'$.source'),
                       CASE WHEN
                         json_type(e.record_json,'$.id')='text' AND
                         json_type(e.record_json,'$.demand')='text' AND
                         json_type(e.record_json,'$.net')='text' AND
                         json_type(e.record_json,'$.link')='text' AND
                         json_type(e.record_json,'$.from')='text' AND
                         json_type(e.record_json,'$.to')='text' AND
                         json_type(e.record_json,'$.slot')='integer' AND
                         json_type(e.record_json,'$.arrival_slot')='integer' AND
                         json_type(e.record_json,'$.lane')='integer' AND
                         (json_type(e.record_json,'$.tdm_ratio') IS NULL OR
                          json_type(e.record_json,'$.tdm_ratio')='integer') AND
                         json_type(r.record_json,'$.id')='text' AND
                         json_type(r.record_json,'$.net')='text' AND
                         json_type(r.record_json,'$.source')='text'
                       THEN 1 ELSE 0 END
                FROM records e
                JOIN records r
                  ON r.section='routes'
                 AND r.identity=json_extract(e.record_json,'$.demand')
                WHERE e.section='entries'
                ORDER BY {order_clause}
                """
            )
            count = 0
            for row in rows:
                count += 1
                (
                    entry_id,
                    record_id,
                    demand,
                    net,
                    link,
                    source,
                    sink,
                    slot,
                    arrival_slot,
                    lane,
                    tdm_ratio,
                    route_identity,
                    route_id,
                    route_net,
                    route_source,
                    is_well_typed,
                ) = row
                strings = (
                    entry_id,
                    demand,
                    net,
                    link,
                    source,
                    sink,
                    route_source,
                )
                integers = (slot, arrival_slot, lane)
                if (
                    is_well_typed != 1
                    or not all(
                        isinstance(value, str) and value
                        for value in strings
                    )
                    or record_id != entry_id
                    or route_identity != demand
                    or route_id != demand
                    or route_net != net
                    or not all(
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value >= 0
                        for value in integers
                    )
                    or (
                        tdm_ratio is not None
                        and (
                            not isinstance(tdm_ratio, int)
                            or isinstance(tdm_ratio, bool)
                            or tdm_ratio < 1
                        )
                    )
                ):
                    raise ValidationError(
                        "typed schedule transport record is malformed"
                    )
                yield ScheduleTransportRecord(
                    entry_id,
                    demand,
                    net,
                    link,
                    source,
                    sink,
                    slot,
                    arrival_slot,
                    lane,
                    tdm_ratio,
                    route_source,
                )
            if count != len(self._sections["entries"]):
                raise ValidationError(
                    "typed schedule transport coverage disagrees"
                )
        finally:
            connection.close()

    def iter_hop_identity_records(
        self,
    ) -> Iterator[tuple[str, str, str, str, str]]:
        """Stream canonical hop keys and entry identities for certificates."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            rows = connection.execute(
                """
                SELECT json_extract(record_json,'$.demand'),
                       json_extract(record_json,'$.link'),
                       json_extract(record_json,'$.from'),
                       json_extract(record_json,'$.to'),
                       identity,
                       json_extract(record_json,'$.id'),
                       CASE WHEN
                         json_type(record_json,'$.demand')='text' AND
                         json_type(record_json,'$.link')='text' AND
                         json_type(record_json,'$.from')='text' AND
                         json_type(record_json,'$.to')='text' AND
                         json_type(record_json,'$.id')='text'
                       THEN 1 ELSE 0 END
                FROM records
                WHERE section='entries'
                ORDER BY 1,2,3,4,5
                """
            )
            count = 0
            for row in rows:
                count += 1
                demand, link, source, target, identity, record_id, valid = row
                if (
                    valid != 1
                    or record_id != identity
                    or not all(
                        isinstance(value, str) and value
                        for value in (
                            demand,
                            link,
                            source,
                            target,
                            identity,
                        )
                    )
                ):
                    raise ValidationError(
                        "schedule hop identity record is malformed"
                    )
                yield demand, link, source, target, identity
            if count != len(self._sections["entries"]):
                raise ValidationError(
                    "schedule hop identity coverage disagrees"
                )
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
