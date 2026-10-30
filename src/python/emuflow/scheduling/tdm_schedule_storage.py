"""Versioned indexed storage for the Phase 5 schedule contract.

The JSON file at the public boundary is a compact, relocatable manifest.  The
hop-sized ordered relations live once in SQLite and are exposed through a lazy
read-only view.  ``read_json`` remains the explicit compatibility API that
materializes the historical logical document.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import struct
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from emuflow.scheduling.tdm import NativeExactScheduleRequest

from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged
from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256, write_json


TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v4"
TDM_SCHEDULE_LOGICAL_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_SCHEDULE_APPLICATION_ID = 0x454D5554
TDM_SCHEDULE_USER_VERSION = 3
TDM_SCHEDULE_STORE_PREFIX = "schedule."
TDM_SCHEDULE_STORE_SUFFIX = ".sqlite"
TDM_SCHEDULE_SECTIONS = (
    "routes",
    "entries",
    "demand_completions",
    "domain_schedules",
)


TYPED_ENTRY_RELATION_SCHEMA = "emuflow.tdm-schedule-entry-relation/v1"
TYPED_ROUTE_RELATION_SCHEMA = "emuflow.tdm-schedule-route-relation/v1"
TYPED_COMPLETION_RELATION_SCHEMA = "emuflow.tdm-schedule-completion-relation/v1"
TDM_EXACT_DEPENDENCY_PROVIDER = "deterministic-dependency-earliest-slot-v1"

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

_TYPED_ROUTE_KEYS = {"id", "net", "source", "sinks", "tree_edges"}
_TYPED_ROUTE_EDGE_KEYS = {"link", "from", "to"}
_TYPED_COMPLETION_KEYS = {
    "demand",
    "net",
    "source_ready_slot",
    "completion_slot",
    "sink_arrival_slots",
}

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
    """Validate current entry relations without applying schedule semantics."""

    summary = connection.execute(
        "SELECT count(*),count(DISTINCT seq),min(seq),max(seq) " "FROM schedule_entries"
    ).fetchone()
    expected_min = 0 if expected_entries else None
    expected_max = expected_entries - 1 if expected_entries else None
    if summary != (
        expected_entries,
        expected_entries,
        expected_min,
        expected_max,
    ):
        raise ValidationError("schedule entries typed sequence is not contiguous")

    index_names = {
        str(row[1]) for row in connection.execute("PRAGMA index_list(schedule_entries)")
    }
    for name, expected_columns in _TYPED_REQUIRED_INDEX_COLUMNS.items():
        if name not in index_names:
            raise ValidationError("schedule typed entry indexes are incomplete")
        actual_columns = tuple(
            str(row[2]) for row in connection.execute(f"PRAGMA index_info({name})")
        )
        if actual_columns != expected_columns:
            raise ValidationError("schedule typed entry index definition disagrees")


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


def _typed_route_values(
    identity: str, record: Mapping[str, Any]
) -> tuple[str, str, str, list[str], list[tuple[str, str, str]]]:
    if set(record) != _TYPED_ROUTE_KEYS:
        raise ValidationError("typed schedule route is malformed")
    demand = record.get("id")
    net = record.get("net")
    source = record.get("source")
    sinks = record.get("sinks")
    edges = record.get("tree_edges")
    if (
        demand != identity
        or not all(
            isinstance(value, str) and value
            for value in (demand, net, source)
        )
        or not isinstance(sinks, list)
        or not all(isinstance(value, str) and value for value in sinks)
        or not isinstance(edges, list)
    ):
        raise ValidationError("typed schedule route is malformed")
    typed_edges: list[tuple[str, str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping) or set(edge) != _TYPED_ROUTE_EDGE_KEYS:
            raise ValidationError("typed schedule route edge is malformed")
        values = (edge.get("link"), edge.get("from"), edge.get("to"))
        if not all(isinstance(value, str) and value for value in values):
            raise ValidationError("typed schedule route edge is malformed")
        typed_edges.append(values)
    return demand, net, source, list(sinks), typed_edges


def _typed_route_mapping(
    demand: str,
    net: str,
    source: str,
    sinks: Sequence[str],
    edges: Sequence[tuple[str, str, str]],
) -> dict[str, Any]:
    return {
        "id": demand,
        "net": net,
        "source": source,
        "sinks": list(sinks),
        "tree_edges": [
            {"link": link, "from": edge_source, "to": edge_sink}
            for link, edge_source, edge_sink in edges
        ],
    }


def _typed_route_canonical(
    demand: str,
    net: str,
    source: str,
    sinks: Sequence[str],
    edges: Sequence[tuple[str, str, str]],
) -> str:
    encoded_sinks = ",".join(_canonical(sink) for sink in sinks)
    encoded_edges = ",".join(
        (
            f'{{"from":{_canonical(edge_source)},'
            f'"link":{_canonical(link)},"to":{_canonical(edge_sink)}}}'
        )
        for link, edge_source, edge_sink in edges
    )
    return (
        f'{{"id":{_canonical(demand)},"net":{_canonical(net)},'
        f'"sinks":[{encoded_sinks}],"source":{_canonical(source)},'
        f'"tree_edges":[{encoded_edges}]}}'
    )


def _typed_completion_values(
    identity: str, record: Mapping[str, Any]
) -> tuple[str, str, int, int, list[tuple[str, int]]]:
    if set(record) != _TYPED_COMPLETION_KEYS:
        raise ValidationError("typed schedule completion is malformed")
    demand = record.get("demand")
    net = record.get("net")
    source_ready = record.get("source_ready_slot")
    completion = record.get("completion_slot")
    arrivals = record.get("sink_arrival_slots")
    if (
        demand != identity
        or not all(isinstance(value, str) and value for value in (demand, net))
        or not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (source_ready, completion)
        )
        or not isinstance(arrivals, Mapping)
    ):
        raise ValidationError("typed schedule completion is malformed")
    typed_arrivals: list[tuple[str, int]] = []
    for sink, arrival in arrivals.items():
        if (
            not isinstance(sink, str)
            or not sink
            or not isinstance(arrival, int)
            or isinstance(arrival, bool)
        ):
            raise ValidationError("typed schedule sink arrival is malformed")
        typed_arrivals.append((sink, arrival))
    # Canonical logical records sort object keys, including sink identities.
    typed_arrivals.sort()
    return demand, net, source_ready, completion, typed_arrivals


def _typed_completion_mapping(
    demand: str,
    net: str,
    source_ready: int,
    completion: int,
    arrivals: Sequence[tuple[str, int]],
) -> dict[str, Any]:
    return {
        "demand": demand,
        "net": net,
        "source_ready_slot": source_ready,
        "completion_slot": completion,
        "sink_arrival_slots": dict(arrivals),
    }


def _typed_completion_canonical(
    demand: str,
    net: str,
    source_ready: int,
    completion: int,
    arrivals: Sequence[tuple[str, int]],
) -> str:
    encoded_arrivals = ",".join(
        f"{_canonical(sink)}:{arrival}" for sink, arrival in arrivals
    )
    return (
        f'{{"completion_slot":{completion},"demand":{_canonical(demand)},'
        f'"net":{_canonical(net)},"sink_arrival_slots":'
        f'{{{encoded_arrivals}}},"source_ready_slot":{source_ready}}}'
    )


def _validate_fully_typed_relation_structure(
    connection: sqlite3.Connection,
    *,
    expected_routes: int,
    expected_completions: int,
    expected_sinks: int,
    expected_edges: int,
    expected_arrivals: int,
) -> None:
    for table, expected in (
        ("schedule_routes", expected_routes),
        ("schedule_completions", expected_completions),
    ):
        summary = connection.execute(
            f"SELECT count(*),count(DISTINCT seq),min(seq),max(seq) FROM {table}"
        ).fetchone()
        if summary != (
            expected,
            expected,
            0 if expected else None,
            expected - 1 if expected else None,
        ):
            raise ValidationError(
                f"{table} typed sequence is not contiguous"
            )
    for table, parent_column, parent_table, expected in (
        ("schedule_route_sinks", "route_seq", "schedule_routes", expected_sinks),
        ("schedule_route_edges", "route_seq", "schedule_routes", expected_edges),
        (
            "schedule_completion_arrivals",
            "completion_seq",
            "schedule_completions",
            expected_arrivals,
        ),
    ):
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if count != expected:
            raise ValidationError(f"{table} typed coverage disagrees")
        gap = connection.execute(
            f"SELECT 1 FROM {table} GROUP BY {parent_column} "
            "HAVING min(seq)!=0 OR max(seq)!=count(*)-1 LIMIT 1"
        ).fetchone()
        orphan = connection.execute(
            f"SELECT 1 FROM {table} c LEFT JOIN {parent_table} p "
            f"ON p.seq=c.{parent_column} WHERE p.seq IS NULL LIMIT 1"
        ).fetchone()
        if gap is not None or orphan is not None:
            raise ValidationError(f"{table} typed sequence is malformed")


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
    expected_user_version = TDM_SCHEDULE_USER_VERSION
    if (
        schema != TDM_SCHEDULE_STORAGE_SCHEMA
        or value.get("logical_schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
        or value.get("application_id") != TDM_SCHEDULE_APPLICATION_ID
        or (value.get("user_version") != expected_user_version)
        or (not isinstance(value.get("logical_sha256"), str))
        or (len(value["logical_sha256"]) != 64)
        or (not isinstance(value.get("store_sha256"), str))
        or (len(value["store_sha256"]) != 64)
        or isinstance(value.get("store_bytes"), bool)
        or (not isinstance(value.get("store_bytes"), int))
        or (value["store_bytes"] < 0)
        or (not isinstance(value.get("sections"), dict))
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    if (
        not isinstance(value.get("logical_pretty_sha256"), str)
        or len(value["logical_pretty_sha256"]) != 64
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    relation = value.get("entry_relation")
    if (
        not isinstance(relation, dict)
        or relation.get("schema") != TYPED_ENTRY_RELATION_SCHEMA
        or isinstance(relation.get("records"), bool)
        or (not isinstance(relation.get("records"), int))
        or (relation["records"] < 0)
    ):
        raise ValidationError(f"{path}: schedule typed entry relation is malformed")
    if value.get("artifact") != _content_addressed_store_name(value["store_sha256"]):
        raise ValidationError(f"{path}: schedule typed artifact identity disagrees")
    route_relation = value.get("route_relation")
    completion_relation = value.get("completion_relation")
    if (
        not isinstance(route_relation, dict)
        or route_relation.get("schema") != TYPED_ROUTE_RELATION_SCHEMA
        or any(
            (
                isinstance(route_relation.get(key), bool)
                or not isinstance(route_relation.get(key), int)
                or route_relation[key] < 0
                for key in ("records", "sink_records", "edge_records")
            )
        )
        or (not isinstance(completion_relation, dict))
        or (completion_relation.get("schema") != TYPED_COMPLETION_RELATION_SCHEMA)
        or any(
            (
                isinstance(completion_relation.get(key), bool)
                or not isinstance(completion_relation.get(key), int)
                or completion_relation[key] < 0
                for key in ("records", "arrival_records")
            )
        )
    ):
        raise ValidationError(
            f"{path}: schedule typed route/completion relations are malformed"
        )
    sections = value["sections"]
    if set(sections) != set(TDM_SCHEDULE_SECTIONS):
        raise ValidationError(f"{path}: schedule storage sections disagree")
    for section in TDM_SCHEDULE_SECTIONS:
        record = sections[section]
        if (
            not isinstance(record, dict)
            or isinstance(record.get("records"), bool)
            or (not isinstance(record.get("records"), int))
            or (record["records"] < 0)
            or (not isinstance(record.get("sha256"), str))
            or (len(record["sha256"]) != 64)
        ):
            raise ValidationError(
                f"{path}: schedule storage section {section!r} is malformed"
            )
    if value["entry_relation"]["records"] != sections["entries"]["records"]:
        raise ValidationError(
            f"{path}: schedule typed entry relation coverage disagrees"
        )
    if (
        value["route_relation"]["records"] != sections["routes"]["records"]
        or value["completion_relation"]["records"]
        != sections["demand_completions"]["records"]
    ):
        raise ValidationError(
            f"{path}: schedule typed route/completion coverage disagrees"
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


def write_native_exact_generation_request(path: Path, producer: "NativeExactScheduleRequest") -> None:
    """Serialize only board-sized configuration for the common native producer."""
    with path.open("wb") as stream:
        def text(value: str) -> None:
            encoded = value.encode("utf-8")
            stream.write(struct.pack("<I", len(encoded)))
            stream.write(encoded)

        def value(item: Any) -> None:
            text(_canonical(item))
            text(json.dumps(item, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))

        stream.write(struct.pack("<QI3Q4q", 0x3147534D44544545, 1, *producer.route_generation))
        stream.write(struct.pack("<I", len(producer.header)))
        for key, item in sorted(producer.header.items()):
            text(key)
            value(item)
        stream.write(struct.pack("<I", len(producer.domain_templates)))
        for domain in producer.domain_templates:
            text(domain["key"])
            value(domain)


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


def _qualified_typed_entry_columns(alias: str) -> str:
    return ",".join(
        f"{alias}.{column}" for column in _TYPED_ENTRY_COLUMNS.split(",")
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


class TypedScheduleRouteSequence(Sequence[Mapping[str, Any]]):
    """Lazy route records reconstructed from normalized typed relations."""

    section = "routes"

    def __init__(self, store_path: Path, count: int, digest: str):
        self._store_path = store_path
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def _iter_values(
        self, order: str
    ) -> Iterator[tuple[str, str, str, list[str], list[tuple[str, str, str]]]]:
        connection = sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            parents = connection.execute(
                "SELECT seq,demand,net,source_fpga FROM schedule_routes "
                f"ORDER BY {order}"
            )
            joined_order = "r.seq" if order == "seq" else "r.demand"
            sinks = iter(connection.execute(
                "SELECT r.seq,s.sink_fpga FROM schedule_routes r "
                "JOIN schedule_route_sinks s ON s.route_seq=r.seq "
                f"ORDER BY {joined_order},s.seq"
            ))
            edges = iter(connection.execute(
                "SELECT r.seq,e.link_id,e.source_fpga,e.sink_fpga "
                "FROM schedule_routes r JOIN schedule_route_edges e "
                "ON e.route_seq=r.seq "
                f"ORDER BY {joined_order},e.seq"
            ))
            sink_row = next(sinks, None)
            edge_row = next(edges, None)
            observed = 0
            for route_seq, demand, net, source in parents:
                observed += 1
                route_sinks: list[str] = []
                while sink_row is not None and sink_row[0] == route_seq:
                    route_sinks.append(sink_row[1])
                    sink_row = next(sinks, None)
                route_edges: list[tuple[str, str, str]] = []
                while edge_row is not None and edge_row[0] == route_seq:
                    route_edges.append(tuple(edge_row[1:]))
                    edge_row = next(edges, None)
                yield demand, net, source, route_sinks, route_edges
            if observed != self._count or sink_row is not None or edge_row is not None:
                raise ValidationError(
                    "schedule routes typed relation coverage disagrees"
                )
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule routes typed relation is unreadable"
            ) from error
        finally:
            connection.close()

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return (
            _typed_route_mapping(*values)
            for values in self._iter_values("seq")
        )

    def iter_canonical_records(self) -> Iterator[str]:
        return (
            _typed_route_canonical(*values)
            for values in self._iter_values("seq")
        )

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            parent = connection.execute(
                "SELECT demand,net,source_fpga FROM schedule_routes WHERE seq=?",
                (index,),
            ).fetchone()
            if parent is None:
                raise ValidationError(
                    "schedule routes typed sequence is not contiguous"
                )
            sinks = [
                row[0]
                for row in connection.execute(
                    "SELECT sink_fpga FROM schedule_route_sinks "
                    "WHERE route_seq=? ORDER BY seq",
                    (index,),
                )
            ]
            edges = [
                tuple(row)
                for row in connection.execute(
                    "SELECT link_id,source_fpga,sink_fpga "
                    "FROM schedule_route_edges WHERE route_seq=? ORDER BY seq",
                    (index,),
                )
            ]
            return _typed_route_mapping(*parent, sinks, edges)
        finally:
            connection.close()

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        return (
            _typed_route_mapping(*values)
            for values in self._iter_values("demand")
        )


class TypedScheduleCompletionSequence(Sequence[Mapping[str, Any]]):
    """Lazy completion/sink-arrival records backed by typed rows."""

    section = "demand_completions"

    def __init__(self, store_path: Path, count: int, digest: str):
        self._store_path = store_path
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def _iter_values(
        self, order: str
    ) -> Iterator[tuple[str, str, int, int, list[tuple[str, int]]]]:
        connection = sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            parents = connection.execute(
                "SELECT seq,demand,net,source_ready_slot,completion_slot "
                f"FROM schedule_completions ORDER BY {order}"
            )
            joined_order = "c.seq" if order == "seq" else "c.demand"
            arrivals = iter(connection.execute(
                "SELECT c.seq,a.sink_fpga,a.arrival_slot "
                "FROM schedule_completions c "
                "JOIN schedule_completion_arrivals a "
                "ON a.completion_seq=c.seq "
                f"ORDER BY {joined_order},a.seq"
            ))
            arrival_row = next(arrivals, None)
            observed = 0
            for completion_seq, demand, net, ready, completion in parents:
                observed += 1
                values: list[tuple[str, int]] = []
                while arrival_row is not None and arrival_row[0] == completion_seq:
                    values.append((arrival_row[1], arrival_row[2]))
                    arrival_row = next(arrivals, None)
                yield demand, net, ready, completion, values
            if observed != self._count or arrival_row is not None:
                raise ValidationError(
                    "schedule completions typed relation coverage disagrees"
                )
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule completions typed relation is unreadable"
            ) from error
        finally:
            connection.close()

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return (
            _typed_completion_mapping(*values)
            for values in self._iter_values("seq")
        )

    def iter_canonical_records(self) -> Iterator[str]:
        return (
            _typed_completion_canonical(*values)
            for values in self._iter_values("seq")
        )

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = sqlite3.connect(
            f"file:{self._store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            parent = connection.execute(
                "SELECT demand,net,source_ready_slot,completion_slot "
                "FROM schedule_completions WHERE seq=?",
                (index,),
            ).fetchone()
            if parent is None:
                raise ValidationError(
                    "schedule completions typed sequence is not contiguous"
                )
            arrivals = [
                tuple(row)
                for row in connection.execute(
                    "SELECT sink_fpga,arrival_slot "
                    "FROM schedule_completion_arrivals "
                    "WHERE completion_seq=? ORDER BY seq",
                    (index,),
                )
            ]
            return _typed_completion_mapping(*parent, arrivals)
        finally:
            connection.close()

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        return (
            _typed_completion_mapping(*values)
            for values in self._iter_values("demand")
        )


class TDMScheduleView(Mapping[str, Any]):
    """Lazy, relocatable view over a sealed schedule store."""

    def __init__(self, manifest_path: Path, *, verify_store_hash: bool = True):
        self.manifest_path = manifest_path.resolve()
        selector_source = bind_store_source(
            self.manifest_path, "tdm-schedule-selector-v4"
        )
        self.manifest = _raw_manifest(self.manifest_path)
        store_path = _validate_manifest(self.manifest, self.manifest_path)
        try:
            store_source = bind_store_source(store_path, "tdm-schedule-store-v4")
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
                != self.manifest["user_version"]
            ):
                raise ValidationError("unsupported schedule SQLite artifact")
            metadata = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            if (
                metadata.get("logical_schema") != TDM_SCHEDULE_LOGICAL_SCHEMA
                or metadata.get("logical_sha256") != self.manifest["logical_sha256"]
                or (not isinstance(metadata.get("header"), dict))
                or (metadata["header"].get("schema") != TDM_SCHEDULE_LOGICAL_SCHEMA)
            ):
                raise ValidationError("schedule SQLite metadata disagrees")
            if (
                metadata.get("entry_relation_schema") != TYPED_ENTRY_RELATION_SCHEMA
                or metadata.get("entry_relation_records")
                != self.manifest["entry_relation"]["records"]
            ):
                raise ValidationError("schedule typed entry metadata disagrees")
            if (
                metadata.get("route_relation_schema") != TYPED_ROUTE_RELATION_SCHEMA
                or metadata.get("route_relation_records")
                != self.manifest["route_relation"]["records"]
                or metadata.get("route_relation_sink_records")
                != self.manifest["route_relation"]["sink_records"]
                or (
                    metadata.get("route_relation_edge_records")
                    != self.manifest["route_relation"]["edge_records"]
                )
                or (
                    metadata.get("completion_relation_schema")
                    != TYPED_COMPLETION_RELATION_SCHEMA
                )
                or (
                    metadata.get("completion_relation_records")
                    != self.manifest["completion_relation"]["records"]
                )
                or (
                    metadata.get("completion_relation_arrival_records")
                    != self.manifest["completion_relation"]["arrival_records"]
                )
            ):
                raise ValidationError(
                    "schedule typed route/completion metadata disagrees"
                )
            self._header = metadata["header"]
            self._sections = {}
            for section in TDM_SCHEDULE_SECTIONS:
                descriptor = self.manifest["sections"][section]
                if section == "entries":
                    records = TypedScheduleEntrySequence(
                        self.store_path, descriptor["records"], descriptor["sha256"]
                    )
                elif section == "routes":
                    records = TypedScheduleRouteSequence(
                        self.store_path, descriptor["records"], descriptor["sha256"]
                    )
                elif section == "demand_completions":
                    records = TypedScheduleCompletionSequence(
                        self.store_path, descriptor["records"], descriptor["sha256"]
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
                if section == "entries":
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_entries"
                    ).fetchone()[0]
                elif section == "routes":
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_routes"
                    ).fetchone()[0]
                elif section == "demand_completions":
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_completions"
                    ).fetchone()[0]
                else:
                    actual = int(actual_counts.get(section, 0))
                if actual != len(records):
                    raise ValidationError(f"schedule {section} row count disagrees")
            expected_record_sections = set(TDM_SCHEDULE_SECTIONS)
            expected_record_sections.remove("entries")
            if "entries" in actual_counts:
                raise ValidationError("typed schedule duplicates logical entry records")
            _validate_typed_relation_structure(
                connection, len(self._sections["entries"])
            )
            expected_record_sections.remove("routes")
            expected_record_sections.remove("demand_completions")
            if "routes" in actual_counts or "demand_completions" in actual_counts:
                raise ValidationError(
                    "typed schedule duplicates route/completion records"
                )
            _validate_fully_typed_relation_structure(
                connection,
                expected_routes=len(self._sections["routes"]),
                expected_completions=len(self._sections["demand_completions"]),
                expected_sinks=self.manifest["route_relation"]["sink_records"],
                expected_edges=self.manifest["route_relation"]["edge_records"],
                expected_arrivals=self.manifest["completion_relation"][
                    "arrival_records"
                ],
            )
            if set(actual_counts) - expected_record_sections:
                raise ValidationError("schedule SQLite has an unknown section")
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule SQLite typed relations are unreadable"
            ) from error
        finally:
            connection.close()

        self._source_bindings = (selector_source, store_source)
        require_store_sources_unchanged(self._source_bindings)

    def bound_store_sources(self):
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def maximum_arrival_slot(self):
        """Reduce current typed arrivals without a Python hop traversal."""
        from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
        sources=self.bound_store_sources()
        with closing(sqlite3.connect(immutable_sqlite_uri(self.store_path),uri=True)) as connection:
            value=connection.execute('SELECT coalesce(max(arrival_slot),0) FROM schedule_entries').fetchone()[0]
        require_store_sources_unchanged(sources)
        if type(value) is not int or value<0:
            raise ValidationError('typed schedule has an invalid maximum arrival')
        return value

    def native_validation_header(self):
        """Bounded header projection; record sections remain in the store."""
        require_store_sources_unchanged(self._source_bindings)
        return dict(self._header)

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
                raise ValidationError(f"schedule {section} logical digest disagrees")
            result[section] = materialized
        if (
            verify_records
            and _streaming_digest(result) != self.manifest["logical_sha256"]
        ):
            raise ValidationError("schedule logical digest disagrees")
        return result

    def iter_canonical_section_records(self, section: str) -> Iterator[str]:
        """Stream sealed canonical records without building Python objects."""
        if section not in self._sections:
            raise ValidationError(f"unknown schedule section {section!r}")
        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        digest = hashlib.sha256()
        count = 0
        try:
            if section == "entries":
                source = (
                    (_typed_entry_canonical(row),)
                    for row in connection.execute(
                        f"SELECT {_TYPED_ENTRY_COLUMNS} FROM schedule_entries ORDER BY seq"
                    )
                )
            elif section in {"routes", "demand_completions"}:
                source = (
                    (encoded,)
                    for encoded in self._sections[section].iter_canonical_records()
                )
            else:
                source = connection.execute(
                    "SELECT record_json FROM records WHERE section=? ORDER BY seq",
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
            raise ValidationError(f"schedule {section} logical seal disagrees")

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
                    if key == "entries":
                        source = (
                            (_typed_entry_canonical(row),)
                            for row in connection.execute(
                                f"SELECT {_TYPED_ENTRY_COLUMNS} FROM schedule_entries ORDER BY seq"
                            )
                        )
                    elif key in {"routes", "demand_completions"}:
                        source = (
                            (encoded,)
                            for encoded in self._sections[key].iter_canonical_records()
                        )
                    else:
                        source = connection.execute(
                            "SELECT record_json FROM records WHERE section=? ORDER BY seq",
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
                        or section_digest.hexdigest() != records.expected_sha256
                    ):
                        raise ValidationError(f"schedule {key} logical seal disagrees")
                    _update_digest(compact_digest, "]")
                    if count:
                        _update_digest(pretty_digest, "\n  ]")
                else:
                    value = self._header[key]
                    _update_digest(compact_digest, _canonical(value))
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule logical seal records are unreadable"
            ) from error
        finally:
            connection.close()
        if compact_digest.hexdigest() != self.manifest["logical_sha256"]:
            raise ValidationError("schedule compact logical seal disagrees")
        expected_pretty = self.manifest.get("logical_pretty_sha256")
        if expected_pretty is not None and pretty_digest.hexdigest() != expected_pretty:
            raise ValidationError("schedule pretty logical seal disagrees")


    def iter_hop_identity_records(self) -> Iterator[tuple[str, str, str, str, str]]:
        """Stream canonical hop keys and entry identities for certificates."""
        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            rows = connection.execute(
                "\n                    SELECT demand,link_id,source_fpga,sink_fpga,\n                           entry_id,entry_id,1\n                    FROM schedule_entries\n                    ORDER BY demand,link_id,source_fpga,sink_fpga,entry_id\n                    "
            )
            count = 0
            for row in rows:
                count += 1
                (demand, link, source, target, identity, record_id, valid) = row
                if (
                    valid != 1
                    or record_id != identity
                    or (
                        not all(
                            (
                                isinstance(value, str) and value
                                for value in (demand, link, source, target, identity)
                            )
                        )
                    )
                ):
                    raise ValidationError("schedule hop identity record is malformed")
                yield (demand, link, source, target, identity)
            if count != len(self._sections["entries"]):
                raise ValidationError("schedule hop identity coverage disagrees")
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule hop identity typed relation is unreadable"
            ) from error
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
    """Open the current typed store or an explicitly supplied logical JSON."""

    value = _raw_manifest(path)
    if value.get("schema") == TDM_SCHEDULE_STORAGE_SCHEMA:
        view = open_tdm_schedule_view(path, verify_store_hash=verify_store_hash)
        return view.materialize() if materialize else view
    if value.get("schema") != TDM_SCHEDULE_LOGICAL_SCHEMA:
        raise ValidationError("unsupported schedule storage or logical schema")
    return value


def canonical_tdm_schedule_sha256(schedule: Mapping[str, Any]) -> str:
    """Return the canonical digest without forcing indexed expansion."""

    if isinstance(schedule, TDMScheduleView):
        return schedule.manifest["logical_sha256"]
    return _streaming_digest(schedule)


def pretty_tdm_schedule_sha256(schedule: Mapping[str, Any]) -> str:
    """Return the historical indented digest used by timing seals."""

    if isinstance(schedule, TDMScheduleView):
        return schedule.manifest["logical_pretty_sha256"]
    return _streaming_digest(schedule, indent=2, trailing_newline=True)


def tdm_schedule_store_path(manifest_path: Path) -> Path | None:
    """Return the indexed sidecar for a storage manifest, if present."""

    value = _raw_manifest(manifest_path)
    if value.get("schema") != TDM_SCHEDULE_STORAGE_SCHEMA:
        if str(value.get("schema", "")).startswith("emuflow.tdm-schedule-storage/"):
            raise ValidationError("unsupported schedule storage schema")
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
