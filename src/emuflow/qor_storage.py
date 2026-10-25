"""Selector-backed typed owner for the canonical Phase 7C QoR report."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import fcntl
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Optional

from .errors import ValidationError
from .store_publication import (
    StoreSource,
    bind_store_source,
    publish_sqlite_store,
    require_store_sources_unchanged,
    validate_store_destination,
)
from .io import file_sha256, write_json
from .runtime import QOR_REPORT_SCHEMA


QOR_REPORT_STORAGE_SCHEMA = "emuflow.qor-report-storage/v1"
QOR_REPORT_APPLICATION_ID = 0x454D5551  # ASCII "EMUQ"
QOR_REPORT_USER_VERSION = 1

_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version", "header",
    "counts", "logical_sha256", "artifact", "store_bytes", "store_sha256",
}
_TYPED_FIELDS = {
    "path", "representative_path", "path_scope", "clock_domain",
    "target_period_ns", "physical_logic_segments_cone_bound",
    "interconnect_delay_model", "system_delay_bound_ns",
    "target_required_time_ns", "target_clock_slack_bound_ns",
    "runtime_required_time_ns", "runtime_clock_slack_bound_ns",
}
_REQUIRED_NUMBERS = (
    "system_delay_bound_ns", "target_required_time_ns",
    "target_clock_slack_bound_ns", "runtime_required_time_ns",
    "runtime_clock_slack_bound_ns",
)

_DDL = (Path(__file__).parent / "data/schema/qor_report.sql").read_text()


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValidationError("QoR value is not canonical JSON") from error


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_count(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


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


def _exact_sqlite_envelope(connection: sqlite3.Connection) -> None:
    if (
        connection.execute("PRAGMA application_id").fetchone()[0]
        != QOR_REPORT_APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0]
        != QOR_REPORT_USER_VERSION
    ):
        raise ValidationError("unsupported QoR report store")
    objects = tuple(
        (str(row[0]), str(row[1]), str(row[2]), _normalized_sql(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )
    if objects != _expected_sqlite_objects():
        raise ValidationError("QoR report store objects are invalid")


def _validate_header(raw: Any, count: Optional[int] = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("QoR report header is malformed")
    header = dict(raw)
    timing = header.get("timing")
    if (
        header.get("schema") != QOR_REPORT_SCHEMA
        or not isinstance(timing, Mapping)
        or "paths" in timing
        or timing.get("schema") != "emuflow.system-timing/v2"
        or not isinstance(timing.get("summary"), Mapping)
    ):
        raise ValidationError("QoR report header is invalid")
    timing = dict(timing)
    implementation = timing.get('global_opensta', {}).get('implementation_id')
    if implementation not in (None, 'native-global-measurements-v1'):
        raise ValidationError('unsupported native QoR implementation')
    header["timing"] = timing
    if count is not None and timing["summary"].get("timing_paths") != count:
        raise ValidationError("QoR report path count disagrees")
    _canonical(header)
    return header


def _optional_string(record: Mapping[str, Any], key: str) -> Optional[str]:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError(f"QoR timing path {key} is invalid")
    return value


def _number(record: Mapping[str, Any], key: str, *, optional: bool = False):
    value = record.get(key)
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValidationError(f"QoR timing path {key} is invalid")
    return 0.0 if value == 0 else float(value)


def _normalize_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValidationError("QoR timing path is malformed")
    record = dict(raw)
    path = record.get("path")
    if not isinstance(path, str) or not path:
        raise ValidationError("QoR timing path identity is invalid")
    for key in _REQUIRED_NUMBERS:
        record[key] = _number(record, key)
    for key in (
        "representative_path", "path_scope", "clock_domain",
        "interconnect_delay_model",
    ):
        if key in record:
            record[key] = _optional_string(record, key)
    if record.get("path_scope") not in {
        None, "cross-fpga", "same-fpga-local",
    }:
        raise ValidationError("QoR timing path scope is invalid")
    if "target_period_ns" in record:
        record["target_period_ns"] = _number(
            record, "target_period_ns", optional=True
        )
    if "physical_logic_segments_cone_bound" in record:
        cone = record["physical_logic_segments_cone_bound"]
        if cone is not None and not isinstance(cone, bool):
            raise ValidationError("QoR timing path cone-bound flag is invalid")
    if record["system_delay_bound_ns"] < 0.0:
        raise ValidationError("QoR system delay is negative")
    extra = {key: value for key, value in record.items() if key not in _TYPED_FIELDS}
    _canonical(extra)
    return record


def _record_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        extra = json.loads(row["extra_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValidationError("QoR timing path extension is invalid") from error
    if not isinstance(extra, dict) or set(extra) & _TYPED_FIELDS:
        raise ValidationError("QoR timing path extension is malformed")
    record = {
        "path": row["path"],
        "system_delay_bound_ns": row["system_delay_bound_ns"],
        "target_required_time_ns": row["target_required_time_ns"],
        "target_clock_slack_bound_ns": row["target_clock_slack_bound_ns"],
        "runtime_required_time_ns": row["runtime_required_time_ns"],
        "runtime_clock_slack_bound_ns": row["runtime_clock_slack_bound_ns"],
        **extra,
    }
    optional = (
        ("representative_path", "representative_path"),
        ("path_scope", "path_scope"),
        ("clock_domain", "clock_domain"),
        ("target_period_ns", "target_period_ns"),
        ("physical_logic_segments_cone_bound", "cone_bound"),
        ("interconnect_delay_model", "interconnect_delay_model"),
    )
    presence = row["typed_presence"]
    if isinstance(presence, bool) or not isinstance(presence, int) or presence >> len(optional):
        raise ValidationError("QoR timing path presence map is invalid")
    for bit, (key, column) in enumerate(optional):
        if presence & (1 << bit):
            record[key] = (
                bool(row[column])
                if key == "physical_logic_segments_cone_bound"
                and row[column] is not None
                else row[column]
            )
    return _normalize_record(record)


def _raw_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("QoR report selector is unreadable") from error
    if not isinstance(value, dict):
        raise ValidationError("QoR report selector is malformed")
    return value


def _validate_selector(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _SELECTOR_KEYS:
        raise ValidationError("QoR report selector is malformed")
    selector = dict(raw)
    counts = selector.get("counts")
    if (
        selector.get("schema") != QOR_REPORT_STORAGE_SCHEMA
        or selector.get("logical_schema") != QOR_REPORT_SCHEMA
        or selector.get("application_id") != QOR_REPORT_APPLICATION_ID
        or selector.get("user_version") != QOR_REPORT_USER_VERSION
        or not isinstance(counts, Mapping)
        or set(counts) != {"timing_paths"}
        or not _strict_count(counts.get("timing_paths"))
        or not _valid_digest(selector.get("logical_sha256"))
        or not isinstance(selector.get("artifact"), str)
        or not selector["artifact"]
        or Path(selector["artifact"]).name != selector["artifact"]
        or not _strict_count(selector.get("store_bytes"))
        or not _valid_digest(selector.get("store_sha256"))
    ):
        raise ValidationError("QoR report selector is invalid")
    selector["counts"] = dict(counts)
    selector["header"] = _validate_header(
        selector.get("header"), counts["timing_paths"]
    )
    return selector


def _store_name(path: Path, digest: str) -> str:
    return f"{path.stem}.{digest}.sqlite"


def _resolve_store(path: Path, selector: Mapping[str, Any]) -> tuple[Path, StoreSource]:
    if path.is_symlink():
        raise ValidationError("QoR report selector is a symbolic link")
    candidate = path.parent / str(selector["artifact"])
    if candidate.name != _store_name(path, str(selector["store_sha256"])):
        raise ValidationError("QoR report artifact identity disagrees")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValidationError("QoR report store is missing")
    resolved = candidate.resolve()
    if resolved.parent != path.parent.resolve():
        raise ValidationError("QoR report store escapes its selector")
    store_source = bind_store_source(resolved, "qor-report-store-v1")
    stat = resolved.stat()
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("QoR report store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return resolved, store_source


class _Lease:
    def __init__(self, path: Path, *, exclusive: bool):
        lock = path.parent.resolve() / f".{path.name}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        self._stream = lock.open("a+b")
        fcntl.flock(
            self._stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        )

    def downgrade_shared(self) -> None:
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_SH)

    def close(self) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


class _TimingPathRows(Sequence[Mapping[str, Any]]):
    def __init__(self, owner: "QorReportView"):
        self._owner = owner

    def __len__(self) -> int:
        self._owner._ensure_open()
        return self._owner.selector["counts"]["timing_paths"]

    def __getitem__(self, index):
        self._owner._ensure_open()
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        row = self._owner.connection.execute(
            "SELECT * FROM timing_paths WHERE seq=?", (index,)
        ).fetchone()
        if row is None:
            raise ValidationError("QoR timing path is missing")
        return _record_from_row(row)

    def __iter__(self):
        self._owner._ensure_open()
        cursor = self._owner.connection.execute(
            "SELECT * FROM timing_paths ORDER BY seq"
        )
        for row in cursor:
            yield _record_from_row(row)


class _SystemTimingView(Mapping[str, Any]):
    def __init__(self, owner: "QorReportView"):
        self._owner = owner
        self._header = owner.selector["header"]["timing"]
        self._paths = _TimingPathRows(owner)

    def __getitem__(self, key: str):
        self._owner._ensure_open()
        if key == "paths":
            return self._paths
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        self._owner._ensure_open()
        yield from self._header
        yield "paths"

    def __len__(self) -> int:
        return len(self._header) + 1


class QorReportView(Mapping[str, Any]):
    def __init__(self, path: Path, *, _lease: Optional[_Lease] = None):
        self._lease = _lease or _Lease(path, exclusive=False)
        self.connection: Optional[sqlite3.Connection] = None
        try:
            if not path.is_file() or path.is_symlink():
                raise ValidationError("QoR report selector is missing")
            self.path = path.resolve()
            selector_source = bind_store_source(self.path, "qor-report-selector-v1")
            self.selector = _validate_selector(_raw_json(path))
            self.store_path, store_source = _resolve_store(path, self.selector)
            self._source_bindings = (selector_source, store_source)
            self.connection = sqlite3.connect(
                f"file:{self.store_path}?mode=ro", uri=True
            )
            self.connection.row_factory = sqlite3.Row
            self._timing = _SystemTimingView(self)
            self._validate_store()
            require_store_sources_unchanged(self._source_bindings)
        except BaseException:
            self.close()
            raise

    @classmethod
    def _from_publication(
        cls,
        path: Path,
        selector: dict[str, Any],
        store: Path,
        lease: _Lease,
    ) -> "QorReportView":
        value = cls.__new__(cls)
        value._lease = lease
        value.path = path.resolve()
        value.selector = selector
        value.store_path = store.resolve()
        value._source_bindings = (
            bind_store_source(value.path, "qor-report-selector-v1"),
            bind_store_source(value.store_path, "qor-report-store-v1"),
        )
        value.connection = sqlite3.connect(
            f"file:{value.store_path}?mode=ro", uri=True
        )
        value.connection.row_factory = sqlite3.Row
        value._timing = _SystemTimingView(value)
        return value

    def bound_store_sources(self):
        self._ensure_open()
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def _ensure_open(self) -> None:
        if self.connection is None:
            raise ValidationError("QoR report view is closed")

    def _validate_store(self) -> None:
        self._ensure_open()
        if self.selector['header']['timing'].get('global_opensta', {}).get('implementation_id') == 'native-global-measurements-v1':
            from .global_measurements import validate_native_qor
            validate_native_qor(self.store_path, self.selector['header'], self.selector['logical_sha256'])
            return
        assert self.connection is not None
        try:
            _exact_sqlite_envelope(self.connection)
            metadata = {
                str(row[0]): json.loads(row[1])
                for row in self.connection.execute(
                    "SELECT key,value FROM metadata ORDER BY key"
                )
            }
            if set(metadata) != {"counts", "header", "logical_sha256"}:
                raise ValidationError("QoR report metadata is malformed")
            count = 0
            previous = None
            digest = hashlib.sha256()
            for row in self.connection.execute(
                "SELECT * FROM timing_paths ORDER BY seq"
            ):
                if row["seq"] != count:
                    raise ValidationError("QoR timing path sequence is incomplete")
                record = _record_from_row(row)
                if previous is not None and previous >= record["path"]:
                    raise ValidationError("QoR timing path order is invalid")
                previous = record["path"]
                digest.update(_canonical(record).encode("utf-8"))
                digest.update(b"\n")
                count += 1
            counts = {"timing_paths": count}
            logical = hashlib.sha256(
                _canonical({
                    "header": self.selector["header"],
                    "counts": counts,
                    "paths_sha256": digest.hexdigest(),
                }).encode("utf-8")
            ).hexdigest()
        except (sqlite3.DatabaseError, TypeError, ValueError) as error:
            if isinstance(error, ValidationError):
                raise
            raise ValidationError("QoR report store is corrupt") from error
        if (
            metadata["counts"] != counts
            or metadata["header"] != self.selector["header"]
            or metadata["logical_sha256"] != logical
            or self.selector["counts"] != counts
            or self.selector["logical_sha256"] != logical
        ):
            raise ValidationError("QoR report store metadata disagrees")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def __enter__(self) -> "QorReportView":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __getitem__(self, key: str):
        self._ensure_open()
        if key == "timing":
            return self._timing
        return self.selector["header"][key]

    def __iter__(self) -> Iterator[str]:
        self._ensure_open()
        return iter(self.selector["header"])

    def __len__(self) -> int:
        return len(self.selector["header"])


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _collect_stale(path: Path, keep: Path) -> None:
    prefix = f"{path.stem}."
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


def write_qor_report(
    path: Path, report: Mapping[str, Any], *, source_bindings=()
) -> QorReportView:
    """Publish a bounded QoR selector and one typed per-path generation."""

    source_bindings = validate_store_destination(path, source_bindings)
    logical = dict(report)
    timing = logical.get("timing")
    if not isinstance(timing, Mapping):
        raise ValidationError("QoR report timing is malformed")
    timing_header = dict(timing)
    paths = timing_header.pop("paths", None)
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes, bytearray)):
        raise ValidationError("QoR report paths are malformed")
    header = dict(logical)
    header["timing"] = timing_header
    header = _validate_header(header, len(paths))

    path.parent.mkdir(parents=True, exist_ok=True)
    lease: Optional[_Lease] = _Lease(path, exclusive=True)
    temporary: Optional[Path] = None
    selector_temporary: Optional[Path] = None
    published: Optional[Path] = None
    connection: Optional[sqlite3.Connection] = None
    created = False
    committed = False
    try:
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
        from .global_measurements import NativeTimingPaths
        native_paths = isinstance(paths, NativeTimingPaths)
        if native_paths:
            source_bindings = validate_store_destination(path, (*source_bindings, *paths.bound_store_sources()))
            counts, logical_sha = paths.publish_store(temporary, header)
        else:
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute(f"PRAGMA application_id={QOR_REPORT_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={QOR_REPORT_USER_VERSION}")
            connection.executescript(_DDL)
            pending = []
            digest = hashlib.sha256()
            previous = None
            for seq, raw in enumerate(paths):
                record = _normalize_record(raw)
                if previous is not None and previous >= record["path"]:
                    raise ValidationError("QoR timing paths are not strictly ordered")
                previous = record["path"]
                digest.update(_canonical(record).encode("utf-8"))
                digest.update(b"\n")
                extra = {
                    key: value for key, value in record.items()
                    if key not in _TYPED_FIELDS
                }
                optional_fields = (
                    "representative_path", "path_scope", "clock_domain",
                    "target_period_ns", "physical_logic_segments_cone_bound",
                    "interconnect_delay_model",
                )
                presence = sum(
                    1 << bit for bit, key in enumerate(optional_fields)
                    if key in record
                )
                pending.append((
                    seq, record["path"], presence,
                    record.get("representative_path"),
                    record.get("path_scope"), record.get("clock_domain"),
                    record.get("target_period_ns"),
                    (
                        None
                        if record.get("physical_logic_segments_cone_bound") is None
                        else int(record["physical_logic_segments_cone_bound"])
                    ),
                    record.get("interconnect_delay_model"),
                    record["system_delay_bound_ns"],
                    record["target_required_time_ns"],
                    record["target_clock_slack_bound_ns"],
                    record["runtime_required_time_ns"],
                    record["runtime_clock_slack_bound_ns"],
                    _canonical(extra),
                ))
                if len(pending) >= 8192:
                    connection.executemany(
                        "INSERT INTO timing_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        pending,
                    )
                    pending.clear()
            if pending:
                connection.executemany(
                    "INSERT INTO timing_paths VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    pending,
                )
            counts = {"timing_paths": len(paths)}
            logical_sha = hashlib.sha256(
                _canonical({
                    "header": header,
                    "counts": counts,
                    "paths_sha256": digest.hexdigest(),
                }).encode("utf-8")
            ).hexdigest()
            for key, value in (
                ("counts", counts), ("header", header),
                ("logical_sha256", logical_sha),
            ):
                connection.execute(
                    "INSERT INTO metadata VALUES (?,?)", (key, _canonical(value))
                )
            connection.commit()
            connection.close()
            connection = None
        store_sha = file_sha256(temporary)
        published = path.with_name(_store_name(path, store_sha))
        if published.is_symlink():
            raise ValidationError("QoR report generation is a symbolic link")
        require_store_sources_unchanged(source_bindings)
        if published.is_file():
            if file_sha256(published) != store_sha:
                raise ValidationError("QoR report generation seal disagrees")
            temporary.unlink()
        else:
            publish_sqlite_store(
                temporary, published, "qor-report-store-v1", sources=source_bindings
            )
            created = True
        selector = _validate_selector({
            "schema": QOR_REPORT_STORAGE_SCHEMA,
            "logical_schema": QOR_REPORT_SCHEMA,
            "application_id": QOR_REPORT_APPLICATION_ID,
            "user_version": QOR_REPORT_USER_VERSION,
            "header": header,
            "counts": counts,
            "logical_sha256": logical_sha,
            "artifact": published.name,
            "store_bytes": published.stat().st_size,
            "store_sha256": store_sha,
        })
        write_json(selector_temporary, selector, compact=True)
        with selector_temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        require_store_sources_unchanged(source_bindings)
        os.replace(selector_temporary, path)
        committed = True
        _fsync_directory(path.parent)
        try:
            _collect_stale(path, published)
        except OSError:
            pass
        assert lease is not None
        lease.downgrade_shared()
        view = QorReportView._from_publication(path, selector, published, lease)
        if not native_paths:
            view._validate_store()
        lease = None
        return view
    except sqlite3.IntegrityError as error:
        raise ValidationError("QoR timing path identity is duplicated") from error
    except BaseException:
        if published is not None and created and not committed:
            published.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if selector_temporary is not None:
            selector_temporary.unlink(missing_ok=True)
        if lease is not None:
            lease.close()


def read_qor_report(path: Path, *, allow_legacy: bool = False) -> Mapping[str, Any]:
    raw = _raw_json(path)
    if raw.get("schema") == QOR_REPORT_STORAGE_SCHEMA:
        return QorReportView(path)
    if allow_legacy and raw.get("schema") == QOR_REPORT_SCHEMA:
        return raw
    raise ValidationError("legacy QoR report requires explicit compatibility")


def expand_qor_report_storage(
    selector: Mapping[str, Any], path: Path
) -> dict[str, Any]:
    _validate_selector(selector)
    with QorReportView(path) as view:
        result = dict(view.selector["header"])
        timing = dict(result["timing"])
        timing["paths"] = list(view["timing"]["paths"])
        result["timing"] = timing
        return result


def qor_validation_proof(path: Path):
    """Bounded header plus independently validated ordered-record identity.

    Current model-only reports have no per-path population. Physical reports
    use the typed owner; a historical full JSON timing tree is not inferred.
    """
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValidationError("QoR selector/header exceeds bound")
    raw = _raw_json(path)
    if raw.get('schema') == QOR_REPORT_STORAGE_SCHEMA:
        with read_qor_report(path) as owner:
            header = owner.selector['header']
            proof = (header, owner.selector['counts'], owner.selector['logical_sha256'])
    elif (raw.get('schema') == QOR_REPORT_SCHEMA and isinstance(raw.get('timing'), dict)
          and 'paths' not in raw['timing']
          and raw['timing'].get('global_opensta', {}).get('execution') != 'standalone'):
        header, proof = raw, (raw, None, None)
    else:
        raise ValidationError("QoR path population requires the current typed owner")
    return proof, header.get('timing', {}).get('global_opensta', {}).get('execution') == 'standalone'
