"""Indexed storage for the Chimew Phase 6 electrical binding.

The logical v1/v2 binding remains readable.  Production artifacts keep their
small header in a JSON selector and normalize group records and schedule-entry
membership into a sealed SQLite sidecar.  A schedule entry is therefore stored
once instead of being embedded in one graph-sized JSON array.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from .errors import ValidationError
from .io import file_sha256, read_json, write_json


CHIMEW_BINDING_STORAGE_SCHEMA = "emuflow.chimew-binding-storage/v1"
CHIMEW_BINDING_APPLICATION_ID = 0x454D5542  # ASCII "EMUB"
CHIMEW_BINDING_USER_VERSION = 1
_LOGICAL_SCHEMAS = {
    "emuflow.chimew-phase6-electrical-binding/v1",
    "emuflow.chimew-phase6-electrical-binding/v2",
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


def _logical_sha256(
    header: Mapping[str, Any], records: int, entries_sha256: str, members: int,
    members_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "header": header,
                "entries": {"records": records, "sha256": entries_sha256},
                "members": {"records": members, "sha256": members_sha256},
            }
        ).encode("utf-8")
    ).hexdigest()


def _start_canonical_document_hash(
    header: Mapping[str, Any],
) -> tuple[Any, list[str], int]:
    """Start a canonical object hash immediately before its entries array."""

    digest = hashlib.sha256()
    keys = sorted((*header.keys(), "entries"))
    entries_index = keys.index("entries")
    digest.update(b"{")
    for index, key in enumerate(keys[: entries_index + 1]):
        if index:
            digest.update(b",")
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        if key == "entries":
            digest.update(b"[")
        else:
            digest.update(_canonical(header[key]).encode("utf-8"))
    return digest, keys, entries_index


def _finish_canonical_document_hash(
    digest: Any,
    header: Mapping[str, Any],
    keys: list[str],
    entries_index: int,
) -> str:
    digest.update(b"]")
    for key in keys[entries_index + 1 :]:
        digest.update(b",")
        digest.update(_canonical(key).encode("utf-8"))
        digest.update(b":")
        digest.update(_canonical(header[key]).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _group_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["group_json"])
    except json.JSONDecodeError as error:
        raise ValidationError("Chimew binding group payload is corrupt") from error
    if not isinstance(value, dict) or "schedule_entries" in value:
        raise ValidationError("Chimew binding group payload is malformed")
    expected = {
        "group": row["group_id"],
        "link": row["link_id"],
        "fpga_a": row["fpga_a"],
        "fpga_b": row["fpga_b"],
        "physical_lane": row["physical_lane"],
        "direction": row["direction"],
        "package_pin_a": row["package_pin_a"],
        "package_pin_b": row["package_pin_b"],
    }
    if any(value.get(key) != field for key, field in expected.items()):
        raise ValidationError("Chimew binding typed group columns disagree")
    return value


class ChimewBindingEntrySequence(Sequence[Mapping[str, Any]]):
    def __init__(self, store_path: Path, count: int):
        self.store_path = store_path
        self._count = count

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            group_cursor = connection.execute(
                "SELECT * FROM groups ORDER BY seq"
            )
            member_cursor = iter(
                connection.execute(
                    "SELECT group_seq,member FROM members "
                    "ORDER BY group_seq,member_seq"
                )
            )
            member_row = next(member_cursor, None)
            observed = 0
            for group_row in group_cursor:
                group_seq = int(group_row["seq"])
                members: list[str] = []
                while member_row is not None and int(member_row[0]) == group_seq:
                    members.append(str(member_row[1]))
                    member_row = next(member_cursor, None)
                if not members:
                    raise ValidationError("Chimew binding group has no members")
                observed += 1
                yield {
                    **_group_from_row(group_row),
                    "schedule_entries": members,
                }
            if observed != self._count or member_row is not None:
                raise ValidationError("Chimew binding relation coverage disagrees")
        finally:
            connection.close()

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        if index < 0:
            index += self._count
        if index < 0 or index >= self._count:
            raise IndexError(index)
        connection = _connect(self.store_path)
        try:
            row = connection.execute(
                "SELECT * FROM groups WHERE seq=?", (index,)
            ).fetchone()
            if row is None:
                raise ValidationError("Chimew binding group sequence is not contiguous")
            members = [
                str(member[0])
                for member in connection.execute(
                    "SELECT member FROM members WHERE group_seq=? "
                    "ORDER BY member_seq",
                    (index,),
                )
            ]
            if not members:
                raise ValidationError("Chimew binding group has no members")
            return {**_group_from_row(row), "schedule_entries": members}
        finally:
            connection.close()


class ChimewBindingView(Mapping[str, Any]):
    def __init__(self, manifest_path: Path):
        self.manifest_path = Path(manifest_path).resolve()
        manifest = read_json(self.manifest_path)
        header = manifest.get("header")
        entries = manifest.get("entries")
        members = manifest.get("members")
        if (
            manifest.get("schema") != CHIMEW_BINDING_STORAGE_SCHEMA
            or manifest.get("logical_schema") not in _LOGICAL_SCHEMAS
            or manifest.get("application_id") != CHIMEW_BINDING_APPLICATION_ID
            or manifest.get("user_version") != CHIMEW_BINDING_USER_VERSION
            or not isinstance(header, dict)
            or not isinstance(entries, dict)
            or not isinstance(members, dict)
            or header.get("schema") != manifest.get("logical_schema")
            or "entries" in header
        ):
            raise ValidationError("Chimew binding storage manifest is malformed")
        for label, relation in (("entries", entries), ("members", members)):
            if (
                isinstance(relation.get("records"), bool)
                or not isinstance(relation.get("records"), int)
                or relation["records"] < 0
                or not _valid_digest(relation.get("sha256"))
            ):
                raise ValidationError(
                    f"Chimew binding {label} seal is malformed"
                )
        if entries["records"] == 0 and members["records"] != 0:
            raise ValidationError("Chimew binding relation counts disagree")
        logical_sha256 = _logical_sha256(
            header,
            entries["records"],
            entries["sha256"],
            members["records"],
            members["sha256"],
        )
        if manifest.get("logical_sha256") != logical_sha256:
            raise ValidationError("Chimew binding logical seal disagrees")
        if not _valid_digest(manifest.get("canonical_sha256")):
            raise ValidationError("Chimew binding canonical seal is malformed")
        artifact = manifest.get("artifact")
        if (
            not isinstance(artifact, str)
            or Path(artifact).name != artifact
            or Path(artifact).suffix != ".sqlite"
        ):
            raise ValidationError("Chimew binding storage path is invalid")
        store_path = self.manifest_path.with_name(artifact)
        try:
            stat = store_path.stat()
        except OSError as error:
            raise ValidationError("Chimew binding SQLite store is missing") from error
        if (
            isinstance(manifest.get("store_bytes"), bool)
            or manifest.get("store_bytes") != stat.st_size
            or not _valid_digest(manifest.get("store_sha256"))
            or file_sha256(store_path) != manifest["store_sha256"]
        ):
            raise ValidationError("Chimew binding SQLite store seal disagrees")
        connection = _connect(store_path)
        try:
            if (
                connection.execute("PRAGMA application_id").fetchone()[0]
                != CHIMEW_BINDING_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != CHIMEW_BINDING_USER_VERSION
            ):
                raise ValidationError("unsupported Chimew binding SQLite artifact")
            try:
                metadata = {
                    str(row[0]): json.loads(row[1])
                    for row in connection.execute("SELECT key,value FROM metadata")
                }
            except json.JSONDecodeError as error:
                raise ValidationError(
                    "Chimew binding SQLite metadata is corrupt"
                ) from error
            group_count, minimum_seq, maximum_seq = connection.execute(
                "SELECT count(*),min(seq),max(seq) FROM groups"
            ).fetchone()
            member_count = connection.execute(
                "SELECT count(*) FROM members"
            ).fetchone()[0]
            orphan = connection.execute(
                "SELECT member FROM members LEFT JOIN groups "
                "ON groups.seq=members.group_seq WHERE groups.seq IS NULL LIMIT 1"
            ).fetchone()
            empty_group = connection.execute(
                "SELECT groups.seq FROM groups LEFT JOIN members "
                "ON members.group_seq=groups.seq "
                "GROUP BY groups.seq HAVING count(members.member)=0 LIMIT 1"
            ).fetchone()
            noncontiguous_members = connection.execute(
                "SELECT group_seq FROM members GROUP BY group_seq "
                "HAVING min(member_seq)!=0 "
                "OR max(member_seq)!=count(*)-1 LIMIT 1"
            ).fetchone()
            if (
                metadata.get("header") != header
                or metadata.get("entries_sha256") != entries["sha256"]
                or metadata.get("members_sha256") != members["sha256"]
                or metadata.get("logical_sha256") != logical_sha256
                or metadata.get("canonical_sha256")
                != manifest["canonical_sha256"]
                or group_count != entries["records"]
                or member_count != members["records"]
                or orphan is not None
                or empty_group is not None
                or noncontiguous_members is not None
                or (
                    group_count > 0
                    and (minimum_seq != 0 or maximum_seq != group_count - 1)
                )
            ):
                raise ValidationError("Chimew binding SQLite metadata disagrees")
        finally:
            connection.close()
        self.manifest = manifest
        self.store_path = store_path
        self._header = dict(header)
        self._entries = ChimewBindingEntrySequence(
            store_path, entries["records"]
        )
        self.canonical_sha256 = manifest["canonical_sha256"]
        self.member_count = members["records"]

    def __getitem__(self, key: str) -> Any:
        if key == "entries":
            return self._entries
        return self._header[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._header
        yield "entries"

    def __len__(self) -> int:
        return len(self._header) + 1

    def iter_group_records(self) -> Iterator[Mapping[str, Any]]:
        """Yield typed group records without expanding schedule members."""

        connection = _connect(self.store_path)
        try:
            for row in connection.execute("SELECT * FROM groups ORDER BY seq"):
                yield _group_from_row(row)
        finally:
            connection.close()

    @contextmanager
    def open_schedule_entry_lookup(
        self,
    ) -> Iterator[Callable[[str], Mapping[str, Any] | None]]:
        """Open a bounded-memory schedule-entry to binding-group lookup.

        The member relation has a primary-key index on the schedule-entry ID.
        Keep one immutable connection open for a consumer batch so graph-sized
        physical flows do not expand the member relation into a Python dict or
        reopen SQLite for every endpoint.
        """

        connection = _connect(self.store_path)
        try:
            def lookup(entry_id: str) -> Mapping[str, Any] | None:
                if not isinstance(entry_id, str) or not entry_id:
                    raise ValidationError(
                        "Chimew binding schedule-entry identity is malformed"
                    )
                try:
                    row = connection.execute(
                        "SELECT members.member AS indexed_member, groups.* "
                        "FROM members JOIN groups "
                        "ON groups.seq=members.group_seq "
                        "WHERE members.member=?",
                        (entry_id,),
                    ).fetchone()
                except sqlite3.DatabaseError as error:
                    raise ValidationError(
                        "Chimew binding member index is corrupt"
                    ) from error
                if row is None:
                    return None
                if row["indexed_member"] != entry_id:
                    raise ValidationError(
                        "Chimew binding indexed identity disagrees"
                    )
                return _group_from_row(row)

            yield lookup
        finally:
            connection.close()

    def materialize(self) -> dict[str, Any]:
        return {**self._header, "entries": [dict(row) for row in self._entries]}


def write_chimew_binding_storage(
    path: Path, binding: Mapping[str, Any]
) -> ChimewBindingView:
    if binding.get("schema") not in _LOGICAL_SCHEMAS:
        raise ValidationError("Chimew binding has an unsupported schema")
    records = binding.get("entries")
    if not isinstance(records, Iterable) or isinstance(records, (str, bytes)):
        raise ValidationError("Chimew binding entries must be iterable")
    header = {key: value for key, value in binding.items() if key != "entries"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_artifact: Path | None = None
    if path.is_file():
        try:
            previous = read_json(path)
        except (OSError, ValueError):
            previous = {}
        previous_name = previous.get("artifact")
        if (
            previous.get("schema") == CHIMEW_BINDING_STORAGE_SCHEMA
            and isinstance(previous_name, str)
            and Path(previous_name).name == previous_name
            and Path(previous_name).suffix == ".sqlite"
        ):
            previous_artifact = path.with_name(previous_name)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.sqlite.tmp")
    connection = sqlite3.connect(temporary)
    entries_digest = hashlib.sha256()
    members_digest = hashlib.sha256()
    group_count = 0
    member_count = 0
    canonical_digest, canonical_keys, entries_key_index = (
        _start_canonical_document_hash(header)
    )
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(f"PRAGMA application_id={CHIMEW_BINDING_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={CHIMEW_BINDING_USER_VERSION}")
        connection.executescript(
            """
            CREATE TABLE metadata(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE groups(
              seq INTEGER NOT NULL UNIQUE,
              group_id TEXT PRIMARY KEY,
              link_id TEXT NOT NULL,
              fpga_a TEXT NOT NULL,
              fpga_b TEXT NOT NULL,
              physical_lane INTEGER NOT NULL,
              direction TEXT NOT NULL,
              package_pin_a TEXT NOT NULL,
              package_pin_b TEXT NOT NULL,
              group_json TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE members(
              member TEXT PRIMARY KEY,
              group_seq INTEGER NOT NULL,
              member_seq INTEGER NOT NULL,
              UNIQUE(group_seq,member_seq)
            ) WITHOUT ROWID;
            CREATE INDEX members_by_group
              ON members(group_seq,member_seq);
            """
        )
        group_rows: list[tuple[Any, ...]] = []
        member_rows: list[tuple[Any, ...]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise ValidationError("Chimew binding entry is malformed")
            full = dict(record)
            members = full.pop("schedule_entries", None)
            required_strings = (
                "group", "link", "fpga_a", "fpga_b", "direction",
                "package_pin_a", "package_pin_b",
            )
            if (
                not isinstance(members, list)
                or not members
                or not all(isinstance(member, str) and member for member in members)
                or any(not isinstance(full.get(key), str) or not full[key]
                       for key in required_strings)
                or isinstance(full.get("physical_lane"), bool)
                or not isinstance(full.get("physical_lane"), int)
            ):
                raise ValidationError("Chimew binding entry is malformed")
            normalized = {**full, "schedule_entries": list(members)}
            encoded = _canonical(normalized)
            entries_digest.update(encoded.encode("utf-8"))
            entries_digest.update(b"\n")
            if group_count:
                canonical_digest.update(b",")
            canonical_digest.update(encoded.encode("utf-8"))
            group_rows.append(
                (
                    group_count,
                    full["group"],
                    full["link"],
                    full["fpga_a"],
                    full["fpga_b"],
                    full["physical_lane"],
                    full["direction"],
                    full["package_pin_a"],
                    full["package_pin_b"],
                    _canonical(full),
                )
            )
            for member_seq, member in enumerate(members):
                members_digest.update(
                    _canonical(
                        {
                            "group_seq": group_count,
                            "member_seq": member_seq,
                            "member": member,
                        }
                    ).encode("utf-8")
                )
                members_digest.update(b"\n")
                member_rows.append((member, group_count, member_seq))
                member_count += 1
            group_count += 1
            if len(member_rows) >= 4096:
                connection.executemany("INSERT INTO members VALUES (?,?,?)", member_rows)
                member_rows.clear()
            if len(group_rows) >= 1024:
                connection.executemany(
                    "INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?)", group_rows
                )
                group_rows.clear()
        if group_rows:
            connection.executemany(
                "INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?,?)", group_rows
            )
        if member_rows:
            connection.executemany("INSERT INTO members VALUES (?,?,?)", member_rows)
        entries_sha256 = entries_digest.hexdigest()
        members_sha256 = members_digest.hexdigest()
        logical_sha256 = _logical_sha256(
            header, group_count, entries_sha256, member_count, members_sha256
        )
        canonical_sha256 = _finish_canonical_document_hash(
            canonical_digest, header, canonical_keys, entries_key_index
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (
                ("header", _canonical(header)),
                ("entries_sha256", _canonical(entries_sha256)),
                ("members_sha256", _canonical(members_sha256)),
                ("logical_sha256", _canonical(logical_sha256)),
                ("canonical_sha256", _canonical(canonical_sha256)),
            ),
        )
        connection.commit()
        connection.close()
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
    except sqlite3.IntegrityError as error:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise ValidationError(
            "Chimew binding repeats a group or schedule entry"
        ) from error
    except BaseException:
        try:
            connection.close()
        finally:
            temporary.unlink(missing_ok=True)
        raise
    store_sha256 = file_sha256(temporary)
    artifact = path.with_name(f"{path.stem}.{store_sha256}.sqlite")
    created_artifact = not artifact.exists()
    if created_artifact:
        os.replace(temporary, artifact)
    else:
        if file_sha256(artifact) != store_sha256:
            temporary.unlink(missing_ok=True)
            raise ValidationError("Chimew binding artifact digest collision")
        temporary.unlink()
    manifest = {
        "schema": CHIMEW_BINDING_STORAGE_SCHEMA,
        "logical_schema": header["schema"],
        "artifact": artifact.name,
        "application_id": CHIMEW_BINDING_APPLICATION_ID,
        "user_version": CHIMEW_BINDING_USER_VERSION,
        "store_bytes": artifact.stat().st_size,
        "store_sha256": store_sha256,
        "header": header,
        "entries": {"records": group_count, "sha256": entries_sha256},
        "members": {"records": member_count, "sha256": members_sha256},
        "logical_sha256": logical_sha256,
        "canonical_sha256": canonical_sha256,
    }
    try:
        write_json(path, manifest, compact=True)
    except BaseException:
        if created_artifact and artifact != previous_artifact:
            artifact.unlink(missing_ok=True)
        raise
    view = ChimewBindingView(path)
    if previous_artifact is not None and previous_artifact != artifact:
        previous_artifact.unlink(missing_ok=True)
    return view


def read_chimew_binding(
    path: Path, *, materialize: bool = False
) -> Mapping[str, Any]:
    value = read_json(path)
    if value.get("schema") == CHIMEW_BINDING_STORAGE_SCHEMA:
        view = ChimewBindingView(path)
        return view.materialize() if materialize else view
    return value


@contextmanager
def open_chimew_binding_schedule_entry_lookup(
    binding: Mapping[str, Any],
) -> Iterator[Callable[[str], Mapping[str, Any] | None]]:
    """Open one lookup contract for indexed and legacy binding artifacts.

    Production selectors use :class:`ChimewBindingView` and therefore retain
    only one group record per lookup.  Legacy JSON remains supported with the
    same duplicate and shape checks as the former physical-flow adapter.
    """

    if isinstance(binding, ChimewBindingView):
        with binding.open_schedule_entry_lookup() as lookup:
            yield lookup
        return

    records = binding.get("entries", [])
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise ValidationError("Chimew electrical binding entries must be an array")
    legacy_index: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValidationError("Chimew electrical binding entry is malformed")
        members = record.get("schedule_entries")
        if (
            not isinstance(members, Sequence)
            or isinstance(members, (str, bytes))
            or not members
        ):
            raise ValidationError("Chimew electrical binding entry is malformed")
        for entry_id in members:
            if not isinstance(entry_id, str) or not entry_id:
                raise ValidationError("Chimew electrical binding entry is malformed")
            if entry_id in legacy_index:
                raise ValidationError(
                    "Chimew electrical binding duplicates a schedule entry"
                )
            legacy_index[entry_id] = record
    yield legacy_index.get


def chimew_binding_store_path(selector_path: Path) -> Path | None:
    value = read_json(selector_path)
    if value.get("schema") != CHIMEW_BINDING_STORAGE_SCHEMA:
        return None
    artifact = value.get("artifact")
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValidationError("Chimew binding storage path is invalid")
    return Path(selector_path).with_name(artifact)
