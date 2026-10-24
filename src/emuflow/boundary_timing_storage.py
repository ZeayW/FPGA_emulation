"""Typed owner for endpoint-scale physical boundary timing evidence."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
import fcntl
from functools import lru_cache
import hashlib
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
from .timing_disposition import OPTIMIZED_AWAY, is_optimized_away, validate_optimization_record


BOUNDARY_TIMING_SCHEMA = "emuflow.boundary-timing/v1"
BOUNDARY_TIMING_STORAGE_SCHEMA_V1 = "emuflow.boundary-timing-storage/v1"
BOUNDARY_TIMING_STORAGE_SCHEMA = "emuflow.boundary-timing-storage/v2"
BOUNDARY_TIMING_APPLICATION_ID = 0x454D5554  # ASCII "EMUT"
BOUNDARY_TIMING_USER_VERSION = 2

_HEADER_KEYS = {
    "schema", "status", "design", "platform", "fpga", "provider",
    "qualification", "measurement_scope", "coverage",
}
_COUNT_KEYS = {"endpoints", "tx", "rx"}
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version", "header",
    "counts", "logical_sha256", "artifact", "store_bytes", "store_sha256",
}

_DDL_V1 = """
CREATE TABLE metadata(
  key TEXT PRIMARY KEY NOT NULL,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE timings(
  id TEXT PRIMARY KEY NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind IN ('tx','rx')),
  schedule_entry TEXT NOT NULL CHECK(length(schedule_entry) > 0),
  delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0),
  start_object TEXT NOT NULL CHECK(length(start_object) > 0),
  end_object TEXT NOT NULL CHECK(length(end_object) > 0),
  measurement TEXT NOT NULL CHECK(length(measurement) > 0)
) WITHOUT ROWID;
CREATE UNIQUE INDEX timings_schedule_kind_unique
  ON timings(schedule_entry,kind);
