"""Typed owner for routed logic-segment timing evidence.

The JSON path is a bounded, atomic selector.  Segment, unsupported-member and
unmeasured rows live in one immutable content-addressed SQLite generation.
Readers hold a shared lease until every cursor is closed; writers commit the
new selector last under an exclusive lease and collect stale generations only
after the commit is durable.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import fcntl
from functools import lru_cache
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Optional
import weakref

from .errors import ValidationError
from .io import file_sha256, read_json, write_json


LOGIC_SEGMENT_TIMING_SCHEMA = "emuflow.logic-segment-timing/v1"
LOGIC_SEGMENT_TIMING_STORAGE_SCHEMA = (
    "emuflow.logic-segment-timing-storage/v1"
)
LOGIC_SEGMENT_TIMING_APPLICATION_ID = 0x454D5547  # ASCII "EMUG"
LOGIC_SEGMENT_TIMING_USER_VERSION = 1
_STORE_SUFFIX = ".sqlite"
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version",
    "header", "counts", "source", "logical_sha256", "artifact",
    "store_bytes", "store_sha256",
}
_HEADER_REQUIRED = {
    "schema", "status", "design", "platform", "fpga", "provider",
    "qualification", "coverage", "system_routes_sha256",
    "tdm_schedule_sha256",
}
_HEADER_OPTIONAL = {"semantic_contract_sha256"}
_SOURCE_KEYS = {
    "design", "platform", "fpga", "system_routes_sha256",
    "tdm_schedule_sha256", "semantic_contract_sha256",
}
_COUNT_KEYS = {
    "segments", "system_paths", "member_paths", "unsupported_member_paths",
    "unmeasured_segments", "endpoint_exact_segments", "cone_bound_segments",
}
_SEGMENT_REQUIRED = {
    "id", "kind", "system_path", "member_path", "cut_index", "fpga",
    "replace_tx_endpoint", "start_pin", "end_pin", "delay_ns",
}
_SEGMENT_OPTIONAL = {
    "static_exact_segment_id", "start_object_kind", "end_object_kind",
    "cone_anchor_object_kind", "cone_anchor_pin", "measurement",
    "actual_start_object", "actual_end_object",
}
_UNMEASURED_KEYS = {
    "id", "kind", "system_path", "member_path", "reason",
}
_UNSUPPORTED_KEYS = {"path", "reason"}

_DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE segments(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  segment_id TEXT NOT NULL CHECK(length(segment_id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('launch','transition','capture')),
  system_path TEXT NOT NULL CHECK(length(system_path) > 0),
  member_path TEXT NOT NULL CHECK(length(member_path) > 0),
  cut_index INTEGER NOT NULL CHECK(cut_index >= 0),
  fpga TEXT NOT NULL CHECK(length(fpga) > 0),
  replace_tx_endpoint TEXT,
  start_pin TEXT NOT NULL CHECK(length(start_pin) > 0),
  end_pin TEXT NOT NULL CHECK(length(end_pin) > 0),
  delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0),
  static_exact_segment_id TEXT,
  start_object_kind TEXT CHECK(start_object_kind IN ('pin','port')),
  end_object_kind TEXT CHECK(end_object_kind IN ('pin','port')),
  cone_anchor_object_kind TEXT CHECK(cone_anchor_object_kind IN ('pin','port')),
  cone_anchor_pin TEXT,
  measurement TEXT NOT NULL CHECK(measurement IN ('endpoint-exact','cut-net-cone-upper-bound')),
  actual_start_object TEXT,
  actual_end_object TEXT
);
CREATE UNIQUE INDEX segments_id_unique ON segments(segment_id);
CREATE UNIQUE INDEX segments_ordered_key_unique
  ON segments(system_path,member_path,cut_index);
CREATE INDEX segments_ordered_cursor
  ON segments(system_path,member_path,cut_index,segment_id);
CREATE TABLE unsupported_members(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  path TEXT NOT NULL CHECK(length(path) > 0),
  reason TEXT NOT NULL CHECK(length(reason) > 0)
);
CREATE UNIQUE INDEX unsupported_members_path_unique
  ON unsupported_members(path);
CREATE TABLE unmeasured_segments(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  segment_id TEXT NOT NULL CHECK(length(segment_id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('launch','transition','capture')),
  system_path TEXT NOT NULL CHECK(length(system_path) > 0),
  member_path TEXT NOT NULL CHECK(length(member_path) > 0),
  reason TEXT NOT NULL CHECK(length(reason) > 0)
);
CREATE UNIQUE INDEX unmeasured_segments_id_unique
  ON unmeasured_segments(segment_id);
"""

