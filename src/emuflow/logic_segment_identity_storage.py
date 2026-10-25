"""Typed owner for logic-segment query identities.

The public ``*.json`` path is a small atomic selector.  The graph-sized
segment population lives in one immutable, content-addressed SQLite
generation.  Readers keep a shared owner lease while cursors are live;
writers publish the selector last while holding the exclusive lease.
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Iterable, Iterator, Mapping, Sequence
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Optional
import weakref

from .errors import ValidationError
from .store_publication import (
    StoreSource,
    bind_store_source,
    publish_sqlite_store,
    require_store_sources_unchanged,
    validate_store_destination,
)
from .io import file_sha256, read_json, write_json


LOGIC_SEGMENT_IDENTITY_SCHEMA = "emuflow.logic-segment-identity/v1"
LOGIC_SEGMENT_IDENTITY_STORAGE_SCHEMA = (
    "emuflow.logic-segment-identity-storage/v1"
)
LOGIC_SEGMENT_IDENTITY_APPLICATION_ID = 0x454D5549  # ASCII "EMUI"
LOGIC_SEGMENT_IDENTITY_USER_VERSION = 1

_HEADER_REQUIRED = {
    "schema", "status", "design", "platform", "fpga", "provider",
    "coverage", "system_routes_sha256", "tdm_schedule_sha256",
}
_HEADER_OPTIONAL = {"semantic_contract_sha256"}
_SOURCE_KEYS = {
    "design", "platform", "fpga", "system_routes_sha256",
    "tdm_schedule_sha256", "semantic_contract_sha256",
}
_COUNT_KEYS = {
    "segments", "system_paths", "member_paths", "unsupported_member_paths",
}
_SEGMENT_REQUIRED = {
    "id", "kind", "system_path", "member_path", "cut_index", "fpga",
    "replace_tx_endpoint", "start_pin", "end_pin",
}
_SEGMENT_OPTIONAL = {
    "static_exact_segment_id", "start_object_kind", "end_object_kind",
    "cone_anchor_object_kind", "cone_anchor_pin",
}
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version", "header",
    "counts", "source", "logical_sha256", "artifact", "store_bytes",
    "store_sha256",
}

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
  static_exact_segment_id TEXT,
  start_object_kind TEXT CHECK(start_object_kind IN ('pin','port')),
  end_object_kind TEXT CHECK(end_object_kind IN ('pin','port')),
  cone_anchor_object_kind TEXT CHECK(cone_anchor_object_kind IN ('pin','port')),
  cone_anchor_pin TEXT
);
CREATE UNIQUE INDEX segments_id_unique ON segments(segment_id);
CREATE UNIQUE INDEX segments_sequence_unique ON segments(seq);
CREATE INDEX segments_canonical_cursor
  ON segments(system_path,member_path,cut_index,segment_id);
CREATE TABLE unsupported_members(
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  path TEXT NOT NULL CHECK(length(path) > 0),
  reason TEXT NOT NULL CHECK(length(reason) > 0)
);
CREATE UNIQUE INDEX unsupported_members_path_unique
  ON unsupported_members(path);
"""


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


@lru_cache(maxsize=1)
def _expected_sqlite_table_envelope() -> dict[str, tuple[Any, ...]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_DDL)
        result = {}
        for table in ("metadata", "segments", "unsupported_members"):
            columns = tuple(tuple(row) for row in connection.execute(
                f"PRAGMA table_xinfo({table})"
            ))
            indexes = []
            for row in connection.execute(f"PRAGMA index_list({table})"):
                name = str(row[1])
                if name.startswith("sqlite_"):
                    continue
                index_columns = tuple(
                    (int(item[0]), int(item[1]), str(item[2]), int(item[5]))
                    for item in connection.execute(
                        f"PRAGMA index_xinfo({json.dumps(name)})"
                    )
                )
                indexes.append(
                    (name, int(row[2]), str(row[3]), int(row[4]), index_columns)
                )
            result[table] = (columns, tuple(sorted(indexes)))
        return result
    finally:
        connection.close()


def _exact_sqlite_envelope(connection: sqlite3.Connection) -> None:
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if (
        application_id != LOGIC_SEGMENT_IDENTITY_APPLICATION_ID
        or user_version != LOGIC_SEGMENT_IDENTITY_USER_VERSION
    ):
        raise ValidationError("unsupported logic segment identity store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects():
        raise ValidationError("logic segment identity store objects are invalid")
    expected_tables = _expected_sqlite_table_envelope()
    for table, expected in expected_tables.items():
        columns = tuple(tuple(row) for row in connection.execute(
            f"PRAGMA table_xinfo({table})"
        ))
        indexes = []
        for row in connection.execute(f"PRAGMA index_list({table})"):
            name = str(row[1])
            if name.startswith("sqlite_"):
                continue
            index_columns = tuple(
                (int(item[0]), int(item[1]), str(item[2]), int(item[5]))
                for item in connection.execute(
                    f"PRAGMA index_xinfo({json.dumps(name)})"
                )
            )
            indexes.append(
                (name, int(row[2]), str(row[3]), int(row[4]), index_columns)
            )
        if (columns, tuple(sorted(indexes))) != expected:
            raise ValidationError(
                "logic segment identity store table envelope is invalid"
            )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "logic segment identity value is not canonical"
        ) from error


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
    return f"{_owner_prefix(path)}{digest}.sqlite"


