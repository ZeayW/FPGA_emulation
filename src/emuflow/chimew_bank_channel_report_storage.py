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
import shutil
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import fcntl

from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .store_publication import (
    bind_store_source,
    require_store_sources_unchanged,
    publish_sqlite_store,
)


REPORT_STORAGE_SCHEMA = "emuflow.chimew-bank-channel-report-storage/v1"
REPORT_APPLICATION_ID = 0x454D5552  # ASCII "EMUR"
REPORT_USER_VERSION = 1
_SELECTOR_KEYS = {
    "schema",
    "logical_schema",
    "artifact",
    "application_id",
    "user_version",
    "store_bytes",
    "store_sha256",
    "header",
    "assignments",
    "logical_sha256",
}
_RELATION_SEAL_KEYS = {"records", "sha256"}
_ASSIGNMENT_KEYS = {
    "group",
    "bank_pair",
    "channel",
    "bank_cost_rank",
    "channel_cost_rank",
    "bank_cost",
    "channel_cost",
}
_HEADER_KEYS = {
    "schema",
    "status",
    "integration_status",
    "design",
    "platform",
    "provider",
    "paper_scope",
    "algorithm2_interpretation",
    "numeric_policy",
    "provenance",
    "input_sha256",
    "metrics",
    "direction_priority",
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


def _normalize_header(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the bounded selector header without coercing identities."""

    from .chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

    if not isinstance(value, Mapping) or set(value) != _HEADER_KEYS:
        raise ValidationError("Chimew bank/channel report header has an invalid shape")

    def validate_json_tree(item: Any) -> bool:
        if item is None or isinstance(item, (bool, str, int)):
            return True
        if isinstance(item, float):
            return math.isfinite(item)
        if isinstance(item, list):
            return all(validate_json_tree(child) for child in item)
        if isinstance(item, Mapping):
            return all(
                isinstance(key, str) and validate_json_tree(child)
                for key, child in item.items()
            )
        return False

    if not validate_json_tree(value):
        raise ValidationError("Chimew bank/channel report header is not finite JSON")
    for key in (
        "status",
        "integration_status",
        "design",
        "platform",
        "provider",
        "paper_scope",
        "algorithm2_interpretation",
        "numeric_policy",
    ):
        _string(value.get(key), f"report.{key}")
    if value.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA:
        raise ValidationError("Chimew bank/channel report schema is invalid")
    if not _valid_digest(value.get("input_sha256")):
        raise ValidationError("Chimew bank/channel report input digest is invalid")
    if not isinstance(value.get("provenance"), Mapping):
        raise ValidationError("Chimew bank/channel report provenance is invalid")
    if not isinstance(value.get("metrics"), Mapping):
        raise ValidationError("Chimew bank/channel report metrics are invalid")
    priorities = value.get("direction_priority")
    if not isinstance(priorities, Mapping) or any(
        not isinstance(bank, str)
        or not bank
        or priority not in {"a_to_b", "b_to_a"}
        for bank, priority in priorities.items()
    ):
        raise ValidationError("Chimew bank/channel direction priority is invalid")
    return dict(value)


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
        from .chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

        self.manifest_path = selector_path.resolve(strict=True)
        selector_source = bind_store_source(
            self.manifest_path, "chimew-bank-report-selector-v1"
        )
        manifest = read_json(self.manifest_path)
        relation = manifest.get("assignments")
        artifact = manifest.get("artifact")
        if (
            set(manifest) != _SELECTOR_KEYS
            or manifest.get("schema") != REPORT_STORAGE_SCHEMA
            or manifest.get("logical_schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
            or manifest.get("application_id") != REPORT_APPLICATION_ID
            or manifest.get("user_version") != REPORT_USER_VERSION
            or not isinstance(manifest.get("header"), dict)
            or not isinstance(relation, dict)
            or set(relation) != _RELATION_SEAL_KEYS
            or isinstance(relation.get("records"), bool)
            or not isinstance(relation.get("records"), int)
            or relation["records"] <= 0
            or not _valid_digest(relation.get("sha256"))
            or not _valid_digest(manifest.get("logical_sha256"))
            or not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
            or not _valid_digest(manifest.get("store_sha256"))
        ):
            raise ValidationError("Chimew bank/channel report selector is malformed")
        self.store_path = self.manifest_path.with_name(artifact)
        try:
            store_source = bind_store_source(
                self.store_path, "chimew-bank-report-store-v1"
            )
            store_lstat = self.store_path.lstat()
            resolved_store = self.store_path.resolve(strict=True)
        except OSError as error:
            raise ValidationError(
                "Chimew bank/channel report store is missing"
            ) from error
        expected_store_sha256 = (
            _verified_store_sha256
            if _verified_store_sha256 is not None
            else file_sha256(self.store_path)
        )
        selector_stem = self.manifest_path.stem
        stem_parts = selector_stem.rsplit(".", 1)
        artifact_stem = (
            stem_parts[0]
            if len(stem_parts) == 2 and _valid_digest(stem_parts[1])
            else selector_stem
        )
        if (
            stat.S_ISLNK(store_lstat.st_mode)
            or resolved_store.parent != self.manifest_path.parent
            or resolved_store != self.store_path
            or isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != store_lstat.st_size
            or expected_store_sha256 != manifest["store_sha256"]
            or artifact
            != f"{artifact_stem}.{manifest['store_sha256']}.sqlite"
        ):
            raise ValidationError("Chimew bank/channel report store seal disagrees")
        connection = _connect(self.store_path)
        try:
            objects = tuple(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            columns = tuple(
                (row[1], row[2], int(row[3]), row[4], int(row[5]))
                for row in connection.execute("PRAGMA table_info(assignments)")
            )
            metadata_columns = tuple(
                (row[1], row[2], int(row[3]), row[4], int(row[5]))
                for row in connection.execute("PRAGMA table_info(metadata)")
            )
            metadata = {
                str(row[0]): json.loads(str(row[1]))
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            endpoints = connection.execute(
                "SELECT min(seq),max(seq),count(*),count(DISTINCT group_id),"
                "count(DISTINCT channel) FROM assignments"
            ).fetchone()
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != REPORT_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != REPORT_USER_VERSION
                or objects
                != (("table", "assignments"), ("table", "metadata"))
                or metadata_columns
                != (
                    ("key", "TEXT", 1, None, 1),
                    ("value", "TEXT", 1, None, 0),
                )
                or columns
                != (
                    ("seq", "INTEGER", 1, None, 1),
                    ("group_id", "TEXT", 1, None, 0),
                    ("bank_pair", "TEXT", 1, None, 0),
                    ("channel", "TEXT", 1, None, 0),
                    ("bank_cost_rank", "INTEGER", 1, None, 0),
                    ("channel_cost_rank", "INTEGER", 1, None, 0),
                    ("bank_cost", "REAL", 1, None, 0),
                    ("channel_cost", "REAL", 1, None, 0),
                )
                or set(metadata)
                != {"header", "assignments_sha256", "logical_sha256"}
                or metadata["header"] != manifest["header"]
                or metadata["assignments_sha256"] != relation["sha256"]
                or metadata["logical_sha256"] != manifest["logical_sha256"]
                or endpoints[0] != 0
                or endpoints[1] != relation["records"] - 1
                or endpoints[2] != relation["records"]
                or endpoints[3] != relation["records"]
                or endpoints[4] != relation["records"]
            ):
                raise ValidationError(
                    "Chimew bank/channel report SQLite metadata disagrees"
                )
        except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
            raise ValidationError(
                "Chimew bank/channel report SQLite store is corrupt"
            ) from error
        finally:
            connection.close()
        normalized_header = _normalize_header(manifest["header"])
        if normalized_header != manifest["header"]:
            raise ValidationError("Chimew bank/channel report header disagrees")
        self.manifest = manifest
        self.logical_sha256 = str(manifest["logical_sha256"])
        self._header = normalized_header
        count = int(relation["records"])
        self._assignments = _AssignmentSequence(
            self.store_path, count, lease_state=self._lease_state
        )
        self._assignments_by_group = _AssignmentSequence(
            self.store_path,
            count,
            order_by_group=True,
            lease_state=self._lease_state,
        )
        relation_digest = hashlib.sha256()
        for assignment in self._assignments:
            encoded = _canonical(assignment).encode("utf-8")
            relation_digest.update(encoded)
            relation_digest.update(b"\n")
        if (
            relation_digest.hexdigest() != relation["sha256"]
            or _logical_sha256(self._header, self._assignments)
            != self.logical_sha256
        ):
            raise ValidationError(
                "Chimew bank/channel report logical record seal disagrees"
            )

        self._source_bindings = (selector_source, store_source)
        require_store_sources_unchanged(self._source_bindings)

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


def _write_locked(
    path: Path,
    document: Mapping[str, Any],
    *,
    derive_physical_costs: bool,
    source_bindings,
) -> BankChannelReportView:
    from .chimew_bank_channel import CHIMEW_BANK_CHANNEL_REPORT_SCHEMA

    if document.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA:
        raise ValidationError("Chimew bank/channel report schema is invalid")
    assignments = document.get("assignments")
    if not isinstance(assignments, Iterable):
        raise ValidationError("Chimew bank/channel assignments are missing")
    header = _normalize_header(
        {key: value for key, value in document.items() if key != "assignments"}
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_selector = path.read_bytes() if path.is_file() else None
    previous_artifact: Path | None = None
    if previous_selector is not None:
        try:
            previous_view = BankChannelReportView(path, _acquire_lease=False)
        except (OSError, ValueError, ValidationError):
            # An invalid old selector is replaceable, but it is never trusted as
            # a GC authority.  Its named path may be attacker-controlled.
            previous_view = None
        if previous_view is not None:
            previous_artifact = previous_view.store_path
            previous_view.close()
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp")
    connection = sqlite3.connect(temporary)
    relation_digest = hashlib.sha256()
    rows: list[tuple[Any, ...]] = []
    count = 0
    stage1_physical_cost = 0.0
    stage2_physical_cost = 0.0
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={REPORT_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={REPORT_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE assignments(
              seq INTEGER PRIMARY KEY,
              group_id TEXT NOT NULL UNIQUE,
              bank_pair TEXT NOT NULL,
              channel TEXT NOT NULL UNIQUE,
              bank_cost_rank INTEGER NOT NULL CHECK(bank_cost_rank>=0),
              channel_cost_rank INTEGER NOT NULL CHECK(channel_cost_rank>=0),
              bank_cost REAL NOT NULL CHECK(bank_cost>=0.0),
              channel_cost REAL NOT NULL CHECK(channel_cost>=0.0)
            ) WITHOUT ROWID;
            """
        )
        for seq, raw in enumerate(assignments):
            value = _normalize_assignment(raw, seq)
            encoded = _canonical(value).encode("utf-8")
            relation_digest.update(encoded)
            relation_digest.update(b"\n")
            stage1_physical_cost += value["bank_cost"]
            stage2_physical_cost += value["channel_cost"]
            rows.append(
                (
                    seq,
                    value["group"],
                    value["bank_pair"],
                    value["channel"],
                    value["bank_cost_rank"],
                    value["channel_cost_rank"],
                    value["bank_cost"],
                    value["channel_cost"],
                )
            )
            count = seq + 1
            if len(rows) >= 4096:
                connection.executemany(
                    "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)", rows
                )
                rows.clear()
        if rows:
            connection.executemany(
                "INSERT INTO assignments VALUES (?,?,?,?,?,?,?,?)", rows
            )
        if count <= 0:
            raise ValidationError("Chimew bank/channel assignments are empty")
        if derive_physical_costs:
            metrics = dict(header["metrics"])
            metrics["stage1_physical_site_cost"] = stage1_physical_cost
            metrics["stage2_physical_site_cost"] = stage2_physical_cost
            header["metrics"] = metrics
        connection.commit()
        sequence = _AssignmentSequence(temporary, count)
        logical_sha256 = _logical_sha256(header, sequence)
        sequence.close()
        assignments_sha256 = relation_digest.hexdigest()
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("assignments_sha256", _canonical(assignments_sha256)),
                ("logical_sha256", _canonical(logical_sha256)),
            ),
        )
        connection.commit()
        connection.close()
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "Chimew bank/channel report repeats a group or channel"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    try:
        store_sha256 = file_sha256(temporary)
        artifact = path.with_name(f"{path.stem}.{store_sha256}.sqlite")
        created = not artifact.exists()
        if created:
            publish_sqlite_store(
                temporary,
                artifact,
                "chimew-bank-report-store-v1",
                sources=source_bindings,
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("Chimew bank/channel report digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": REPORT_STORAGE_SCHEMA,
        "logical_schema": CHIMEW_BANK_CHANNEL_REPORT_SCHEMA,
        "artifact": artifact.name,
        "application_id": REPORT_APPLICATION_ID,
        "user_version": REPORT_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "assignments": {
            "records": count,
            "sha256": assignments_sha256,
        },
        "logical_sha256": logical_sha256,
    }
    try:
        write_json(path, manifest, compact=True)
        _fsync_directory(path.parent)
        checked = BankChannelReportView(path, _acquire_lease=False)
    except BaseException:
        if previous_selector is None:
            path.unlink(missing_ok=True)
        else:
            restore = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
            restore.write_bytes(previous_selector)
            with restore.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(restore, path)
            _fsync_directory(path.parent)
        if created and artifact != previous_artifact:
            artifact.unlink(missing_ok=True)
        raise
    if previous_artifact is not None and previous_artifact != artifact:
        try:
            previous_artifact.unlink(missing_ok=True)
        except OSError:
            # The new selector is already committed and validated.  Failure to
            # retire an old immutable sidecar is a safe deferred-GC condition,
            # not a publication failure.
            pass
    return checked


def write_chimew_bank_channel_report_storage(
    path: Path,
    document: Mapping[str, Any],
    *,
    _derive_physical_costs: bool = False,
    source_bindings=(),
) -> BankChannelReportView:
    source_bindings = tuple(source_bindings)
    require_store_sources_unchanged(source_bindings)
    path = Path(path)
    if (
        getattr(document, "manifest_path", None) == path.resolve(strict=False)
    ):
        raise ValidationError(
            "cannot replace a Chimew bank/channel report from its active view"
        )
    lease = _report_lease(path, exclusive=True)
    descriptor = lease.__enter__()
    try:
        checked = _write_locked(
            path,
            document,
            derive_physical_costs=_derive_physical_costs,
            source_bindings=source_bindings,
        )
        checked.close()
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        view = BankChannelReportView(path, _acquire_lease=False)
        view._lease = lease
        return view
    except BaseException:
        lease.__exit__(None, None, None)
        raise


def read_chimew_bank_channel_report(
    path: Path,
    *,
    materialize: bool = False,
    allow_legacy: bool = False,
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
    if not allow_legacy:
        raise ValidationError(
            "legacy Chimew bank/channel report JSON requires allow_legacy=True"
        )
    return value


def chimew_bank_channel_report_store_path(path: Path) -> Path | None:
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    if value.get("schema") != REPORT_STORAGE_SCHEMA:
        return None
    with BankChannelReportView(path) as view:
        return view.store_path


def copy_chimew_bank_channel_report_storage(
    source: Path, destination: Path, *, allow_legacy: bool = False
) -> None:
    source = Path(source)
    destination = Path(destination)
    if source.resolve(strict=True) == destination.resolve(strict=False):
        value = read_chimew_bank_channel_report(
            source, allow_legacy=allow_legacy
        )
        if isinstance(value, BankChannelReportView):
            value.close()
        return
    value = read_json(source)
    if not isinstance(value, dict):
        raise ValidationError("Chimew bank/channel report must be an object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if value.get("schema") != REPORT_STORAGE_SCHEMA:
        if not allow_legacy:
            raise ValidationError(
                "legacy Chimew bank/channel report copy requires allow_legacy=True"
            )
        with _report_lease(destination, exclusive=True):
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.legacy.tmp"
            )
            try:
                shutil.copy2(source, temporary)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return
    with BankChannelReportView(source) as view:
        # Re-publish from the logical cursor.  This is intentionally streaming
        # and also revalidates every typed assignment before selector commit.
        copied = write_chimew_bank_channel_report_storage(
            destination, view, source_bindings=view.bound_store_sources()
        )
        copied.close()
