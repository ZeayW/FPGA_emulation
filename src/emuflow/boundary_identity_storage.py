"""Typed owner for graph-sized physical boundary identities.

The public ``boundary-identities.json`` path is a bounded selector.  Endpoint
records and their register memberships live in one immutable, content-addressed
SQLite generation.  Readers hold a shared lease while lazy endpoint cursors are
live; writers publish the selector last while holding the exclusive lease.
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


BOUNDARY_IDENTITY_SCHEMA = "emuflow.boundary-identity/v1"
BOUNDARY_IDENTITY_STORAGE_SCHEMA = "emuflow.boundary-identity-storage/v1"
BOUNDARY_IDENTITY_APPLICATION_ID = 0x454D5542  # ASCII "EMUB"
BOUNDARY_IDENTITY_USER_VERSION = 1

_HEADER_KEYS = {
    "schema", "status", "design", "platform", "fpga", "provider", "coverage",
}
_COUNT_KEYS = {"endpoints", "tx", "rx", "external_port_nets"}
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version", "header",
    "counts", "logical_sha256", "artifact", "store_bytes", "store_sha256",
}

_DDL = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE endpoints(
  id TEXT PRIMARY KEY NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  schedule_entry TEXT NOT NULL CHECK(length(schedule_entry) > 0),
  demand TEXT NOT NULL CHECK(length(demand) > 0),
  logical_net TEXT NOT NULL CHECK(length(logical_net) > 0),
  link TEXT NOT NULL CHECK(length(link) > 0),
  peer TEXT NOT NULL CHECK(length(peer) > 0),
  lane INTEGER NOT NULL CHECK(lane >= 0),
  logical_lane INTEGER NOT NULL CHECK(logical_lane >= 0),
  internal_signal TEXT NOT NULL CHECK(length(internal_signal) > 0),
  source_class TEXT NOT NULL CHECK(
    source_class IN ('dut-net','forwarded-shadow','captured-shadow')
  ),
  external_port TEXT NOT NULL CHECK(length(external_port) > 0),
  external_port_bit INTEGER NOT NULL CHECK(external_port_bit >= 0),
  external_net TEXT NOT NULL CHECK(length(external_net) > 0),
  merged_logical_net TEXT
) WITHOUT ROWID;
CREATE UNIQUE INDEX endpoints_schedule_kind_unique
  ON endpoints(schedule_entry,kind);
CREATE INDEX endpoints_kind_id ON endpoints(kind,id);
CREATE TABLE boundary_registers(
  endpoint_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  instance TEXT NOT NULL CHECK(length(instance) > 0),
  PRIMARY KEY(endpoint_id,ordinal),
  UNIQUE(endpoint_id,instance),
  FOREIGN KEY(endpoint_id) REFERENCES endpoints(id)
) WITHOUT ROWID;
"""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("boundary identity value is not canonical") from error


def _strict_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _normalized_sql(value: Any) -> str:
    return " ".join(str(value).split())


@lru_cache(maxsize=1)
def _expected_sqlite_objects() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
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


