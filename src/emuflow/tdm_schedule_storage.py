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
TYPED_TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v3"
TDM_SCHEDULE_STORAGE_SCHEMAS = {
    LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
}
TDM_SCHEDULE_LOGICAL_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_SCHEDULE_APPLICATION_ID = 0x454D5554  # ASCII "EMUT"
TDM_SCHEDULE_USER_VERSION = 1
TYPED_TDM_SCHEDULE_USER_VERSION = 2
TDM_SCHEDULE_STORE_NAME = "schedule.sqlite"
TDM_SCHEDULE_STORE_PREFIX = "schedule."
TDM_SCHEDULE_STORE_SUFFIX = ".sqlite"
TDM_SCHEDULE_SECTIONS = (
    "routes",
    "entries",
    "demand_completions",
    "domain_schedules",
)


LEGACY_DDL = """
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

TYPED_ENTRY_RELATION_SCHEMA = "emuflow.tdm-schedule-entry-relation/v1"
TDM_EXACT_DEPENDENCY_PROVIDER = "deterministic-dependency-earliest-slot-v1"

TYPED_DDL = LEGACY_DDL + """
CREATE TABLE schedule_entries(
  seq INTEGER NOT NULL UNIQUE,
  entry_id TEXT NOT NULL UNIQUE,
  demand TEXT NOT NULL,
  net TEXT NOT NULL,
  hop INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  capacity_key TEXT NOT NULL,
  slot INTEGER NOT NULL,
  lane INTEGER NOT NULL,
  ready_slot INTEGER NOT NULL,
  arrival_slot INTEGER NOT NULL,
  tdm_ratio INTEGER,
  PRIMARY KEY(demand,link_id,source_fpga,sink_fpga)
) WITHOUT ROWID;
"""

TYPED_INDEX_DDL = """
CREATE INDEX schedule_entries_seq ON schedule_entries(seq);
CREATE INDEX schedule_entries_capacity_order
  ON schedule_entries(capacity_key,demand,link_id,source_fpga,sink_fpga);
CREATE INDEX schedule_entries_collision
  ON schedule_entries(capacity_key,slot,lane);
