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
import struct
import subprocess
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from .tdm import NativeExactScheduleRequest

from .errors import ValidationError
from .io import file_sha256, write_json


LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v1"
TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v2"
TYPED_TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v3"
FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA = "emuflow.tdm-schedule-storage/v4"
TDM_SCHEDULE_STORAGE_SCHEMAS = {
    LEGACY_TDM_SCHEDULE_STORAGE_SCHEMA,
    TDM_SCHEDULE_STORAGE_SCHEMA,
    TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
}
TDM_SCHEDULE_LOGICAL_SCHEMA = "emuflow.tdm-schedule/v1"
TDM_SCHEDULE_APPLICATION_ID = 0x454D5554  # ASCII "EMUT"
TDM_SCHEDULE_USER_VERSION = 1
TYPED_TDM_SCHEDULE_USER_VERSION = 2
FULLY_TYPED_TDM_SCHEDULE_USER_VERSION = 3
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
TYPED_ROUTE_RELATION_SCHEMA = "emuflow.tdm-schedule-route-relation/v1"
TYPED_COMPLETION_RELATION_SCHEMA = (
    "emuflow.tdm-schedule-completion-relation/v1"
)
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

FULLY_TYPED_DDL = TYPED_DDL + """
CREATE TABLE schedule_routes(
  seq INTEGER NOT NULL UNIQUE,
  demand TEXT NOT NULL PRIMARY KEY,
  net TEXT NOT NULL,
  source_fpga TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE schedule_route_sinks(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq)
) WITHOUT ROWID;
CREATE TABLE schedule_route_edges(
  route_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  link_id TEXT NOT NULL,
  source_fpga TEXT NOT NULL,
  sink_fpga TEXT NOT NULL,
  PRIMARY KEY(route_seq,seq)
) WITHOUT ROWID;
CREATE TABLE schedule_completions(
  seq INTEGER NOT NULL UNIQUE,
  demand TEXT NOT NULL PRIMARY KEY,
  net TEXT NOT NULL,
  source_ready_slot INTEGER NOT NULL,
  completion_slot INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE schedule_completion_arrivals(
  completion_seq INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  sink_fpga TEXT NOT NULL,
  arrival_slot INTEGER NOT NULL,
  PRIMARY KEY(completion_seq,seq),
  UNIQUE(completion_seq,sink_fpga)
) WITHOUT ROWID;
CREATE INDEX schedule_routes_net ON schedule_routes(net);
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

_TYPED_ROUTE_KEYS = {"id", "net", "source", "sinks", "tree_edges"}
_TYPED_ROUTE_EDGE_KEYS = {"link", "from", "to"}
_TYPED_COMPLETION_KEYS = {
    "demand",
    "net",
    "source_ready_slot",
    "completion_slot",
    "sink_arrival_slots",
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


class ScheduleDeadlineArrival(NamedTuple):
    entry_id: str
    arrival_slot: int


class ScheduleDeadlineSource(NamedTuple):
    deadline_slot: int
    causal_sink: str


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


class ScheduleStaticExactTimingIndex:
    """Narrow deadline queries over the authoritative schedule entry rows."""

    def __init__(
        self,
        store_path: Path,
        expected_entries: int,
        *,
        producer_owned_entries: bool,
        producer_owned_routes: bool,
    ) -> None:
        self._connection = sqlite3.connect(
            f"file:{store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        self._expected_entries = expected_entries
        self._producer_owned_entries = producer_owned_entries
        self._producer_owned_routes = producer_owned_routes
        self._entry_relation = "schedule_entries"
        self._route_relation = "schedule_routes"
        try:
            if producer_owned_routes:
                indexes = {
                    str(row[1])
                    for row in self._connection.execute(
                        "PRAGMA index_list(schedule_routes)"
                    )
                }
                if "schedule_routes_net" in indexes:
                    columns = tuple(
                        str(row[2])
                        for row in self._connection.execute(
                            "PRAGMA index_info(schedule_routes_net)"
                        )
                    )
                    if columns != ("net",):
                        raise ValidationError(
                            "static exact route lookup index is malformed"
                        )
                else:
                    self._connection.execute("PRAGMA temp_store=FILE")
                    self._connection.execute(
                        "CREATE TEMP TABLE static_exact_routes("
                        "net TEXT NOT NULL,demand TEXT NOT NULL)"
                    )
                    self._connection.execute(
                        "INSERT INTO static_exact_routes "
                        "SELECT net,demand FROM schedule_routes"
                    )
                    self._connection.execute(
                        "CREATE INDEX static_exact_routes_net "
                        "ON static_exact_routes(net)"
                    )
                    self._route_relation = "static_exact_routes"
            else:
                self._connection.execute("PRAGMA temp_store=FILE")
                self._connection.execute(
                    "CREATE TEMP TABLE static_exact_entries("
                    "entry_id TEXT PRIMARY KEY,demand TEXT NOT NULL,"
                    "net TEXT NOT NULL,link_id TEXT NOT NULL,"
                    "source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL,"
                    "slot INTEGER NOT NULL,arrival_slot INTEGER NOT NULL) "
                    "WITHOUT ROWID"
                )
                if producer_owned_entries:
                    self._connection.execute(
                        "INSERT INTO static_exact_entries "
                        "SELECT entry_id,demand,net,link_id,source_fpga,"
                        "sink_fpga,slot,arrival_slot FROM schedule_entries"
                    )
                else:
                    self._connection.execute(
                        "INSERT INTO static_exact_entries "
                        "SELECT identity,"
                        "json_extract(record_json,'$.demand'),"
                        "json_extract(record_json,'$.net'),"
                        "json_extract(record_json,'$.link'),"
                        "json_extract(record_json,'$.from'),"
                        "json_extract(record_json,'$.to'),"
                        "json_extract(record_json,'$.slot'),"
                        "json_extract(record_json,'$.arrival_slot') "
                        "FROM records WHERE section='entries'"
                    )
                self._connection.execute(
                    "CREATE INDEX static_exact_entries_source "
                    "ON static_exact_entries(net,source_fpga,entry_id)"
                )
                self._connection.execute(
                    "CREATE UNIQUE INDEX static_exact_entries_arrival "
                    "ON static_exact_entries(net,sink_fpga)"
                )
                self._entry_relation = "static_exact_entries"
            count = self._connection.execute(
                f"SELECT count(*) FROM {self._entry_relation}"
            ).fetchone()[0]
            duplicate = self._connection.execute(
                f"SELECT demand,sink_fpga FROM {self._entry_relation} "
                "GROUP BY demand,sink_fpga HAVING count(*)!=1 LIMIT 1"
            ).fetchone()
            if count != expected_entries:
                raise ValidationError(
                    "static exact schedule entry coverage disagrees"
                )
            if duplicate is not None:
                raise ValidationError(
                    "static exact schedule has a duplicate current-frame "
                    "arrival"
                )
        except ValidationError:
            self.close()
            raise
        except (sqlite3.DatabaseError, sqlite3.IntegrityError) as error:
            self.close()
            raise ValidationError(
                "static exact schedule timing index is unreadable"
            ) from error

    def __enter__(self) -> "ScheduleStaticExactTimingIndex":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        if connection is not None:
            connection.close()
            self._connection = None

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("static exact schedule timing index is closed")
        return self._connection

    def iter_link_ids(self) -> Iterator[str]:
        connection = self._open_connection()
        try:
            rows = connection.execute(
                f"SELECT DISTINCT link_id FROM {self._entry_relation} "
                "ORDER BY link_id"
            )
            for row in rows:
                if not isinstance(row[0], str) or not row[0]:
                    raise ValidationError(
                        "static exact schedule link identity is malformed"
                    )
                yield row[0]
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "static exact schedule link identities are unreadable"
            ) from error

    def _demand_for_net(self, net: str) -> Optional[str]:
        if not self._producer_owned_routes:
            return None
        connection = self._open_connection()
        rows = connection.execute(
            f"SELECT demand FROM {self._route_relation} WHERE net=? LIMIT 2",
            (net,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            raise ValidationError(
                "static exact schedule route identity is malformed"
            )
        return rows[0][0]

    def source(self, net: str, fpga: str) -> ScheduleDeadlineSource:
        connection = self._open_connection()
        try:
            demand = self._demand_for_net(net)
            if self._producer_owned_routes:
                if demand is None:
                    rows = ()
                else:
                    rows = connection.execute(
                        "SELECT entry_id,slot FROM schedule_entries "
                        "WHERE demand=? AND source_fpga=? ORDER BY entry_id",
                        (demand, fpga),
                    )
            else:
                rows = connection.execute(
                    f"SELECT entry_id,slot FROM {self._entry_relation} "
                    "WHERE net=? AND source_fpga=? ORDER BY entry_id",
                    (net, fpga),
                )
            identities: list[str] = []
            deadline = None
            for identity, slot in rows:
                if (
                    not isinstance(identity, str)
                    or not identity
                    or isinstance(slot, bool)
                    or not isinstance(slot, int)
                ):
                    raise ValidationError(
                        "static exact source entry is malformed"
                    )
                identities.append(identity)
                deadline = slot if deadline is None else min(deadline, slot)
            if deadline is None:
                raise ValidationError(
                    f"static exact cut {net!r} has no TX from source "
                    f"{fpga!r}"
                )
            return ScheduleDeadlineSource(deadline, ",".join(identities))
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "static exact source entries are unreadable"
            ) from error

    def arrival(
        self, net: str, fpga: str
    ) -> Optional[ScheduleDeadlineArrival]:
        connection = self._open_connection()
        try:
            demand = self._demand_for_net(net)
            if self._producer_owned_routes:
                if demand is None:
                    rows = []
                else:
                    rows = connection.execute(
                        "SELECT entry_id,arrival_slot FROM schedule_entries "
                        "WHERE demand=? AND sink_fpga=? "
                        "ORDER BY entry_id LIMIT 2",
                        (demand, fpga),
                    ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT entry_id,arrival_slot FROM {self._entry_relation} "
                    "WHERE net=? AND sink_fpga=? ORDER BY entry_id LIMIT 2",
                    (net, fpga),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "static exact arrival entry is unreadable"
            ) from error
        if not rows:
            return None
        if len(rows) != 1:
            raise ValidationError(
                "static exact schedule has a duplicate current-frame arrival"
            )
        identity, arrival_slot = rows[0]
        if (
            not isinstance(identity, str)
            or not identity
            or isinstance(arrival_slot, bool)
            or not isinstance(arrival_slot, int)
        ):
            raise ValidationError("static exact arrival entry is malformed")
        return ScheduleDeadlineArrival(identity, arrival_slot)


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
class ScheduleValidationIndex(Mapping[tuple[str, str, str, str], Mapping[str, Any]]):
    """Disk-backed typed hop index over one sealed ScheduleStore.

    Stores v3/v4 own this indexed relation directly. Legacy v1/v2 stores
    retain the compatibility path which derives a temporary relation from
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
            connection.execute("PRAGMA temp_store=FILE")
            if self._producer_owned:
                _validate_typed_relation_structure(
                    connection, self._expected_entries
                )
            else:
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

    def validate_expected_completions(
        self, records: Iterator[Mapping[str, Any]]
    ) -> int:
        """Compare recomputed completions through a bounded TEMP relation.

        Phase 5 produces completions in dependency order while their logical
        ScheduleStore section is ordered by demand.  Sorting R Python objects
        duplicated the full completion/sink relation.  This method spools the
        independent reconstruction into SQLite in bounded batches and compares
        the complete typed relation (or an explicit legacy canonical row)
        without weakening coverage.
        """

        db = self._db
        db.execute(
            "CREATE TEMP TABLE expected_schedule_completions("
            "demand TEXT PRIMARY KEY,net TEXT NOT NULL,"
            "source_ready_slot INTEGER NOT NULL,completion_slot INTEGER NOT NULL,"
            "canonical_record TEXT NOT NULL) WITHOUT ROWID"
        )
        db.execute(
            "CREATE TEMP TABLE expected_schedule_completion_arrivals("
            "demand TEXT NOT NULL,seq INTEGER NOT NULL,sink_fpga TEXT NOT NULL,"
            "arrival_slot INTEGER NOT NULL,PRIMARY KEY(demand,seq),"
            "UNIQUE(demand,sink_fpga)) WITHOUT ROWID"
        )
        completion_batch: list[tuple[Any, ...]] = []
        arrival_batch: list[tuple[Any, ...]] = []
        observed = 0

        def flush() -> None:
            if completion_batch:
                db.executemany(
                    "INSERT INTO expected_schedule_completions "
                    "VALUES(?,?,?,?,?)",
                    completion_batch,
                )
                completion_batch.clear()
            if arrival_batch:
                db.executemany(
                    "INSERT INTO expected_schedule_completion_arrivals "
                    "VALUES(?,?,?,?)",
                    arrival_batch,
                )
                arrival_batch.clear()

        try:
            for record in records:
                demand = record.get("demand")
                net = record.get("net")
                source_ready = record.get("source_ready_slot")
                completion = record.get("completion_slot")
                arrivals = record.get("sink_arrival_slots")
                if (
                    not isinstance(demand, str)
                    or not demand
                    or not isinstance(net, str)
                    or not net
                    or isinstance(source_ready, bool)
                    or not isinstance(source_ready, int)
                    or isinstance(completion, bool)
                    or not isinstance(completion, int)
                    or not isinstance(arrivals, Mapping)
                ):
                    raise ValidationError(
                        "recomputed schedule completion is malformed"
                    )
                canonical = _canonical(record)
                completion_batch.append(
                    (demand, net, source_ready, completion, canonical)
                )
                for sequence, (sink, arrival) in enumerate(
                    sorted(arrivals.items())
                ):
                    if (
                        not isinstance(sink, str)
                        or not sink
                        or isinstance(arrival, bool)
                        or not isinstance(arrival, int)
                    ):
                        raise ValidationError(
                            "recomputed schedule sink arrival is malformed"
                        )
                    arrival_batch.append(
                        (demand, sequence, sink, arrival)
                    )
                observed += 1
                if len(completion_batch) >= 4096:
                    flush()
            flush()
        except sqlite3.IntegrityError as error:
            raise ValidationError(
                "recomputed schedule completion identity repeats"
            ) from error

        typed = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schedule_completions'"
        ).fetchone()
        if typed is not None:
            scalar_mismatch = db.execute(
                "SELECT * FROM (SELECT demand,net,source_ready_slot,"
                "completion_slot FROM expected_schedule_completions EXCEPT "
                "SELECT demand,net,source_ready_slot,completion_slot FROM "
                "schedule_completions) UNION ALL SELECT * FROM (SELECT demand,"
                "net,source_ready_slot,completion_slot FROM schedule_completions "
                "EXCEPT SELECT demand,net,source_ready_slot,completion_slot "
                "FROM expected_schedule_completions) LIMIT 1"
            ).fetchone()
            arrival_mismatch = db.execute(
                "SELECT * FROM (SELECT e.demand,a.seq,a.sink_fpga,a.arrival_slot "
                "FROM schedule_completions e JOIN "
                "schedule_completion_arrivals a ON a.completion_seq=e.seq "
                "EXCEPT SELECT demand,seq,sink_fpga,arrival_slot FROM "
                "expected_schedule_completion_arrivals) UNION ALL SELECT * "
                "FROM (SELECT demand,seq,sink_fpga,arrival_slot FROM "
                "expected_schedule_completion_arrivals EXCEPT SELECT e.demand,"
                "a.seq,a.sink_fpga,a.arrival_slot FROM schedule_completions e "
                "JOIN schedule_completion_arrivals a ON a.completion_seq=e.seq) "
                "LIMIT 1"
            ).fetchone()
            if scalar_mismatch is not None or arrival_mismatch is not None:
                raise ValidationError(
                    "schedule.demand_completions does not match recomputed values"
                )
        else:
            mismatch = db.execute(
                "SELECT * FROM (SELECT demand,canonical_record FROM "
                "expected_schedule_completions EXCEPT SELECT identity,"
                "record_json FROM records WHERE section='demand_completions') "
                "UNION ALL SELECT * FROM (SELECT identity,record_json FROM "
                "records WHERE section='demand_completions' EXCEPT SELECT "
                "demand,canonical_record FROM expected_schedule_completions) "
                "LIMIT 1"
            ).fetchone()
            if mismatch is not None:
                raise ValidationError(
                    "schedule.demand_completions does not match recomputed values"
                )
        return observed

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
    expected_user_version = {
        TYPED_TDM_SCHEDULE_STORAGE_SCHEMA: TYPED_TDM_SCHEDULE_USER_VERSION,
        FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA: (
            FULLY_TYPED_TDM_SCHEDULE_USER_VERSION
        ),
    }.get(schema, TDM_SCHEDULE_USER_VERSION)
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
        FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    } and (
        not isinstance(value.get("logical_pretty_sha256"), str)
        or len(value["logical_pretty_sha256"]) != 64
    ):
        raise ValidationError(f"{path}: schedule storage manifest is malformed")
    if schema in {
        TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
        FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
    }:
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
    if schema == FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA:
        route_relation = value.get("route_relation")
        completion_relation = value.get("completion_relation")
        if (
            not isinstance(route_relation, dict)
            or route_relation.get("schema") != TYPED_ROUTE_RELATION_SCHEMA
            or any(
                isinstance(route_relation.get(key), bool)
                or not isinstance(route_relation.get(key), int)
                or route_relation[key] < 0
                for key in ("records", "sink_records", "edge_records")
            )
            or not isinstance(completion_relation, dict)
            or completion_relation.get("schema")
            != TYPED_COMPLETION_RELATION_SCHEMA
            or any(
                isinstance(completion_relation.get(key), bool)
                or not isinstance(completion_relation.get(key), int)
                or completion_relation[key] < 0
                for key in ("records", "arrival_records")
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
            or not isinstance(record.get("records"), int)
            or record["records"] < 0
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
        ):
            raise ValidationError(
                f"{path}: schedule storage section {section!r} is malformed"
            )
    if (
        schema in {
            TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
            FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
        }
        and value["entry_relation"]["records"]
        != sections["entries"]["records"]
    ):
        raise ValidationError(
            f"{path}: schedule typed entry relation coverage disagrees"
        )
    if schema == FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA and (
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
    fully_typed: bool,
) -> tuple[
    str,
    str,
    dict[str, dict[str, Any]],
    dict[str, int],
]:
    """Hash and insert the schedule with one traversal of each large section."""

    compact_digest = hashlib.sha256()
    pretty_digest = hashlib.sha256()
    section_summaries: dict[str, dict[str, Any]] = {}
    relation_counts = {
        "route_sinks": 0,
        "route_edges": 0,
        "completion_arrivals": 0,
    }
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
                        "INSERT INTO schedule_entries("
                        "seq,entry_id,demand,net,hop,link_id,source_fpga,"
                        "sink_fpga,capacity_key,slot,lane,ready_slot,"
                        "arrival_slot,tdm_ratio) VALUES (?,?,?,?,?,?,?,?,"
                        "?,?,?,?,?,?)",
                        pending_entries,
                    )
                    pending_entries.clear()
                if pending_routes:
                    connection.executemany(
                        "INSERT INTO schedule_routes("
                        "seq,demand,net,source_fpga) VALUES (?,?,?,?)",
                        pending_routes,
                    )
                    pending_routes.clear()
                if pending_sinks:
                    connection.executemany(
                        "INSERT INTO schedule_route_sinks("
                        "route_seq,seq,sink_fpga) VALUES (?,?,?)",
                        pending_sinks,
                    )
                    pending_sinks.clear()
                if pending_edges:
                    connection.executemany(
                        "INSERT INTO schedule_route_edges("
                        "route_seq,seq,link_id,source_fpga,sink_fpga) "
                        "VALUES (?,?,?,?,?)",
                        pending_edges,
                    )
                    pending_edges.clear()
                if pending_completions:
                    connection.executemany(
                        "INSERT INTO schedule_completions("
                        "seq,demand,net,source_ready_slot,completion_slot) "
                        "VALUES (?,?,?,?,?)",
                        pending_completions,
                    )
                    pending_completions.clear()
                if pending_arrivals:
                    connection.executemany(
                        "INSERT INTO schedule_completion_arrivals("
                        "completion_seq,seq,sink_fpga,arrival_slot) "
                        "VALUES (?,?,?,?)",
                        pending_arrivals,
                    )
                    pending_arrivals.clear()

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
                and cursor_schema == NATIVE_EXACT_CANONICAL_ENTRY_CURSOR_SCHEMA
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
                stored_typed = False
                if typed_entries and key == "entries":
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
                elif fully_typed and key == "routes":
                    if not isinstance(typed_value, Mapping):
                        raise ValidationError("typed schedule route is malformed")
                    demand, net, route_source, sinks, edges = _typed_route_values(
                        identity, typed_value
                    )
                    encoded = _typed_route_canonical(
                        demand, net, route_source, sinks, edges
                    )
                    pending_routes.append((seq, demand, net, route_source))
                    pending_sinks.extend(
                        (seq, child_seq, sink)
                        for child_seq, sink in enumerate(sinks)
                    )
                    pending_edges.extend(
                        (seq, child_seq, *edge)
                        for child_seq, edge in enumerate(edges)
                    )
                    relation_counts["route_sinks"] += len(sinks)
                    relation_counts["route_edges"] += len(edges)
                    stored_typed = True
                elif fully_typed and key == "demand_completions":
                    if not isinstance(typed_value, Mapping):
                        raise ValidationError(
                            "typed schedule completion is malformed"
                        )
                    demand, net, ready, completion, arrivals = (
                        _typed_completion_values(identity, typed_value)
                    )
                    encoded = _typed_completion_canonical(
                        demand, net, ready, completion, arrivals
                    )
                    pending_completions.append(
                        (seq, demand, net, ready, completion)
                    )
                    pending_arrivals.extend(
                        (seq, child_seq, sink, arrival)
                        for child_seq, (sink, arrival) in enumerate(arrivals)
                    )
                    relation_counts["completion_arrivals"] += len(arrivals)
                    stored_typed = True

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
                if not stored_typed:
                    pending.append((key, seq, identity, encoded))
                if len(pending) >= 4096:
                    connection.executemany(
                        "INSERT INTO records(section,seq,identity,record_json) "
                        "VALUES (?,?,?,?)",
                        pending,
                    )
                    pending.clear()
                if max(
                    len(pending_entries),
                    len(pending_routes),
                    len(pending_sinks),
                    len(pending_edges),
                    len(pending_completions),
                    len(pending_arrivals),
                ) >= 4096:
                    flush_typed()
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
                ("entry_relation_schema", _canonical(TYPED_ENTRY_RELATION_SCHEMA)),
                (
                    "entry_relation_records",
                    _canonical(section_summaries["entries"]["records"]),
                ),
            )
        )
    if fully_typed:
        metadata.extend(
            (
                ("route_relation_schema", _canonical(TYPED_ROUTE_RELATION_SCHEMA)),
                (
                    "route_relation_records",
                    _canonical(section_summaries["routes"]["records"]),
                ),
                (
                    "route_relation_sink_records",
                    _canonical(relation_counts["route_sinks"]),
                ),
                (
                    "route_relation_edge_records",
                    _canonical(relation_counts["route_edges"]),
                ),
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
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES (?,?)", metadata
    )
    return (
        compact_digest.hexdigest(),
        pretty_digest.hexdigest(),
        section_summaries,
        relation_counts,
    )

def _publish_native_exact_store(
    temporary: Path,
    schedule: Mapping[str, Any],
    source: Mapping[str, Any],
) -> tuple[str, str, dict[str, dict[str, Any]], dict[str, int]]:
    """Native record owner publishes v4; Python sends only header/domain data."""

    if source.get("schema") != "emuflow.native-exact-schedule-publication-source/v1":
        raise ValidationError("unsupported native schedule publication source")
    if source.get("route_records") is not schedule["routes"] or source.get("completion_records") is not schedule["demand_completions"]:
        raise ValidationError("native ScheduleStore record owners were replaced")
    command = source.get("command")
    if not isinstance(command, list) or len(command) != 3 or command[1:] != ["phase5", "publish-schedule"]:
        raise ValidationError("invalid native schedule publication command")
    request = temporary.with_name(temporary.name + ".request")
    try:
        with request.open("wb") as stream:
            def text(value: str) -> None:
                encoded = value.encode("utf-8")
                stream.write(struct.pack("<I", len(encoded)))
                stream.write(encoded)

            def value(item: Any) -> None:
                text(_canonical(item))
                text(json.dumps(item, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))

            stream.write(struct.pack("<QI", 0x3157534D44544545, 1))
            generations = source.get("generations", ())
            if len(generations) != 3 or any(len(item) != 7 for item in generations):
                raise ValidationError("native ScheduleStore source generations are missing")
            for generation in generations:
                stream.write(struct.pack("<3Q4q", *generation))
            catalogs = source.get("catalogs", ())
            if len(catalogs) != 3:
                raise ValidationError("native ScheduleStore board catalogs are missing")
            for catalog in catalogs:
                stream.write(struct.pack("<I", len(catalog)))
                for identity in catalog:
                    text(identity)
            header = {key: item for key, item in schedule.items() if key not in TDM_SCHEDULE_SECTIONS}
            stream.write(struct.pack("<I", len(header)))
            for key, item in sorted(header.items()):
                text(key)
                value(item)
            domains = schedule["domain_schedules"]
            stream.write(struct.pack("<I", len(domains)))
            for domain in domains:
                text(_section_identity("domain_schedules", domain))
                value(domain)
        completed = subprocess.run(
            [*command, str(source["input"]), str(source["result"]),
             str(source["route_store"]), str(request), str(temporary)],
            capture_output=True, text=True,
        )
        if completed.returncode:
            raise ValidationError("native ScheduleStore publication failed: " + completed.stderr.strip())
        with sqlite3.connect(f"file:{temporary.resolve()}?mode=ro&immutable=1", uri=True) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='native_publication_summary'").fetchone()
        if row is None:
            raise ValidationError("native ScheduleStore publication summary is missing")
        summary = json.loads(row[0])
        if summary.get("schema") != "emuflow.native-schedule-publication/v1":
            raise ValidationError("unsupported native ScheduleStore publication summary")
        sections = summary["sections"]
        for section in TDM_SCHEDULE_SECTIONS:
            if sections[section]["records"] != len(schedule[section]):
                raise ValidationError("native ScheduleStore publication coverage disagrees")
        return (summary["logical_sha256"], summary["logical_pretty_sha256"],
                sections, summary["relation_counts"])
    finally:
        request.unlink(missing_ok=True)


def _publish_native_exact_generation(
    temporary: Path, producer: "NativeExactScheduleRequest",
) -> tuple[str, str, dict[str, dict[str, Any]], dict[str, int]]:
    """One core call owns model, calculation, certificate and typed records."""
    from .tdm import _run_native_exact_schedule

    board = temporary.with_name(temporary.name + ".board-request")
    request = temporary.with_name(temporary.name + ".generation-request")
    try:
        producer.write_board_request(board)
        with request.open("wb") as stream:
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
        _run_native_exact_schedule([
            *producer.command, str(producer.route_store), str(board), str(request), str(temporary),
        ])
        with sqlite3.connect(f"file:{temporary.resolve()}?mode=ro&immutable=1", uri=True) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='native_publication_summary'").fetchone()
        if row is None:
            raise ValidationError("native ScheduleStore generation summary is missing")
        summary = json.loads(row[0])
        if summary.get("schema") != "emuflow.native-schedule-publication/v1":
            raise ValidationError("unsupported native ScheduleStore generation summary")
        sections = summary["sections"]
        if (sections["routes"]["records"] != producer.route_count or
                sections["demand_completions"]["records"] != producer.route_count or
                sections["domain_schedules"]["records"] != len(producer.domain_templates)):
            raise ValidationError("native ScheduleStore generation coverage disagrees")
        return summary["logical_sha256"], summary["logical_pretty_sha256"], sections, summary["relation_counts"]
    finally:
        board.unlink(missing_ok=True)
        request.unlink(missing_ok=True)


def write_tdm_schedule_storage(
    manifest_path: Path,
    schedule: Union[Mapping[str, Any], "NativeExactScheduleRequest"],
) -> dict[str, Any]:
    """Atomically publish the indexed store, then its compact commit manifest."""

    from .tdm import NativeExactScheduleRequest
    native_generation = isinstance(schedule, NativeExactScheduleRequest)
    header = schedule.header if native_generation else schedule
    if header.get("schema") != TDM_SCHEDULE_LOGICAL_SCHEMA:
        raise ValidationError("cannot store an unsupported TDM schedule schema")
    for section in (() if native_generation else TDM_SCHEDULE_SECTIONS):
        records = schedule.get(section)
        if not isinstance(records, list):
            raise ValidationError(f"schedule.{section}: expected an array")

    typed_entries = header.get("provider") == TDM_EXACT_DEPENDENCY_PROVIDER
    fully_typed = bool(
        typed_entries
        and header.get("qualification")
        == "dependency-schedule-readiness-pass"
    )
    storage_schema = (
        FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        if fully_typed
        else TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
        if typed_entries
        else TDM_SCHEDULE_STORAGE_SCHEMA
    )
    user_version = (
        FULLY_TYPED_TDM_SCHEDULE_USER_VERSION
        if fully_typed
        else TYPED_TDM_SCHEDULE_USER_VERSION
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
    connection: sqlite3.Connection | None = None
    try:
        native_source = (None if native_generation else
                         getattr(schedule["entries"], "native_store_publication_source", None))
        if native_generation:
            if not fully_typed:
                raise ValidationError("native generation requires fully typed exact semantics")
            (logical_sha256, logical_pretty_sha256, section_summaries, relation_counts) = (
                _publish_native_exact_generation(temporary, schedule)
            )
        elif native_source is not None:
            if not fully_typed:
                raise ValidationError("native ScheduleStore publication requires fully typed exact semantics")
            (logical_sha256, logical_pretty_sha256, section_summaries, relation_counts) = (
                _publish_native_exact_store(temporary, schedule, native_source)
            )
            # The core has committed and closed its store. No Python row writer
            # or logical-record hash traversal follows this production branch.
        else:
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(f"PRAGMA application_id={TDM_SCHEDULE_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={user_version}")
            connection.executescript(
                FULLY_TYPED_DDL if fully_typed else (
                    TYPED_DDL if typed_entries else LEGACY_DDL
                )
            )
            (
                logical_sha256,
                logical_pretty_sha256,
                section_summaries,
                relation_counts,
            ) = _publish_logical_schedule(
                connection,
                schedule,
                typed_entries=typed_entries,
                fully_typed=fully_typed,
            )
            if typed_entries:
                connection.executescript(TYPED_INDEX_DDL)
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
                raise ValidationError(
                    "schedule content-addressed artifact disagrees"
                )
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
    if typed_entries:
        manifest["entry_relation"] = {
            "schema": TYPED_ENTRY_RELATION_SCHEMA,
            "records": section_summaries["entries"]["records"],
        }
    if fully_typed:
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
        self._producer_owned_entries = self.manifest["schema"] in {
            TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
            FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA,
        }
        self._producer_owned_routes = (
            self.manifest["schema"]
            == FULLY_TYPED_TDM_SCHEDULE_STORAGE_SCHEMA
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
            if self._producer_owned_routes and (
                metadata.get("route_relation_schema")
                != TYPED_ROUTE_RELATION_SCHEMA
                or metadata.get("route_relation_records")
                != self.manifest["route_relation"]["records"]
                or metadata.get("route_relation_sink_records")
                != self.manifest["route_relation"]["sink_records"]
                or metadata.get("route_relation_edge_records")
                != self.manifest["route_relation"]["edge_records"]
                or metadata.get("completion_relation_schema")
                != TYPED_COMPLETION_RELATION_SCHEMA
                or metadata.get("completion_relation_records")
                != self.manifest["completion_relation"]["records"]
                or metadata.get("completion_relation_arrival_records")
                != self.manifest["completion_relation"]["arrival_records"]
            ):
                raise ValidationError(
                    "schedule typed route/completion metadata disagrees"
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
                elif section == "routes" and self._producer_owned_routes:
                    records = TypedScheduleRouteSequence(
                        self.store_path,
                        descriptor["records"],
                        descriptor["sha256"],
                    )
                elif (
                    section == "demand_completions"
                    and self._producer_owned_routes
                ):
                    records = TypedScheduleCompletionSequence(
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
                if section == "entries" and self._producer_owned_entries:
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_entries"
                    ).fetchone()[0]
                elif section == "routes" and self._producer_owned_routes:
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_routes"
                    ).fetchone()[0]
                elif (
                    section == "demand_completions"
                    and self._producer_owned_routes
                ):
                    actual = connection.execute(
                        "SELECT count(*) FROM schedule_completions"
                    ).fetchone()[0]
                else:
                    actual = int(actual_counts.get(section, 0))
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
            if self._producer_owned_routes:
                expected_record_sections.remove("routes")
                expected_record_sections.remove("demand_completions")
                if (
                    "routes" in actual_counts
                    or "demand_completions" in actual_counts
                ):
                    raise ValidationError(
                        "typed schedule duplicates route/completion records"
                    )
                _validate_fully_typed_relation_structure(
                    connection,
                    expected_routes=len(self._sections["routes"]),
                    expected_completions=len(
                        self._sections["demand_completions"]
                    ),
                    expected_sinks=self.manifest["route_relation"][
                        "sink_records"
                    ],
                    expected_edges=self.manifest["route_relation"][
                        "edge_records"
                    ],
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
            elif (
                self._producer_owned_routes
                and section in {"routes", "demand_completions"}
            ):
                source = (
                    (encoded,)
                    for encoded in self._sections[
                        section
                    ].iter_canonical_records()
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
                    elif (
                        self._producer_owned_routes
                        and key in {"routes", "demand_completions"}
                    ):
                        source = (
                            (encoded,)
                            for encoded in self._sections[
                                key
                            ].iter_canonical_records()
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

    def open_static_exact_timing_index(
        self,
    ) -> ScheduleStaticExactTimingIndex:
        """Open bounded source/arrival queries for Phase 7C deadlines."""

        return ScheduleStaticExactTimingIndex(
            self.store_path,
            len(self._sections["entries"]),
            producer_owned_entries=self._producer_owned_entries,
            producer_owned_routes=self._producer_owned_routes,
        )

    def iter_entry_route_records(
        self,
    ) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Stream entries in identity order with their owning route record."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            if self._producer_owned_routes:
                rows = connection.execute(
                    f"""
                    WITH ordered_sinks AS (
                      SELECT route_seq,seq,json_quote(sink_fpga) AS value
                      FROM schedule_route_sinks
                      ORDER BY route_seq,seq
                    ), route_sinks AS (
                      SELECT route_seq,
                             '[' || group_concat(value,',') || ']' AS value
                      FROM ordered_sinks
                      GROUP BY route_seq
                    ), ordered_edges AS (
                      SELECT route_seq,seq,
                             '{{"link":' || json_quote(link_id) ||
                             ',"from":' || json_quote(source_fpga) ||
                             ',"to":' || json_quote(sink_fpga) || '}}' AS value
                      FROM schedule_route_edges
                      ORDER BY route_seq,seq
                    ), route_edges AS (
                      SELECT route_seq,
                             '[' || group_concat(value,',') || ']' AS value
                      FROM ordered_edges
                      GROUP BY route_seq
                    )
                    SELECT {_qualified_typed_entry_columns("e")},
                           r.demand,r.net,r.source_fpga,
                           COALESCE(s.value,'[]'),COALESCE(t.value,'[]')
                    FROM schedule_entries e
                    JOIN schedule_routes r ON r.demand=e.demand
                    LEFT JOIN route_sinks s ON s.route_seq=r.seq
                    LEFT JOIN route_edges t ON t.route_seq=r.seq
                    ORDER BY e.entry_id
                    """
                )
            elif self._producer_owned_entries:
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
                if self._producer_owned_routes:
                    entry_values = _validate_typed_entry_values(
                        row[0], row[:13]
                    )
                    entry = _typed_entry_mapping(entry_values)
                    demand, net, source, sinks_json, edges_json = row[13:]
                    sinks = json.loads(sinks_json)
                    edge_records = json.loads(edges_json)
                    route_record = {
                        "id": demand,
                        "net": net,
                        "source": source,
                        "sinks": sinks,
                        "tree_edges": edge_records,
                    }
                    route = _typed_route_mapping(
                        *_typed_route_values(demand, route_record)
                    )
                elif self._producer_owned_entries:
                    entry = _typed_entry_mapping(row[:-1])
                    route_json = row[-1]
                else:
                    entry_json, route_json = row
                    entry = json.loads(entry_json)
                if not self._producer_owned_routes:
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule entry/route typed relation is unreadable"
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
            if self._producer_owned_routes:
                order_clause = (
                    "length(e.entry_id),e.entry_id"
                    if identity_order
                    else "e.seq"
                )
                rows = connection.execute(
                    f"""
                    SELECT e.entry_id,e.entry_id,e.demand,e.net,e.link_id,
                           e.source_fpga,e.sink_fpga,e.slot,e.arrival_slot,
                           e.lane,e.tdm_ratio,r.demand,r.demand,r.net,
                           r.source_fpga,1
                    FROM schedule_entries e
                    JOIN schedule_routes r ON r.demand=e.demand
                    ORDER BY {order_clause}
                    """
                )
            elif self._producer_owned_entries:
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule transport typed relation is unreadable"
            ) from error
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
            if self._producer_owned_routes:
                rows = connection.execute(
                    """
                    SELECT demand,link_id,source_fpga,sink_fpga,
                           entry_id,entry_id,1
                    FROM schedule_entries
                    ORDER BY demand,link_id,source_fpga,sink_fpga,entry_id
                    """
                )
            elif self._producer_owned_entries:
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule hop identity typed relation is unreadable"
            ) from error
        finally:
            connection.close()

    def iter_incoming_records(self) -> Iterator[tuple[str, str, str]]:
        """Stream (net, target FPGA, entry id) for Phase 6 cut joins."""

        connection = sqlite3.connect(
            f"file:{self.store_path.resolve()}?mode=ro&immutable=1", uri=True
        )
        try:
            if self._producer_owned_routes:
                duplicate = connection.execute(
                    """
                    SELECT 1
                    FROM schedule_entries e
                    JOIN schedule_routes r ON r.demand=e.demand
                    GROUP BY r.net,e.sink_fpga
                    HAVING count(*) != 1
                    LIMIT 1
                    """
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT r.net,e.sink_fpga,e.entry_id
                    FROM schedule_entries e
                    JOIN schedule_routes r ON r.demand=e.demand
                    ORDER BY r.net,e.sink_fpga,e.entry_id
                    """
                )
            elif self._producer_owned_entries:
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
            if self._producer_owned_routes:
                pass
            elif self._producer_owned_entries:
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
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "schedule incoming typed relation is unreadable"
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