CREATE INDEX timings_kind_id ON timings(kind,id);
"""


_DDL = _DDL_V1.replace(
    "delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0)", "delay_ns REAL CHECK(delay_ns >= 0.0)"
).replace("start_object TEXT NOT NULL", "start_object TEXT").replace("end_object TEXT NOT NULL", "end_object TEXT").replace(
    "measurement TEXT NOT NULL CHECK(length(measurement) > 0)",
    "measurement TEXT NOT NULL CHECK(length(measurement) > 0), optimization_source TEXT, "
    "CHECK((measurement='optimized-away' AND delay_ns IS NULL AND start_object IS NULL AND end_object IS NULL "
    "AND optimization_source IS NOT NULL) OR (measurement!='optimized-away' AND delay_ns IS NOT NULL "
    "AND start_object IS NOT NULL AND end_object IS NOT NULL AND optimization_source IS NULL))"
)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("boundary timing value is not canonical") from error


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


@lru_cache(maxsize=2)
def _expected_sqlite_objects(version: int = BOUNDARY_TIMING_USER_VERSION) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_DDL_V1 if version == 1 else _DDL)
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
        != BOUNDARY_TIMING_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        not in {1, BOUNDARY_TIMING_USER_VERSION}
    ):
        raise ValidationError("unsupported boundary timing store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects(connection.execute("PRAGMA user_version").fetchone()[0]):
        raise ValidationError("boundary timing store objects are invalid")


def _validate_header(raw: Any, counts: Optional[Mapping[str, int]] = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _HEADER_KEYS:
        raise ValidationError("boundary timing header is malformed")
    header = dict(raw)
    if (
        header.get("schema") != BOUNDARY_TIMING_SCHEMA
        or header.get("status") != "pass"
        or header.get("measurement_scope") not in {
            "partition-boundary", "board-integrated-interface",
        }
        or any(
            not isinstance(header.get(field), str) or not header[field]
            for field in (
                "design", "platform", "fpga", "provider", "qualification",
            )
        )
        or not isinstance(header.get("coverage"), Mapping)
        or set(header["coverage"]) != _COUNT_KEYS
        or any(
            not _strict_nonnegative_int(value)
            for value in header["coverage"].values()
        )
    ):
        raise ValidationError("boundary timing header is invalid")
    header["coverage"] = dict(header["coverage"])
    if counts is not None and header["coverage"] != dict(counts):
        raise ValidationError("boundary timing header counts disagree")
    return header


def _normalize_record(raw: Any) -> dict[str, Any]:
    expected = {"id", "kind", "schedule_entry", "delay_ns", "start_object", "end_object", "measurement"}
    if isinstance(raw, Mapping) and is_optimized_away(raw):
        expected.add("optimization_source")
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValidationError("boundary timing endpoint fields are invalid")
    record = dict(raw)
    eliminated = validate_optimization_record(record)
    delay = record.get("delay_ns")
    if (
        record.get("kind") not in {"tx", "rx"}
        or any(
            not isinstance(record.get(field), str) or not record[field]
            for field in (
                "id", "schedule_entry", "measurement",
            )
        )
        or (not eliminated and (isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay)) or float(delay) < 0.0
            or any(not isinstance(record.get(k),str) or not record[k] for k in ("start_object","end_object"))))
    ):
        raise ValidationError("boundary timing endpoint values are invalid")
    record["delay_ns"] = None if eliminated else float(delay)
    return record


def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    record = {
        "id": str(row["id"]), "kind": str(row["kind"]),
        "schedule_entry": str(row["schedule_entry"]),
        "delay_ns": None if row["delay_ns"] is None else float(row["delay_ns"]),
        "start_object": None if row["start_object"] is None else str(row["start_object"]),
        "end_object": None if row["end_object"] is None else str(row["end_object"]),
        "measurement": str(row["measurement"]),
    }
    if "optimization_source" in row.keys() and row["optimization_source"] is not None:
        record["optimization_source"] = str(row["optimization_source"])
    return record


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
            raise ValidationError("boundary timing lock is a symbolic link")
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
        raise ValidationError("boundary timing selector is malformed")
    if (
        raw.get("schema") not in {BOUNDARY_TIMING_STORAGE_SCHEMA_V1, BOUNDARY_TIMING_STORAGE_SCHEMA}
        or raw.get("logical_schema") != BOUNDARY_TIMING_SCHEMA
        or raw.get("application_id") != BOUNDARY_TIMING_APPLICATION_ID
        or raw.get("user_version") != (1 if raw.get("schema") == BOUNDARY_TIMING_STORAGE_SCHEMA_V1 else BOUNDARY_TIMING_USER_VERSION)
    ):
        raise ValidationError("unsupported boundary timing selector")
    counts = raw.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(value) for value in counts.values())
    ):
        raise ValidationError("boundary timing selector counts are invalid")
    _validate_header(raw.get("header"), counts)
    artifact = raw.get("artifact")
    if (
        not _valid_digest(raw.get("logical_sha256"))
        or not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _strict_nonnegative_int(raw.get("store_bytes"))
        or not _valid_digest(raw.get("store_sha256"))
    ):
        raise ValidationError("boundary timing selector artifact is invalid")
    return raw


def _resolve_store(selector_path: Path, selector: Mapping[str, Any]) -> Path:
    candidate = selector_path.parent / str(selector["artifact"])
    if candidate.name != _store_name(selector_path, str(selector["store_sha256"])):
        raise ValidationError("boundary timing artifact owner disagrees")
    if candidate.is_symlink():
        raise ValidationError("boundary timing store is a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise ValidationError("boundary timing store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("boundary timing store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("boundary timing store seal disagrees")
    return resolved


class _TimingSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "BoundaryTimingView", count: int):
        self._owner = weakref.ref(owner)
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        owner = self._owner()
        if owner is None:
            raise ValidationError("boundary timing view is closed")
        owner._ensure_open()
        with _connect(owner.store_path) as connection:
            for row in connection.execute("SELECT * FROM timings ORDER BY id"):
                yield _record_from_row(row)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        owner = self._owner()
        if owner is None:
            raise ValidationError("boundary timing view is closed")
        owner._ensure_open()
        with _connect(owner.store_path) as connection:
            row = connection.execute(
                "SELECT * FROM timings ORDER BY id LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("boundary timing endpoint is missing")
            return _record_from_row(row)


class BoundaryTimingView(Mapping[str, Any]):
    def __init__(self, selector_path: Path, *, _lease: Optional[_Lease] = None):
        self._lease = _lease or _Lease(selector_path, exclusive=False)
        self._closed = False
        try:
            if selector_path.is_symlink() or not selector_path.is_file():
                raise ValidationError("boundary timing selector is missing")
            self.selector_path = selector_path.resolve()
            self.selector = _validate_selector(read_json(selector_path))
            self.store_path = _resolve_store(selector_path, self.selector)
            self._validate_store()
            self._endpoints = _TimingSequence(
                self, self.selector["counts"]["endpoints"]
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls, selector_path: Path, selector: dict[str, Any], store_path: Path,
        lease: _Lease,
    ) -> "BoundaryTimingView":
        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        value._endpoints = _TimingSequence(
            value, selector["counts"]["endpoints"]
        )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("boundary timing view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "BoundaryTimingView":
        self._ensure_open()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _validate_store(self) -> None:
        digest = hashlib.sha256()
        try:
            with _connect(self.store_path) as connection:
                _exact_sqlite_envelope(connection)
                if connection.execute("PRAGMA user_version").fetchone()[0] != self.selector["user_version"]:
                    raise ValidationError("boundary timing selector/store version disagrees")
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
                    raise ValidationError("boundary timing metadata disagrees")
                counts = {"endpoints": 0, "tx": 0, "rx": 0}
                previous = None
                for row in connection.execute("SELECT * FROM timings ORDER BY id"):
                    record = _normalize_record(_record_from_row(row))
                    if previous is not None and record["id"] <= previous:
                        raise ValidationError("boundary timing order is invalid")
                    previous = record["id"]
                    counts["endpoints"] += 1
                    counts[record["kind"]] += 1
                    digest.update(_canonical(record).encode("utf-8"))
                    digest.update(b"\n")
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("boundary timing store is corrupt") from error
        if counts != self.selector["counts"]:
            raise ValidationError("boundary timing store counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": self.selector["header"],
            "counts": counts,
            "endpoint_sha256": digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        if logical != self.selector["logical_sha256"]:
            raise ValidationError("boundary timing logical seal disagrees")

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


def write_boundary_timing_storage(
    path: Path,
    header: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
) -> BoundaryTimingView:
    """Atomically publish one typed boundary timing generation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    base_header = dict(header)
    base_header["coverage"] = {"endpoints": 0, "tx": 0, "rx": 0}
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
            raise ValidationError("boundary timing selector is invalid")
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
        connection.execute(f"PRAGMA application_id={BOUNDARY_TIMING_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={BOUNDARY_TIMING_USER_VERSION}")
        connection.executescript(_DDL)
        rows: list[tuple[Any, ...]] = []

        def flush() -> None:
            if rows:
                connection.executemany(
                    "INSERT INTO timings VALUES (?,?,?,?,?,?,?,?)", rows
                )
                rows.clear()

        try:
            for raw in records:
                record = _normalize_record(raw)
                rows.append((
                    record["id"], record["kind"], record["schedule_entry"],
                    record["delay_ns"], record["start_object"],
                    record["end_object"], record["measurement"], record.get("optimization_source"),
                ))
                if len(rows) >= 8192:
                    flush()
            flush()
        except sqlite3.IntegrityError as error:
            raise ValidationError("boundary timing endpoint is duplicated") from error
        kind_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT kind,count(*) FROM timings GROUP BY kind"
            )
        }
        endpoint_count = sum(kind_counts.values())
        counts = {
            "endpoints": endpoint_count,
            "tx": kind_counts.get("tx", 0),
            "rx": kind_counts.get("rx", 0),
        }
        normalized_header = _validate_header(
            {**base_header, "coverage": counts}, counts
        )
        endpoint_digest = hashlib.sha256()
        for row in connection.execute("SELECT * FROM timings ORDER BY id"):
            endpoint_digest.update(
                _canonical(_record_from_row(row)).encode("utf-8")
            )
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
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        store_sha = file_sha256(temporary)
        published_store = path.with_name(_store_name(path, store_sha))
        if published_store.is_symlink():
            raise ValidationError("boundary timing generation is a symlink")
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError("boundary timing generation seal disagrees")
            temporary.unlink()
        else:
            os.replace(temporary, published_store)
            created_store = True
            _fsync_directory(path.parent)
        selector = {
            "schema": BOUNDARY_TIMING_STORAGE_SCHEMA,
            "logical_schema": BOUNDARY_TIMING_SCHEMA,
            "application_id": BOUNDARY_TIMING_APPLICATION_ID,
            "user_version": BOUNDARY_TIMING_USER_VERSION,
            "header": normalized_header, "counts": counts,
            "logical_sha256": logical_sha,
            "artifact": published_store.name,
            "store_bytes": published_store.stat().st_size,
            "store_sha256": store_sha,
        }
        _validate_selector(selector)
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(selector_temporary, path)
        committed = True
        _fsync_directory(path.parent)
        try:
            _collect_stale(path, published_store)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = BoundaryTimingView._from_publication(
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


def read_boundary_timing_storage(
    path: Path,
    *,
    allow_legacy: bool = False,
    expected_design: Any = None,
    expected_platform: Any = None,
    expected_fpga: Any = None,
    expected_selector_bytes: Any = None,
    expected_selector_sha256: Any = None,
) -> Mapping[str, Any]:
    if expected_selector_bytes is not None and (
        isinstance(expected_selector_bytes, bool)
        or not isinstance(expected_selector_bytes, int)
        or path.stat().st_size != expected_selector_bytes
    ):
        raise ValidationError("boundary timing selector byte count disagrees")
    if expected_selector_sha256 is not None and (
        not _valid_digest(expected_selector_sha256)
        or file_sha256(path) != expected_selector_sha256
    ):
        raise ValidationError("boundary timing selector digest disagrees")
    value = read_json(path)
    if value.get("schema") in {BOUNDARY_TIMING_STORAGE_SCHEMA_V1, BOUNDARY_TIMING_STORAGE_SCHEMA}:
        view = BoundaryTimingView(path)
        if any(
            expected is not None and view.get(field) != expected
            for field, expected in (
                ("design", expected_design),
                ("platform", expected_platform),
                ("fpga", expected_fpga),
            )
        ):
            view.close()
            raise ValidationError("boundary timing indexed evidence source disagrees")
        return view
    if not allow_legacy or value.get("schema") != BOUNDARY_TIMING_SCHEMA:
        raise ValidationError(
            "legacy boundary timing JSON requires explicit compatibility"
        )
    if any(
        expected is not None and value.get(field) != expected
        for field, expected in (
            ("design", expected_design),
            ("platform", expected_platform),
            ("fpga", expected_fpga),
        )
    ):
        raise ValidationError("boundary timing legacy evidence source disagrees")
    return value


def boundary_timing_reference(
    selector_path: Path, *, relative_to: Path
) -> dict[str, Any]:
    if selector_path.is_symlink() or not selector_path.is_file():
        raise ValidationError("boundary timing selector is missing")
    _validate_selector(read_json(selector_path))
    resolved = selector_path.resolve()
    relative = Path(os.path.relpath(resolved, relative_to.resolve()))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("boundary timing selector escapes its summary")
    return {
        "schema": "emuflow.physical-evidence-reference/v2",
        "logical_schema": BOUNDARY_TIMING_SCHEMA,
        "path": str(relative),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def publish_boundary_timing_selector(
    source: Path, output_directory: Path, relative_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Relocate one typed selector and immutable generation without expansion."""

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("boundary timing relative path is unsafe")
    target = output_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        with BoundaryTimingView(source) as view:
            selector = dict(view.selector)
        return selector, boundary_timing_reference(
            target, relative_to=output_directory
        )
    with BoundaryTimingView(source) as source_view:
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
                raise ValidationError("boundary timing generation is a symlink")
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
                        "boundary timing copied generation seal disagrees"
                    )
                os.replace(temporary_store, destination_store)
                temporary_store = None
                _fsync_directory(target.parent)
            elif (
                destination_store.stat().st_size != selector["store_bytes"]
                or file_sha256(destination_store) != selector["store_sha256"]
            ):
                raise ValidationError(
                    "boundary timing destination generation seal disagrees"
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
    return selector, boundary_timing_reference(
        target, relative_to=output_directory
    )
