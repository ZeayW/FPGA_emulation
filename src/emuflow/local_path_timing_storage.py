"""Typed owner for original same-FPGA routed path timing evidence."""

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


LOCAL_PATH_TIMING_SCHEMA = "emuflow.local-path-timing/v1"
LOCAL_PATH_TIMING_STORAGE_SCHEMA_V1 = "emuflow.local-path-timing-storage/v1"
LOCAL_PATH_TIMING_STORAGE_SCHEMA = "emuflow.local-path-timing-storage/v2"
LOCAL_PATH_TIMING_APPLICATION_ID = 0x454D554C  # ASCII "EMUL"
LOCAL_PATH_TIMING_USER_VERSION = 2

_HEADER_KEYS = {
    "schema", "status", "design", "fpga", "provider", "qualification",
    "source", "identity_schema", "coverage",
}
_SOURCE_KEYS = {
    "path_database_sha256", "original_ir_sha256", "assignment_sha256",
    "routes_sha256", "original_paths", "original_path_ids_sha256",
}
_COUNT_KEYS = {"local_paths"}
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
  seq INTEGER PRIMARY KEY NOT NULL CHECK(seq >= 0),
  id TEXT NOT NULL CHECK(length(id) > 0),
  kind TEXT NOT NULL CHECK(kind = 'local'),
  fpga TEXT NOT NULL CHECK(length(fpga) > 0),
  clock_domain TEXT NOT NULL CHECK(length(clock_domain) > 0),
  clock_period_ns REAL NOT NULL CHECK(clock_period_ns > 0.0),
  required_time_ns REAL,
  start_pin TEXT NOT NULL CHECK(length(start_pin) > 0),
  end_pin TEXT NOT NULL CHECK(length(end_pin) > 0),
  measurement TEXT,
  delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0)
);
CREATE UNIQUE INDEX timings_id_unique ON timings(id);
CREATE TABLE path_pins(
  path_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  pin TEXT NOT NULL CHECK(length(pin) > 0),
  PRIMARY KEY(path_id,ordinal),
  FOREIGN KEY(path_id) REFERENCES timings(id)
) WITHOUT ROWID;
"""


_DDL = _DDL_V1.replace(
    "delay_ns REAL NOT NULL CHECK(delay_ns >= 0.0)",
    "delay_ns REAL CHECK(delay_ns >= 0.0), optimization_source TEXT, "
    "CHECK((COALESCE(measurement,'')='optimized-away' AND delay_ns IS NULL AND optimization_source IS NOT NULL) OR "
    "(COALESCE(measurement,'')!='optimized-away' AND delay_ns IS NOT NULL AND optimization_source IS NULL))"
)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("local path timing value is not canonical") from error


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
def _expected_sqlite_objects(version: int = LOCAL_PATH_TIMING_USER_VERSION) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
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
        != LOCAL_PATH_TIMING_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        not in {1, LOCAL_PATH_TIMING_USER_VERSION}
    ):
        raise ValidationError("unsupported local path timing store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects(connection.execute("PRAGMA user_version").fetchone()[0]):
        raise ValidationError("local path timing store objects are invalid")


def _validate_header(
    raw: Any, counts: Optional[Mapping[str, int]] = None
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _HEADER_KEYS:
        raise ValidationError("local path timing header is malformed")
    header = dict(raw)
    source = header.get("source")
    if (
        header.get("schema") != LOCAL_PATH_TIMING_SCHEMA
        or header.get("status") != "pass"
        or any(
            not isinstance(header.get(field), str) or not header[field]
            for field in ("design", "fpga", "provider", "qualification")
        )
        or header.get("identity_schema") not in {
            "emuflow.local-path-identity/v1",
            "emuflow.local-path-identity/v2",
        }
        or not isinstance(source, Mapping)
        or set(source) != _SOURCE_KEYS
        or any(
            not _valid_digest(source.get(field))
            for field in _SOURCE_KEYS - {"original_paths"}
        )
        or not _strict_nonnegative_int(source.get("original_paths"))
        or source["original_paths"] == 0
        or not isinstance(header.get("coverage"), Mapping)
        or set(header["coverage"]) != _COUNT_KEYS
        or any(
            not _strict_nonnegative_int(value)
            for value in header["coverage"].values()
        )
    ):
        raise ValidationError("local path timing header is invalid")
    header["source"] = dict(source)
    header["coverage"] = dict(header["coverage"])
    if counts is not None and header["coverage"] != dict(counts):
        raise ValidationError("local path timing header counts disagree")
    return header


def _normalize_record(raw: Any, header: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "id", "kind", "fpga", "clock_domain", "clock_period_ns",
        "start_pin", "end_pin", "delay_ns",
    }
    expected = (
        base | {"required_time_ns", "measurement", "path_pins"}
        if header["identity_schema"] == "emuflow.local-path-identity/v2"
        else base
    )
    if isinstance(raw, Mapping) and is_optimized_away(raw):
        expected = expected | {"optimization_source"}
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ValidationError("local path timing path fields are invalid")
    record = dict(raw)
    eliminated = validate_optimization_record(record)
    period = record.get("clock_period_ns")
    delay = record.get("delay_ns")
    if (
        record.get("kind") != "local"
        or record.get("fpga") != header["fpga"]
        or any(
            not isinstance(record.get(field), str) or not record[field]
            for field in ("id", "clock_domain", "start_pin", "end_pin")
        )
        or isinstance(period, bool)
        or not isinstance(period, (int, float))
        or not math.isfinite(float(period))
        or float(period) <= 0.0
        or (not eliminated and (isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay)) or float(delay) < 0.0))
    ):
        raise ValidationError("local path timing path values are invalid")
    record["clock_period_ns"] = float(period)
    record["delay_ns"] = None if eliminated else float(delay)
    if header["identity_schema"] == "emuflow.local-path-identity/v2":
        required = record.get("required_time_ns")
        pins = record.get("path_pins")
        measurement = record.get("measurement")
        if (
            isinstance(required, bool)
            or not isinstance(required, (int, float))
            or not math.isfinite(float(required))
            or float(required) <= 0.0
            or measurement not in {
                "explicit-routed-path-chain",
                "endpoint-longest-path-fallback", OPTIMIZED_AWAY,
            }
            or not isinstance(pins, Sequence)
            or isinstance(pins, (str, bytes, bytearray))
            or any(not isinstance(pin, str) or not pin or "," in pin for pin in pins)
            or (
                measurement == "explicit-routed-path-chain"
                and (
                    len(pins) < 2
                    or pins[0] != record["start_pin"]
                    or pins[-1] != record["end_pin"]
                )
            )
            or (measurement in {"endpoint-longest-path-fallback", OPTIMIZED_AWAY} and pins)
        ):
            raise ValidationError("local path timing measurement is invalid")
        record["required_time_ns"] = float(required)
        record["path_pins"] = list(pins)
    return record


def _record_from_row(
    row: sqlite3.Row, pins: Optional[Sequence[str]] = None
) -> dict[str, Any]:
    record = {
        "id": str(row["id"]), "kind": str(row["kind"]),
        "fpga": str(row["fpga"]),
        "clock_domain": str(row["clock_domain"]),
        "clock_period_ns": float(row["clock_period_ns"]),
        "start_pin": str(row["start_pin"]), "end_pin": str(row["end_pin"]),
        "delay_ns": None if row["delay_ns"] is None else float(row["delay_ns"]),
    }
    if "optimization_source" in row.keys() and row["optimization_source"] is not None:
        record["optimization_source"] = str(row["optimization_source"])
    if row["required_time_ns"] is not None:
        record.update({
            "required_time_ns": float(row["required_time_ns"]),
            "measurement": str(row["measurement"]),
            "path_pins": list(pins or ()),
        })
    return record


def _iter_records(connection: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    pin_cursor = iter(connection.execute(
        "SELECT path_id,ordinal,pin FROM path_pins ORDER BY path_id,ordinal"
    ))
    pending = next(pin_cursor, None)
    for row in connection.execute("SELECT * FROM timings ORDER BY id"):
        identity = str(row["id"])
        pins: list[str] = []
        while pending is not None and str(pending["path_id"]) == identity:
            if int(pending["ordinal"]) != len(pins):
                raise ValidationError("local path timing pin order is invalid")
            pins.append(str(pending["pin"]))
            pending = next(pin_cursor, None)
        if pending is not None and str(pending["path_id"]) < identity:
            raise ValidationError("local path timing pin owner is invalid")
        yield _record_from_row(row, pins)
    if pending is not None:
        raise ValidationError("local path timing pin owner is invalid")

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
            raise ValidationError("local path timing lock is a symbolic link")
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
        raise ValidationError("local path timing selector is malformed")
    if (
        raw.get("schema") not in {LOCAL_PATH_TIMING_STORAGE_SCHEMA_V1, LOCAL_PATH_TIMING_STORAGE_SCHEMA}
        or raw.get("logical_schema") != LOCAL_PATH_TIMING_SCHEMA
        or raw.get("application_id") != LOCAL_PATH_TIMING_APPLICATION_ID
        or raw.get("user_version") != (1 if raw.get("schema")==LOCAL_PATH_TIMING_STORAGE_SCHEMA_V1 else LOCAL_PATH_TIMING_USER_VERSION)
    ):
        raise ValidationError("unsupported local path timing selector")
    counts = raw.get("counts")
    if (
        not isinstance(counts, dict)
        or set(counts) != _COUNT_KEYS
        or any(not _strict_nonnegative_int(value) for value in counts.values())
    ):
        raise ValidationError("local path timing selector counts are invalid")
    _validate_header(raw.get("header"), counts)
    artifact = raw.get("artifact")
    if (
        not _valid_digest(raw.get("logical_sha256"))
        or not isinstance(artifact, str)
        or Path(artifact).name != artifact
        or not _strict_nonnegative_int(raw.get("store_bytes"))
        or not _valid_digest(raw.get("store_sha256"))
    ):
        raise ValidationError("local path timing selector artifact is invalid")
    return raw


def _resolve_store(selector_path: Path, selector: Mapping[str, Any]) -> Path:
    candidate = selector_path.parent / str(selector["artifact"])
    if candidate.name != _store_name(selector_path, str(selector["store_sha256"])):
        raise ValidationError("local path timing artifact owner disagrees")
    if candidate.is_symlink():
        raise ValidationError("local path timing store is a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise ValidationError("local path timing store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("local path timing store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("local path timing store seal disagrees")
    return resolved


class _TimingSequence(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "LocalPathTimingView", count: int):
        self._owner = weakref.ref(owner)
        self._count = count

    def _view(self) -> "LocalPathTimingView":
        owner = self._owner()
        if owner is None:
            raise ValidationError("local path timing view is closed")
        owner._ensure_open()
        return owner

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        with _connect(self._view().store_path) as connection:
            yield from _iter_records(connection)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(self._count))]
        if not isinstance(index, int):
            raise TypeError("local path timing index must be an integer")
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        with _connect(self._view().store_path) as connection:
            row = connection.execute(
                "SELECT * FROM timings ORDER BY id LIMIT 1 OFFSET ?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("local path timing path is missing")
            pins = [
                str(item[0]) for item in connection.execute(
                    "SELECT pin FROM path_pins WHERE path_id=? ORDER BY ordinal",
                    (str(row["id"]),),
                )
            ]
            return _record_from_row(row, pins)


class LocalPathTimingView(Mapping[str, Any]):
    def __init__(self, selector_path: Path, *, _lease: Optional[_Lease] = None):
        self._lease = _lease or _Lease(selector_path, exclusive=False)
        self._closed = False
        try:
            if selector_path.is_symlink() or not selector_path.is_file():
                raise ValidationError("local path timing selector is missing")
            self.selector_path = selector_path.resolve()
            self.selector = _validate_selector(read_json(selector_path))
            self.store_path = _resolve_store(selector_path, self.selector)
            self._validate_store()
            self._paths = _TimingSequence(
                self, self.selector["counts"]["local_paths"]
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls, selector_path: Path, selector: dict[str, Any], store_path: Path,
        lease: _Lease,
    ) -> "LocalPathTimingView":
        value = cls.__new__(cls)
        value._lease = lease
        value._closed = False
        value.selector_path = selector_path.resolve()
        value.selector = selector
        value.store_path = store_path.resolve()
        value._paths = _TimingSequence(
            value, selector["counts"]["local_paths"]
        )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError("local path timing view is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self._lease.close()

    def __enter__(self) -> "LocalPathTimingView":
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
                    raise ValidationError("local path timing store version disagrees with selector")
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
                    raise ValidationError("local path timing metadata disagrees")
                count = 0
                previous = None
                maximum = 0.0
                measurement_counts = {
                    "explicit-routed-path-chain": 0,
                    "endpoint-longest-path-fallback": 0, OPTIMIZED_AWAY: 0,
                }
                for record in _iter_records(connection):
                    record = _normalize_record(record, self.selector["header"])
                    if previous is not None and record["id"] <= previous:
                        raise ValidationError("local path timing order is invalid")
                    previous = record["id"]
                    count += 1
                    if record["delay_ns"] is not None:
                        maximum = max(maximum, record["delay_ns"])
                    if record.get("measurement") in measurement_counts:
                        measurement_counts[record["measurement"]] += 1
                    digest.update(_canonical(record).encode("utf-8"))
                    digest.update(b"\n")
                sequence = tuple(connection.execute(
                    "SELECT MIN(seq),MAX(seq),COUNT(DISTINCT seq) FROM timings"
                ).fetchone())
        except (sqlite3.DatabaseError, ValueError, TypeError) as error:
            raise ValidationError("local path timing store is corrupt") from error
        counts = {"local_paths": count}
        if (
            (count and sequence != (0, count - 1, count))
            or (not count and any(item is not None for item in sequence[:2]))
        ):
            raise ValidationError("local path timing sequence is incomplete")
        if counts != self.selector["counts"]:
            raise ValidationError("local path timing store counts disagree")
        logical = hashlib.sha256(_canonical({
            "header": self.selector["header"],
            "counts": counts,
            "path_sha256": digest.hexdigest(),
        }).encode("utf-8")).hexdigest()
        if logical != self.selector["logical_sha256"]:
            raise ValidationError("local path timing logical seal disagrees")
        self._maximum_delay_ns = maximum
        self._measurement_counts = (
            measurement_counts
            if self.selector["header"]["identity_schema"]
            == "emuflow.local-path-identity/v2"
            else {}
        )

    def validate_timing(self) -> dict[str, Any]:
        self._ensure_open()
        return {
            "status": "pass", "fpga": self.selector["header"]["fpga"],
            "local_paths": self.selector["counts"]["local_paths"],
            "maximum_delay_ns": self._maximum_delay_ns,
            "optimized_away_paths": self._measurement_counts.get(OPTIMIZED_AWAY, 0),
            "implemented_paths": self.selector["counts"]["local_paths"]-self._measurement_counts.get(OPTIMIZED_AWAY, 0),
            "measurement_counts": dict(self._measurement_counts),
        }

    def __getitem__(self, key: str) -> Any:
        self._ensure_open()
        if key == "paths":
            return self._paths
        return self.selector["header"][key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        yield from self.selector["header"]
        yield "paths"

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


def write_local_path_timing_storage(
    path: Path,
    header: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
) -> LocalPathTimingView:
    """Atomically publish one typed local-path timing generation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    base_header = _validate_header(dict(header))
    lease: Optional[_Lease] = _Lease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    published_store: Optional[Path] = None
    created_store = False
    committed = False
    connection: Optional[sqlite3.Connection] = None
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValidationError("local path timing selector is invalid")
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
        connection.execute(f"PRAGMA application_id={LOCAL_PATH_TIMING_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={LOCAL_PATH_TIMING_USER_VERSION}")
        connection.executescript(_DDL)
        timing_rows: list[tuple[Any, ...]] = []
        pin_rows: list[tuple[Any, ...]] = []
        count = 0

        def flush() -> None:
            if timing_rows:
                connection.executemany(
                    "INSERT INTO timings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    timing_rows,
                )
                timing_rows.clear()
            if pin_rows:
                connection.executemany(
                    "INSERT INTO path_pins VALUES (?,?,?)", pin_rows
                )
                pin_rows.clear()

        try:
            for raw in records:
                record = _normalize_record(raw, base_header)
                timing_rows.append((
                    count, record["id"], record["kind"], record["fpga"],
                    record["clock_domain"], record["clock_period_ns"],
                    record.get("required_time_ns"), record["start_pin"],
                    record["end_pin"], record.get("measurement"),
                    record["delay_ns"], record.get("optimization_source"),
                ))
                pin_rows.extend(
                    (record["id"], ordinal, pin)
                    for ordinal, pin in enumerate(record.get("path_pins", ()))
                )
                count += 1
                if len(timing_rows) >= 8192 or len(pin_rows) >= 8192:
                    flush()
            flush()
        except sqlite3.IntegrityError as error:
            raise ValidationError("local path timing path is duplicated") from error
        counts = {"local_paths": count}
        normalized_header = _validate_header(base_header, counts)
        path_digest = hashlib.sha256()
        for record in _iter_records(connection):
            path_digest.update(_canonical(record).encode("utf-8"))
            path_digest.update(b"\n")
        logical_sha = hashlib.sha256(_canonical({
            "header": normalized_header, "counts": counts,
            "path_sha256": path_digest.hexdigest(),
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
            raise ValidationError("local path timing generation is a symlink")
        if published_store.is_file():
            if file_sha256(published_store) != store_sha:
                raise ValidationError("local path timing generation seal disagrees")
            temporary.unlink()
        else:
            os.replace(temporary, published_store)
            created_store = True
            _fsync_directory(path.parent)
        selector = {
            "schema": LOCAL_PATH_TIMING_STORAGE_SCHEMA,
            "logical_schema": LOCAL_PATH_TIMING_SCHEMA,
            "application_id": LOCAL_PATH_TIMING_APPLICATION_ID,
            "user_version": LOCAL_PATH_TIMING_USER_VERSION,
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
        view = LocalPathTimingView._from_publication(
            path, selector, published_store, lease
        )
        view._validate_store()
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

def read_local_path_timing_storage(
    path: Path,
    *,
    allow_legacy: bool = False,
    expected_design: Any = None,
    expected_fpga: Any = None,
    expected_selector_bytes: Any = None,
    expected_selector_sha256: Any = None,
) -> Mapping[str, Any]:
    if expected_selector_bytes is not None and (
        isinstance(expected_selector_bytes, bool)
        or not isinstance(expected_selector_bytes, int)
        or path.stat().st_size != expected_selector_bytes
    ):
        raise ValidationError("local path timing selector byte count disagrees")
    if expected_selector_sha256 is not None and (
        not _valid_digest(expected_selector_sha256)
        or file_sha256(path) != expected_selector_sha256
    ):
        raise ValidationError("local path timing selector digest disagrees")
    value = read_json(path)
    if value.get("schema") in {LOCAL_PATH_TIMING_STORAGE_SCHEMA, LOCAL_PATH_TIMING_STORAGE_SCHEMA_V1}:
        view = LocalPathTimingView(path)
        if any(
            expected is not None and view.get(field) != expected
            for field, expected in (
                ("design", expected_design),
                ("fpga", expected_fpga),
            )
        ):
            view.close()
            raise ValidationError("local path timing indexed evidence source disagrees")
        return view
    if not allow_legacy or value.get("schema") != LOCAL_PATH_TIMING_SCHEMA:
        raise ValidationError(
            "legacy local path timing JSON requires explicit compatibility"
        )
    if any(
        expected is not None and value.get(field) != expected
        for field, expected in (
            ("design", expected_design),
            ("fpga", expected_fpga),
        )
    ):
        raise ValidationError("local path timing legacy evidence source disagrees")
    return value


def local_path_timing_reference(
    selector_path: Path, *, relative_to: Path
) -> dict[str, Any]:
    if selector_path.is_symlink() or not selector_path.is_file():
        raise ValidationError("local path timing selector is missing")
    _validate_selector(read_json(selector_path))
    resolved = selector_path.resolve()
    relative = Path(os.path.relpath(resolved, relative_to.resolve()))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError("local path timing selector escapes its summary")
    return {
        "schema": "emuflow.physical-evidence-reference/v2",
        "logical_schema": LOCAL_PATH_TIMING_SCHEMA,
        "path": str(relative),
        "bytes": resolved.stat().st_size,
        "sha256": file_sha256(resolved),
    }


def publish_local_path_timing_selector(
    source: Path, output_directory: Path, relative_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Relocate one typed selector and immutable generation without expansion."""

    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValidationError("local path timing relative path is unsafe")
    target = output_directory / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        with LocalPathTimingView(source) as view:
            selector = dict(view.selector)
        return selector, local_path_timing_reference(
            target, relative_to=output_directory
        )
    with LocalPathTimingView(source) as source_view:
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
                raise ValidationError("local path timing generation is a symlink")
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
                        "local path timing copied generation seal disagrees"
                    )
                os.replace(temporary_store, destination_store)
                temporary_store = None
                _fsync_directory(target.parent)
            elif (
                destination_store.stat().st_size != selector["store_bytes"]
                or file_sha256(destination_store) != selector["store_sha256"]
            ):
                raise ValidationError(
                    "local path timing destination generation seal disagrees"
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
    return selector, local_path_timing_reference(
        target, relative_to=output_directory
    )
