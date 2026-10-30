"""Versioned indexed storage for Phase 6 signal position hints."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import require_store_sources_unchanged


POSITION_HINTS_LOGICAL_SCHEMA = "emuflow.signal-position-hints/v1"
POSITION_HINTS_STORAGE_SCHEMA = "emuflow.signal-position-hints-storage/v1"
POSITION_HINTS_APPLICATION_ID = 0x454D5548  # ASCII "EMUH"
POSITION_HINTS_USER_VERSION = 1


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )




def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        immutable_sqlite_uri(path), uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


class PositionHintEntrySequence(Sequence[Mapping[str, Any]]):
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
                    raise ValidationError(
                        "position-hint entry is corrupt"
                    ) from error
                if not isinstance(value, dict):
                    raise ValidationError(
                        "position-hint entry is not an object"
                    )
                observed += 1
                yield value
            if observed != self._count:
                raise ValidationError("position-hint entry count disagrees")
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
                "SELECT record_json FROM entries ORDER BY seq LIMIT 1 OFFSET ?",
                (index,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationError("position-hint sequence is not contiguous")
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as error:
            raise ValidationError("position-hint entry is corrupt") from error
        if not isinstance(value, dict):
            raise ValidationError("position-hint entry is not an object")
        return value


class PositionHintsView(Mapping[str, Any]):
    def __init__(self, manifest_path: Path):
        from emuflow_tools.transport.pin_artifacts_native import read_pin_manifest
        self.manifest_path = manifest_path.resolve()
        self.manifest, self._source_bindings = read_pin_manifest(self.manifest_path, "position-hints-store-v1")
        self.store_path = self.manifest_path.with_name(self.manifest["artifact"])
        self._header = dict(self.manifest["header"])
        entries = self.manifest["entries"]
        self._entries = PositionHintEntrySequence(self.store_path, entries["records"], entries["sha256"])

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

    def iter_by_identity(self) -> Iterator[Mapping[str, Any]]:
        connection = _connect(self.store_path)
        try:
            for row in connection.execute(
                "SELECT record_json FROM entries ORDER BY schedule_entry"
            ):
                value = json.loads(row[0])
                if not isinstance(value, dict):
                    raise ValidationError(
                        "position-hint entry is not an object"
                    )
                yield value
        except json.JSONDecodeError as error:
            raise ValidationError("position-hint entry is corrupt") from error
        finally:
            connection.close()

    def attach_validation_relation(
        self, connection: sqlite3.Connection
    ) -> tuple[int, int]:
        """Attach a typed read-only relation as ``position_rows``."""

        connection.execute(
            "ATTACH DATABASE ? AS position_store",
            (
                immutable_sqlite_uri(self.store_path),
            ),
        )
        mismatch = connection.execute(
            """
            SELECT schedule_entry FROM position_store.entries
            WHERE schedule_entry IS NOT json_extract(
                    record_json,'$.schedule_entry'
                  )
               OR source_y IS NOT json_extract(record_json,'$.source_y')
               OR sink_y IS NOT json_extract(record_json,'$.sink_y')
               OR source_region IS NOT json_extract(
                    record_json,'$.source_region'
                  )
               OR sink_region IS NOT json_extract(
                    record_json,'$.sink_region'
                  )
               OR source_fallback IS NOT json_extract(
                    record_json,'$.source_fallback'
                  )
               OR sink_fallback IS NOT json_extract(
                    record_json,'$.sink_fallback'
                  )
            LIMIT 1
            """
        ).fetchone()
        if mismatch is not None:
            raise ValidationError(
                f"position-hint indexed record {mismatch[0]!r} disagrees"
            )
        connection.execute(
            """
            CREATE TEMP VIEW position_rows AS
            SELECT schedule_entry AS entry_id,
                   source_y,sink_y,source_region,sink_region,
                   source_fallback,sink_fallback
            FROM position_store.entries
            """
        )
        row = connection.execute(
            """
            SELECT count(*),coalesce(sum(source_fallback+sink_fallback),0)
            FROM position_store.entries
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def materialize(self, *, verify_records: bool = True) -> dict[str, Any]:
        digest = hashlib.sha256()
        values = []
        for record in self._entries:
            value = dict(record)
            digest.update(_canonical(value).encode("utf-8"))
            digest.update(b"\n")
            values.append(value)
        if verify_records and digest.hexdigest() != self._entries.expected_sha256:
            raise ValidationError("position-hint entry seal disagrees")
        return {**self._header, "entries": values}




def read_position_hints(path: Path, *, materialize: bool = False) -> Mapping[str, Any]:
    view = PositionHintsView(path)
    return view.materialize() if materialize else view


def copy_position_hints_storage(source, path, *, source_bindings=()):
    from emuflow_tools.transport.pin_artifacts_native import copy_pin_artifact
    require_store_sources_unchanged(source_bindings)
    selector = source if isinstance(source, Path) else getattr(source, "manifest_path", None)
    if selector is None:
        raise ValidationError("pin artifact copying requires a current typed owner")
    copy_pin_artifact(selector, "position-hints-store-v1", path)
    require_store_sources_unchanged(source_bindings)
    return PositionHintsView(path)
