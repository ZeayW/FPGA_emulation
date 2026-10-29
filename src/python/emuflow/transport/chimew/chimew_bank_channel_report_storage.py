"""Typed storage for graph-sized Chimew bank/channel assignment reports.

The logical report remains ``emuflow.chimew-bank-channel-report/v1``.  The
production representation keeps its bounded header in a JSON selector and
stores the one-record-per-group assignment relation in a sealed SQLite
sidecar.  Readers hold a shared lease for the lifetime of every cursor, so a
concurrent selector replacement cannot unlink the generation being read.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.storage.store_publication import bind_store_source, require_store_sources_unchanged


REPORT_STORAGE_SCHEMA = "emuflow.chimew-bank-channel-report-storage/v1"
REPORT_APPLICATION_ID = 0x454D5552  # ASCII "EMUR"
REPORT_USER_VERSION = 1
_ASSIGNMENT_KEYS = {
    "group",
    "bank_pair",
    "channel",
    "bank_cost_rank",
    "channel_cost_rank",
    "bank_cost",
    "channel_cost",
}
def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _lock_path(selector_path: Path) -> Path:
    identity = hashlib.sha256(
        os.fsencode(Path(selector_path).resolve(strict=False))
    ).hexdigest()
    return Path(tempfile.gettempdir()) / "emuflow-bank-report-locks" / (
        f"{identity}.lock"
    )


@contextmanager
def _report_lease(selector_path: Path, *, exclusive: bool) -> Iterator[int]:
    lock_path = _lock_path(selector_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o666)
    try:
        fcntl.flock(
            descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        )
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}: expected a non-empty string")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label}: expected a non-negative integer")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValidationError(f"{label}: expected a finite non-negative number")
    return result


def _normalize_assignment(value: Mapping[str, Any], seq: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ASSIGNMENT_KEYS:
        raise ValidationError(
            f"Chimew bank/channel assignment {seq} has an invalid shape"
        )
    return {
        "group": _string(value.get("group"), f"assignment[{seq}].group"),
        "bank_pair": _string(
            value.get("bank_pair"), f"assignment[{seq}].bank_pair"
        ),
        "channel": _string(value.get("channel"), f"assignment[{seq}].channel"),
        "bank_cost_rank": _integer(
            value.get("bank_cost_rank"), f"assignment[{seq}].bank_cost_rank"
        ),
        "channel_cost_rank": _integer(
            value.get("channel_cost_rank"),
            f"assignment[{seq}].channel_cost_rank",
        ),
        "bank_cost": _number(
            value.get("bank_cost"), f"assignment[{seq}].bank_cost"
        ),
        "channel_cost": _number(
            value.get("channel_cost"), f"assignment[{seq}].channel_cost"
        ),
    }


def _row_value(row: sqlite3.Row, seq: int) -> dict[str, Any]:
    if isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] != seq:
        raise ValidationError("Chimew bank/channel report order is not contiguous")
    return _normalize_assignment(
        {
            "group": row[1],
            "bank_pair": row[2],
            "channel": row[3],
            "bank_cost_rank": row[4],
            "channel_cost_rank": row[5],
            "bank_cost": row[6],
            "channel_cost": row[7],
        },
        seq,
    )


class _AssignmentSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        count: int,
        *,
        order_by_group: bool = False,
        lease_state: Mapping[str, bool] | None = None,
    ):
        self.store_path = store_path
        self._count = count
        self._order_by_group = order_by_group
        self._lease_state = lease_state
        self._lookup: sqlite3.Connection | None = None

    def _ensure_open(self) -> None:
        if self._lease_state is not None and not self._lease_state["open"]:
            raise ValidationError("Chimew bank/channel report view is closed")

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        connection = _connect(self.store_path)
        try:
            order = "group_id" if self._order_by_group else "seq"
            previous_group: str | None = None
            observed = 0
            for row in connection.execute(
                "SELECT seq,group_id,bank_pair,channel,bank_cost_rank,"
                "channel_cost_rank,bank_cost,channel_cost FROM assignments "
                f"ORDER BY {order}"
            ):
                self._ensure_open()
                value = _row_value(row, int(row[0]) if self._order_by_group else observed)
                if (
                    self._order_by_group
                    and previous_group is not None
                    and value["group"] <= previous_group
                ):
                    raise ValidationError(
                        "Chimew bank/channel report group order disagrees"
                    )
                previous_group = value["group"]
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError(
                    "Chimew bank/channel report assignment count disagrees"
                )
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        self._ensure_open()
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        if self._lookup is None:
            self._lookup = _connect(self.store_path)
        if self._order_by_group:
            row = self._lookup.execute(
                "SELECT seq,group_id,bank_pair,channel,bank_cost_rank,"
                "channel_cost_rank,bank_cost,channel_cost FROM assignments "
                "ORDER BY group_id LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        else:
            row = self._lookup.execute(
                "SELECT seq,group_id,bank_pair,channel,bank_cost_rank,"
                "channel_cost_rank,bank_cost,channel_cost FROM assignments "
                "WHERE seq=?",
                (index,),
            ).fetchone()
        if row is None:
            raise ValidationError(
                "Chimew bank/channel report order is not contiguous"
            )
        return _row_value(row, int(row[0]))

    def close(self) -> None:
        if self._lookup is not None:
            self._lookup.close()
            self._lookup = None

    def __del__(self) -> None:
        self.close()


def _logical_sha256(
    header: Mapping[str, Any], assignments: Iterable[Mapping[str, Any]]
) -> str:
    digest = hashlib.sha256()
    keys = sorted((*header, "assignments"))
    digest.update(b"{")
    for key_index, key in enumerate(keys):
        if key_index:
            digest.update(b",")
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        if key == "assignments":
            digest.update(b"[")
            for index, assignment in enumerate(assignments):
                if index:
                    digest.update(b",")
                digest.update(_canonical(assignment).encode("utf-8"))
            digest.update(b"]")
        else:
            digest.update(_canonical(header[key]).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()


class BankChannelReportView(Mapping[str, Any]):
    """Lazy logical assignment report backed by one immutable typed store."""

    def __init__(
        self,
        selector_path: Path,
        *,
        _verified_store_sha256: str | None = None,
        _acquire_lease: bool = True,
    ):
        self._lease = None
        self._closed = False
        self._lease_state = {"open": True}
        selector_path = Path(selector_path).absolute()
        try:
            if stat.S_ISLNK(selector_path.lstat().st_mode):
                raise ValidationError(
                    "Chimew bank/channel report selector cannot be a symlink"
                )
        except OSError as error:
            raise ValidationError(
                "Chimew bank/channel report selector is missing"
            ) from error
        if _acquire_lease:
            self._lease = _report_lease(selector_path, exclusive=False)
            self._lease.__enter__()
        try:
            self._initialize(
                selector_path,
                _verified_store_sha256=_verified_store_sha256,
            )
        except BaseException:
            self.close()
            raise

    def _initialize(
        self, selector_path: Path, *, _verified_store_sha256: str | None
    ) -> None:
        from emuflow.storage.store_publication import StoreSource

        self.manifest_path = selector_path.resolve(strict=True)
        original = bind_store_source(self.manifest_path, "chimew-bank-report-selector-v1")
        proof = _transfer_report_native(self.manifest_path, self.manifest_path)
        names = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds",
                 "ctime_seconds", "ctime_nanoseconds")
        sources = tuple(StoreSource(Path(row["path"]), row["schema"],
                                    tuple(row["generation"][name] for name in names))
                        for row in proof["inputs"])
        if sources[0] != original:
            raise ValidationError("Chimew bank/channel report selector generation changed")
        # The core has checked the bounded header, complete schema, ordered
        # assignments, aggregate costs and all content/logical record seals.
        manifest = read_json(self.manifest_path)
        self.store_path = self.manifest_path.with_name(manifest["artifact"])
        if (self.store_path != sources[1].path
                or _verified_store_sha256 is not None
                and _verified_store_sha256 != manifest["store_sha256"]):
            raise ValidationError("Chimew bank/channel report store seal disagrees")
        self.manifest = manifest
        self.logical_sha256 = manifest["logical_sha256"]
        self._header = manifest["header"]
        count = manifest["assignments"]["records"]
        self._assignments = _AssignmentSequence(self.store_path, count, lease_state=self._lease_state)
        self._assignments_by_group = _AssignmentSequence(
            self.store_path, count, order_by_group=True, lease_state=self._lease_state)
        self._source_bindings = sources
        require_store_sources_unchanged(sources)

    def bound_store_sources(self):
        if self._closed:
            raise ValidationError("Chimew bank/channel report view is closed")
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def __getitem__(self, key: str) -> Any:
        if self._closed:
            raise ValidationError("Chimew bank/channel report view is closed")
        if key == "assignments":
            return self._assignments
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        if self._closed:
            raise ValidationError("Chimew bank/channel report view is closed")
        yield from self._header
        yield "assignments"

    def __len__(self) -> int:
        if self._closed:
            raise ValidationError("Chimew bank/channel report view is closed")
        return len(self._header) + 1

    def iter_assignments_by_group(self) -> Iterator[Mapping[str, Any]]:
        yield from self._assignments_by_group

    def materialize(self) -> dict[str, Any]:
        return {**self._header, "assignments": list(self._assignments)}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lease_state["open"] = False
        assignments = getattr(self, "_assignments", None)
        if assignments is not None:
            assignments.close()
        assignments_by_group = getattr(self, "_assignments_by_group", None)
        if assignments_by_group is not None:
            assignments_by_group.close()
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None

    def __enter__(self) -> "BankChannelReportView":
        if self._closed:
            raise ValidationError("Chimew bank/channel report view is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def canonical_bank_channel_report_sha256(document: Mapping[str, Any]) -> str:
    if isinstance(document, BankChannelReportView):
        return document.logical_sha256
    assignments = document.get("assignments")
    if not isinstance(assignments, Iterable):
        raise ValidationError("Chimew bank/channel assignments are missing")
    header = {key: value for key, value in document.items() if key != "assignments"}
    return _logical_sha256(header, assignments)


def read_chimew_bank_channel_report(
    path: Path,
    *,
    materialize: bool = False,
    _verified_store_sha256: str | None = None,
) -> Mapping[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("Chimew bank/channel report must be an object")
    if value.get("schema") == REPORT_STORAGE_SCHEMA:
        view = BankChannelReportView(
            path, _verified_store_sha256=_verified_store_sha256
        )
        if not materialize:
            return view
        try:
            return view.materialize()
        finally:
            view.close()
    raise ValidationError("Chimew bank/channel reports require a typed selector and store")


def chimew_bank_channel_report_store_path(path: Path) -> Path | None:
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    if value.get("schema") != REPORT_STORAGE_SCHEMA:
        return None
    with BankChannelReportView(path) as view:
        return view.store_path


def _transfer_report_native(source: Path, destination: Path):
    """Publish a source-bound typed report through the native store owner."""
    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
    from emuflow.common.tool_runtime import NativeToolProcess

    source, destination = Path(source), Path(destination)
    if source.is_symlink():
        raise ValidationError("Chimew report source selector is a symlink")
    source = source.resolve()
    if destination.is_symlink():
        raise ValidationError("Chimew report destination is a symlink")
    destination = destination.resolve()
    same_owner = source == destination
    if not same_owner:
        destination.parent.mkdir(parents=True, exist_ok=True)
    native = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix=".chimew-report-copy-", dir=None if same_owner else destination.parent) as temporary:
        root = Path(temporary)
        receipt = root / "operation.json"
        with NativeToolProcess(native, [native, "--report", str(receipt), "phase6",
                "copy-bank-report", str(source), str(destination)],
                environment=dict(os.environ), scratch_parent=root) as process:
            result = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
            raise ValidationError("native Chimew report copy receipt is missing or exceeds bound")
        operation = read_json(receipt)
        if result.returncode:
            raise ValidationError("native Chimew report copy failed: " +
                operation.get("error", {}).get("message", result.stdout))
        if (operation.get("schema") != "emuflow.native-stage-result/v1"
                or operation.get("stage") != "phase6.copy-bank-report"
                or operation.get("status") != "pass"
                or len(operation.get("inputs", [])) != 2
                or len(operation.get("outputs", [])) != 2):
            raise ValidationError("native Chimew report copy identity disagrees")
        inputs, outputs = operation["inputs"], operation["outputs"]
        if (inputs[0].get("path") != str(source)
                or inputs[0].get("schema") != "chimew-bank-report-selector-v1"
                or inputs[1].get("schema") != "chimew-bank-report-store-v1"
                or outputs[0].get("schema") != "chimew-bank-report-store-v1"
                or Path(outputs[0].get("path", "")).parent != destination.parent
                or outputs[1].get("schema") != "chimew-bank-report-selector-v1"
                or outputs[1].get("path") != str(destination)):
            raise ValidationError("native Chimew report copy owner disagrees")
        names = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds",
                 "ctime_seconds", "ctime_nanoseconds")
        for row in (*inputs, *outputs):
            if native_file_generation(Path(row["path"])) != tuple(row["generation"][name] for name in names):
                raise ValidationError("native Chimew report copy generation changed")
        return operation


def copy_chimew_bank_channel_report_storage(source: Path, destination: Path) -> None:
    """Copy a typed report without Python assignment parsing or serialization."""
    _transfer_report_native(source, destination)
