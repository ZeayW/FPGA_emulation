"""Indexed storage for the Chimew Phase 6 electrical binding.

The logical v1/v2 binding remains readable; v3 seals fixed LVDS P/N wiring.  Production artifacts keep their
small header in a JSON selector and normalize group records and schedule-entry
membership into a sealed SQLite sidecar.  A schedule entry is therefore stored
once instead of being embedded in one graph-sized JSON array.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.storage.store_publication import require_store_sources_unchanged


CHIMEW_BINDING_STORAGE_SCHEMA = "emuflow.chimew-binding-storage/v1"
CHIMEW_BINDING_APPLICATION_ID = 0x454D5542  # ASCII "EMUB"
CHIMEW_BINDING_USER_VERSION = 1
_LOGICAL_SCHEMAS = {
    "emuflow.chimew-phase6-electrical-binding/v1",
    "emuflow.chimew-phase6-electrical-binding/v2",
    "emuflow.chimew-phase6-electrical-binding/v3",
}


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )






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
        from emuflow.transport.pin_artifacts_native import read_pin_manifest
        self.manifest_path = manifest_path.resolve()
        self.manifest, self._source_bindings = read_pin_manifest(self.manifest_path, "chimew-binding-store-v1")
        self.store_path = self.manifest_path.with_name(self.manifest["artifact"])
        self._header = dict(self.manifest["header"])
        entries = self.manifest["entries"]
        members = self.manifest["members"]
        self._entries = ChimewBindingEntrySequence(self.store_path, entries["records"])
        self.canonical_sha256 = self.manifest["canonical_sha256"]
        self.member_count = members["records"]

    def bound_store_sources(self):
        require_store_sources_unchanged(self._source_bindings)
        return self._source_bindings

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




def read_chimew_binding(path: Path, *, materialize: bool = False) -> Mapping[str, Any]:
    view = ChimewBindingView(path)
    return view.materialize() if materialize else view




def chimew_binding_store_path(selector_path: Path) -> Path | None:
    value = read_json(selector_path)
    if value.get("schema") != CHIMEW_BINDING_STORAGE_SCHEMA:
        return None
    artifact = value.get("artifact")
    if not isinstance(artifact, str) or Path(artifact).name != artifact:
        raise ValidationError("Chimew binding storage path is invalid")
    return Path(selector_path).with_name(artifact)


def copy_chimew_binding_storage(source, path, *, source_bindings=()):
    from emuflow.transport.pin_artifacts_native import copy_pin_artifact
    require_store_sources_unchanged(source_bindings)
    selector = source if isinstance(source, Path) else getattr(source, "manifest_path", None)
    if selector is None:
        raise ValidationError("pin artifact copying requires a current typed owner")
    copy_pin_artifact(selector, "chimew-binding-store-v1", path)
    require_store_sources_unchanged(source_bindings)
    return ChimewBindingView(path)