class _Lease:
    def __init__(self, selector_path: Path, *, exclusive: bool):
        parent = selector_path.parent.resolve()
        lock_path = parent / f".{selector_path.name}.lock"
        if lock_path.is_symlink():
            raise ValidationError("logic segment identity lock is a symbolic link")
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
            raise ValidationError("logic segment identity lease is closed")
        fcntl.flock(self.descriptor, fcntl.LOCK_SH)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _validate_header(
    raw: Mapping[str, Any], *, require_exact_coverage: bool = True
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) - _HEADER_REQUIRED - _HEADER_OPTIONAL:
        raise ValidationError("logic segment identity header is malformed")
    if not _HEADER_REQUIRED <= set(raw):
        raise ValidationError("logic segment identity header is incomplete")
    header = dict(raw)
    if (
        header.get("schema") != LOGIC_SEGMENT_IDENTITY_SCHEMA
        or header.get("status") != "pass"
        or any(
            not isinstance(header.get(key), str) or not header[key]
            for key in ("design", "platform", "fpga", "provider")
        )
        or not isinstance(header.get("coverage"), Mapping)
    ):
        raise ValidationError("logic segment identity header is malformed")
    coverage = header["coverage"]
    if require_exact_coverage and (
        not isinstance(coverage, dict)
        or set(coverage) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(value) for value in coverage.values())
    ):
        raise ValidationError("logic segment identity coverage is malformed")
    for key in ("system_routes_sha256", "tdm_schedule_sha256"):
        if not _valid_digest(header.get(key)):
            raise ValidationError(f"logic segment identity {key} is invalid")
    semantic = header.get("semantic_contract_sha256")
    if semantic is not None and not _valid_digest(semantic):
        raise ValidationError(
            "logic segment identity semantic contract seal is invalid"
        )
    return header


def _source(header: Mapping[str, Any]) -> dict[str, Any]:
    return {key: header.get(key) for key in sorted(_SOURCE_KEYS)}


def _validate_segment(raw: Mapping[str, Any], header: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(raw, Mapping)
        or set(raw) - _SEGMENT_REQUIRED - _SEGMENT_OPTIONAL
        or not _SEGMENT_REQUIRED <= set(raw)
    ):
        raise ValidationError("logic segment identity record is malformed")
    record = dict(raw)
    if (
        any(
            not isinstance(record.get(key), str) or not record[key]
            for key in ("id", "system_path", "member_path", "fpga", "start_pin", "end_pin")
        )
        or record["kind"] not in {"launch", "transition", "capture"}
        or record["fpga"] != header["fpga"]
        or not _strict_nonnegative_int(record.get("cut_index"))
    ):
        raise ValidationError("logic segment identity record is malformed")
    replacement = record.get("replace_tx_endpoint")
    if (record["kind"] == "capture") != (replacement is None) or (
        replacement is not None
        and (not isinstance(replacement, str) or not replacement)
    ):
        raise ValidationError("logic segment identity replacement is invalid")
    exact_id = record.get("static_exact_segment_id")
    semantic = header.get("semantic_contract_sha256")
    if (semantic is None and exact_id is not None) or (
        semantic is not None and (not isinstance(exact_id, str) or not exact_id)
    ):
        raise ValidationError("logic segment identity exact identity is invalid")
    kinds = (record.get("start_object_kind"), record.get("end_object_kind"))
    if (kinds[0] is None) != (kinds[1] is None) or any(
        value is not None and value not in {"pin", "port"} for value in kinds
    ):
        raise ValidationError("logic segment identity object kinds are invalid")
    anchor_kind = record.get("cone_anchor_object_kind")
    anchor_pin = record.get("cone_anchor_pin")
    if (anchor_kind is None) != (anchor_pin is None) or (
        anchor_kind is not None
        and (
            anchor_kind not in {"pin", "port"}
            or record["kind"] == "capture"
            or not isinstance(anchor_pin, str)
            or not anchor_pin
        )
    ):
        raise ValidationError("logic segment identity cone anchor is invalid")
    return record


def _segment_tuple(seq: int, record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        seq, record["id"], record["kind"], record["system_path"],
        record["member_path"], record["cut_index"], record["fpga"],
        record.get("replace_tx_endpoint"), record["start_pin"],
        record["end_pin"], record.get("static_exact_segment_id"),
        record.get("start_object_kind"), record.get("end_object_kind"),
        record.get("cone_anchor_object_kind"), record.get("cone_anchor_pin"),
    )