_EXPECTED_COLUMNS = {
    "metadata": (
        (0, "key", "TEXT", 1, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
    ),
    "segments": (
        (0, "seq", "INTEGER", 1, None, 1, 0),
        (1, "segment_id", "TEXT", 1, None, 0, 0),
        (2, "kind", "TEXT", 1, None, 0, 0),
        (3, "system_path", "TEXT", 1, None, 0, 0),
        (4, "member_path", "TEXT", 1, None, 0, 0),
        (5, "cut_index", "INTEGER", 1, None, 0, 0),
        (6, "fpga", "TEXT", 1, None, 0, 0),
        (7, "replace_tx_endpoint", "TEXT", 0, None, 0, 0),
        (8, "start_pin", "TEXT", 1, None, 0, 0),
        (9, "end_pin", "TEXT", 1, None, 0, 0),
        (10, "delay_ns", "REAL", 1, None, 0, 0),
        (11, "static_exact_segment_id", "TEXT", 0, None, 0, 0),
        (12, "start_object_kind", "TEXT", 0, None, 0, 0),
        (13, "end_object_kind", "TEXT", 0, None, 0, 0),
        (14, "cone_anchor_object_kind", "TEXT", 0, None, 0, 0),
        (15, "cone_anchor_pin", "TEXT", 0, None, 0, 0),
        (16, "measurement", "TEXT", 1, None, 0, 0),
        (17, "actual_start_object", "TEXT", 0, None, 0, 0),
        (18, "actual_end_object", "TEXT", 0, None, 0, 0),
    ),
    "unsupported_members": (
        (0, "seq", "INTEGER", 1, None, 1, 0),
        (1, "path", "TEXT", 1, None, 0, 0),
        (2, "reason", "TEXT", 1, None, 0, 0),
    ),
    "unmeasured_segments": (
        (0, "seq", "INTEGER", 1, None, 1, 0),
        (1, "segment_id", "TEXT", 1, None, 0, 0),
        (2, "kind", "TEXT", 1, None, 0, 0),
        (3, "system_path", "TEXT", 1, None, 0, 0),
        (4, "member_path", "TEXT", 1, None, 0, 0),
        (5, "reason", "TEXT", 1, None, 0, 0),
    ),
}
_EXPECTED_INDEXES = {
    "metadata": (),
    "segments": (
        ("segments_id_unique", 1, ("segment_id",)),
        ("segments_ordered_cursor", 0, (
            "system_path", "member_path", "cut_index", "segment_id"
        )),
        ("segments_ordered_key_unique", 1, (
            "system_path", "member_path", "cut_index"
        )),
    ),
    "unsupported_members": (
        ("unsupported_members_path_unique", 1, ("path",)),
    ),
    "unmeasured_segments": (
        ("unmeasured_segments_id_unique", 1, ("segment_id",)),
    ),
}


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            sort_keys=True, allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("logic segment timing value is not canonical") from error


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _owner_prefix(path: Path) -> str:
    return f"{path.name}.store."


def _store_name(path: Path, digest: str) -> str:
    return f"{_owner_prefix(path)}{digest}{_STORE_SUFFIX}"


class _Lease:
    def __init__(self, selector_path: Path, *, exclusive: bool):
        parent = selector_path.parent.resolve()
        lock_path = parent / f".{selector_path.name}.lock"
        if lock_path.is_symlink():
            raise ValidationError("logic segment timing lock is a symbolic link")
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(lock_path, flags, 0o600)
        self.closed = False
        try:
            fcntl.flock(
                self.descriptor,
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except BaseException:
            os.close(self.descriptor)
            self.closed = True
            raise

    def downgrade_shared(self) -> None:
        if self.closed:
            raise ValidationError("logic segment timing lease is closed")
        fcntl.flock(self.descriptor, fcntl.LOCK_SH)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _normalized_sql(value: Any) -> str:
    return " ".join(str(value).split())


@lru_cache(maxsize=1)
def _expected_sqlite_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_DDL)
        return tuple(
            (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
    finally:
        connection.close()


def _validate_header(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _HEADER_REQUIRED - _HEADER_OPTIONAL:
        raise ValidationError("logic segment timing header is malformed")
    if not _HEADER_REQUIRED <= set(value):
        raise ValidationError("logic segment timing header is incomplete")
    coverage = value.get("coverage")
    if (
        value.get("schema") != LOGIC_SEGMENT_TIMING_SCHEMA
        or value.get("status") != "pass"
        or any(not isinstance(value.get(key), str) or not value[key]
               for key in (
                   "design", "platform", "fpga", "provider", "qualification",
                   "system_routes_sha256", "tdm_schedule_sha256",
               ))
        or not isinstance(coverage, dict)
        or any(not isinstance(key, str) for key in coverage)
        or any(not _strict_nonnegative_int(item) for item in coverage.values())
    ):
        raise ValidationError("logic segment timing header is malformed")
    semantic = value.get("semantic_contract_sha256")
    if semantic is not None and not _valid_digest(semantic):
        raise ValidationError("logic segment timing semantic seal is malformed")
    for key in ("system_routes_sha256", "tdm_schedule_sha256"):
        if not _valid_digest(value.get(key)):
            raise ValidationError(
                f"logic segment timing {key} seal is malformed"
            )
    return dict(value)


def _source(header: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "design": header["design"],
        "platform": header["platform"],
        "fpga": header["fpga"],
        "system_routes_sha256": header["system_routes_sha256"],
        "tdm_schedule_sha256": header["tdm_schedule_sha256"],
        "semantic_contract_sha256": header.get("semantic_contract_sha256"),
    }


def _validate_selector(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _SELECTOR_KEYS
        or value.get("schema") != LOGIC_SEGMENT_TIMING_STORAGE_SCHEMA
        or value.get("logical_schema") != LOGIC_SEGMENT_TIMING_SCHEMA
        or value.get("application_id") != LOGIC_SEGMENT_TIMING_APPLICATION_ID
        or isinstance(value.get("application_id"), bool)
        or value.get("user_version") != LOGIC_SEGMENT_TIMING_USER_VERSION
        or isinstance(value.get("user_version"), bool)
    ):
        raise ValidationError("logic segment timing selector is malformed")
    header = _validate_header(value.get("header"))
    counts = value.get("counts")
    source = value.get("source")
    if (
        not isinstance(counts, dict)
        or set(counts) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(item) for item in counts.values())
        or not isinstance(source, dict)
        or set(source) != _SOURCE_KEYS
        or source != _source(header)
        or not _valid_digest(value.get("logical_sha256"))
    ):
        raise ValidationError("logic segment timing selector metadata is malformed")
    artifact = value.get("artifact")
    if (
        not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _valid_digest(value.get("store_sha256"))
        or not _strict_nonnegative_int(value.get("store_bytes"))
    ):
        raise ValidationError("logic segment timing artifact descriptor is malformed")
    return value


def _resolve_store(selector_path: Path, selector: Mapping[str, Any]) -> Path:
    if selector_path.is_symlink():
        raise ValidationError("logic segment timing selector is a symbolic link")
    candidate = selector_path.parent / str(selector["artifact"])
    if candidate.name != _store_name(
        selector_path, str(selector["store_sha256"])
    ):
        raise ValidationError(
            "logic segment timing artifact owner disagrees"
        )
    if candidate.is_symlink():
        raise ValidationError("logic segment timing store is a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise ValidationError("logic segment timing store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("logic segment timing store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("logic segment timing store seal disagrees")
    return resolved


def _exact_sqlite_envelope(connection: sqlite3.Connection) -> None:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        application_id != LOGIC_SEGMENT_TIMING_APPLICATION_ID
        or user_version != LOGIC_SEGMENT_TIMING_USER_VERSION
    ):
        raise ValidationError("unsupported logic segment timing store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects():
        raise ValidationError("logic segment timing store objects are invalid")
    for table, expected in _EXPECTED_COLUMNS.items():
        actual = tuple(tuple(row) for row in connection.execute(
            f"PRAGMA table_xinfo({table})"
        ))
        if actual != expected:
            raise ValidationError("logic segment timing store columns are invalid")
        indexes = []
        for row in connection.execute(f"PRAGMA index_list({table})"):
            name = str(row[1])
            if name.startswith("sqlite_"):
                continue
            columns = tuple(
                str(item[2]) for item in connection.execute(
                    f"PRAGMA index_xinfo({json.dumps(name)})"
                ) if int(item[5]) == 1
            )
            indexes.append((name, int(row[2]), columns))
        if tuple(sorted(indexes)) != tuple(sorted(_EXPECTED_INDEXES[table])):
            raise ValidationError("logic segment timing store indexes are invalid")


def _validate_segment(raw: Mapping[str, Any], header: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(raw, Mapping)
        or set(raw) - _SEGMENT_REQUIRED - _SEGMENT_OPTIONAL
        or not _SEGMENT_REQUIRED <= set(raw)
    ):
        raise ValidationError("logic segment timing record is malformed")
    record = dict(raw)
    strings = ("id", "system_path", "member_path", "fpga", "start_pin", "end_pin")
    delay = record.get("delay_ns")
    cut_index = record.get("cut_index")
    measurement = record.get("measurement", "endpoint-exact")
    if (
        any(not isinstance(record.get(key), str) or not record[key] for key in strings)
        or record["fpga"] != header["fpga"]
        or record.get("kind") not in {"launch", "transition", "capture"}
        or not _strict_nonnegative_int(cut_index)
        or isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(float(delay))
        or float(delay) < 0.0
        or measurement not in {"endpoint-exact", "cut-net-cone-upper-bound"}
    ):
        raise ValidationError("logic segment timing record is malformed")
    replacement = record.get("replace_tx_endpoint")
    if (record["kind"] == "capture") != (replacement is None) or (
        replacement is not None and (not isinstance(replacement, str) or not replacement)
    ):
        raise ValidationError("logic segment timing replacement is invalid")
    semantic = header.get("semantic_contract_sha256")
    exact_id = record.get("static_exact_segment_id")
    if (semantic is None and exact_id is not None) or (
        semantic is not None and (not isinstance(exact_id, str) or not exact_id)
    ):
        raise ValidationError("logic segment timing exact identity is invalid")
    object_kinds = (record.get("start_object_kind"), record.get("end_object_kind"))
    if any(item is not None for item in object_kinds) and any(
        item not in {"pin", "port"} for item in object_kinds
    ):
        raise ValidationError("logic segment timing object kinds are invalid")
    anchor_kind = record.get("cone_anchor_object_kind")
    anchor_pin = record.get("cone_anchor_pin")
    if (anchor_kind is None) != (anchor_pin is None) or (
        anchor_kind is not None and (
            anchor_kind not in {"pin", "port"}
            or record["kind"] == "capture"
            or not isinstance(anchor_pin, str)
            or not anchor_pin
        )
    ):
        raise ValidationError("logic segment timing cone anchor is invalid")
    if measurement == "cut-net-cone-upper-bound" and anchor_pin is None:
        raise ValidationError("logic segment timing cone-bound trace is invalid")
    for key in ("actual_start_object", "actual_end_object"):
        if key in record and (
            not isinstance(record[key], str) or not record[key]
        ):
            raise ValidationError("logic segment timing object trace is invalid")
    record["delay_ns"] = float(delay)
    record["measurement"] = measurement
    return record


def _segment_tuple(seq: int, record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        seq, record["id"], record["kind"], record["system_path"],
        record["member_path"], record["cut_index"], record["fpga"],
        record.get("replace_tx_endpoint"), record["start_pin"], record["end_pin"],
        record["delay_ns"], record.get("static_exact_segment_id"),
        record.get("start_object_kind"), record.get("end_object_kind"),
        record.get("cone_anchor_object_kind"), record.get("cone_anchor_pin"),
        record["measurement"], record.get("actual_start_object"),
        record.get("actual_end_object"),
    )


def _segment_from_row(row: sqlite3.Row) -> dict[str, Any]:
    record = {
        "id": str(row["segment_id"]), "kind": str(row["kind"]),
        "system_path": str(row["system_path"]),
        "member_path": str(row["member_path"]),
        "cut_index": int(row["cut_index"]), "fpga": str(row["fpga"]),
        "replace_tx_endpoint": row["replace_tx_endpoint"],
        "start_pin": str(row["start_pin"]), "end_pin": str(row["end_pin"]),
        "delay_ns": float(row["delay_ns"]),
    }
    optional = {
        "static_exact_segment_id": row["static_exact_segment_id"],
        "start_object_kind": row["start_object_kind"],
        "end_object_kind": row["end_object_kind"],
        "cone_anchor_object_kind": row["cone_anchor_object_kind"],
        "cone_anchor_pin": row["cone_anchor_pin"],
        "actual_start_object": row["actual_start_object"],
        "actual_end_object": row["actual_end_object"],
    }
    record.update({key: value for key, value in optional.items() if value is not None})
    record["measurement"] = str(row["measurement"])
    return record


class _StoreSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "LogicSegmentTimingView", table: str, count: int):
        self._owner = weakref.ref(owner)
        self.table = table
        self.count = count

    def _view(self) -> "LogicSegmentTimingView":
        view = self._owner()
        if view is None:
            raise ValidationError("logic segment timing view is closed")
        view._ensure_open()
        return view

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        view = self._view()
        with _connect(view.store_path) as connection:
            if self.table == "segments":
                for row in connection.execute("SELECT * FROM segments ORDER BY seq"):
                    yield _segment_from_row(row)
            elif self.table == "unsupported_members":
                for row in connection.execute(
                    "SELECT path,reason FROM unsupported_members ORDER BY seq"
                ):
                    yield {"path": str(row[0]), "reason": str(row[1])}
            else:
                for row in connection.execute(
                    "SELECT segment_id,kind,system_path,member_path,reason "
                    "FROM unmeasured_segments ORDER BY seq"
                ):
                    yield {
                        "id": str(row[0]), "kind": str(row[1]),
                        "system_path": str(row[2]), "member_path": str(row[3]),
                        "reason": str(row[4]),
                    }

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self.count))]
        if not isinstance(index, int):
            raise TypeError("logic segment timing index must be an integer")
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        view = self._view()
        with _connect(view.store_path) as connection:
            if self.table == "segments":
                row = connection.execute(
                    "SELECT * FROM segments WHERE seq=?", (index,)
                ).fetchone()
                if row is None:
                    raise ValidationError("logic segment timing sequence is incomplete")
                return _segment_from_row(row)
            columns = (
                "path,reason" if self.table == "unsupported_members" else
                "segment_id,kind,system_path,member_path,reason"
            )
            row = connection.execute(
                f"SELECT {columns} FROM {self.table} WHERE seq=?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("logic segment timing sequence is incomplete")
            if self.table == "unsupported_members":
                return {"path": str(row[0]), "reason": str(row[1])}
            return {
                "id": str(row[0]), "kind": str(row[1]),
                "system_path": str(row[2]), "member_path": str(row[3]),
                "reason": str(row[4]),
            }


class LogicSegmentTimingView(Mapping[str, Any]):
    def __init__(
        self,
        selector_path: Path,
        *,
        expected_design: Optional[str] = None,
        expected_platform: Optional[str] = None,
        expected_fpga: Optional[str] = None,
        expected_system_routes_sha256: Optional[str] = None,
        expected_tdm_schedule_sha256: Optional[str] = None,
        expected_semantic_contract_sha256: Optional[str] = None,
        expected_selector_bytes: Optional[int] = None,
        expected_selector_sha256: Optional[str] = None,
        _lease: Optional[_Lease] = None,
    ):
        self._lease = _lease or _Lease(selector_path, exclusive=False)
        self._closed = False
        try:
            if selector_path.is_symlink() or not selector_path.is_file():
                raise ValidationError("logic segment timing selector is missing")
            selector_stat = selector_path.stat()
            if (
                expected_selector_bytes is not None
                and selector_stat.st_size != expected_selector_bytes
            ) or (
                expected_selector_sha256 is not None
                and file_sha256(selector_path) != expected_selector_sha256
            ):
                raise ValidationError(
                    "logic segment timing selector seal disagrees"
                )
            self.selector_path = selector_path.resolve()
            self.selector = _validate_selector(read_json(selector_path))
            expected = {
                "design": expected_design, "platform": expected_platform,
                "fpga": expected_fpga,
                "system_routes_sha256": expected_system_routes_sha256,
                "tdm_schedule_sha256": expected_tdm_schedule_sha256,
                "semantic_contract_sha256": expected_semantic_contract_sha256,
            }
            for key, value in expected.items():
                if value is not None and self.selector["source"].get(key) != value:
                    raise ValidationError(
                        f"logic segment timing {key} source seal disagrees"
                    )
            self.store_path = _resolve_store(selector_path, self.selector)
            self._validate_store()
            counts = self.selector["counts"]
            self._segments = _StoreSequence(self, "segments", counts["segments"])
            self._unsupported = _StoreSequence(
                self, "unsupported_members", counts["unsupported_member_paths"]
            )
            self._unmeasured = _StoreSequence(
                self, "unmeasured_segments", counts["unmeasured_segments"]
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls, selector_path: Path, selector: dict[str, Any],
        store_path: Path, lease: _Lease,
    ) -> "LogicSegmentTimingView":
        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        counts = selector["counts"]
        value._segments = _StoreSequence(value, "segments", counts["segments"])
        value._unsupported = _StoreSequence(
            value, "unsupported_members", counts["unsupported_member_paths"]
        )
        value._unmeasured = _StoreSequence(
            value, "unmeasured_segments", counts["unmeasured_segments"]
        )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("logic segment timing view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "LogicSegmentTimingView":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_store(self) -> None:
        selector = self.selector
        segment_digest = hashlib.sha256()
        unsupported_digest = hashlib.sha256()
        unmeasured_digest = hashlib.sha256()
        try:
            with _connect(self.store_path) as connection:
                _exact_sqlite_envelope(connection)
                metadata_rows = list(connection.execute(
                    "SELECT key,value FROM metadata ORDER BY key"
                ))
                metadata = {
                    str(row[0]): json.loads(str(row[1])) for row in metadata_rows
                }
                if len(metadata) != len(metadata_rows) or metadata != {
                    key: selector[key]
                    for key in ("header", "counts", "source", "logical_sha256")
                }:
                    raise ValidationError("logic segment timing metadata disagrees")
                segment_count = 0
                previous_key = None
                for row in connection.execute(
                    "SELECT * FROM segments ORDER BY system_path,member_path,cut_index,segment_id"
                ):
                    key = (
                        str(row["system_path"]), str(row["member_path"]),
                        int(row["cut_index"]), str(row["segment_id"]),
                    )
                    if previous_key is not None and key <= previous_key:
                        raise ValidationError("logic segment timing order is invalid")
                    previous_key = key
                    record = _validate_segment(
                        _segment_from_row(row), selector["header"]
                    )
                    segment_digest.update(_canonical(record).encode())
                    segment_digest.update(b"\n")
                    segment_count += 1
                unsupported_count = 0
                for row in connection.execute(
                    "SELECT seq,path,reason FROM unsupported_members ORDER BY seq"
                ):
                    if int(row["seq"]) != unsupported_count:
                        raise ValidationError("unsupported member sequence is invalid")
                    record = {"path": str(row["path"]), "reason": str(row["reason"])}
                    unsupported_digest.update(_canonical(record).encode())
                    unsupported_digest.update(b"\n")
                    unsupported_count += 1
                unmeasured_count = 0
                for row in connection.execute(
                    "SELECT * FROM unmeasured_segments ORDER BY seq"
                ):
                    if int(row["seq"]) != unmeasured_count:
                        raise ValidationError("unmeasured segment sequence is invalid")
                    record = {
                        "id": str(row["segment_id"]), "kind": str(row["kind"]),
                        "system_path": str(row["system_path"]),
                        "member_path": str(row["member_path"]),
                        "reason": str(row["reason"]),
                    }
                    unmeasured_digest.update(_canonical(record).encode())
                    unmeasured_digest.update(b"\n")
                    unmeasured_count += 1
                overlap = connection.execute(
                    "SELECT s.segment_id FROM segments s "
                    "JOIN unmeasured_segments u USING(segment_id) LIMIT 1"
                ).fetchone()
                if overlap is not None:
                    raise ValidationError(
                        "logic timing segment is both measured and unmeasured"
                    )
                aggregate = connection.execute(
                    "SELECT COUNT(DISTINCT system_path),COUNT(DISTINCT member_path),"
                    "SUM(measurement='endpoint-exact'),"
                    "SUM(measurement='cut-net-cone-upper-bound'),"
                    "MIN(seq),MAX(seq),COUNT(DISTINCT seq) FROM segments"
                ).fetchone()
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("logic segment timing store is corrupt") from error
        actual_counts = {
            "segments": segment_count,
            "system_paths": int(aggregate[0]),
            "member_paths": int(aggregate[1]),
            "unsupported_member_paths": unsupported_count,
            "unmeasured_segments": unmeasured_count,
            "endpoint_exact_segments": int(aggregate[2] or 0),
            "cone_bound_segments": int(aggregate[3] or 0),
        }
        if (
            segment_count
            and (
                aggregate[4] != 0
                or aggregate[5] != segment_count - 1
                or aggregate[6] != segment_count
            )
        ) or (
            not segment_count
            and any(aggregate[index] is not None for index in (4, 5))
        ):
            raise ValidationError(
                "logic segment timing segment sequence is incomplete"
            )
        if actual_counts != selector["counts"]:
            raise ValidationError("logic segment timing row counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": selector["header"], "counts": actual_counts,
            "source": selector["source"],
            "segment_sha256": segment_digest.hexdigest(),
            "unsupported_sha256": unsupported_digest.hexdigest(),
            "unmeasured_sha256": unmeasured_digest.hexdigest(),
        }).encode()).hexdigest()
        if logical != selector["logical_sha256"]:
            raise ValidationError("logic segment timing logical seal disagrees")

    def validate_timing(self) -> dict[str, Any]:
        self._ensure_open()
        counts = self.selector["counts"]
        with _connect(self.store_path) as connection:
            maximum = connection.execute(
                "SELECT COALESCE(MAX(delay_ns),0.0) FROM segments"
            ).fetchone()[0]
        return {
            "status": "pass", "fpga": self.selector["source"]["fpga"],
            "segments": counts["segments"],
            "endpoint_exact_segments": counts["endpoint_exact_segments"],
            "cone_bound_segments": counts["cone_bound_segments"],
            "maximum_delay_ns": float(maximum),
        }

    def iter_segment_ids(self) -> Iterator[str]:
        self._ensure_open()
        with _connect(self.store_path) as connection:
            for row in connection.execute(
                "SELECT segment_id FROM segments ORDER BY segment_id"
            ):
                yield str(row[0])

    def iter_ordered_segments(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        with _connect(self.store_path) as connection:
            for row in connection.execute(
                "SELECT * FROM segments "
                "ORDER BY system_path,member_path,cut_index,segment_id"
            ):
                yield _segment_from_row(row)

    def __getitem__(self, key: str) -> Any:
        self._ensure_open()
        if key == "segments":
            return self._segments
        if key == "unsupported_member_paths":
            return self._unsupported
        if key == "unmeasured_segments":
            return self._unmeasured
        return self.selector["header"][key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        yield from self.selector["header"]
        yield "unsupported_member_paths"
        if self.selector["counts"]["unmeasured_segments"]:
            yield "unmeasured_segments"
        yield "segments"

    def __len__(self) -> int:
        return len(self.selector["header"]) + 2 + int(
            bool(self.selector["counts"]["unmeasured_segments"])
        )


def _collect_stale(path: Path, keep: Path) -> None:
    changed = False
    for candidate in path.parent.glob(
        f"{_owner_prefix(path)}*{_STORE_SUFFIX}"
    ):
        if candidate.is_symlink() or candidate.resolve() == keep.resolve():
            continue
        try:
            candidate.unlink()
            changed = True
        except OSError:
            continue
    if changed:
        _fsync_directory(path.parent)


def write_logic_segment_timing_storage(
    path: Path,
    *,
    header: Mapping[str, Any],
    segments: Iterable[Mapping[str, Any]],
    unsupported_member_paths: Iterable[Mapping[str, Any]] = (),
    unmeasured_segments: Iterable[Mapping[str, Any]] = (),
) -> LogicSegmentTimingView:
    normalized_header = _validate_header(dict(header))
    path.parent.mkdir(parents=True, exist_ok=True)
    lease: Optional[_Lease] = _Lease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    connection: Optional[sqlite3.Connection] = None
    published_store: Optional[Path] = None
    created_store = False
    selector_committed = False
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".sqlite.tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".selector.tmp", dir=path.parent
        )
        os.close(descriptor)
        selector_temporary = Path(name)
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            f"PRAGMA application_id={LOGIC_SEGMENT_TIMING_APPLICATION_ID}"
        )
        connection.execute(
            f"PRAGMA user_version={LOGIC_SEGMENT_TIMING_USER_VERSION}"
        )
        connection.executescript(_DDL)
        segment_count = 0
        batch = []
        previous_segment_key = None
        for seq, raw in enumerate(segments):
            record = _validate_segment(raw, normalized_header)
            segment_key = (
                record["system_path"], record["member_path"],
                record["cut_index"], record["id"],
            )
            if (
                previous_segment_key is not None
                and segment_key <= previous_segment_key
            ):
                raise ValidationError(
                    "logic segment timing input is not canonically ordered"
                )
            previous_segment_key = segment_key
            batch.append(_segment_tuple(seq, record))
            if len(batch) >= 8192:
                connection.executemany(
                    "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()
            segment_count += 1
        if batch:
            connection.executemany(
                "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
        unsupported_digest = hashlib.sha256()
        unsupported_count = 0
        previous_unsupported_path = None
        for seq, raw in enumerate(unsupported_member_paths):
            if (
                not isinstance(raw, Mapping)
                or set(raw) != _UNSUPPORTED_KEYS
                or any(not isinstance(raw.get(key), str) or not raw[key]
                       for key in _UNSUPPORTED_KEYS)
            ):
                raise ValidationError("unsupported logic timing member is malformed")
            record = dict(raw)
            if (
                previous_unsupported_path is not None
                and record["path"] <= previous_unsupported_path
            ):
                raise ValidationError(
                    "unsupported logic timing members are not ordered"
                )
            previous_unsupported_path = record["path"]
            connection.execute(
                "INSERT INTO unsupported_members VALUES (?,?,?)",
                (seq, record["path"], record["reason"]),
            )
            unsupported_digest.update(_canonical(record).encode())
            unsupported_digest.update(b"\n")
            unsupported_count += 1
        unmeasured_digest = hashlib.sha256()
        unmeasured_count = 0
        previous_unmeasured_id = None
        for seq, raw in enumerate(unmeasured_segments):
            if (
                not isinstance(raw, Mapping)
                or set(raw) != _UNMEASURED_KEYS
                or raw.get("kind") not in {"launch", "transition", "capture"}
                or any(not isinstance(raw.get(key), str) or not raw[key]
                       for key in _UNMEASURED_KEYS - {"kind"})
            ):
                raise ValidationError("unmeasured logic timing segment is malformed")
            record = dict(raw)
            if (
                previous_unmeasured_id is not None
                and record["id"] <= previous_unmeasured_id
            ):
                raise ValidationError(
                    "unmeasured logic timing segments are not ordered"
                )
            previous_unmeasured_id = record["id"]
            connection.execute(
                "INSERT INTO unmeasured_segments VALUES (?,?,?,?,?,?)",
                (seq, record["id"], record["kind"], record["system_path"],
                 record["member_path"], record["reason"]),
            )
            unmeasured_digest.update(_canonical(record).encode())
            unmeasured_digest.update(b"\n")
            unmeasured_count += 1
        overlap = connection.execute(
            "SELECT s.segment_id FROM segments s "
            "JOIN unmeasured_segments u USING(segment_id) LIMIT 1"
        ).fetchone()
        if overlap is not None:
            raise ValidationError(
                "logic timing segment is both measured and unmeasured"
            )
        aggregate = connection.execute(
            "SELECT COUNT(DISTINCT system_path),COUNT(DISTINCT member_path),"
            "SUM(measurement='endpoint-exact'),"
            "SUM(measurement='cut-net-cone-upper-bound') FROM segments"
        ).fetchone()
        counts = {
            "segments": segment_count,
            "system_paths": int(aggregate[0]),
            "member_paths": int(aggregate[1]),
            "unsupported_member_paths": unsupported_count,
            "unmeasured_segments": unmeasured_count,
            "endpoint_exact_segments": int(aggregate[2] or 0),
            "cone_bound_segments": int(aggregate[3] or 0),
        }
        coverage = dict(normalized_header["coverage"])
        for key in (
            "segments", "system_paths", "member_paths", "unsupported_member_paths",
            "endpoint_exact_segments", "cone_bound_segments",
        ):
            coverage[key] = counts[key]
        normalized_header = {**normalized_header, "coverage": coverage}
        segment_digest = hashlib.sha256()
        for row in connection.execute(
            "SELECT * FROM segments "
            "ORDER BY system_path,member_path,cut_index,segment_id"
        ):
            record = _validate_segment(
                _segment_from_row(row), normalized_header
            )
            segment_digest.update(_canonical(record).encode())
            segment_digest.update(b"\n")
        source = _source(normalized_header)
        logical_sha = hashlib.sha256(_canonical({
            "header": normalized_header, "counts": counts, "source": source,
            "segment_sha256": segment_digest.hexdigest(),
            "unsupported_sha256": unsupported_digest.hexdigest(),
            "unmeasured_sha256": unmeasured_digest.hexdigest(),
        }).encode()).hexdigest()
        selector = {
            "schema": LOGIC_SEGMENT_TIMING_STORAGE_SCHEMA,
            "logical_schema": LOGIC_SEGMENT_TIMING_SCHEMA,
            "application_id": LOGIC_SEGMENT_TIMING_APPLICATION_ID,
            "user_version": LOGIC_SEGMENT_TIMING_USER_VERSION,
            "header": normalized_header, "counts": counts, "source": source,
            "logical_sha256": logical_sha,
            "artifact": "", "store_bytes": 0, "store_sha256": "",
        }
        for key, value in (
            ("header", normalized_header), ("counts", counts),
            ("source", source), ("logical_sha256", logical_sha),
        ):
            connection.execute(
                "INSERT INTO metadata VALUES (?,?)", (key, _canonical(value))
            )
        connection.commit()
        connection.close()
        connection = None
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        store_sha = file_sha256(temporary)
        published_store = path.with_name(_store_name(path, store_sha))
        if published_store.is_symlink():
            raise ValidationError("logic segment timing generation is a symlink")
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError("logic segment timing generation seal disagrees")
            temporary.unlink()
        else:
            os.replace(temporary, published_store)
            created_store = True
            _fsync_directory(path.parent)
        selector.update({
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        })
        _validate_selector(selector)
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(selector_temporary, path)
        selector_committed = True
        _fsync_directory(path.parent)
        try:
            _collect_stale(path, published_store)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = LogicSegmentTimingView._from_publication(
            path, selector, published_store, lease
        )
        lease = None
        return view
    except BaseException as error:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if selector_temporary is not None:
            selector_temporary.unlink(missing_ok=True)
        if published_store is not None and created_store and not selector_committed:
            published_store.unlink(missing_ok=True)
        if isinstance(error, sqlite3.IntegrityError):
            raise ValidationError(
                "logic segment timing identity is duplicated"
            ) from error
        raise
    finally:
        if lease is not None:
            lease.close()


def read_logic_segment_timing_storage(
    path: Path,
    *,
    expected_design: Optional[str] = None,
    expected_platform: Optional[str] = None,
    expected_fpga: Optional[str] = None,
    expected_system_routes_sha256: Optional[str] = None,
    expected_tdm_schedule_sha256: Optional[str] = None,
    expected_semantic_contract_sha256: Optional[str] = None,
    expected_selector_bytes: Optional[int] = None,
    expected_selector_sha256: Optional[str] = None,
    allow_legacy: bool = False,
) -> Mapping[str, Any]:
    lease: Optional[_Lease] = _Lease(path, exclusive=False)
    try:
        value = read_json(path)
        if value.get("schema") == LOGIC_SEGMENT_TIMING_STORAGE_SCHEMA:
            view = LogicSegmentTimingView(
                path,
                expected_design=expected_design,
                expected_platform=expected_platform,
                expected_fpga=expected_fpga,
                expected_system_routes_sha256=expected_system_routes_sha256,
                expected_tdm_schedule_sha256=expected_tdm_schedule_sha256,
                expected_semantic_contract_sha256=expected_semantic_contract_sha256,
                expected_selector_bytes=expected_selector_bytes,
                expected_selector_sha256=expected_selector_sha256,
                _lease=lease,
            )
            lease = None
            return view
        if not allow_legacy or value.get("schema") != LOGIC_SEGMENT_TIMING_SCHEMA:
            raise ValidationError(
                "legacy logic segment timing JSON requires explicit compatibility"
            )
        for key, expected in (
            ("design", expected_design), ("platform", expected_platform),
            ("fpga", expected_fpga),
            ("system_routes_sha256", expected_system_routes_sha256),
            ("tdm_schedule_sha256", expected_tdm_schedule_sha256),
            ("semantic_contract_sha256", expected_semantic_contract_sha256),
        ):
            if expected is not None and value.get(key) != expected:
                raise ValidationError(
                    f"legacy logic segment timing {key} source seal disagrees"
                )
        return value
    finally:
        if lease is not None:
            lease.close()


class LogicSegmentTimingCollection:
    """Multi-FPGA owner whose cursors validate one bounded ordered merge."""

    def __init__(self, views: Sequence[LogicSegmentTimingView]):
        if not views:
            raise ValidationError("logic segment timing collection is empty")
        self.views = tuple(views)
        source = self.views[0].selector["source"]
        shared_keys = (
            "design", "platform", "system_routes_sha256",
            "tdm_schedule_sha256", "semantic_contract_sha256",
        )
        for view in self.views[1:]:
            candidate = view.selector["source"]
            if any(candidate[key] != source[key] for key in shared_keys):
                raise ValidationError(
                    "physical logic segment timing source seals disagree"
                )
        self._active_cursors = 0
        self._close_requested = False
        self._closed = False

    def _acquire_cursor(self) -> None:
        if self._closed or self._close_requested:
            raise ValidationError("logic segment timing collection is closed")
        self._active_cursors += 1

    def _release_cursor(self) -> None:
        if self._active_cursors:
            self._active_cursors -= 1
        if self._close_requested and not self._active_cursors:
            self._close_views()

    def _close_views(self) -> None:
        if self._closed:
            return
        self._closed = True
        for view in self.views:
            view.close()

    def close(self) -> None:
        self._close_requested = True
        if not self._active_cursors:
            self._close_views()

    def __enter__(self) -> "LogicSegmentTimingCollection":
        if self._closed:
            raise ValidationError("logic segment timing collection is closed")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _iter_validated_groups(
        self,
    ) -> Iterator[tuple[str, str, tuple[Mapping[str, Any], ...]]]:
        iterators = [view.iter_ordered_segments() for view in self.views]
        merged = heapq.merge(
            *iterators,
            key=lambda row: (
                row["system_path"], row["member_path"],
                row["cut_index"], row["id"],
            ),
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="emuflow-logic-segment-ids-", suffix=".sqlite"
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(temporary)
            connection.execute(
                "CREATE TABLE seen_ids(segment_id TEXT PRIMARY KEY NOT NULL) "
                "WITHOUT ROWID"
            )
            prior_ordered_key = None
            group_key = None
            records: list[Mapping[str, Any]] = []
            for record in merged:
                ordered_key = (
                    str(record["system_path"]),
                    str(record["member_path"]),
                    int(record["cut_index"]),
                )
                if prior_ordered_key is not None and ordered_key <= prior_ordered_key:
                    if ordered_key == prior_ordered_key:
                        raise ValidationError(
                            "duplicate physical logic segment ordered key "
                            f"{ordered_key!r}"
                        )
                    raise ValidationError(
                        "physical logic segments are not globally ordered"
                    )
                prior_ordered_key = ordered_key
                try:
                    connection.execute(
                        "INSERT INTO seen_ids VALUES (?)", (record["id"],)
                    )
                except sqlite3.IntegrityError as error:
                    raise ValidationError(
                        f"duplicate physical logic segment {record['id']!r}"
                    ) from error
                candidate = (ordered_key[0], ordered_key[1])
                if group_key is not None and candidate != group_key:
                    yield group_key[0], group_key[1], tuple(records)
                    records.clear()
                group_key = candidate
                records.append(record)
            if group_key is not None:
                yield group_key[0], group_key[1], tuple(records)
        finally:
            close_merged = getattr(merged, "close", None)
            if callable(close_merged):
                close_merged()
            for iterator in iterators:
                close_iterator = getattr(iterator, "close", None)
                if callable(close_iterator):
                    close_iterator()
            if connection is not None:
                connection.close()
            temporary.unlink(missing_ok=True)

    def ordered_cursor(self) -> "OrderedLogicSegmentCursor":
        return OrderedLogicSegmentCursor(self)


class OrderedLogicSegmentCursor:
    """Consume requested representative/member keys and own its live leases."""

    def __init__(self, collection: LogicSegmentTimingCollection):
        self._collection = collection
        self._closed = False
        collection._acquire_cursor()
        try:
            self._groups = collection._iter_validated_groups()
            self._current = next(self._groups, None)
            self._previous_request: Optional[tuple[str, str]] = None
        except BaseException:
            groups = getattr(self, "_groups", None)
            close_groups = getattr(groups, "close", None)
            if callable(close_groups):
                close_groups()
            collection._release_cursor()
            self._closed = True
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("logic segment timing cursor is closed")

    def take(
        self, system_path: str, member_path: str
    ) -> tuple[Mapping[str, Any], ...]:
        self._ensure_open()
        requested = (system_path, member_path)
        if self._previous_request is not None and requested <= self._previous_request:
            raise ValidationError(
                "logic segment timing requests are not strictly ordered"
            )
        self._previous_request = requested
        if self._current is None:
            raise ValidationError(
                "physical logic segment timing is missing an original member"
            )
        current_key = (self._current[0], self._current[1])
        if current_key < requested:
            raise ValidationError(
                "physical logic segment timing has an extra ordered key"
            )
        if current_key > requested:
            raise ValidationError(
                "physical logic segment timing is missing an ordered key"
            )
        records = self._current[2]
        self._current = next(self._groups, None)
        return records

    def finish(self) -> None:
        self._ensure_open()
        try:
            if self._current is not None:
                raise ValidationError(
                    "physical logic segment timing has unconsumed ordered keys"
                )
        finally:
            self.close()

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        try:
            groups = getattr(self, "_groups", None)
            close_groups = getattr(groups, "close", None)
            if callable(close_groups):
                close_groups()
        finally:
            self._collection._release_cursor()

    def __enter__(self) -> "OrderedLogicSegmentCursor":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

def logic_segment_timing_reference(
    selector_path: Path, *, relative_to: Path
) -> dict[str, Any]:
    """Seal a bounded selector for a physical-summary reference."""

    if selector_path.is_symlink() or not selector_path.is_file():
        raise ValidationError("logic segment timing selector is missing")
    _validate_selector(read_json(selector_path))
    resolved = selector_path.resolve()
    relative = Path(os.path.relpath(resolved, relative_to.resolve()))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("logic segment timing selector escapes its summary")
    return {
        "schema": "emuflow.physical-evidence-reference/v2",
        "logical_schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "path": str(relative),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def publish_logic_segment_timing_selector(
    source: Path, output_directory: Path, relative_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy one immutable generation and atomically publish its selector last."""

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("logic segment timing relative path is unsafe")
    target = output_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        with read_logic_segment_timing_storage(source) as view:
            selector = dict(view.selector)
        return selector, logic_segment_timing_reference(
            target, relative_to=output_directory
        )
    with read_logic_segment_timing_storage(source) as source_view:
        assert isinstance(source_view, LogicSegmentTimingView)
        selector = dict(source_view.selector)
        lease: Optional[_Lease] = _Lease(target, exclusive=True)
        temporary_store: Optional[Path] = None
        temporary_selector: Optional[Path] = None
        try:
            destination_store = target.with_name(
                _store_name(target, selector["store_sha256"])
            )
            selector["artifact"] = destination_store.name
            if destination_store.is_symlink():
                raise ValidationError("logic segment timing generation is a symlink")
            if not destination_store.is_file():
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{destination_store.name}.", suffix=".tmp",
                    dir=target.parent,
                )
                os.close(descriptor)
                temporary_store = Path(name)
                with source_view.store_path.open("rb") as input_stream:
                    with temporary_store.open("wb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                if (
                    temporary_store.stat().st_size != selector["store_bytes"]
                    or file_sha256(temporary_store) != selector["store_sha256"]
                ):
                    raise ValidationError(
                        "logic segment timing copied generation seal disagrees"
                    )
                os.replace(temporary_store, destination_store)
                temporary_store = None
                _fsync_directory(target.parent)
            elif (
                destination_store.stat().st_size != selector["store_bytes"]
                or file_sha256(destination_store) != selector["store_sha256"]
            ):
                raise ValidationError(
                    "logic segment timing destination generation seal disagrees"
                )
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            temporary_selector = Path(name)
            write_json(temporary_selector, selector, compact=True)
            with temporary_selector.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_selector, target)
            temporary_selector = None
            _fsync_directory(target.parent)
            _collect_stale(target, destination_store)
        finally:
            if temporary_store is not None:
                temporary_store.unlink(missing_ok=True)
            if temporary_selector is not None:
                temporary_selector.unlink(missing_ok=True)
            if lease is not None:
                lease.close()
    return selector, logic_segment_timing_reference(
        target, relative_to=output_directory
    )
