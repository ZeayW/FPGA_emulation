"""Typed owner for routed logic-segment timing evidence.

The JSON path is a bounded, atomic selector.  Segment, unsupported-member and
unmeasured rows live in one immutable content-addressed SQLite generation.
Readers hold a shared lease until every cursor is closed; writers commit the
new selector last under an exclusive lease and collect stale generations only
after the commit is durable.
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Iterator, Mapping, Sequence
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Optional
import weakref

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource, bind_store_source, publish_sqlite_store, require_store_sources_unchanged, validate_store_destination
from emuflow.common.json_io import file_sha256, write_json
from emuflow_tools.document_io import read_json


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
        or set(counts) not in (_COUNT_KEYS, _COUNT_KEYS | {"endpoint_collection_bound_segments"})
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


def _resolve_store(
    selector_path: Path, selector: Mapping[str, Any]
) -> tuple[Path, StoreSource]:
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
        store_source = bind_store_source(resolved, "logic-segment-timing-store-v1")
    except OSError as error:
        raise ValidationError("logic segment timing store is missing") from error
    if resolved.parent != selector_path.parent.resolve():
        raise ValidationError("logic segment timing store escapes its selector")
    if (
        stat.st_size != selector["store_bytes"]
        or file_sha256(resolved) != selector["store_sha256"]
    ):
        raise ValidationError("logic segment timing store seal disagrees")
    require_store_sources_unchanged((store_source,))
    return resolved, store_source








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
        with closing(_connect(view.store_path)) as connection, connection:
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
        with closing(_connect(view.store_path)) as connection, connection:
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
            selector_source = bind_store_source(
                selector_path, "logic-segment-timing-selector-v1"
            )
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
            self.store_path, store_source = _resolve_store(selector_path, self.selector)
            self._source_bindings = (selector_source, store_source)
            self._validate_store()
            require_store_sources_unchanged(self._source_bindings)
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
        value._source_bindings = (
            bind_store_source(value.selector_path, "logic-segment-timing-selector-v1"),
            bind_store_source(value.store_path, "logic-segment-timing-store-v1"),
        )
        counts = selector["counts"]
        value._segments = _StoreSequence(value, "segments", counts["segments"])
        value._unsupported = _StoreSequence(
            value, "unsupported_members", counts["unsupported_member_paths"]
        )
        value._unmeasured = _StoreSequence(
            value, "unmeasured_segments", counts["unmeasured_segments"]
        )
        return value

    def bound_store_sources(self):
        self._ensure_open()
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

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
        from emuflow_tools.physical.physical_evidence_native import validate_physical_evidence
        result = validate_physical_evidence(
            self.store_path, self.selector, self._source_bindings,
            "logic-segment-timing-store-v1",
        )
        self._maximum_delay_ns = result["maximum_delay_ns"]

    def validate_timing(self) -> dict[str, Any]:
        self._ensure_open()
        counts = self.selector["counts"]
        require_store_sources_unchanged(self._source_bindings)
        maximum = self._maximum_delay_ns
        return {
            "status": "pass", "fpga": self.selector["source"]["fpga"],
            "segments": counts["segments"],
            "endpoint_exact_segments": counts["endpoint_exact_segments"],
            "cone_bound_segments": counts["cone_bound_segments"],
            "endpoint_collection_bound_segments": counts.get("endpoint_collection_bound_segments", 0),
            "maximum_delay_ns": float(maximum),
        }

    def iter_segment_ids(self) -> Iterator[str]:
        self._ensure_open()
        with closing(_connect(self.store_path)) as connection, connection:
            for row in connection.execute(
                "SELECT segment_id FROM segments ORDER BY segment_id"
            ):
                yield str(row[0])

    def iter_ordered_segments(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        with closing(_connect(self.store_path)) as connection, connection:
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
) -> Mapping[str, Any]:
    """Open only the current typed evidence owner."""
    return LogicSegmentTimingView(
        path,
        expected_design=expected_design,
        expected_platform=expected_platform,
        expected_fpga=expected_fpga,
        expected_system_routes_sha256=expected_system_routes_sha256,
        expected_tdm_schedule_sha256=expected_tdm_schedule_sha256,
        expected_semantic_contract_sha256=expected_semantic_contract_sha256,
        expected_selector_bytes=expected_selector_bytes,
        expected_selector_sha256=expected_selector_sha256,
    )





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
        source_bindings = validate_store_destination(
            target, source_view.bound_store_sources()
        )
        assert isinstance(source_view, LogicSegmentTimingView)
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
                if (
                    temporary_store.stat().st_size != selector["store_bytes"]
                    or file_sha256(temporary_store) != selector["store_sha256"]
                ):
                    raise ValidationError(
                        "logic segment timing copied generation seal disagrees"
                    )
                publish_sqlite_store(
                    temporary_store,
                    destination_store,
                    "logic-segment-timing-store-v1",
                    sources=source_bindings,
                )
                created_store = True
                temporary_store = None
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
    return selector, logic_segment_timing_reference(
        target, relative_to=output_directory
    )