def segment_from_row(row: sqlite3.Row) -> dict[str, Any]:
    record = {
        "id": str(row["segment_id"]), "kind": str(row["kind"]),
        "system_path": str(row["system_path"]),
        "member_path": str(row["member_path"]),
        "cut_index": int(row["cut_index"]), "fpga": str(row["fpga"]),
        "replace_tx_endpoint": row["replace_tx_endpoint"],
        "start_pin": str(row["start_pin"]), "end_pin": str(row["end_pin"]),
    }
    for key in (
        "static_exact_segment_id", "start_object_kind", "end_object_kind",
        "cone_anchor_object_kind", "cone_anchor_pin",
    ):
        if row[key] is not None:
            record[key] = str(row[key])
    return record


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class _SegmentSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "LogicSegmentIdentityView", count: int):
        self._owner = weakref.ref(owner)
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        owner = self._owner()
        if owner is None:
            raise ValidationError("logic segment identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            for row in connection.execute("SELECT * FROM segments ORDER BY seq"):
                yield segment_from_row(row)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        owner = self._owner()
        if owner is None:
            raise ValidationError("logic segment identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            row = connection.execute(
                "SELECT * FROM segments WHERE seq=?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("logic segment identity sequence is incomplete")
            return segment_from_row(row)


class _UnsupportedSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "LogicSegmentIdentityView", count: int):
        self._owner = weakref.ref(owner)
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        owner = self._owner()
        if owner is None:
            raise ValidationError("logic segment identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            for row in connection.execute(
                "SELECT path,reason FROM unsupported_members ORDER BY seq"
            ):
                yield {"path": str(row[0]), "reason": str(row[1])}

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        owner = self._owner()
        if owner is None:
            raise ValidationError("logic segment identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            row = connection.execute(
                "SELECT path,reason FROM unsupported_members WHERE seq=?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("unsupported member sequence is incomplete")
            return {"path": str(row[0]), "reason": str(row[1])}


def _validate_selector(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _SELECTOR_KEYS:
        raise ValidationError("logic segment identity selector is malformed")
    if (
        raw.get("schema") != LOGIC_SEGMENT_IDENTITY_STORAGE_SCHEMA
        or raw.get("logical_schema") != LOGIC_SEGMENT_IDENTITY_SCHEMA
        or isinstance(raw.get("application_id"), bool)
        or not isinstance(raw.get("application_id"), int)
        or raw.get("application_id") != LOGIC_SEGMENT_IDENTITY_APPLICATION_ID
        or isinstance(raw.get("user_version"), bool)
        or not isinstance(raw.get("user_version"), int)
        or raw.get("user_version") != LOGIC_SEGMENT_IDENTITY_USER_VERSION
    ):
        raise ValidationError("unsupported logic segment identity selector")
    header = _validate_header(raw.get("header", {}))
    counts = raw.get("counts")
    source = raw.get("source")
    if (
        not isinstance(counts, dict)
        or set(counts) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(value) for value in counts.values())
        or header["coverage"] != counts
        or source != _source(header)
        or not _valid_digest(raw.get("logical_sha256"))
    ):
        raise ValidationError("logic segment identity selector metadata is malformed")
    artifact = raw.get("artifact")
    if (
        not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _valid_digest(raw.get("store_sha256"))
        or not _strict_nonnegative_int(raw.get("store_bytes"))
    ):
        raise ValidationError("logic segment identity artifact is malformed")
    return raw


def _resolve_store(
    selector_path: Path, selector: Mapping[str, Any]
) -> tuple[Path, StoreSource]:
    if selector_path.is_symlink():
        raise ValidationError("logic segment identity selector is a symbolic link")
    candidate = selector_path.parent / str(selector["artifact"])
    if candidate.name != _store_name(selector_path, str(selector["store_sha256"])):
        raise ValidationError("logic segment identity artifact owner disagrees")
    if candidate.is_symlink():
        raise ValidationError("logic segment identity store is a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
        store_source = bind_store_source(resolved, "logic-segment-identity-store-v1")
    except OSError as error:
        raise ValidationError("logic segment identity store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("logic segment identity store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("logic segment identity store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return resolved, store_source


class LogicSegmentIdentityView(Mapping[str, Any]):
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
        _lease: Optional[_Lease] = None,
    ):
        self._lease = _lease or _Lease(selector_path, exclusive=False)
        self._closed = False
        try:
            if selector_path.is_symlink() or not selector_path.is_file():
                raise ValidationError("logic segment identity selector is missing")
            self.selector_path = selector_path.resolve()
            selector_source = bind_store_source(
                self.selector_path, "logic-segment-identity-selector-v1"
            )
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
                        f"logic segment identity {key} source seal disagrees"
                    )
            self.store_path, store_source = _resolve_store(selector_path, self.selector)
            self._source_bindings = (selector_source, store_source)
            self._validate_store()
            require_store_sources_unchanged(self._source_bindings)
            self._segments = _SegmentSequence(self, self.selector["counts"]["segments"])
            self._unsupported = _UnsupportedSequence(
                self, self.selector["counts"]["unsupported_member_paths"]
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls, selector_path: Path, selector: dict[str, Any], store_path: Path,
        lease: _Lease,
    ) -> "LogicSegmentIdentityView":
        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        value._source_bindings = (
            bind_store_source(
                value.selector_path, "logic-segment-identity-selector-v1"
            ),
            bind_store_source(value.store_path, "logic-segment-identity-store-v1"),
        )
        value._segments = _SegmentSequence(value, selector["counts"]["segments"])
        value._unsupported = _UnsupportedSequence(
            value, selector["counts"]["unsupported_member_paths"]
        )
        return value

    def bound_store_sources(self):
        self._ensure_open()
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("logic segment identity view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "LogicSegmentIdentityView":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_store(self) -> None:
        segment_digest = hashlib.sha256()
        unsupported_digest = hashlib.sha256()
        try:
            with closing(_connect(self.store_path)) as connection, connection:
                _exact_sqlite_envelope(connection)
                metadata_rows = list(connection.execute(
                    "SELECT key,value FROM metadata ORDER BY key"
                ))
                metadata = {
                    str(row[0]): json.loads(str(row[1])) for row in metadata_rows
                }
                if len(metadata) != len(metadata_rows) or metadata != {
                    key: self.selector[key]
                    for key in ("header", "counts", "source", "logical_sha256")
                }:
                    raise ValidationError("logic segment identity metadata disagrees")
                count = 0
                previous_id = None
                for row in connection.execute("SELECT * FROM segments ORDER BY seq"):
                    if int(row["seq"]) != count:
                        raise ValidationError("logic segment identity sequence is invalid")
                    record = _validate_segment(
                        segment_from_row(row), self.selector["header"]
                    )
                    if previous_id is not None and record["id"] <= previous_id:
                        raise ValidationError("logic segment identity order is invalid")
                    previous_id = record["id"]
                    segment_digest.update(_canonical(record).encode())
                    segment_digest.update(b"\n")
                    count += 1
                unsupported_count = 0
                previous_path = None
                for row in connection.execute(
                    "SELECT seq,path,reason FROM unsupported_members ORDER BY seq"
                ):
                    if int(row["seq"]) != unsupported_count:
                        raise ValidationError("unsupported member sequence is invalid")
                    record = {"path": str(row["path"]), "reason": str(row["reason"])}
                    if previous_path is not None and record["path"] <= previous_path:
                        raise ValidationError("unsupported member order is invalid")
                    previous_path = record["path"]
                    unsupported_digest.update(_canonical(record).encode())
                    unsupported_digest.update(b"\n")
                    unsupported_count += 1
                aggregate = connection.execute(
                    "SELECT COUNT(DISTINCT system_path),COUNT(DISTINCT member_path) FROM segments"
                ).fetchone()
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("logic segment identity store is corrupt") from error
        counts = {
            "segments": count,
            "system_paths": int(aggregate[0]),
            "member_paths": int(aggregate[1]),
            "unsupported_member_paths": unsupported_count,
        }
        if counts != self.selector["counts"]:
            raise ValidationError("logic segment identity counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": self.selector["header"], "counts": counts,
            "source": self.selector["source"],
            "segment_sha256": segment_digest.hexdigest(),
            "unsupported_sha256": unsupported_digest.hexdigest(),
        }).encode()).hexdigest()
        if logical != self.selector["logical_sha256"]:
            raise ValidationError("logic segment identity logical seal disagrees")

    def __getitem__(self, key: str) -> Any:
        self._ensure_open()
        if key == "segments":
            return self._segments
        if key == "unsupported_member_paths":
            return self._unsupported
        return self.selector["header"][key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        yield from self.selector["header"]
        yield "unsupported_member_paths"
        yield "segments"

    def __len__(self) -> int:
        return len(self.selector["header"]) + 2

    def canonical_segments(self) -> Iterator[Mapping[str, Any]]:
        """Yield the timing-store join order without materializing an index."""
        self._ensure_open()
        with closing(_connect(self.store_path)) as connection, connection:
            for row in connection.execute(
                "SELECT * FROM segments "
                "ORDER BY system_path,member_path,cut_index,segment_id"
            ):
                yield segment_from_row(row)

    def validate_identity(self) -> dict[str, Any]:
        self._ensure_open()
        counts = self.selector["counts"]
        return {
            "status": "pass", "segments": counts["segments"],
            "member_paths": counts["member_paths"],
            "system_paths": counts["system_paths"],
        }


class LogicSegmentIdentitySpool(Sequence[Mapping[str, Any]]):
    """Disk-backed producer buffer for identities generated out of order."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".logic-segment-identity.", suffix=".spool.sqlite",
            dir=directory,
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute(
            "CREATE TABLE segments("
            "seq INTEGER PRIMARY KEY,segment_id TEXT UNIQUE NOT NULL,"
            "kind TEXT NOT NULL,system_path TEXT NOT NULL,member_path TEXT NOT NULL,"
            "cut_index INTEGER NOT NULL,fpga TEXT NOT NULL,"
            "replace_tx_endpoint TEXT,start_pin TEXT NOT NULL,end_pin TEXT NOT NULL,"
            "static_exact_segment_id TEXT,start_object_kind TEXT,"
            "end_object_kind TEXT,cone_anchor_object_kind TEXT,cone_anchor_pin TEXT)"
        )
        self.connection.execute(
            "CREATE INDEX canonical_cursor ON segments("
            "system_path,member_path,cut_index,segment_id)"
        )
        self.connection.execute(
            "CREATE INDEX exact_endpoint_cursor ON segments("
            "static_exact_segment_id,start_pin,end_pin)"
        )
        self.connection.execute(
            "CREATE TABLE exact_endpoints("
            "static_exact_segment_id TEXT NOT NULL,start_pin TEXT NOT NULL,"
            "end_pin TEXT NOT NULL,PRIMARY KEY("
            "static_exact_segment_id,start_pin,end_pin)) WITHOUT ROWID"
        )
        self.connection.execute(
            "CREATE TABLE schedule_entries("
            "seq INTEGER PRIMARY KEY,entry_id TEXT UNIQUE NOT NULL,"
            "net TEXT NOT NULL,source_fpga TEXT NOT NULL,sink_fpga TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE INDEX schedule_from ON schedule_entries("
            "net,source_fpga,entry_id)"
        )
        self.connection.execute(
            "CREATE INDEX schedule_to ON schedule_entries("
            "net,sink_fpga,entry_id)"
        )
        self.connection.execute(
            "CREATE TABLE exact_captures("
            "capture_id TEXT PRIMARY KEY,cut_net TEXT NOT NULL,fpga TEXT NOT NULL,"
            "kind TEXT NOT NULL,endpoint TEXT NOT NULL,port TEXT,bit INTEGER) WITHOUT ROWID"
        )
        self.connection.execute(
            "CREATE TABLE exact_contract_segments("
            "segment_id TEXT PRIMARY KEY,kind TEXT NOT NULL,fpga TEXT NOT NULL,"
            "source_cut_net TEXT,sink_cut_net TEXT,capture_requirement TEXT,"
            "source_semantics TEXT) WITHOUT ROWID"
        )
        self.connection.execute(
            "CREATE INDEX exact_contract_key ON exact_contract_segments("
            "kind,source_cut_net,sink_cut_net,fpga,capture_requirement)"
        )
        self._count = 0
        self._id_order = False

    def append(self, record: Mapping[str, Any]) -> None:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        try:
            self.connection.execute(
                "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _segment_tuple(self._count, record),
            )
        except sqlite3.IntegrityError as error:
            raise ValidationError("logic segment identity is duplicated") from error
        exact_id = record.get("static_exact_segment_id")
        if exact_id is not None:
            self.connection.execute(
                "INSERT OR IGNORE INTO exact_endpoints VALUES (?,?,?)",
                (exact_id, record["start_pin"], record["end_pin"]),
            )
        self._count += 1

    def append_if_new_exact(self, record: Mapping[str, Any]) -> bool:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        exact_id = record.get("static_exact_segment_id")
        if not isinstance(exact_id, str) or not exact_id:
            raise ValidationError("exact logic segment identity is absent")
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO exact_endpoints VALUES (?,?,?)",
            (exact_id, record["start_pin"], record["end_pin"]),
        )
        if cursor.rowcount == 0:
            return False
        self.append(record)
        return True

    def index_schedule_entries(
        self, entries: Iterable[Mapping[str, Any]]
    ) -> int:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        batch = []
        count = 0
        try:
            for seq, entry in enumerate(entries):
                batch.append((
                    seq, entry["id"], entry["net"], entry["from"], entry["to"],
                ))
                if len(batch) >= 8192:
                    self.connection.executemany(
                        "INSERT INTO schedule_entries VALUES (?,?,?,?,?)", batch
                    )
                    count += len(batch)
                    batch.clear()
            if batch:
                self.connection.executemany(
                    "INSERT INTO schedule_entries VALUES (?,?,?,?,?)", batch
                )
                count += len(batch)
        except (KeyError, sqlite3.IntegrityError) as error:
            raise ValidationError("logic segment schedule index is invalid") from error
        return count

    def schedule_from(
        self, net: str, fpga: str
    ) -> Iterator[Mapping[str, Any]]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        for row in self.connection.execute(
            "SELECT entry_id,net,source_fpga,sink_fpga FROM schedule_entries "
            "WHERE net=? AND source_fpga=? ORDER BY entry_id", (net, fpga)
        ):
            yield {
                "id": str(row[0]), "net": str(row[1]),
                "from": str(row[2]), "to": str(row[3]),
            }

    def schedule_to(
        self, net: str, fpga: str
    ) -> list[Mapping[str, Any]]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        rows = self.connection.execute(
            "SELECT entry_id,net,source_fpga,sink_fpga FROM schedule_entries "
            "WHERE net=? AND sink_fpga=? ORDER BY entry_id LIMIT 2", (net, fpga)
        ).fetchall()
        return [
            {
                "id": str(row[0]), "net": str(row[1]),
                "from": str(row[2]), "to": str(row[3]),
            }
            for row in rows
        ]

    def index_exact_contract(
        self,
        captures: Iterable[Mapping[str, Any]],
        segments: Iterable[Mapping[str, Any]],
    ) -> tuple[int, int]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        capture_count = 0
        segment_count = 0
        try:
            batch = []
            for capture in captures:
                bit = capture.get("bit")
                batch.append((
                    capture["id"], capture["cut_net"], capture["fpga"],
                    capture["kind"], capture["endpoint"], capture.get("port"), bit,
                ))
                if len(batch) >= 8192:
                    self.connection.executemany(
                        "INSERT INTO exact_captures VALUES (?,?,?,?,?,?,?)", batch
                    )
                    capture_count += len(batch)
                    batch.clear()
            if batch:
                self.connection.executemany(
                    "INSERT INTO exact_captures VALUES (?,?,?,?,?,?,?)", batch
                )
                capture_count += len(batch)
            batch = []
            for segment in segments:
                batch.append((
                    segment["id"], segment["kind"], segment["fpga"],
                    segment.get("source_cut_net"), segment.get("sink_cut_net"),
                    segment.get("capture_requirement"),
                    segment.get("source_semantics"),
                ))
                if len(batch) >= 8192:
                    self.connection.executemany(
                        "INSERT INTO exact_contract_segments VALUES (?,?,?,?,?,?,?)",
                        batch,
                    )
                    segment_count += len(batch)
                    batch.clear()
            if batch:
                self.connection.executemany(
                    "INSERT INTO exact_contract_segments VALUES (?,?,?,?,?,?,?)",
                    batch,
                )
                segment_count += len(batch)
        except (KeyError, sqlite3.IntegrityError, TypeError, ValueError) as error:
            raise ValidationError("logic segment exact contract index is invalid") from error
        dangling = self.connection.execute(
            "SELECT 1 FROM exact_contract_segments s LEFT JOIN exact_captures c "
            "ON c.capture_id=s.capture_requirement WHERE s.kind='rx_to_capture' "
            "AND c.capture_id IS NULL LIMIT 1"
        ).fetchone()
        if dangling is not None:
            raise ValidationError("logic segment exact capture is absent")
        return segment_count, capture_count

    def exact_contract_segments(self) -> Iterator[Mapping[str, Any]]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        for row in self.connection.execute(
            "SELECT * FROM exact_contract_segments ORDER BY segment_id"
        ):
            record = {
                "id": str(row["segment_id"]), "kind": str(row["kind"]),
                "fpga": str(row["fpga"]),
            }
            for field in (
                "source_cut_net", "sink_cut_net", "capture_requirement",
                "source_semantics",
            ):
                if row[field] is not None:
                    record[field] = str(row[field])
            yield record

    def exact_capture(self, capture_id: str) -> Mapping[str, Any]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        row = self.connection.execute(
            "SELECT * FROM exact_captures WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("logic segment exact capture is absent")
        record = {
            "id": str(row["capture_id"]), "cut_net": str(row["cut_net"]),
            "fpga": str(row["fpga"]), "kind": str(row["kind"]),
            "endpoint": str(row["endpoint"]),
        }
        if row["port"] is not None:
            record["port"] = str(row["port"])
        if row["bit"] is not None:
            record["bit"] = int(row["bit"])
        return record

    def exact_segment_id(self, key: tuple[Any, ...]) -> Optional[str]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        role, source, sink, fpga, capture_identity = key
        if role == "launch":
            query = (
                "SELECT segment_id FROM exact_contract_segments WHERE "
                "kind='launch_to_tx' AND source_cut_net IS NULL AND sink_cut_net=? "
                "AND fpga=? ORDER BY segment_id LIMIT 2"
            )
            arguments = (sink, fpga)
        elif role == "transition":
            query = (
                "SELECT segment_id FROM exact_contract_segments WHERE "
                "kind='rx_to_tx' AND source_cut_net=? AND sink_cut_net=? AND fpga=? "
                "ORDER BY segment_id LIMIT 2"
            )
            arguments = (source, sink, fpga)
        else:
            endpoint, port, bit = capture_identity
            query = (
                "SELECT s.segment_id FROM exact_contract_segments s "
                "JOIN exact_captures c ON c.capture_id=s.capture_requirement "
                "WHERE s.kind='rx_to_capture' AND s.source_cut_net=? "
                "AND s.sink_cut_net IS NULL AND s.fpga=? AND c.endpoint=? "
                "AND c.port IS ? AND c.bit IS ? ORDER BY s.segment_id LIMIT 2"
            )
            arguments = (source, fpga, endpoint, port, bit)
        rows = self.connection.execute(query, arguments).fetchall()
        if len(rows) > 1:
            raise ValidationError("logic segment exact contract mapping is ambiguous")
        return None if not rows else str(rows[0][0])

    def coverage_counts(self) -> dict[str, int]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        row = self.connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT system_path),"
            "COUNT(DISTINCT member_path) FROM segments"
        ).fetchone()
        return {
            "segments": int(row[0]), "system_paths": int(row[1]),
            "member_paths": int(row[2]),
        }

    def order_by_segment_id(self) -> None:
        """Select the externally stable identity/query publication order."""
        self._id_order = True

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        self.connection.commit()
        order = "segment_id" if self._id_order else "seq"
        cursor = self.connection.execute(
            f"SELECT * FROM segments ORDER BY {order}"
        )
        for row in cursor:
            yield segment_from_row(row)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        order = "segment_id" if self._id_order else "seq"
        row = self.connection.execute(
            f"SELECT * FROM segments ORDER BY {order} LIMIT 1 OFFSET ?",
            (index,),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return segment_from_row(row)

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is None:
            return
        self.connection = None
        connection.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "LogicSegmentIdentitySpool":
        if self.connection is None:
            raise ValidationError("logic segment identity spool is closed")
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def _collect_stale(path: Path, keep: Path) -> None:
    changed = False
    for candidate in path.parent.glob(f"{_owner_prefix(path)}*.sqlite"):
        if candidate.is_symlink() or candidate.resolve() == keep.resolve():
            continue
        try:
            candidate.unlink()
            changed = True
        except OSError:
            continue
    if changed:
        _fsync_directory(path.parent)


def write_logic_segment_identity_storage(
    path: Path,
    *,
    header: Mapping[str, Any],
    segments: Iterable[Mapping[str, Any]],
    unsupported_member_paths: Iterable[Mapping[str, Any]] = (),
    sidecar_publication: Optional[tuple[Path, Path]] = None,
    source_bindings=(),
) -> LogicSegmentIdentityView:
    normalized_header = _validate_header(
        header, require_exact_coverage=False
    )
    source_bindings = validate_store_destination(path, source_bindings)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease: Optional[_Lease] = _Lease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    connection: Optional[sqlite3.Connection] = None
    published_store: Optional[Path] = None
    created_store = False
    selector_committed = False
    selector_published = False
    selector_backup: Optional[Path] = None
    sidecar_published = False
    sidecar_backup: Optional[Path] = None
    sidecar_temporary = (
        sidecar_publication[0] if sidecar_publication is not None else None
    )
    sidecar_target = (
        sidecar_publication[1] if sidecar_publication is not None else None
    )
    try:
        if path.is_symlink():
            raise ValidationError(
                "logic segment identity selector is a symbolic link"
            )
        if path.exists() and not path.is_file():
            raise ValidationError("logic segment identity selector is invalid")
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
            f"PRAGMA application_id={LOGIC_SEGMENT_IDENTITY_APPLICATION_ID}"
        )
        connection.execute(
            f"PRAGMA user_version={LOGIC_SEGMENT_IDENTITY_USER_VERSION}"
        )
        connection.executescript(_DDL)
        segment_digest = hashlib.sha256()
        segment_count = 0
        previous_id = None
        batch = []
        for seq, raw in enumerate(segments):
            record = _validate_segment(raw, normalized_header)
            if previous_id is not None and record["id"] <= previous_id:
                raise ValidationError(
                    "logic segment identity input is not canonically ordered"
                )
            previous_id = record["id"]
            batch.append(_segment_tuple(seq, record))
            segment_digest.update(_canonical(record).encode())
            segment_digest.update(b"\n")
            if len(batch) >= 8192:
                connection.executemany(
                    "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()
            segment_count += 1
        if batch:
            connection.executemany(
                "INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                batch,
            )
        unsupported_digest = hashlib.sha256()
        unsupported_count = 0
        previous_path = None
        for seq, raw in enumerate(unsupported_member_paths):
            if (
                not isinstance(raw, Mapping)
                or set(raw) != {"path", "reason"}
                or any(not isinstance(raw.get(key), str) or not raw[key]
                       for key in ("path", "reason"))
            ):
                raise ValidationError("unsupported logic timing member is malformed")
            record = dict(raw)
            if previous_path is not None and record["path"] <= previous_path:
                raise ValidationError("unsupported logic timing members are not ordered")
            previous_path = record["path"]
            connection.execute(
                "INSERT INTO unsupported_members VALUES (?,?,?)",
                (seq, record["path"], record["reason"]),
            )
            unsupported_digest.update(_canonical(record).encode())
            unsupported_digest.update(b"\n")
            unsupported_count += 1
        aggregate = connection.execute(
            "SELECT COUNT(DISTINCT system_path),COUNT(DISTINCT member_path) FROM segments"
        ).fetchone()
        counts = {
            "segments": segment_count,
            "system_paths": int(aggregate[0]),
            "member_paths": int(aggregate[1]),
            "unsupported_member_paths": unsupported_count,
        }
        coverage = dict(normalized_header["coverage"])
        coverage.update(counts)
        normalized_header = {**normalized_header, "coverage": coverage}
        normalized_header = _validate_header(normalized_header)
        source = _source(normalized_header)
        logical_sha = hashlib.sha256(_canonical({
            "header": normalized_header, "counts": counts, "source": source,
            "segment_sha256": segment_digest.hexdigest(),
            "unsupported_sha256": unsupported_digest.hexdigest(),
        }).encode()).hexdigest()
        selector = {
            "schema": LOGIC_SEGMENT_IDENTITY_STORAGE_SCHEMA,
            "logical_schema": LOGIC_SEGMENT_IDENTITY_SCHEMA,
            "application_id": LOGIC_SEGMENT_IDENTITY_APPLICATION_ID,
            "user_version": LOGIC_SEGMENT_IDENTITY_USER_VERSION,
            "header": normalized_header, "counts": counts, "source": source,
            "logical_sha256": logical_sha, "artifact": "", "store_bytes": 0,
            "store_sha256": "",
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
        store_sha = file_sha256(temporary)
        published_store = path.with_name(_store_name(path, store_sha))
        if published_store.is_symlink():
            raise ValidationError("logic segment identity generation is a symlink")
        require_store_sources_unchanged(source_bindings)
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError("logic segment identity generation seal disagrees")
            temporary.unlink()
        else:
            publish_sqlite_store(
                temporary,
                published_store,
                "logic-segment-identity-store-v1",
                sources=source_bindings,
            )
            created_store = True
        selector.update({
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        })
        _validate_selector(selector)
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        if sidecar_temporary is not None and sidecar_target is not None:
            if (
                sidecar_temporary.is_symlink()
                or not sidecar_temporary.is_file()
                or sidecar_target.is_symlink()
                or sidecar_temporary.parent.resolve()
                != sidecar_target.parent.resolve()
                or (sidecar_target.exists() and not sidecar_target.is_file())
            ):
                raise ValidationError(
                    "logic segment identity sidecar publication is invalid"
                )
            if sidecar_target.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{sidecar_target.name}.", suffix=".rollback.tmp",
                    dir=sidecar_target.parent,
                )
                os.close(descriptor)
                sidecar_backup = Path(backup_name)
                sidecar_backup.unlink()
                os.replace(sidecar_target, sidecar_backup)
            os.replace(sidecar_temporary, sidecar_target)
            sidecar_published = True
            _fsync_directory(sidecar_target.parent)
        if path.exists():
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".rollback.tmp", dir=path.parent
            )
            os.close(descriptor)
            selector_backup = Path(backup_name)
            shutil.copyfile(path, selector_backup)
            with selector_backup.open("rb") as stream:
                os.fsync(stream.fileno())
        require_store_sources_unchanged(source_bindings)
        os.replace(selector_temporary, path)
        selector_published = True
        _fsync_directory(path.parent)
        selector_committed = True
        if selector_backup is not None:
            selector_backup.unlink(missing_ok=True)
        if sidecar_backup is not None:
            sidecar_backup.unlink(missing_ok=True)
            _fsync_directory(sidecar_target.parent)
        try:
            _collect_stale(path, published_store)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = LogicSegmentIdentityView._from_publication(
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
        if not selector_committed:
            if selector_backup is not None and selector_backup.is_file():
                os.replace(selector_backup, path)
                _fsync_directory(path.parent)
            elif selector_published:
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
        if not selector_committed and sidecar_target is not None:
            if sidecar_backup is not None and sidecar_backup.is_file():
                os.replace(sidecar_backup, sidecar_target)
            elif sidecar_published:
                sidecar_target.unlink(missing_ok=True)
            if sidecar_published or sidecar_backup is not None:
                _fsync_directory(sidecar_target.parent)
        if isinstance(error, sqlite3.IntegrityError):
            raise ValidationError("logic segment identity is duplicated") from error
        raise
    finally:
        if sidecar_temporary is not None:
            sidecar_temporary.unlink(missing_ok=True)
        if sidecar_backup is not None and selector_committed:
            sidecar_backup.unlink(missing_ok=True)
        if selector_backup is not None:
            selector_backup.unlink(missing_ok=True)
        if lease is not None:
            lease.close()


def read_logic_segment_identity_storage(
    path: Path,
    *,
    expected_design: Optional[str] = None,
    expected_platform: Optional[str] = None,
    expected_fpga: Optional[str] = None,
    expected_system_routes_sha256: Optional[str] = None,
    expected_tdm_schedule_sha256: Optional[str] = None,
    expected_semantic_contract_sha256: Optional[str] = None,
    allow_legacy: bool = False,
) -> Mapping[str, Any]:
    lease: Optional[_Lease] = _Lease(path, exclusive=False)
    try:
        value = read_json(path)
        if value.get("schema") == LOGIC_SEGMENT_IDENTITY_STORAGE_SCHEMA:
            view = LogicSegmentIdentityView(
                path,
                expected_design=expected_design,
                expected_platform=expected_platform,
                expected_fpga=expected_fpga,
                expected_system_routes_sha256=expected_system_routes_sha256,
                expected_tdm_schedule_sha256=expected_tdm_schedule_sha256,
                expected_semantic_contract_sha256=expected_semantic_contract_sha256,
                _lease=lease,
            )
            lease = None
            return view
        if not allow_legacy or value.get("schema") != LOGIC_SEGMENT_IDENTITY_SCHEMA:
            raise ValidationError(
                "legacy logic segment identity JSON requires explicit compatibility"
            )
        from .logic_segment_timing import validate_logic_segment_identity

        validate_logic_segment_identity(value)
        expected = {
            "design": expected_design, "platform": expected_platform,
            "fpga": expected_fpga,
            "system_routes_sha256": expected_system_routes_sha256,
            "tdm_schedule_sha256": expected_tdm_schedule_sha256,
            "semantic_contract_sha256": expected_semantic_contract_sha256,
        }
        for key, required in expected.items():
            if required is not None and value.get(key) != required:
                raise ValidationError(
                    f"legacy logic segment identity {key} source seal disagrees"
                )
        return value
    finally:
        if lease is not None:
            lease.close()
