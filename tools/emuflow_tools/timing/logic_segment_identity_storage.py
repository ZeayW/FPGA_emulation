"""Typed owner for logic-segment query identities.

The public ``*.json`` path is a small atomic selector.  The graph-sized
segment population lives in one immutable, content-addressed SQLite
generation, produced by the native physical-query owner. Readers keep a
shared owner lease while cursors are live.
"""

from __future__ import annotations

from contextlib import closing
from collections.abc import Iterator, Mapping, Sequence
import fcntl
import os
from pathlib import Path
import sqlite3
from typing import Any, Optional
import weakref

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.common.json_io import file_sha256
from emuflow_tools.document_io import read_json


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
_SELECTOR_KEYS = {
    "schema", "logical_schema", "application_id", "user_version", "header",
    "counts", "source", "logical_sha256", "artifact", "store_bytes",
    "store_sha256",
}


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_nonnegative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


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

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _validate_header(raw: Mapping[str, Any]) -> dict[str, Any]:
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
    if (
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
        from emuflow_tools.physical.physical_evidence_native import validate_physical_evidence
        validate_physical_evidence(self.store_path,self.selector,self._source_bindings,
                                   "logic-segment-identity-store-v1")

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


def read_logic_segment_identity_storage(
    path: Path,
    *,
    expected_design: Optional[str] = None,
    expected_platform: Optional[str] = None,
    expected_fpga: Optional[str] = None,
    expected_system_routes_sha256: Optional[str] = None,
    expected_tdm_schedule_sha256: Optional[str] = None,
    expected_semantic_contract_sha256: Optional[str] = None,
) -> Mapping[str, Any]:
    """Open only the current typed evidence owner."""
    return LogicSegmentIdentityView(
        path,
        expected_design=expected_design,
        expected_platform=expected_platform,
        expected_fpga=expected_fpga,
        expected_system_routes_sha256=expected_system_routes_sha256,
        expected_tdm_schedule_sha256=expected_tdm_schedule_sha256,
        expected_semantic_contract_sha256=expected_semantic_contract_sha256,
    )