def _exact_sqlite_envelope(connection: sqlite3.Connection) -> None:
    if (
        connection.execute("PRAGMA application_id").fetchone()[0]
        != BOUNDARY_IDENTITY_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        != BOUNDARY_IDENTITY_USER_VERSION
    ):
        raise ValidationError("unsupported boundary identity store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects():
        raise ValidationError("boundary identity store objects are invalid")


def _validate_header(raw: Any, counts: Optional[Mapping[str, int]] = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _HEADER_KEYS:
        raise ValidationError("boundary identity header is malformed")
    header = dict(raw)
    if (
        header.get("schema") != BOUNDARY_IDENTITY_SCHEMA
        or header.get("status") != "pass"
        or any(
            not isinstance(header.get(field), str) or not header[field]
            for field in ("design", "platform", "fpga", "provider")
        )
        or not isinstance(header.get("coverage"), Mapping)
        or set(header["coverage"]) != _COUNT_KEYS
        or any(
            not _strict_nonnegative_int(value)
            for value in header["coverage"].values()
        )
    ):
        raise ValidationError("boundary identity header is invalid")
    header["coverage"] = dict(header["coverage"])
    if counts is not None and header["coverage"] != dict(counts):
        raise ValidationError("boundary identity header counts disagree")
    return header


def _normalize_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("boundary identity endpoint is invalid")
    required = {
        "id", "kind", "schedule_entry", "demand", "logical_net", "link",
        "peer", "lane", "logical_lane", "internal_signal", "source_class",
        "merged_ir",
    }
    if set(raw) != required:
        raise ValidationError("boundary identity endpoint fields are invalid")
    record = dict(raw)
    if (
        any(
            not isinstance(record.get(field), str) or not record[field]
            for field in (
                "id", "schedule_entry", "demand", "logical_net", "link",
                "peer", "internal_signal",
            )
        )
        or record.get("kind") not in {"tx", "rx"}
        or record.get("source_class") not in {
            "dut-net", "forwarded-shadow", "captured-shadow",
        }
        or not _strict_nonnegative_int(record.get("lane"))
        or not _strict_nonnegative_int(record.get("logical_lane"))
    ):
        raise ValidationError("boundary identity endpoint values are invalid")
    merged = record.get("merged_ir")
    if not isinstance(merged, Mapping) or set(merged) != {
        "external_port", "external_port_bit", "external_net", "logical_net",
        "boundary_register_instances",
    }:
        raise ValidationError("boundary identity merged mapping is invalid")
    if (
        not isinstance(merged.get("external_port"), str)
        or not merged["external_port"]
        or not _strict_nonnegative_int(merged.get("external_port_bit"))
        or not isinstance(merged.get("external_net"), str)
        or not merged["external_net"]
        or (
            merged.get("logical_net") is not None
            and (
                not isinstance(merged["logical_net"], str)
                or not merged["logical_net"]
            )
        )
    ):
        raise ValidationError("boundary identity merged values are invalid")
    registers = merged.get("boundary_register_instances")
    if (
        not isinstance(registers, Sequence)
        or isinstance(registers, (str, bytes, bytearray))
        or any(not isinstance(item, str) or not item for item in registers)
        or len(set(registers)) != len(registers)
        or list(registers) != sorted(registers)
    ):
        raise ValidationError("boundary identity register mapping is invalid")
    normalized = dict(record)
    normalized["merged_ir"] = {
        **dict(merged), "boundary_register_instances": list(registers),
    }
    return normalized


def _record_from_values(row: sqlite3.Row, registers: list[str]) -> dict[str, Any]:
    return {
        "id": str(row["id"]), "kind": str(row["kind"]),
        "schedule_entry": str(row["schedule_entry"]),
        "demand": str(row["demand"]), "logical_net": str(row["logical_net"]),
        "link": str(row["link"]), "peer": str(row["peer"]),
        "lane": int(row["lane"]), "logical_lane": int(row["logical_lane"]),
        "internal_signal": str(row["internal_signal"]),
        "source_class": str(row["source_class"]),
        "merged_ir": {
            "external_port": str(row["external_port"]),
            "external_port_bit": int(row["external_port_bit"]),
            "external_net": str(row["external_net"]),
            "logical_net": (
                None if row["merged_logical_net"] is None
                else str(row["merged_logical_net"])
            ),
            "boundary_register_instances": registers,
        },
    }


def _iter_records(connection: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    cursor = connection.execute(
        "SELECT e.*,r.ordinal,r.instance FROM endpoints e "
        "LEFT JOIN boundary_registers r ON r.endpoint_id=e.id "
        "ORDER BY e.id,r.ordinal"
    )
    pending: Optional[sqlite3.Row] = None
    registers: list[str] = []
    for row in cursor:
        if pending is not None and row["id"] != pending["id"]:
            yield _record_from_values(pending, registers)
            registers = []
        pending = row
        if row["instance"] is not None:
            if int(row["ordinal"]) != len(registers):
                raise ValidationError("boundary identity register order is invalid")
            registers.append(str(row["instance"]))
    if pending is not None:
        yield _record_from_values(pending, registers)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


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
        lock_path = selector_path.parent.resolve() / f".{selector_path.name}.lock"
        if lock_path.is_symlink():
            raise ValidationError("boundary identity lock is a symbolic link")
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
        fcntl.flock(self.descriptor, fcntl.LOCK_SH)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _validate_selector(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _SELECTOR_KEYS:
        raise ValidationError("boundary identity selector is malformed")
    if (
        raw.get("schema") != BOUNDARY_IDENTITY_STORAGE_SCHEMA
        or raw.get("logical_schema") != BOUNDARY_IDENTITY_SCHEMA
        or raw.get("application_id") != BOUNDARY_IDENTITY_APPLICATION_ID
        or raw.get("user_version") != BOUNDARY_IDENTITY_USER_VERSION
    ):
        raise ValidationError("unsupported boundary identity selector")
    counts = raw.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(value) for value in counts.values())
    ):
        raise ValidationError("boundary identity selector counts are invalid")
    _validate_header(raw.get("header"), counts)
    artifact = raw.get("artifact")
    if (
        not _valid_digest(raw.get("logical_sha256"))
        or not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _strict_nonnegative_int(raw.get("store_bytes"))
        or not _valid_digest(raw.get("store_sha256"))
    ):
        raise ValidationError("boundary identity selector artifact is invalid")
    return raw


def _resolve_store(
    selector_path: Path, selector: Mapping[str, Any]
) -> tuple[Path, StoreSource]:
    candidate = selector_path.parent / str(selector["artifact"])
    if candidate.name != _store_name(selector_path, str(selector["store_sha256"])):
        raise ValidationError("boundary identity artifact owner disagrees")
    if candidate.is_symlink():
        raise ValidationError("boundary identity store is a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
        store_source = bind_store_source(resolved, "boundary-identity-store-v1")
    except OSError as error:
        raise ValidationError("boundary identity store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("boundary identity store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("boundary identity store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return resolved, store_source


class _EndpointSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "BoundaryIdentityView", count: int):
        self._owner = weakref.ref(owner)
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        owner = self._owner()
        if owner is None:
            raise ValidationError("boundary identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            yield from _iter_records(connection)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        owner = self._owner()
        if owner is None:
            raise ValidationError("boundary identity view is closed")
        owner._ensure_open()
        with closing(_connect(owner.store_path)) as connection, connection:
            row = connection.execute(
                "SELECT * FROM endpoints ORDER BY id LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("boundary identity endpoint is missing")
            registers = [
                str(item[0]) for item in connection.execute(
                    "SELECT instance FROM boundary_registers WHERE endpoint_id=? "
                    "ORDER BY ordinal", (row["id"],)
                )
            ]
            return _record_from_values(row, registers)


class BoundaryIdentityView(Mapping[str, Any]):
    def __init__(self, selector_path: Path, *, _lease: Optional[_Lease] = None):
        self._lease = _lease or _Lease(selector_path, exclusive=False)
        self._closed = False
        try:
            if selector_path.is_symlink() or not selector_path.is_file():
                raise ValidationError("boundary identity selector is missing")
            self.selector_path = selector_path.resolve()
            selector_source = bind_store_source(
                self.selector_path, "boundary-identity-selector-v1"
            )
            self.selector = _validate_selector(read_json(selector_path))
            self.store_path, store_source = _resolve_store(selector_path, self.selector)
            self._source_bindings = (selector_source, store_source)
            self._validate_store()
            require_store_sources_unchanged(self._source_bindings)
            self._endpoints = _EndpointSequence(
                self, self.selector["counts"]["endpoints"]
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls, selector_path: Path, selector: dict[str, Any], store_path: Path,
        lease: _Lease,
    ) -> "BoundaryIdentityView":
        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        value._source_bindings = (
            bind_store_source(value.selector_path, "boundary-identity-selector-v1"),
            bind_store_source(value.store_path, "boundary-identity-store-v1"),
        )
        value._endpoints = _EndpointSequence(
            value, selector["counts"]["endpoints"]
        )
        return value

    def bound_store_sources(self):
        self._ensure_open()
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("boundary identity view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "BoundaryIdentityView":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_store(self) -> None:
        digest = hashlib.sha256()
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
                    "counts": self.selector["counts"],
                    "header": self.selector["header"],
                    "logical_sha256": self.selector["logical_sha256"],
                }:
                    raise ValidationError("boundary identity metadata disagrees")
                counts = {"endpoints": 0, "tx": 0, "rx": 0, "external_port_nets": 0}
                previous = None
                for record in _iter_records(connection):
                    record = _normalize_record(record)
                    if previous is not None and record["id"] <= previous:
                        raise ValidationError("boundary identity order is invalid")
                    previous = record["id"]
                    counts["endpoints"] += 1
                    counts[record["kind"]] += 1
                    counts["external_port_nets"] += 1
                    digest.update(_canonical(record).encode("utf-8"))
                    digest.update(b"\n")
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("boundary identity store is corrupt") from error
        if counts != self.selector["counts"]:
            raise ValidationError("boundary identity store counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": self.selector["header"],
            "counts": counts,
            "endpoint_sha256": digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        if logical != self.selector["logical_sha256"]:
            raise ValidationError("boundary identity logical seal disagrees")

    def __getitem__(self, key: str) -> Any:
        self._ensure_open()
        if key == "endpoints":
            return self._endpoints
        return self.selector["header"][key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        yield from self.selector["header"]
        yield "endpoints"

    def __len__(self) -> int:
        return len(self.selector["header"]) + 1


def _collect_stale(path: Path, keep: Path) -> None:
    prefix = _owner_prefix(path)
    for candidate in path.parent.iterdir():
        if (
            candidate == keep
            or not candidate.name.startswith(prefix)
            or candidate.suffix != ".sqlite"
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            continue
        candidate.unlink()


def write_boundary_identity_storage(
    path: Path,
    header: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    source_bindings=(),
) -> BoundaryIdentityView:
    """Atomically publish one typed boundary identity generation."""

    source_bindings = validate_store_destination(path, source_bindings)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_header = dict(header)
    base_header["coverage"] = {
        "endpoints": 0, "tx": 0, "rx": 0, "external_port_nets": 0,
    }
    _validate_header(base_header)
    lease: Optional[_Lease] = _Lease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    published_store: Optional[Path] = None
    created_store = False
    committed = False
    connection: Optional[sqlite3.Connection] = None
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValidationError("boundary identity selector is invalid")
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".sqlite.tmp", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        os.close(descriptor)
        selector_temporary = Path(name)
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA application_id={BOUNDARY_IDENTITY_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={BOUNDARY_IDENTITY_USER_VERSION}")
        connection.executescript(_DDL)
        endpoint_rows: list[tuple[Any, ...]] = []
        register_rows: list[tuple[Any, ...]] = []

        def flush() -> None:
            if endpoint_rows:
                connection.executemany(
                    "INSERT INTO endpoints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    endpoint_rows,
                )
                endpoint_rows.clear()
            if register_rows:
                connection.executemany(
                    "INSERT INTO boundary_registers VALUES (?,?,?)",
                    register_rows,
                )
                register_rows.clear()

        try:
            for raw in records:
                record = _normalize_record(raw)
                merged = record["merged_ir"]
                endpoint_rows.append((
                    record["id"], record["kind"], record["schedule_entry"],
                    record["demand"], record["logical_net"], record["link"],
                    record["peer"], record["lane"], record["logical_lane"],
                    record["internal_signal"], record["source_class"],
                    merged["external_port"], merged["external_port_bit"],
                    merged["external_net"], merged["logical_net"],
                ))
                register_rows.extend(
                    (record["id"], ordinal, instance)
                    for ordinal, instance in enumerate(
                        merged["boundary_register_instances"]
                    )
                )
                if len(endpoint_rows) >= 8192:
                    flush()
            flush()
        except sqlite3.IntegrityError as error:
            raise ValidationError("boundary identity endpoint is duplicated") from error
        count_rows = connection.execute(
            "SELECT kind,count(*) FROM endpoints GROUP BY kind"
        ).fetchall()
        kind_counts = {str(row[0]): int(row[1]) for row in count_rows}
        endpoint_count = sum(kind_counts.values())
        counts = {
            "endpoints": endpoint_count,
            "tx": kind_counts.get("tx", 0),
            "rx": kind_counts.get("rx", 0),
            "external_port_nets": endpoint_count,
        }
        normalized_header = _validate_header(
            {**base_header, "coverage": counts}, counts
        )
        endpoint_digest = hashlib.sha256()
        for record in _iter_records(connection):
            endpoint_digest.update(_canonical(record).encode("utf-8"))
            endpoint_digest.update(b"\n")
        logical_sha = hashlib.sha256(_canonical({
            "header": normalized_header, "counts": counts,
            "endpoint_sha256": endpoint_digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        for key, value in (
            ("counts", counts), ("header", normalized_header),
            ("logical_sha256", logical_sha),
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
            raise ValidationError("boundary identity generation is a symlink")
        require_store_sources_unchanged(source_bindings)
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError("boundary identity generation seal disagrees")
            temporary.unlink()
        else:
            publish_sqlite_store(
                temporary,
                published_store,
                "boundary-identity-store-v1",
                sources=source_bindings,
            )
            created_store = True
        selector = {
            "schema": BOUNDARY_IDENTITY_STORAGE_SCHEMA,
            "logical_schema": BOUNDARY_IDENTITY_SCHEMA,
            "application_id": BOUNDARY_IDENTITY_APPLICATION_ID,
            "user_version": BOUNDARY_IDENTITY_USER_VERSION,
            "header": normalized_header,
            "counts": counts,
            "logical_sha256": logical_sha,
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        }
        _validate_selector(selector)
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        require_store_sources_unchanged(source_bindings)
        os.replace(selector_temporary, path)
        committed = True
        _fsync_directory(path.parent)
        try:
            _collect_stale(path, published_store)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = BoundaryIdentityView._from_publication(
            path, selector, published_store, lease
        )
        lease = None
        return view
    except BaseException:
        if connection is not None:
            connection.close()
        if published_store is not None and created_store and not committed:
            published_store.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if selector_temporary is not None:
            selector_temporary.unlink(missing_ok=True)
        if lease is not None:
            lease.close()


def read_boundary_identity_storage(
    path: Path, *, allow_legacy: bool = False
) -> Mapping[str, Any]:
    value = read_json(path)
    if value.get("schema") == BOUNDARY_IDENTITY_STORAGE_SCHEMA:
        return BoundaryIdentityView(path)
    if not allow_legacy or value.get("schema") != BOUNDARY_IDENTITY_SCHEMA:
        raise ValidationError(
            "legacy boundary identity JSON requires explicit compatibility"
        )
    return value


def boundary_identity_reference(
    selector_path: Path, *, relative_to: Path
) -> dict[str, Any]:
    if selector_path.is_symlink() or not selector_path.is_file():
        raise ValidationError("boundary identity selector is missing")
    _validate_selector(read_json(selector_path))
    resolved = selector_path.resolve()
    relative = Path(os.path.relpath(resolved, relative_to.resolve()))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("boundary identity selector escapes its summary")
    return {
        "schema": "emuflow.physical-evidence-reference/v2",
        "logical_schema": BOUNDARY_IDENTITY_SCHEMA,
        "path": str(relative),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def publish_boundary_identity_selector(
    source: Path, output_directory: Path, relative_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Relocate one typed selector and immutable generation without expansion."""

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("boundary identity relative path is unsafe")
    target = output_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        with BoundaryIdentityView(source) as view:
            selector = dict(view.selector)
        return selector, boundary_identity_reference(
            target, relative_to=output_directory
        )
    with BoundaryIdentityView(source) as source_view:
        source_bindings = validate_store_destination(
            target, source_view.bound_store_sources()
        )
        selector = dict(source_view.selector)
        lease: Optional[_Lease] = _Lease(target, exclusive=True)
        temporary_store: Optional[Path] = None
        created_store = False
        selector_committed = False
        temporary_selector: Optional[Path] = None
        try:
            destination_store = target.with_name(
                _store_name(target, selector["store_sha256"])
            )
            selector["artifact"] = destination_store.name
            if destination_store.is_symlink():
                raise ValidationError("boundary identity generation is a symlink")
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
                if (
                    temporary_store.stat().st_size != selector["store_bytes"]
                    or file_sha256(temporary_store) != selector["store_sha256"]
                ):
                    raise ValidationError(
                        "boundary identity copied generation seal disagrees"
                    )
                publish_sqlite_store(
                    temporary_store,
                    destination_store,
                    "boundary-identity-store-v1",
                    sources=source_bindings,
                )
                created_store = True
                temporary_store = None
            elif (
                destination_store.stat().st_size != selector["store_bytes"]
                or file_sha256(destination_store) != selector["store_sha256"]
            ):
                raise ValidationError(
                    "boundary identity destination generation seal disagrees"
                )
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            temporary_selector = Path(name)
            write_json(temporary_selector, selector, compact=True)
            with temporary_selector.open("rb") as stream:
                os.fsync(stream.fileno())
            require_store_sources_unchanged(source_bindings)
            os.replace(temporary_selector, target)
            selector_committed = True
            temporary_selector = None
            _fsync_directory(target.parent)
            _collect_stale(target, destination_store)
        finally:
            if created_store and not selector_committed:
                destination_store.unlink(missing_ok=True)
            if temporary_store is not None:
                temporary_store.unlink(missing_ok=True)
            if temporary_selector is not None:
                temporary_selector.unlink(missing_ok=True)
            if lease is not None:
                lease.close()
    return selector, boundary_identity_reference(
        target, relative_to=output_directory
    )
