"""Indexed storage for graph-sized Chimew bank/channel inputs.

The logical v1/v2 contracts remain readable.  Production lookahead writes the
group relation once into a sealed SQLite sidecar and leaves only bounded
headers and relation seals in the JSON selector.  Readers iterate one group at
a time and normalize only that record, so qualification and the Phase 6
adapter do not retain a second group/member/fanin tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

import fcntl

from .errors import ValidationError
from .io import file_sha256, read_json, write_json
from .store_publication import (
    bind_store_source,
    bind_view_sources,
    require_store_sources_unchanged,
    publish_sqlite_store,
)


BANK_CHANNEL_STORAGE_SCHEMA = "emuflow.chimew-bank-channel-storage/v1"
BANK_CHANNEL_APPLICATION_ID = 0x454D5543  # ASCII "EMUC"
BANK_CHANNEL_USER_VERSION = 1
_SELECTOR_KEYS = {
    "schema",
    "logical_schema",
    "artifact",
    "application_id",
    "user_version",
    "store_bytes",
    "store_sha256",
    "header",
    "problem_header",
    "bank_pairs",
    "groups",
    "logical_sha256",
}
_RELATION_SEAL_KEYS = {"records", "sha256"}


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


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _lock_path(selector_path: Path) -> Path:
    selector_path = Path(selector_path).resolve(strict=False)
    identity = hashlib.sha256(os.fsencode(selector_path)).hexdigest()
    return Path(tempfile.gettempdir()) / "emuflow-bank-channel-locks" / (
        f"{identity}.lock"
    )


@contextmanager
def _bank_channel_lease(
    selector_path: Path, *, exclusive: bool
) -> Iterator[int]:
    """Hold one selector's POSIX reader/writer lease.

    The name is stable across selector replacement, so an old reader prevents
    a writer from unlinking its content-addressed sidecar until every cursor
    using that generation has closed.
    """

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


class _RawGroupSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        count: int,
        *,
        order_by_id: bool = False,
        lease_state: Mapping[str, bool] | None = None,
    ):
        self.store_path = store_path
        self._count = count
        self._order_by_id = order_by_id
        self._lease_state = lease_state
        self._lookup_connection: sqlite3.Connection | None = None

    def _ensure_open(self) -> None:
        if self._lease_state is not None and not self._lease_state["open"]:
            raise ValidationError("Chimew bank/channel view is closed")

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _decode(encoded: str) -> Mapping[str, Any]:
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValidationError("Chimew bank/channel group is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("Chimew bank/channel group is not an object")
        return value

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        connection = _connect(self.store_path)
        try:
            observed = 0
            order = "group_id" if self._order_by_id else "seq"
            previous_id: str | None = None
            for row in connection.execute(
                f"SELECT seq,group_id,record_json FROM groups ORDER BY {order}"
            ):
                self._ensure_open()
                if not self._order_by_id and int(row[0]) != observed:
                    raise ValidationError(
                        "Chimew bank/channel group order is not contiguous"
                    )
                value = self._decode(str(row[2]))
                group_id = str(row[1])
                if value.get("id") != group_id or (
                    self._order_by_id
                    and previous_id is not None
                    and group_id <= previous_id
                ):
                    raise ValidationError(
                        "Chimew bank/channel group identity order disagrees"
                    )
                previous_id = group_id
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError(
                    "Chimew bank/channel group count disagrees"
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
        if self._lookup_connection is None:
            self._lookup_connection = _connect(self.store_path)
        if self._order_by_id:
            row = self._lookup_connection.execute(
                "SELECT group_id,record_json FROM groups "
                "ORDER BY group_id LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        else:
            row = self._lookup_connection.execute(
                "SELECT group_id,record_json FROM groups WHERE seq=?", (index,)
            ).fetchone()
        if row is None:
            raise ValidationError(
                "Chimew bank/channel group order is not contiguous"
            )
        value = self._decode(str(row[1]))
        if value.get("id") != str(row[0]):
            raise ValidationError("Chimew bank/channel group identity disagrees")
        return value

    def iter_encoded(self) -> Iterator[str]:
        self._ensure_open()
        connection = _connect(self.store_path)
        try:
            for expected, row in enumerate(
                connection.execute(
                    "SELECT seq,record_json FROM groups ORDER BY seq"
                )
            ):
                self._ensure_open()
                if int(row[0]) != expected:
                    raise ValidationError(
                        "Chimew bank/channel group order is not contiguous"
                    )
                yield str(row[1])
        finally:
            connection.close()

    def close(self) -> None:
        if self._lookup_connection is not None:
            self._lookup_connection.close()
            self._lookup_connection = None

    def __del__(self) -> None:
        self.close()


class _BankSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        store_path: Path,
        count: int,
        *,
        normalized: bool,
        domain_by_id: Mapping[str, int] | None = None,
        lease_state: Mapping[str, bool] | None = None,
    ):
        self.store_path = store_path
        self._count = count
        self._normalized = normalized
        self._domain_by_id = dict(domain_by_id or {})
        self._lease_state = lease_state
        self._lookup_connection: sqlite3.Connection | None = None

    def _ensure_open(self) -> None:
        if self._lease_state is not None and not self._lease_state["open"]:
            raise ValidationError("Chimew bank/channel view is closed")

    def __len__(self) -> int:
        return self._count

    @staticmethod
    def _decode(encoded: str) -> Mapping[str, Any]:
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValidationError("Chimew bank/channel bank is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("Chimew bank/channel bank is not an object")
        return value

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        self._ensure_open()
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT seq,bank_id,first_channel,record_json "
                "FROM banks ORDER BY seq"
            ):
                self._ensure_open()
                if int(row[0]) != observed:
                    raise ValidationError(
                        "Chimew bank/channel bank order is not contiguous"
                    )
                observed += 1
                yield self._value(
                    str(row[3]),
                    int(row[0]),
                    int(row[2]),
                    expected_id=str(row[1]),
                )
            if observed != self._count:
                raise ValidationError("Chimew bank/channel bank count disagrees")
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
        if self._lookup_connection is None:
            self._lookup_connection = _connect(self.store_path)
        row = self._lookup_connection.execute(
            "SELECT bank_id,first_channel,record_json FROM banks WHERE seq=?",
            (index,),
        ).fetchone()
        if row is None:
            raise ValidationError("Chimew bank/channel bank order is not contiguous")
        return self._value(
            str(row[2]), index, int(row[1]), expected_id=str(row[0])
        )

    def _value(
        self,
        encoded: str,
        index: int,
        first_channel: int,
        *,
        expected_id: str,
    ) -> Mapping[str, Any]:
        value = self._decode(encoded)
        if value.get("id") != expected_id:
            raise ValidationError("Chimew bank/channel bank identity disagrees")
        if not self._normalized:
            return value
        from .chimew_bank_channel import _normalize_chimew_bank

        normalized, next_channel = _normalize_chimew_bank(
            value,
            index,
            domain_by_id=self._domain_by_id,
            first_channel_index=first_channel,
        )
        if next_channel != first_channel + len(normalized["channels"]):
            raise ValidationError("Chimew bank/channel index range disagrees")
        return normalized

    def iter_encoded(self) -> Iterator[str]:
        self._ensure_open()
        connection = _connect(self.store_path)
        try:
            for expected, row in enumerate(
                connection.execute(
                    "SELECT seq,bank_id,record_json FROM banks ORDER BY seq"
                )
            ):
                self._ensure_open()
                if int(row[0]) != expected:
                    raise ValidationError(
                        "Chimew bank/channel bank order is not contiguous"
                    )
                encoded = str(row[2])
                if self._decode(encoded).get("id") != str(row[1]):
                    raise ValidationError(
                        "Chimew bank/channel bank identity disagrees"
                    )
                yield encoded
        finally:
            connection.close()

    def close(self) -> None:
        if self._lookup_connection is not None:
            self._lookup_connection.close()
            self._lookup_connection = None

    def __del__(self) -> None:
        self.close()


class _NormalizedGroupSequence(Sequence[Mapping[str, Any]]):
    def __init__(
        self,
        raw: _RawGroupSequence,
        *,
        schema: str,
        domain_by_id: Mapping[str, int],
    ):
        self._raw = raw
        self._schema = schema
        self._domain_by_id = dict(domain_by_id)

    def __len__(self) -> int:
        return len(self._raw)

    def _normalize(self, value: Mapping[str, Any], index: int) -> Dict[str, Any]:
        from .chimew_bank_channel import _normalize_chimew_group

        return _normalize_chimew_group(
            value,
            index,
            schema=self._schema,
            domain_by_id=self._domain_by_id,
        )

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        for index, value in enumerate(self._raw):
            yield self._normalize(value, index)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        normalized_index = index if index >= 0 else len(self) + index
        return self._normalize(self._raw[index], normalized_index)


class BankChannelInputView(Mapping[str, Any]):
    """Lazy logical view over one sealed bank/channel selector and store."""

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
        if _acquire_lease:
            self._lease = _bank_channel_lease(selector_path, exclusive=False)
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
        self,
        selector_path: Path,
        *,
        _verified_store_sha256: str | None,
    ) -> None:
        self.manifest_path = selector_path.resolve(strict=True)
        selector_source = bind_store_source(
            self.manifest_path, "chimew-bank-input-selector-v1"
        )
        manifest = read_json(self.manifest_path)
        from .chimew_bank_channel import (
            CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
        )

        if (
            set(manifest) != _SELECTOR_KEYS
            or manifest.get("schema") != BANK_CHANNEL_STORAGE_SCHEMA
            or manifest.get("logical_schema")
            not in {
                CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
                CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
            }
            or manifest.get("application_id") != BANK_CHANNEL_APPLICATION_ID
            or manifest.get("user_version") != BANK_CHANNEL_USER_VERSION
            or not isinstance(manifest.get("header"), dict)
            or not isinstance(manifest.get("problem_header"), dict)
            or not isinstance(manifest.get("bank_pairs"), dict)
            or not isinstance(manifest.get("groups"), dict)
            or not _valid_digest(manifest.get("logical_sha256"))
            or manifest.get("logical_schema")
            != manifest.get("header", {}).get("schema")
        ):
            raise ValidationError("Chimew bank/channel selector is malformed")
        banks = manifest["bank_pairs"]
        groups = manifest["groups"]
        if (
            set(banks) != _RELATION_SEAL_KEYS
            or set(groups) != _RELATION_SEAL_KEYS
            or
            isinstance(banks.get("records"), bool)
            or not isinstance(banks.get("records"), int)
            or banks["records"] <= 0
            or not _valid_digest(banks.get("sha256"))
            or
            isinstance(groups.get("records"), bool)
            or not isinstance(groups.get("records"), int)
            or groups["records"] <= 0
            or not _valid_digest(groups.get("sha256"))
        ):
            raise ValidationError("Chimew bank/channel group seal is malformed")
        artifact = manifest.get("artifact")
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
            or not _valid_digest(manifest.get("store_sha256"))
        ):
            raise ValidationError("Chimew bank/channel store path is invalid")
        self.store_path = self.manifest_path.with_name(artifact)
        try:
            store_source = bind_store_source(
                self.store_path, "chimew-bank-input-store-v1"
            )
            store_lstat = self.store_path.lstat()
            resolved_store = self.store_path.resolve(strict=True)
        except OSError as error:
            raise ValidationError(
                "Chimew bank/channel SQLite store is missing"
            ) from error
        if (
            stat.S_ISLNK(store_lstat.st_mode)
            or resolved_store.parent != self.manifest_path.parent
            or resolved_store != self.store_path
            or isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != store_lstat.st_size
            or (
                _verified_store_sha256
                if _verified_store_sha256 is not None
                else file_sha256(self.store_path)
            )
            != manifest["store_sha256"]
            or artifact != f"{self.manifest_path.stem}.{manifest['store_sha256']}.sqlite"
        ):
            raise ValidationError("Chimew bank/channel store seal disagrees")
        connection = _connect(self.store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != BANK_CHANNEL_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != BANK_CHANNEL_USER_VERSION
            ):
                raise ValidationError(
                    "unsupported Chimew bank/channel SQLite artifact"
                )
            objects = tuple(
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
            def table_signature(name: str) -> tuple[tuple[Any, ...], ...]:
                return tuple(
                    (row[1], row[2], int(row[3]), row[4], int(row[5]))
                    for row in connection.execute(f"PRAGMA table_info({name})")
                )

            metadata_columns = table_signature("metadata")
            bank_columns = table_signature("banks")
            columns = table_signature("groups")
            if (
                objects != (
                    ("table", "banks"),
                    ("table", "groups"),
                    ("table", "metadata"),
                )
                or metadata_columns
                != (
                    ("key", "TEXT", 1, None, 1),
                    ("value", "TEXT", 1, None, 0),
                )
                or bank_columns != (
                    ("seq", "INTEGER", 1, None, 1),
                    ("bank_id", "TEXT", 1, None, 0),
                    ("first_channel", "INTEGER", 1, None, 0),
                    ("record_json", "TEXT", 1, None, 0),
                )
                or columns
                != (
                    ("seq", "INTEGER", 1, None, 1),
                    ("group_id", "TEXT", 1, None, 0),
                    ("record_json", "TEXT", 1, None, 0),
                )
            ):
                raise ValidationError(
                    "Chimew bank/channel SQLite schema is invalid"
                )
            metadata = {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT key,value FROM metadata")
            }
            bank_count = int(
                connection.execute("SELECT count(*) FROM banks").fetchone()[0]
            )
            count = int(
                connection.execute("SELECT count(*) FROM groups").fetchone()[0]
            )
            bank_endpoints = connection.execute(
                "SELECT min(seq),max(seq),count(DISTINCT bank_id) FROM banks"
            ).fetchone()
            endpoints = connection.execute(
                "SELECT min(seq),max(seq),count(DISTINCT group_id) FROM groups"
            ).fetchone()
            if (
                set(metadata)
                != {
                    "header",
                    "problem_header",
                    "banks_sha256",
                    "groups_sha256",
                    "logical_sha256",
                }
                or metadata["header"] != manifest["header"]
                or metadata["problem_header"] != manifest["problem_header"]
                or metadata["banks_sha256"] != banks["sha256"]
                or metadata["groups_sha256"] != groups["sha256"]
                or metadata["logical_sha256"] != manifest["logical_sha256"]
                or count != groups["records"]
                or bank_count != banks["records"]
                or bank_endpoints[0] != 0
                or bank_endpoints[1] != bank_count - 1
                or bank_endpoints[2] != bank_count
                or endpoints[0] != 0
                or endpoints[1] != count - 1
                or endpoints[2] != count
            ):
                raise ValidationError(
                    "Chimew bank/channel SQLite metadata disagrees"
                )
        except (json.JSONDecodeError, sqlite3.DatabaseError) as error:
            raise ValidationError(
                "Chimew bank/channel SQLite metadata is corrupt"
            ) from error
        finally:
            connection.close()
        self.manifest = manifest
        self.logical_sha256 = str(manifest["logical_sha256"])
        self._header = dict(manifest["header"])
        from .chimew_bank_channel import _normalize_chimew_problem_header

        domains = _normalize_chimew_problem_header(
            self._header, metrics={}
        )["domains"]
        domain_by_id = {
            domain.get("id"): index
            for index, domain in enumerate(domains)
        }
        self._banks = _BankSequence(
            self.store_path,
            banks["records"],
            normalized=False,
            lease_state=self._lease_state,
        )
        self._normalized_banks = _BankSequence(
            self.store_path,
            banks["records"],
            normalized=True,
            domain_by_id=domain_by_id,
            lease_state=self._lease_state,
        )
        self._groups = _RawGroupSequence(
            self.store_path,
            groups["records"],
            lease_state=self._lease_state,
        )
        self._groups_by_id = _RawGroupSequence(
            self.store_path,
            groups["records"],
            order_by_id=True,
            lease_state=self._lease_state,
        )
        self._validated: Dict[str, Any] | None = None

        self._source_bindings = (selector_source, store_source)
        require_store_sources_unchanged(self._source_bindings)

    def bound_store_sources(self):
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

    def __getitem__(self, key: str) -> Any:
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        if key == "groups":
            return self._groups
        if key == "bank_pairs":
            return self._banks
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        yield from self._header
        yield "bank_pairs"
        yield "groups"

    def __len__(self) -> int:
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        return len(self._header) + 2

    def validated_problem(self) -> Dict[str, Any]:
        """Return a bounded header plus a lazy normalized group relation."""

        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        if self._validated is not None:
            return self._validated
        connection = _connect(self.store_path)
        try:
            malformed = connection.execute(
                "SELECT bank_id FROM banks WHERE NOT json_valid(record_json) LIMIT 1"
            ).fetchone()
            duplicate_channel = connection.execute(
                """
                SELECT json_extract(channel.value,'$.id') AS channel_id
                FROM banks,json_each(banks.record_json,'$.channels') AS channel
                GROUP BY channel_id HAVING count(*)>1 LIMIT 1
                """
            ).fetchone()
            bad_offset = connection.execute(
                """
                WITH counts AS (
                  SELECT seq,first_channel,
                         json_array_length(record_json,'$.channels') AS channels
                  FROM banks
                )
                SELECT seq FROM counts AS current
                WHERE first_channel != coalesce(
                  (SELECT sum(previous.channels) FROM counts AS previous
                   WHERE previous.seq<current.seq),0
                )
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise ValidationError(
                "Chimew bank/channel bank relation is corrupt"
            ) from error
        finally:
            connection.close()
        if malformed is not None or duplicate_channel is not None or bad_offset is not None:
            raise ValidationError("Chimew bank/channel bank relation is invalid")
        stored_problem_header = self.manifest["problem_header"]
        from .chimew_bank_channel import _normalize_chimew_problem_header

        declared_metrics = self._header.get("metrics")
        if not isinstance(declared_metrics, dict):
            raise ValidationError("Chimew bank/channel metrics are invalid")
        raw_derived_header = _normalize_chimew_problem_header(
            self._header, metrics=declared_metrics
        )
        domains = raw_derived_header["domains"]
        banks = self._normalized_banks
        metrics = self._header.get("metrics")
        if (
            not isinstance(domains, list)
            or not domains
            or not banks
            or not isinstance(metrics, dict)
        ):
            raise ValidationError(
                "Chimew bank/channel normalized header is invalid"
            )
        domain_by_id = {
            domain.get("id"): index for index, domain in enumerate(domains)
        }
        if (
            len(domain_by_id) != len(domains)
            or any(not isinstance(identity, str) or not identity for identity in domain_by_id)
        ):
            raise ValidationError("Chimew bank/channel domains are invalid")
        groups = _NormalizedGroupSequence(
            self._groups,
            schema=str(self.manifest["logical_schema"]),
            domain_by_id=domain_by_id,
        )
        demand = [0] * len(domains)
        capacity = [0] * len(domains)
        signals = fanins = bidirectional = 0
        logical_digest = hashlib.sha256()
        bank_digest = hashlib.sha256()
        group_digest = hashlib.sha256()
        logical_keys = sorted((*self._header, "bank_pairs", "groups"))
        logical_digest.update(b"{")
        for key_index, key in enumerate(logical_keys):
            if key_index:
                logical_digest.update(b",")
            logical_digest.update(_canonical(key).encode("utf-8"))
            logical_digest.update(b":")
            if key == "bank_pairs":
                logical_digest.update(b"[")
                for index, encoded_text in enumerate(self._banks.iter_encoded()):
                    if index:
                        logical_digest.update(b",")
                    encoded = encoded_text.encode("utf-8")
                    logical_digest.update(encoded)
                    bank_digest.update(encoded)
                    bank_digest.update(b"\n")
                logical_digest.update(b"]")
            elif key == "groups":
                logical_digest.update(b"[")
                for index, encoded_text in enumerate(self._groups.iter_encoded()):
                    if index:
                        logical_digest.update(b",")
                    encoded = encoded_text.encode("utf-8")
                    logical_digest.update(encoded)
                    group_digest.update(encoded)
                    group_digest.update(b"\n")
                logical_digest.update(b"]")
            else:
                logical_digest.update(
                    _canonical(self._header[key]).encode("utf-8")
                )
        logical_digest.update(b"}")
        if (
            bank_digest.hexdigest()
            != self.manifest["bank_pairs"]["sha256"]
            or group_digest.hexdigest()
            != self.manifest["groups"]["sha256"]
            or logical_digest.hexdigest() != self.logical_sha256
        ):
            raise ValidationError(
                "Chimew bank/channel logical record seal disagrees"
            )
        for bank in banks:
            capacity[bank["domain"]] += len(bank["channels"])
        for group in groups:
            demand[group["domain"]] += 1
            signals += len(group["members"])
            fanins += sum(len(member["fanins"]) for member in group["members"])
            bidirectional += int(group["direction"] == 2)
        if any(available < required for available, required in zip(capacity, demand)):
            raise ValidationError("Chimew domain has insufficient channel capacity")
        expected = {
            "groups": len(groups),
            "signals": signals,
            "fanins": fanins,
            "bank_pairs": len(banks),
            "channels": sum(len(bank["channels"]) for bank in banks),
            "bidirectional_bundles": bidirectional,
        }
        required_metrics = expected.keys() - {"bidirectional_bundles"}
        if any(metrics.get(key) != expected[key] for key in required_metrics):
            raise ValidationError("Chimew bank/channel input metrics do not agree")
        declared_bidirectional = metrics.get("bidirectional_bundles")
        if declared_bidirectional is not None and (
            isinstance(declared_bidirectional, bool)
            or declared_bidirectional != bidirectional
        ):
            raise ValidationError(
                "Chimew bidirectional-bundle metrics do not agree"
            )
        derived_problem_header = _normalize_chimew_problem_header(
            self._header, metrics=expected
        )
        if stored_problem_header != derived_problem_header:
            raise ValidationError(
                "Chimew bank/channel normalized header is not derived from "
                "the sealed logical header"
            )
        self._validated = {
            key: value
            for key, value in derived_problem_header.items()
            if key != "groups"
        }
        self._validated["banks"] = banks
        self._validated["groups"] = groups
        return self._validated

    def iter_normalized_groups_by_id(self) -> Iterator[Mapping[str, Any]]:
        """Yield the validated production cursor in strict group-id order."""

        problem = self.validated_problem()
        domains = problem["domains"]
        domain_by_id = {
            domain["id"]: index for index, domain in enumerate(domains)
        }
        yield from _NormalizedGroupSequence(
            self._groups_by_id,
            schema=str(self.manifest["logical_schema"]),
            domain_by_id=domain_by_id,
        )

    def materialize(self) -> Dict[str, Any]:
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        return {
            **self._header,
            "bank_pairs": [dict(bank) for bank in self._banks],
            "groups": [dict(group) for group in self._groups],
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lease_state["open"] = False
        for sequence_name in ("_banks", "_normalized_banks", "_groups", "_groups_by_id"):
            sequence = getattr(self, sequence_name, None)
            if sequence is not None:
                sequence.close()
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None

    def __enter__(self) -> "BankChannelInputView":
        if self._closed:
            raise ValidationError("Chimew bank/channel view is closed")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def canonical_bank_channel_input_sha256(document: Mapping[str, Any]) -> str:
    if isinstance(document, BankChannelInputView):
        return document.logical_sha256
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _open_validated_view(
    path: Path, *, acquire_lease: bool = True
) -> BankChannelInputView:
    from .chimew_bank_channel import validate_chimew_bank_channel_input

    view = BankChannelInputView(path, _acquire_lease=acquire_lease)
    validate_chimew_bank_channel_input(view)
    return view


def _write_chimew_bank_channel_storage_locked(
    path: Path, document: Mapping[str, Any], *, source_bindings
) -> BankChannelInputView:
    """Validate and atomically publish one logical input as selector + store."""

    from .chimew_bank_channel import validate_chimew_bank_channel_input

    raw_groups = document.get("groups")
    raw_banks = document.get("bank_pairs")
    indexed = isinstance(document, BankChannelInputView)
    if (
        not isinstance(raw_banks, (Sequence if indexed else list))
        or isinstance(raw_banks, (str, bytes))
        or not raw_banks
    ):
        raise ValidationError("Chimew bank pairs are missing")
    if (
        not isinstance(raw_groups, (Sequence if indexed else list))
        or isinstance(raw_groups, (str, bytes))
        or not raw_groups
    ):
        raise ValidationError("Chimew signal groups are missing")
    problem = validate_chimew_bank_channel_input(document)
    header = {
        key: value
        for key, value in document.items()
        if key not in {"bank_pairs", "groups"}
    }
    problem_header = {
        key: value
        for key, value in problem.items()
        if key not in {"banks", "groups"}
    }
    logical_sha256 = canonical_bank_channel_input_sha256(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_selector = path.read_bytes() if path.is_file() else None
    previous_artifact: Path | None = None
    if path.is_file():
        try:
            previous = read_json(path)
        except (OSError, ValueError):
            previous = {}
        name = previous.get("artifact")
        if (
            previous.get("schema") == BANK_CHANNEL_STORAGE_SCHEMA
            and isinstance(name, str)
            and Path(name).name == name
        ):
            previous_artifact = path.with_name(name)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp")
    connection = sqlite3.connect(temporary)
    bank_digest = hashlib.sha256()
    digest = hashlib.sha256()
    bank_rows: list[tuple[int, str, int, str]] = []
    rows: list[tuple[int, str, str]] = []
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={BANK_CHANNEL_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={BANK_CHANNEL_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE groups(
              seq INTEGER PRIMARY KEY,
              group_id TEXT NOT NULL UNIQUE,
              record_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE banks(
              seq INTEGER PRIMARY KEY,
              bank_id TEXT NOT NULL UNIQUE,
              first_channel INTEGER NOT NULL,
              record_json TEXT NOT NULL,
              CHECK(first_channel>=0)
            ) WITHOUT ROWID;
            """
        )
        first_channel = 0
        for seq, (bank, normalized) in enumerate(zip(raw_banks, problem["banks"])):
            if not isinstance(bank, Mapping):
                raise ValidationError("Chimew bank/channel bank is malformed")
            bank_id = bank.get("id")
            if not isinstance(bank_id, str) or not bank_id:
                raise ValidationError("Chimew bank/channel bank identity is invalid")
            encoded = _canonical(bank)
            bank_digest.update(encoded.encode("utf-8"))
            bank_digest.update(b"\n")
            bank_rows.append((seq, bank_id, first_channel, encoded))
            first_channel += len(normalized["channels"])
            if len(bank_rows) >= 1024:
                connection.executemany(
                    "INSERT INTO banks VALUES (?,?,?,?)", bank_rows
                )
                bank_rows.clear()
        if bank_rows:
            connection.executemany(
                "INSERT INTO banks VALUES (?,?,?,?)", bank_rows
            )
        for seq, group in enumerate(raw_groups):
            if not isinstance(group, Mapping):
                raise ValidationError("Chimew bank/channel group is malformed")
            group_id = group.get("id")
            if not isinstance(group_id, str) or not group_id:
                raise ValidationError("Chimew bank/channel group identity is invalid")
            encoded = _canonical(group)
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
            rows.append((seq, group_id, encoded))
            if len(rows) >= 4096:
                connection.executemany("INSERT INTO groups VALUES (?,?,?)", rows)
                rows.clear()
        if rows:
            connection.executemany("INSERT INTO groups VALUES (?,?,?)", rows)
        banks_sha256 = bank_digest.hexdigest()
        groups_sha256 = digest.hexdigest()
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("problem_header", _canonical(problem_header)),
                ("banks_sha256", _canonical(banks_sha256)),
                ("groups_sha256", _canonical(groups_sha256)),
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
        raise ValidationError("Chimew bank/channel groups repeat an identity") from error
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
                "chimew-bank-input-store-v1",
                sources=source_bindings,
            )
        else:
            require_store_sources_unchanged(source_bindings)
            if file_sha256(artifact) != store_sha256:
                raise ValidationError("Chimew bank/channel artifact digest collision")
            temporary.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    manifest = {
        "schema": BANK_CHANNEL_STORAGE_SCHEMA,
        "logical_schema": header["schema"],
        "artifact": artifact.name,
        "application_id": BANK_CHANNEL_APPLICATION_ID,
        "user_version": BANK_CHANNEL_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "problem_header": problem_header,
        "bank_pairs": {"records": len(raw_banks), "sha256": banks_sha256},
        "groups": {"records": len(raw_groups), "sha256": groups_sha256},
        "logical_sha256": logical_sha256,
    }
    try:
        write_json(path, manifest, compact=True)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        view = _open_validated_view(path, acquire_lease=False)
    except BaseException:
        if previous_selector is None:
            path.unlink(missing_ok=True)
        else:
            restore = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
            restore.write_bytes(previous_selector)
            with restore.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(restore, path)
        if created and artifact != previous_artifact:
            artifact.unlink(missing_ok=True)
        raise
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view


def write_chimew_bank_channel_storage(
    path: Path, document: Mapping[str, Any], *, source_bindings=()
) -> BankChannelInputView:
    """Publish under an exclusive lease and return a leased new view."""

    path = Path(path)
    if getattr(document, "manifest_path", None) == path.resolve(strict=False):
        raise ValidationError(
            "cannot replace a Chimew bank/channel input from its active view"
        )
    source_bindings = tuple(source_bindings) + bind_view_sources(
        (document, "chimew-bank-input-store-v1")
    )
    require_store_sources_unchanged(source_bindings)
    lease = _bank_channel_lease(path, exclusive=True)
    descriptor = lease.__enter__()
    try:
        checked = _write_chimew_bank_channel_storage_locked(
            path, document, source_bindings=source_bindings
        )
        checked.close()
        # Atomically downgrade the writer lease.  No replacement can enter
        # between selector commit and construction of the returned view.
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        view = BankChannelInputView(path, _acquire_lease=False)
        view._lease = lease
        return view
    except BaseException:
        lease.__exit__(None, None, None)
        raise


def read_chimew_bank_channel_input(
    path: Path,
    *,
    materialize: bool = False,
    allow_legacy: bool = False,
    _verified_store_sha256: str | None = None,
) -> Mapping[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("Chimew bank/channel input must be an object")
    if value.get("schema") == BANK_CHANNEL_STORAGE_SCHEMA:
        view = BankChannelInputView(
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
            "legacy Chimew bank/channel JSON requires allow_legacy=True"
        )
    return value


def chimew_bank_channel_store_path(path: Path) -> Path | None:
    try:
        value = read_json(path)
    except (OSError, ValueError):
        return None
    if value.get("schema") != BANK_CHANNEL_STORAGE_SCHEMA:
        return None
    with BankChannelInputView(path) as view:
        return view.store_path


def copy_chimew_bank_channel_storage(
    source: Path, destination: Path, *, allow_legacy: bool = False
) -> None:
    """Copy a selector with its immutable sidecar, or one explicit legacy JSON."""

    source = Path(source)
    destination = Path(destination)
    value = read_json(source)
    if not isinstance(value, dict):
        raise ValidationError("Chimew bank/channel input must be an object")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if value.get("schema") != BANK_CHANNEL_STORAGE_SCHEMA:
        if not allow_legacy:
            raise ValidationError(
                "legacy Chimew bank/channel copy requires allow_legacy=True"
            )
        with _bank_channel_lease(destination, exclusive=True):
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
    with BankChannelInputView(source) as view:
        with _bank_channel_lease(destination, exclusive=True):
            previous_selector = (
                destination.read_bytes() if destination.is_file() else None
            )
            previous_artifact: Path | None = None
            if previous_selector is not None:
                try:
                    previous = json.loads(previous_selector)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    previous = {}
                previous_name = previous.get("artifact")
                if (
                    previous.get("schema") == BANK_CHANNEL_STORAGE_SCHEMA
                    and isinstance(previous_name, str)
                    and Path(previous_name).name == previous_name
                ):
                    previous_artifact = destination.with_name(previous_name)
            sidecar = destination.with_name(
                f"{destination.stem}.{view.manifest['store_sha256']}.sqlite"
            )
            created_sidecar = not sidecar.exists()
            if created_sidecar:
                temporary = destination.with_name(
                    f".{destination.stem}.{uuid.uuid4().hex}.sqlite.tmp"
                )
                try:
                    shutil.copy2(view.store_path, temporary)
                    with temporary.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(temporary, sidecar)
                    _fsync_directory(destination.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            elif file_sha256(sidecar) != view.manifest["store_sha256"]:
                raise ValidationError("copied Chimew bank/channel sidecar collides")
            selector = dict(view.manifest)
            selector["artifact"] = sidecar.name
            try:
                write_json(destination, selector, compact=True)
                _fsync_directory(destination.parent)
                copied = BankChannelInputView(
                    destination, _acquire_lease=False
                )
                try:
                    if copied.store_path != sidecar.resolve():
                        raise ValidationError(
                            "copied Chimew bank/channel sidecar differs"
                        )
                finally:
                    copied.close()
            except BaseException:
                if previous_selector is None:
                    destination.unlink(missing_ok=True)
                else:
                    restore = destination.with_name(
                        f".{destination.name}.{uuid.uuid4().hex}.restore"
                    )
                    restore.write_bytes(previous_selector)
                    with restore.open("rb") as stream:
                        os.fsync(stream.fileno())
                    os.replace(restore, destination)
                    _fsync_directory(destination.parent)
                if created_sidecar:
                    sidecar.unlink(missing_ok=True)
                raise
            if previous_artifact is not None and previous_artifact != sidecar:
                previous_artifact.unlink(missing_ok=True)
                _fsync_directory(destination.parent)
