"""Indexed Phase 6 Chimew pin-plan storage.

The logical v1 pin-plan contract remains unchanged.  Production Phase 6
publishes its hop-sized records once in SQLite and keeps only the small header
and relation seals in the JSON selector.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import require_store_sources_unchanged


PIN_PLAN_STORAGE_SCHEMA = "emuflow.pin-plan-storage/v1"
PIN_PLAN_APPLICATION_ID = 0x454D5550  # ASCII "EMUP"
PIN_PLAN_USER_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )




def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class PinPlanEntrySequence(Sequence[Mapping[str, Any]]):
    def __init__(self, store_path: Path, count: int, digest: str):
        self.store_path = store_path
        self._count = count
        self.expected_sha256 = digest

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            observed = 0
            for row in connection.execute(
                "SELECT record_json FROM entries ORDER BY seq"
            ):
                try:
                    value = json.loads(row[0])
                except json.JSONDecodeError as error:
                    raise ValidationError("pin-plan entry is corrupt") from error
                if not isinstance(value, dict):
                    raise ValidationError("pin-plan entry is not an object")
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("pin-plan entry count disagrees")
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
                "SELECT record_json FROM entries ORDER BY seq "
                "LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError("pin-plan sequence is not contiguous")
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError("pin-plan entry is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("pin-plan entry is not an object")
        return value


class PinPlanView(Mapping[str, Any]):
    def __init__(self, manifest_path: Path):
        from emuflow_tools.transport.pin_artifacts_native import read_pin_manifest
        self.manifest_path = manifest_path.resolve()
        self.manifest, self._source_bindings = read_pin_manifest(self.manifest_path, "pin-plan-store-v1")
        self.store_path = self.manifest_path.with_name(self.manifest["artifact"])
        self._header = dict(self.manifest["header"])
        entries = self.manifest["entries"]
        self._entries = PinPlanEntrySequence(self.store_path, entries["records"], entries["sha256"])

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

    def iter_lane_records(self) -> Iterator[tuple[str, int, int]]:
        """Yield identity, physical lane, and logical lane in schedule-ID order."""

        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT schedule_entry,physical_lane,logical_lane "
                "FROM entries ORDER BY length(schedule_entry),schedule_entry"
            ):
                yield str(row[0]), int(row[1]), int(row[2])
        finally:
            connection.close()

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        digest = hashlib.sha256()
        values = []
        for record in self._entries:
            value = dict(record)
            digest.update(_canonical(value).encode("utf-8"))
            digest.update(b"\n")
            values.append(value)
        if verify_records and digest.hexdigest() != self._entries.expected_sha256:
            raise ValidationError("pin-plan entry seal disagrees")
        return {**self._header, "entries": values}




def read_pin_plan(path: Path, *, materialize: bool = False) -> Mapping[str, Any]:
    view = PinPlanView(path)
    return view.materialize() if materialize else view


def copy_pin_plan_storage(source, path, *, source_bindings=()):
    from emuflow_tools.transport.pin_artifacts_native import copy_pin_artifact
    require_store_sources_unchanged(source_bindings)
    selector = source if isinstance(source, Path) else getattr(source, "manifest_path", None)
    if selector is None:
        raise ValidationError("pin artifact copying requires a current typed owner")
    copy_pin_artifact(selector, "pin-plan-store-v1", path)
    require_store_sources_unchanged(source_bindings)
    return PinPlanView(path)