"""

_TYPED_REQUIRED_INDEX_COLUMNS = {
    "schedule_entries_seq": ("seq",),
    "schedule_entries_capacity_order": (
        "capacity_key",
        "demand",
        "link_id",
        "source_fpga",
        "sink_fpga",
    ),
    "schedule_entries_collision": ("capacity_key", "slot", "lane"),
}

NATIVE_EXACT_CANONICAL_ENTRY_CURSOR_SCHEMA = (
    "emuflow.native-exact-schedule-entry-cursor/v1"
)
NATIVE_EXACT_TYPED_ENTRY_CURSOR_SCHEMA = (
    "emuflow.native-exact-schedule-typed-entry-cursor/v1"
)

_TYPED_ENTRY_KEYS = {
    "id",
    "demand",
    "net",
    "hop",
    "link",
    "from",
    "to",
    "capacity_key",
    "slot",
    "lane",
    "ready_slot",
    "arrival_slot",
    "tdm_ratio",
}


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


class ScheduleValidationRecord(NamedTuple):
    """Typed ScheduleStore projection used by independent validators."""

    entry_id: str
    demand: str
    net: str
    hop: int
    link: str
    source: str
    sink: str
    capacity_key: str
    slot: int
    lane: int
    ready_slot: int
    arrival_slot: int
    tdm_ratio: Optional[int]

    def as_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.entry_id,
            "demand": self.demand,
            "net": self.net,
            "hop": self.hop,
            "link": self.link,
            "from": self.source,
            "to": self.sink,
            "capacity_key": self.capacity_key,
            "slot": self.slot,
            "lane": self.lane,
            "ready_slot": self.ready_slot,
            "arrival_slot": self.arrival_slot,
        }
        if self.tdm_ratio is not None:
            value["tdm_ratio"] = self.tdm_ratio
        return value


def _validate_typed_relation_structure(
    connection: sqlite3.Connection, expected_entries: int
) -> None:
    """Validate the v3 relation shape without applying schedule semantics."""

    summary = connection.execute(
        "SELECT count(*),count(DISTINCT seq),min(seq),max(seq) "
        "FROM schedule_entries"
    ).fetchone()
    expected_min = 0 if expected_entries else None
    expected_max = expected_entries - 1 if expected_entries else None
    if summary != (
        expected_entries,
        expected_entries,
        expected_min,
        expected_max,
    ):
        raise ValidationError(
            "schedule entries typed sequence is not contiguous"
        )

    index_names = {
        str(row[1])
        for row in connection.execute("PRAGMA index_list(schedule_entries)")
    }
    for name, expected_columns in _TYPED_REQUIRED_INDEX_COLUMNS.items():
        if name not in index_names:
            raise ValidationError(
                "schedule typed entry indexes are incomplete"
            )
        actual_columns = tuple(
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info({name})")
        )
        if actual_columns != expected_columns:
            raise ValidationError(
                "schedule typed entry index definition disagrees"
            )


def _typed_entry_values(
    identity: str, record: Mapping[str, Any]
) -> tuple[Any, ...]:
    """Validate and project one exact entry without retaining its mapping."""

    if set(record) - _TYPED_ENTRY_KEYS:
        raise ValidationError(
            "typed schedule entry contains unsupported fields"
        )
    values = (
        record.get("id"),
        record.get("demand"),
        record.get("net"),
        record.get("hop"),
        record.get("link"),
        record.get("from"),
        record.get("to"),
        record.get("capacity_key"),
        record.get("slot"),
        record.get("lane"),
        record.get("ready_slot"),
        record.get("arrival_slot"),
        record.get("tdm_ratio"),
    )
    return _validate_typed_entry_values(identity, values)


def _validate_typed_entry_values(
    identity: str, values: Sequence[Any]
) -> tuple[Any, ...]:
    if len(values) != len(ScheduleValidationRecord._fields):
        raise ValidationError("typed schedule entry is malformed")
    record = ScheduleValidationRecord(*values)
    strings = (
        record.entry_id,
        record.demand,
        record.net,
        record.link,
        record.source,
        record.sink,
        record.capacity_key,
    )
    integers = (
        record.hop,
        record.slot,
        record.lane,
        record.ready_slot,
        record.arrival_slot,
    )
    if (
        record.entry_id != identity
        or not all(isinstance(value, str) and value for value in strings)
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in integers
        )
        or (
            record.tdm_ratio is not None
            and (
                not isinstance(record.tdm_ratio, int)
                or isinstance(record.tdm_ratio, bool)
            )
        )
    ):
        raise ValidationError("typed schedule entry is malformed")
    return tuple(values)


def _typed_entry_mapping(values: Sequence[Any]) -> dict[str, Any]:
    record = ScheduleValidationRecord(*values).as_mapping()
    return record


def _typed_entry_canonical(values: Sequence[Any]) -> str:
    record = ScheduleValidationRecord(*values)
    ratio = (
        f',"tdm_ratio":{record.tdm_ratio}'
        if record.tdm_ratio is not None
        else ""
    )
    return (
        f'{{"arrival_slot":{record.arrival_slot},'
        f'"capacity_key":{_canonical(record.capacity_key)},'
        f'"demand":{_canonical(record.demand)},'
        f'"from":{_canonical(record.source)},'
        f'"hop":{record.hop},"id":{_canonical(record.entry_id)},'
        f'"lane":{record.lane},"link":{_canonical(record.link)},'
        f'"net":{_canonical(record.net)},'
        f'"ready_slot":{record.ready_slot},"slot":{record.slot}'
        f'{ratio},"to":{_canonical(record.sink)}}}'
    )


class ScheduleValidationIndex(Mapping[tuple[str, str, str, str], Mapping[str, Any]]):
    """Disk-backed typed hop index over one sealed ScheduleStore.

    Store v3 owns this indexed relation directly.  Legacy v1/v2 stores retain
    the former compatibility path which derives a temporary relation from
    canonical JSON rows without constructing hop-sized Python containers.
    """

    def __init__(
        self,
        store_path: Path,
        expected_entries: int,
        *,
        producer_owned: bool,
    ):
        self._store_path = store_path
        self._expected_entries = expected_entries
        self._producer_owned = producer_owned
        self._connection: sqlite3.Connection | None = None
        self._relation = (
            "schedule_entries" if producer_owned else "validation_entries"
        )

    def __enter__(self) -> "ScheduleValidationIndex":
        connection = sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        self._connection = connection
        try:
            if self._producer_owned:
                _validate_typed_relation_structure(
                    connection, self._expected_entries
                )
            else:
                connection.execute("PRAGMA temp_store=FILE")
                malformed = connection.execute(
                """
                SELECT identity
                FROM records
                WHERE section='entries' AND NOT (
                  json_valid(record_json) AND
                  json_type(record_json,'$.id')='text' AND
                  json_extract(record_json,'$.id')=identity AND
                  length(json_extract(record_json,'$.id'))>0 AND
                  json_type(record_json,'$.demand')='text' AND
                  length(json_extract(record_json,'$.demand'))>0 AND
                  json_type(record_json,'$.net')='text' AND
                  length(json_extract(record_json,'$.net'))>0 AND
                  json_type(record_json,'$.hop')='integer' AND
                  json_type(record_json,'$.link')='text' AND
                  length(json_extract(record_json,'$.link'))>0 AND
                  json_type(record_json,'$.from')='text' AND
                  length(json_extract(record_json,'$.from'))>0 AND
                  json_type(record_json,'$.to')='text' AND
                  length(json_extract(record_json,'$.to'))>0 AND
                  json_type(record_json,'$.capacity_key')='text' AND
                  length(json_extract(record_json,'$.capacity_key'))>0 AND
                  json_type(record_json,'$.slot')='integer' AND
                  json_type(record_json,'$.lane')='integer' AND
                  json_type(record_json,'$.ready_slot')='integer' AND
                  json_type(record_json,'$.arrival_slot')='integer' AND
                  (json_type(record_json,'$.tdm_ratio') IS NULL OR
                   json_type(record_json,'$.tdm_ratio')='integer')
                )
                LIMIT 1
                """
                ).fetchone()
                if malformed is not None:
                    raise ValidationError(
                        "typed schedule validation record is malformed"
                    )
                connection.execute(
                """
                CREATE TEMP TABLE validation_entries(
                  entry_id TEXT NOT NULL UNIQUE,
                  demand TEXT NOT NULL,
                  net TEXT NOT NULL,
                  hop INTEGER NOT NULL,
                  link_id TEXT NOT NULL,
                  source_fpga TEXT NOT NULL,
                  sink_fpga TEXT NOT NULL,
                  capacity_key TEXT NOT NULL,
                  slot INTEGER NOT NULL,
                  lane INTEGER NOT NULL,
                  ready_slot INTEGER NOT NULL,
                  arrival_slot INTEGER NOT NULL,
                  tdm_ratio INTEGER,
                  PRIMARY KEY(demand,link_id,source_fpga,sink_fpga)
                ) WITHOUT ROWID
                """
                )
                connection.execute(
                """
                INSERT INTO validation_entries
                SELECT identity,
                       json_extract(record_json,'$.demand'),
                       json_extract(record_json,'$.net'),
                       json_extract(record_json,'$.hop'),
                       json_extract(record_json,'$.link'),
                       json_extract(record_json,'$.from'),
                       json_extract(record_json,'$.to'),
                       json_extract(record_json,'$.capacity_key'),
                       json_extract(record_json,'$.slot'),
                       json_extract(record_json,'$.lane'),
                       json_extract(record_json,'$.ready_slot'),
                       json_extract(record_json,'$.arrival_slot'),
                       json_extract(record_json,'$.tdm_ratio')
                FROM records
                WHERE section='entries'
                """
                )
                count = connection.execute(
                    "SELECT count(*) FROM validation_entries"
                ).fetchone()[0]
                if count != self._expected_entries:
                    raise ValidationError(
                        "typed schedule validation coverage disagrees"
                    )
                connection.execute(
                    "CREATE INDEX validation_entries_capacity_order "
                    "ON validation_entries(capacity_key,demand,link_id,"
                    "source_fpga,sink_fpga)"
                )
                connection.execute(
                    "CREATE INDEX validation_entries_collision "
                    "ON validation_entries(capacity_key,slot,lane)"
                )
        except (sqlite3.DatabaseError, sqlite3.IntegrityError) as error:
            connection.close()
            self._connection = None
            raise ValidationError(
                "typed schedule validation index is malformed"
            ) from error
        except Exception:
            connection.close()
            self._connection = None
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("schedule validation index is not open")
        return self._connection

    @staticmethod
    def _record(row: tuple[Any, ...]) -> ScheduleValidationRecord:
        return ScheduleValidationRecord(*row)

    def __len__(self) -> int:
        return self._expected_entries

    def __iter__(self) -> Iterator[tuple[str, str, str, str]]:
        rows = self._db.execute(
            "SELECT demand,link_id,source_fpga,sink_fpga "
            f"FROM {self._relation} ORDER BY 1,2,3,4"
        )
        yield from rows

    def __getitem__(
        self, key: tuple[str, str, str, str]
    ) -> Mapping[str, Any]:
        row = self._db.execute(
            "SELECT entry_id,demand,net,hop,link_id,source_fpga,sink_fpga,"
            "capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio "
            f"FROM {self._relation} WHERE demand=? AND link_id=? AND "
            "source_fpga=? AND sink_fpga=?",
            key,
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return self._record(row).as_mapping()

    def install_demand_order(self, demands: Iterator[str]) -> None:
        self._db.execute(
            "CREATE TEMP TABLE validation_demand_order("
            "seq INTEGER PRIMARY KEY,demand TEXT NOT NULL UNIQUE)"
        )
        try:
            self._db.executemany(
                "INSERT INTO validation_demand_order(seq,demand) VALUES(?,?)",
                enumerate(demands),
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "schedule validation demand order is not unique"
            ) from error

    def iter_demand_order(self) -> Iterator[tuple[int, ScheduleValidationRecord]]:
        rows = self._db.execute(
            """
            SELECT o.seq,e.entry_id,e.demand,e.net,e.hop,e.link_id,
                   e.source_fpga,e.sink_fpga,e.capacity_key,e.slot,e.lane,
                   e.ready_slot,e.arrival_slot,e.tdm_ratio
            FROM validation_demand_order o
            JOIN {relation} e USING(demand)
            ORDER BY o.seq,e.hop,e.link_id,e.source_fpga,e.sink_fpga
            """.format(relation=self._relation)
        )
        for row in rows:
            yield int(row[0]), self._record(row[1:])

    def iter_capacity_order(self) -> Iterator[ScheduleValidationRecord]:
        rows = self._db.execute(
            "SELECT entry_id,demand,net,hop,link_id,source_fpga,sink_fpga,"
            "capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio "
            f"FROM {self._relation} ORDER BY capacity_key,demand,link_id,"
            "source_fpga,sink_fpga"
        )
        for row in rows:
            yield self._record(row)

    def domain_counts(self) -> dict[str, int]:
        return {
            str(key): int(count)
            for key, count in self._db.execute(
                f"SELECT capacity_key,count(*) FROM {self._relation} "
                "GROUP BY capacity_key"
            )
        }

    def first_collision(self) -> tuple[str, int, int] | None:
        row = self._db.execute(
            f"SELECT capacity_key,slot,lane FROM {self._relation} "
            "GROUP BY capacity_key,slot,lane HAVING count(*)>1 "
            "ORDER BY capacity_key,slot,lane LIMIT 1"
        ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]), int(row[2]))


_CANONICAL_JSON_STRING = (
    r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7f]|'
    r'\\(?:["\\bfnrt]|u[0-9a-f]{4}))*"'
)
_CANONICAL_INT = r"(?:0|-?[1-9][0-9]*)"
_NATIVE_EXACT_CANONICAL_ENTRY = re.compile(
    rf'^\{{"arrival_slot":{_CANONICAL_INT},'
    rf'"capacity_key":{_CANONICAL_JSON_STRING},'
    rf'"demand":{_CANONICAL_JSON_STRING},'
    rf'"from":{_CANONICAL_JSON_STRING},'
    rf'"hop":{_CANONICAL_INT},'
    rf'"id":(?P<identity>{_CANONICAL_JSON_STRING}),'
    rf'"lane":{_CANONICAL_INT},'
    rf'"link":{_CANONICAL_JSON_STRING},'
    rf'"net":{_CANONICAL_JSON_STRING},'
    rf'"ready_slot":{_CANONICAL_INT},'
    rf'"slot":{_CANONICAL_INT}'
    rf'(?:,"tdm_ratio":{_CANONICAL_INT})?,'
    rf'"to":{_CANONICAL_JSON_STRING}\}}$'
)

_CANONICAL_ENCODER = json.JSONEncoder(
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
)


def _canonical(value: Any) -> str:
    return _CANONICAL_ENCODER.encode(value)


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


def _content_addressed_store_name(store_sha256: str) -> str:
    return (
        f"{TDM_SCHEDULE_STORE_PREFIX}{store_sha256}"
        f"{TDM_SCHEDULE_STORE_SUFFIX}"
    )


def _validate_manifest(value: Mapping[str, Any], path: Path) -> Path:
    schema = value.get("schema")
    expected_user_version = (
        TYPED_TDM_SCHEDULE_USER_VERSION
        if schema == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        else TDM_SCHEDULE_USER_VERSION
    )
    if (
        schema not in TDM_SCHEDULE_STORAGE_SCHEMAS
        or value.get("logical_schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
        or value.get("application_id") != TDM_SCHEDULE_APPLICATION_ID
        or value.get("user_version") != expected_user_version
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
    if schema in {
        TDM_SCHEDULE_STORAGE_SCHEMA,
        TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    } and (
        not isinstance(value.get("logical_pretty_sha256"), str)
        or len(value["logical_pretty_sha256"]) != 64
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    if schema == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA:
        relation = value.get("entry_relation")
        if (
            not isinstance(relation, dict)
            or relation.get("schema") != TYPED_ENTRY_RELATION_SCHEMA
            or isinstance(relation.get("records"), bool)
            or not isinstance(relation.get("records"), int)
            or relation["records"] < 0
        ):
            raise ValidationError(
                f"{path}: schedule typed entry relation is malformed"
            )
        if value.get("artifact") != _content_addressed_store_name(
            value["store_sha256"]
        ):
            raise ValidationError(
                f"{path}: schedule typed artifact identity disagrees"
            )
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
    if (
        schema == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        and value["entry_relation"]["records"]
        != sections["entries"]["records"]
    ):
        raise ValidationError(
            f"{path}: schedule typed entry relation coverage disagrees"
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
    *,
    typed_entries: bool,
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
            pending_typed: list[tuple[Any, ...]] = []
            observed = 0
            canonical_cursor = getattr(value, "iter_canonical_records", None)
            cursor_schema = getattr(
                value, "canonical_record_cursor_schema", None
            )
            typed_cursor = getattr(value, "iter_typed_records", None)
            typed_cursor_schema = getattr(
                value, "typed_record_cursor_schema", None
            )
            if (
                typed_entries
                and key == "entries"
                and typed_cursor_schema
                == NATIVE_EXACT_TYPED_ENTRY_CURSOR_SCHEMA
                and callable(typed_cursor)
            ):
                source = (
                    (identity, None, typed_value)
                    for identity, typed_value in typed_cursor()
                )
                validate_canonical = _validate_native_exact_canonical_entry
            elif (
                key == "entries"
                and cursor_schema
                == NATIVE_EXACT_CANONICAL_ENTRY_CURSOR_SCHEMA
                and callable(canonical_cursor)
            ):
                source = (
                    (identity, encoded, None)
                    for identity, encoded in canonical_cursor()
                )
                validate_canonical = _validate_native_exact_canonical_entry
            else:
                source = (
                    (_section_identity(key, item), _canonical(item), item)
                    if isinstance(item, Mapping)
                    else (None, None, None)
                    for item in value
                )
                validate_canonical = None
            for seq, (identity, encoded, typed_value) in enumerate(source):
                observed += 1
                if typed_entries and key == "entries":
                    if typed_value is None:
                        raise ValidationError(
                            "typed schedule entry cursor is unavailable"
                        )
                    values = (
                        _typed_entry_values(identity, typed_value)
                        if isinstance(typed_value, Mapping)
                        else _validate_typed_entry_values(
                            identity, typed_value
                        )
                    )
                    canonical = _typed_entry_canonical(values)
                    if encoded is not None and canonical != encoded:
                        raise ValidationError(
                            "typed schedule entry disagrees with canonical record"
                        )
                    encoded = canonical
                    pending_typed.append((seq, *values))
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
                if typed_entries and key == "entries":
                    if len(pending_typed) >= 4096:
                        connection.executemany(
                            "INSERT INTO schedule_entries("
                            "seq,entry_id,demand,net,hop,link_id,source_fpga,"
                            "sink_fpga,capacity_key,slot,lane,ready_slot,"
                            "arrival_slot,tdm_ratio) VALUES (?,?,?,?,?,?,?,?,"
                            "?,?,?,?,?,?)",
                            pending_typed,
                        )
                        pending_typed.clear()
                else:
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
            if pending_typed:
                connection.executemany(
                    "INSERT INTO schedule_entries("
                    "seq,entry_id,demand,net,hop,link_id,source_fpga,"
                    "sink_fpga,capacity_key,slot,lane,ready_slot,"
                    "arrival_slot,tdm_ratio) VALUES (?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?)",
                    pending_typed,
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
    metadata = [
        ("logical_schema", _canonical(TDM_SCHEDULE_LOGICAL_SCHEMA)),
        ("header", _canonical(header)),
        ("logical_sha256", _canonical(compact_digest.hexdigest())),
    ]
    if typed_entries:
        metadata.extend(
            (
                (
                    "entry_relation_schema",
                    _canonical(TYPED_ENTRY_RELATION_SCHEMA),
                ),
                (
                    "entry_relation_records",
                    _canonical(section_summaries["entries"]["records"]),
                ),
            )
        )
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)", metadata
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

    typed_entries = schedule.get("provider") == TDM_EXACT_DEPENDENCY_PROVIDER
    storage_schema = (
        TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        if typed_entries
        else TDM_SCHEDULE_STORAGE_SCHEMA
    )
    user_version = (
        TYPED_TDM_SCHEDULE_USER_VERSION
        if typed_entries
        else TDM_SCHEDULE_USER_VERSION
    )

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_store_path = manifest_path.with_name(TDM_SCHEDULE_STORE_NAME)
    temporary = legacy_store_path.with_name(
        f".{legacy_store_path.name}.{uuid.uuid4().hex}.tmp"
    )
    store_path = legacy_store_path
    store_sha256: str | None = None
    store_bytes: int | None = None
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(f"PRAGMA application_id={TDM_SCHEDULE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={user_version}")
        connection.executescript(TYPED_DDL if typed_entries else LEGACY_DDL)
        (
            logical_sha256,
            logical_pretty_sha256,
            section_summaries,
        ) = _publish_logical_schedule(
            connection, schedule, typed_entries=typed_entries
        )
        if typed_entries:
            connection.executescript(TYPED_INDEX_DDL)
        connection.commit()
        connection.close()
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
                raise ValidationError(
                    "schedule content-addressed artifact disagrees"
                )
            temporary.unlink()
        else:
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
    if typed_entries:
        manifest["entry_relation"] = {
            "schema": TYPED_ENTRY_RELATION_SCHEMA,
            "records": section_summaries["entries"]["records"],
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


_TYPED_ENTRY_COLUMNS = (
    "entry_id,demand,net,hop,link_id,source_fpga,sink_fpga,"
    "capacity_key,slot,lane,ready_slot,arrival_slot,tdm_ratio"
)


class TypedScheduleEntrySequence(Sequence[Mapping[str, Any]]):
    """Lazy logical entry sequence backed only by typed ScheduleStore rows."""

    section = "entries"

    def __init__(self, store_path: Path, count: int, digest: str):
        self._store_path = store_path
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
                f"SELECT {_TYPED_ENTRY_COLUMNS} FROM schedule_entries "
                "ORDER BY seq"
            )
            count = 0
            for row in rows:
                count += 1
                yield _typed_entry_mapping(row)
            if count != self._count:
                raise ValidationError(
                    "schedule entries typed relation coverage disagrees"
                )
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule entries typed relation is unreadable"
            ) from error
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {_TYPED_ENTRY_COLUMNS} FROM schedule_entries "
                "WHERE seq=?",
                (index,),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule entries typed relation is unreadable"
            ) from error
        finally:
            connection.close()
        if row is None:
            raise ValidationError(
                "schedule entries typed sequence is not contiguous"
            )
        return _typed_entry_mapping(row)

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT {_TYPED_ENTRY_COLUMNS} FROM schedule_entries "
                "ORDER BY length(entry_id),entry_id"
            )
            for row in rows:
                yield _typed_entry_mapping(row)
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule entries typed relation is unreadable"
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
        self._producer_owned_entries = (
            self.manifest["schema"] == TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        )
        connection = sqlite3.connect(
            f"file:{store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != TDM_SCHEDULE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != self.manifest["user_version"]
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
            if self._producer_owned_entries and (
                metadata.get("entry_relation_schema")
                != TYPED_ENTRY_RELATION_SCHEMA
                or metadata.get("entry_relation_records")
                != self.manifest["entry_relation"]["records"]
            ):
                raise ValidationError(
                    "schedule typed entry metadata disagrees"
                )
            self._header = metadata["header"]
            self._sections = {}
            for section in TDM_SCHEDULE_SECTIONS:
                descriptor = self.manifest["sections"][section]
                if section == "entries" and self._producer_owned_entries:
                    records = TypedScheduleEntrySequence(
                        self.store_path,
                        descriptor["records"],
                        descriptor["sha256"],
                    )
                else:
                    records = ScheduleRecordSequence(
                        self.store_path,
                        section,
                        descriptor["records"],
                        descriptor["sha256"],
                    )
                self._sections[section] = records
            actual_counts = dict(
                connection.execute(
                    "SELECT section,count(*) FROM records GROUP BY section"
                )
            )
            for section, records in self._sections.items():
                actual = (
                    connection.execute(
                        "SELECT count(*) FROM schedule_entries"
                    ).fetchone()[0]
                    if section == "entries" and self._producer_owned_entries
                    else int(actual_counts.get(section, 0))
                )
                if actual != len(records):
                    raise ValidationError(
                        f"schedule {section} row count disagrees"
                    )
            expected_record_sections = set(TDM_SCHEDULE_SECTIONS)
            if self._producer_owned_entries:
                expected_record_sections.remove("entries")
                if "entries" in actual_counts:
                    raise ValidationError(
                        "typed schedule duplicates logical entry records"
                    )
                _validate_typed_relation_structure(
                    connection, len(self._sections["entries"])
                )
            if set(actual_counts) - expected_record_sections:
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

    def iter_canonical_section_records(
        self, section: str
    ) -> Iterator[str]:
        """Stream sealed canonical records without building Python objects."""

        if section not in self._sections:
            raise ValidationError(f"unknown schedule section {section!r}")
        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        digest = hashlib.sha256()
        count = 0
        try:
            if section == "entries" and self._producer_owned_entries:
                source = (
                    (_typed_entry_canonical(row),)
                    for row in connection.execute(
                        f"SELECT {_TYPED_ENTRY_COLUMNS} "
                        "FROM schedule_entries ORDER BY seq"
                    )
                )
            else:
                source = connection.execute(
                    "SELECT record_json FROM records "
                    "WHERE section=? ORDER BY seq",
                    (section,),
                )
            for (encoded,) in source:
                count += 1
                digest.update(encoded.encode("utf-8"))
                digest.update(b"\n")
                yield encoded
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                f"schedule {section} record cursor is unreadable"
            ) from error
        finally:
            connection.close()
        records = self._sections[section]
        if count != len(records) or digest.hexdigest() != records.expected_sha256:
            raise ValidationError(
                f"schedule {section} logical seal disagrees"
            )

    def validate_logical_seals(self) -> None:
        """Recompute compact/pretty logical seals without JSON expansion."""

        compact_digest = hashlib.sha256()
        pretty_digest = hashlib.sha256()
        keys = sorted((*self._header, *TDM_SCHEDULE_SECTIONS))
        _update_digest(compact_digest, "{")
        _update_digest(pretty_digest, "{\n" if keys else "{}\n")
        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            for key_index, key in enumerate(keys):
                if key_index:
                    _update_digest(compact_digest, ",")
                encoded_key = _canonical(key)
                _update_digest(compact_digest, f"{encoded_key}:")
                _update_digest(pretty_digest, f"  {encoded_key}: ")
                if key in TDM_SCHEDULE_SECTIONS:
                    records = self._sections[key]
                    count = len(records)
                    section_digest = hashlib.sha256()
                    _update_digest(compact_digest, "[")
                    _update_digest(pretty_digest, "[\n" if count else "[]")
                    observed = 0
                    if key == "entries" and self._producer_owned_entries:
                        source = (
                            (_typed_entry_canonical(row),)
                            for row in connection.execute(
                                f"SELECT {_TYPED_ENTRY_COLUMNS} "
                                "FROM schedule_entries ORDER BY seq"
                            )
                        )
                    else:
                        source = connection.execute(
                            "SELECT record_json FROM records "
                            "WHERE section=? ORDER BY seq",
                            (key,),
                        )
                    for seq, (encoded,) in enumerate(source):
                        observed += 1
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
                    if (
                        observed != count
                        or section_digest.hexdigest()
                        != records.expected_sha256
                    ):
                        raise ValidationError(
                            f"schedule {key} logical seal disagrees"
                        )
                    _update_digest(compact_digest, "]")
                    if count:
                        _update_digest(pretty_digest, "\n  ]")
                else:
                    value = self._header[key]
                    _update_digest(compact_digest, _canonical(value))
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule logical seal records are unreadable"
            ) from error
        finally:
            connection.close()
        if compact_digest.hexdigest() != self.manifest["logical_sha256"]:
            raise ValidationError("schedule compact logical seal disagrees")
        expected_pretty = self.manifest.get("logical_pretty_sha256")
        if (
            expected_pretty is not None
            and pretty_digest.hexdigest() != expected_pretty
        ):
            raise ValidationError("schedule pretty logical seal disagrees")

    def open_validation_index(self) -> ScheduleValidationIndex:
        return ScheduleValidationIndex(
            self.store_path,
            len(self._sections["entries"]),
            producer_owned=self._producer_owned_entries,
        )

    def iter_entry_route_records(
        self,
    ) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Stream entries in identity order with their owning route record."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            if self._producer_owned_entries:
                rows = connection.execute(
                    f"""
                    SELECT {_TYPED_ENTRY_COLUMNS},r.record_json
                    FROM schedule_entries e
                    JOIN records r
                      ON r.section='routes' AND r.identity=e.demand
                    ORDER BY e.entry_id
                    """
                )
            else:
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
            for row in rows:
                count += 1
                if self._producer_owned_entries:
                    entry = _typed_entry_mapping(row[:-1])
                    route_json = row[-1]
                else:
                    entry_json, route_json = row
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
            if self._producer_owned_entries:
                order_clause = (
                    "length(e.entry_id),e.entry_id"
                    if identity_order
                    else "e.seq"
                )
                rows = connection.execute(
                    f"""
                    SELECT e.entry_id,e.entry_id,e.demand,e.net,e.link_id,
                           e.source_fpga,e.sink_fpga,e.slot,e.arrival_slot,
                           e.lane,e.tdm_ratio,r.identity,
                           json_extract(r.record_json,'$.id'),
                           json_extract(r.record_json,'$.net'),
                           json_extract(r.record_json,'$.source'),1
                    FROM schedule_entries e
                    JOIN records r
                      ON r.section='routes' AND r.identity=e.demand
                    ORDER BY {order_clause}
                    """
                )
            else:
                order_clause = (
                    "length(e.identity),e.identity"
                    if identity_order
                    else "e.seq"
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
                        for value in integers
                    )
                    or (
                        tdm_ratio is not None
                        and (
                            not isinstance(tdm_ratio, int)
                            or isinstance(tdm_ratio, bool)
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
            if self._producer_owned_entries:
                rows = connection.execute(
                    """
                    SELECT demand,link_id,source_fpga,sink_fpga,
                           entry_id,entry_id,1
                    FROM schedule_entries
                    ORDER BY demand,link_id,source_fpga,sink_fpga,entry_id
                    """
                )
            else:
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
            if self._producer_owned_entries:
                duplicate = connection.execute(
                    """
                    SELECT 1
                    FROM schedule_entries e
                    JOIN records r
                      ON r.section='routes' AND r.identity=e.demand
                    GROUP BY json_extract(r.record_json,'$.net'),e.sink_fpga
                    HAVING count(*) != 1
                    LIMIT 1
                    """
                ).fetchone()
            else:
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
            if self._producer_owned_entries:
                rows = connection.execute(
                    """
                    SELECT json_extract(r.record_json,'$.net'),
                           e.sink_fpga,e.entry_id
                    FROM schedule_entries e
                    JOIN records r
                      ON r.section='routes' AND r.identity=e.demand
                    ORDER BY 1,2,3
                    """
                )
            else:
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
