"""Indexed on-disk storage for large EmuIR graphs.

The logical EmuIR schema remains ``emuflow.emuir/v1``.  This module changes
only its physical representation: repeated instance/net/endpoint records are
stored as normalized SQLite rows instead of one monolithic JSON document.
Small, irregular dictionaries (cell parameters and attributes) remain
canonical JSON *row values*; no consumer has to parse the complete graph to
reach one record or one summary.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator


EMUIR_STORAGE_SCHEMA = "emuflow.emuir-sqlite/v1"
EMUIR_COMPACT_STORAGE_SCHEMA = "emuflow.emuir-sqlite/v2"
EMUIR_STORAGE_SUFFIX = ".emuir.sqlite"
EMUIR_SQLITE_APPLICATION_ID = 0x454D5549  # ASCII "EMUI"
EMUIR_SQLITE_USER_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(value: str, context: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context}: invalid canonical JSON row") from error


def open_emuir_sqlite_read_only(path: Path) -> sqlite3.Connection:
    """Open and validate a sealed indexed EmuIR artifact without journals."""

    if not is_emuir_sqlite(path):
        raise ValueError(f"{path}: not an EmuIR SQLite artifact")
    # ``immutable=1`` prevents journal creation beside a sealed artifact and
    # is safe because EmuFlow never mutates a published EmuIR store in place.
    from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
    connection = sqlite3.connect(immutable_sqlite_uri(path), uri=True)
    connection.row_factory = sqlite3.Row
    try:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (
            application_id != EMUIR_SQLITE_APPLICATION_ID
            or version not in {EMUIR_SQLITE_USER_VERSION, 2}
        ):
            raise ValueError(f"{path}: unsupported EmuIR SQLite header")
        storage_schema = connection.execute(
            "SELECT value FROM metadata WHERE key = 'storage_schema'"
        ).fetchone()
        if storage_schema is None or storage_schema[0] not in {EMUIR_STORAGE_SCHEMA, EMUIR_COMPACT_STORAGE_SCHEMA}:
            raise ValueError(f"{path}: unsupported EmuIR storage schema")
        if (version == 2) != (storage_schema[0] == EMUIR_COMPACT_STORAGE_SCHEMA):
            raise ValueError(f"{path}: inconsistent EmuIR storage version")
        return connection
    except BaseException:
        connection.close()
        raise


def is_emuir_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


class _RowsForOwner:
    """Merge ordered child rows with their owner using one-row lookahead."""

    def __init__(self, rows: Iterable[sqlite3.Row], owner_column: str):
        self._rows = iter(rows)
        self._owner_column = owner_column
        self._next = next(self._rows, None)

    def take(self, owner: int) -> Iterator[sqlite3.Row]:
        if self._next is not None and self._next[self._owner_column] < owner:
            raise ValueError(f"EmuIR SQLite orphaned {self._owner_column} row")
        while self._next is not None and self._next[self._owner_column] == owner:
            row = self._next
            self._next = next(self._rows, None)
            yield row

    def finish(self) -> None:
        if self._next is not None:
            raise ValueError(f"EmuIR SQLite orphaned {self._owner_column} row")


def read_emuir_sqlite(path: Path) -> Dict[str, Any]:
    """Read an indexed EmuIR store into the existing logical Python schema."""

    from emuflow.ir.emuir_compact import read_compact_emuir, is_compact_emuir
    if is_compact_emuir(path):
        return read_compact_emuir(path)

    connection = open_emuir_sqlite_read_only(path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM metadata")
        }

        port_constants = _RowsForOwner(connection.execute(
            "SELECT * FROM port_constants ORDER BY port_seq, ordinal"
        ), "port_seq")
        ports = []
        for row in connection.execute("SELECT * FROM ports ORDER BY seq"):
            record = {
                "id": row["id"],
                "direction": row["direction"],
                "width": row["width"],
                "constant_connections": [
                    {
                        "bit": child["bit"], "value": child["value"],
                        **_decode(child["extras_json"], "port constant extras"),
                    }
                    for child in port_constants.take(row["seq"])
                ],
                **_decode(row["extras_json"], "port extras"),
            }
            if row["name"] is not None:
                record["name"] = row["name"]
            if row["clock"] is not None:
                record["clock"] = bool(row["clock"])
            if row["reset"] is not None:
                record["reset"] = bool(row["reset"])
            ports.append(record)
        port_constants.finish()

        resources = _RowsForOwner(connection.execute(
            "SELECT * FROM instance_resources ORDER BY instance_seq, resource"
        ), "instance_seq")
        constants = _RowsForOwner(connection.execute(
            "SELECT * FROM instance_constants ORDER BY instance_seq, ordinal"
        ), "instance_seq")
        instances = []
        for row in connection.execute("SELECT * FROM instances ORDER BY seq"):
            record = {
                "id": row["id"],
                "type": row["type"],
                "resources": {
                    child["resource"]: child["amount"]
                    for child in resources.take(row["seq"])
                },
                "constant_connections": [
                    {
                        "port": child["port"], "bit": child["bit"],
                        "value": child["value"],
                        **_decode(child["extras_json"], "instance constant extras"),
                    }
                    for child in constants.take(row["seq"])
                ],
                **_decode(row["extras_json"], "instance extras"),
            }
            if row["name"] is not None:
                record["name"] = row["name"]
            if row["parameters_json"] is not None:
                record["parameters"] = _decode(
                    row["parameters_json"], "instance parameters"
                )
            if row["attributes_json"] is not None:
                record["attributes"] = _decode(
                    row["attributes_json"], "instance attributes"
                )
            instances.append(record)
        resources.finish()
        constants.finish()

        aliases = _RowsForOwner(connection.execute(
            "SELECT * FROM net_aliases ORDER BY net_seq, ordinal"
        ), "net_seq")
        endpoints = _RowsForOwner(connection.execute(
            "SELECT * FROM endpoints ORDER BY net_seq, role, ordinal"
        ), "net_seq")
        nets = []
        for row in connection.execute("SELECT * FROM nets ORDER BY seq"):
            drivers: list[Dict[str, Any]] = []
            sinks: list[Dict[str, Any]] = []
            for child in endpoints.take(row["seq"]):
                if child["role"] not in (0, 1):
                    raise ValueError("EmuIR SQLite invalid endpoint role")
                target = drivers if child["role"] == 0 else sinks
                target.append({
                    "instance": child["instance_id"],
                    "port": child["port"], "bit": child["bit"],
                    **_decode(child["extras_json"], "endpoint extras"),
                })
            record = {
                "id": row["id"],
                "cut_class": row["cut_class"],
                "aliases": [child["alias"] for child in aliases.take(row["seq"])],
                "drivers": drivers,
                "sinks": sinks,
                **_decode(row["extras_json"], "net extras"),
            }
            for field in ("name", "yosys_bit", "bus_index", "fanout"):
                if row[field] is not None:
                    record[field] = row[field]
            nets.append(record)
        aliases.finish()
        endpoints.finish()

        clocks = [
            _decode(row["record_json"], "clock record")
            for row in connection.execute("SELECT record_json FROM clocks ORDER BY seq")
        ]
        return {
            "schema": metadata.get("logical_schema"),
            "design": _decode(metadata["design_json"], "design metadata"),
            "ports": ports,
            "instances": instances,
            "nets": nets,
            "clocks": clocks,
            "warnings": _decode(metadata["warnings_json"], "warnings metadata"),
        }
    finally:
        connection.close()


def read_indexed_emuir_identity(ir_path: Path) -> tuple[str, int, int]:
    """Read the bounded identity/count subset used by source-bound stage validation."""

    connection = open_emuir_sqlite_read_only(ir_path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key, value FROM metadata WHERE key IN (?, ?)",
                ("design_json", "record_counts_json"),
            )
        }
        design = json.loads(metadata["design_json"])
        if connection.execute("PRAGMA user_version").fetchone()[0] == 2:
            counts = json.loads(metadata["record_counts_json"])
            instances = counts["cell_records"]
            nets = counts["net_records"]
        else:
            instances = connection.execute(
                "SELECT COUNT(*) FROM instances"
            ).fetchone()[0]
            nets = connection.execute("SELECT COUNT(*) FROM nets").fetchone()[0]
        if (
            not isinstance(design.get("name"), str)
            or not design["name"]
            or isinstance(instances, bool)
            or not isinstance(instances, int)
            or instances < 0
            or isinstance(nets, bool)
            or not isinstance(nets, int)
            or nets < 0
        ):
            raise ValueError("indexed EmuIR identity is invalid")
        return design["name"], instances, nets
    finally:
        connection.close()
